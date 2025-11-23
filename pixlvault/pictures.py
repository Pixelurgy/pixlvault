import numpy as np
import sqlite3
import time
import os
import cv2
import threading

from concurrent.futures import ThreadPoolExecutor, as_completed

from enum import Enum
from typing import Union, List

import pixlvault.picture_db_tools as db_tools

from pixlvault.logging import get_logger
from pixlvault.picture import PictureModel
from pixlvault.picture_quality import PictureQuality
from pixlvault.picture_tagger import PictureTagger
from pixlvault.picture_utils import PictureUtils
from pixlvault.database import DBPriority
from pixlvault.facial_features_worker import FacialFeaturesWorker  # noqa: F401
from pixlvault.tag_worker import TagWorker  # noqa: F401
from pixlvault.worker_registry import WorkerType, WorkerRegistry

logger = get_logger(__name__)


# Enum for sorting mechanisms
class SortMechanism(str, Enum):
    # If value starts with "ORDER BY", it's SQL-sortable; underscores become spaces for SQL
    DATE_DESC = "ORDER BY created_at DESC"
    DATE_ASC = "ORDER BY created_at ASC"
    SCORE_DESC = "ORDER BY score DESC"
    SCORE_ASC = "ORDER BY score ASC"
    SEARCH_LIKENESS = "search_likeness"
    SHARPNESS_DESC = "ORDER BY sharpness DESC"
    SHARPNESS_ASC = "ORDER BY sharpness ASC"
    EDGE_DENSITY_DESC = "ORDER BY edge_density DESC"
    EDGE_DENSITY_ASC = "ORDER BY edge_density ASC"
    NOISE_LEVEL_DESC = "ORDER BY noise_level DESC"
    NOISE_LEVEL_ASC = "ORDER BY noise_level ASC"
    HAS_DESCRIPTION = "ORDER BY description IS NOT NULL DESC"
    NO_DESCRIPTION = "ORDER BY escription IS NULL DESC"
    FORMAT_ASC = "ORDER BY format ASC"
    FORMAT_DESC = "ORDER BY format DESC"

    @classmethod
    def is_sql_sortable(cls, sort):
        return sort and str(sort).startswith("ORDER BY")


# List of available sorting mechanisms for API
def get_sort_mechanisms():
    """Return a list of available sort mechanisms as dicts for API consumption."""
    return [
        {"id": sm.value, "label": label}
        for sm, label in [
            (SortMechanism.DATE_DESC, "Date (latest first)"),
            (SortMechanism.DATE_ASC, "Date (oldest first)"),
            (SortMechanism.SCORE_DESC, "Score (highest first)"),
            (SortMechanism.SCORE_ASC, "Score (lowest first)"),
            (SortMechanism.SEARCH_LIKENESS, "Sort by search likeness"),
            (SortMechanism.SHARPNESS_DESC, "Sharpness (highest first)"),
            (SortMechanism.SHARPNESS_ASC, "Sharpness (lowest first)"),
            (SortMechanism.EDGE_DENSITY_DESC, "Edge Density (highest first)"),
            (SortMechanism.EDGE_DENSITY_ASC, "Edge Density (lowest first)"),
            (SortMechanism.NOISE_LEVEL_DESC, "Noise Level (highest first)"),
            (SortMechanism.NOISE_LEVEL_ASC, "Noise Level (lowest first)"),
            (SortMechanism.HAS_DESCRIPTION, "Has Description"),
            (SortMechanism.NO_DESCRIPTION, "No Description"),
            (SortMechanism.FORMAT_ASC, "Format (A-Z)"),
            (SortMechanism.FORMAT_DESC, "Format (Z-A)"),
        ]
    ]


class Pictures:
    NUM_LIKENESS_THREADS = 4

    def __init__(self, db, characters=None, device=None):
        self._db = db
        self._skip_pictures = set()
        self._characters = characters  # Should be a Characters manager or None
        # Pass device to PictureTagger (default: None lets PictureTagger auto-detect)
        self._device = device
        self._picture_tagger = PictureTagger(device=device)
        logger.info(
            f"Initialized PictureTagger for Pictures manager with device={device!r}."
        )

        self._workers = {}

        for worker_type in WorkerType.all():
            self._workers[worker_type] = WorkerRegistry.create_worker(
                worker_type, self._db, self._picture_tagger, self._characters
            )

        self._quality_worker = None
        self._quality_worker_stop = None

        self._likeness_worker = None
        self._likeness_worker_stop = None

    def __getitem__(self, picture_id):
        logger.debug(f"Fetching picture with id={picture_id} (type={type(picture_id)})")

        def get_pictures(conn, picture_id):
            picture_row = conn.execute(
                "SELECT * FROM pictures WHERE id = ?", picture_id
            ).fetchone()
            if not picture_row:
                raise KeyError(f"Picture with id {picture_id} not found.")
            pic = PictureModel.from_dict(picture_row)
            tags_rows = conn.execute(
                "SELECT tag FROM picture_tags WHERE picture_id = ?", picture_id
            )
            pic.tags = [tag_row["tag"] for tag_row in tags_rows]
            return pic

        return self._db.execute_read(get_pictures, (picture_id,))

    def __setitem__(self, picture_id, picture):
        picture.id = picture_id
        self.import_picture(picture)

    def __delitem__(self, picture_id):
        self._db.submit_write(
            lambda conn: conn.execute(
                "DELETE FROM picture_tags WHERE picture_id = ?", (picture_id,)
            ),
            priority=DBPriority.IMMEDIATE,
        ).result()

    def __iter__(self):
        def row_generator(conn):
            cursor = conn.execute("SELECT * FROM pictures")
            picture_rows = list(cursor)
            if not picture_rows:
                return
            # Fetch all tags for these pictures in one query
            pic_ids = [row["id"] for row in picture_rows]
            placeholders = ",".join(["?"] * len(pic_ids))
            tag_cursor = conn.execute(
                f"SELECT picture_id, tag FROM picture_tags WHERE picture_id IN ({placeholders})",
                pic_ids,
            )
            tag_map = {}
            for tag_row in tag_cursor:
                tag_map.setdefault(tag_row["picture_id"], []).append(tag_row["tag"])
            for row in picture_rows:
                pic = PictureModel.from_dict(row)
                pic.tags = tag_map.get(row["id"], [])
                yield pic

        yield from self._db.execute_read(row_generator)

    def start_worker(self, worker: WorkerType):
        if worker in self._workers:
            self._workers[worker].start()
        elif worker == WorkerType.QUALITY:
            self._start_quality_worker()
        elif worker == WorkerType.LIKENESS:
            self._start_likeness_worker()
        else:
            raise ValueError(f"Unknown worker type: {worker}")

    def stop_worker(self, worker: WorkerType):
        if worker in self._workers:
            self._workers[worker].stop()
        elif worker == WorkerType.QUALITY:
            self._stop_quality_worker()
        elif worker == WorkerType.LIKENESS:
            self._stop_likeness_worker()
        else:
            raise ValueError(f"Unknown worker type: {worker}")

    def _start_likeness_worker(self, batch_size=200000, interval=5):
        if (
            hasattr(self, "_likeness_worker")
            and self._likeness_worker
            and self._likeness_worker.is_alive()
        ):
            logger.info("Likeness worker already running.")
            return
        self._likeness_worker_stop = threading.Event()
        self._likeness_worker = threading.Thread(
            target=self._likeness_loop, args=(batch_size, interval), daemon=True
        )
        self._likeness_worker.start()

    def _stop_likeness_worker(self):
        if hasattr(self, "_likeness_worker_stop") and self._likeness_worker_stop:
            self._likeness_worker_stop.set()
        if hasattr(self, "_likeness_worker") and self._likeness_worker:
            self._likeness_worker.join(timeout=10)

    def _likeness_loop(self, batch_size, interval):
        while not self._likeness_worker_stop.is_set():
            data_updated = False
            likeness_score_count = 0
            start = time.time()
            logger.debug("[LIKENESS] Starting iteration...")
            pending_pairs = []

            total_pending = self._db.execute_read(
                lambda conn: conn.execute(
                    "SELECT COUNT(*) FROM likeness_work_queue"
                ).fetchone()
            )[0]
            logger.info(
                "Got %d pending likeness pairs to process from work queue."
                % (total_pending)
            )
            if total_pending == 0:
                logger.info(
                    "[LIKENESS] No pending pairs, sleeping and skipping any deletion..."
                )
                time.sleep(interval)
                continue

            self._db.submit_write(
                lambda conn: conn.execute(
                    """
                DELETE FROM likeness_work_queue
                WHERE EXISTS (
                    SELECT 1 FROM picture_likeness
                    WHERE picture_likeness.picture_id_a = likeness_work_queue.picture_id_a
                        AND picture_likeness.picture_id_b = likeness_work_queue.picture_id_b
                )
            """
                ),
                priority=DBPriority.LOW,
            ).result()
            time_after_cleanup = time.time()
            logger.debug(
                f"[LIKENESS] DELETING existing items from likeness_work_queue took {time_after_cleanup - start:.2f} seconds."
            )

            rows = self._db.execute_read(
                lambda conn: conn.execute(
                    "SELECT picture_id_a, picture_id_b FROM likeness_work_queue ORDER BY rowid LIMIT ?",
                    (batch_size,),
                ).fetchall()
            )

            total_pending = self._db.execute_read(
                lambda conn: conn.execute(
                    "SELECT COUNT(*) FROM likeness_work_queue"
                ).fetchone()
            )[0]
            logger.info(
                f"[LIKENESS] Fetched {len(rows)} rows from likeness_work_queue out of {total_pending}."
            )
            # Batch fetch all required pictures
            all_ids = set()
            for row in rows:
                all_ids.add(row[0])
                all_ids.add(row[1])
            if all_ids:
                placeholders = ",".join(["?"] * len(all_ids))
                pic_rows = self._db.execute_read(
                    lambda conn: conn.execute(
                        f"SELECT * FROM pictures WHERE id IN ({placeholders})",
                        tuple(all_ids),
                    ).fetchall()
                )
                logger.info(
                    "Got %d pictures for likeness calculation." % (len(pic_rows))
                )
                pic_dict = {
                    row["id"]
                    if isinstance(row, dict)
                    else row[0]: PictureModel.from_dict(row)
                    for row in pic_rows
                }
                for row in rows:
                    pic_a_id, pic_b_id = row[0], row[1]
                    pic_a = pic_dict.get(pic_a_id)
                    pic_b = pic_dict.get(pic_b_id)
                    if not pic_a or not pic_b:
                        continue
                    # Only process if both have facial features
                    if not pic_a.facial_features:
                        logger.warning(
                            f"[LIKENESS] Picture id={pic_a_id} missing facial features, skipping pair."
                        )
                    if not pic_b.facial_features:
                        logger.warning(
                            f"[LIKENESS] Picture id={pic_b_id} missing facial features, skipping pair."
                        )
                    if not pic_a.facial_features or not pic_b.facial_features:
                        continue
                    pending_pairs.append((pic_a_id, pic_b_id, pic_a, pic_b))

            logger.info(
                "[LIKENESS] Got %d pending likeness pairs to process from work queue."
                % (len(pending_pairs))
            )
            if pending_pairs:
                batches = [
                    pending_pairs[i * batch_size : (i + 1) * batch_size]
                    for i in range(
                        min(
                            Pictures.NUM_LIKENESS_THREADS,
                            (len(pending_pairs) + batch_size - 1) // batch_size,
                        )
                    )
                ]

                def process_batch(batch):
                    pic_a_list = [item[2] for item in batch]
                    pic_b_list = [item[3] for item in batch]
                    features_a = [
                        np.frombuffer(pic.facial_features, dtype=np.float32)
                        for pic in pic_a_list
                    ]
                    features_b = [
                        np.frombuffer(pic.facial_features, dtype=np.float32)
                        for pic in pic_b_list
                    ]
                    likeness_values = PictureQuality.batch_likeness_scores(
                        features_a, features_b
                    )
                    likeness_scores = [
                        (item[0], item[1], float(likeness), "cosine")
                        for item, likeness in zip(batch, likeness_values)
                    ]
                    # Remove processed pairs from work queue
                    queue_pairs = [(item[0], item[1]) for item in batch]
                    return likeness_scores, queue_pairs

                # Run batches in thread pool and join
                processed_total = 0
                all_likeness_scores = []
                all_processed_pairs = []
                with ThreadPoolExecutor(max_workers=len(batches)) as executor:
                    futures = [
                        executor.submit(process_batch, batch)
                        for batch in batches
                        if batch
                    ]
                    for future in as_completed(futures):
                        batch_scores, processed_pairs = future.result()
                        all_likeness_scores.extend(batch_scores)
                        all_processed_pairs.extend(processed_pairs)
                        processed_total += len(batch_scores)

                logger.debug(
                    f"[LIKENESS] Processed {processed_total} likeness scores in this iteration."
                )
                # Bulk insert all likeness scores and remove processed pairs
                if all_likeness_scores:

                    def insert_likeness_scores(conn, all_likeness_scores):
                        cursor = conn.cursor()
                        cursor.executemany(
                            """
                            INSERT OR IGNORE INTO picture_likeness (
                                picture_id_a, picture_id_b, likeness, metric
                            ) VALUES (?, ?, ?, ?)
                            """,
                            all_likeness_scores,
                        )
                        # Remove processed pairs from work queue
                        cursor.executemany(
                            "DELETE FROM likeness_work_queue WHERE picture_id_a = ? AND picture_id_b = ?",
                            all_processed_pairs,
                        )
                        conn.commit()

                    self._db.submit_write(
                        insert_likeness_scores,
                        all_likeness_scores,
                        priority=DBPriority.LOW,
                    )
                    logger.debug(
                        f"[LIKENESS] Bulk inserted {len(all_likeness_scores)} likeness scores and removed {len(all_processed_pairs)} processed pairs from work queue."
                    )
                    likeness_score_count = len(all_likeness_scores)
                    data_updated = True

            timing = time.time() - start
            if timing > 0.5:
                logger.info(
                    "[LIKENESS] Calculated and updated %d likeness scores in %.2f seconds."
                    % (likeness_score_count, time.time() - start)
                )
            if not data_updated:
                self._likeness_worker_stop.wait(interval)
        logger.info("[LIKENESS] Likeness worker stopped.")

    def _quality_worker_loop(self, interval):
        # ...existing code...
        # Efficiently group pictures by size before batch quality calculation
        # (inside the main loop, after fetching pics)
        BATCH_SIZE = 8
        while not self._quality_worker_stop.is_set():
            start = time.time()
            logger.debug("[QUALITY] Starting iteration...")
            quality_updates = 0
            try:
                start_quality_fetch = time.time()
                # 1. Full image quality measures
                logger.debug(
                    "Searching for pictures needing full image quality calculation."
                )
                rows = self._db.execute_read(
                    lambda conn: conn.execute(
                        "SELECT * FROM pictures WHERE sharpness IS NULL OR edge_density IS NULL OR noise_level IS NULL OR contrast IS NULL OR brightness IS NULL",
                    ).fetchall()
                )

                pics_full = db_tools.from_batch_of_db_dicts(rows)

                start_quality_grouping = time.time()
                logger.debug(
                    "[QUALITY] It took %.2f seconds to fetch pictures needing quality calculation."
                    % (start_quality_grouping - start_quality_fetch)
                )
                logger.debug(
                    f"Read metadata for {len(pics_full)} pictures needing full image quality."
                )
                grouped_full = self._group_pictures_by_size(pics_full, region="full")
                logger.debug(
                    f"Grouped {len(grouped_full)} full image batches by size. Will calculate quality now."
                )
                start_quality_calculation = time.time()
                logger.debug(
                    "[QUALITY] It took %.2f seconds to group pictures by size."
                    % (start_quality_calculation - start_quality_grouping)
                )

                for group in grouped_full.values():
                    batch = group[:BATCH_SIZE]
                    if batch:
                        size = PictureUtils.load_metadata(batch[0].file_path)
                        logger.debug(
                            f"Processing batch of {len(batch)} images of size {size} out of a total of {len(group)}."
                        )
                        quality_updates = self._calculate_quality(batch)
                        if quality_updates:
                            self._db.submit_write(
                                self._update_quality,
                                quality_updates,
                                priority=DBPriority.LOW,
                            ).result()
                start_face_quality_fetch = time.time()
                logger.debug(
                    "[QUALITY] It took %.2f seconds to calculate full image quality."
                    % (start_face_quality_fetch - start_quality_calculation)
                )
                # 2. Face quality measures
                logger.debug("Searching for pictures needing face quality calculation.")
                face_rows = self._db.execute_read(
                    lambda conn: conn.execute(
                        "SELECT * FROM pictures WHERE face_sharpness IS NULL OR face_edge_density IS NULL OR face_noise_level IS NULL OR face_contrast IS NULL OR face_brightness IS NULL",
                    ).fetchall()
                )
                pics_face = db_tools.from_batch_of_db_dicts(face_rows)

                logger.debug(
                    "[QUALITY] It took %.2f seconds to fetch pictures needing face quality calculation."
                    % (time.time() - start_face_quality_fetch)
                )
                logger.debug(
                    f"Read metadata for {len(pics_face)} pictures needing face quality."
                )
                grouped_face = self._group_pictures_by_size(pics_face, region="face")
                logger.debug(
                    f"Grouped {len(grouped_face)} face crop batches by bbox size. Will calculate face quality now."
                )
                for group in grouped_face.values():
                    batch = group[:BATCH_SIZE]
                    if batch:
                        bbox_size = None
                        if (
                            batch[0].face_bbox is not None
                            and len(batch[0].face_bbox) == 4
                        ):
                            x1, y1, x2, y2 = batch[0].face_bbox
                            w = int(round((x2 - x1) / 64.0) * 64)
                            h = int(round((y2 - y1) / 64.0) * 64)
                            bbox_size = (h, w, 3)
                        logger.debug(
                            f"Processing face batch of {len(batch)} images of bbox size {bbox_size}."
                        )
                        quality_updates = self._calculate_quality(batch, True)
                        self._db.submit_write(
                            self._update_face_quality,
                            quality_updates,
                            priority=DBPriority.LOW,
                        ).result()

            except (sqlite3.OperationalError, OSError) as e:
                msg = str(e)
                if "no such column" in msg:
                    logger.error(
                        f"Quality worker exiting due to schema error (missing column): {e}"
                    )
                else:
                    logger.error(
                        f"Quality worker exiting due to DB error (likely shutdown): {e}"
                    )
                break
            except Exception as e:
                logger.error(f"Quality worker error: {e}")

            timing = time.time() - start
            if timing > 0.5:
                logger.info("[QUALITY] Done after %.2f seconds." % timing)
            if quality_updates == 0:
                self._quality_worker_stop.wait(interval)

    def _group_pictures_by_size(self, pics: List[PictureModel], region: str = "full"):
        """
        Group pictures by region size (full image or face bbox).
        region: "full" for full image, "face" for face bbox.
        Returns a dict: {key: [PictureModel, ...]}, where key is (h, w, c) for images, or (h, w) for face crops, or file_path for videos.
        """
        from collections import defaultdict

        video_exts = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".wmv"}
        sizes = []
        pic_groups = []
        video_groups = defaultdict(list)
        for pic in pics:
            ext = os.path.splitext(pic.file_path)[1].lower()
            if ext in video_exts:
                video_groups[pic.file_path].append(pic)
            else:
                try:
                    if region == "full":
                        size = PictureUtils.load_metadata(pic.file_path)
                    elif (
                        region == "face"
                        and pic.face_bbox is not None
                        and len(pic.face_bbox) == 4
                    ):
                        x1, y1, x2, y2 = pic.face_bbox
                        w = int(round((x2 - x1) / 64.0) * 64)
                        h = int(round((y2 - y1) / 64.0) * 64)
                        # Assume 3 channels for face crops
                        size = (h, w, 3)
                    else:
                        size = None
                    if size:
                        sizes.append(size)
                        pic_groups.append((size, pic))
                except Exception as e:
                    logger.error(
                        f"Failed to read metadata for grouping: {pic.file_path}, error: {e}"
                    )
        all_groups = dict(video_groups)
        if sizes:
            unique_sizes = set(sizes)
            logger.debug(
                f"Read metadata for {len(pic_groups)} images. Found {len(unique_sizes)} unique sizes."
            )
            if len(unique_sizes) == 1:
                batch_size = len(pic_groups)
                logger.debug(
                    f"All images same size: batching {batch_size} images together."
                )
                all_groups[list(unique_sizes)[0]] = [pic for _, pic in pic_groups]
            else:
                image_groups = defaultdict(list)
                for size, pic in pic_groups:
                    image_groups[size].append(pic)
                for size, group in image_groups.items():
                    if region == "face":
                        logger.debug(
                            f"Face batch for bbox size {size}: {len(group)} images."
                        )
                    else:
                        logger.debug(f"Batch for size {size}: {len(group)} images.")
                all_groups.update(image_groups)
        if video_groups:
            for vkey, vgroup in video_groups.items():
                logger.debug(f"Video batch for {vkey}: {len(vgroup)} video(s).")
        return all_groups

    def _calculate_quality(
        self, pics: List[PictureModel], is_face: bool = False
    ) -> List[PictureModel]:
        try:
            images = []
            if not is_face:
                # Full image quality
                for pic in pics:
                    images.append(PictureUtils.load_image_or_video(pic.file_path))
                batch_array = np.stack(images, axis=0)
                qualities = PictureQuality.calculate_quality_batch(batch_array)
                for pic, q in zip(pics, qualities):
                    logger.debug(
                        "[QUALITY] Picture id %s calculated sharpness: %s edge_density: %s contrast: %s brightness: %s noise_level: %s",
                        pic.id,
                        q.sharpness,
                        q.edge_density,
                        q.contrast,
                        q.brightness,
                        q.noise_level,
                    )
                    pic.sharpness = q.sharpness
                    pic.edge_density = q.edge_density
                    pic.contrast = q.contrast
                    pic.brightness = q.brightness
                    pic.noise_level = q.noise_level
            else:
                # Face crop quality
                h, w, _ = None, None, None
                if (
                    pics
                    and pics[0].face_bbox is not None
                    and len(pics[0].face_bbox) == 4
                ):
                    x1, y1, x2, y2 = pics[0].face_bbox
                    w = int(round((x2 - x1) / 64.0) * 64)
                    h = int(round((y2 - y1) / 64.0) * 64)
                valid_images = []
                valid_pics = []
                for pic in pics:
                    # Only check if bbox is present and well-formed
                    if pic.face_bbox is not None and len(pic.face_bbox) == 4:
                        x1, y1, x2, y2 = pic.face_bbox
                        # Check for zero area or malformed bbox
                        if x2 > x1 and y2 > y1:
                            img = PictureUtils.load_image_or_video(pic.file_path)
                            if img is not None:
                                crop = img[int(y1) : int(y2), int(x1) : int(x2)]
                                if (
                                    h is not None
                                    and w is not None
                                    and (crop.shape[0] != h or crop.shape[1] != w)
                                ):
                                    try:
                                        crop = cv2.resize(crop, (w, h))
                                    except Exception:
                                        # Silently drop this bbox and skip picture
                                        pic.face_bbox = None
                                        continue
                                if crop.size > 0:
                                    valid_images.append(crop)
                                    valid_pics.append(pic)
                                    continue
                        # If bbox is present but invalid, set metrics to -1.0
                        pic.face_sharpness = -1.0
                        pic.face_edge_density = -1.0
                        pic.face_noise_level = -1.0
                        pic.face_contrast = -1.0
                        pic.face_brightness = -1.0
                if valid_images:
                    batch_array = np.stack(valid_images, axis=0)
                    qualities = PictureQuality.calculate_quality_batch(batch_array)
                    for pic, fq in zip(valid_pics, qualities):
                        pic.face_sharpness = fq.sharpness
                        pic.face_edge_density = fq.edge_density
                        pic.face_contrast = fq.contrast
                        pic.face_brightness = fq.brightness
                        pic.face_noise_level = fq.noise_level
            return pics
        except Exception as e:
            logger.error(f"Failed to calculate quality for batch: {e}")

    def _update_quality(self, conn, pics):
        cursor = conn.cursor()
        values = []
        for pic in pics:
            # Assert metrics are not bytes
            for metric_name in [
                "sharpness",
                "edge_density",
                "contrast",
                "brightness",
                "noise_level",
            ]:
                metric_value = getattr(pic, metric_name)
                assert not isinstance(metric_value, bytes), (
                    f"Corrupt metric: {metric_name} for picture {pic.id} is bytes: {metric_value!r}"
                )
            logger.debug(
                "[UPDATE] Picture id %s sharpness: %s edge_density: %s contrast: %s brightness: %s noise_level: %s",
                pic.id,
                pic.sharpness,
                pic.edge_density,
                pic.contrast,
                pic.brightness,
                pic.noise_level,
            )
            values.append(
                (
                    pic.sharpness,
                    pic.edge_density,
                    pic.contrast,
                    pic.brightness,
                    pic.noise_level,
                    pic.id,
                )
            )

        logger.debug(
            f"[UPDATE] Committing {len(values)} full image quality updates to database."
        )
        cursor.executemany(
            "UPDATE pictures SET sharpness = ?, edge_density = ?, contrast = ?, brightness = ?, noise_level = ? WHERE id = ?",
            values,
        )
        conn.commit()

    def _update_face_quality(self, conn, pics):
        cursor = conn.cursor()
        values = []
        for pic in pics:
            # Assert face metrics are not bytes
            for metric_name in [
                "face_sharpness",
                "face_edge_density",
                "face_contrast",
                "face_brightness",
                "face_noise_level",
            ]:
                metric_value = getattr(pic, metric_name)
                assert not isinstance(metric_value, bytes), (
                    f"Corrupt face metric: {metric_name} for picture {pic.id} is bytes: {metric_value!r}"
                )
            logger.debug(
                "[UPDATE] Picture id %s face_sharpness: %s face_edge_density: %s face_contrast: %s face_brightness: %s face_noise_level: %s",
                pic.id,
                pic.face_sharpness,
                pic.face_edge_density,
                pic.face_contrast,
                pic.face_brightness,
                pic.face_noise_level,
            )
            values.append(
                (
                    pic.face_sharpness,
                    pic.face_edge_density,
                    pic.face_contrast,
                    pic.face_brightness,
                    pic.face_noise_level,
                    pic.id,
                )
            )

        logger.debug(
            f"[UPDATE] Committing {len(values)} face quality updates to database."
        )
        cursor.executemany(
            "UPDATE pictures SET face_sharpness = ?, face_edge_density = ?, face_contrast = ?, face_brightness = ?, face_noise_level = ? WHERE id = ?",
            values,
        )
        conn.commit()

    def find(self, **kwargs):
        """
        Find and return a list of Picture objects matching all provided attribute=value pairs.
        Example: pictures.find(primary_character_id="hero")
        Special case: if a value is an empty string, search for IS NULL.
        Uses VaultDatabase for all DB access.
        """
        sort = kwargs.pop("sort", None)
        offset = kwargs.pop("offset", None)
        limit = kwargs.pop("limit", None)
        info = kwargs.pop("info", False)
        count = kwargs.pop("count", False)
        if count:
            # Return count of matching pictures
            clauses = []
            values = []
            for k, v in kwargs.items():
                if v == "" or v == "null":
                    clauses.append(f"{k} IS NULL")
                else:
                    clauses.append(f"{k}=?")
                    values.append(v)
            where_clause = ""
            if clauses:
                where_clause = "WHERE " + " AND ".join(clauses)
            query = f"SELECT COUNT(*) FROM pictures {where_clause}".strip()
            rows = self._db.execute_read(
                lambda conn: conn.execute(query, tuple(values)).fetchall()
            )
            if rows:
                return rows[0][0]
            else:
                return 0

        order_by = ""
        if SortMechanism.is_sql_sortable(sort):
            order_by = sort
        clauses = []
        values = []
        for k, v in kwargs.items():
            if v == "" or v == "null":
                clauses.append(f"{k} IS NULL")
            else:
                clauses.append(f"{k}=?")
                values.append(v)
        where_clause = ""
        if clauses:
            where_clause = "WHERE " + " AND ".join(clauses)
        if info:
            fields = PictureModel.metadata()
            select_fields = ", ".join(fields)
            query = f"SELECT {select_fields} FROM pictures {where_clause} {order_by}".strip()
        else:
            query = f"SELECT * FROM pictures {where_clause} {order_by}".strip()
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        if offset is not None:
            query += f" OFFSET {int(offset)}"
        rows = self._db.execute_read(
            lambda conn: conn.execute(query, tuple(values)).fetchall()
        )
        result = []
        for row in rows:
            pic = PictureModel.from_dict(row)
            if not info:
                tag_rows = self._db.execute_read(
                    lambda conn: conn.execute(
                        "SELECT tag FROM picture_tags WHERE picture_id = ?", (pic.id,)
                    ).fetchall()
                )
                pic.tags = [
                    tag_row["tag"] if isinstance(tag_row, dict) else tag_row[0]
                    for tag_row in tag_rows
                ]
            result.append(pic)
        return result

    def find_by_text(
        self, text, top_n=5, include_scores=False, threshold=0.5, count=False
    ):
        """
        Find the top N pictures whose embeddings best match the input text.
        Returns a list of Picture objects (and optionally similarity scores).
        If the input text is empty, returns an empty list.
        Adds debug logging for diagnosis.
        """
        if not text or not str(text).strip():
            logger.warning(
                "find_by_text called with empty text; returning empty result."
            )
            return []
        # Generate query embedding
        (
            query_emb,
            _,
        ) = self._picture_tagger.generate_text_embedding(picture={"description": text})
        logger.debug(
            f"Semantic search: query embedding shape: {getattr(query_emb, 'shape', None)}"
        )
        # Load all picture embeddings and ids
        rows = self._db.execute_read(
            lambda conn: conn.execute(
                "SELECT id, text_embedding FROM pictures WHERE text_embedding IS NOT NULL"
            ).fetchall()
        )
        logger.debug(
            f"Semantic search: found {len(rows)} candidate images with embeddings."
        )
        if not rows:
            return []
        # Compute similarities

        sims = []
        for row in rows:
            pic_id = row["id"] if isinstance(row, dict) else row[0]
            emb_blob = row["text_embedding"] if isinstance(row, dict) else row[1]
            if emb_blob is None:
                continue

            # Embedding is stored as base64 string in DB (from to_dict())
            # Decode it to bytes for numpy
            try:
                import base64

                if isinstance(emb_blob, str):
                    emb_bytes = base64.b64decode(emb_blob)
                else:
                    # Already bytes (shouldn't happen with consistent to_dict usage)
                    emb_bytes = emb_blob

                emb = np.frombuffer(emb_bytes, dtype=np.float32)
            except Exception as e:
                logger.error(f"Failed to parse embedding for {pic_id}: {e}")
                continue
            sim = float(
                np.dot(query_emb, emb)
                / (np.linalg.norm(query_emb) * np.linalg.norm(emb) + 1e-8)
            )
            logger.debug(f"Semantic search: similarity for {pic_id}: {sim}")
            if sim >= threshold:
                sims.append((pic_id, sim))
        # Sort by similarity, descending
        sims.sort(key=lambda x: x[1], reverse=True)
        top = sims[:top_n]
        logger.debug(
            f"Semantic search: top {top_n} results above threshold {threshold}: {top}"
        )
        # Fetch Picture objects
        results = []
        for pic_id, sim in top:
            pic = self[pic_id]
            if include_scores:
                results.append((pic, sim))
            else:
                results.append(pic)

        if count:
            return len(results)
        return results

    def _start_quality_worker(self, interval=5):
        if self._quality_worker and self._quality_worker.is_alive():
            return
        self._quality_worker_stop = threading.Event()
        self._quality_worker = threading.Thread(
            target=self._quality_worker_loop, args=(interval,), daemon=True
        )
        self._quality_worker.start()

    def _stop_quality_worker(self):
        logger.debug("Stopping quality worker...")
        if self._quality_worker_stop:
            self._quality_worker_stop.set()
        if self._quality_worker:
            self._quality_worker.join(timeout=10)  # Wait for thread to exit
            if self._quality_worker.is_alive():
                logger.warning("Quality worker thread did not exit within timeout.")

    def delete(self, picture_ids: Union[str, List[str]]):
        """Delete one or more pictures. Supports both single ID and batch operations."""
        if not isinstance(picture_ids, list):
            picture_ids = [picture_ids]

        def delete_pictures(conn, picture_ids=picture_ids):
            cursor = conn.cursor()
            cursor.executemany(
                "DELETE FROM pictures WHERE id = ?", [(pid,) for pid in picture_ids]
            )
            cursor.executemany(
                "DELETE FROM picture_tags WHERE picture_id = ?",
                [(pid,) for pid in picture_ids],
            )
            # Also delete from picture_likeness where either side matches
            cursor.executemany(
                "DELETE FROM picture_likeness WHERE picture_id_a = ? OR picture_id_b = ?",
                [(pid, pid) for pid in picture_ids],
            )
            conn.commit()

        self._db.submit_write(delete_pictures, picture_ids).result()

    def likeness_query(self, treshold: float):
        """Return pairs of picture IDs with a likeness score above threshold."""
        rows = self._db.execute_read(
            lambda conn: conn.execute(
                "SELECT picture_id_a, picture_id_b, likeness FROM picture_likeness WHERE likeness >= ?",
                (treshold,),
            ).fetchall()
        )
        result = []
        for row in rows:
            result.append((row["picture_id_a"], row["picture_id_b"], row["likeness"]))
        return result

    def add(self, pictures: Union[PictureModel, List[PictureModel]]):
        """Add one or more pictures. Supports both single picture and batch operations."""
        if not isinstance(pictures, list):
            pictures = [pictures]

        # Batch insert
        picture_dicts, list_of_tag_dicts = db_tools.to_batch_of_db_dicts(pictures)
        new_picture_ids = []

        def insert_pictures_and_tags(conn, picture_dicts, list_of_tag_dicts):
            cursor = conn.cursor()
            # Insert pictures
            new_ids = []
            for pic_dict, tag_dicts in zip(picture_dicts, list_of_tag_dicts):
                logger.debug(f"Inserting picture: {pic_dict}")

                try:
                    cursor.execute(
                        f"INSERT INTO pictures ({', '.join(pic_dict.keys())}) VALUES ({', '.join(['?' for _ in pic_dict.keys()])})",
                        tuple(pic_dict.values()),
                    )
                    new_ids.append(pic_dict["id"])

                except Exception as e:
                    logger.error(f"Failed to insert picture {pic_dict}: {e}")

                for tag_dict in tag_dicts:
                    cursor.executemany(
                        "INSERT INTO picture_tags (picture_id, tag) VALUES (?, ?)",
                        [
                            (tag_dict["picture_id"], tag_dict["tag"])
                            for tag_dict in tag_dict
                        ],
                    )
            conn.commit()

        new_picture_ids = self._db.submit_write(
            lambda conn: insert_pictures_and_tags(
                conn, picture_dicts, list_of_tag_dicts
            ),
            priority=DBPriority.IMMEDIATE,
        ).result()
        if new_picture_ids:
            # Get all existing picture IDs (excluding new ones)
            def append_work_queue(conn, new_picture_ids=new_picture_ids):
                cursor = conn.cursor()
                rows = cursor.execute(
                    f"SELECT id FROM pictures WHERE id NOT IN ({','.join(['?'] * len(new_picture_ids))})",
                    tuple(new_picture_ids),
                ).fetchall()
                existing_ids = [row["id"] for row in rows]
                # Prepare all pairs (new_id, existing_id) and (existing_id, new_id)
                queue_pairs = []
                for new_id in new_picture_ids:
                    for existing_id in existing_ids:
                        queue_pairs.append(
                            (min(new_id, existing_id), max(new_id, existing_id))
                        )
                if queue_pairs:
                    cursor.executemany(
                        "INSERT OR IGNORE INTO likeness_work_queue (picture_id_a, picture_id_b) VALUES (?, ?)",
                        queue_pairs,
                    )
                    conn.commit()

            self._db.submit_write(
                append_work_queue, new_picture_ids, priority=DBPriority.LOW
            ).result()
        return new_picture_ids

    def update(self, pictures: Union[PictureModel, List[PictureModel]]):
        """Update one or more pictures. Supports both single picture and batch operations."""
        if not isinstance(pictures, list):
            pictures = [pictures]

        picture_dicts, list_of_tag_dicts = db_tools.to_batch_of_db_dicts(pictures)

        def update_picture_and_tag(conn, pic_dict, tag_dicts):
            set_clause = ", ".join([f"{k}=?" for k in pic_dict.keys()])
            sql = f"UPDATE pictures SET {set_clause} WHERE id = ?"
            values = list(pic_dict.values()) + [pic_dict["id"]]
            cursor = conn.cursor()
            cursor.execute(sql, tuple(values))

            # Update tags in picture_tags table
            if tag_dicts:
                cursor.execute(
                    "DELETE FROM picture_tags WHERE picture_id = ?",
                    (pic_dict["id"],),
                )
                cursor.executemany(
                    "INSERT INTO picture_tags (picture_id, tag) VALUES (?, ?)",
                    [
                        (tag_dict["picture_id"], tag_dict["tag"])
                        for tag_dict in tag_dicts
                    ],
                )
            conn.commit()

        for pic_dict, tag_dicts in zip(picture_dicts, list_of_tag_dicts):
            self._db.submit_write(update_picture_and_tag, pic_dict, tag_dicts)

    def fetch_by_shas(self, shas: list[str]) -> list[PictureModel]:
        if not shas:
            return []

        placeholders = ",".join(["?"] * len(shas))
        sql = f"SELECT id FROM pictures WHERE pixel_sha IN ({placeholders})"
        ids = self._db.execute_read(
            lambda conn: conn.execute(sql, tuple(shas)).fetchall()
        )

        pics = []
        for id in ids:
            pic = self[id["id"]]
            if pic is not None:
                pics.append(pic)
        return pics

from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np
import time
from sqlmodel import select

from pixlvault.logging import get_logger
from pixlvault.worker_registry import BaseWorker, WorkerType
from pixlvault.db_models.face import Face
from pixlvault.db_models.face_likeness_work_queue import FaceLikenessWorkQueue
from pixlvault.db_models.face_likeness import FaceLikeness

logger = get_logger(__name__)

class FaceLikenessWorker(BaseWorker):
    BATCH_SIZE = 5000
    NUM_THREADS = 4
    INTERVAL = 5

    def worker_type(self) -> WorkerType:
        return WorkerType.FACE_LIKENESS

    def _run(self):
        while not self._stop.is_set():
            start = time.time()

            # 1. Fetch pending pairs from the work queue
            def fetch_pending_pairs(session):
                pairs = session.exec(
                    select(FaceLikenessWorkQueue).limit(self.BATCH_SIZE)
                ).all()
                face_ids = set()
                for pair in pairs:
                    face_ids.add(pair.face_id_a)
                    face_ids.add(pair.face_id_b)
                faces = session.exec(
                    select(Face).where(Face.id.in_(face_ids))
                ).all()
                face_dict = {face.id: face for face in faces}
                return pairs, face_dict

            pending_pairs, face_dict = self._db.run_task(fetch_pending_pairs)
            if not pending_pairs:
                logger.debug("FaceLikenessWorker: No pending pairs. Sleeping...")
                self._stop.wait(self.INTERVAL)
                continue

            logger.info(f"FaceLikenessWorker: Processing {len(pending_pairs)} pairs.")

            # 2. Do likeness computation outside the session
            likeness_results = []
            to_remove = []
            processed_notify_ids = []
            for pair in pending_pairs:
                face_a = face_dict.get(pair.face_id_a)
                face_b = face_dict.get(pair.face_id_b)
                if not face_a or not face_b:
                    logger.warning(
                        f"Skipping pair ({pair.face_id_a}, {pair.face_id_b}): missing face(s). Removing from queue."
                    )
                    to_remove.append(pair)
                    continue
                features_a = face_a.features if hasattr(face_a, "features") else None
                features_b = face_b.features if hasattr(face_b, "features") else None
                if features_a is None or features_b is None:
                    logger.warning(
                        f"Skipping pair ({pair.face_id_a}, {pair.face_id_b}): missing facial features. Removing from queue."
                    )
                    to_remove.append(pair)
                    continue
                likeness = self._cosine_similarity(features_a, features_b)
                likeness_results.append(
                    FaceLikeness(
                        face_id_a=pair.face_id_a,
                        face_id_b=pair.face_id_b,
                        likeness=likeness,
                        metric="cosine_similarity",
                    )
                )
                to_remove.append(pair)
                processed_notify_ids.append(
                    (FaceLikeness, (pair.face_id_a, pair.face_id_b), "pair")
                )

            # 3. Write results and remove processed pairs in a new DB task
            def write_results(session):
                if likeness_results:
                    session.add_all(likeness_results)
                    logger.info(f"Inserted {len(likeness_results)} face likeness scores.")
                if to_remove:
                    for pair in to_remove:
                        session.delete(pair)
                    logger.info(
                        f"Removed {len(to_remove)} processed pairs from work queue."
                    )
                session.commit()

            self._db.run_task(write_results)

            if processed_notify_ids:
                self._notify_ids_processed(processed_notify_ids)

            elapsed = time.time() - start
            if elapsed < self.INTERVAL:
                self._stop.wait(self.INTERVAL - elapsed)
        logger.info("FaceLikenessWorker: Face likeness worker stopped.")

    def _cosine_similarity(self, features_a, features_b):
        # Assume features are bytes representing np.ndarray
        arr_a = np.frombuffer(features_a, dtype=np.float32)
        arr_b = np.frombuffer(features_b, dtype=np.float32)
        norm_a = np.linalg.norm(arr_a)
        norm_b = np.linalg.norm(arr_b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        similarity = float(np.dot(arr_a, arr_b) / (norm_a * norm_b))
        return similarity

import pickle
import time
import os
import numpy as np
from PIL import Image
from collections import defaultdict
from sqlmodel import Session, select
from sentence_transformers import SentenceTransformer

from pixlvault.database import DBPriority
from pixlvault.db_models import Picture
from pixlvault.worker_registry import BaseWorker, WorkerType
from pixlvault.pixl_logging import get_logger
from pixlvault.picture_utils import PictureUtils

logger = get_logger(__name__)


class ImageEmbeddingWorker(BaseWorker):
    """
    Worker for generating image embeddings (CLIP) for pictures.
    """

    BATCH_SIZE = 32
    # Use a lightweight CLIP model. 
    # 'clip-ViT-B-32' is standard, ~600MB download, 512 dim.
    # 'clip-ViT-L-14' is heavier, 768 dim.
    # The spec mentions "L2-normalized image embedding... emb(img) ∈ ℝ^D"
    MODEL_NAME = "clip-ViT-B-32" 

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.model = None

    def worker_type(self) -> WorkerType:
        return WorkerType.IMAGE_EMBEDDING

    def _ensure_model(self):
        if self.model is None:
            logger.info(f"ImageEmbeddingWorker: Loading model {self.MODEL_NAME}...")
            # We use SentenceTransformer's CLIP wrapper
            self.model = SentenceTransformer(self.MODEL_NAME)
            logger.info("ImageEmbeddingWorker: Model loaded.")

    def _run(self):
        logger.info("ImageEmbeddingWorker: Started.")
        
        while not self._stop.is_set():
            try:
                # Find pictures without image_embedding
                batch = self._db.run_immediate_read_task(self._fetch_missing_embeddings)
                
                if not batch:
                    logger.debug("ImageEmbeddingWorker: No pictures need embeddings. Sleeping...")
                    self._wait()
                    continue

                self._ensure_model()
                
                logger.debug(f"ImageEmbeddingWorker: Processing {len(batch)} pictures.")
                
                # Load images (flattened list of PIL images)
                flat_images = []
                # Map each image index back to the picture ID
                flat_pids = []
                
                for pid, file_path in batch:
                    try:
                        # Prepend image root
                        full_path = os.path.join(self._db.image_root, file_path)
                        
                        if PictureUtils.is_video_file(file_path):
                            # Extract 3 key frames: Start, Middle, End
                            pil_imgs = PictureUtils.extract_representative_video_frames(full_path, count=3)
                            
                            if pil_imgs:
                                flat_images.extend(pil_imgs)
                                flat_pids.extend([pid] * len(pil_imgs))

                        else:
                            # Standard Image
                            try:
                                img = Image.open(full_path)
                            except Exception as e:
                                logger.error(f"ImageEmbeddingWorker: PIL failed to open {file_path}: {e}")
                                continue                                

                            # Ensure it's RGB
                            if img.mode != 'RGB':
                                img = img.convert('RGB')
                            flat_images.append(img)
                            flat_pids.append(pid)
                            
                    except Exception as e:
                        logger.error(f"ImageEmbeddingWorker: Error loading {file_path}: {e}")
                        pass

                if flat_images:
                    # Compute embeddings for all frames
                    embeddings = self.model.encode(flat_images, batch_size=self.BATCH_SIZE, convert_to_numpy=True, normalize_embeddings=True)
                    
                    # Group by PID and average
                    pid_embeddings = defaultdict(list)
                    for pid, emb in zip(flat_pids, embeddings):
                        pid_embeddings[pid].append(emb)
                        
                    updates = []
                    for pid, embs in pid_embeddings.items():
                        if len(embs) == 1:
                            # Already normalized by model.encode
                            final_emb = embs[0]
                        else:
                            # Average and re-normalize
                            avg = np.mean(embs, axis=0)
                            norm = np.linalg.norm(avg)
                            if norm > 0:
                                final_emb = avg / norm
                            else:
                                final_emb = avg # Should not happen unless empty or zero vector
                        
                        updates.append((pid, pickle.dumps(final_emb)))
                    
                    self._db.run_task(self._save_embeddings, updates, priority=DBPriority.LOW)
                    
                    logger.info(f"ImageEmbeddingWorker: Saved {len(updates)} embeddings (processed {len(flat_images)} frames).")

            except Exception as e:
                logger.error(f"ImageEmbeddingWorker: Error in loop: {e}")
                time.sleep(5)
                
        logger.info("ImageEmbeddingWorker: Stopped.")

    def _fetch_missing_embeddings(self, session: Session):
        """Fetch a batch of pictures that have no image_embedding"""
        stmt = (
            select(Picture.id, Picture.file_path)
            .where(Picture.image_embedding.is_(None))
            .limit(self.BATCH_SIZE)
        )
        results = session.exec(stmt).all()
        return results

    def _save_embeddings(self, session: Session, updates):
        for pid, emb_bytes in updates:
            pic = session.get(Picture, pid)
            if pic:
                pic.image_embedding = emb_bytes
        session.commit()

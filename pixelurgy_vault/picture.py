import logging
from typing import Optional, List
import uuid
import numpy as np
import os
from PIL import Image
from .picture_quality import PictureQuality

# Configure logging for the module
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)


class Picture:
    """
    Represents a digital picture with typical metadata and AI/Diffusion-friendly thumbnail storage as a NumPy array.
    """

    def __init__(
        self,
        file_path: str,
        character_id: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        format: Optional[str] = None,
        created_at: Optional[str] = None,
        thumbnail: Optional[np.ndarray] = None,
    ):
        if not os.path.exists(file_path):
            raise ValueError(f"File path does not exist: {file_path}")

        self.id = self._calculate_sha256(file_path)
        self.file_path = file_path  # Path to image file on disk
        self.character_id = character_id  # Reference to Character
        self.title = title
        self.description = description
        self.tags = tags or []
        self.width = width
        self.height = height
        self.format = format
        self.created_at = created_at
        self._thumbnail_array = thumbnail  # NumPy array (H, W, C), dtype=uint8
        self.quality = PictureQuality()

    @property
    def thumbnail(self) -> Optional[Image.Image]:
        """
        Returns a PIL Image object for the thumbnail, or None if not available.
        """
        try:
            if self._thumbnail_array is None:
                self._thumbnail_array = np.array(self.generate_thumbnail())
            return Image.fromarray(self._thumbnail_array)
        except Exception as e:
            logger.error(f"Error getting thumbnail: {e}")
            return None

    @property
    def thumbnail_array(self) -> Optional[np.ndarray]:
        """
        Returns a NumPy array for the thumbnail, or None if not available.
        """
        try:
            if self._thumbnail_array is None:
                self._thumbnail_array = np.array(self.generate_thumbnail())
            return self._thumbnail_array
        except Exception as e:
            logger.error(f"Error getting thumbnail: {e}")
            return None

    @property
    def image(self) -> Optional[Image.Image]:
        """
        Returns a PIL Image object for the picture file, or None if not available.
        """
        try:
            return Image.open(self.file_path)
        except Exception:
            return None

    def calculate_quality_metrics(self):
        """
        Calculate and store quality metrics using the PictureQuality class.
        """
        try:
            image = Image.open(self.file_path)
            self.quality = np.array(PictureQuality.calculate_metrics(image))
        except Exception as e:
            logger.error(f"Error calculating quality metrics: {e}")

    def generate_thumbnail(self, size=(128, 128)):
        """
        Generate and store a thumbnail as a NumPy array.
        """
        try:
            image = Image.open(self.file_path)
            image.thumbnail(size)
            return image
        except Exception as e:
            logger.error(f"Error generating thumbnail: {e}")
            return None

    def _calculate_sha256(self, file_path: str) -> str:
        """
        Calculate SHA-256 hash of the file for a unique ID.
        """
        import hashlib

        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(8192):
                sha256.update(chunk)
        return sha256.hexdigest()

import cv2
import hashlib
import json
import os
import uuid

from datetime import datetime, timezone
from io import BytesIO
from PIL import Image
from typing import Optional, List, Tuple

from .logging import get_logger

# Configure logging for the module
logger = get_logger(__name__)


class Picture:
    """Master asset representing a logical picture (stable UUID)."""

    def __init__(
        self,
        id: Optional[str] = None,
        character_id: Optional[str] = None,
        file_path: Optional[str] = None,        
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
        format: Optional[str] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        size_bytes: Optional[int] = None,        
        created_at: Optional[str] = None,
        is_reference: bool = False,
        embedding: Optional[bytes] = None,
        face_embedding: Optional[bytes] = None,
        thumbnail: Optional[bytes] = None,
        quality: Optional[str] = None,
        face_quality: Optional[str] = None,
        score: Optional[int] = None,
        character_likeness: Optional[float] = None,
        pixel_sha: Optional[str] = None,
    ):
        self.format = format if format else file_path.split(".")[-1] if file_path else "png"
        if id:
            self.id = id
        else:
            self.id = f"{uuid.uuid4().hex}.{self.format}"

        self.character_id = character_id
        self.file_path = file_path
        self.description = description
        self.tags = tags or []
        self.width = width
        self.height = height
        self.size_bytes = size_bytes

        self.created_at = created_at or datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        self.is_reference = is_reference
        self.embedding = embedding
        self.face_embedding = face_embedding
        self.thumbnail = thumbnail
        self.quality = quality
        self.face_quality = face_quality
        self.score = score
        self.character_likeness = character_likeness
        self.pixel_sha = pixel_sha
        if not self.pixel_sha and self.file_path and os.path.exists(self.file_path):
            self.pixel_sha = self.calculate_hash_from_file_path(self.file_path)

    @staticmethod
    def create_from_file(
        image_root_path: str,
        source_file_path: str,
        picture_id: str,
    ) -> Tuple[str, "Picture"]:
        if not picture_id:
            raise ValueError(
                "picture_id must be provided when creating a picture."
            )
        if not os.path.exists(source_file_path):
            raise ValueError(f"Source file path does not exist: {source_file_path}")
        with open(source_file_path, "rb") as f:
            image_bytes = f.read()
        return Picture.create_from_bytes(
            image_root_path=image_root_path,
            image_bytes=image_bytes,
            picture_id=picture_id,
        )

    @staticmethod
    def create_from_bytes(
        image_root_path: str,
        image_bytes: bytes,
        picture_id: str,
    ) -> Tuple[str, "Picture"]:
        """Create a a Picture from raw bytes. Returns (picture_id, Picture). Supports both images and videos."""

        raw_sha = Picture.calculate_hash_from_bytes(image_bytes)
        if not picture_id:
            raise ValueError(
                "picture_uuid must be provided when creating a picture."
            )

        # Try to detect if this is a video or image
        img_format = None
        width = height = None
        thumbnail_bytes = None
        is_video = False
        # Try image first
        try:
            with Image.open(BytesIO(image_bytes)) as img:
                img_format = img.format or "PNG"
                width, height = img.size
                thumbnail_bytes = Picture._generate_thumbnail_bytes(img)
        except Exception:
            # Not an image, try video
            is_video = True
        if is_video:
            # Write bytes to temp file to read with cv2
            import tempfile

            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
                tmp.write(image_bytes)
                tmp_path = tmp.name
            cap = cv2.VideoCapture(tmp_path)
            ret, frame = cap.read()
            if not ret:
                logger.error("Could not read first frame from video for thumbnail.")
            else:
                height, width = frame.shape[:2]
                thumbnail_bytes = Picture._generate_thumbnail_bytes(frame)
            cap.release()
            img_format = "MP4"  # Default, could be improved by sniffing
            # Remove temp file
            os.remove(tmp_path)

        ext = f".{img_format.lower()}" if not img_format.startswith(".") else img_format
        id_with_ext = f"{raw_sha}{ext}"
        file_path = os.path.join(image_root_path, id_with_ext)
        if os.path.exists(file_path):
            size_bytes = os.path.getsize(file_path)
        else:
            os.makedirs(image_root_path, exist_ok=True)
            with open(file_path, "wb") as f:
                f.write(image_bytes)
            size_bytes = len(image_bytes)

        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        pic = Picture(
            id=id_with_ext,
            picture_id=picture_id,
            file_path=file_path,
            format=img_format,
            width=width,
            height=height,
            size_bytes=size_bytes,
            created_at=created_at,
            thumbnail=thumbnail_bytes,
        )
        return picture_id, pic


    @classmethod
    def from_row(cls, row):
        return cls(
            id=row["id"],
            character_id=row.get("character_id"),
            file_path=row.get("file_path"),
            description=row.get("description"),
            tags=json.loads(row["tags"]) if row.get("tags") else [],
            format=row.get("format"),
            width=row.get("width"),
            height=row.get("height"),
            size_bytes=row.get("size_bytes"),
            created_at=row.get("created_at"),
            is_reference=row.get("is_reference", 0),
            embedding=row.get("embedding"),
            face_embedding=row.get("face_embedding"),
            thumbnail=row.get("thumbnail"),
            quality=row.get("quality"),
            face_quality=row.get("face_quality"),
            score=row.get("score"),
            character_likeness=row.get("character_likeness"),
            pixel_sha=row.get("pixel_sha"),
        )
    

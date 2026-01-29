from sqlalchemy import Column, ForeignKey, UniqueConstraint
from sqlmodel import SQLModel, Field, Integer, Relationship

from typing import TYPE_CHECKING, Optional

from .face_tag import FaceTag
from .hand_tag import HandTag

if TYPE_CHECKING:
    from .picture import Picture
    from .face import Face
    from .hand import Hand


class Tag(SQLModel, table=True):
    """
    SQLModel for the picture_tags table.
    """

    id: int = Field(default=None, primary_key=True)

    picture_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("picture.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    tag: str = Field(index=True)

    __table_args__ = (UniqueConstraint("picture_id", "tag"),)

    picture: Optional["Picture"] = Relationship(
        back_populates="tags",
        sa_relationship_kwargs={
            "passive_deletes": True,
            "foreign_keys": "[Tag.picture_id]",
        },
    )
    faces: list["Face"] = Relationship(
        back_populates="tags",
        link_model=FaceTag,
        sa_relationship_kwargs={"passive_deletes": True},
    )
    hands: list["Hand"] = Relationship(
        back_populates="tags",
        link_model=HandTag,
        sa_relationship_kwargs={"passive_deletes": True},
    )

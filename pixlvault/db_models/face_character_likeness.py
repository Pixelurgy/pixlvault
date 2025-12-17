from sqlalchemy import Column, Integer, ForeignKey
from sqlmodel import (
    SQLModel,
    Field,
)


class FaceCharacterLikeness(SQLModel, table=True):
    """
    Database model for the Face_likeness table.
    Stores likeness scores for each (Face, Face) combination.
    Note, this is NOT picture likeness, but individual face likeness.
    """

    face_id: int = Field(
        sa_column=Column(
            Integer, ForeignKey("face.id", ondelete="CASCADE"), primary_key=True
        )
    )
    character_id: int = Field(
        sa_column=Column(
            Integer, ForeignKey("character.id", ondelete="CASCADE"), primary_key=True
        )
    )
    likeness: float = Field(default=None, index=True)
    metric: str = Field(default=None)

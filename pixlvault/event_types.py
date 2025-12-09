from enum import auto, Enum


class EventTypes(Enum):
    CHANGED_PICTURES = auto()
    CHANGED_TAGS = auto()
    CHANGED_CHARACTERS = auto()
    CHANGED_DESCRIPTIONS = auto()
    CHANGED_FACES = auto()

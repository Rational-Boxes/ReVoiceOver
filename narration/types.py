"""Core data shapes shared by every stage, with plain-dict (de)serialization."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Self


class _Serializable:
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)  # type: ignore[call-overload]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        names = {f.name for f in fields(cls)}  # type: ignore[arg-type]
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class Chunk(_Serializable):
    """A slice of the ASR audio; word timestamps are shifted by ``offset``."""

    file: str        # relative to the work dir
    offset: float    # seconds into the source video
    duration: float


@dataclass
class Word(_Serializable):
    text: str
    start: float     # seconds, absolute in source video
    end: float
    confidence: float | None = None


@dataclass
class Unit(_Serializable):
    id: str          # "u0001"
    start: float
    end: float
    slot_end: float  # next unit's start, or video end
    raw_text: str
    clean_text: str | None = None
    keep_original: bool = False

    @property
    def slot(self) -> float:
        return self.slot_end - self.start


@dataclass
class TimelineEntry(_Serializable):
    unit_id: str
    src_start: float
    src_end: float
    out_start: float
    out_end: float
    audio: str       # fitted clip, relative to the work dir
    atempo: float = 1.0
    video_stretch: float = 1.0

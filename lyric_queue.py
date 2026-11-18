from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LyricState(Enum):
    SCHEDULED = "scheduled"
    ENTERING = "entering"
    VISIBLE = "visible"
    EXITING = "exiting"
    REMOVED = "removed"


@dataclass
class LyricEvent:
    event_id: str
    track_id: str
    lyric_index: int
    text: str
    start_ms: int
    end_ms: int
    state: LyricState = LyricState.SCHEDULED

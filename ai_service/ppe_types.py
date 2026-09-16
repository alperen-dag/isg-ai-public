"""Pixel xyxy boxes, [0, 1] confidence; pose IDs are frame-local until PersonTracker assigns session tracks."""
from dataclasses import dataclass, field
from math import isfinite


@dataclass(frozen=True)
class Detection:
    class_name: str
    confidence: float
    bbox: tuple[float, float, float, float]
    person_id: str | None = None

    def __post_init__(self):
        if not isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("Invalid confidence")
        if len(self.bbox) != 4 or not all(isfinite(v) for v in self.bbox):
            raise ValueError("Invalid bbox")
        x1, y1, x2, y2 = self.bbox
        if x2 <= x1 or y2 <= y1:
            raise ValueError("Empty bbox")


@dataclass(frozen=True)
class Person:
    person_id: str
    bbox: tuple
    confidence: float
    keypoints: tuple = ()  # COCO (x, y, confidence)


@dataclass(frozen=True)
class PPEBatch:
    detections: tuple[Detection, ...] = ()
    available: bool = False
    supported_classes: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Violation:
    ihlal_turu: str
    confidence: float | None
    person_id: str | None = None
    note: str = ""

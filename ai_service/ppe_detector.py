"""Implement this adapter with a separately validated PPE detection model."""
from abc import ABC, abstractmethod
from ai_service.ppe_types import PPEBatch


class PPEDetector(ABC):
    @abstractmethod
    def detect(self, frame) -> PPEBatch:
        """Return explicit positive/negative PPE classes; never infer absence here."""


class UnavailablePPEDetector(PPEDetector):
    def detect(self, frame):
        return PPEBatch()

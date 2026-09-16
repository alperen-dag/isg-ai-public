import logging
from ai_service.ppe_association import associate
from ai_service.ppe_detector import UnavailablePPEDetector
from ai_service.rules import evaluate_ppe

logger = logging.getLogger(__name__)


class PPEPipeline:
    def __init__(self, detector=None):
        self.detector = detector if detector is not None else UnavailablePPEDetector()
        self.failed = False

    def evaluate(self, frame, people, configs):
        if not configs:
            return []
        try:
            batch = self.detector.detect(frame)
            if not batch.available:
                return []
            violations = []
            for config in configs:
                matches = associate(people, batch.detections, config.confidence_threshold)
                violations.extend(evaluate_ppe(people, matches, config, batch.supported_classes))
            self.failed = False
            return violations
        except Exception:
            if not self.failed:
                logger.exception("PPE unavailable; restricted zone processing continues")
            self.failed = True
            return []

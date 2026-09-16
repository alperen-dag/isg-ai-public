"""Local YOLO inference, separate from the unchanged pose model."""
import hashlib
import logging
from time import perf_counter

from ai_service.ppe_config import load_ppe_config
from ai_service.ppe_detector import PPEDetector, UnavailablePPEDetector
from ai_service.ppe_labels import canonical_class
from ai_service.ppe_types import Detection, PPEBatch

logger = logging.getLogger(__name__)


class YOLOPPEDetector(PPEDetector):
    def __init__(self, config, model_factory=None):
        self.config = config
        if not config.model_path.is_file():
            raise FileNotFoundError(config.model_path)
        if config.sha256:
            digest = hashlib.sha256(config.model_path.read_bytes()).hexdigest()
            if digest != config.sha256:
                raise ValueError('PPE weight SHA-256 mismatch')
        if model_factory is None:
            from ultralytics import YOLO
            model_factory = YOLO
        self.model = model_factory(str(config.model_path), task='detect')
        if self.model.task != 'detect':
            raise ValueError('PPE requires a detection model, not pose')
        names = self.model.names
        self.names = dict(enumerate(names)) if isinstance(names, list) else dict(names)
        if config.expected_names and tuple(self.names[i] for i in sorted(self.names)) != config.expected_names:
            raise ValueError('PPE checkpoint classes differ from verified manifest')
        self.class_map = {i: canonical_class(name) for i, name in self.names.items()}
        self.supported_classes = frozenset(name for name in self.class_map.values() if name)
        if not self.supported_classes:
            raise ValueError('Model has no recognized PPE classes')
        self.last_inference_ms = None
        self.last_model_inference_ms = None
        logger.info('PPE loaded: %s; supported=%s', config.model_path, sorted(self.supported_classes))

    def detect(self, frame):
        self.last_inference_ms = None
        self.last_model_inference_ms = None
        start = perf_counter()
        results = self.model.predict(frame, conf=self.config.confidence,
                                     imgsz=self.config.image_size, device=self.config.device,
                                     verbose=False, save=False)
        detections = []
        for result in results:
            if result.boxes is None:
                continue
            for row in result.boxes.data.cpu().tolist():
                x1, y1, x2, y2, confidence, class_id = row[:6]
                name = self.class_map.get(int(class_id))
                if name is not None and confidence >= self.config.confidence:
                    detections.append(Detection(name, confidence, (x1, y1, x2, y2)))
        self.last_inference_ms = (perf_counter() - start) * 1000
        speeds = [getattr(result, 'speed', {}).get('inference') for result in results]
        if speeds and all(speed is not None for speed in speeds):
            self.last_model_inference_ms = sum(speeds)
        return PPEBatch(tuple(detections), True, self.supported_classes)


def create_ppe_detector():
    """Bad configuration/weights must never take down zone detection."""
    try:
        return YOLOPPEDetector(load_ppe_config())
    except Exception:
        logger.exception('PPE model unavailable; restricted zone remains active')
        return UnavailablePPEDetector()

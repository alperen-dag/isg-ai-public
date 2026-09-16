from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from ai_service.ppe_config import PPEModelConfig, ROOT, load_ppe_config
from ai_service.ppe_detector import UnavailablePPEDetector
from ai_service.ppe_labels import canonical_class
from ai_service.ppe_pipeline import PPEPipeline
from ai_service.ppe_types import Person
from ai_service.rules import CameraPPERules
from ai_service.yolo_ppe_detector import YOLOPPEDetector, create_ppe_detector


class FakeModel:
    task = 'detect'
    names = {0:'Hardhat', 1:'NO-Hardhat', 2:'Safety Vest', 3:'NO-Safety Vest', 4:'Person'}

    def __init__(self, rows):
        self.rows = rows

    def predict(self, *args, **kwargs):
        self.options = kwargs
        return [SimpleNamespace(boxes=SimpleNamespace(data=SimpleNamespace(
            cpu=lambda: np.array(self.rows))))]


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'model.pt'
        self.path.write_bytes(b'unit test stub, never deserialized')

    def detector(self, rows, names=None):
        model = FakeModel(rows)
        if names is not None:
            model.names = names
        return YOLOPPEDetector(PPEModelConfig(self.path, confidence=.5), lambda *a, **k: model)

    def test_mapping_threshold_and_unknown_class(self):
        detector = self.detector([[20,5,60,35,.9,1], [20,60,60,120,.8,3],
                                  [0,0,100,200,.99,4], [20,5,60,35,.49,0]])
        batch = detector.detect(None)
        self.assertEqual([d.class_name for d in batch.detections], ['no_helmet','no_vest'])
        self.assertEqual(detector.model.options['conf'], .5)
        self.assertEqual(batch.supported_classes, frozenset(['helmet','no_helmet','vest','no_vest']))
        self.assertEqual(canonical_class('no_safety_vest'), 'no_vest')
        self.assertIsNone(canonical_class('boots'))

    def test_two_violations_correct_person(self):
        detector = self.detector([[220,5,260,35,.9,1],[220,60,260,120,.8,3]])
        people = [Person('a',(0,0,100,200),.9), Person('b',(200,0,300,200),.9)]
        rules = [CameraPPERules(frozenset(['helmet','vest']))]
        events = PPEPipeline(detector).evaluate(None,people,rules)
        self.assertEqual([e.person_id for e in events], ['b','b'])
        self.assertEqual({e.ihlal_turu for e in events}, {'Baret Yok','Reflektif Yelek Yok'})

    def test_positive_only_never_implies_absence(self):
        detector = self.detector([], {0:'Hardhat',1:'Safety Vest'})
        rules = [CameraPPERules(frozenset(['helmet','vest']))]
        self.assertEqual(PPEPipeline(detector).evaluate(None,[Person('a',(0,0,100,200),.9)],rules), [])
        self.assertNotIn('no_helmet',detector.supported_classes)

    def test_fallback_bad_path_config_hash_and_model(self):
        with patch('ai_service.yolo_ppe_detector.load_ppe_config', side_effect=ValueError('bad config')):
            with self.assertLogs('ai_service.yolo_ppe_detector', level='ERROR'):
                self.assertIsInstance(create_ppe_detector(), UnavailablePPEDetector)
        for config in (PPEModelConfig(self.path.with_name('missing.pt')),
                       PPEModelConfig(self.path, sha256='0'*64)):
            with patch('ai_service.yolo_ppe_detector.load_ppe_config', return_value=config):
                with self.assertLogs('ai_service.yolo_ppe_detector', level='ERROR'):
                    self.assertFalse(create_ppe_detector().detect(None).available)
        with self.assertRaises(ValueError):
            YOLOPPEDetector(PPEModelConfig(self.path,expected_names=('wrong',)), lambda *a, **k: FakeModel([]))

    def test_bad_threshold_rejected(self):
        for value in (-.1, 1.1, float('nan')):
            with self.assertRaises(ValueError):
                PPEModelConfig(self.path, confidence=value)

    def test_load_exception_uses_fallback(self):
        with patch('ai_service.yolo_ppe_detector.YOLOPPEDetector', side_effect=RuntimeError('load failure')):
            with self.assertLogs('ai_service.yolo_ppe_detector', level='ERROR'):
                self.assertIsInstance(create_ppe_detector(), UnavailablePPEDetector)


@unittest.skipUnless((ROOT/'models/ppe_yolov8n.pt').is_file(), 'Local verified PPE weights not installed')
class RealModelTests(unittest.TestCase):
    def test_real_checkpoint_load_classes_and_inference(self):
        detector = YOLOPPEDetector(load_ppe_config())
        batch = detector.detect(np.zeros((480,640,3),dtype=np.uint8))
        self.assertTrue(batch.available)
        self.assertEqual(batch.supported_classes, frozenset(['helmet','no_helmet','vest','no_vest']))
        self.assertEqual(batch.detections, ())
        self.assertGreater(detector.last_inference_ms, 0)

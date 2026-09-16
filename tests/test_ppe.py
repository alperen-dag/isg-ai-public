import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime
from contextlib import closing, ExitStack
from unittest.mock import MagicMock

import numpy as np
import isg_database as db
from ai_service.main import IhlalTakipDurumu, kareyi_analiz_et, ihlal_kaydi_olustur
from ai_service.ppe_types import Detection, Person, PPEBatch
from ai_service.ppe_pipeline import PPEPipeline
from ai_service.ppe_detector import UnavailablePPEDetector
from ai_service.rules import CameraPPERules, load_camera_rules
from ai_service.violation_store import save_violation


class MockDetector:
    def __init__(self, *detections):
        self.detections = detections

    def detect(self, frame):
        return PPEBatch(self.detections, True, frozenset(
            ['no_helmet', 'no_vest', 'no_ear_protection', 'no_safety_shoes']))


class PPETests(unittest.TestCase):
    def setUp(self):
        self.people = [Person('a', (0, 0, 100, 200), .9)]
        self.configs = [CameraPPERules(frozenset(['helmet', 'vest']))]

    def run_ppe(self, *detections, people=None):
        return PPEPipeline(MockDetector(*detections)).evaluate(
            None, self.people if people is None else people, self.configs)

    def test_unavailable_and_empty_do_not_mean_missing(self):
        self.assertEqual(PPEPipeline().evaluate(None, self.people, self.configs), [])
        self.assertEqual(self.run_ppe(), [])

    def test_helmet_and_vest_separate(self):
        helmet = Detection('no_helmet', .9, (20, 5, 60, 35))
        self.assertEqual(len(self.run_ppe(helmet)), 1)
        events = self.run_ppe(helmet, Detection('no_vest', .8, (20, 65, 60, 120)))
        self.assertEqual({e.ihlal_turu for e in events}, {'Baret Yok', 'Reflektif Yelek Yok'})

    def test_low_confidence_and_conflict(self):
        self.assertEqual(self.run_ppe(Detection('no_helmet', .49, (20, 5, 60, 35))), [])
        self.assertEqual(self.run_ppe(Detection('no_helmet', .9, (20, 5, 60, 35)),
                                     Detection('helmet', .8, (20, 5, 60, 35))), [])

    def test_multiple_people_and_ambiguity(self):
        people = self.people + [Person('b', (200, 0, 300, 200), .9)]
        events = self.run_ppe(Detection('no_helmet', .9, (220, 5, 260, 35)), people=people)
        self.assertEqual([e.person_id for e in events], ['b'])
        self.assertEqual(self.run_ppe(Detection('no_helmet', .9, (220, 5, 260, 35), 'a'), people=people), [])
        people = self.people + [Person('b', (0, 0, 100, 200), .9)]
        self.assertEqual(self.run_ppe(Detection('no_helmet', .9, (20, 5, 60, 35)), people=people), [])

    def test_keypoints_and_other_equipment(self):
        points = [(0, 0, 0)] * 17
        points[0] = (50, 70, .9)  # head outside bbox-fallback head band
        people = [Person('a', (0, 0, 100, 200), .9, tuple(points))]
        events = self.run_ppe(Detection('no_helmet', .9, (40, 60, 60, 80)), people=people)
        self.assertEqual(len(events), 1)
        self.configs = [CameraPPERules(frozenset(['ear_protection', 'safety_shoes']))]
        self.assertEqual(len(self.run_ppe(
            Detection('no_ear_protection', .9, (20, 5, 60, 35)),
            Detection('no_safety_shoes', .9, (20, 170, 60, 195)))), 2)

    def test_failure_isolated(self):
        detector = MockDetector()
        with patch.object(detector, 'detect', side_effect=RuntimeError('offline')):
            with self.assertLogs('ai_service.ppe_pipeline', level='ERROR'):
                self.assertEqual(PPEPipeline(detector).evaluate(None, self.people, self.configs), [])

    def test_unsupported_and_disabled(self):
        detector = MockDetector(Detection('no_helmet', .9, (20, 5, 60, 35)))
        self.assertEqual(PPEPipeline(detector).evaluate(None, self.people, []), [])
        with patch.object(detector, 'detect', return_value=PPEBatch(detector.detections, True)):
            self.assertEqual(PPEPipeline(detector).evaluate(None, self.people, self.configs), [])

    def test_invalid_detection(self):
        for score in (float('nan'), -1, 2):
            with self.assertRaises(ValueError):
                Detection('helmet', score, (0, 0, 10, 10))


class DatabaseTests(unittest.TestCase):
    setUp = PPETests.setUp
    run_ppe = PPETests.run_ppe

    def test_migration_and_separate_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.db'
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("CREATE TABLE ihlaller (id INTEGER PRIMARY KEY, tarih TEXT, ihlal_turu TEXT, guven_skoru REAL, fotograf_yolu TEXT, durum TEXT, aciklama TEXT)")
                conn.execute("INSERT INTO ihlaller VALUES (1,'old','old',80,'old.jpg','Yeni','keep')")
            with patch.object(db, 'DB_PATH', path):
                db.prepare_database()
                db.prepare_database()
                with closing(db.connect_database()) as conn, conn:
                    self.assertEqual(conn.execute('SELECT aciklama, kamera_id FROM ihlaller WHERE id=1').fetchone(), ('keep', None))
                    self.assertEqual(load_camera_rules(conn, 1), [])
                    conn.execute("INSERT INTO kamera_ppe_kurallari VALUES(1,'helmet',1,.7)")
                    self.assertEqual(load_camera_rules(conn, 1)[0].confidence_threshold, .7)
                    self.assertEqual(load_camera_rules(conn, 999), [])
                    events = self.run_ppe(Detection('no_helmet', .9, (20, 5, 60, 35)), Detection('no_vest', .8, (20, 65, 60, 120)))
                    frame = np.zeros((200, 100, 3), dtype=np.uint8)
                    paths = [save_violation(conn, frame, e, Path(tmp), 1, datetime(2026, 1, 1)) for e in events]
                    self.assertEqual(len(set(paths)), 2)
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM ihlaller').fetchone()[0], 3)
                    with patch('ai_service.main.IHLAL_KLASORU', Path(tmp)):
                        ihlal_kaydi_olustur(conn, frame, .85)
                    self.assertEqual(conn.execute('SELECT ihlal_turu,guven_skoru,durum FROM ihlaller ORDER BY id DESC LIMIT 1').fetchone(), ('Yasak Bolge Ihlali',85,'Yeni'))
                    before = set(Path(tmp).glob('*.jpg'))
                    with self.assertRaises(sqlite3.IntegrityError):
                        save_violation(conn, frame, events[0], Path(tmp), 999)
                    self.assertEqual(set(Path(tmp).glob('*.jpg')), before)
                    self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])


class ZoneRegressionTests(unittest.TestCase):
    def test_service_tracker_disabled_keeps_zone_recording(self):
        from ai_service.event_tracking import TrackingConfig
        with patch('ai_service.main.load_tracking_config', return_value=TrackingConfig(enabled=False)):
            self._run_service_check()

    def test_service_without_ppe_model_keeps_zone_recording(self):
        self._run_service_check()

    def test_service_detector_failure_keeps_zone_recording(self):
        detector = MagicMock()
        detector.detect.side_effect = RuntimeError('PPE inference failed')
        with self.assertLogs('ai_service.ppe_pipeline', level='ERROR'):
            self._run_service_check(detector)
        self.assertEqual(detector.detect.call_count, 5)

    def _run_service_check(self, detector=None):
        from ai_service import main
        camera = MagicMock()
        camera.read.side_effect = [(True, np.zeros((200, 100, 3), dtype=np.uint8))] * 5
        connection = MagicMock()
        with ExitStack() as stack:
            stack.enter_context(patch.object(main, "CameraHealthReporter"))
            for name, value in {
                'prepare_database': None, 'connect_database': connection,
                'aktif_kamera_ayarini_getir': {'id': 1, 'kamera_adi': 'test', 'kamera_index': 0, 'yasak_bolge_orani': .35},
                'modeli_yukle': object(), 'kamerayi_ac': camera,
                'load_camera_rules': [CameraPPERules(frozenset(['helmet']))],
                'create_ppe_detector': UnavailablePPEDetector(),
                'kareyi_analiz_et': (True, .9),
            }.items():
                stack.enter_context(patch.object(main, name, return_value=value))
            rule_connection = MagicMock()
            stack.enter_context(patch.object(main, 'connect_database', side_effect=[connection, rule_connection]))
            zone_save = stack.enter_context(patch.object(main, 'ihlal_kaydi_olustur'))
            ppe_save = stack.enter_context(patch.object(main, 'save_violation'))
            stack.enter_context(patch.object(main, 'IHLAL_KLASORU'))
            for name in ('imshow', 'putText', 'destroyAllWindows'):
                stack.enter_context(patch.object(main.cv2, name))
            stack.enter_context(patch.object(main.cv2, 'waitKey', side_effect=[0,0,0,0,ord('q')]))
            main.servisi_calistir(detector)
            zone_save.assert_called_once()
            ppe_save.assert_not_called()
            camera.release.assert_called_once()
            connection.close.assert_called_once()
            rule_connection.close.assert_called_once()

    def test_suppression_is_per_person_and_type(self):
        from ai_service import main
        from ai_service.ppe_types import Violation
        camera = MagicMock()
        camera.read.side_effect = [(True, np.zeros((200,100,3), dtype=np.uint8))] * 3
        a = Violation('Baret Yok', .9, 'a')
        b = Violation('Baret Yok', .9, 'b')
        c = Violation('Baret Yok', .9, 'c')
        with ExitStack() as stack:
            stack.enter_context(patch.object(main, "CameraHealthReporter"))
            for name, value in {
                'prepare_database': None, 'connect_database': MagicMock(),
                'aktif_kamera_ayarini_getir': {'id':1, 'kamera_adi':'test', 'kamera_index':0, 'yasak_bolge_orani':.35},
                'modeli_yukle': object(), 'kamerayi_ac': camera,
                'load_camera_rules': [CameraPPERules(frozenset(['helmet']))],
                'create_ppe_detector': UnavailablePPEDetector(),
                'kareyi_analiz_et': (False, 0),
            }.items():
                stack.enter_context(patch.object(main, name, return_value=value))
            from ai_service.event_tracking import TrackingConfig
            stack.enter_context(patch.object(main, 'load_tracking_config', return_value=TrackingConfig(ppe_required=1)))
            pipeline = stack.enter_context(patch.object(main, 'PPEPipeline'))
            pipeline.return_value.evaluate.side_effect = [[a,b], [c], [c]]
            clock = stack.enter_context(patch.object(main, 'time'))
            clock.monotonic.side_effect = [100,110,130]
            save = stack.enter_context(patch.object(main, 'save_violation'))
            saved_frames = []
            save.side_effect = lambda *args: saved_frames.append(camera.read.call_count)
            stack.enter_context(patch.object(main, 'IHLAL_KLASORU'))
            for name in ('imshow', 'putText', 'destroyAllWindows'):
                stack.enter_context(patch.object(main.cv2, name))
            stack.enter_context(patch.object(main.cv2, 'waitKey', side_effect=[0,0,ord('q')]))
            main.servisi_calistir()
            self.assertEqual([call.args[2].person_id for call in save.call_args_list], ['a','b','c'])
            # c is independent of a/b and is saved immediately at t=110.
            self.assertEqual(save.call_count, 3)
            self.assertEqual(saved_frames, [1, 1, 2])

    def test_confirmation_and_rearm(self):
        state = IhlalTakipDurumu()
        for _ in range(4):
            self.assertFalse(state.kareyi_isle(True)[1])
        self.assertTrue(state.kareyi_isle(True)[1])
        state.ihlal_kaydedildi()
        self.assertFalse(state.kareyi_isle(True)[1])
        for _ in range(5):
            state.kareyi_isle(False)
        self.assertFalse(state.ihlal_aktif)

    def test_pose_zone_boundary_and_filters(self):
        from types import SimpleNamespace
        def analyze(box, confidence=.9, pose_conf=.9, collect=False):
            result = SimpleNamespace(
                boxes=[SimpleNamespace(conf=[confidence], xyxy=np.array([box]))],
                keypoints=SimpleNamespace(conf=np.full((1,17), pose_conf), xy=np.ones((1,17,2))))
            people = [] if collect else None
            output = kareyi_analiz_et(lambda *a, **k: [result], np.zeros((400,400,3), dtype=np.uint8), .35, people)
            return output, people
        self.assertEqual(analyze((260, 10, 360, 210))[0], (True, .9))
        self.assertEqual(analyze((210, 10, 310, 210))[0], (False, 0))
        self.assertEqual(analyze((280, 10, 310, 210))[0], (False, 0))
        self.assertEqual(analyze((260, 10, 360, 210), pose_conf=.1)[0], (False, 0))
        output, people = analyze((260, 10, 360, 210), collect=True)
        self.assertEqual(output, (True, .9))
        self.assertEqual(len(people), 1)


if __name__ == '__main__':
    unittest.main()

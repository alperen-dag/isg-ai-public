from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import isg_database as db
from ai_service.event_tracking import PersonTracker, TrackingConfig, PPEEventGate
from ai_service.main import IhlalTakipDurumu
from ai_service.ppe_types import Person, Violation
from ai_service.rules import restricted_zone_violation
from ai_service.violation_store import save_violation


class TrackingTests(unittest.TestCase):
    def test_two_people_through_association_tracking_and_gate(self):
        from ai_service.ppe_pipeline import PPEPipeline
        from ai_service.ppe_types import Detection, PPEBatch
        from ai_service.rules import CameraPPERules
        from unittest.mock import Mock
        detector = Mock()
        detector.detect.return_value = PPEBatch((
            Detection('no_helmet', .9, (20, 5, 60, 35)),
            Detection('no_helmet', .85, (220, 5, 260, 35)),
        ), True, frozenset(['no_helmet']))
        pipeline = PPEPipeline(detector)
        tracker, gate = PersonTracker(TrackingConfig()), PPEEventGate(TrackingConfig())
        people = [Person('0', (0, 0, 100, 200), .9), Person('1', (200, 0, 300, 200), .9)]
        saved = []
        for now in range(20):
            tracked = tracker.update(people if now % 2 else list(reversed(people)))
            events = pipeline.evaluate(None, tracked, [CameraPPERules(frozenset(['helmet']))])
            for event in gate.evaluate(1, events, now):
                saved.append(event)
                gate.saved(1, event, now)
        self.assertEqual(len(saved), 2)
        self.assertEqual(len({e.person_id for e in saved}), 2)

    def test_motion_order_missing_expiry_and_restart(self):
        tracker = PersonTracker(TrackingConfig(max_missing_frames=2))
        a = Person('0', (0, 0, 100, 200), .9)
        b = Person('1', (200, 0, 300, 200), .8)
        first = tracker.update([a, b])
        moved = Person('0', (205, 0, 305, 200), .8)
        second = tracker.update([moved, a])
        self.assertEqual([p.person_id for p in second], [first[1].person_id, first[0].person_id])
        tracker.update([])
        self.assertEqual(tracker.update([a])[0].person_id, first[0].person_id)
        for _ in range(3):
            tracker.update([])
        self.assertNotEqual(tracker.update([a])[0].person_id, first[0].person_id)
        self.assertNotEqual(PersonTracker(TrackingConfig()).update([a])[0].person_id, first[0].person_id)

    def test_disabled_and_failure_keep_detection_without_false_identity(self):
        person = Person('0', (0, 0, 100, 200), .9)
        tracker = PersonTracker(TrackingConfig(enabled=False))
        first, second = tracker.update([person])[0], tracker.update([person])[0]
        self.assertEqual(first.bbox, person.bbox)
        self.assertNotEqual(first.person_id, second.person_id)
        tracker = PersonTracker(TrackingConfig())
        with patch.object(tracker, '_update', side_effect=RuntimeError('lost')):
            with self.assertLogs('ai_service.event_tracking', level='ERROR'):
                self.assertEqual(tracker.update([person])[0].confidence, .9)
        self.assertIn(':track:', tracker.update([person])[0].person_id)

    def test_temporal_confirmation_cooldown_and_types(self):
        gate = PPEEventGate(TrackingConfig())
        a = Violation('Baret Yok', .9, 'a')
        b = Violation('Baret Yok', .8, 'b')
        vest = Violation('Reflektif Yelek Yok', .7, 'a')
        for now in (0, 1):
            self.assertEqual(gate.evaluate(1, [a, b, vest], now), [])
        self.assertEqual(gate.evaluate(1, [a, b, vest], 2), [a, b, vest])
        for event in [a, b, vest]:
            gate.saved(1, event, 2)
        for now in range(3, 32):
            self.assertEqual(gate.evaluate(1, [a, b, vest], now), [])
        self.assertEqual(gate.evaluate(1, [a, b, vest], 32), [a, b, vest])
        # A different camera has its own history and cooldown.
        for now in (33, 34):
            self.assertEqual(gate.evaluate(2, [a], now), [])
        self.assertEqual(gate.evaluate(2, [a], 35), [a])

    def test_single_negative_missing_frames_id_change_and_save_retry(self):
        gate = PPEEventGate(TrackingConfig())
        a = Violation('Baret Yok', .9, 'a')
        self.assertEqual(gate.evaluate(1, [a], 0), [])
        for now in range(1, 6):
            self.assertEqual(gate.evaluate(1, [], now), [])
        self.assertEqual(gate.evaluate(1, [a], 6), [])
        b = Violation('Baret Yok', .9, 'new-track')
        self.assertEqual(gate.evaluate(1, [b], 7), [])
        self.assertEqual(gate.evaluate(1, [b], 8), [])
        self.assertEqual(gate.evaluate(1, [b], 9), [b])
        self.assertEqual(gate.evaluate(1, [b], 10), [b])  # failed save is retryable
        gate.evaluate(1, [], 200)
        self.assertEqual(gate.states, {})

    def test_zone_window_retains_real_confidence_on_safe_trigger(self):
        state = IhlalTakipDurumu()
        # Existing 5/10 semantics can rearm on a safe frame while five positives remain.
        state.ihlal_aktif = True
        for score in (.6, .7, .8, .75, .65):
            state.kareyi_isle(True, score)
        for _ in range(4):
            self.assertFalse(state.kareyi_isle(False, 0)[1])
        self.assertTrue(state.kareyi_isle(False, 0)[1])
        self.assertEqual(state.confidence, .8)
        state.ihlal_kaydedildi()
        self.assertIsNone(state.confidence)

    def test_database_null_real_confidence_and_two_people(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'regression.db'
            with patch.object(db, 'DB_PATH', path):
                db.prepare_database()
                with closing(db.connect_database()) as conn:
                    frame = np.zeros((20, 20, 3), dtype=np.uint8)
                    gate = PPEEventGate(TrackingConfig())
                    events = [Violation('Baret Yok', .9, 'a'), Violation('Baret Yok', .8, 'b')]
                    for now in range(20):
                        for event in gate.evaluate(1, events, now):
                            save_violation(conn, frame, event, Path(directory), 1)
                            gate.saved(1, event, now)
                    self.assertEqual(conn.execute('SELECT person_id FROM ihlaller ORDER BY id').fetchall(), [('a',), ('b',)])
                    for confidence in (0, None, .87):
                        save_violation(conn, frame, restricted_zone_violation(confidence), Path(directory), 1)
                    self.assertEqual(conn.execute('SELECT guven_skoru FROM ihlaller ORDER BY id').fetchall(), [(90,), (80,), (None,), (None,), (87,)])
                db.prepare_database()
                with closing(sqlite3.connect(path)) as conn:
                    self.assertEqual(conn.execute('SELECT count(*) FROM ihlaller').fetchone()[0], 5)


if __name__ == '__main__':
    unittest.main()

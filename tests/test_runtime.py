import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import closing

import isg_database as db
from camera_health import CameraHealthReporter, health_view, load_health_views
from ai_service.runtime_config import RuntimeConfig
from ai_service.rule_cache import CameraRuleCache, changed_violation_types
from ai_service.rules import CameraPPERules, restricted_zone_violation
from ai_service.event_tracking import PPEEventGate, TrackingConfig


class RuntimeTests(unittest.TestCase):
    def test_config_validation(self):
        for kwargs in ({'rule_refresh_seconds':0}, {'db_timeout_seconds':2},
                       {'health_stale_seconds':1}, {'rule_refresh_seconds':float('nan')}):
            with self.assertRaises(ValueError):
                RuntimeConfig(**kwargs)

    def test_unknown_online_stale_and_offline(self):
        self.assertEqual(health_view()['status'], 'unknown')
        row = dict(status='online', last_frame_at=99, updated_at=99, run_started_at=90, fps=12)
        self.assertEqual(health_view(row, now=100)['status'], 'online')
        self.assertEqual(health_view(row, now=110)['status'], 'offline')
        row['run_started_at'] = 100
        self.assertNotEqual(health_view(row, now=100)['status'], 'online')
        row.update(status='unknown', last_frame_at=None)
        self.assertEqual(health_view(row, now=100)['status'], 'unknown')
        row.update(status='offline', error_code='rtsp://user:secret@camera')
        self.assertEqual(health_view(row, now=100)['error_summary'], '')

    def test_ttl_failure_expiry_recovery_and_unsupported(self):
        clock = Mock(return_value=0)
        helmet = CameraPPERules(frozenset({'helmet', 'ear_protection'}), .7)
        loader = Mock(return_value=[helmet])
        cache = CameraRuleCache(loader, RuntimeConfig(), clock)
        self.assertEqual(cache.get()[0].required, frozenset({'helmet'}))
        for _ in range(100): cache.get()
        self.assertEqual(loader.call_count, 1)
        clock.return_value = 2
        loader.side_effect = sqlite3.OperationalError('locked')
        self.assertEqual(cache.get()[0].confidence_threshold, .7)
        clock.return_value = 30
        self.assertEqual(cache.get(), ())
        clock.return_value = 32
        loader.side_effect = None
        loader.return_value = []
        self.assertEqual(cache.get(), ())
        self.assertFalse(cache.failed)

    def test_rule_change_clears_evidence_preserves_cooldown(self):
        gate = PPEEventGate(TrackingConfig(ppe_required=1))
        event = restricted_zone_violation(.8, 'person')
        gate.evaluate(1, [event], 0)
        gate.saved(1, event, 0)
        gate.reset_rules(1, {event.ihlal_turu})
        self.assertEqual(list(gate.states[(1,'person',event.ihlal_turu)]['history']), [])
        self.assertEqual(gate.evaluate(1, [event], 1), [])
        self.assertEqual(changed_violation_types([], [CameraPPERules(frozenset({'vest'}))]), {'Reflektif Yelek Yok'})

    def test_health_roundtrip_and_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(db, 'DB_PATH', Path(tmp)/'test.db'):
            db.prepare_database()
            reporter = CameraHealthReporter(1, RuntimeConfig())
            reporter.frame_received()
            reporter.queue.join()
            with closing(db.connect_database()) as conn:
                self.assertEqual(load_health_views(conn)[1]['status'], 'online')
            reporter.error('read_failed')
            reporter.close()
            self.assertFalse(reporter.worker.is_alive())
            with closing(db.connect_database()) as conn:
                health = load_health_views(conn)[1]
                self.assertEqual(health['status'], 'offline')
                self.assertNotEqual(health['last_error'], 'Mevcut değil')
                self.assertNotEqual(health['last_frame'], 'Mevcut değil')

    def test_reporter_failure_does_not_break_frames(self):
        reporter = CameraHealthReporter(1, RuntimeConfig(), writer=Mock(side_effect=sqlite3.OperationalError('locked')))
        reporter.frame_received()
        reporter.close()
        self.assertFalse(reporter.worker.is_alive())

    def test_missing_health_table(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            self.assertEqual(load_health_views(conn), {})

    def test_locked_database_refresh_is_bounded_and_recovers(self):
        from ai_service.rules import load_camera_rules
        with tempfile.TemporaryDirectory() as tmp, patch.object(db, 'DB_PATH', Path(tmp)/'test.db'):
            db.prepare_database()
            with closing(db.connect_database()) as writer, closing(db.connect_database()) as reader:
                writer.execute("INSERT INTO kamera_ppe_kurallari VALUES(1,'helmet',1,.5)")
                writer.commit()
                reader.execute('PRAGMA busy_timeout=100')
                clock = Mock(return_value=0)
                cache = CameraRuleCache(lambda: load_camera_rules(reader, 1), RuntimeConfig(), clock)
                self.assertEqual(len(cache.get()), 1)
                writer.execute('BEGIN EXCLUSIVE')
                writer.execute('UPDATE kamera_ppe_kurallari SET aktif=0')
                clock.return_value = 2
                start = time.monotonic()
                self.assertEqual(len(cache.get()), 1)
                self.assertLess(time.monotonic()-start, 1)
                writer.commit()
                clock.return_value = 4
                self.assertEqual(cache.get(), ())

    def test_new_session_never_inherits_online_from_old_frame(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(db, 'DB_PATH', Path(tmp)/'test.db'):
            db.prepare_database()
            first = CameraHealthReporter(1, RuntimeConfig())
            first.frame_received()
            first.close()
            second = CameraHealthReporter(1, RuntimeConfig())
            try:
                second.queue.join()
                with closing(db.connect_database()) as conn:
                    health = load_health_views(conn)[1]
                    self.assertEqual(health['status'], 'unknown')
                    self.assertNotEqual(health['last_frame'], 'Mevcut değil')
            finally:
                second.close()

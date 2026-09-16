import io
import logging
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import isg_database as db
from ai_service.camera_sources import CameraConfig, load_cameras
from ai_service.camera_runtime import CameraManager, ManagerSettings, LatestFrame


class Health:
    def __init__(self):
        self.states = ['unknown']
        self.closed = False

    def frame_received(self):
        if self.states[-1] != 'online':
            self.states.append('online')

    def error(self, code):
        self.states.append('offline')

    def close(self):
        self.closed = True


class Capture:
    def __init__(self, source, failures):
        self.source, self.failures = source, failures
        self.released = False
        self.sequence = 0
        self.interval = .005

    def isOpened(self):
        return True

    def read(self):
        time.sleep(self.interval)
        if self.source.id in self.failures:
            raise RuntimeError('decoder failure')
        self.sequence += 1
        return True, self.sequence

    def release(self):
        self.released = True


class MultiCameraTests(unittest.TestCase):
    def setUp(self):
        self.configs = [CameraConfig(i, kamera_index=i, reconnect_interval=.01) for i in range(1, 4)]
        self.failures, self.captures, self.health, self.results = set(), [], {}, []
        def factory(config):
            capture = Capture(config, self.failures)
            self.captures.append(capture)
            return capture
        def health(camera_id):
            self.health[camera_id] = Health()
            return self.health[camera_id]
        self.manager = CameraManager(lambda: self.configs,
            lambda c, f: self.results.append((c.id, f)), health, factory, ManagerSettings(.02, 10, .05))

    def tearDown(self):
        self.manager.close()

    def run_for(self, seconds=.15):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.manager.tick()
            time.sleep(.001)

    def test_three_cameras(self):
        self.run_for()
        self.assertEqual({i for i, f in self.results}, {1, 2, 3})

    def test_exception_isolation_reconnect_health(self):
        self.run_for()
        self.failures.add(2)
        self.run_for(.25)
        self.assertEqual(self.health[2].states[-1], 'offline')
        self.assertEqual(self.health[1].states[-1], 'online')
        self.failures.clear()
        self.run_for(.25)
        self.assertEqual(self.health[2].states, ['unknown', 'online', 'offline', 'online'])
        self.assertGreater(sum(c.source.id == 2 for c in self.captures), 1)

    def test_dynamic_add_disable_source_change(self):
        self.run_for()
        first = self.manager.workers[1]
        self.configs.append(CameraConfig(4, kamera_index=4))
        self.run_for()
        self.assertIn(4, self.manager.workers)
        self.configs[1] = replace(self.configs[1], aktif=0)
        self.configs[2] = replace(self.configs[2], kamera_index=8)
        old = self.manager.workers[3]
        self.run_for()
        self.assertNotIn(2, self.manager.workers)
        self.assertIs(self.manager.workers[1], first)
        self.assertIsNot(self.manager.workers[3], old)
        self.assertFalse(old.thread.is_alive())

    def test_fps_throttle_latest_and_config_updates(self):
        self.configs = self.configs[:1]
        self.manager.refresh()
        time.sleep(.01)
        self.captures[0].interval = .04  # 25 FPS input, 5 FPS target
        self.run_for(1.05)
        self.assertTrue(5 <= len(self.results) <= 6, self.results)
        self.assertGreater(self.results[-1][1], 20)
        worker = self.manager.workers[1]
        self.assertGreater(worker.frames.dropped, 10)
        self.configs[0] = replace(self.configs[0], kamera_adi='Changed', analysis_enabled=0, analysis_fps=2)
        self.run_for(.05)
        count = len(self.results)
        self.run_for(.25)
        self.assertEqual(count, len(self.results))
        self.assertIs(worker, self.manager.workers[1])

    def test_deletion_and_db_failure(self):
        self.run_for()
        self.manager.loader = lambda: (_ for _ in ()).throw(sqlite3.OperationalError('offline'))
        self.run_for()
        self.assertEqual(len(self.manager.workers), 3)
        self.manager.loader = lambda: []
        self.run_for()
        self.assertFalse(self.manager.workers)

    def test_inference_exception_does_not_stop_capture(self):
        def analyze(config, frame):
            if config.id == 2:
                raise RuntimeError('inference failure')
            self.results.append((config.id, frame))
        self.manager.analyze = analyze
        self.run_for()
        self.assertEqual({i for i, f in self.results}, {1, 3})

    def test_graceful_shutdown(self):
        self.run_for()
        workers = list(self.manager.workers.values())
        self.manager.close()
        self.assertTrue(all(not w.thread.is_alive() for w in workers))
        self.assertTrue(all(c.released for c in self.captures))
        self.assertTrue(all(h.closed for h in self.health.values()))

    def test_latest_buffer_bounded(self):
        frames = LatestFrame()
        for i in range(10000):
            frames.put(i, i)
        self.assertEqual(frames.take(), (9999, 9999))
        self.assertIsNone(frames.take())
        self.assertEqual(frames.dropped, 9999)

    def test_repeated_image_freeze_detection(self):
        from ai_service.camera_capture import FreezeWatch
        watch = FreezeWatch(10)
        self.assertFalse(watch.frozen(b'frame one', 0))
        self.assertFalse(watch.frozen(b'frame one', 9))
        self.assertTrue(watch.frozen(b'frame one', 10))
        self.assertFalse(watch.frozen(b'frame two', 11))
        self.assertFalse(FreezeWatch(0).frozen(b'frame', 100))

    def test_stale_read_reconnects(self):
        self.manager.settings = ManagerSettings(.02, .001, .01)
        self.run_for(.1)
        self.assertFalse(self.results)
        self.assertTrue(all(h.states[-1] == 'offline' for h in self.health.values()))
        self.assertGreater(len(self.captures), 3)

    def test_open_failure_isolated(self):
        original = self.manager.capture_factory
        def factory(config):
            capture = original(config)
            if config.id == 2:
                capture.isOpened = lambda: False
            return capture
        self.manager.capture_factory = factory
        self.run_for()
        self.assertEqual({i for i, f in self.results}, {1, 3})
        self.assertEqual(self.health[2].states[-1], 'offline')

    def test_analyzer_keeps_three_camera_associations(self):
        from unittest.mock import Mock
        from ai_service.multi_camera_service import CameraAnalyzer
        from ai_service.runtime_config import RuntimeConfig
        analyzer = CameraAnalyzer(Mock(), Mock(), Mock(), RuntimeConfig())
        with patch('ai_service.multi_camera_service.load_camera_rules', return_value=[]) as rules, \
             patch('ai_service.main.kareyi_analiz_et', return_value=(True, .9)), \
             patch('ai_service.main.ihlal_kaydi_olustur') as save:
            for _ in range(5):
                for config in self.configs:
                    analyzer(config, Mock())
            self.assertEqual({call.kwargs['kamera_id'] for call in save.call_args_list}, {1, 2, 3})
            self.assertEqual({call.args[1] for call in rules.call_args_list}, {1, 2, 3})
            analyzer.retire(2)
            self.assertEqual(set(analyzer.states), {1, 3})

    def test_migration_usb_zero_and_multiple_rtsp(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(db, 'DB_PATH', Path(tmp)/'test.db'):
            db.prepare_database()
            db.prepare_database()
            with closing(db.connect_database()) as connection, connection:
                self.assertEqual(load_cameras(connection)[0].source(), 0)
                for i in range(2):
                    connection.execute("INSERT INTO kameralar(kamera_adi,kamera_index,source_type,source_uri,olusturma_tarihi,guncelleme_tarihi) VALUES ('IP',0,'rtsp','rtsp://10.0.0.1/test','','')")
                self.assertEqual(len(load_cameras(connection)), 3)

    def test_credentials_rejected_without_echo(self):
        secret = 'secret123'
        with self.assertRaises(ValueError) as raised:
            CameraConfig(1, source_type='rtsp', source_uri=f'rtsp://admin:{secret}@10.0.0.1/test')
        self.assertNotIn(secret, str(raised.exception))
        config = CameraConfig(1, source_type='rtsp', source_uri='rtsp://10.0.0.1/test', credential_env='ISG_CAMERA_TEST')
        with patch.dict('os.environ', ISG_CAMERA_TEST=f'rtsp://admin:{secret}@10.0.0.1/test'):
            self.assertIn(secret, config.source())
            self.assertNotIn(secret, repr(config))

    def test_native_video_process_and_release(self):
        import cv2
        import numpy as np
        from ai_service.camera_capture import ProcessCapture
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/'sample.avi')
            writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*'MJPG'), 25, (64, 48))
            self.assertTrue(writer.isOpened())
            for i in range(50):
                writer.write(np.full((48, 64, 3), i, dtype=np.uint8))
            writer.release()
            capture = ProcessCapture(CameraConfig(9, source_type='video', source_uri=path, connection_timeout=10))
            try:
                ok, frame = capture.read()
                self.assertTrue(ok)
                self.assertEqual(frame.shape, (48, 64, 3))
            finally:
                capture.release()
            self.assertFalse(capture.process.is_alive())

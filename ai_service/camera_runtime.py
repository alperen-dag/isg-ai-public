"""Independent capture workers and fair, single-model inference scheduling."""
from dataclasses import dataclass, replace
import logging
import os
from threading import Event, Lock, Thread
import time

from ai_service.camera_capture import ProcessCapture

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ManagerSettings:
    refresh: float = 2
    stale: float = 10
    reconnect_max: float = 10

    @classmethod
    def environment(cls):
        import math
        values = [float(os.getenv(key, default)) for key, default in (
            ('CAMERA_CONFIG_REFRESH_SECONDS', '2'), ('CAMERA_STALE_SECONDS', '10'),
            ('CAMERA_RECONNECT_MAX_SECONDS', '10'))]
        if any(not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError('Invalid camera runtime intervals')
        if int(os.getenv('MAX_INFERENCE_CONCURRENCY', '1')) != 1:
            raise ValueError('This GPU scheduler supports MAX_INFERENCE_CONCURRENCY=1')
        return cls(*values)


class LatestFrame:
    def __init__(self):
        self.lock = Lock()
        self.value = None
        self.dropped = 0

    def put(self, frame, now):
        with self.lock:
            if self.value is not None:
                self.dropped += 1
            self.value = (frame, now)

    def take(self):
        with self.lock:
            value, self.value = self.value, None
            return value


class CameraWorker:
    def __init__(self, config, factory, health, settings):
        self.config, self.factory, self.health, self.settings = config, factory, health, settings
        self.frames = LatestFrame()
        self.stop = Event()
        self.next_analysis = 0
        self.last_error = 'unknown'
        self.inference_failed = False
        self.thread = Thread(target=self.run, name=f'capture-{config.id}', daemon=True)

    def error(self, code):
        self.frames.take()
        if self.last_error != code:
            LOG.warning('camera_id=%s event=%s', self.config.id, code)
            self.health.error(code)
            self.last_error = code

    def run(self):
        delay = self.config.reconnect_interval
        try:
            while not self.stop.is_set():
                capture = None
                try:
                    capture = self.factory(replace(self.config,
                        connection_timeout=min(self.config.connection_timeout, self.settings.stale)))
                    if not capture.isOpened():
                        self.error('STREAM_OPEN_FAILED')
                    else:
                        while not self.stop.is_set():
                            started = time.monotonic()
                            ok, frame = capture.read()
                            elapsed = time.monotonic() - started
                            if not ok or frame is None or elapsed > self.settings.stale:
                                self.error('STREAM_TIMEOUT' if elapsed >= self.config.connection_timeout else 'STREAM_READ_FAILED')
                                break
                            if self.last_error is not None:
                                LOG.info('camera_id=%s event=stream_connected', self.config.id)
                            self.last_error = None
                            self.health.frame_received()
                            self.frames.put(frame, time.monotonic())
                            delay = self.config.reconnect_interval
                except Exception:
                    self.error('STREAM_READ_FAILED')
                finally:
                    if capture is not None:
                        try:
                            capture.release()
                        except Exception:
                            self.error('STREAM_READ_FAILED')
                if self.stop.wait(min(delay, self.settings.reconnect_max)):
                    break
                delay = min(delay * 2, self.settings.reconnect_max)
        finally:
            self.frames.take()
            self.health.close()

    def close(self):
        self.stop.set()
        self.thread.join(self.config.connection_timeout + 2)
        if self.thread.is_alive():
            LOG.error('camera_id=%s event=worker_shutdown_timeout', self.config.id)


class CameraManager:
    def __init__(self, loader, analyze, health_factory, capture_factory=ProcessCapture,
                 settings=None, retire=None, clock=time.monotonic):
        self.loader, self.analyze, self.health_factory = loader, analyze, health_factory
        self.capture_factory = capture_factory
        self.settings = settings or ManagerSettings.environment()
        self.retire = retire or (lambda camera_id: None)
        self.clock = clock
        self.workers = {}
        self.next_refresh = 0
        self.cursor = 0
        self.failed = False

    def refresh(self):
        try:
            configs = {c.id: c for c in self.loader() if c.aktif}
        except Exception:
            if not self.failed:
                LOG.warning('event=camera_config_unavailable')
            self.failed = True
            return
        self.failed = False
        for camera_id, worker in list(self.workers.items()):
            config = configs.get(camera_id)
            if config is None or config.capture_key != worker.config.capture_key:
                worker.close()
                del self.workers[camera_id]
                self.retire(camera_id)
            else:
                worker.config = config
        for camera_id, config in configs.items():
            if camera_id not in self.workers:
                worker = CameraWorker(config, self.capture_factory, self.health_factory(camera_id), self.settings)
                self.workers[camera_id] = worker
                worker.thread.start()

    def tick(self):
        now = self.clock()
        if now >= self.next_refresh:
            self.next_refresh = now + self.settings.refresh
            self.refresh()
        workers = [w for w in self.workers.values() for _ in range(w.config.priority)]
        # One item per tick, rotating the starting point prevents hot-camera starvation.
        for _ in workers:
            worker = workers[self.cursor % len(workers)]
            self.cursor += 1
            if not worker.config.analysis_enabled or now < worker.next_analysis:
                continue
            value = worker.frames.take()
            if value is None:
                continue
            frame, captured = value
            if now - captured > self.settings.stale:
                continue
            worker.next_analysis = now + 1 / worker.config.analysis_fps
            try:
                self.analyze(worker.config, frame)
                worker.inference_failed = False
            except Exception:
                if not worker.inference_failed:
                    LOG.error('camera_id=%s event=inference_failed', worker.config.id)
                worker.inference_failed = True
            return True
        return False

    def close(self):
        for worker in self.workers.values():
            worker.stop.set()
        for camera_id, worker in self.workers.items():
            worker.close()
            self.retire(camera_id)
        self.workers.clear()

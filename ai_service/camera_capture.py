"""Killable native capture processes; one bounded latest-frame IPC slot each."""
import multiprocessing as mp
import os
from queue import Empty, Full
import time
import zlib
import math

from ai_service.camera_sources import opencv_capture


class FreezeWatch:
    """Sample once per second; repeated pixel data is distinct from read timeout."""
    def __init__(self, seconds=10):
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError('Invalid freeze interval')
        self.seconds = seconds
        self.next_sample = 0
        self.changed_at = None
        self.signature = None

    def frozen(self, frame, now):
        if self.seconds == 0 or now < self.next_sample:
            return False
        self.next_sample = now + min(1, self.seconds)
        signature = zlib.crc32(memoryview(frame))
        if signature != self.signature:
            self.signature, self.changed_at = signature, now
        return self.changed_at is not None and now - self.changed_at >= self.seconds


def _capture(config, queue, stop):
    # Redirect in this child only: native FFmpeg stderr can contain credentials.
    with open(os.devnull, 'w') as sink:
        os.dup2(sink.fileno(), 2)
        capture = None
        try:
            freeze = FreezeWatch(float(os.getenv('CAMERA_FREEZE_SECONDS', '10')))
            capture = opencv_capture(config)
            if not capture.isOpened():
                return
            while not stop.is_set():
                ok, frame = capture.read()
                if not ok:
                    return
                if freeze.frozen(frame, time.monotonic()):
                    return
                try:
                    queue.put_nowait((time.monotonic(), frame))
                except Full:
                    try:
                        queue.get_nowait()
                    except Empty:
                        continue
                    try:
                        queue.put_nowait((time.monotonic(), frame))
                    except Full:
                        continue
                if config.source_type == 'video':
                    import cv2
                    fps = capture.get(cv2.CAP_PROP_FPS)
                    stop.wait(1 / (fps if 0 < fps <= 240 else 25))
        except Exception:
            return  # Parent emits only allowlisted failure codes.
        finally:
            if capture is not None:
                capture.release()
            queue.cancel_join_thread()


class ProcessCapture:
    def __init__(self, config):
        self.config = config
        context = mp.get_context('spawn')
        self.queue = context.Queue(maxsize=1)
        self.stop = context.Event()
        self.process = context.Process(target=_capture, args=(config, self.queue, self.stop), daemon=True)
        self.process.start()

    def isOpened(self):
        return self.process.is_alive()

    def read(self):
        deadline = time.monotonic() + self.config.connection_timeout
        while time.monotonic() < deadline and not self.stop.is_set():
            try:
                captured, frame = self.queue.get(timeout=.1)
                if time.monotonic() - captured <= self.config.connection_timeout:
                    return True, frame
            except Empty:
                if not self.process.is_alive():
                    break
        return False, None

    def release(self):
        self.stop.set()
        self.process.join(.2)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(1)
        self.queue.close()
        self.queue.cancel_join_thread()

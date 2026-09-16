"""Synthetic scheduling benchmark: no cameras, model, credentials or production DB."""
import json
from pathlib import Path
import sys
import threading
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_service.camera_sources import CameraConfig
from ai_service.camera_runtime import CameraManager
from ai_service.runtime_config import RuntimeConfig
from camera_health import CameraHealthReporter, HealthDispatcher


class GeneratedCapture:
    def __init__(self, config):
        self.frame = bytearray(320 * 180 * 3)

    def isOpened(self):
        return True

    def read(self):
        time.sleep(.04)
        return True, self.frame[:]

    def release(self):
        self.frame = None


def benchmark(count, seconds=4):
    writes = []
    dispatcher = HealthDispatcher(writer=lambda *args: writes.append(time.monotonic()))
    seen = {}
    def analyze(config, frame):
        seen[config.id] = seen.get(config.id, 0) + 1
    configs = [CameraConfig(i, kamera_index=i) for i in range(count)]
    manager = CameraManager(lambda: configs, analyze,
        lambda i: CameraHealthReporter(i, RuntimeConfig(), dispatcher=dispatcher), GeneratedCapture)
    tracemalloc.start()
    start, cpu = time.monotonic(), time.process_time()
    samples = []
    try:
        while time.monotonic() - start < seconds:
            manager.tick()
            time.sleep(.001)
            samples.append(tracemalloc.get_traced_memory()[0])
        elapsed = time.monotonic() - start
        result = dict(cameras=count, seconds=round(elapsed, 2), analyzed_cameras=len(seen),
            analyzed_frames=sum(seen.values()), min_frames=min(seen.values()), max_frames=max(seen.values()),
            threads=threading.active_count(), capture_processes=0,
            cpu_seconds=round(time.process_time()-cpu, 3),
            python_memory_mb=round(samples[-1]/1024**2, 2),
            late_memory_growth_mb=round((samples[-1]-samples[len(samples)//2])/1024**2, 2),
            peak_python_memory_mb=round(tracemalloc.get_traced_memory()[1]/1024**2, 2),
            buffered_frames=sum(w.frames.value is not None for w in manager.workers.values()),
            max_frames_per_buffer=1, health_writes=len(writes),
            health_writes_per_second=round(len(writes)/elapsed, 2))
        assert len(seen) == count
        assert result['buffered_frames'] <= count
        assert result['late_memory_growth_mb'] < 10
        return result
    finally:
        manager.close()
        dispatcher.close()
        tracemalloc.stop()


if __name__ == '__main__':
    result = [benchmark(n) for n in (10, 25, 50, 100)]
    path = Path(__file__).resolve().parents[1] / 'reports/multi_camera_scale.json'
    path.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))

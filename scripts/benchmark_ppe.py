"""Measure both real models on the configured camera; never write violations/DB."""
import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import statistics
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cv2
from ai_service.main import aktif_kamera_ayarini_getir, kamerayi_ac, kareyi_analiz_et, modeli_yukle
from ai_service.ppe_config import load_ppe_config
from ai_service.ppe_pipeline import PPEPipeline
from ai_service.rules import CameraPPERules
from ai_service.yolo_ppe_detector import YOLOPPEDetector
from isg_database import connect_database


class TimedPose:
    def __init__(self, model):
        self.model = model

    def __call__(self, *args, **kwargs):
        start = perf_counter()
        results = self.model(*args, **kwargs)
        self.wall_ms = (perf_counter() - start) * 1000
        self.inference_ms = sum(r.speed['inference'] for r in results)
        return results


def run(frames, warmup, display):
    with closing(connect_database()) as connection:
        camera_config = aktif_kamera_ayarini_getir(connection)
    pose = TimedPose(modeli_yukle())
    detector = YOLOPPEDetector(load_ppe_config())
    pipeline = PPEPipeline(detector)
    # Benchmark evaluates supported rules without changing camera configuration.
    rules = [CameraPPERules(frozenset(['helmet', 'vest']), .5)]
    camera = kamerayi_ac(camera_config['kamera_index'])
    samples = []
    event_counts = Counter()
    positive_zone_frames = 0
    person_frames = 0
    try:
        for index in range(warmup + frames):
            start = perf_counter()
            ok, raw = camera.read()
            if not ok:
                raise RuntimeError('Camera read failed')
            annotated = raw.copy()
            people = []
            zone, _ = kareyi_analiz_et(pose, annotated, camera_config['yasak_bolge_orani'], people)
            events = pipeline.evaluate(raw, people, rules)
            if pipeline.failed or detector.last_inference_ms is None:
                raise RuntimeError('PPE failed; do not report failed inference as performance')
            if display:
                cv2.putText(annotated, 'BENCHMARK - DB kaydi yok', (10, 60), cv2.FONT_HERSHEY_SIMPLEX, .6, (255,255,255), 2)
                cv2.imshow('ISG PPE benchmark', annotated)
                if cv2.waitKey(1) & 0xff == ord('q'):
                    break
            elapsed = perf_counter() - start
            if index >= warmup:
                samples.append((pose.inference_ms, pose.wall_ms, detector.last_inference_ms, elapsed,
                                detector.last_model_inference_ms))
                event_counts.update(e.ihlal_turu for e in events)
                positive_zone_frames += int(zone)
                person_frames += int(bool(people))
    finally:
        camera.release()
        if display:
            cv2.destroyAllWindows()
    if not samples:
        raise RuntimeError('No measured frames')
    def stats(column):
        values = sorted(row[column] for row in samples)
        return {'mean_ms': round(statistics.mean(values), 2),
                'p95_ms': round(values[min(len(values)-1, int(.95*len(values)))], 2)}
    return {'frames': len(samples), 'warmup_frames': warmup,
            'camera_id': camera_config['id'], 'resolution': list(raw.shape[:2][::-1]),
            'device': detector.config.device, 'ppe_imgsz': detector.config.image_size,
            'model_classes': detector.names, 'supported_classes': sorted(detector.supported_classes),
            'pose_inference': stats(0), 'pose_predict_wall': stats(1), 'ppe_predict_wall': stats(2),
            'ppe_inference': stats(4),
            'total_fps': round(len(samples) / sum(row[3] for row in samples), 2),
            'frames_with_accepted_people': person_frames, 'zone_positive_frames': positive_zone_frames,
            'ppe_event_observations': dict(event_counts), 'db_writes': 0,
            'note': 'Events are per-frame observations, not distinct people or accuracy measurements.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frames', type=int, default=120)
    parser.add_argument('--warmup', type=int, default=10)
    parser.add_argument('--display', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.frames < 1 or args.warmup < 0:
        parser.error('frames must be positive and warmup nonnegative')
    report = run(args.frames, args.warmup, args.display)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding='utf-8')
    print(payload)

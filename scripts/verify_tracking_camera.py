"""Bounded real-camera observation; read-only database, no images or event writes."""
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ai_service.main import aktif_kamera_ayarini_getir, kamerayi_ac, kareyi_analiz_et, modeli_yukle, IhlalTakipDurumu
from ai_service.event_tracking import PersonTracker, PPEEventGate, load_tracking_config
from ai_service.ppe_pipeline import PPEPipeline
from ai_service.yolo_ppe_detector import create_ppe_detector
from ai_service.rules import CameraPPERules


def run(seconds=30):
    with closing(sqlite3.connect((ROOT / 'isg.db').as_uri() + '?mode=ro', uri=True)) as connection:
        config = aktif_kamera_ayarini_getir(connection)
    model = modeli_yukle()
    pipeline = PPEPipeline(create_ppe_detector())
    tracker_config = load_tracking_config()
    tracker, gate, zone_state = PersonTracker(tracker_config), PPEEventGate(tracker_config), IhlalTakipDurumu()
    rules = [CameraPPERules(frozenset(['helmet', 'vest']))]
    camera = kamerayi_ac(config['kamera_index'])
    report = dict(frames=0, people_count_frames=Counter(), track_frames=Counter(),
                  raw_ppe=Counter(), eligible_ppe=Counter(), zone_confidences=[],
                  empty_to_person_transitions=0, ppe_failed_frames=0, tracker_failed_frames=0,
                  db_writes=0, image_writes=0)
    previous_count = None
    start = time.monotonic()
    try:
        while time.monotonic() - start < seconds:
            ok, frame = camera.read()
            if not ok:
                raise RuntimeError('Camera read failed')
            raw = frame.copy()
            people = []
            zone, confidence = kareyi_analiz_et(model, frame, config['yasak_bolge_orani'], people)
            people = tracker.update(people)
            report['tracker_failed_frames'] += int(tracker.failed)
            _, new = zone_state.kareyi_isle(zone, confidence)
            if new:
                report['zone_confidences'].append(zone_state.confidence)
                zone_state.ihlal_kaydedildi()
            now = time.monotonic()
            events = pipeline.evaluate(raw, people, rules)
            report['ppe_failed_frames'] += int(pipeline.failed)
            report['raw_ppe'].update(e.ihlal_turu for e in events)
            for event in gate.evaluate(config['id'], events, now):
                report['eligible_ppe'][event.ihlal_turu] += 1
                gate.saved(config['id'], event, now)  # Simulated successful write only.
            report['frames'] += 1
            report['people_count_frames'][len(people)] += 1
            report['track_frames'].update(p.person_id for p in people)
            report['empty_to_person_transitions'] += int(previous_count == 0 and bool(people))
            previous_count = len(people)
    finally:
        camera.release()
    report['elapsed_seconds'] = round(time.monotonic() - start, 2)
    report['note'] = 'Unsupervised camera observations; transitions are detector observations, not proof of physical exit/re-entry. No controlled two-person claim.'
    report['supported_classes'] = sorted(getattr(pipeline.detector, 'supported_classes', ()))
    return report


if __name__ == '__main__':
    report = run()
    output = ROOT / 'reports/tracking_camera.json'
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(output.read_text(encoding='utf-8'))

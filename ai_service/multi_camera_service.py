"""Production entry point. All model calls and rule state live on one scheduler."""
import os
import signal
from threading import Event

from isg_database import connect_database, prepare_database
from camera_health import CameraHealthReporter, HealthDispatcher
from ai_service.camera_sources import load_cameras
from ai_service.camera_runtime import CameraManager
from ai_service.runtime_config import load_runtime_config
from ai_service.rule_cache import CameraRuleCache, changed_violation_types
from ai_service.rules import load_camera_rules
from ai_service.event_tracking import PersonTracker, PPEEventGate, load_tracking_config
from ai_service.ppe_pipeline import PPEPipeline
from ai_service.yolo_ppe_detector import create_ppe_detector
from ai_service.violation_store import save_violation


class CameraAnalyzer:
    def __init__(self, model, detector, connection, runtime):
        self.model, self.connection, self.runtime = model, connection, runtime
        self.pipeline = PPEPipeline(detector)
        self.states = {}

    def retire(self, camera_id):
        self.states.pop(camera_id, None)

    def __call__(self, config, frame):
        import time
        from ai_service.main import IhlalTakipDurumu, kareyi_analiz_et, ihlal_kaydi_olustur, IHLAL_KLASORU
        if config.id not in self.states:
            tracking = load_tracking_config()
            self.states[config.id] = dict(zone=IhlalTakipDurumu(), tracker=PersonTracker(tracking),
                gate=PPEEventGate(tracking), rules=(),
                cache=CameraRuleCache(lambda: load_camera_rules(self.connection, config.id), self.runtime))
        state = self.states[config.id]
        if state.get('zone_ratio') != config.yasak_bolge_orani:
            state['zone'] = IhlalTakipDurumu()
            state['zone_ratio'] = config.yasak_bolge_orani
        rules = state['cache'].get()
        changed = changed_violation_types(state['rules'], rules)
        if changed:
            state['gate'].reset_rules(config.id, changed)
        state['rules'] = rules
        raw = frame.copy()
        people = []
        violation, confidence = kareyi_analiz_et(self.model, frame, config.yasak_bolge_orani, people)
        _, new = state['zone'].kareyi_isle(violation, confidence)
        if new:
            ihlal_kaydi_olustur(self.connection, frame, state['zone'].confidence, kamera_id=config.id)
            state['zone'].ihlal_kaydedildi()
        people = state['tracker'].update(people)
        now = time.monotonic()
        for event in state['gate'].evaluate(config.id, self.pipeline.evaluate(raw, people, rules), now):
            save_violation(self.connection, frame, event, IHLAL_KLASORU, config.id)
            state['gate'].saved(config.id, event, now)


def run():
    from ai_service.main import modeli_yukle, IHLAL_KLASORU
    prepare_database()
    runtime = load_runtime_config()
    stop = Event()
    handlers = {}
    connection = connect_database()
    manager = None
    dispatcher = HealthDispatcher()
    try:
        connection.execute(f'PRAGMA busy_timeout={int(runtime.db_timeout_seconds * 1000)}')
        analyzer = CameraAnalyzer(modeli_yukle(), create_ppe_detector(), connection, runtime)
        IHLAL_KLASORU.mkdir(parents=True, exist_ok=True)
        manager = CameraManager(lambda: load_cameras(connection, os.getenv('CAMERA_WORKER_GROUP')),
            analyzer, lambda camera_id: CameraHealthReporter(camera_id, runtime, dispatcher=dispatcher), retire=analyzer.retire)
        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers[sig] = signal.signal(sig, lambda *_: stop.set())
        while not stop.is_set():
            if not manager.tick():
                stop.wait(.005)
    finally:
        if manager is not None:
            manager.close()
        dispatcher.close()
        connection.close()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)

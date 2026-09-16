"""Small camera-local IoU tracker and independently acknowledged event cooldown."""
from collections import deque
from dataclasses import dataclass, replace
import json
import logging
import math
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class TrackingConfig:
    enabled: bool = True
    iou_threshold: float = .3
    max_missing_frames: int = 10
    ppe_window: int = 5
    ppe_required: int = 3
    cooldown_seconds: float = 30
    state_ttl_seconds: float = 120

    def __post_init__(self):
        if not 0 < self.iou_threshold <= 1:
            raise ValueError('Invalid tracking IoU threshold')
        if not 1 <= self.ppe_required <= self.ppe_window or self.max_missing_frames < 0:
            raise ValueError('Invalid frame thresholds')
        if not all(math.isfinite(v) for v in (self.cooldown_seconds, self.state_ttl_seconds)):
            raise ValueError('Invalid time thresholds')
        if self.cooldown_seconds < 0 or self.state_ttl_seconds <= self.cooldown_seconds:
            raise ValueError('State TTL must exceed cooldown')


def load_tracking_config():
    path = Path(__file__).resolve().parents[1] / 'config/tracking.json'
    return TrackingConfig(**json.loads(path.read_text(encoding='utf-8')))


def iou(a, b):
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - intersection
    return intersection / union if union > 0 else 0


class PersonTracker:
    def __init__(self, config):
        self.config = config
        self.session = uuid4().hex[:12]
        self.frame = 0
        self.serial = 0
        self.tracks = {}
        self.failed = False
        if not config.enabled:
            logging.getLogger(__name__).warning(
                'Tracker disabled: zone/detection continue; multi-frame PPE confirmation cannot accumulate')

    def update(self, people):
        self.frame += 1
        if self.config.enabled:
            try:
                result = self._update(people)
                self.failed = False
                return result
            except Exception:
                if not self.failed:
                    logging.getLogger(__name__).exception('Tracker failed; detection continues with ephemeral IDs')
                self.failed = True
                self.tracks.clear()
        # Never pretend frame positions identify physical people. Zone still works;
        # temporal PPE confirmation cannot accumulate across these ephemeral IDs.
        return [replace(p, person_id=f'{self.session}:frame:{self.frame}:{i}') for i, p in enumerate(people)]

    def _update(self, people):
        self.tracks = {key: value for key, value in self.tracks.items()
                       if self.frame - value[1] <= self.config.max_missing_frames + 1}
        candidates = sorted(((iou(p.bbox, box), index, key)
                             for index, p in enumerate(people)
                             for key, (box, _) in self.tracks.items()), reverse=True)
        assigned, used = {}, set()
        for score, index, key in candidates:
            if score >= self.config.iou_threshold and index not in assigned and key not in used:
                assigned[index] = key
                used.add(key)
        result = []
        for index, person in enumerate(people):
            if index not in assigned:
                self.serial += 1
                assigned[index] = f'{self.session}:track:{self.serial}'
            key = assigned[index]
            self.tracks[key] = (person.bbox, self.frame)
            result.append(replace(person, person_id=key))
        return result


class PPEEventGate:
    def __init__(self, config):
        self.config = config
        self.states = {}

    @staticmethod
    def key(camera_id, event):
        return camera_id, event.person_id, event.ihlal_turu

    def evaluate(self, camera_id, events, now):
        self.states = {key: state for key, state in self.states.items()
                       if now - state['seen'] <= self.config.state_ttl_seconds}
        current = {self.key(camera_id, event): event for event in events if event.person_id is not None}
        for key in current:
            self.states.setdefault(key, dict(history=deque(maxlen=self.config.ppe_window),
                                            seen=now, saved=float('-inf')))
        eligible = []
        for key, state in self.states.items():
            if key[0] != camera_id:
                continue
            state['history'].append(key in current)
            if key in current:
                state['seen'] = now
                if (sum(state['history']) >= self.config.ppe_required
                        and now - state['saved'] >= self.config.cooldown_seconds):
                    eligible.append(current[key])
        return eligible

    def reset_rules(self, camera_id, violation_types):
        # Clear accumulated evidence but preserve cooldown across rule edits.
        for key, state in self.states.items():
            if key[0] == camera_id and key[2] in violation_types:
                state['history'].clear()

    def saved(self, camera_id, event, now):
        self.states[self.key(camera_id, event)]['saved'] = now

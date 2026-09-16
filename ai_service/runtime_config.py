"""Small shared runtime settings; no model or tracking configuration changes."""
from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path


@dataclass(frozen=True)
class RuntimeConfig:
    health_write_seconds: float = 2
    health_stale_seconds: float = 10
    rule_refresh_seconds: float = 2
    rule_max_stale_seconds: float = 30
    db_timeout_seconds: float = .1

    def __post_init__(self):
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                   and math.isfinite(v) and v > 0 for v in vars(self).values()):
            raise ValueError('Runtime intervals must be positive finite numbers')
        if self.health_stale_seconds <= self.health_write_seconds:
            raise ValueError('Health stale interval must exceed write interval')
        if self.rule_max_stale_seconds < self.rule_refresh_seconds:
            raise ValueError('Rule max stale must cover the refresh interval')
        if self.db_timeout_seconds > 1:
            raise ValueError('Runtime DB timeout must not exceed one second')


def load_runtime_config():
    path = Path(__file__).resolve().parents[1] / 'config/runtime.json'
    try:
        values = json.loads(path.read_text(encoding='utf-8'))
        for key, env in [('health_write_seconds', 'CAMERA_HEALTH_DB_FLUSH_SECONDS'),
                         ('health_stale_seconds', 'CAMERA_STALE_SECONDS')]:
            if env in os.environ:
                values[key] = float(os.environ[env])
        return RuntimeConfig(**values)
    except (OSError, ValueError, TypeError):
        logging.getLogger(__name__).warning('Runtime config unavailable/invalid; using safe defaults')
        return RuntimeConfig()

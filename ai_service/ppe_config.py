from dataclasses import dataclass
import json
import math
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class PPEModelConfig:
    model_path: Path
    confidence: float = .25
    image_size: int = 640
    device: str = 'cpu'
    sha256: str | None = None
    expected_names: tuple[str, ...] = ()

    def __post_init__(self):
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError('PPE confidence must be in [0, 1]')
        if not 32 <= self.image_size <= 2048 or self.image_size % 32:
            raise ValueError('PPE image_size must be a multiple of 32 in [32, 2048]')


def load_ppe_config():
    config_path = Path(os.environ.get('ISG_PPE_CONFIG', ROOT / 'config/ppe.json'))
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    data = json.loads(config_path.read_text(encoding='utf-8'))
    model_path = Path(os.environ.get('ISG_PPE_MODEL_PATH', data['model_path']))
    if not model_path.is_absolute():
        model_path = ROOT / model_path
    return PPEModelConfig(
        model_path=model_path,
        confidence=float(os.environ.get('ISG_PPE_CONFIDENCE', data.get('confidence', .25))),
        image_size=int(data.get('image_size', 640)),
        device=os.environ.get('ISG_PPE_DEVICE', data.get('device', 'cpu')),
        sha256=data.get('sha256'), expected_names=tuple(data.get('expected_names', ())),
    )

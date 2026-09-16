"""Install the pinned PPE weight from the checked-in manifest; no overwrite."""
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    manifest = json.loads((ROOT/'config/ppe.json').read_text(encoding='utf-8'))
    path = ROOT/manifest['model_path']
    if path.is_file():
        data = path.read_bytes()
    else:
        with urllib.request.urlopen(manifest['download_url'], timeout=60) as response:
            data = response.read()
    if hashlib.sha256(data).hexdigest() != manifest['sha256']:
        raise RuntimeError('SHA-256 mismatch; model not installed or overwritten')
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as output:
            output.write(data)
    print(f'Verified PPE weights: {path} ({len(data)} bytes)')


if __name__ == '__main__':
    main()

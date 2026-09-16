"""Local-only panel preview using the real DB and existing account credentials."""
import os
from pathlib import Path
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Ephemeral signing key for a local preview; never change account credentials.
os.environ.setdefault('ISG_SECRET_KEY', secrets.token_urlsafe(48))
from web_app.web import app

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5055, debug=False, use_reloader=False)

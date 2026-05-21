"""
Test setup — bypass auth dependencies so existing tests can hit protected
routes without setting up real users / JWTs.

`get_current_user` and `require_admin` are FastAPI dependencies on the app
instance. Tests use TestClient(main.app), so we install dependency_overrides
once at import time. Each test still mocks DB calls as before.
"""
from __future__ import annotations

import os
import sys

# Make backend imports work the same way the app does (it tries `from . import db`,
# falling back to flat `import db`).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# Ensure SECRET_KEY exists so auth.py module-level paths don't blow up if
# anything imports the encoder during a test.
os.environ.setdefault("SECRET_KEY", "test-only-secret-do-not-use-in-prod")
os.environ.setdefault("MLFLOW_ENABLED", "false")  # never emit real traces during tests

try:
    from backend import main as _main
    from backend.auth import get_current_user, require_admin
except ImportError:
    import main as _main  # type: ignore[no-redef]
    from auth import get_current_user, require_admin  # type: ignore[no-redef]


def _fake_admin():
    return {"id": "00000000-0000-0000-0000-000000000000", "role": "admin", "email": "test@admin"}


_main.app.dependency_overrides[get_current_user] = _fake_admin
_main.app.dependency_overrides[require_admin] = _fake_admin

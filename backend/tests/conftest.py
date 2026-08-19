"""Force pytest onto an isolated demo store so it cannot touch live vCenter or the operator queue."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TEST_DATA = Path(tempfile.mkdtemp(prefix="vfleet-pytest-"))

os.environ["APP_MODE"] = "demo"
os.environ["DATA_DIR"] = str(_TEST_DATA)
os.environ["VCENTER_HOST"] = ""
os.environ["VCENTER_USER"] = ""
os.environ["VCENTER_PASSWORD"] = ""

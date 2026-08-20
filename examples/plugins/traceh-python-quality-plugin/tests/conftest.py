from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(PLUGIN_SRC))

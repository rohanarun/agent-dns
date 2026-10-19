# Entry point for mitmdump:  mitmdump -s frozen_addon.py ...
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from frozen.core import FrozenWeb  # noqa: E402

logging.basicConfig(level=logging.INFO)
addons = [FrozenWeb()]

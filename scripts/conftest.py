"""Lets tests import the flat modules in scripts/ regardless of where pytest is run from."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

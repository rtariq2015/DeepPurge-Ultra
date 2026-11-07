#!/usr/bin/python3
"""Run the suite with system GTK modules, including from Snap-packaged editors."""
from pathlib import Path
import runpy
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
runpy.run_path(str(ROOT / "src/deep_purge.py"), run_name="runtime_setup")
suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())

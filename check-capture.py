#!/usr/bin/env python3
"""Validate TX accounting and independent stock restoration using shared checker."""
from pathlib import Path
import subprocess
import sys

checker = Path(__file__).resolve().parent.parent / "cyw43_iso/analysis/check-embassy-capture.py"
subprocess.run([sys.executable, str(checker), "--normal-runner", *sys.argv[1:]], check=True)

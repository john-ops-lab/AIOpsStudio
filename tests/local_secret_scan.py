#!/usr/bin/env python3
"""Compatibility entry point: publication history gate without application initialization."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

if __name__ == '__main__':
    sys.exit(subprocess.call([sys.executable, str(ROOT / 'scripts/security_scan.py'), 'history']))

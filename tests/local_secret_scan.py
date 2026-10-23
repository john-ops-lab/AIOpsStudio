#!/usr/bin/env python3
"""Compatibility entry point: publication history gate without application initialization."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def known_secret_values():
    # Live API acceptance compares credentials only: published entity IDs are not secrets.
    import importlib.util
    spec = importlib.util.spec_from_file_location('publication_gate', ROOT / 'scripts/security_scan.py')
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    return gate.configured_private_values(include_identifiers=False)[0]


if __name__ == '__main__':
    sys.exit(subprocess.call([sys.executable, str(ROOT / 'scripts/security_scan.py'), 'history']))

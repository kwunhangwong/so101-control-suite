#!/usr/bin/env python3
"""Open Dataset Collection using the shared calibration hardware service."""
from pathlib import Path
import runpy
import sys

shared = Path(__file__).resolve().parent.parent / 'calibration_check'
sys.path.insert(0, str(shared))
sys.argv[1:1] = ['--view', 'dataset']
runpy.run_path(str(shared / 'server.py'), run_name='__main__')

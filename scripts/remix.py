#!/usr/bin/env python3
"""Portable Skill entrypoint, independent of the highlight Skill."""
from pathlib import Path
import importlib.util
import json
import os
import sys
root=Path(__file__).resolve().parents[1]
if importlib.util.find_spec('jsonschema') is None or importlib.util.find_spec('cv2') is None:
    configured = os.environ.get('JEV_REMIX_PYTHON')
    if not configured:
        try:
            configured=json.loads((Path.home()/'.config'/'jev-remix'/'runtime.json').read_text()).get('python')
        except (OSError,ValueError):
            configured=None
    if configured:
        interpreter=Path(configured).expanduser()
        if interpreter.is_file() and interpreter.absolute()!=Path(sys.executable).absolute():
            os.execv(str(interpreter),[str(interpreter),str(Path(__file__).resolve()),*sys.argv[1:]])
sys.path[:0]=[str(root/'src'),str(root/'_vendor')]
from jev_remix.cli import main
raise SystemExit(main())

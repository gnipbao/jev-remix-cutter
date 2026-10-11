#!/usr/bin/env python3
"""Synchronize this versioned stdlib vendor into an explicitly chosen Skill."""
import argparse
from pathlib import Path
import shutil

def sync(target):
    source=Path(__file__).resolve().parents[1]/'_vendor'/'jevkit'
    target=Path(target).expanduser().resolve()/'_vendor'/'jevkit'
    if target==source.resolve():raise ValueError('Source and destination are the same')
    if target.exists():
        raise ValueError('Destination already exists; compare/migrate before replacing a vendor version')
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copytree(source,target,ignore=shutil.ignore_patterns('__pycache__'))
    return target

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('target_skill');a=p.parse_args()
    print(sync(a.target_skill))

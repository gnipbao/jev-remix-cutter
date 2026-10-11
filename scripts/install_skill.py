#!/usr/bin/env python3
"""Register this local Skill and optional external Python without storing secrets."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

def install(destination=None,python=None):
    root=Path(__file__).resolve().parents[1]
    home=Path(os.environ.get('CODEX_HOME',Path.home()/'.codex'))
    target=Path(destination).expanduser() if destination else home/'skills'/'jev-remix-cutter'
    if target.is_symlink():
        if target.resolve()!=root:
            raise ValueError('An existing Skill is installed at the destination; it was not replaced')
    elif target.exists():
        raise ValueError('An existing destination was not replaced')
    interpreter=Path(python).expanduser().absolute() if python else None
    if interpreter:
        if not interpreter.is_file():
            raise ValueError('Python interpreter does not exist')
        # Dependency validation needs no service credentials. Pass only runtime
        # lookup/locale settings, and explicitly select the requested Python.
        check_env={name:os.environ[name] for name in ('PATH','SystemRoot','WINDIR','LANG','LC_ALL') if name in os.environ}
        check_env['JEV_REMIX_PYTHON']=str(interpreter)
        check_env['PYTHONDONTWRITEBYTECODE']='1'
        check=subprocess.run([str(interpreter),str(root/'scripts'/'remix.py'),'doctor','--json'],capture_output=True,text=True,env=check_env)
        if check.returncode:
            raise ValueError('Selected interpreter does not pass doctor; install core dependencies first')
    target.parent.mkdir(parents=True,exist_ok=True)
    if not target.is_symlink():
        target.symlink_to(root,target_is_directory=True)
    if interpreter:
        config=Path.home()/'.config'/'jev-remix'/'runtime.json'
        if config.is_symlink():
            raise ValueError('Runtime configuration must not be a symlink')
        config.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        config.write_text(json.dumps({'version':1,'python':str(interpreter)},indent=2)+'\n')
        config.chmod(0o600)
    return {'skill':str(target),'source':str(root),'python':str(interpreter) if interpreter else None}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--destination');p.add_argument('--python')
    a=p.parse_args()
    try:print(json.dumps({'ok':True,**install(a.destination,a.python)}))
    except (ValueError,OSError) as e:
        print(json.dumps({'ok':False,'error':str(e)}));sys.exit(2)

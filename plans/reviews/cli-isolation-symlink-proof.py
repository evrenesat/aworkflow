"""Run from the execution root with .venv/bin/python; uses disposable state only."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[2]
python = root / '.venv/bin/python'
with tempfile.TemporaryDirectory(prefix='aflow-review-symlink-') as scratch:
    scratch = Path(scratch).resolve()
    real = scratch / 'real'
    real.mkdir()
    alias = scratch / 'alias'
    alias.symlink_to(real, target_is_directory=True)
    outer = scratch / 'outer'
    outer.mkdir()
    subprocess.run(['git', 'init', '-q', str(outer)], check=True)
    env = dict(os.environ, TMPDIR=str(alias), TEMP=str(alias), TMP=str(alias))
    probe = '''import pathlib, sys, tempfile, aflow
alias, real, root = map(pathlib.Path, sys.argv[1:])
assert pathlib.Path(aflow.__file__).resolve().is_relative_to(root), aflow.__file__
assert tempfile.gettempdir() == str(alias), tempfile.gettempdir()
with tempfile.TemporaryDirectory() as name:
    path = pathlib.Path(name)
    assert path.parent == alias, path
    assert path.resolve().parent == real, path.resolve()
    assert path != path.resolve()
    print('PASS: tempfile uses symlink alias before fixture resolution', flush=True)
import pytest
raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider', str(root / 'tests/test_cli.py'), '-k', 'test_cli_workflow_override']))
'''
    result = subprocess.run([str(python), '-c', probe, str(alias), str(real), str(root)], cwd=outer, env=env)
    assert result.returncode == 0, result.returncode
    assert not (outer / '.aflow').exists()
    print('PASS: disposable outer Git caller remains without .aflow', flush=True)

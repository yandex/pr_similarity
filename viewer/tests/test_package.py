"""A wheel installed outside the source tree retains independent contracts."""

import json
import os
import subprocess
import sys
from pathlib import Path


def test_wheel_installed_without_source(tmp_path):
    root=Path(__file__).parents[1]
    subprocess.run([sys.executable,'-m','build','--wheel','--no-isolation','--outdir',str(tmp_path/'dist')],cwd=root,check=True,capture_output=True)
    wheel=next((tmp_path/'dist').glob('*.whl'))
    subprocess.run([sys.executable,'-m','pip','install','--no-deps','--target',str(tmp_path/'installed'),str(wheel)],check=True,capture_output=True)
    code='import sys;sys.path.insert(0,sys.argv[1]);from viewer.contracts import validator;from viewer.metrics import aggregate;import viewer;assert "installed" in viewer.__file__;assert validator("report");assert aggregate([])["score"] is None;from importlib.resources import files;assert all(len(files("viewer").joinpath("static",name).read_bytes())>100 for name in ("index.html","app.js","style.css"));print(viewer.__version__)'
    result=subprocess.run([sys.executable,'-I','-c',code,str(tmp_path/'installed')],cwd=tmp_path,check=True,capture_output=True,text=True)
    assert result.stdout.strip()=='0.1.1'


def test_frozen_consumer_sources_match_recorded_hashes():
    import hashlib
    root=Path(__file__).parents[1]
    records=json.loads((root/'tests/fixtures/source-hashes.json').read_bytes())
    for target,entry in records.items():
        assert hashlib.sha256((root/target).read_bytes()).hexdigest()==entry['sha256']

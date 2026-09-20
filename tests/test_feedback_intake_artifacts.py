"""App consumer of platform binding and the mature safe archive extractor."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from core.managed_runtime import runtime_platform_tag
from tests.test_feedback_intake import APP, ROOT

MANIFEST = os.environ.get("AVIBE_FEEDBACK_RUNTIME_MANIFEST")
ARCHIVE = os.environ.get("AVIBE_FEEDBACK_RUNTIME_ARCHIVE")
LEGACY_PYTHON = os.environ.get("AVIBE_FEEDBACK_LEGACY_PYTHON")


def load_prepare():
    spec=importlib.util.spec_from_file_location("prepare",APP / "integration/prepare-runtime.py")
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def synthetic_bundle(tmp_path, unsafe=None):
    platform=runtime_platform_tag()
    archive_path=tmp_path / f"vibe-show-runtime-node-{platform}.tgz"
    with tarfile.open(archive_path,"w:gz") as archive:
        cli=tarfile.TarInfo("packages/runtime/dist/cli.js");cli.size=4
        archive.addfile(cli,io.BytesIO(b"safe"))
        if unsafe:
            item=tarfile.TarInfo("../escape" if unsafe=="path" else "escape")
            if unsafe=="link":item.type=tarfile.SYMTYPE;item.linkname="../outside"
            archive.addfile(item)
    prepare=load_prepare()
    manifest=dict(runtime_version=prepare.VERSION, archives={
        "linux-x64":dict(name="vibe-show-runtime-node-linux-x64.tgz",sha256=prepare.LINUX_SHA),
        platform:dict(name=archive_path.name,sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest()),
    })
    manifest_path=tmp_path / "manifest.json";manifest_path.write_text(json.dumps(manifest))
    return manifest_path,archive_path


@pytest.mark.parametrize("optimized",[False,True])
@pytest.mark.parametrize("failure",["wrong_platform","renamed_bytes","unsupported","manifest_hash"])
def test_platform_and_hash_rejection_precedes_extraction(tmp_path,optimized,failure):
    manifest,archive=synthetic_bundle(tmp_path)
    destination=tmp_path / "destination"
    # Synthetic manifest bindings only; real platform selector is used except
    # for an explicit unsupported host. Valid release bindings also run below.
    script='''
import importlib.util,hashlib,json,pathlib,sys
spec=importlib.util.spec_from_file_location('p',sys.argv[1]);p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
m,a,d=map(pathlib.Path,sys.argv[2:5]);payload=json.loads(m.read_bytes())
p.MANIFEST_SHA=hashlib.sha256(m.read_bytes()).hexdigest()
p.LINUX_SHA=payload['archives']['linux-x64']['sha256']
mode=sys.argv[5]
if mode=='wrong_platform':
    wrong=dict(name='vibe-show-runtime-node-foreign.tgz',sha256=hashlib.sha256(a.read_bytes()).hexdigest())
    payload['archives']['foreign']=wrong;m.write_text(json.dumps(payload));p.MANIFEST_SHA=hashlib.sha256(m.read_bytes()).hexdigest()
    a=a.rename(a.with_name(wrong['name']))
elif mode=='renamed_bytes': a.write_bytes(b'foreign bytes')
elif mode=='unsupported': p.runtime_platform_tag=lambda:'unsupported-arch'
elif mode=='manifest_hash': p.MANIFEST_SHA='0'*64
try:p.prepare(m,a,d)
except ValueError:sys.exit(0 if not d.exists() else 8)
sys.exit(9)
'''
    result=subprocess.run([sys.executable,*( ['-O'] if optimized else []),"-c",script,str(APP / "integration/prepare-runtime.py"),
                           str(manifest),str(archive),str(destination),failure],cwd=ROOT,capture_output=True)
    assert result.returncode==0,result.stderr.decode()
    assert not destination.exists()


@pytest.mark.parametrize("legacy",[False,True])
@pytest.mark.parametrize("unsafe",[None,"path","link"])
def test_helper_consumes_safe_compatible_extractor(tmp_path,legacy,unsafe):
    python=LEGACY_PYTHON if legacy else sys.executable
    if not python:pytest.skip("Set AVIBE_FEEDBACK_LEGACY_PYTHON to actual Python without tarfile extraction filters")
    manifest,archive=synthetic_bundle(tmp_path,unsafe)
    script='''
import importlib.util,hashlib,json,pathlib,sys,tarfile
spec=importlib.util.spec_from_file_location('p',sys.argv[1]);p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
m,a,d=map(pathlib.Path,sys.argv[2:5]);payload=json.loads(m.read_bytes())
p.MANIFEST_SHA=hashlib.sha256(m.read_bytes()).hexdigest();p.LINUX_SHA=payload['archives']['linux-x64']['sha256']
if sys.argv[6]=='legacy' and hasattr(tarfile,'data_filter'):raise RuntimeError('actual old interpreter required')
try:
    p.prepare(m,a,d)
except ValueError:
    if sys.argv[5]=='safe':raise
else:
    if sys.argv[5]!='safe':raise RuntimeError('unsafe archive accepted')
    if (d/'packages/runtime/dist/cli.js').read_bytes()!=b'safe':raise RuntimeError('wrong bytes')
if (d.parent/'escape').exists() or (d.parent/'outside').exists():raise RuntimeError('escape')
'''
    env=dict(os.environ,PYTHONPATH=str(ROOT))
    result=subprocess.run([python,"-O","-c",script,str(APP / "integration/prepare-runtime.py"),str(manifest),str(archive),
                           str(tmp_path / "destination"),unsafe or "safe","legacy" if legacy else "modern"],env=env,capture_output=True)
    assert result.returncode==0,result.stderr.decode()


def test_real_pinned_manifest_local_archive_and_wrong_platform(tmp_path):
    if not MANIFEST or not ARCHIVE:pytest.skip("Commissioning archive required")
    prepare=load_prepare()
    manifest=Path(MANIFEST);local=Path(ARCHIVE)
    # Same valid manifest, another entry's actual name: must fail before any
    # extraction even if the name belongs to this release.
    payload=json.loads(manifest.read_bytes())
    wrong=next(v for k,v in payload['archives'].items() if k!=runtime_platform_tag())
    wrong_path=tmp_path / wrong['name'];wrong_path.write_bytes(local.read_bytes())
    with pytest.raises(ValueError,match="executing platform"):
        prepare.prepare(manifest,wrong_path,tmp_path / "wrong")
    assert not (tmp_path / "wrong").exists()
    result=subprocess.run([sys.executable,"-O",str(APP / "integration/prepare-runtime.py"),str(manifest),str(local),str(tmp_path / "valid")],
                          env=dict(os.environ,PYTHONPATH=str(ROOT)),capture_output=True)
    assert result.returncode==0,result.stderr.decode()


@pytest.mark.parametrize("optimized",[False,True])
def test_other_platform_release_archive_rejected_on_actual_host(tmp_path,optimized):
    other=os.environ.get("AVIBE_FEEDBACK_FOREIGN_RUNTIME_ARCHIVE")
    if not MANIFEST or not other:pytest.skip("Supply another platform archive from the frozen release")
    payload=json.loads(Path(MANIFEST).read_bytes())
    archive=Path(other)
    entries=[(tag,v) for tag,v in payload['archives'].items() if v['name']==archive.name]
    assert len(entries)==1 and entries[0][0]!=runtime_platform_tag()
    assert hashlib.sha256(archive.read_bytes()).hexdigest()==entries[0][1]['sha256']
    for renamed in (False,True):
        candidate=archive
        if renamed:
            candidate=tmp_path / payload['archives'][runtime_platform_tag()]['name']
            candidate.write_bytes(archive.read_bytes())
        destination=tmp_path / f'extract-{renamed}'
        result=subprocess.run([sys.executable,*(['-O'] if optimized else []),str(APP / 'integration/prepare-runtime.py'),
                               MANIFEST,str(candidate),str(destination)],env=dict(os.environ,PYTHONPATH=str(ROOT)),capture_output=True)
        assert result.returncode!=0 and not destination.exists()
        assert (b'hash mismatch' if renamed else b'executing platform') in result.stderr

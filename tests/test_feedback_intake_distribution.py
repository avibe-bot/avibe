"""Normal wheel install → installed built-in source → immutable Skill snapshot."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
WHEEL = os.environ.get("AVIBE_FEEDBACK_TEST_WHEEL")


def test_normal_wheel_install_and_builtin_snapshot(tmp_path):
    if not WHEEL:
        pytest.skip("Set AVIBE_FEEDBACK_TEST_WHEEL to the reviewed normal wheel")
    wheel = Path(WHEEL).resolve()
    paths = ("SKILL.md", "references/feedback.md", "scripts/feedback_intake.py")
    expected = {}
    with zipfile.ZipFile(wheel) as archive:
        for path in paths:
            raw = archive.read("vibe/builtin_skills_source/use-avibe/" + path)
            assert raw == (ROOT / "skills/use-avibe" / path).read_bytes()
            expected[path] = hashlib.sha256(raw).hexdigest()
        assert len(archive.read("vibe/ui/dist/index.html")) > 1000
        assert archive.read("vibe/show_runtime_manifest.json")
    installed = tmp_path / "installed"
    env = {key: os.environ[key] for key in ("PATH", "HOME", "AVIBE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")}
    env.update(PYTHONPATH=str(installed), AVIBE_ALLOW_DEV_STATE_MIGRATION="1")
    subprocess.run([shutil.which("uv"), "pip", "install", "--no-deps", "--target", str(installed), str(wheel)],
                   env=env, cwd=tmp_path, check=True, capture_output=True)
    script = '''
import hashlib,json,os,sys
from pathlib import Path
from core.managed_skills import builtin_skills_source,prepare_builtin_skills
source=builtin_skills_source()
assert 'builtin_skills_source' in str(source)
assert Path(sys.argv[1]) in source.parents
snapshot=prepare_builtin_skills()
root=Path(os.environ['AVIBE_BUILTIN_SKILLS_ROOT'])
assert root.name==snapshot
expected=json.loads(sys.argv[2])
for path,digest in expected.items():
    assert hashlib.sha256((root/'use-avibe'/path).read_bytes()).hexdigest()==digest
print(json.dumps({'snapshot':snapshot,'source':str(source)}))
'''
    result = subprocess.run([sys.executable,"-c",script,str(installed),json.dumps(expected)], env=env,
                            cwd=tmp_path, check=True, capture_output=True, text=True)
    assert json.loads(result.stdout)["snapshot"]

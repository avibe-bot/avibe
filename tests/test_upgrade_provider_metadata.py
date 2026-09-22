"""Core integrity uses metadata, not every file sharing the vibe namespace."""

from importlib import metadata

import pytest
from packaging.utils import canonicalize_name

from vibe import upgrade


@pytest.fixture
def installed_records(tmp_path, monkeypatch):
    """Discover only test-owned dist-info, without importing installed code.

    Released avibe_memory-3.1.0-py3-none-any.whl has no top_level.txt;
    its RECORD's vibe/memory_runtime_manifest.json makes it a vibe provider.
    """
    discover = metadata.distributions
    monkeypatch.setattr(metadata, "distributions", lambda: discover(path=[str(tmp_path)]))
    reads = []

    def distribution(name):
        reads.append(name)
        assert canonicalize_name(name) != "avibe-memory"
        return next(discover(path=[str(tmp_path)], name=name))

    monkeypatch.setattr(metadata, "distribution", distribution)
    monkeypatch.setattr(upgrade, "find_uv_binary", lambda **kwargs: None)
    monkeypatch.setattr("vibe.__version__", "3.2.0")

    def add(name, version, *, companion=False, origin=None):
        directory = tmp_path / f"{canonicalize_name(name).replace('-', '_')}-{version}.dist-info"
        directory.mkdir()
        (directory / "METADATA").write_text(f"Metadata-Version: 2.5\nName: {name}\nVersion: {version}\n")
        member = "vibe/memory_runtime_manifest.json" if companion else "vibe/__init__.py"
        payload = tmp_path / member
        payload.parent.mkdir(exist_ok=True)
        payload.write_text('{}' if companion else 'raise AssertionError("must not import fixture code")\n')
        (directory / "RECORD").write_text(f"{member},,\n{directory.name}/METADATA,,\n")
        if origin:
            import json

            (directory / "direct_url.json").write_text(json.dumps({"url": origin, "archive_info": {}}))
        return directory

    return add, reads


@pytest.mark.parametrize("companion", ["avibe-memory", "Avibe_Memory", "AVIBE.Memory", "avibe__memory"])
@pytest.mark.parametrize("core", ["avibe-os", "vibe-remote"])
def test_retained_companion_does_not_force_core_reinstall(installed_records, companion, core):
    add, reads = installed_records
    add(core, "3.2.0")
    directory = add(companion, "3.1.0", companion=True)
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()}
    assert set(metadata.packages_distributions()["vibe"]) == {core, companion}
    assert upgrade._distributions_providing_this_package() == [core]
    assert upgrade._providers_recording_a_published_release() == [(core, "3.2.0")]
    assert upgrade._providers_describing_running_code() == [core]
    assert upgrade.installed_metadata_describes_running_code()
    plan = upgrade.build_upgrade_plan(python_executable="/fixture/python", base_env={"PATH": ""})
    assert plan.command == ["/fixture/python", "-m", "pip", "install", "--upgrade", "avibe-os"]
    assert plan.preflight_command is None
    assert plan.preflight_fallback_command is None
    assert set(reads) == {core}
    assert {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()} == before


@pytest.mark.parametrize("stale_core", ["avibe-os", "vibe-remote", "vendor-core"])
def test_real_core_disagreement_still_forces_reinstall(installed_records, stale_core):
    add, _ = installed_records
    add("avibe-memory", "3.2.0", companion=True)
    add(stale_core, "3.1.0")
    if stale_core != "avibe-os":
        add("avibe-os", "3.2.0")
    assert not upgrade.installed_metadata_describes_running_code()
    plan = upgrade.build_upgrade_plan(python_executable="/fixture/python", base_env={"PATH": ""})
    assert "--force-reinstall" in plan.command


@pytest.mark.parametrize("core_version", [None, "0.0.0.dev0+editable"])
def test_companion_only_or_unpublished_core_is_not_evidence_of_damage(installed_records, core_version):
    add, _ = installed_records
    add("avibe-memory", "3.1.0", companion=True)
    if core_version:
        add("avibe-os", core_version)
    assert upgrade._providers_recording_a_published_release() == []
    assert upgrade.installed_metadata_describes_running_code()
    plan = upgrade.build_upgrade_plan(python_executable="/fixture/python", base_env={"PATH": ""})
    assert "--force-reinstall" not in plan.command


def test_exact_repair_preserves_explicit_core_provenance_and_preflight(installed_records):
    add, _ = installed_records
    origin = "https://github.com/avibe-bot/avibe/releases/download/gh-v3.2.0rc1/avibe_os-3.2.0rc1-py3-none-any.whl"
    add("avibe-os", "3.2.0rc1", origin=origin)
    add("avibe-memory", "3.1.0", companion=True, origin="https://invalid.example/companion.whl")
    assert upgrade._recorded_install_origin("avibe-os") == origin
    plan = upgrade.build_upgrade_plan(
        python_executable="/fixture/python", base_env={"PATH": ""}, version="3.2.0rc1", core_spec=origin,
    )
    assert plan.command == ["/fixture/python", "-m", "pip", "install", "--force-reinstall", origin]
    assert plan.preflight_command == [
        "/fixture/python", "-m", "pip", "download", "--dest", upgrade.PIP_DOWNLOAD_DEST_PLACEHOLDER,
        "--no-deps", origin,
    ]

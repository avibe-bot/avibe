"""Signed update publication fails closed independently from manual installers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

from scripts import desktop_release, desktop_updater

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return yaml.safe_load((ROOT / '.github/workflows' / name).read_text())['jobs']


@pytest.mark.parametrize('enabled,public,private,accepted', [
    (False, '', '', True), (True, '', '', False), (True, 'public', '', False),
    (True, '', 'private', False), (True, 'public', 'private', True),
])
def test_enabled_channel_requires_both_signing_key_and_pinned_public_key(enabled, public, private, accepted):
    if accepted:
        desktop_updater.configuration(enabled, public, private)
    else:
        with pytest.raises(ValueError):
            desktop_updater.configuration(enabled, public, private)


def test_disabled_build_explicitly_erases_the_embedded_public_key(tmp_path, monkeypatch):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'version': '3.1.2-rc.15'}))
    env = tmp_path / 'env'
    monkeypatch.setenv('AVIBE_DESKTOP_UPDATER_PUBLIC_KEY', 'a configured key')
    desktop_updater.configure(config, env, False)
    actual = json.loads(config.read_text())
    assert not actual['bundle']['createUpdaterArtifacts']
    assert env.read_text() == 'AVIBE_DESKTOP_UPDATER_PUBLIC_KEY=\n'


@pytest.mark.parametrize('tag,expected', [('v3.1.2', '3.1.2'), ('gh-v3.1.2rc15', '3.1.2-rc.15')])
def test_source_version_binding_for_both_channels(tag, expected, monkeypatch):
    source = 'a' * 40
    monkeypatch.setattr(desktop_release, 'git_output', lambda *_: source)
    assert desktop_release.resolve(tag, source)['version'] == expected
    with pytest.raises(ValueError, match='source SHA'):
        desktop_release.resolve(tag, 'b' * 40)


@pytest.mark.parametrize('tag,test,stable,enabled,signed', [
    ('gh-v3.1.2rc15', '', '', True, False),
    ('gh-v3.1.2rc15', 'true', '', True, True),
    ('v3.1.2', '', '', False, False),
    ('v3.1.2', '', 'true', True, True),
    ('v3.1.2rc1', '', 'true', False, False),
])
def test_real_workflow_channel_resolution(tmp_path, tag, test, stable, enabled, signed):
    steps = workflow('desktop-release.yml')['resolve-desktop-release']['steps']
    resolver = next(s for s in steps if s.get('id') == 'resolve')
    script = resolver['run']
    assert resolver['env']['BACKSTOP_SOURCE_SHA'] == '${{ needs.release-backstop.outputs.source_sha }}'
    # A test-owned resolver witnesses that disabled paths never invoke release tooling.
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    (scripts / 'desktop_release.py').write_text("from pathlib import Path\nPath('resolved').touch()\n")
    output = tmp_path / 'output'
    result = subprocess.run(['bash', '-e', '-c', script], cwd=tmp_path, capture_output=True, text=True,
                            env={**os.environ, 'RELEASE_TAG': tag, 'TEST_ENABLED': test,
                                 'STABLE_ENABLED': stable, 'BACKSTOP_SOURCE_SHA': 'a' * 40,
                                 'GITHUB_OUTPUT': str(output)})
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'resolved').exists() == enabled
    assert dict(line.split('=') for line in output.read_text().splitlines()) == {
        'enabled': str(enabled).lower(), 'updater_enabled': str(signed).lower(),
    }


def test_resolver_appends_to_the_step_output_it_shares(tmp_path, monkeypatch):
    # rc21 shipped without desktop packages: the resolver replaced the step's
    # enabled/updater_enabled outputs, so the package job read enabled=''.
    source = 'a' * 40
    monkeypatch.setattr(desktop_release, 'git_output', lambda *_: source)
    output = tmp_path / 'output'
    output.write_text('enabled=true\nupdater_enabled=false\n', encoding='utf-8')
    monkeypatch.setattr('sys.argv', ['desktop_release.py', 'resolve', '--tag', 'gh-v3.1.2rc15',
                                     '--output', str(output)])
    desktop_release.main()
    assert dict(line.split('=', 1) for line in output.read_text().splitlines()) == {
        'enabled': 'true', 'updater_enabled': 'false', 'tag': 'gh-v3.1.2rc15',
        'source_sha': source, 'version': '3.1.2-rc.15', 'package_version': '3.1.2rc15',
        'targets': '["aarch64-apple-darwin","x86_64-pc-windows-msvc"]',
    }


def test_release_cannot_publish_before_all_targets_and_signed_verification():
    jobs = workflow('desktop-release.yml')
    assert 'desktop-packages' in jobs['stage-desktop']['needs']
    assert "needs.desktop-packages.result == 'success'" in jobs['stage-desktop']['if']
    assert "needs.resolve-desktop-release.outputs.enabled == 'true'" in jobs['stage-desktop']['if']
    package = workflow('desktop-package.yml')['package']
    assert package['environment'] == 'desktop-updater'
    steps = package['steps']
    configure = next(i for i,s in enumerate(steps) if s.get('name') == 'Configure signed updater or explicitly disable it')
    runtime = next(i for i,s in enumerate(steps) if s.get('name') == 'Build verified private Runtime')
    sign = next(i for i,s in enumerate(steps) if s.get('name') == 'Sign and verify final updater artifacts')
    assert configure < runtime < sign < len(steps) - 1
    assert steps[configure]['env']['TAURI_SIGNING_PRIVATE_KEY'] == '${{ secrets.TAURI_SIGNING_PRIVATE_KEY }}'
    stage_steps = jobs['stage-desktop']['steps']
    attach = next(s for s in stage_steps if s.get('name') == 'Attach verified desktop assets to the same release')
    assert 'desktop_release.py stage' in attach['run']
    finalizer = workflow('publish.yml')['finalize-github-release']['steps']
    assert next(i for i,s in enumerate(finalizer) if s.get('name') == 'Wait for exact-source release notes') < next(
        i for i,s in enumerate(finalizer) if s.get('name') == 'Publish GitHub Release')


def test_signed_gate_refuses_missing_public_key_and_never_accepts_manual_signature(tmp_path, monkeypatch):
    monkeypatch.delenv('AVIBE_DESKTOP_UPDATER_PUBLIC_KEY', raising=False)
    with pytest.raises(ValueError, match='public key'):
        desktop_release.verify(tmp_path, 'gh-v3.1.2rc15', 'a' * 40, updater_enabled=True)
    (tmp_path / 'desktop-update-aarch64-apple-darwin.json').write_text('{}')
    (tmp_path / 'desktop-update-aarch64-apple-darwin.json.sig').write_text('app-adhoc')
    with pytest.raises(ValueError):
        desktop_release.verify(tmp_path, 'gh-v3.1.2rc15', 'a' * 40)


@pytest.mark.parametrize('channel,tag,version', [
    ('test', 'gh-v3.1.2rc15', '3.1.2-rc.15'), ('stable', 'v3.1.2', '3.1.2'),
])
def test_real_release_verifier_accepts_signed_fixtures_and_rejects_missing_or_tampered_assets(
    tmp_path, monkeypatch, channel, tag, version,
):
    import shutil
    executable = ROOT / 'desktop/target/debug/examples' / ('verify_update.exe' if os.name == 'nt' else 'verify_update')
    if not executable.is_file():
        pytest.skip('Build the Rust verifier first; its contract is also tested by desktop-shell CI')
    # Pytest isolates HOME. Use the already-built real verifier instead of
    # invoking rustup or writing into the user's Cargo/toolchain stores.
    run = subprocess.run

    def launch(command, **kwargs):
        assert command[:4] == ['cargo', 'run', '--locked', '--quiet']
        return run([str(executable), *command[command.index('--') + 1:]], **kwargs)

    monkeypatch.setattr(desktop_updater.subprocess, 'run', launch)
    fixtures = ROOT / 'desktop/runtime-host/tests/fixtures/updater'
    target = 'aarch64-apple-darwin'
    manifest = tmp_path / f'desktop-update-{target}.json'
    signature = Path(str(manifest) + '.sig')
    artifact = tmp_path / f'Avibe_{version}_{target}.app.tar.gz'
    shutil.copyfile(fixtures / f'{channel}-{target}.json', manifest)
    shutil.copyfile(fixtures / f'{channel}-{target}.json.sig', signature)
    shutil.copyfile(fixtures / 'artifact.bin', artifact)
    key = fixtures / 'public-key.txt'
    desktop_updater.verify(tmp_path, tag, 'a' * 40, key, [target])
    with pytest.raises(subprocess.CalledProcessError):
        desktop_updater.verify(tmp_path, tag, 'b' * 40, key, [target])
    artifact.write_bytes(b'tampered executable')
    with pytest.raises(subprocess.CalledProcessError):
        desktop_updater.verify(tmp_path, tag, 'a' * 40, key, [target])
    shutil.copyfile(fixtures / 'artifact.bin', artifact)
    signature.unlink()
    with pytest.raises(subprocess.CalledProcessError):
        desktop_updater.verify(tmp_path, tag, 'a' * 40, key, [target])


@pytest.mark.parametrize('tag', ['gh-v3.1.2rc15', 'v3.1.2'])
def test_default_signed_verifier_checks_each_required_channel_target(tmp_path, monkeypatch, tag):
    commands = []
    monkeypatch.setattr(desktop_updater.subprocess, 'run', lambda command, **_: commands.append(command))
    desktop_updater.verify(tmp_path, tag, 'a' * 40, tmp_path / 'public-key')
    assert [command[-2] for command in commands] == list(desktop_release.release_targets(tag))
    assert all(command[:4] == ['cargo', 'run', '--locked', '--quiet'] for command in commands)


@pytest.mark.parametrize('channel,tag,version', [
    ('test', 'gh-v3.1.2rc15', '3.1.2-rc.15'), ('stable', 'v3.1.2', '3.1.2'),
])
def test_real_release_verifier_checks_complete_channel_set(tmp_path, monkeypatch, channel, tag, version):
    import shutil
    from tests.test_desktop_release import assemble_assets

    executable = ROOT / 'desktop/target/debug/examples' / ('verify_update.exe' if os.name == 'nt' else 'verify_update')
    if not executable.is_file():
        pytest.skip('Build the Rust verifier first; desktop-shell CI also runs this contract')
    run = subprocess.run
    monkeypatch.setattr(desktop_updater.subprocess, 'run',
                        lambda command, **kw: run([str(executable), *command[command.index('--') + 1:]], **kw))
    directory = assemble_assets(tmp_path, tag=tag)
    fixtures = ROOT / 'desktop/runtime-host/tests/fixtures/updater'
    targets = desktop_release.release_targets(tag)
    for target in targets:
        for suffix in ('.json', '.json.sig'):
            shutil.copyfile(fixtures / f'{channel}-{target}{suffix}', directory / f'desktop-update-{target}{suffix}')
        extension = desktop_updater.TARGETS[target][1]
        shutil.copyfile(fixtures / 'artifact.bin', directory / f'Avibe_{version}_{target}{extension}')
        # The Windows installer is also the signed updater payload.
        names = desktop_release.asset_names(version, target)
        (directory / names[4]).write_text(''.join(
            f'{desktop_release.digest(directory / name)}  {name}\n' for name in sorted(names[:4])
        ))
    monkeypatch.setenv('AVIBE_DESKTOP_UPDATER_PUBLIC_KEY', (fixtures / 'public-key.txt').read_text().strip())
    desktop_release.verify(directory, tag, 'a' * 40, updater_enabled=True)
    for target in targets:
        for name in desktop_updater.names(version, target):
            path = directory / name
            original = path.read_bytes()
            for replacement in (None, b'tampered'):
                if replacement is None:
                    path.unlink()
                else:
                    path.write_bytes(replacement)
                with pytest.raises(subprocess.CalledProcessError):
                    desktop_release.verify(directory, tag, 'a' * 40, updater_enabled=True)
                path.write_bytes(original)


def test_remote_tag_rebinding_prevents_signed_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop_release, 'verify', lambda *a, **kw: [])
    monkeypatch.setattr(desktop_release.subprocess, 'check_output', lambda *a, **kw: 'b' * 40)
    monkeypatch.setitem(desktop_release._github, 'get_release', lambda *a: pytest.fail('source mismatch reached publication'))
    with pytest.raises(ValueError, match='Live release tag'):
        desktop_release.check_remote(tmp_path, 'v3.1.2', 'a' * 40, 'avibe-bot/avibe', complete=True, updater_enabled=True)

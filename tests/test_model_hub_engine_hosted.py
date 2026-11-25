"""A signed index can move clients only to the reviewed master snapshot."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from scripts import model_hub_engine_index as index
from scripts import model_hub_engine_preflight as gate
from scripts import model_hub_engine_snapshot as snapshot
from tests.engine_channel_support import (ISSUED, LocalVerifier, Signer, envelope_bytes, manifest_bytes,
                                          payload_document, release_entry, tombstone)

hosted = index.hosted
channel = hosted.channel
A, B, C = [f'model-hub-engine-v8.0.63-{n}' for n in (1, 2, 3)]


def payload(seq, builds, revoked=(), **options):
    doc = payload_document(seq, releases=[release_entry(manifest_bytes(build=n)) for n in builds], revoked=list(revoked), **options)
    return channel.parse_payload(json.dumps(doc, sort_keys=True, separators=(',', ':')).encode())


def commit(repo, raw):
    path = repo / snapshot.PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(raw)
    hosted.git(repo, 'add', '.')
    hosted.git(repo, '-c', 'user.name=fixture', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'fixture')
    return hosted.git(repo, 'rev-parse', 'HEAD').decode().strip()


def test_distinct_heads_cannot_sign_changed_three_after_failed_upload(tmp_path):
    repo = tmp_path
    hosted.git(repo, 'init', '-q')
    base = commit(repo, payload(2, [1]).raw)
    p = commit(repo, payload(3, [1, 2]).raw)
    signer = Signer()
    def sign_head():
        candidate = snapshot.master_snapshot(repo, p)
        signed = envelope_bytes(signer, candidate.raw)  # deliberately never uploaded
        return channel.authenticate(signed, roots=signer.roots(), verifier=LocalVerifier())[1].raw
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: sign_head(), range(2))) == [payload(3, [1, 2]).raw] * 2
    q = commit(repo, payload(3, [1, 2], issued_at=ISSUED.replace(second=1)).raw)
    assert snapshot.revision_step(repo, base, q)[1].sequence == 3  # stale merge-base would pass
    with pytest.raises(hosted.Error, match='higher sequence'):
        snapshot.revision_step(repo, p, q)
    with pytest.raises(hosted.Error, match='reused'):
        snapshot.master_snapshot(repo, q)
    repaired = commit(repo, payload(4, [1]).raw)
    assert snapshot.master_snapshot(repo, repaired).sequence == 4


@pytest.mark.parametrize('bad', ['delete', 'format', 'listed withdrawal', 'withdrawn fallback'])
def test_invalid_snapshot_stops_at_merge_tree(tmp_path, bad):
    hosted.git(tmp_path, 'init', '-q')
    base = commit(tmp_path, payload(1, [1]).raw)
    raw = {'delete': None, 'format': payload(2, [1]).raw + b'\n',
           'listed withdrawal': payload(2, [1, 2], [tombstone(B, A)]).raw,
           'withdrawn fallback': payload(2, [1, 2], [tombstone(B, A), tombstone(C, B)]).raw}[bad]
    with pytest.raises(hosted.Error):
        snapshot.revision_step(tmp_path, base, commit(tmp_path, raw))


def test_preflight_binds_real_merge_parents_after_base_advances(tmp_path):
    hosted.git(tmp_path, 'init', '-q')
    recorded = commit(tmp_path, None)
    actual = commit(tmp_path, payload(2, [1]).raw)
    hosted.git(tmp_path, 'update-ref', 'refs/remotes/origin/master', actual)
    hosted.git(tmp_path, 'checkout', '-qb', 'topic', recorded)
    topic = commit(tmp_path, payload(3, [1, 2]).raw)
    # Use a genuine two-parent Git commit, without resolving the fixture's two
    # deliberately competing ledger edits by merging the worktree ourselves.
    tree = hosted.git(tmp_path, 'rev-parse', topic + '^{tree}').decode().strip()
    def merge(first, second):
        head = hosted.git(tmp_path, '-c', 'user.name=test', '-c', 'user.email=t@example.invalid',
                          'commit-tree', tree, '-p', first, '-p', second, '-m', 'synthetic').decode().strip()
        hosted.git(tmp_path, 'checkout', '-q', '--detach', head)
        return head
    event = {'pull_request': {'base': {'sha': recorded, 'ref': 'master'}, 'head': {'sha': topic}}}
    sha = merge(actual, topic)
    assert gate.merge_base(tmp_path, 'pull_request', event, sha) == actual
    with pytest.raises(hosted.Error, match='second parent'):
        gate.merge_base(tmp_path, 'pull_request', event, merge(actual, recorded))
    with pytest.raises(hosted.Error, match='base branch'):
        gate.merge_base(tmp_path, 'pull_request', event, merge(sha, topic))


@pytest.mark.parametrize('minimum,action', [('3.2.6', 'withdraw'), ('3.3.0', 'blocked')])
def test_packaged_only_withdrawal_must_work_on_oldest_client(tmp_path, monkeypatch, minimum, action):
    fallback = release_entry(manifest_bytes(build=1), minimum)
    def make(seq, revoked):
        return channel.parse_payload(json.dumps(payload_document(seq, releases=[fallback], revoked=revoked),
                                                sort_keys=True, separators=(',', ':')).encode())
    old = make(2, [])
    desired = make(3, [tombstone(B, A)])
    # B was packaged, never listed. Exercise the *client's* real decision first.
    assert channel.resolve(desired, hold=None, avibe_version='3.2.6', host_platform='linux-amd64',
                           bundled=B, current=B).action.value == action
    hosted.git(tmp_path, 'init', '-q')
    before, head = commit(tmp_path, old.raw), commit(tmp_path, desired.raw)
    monkeypatch.setattr(gate.qualification, 'require_listed_assets', lambda _: None)
    def preflight():
        gate.preflight(tmp_path, before, head, published=lambda: pytest.fail('public cache read'), api=None, token=None)
    if action == 'blocked':
        with pytest.raises(hosted.Error, match='affected client'):
            preflight()
        with pytest.raises(hosted.Error, match='affected client'):
            snapshot.transition(old, desired)
    else:
        preflight()
        assert snapshot.transition(old, desired) == 'sign'


class Bucket:
    def __init__(self, initial=None):
        self.initial = initial
        self.writes = 0

    def read(self):
        return (self.initial, self.initial.hex()) if self.initial is not None else None

    def replace(self, body, *, etag):
        if etag != (self.read()[1] if self.read() else None):
            raise hosted.Error('lost conditional write')
        self.initial = body
        self.writes += 1


def test_origin_four_public_two_retries_never_rewrite_origin(monkeypatch):
    monkeypatch.setattr(index.qualification, 'require_listed_assets', lambda _: None)
    signer = Signer()
    previous = envelope_bytes(signer, payload(2, [1, 2]).raw)
    bucket = Bucket(previous)
    current = payload(4, [1], [tombstone(B, A)])  # never replay the unpublished 3
    envelope = envelope_bytes(signer, current.raw)
    served = [previous]
    def upload():
        return index.writer(current, roots=signer.roots(), verifier=LocalVerifier(), bucket=bucket, envelope=envelope,
                            current_head=lambda: None, fetch_public=lambda: served[0])
    with pytest.raises(hosted.Error, match='pending'):
        upload()
    assert bucket.initial == envelope and bucket.writes == 1
    with pytest.raises(hosted.Error, match='pending'):
        upload()
    served[0] = envelope
    assert upload() == 'readback' and bucket.writes == 1


@pytest.mark.parametrize('bad', ['older', 'same/different', 'omit B', 'retag', 'minimum', 'drop tombstone', 'tampered origin', 'incompatible fallback'])
def test_writer_refusals_preserve_authenticated_state_without_upload(bad):
    signer = Signer()
    current = payload(2, [1, 2])
    desired = payload(4, [1], [tombstone(B, A)])
    if bad == 'older':
        desired = payload(1, [1, 2])
    elif bad == 'same/different':
        desired = payload(2, [1])
    elif bad == 'omit B':
        desired = payload(3, [1])
    elif bad == 'drop tombstone':
        current = payload(3, [1], [tombstone(B, A)])
        desired = payload(4, [1])
    elif bad in ('retag', 'minimum', 'incompatible fallback'):
        changed = release_entry(manifest_bytes(build=2, salt='changed' if bad == 'retag' else ''),
                                '3.2.7' if bad == 'minimum' else '3.2.6')
        releases = [release_entry(manifest_bytes(build=1)), changed]
        if bad == 'incompatible fallback':
            current = channel.parse_payload(json.dumps(payload_document(2, releases=[release_entry(manifest_bytes(build=1), '3.2.7'), changed])).encode())
            releases = [release_entry(manifest_bytes(build=1), '3.2.7')]
        desired = channel.parse_payload(json.dumps(payload_document(4, releases=releases,
                                              revoked=[tombstone(B, A)] if bad == 'incompatible fallback' else [])).encode())
    raw = envelope_bytes(Signer() if bad == 'tampered origin' else signer, current.raw)
    bucket = Bucket(raw)
    with pytest.raises((hosted.Error, channel.ChannelError)):
        index.writer(desired, roots=signer.roots(), verifier=LocalVerifier(), bucket=bucket,
                     envelope=envelope_bytes(signer, desired.raw), current_head=lambda: None, fetch_public=lambda: raw)
    assert bucket.read()[0] == raw and bucket.writes == 0


def test_losing_writer_cannot_overwrite_newer_origin(monkeypatch):
    monkeypatch.setattr(index.qualification, 'require_listed_assets', lambda _: None)
    signer = Signer()
    current = payload(2, [1])
    raw = envelope_bytes(signer, current.raw)
    newer = envelope_bytes(signer, payload(4, [1, 3]).raw)
    bucket = Bucket(raw)
    desired = payload(3, [1, 2])
    def race():
        bucket.initial = newer
    with pytest.raises(hosted.Error, match='conditional'):
        index.writer(desired, bucket=bucket, roots=signer.roots(), verifier=LocalVerifier(),
                     current_head=race, envelope=envelope_bytes(signer, desired.raw), fetch_public=lambda: raw)
    assert bucket.initial == newer and bucket.writes == 0


def test_unpublished_bad_candidate_can_disappear_but_not_published_B(tmp_path, monkeypatch):
    signer = Signer()
    origin = payload(2, [1, 2])
    def signed(p):
        return channel.authenticate(envelope_bytes(signer, p.raw), roots=signer.roots(), verifier=LocalVerifier())[1]
    def resolve(p, current):
        return channel.resolve(p, hold=None, avibe_version='3.2.6', host_platform='linux-amd64', bundled=A, current=current)
    hosted.git(tmp_path, 'init', '-q')
    old = commit(tmp_path, payload(3, [1, 2, 3]).raw)
    checked = []
    def available(directory, work):
        checked.append(gate.qualification.load_candidate(directory).asset_release_tag)
        assert checked[-1] == A
    monkeypatch.setattr(gate.qualification, 'require_published', available)
    removed = commit(tmp_path, payload(4, [1]).raw)
    def check(head):
        gate.preflight(tmp_path, old, head, published=lambda: signed(origin), api=None, token=None)
    with pytest.raises(hosted.Error, match='explicit withdrawal'):
        check(removed)
    assert resolve(signed(origin), B).action.value == 'keep' and checked == []
    desired = payload(4, [1], [tombstone(B, A)])
    head = commit(tmp_path, desired.raw)
    check(head)
    assert checked == [A]  # neither the withdrawn B nor never-published C is fetched
    installed = resolve(signed(desired), B)
    assert (installed.action.value, installed.target.asset_release_tag) == ('withdraw', A)
    assert resolve(signed(desired), A).action.value == 'keep'
    def missing(*_):
        raise gate.qualification.QualificationError('missing fallback bytes')
    monkeypatch.setattr(gate.qualification, 'require_published', missing)
    with pytest.raises(gate.qualification.QualificationError, match='fallback'):
        check(head)


@pytest.mark.parametrize('bad', ['ok', 'release missing', 'mirror changed'])
def test_preflight_checks_exact_active_archives_from_both_sources(tmp_path, monkeypatch, bad):
    from scripts import model_hub_engine_release_guard as guard
    from tests.test_model_hub_engine_release_guard import _manifest, _fake_download

    manifest, remote, _ = _manifest(tmp_path)
    desired = channel.parse_payload(json.dumps(payload_document(1, releases=[release_entry(manifest.read_bytes())])).encode())
    release, = desired.releases
    url = next(url for url in remote if url.endswith('.tar.gz'))
    mirror = {url.replace(guard.OWNED_RELEASE_ROOT, gate.qualification.MIRROR_RELEASE_ROOT): data for url, data in remote.items()}
    if bad == 'release missing':
        del remote[url]
    elif bad == 'mirror changed':
        mirror[url.replace(guard.OWNED_RELEASE_ROOT, gate.qualification.MIRROR_RELEASE_ROOT)] = b'wrong archive'
    monkeypatch.setattr(guard, '_download', _fake_download({**remote, **mirror}))
    if bad == 'ok':
        gate.qualification.require_listed_assets(release)
    else:
        with pytest.raises(gate.qualification.QualificationError):
            gate.qualification.require_listed_assets(release)


@pytest.mark.parametrize('loss', ['before sign', 'before put', 'withdrawn only'])
def test_withdrawal_rechecks_fallback_after_preflight_but_never_fetches_bad_archive(tmp_path, monkeypatch, loss):
    from scripts import model_hub_engine_release_guard as guard
    from tests.test_model_hub_engine_release_guard import _manifest, _fake_download

    manifest, release_assets, _ = _manifest(tmp_path)
    fallback = release_entry(manifest.read_bytes())
    tag = json.loads(manifest.read_bytes())['asset_release_tag']
    # No endpoint for B exists at any point, including during preflight.
    mirror_assets = {url.replace(guard.OWNED_RELEASE_ROOT, gate.qualification.MIRROR_RELEASE_ROOT): data
                     for url, data in release_assets.items()}
    available = {**release_assets, **mirror_assets}
    monkeypatch.setattr(guard, '_download', _fake_download(available))
    def make(seq, entries, revoked):
        return channel.parse_payload(json.dumps(payload_document(seq, releases=entries, revoked=revoked),
                                                sort_keys=True, separators=(',', ':')).encode())
    old = make(2, [fallback, release_entry(manifest_bytes(build=2))], [])
    desired = make(4, [fallback], [tombstone(B, tag)])
    repo = tmp_path / 'ledger'; repo.mkdir(); hosted.git(repo, 'init', '-q')
    before = commit(repo, old.raw)
    head = commit(repo, desired.raw)
    def offline():
        raise OSError('public index is down')
    gate.preflight(repo, before, head, published=offline, api=None, token=None)

    signer = Signer()
    raw = envelope_bytes(signer, old.raw)
    bucket = Bucket(raw)
    signatures = []
    def upload():
        return index.writer(desired, bucket=bucket, roots=signer.roots(), verifier=LocalVerifier(),
                            current_head=lambda: None, fetch_public=lambda: bucket.initial, envelope=signatures[-1])
    prepare_env = {'GITHUB_REPOSITORY': index.producer.REPOSITORY, 'GITHUB_REF': 'refs/heads/master',
                   'GITHUB_WORKFLOW_REF': f'{index.producer.REPOSITORY}/{index.INDEX_WORKFLOW}@refs/heads/master',
                   'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_RUN_ID': '1', 'GITHUB_TOKEN': 'fixture',
                   'HOSTED_COMMISSIONED': 'true', 'GITHUB_OUTPUT': str(tmp_path / 'output')}
    for key, value in prepare_env.items():
        monkeypatch.setenv(key, value)
    root = repo / hosted.ROOT
    root.parent.mkdir(parents=True)
    root.write_text(json.dumps({'schema_version': 1, 'roots': [{'key_id': 'fixture', 'public_key': signer.public_key}],
                                'sequence_floor': 1}))
    master = commit(repo, desired.raw)
    def ref(path, **_):
        assert path == f'/repos/{index.producer.REPOSITORY}/git/ref/heads/master'
        return {'object': {'sha': master}}
    monkeypatch.setattr(index.producer, 'github_api', ref)
    def prepare():
        index.main(['prepare', '--data', str(repo), '--avault', 'unused-by-prepare'])
    archive = next(url for url in mirror_assets if url.endswith('.tar.gz'))
    if loss == 'before sign':
        del available[archive]
        with pytest.raises(gate.qualification.QualificationError, match='not fully published'):
            prepare()
        assert not (tmp_path / 'output').exists() and not signatures and bucket.writes == 0
        return
    prepare()
    assert (tmp_path / 'output').read_text().splitlines() == [f'revision={master}']
    signatures.append(envelope_bytes(signer, desired.raw))
    if loss == 'before put':
        available[archive] = b'wrong bytes'
        with pytest.raises(gate.qualification.QualificationError, match='not fully published'):
            upload()
        assert bucket.writes == 0 and bucket.initial == raw
    else:
        assert upload() == 'readback' and bucket.writes == 1
        assert channel.authenticate(bucket.initial, roots=signer.roots(), verifier=LocalVerifier())[1].raw == desired.raw


@pytest.mark.parametrize('bad', ['ok', 'payload', 'parent', 'attempt', 'missing'])
def test_promotion_requires_evidence_bound_to_exact_merge_tree(tmp_path, monkeypatch, bad):
    import hashlib
    from tests.test_model_hub_engine_qualification import _record, _result, GitHub, HARNESS, AVIBE

    hosted.git(tmp_path, 'init', '-q')
    before = commit(tmp_path, None)
    candidate = gate.qualification.load_candidate(_record(tmp_path / 'patches/model-hub-engine-candidates'))
    doc = payload_document(1, releases=[release_entry(candidate.manifest_bytes)])
    raw = json.dumps(doc, sort_keys=True, separators=(',', ':')).encode()
    result = _result(candidate)
    result['baselines'] = {}
    receipt = {'payload_sha256': '0' * 64 if bad == 'payload' else hashlib.sha256(raw).hexdigest(),
               'parent_sha256': '0' * 64 if bad == 'parent' else None, 'rehearsal': result}
    path = tmp_path / gate.evidence_path(1)
    path.parent.mkdir(parents=True)
    if bad != 'missing':
        path.write_text(json.dumps(receipt))
    head = commit(tmp_path, raw)
    github = GitHub()
    def api(path):
        if path.endswith(f"/runs/{result['run_id']}"):
            # A later rerun happened and failed. That must not invalidate the
            # earlier designated attempt (or make its failed attempt succeed).
            return {**github.run, 'run_attempt': result['run_attempt'] + 1, 'conclusion': 'failure'}
        if path.endswith(f"/attempts/{result['run_attempt']}"):
            return {**github.run, 'run_attempt': result['run_attempt'], 'id': result['run_id'], 'status': 'completed',
                    'conclusion': 'failure' if bad == 'attempt' else 'success'}
        return github(path)
    def download(cmd, **kwargs):
        if cmd[:3] != ['gh', 'run', 'download']:
            return original(cmd, **kwargs)
        assert cmd[cmd.index('--name') + 1].endswith(f"-attempt-{result['run_attempt']}")
        target = Path(cmd[-1]); target.mkdir()
        (target / 'untrusted_rehearsal.json').write_text(json.dumps(result))
    original = gate.subprocess.run
    monkeypatch.setattr(gate.subprocess, 'run', download)
    monkeypatch.setattr(gate.qualification, 'trusted_released_context', lambda *_a, **_kw: AVIBE)
    monkeypatch.setattr(gate.qualification, 'harness_digest', lambda: HARNESS)
    visited = []
    monkeypatch.setattr(gate.qualification, 'build_evidence', lambda *_: visited.append('archives'))
    monkeypatch.setattr(gate.qualification, 'require_listed_assets', lambda *_: visited.append('published'))
    if bad == 'ok':
        gate.preflight(tmp_path, before, head, published=lambda: None, api=api, token='fixture')
        assert visited == ['archives', 'published']
    else:
        with pytest.raises(hosted.Error):
            gate.preflight(tmp_path, before, head, published=lambda: None, api=api, token='fixture')
        assert visited == []


@pytest.mark.skipif(not os.environ.get('AVIBE_TEST_HOSTED_KEYGEN'), reason='isolated pinned fixture binaries required')
def test_disposable_bundle_signs_exact_snapshot(tmp_path, monkeypatch):
    generator = Path(os.environ['AVIBE_TEST_HOSTED_KEYGEN'])
    binary = os.environ['AVIBE_TEST_AVAULT_VERIFIER']
    credential = tmp_path / 'private.json'
    public = hosted.generate_bundle(generator, credential)
    roots = channel.TrustRoots((channel.TrustRoot('fixture', public),), 1)
    desired = payload(1, [1])
    verifier = hosted.producer.avault_verifier(binary)
    def sign():
        monkeypatch.setenv('HOSTED_SIGNING_BUNDLE', credential.read_text())
        return index.sign(desired, roots, verifier, binary)
    a, repeated = sign(), sign()
    assert a == repeated
    assert channel.authenticate(a, roots=roots, verifier=verifier)[1].raw == desired.raw
    bucket = Bucket(a)
    def retry(public):
        return index.writer(desired, bucket=bucket, roots=roots, verifier=verifier, envelope=repeated,
                            current_head=lambda: None, fetch_public=lambda: public)
    with pytest.raises(hosted.Error, match='pending'):
        retry(None)
    assert retry(a) == 'readback' and bucket.writes == 0

"""Avibe's channel signature check against the published avault verifier.

These run the real ``avault verify`` (0.1.10, the first release that has it)
through ``AvaultVerifier``: avault's own shared vectors, and an index signed
in the test. A stand-in that prints ``{"valid":true}`` cannot show that
Avibe's digest and request are the ones avault checks, so these do not use one.

The binary comes from ``AVIBE_TEST_AVAULT_VERIFIER``; CI fetches it with
``scripts/ci_avault_verifier.py`` and sets ``AVIBE_REQUIRE_AVAULT_VERIFIER`` so
a missing binary fails there instead of skipping. Vectors:
avault ``tests/vectors/verify_ecdsa_secp256k1_der.json`` at tag v0.1.10.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core import model_hub_engine_channel as channel
from core.model_hub_engine_channel import ChannelError
from tests.engine_channel_support import KEY_ID, Signer, encode, envelope_bytes, payload_document

VECTORS = json.loads(
    (Path(__file__).parent / "fixtures/avault/verify_ecdsa_secp256k1_der.json").read_text(encoding="utf-8")
)
EXPECTED_REASON = {"valid": None, "invalid": "signature_invalid", "error": "verifier_failed"}


@pytest.fixture
def verifier(tmp_path) -> channel.AvaultVerifier:
    binary = os.environ.get("AVIBE_TEST_AVAULT_VERIFIER")
    if not binary:
        if os.environ.get("AVIBE_REQUIRE_AVAULT_VERIFIER"):
            pytest.fail("AVIBE_TEST_AVAULT_VERIFIER must name the published avault verifier")
        pytest.skip("needs the published avault verifier (AVIBE_TEST_AVAULT_VERIFIER)")
    # Every place avault could keep a store, pointed at an empty directory.
    isolated = {
        name: str(tmp_path / "isolated" / name.lower())
        for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                     "XDG_STATE_HOME", "XDG_CACHE_HOME")
    }
    for directory in isolated.values():
        Path(directory).mkdir(parents=True)
    environment = {**{k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP"}}, **isolated}
    return channel.AvaultVerifier(lambda: binary, environ=environment)


def _outcome(verifier, **request) -> str | None:
    try:
        verifier.verify(**request)
    except ChannelError as exc:
        return exc.reason
    return None


def test_the_vector_file_is_the_one_avault_published_with_its_verifier() -> None:
    assert VECTORS["scheme"] == channel.SIGNATURE_SCHEME
    assert [sum(case["expect"] == kind for case in VECTORS["cases"]) for kind in ("valid", "invalid", "error")] == [
        5, 5, 25,
    ]


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda case: case["name"])
def test_avaults_shared_verify_vectors_give_the_same_answer_through_avibes_caller(verifier, case) -> None:
    outcome = _outcome(
        verifier, public_key=case["public_key_hex"], digest=case["digest_hex"], signature=case["signature_hex"]
    )

    assert outcome == EXPECTED_REASON[case["expect"]]


def test_an_index_authenticates_only_as_the_exact_bytes_that_were_signed(verifier, tmp_path) -> None:
    signer = Signer()
    payload = encode(payload_document(3))
    genuine = envelope_bytes(signer, payload)

    _envelope, parsed = channel.authenticate(genuine, roots=signer.roots(), verifier=verifier)
    assert parsed.raw == payload and parsed.sequence == 3

    altered = encode(payload_document(4))
    forged = channel.encode_envelope(
        key_id=KEY_ID, payload=altered, signature=signer.sign(channel.channel_digest(payload))
    )
    high_s = channel.encode_envelope(
        key_id=KEY_ID, payload=payload, signature=signer.sign(channel.channel_digest(payload), high_s=True)
    )
    for raw in (forged, high_s, envelope_bytes(Signer(), payload)):
        with pytest.raises(ChannelError) as raised:
            channel.authenticate(raw, roots=signer.roots(), verifier=verifier)
        assert raised.value.reason == "signature_invalid"

    # Keyless: verification left nothing in any place a store could live.
    assert [path for path in (tmp_path / "isolated").rglob("*") if path.is_file()] == []


def test_ci_fetches_exactly_the_first_avault_with_verify_by_its_published_digest(tmp_path) -> None:
    """The jobs that verify an index use the published 0.1.10 archive and nothing else."""
    import io
    import tarfile

    from scripts import ci_avault_verifier as fetcher

    assert fetcher.VERSION == ".".join(str(part) for part in channel.VERIFIER_MIN_VERSION)
    assert sorted(fetcher.ARCHIVES) == [
        "linux-arm64", "linux-x64", "macos-arm64", "macos-x64", "windows-arm64", "windows-x64",
    ]
    assert all(len(digest) == 64 for digest in fetcher.ARCHIVES.values())
    assert [fetcher.host_target(*host) for host in (("Darwin", "arm64"), ("Linux", "aarch64"), ("Windows", "AMD64"))] == [
        "macos-arm64", "linux-arm64", "windows-x64",
    ]
    with pytest.raises(SystemExit):
        fetcher.host_target("FreeBSD", "amd64")

    # Any other bytes under the same name are refused before anything is extracted.
    archive = tmp_path / "avault-0.1.10-linux-x64.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        member = tarfile.TarInfo("avault")
        member.size = 4
        bundle.addfile(member, io.BytesIO(b"fake"))
    with pytest.raises(SystemExit, match="does not match its published digest"):
        fetcher.extract_verified(archive, "linux-x64", tmp_path / "out")
    assert not (tmp_path / "out").exists()

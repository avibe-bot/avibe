"""Signing-context file/argv boundaries, with no product runtime or user state."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from storage import vault_bitcoin as bitcoin, vault_signing_input as inputs
from vibe import api, cli


def _native():
    vectors = json.loads((Path(__file__).parent / "fixtures/vault_signing/hd-bitcoin/transaction-signing-vectors.json").read_text())
    case = next(v for v in vectors["synthetic_public_corpus_vectors"] if v["address_type"] == "p2wpkh")
    return {
        "kind": "bitcoin-p2wpkh-v1", "addressType": "p2wpkh", "network": "regtest",
        "unsignedTransaction": case["unsigned_transaction"],
        "prevout": {"amountSats": case["prevout_amount_satoshis"], "scriptPubKey": case["prevout_script_pubkey"]},
        "digestAlgorithm": "bitcoin-bip143-sighash-all", "digest": case["digest"], "sighashType": 1,
    }


def _read_cli(path=None, raw=None, context=None):
    context = context or _native()
    return cli._vault_cli_signing_context(
        SimpleNamespace(signing_context_json=raw, signing_context_file=path),
        digest=context["digest"], message_hex=None, help_command="vibe vault sign --help",
    )


def test_small_file_literal_identity_and_symlink(tmp_path):
    context = _native()
    raw = json.dumps(context, separators=(",", ":"))
    path = tmp_path / "context.json"
    path.write_text(raw)
    assert _read_cli(path=str(path)) == _read_cli(raw=raw) == context
    link = tmp_path / "link.json"
    link.symlink_to(path)
    assert _read_cli(path=str(link)) == context


def test_raw_file_capacity_is_inclusive_and_excess_precedes_decoder(tmp_path, monkeypatch):
    context = _native()
    raw = json.dumps(context).encode()
    path = tmp_path / "context.json"
    path.write_bytes(raw + b" " * (bitcoin.MAX_CONTEXT_BYTES - len(raw)))
    assert _read_cli(path=str(path)) == context
    path.write_bytes(path.read_bytes() + b" ")
    monkeypatch.setattr(inputs.json, "loads", lambda *a, **k: pytest.fail("oversized file reached JSON decoder"))
    with pytest.raises(inputs.SigningInputError, match="2,162,688"):
        inputs.read_context_file(str(path))


def test_growth_after_fstat_uses_bounded_reads_on_same_descriptor(tmp_path, monkeypatch):
    path = tmp_path / "context.json"
    path.write_bytes(b"x" * (bitcoin.MAX_CONTEXT_BYTES + 100_000))
    real_fstat, real_read = inputs.os.fstat, inputs.os.read
    descriptors, bytes_read, maximum_requests = [], [], []

    def stale_stat(descriptor):
        assert stat.S_ISREG(real_fstat(descriptor).st_mode)
        descriptors.append(descriptor)
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_size=0)

    def tracked_read(descriptor, maximum):
        descriptors.append(descriptor)
        maximum_requests.append(maximum)
        chunk = real_read(descriptor, maximum)
        bytes_read.append(len(chunk))
        return chunk

    monkeypatch.setattr(inputs.os, "fstat", stale_stat)
    monkeypatch.setattr(inputs.os, "read", tracked_read)
    monkeypatch.setattr(inputs.json, "loads", lambda *a, **k: pytest.fail("growth reached decoder"))
    with pytest.raises(inputs.SigningInputError, match="2,162,688"):
        inputs.read_context_file(str(path))
    assert len(set(descriptors)) == 1
    assert sum(bytes_read) == bitcoin.MAX_CONTEXT_BYTES + 1
    assert max(maximum_requests) <= 65_536


def test_path_replacement_after_fstat_does_not_reopen(tmp_path, monkeypatch):
    context = _native()
    path, replacement = tmp_path / "context.json", tmp_path / "replacement.json"
    path.write_text(json.dumps(context))
    replacement.write_text("not the original context")
    real_fstat, real_open = inputs.os.fstat, inputs.os.open
    opened = []
    target_descriptor = None
    replaced = False

    def replacing_stat(descriptor):
        nonlocal replaced
        result = real_fstat(descriptor)
        if descriptor == target_descriptor and not replaced:
            os.replace(replacement, path)
            replaced = True
        return result

    def track_open(path, flags):
        nonlocal target_descriptor
        opened.append((path, flags))
        target_descriptor = real_open(path, flags)
        return target_descriptor

    monkeypatch.setattr(inputs.os, "fstat", replacing_stat)
    monkeypatch.setattr(inputs.os, "open", track_open)
    assert inputs.read_context_file(str(path)) == context
    assert len(opened) == 1
    assert opened[0][1] & getattr(os, "O_NONBLOCK", 0) == getattr(os, "O_NONBLOCK", 0)


@pytest.mark.parametrize("kind", ["directory", "fifo", "device", "stdin", "missing", "literal-tilde"])
def test_nonregular_stdin_missing_and_no_internal_expansion(tmp_path, kind):
    path = tmp_path / "not-a-file"
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "device":
        path = Path(os.devnull)
    elif kind == "stdin":
        path = "-"
    elif kind == "literal-tilde":
        path = "~/not-expanded-${HOME}"
    with pytest.raises(inputs.SigningInputError):
        inputs.read_context_file(str(path))


@pytest.mark.parametrize("raw", [b"\xff", b"\xef\xbb\xbf{}", b"", b"{}", b"[]", b"null", b'{"a":1,"a":2}', b'{"a":"\xc0\xaf"}'])
def test_file_strict_utf8_bom_nonempty_shape_duplicates_and_sanitized_errors(tmp_path, raw):
    path = tmp_path / "do-not-echo-path-contents.json"
    path.write_bytes(raw)
    with pytest.raises(cli.TaskCliError) as error:
        _read_cli(path=str(path))
    assert error.value.code == "invalid_signing_context"
    assert str(path) not in str(error.value)
    assert "do-not-echo" not in str(error.value)


@pytest.mark.parametrize("raw", [
    '[' * 18 + '0' + ']' * 18,
    '{' + ','.join(f'"k{i}":0' for i in range(129)) + '}',
    '[' + ','.join('0' for _ in range(2049)) + ']',
    '[["x",' + ','.join('0' for _ in range(2047)) + '],[' + ','.join('0' for _ in range(2048)) + ']]',
    '{"number":' + '1' * 1000 + '}',
])
def test_lexical_shape_token_limits_precede_general_json_decoder(tmp_path, monkeypatch, raw):
    path = tmp_path / "context.json"
    path.write_text(raw)
    monkeypatch.setattr(inputs.json, "loads", lambda *a, **k: pytest.fail("lexical budget breach reached decoder"))
    with pytest.raises(inputs.SigningInputError):
        inputs.read_context_file(str(path))


def test_lexical_pass_is_string_escape_aware_and_keeps_standard_decoder_authority(tmp_path):
    value = {"braces": '{["\\]}', "unicode": "é😀", "numbers": [0, -1, True, None]}
    path = tmp_path / "context.json"
    path.write_text(json.dumps(value))
    assert inputs.read_context_file(str(path)) == value
    for raw in ['{"a":1,}', '{"a":"\\q"}', '{"a":01}', '{"a": "\\uZZZZ"}']:
        path.write_text(raw)
        with pytest.raises(inputs.SigningInputError):
            inputs.read_context_file(str(path))


@pytest.mark.parametrize("token", ["NaN", "Infinity", "1.5", "9007199254740992", '"\\ud800"'])
def test_file_lossless_validation_rejects_nonfinite_floats_unsafe_unicode(tmp_path, token):
    path = tmp_path / "context.json"
    path.write_text('{"kind":"avault-agent-operation","bad":' + token + '}')
    with pytest.raises(cli.TaskCliError) as error:
        _read_cli(path=str(path))
    assert error.value.code == "invalid_signing_context"


def test_both_flags_even_empty_literal_refuse_before_file_or_storage(monkeypatch):
    monkeypatch.setattr(inputs.os, "open", lambda *a, **k: pytest.fail("argparse conflict opened a file"))
    monkeypatch.setattr(cli, "_open_vault_engine", lambda: pytest.fail("argparse conflict opened storage"))
    with pytest.raises(SystemExit) as error:
        cli.build_parser().parse_args([
            "vault", "sign", "KEY", "--digest", "00" * 32,
            "--signing-context-json", "", "--signing-context-file", "/not/opened",
        ])
    assert error.value.code != 0


@pytest.mark.parametrize("alias", ["direct-snake", "direct-camel", "delivery-snake", "delivery-camel"])
def test_api_resolves_exactly_one_new_context_alias(alias):
    context = _native()
    key = "signing_context" if alias.endswith("snake") else "signingContext"
    payload = {key: context} if alias.startswith("direct") else {"delivery": {key: context}}
    assert api._verifiable_signing_context_from_payload(payload, digest=context["digest"]) == context
    payload["signingContext" if key == "signing_context" else "signing_context"] = context
    with pytest.raises(api.VaultApiError, match="exactly one"):
        api._verifiable_signing_context_from_payload(payload, digest=context["digest"])


@pytest.mark.parametrize("reserved", ["operation_context", "signing_key", "bitcoin_identity", "bitcoin_cohort", "card", "context"])
def test_api_new_context_refuses_caller_authority_without_old_generic_walk(monkeypatch, reserved):
    context = _native()
    payload = {"signing_context": context, "delivery": {reserved: {"never": "trusted"}}}
    monkeypatch.setattr(api, "_validate_signing_context_json", lambda *a, **k: pytest.fail("new kind reached legacy generic walker"))
    with pytest.raises(api.VaultApiError, match="reserved"):
        api._verifiable_signing_context_from_payload(payload, digest=context["digest"])


def test_literal_new_context_duplicates_are_not_silently_normalized():
    context = _native()
    raw = json.dumps(context)[:-1] + ',"network":"regtest"}'
    with pytest.raises(cli.TaskCliError) as error:
        _read_cli(raw=raw)
    assert error.value.code == "invalid_signing_context"

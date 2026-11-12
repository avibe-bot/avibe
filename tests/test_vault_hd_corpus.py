"""Public address serialization against a Python oracle independent of product code.

This covers every account and both Taproot internal-key parities, beyond the
single seed / first two accounts in the existing custody consumer fixture.
It does not claim that custody imported or signed these wallets.
All corpus material is deliberately public; never send funds to these addresses.
"""
from __future__ import annotations

import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import pytest

from storage import vault_hd

CORPUS = json.loads((Path(__file__).parent / "fixtures/vault_signing/hd-seed-corpus.json").read_text())


@pytest.mark.parametrize("entry", CORPUS["entries"], ids=lambda entry: entry["id"])
def test_all_public_account_addresses_match_independent_python_corpus(entry):
    incoming = []
    for account in entry["accounts"]:
        # Oracle EVM keys are uncompressed SEC1; custody's wire format is compressed.
        # This representation conversion performs no seed/child-key derivation.
        evm = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256K1(), bytes.fromhex(account["public_keys"]["evm"]),
        ).public_bytes(serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint).hex()
        incoming.append({"account": account["account"], **account["public_keys"], "evm": evm})
    stored = vault_hd.import_accounts(incoming)
    for account in stored:
        account["enabled"] = True
    actual = vault_hd.public_accounts({"hd_accounts": stored})
    assert len(actual) == 20
    for observed, expected in zip(actual, entry["accounts"], strict=True):
        fields = ("account", "eth", "btc_taproot", "solana", "paths")
        assert {key: observed[key] for key in fields} == {key: expected[key] for key in fields}

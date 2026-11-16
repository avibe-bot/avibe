"""Public-only HD BTC return projection, not custody or consumer integration.

Behavior: purpose44/84/86 at one receiving index produce three distinct script
identities. The credible regression is treating all three as encodings of the
selected scalar (ordinary-key semantics). Existing ordinary tests cannot detect
that mistake. Expected scripts come from the independent default-branch corpus,
not from an Avibe address or sighash helper; every fixture is PUBLIC NEVER-FUND.
The protected-HD table/authentication/selection owner is intentionally not copied
here and must call this projection only after its full-table validation.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from storage import vault_bitcoin

CORPUS = json.loads((Path(__file__).parent / "fixtures/vault_signing/hd-bitcoin/corpus.json").read_text())
ENTRIES = [CORPUS["entries"][0], CORPUS["entries"][-1]]
CASES = [(entry, index) for entry in ENTRIES for index in (0, 1, 19)]


def _account(entry, index):
    row = entry["accounts"][index]
    return {"account": index, **row["public_keys"]}


def _expected(entry, index):
    row = entry["accounts"][index]
    return {kind: bytes.fromhex(row[field]) for kind, field in (
        ("p2pkh", "bitcoin_p2pkh_script_pubkey"),
        ("p2wpkh", "bitcoin_p2wpkh_script_pubkey"),
        ("p2tr", "bitcoin_script_pubkey"),
    )}


@pytest.mark.parametrize("entry,index", CASES, ids=[f"{entry['id']}-{index}" for entry, index in CASES])
def test_hd_purpose_scripts_match_independent_corpus_at_receiving_index(entry, index):
    account = _account(entry, index)
    before = copy.deepcopy(account)
    scripts = vault_bitcoin.hd_account_return_scripts(account)
    assert scripts == _expected(entry, index)
    assert len(set(scripts.values())) == 3
    assert account == before
    # A correctly encoded script under another index is still a recipient, not
    # a return to this index. No parent-supplied same-account flag is involved.
    other_index = 1 if index != 1 else 0
    assert set(scripts.values()).isdisjoint(_expected(entry, other_index).values())


@pytest.mark.parametrize("field", ["bitcoin_p2pkh", "bitcoin_p2wpkh"])
def test_bip86_substitution_does_not_match_the_purpose_precommitment(field):
    account = _account(ENTRIES[0], 0)
    expected = _expected(ENTRIES[0], 0)
    account[field] = account["bitcoin"]
    scripts = vault_bitcoin.hd_account_return_scripts(account)
    kind = "p2pkh" if field == "bitcoin_p2pkh" else "p2wpkh"
    assert scripts[kind] != expected[kind]
    # This pure projection cannot prove seed derivation. The owning authenticated
    # table + pinned sandbox proof is what refuses a valid-but-wrong sibling.


@pytest.mark.parametrize("field", ["bitcoin", "bitcoin_p2pkh", "bitcoin_p2wpkh"])
@pytest.mark.parametrize("value", [None, "", "04" + "00" * 32, "02" + "ff" * 32])
def test_missing_or_invalid_sibling_point_refuses_instead_of_partial_scripts(field, value):
    account = _account(ENTRIES[0], 0)
    account[field] = value
    with pytest.raises(vault_bitcoin.BitcoinContextError, match="all three valid purpose points"):
        vault_bitcoin.hd_account_return_scripts(account)


@pytest.mark.parametrize("account", [None, [], {}, {"account": True}, {"account": -1}, {"account": 20}])
def test_script_projection_requires_an_exact_integer_receiving_index(account):
    with pytest.raises(vault_bitcoin.BitcoinContextError, match="selected account"):
        vault_bitcoin.hd_account_return_scripts(account)


def test_cosmetic_settings_are_not_reinterpreted_as_seed_or_script_identity():
    account = _account(ENTRIES[0], 0)
    account.update(name="Public fixture label", always_ask=True, last_signer=None)
    assert vault_bitcoin.hd_account_return_scripts(account) == _expected(ENTRIES[0], 0)

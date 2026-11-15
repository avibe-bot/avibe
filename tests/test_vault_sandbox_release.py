"""Complete packaged-pin capability authority, not a release/promotion test."""
from __future__ import annotations

import copy
import hashlib

import pytest

from storage import vault_policy
from vibe import vault_sandbox_release as release


def test_exact_packaged_legacy_pin_advertises_only_implemented_input_class():
    pin = release.load_packaged_pin()
    support = release.support_for_pin(pin)
    assert support is not None
    assert support.pin_id == hashlib.sha256(vault_policy.canonical_json(pin).encode()).hexdigest()
    assert support.ordinary_input_types == ("p2tr",) and support.hd_input_types == ()
    assert support.ecdsa_return_types == ()
    assert support.cohort == {"pin_id": support.pin_id, "buildHash": pin["buildHash"], "manifestHash": pin["manifestHash"]}


@pytest.mark.parametrize("field", ["schema", "algorithm", "sandboxOrigin", "sandboxVersion", "buildHash", "resources", "manifestHash", "extra"])
def test_every_full_pin_field_participates_in_authority(field):
    pin = release.load_packaged_pin()
    if field == "resources":
        path = next(iter(pin[field]))
        pin[field][path] = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    elif field == "extra":
        pin["ignored-but-not-authorized"] = True
    elif field == "schema":
        pin[field] = True
    else:
        pin[field] += "changed"
    assert release.support_for_pin(pin) is None


@pytest.mark.parametrize("fields", [
    {"ordinary_input_types": ("p2pkh",)},
    {"ordinary_input_types": ("p2pkh", "p2wpkh")},
    {"ordinary_input_types": ("p2pkh", "p2wpkh", "p2tr"), "ecdsa_return_types": ("p2pkh",)},
    {"hd_input_types": ("p2wpkh", "p2tr")},
    {"ordinary_input_types": ("p2tr", "p2tr")},
    {"ordinary_input_types": ("p2tr", "p2wpkh", "p2pkh")},
    {"ordinary_input_types": ("unknown",)},
    {"unexpected": ()},
])
def test_invalid_source_entry_cannot_grant_partial_or_fallback_support(monkeypatch, fields):
    current = release.current_support()
    value = {"ordinary_input_types": ("p2tr",), "hd_input_types": (), "ecdsa_return_types": ()}
    value.update(fields)
    monkeypatch.setattr(release, "CAPABILITIES_BY_PIN_ID", {current.pin_id: value})
    assert release.current_support() is None


def test_unknown_valid_pin_does_not_guess_taproot_or_use_build_version_only(monkeypatch):
    pin = copy.deepcopy(release.load_packaged_pin())
    pin["sandboxVersion"] += ".unrecognized"
    assert release._valid_pin(pin)
    assert release.support_for_pin(pin) is None
    monkeypatch.setattr(release, "load_packaged_pin", lambda: pin)
    assert release.current_support() is None


def test_complete_capable_source_entry_requires_all_three_return_types(monkeypatch):
    current = release.current_support()
    value = {"ordinary_input_types": ("p2pkh", "p2wpkh", "p2tr"),
             "hd_input_types": ("p2pkh", "p2wpkh", "p2tr"), "ecdsa_return_types": ("p2pkh", "p2wpkh", "p2tr")}
    monkeypatch.setattr(release, "CAPABILITIES_BY_PIN_ID", {current.pin_id: value})
    support = release.current_support()
    assert support.pin_id == current.pin_id and support.build_hash == current.build_hash
    assert support.ordinary_input_types == support.hd_input_types == support.ecdsa_return_types == ("p2pkh", "p2wpkh", "p2tr")

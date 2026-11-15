"""Complete packaged-pin capability authority, not a release/promotion test."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from storage import vault_policy
from vibe import vault_sandbox_release as release


@pytest.mark.parametrize("pin_id", [
    "7f320bc014c0a2ee7d65f9c48c8b91b7e0c0fcc0effbfb3a36d3a3c30fa7c08b",
    "9f1264cb022625f1d3880c16019cc4ddcf515fce252193ad482714fe30ad046a",
])
def test_retained_legacy_full_pins_stay_taproot_only(pin_id):
    pins = json.loads((Path(__file__).parent / "fixtures" / "vault-sandbox-legacy-pins.json").read_text())
    support = release.support_for_pin(pins[pin_id])
    assert support is not None and support.pin_id == pin_id
    assert support.ordinary_input_types == ("p2tr",)
    assert support.hd_input_types == support.ecdsa_return_types == ()


def test_exact_packaged_pin_advertises_only_implemented_input_classes():
    pin = release.load_packaged_pin()
    support = release.support_for_pin(pin)
    assert support is not None
    assert support.pin_id == "b98626850db4ca71ef4896cfb8a44cea8919f4cf1cd924f84963213a1a6868d9"
    assert support.pin_id == hashlib.sha256(vault_policy.canonical_json(pin).encode()).hexdigest()
    assert support.build_hash == "185708641db830f2a02fa2f2d7169eff9aec0d3ec0564e2a4216761300ee3d67"
    assert support.manifest_hash == "sha256-PaxdZJ+2/lUAeszQgR/BCSXNa5TKL/5kxDAI5CqMUlQ="
    assert support.ordinary_input_types == support.hd_input_types == support.ecdsa_return_types == ("p2pkh", "p2wpkh", "p2tr")
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
    {"hd_input_types": ("p2pkh", "p2wpkh", "p2tr")},
    {"hd_input_types": ("p2pkh", "p2wpkh", "p2tr"), "ecdsa_return_types": ("p2wpkh", "p2tr")},
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

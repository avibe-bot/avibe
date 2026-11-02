"""Address derivation pinned to published BIP/EIP test vectors (custody-critical)."""

import pytest

from storage.vault_addresses import derive_addresses

# secp256k1 private key = 1 → the generator point G, compressed. Its addresses are
# widely published reference vectors.
PRIVKEY_1_COMPRESSED = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"

# BIP341 wallet-test-vectors.json, scriptPubKey[0] (key-path only, no merkle root):
# internal x-only pubkey → mainnet P2TR address.
BIP341_INTERNAL_XONLY = "d6889cb081036e0faefa3a35157ad71086b123b2b144b649798b494c300a961d"
BIP341_P2TR_ADDRESS = "bc1p2wsldez5mud2yam29q22wgfh9439spgduvct83k3pm50fcxa5dps59h4z5"


def test_privkey_one_reference_addresses():
    addrs = derive_addresses(PRIVKEY_1_COMPRESSED)
    # ETH: privkey 1 → 0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf (EIP-55 checksum).
    assert addrs["eth"] == "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"
    # P2PKH compressed.
    assert addrs["btc_legacy"] == "1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH"
    # P2WPKH — BIP173 canonical example (witness program = hash160 of G-compressed).
    assert addrs["btc_segwit"] == "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"


def test_taproot_matches_bip341_vector():
    # Any parity for the x gives the same key-path taproot output (BIP341 lifts x to even Y).
    addrs = derive_addresses("02" + BIP341_INTERNAL_XONLY)
    assert addrs["btc_taproot"] == BIP341_P2TR_ADDRESS
    # Odd-parity encoding of the same x must derive the identical taproot address.
    assert derive_addresses("03" + BIP341_INTERNAL_XONLY)["btc_taproot"] == BIP341_P2TR_ADDRESS


@pytest.mark.parametrize("internal,output,address", [
    ("cc8a4bc64d897bddc5fbc2f670f7a8ba0b386779106cf1223c6fc5d7cd6fc115", "a60869f0dbcf1dc659c9cecbaf8050135ea9e8cdc487053f1dc6880949dc684c", "bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr"),
    ("83dfe85a3151d2517290da461fe2815591ef69f2b18a2ce63f01697a8b313145", "a82f29944d65b86ae6b5e5cc75e294ead6c59391a1edc5e016e3498c67fc7bbb", "bc1p4qhjn9zdvkux4e44uhx8tc55attvtyu358kutcqkudyccelu0was9fqzwh"),
    ("399f1b2f4393f29a18c937859c5dd8a77350103157eb880f02e8c08214277cef", "882d74e5d0572d5a816cef0041a96b6c1de832f6f9676d9605c44d5e9a97d3dc", "bc1p3qkhfews2uk44qtvauqyr2ttdsw7svhkl9nkm9s9c3x4ax5h60wqwruhk7"),
])
def test_bip86_receive_and_change_vectors_share_signing_output_key(internal, output, address):
    # BIP86's three published leaves protect a different derivation set than the
    # single empty-tree BIP341 wallet vector. Both SEC1 parities must normalize.
    from storage.vault_addresses import taproot_output_key_xonly

    for prefix in ("02", "03"):
        assert taproot_output_key_xonly(prefix + internal) == int(output, 16)
        assert derive_addresses(prefix + internal)["btc_taproot"] == address


@pytest.mark.parametrize("tweak", [0, 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141, 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364140])
def test_taproot_tweak_boundary_zero_order_and_infinity(monkeypatch, tweak):
    # Improbable hash outputs need fault injection; ordinary vectors cannot hit
    # the valid-zero or invalid-order/infinity branches.
    from storage import vault_addresses as addresses

    monkeypatch.setattr(addresses, "_tagged_hash", lambda *_: tweak.to_bytes(32, "big"))
    if tweak == 0:
        assert addresses.taproot_output_key_xonly(PRIVKEY_1_COMPRESSED) == int(PRIVKEY_1_COMPRESSED[2:], 16)
    else:
        with pytest.raises(ValueError):
            addresses.taproot_output_key_xonly(PRIVKEY_1_COMPRESSED)


def test_accepts_uncompressed_and_matches_compressed():
    # privkey 1 uncompressed point (0x04 || X || Y) should normalize to the same addresses.
    gx = "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
    gy = "483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8"
    assert derive_addresses("04" + gx + gy) == derive_addresses(PRIVKEY_1_COMPRESSED)


def test_eth_checksum_has_mixed_case():
    # A sanity guard that EIP-55 is applied (not just lowercased).
    eth = derive_addresses(PRIVKEY_1_COMPRESSED)["eth"]
    assert eth.startswith("0x") and eth != eth.lower()


@pytest.mark.parametrize("bad", ["", "zz", "02" + "00" * 33, "04" + "11" * 10])
def test_rejects_malformed_keys(bad):
    with pytest.raises((ValueError, Exception)):
        derive_addresses(bad)


def test_rejects_offcurve_uncompressed_key():
    # Valid x (G's x) but a garbage Y that is not on the curve: must be rejected, never
    # silently re-lifted to a different valid point with the same x.
    gx = "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
    with pytest.raises((ValueError, Exception)):
        derive_addresses("04" + gx + "11" * 32)

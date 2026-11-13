"""Public blockchain address derivation for secp256k1 signing keys.

A vault signing key is a chain-agnostic secp256k1 keypair. From its (public)
compressed public key we derive the standard receive addresses so the Web UI and
the agent can identify a key by address instead of raw hex. Only PUBLIC key
material is used here — never the private key — so this derivation is safe to run
outside the avault custody core.

Addresses derived (mainnet):
  eth          Ethereum, EIP-55 checksummed ``0x`` address
  btc_legacy   Bitcoin P2PKH, base58check ``1...``
  btc_segwit   Bitcoin P2WPKH native segwit v0, bech32 ``bc1q...``
  btc_taproot  Bitcoin P2TR key-path taproot v1, bech32m ``bc1p...``

Correctness is custody-critical; ``tests/test_vault_addresses.py`` pins every
output to published BIP/EIP test vectors.
"""

from __future__ import annotations

import hashlib

from Crypto.Hash import keccak
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

# secp256k1 field prime (P) and group order (N).
_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3
_B58_CHARSET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


# ---- hashing --------------------------------------------------------------


def _keccak256(data: bytes) -> bytes:
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def _ripemd160(data: bytes) -> bytes:
    # hashlib exposes ripemd160 only when the OpenSSL build provides it (often
    # disabled on OpenSSL 3); fall back to pycryptodome, which always ships it.
    try:
        h = hashlib.new("ripemd160")
        h.update(data)
        return h.digest()
    except (ValueError, TypeError):
        from Crypto.Hash import RIPEMD160

        return RIPEMD160.new(data).digest()


def _hash160(data: bytes) -> bytes:
    return _ripemd160(hashlib.sha256(data).digest())


def _tagged_hash(tag: str, msg: bytes) -> bytes:
    prefix = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(prefix + prefix + msg).digest()


# ---- secp256k1 points -----------------------------------------------------


def _point(encoded: bytes) -> tuple[int, int]:
    """Affine (x, y) of a SEC1-encoded point; validates it is on the curve."""
    pk = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), encoded)
    nums = pk.public_numbers()
    return nums.x, nums.y


def _uncompressed(compressed: bytes) -> bytes:
    pk = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), compressed)
    return pk.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def _point_add(p1: tuple[int, int], p2: tuple[int, int]) -> tuple[int, int]:
    """Affine point addition on secp256k1 (handles the doubling case)."""
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % _P == 0:
        raise ValueError("point addition resulted in the point at infinity")
    if x1 == x2 and y1 == y2:
        lam = (3 * x1 * x1) * pow(2 * y1, -1, _P) % _P
    else:
        lam = (y2 - y1) * pow((x2 - x1) % _P, -1, _P) % _P
    x3 = (lam * lam - x1 - x2) % _P
    y3 = (lam * (x1 - x3) - y1) % _P
    return x3, y3


def _normalize_compressed(public_key_hex: str) -> bytes:
    """Return the 33-byte compressed form, accepting compressed or uncompressed hex."""
    raw = bytes.fromhex(public_key_hex.strip())
    if len(raw) == 65 and raw[0] == 0x04:
        # Validate the FULL uncompressed point on-curve before trusting its Y parity, then
        # compress from the validated coordinates — an off-curve X||Y must be rejected, not
        # silently "corrected" to a different valid point with the same X.
        x, y = _point(raw)
        raw = bytes([0x02 + (y & 1)]) + x.to_bytes(32, "big")
    if len(raw) != 33 or raw[0] not in (0x02, 0x03):
        raise ValueError("expected a compressed secp256k1 public key")
    _point(raw)  # raises if not on curve
    return raw


# ---- encoders -------------------------------------------------------------


def _b58check(payload: bytes) -> str:
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return base58_encode(payload + checksum)


def base58_encode(data: bytes) -> str:
    """Plain base58 (Bitcoin alphabet, no checksum); leading zero bytes become ``1``."""
    n = int.from_bytes(data, "big")
    out = ""
    while n > 0:
        n, rem = divmod(n, 58)
        out = _B58_CHARSET[rem] + out
    pad = len(data) - len(data.lstrip(b"\x00"))
    return "1" * pad + out


def _bech32_polymod(values: list[int]) -> int:
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ value
        for i in range(5):
            chk ^= generators[i] if ((top >> i) & 1) else 0
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _bech32_checksum(hrp: str, data: list[int], const: int) -> list[int]:
    values = _hrp_expand(hrp) + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ const
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def _convertbits(data: bytes, frombits: int, tobits: int) -> list[int]:
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if bits:
        ret.append((acc << (tobits - bits)) & maxv)
    return ret


def _segwit_encode(hrp: str, witver: int, witprog: bytes) -> str:
    const = _BECH32_CONST if witver == 0 else _BECH32M_CONST
    data = [witver] + _convertbits(witprog, 8, 5)
    combined = data + _bech32_checksum(hrp, data, const)
    return hrp + "1" + "".join(_BECH32_CHARSET[d] for d in combined)


# ---- address derivations --------------------------------------------------


def _eip55(addr_hex: str) -> str:
    lower = addr_hex.lower()
    digest = _keccak256(lower.encode()).hex()
    return "".join(c.upper() if c.isalpha() and int(digest[i], 16) >= 8 else c for i, c in enumerate(lower))


def _eth(compressed: bytes) -> str:
    # keccak256 of the 64-byte X||Y (uncompressed point without the 0x04 prefix).
    digest = _keccak256(_uncompressed(compressed)[1:])
    return "0x" + _eip55(digest[-20:].hex())


def _p2pkh(compressed: bytes) -> str:
    return _b58check(b"\x00" + _hash160(compressed))


def _p2wpkh(compressed: bytes) -> str:
    return _segwit_encode("bc", 0, _hash160(compressed))


def _p2tr(compressed: bytes) -> str:
    return _segwit_encode("bc", 1, taproot_output_key_xonly(compressed).to_bytes(32, "big"))


def taproot_output_key_xonly(public_key: bytes | str) -> int:
    """Return the BIP341 key-path output key x-coordinate for a public key.

    This is public-only BIP86 derivation: the internal key is lifted to even Y
    and tweaked with ``TaggedHash("TapTweak", P.x)`` and an empty script tree.
    A zero tweak is valid under BIP341 and therefore must not be rejected.
    """
    compressed = _normalize_compressed(public_key if isinstance(public_key, str) else public_key.hex())
    x, _ = _point(compressed)
    x_bytes = x.to_bytes(32, "big")
    internal = _point(b"\x02" + x_bytes)
    tweak = int.from_bytes(_tagged_hash("TapTweak", x_bytes), "big")
    if tweak >= _N:
        raise ValueError("invalid taproot tweak")
    if tweak == 0:
        output = internal
    else:
        tweak_point = ec.derive_private_key(tweak, ec.SECP256K1()).public_key().public_numbers()
        output = _point_add(internal, (tweak_point.x, tweak_point.y))
    return output[0]


def validate_public_address(field: str, value: str) -> None:
    """Require the exact mainnet public encoding used by our address projection.

    This is encoding/checksum validation only, not proof that an address belongs
    to a particular key. Keep the encoders above as the canonical authority.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise ValueError("invalid public address")
    if field == "eth":
        if len(value) != 42 or not value.startswith("0x"):
            raise ValueError("invalid EIP-55 address")
        try:
            raw = bytes.fromhex(value[2:])
        except ValueError as exc:
            raise ValueError("invalid EIP-55 address") from exc
        if len(raw) != 20 or "0x" + _eip55(raw.hex()) != value:
            raise ValueError("noncanonical EIP-55 address")
        return
    if field in {"btc_legacy", "solana"}:
        number = 0
        for char in value:
            try:
                number = number * 58 + _B58_CHARSET.index(char)
            except ValueError as exc:
                raise ValueError("invalid base58 address") from exc
        raw = b"\x00" * (len(value) - len(value.lstrip("1"))) + number.to_bytes((number.bit_length() + 7) // 8, "big")
        if field == "solana":
            valid = len(raw) == 32 and base58_encode(raw) == value
        else:
            valid = len(raw) == 25 and raw[0] == 0 and _b58check(raw[:21]) == value
        if not valid:
            raise ValueError("invalid canonical base58 address")
        return
    if field in {"btc_segwit", "btc_taproot"}:
        version, program_size = (0, 20) if field == "btc_segwit" else (1, 32)
        if not value.startswith("bc1") or value != value.lower() or len(value) < 10:
            raise ValueError("invalid mainnet witness address")
        try:
            words = [_BECH32_CHARSET.index(char) for char in value[3:]]
        except ValueError as exc:
            raise ValueError("invalid witness address alphabet") from exc
        data = words[1:-6]
        number = 0
        for word in data:
            number = (number << 5) | word
        size, padding = divmod(len(data) * 5, 8)
        if words[0] != version or size != program_size:
            raise ValueError("invalid witness version/program length")
        program = (number >> padding).to_bytes(size, "big")
        if _segwit_encode("bc", version, program) != value:
            raise ValueError("invalid witness checksum or noncanonical padding")
        return
    raise ValueError("unsupported public address field")


def derive_addresses(public_key_hex: str) -> dict[str, str]:
    """Derive the standard mainnet addresses for a compressed secp256k1 public key.

    Raises ``ValueError`` for a malformed / off-curve key. Callers surfacing this
    for a stored key should treat failure as "no addresses", never a hard error.
    """
    compressed = _normalize_compressed(public_key_hex)
    return {
        "eth": _eth(compressed),
        "btc_legacy": _p2pkh(compressed),
        "btc_segwit": _p2wpkh(compressed),
        "btc_taproot": _p2tr(compressed),
    }

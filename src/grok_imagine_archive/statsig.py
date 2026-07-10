"""Generate grok.com x-statsig-id anti-bot headers.

Ported from auroro-grok2api internal/grok/statsig/pure.go. grok validates the
header by extracting the embedded 48-byte seed, recomputing
SHA-256(method!path!number+salt+HEX), and checking the XOR-wrapped payload.
A forged/random value is rejected with HTTP 403 code:7.
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct
import time

# Epoch and salt baked into grok's front-end statsig chunk.
_STATSIG_EPOCH = 1682924400  # 0x644f6370
_STATSIG_SALT = "obfiowerehiring"
_STATSIG_MARK = 0x03

# Known genuine (seed, HEX) pair captured from live browser (same defaults as
# auroro-grok2api). Random seeds need an up-to-date SVG path table to compute
# HEX; this fixed pair is internally consistent and accepted by grok.
_DEFAULT_SEED_B64 = "t2ODAFY4ozXd0K2Y8MdI2XfxTDiJoakZPuoaKfcQn8VuasZMcKliyhA1pJ+o1oMf"
_DEFAULT_HEX = "3bab9506b851eb851eb840e8f5c28f5c28f80e8f5c28f5c28f806b851eb851eb8400"

_seed: bytes | None = None
_hex: str | None = None


def _decode_seed(value: str) -> bytes:
    raw = value.strip()
    padding = "=" * (-len(raw) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return decoder(raw + padding)
        except Exception:
            continue
    raise ValueError("statsig seed is not valid base64")


def _ensure_pair() -> tuple[bytes, str]:
    global _seed, _hex
    if _seed is not None and _hex is not None:
        return _seed, _hex
    seed = _decode_seed(_DEFAULT_SEED_B64)
    if len(seed) != 48:
        raise RuntimeError("default statsig seed must be 48 bytes")
    _seed, _hex = seed, _DEFAULT_HEX
    return _seed, _hex


def set_pair(seed_b64: str, hex_value: str) -> None:
    """Override the active (seed, HEX) pair (must be a matched genuine pair)."""
    global _seed, _hex
    seed = _decode_seed(seed_b64)
    if len(seed) != 48:
        raise ValueError("statsig seed must decode to 48 bytes")
    if not str(hex_value or "").strip():
        raise ValueError("statsig HEX must be non-empty")
    _seed = seed
    _hex = str(hex_value).strip()


def generate(
    pathname: str,
    method: str = "POST",
    *,
    now_unix: int | None = None,
) -> str:
    """Return a fresh x-statsig-id for (pathname, method).

    Output is base64.raw (no padding), decoding to exactly 70 bytes.
    """
    seed, hex_value = _ensure_pair()
    path = pathname or "/rest/media/post/list"
    meth = (method or "POST").upper()
    number = int((now_unix if now_unix is not None else time.time()) - _STATSIG_EPOCH) & 0xFFFFFFFF

    material = f"{meth}!{path}!{number}{_STATSIG_SALT}{hex_value}".encode("utf-8")
    digest = hashlib.sha256(material).digest()

    key = os.urandom(1)[0]
    out = bytearray(70)
    out[0] = key
    for i in range(48):
        out[1 + i] = seed[i] ^ key
    # tail = uint32LE(number) ++ sha[0:16] ++ [mark]
    out[49:53] = bytes(b ^ key for b in struct.pack("<I", number))
    for i in range(16):
        out[53 + i] = digest[i] ^ key
    out[69] = _STATSIG_MARK ^ key
    return base64.b64encode(out).decode("ascii").rstrip("=")


def is_real_statsig(value: str) -> bool:
    """Return True if value decodes to the 70-byte real statsig shape."""
    raw = value.strip()
    padding = "=" * (-len(raw) % 4)
    try:
        decoded = base64.b64decode(raw + padding)
    except Exception:
        return False
    return len(decoded) == 70

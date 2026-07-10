import base64
import hashlib
import struct

from grok_imagine_archive.statsig import generate, is_real_statsig, set_pair


def test_generate_shape_is_70_bytes_raw_base64() -> None:
    value = generate("/rest/media/post/list", "POST", now_unix=1_783_568_975)
    assert is_real_statsig(value)
    padding = "=" * (-len(value) % 4)
    raw = base64.b64decode(value + padding)
    assert len(raw) == 70
    assert len(value) == 94  # 70 bytes → 94 raw base64 chars


def test_generate_is_path_and_time_sensitive() -> None:
    a = generate("/rest/media/post/list", "POST", now_unix=1_783_568_975)
    b = generate("/rest/media/folder/list", "POST", now_unix=1_783_568_975)
    c = generate("/rest/media/post/list", "POST", now_unix=1_783_568_976)
    # XOR key is random so values differ, but all must be valid shape.
    assert is_real_statsig(a) and is_real_statsig(b) and is_real_statsig(c)
    # Different path/time should not produce identical ciphertext payload tails
    # after stripping the random key — decode and compare XOR-unwrapped digests.
    def unwrap_digest(token: str) -> bytes:
        raw = base64.b64decode(token + "=" * (-len(token) % 4))
        key = raw[0]
        return bytes(b ^ key for b in raw[53:69])

    assert unwrap_digest(a) != unwrap_digest(b)
    assert unwrap_digest(a) != unwrap_digest(c)


def test_generate_matches_algorithm_material() -> None:
    # Rebuild SHA material with known pair defaults and check digest region.
    from grok_imagine_archive import statsig as mod

    seed, hex_value = mod._ensure_pair()  # noqa: SLF001
    now = 1_783_568_975
    number = (now - mod._STATSIG_EPOCH) & 0xFFFFFFFF  # noqa: SLF001
    path = "/rest/media/post/list"
    method = "POST"
    material = f"{method}!{path}!{number}{mod._STATSIG_SALT}{hex_value}".encode()  # noqa: SLF001
    expected = hashlib.sha256(material).digest()[:16]

    token = generate(path, method, now_unix=now)
    raw = base64.b64decode(token + "=" * (-len(token) % 4))
    key = raw[0]
    # seed region
    assert bytes(b ^ key for b in raw[1:49]) == seed
    # number LE
    assert struct.unpack("<I", bytes(b ^ key for b in raw[49:53]))[0] == number
    # digest prefix
    assert bytes(b ^ key for b in raw[53:69]) == expected
    # mark
    assert (raw[69] ^ key) == mod._STATSIG_MARK  # noqa: SLF001


def test_set_pair_rejects_bad_seed() -> None:
    try:
        set_pair("AAAA", "abc")
        assert False, "expected ValueError"
    except ValueError:
        pass

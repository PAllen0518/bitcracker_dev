"""Acceptance tests against the real CUDA implementation and executable."""

import base64
import hashlib
import json
import os
import random
import subprocess
from pathlib import Path

import pytest
from Crypto.Cipher import AES

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".cuda-build"
HARNESS = Path(
    os.environ.get(
        "CUDA_TEST_EXE",
        str(BUILD / "optimized_test.exe"),
    )
)
pytestmark = pytest.mark.skipif(
    not HARNESS.exists(),
    reason="Build the native CUDA test target first",
)
BASE58 = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
PLAINTEXT = b"K" + b"1" * 31


def invoke(*arguments, check=True):
    """Run a bounded native harness command."""
    result = subprocess.run(
        [str(HARNESS), *map(str, arguments)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result


def encrypt_fixture(password, plaintext=PLAINTEXT, salt=b"12345678"):
    """Create a deterministic independent MultiBit cryptographic fixture."""
    first = hashlib.md5(password + salt).digest()
    second = hashlib.md5(first + password + salt).digest()
    iv = hashlib.md5(second + password + salt).digest()
    encrypted = AES.new(first + second, AES.MODE_CBC, iv).encrypt(plaintext)
    return salt, encrypted


def oracle(password, salt, encrypted):
    """Return the expected result using independent MD5 and AES libraries."""
    if not password:
        return False
    first = hashlib.md5(password + salt).digest()
    second = hashlib.md5(first + password + salt).digest()
    iv = hashlib.md5(second + password + salt).digest()
    plain = AES.new(first + second, AES.MODE_CBC, iv).decrypt(encrypted)
    return plain[0] in b"LK5Q" and all(byte in BASE58 for byte in plain)


def crypto_run(tmp_path, candidates, fixture, mode=1):
    """Send exact raw bytes to a CUDA kernel and return per-candidate results."""
    salt, encrypted = fixture
    path = tmp_path / "crypto.txt"
    path.write_text(
        f"{salt.hex()}\n{encrypted.hex()}\n{len(candidates)}\n"
        + "\n".join(value.hex() or "-" for value in candidates)
        + "\n",
        encoding="ascii",
    )
    return json.loads(invoke("crypto", path, mode).stdout)


def test_known_wallet_password(tmp_path):
    fixture = ROOT / "btcrecover/test/test-wallets/multibit-wallet.key"
    lines = [
        line
        for line in fixture.read_text().splitlines()
        if line and not line.startswith("#")
    ]
    data = base64.b64decode("".join(lines))
    candidates = [b"btcr-test-password", b"wrong", b"", b"x" * 40]
    result = crypto_run(tmp_path, candidates, (data[8:16], data[16:48]))
    assert [row[0] for row in result["rows"]] == [1, 0, 0, 0]


@pytest.mark.parametrize(
    "length",
    [
        1,
        15,
        16,
        30,
        31,
        32,
        47,
        48,
        55,
        56,
        63,
        64,
        95,
        96,
        111,
        112,
        127,
        128,
    ],
)
def test_length_boundaries_are_correct(tmp_path, length):
    password = bytes((index * 17 + 33) % 256 for index in range(length))
    candidates = [password, password[:-1] + bytes([password[-1] ^ 1]), b""]
    fixture = encrypt_fixture(password)
    result = crypto_run(tmp_path, candidates, fixture)
    assert [bool(row[0]) for row in result["rows"]] == [True, False, False]
    if length <= 31:
        assert result["rows"][0][2] == 3


def test_gpu_matches_independent_crypto(tmp_path):
    generator = random.Random(20260920)
    candidates = [
        generator.randbytes(generator.randrange(1, 129)) for _ in range(257)
    ]
    fixture = encrypt_fixture(candidates[128])
    expected = [oracle(value, *fixture) for value in candidates]
    assert expected.count(True) == 1
    for mode in [0, 1, 2, 3]:
        result = crypto_run(tmp_path, candidates, fixture, mode)
        assert [bool(row[0]) for row in result["rows"]] == expected


def test_second_block_defers_iv(tmp_path):
    password = b"reject-before-iv"
    fixture = encrypt_fixture(password, b"K" + b"1" * 15 + b"!" * 16)
    result = crypto_run(tmp_path, [password], fixture)
    assert result["rows"][0] == [0, 0, 2]
    for plaintext in [b"!" + b"1" * 31, b"K!" + b"1" * 30]:
        result = crypto_run(
            tmp_path,
            [password],
            encrypt_fixture(password, plaintext),
        )
        assert result["rows"][0] == [0, 1, 3]


@pytest.mark.parametrize("count", [1, 31, 32, 33, 255, 256, 257])
def test_shared_tables_handle_partial_blocks(tmp_path, count):
    candidates = [b"wrong"] * count
    candidates[-1] = b"correct"
    result = crypto_run(tmp_path, candidates, encrypt_fixture(b"correct"))
    assert result["shared"] > 0
    assert [row[0] for row in result["rows"]] == [0] * (count - 1) + [1]


def test_compact_stride_preserves_bytes():
    assert invoke("compact").stdout.split() == ["32", "64", "64", "128", "128"]


@pytest.mark.parametrize("flags", [1, 2, 4, 8, 16, 32, 3, 28, 63])
@pytest.mark.parametrize("budget", [0, 1, 2])
def test_streamed_typos_preserve_reference_order(flags, budget):
    for base in [b"aB", b"aa", b"abcd"]:
        expected = invoke("typos", base.hex(), budget, flags, 0).stdout
        actual = invoke("typos", base.hex(), budget, flags, 1).stdout
        assert actual == expected


@pytest.mark.parametrize(
    "name",
    [
        "assembly",
        "pool",
        "pipeline",
        "resume",
        "checkpoint",
        "legacy",
        "bounded_typos",
        "cancel",
        "mixed_lengths",
        "md5",
        "parallel",
        "block_merge",
        "chunk_reuse",
        "block_boundaries",
        "raw_blocks",
        "pool_ownership",
        "pool_progress",
        "pool_cancel",
        "seeded_blocks",
    ],
)
def test_native_contract(name):
    assert invoke("contract", name).stdout.strip() == "PASS"


def test_parallel_cancellation_wakes_blocked_workers():
    """Stopping a backed-up parallel generator must join every worker."""
    result = subprocess.run(
        [str(HARNESS), "contract", "parallel_cancel"],
        capture_output=True,
        text=True,
        check=False,
        timeout=18,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "PASS"


def test_rejects_overlength_candidate(tmp_path):
    fixture = encrypt_fixture(b"correct")
    path = tmp_path / "bad-crypto.txt"
    path.write_text(
        f"{fixture[0].hex()}\n{fixture[1].hex()}\n1\n"
        + (b"x" * 129).hex()
        + "\n",
        encoding="ascii",
    )
    result = invoke("crypto", path, 1, check=False)
    assert result.returncode != 0
    assert "exceeds 128" in result.stderr


@pytest.mark.parametrize("base,flags", [(b"a" * 128, 2), (b"1" * 128, 16)])
def test_degenerate_typos_finish_without_enumerating_noops(base, flags):
    result = subprocess.run(
        [str(HARNESS), "typos", base.hex(), "64", str(flags), "1"],
        capture_output=True,
        text=True,
        check=True,
        timeout=2,
    )
    assert result.stdout.strip() == base.hex()


def test_seeded_typo_sequences_match_reference():
    generator = random.Random(20260920)
    for _ in range(40):
        base = bytes(
            generator.choice(b"aABb119!")
            for _ in range(generator.randrange(1, 9))
        )
        budget = generator.randrange(4)
        flags = generator.randrange(1, 64)
        expected = invoke("typos", base.hex(), budget, flags, 0).stdout
        actual = invoke("typos", base.hex(), budget, flags, 1).stdout
        assert actual == expected

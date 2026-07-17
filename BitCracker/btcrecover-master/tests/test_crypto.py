"""Known-answer tests for the MultiBit Classic key derivation.

These pin the crypto against the test wallet shipped with btcrecover, whose
password is known. If the MD5/AES/base58 derivation ever regresses, the correct
password stops validating (or a wrong one starts) and these fail.
"""

import multibit_check
from conftest import TEST_WALLET


def test_wallet_loads():
    w = multibit_check.MultiBitWallet.load(TEST_WALLET)
    assert len(w.salt) == 8
    assert len(w.encrypted) == 32


def test_correct_password_validates(wallet, correct_password):
    assert wallet.check(correct_password) is True


def test_wrong_passwords_rejected(wallet):
    for bad in ["btcr-wrong-password-1", "", "btcr-test-passwor",
                "btcr-test-password ", "BTCR-TEST-PASSWORD", "x" * 40]:
        assert wallet.check(bad) is False, f"{bad!r} should not validate"


def test_derivation_is_deterministic(wallet, correct_password):
    assert wallet.check(correct_password) == wallet.check(correct_password)


def test_bad_file_rejected(tmp_path):
    junk = tmp_path / "not-a-wallet.key"
    junk.write_text("bm90IGEgd2FsbGV0IGZpbGUgYXQgYWxsLCBqdXN0IGdhcmJhZ2U=")
    try:
        multibit_check.MultiBitWallet.load(str(junk))
        assert False, "expected ValueError for a non-MultiBit file"
    except ValueError:
        pass

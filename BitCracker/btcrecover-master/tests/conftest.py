"""Shared pytest fixtures and import path setup.

multibit_check.py lives one directory up; add it to sys.path so the tests can
import it without an install step.
"""

import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# The MultiBit Classic test wallet shipped with btcrecover. Its known password
# is "btcr-test-password" (see btcrecover/test/test_passwords.py).
TEST_WALLET = os.path.join(_ROOT, "btcrecover", "test", "test-wallets", "multibit-wallet.key")
TEST_WALLET_PASSWORD = "btcr-test-password"


@pytest.fixture
def wallet():
    import multibit_check
    return multibit_check.MultiBitWallet.load(TEST_WALLET)


@pytest.fixture
def correct_password():
    return TEST_WALLET_PASSWORD

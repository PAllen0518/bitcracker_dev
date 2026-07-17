"""Tests for tokenlist parsing, base-password generation, and end-to-end search."""

from multibit_check import (MultiBitWallet, TypoSpec, generate_passwords,
                            parse_tokenlist, search)
from conftest import TEST_WALLET, TEST_WALLET_PASSWORD


def write_tokenlist(tmp_path, text):
    path = tmp_path / "tokens.txt"
    path.write_text(text)
    return str(path)


def test_required_single_token(tmp_path):
    tl = write_tokenlist(tmp_path, "+ hello\n")
    assert list(generate_passwords(parse_tokenlist(tl))) == ["hello"]


def test_optional_line_can_be_skipped(tmp_path):
    tl = write_tokenlist(tmp_path, "+ base\nsuffix\n")
    out = set(generate_passwords(parse_tokenlist(tl)))
    assert "base" in out            # optional line skipped
    assert "basesuffix" in out      # optional line included


def test_mutually_exclusive_tokens(tmp_path):
    tl = write_tokenlist(tmp_path, "+ cat dog\n")
    assert set(generate_passwords(parse_tokenlist(tl))) == {"cat", "dog"}


def test_permutation_of_free_tokens(tmp_path):
    tl = write_tokenlist(tmp_path, "+ a\n+ b\n")
    assert set(generate_passwords(parse_tokenlist(tl))) == {"ab", "ba"}


def test_first_position_anchor(tmp_path):
    tl = write_tokenlist(tmp_path, "+ ^head\n+ tail\n")
    out = set(generate_passwords(parse_tokenlist(tl)))
    assert out == {"headtail"}      # ^head is pinned first, so no "tailhead"


def test_positional_anchor(tmp_path):
    tl = write_tokenlist(tmp_path, "+ a\n+ ^2^b\n+ c\n")
    for pw in generate_passwords(parse_tokenlist(tl)):
        assert pw[1] == "b"         # b pinned to position 2 (index 1)


def test_digit_wildcard(tmp_path):
    tl = write_tokenlist(tmp_path, "+ pin\n+ %0,2d\n")
    out = set(generate_passwords(parse_tokenlist(tl)))
    assert "pin" in out             # 0 digits
    assert "pin7" in out            # 1 digit
    assert "pin42" in out           # 2 digits


def test_delimiter_allows_spaces_in_tokens(tmp_path):
    # Two required lines, each a single token containing a literal space. With a
    # comma delimiter the spaces stay inside the tokens instead of splitting them.
    tl = write_tokenlist(tmp_path, "+ correct horse\n+ battery staple\n")
    out = set(generate_passwords(parse_tokenlist(tl, delimiter=",")))
    assert out == {"correct horsebattery staple", "battery staplecorrect horse"}


def test_alternatives_on_one_line_are_not_concatenated(tmp_path):
    # Same-line tokens are mutually exclusive alternatives, one per password.
    tl = write_tokenlist(tmp_path, "+ correct horse,battery staple\n")
    out = set(generate_passwords(parse_tokenlist(tl, delimiter=",")))
    assert out == {"correct horse", "battery staple"}


def test_end_to_end_search_finds_known_password(tmp_path):
    wallet = MultiBitWallet.load(TEST_WALLET)
    tl = write_tokenlist(tmp_path, f"+ wrong-guess {TEST_WALLET_PASSWORD} another-wrong\n")
    assert search(wallet, parse_tokenlist(tl), TypoSpec()) == TEST_WALLET_PASSWORD


def test_end_to_end_search_with_typo(tmp_path):
    # Base password has one wrong character; a single closecase typo fixes it.
    wallet = MultiBitWallet.load(TEST_WALLET)
    tl = write_tokenlist(tmp_path, "+ btcr-test-Password\n")   # capital P
    spec = TypoSpec(max_typos=1, case=True)
    assert search(wallet, parse_tokenlist(tl), spec) == TEST_WALLET_PASSWORD


def test_search_returns_none_when_absent(tmp_path):
    wallet = MultiBitWallet.load(TEST_WALLET)
    tl = write_tokenlist(tmp_path, "+ definitely-not-it also-wrong\n")
    assert search(wallet, parse_tokenlist(tl), TypoSpec()) is None

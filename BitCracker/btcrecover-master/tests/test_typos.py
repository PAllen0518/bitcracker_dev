"""Tests for typo generation.

The expected variant counts are worked out by hand from btcrecover's typo
semantics, so these check the implementation against the spec, not just against
itself. The same three cases are used as smoke tests for the CUDA port.
"""

from multibit_check import TypoSpec, typo_variants


def variants(base, spec):
    return list(typo_variants(base, spec))


def test_no_typos_yields_only_base():
    assert variants("hello", TypoSpec()) == ["hello"]


def test_repeat_delete_closecase_count():
    # base "aB": each of 2 positions gets repeat + delete + closecase = 3 each,
    # plus the base itself = 1 + 3 + 3 = 7.
    spec = TypoSpec(max_typos=1, repeat=True, delete=True, closecase=True)
    out = variants("aB", spec)
    assert len(out) == 7
    assert "aB" in out           # base included
    assert "aaB" in out          # repeat pos 0
    assert "B" in out            # delete pos 0
    assert "AB" in out           # closecase pos 0
    assert "ab" in out           # closecase pos 1


def test_capslock_swap_shared_budget_count():
    # base "abcd", budget 2, capslock then swap. capslock gives abcd/ABCD;
    # swap adds single/double adjacent swaps within the remaining budget = 9.
    spec = TypoSpec(max_typos=2, capslock=True, swap=True)
    out = variants("abcd", spec)
    assert len(out) == 9
    assert "abcd" in out
    assert "ABCD" in out         # capslock
    assert "bacd" in out         # one swap
    assert "badc" in out         # two swaps
    assert "BACD" in out         # capslock + one swap (uses both typos)


def test_insert_count():
    # base "ab", insert one of {x,y} at any of 3 gaps = 6, plus base = 7.
    spec = TypoSpec(max_typos=1, insert="xy")
    out = variants("ab", spec)
    assert len(out) == 7
    for expected in ["ab", "xab", "yab", "axb", "ayb", "abx", "aby"]:
        assert expected in out


def test_variants_are_deduplicated():
    spec = TypoSpec(max_typos=2, case=True, capslock=True)
    out = variants("aa", spec)
    assert len(out) == len(set(out))


def test_budget_caps_typos():
    # With a budget of 1, no variant should differ from the base by 2 edits.
    spec = TypoSpec(max_typos=1, insert="x")
    out = variants("ab", spec)
    assert "xxab" not in out       # would be 2 inserts
    assert all(len(v) <= 3 for v in out)


def test_replace_and_map():
    spec = TypoSpec(max_typos=1, replace="Z")
    assert "Zb" in variants("ab", spec)
    spec_map = TypoSpec(max_typos=1, typos_map={"a": "@"})
    assert "@b" in variants("ab", spec_map)

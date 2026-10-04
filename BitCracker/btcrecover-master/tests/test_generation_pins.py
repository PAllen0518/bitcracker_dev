"""Keep generation pins append-only (GENERATION_BINDING_SPEC.md Amendment 1, A2).

The canary pins a fingerprint of the candidate-generation order to each
SAVE_GENERATION_VERSION. Equality alone cannot stop a developer from repinning
the current version after an order change, so this test compares the pin rows
with the integration branch: existing rows never change, versions run 1, 2, 3,
fingerprints never repeat, and the newest row is the build's version.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "tests/cuda_host_contracts.hpp"
SAVE_FORMAT = ROOT / "save_format.hpp"
BEGIN = "// GENERATION-PINS-BEGIN"
END = "// GENERATION-PINS-END"
ROW = re.compile(r'\s*\{(\d+), "([0-9a-f]{64})"\},\s*')
VERSION = re.compile(r"static const uint32_t SAVE_GENERATION_VERSION = (\d+);")
BASE_REF_VARIABLE = "GENERATION_PINS_BASE_REF"


def parse_pins(text, label, required):
    """Return (rows, errors) for the pin rows between the markers."""
    begin, end = text.count(BEGIN), text.count(END)
    if begin == end == 0 and not required:
        return [], []  # bootstrap: the integration branch has no pins yet
    if begin != 1 or end != 1 or text.index(BEGIN) > text.index(END):
        return [], [f"{label}: expected one {BEGIN} then one {END} marker"]
    body = text.split(BEGIN, 1)[1].split(END, 1)[0]
    rows, errors = [], []
    for line in body.splitlines():
        if not line.strip():
            continue
        match = ROW.fullmatch(line)
        if match is None:
            errors.append(f"{label}: unparseable pin line {line.strip()!r}")
        else:
            rows.append((int(match.group(1)), match.group(2)))
    return rows, errors


def check_generation_pins(base_text, current_text, generation_version):
    """Return every append-only violation; an empty list means valid."""
    base, errors = parse_pins(base_text, "integration branch", required=False)
    current, current_errors = parse_pins(current_text, "current", required=True)
    errors += current_errors
    if errors:
        return errors
    if current[: len(base)] != base:
        errors.append(
            "existing pin rows were edited, removed or reordered; never edit "
            "a pin - bump SAVE_GENERATION_VERSION and append a row"
        )
    if [version for version, _ in current] != list(range(1, len(current) + 1)):
        errors.append("pin versions must run 1, 2, 3, ... with no gaps or repeats")
    fingerprints = [fingerprint for _, fingerprint in current]
    if len(set(fingerprints)) != len(fingerprints):
        errors.append(
            "a fingerprint is pinned twice; a version bump needs a real "
            "candidate-order change"
        )
    if not current or current[-1][0] != generation_version:
        errors.append(
            "the newest pin must be the build's SAVE_GENERATION_VERSION "
            f"({generation_version})"
        )
    return errors


def git(*arguments):
    """Run git in this checkout and return stripped standard output."""
    return subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=60,
    ).stdout.strip()


def integration_pin_text():
    """Return the contracts file at the merge-base with the integration ref."""
    ref = os.environ.get(BASE_REF_VARIABLE, "master")
    try:
        base = git("merge-base", "HEAD", ref)
        return git("show", f"{base}:./tests/cuda_host_contracts.hpp")
    except (OSError, subprocess.SubprocessError) as error:
        detail = getattr(error, "stderr", "") or error
        pytest.fail(
            f"cannot read the pin table from integration ref {ref!r}; set "
            f"{BASE_REF_VARIABLE} to the branch this work merges into "
            f"({detail})"
        )


def build_generation_version():
    """Return SAVE_GENERATION_VERSION as declared in save_format.hpp."""
    matches = VERSION.findall(SAVE_FORMAT.read_text(encoding="utf-8"))
    assert len(matches) == 1, "expected one SAVE_GENERATION_VERSION"
    return int(matches[0])


def test_repository_pins_are_append_only():
    errors = check_generation_pins(
        integration_pin_text(),
        CONTRACTS.read_text(encoding="utf-8"),
        build_generation_version(),
    )
    assert errors == []


A, B = "a" * 64, "b" * 64


def table(*rows):
    """Render pin rows between the markers, as in the contracts file."""
    lines = [f"        {BEGIN}"]
    lines += [f'        {{{version}, "{hex_}"}},' for version, hex_ in rows]
    lines.append(f"        {END}")
    return "    static const Pin pins[] = {\n" + "\n".join(lines) + "\n    };\n"


@pytest.mark.parametrize(
    "base,current,version",
    [
        ("no pin table yet", table((1, A)), 1),        # bootstrap
        (table((1, A)), table((1, A)), 1),             # unchanged
        (table((1, A)), table((1, A), (2, B)), 2),     # bump plus new row
    ],
    ids=["bootstrap", "unchanged", "bump_and_append"],
)
def test_valid_pin_histories_pass(base, current, version):
    assert check_generation_pins(base, current, version) == []


@pytest.mark.parametrize(
    "base,current,version,reason",
    [
        (table((1, A)), table((1, B)), 1, "edited"),             # repin, no bump
        (table((1, A)), table((1, A), (2, B)), 1, "newest"),     # row, no bump
        (table((1, A)), table((1, A), (1, B)), 1, "1, 2, 3"),    # repeated version
        (table((1, A)), table((1, A), (3, B)), 3, "1, 2, 3"),    # version gap
        (table((1, A)), table((1, A), (2, A)), 2, "twice"),      # bump, same order
        (table((1, A), (2, B)), table((1, A)), 1, "edited"),     # row deleted
        (table((1, A), (2, B)), table((2, B), (1, A)), 1, "edited"),  # reordered
        (table((1, A)), "markers removed", 1, "marker"),
        (table((1, A)), table((1, A)).replace(END, "junk\n" + END), 1,
         "unparseable"),
        ("no pin table yet", table(), 1, "newest"),              # empty table
    ],
    ids=[
        "repin_without_bump", "append_without_bump", "repeated_version",
        "version_gap", "bump_reusing_fingerprint", "deleted_row",
        "reordered_rows", "markers_removed", "unparseable_line", "empty_table",
    ],
)
def test_pin_violations_fail(base, current, version, reason):
    errors = check_generation_pins(base, current, version)
    assert any(reason in error for error in errors), errors

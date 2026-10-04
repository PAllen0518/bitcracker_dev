"""Check that the mutation driver only counts a kill it actually observed.

A kill means the named test ran and failed. Any other pytest outcome (usage
error, nothing collected, interrupted, setup error, skip, a different test)
is a runner error, and the driver must fail rather than report a kill.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import validate_save_format as driver  # noqa: E402

TEST = "tests/test_cuda.py::test_native_contract[save_reject_generation]"
CLASSNAME = "tests.test_cuda"
NAME = "test_native_contract[save_reject_generation]"


def junit(*cases):
    """Render a pytest-style JUnit report with the given test cases."""
    rendered = "".join(
        f'<testcase classname="{classname}" name="{name}" time="0.01">'
        f"{child}</testcase>"
        for classname, name, child in cases
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
        f'<testsuite name="pytest">{rendered}</testsuite></testsuites>'
    )


FAILURE = '<failure message="boom">trace</failure>'
ERROR = '<error message="fixture failed">trace</error>'
SKIPPED = '<skipped message="no executable" />'


def test_failed_named_test_is_a_kill():
    report = junit((CLASSNAME, NAME, FAILURE))
    assert driver.classify_test(1, report, TEST) == "killed"


def test_passed_named_test_is_a_survivor():
    report = junit((CLASSNAME, NAME, ""))
    assert driver.classify_test(0, report, TEST) == "survived"


def test_test_in_a_class_maps_to_its_classname():
    report = junit(("tests.test_x.TestGroup", "test_y", FAILURE))
    assert driver.classify_test(1, report, "tests/test_x.py::TestGroup::test_y") == "killed"


@pytest.mark.parametrize(
    "returncode,report",
    [
        (4, junit()),                                   # test not found
        (5, junit()),                                   # nothing collected
        (2, junit((CLASSNAME, NAME, FAILURE))),         # interrupted
        (3, junit((CLASSNAME, NAME, FAILURE))),         # internal error
        (1, None),                                      # no report written
        (1, "<testsuites><testsuite>"),                 # unreadable report
        (1, junit((CLASSNAME, NAME, ERROR))),           # setup error, not a test failure
        (0, junit((CLASSNAME, NAME, SKIPPED))),         # skipped: nothing ran
        (1, junit((CLASSNAME, "test_other", FAILURE))),  # a different test failed
        (1, junit((CLASSNAME, NAME, FAILURE), (CLASSNAME, "test_other", ""))),
        (1, junit((CLASSNAME, NAME, ""))),              # exit 1 but the test passed
        (0, junit((CLASSNAME, NAME, FAILURE))),         # exit 0 but the test failed
    ],
    ids=[
        "not_found", "no_tests", "interrupted", "internal_error", "no_report",
        "unreadable_report", "setup_error", "skipped", "other_test",
        "extra_test", "exit1_but_passed", "exit0_but_failed",
    ],
)
def test_anything_else_is_a_runner_error(returncode, report):
    with pytest.raises(RuntimeError):
        driver.classify_test(returncode, report, TEST)


@pytest.mark.parametrize(
    "statuses,expected",
    [
        (["killed", "killed"], driver.EXIT_ALL_KILLED),
        (["killed", "survived"], driver.EXIT_SURVIVOR),
        (["killed", "runner_error"], driver.EXIT_RUNNER_ERROR),
        (["build_failed"], driver.EXIT_RUNNER_ERROR),
        (["survived", "runner_error"], driver.EXIT_RUNNER_ERROR),
    ],
    ids=["all_killed", "survivor", "runner_error", "build_failed", "both"],
)
def test_exit_code(statuses, expected):
    assert driver.exit_code([{"status": status} for status in statuses]) == expected

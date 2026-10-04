"""Check that the mutation driver only counts a kill it actually observed.

A kill means the named test ran and failed. Any other pytest outcome (usage
error, nothing collected, interrupted, setup error, skip, a different test)
is a runner error, and the driver must fail rather than report a kill.
"""

import sys

import pytest

from tools import validate_save_format as driver

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


OUTPUT = (
    "<properties><property name='k' value='v' /></properties>"
    "<system-out>out</system-out><system-err>err</system-err>"
)


def test_output_elements_do_not_hide_a_kill():
    report = junit((CLASSNAME, NAME, OUTPUT + FAILURE))
    assert driver.classify_test(1, report, TEST) == "killed"


def test_output_elements_do_not_hide_a_survivor():
    report = junit((CLASSNAME, NAME, OUTPUT))
    assert driver.classify_test(0, report, TEST) == "survived"


def test_bare_testsuite_root_is_accepted():
    report = (
        '<testsuite name="pytest"><testcase classname="'
        f'{CLASSNAME}" name="{NAME}">{FAILURE}</testcase></testsuite>'
    )
    assert driver.classify_test(1, report, TEST) == "killed"


def test_real_pytest_report_layout_is_accepted():
    # Byte layout pytest 8 writes (from a real run), with a failure added.
    report = (
        '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
        '<testsuite name="pytest" errors="0" failures="1" skipped="0" tests="1" '
        'time="0.4" timestamp="2026-10-03T17:31:53" hostname="h">'
        f'<testcase classname="{CLASSNAME}" name="{NAME}" time="0.01">'
        f"{FAILURE}</testcase></testsuite></testsuites>"
    )
    assert driver.classify_test(1, report, TEST) == "killed"


@pytest.mark.parametrize(
    "returncode,report",
    [
        (1, junit((CLASSNAME, NAME, FAILURE)).replace("testsuites", "not_junit")),
        (1, junit((CLASSNAME, NAME, FAILURE)).replace(
            '<testsuite name="pytest">', '<testsuite name="pytest"><testsuite>'
        ).replace("</testsuite>", "</testsuite></testsuite>")),
        (0, junit((CLASSNAME, NAME, "<weird />"))),
        (1, junit((CLASSNAME, NAME, FAILURE + "<weird />"))),
        (1, junit((CLASSNAME, NAME, FAILURE + FAILURE))),
        (1, junit((CLASSNAME, NAME, FAILURE + SKIPPED))),
        (1, junit((CLASSNAME, NAME, "<system-out>" + FAILURE + "</system-out>"))),
    ],
    ids=[
        "wrong_root", "nested_testsuite", "unknown_child_on_pass",
        "unknown_child_on_fail", "duplicate_failure", "two_outcomes",
        "failure_hidden_in_output",
    ],
)
def test_unsupported_report_structure_is_a_runner_error(returncode, report):
    with pytest.raises(RuntimeError):
        driver.classify_test(returncode, report, TEST)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--negative-control", "--only", "drop_generation_check"],
        ["--runner-control", "--only", "drop_generation_check"],
        ["--report-control", "--only", "drop_generation_check"],
        ["--negative-control", "--runner-control"],
        ["--negative-control", "--report-control"],
        ["--runner-control", "--report-control"],
    ],
    ids=[
        "negative_with_only", "runner_with_only", "report_with_only",
        "negative_and_runner", "negative_and_report", "runner_and_report",
    ],
)
def test_controls_cannot_be_combined(monkeypatch, arguments):
    def refuse(mutant):
        raise AssertionError(f"ran {mutant.name} for a rejected command line")

    monkeypatch.setattr(driver, "run_mutant", refuse)
    monkeypatch.setattr(sys, "argv", ["validate_save_format.py", *arguments])
    with pytest.raises(SystemExit) as stopped:
        driver.main()
    assert stopped.value.code != 0


def test_report_fault_breaks_only_the_root():
    report = junit((CLASSNAME, NAME, FAILURE))
    corrupted = driver.corrupt_report_root(report)
    assert corrupted != report
    assert driver.classify_test(1, report, TEST) == "killed"
    with pytest.raises(RuntimeError):
        driver.classify_test(1, corrupted, TEST)


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

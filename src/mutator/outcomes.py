"""Classify failed build-and-test commands using evidence from the tool."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from mutator.runner import CommandResult, TestPlan

Outcome = Literal["survived", "killed", "invalid", "error"]

_CONTROL_SEQUENCE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_MAVEN_COMPILER = re.compile(
    r"^\[ERROR\] Failed to execute goal \S*maven-compiler-plugin:\S+:(?:compile|testCompile)"
    r" \([^\n]*\) on project [^\n]+: Compilation failure\b",
    re.M,
)
_MAVEN_DIAGNOSTIC = re.compile(
    r"^\[ERROR\] (?P<file>[^\n]+?\.java):\[\d+,\d+\][ \t]*(?P<message>[^\n]*)", re.M
)
_MAVEN_TEST = re.compile(
    r"^\[ERROR\] Failed to execute goal \S*maven-surefire-plugin:\S+:test"
    r" \([^\n]*\) on project [^\n]+: There are test failures\.",
    re.M,
)
_MAVEN_COUNTS = re.compile(r"^\[ERROR\] Tests run: \d+, Failures: (\d+), Errors: (\d+)", re.M)
_MAVEN_TEST_RUN_ERROR = re.compile(
    r"^\[ERROR\].*(?:There was an error in the forked process|"
    r"forked VM terminated without properly saying goodbye|"
    r"Error occurred in starting fork)",
    re.M,
)

_GO_SOURCE_LINE = re.compile(r"^(?P<file>[^\n]+?\.go):\d+:\d+: (?P<message>[^\n]+)$")
_GO_BUILD_FAILED = re.compile(r"^FAIL[ \t]+\S+ \[build failed\]$", re.M)
_GO_SETUP_FAILED = re.compile(r"^FAIL[ \t]+\S+ \[setup failed\]$", re.M)
_GO_TEST_FAILURE = re.compile(r"^--- FAIL: \S+ \([^)]+\)$", re.M)
_GO_TEST_SUMMARY = re.compile(r"^FAIL[ \t]+\S+[ \t]+\S+s$", re.M)
_GO_SYNTAX = re.compile(r"^(?:expected |syntax error:|imports must appear )")
_GO_IO_ERRORS = ("open ", "read ", "write ", "could not import ", "cannot import ", "pattern ")

_CARGO_COMPILE_ERROR = re.compile(r"^error: could not compile ", re.M)
_RUSTC_DIAGNOSTIC = re.compile(
    r"^error(?:\[E\d{4}\])?: (?P<message>[^\n]+)\n[ \t]*--> (?P<file>[^\n]+?\.rs):\d+:\d+", re.M
)
_CARGO_TEST_FAILURE = re.compile(r"^test result: FAILED\.[^\n]*\b[1-9]\d* failed\b", re.M)
_CARGO_TEST_EXIT = re.compile(r"^error: (?:test|doctest) failed, to rerun pass ", re.M)
_RUSTC_IO_ERRORS = ("couldn't read ", "failed to read ", "could not write ", "can't find crate ", "linking with ")

_ROSLYN_DIAGNOSTIC = re.compile(
    r"^[ \t]*(?P<file>[^\n]+?\.cs)\(\d+,\d+(?:,\d+,\d+)?\)\s*:\s*error (CS\d{4})\b",
    re.M | re.I,
)
_ROSLYN_IO_ERRORS = frozenset({
    "CS0006", "CS0041", "CS1566", "CS1569", "CS2001", "CS2012", "CS8032", "CS8784", "CS8785",
})
_VSTEST_TEST_FAILURE = re.compile(
    r"^[ \t]*(?:Failed![ \t]+-[ \t]+Failed:|Test summary:[ \t]+total:[ \t]+\d+,[ \t]+failed:)"
    r"[ \t]+[1-9]\d*\b", re.M,
)
_MTP_SUMMARY = re.compile(r"^[ \t]*Test run summary:", re.M)
_MTP_TEST_FAILURE = re.compile(r"^[ \t]+failed:[ \t]+[1-9]\d*\b", re.M)
_DOTNET_TEST_RUN_ERROR = re.compile(
    r"^[ \t]*(?:Test Run Aborted\.|The active test run was aborted|"
    r"Testhost process .* exited with error|error:[ \t]+[1-9]\d*)"
    r"|^[^\n]* : error (?:NU|MSB|NETSDK)\d{4}\b",
    re.M | re.I,
)


def _events(output: str) -> Iterator[dict]:
    for line in output.splitlines():
        if line.startswith("{"):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event


def _project_source(file: str, cwd: Path, mutant: Path, root: Path) -> bool:
    file = file.strip()
    if os.name == "nt" and re.match(r"^/[A-Za-z]:/", file):
        # Maven can render an absolute Windows source path as /C:/...
        file = file[1:]
    path = Path(file)
    for base in (cwd, root, mutant.parent):
        try:
            source = (base / path).resolve()
            if source.is_relative_to(root) and source.is_file():
                return True
        except (OSError, ValueError):
            continue
    return False


def _maven(output: str, cwd: Path, mutant: Path, root: Path) -> Outcome:
    if _MAVEN_COMPILER.search(output) and any(
        not match["message"].startswith(("error reading ", "error while writing "))
        and _project_source(match["file"], cwd, mutant, root)
        for match in _MAVEN_DIAGNOSTIC.finditer(output)
    ):
        return "invalid"
    if _MAVEN_TEST_RUN_ERROR.search(output):
        return "error"
    if _MAVEN_TEST.search(output) and any(
        int(failed) + int(errored) > 0 for failed, errored in _MAVEN_COUNTS.findall(output)
    ):
        return "killed"
    return "error"


def _go_compiler_locations(output: str, *, setup_failed: bool) -> Iterator[str]:
    vet_block = False
    for line in output.splitlines():
        if line.startswith("# "):
            vet_block = line.startswith("# [")
        match = _GO_SOURCE_LINE.fullmatch(line)
        if match is None or vet_block or match["message"].startswith(_GO_IO_ERRORS):
            continue
        if not setup_failed or _GO_SYNTAX.search(match["message"]):
            yield match["file"]


def _go_build_output(events: list[dict]) -> str | None:
    """Return the diagnostics attached to failed builds, if Go reported any failed build."""
    builds = [event.get("ImportPath") for event in events if event.get("Action") == "build-fail"]
    if not builds:
        return None
    return "".join(
        event["Output"] for event in events
        if event.get("Action") == "build-output" and event.get("ImportPath") in builds
        and isinstance(event.get("Output"), str)
    )


def _go_build_failure(output: str, events: list[dict]) -> tuple[str, bool] | None:
    build_text = _go_build_output(events)
    test_output = "".join(
        event["Output"] for event in events
        if event.get("Action") == "output" and not event.get("Test") and isinstance(event.get("Output"), str)
    )
    summary = output + "\n" + test_output
    setup_failed = bool(_GO_SETUP_FAILED.search(summary))
    if build_text is not None or setup_failed or _GO_BUILD_FAILED.search(summary):
        # Go 1.23 and gotestjsonbuildtext emit compiler diagnostics as text.
        return build_text or output, setup_failed
    return None


def _go(output: str, cwd: Path, mutant: Path, root: Path) -> Outcome:
    events = list(_events(output))
    failure = _go_build_failure(output, events)
    if failure is not None:
        diagnostics, setup_failed = failure
        locations = _go_compiler_locations(diagnostics, setup_failed=setup_failed)
        if any(_project_source(file, cwd, mutant, root) for file in locations):
            return "invalid"
        return "error"
    if any(event.get("Action") == "fail" and event.get("Test") for event in events):
        return "killed"
    if _GO_TEST_FAILURE.search(output) and _GO_TEST_SUMMARY.search(output):
        return "killed"
    return "error"


def _rustc_error_spans(events: list[dict]) -> Iterator[dict]:
    for event in events:
        if event.get("reason") != "compiler-message":
            continue
        message = event.get("message")
        if not isinstance(message, dict) or message.get("level") != "error":
            continue
        description = message.get("message")
        spans = message.get("spans")
        if not isinstance(description, str) or description.startswith(_RUSTC_IO_ERRORS) or not isinstance(spans, list):
            continue
        yield from (span for span in spans if isinstance(span, dict))


def _rustc_primary_files(events: list[dict]) -> Iterator[str]:
    for span in _rustc_error_spans(events):
        file = span.get("file_name")
        if span.get("is_primary") is True and isinstance(file, str) and file.endswith(".rs"):
            yield file


def _rustc_text_files(output: str) -> Iterator[str]:
    if _CARGO_COMPILE_ERROR.search(output):
        for match in _RUSTC_DIAGNOSTIC.finditer(output):
            if not match["message"].startswith(_RUSTC_IO_ERRORS):
                yield match["file"]


def _cargo(output: str, cwd: Path, mutant: Path, root: Path) -> Outcome:
    events = []
    for event in _events(output):
        if event.get("reason") == "build-finished":
            if event.get("success") is False:
                located = any(_project_source(file, cwd, mutant, root) for file in _rustc_primary_files(events))
                return "invalid" if located else "error"
            break
        events.append(event)
    else:
        # A supplied cargo command can use rustc's human-readable diagnostics.
        if any(_project_source(file, cwd, mutant, root) for file in _rustc_text_files(output)):
            return "invalid"
    if _CARGO_TEST_FAILURE.search(output) and _CARGO_TEST_EXIT.search(output):
        return "killed"
    return "error"


def _dotnet(output: str, cwd: Path, mutant: Path, root: Path) -> Outcome:
    if any(
        match[2].upper() not in _ROSLYN_IO_ERRORS and _project_source(match["file"], cwd, mutant, root)
        for match in _ROSLYN_DIAGNOSTIC.finditer(output)
    ):
        return "invalid"
    if _DOTNET_TEST_RUN_ERROR.search(output):
        return "error"
    if _VSTEST_TEST_FAILURE.search(output):
        return "killed"
    summaries = _MTP_SUMMARY.split(output)
    if any(_MTP_TEST_FAILURE.search(summary) for summary in summaries[1:]):
        return "killed"
    return "error"


_INTERPRET = {"maven": _maven, "go": _go, "cargo": _cargo, "dotnet": _dotnet}


def classify_mutant(
    plan: TestPlan,
    result: CommandResult,
    cwd: Path,
    mutant_path: Path,
    *,
    worker_root: Path | None = None,
) -> Outcome:
    """Return a scored outcome only when the test command supplies its evidence."""
    if result.spawn_failed:
        return "error"
    if result.code == 0 and not result.timed_out:
        return "survived"
    if plan.evidence == "exit-code":
        return "killed"
    if result.timed_out:
        return "error"
    output = _CONTROL_SEQUENCE.sub("", result.output)
    return _INTERPRET[plan.evidence](output, cwd, mutant_path, (worker_root or cwd).resolve())

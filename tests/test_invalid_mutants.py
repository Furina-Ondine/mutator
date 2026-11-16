from pathlib import Path

import pytest

from mutator.model import FormResult, Site
from mutator.cli import _finish, run
from mutator.edn import loads
from mutator.metrics import load_history, snapshot_path, write_results
from mutator.functions import mutation_id
from mutator.report import format_results
from mutator.runner import CommandResult
from mutator.workers import _execute


def _run(tmp_path, build_code, test_code, *, command=None, output="", relative="Value.cs"):
    original = b"class Value { bool F() => true; }"
    offset = original.index(b"true")
    site = Site(relative, "Value", "defn/F", "F", 1, offset, offset + 4, "true", "false", "boolean", "one")
    directory = tmp_path / "worker"
    directory.mkdir()
    target = directory / relative
    target.write_bytes(original)
    calls = []

    class Runner:
        def run(self, arguments, cwd, timeout):
            calls.append((arguments, timeout))
            code = build_code if arguments == ["dotnet", "build"] else test_code
            return CommandResult(code, False, 0.01, output)

    status = _execute(directory, site, tmp_path, relative, original, Runner(),
                      command or ["dotnet", "test"], tmp_path, 5, relative)
    assert target.read_bytes() == original
    return status, calls


def test_a_default_csharp_build_failure_is_invalid_and_does_not_run_tests(tmp_path):
    status, calls = _run(tmp_path, 1, 0)
    assert status == "invalid"
    assert [command for command, timeout in calls] == [["dotnet", "build"]]


def test_default_csharp_test_failures_are_killed_after_a_successful_build(tmp_path):
    status, calls = _run(tmp_path, 0, 1)
    assert status == "killed"
    assert calls == [(["dotnet", "build"], None), (["dotnet", "test", "--no-build"], 5)]


def test_successful_csharp_tests_survive_after_a_successful_build(tmp_path):
    status, calls = _run(tmp_path, 0, 0)
    assert status == "survived"
    assert len(calls) == 2


@pytest.mark.parametrize("output,expected", [
    ("D:/Project/Value.cs(1,27): error CS0019: Operator '-' cannot be applied [Tests.csproj]", "invalid"),
    ("Assertion failed: expected 1, actual 0", "killed"),
    ("Assertion failed: expected message 'error CS0019'", "killed"),
    ("\x1b[31mCSC : error CS1002: ; expected\x1b[0m", "invalid"),
])
def test_custom_commands_distinguish_located_compiler_errors_from_test_failures(tmp_path, output, expected):
    status, calls = _run(tmp_path, 0, 1, command="dotnet test --project Tests.csproj", output=output)
    assert status == expected
    assert len(calls) == 1


def test_other_languages_keep_the_existing_nonzero_test_status(tmp_path):
    status, calls = _run(tmp_path, 0, 1, command="tests", output="Value.cs(1,1): error CS0019: bad", relative="value.py")
    assert status == "killed"


def test_invalid_mutants_are_excluded_from_the_score():
    form = FormResult("defn/F", "Value", "F", False, "Value.cs", 1, 1, "hash", killed=1, survived=1, invalid=20)
    assert form.score == 0.5


def test_invalid_outcomes_and_counts_round_trip_without_becoming_kills(tmp_path):
    mutation = mutation_id("Value.cs", "Value", "defn/F", 0, 1, "+", "-")
    form = FormResult("defn/F", "Value", "F", False, "Value.cs", 1, 1, "hash", sites=1, invalid=1)
    write_results(tmp_path, "Value.cs", [form], {mutation: "invalid"})
    data = loads(snapshot_path(tmp_path, "Value").read_text(encoding="utf-8"))
    assert data["forms"][0]["invalid"] == 1
    assert data["forms"][0]["killed"] == 0
    assert load_history(tmp_path, "Value.cs").outcomes[mutation] == "invalid"
    assert "Invalid" in format_results([form])
    assert form.score is None


def test_invalid_mutants_have_a_distinct_nonzero_cli_status():
    assert _finish(False, False) == 0
    assert _finish(False, False, True) == 4
    assert _finish(False, True, True) == 4
    assert _finish(True, False, True) == 2


def test_a_compiler_rejected_mutant_is_reported_and_retried_without_a_false_kill(tmp_path, monkeypatch, capsys):
    path = tmp_path / "Price.cs"
    original = b"class Price { public string Add(string a, string b) => a + b; }"
    path.write_bytes(original)
    builds = []

    class Runner:
        verbose = False

        def run(self, command, cwd, timeout):
            if command == ["dotnet", "build"]:
                builds.append(cwd)
                assert b"a - b" in (cwd / "Price.cs").read_bytes()
                return CommandResult(1, False, 0.01, "Price.cs(1,58): error CS0019: Invalid string subtraction")
            return CommandResult(0, False, 0.01, "")

    monkeypatch.setattr("mutator.cli.CommandRunner", lambda verbose=False: Runner())
    for _ in range(2):
        assert run(["--root", str(tmp_path), "--no-coverage", "Price.cs"]) == 4
        assert path.read_bytes() == original
        data = loads(snapshot_path(tmp_path, "Price").read_text(encoding="utf-8"))
        form = data["forms"][0]
        assert (form["killed"], form["survived"], form["invalid"], form["sites"]) == (0, 0, 1, 1)
        assert set(data["outcomes"].values()) == {"invalid"}
    assert len(builds) == 2
    captured = capsys.readouterr()
    assert "INVALID" in captured.out
    assert "no test kill was recorded" in captured.err


def test_invalid_diagnostic_output_retains_the_end_of_a_long_build_log(capsys):
    from mutator.workers import _invalid

    output = "obsolete first message\n" + "\n".join(f"build step {i}" for i in range(30)) + "\nlatest diagnostic"
    assert _invalid(output) == "invalid"
    captured = capsys.readouterr().err
    assert "latest diagnostic" in captured
    assert "build step 15" in captured
    assert "obsolete first message" not in captured

import json
import os

import pytest

from mutator.cli import _finish, _record
from mutator.edn import dumps, loads
from mutator.engine import mutate_file
from mutator.functions import mutation_id
from mutator.metrics import load_history, snapshot_path, write_results
from mutator.model import FormResult
from mutator.outcomes import classify_mutant
from mutator.report import format_results
from mutator.runner import CommandResult, CommandRunner, resolve_test_plan


def _ndjson(*events):
    return "\n".join(json.dumps(event) for event in events)


def _go_build(diagnostic, *, setup=False):
    failure = "setup failed" if setup else "build failed"
    return _ndjson(
        {"Action": "build-output", "ImportPath": "example", "Output": diagnostic},
        {"Action": "build-fail", "ImportPath": "example"},
        {"Action": "output", "Package": "example", "Output": f"FAIL\texample [{failure}]\n"},
        {"Action": "fail", "Package": "example", "FailedBuild": "example"},
    )


GO_TEST_FAILURE = _ndjson(
    {"Action": "run", "Package": "example", "Test": "TestAdd"},
    {"Action": "fail", "Package": "example", "Test": "TestAdd"},
    {"Action": "fail", "Package": "example"},
)


def _cargo_build(diagnostic, source="src/lib.rs"):
    return _ndjson(
        {"reason": "compiler-message", "message": {
            "level": "error", "message": diagnostic,
            "spans": [{"is_primary": True, "file_name": source}],
        }},
        {"reason": "build-finished", "success": False},
    )


def _maven_failure(plugin, target, message):
    return (
        f"[ERROR] Failed to execute goal org.apache.maven.plugins:{plugin}:3.14.1:{target} "
        f"(default-{target}) on project example: {message}"
    )


@pytest.mark.parametrize(
    ("language", "source", "output", "expected"),
    [
        ("java", "src/main/java/Example.java",
         "[ERROR] src/main/java/Example.java:[5,13] bad operand types for binary operator '-'\n"
         + _maven_failure("maven-compiler-plugin", "compile", "Compilation failure"),
         "invalid"),
        ("java", "src/main/java/Example.java",
         _maven_failure("maven-compiler-plugin", "compile", "Fatal error compiling: invalid target release: 99"),
         "error"),
        ("java", "src/main/java/Example.java",
         "[ERROR] Tests run: 1, Failures: 1, Errors: 0, Skipped: 0\n"
         + _maven_failure("maven-surefire-plugin", "test", "There are test failures."),
         "killed"),
        ("java", "src/main/java/Example.java",
         _maven_failure("maven-surefire-plugin", "test", "There was an error in the forked process"),
         "error"),
        ("go", "file.go", _go_build("# example\n./file.go:3:9: invalid operation: a - b\n"), "invalid"),
        ("go", "file.go", "# example\n./file.go:3:9: invalid operation: a - b\nFAIL\texample [build failed]", "invalid"),
        ("go", "file.go", _go_build("file.go:3:9: expected operand, found '*'\n", setup=True), "invalid"),
        ("go", "file.go", _go_build("file.go:3:9: import cycle not allowed\n", setup=True), "error"),
        ("go", "file.go", _go_build("no required module provides package example/dep\n", setup=True), "error"),
        ("go", "file.go", _go_build("# example\n# [example]\n./file.go:3:9: fmt.Printf call has no args\n"), "error"),
        ("go", "file.go", GO_TEST_FAILURE, "killed"),
        ("go", "file.go", "--- FAIL: TestAdd (0.00s)\nFAIL\texample\t0.001s", "killed"),
        ("go", "file.go", _ndjson({"Action": "fail", "Package": "example"}), "error"),
        ("go", "file.go", _go_build("could not create build directory\n") + "\n" + GO_TEST_FAILURE, "error"),
        ("rust", "src/lib.rs", _cargo_build("cannot subtract `&str` from `String`"), "invalid"),
        ("rust", "src/lib.rs", _cargo_build("couldn't read `fixture.txt`: No such file"), "error"),
        ("rust", "src/lib.rs", _cargo_build("could not compile dependency", "registry/src/other.rs"), "error"),
        ("rust", "src/lib.rs",
         "error[E0369]: cannot subtract\n --> src/lib.rs:4:9\nerror: could not compile `example`",
         "invalid"),
        ("rust", "src/lib.rs",
         '{"reason":"build-finished","success":true}\n'
         "test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured\n"
         "error: test failed, to rerun pass `--lib`",
         "killed"),
        ("rust", "src/lib.rs",
         "error: failed to run custom build command for `example`\n"
         '{"reason":"build-finished","success":false}',
         "error"),
        ("rust", "src/lib.rs", _ndjson(
            {"reason": "compiler-message", "message": {
                "level": "warning", "message": "unused variable",
                "spans": [{"is_primary": True, "file_name": "src/lib.rs"}],
            }},
            {"reason": "compiler-message", "message": {
                "level": "error", "message": "cannot compile dependency",
                "spans": [{"is_primary": False, "file_name": "src/lib.rs"}],
            }},
            {"reason": "build-finished", "success": False},
        ), "error"),
        ("csharp", "Example.cs",
         "\x1b[31mExample.cs(4,9): error CS0019: Operator '-' cannot be applied\x1b[0m",
         "invalid"),
        ("csharp", "Example.cs", "Example.cs(4,9): error CS2001: Source file not found", "error"),
        ("csharp", "Example.cs", "CSC : error CS0006: Metadata file could not be found", "error"),
        ("csharp", "Example.cs", "Failed!  - Failed: 1, Passed: 0, Skipped: 0, Total: 1", "killed"),
        ("csharp", "Example.cs", "Test summary: total: 5, failed: 2, succeeded: 3, skipped: 0", "killed"),
        ("csharp", "Example.cs", "Test run summary: Failed!\n  total: 1\n  failed: 1\n  succeeded: 0", "killed"),
        ("csharp", "Example.cs", "Test run summary: Failed!\n  error: 1\n  total: 1\n  failed: 0", "error"),
        ("csharp", "Example.cs", "Test Run Aborted.\nFailed!  - Failed: 1, Passed: 0, Total: 1", "error"),
        ("csharp", "Example.cs",
         "Example.csproj : error NU1301: Unable to load the service index\n"
         "Failed!  - Failed: 1, Passed: 0, Total: 1",
         "error"),
    ],
)
def test_compiled_outcomes_require_tool_evidence(tmp_path, language, source, output, expected):
    path = tmp_path / source
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    plan = resolve_test_plan(tmp_path, path, language, None)
    result = CommandResult(1, False, 0.1, output)
    assert classify_mutant(plan, result, tmp_path, path) == expected


def test_diagnostic_can_refer_to_a_different_project_file(tmp_path):
    source = tmp_path / "Source.cs"
    source.touch()
    test_source = tmp_path / "tests" / "ExampleTests.cs"
    test_source.parent.mkdir()
    test_source.touch()
    plan = resolve_test_plan(tmp_path, source, "csharp", None)
    result = CommandResult(1, False, 0.1, f"{test_source}(4,9): error CS1501: No overload for method")
    assert classify_mutant(plan, result, tmp_path, source) == "invalid"


def test_windows_maven_compiler_diagnostic_is_a_drive_absolute_path(tmp_path):
    if not tmp_path.drive:
        pytest.skip("Maven's Windows drive path")
    root = tmp_path / "worker-0"
    source = root / "src" / "main" / "java" / "中文" / "Example.java"
    source.parent.mkdir(parents=True)
    source.touch()
    plan = resolve_test_plan(root, source, "java", None)
    output = (
        f"[ERROR] COMPILATION ERROR : \n[ERROR] /{source.as_posix()}:[3,62] 运算符的操作数类型不正确\n"
        + _maven_failure("maven-compiler-plugin", "compile", "Compilation failure")
    )
    assert classify_mutant(plan, CommandResult(1, False, 0.1, output), root, source) == "invalid"


def test_tool_timeout_and_spawn_failure_are_not_compiled_mutation_outcomes(tmp_path):
    source = tmp_path / "Example.cs"
    plan = resolve_test_plan(tmp_path, source, "csharp", None)
    output = "Failed!  - Failed: 1, Passed: 0, Skipped: 0, Total: 1"
    assert classify_mutant(plan, CommandResult(124, True, 1, output), tmp_path, source) == "error"
    assert classify_mutant(plan, CommandResult(127, False, 0.1, output, True), tmp_path, source) == "error"


def test_dotnet_child_diagnostics_use_stable_ui_language(tmp_path, monkeypatch):
    monkeypatch.setenv("DOTNET_CLI_UI_LANGUAGE", "zh-CN")

    def spawn(command, cwd, environment):
        assert environment["DOTNET_CLI_UI_LANGUAGE"] == "en-US"
        raise FileNotFoundError("test process not available")

    monkeypatch.setattr("mutator.runner._spawn", spawn)
    assert CommandRunner().run(["dotnet", "test"], tmp_path, None).spawn_failed
    assert os.environ["DOTNET_CLI_UI_LANGUAGE"] == "zh-CN"


def test_typescript_leaves_test_command_to_the_project(tmp_path):
    source = tmp_path / "index.ts"
    plan = resolve_test_plan(tmp_path, source, "typescript", None)
    assert plan.command == ["npm", "test"]
    override = resolve_test_plan(tmp_path, source, "typescript", "npm run check")
    assert override.command == "npm run check"
    result = CommandResult(1, False, 0.1, "Error in the project test command")
    assert classify_mutant(override, result, tmp_path, source) == "killed"


class _GoRunner:
    verbose = False

    def __init__(self, mutant_output):
        self.mutant_output = mutant_output
        self.mutants = 0

    def run(self, command, cwd, timeout):
        assert command == ["go", "test", "-json", "-count=1", "."]
        if "a + b" in (cwd / "file.go").read_text(encoding="utf-8"):
            return CommandResult(0, False, 0.1, "")
        self.mutants += 1
        return CommandResult(1, False, 0.1, self.mutant_output)


def _mutate_go(root, runner):
    source = root / "file.go"
    source.write_text("package example\nfunc Add(a, b int) int { return a + b }\n", encoding="utf-8")
    (root / "go.mod").write_text("module example\n", encoding="utf-8")
    return mutate_file(
        source, root, runner=runner, covered_lines=None, ignore_coverage=True,
        mutate_all=False, lines={2}, test_command=None, timeout_factor=10,
        mutation_warning=50, baselines={}, max_workers=1,
    )


def test_invalid_does_not_count_as_a_kill_and_execution_failure_preserves_snapshot(tmp_path, capsys):
    runner = _GoRunner(_go_build("# example\nfile.go:2:9: invalid operation\n"))
    invalid = _mutate_go(tmp_path, runner)
    assert runner.mutants == 1
    form = invalid.forms[0]
    assert (form.killed, form.survived, form.invalid, form.score) == (0, 0, 1, None)
    assert _record(invalid, [], []) == "invalid"
    assert "INVALID" in capsys.readouterr().out
    assert "Invalid" in format_results(invalid.forms)
    assert _finish(False, True, True) == 4
    snapshot = snapshot_path(tmp_path, form.namespace)
    written = snapshot.read_bytes()
    assert loads(written.decode("utf-8"))["forms"][0]["invalid"] == 1

    unexpected = _GoRunner("go: could not create temporary build directory")
    execution_error = _mutate_go(tmp_path, unexpected)
    assert unexpected.mutants == 1
    assert "could not create temporary build directory" in execution_error.execution_error
    assert not execution_error.baseline_failed
    assert _record(execution_error, [], []) == "error"
    assert _finish(True, True, True) == 2
    assert execution_error.written == []
    assert snapshot.read_bytes() == written


def test_legacy_kill_is_retested(tmp_path):
    first = _mutate_go(tmp_path, _GoRunner(GO_TEST_FAILURE))
    assert first.forms[0].killed == 1
    snapshot = snapshot_path(tmp_path, first.forms[0].namespace)
    old = loads(snapshot.read_text(encoding="utf-8"))
    old.pop("outcome-policy")
    snapshot.write_text(dumps(old), encoding="utf-8")

    runner = _GoRunner(GO_TEST_FAILURE)
    refreshed = _mutate_go(tmp_path, runner)
    assert runner.mutants == 1
    assert refreshed.forms[0].killed == 1
    assert loads(snapshot.read_text(encoding="utf-8"))["outcome-policy"] == 1


def test_merging_namespace_does_not_recertify_another_files_legacy_kill(tmp_path):
    old_form = FormResult("defn/Old", "example", "Old", False, "old.go", 1, 2, "digest", killed=1)
    old_mutation = mutation_id("old.go", "example", old_form.id, 3, 4, "+", "-")
    [path] = write_results(tmp_path, "old.go", [old_form], {old_mutation: "killed"})
    old = loads(path.read_text(encoding="utf-8"))
    old.pop("outcome-policy")
    path.write_text(dumps(old), encoding="utf-8")

    new_form = FormResult("defn/New", "example", "New", False, "new.go", 1, 2, "digest", killed=1)
    new_mutation = mutation_id("new.go", "example", new_form.id, 3, 4, "+", "-")
    assert write_results(tmp_path, "new.go", [new_form], {new_mutation: "killed"}) == [path]
    assert load_history(tmp_path, "old.go").forms[("example", old_form.id)].digest == "digest"
    assert load_history(tmp_path, "old.go").outcomes == {}
    assert load_history(tmp_path, "new.go").outcomes == {new_mutation: "killed"}

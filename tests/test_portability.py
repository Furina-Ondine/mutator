import errno
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from mutator.model import Site
from mutator.runner import CommandResult, CommandRunner, _python_command, display_command
from mutator.runner import TestPlan as Plan
from mutator.workers import create_workers, delete_tree, new_run_dir, run_mutants, symlink


def test_workers_fall_back_to_independent_copies_without_symlink_privilege(tmp_path, monkeypatch):
    source = tmp_path / "src" / "demo.py"
    source.parent.mkdir()
    source.write_bytes(b"def add(a, b):\n    return a + b\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "data.txt").write_text("original", encoding="utf-8")
    for skipped in (".git", "target", ".venv", "__pycache__"):
        directory = tests / skipped
        directory.mkdir()
        (directory / "excluded.txt").write_text("skip", encoding="utf-8")

    def denied(*args, **kwargs):
        raise OSError(errno.EPERM, "symlink privilege is unavailable")

    monkeypatch.setattr(Path, "symlink_to", denied)
    base = new_run_dir(tmp_path)
    try:
        workers = create_workers(base, tmp_path, "src/demo.py", source.read_bytes(), 2)
        for worker in workers:
            assert (worker / "tests" / "data.txt").read_text(encoding="utf-8") == "original"
            assert not (worker / "tests").is_symlink()
            for skipped in (".git", "target", ".venv", "__pycache__"):
                assert not (worker / "tests" / skipped).exists()
        (workers[0] / "tests" / "data.txt").write_text("worker", encoding="utf-8")
        assert (workers[1] / "tests" / "data.txt").read_text(encoding="utf-8") == "original"
        assert (tests / "data.txt").read_text(encoding="utf-8") == "original"
    finally:
        delete_tree(base)
    assert source.read_bytes() == b"def add(a, b):\n    return a + b\n"
    assert not base.exists()


def test_native_virtualenv_layout_is_selected(tmp_path):
    scripts = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    interpreter = tmp_path / ".venv" / scripts / executable
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"placeholder")
    interpreter.chmod(0o755)
    assert _python_command(tmp_path)[0] == str(interpreter.absolute())


def test_timeout_terminates_descendants_before_worker_cleanup(tmp_path):
    marker = tmp_path / "late-marker"
    script = tmp_path / "parent.py"
    child = "import pathlib, time; time.sleep(1.5); pathlib.Path('late-marker').write_text('alive')"
    script.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        "print('child started', flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    result = CommandRunner().run([sys.executable, str(script)], tmp_path, 0.5)
    assert result.timed_out
    assert result.code == 124
    assert "child started" in result.output
    assert time.monotonic() - started < 10
    time.sleep(1.5)
    assert not marker.exists()


def test_argument_vectors_preserve_spaces_metacharacters_and_unicode(tmp_path):
    arguments = ["a b", "a&b", "quoted\"value", "中文"]
    result = CommandRunner().run(
        [sys.executable, "-c", "import json, sys; print(json.dumps(sys.argv[1:], ensure_ascii=False))", *arguments],
        tmp_path,
        5,
    )
    assert result.code == 0
    assert json.loads(result.output) == arguments


def test_timeout_terminates_a_child_even_after_its_parent_exits(tmp_path):
    child = "import pathlib, time; time.sleep(1.5); pathlib.Path('late-marker').write_text('alive')"
    command = [sys.executable, "-c", f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {child!r}])"]
    result = CommandRunner().run(command, tmp_path, 0.3)
    assert result.timed_out
    assert result.code == 124
    time.sleep(1.5)
    assert not (tmp_path / "late-marker").exists()


def test_mutations_preserve_crlf_and_utf8_bytes(tmp_path):
    original = "def add(a, b):\r\n    # 中文\r\n    return a + b\r\n".encode("utf-8")
    source = tmp_path / "demo.py"
    source.write_bytes(original)
    offset = original.index(b"+")
    site = Site("demo.py", "demo", "defn/add", "add", 3, offset, offset + 1, "+", "-", "arithmetic", "one")

    class CheckBytes:
        verbose = False

        def run(self, command, cwd, timeout):
            actual = (cwd / "demo.py").read_bytes()
            if timeout is None:
                assert actual == original
                return CommandResult(0, False, 0.01, "")
            assert actual == original.replace(b"+", b"-")
            return CommandResult(1, False, 0.01, "")

    outcomes = {}
    plan = Plan(["unused"], tmp_path, "exit-code", False)
    assert run_mutants(tmp_path, source, original, [site], 1, CheckBytes(), plan, 5, "demo.py", outcomes) is None
    assert outcomes == {"one": "killed"}
    assert source.read_bytes() == original


def test_copied_read_only_dependencies_can_be_cleaned_up(tmp_path, monkeypatch):
    source = tmp_path / "demo.py"
    source.write_bytes(b"def f():\n    return 1\n")
    dependency = tmp_path / "read-only.txt"
    dependency.write_bytes(b"keep")
    dependency.chmod(stat.S_IREAD)

    def denied(*args, **kwargs):
        raise OSError(errno.EPERM, "no links")

    monkeypatch.setattr(Path, "symlink_to", denied)
    base = new_run_dir(tmp_path)
    try:
        worker = create_workers(base, tmp_path, "demo.py", source.read_bytes(), 1)[0]
        assert (worker / "read-only.txt").read_bytes() == b"keep"
        delete_tree(base)
        assert dependency.read_bytes() == b"keep"
        assert not base.exists()
    finally:
        dependency.chmod(stat.S_IREAD | stat.S_IWRITE)


@pytest.mark.skipif(os.name != "nt", reason="Windows batch entry points")
def test_windows_batch_entry_points_preserve_argument_boundaries(tmp_path, monkeypatch):
    directory = tmp_path / "tools with spaces"
    directory.mkdir()
    capture = directory / "capture.py"
    capture.write_text("import json, sys; print(json.dumps(sys.argv[1:], ensure_ascii=False))", encoding="utf-8")
    shim = directory / "capture.cmd"
    shim.write_text(f'@"{sys.executable}" "{capture}" %*\n', encoding="utf-8")
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ["PATH"])
    arguments = ["a b", "x&echo>pwned", "%PATH%", "bang!name", "a^b", 'embedded"quote', 'x"&echo hacked>pwned&"', 'backslash\\"quote', "C:\\trailing\\", "中文"]
    result = CommandRunner().run(["capture", *arguments], tmp_path, 5)
    assert result.code == 0, result.output
    assert json.loads(result.output) == arguments
    assert not (tmp_path / "pwned").exists()


def test_cli_emits_utf8_under_a_legacy_stdio_encoding(tmp_path):
    import subprocess

    directory = tmp_path / "中文 😀"
    directory.mkdir()
    source = directory / "demo.py"
    source.write_text("def f():\n    return 1\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "mutator", "--scan", "--no-coverage", "--root", str(tmp_path), str(source)],
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "gbk:strict"},
    )
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert "中文 😀/demo.py" in result.stdout.decode("utf-8")


@pytest.mark.skipif(os.name != "nt", reason="Windows job setup")
def test_failed_job_attachment_reaps_the_suspended_process(tmp_path, monkeypatch):
    import subprocess
    from mutator.windows_job import WindowsJob

    processes = []
    jobs = []
    real_popen = subprocess.Popen

    def record_process(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    def fail_attachment(self, pid):
        jobs.append(self)
        raise OSError("attachment failed")

    monkeypatch.setattr("mutator.runner.subprocess.Popen", record_process)
    monkeypatch.setattr(WindowsJob, "attach", fail_attachment)
    result = CommandRunner().run(
        [sys.executable, "-c", "from pathlib import Path; Path('must-not-run').touch()"], tmp_path, 5,
    )
    assert result.code == 127
    assert "attachment failed" in result.output
    assert processes[0].returncode is not None
    assert processes[0].wait(timeout=1) is not None
    assert jobs[0].handle is None
    assert not (tmp_path / "must-not-run").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows job setup")
def test_failure_after_job_attachment_terminates_descendants(tmp_path, monkeypatch):
    from mutator.windows_job import WindowsJob

    original_attach = WindowsJob.attach

    def attach_then_fail(job, pid):
        original_attach(job, pid)
        deadline = time.monotonic() + 5
        while not (tmp_path / "started").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise OSError("failure after resuming")

    monkeypatch.setattr(WindowsJob, "attach", attach_then_fail)
    child = "from pathlib import Path; import time; Path('started').touch(); time.sleep(1.5); Path('escaped').touch()"
    parent = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)"
    result = CommandRunner().run([sys.executable, "-c", parent], tmp_path, 5)
    assert (tmp_path / "started").exists()
    assert result.code == 127
    assert "failure after resuming" in result.output
    time.sleep(1.5)
    assert not (tmp_path / "escaped").exists()


def test_overlay_entry_replaces_an_existing_file(tmp_path):
    source = tmp_path / "source.txt"
    source.write_bytes(b"project content")
    destination = tmp_path / "overlay" / "source.txt"
    destination.parent.mkdir()
    destination.write_bytes(b"stale worker content")
    symlink(destination, source)
    assert destination.read_bytes() == b"project content"
    assert source.read_bytes() == b"project content"


def test_overlay_does_not_hide_non_permission_io_errors(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_bytes(b"project content")
    destination = tmp_path / "overlay" / "source.txt"
    failure = OSError(errno.EIO, "storage unavailable")

    def fail_link(*args, **kwargs):
        raise failure

    monkeypatch.setattr(Path, "symlink_to", fail_link)
    with pytest.raises(OSError) as error:
        symlink(destination, source)
    assert error.value is failure
    assert not destination.exists()
    assert source.read_bytes() == b"project content"


@pytest.mark.skipif(os.name != "nt", reason="Windows job setup")
def test_job_construction_failure_does_not_start_a_process(tmp_path, monkeypatch):
    from mutator.windows_job import WindowsJob

    def fail_construction(self):
        raise OSError("cannot create job")

    def unexpected_start(*args, **kwargs):
        pytest.fail("A test process started without a job")

    monkeypatch.setattr(WindowsJob, "__init__", fail_construction)
    monkeypatch.setattr("mutator.runner.subprocess.Popen", unexpected_start)
    result = CommandRunner().run([sys.executable, "-c", "pass"], tmp_path, 5)
    assert result.code == 127
    assert "cannot create job" in result.output
    assert not result.timed_out


def test_main_reconfigures_legacy_text_streams_for_utf8(monkeypatch):
    import io
    from mutator import cli

    output = io.BytesIO()
    errors = io.BytesIO()
    stdout = io.TextIOWrapper(output, encoding="gbk", errors="strict", newline="\n")
    stderr = io.TextIOWrapper(errors, encoding="gbk", errors="strict", newline="\n")
    help_text = cli.HELP + "\n中文 😀\n"
    monkeypatch.setattr(cli, "HELP", help_text)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    stdout.flush()
    stderr.write("中文 😀")
    stderr.flush()
    assert exit_info.value.code == 0
    assert output.getvalue().decode("utf-8") == help_text
    assert errors.getvalue().decode("utf-8") == "中文 😀"


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell command overrides")
def test_windows_command_overrides_use_powershell_and_preserve_exit_codes(tmp_path):
    result = CommandRunner().run("$value = 2 + 3; Write-Output ('ps:' + $value); exit 7", tmp_path, 5)
    assert result.code == 7
    assert result.output.strip() == "ps:5"
    native = CommandRunner().run(display_command([sys.executable, "-c", "raise SystemExit(37)"]), tmp_path, 5)
    assert native.code == 37, native.output


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell command overrides")
def test_powershell_override_timeout_terminates_orphaned_descendants(tmp_path):
    child = "from pathlib import Path; import time; time.sleep(1.5); Path('escaped').touch()"
    parent = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {child!r}])"
    result = CommandRunner().run(display_command([sys.executable, "-c", parent]), tmp_path, 1)
    assert result.timed_out
    assert result.code == 124
    time.sleep(1.5)
    assert not (tmp_path / "escaped").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell launcher")
def test_powershell_launcher_scans_a_unicode_project(tmp_path):
    import subprocess

    project = tmp_path / "中文 项目 🧪"
    project.mkdir()
    (project / "demo.clj").write_text("(ns demo)\n(defn pick [x] (if x 1 0))\n", encoding="utf-8")
    launcher = Path(__file__).resolve().parents[1] / "mutator.ps1"
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(launcher), "--scan", "--root", str(project)],
        cwd=tmp_path, capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert "demo.clj" in result.stdout.decode("utf-8")
    assert not (project / ".metrics").exists()
    invalid = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(launcher), "--max-workers", "invalid"],
        cwd=tmp_path, capture_output=True,
    )
    assert invalid.returncode == 1
    assert "--max-workers" in invalid.stderr.decode("utf-8")


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell launcher")
def test_powershell_launcher_preserves_arguments_directory_and_exit_code(tmp_path):
    import subprocess

    (tmp_path / "mutator.py").write_text(
        "import json, os, sys\n"
        "print(json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd()}))\n"
        "raise SystemExit(37)\n", encoding="utf-8",
    )
    arguments = ["", 'quoted"value', "a&b", "%PATH%", "$HOME", "`tick", "中文 🧪", "C:\\trailing\\"]
    launcher = Path(__file__).resolve().parents[1] / "mutator.ps1"
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(launcher), *arguments],
        cwd=tmp_path, capture_output=True,
    )
    assert result.returncode == 37, result.stderr.decode("utf-8", errors="replace")
    assert json.loads(result.stdout.decode("utf-8")) == {"args": arguments, "cwd": str(tmp_path)}

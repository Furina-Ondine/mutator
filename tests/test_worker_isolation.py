import os
import subprocess
import sys
import time
from threading import Barrier, Thread

import pytest

from mutator.model import Site
from mutator.runner import CommandResult, TestPlan as Plan
from mutator.workers import delete_tree, run_mutants


def project(tmp_path):
    root = tmp_path / "original"
    source = root / "src" / "calc.go"
    source.parent.mkdir(parents=True)
    original = b"a + b + c\n"
    source.write_bytes(original)
    sites = [
        Site("src/calc.go", "calc", "f", "f", 1, i, i + 1, "+", "-", "arithmetic", str(i))
        for i in (2, 6)
    ]
    return root, source, original, sites


def test_copied_workers_validate_the_original_command_and_keep_build_outputs_private(
    tmp_path, monkeypatch,
):
    root, source, original, sites = project(tmp_path)
    for relative in ("build/input.txt", "target/fixtures/data.txt", "src/bin/config.txt"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    output = "build/current.txt"
    (root / output).write_bytes(b"original output")
    command = ["test-command", "--project=."]
    plan = Plan(command, root / "src", "go", True)
    monkeypatch.setattr("mutator.workers.os.cpu_count", lambda: 2)
    baselines = set()
    mutants = set()
    both_mutants = Barrier(2, timeout=5)

    class Runner:
        verbose = False

        def run(self, command_used, cwd, timeout):
            worker = cwd.parent
            assert cwd == worker / "src"
            assert command_used == command
            assert not worker.is_relative_to(root)
            for relative in ("build/input.txt", "target/fixtures/data.txt", "src/bin/config.txt"):
                assert (worker / relative).read_bytes() == b"fixture"
            content = (cwd / source.name).read_bytes()
            if content == original:
                assert (worker / output).read_bytes() == b"original output"
                (worker / output).write_bytes(worker.name.encode())
                baselines.add(worker)
            else:
                assert len(baselines) == 2
                assert (worker / output).read_bytes() == worker.name.encode()
                (worker / output).write_bytes(content)
                mutants.add(worker)
                both_mutants.wait()
                assert (worker / output).read_bytes() == content
            return CommandResult(0, False, 0.01, "")

    outcomes = {}
    failure = run_mutants(root, source, original, sites, 2, Runner(), plan, 5, "src/calc.go", outcomes)
    assert failure is None
    assert baselines == mutants
    assert len(mutants) == 2
    assert not any(worker.exists() for worker in mutants)
    assert outcomes == {"2": "survived", "6": "survived"}
    assert (root / output).read_bytes() == b"original output"
    assert source.read_bytes() == original


def test_worker_baseline_failure_starts_no_mutants(tmp_path, monkeypatch):
    root, source, original, sites = project(tmp_path)
    monkeypatch.setattr("mutator.workers.os.cpu_count", lambda: 2)
    plan = Plan(["test-command"], root, "go", True)
    calls = []

    class Runner:
        verbose = False

        def run(self, command, cwd, timeout):
            calls.append((cwd, (cwd / "src" / source.name).read_bytes()))
            if cwd.name == "worker-1":
                return CommandResult(1, False, 0.01, "dependency setup failed")
            return CommandResult(0, False, 0.01, "")

    outcomes = {"old": "survived"}
    failure = run_mutants(root, source, original, sites, 2, Runner(), plan, 5, "src/calc.go", outcomes)
    assert [content for _, content in calls] == [original, original]
    assert not any(worker.exists() for worker, _ in calls)
    assert failure.stage == "worker-baseline"
    assert "dependency setup failed" in failure.message
    assert outcomes == {"old": "survived"}
    assert source.read_bytes() == original


def test_failed_mutant_stops_work_and_keeps_only_previous_outcomes(tmp_path):
    root, source, original, sites = project(tmp_path)
    sites.append(Site("src/calc.go", "calc", "f", "f", 1, 2, 3, "+", "*", "arithmetic", "later"))
    plan = Plan(["test-command"], root, "go", True)
    calls = []

    class Runner:
        verbose = False

        def run(self, command, cwd, timeout):
            content = (cwd / "src" / source.name).read_bytes()
            calls.append(content)
            if content == b"a + b - c\n":
                return CommandResult(1, False, 0.01, "could not start test command", spawn_failed=True)
            return CommandResult(0, False, 0.01, "")

    outcomes = {"old": "killed"}
    failure = run_mutants(root, source, original, sites, 1, Runner(), plan, 5, "src/calc.go", outcomes)
    assert calls == [original, b"a - b + c\n", b"a + b - c\n"]
    assert failure.stage == "mutant"
    assert "src/calc.go:1" in failure.message and "could not start test command" in failure.message
    assert outcomes == {"old": "killed"}
    assert source.read_bytes() == original


@pytest.mark.skipif(os.name != "nt", reason="Windows locks a live process's working directory")
def test_worker_cleanup_waits_for_a_released_working_directory(tmp_path):
    worker = tmp_path / "worker"
    worker.mkdir()
    child = subprocess.Popen(
        [sys.executable, "-c", 'import time; print("ready", flush=True); time.sleep(15)'],
        cwd=worker, stdout=subprocess.PIPE, text=True,
    )

    def release():
        time.sleep(0.05)
        child.terminate()

    thread = Thread(target=release)
    try:
        assert child.stdout.readline().strip() == "ready"
        thread.start()
        delete_tree(worker)
        assert not worker.exists()
    finally:
        if thread.ident is not None:
            thread.join(timeout=5)
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        child.stdout.close()


@pytest.mark.parametrize("baseline_ok", [True, False])
def test_worker_cleanup_failure_preserves_outcomes_and_explains_both_errors(
    tmp_path, monkeypatch, baseline_ok,
):
    root, source, original, sites = project(tmp_path)
    plan = Plan(["test-command"], root, "go", True)

    def cleanup_failed(path):
        delete_tree(path)
        raise PermissionError("worker cleanup denied")

    class Runner:
        verbose = False

        def run(self, command, cwd, timeout):
            return CommandResult(
                0 if baseline_ok else 1, False, 0.01, "worker baseline dependency unavailable",
            )

    monkeypatch.setattr("mutator.workers.delete_tree", cleanup_failed)
    outcomes = {"previous": "killed"}
    failure = run_mutants(root, source, original, sites, 1, Runner(), plan, 5, "src/calc.go", outcomes)
    assert failure.stage == ("mutant" if baseline_ok else "worker-baseline")
    assert "worker cleanup denied" in failure.message
    assert ("worker baseline dependency unavailable" in failure.message) == (not baseline_ok)
    assert outcomes == {"previous": "killed"}
    assert source.read_bytes() == original

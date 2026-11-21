"""Run mutants in verified worker directories.

Overlay workers link project files when the platform permits it. Isolated
workers copy the project to a temporary directory outside the original tree.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

from mutator.model import MutationRunFailure, Site
from mutator.outcomes import classify_mutant
from mutator.runner import CommandResult, TestPlan
from mutator.sites import apply_site

# Build output and the worker tree itself must not be shared. A link to
# ``target`` would point a worker at the directory that contains the worker.
SKIP_LINK = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".venv",
        "venv",
        "target",
        "dist",
        "build",
        "out",
        "coverage",
        ".metrics",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".clj-kondo",
        ".gradle",
    }
)

CONFIGS = (
    "deps.edn",
    "bb.edn",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "go.mod",
    "go.sum",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "tsconfig.json",
    "Cargo.toml",
    "Cargo.lock",
    "pyproject.toml",
    "pytest.ini",
    "setup.cfg",
    "setup.py",
    "conftest.py",
    "requirements.txt",
    "Pipfile",
    "poetry.lock",
)


def worker_count(site_count: int, requested: int | None) -> int:
    """The smaller of the sites, the cores, and an explicit --max-workers."""

    processors = os.cpu_count() or 1
    limit = processors if requested is None else requested
    return max(1, min(site_count, processors, limit))


def new_run_dir(root: Path, *, isolated: bool = False) -> Path:
    if isolated:
        parent = Path(tempfile.gettempdir()).resolve() / "mutation-workers"
        if parent.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Temporary worker directory is inside the original project: {parent}")
        parent.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="run-", dir=parent))
    directory = root / "target" / "mutation-workers" / f"run-{uuid.uuid4()}"
    directory.mkdir(parents=True)
    return directory


def symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    try:
        link.symlink_to(target.resolve(), target_is_directory=target.is_dir())
    except OSError as exc:
        unsupported = exc.errno in {errno.EPERM, errno.EACCES, errno.ENOSYS, errno.ENOTSUP}
        if not unsupported and getattr(exc, "winerror", None) != 1314:
            raise
        _copy_tree(target, link, frozenset())


def _filesystem_path(path: Path) -> Path:
    """Use Windows extended paths for filesystem operations."""

    if os.name != "nt":
        return path
    absolute = str(path.absolute())
    if absolute.startswith("\\\\?\\"):
        return Path(absolute)
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute[2:])
    return Path("\\\\?\\" + absolute)


def _copy_tree(
    source: Path,
    destination: Path,
    ancestors: frozenset[Path],
    excluded_names: frozenset[str] = SKIP_LINK,
    omitted: Path | None = None,
) -> None:
    source = _filesystem_path(source)
    destination = _filesystem_path(destination)
    if source == omitted:
        return
    if not source.is_dir():
        shutil.copy2(source, destination)
        return
    resolved = source.resolve()
    if resolved in ancestors:
        return
    destination.mkdir(parents=True, exist_ok=True)
    ancestors = ancestors | {resolved}
    for child in source.iterdir():
        if child.name not in excluded_names:
            _copy_tree(child, destination / child.name, ancestors, excluded_names, omitted)


def _remove_directory(path: Path) -> None:
    # Windows can briefly retain a child's working directory after killing it.
    deadline = time.monotonic() + 2
    while True:
        try:
            path.rmdir()
            return
        except OSError as error:
            if getattr(error, "winerror", None) != 32 or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def delete_tree(path: Path) -> None:
    """Remove a worker tree without following a link into the real project."""

    path = _filesystem_path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        path.unlink()
        return
    if os.name == "nt" and getattr(info, "st_reparse_tag", None) == stat.IO_REPARSE_TAG_MOUNT_POINT:
        _remove_directory(path)
        return
    if stat.S_ISDIR(info.st_mode):
        for child in path.iterdir():
            delete_tree(child)
        _remove_directory(path)
        return
    try:
        path.unlink()
    except PermissionError:
        path.chmod(path.stat().st_mode | stat.S_IWUSR)
        path.unlink()


def _copy_configs(worker: Path, root: Path, relative: str) -> None:
    for name in CONFIGS:
        source = root / name
        if not source.is_file():
            continue
        if name == relative:
            continue
        (worker / name).write_bytes(source.read_bytes())


def _link_children(worker_dir: Path, real_dir: Path, skip: set[str]) -> None:
    if not real_dir.is_dir():
        return
    for child in real_dir.iterdir():
        if child.name in skip or child.name in SKIP_LINK:
            continue
        destination = worker_dir / child.name
        if destination.exists() or destination.is_symlink():
            continue
        symlink(destination, child)


# Node, and Vite in front of it, resolve a relative import from the real path
# of the module. A symlinked test therefore loads the original source.
_SPECIFIER = re.compile(
    r"""(?:from|import|require)\s*\(?\s*['"](\.[^'"]+)['"]""",
)
_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")


def _specifiers(text: str) -> list[str]:
    found = []
    for match in _SPECIFIER.finditer(text):
        specifier = match.group(1).split("?", 1)[0].split("#", 1)[0]
        if specifier.startswith("."):
            found.append(specifier)
    return found


def _stem(base: Path) -> Path:
    if base.suffix in _EXTENSIONS:
        return Path(str(base)[: -len(base.suffix)])
    return base


def _candidates(directory: Path, specifier: str) -> list[Path]:
    base = directory / specifier
    stem = _stem(base)
    names = [base]
    for ext in _EXTENSIONS:
        names.append(Path(str(stem) + ext))
    for ext in _EXTENSIONS:
        names.append(stem / f"index{ext}")
    return names


def _inside(root: Path, path: Path) -> Path | None:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return root / relative


def _imported_files(importer: Path, root: Path) -> set[Path]:
    try:
        text = importer.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return set()
    found = set()
    for specifier in _specifiers(text):
        for candidate in _candidates(importer.parent, specifier):
            local = _inside(root, candidate)
            if local is not None and local.is_file():
                found.add(local.resolve())
    return found


def _keep_dir(name: str) -> bool:
    return name not in SKIP_LINK and name != "node_modules"


def _source_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if _keep_dir(name)]
        for name in filenames:
            if Path(name).suffix in _EXTENSIONS:
                yield Path(dirpath) / name


def importers_of(root: Path, relative: str) -> set[str]:
    """Modules that reach ``relative`` through a relative import.

    Those modules have to be real files in the worker. A symlink would send
    Node back to the original tree.
    """

    target = (root / relative).resolve()
    reverse: dict[Path, set[Path]] = {}
    for source in _source_files(root):
        for imported in _imported_files(source, root):
            reverse.setdefault(imported, set()).add(source.resolve())
    found: set[Path] = set()
    pending = [target]
    while pending:
        current = pending.pop()
        for importer in reverse.get(current, ()):
            if importer in found or importer == target:
                continue
            found.add(importer)
            pending.append(importer)
    root_resolved = root.resolve()
    return {path.relative_to(root_resolved).as_posix() for path in found}


def _expand_directory(link: Path, real_dir: Path) -> None:
    """Replace a linked directory so a copied module is not written through it."""

    if not real_dir.is_dir():
        return
    link.unlink()
    link.mkdir()
    for child in real_dir.iterdir():
        if child.name in SKIP_LINK:
            continue
        symlink(link / child.name, child)


def _copy_importer(worker: Path, root: Path, relative: str) -> None:
    real = root
    current = worker
    for segment in relative.split("/")[:-1]:
        real = real / segment
        current = current / segment
        if current.is_symlink():
            _expand_directory(current, real)
        elif not current.exists():
            current.mkdir(parents=True)
    destination = worker / relative
    if destination.is_symlink():
        destination.unlink()
    elif destination.exists():
        return
    destination.write_bytes((root / relative).read_bytes())


def _overlay(worker: Path, root: Path, relative: str, original: bytes) -> None:
    """Real directories along the source path. Every sibling is a link."""

    segments = tuple(relative.split("/"))
    for index, _segment in enumerate(segments[:-1]):
        rel_dir = Path(*segments[: index + 1])
        _link_children(worker / rel_dir, root / rel_dir, {segments[index + 1]})
    destination = worker.joinpath(*segments)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(original)


def create_workers(
    base: Path, root: Path, relative: str, original: bytes, count: int, *, isolated: bool = False,
) -> list[Path]:
    if isolated:
        omitted = _filesystem_path(root / "target" / "mutation-workers")
        created = []
        for index in range(count):
            worker = base / f"worker-{index}"
            _copy_tree(root, worker, frozenset(), frozenset(), omitted)
            target = _filesystem_path(worker / relative)
            target.chmod(target.stat().st_mode | stat.S_IWUSR)
            target.write_bytes(original)
            created.append(worker)
        return created
    created = []
    first = relative.split("/", 1)[0]
    importers = importers_of(root, relative)
    for index in range(count):
        worker = base / f"worker-{index}"
        worker.mkdir(parents=True, exist_ok=True)
        _copy_configs(worker, root, relative)
        _link_children(worker, root, {first})
        _overlay(worker, root, relative, original)
        for importer in importers:
            _copy_importer(worker, root, importer)
        created.append(worker)
    return created


def mapped_cwd(worker: Path, root: Path, cwd: Path) -> Path:
    relative = cwd.resolve().relative_to(root.resolve())
    mapped = (worker / relative).resolve()
    if not mapped.is_relative_to(worker.resolve()):
        raise ValueError(f"The command would run outside worker {worker}: {mapped}")
    return mapped


def _execute(
    directory: Path,
    site: Site,
    cwd: Path,
    relative: str,
    original: bytes,
    runner,
    plan: TestPlan,
    timeout: float,
    file_key: str,
) -> str | MutationRunFailure:
    label = f"{file_key}:{site.line} {site.description}"
    if original[site.start : site.end] != site.original.encode("utf-8"):
        return MutationRunFailure("mutant", f"Cannot apply {label}: source bytes moved.")
    mutant_path = directory / relative
    destination = _filesystem_path(mutant_path)
    mutated = apply_site(original.decode("utf-8"), site.start, site.end, site.mutant)
    try:
        destination.write_bytes(mutated.encode("utf-8"))
        result = runner.run(plan.command, cwd, timeout)
    finally:
        destination.write_bytes(original)
    status = classify_mutant(plan, result, cwd, mutant_path, worker_root=directory)
    if status == "error":
        return MutationRunFailure(
            "mutant", _command_failure(f"Cannot classify mutant {label}", cwd, result)
        )
    return status


def _command_failure(label: str, cwd: Path, result: CommandResult) -> str:
    reason = f"exit code {result.code}"
    if result.spawn_failed:
        reason = "could not start command"
    elif result.timed_out:
        reason = "timed out"
    detail = "\n".join(result.output.splitlines()[-20:])
    message = f"{label} in {cwd}: {reason}"
    return f"{message}\n{detail}" if detail else message


def _run_all(
    workers: list[tuple[Path, Path]],
    sites: list[Site],
    outcomes: dict[str, str],
    relative: str,
    original: bytes,
    runner,
    plan: TestPlan,
    timeout: float,
    file_key: str,
) -> MutationRunFailure | None:
    pending = iter(sites)
    lock = Lock()
    failure = None

    def drain(worker: tuple[Path, Path]) -> None:
        nonlocal failure
        directory, cwd = worker
        while True:
            with lock:
                if failure is not None:
                    return
                site = next(pending, None)
                if site is None:
                    return
                if runner.verbose:
                    print(f"{file_key}:{site.line} {site.description}", file=sys.stderr)
            try:
                status = _execute(directory, site, cwd, relative, original, runner, plan, timeout, file_key)
            except (OSError, RuntimeError, ValueError) as error:
                status = MutationRunFailure("mutant", f"{file_key}:{site.line} {site.description}: {error}")
            with lock:
                if isinstance(status, MutationRunFailure):
                    if failure is None:
                        failure = status
                    return
                outcomes[site.mutation_id] = status

    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        list(pool.map(drain, workers))
    return failure


def _worker_baseline(
    workers: list[tuple[Path, Path]], relative: str, original: bytes, runner, plan: TestPlan,
) -> MutationRunFailure | None:
    for directory, cwd in workers:
        result = runner.run(plan.command, cwd, None)
        if result.code != 0 or result.timed_out or result.spawn_failed:
            return MutationRunFailure("worker-baseline", _command_failure("Worker baseline failed", cwd, result))
        if _filesystem_path(directory / relative).read_bytes() != original:
            return MutationRunFailure("worker-baseline", f"Worker baseline changed {relative} in {directory}.")
    return None


def _cleanup_workers(base: Path, stage: str, failure: MutationRunFailure | None) -> MutationRunFailure | None:
    try:
        delete_tree(base)
    except OSError as error:
        detail = f"Cannot remove worker directory {base}: {error}"
        if failure is None:
            return MutationRunFailure(stage, detail)
        return MutationRunFailure(failure.stage, f"{failure.message}\n{detail}")
    return failure


def run_mutants(
    root: Path,
    source: Path,
    original: bytes,
    sites: list[Site],
    max_workers: int | None,
    runner,
    plan: TestPlan,
    timeout: float,
    file_key: str,
    outcomes: dict[str, str],
) -> MutationRunFailure | None:
    """Verify every worker, then run mutants and publish complete outcomes."""

    if not sites:
        return None
    base = None
    failure = None
    stage = "worker-baseline"
    tested = {}
    try:
        base = new_run_dir(root, isolated=plan.isolate)
        relative = source.resolve().relative_to(root.resolve()).as_posix()
        directories = create_workers(
            base, root, relative, original, worker_count(len(sites), max_workers), isolated=plan.isolate,
        )
        workers = [(directory, mapped_cwd(directory, root, plan.cwd)) for directory in directories]
        failure = _worker_baseline(workers, relative, original, runner, plan)
        if failure is None:
            stage = "mutant"
            failure = _run_all(workers, sites, tested, relative, original, runner, plan, timeout, file_key)
    except (OSError, RuntimeError, ValueError) as error:
        failure = MutationRunFailure(stage, f"Worker execution failed for {file_key}: {error}")
    finally:
        if base is not None:
            failure = _cleanup_workers(base, stage, failure)
    if failure is None:
        outcomes.update(tested)
    return failure

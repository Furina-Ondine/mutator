from pathlib import Path
import os

import pytest

from mutator.functions import is_private, sites_in_file
from mutator.coverage import covered_lines
from mutator.runner import test_plan as plan_for
from mutator.sites import apply_site, discover_raw
from mutator.workers import create_workers, delete_tree, new_run_dir
from mutator.workers import _copy_tree
from mutator.cli import run
from mutator.edn import loads
from mutator.runner import CommandResult, CommandRunner


def test_csharp_mutations_cover_executable_operators_without_metadata_or_strings():
    source = '''namespace Demo;
class Price {
    [Flag(true, 1)]
    private bool Check(int x, bool ready) {
        var message = "false + 1";
        return !ready && x < 1 || -x + 0 == 1;
    }
}
'''
    sites = discover_raw(source, "csharp", "Price.cs")
    pairs = {(site.original, site.mutant) for site in sites}
    assert {( "!", ""), ("&&", "||"), ("||", "&&"), ("<", "<="),
            ("-", ""), ("+", "-"), ("==", "!="), ("0", "1"), ("1", "0")} <= pairs
    assert {site.line for site in sites} == {6}
    for site in sites:
        assert source.encode()[site.start:site.end].decode() == site.original
        assert apply_site(source, site.start, site.end, site.mutant) != source


def test_csharp_boolean_literals_are_mutated():
    source = "class Demo { bool F(bool value) => value ? true : false; }"
    sites = discover_raw(source, "csharp", "Demo.cs")
    assert {(site.original, site.mutant) for site in sites} == {("true", "false"), ("false", "true")}


def test_csharp_private_members_use_the_existing_snapshot_contract():
    assert is_private("csharp", "Hide", "private int Hide() => 1;", 1, 1)
    assert not is_private("csharp", "Show", "public int Show() => 1;", 1, 1)


def test_csharp_functions_bind_mutations_to_the_reported_type(tmp_path):
    source = "namespace Demo; class Price { public int Fee(int value) => value + 1; }"
    path = tmp_path / "Price.cs"
    path.write_text(source)
    sites = sites_in_file(source, path, tmp_path, "Price.cs")
    assert {(site.namespace, site.form_id) for site in sites} == {("Demo.Price", "defn/Fee")}


def test_csharp_privacy_uses_member_spans_and_inherits_accessor_visibility(tmp_path):
    source = '''public class Price {
    int Hidden() => 1;
    public int Value { get => 1; private set { Store(1); } }
    public int Explicit() => 1;
}
interface IPrice { int Default() => 1; class Nested { int Secret() => 1; } }
'''
    path = tmp_path / "Price.cs"
    path.write_text(source)
    sites = sites_in_file(source, path, tmp_path, "Price.cs")
    assert {site.form_id for site in sites} == {
        "defn-/Hidden", "defn/Value.get", "defn-/Value.set", "defn/Explicit", "defn/Default", "defn-/Secret",
    }


def test_csharp_default_command_runs_the_root_solution_for_both_test_platforms(tmp_path):
    source = tmp_path / "Library" / "Price.cs"
    source.parent.mkdir()
    source.touch()
    (tmp_path / "global.json").write_text('{"test":{"runner":"Microsoft.Testing.Platform"}}')
    assert plan_for(tmp_path, source, "csharp", None) == (["dotnet", "test"], tmp_path.resolve())
    assert plan_for(tmp_path, source, "csharp", "custom") == ("custom", tmp_path.resolve())


def test_csharp_uses_real_cobertura_line_hits_from_the_shared_coverage_bundle(tmp_path):
    source = tmp_path / "Library" / "Price.cs"
    source.parent.mkdir()
    source.write_text("class Price { public int Fee(int n) => n + 1; }")
    report = tmp_path / "target" / "coverage" / "csharp" / "run" / "coverage.cobertura.xml"
    report.parent.mkdir(parents=True)
    report.write_text(
        f'<coverage><sources><source>{tmp_path.as_posix()}</source></sources><packages><package><classes>'
        '<class filename="Library/Price.cs"><lines><line number="1" hits="1" />'
        '<line number="2" hits="0" /></lines></class>'
        '</classes></package></packages></coverage>'
    )
    assert covered_lines(tmp_path, source, "csharp") == {1}
    assert covered_lines(tmp_path, tmp_path / "Other.cs", "csharp") is None
    assert covered_lines(tmp_path, source, "csharp", source_reports={"csharp": []}) is None


def test_csharp_coverage_resolves_paths_against_all_project_sources(tmp_path):
    first = tmp_path / "A" / "Widget.cs"
    second = tmp_path / "B" / "Widget.cs"
    for source in (first, second):
        source.parent.mkdir()
        source.write_text("class Widget {}")
    report = tmp_path / "target/coverage/csharp/coverage.cobertura.xml"
    report.parent.mkdir(parents=True)

    def write_report(name):
        report.write_text(f'<coverage><packages><package><classes><class filename="{name}">'
                          '<lines><line number="7" hits="1"/></lines></class>'
                          '</classes></package></packages></coverage>')

    write_report("Widget.cs")
    assert covered_lines(tmp_path, first, "csharp") is None
    assert covered_lines(tmp_path, second, "csharp") is None
    for name in ("A/Widget.cs", first.as_posix()):
        write_report(name)
        assert covered_lines(tmp_path, first, "csharp") == {7}
        assert covered_lines(tmp_path, second, "csharp") is None
    second.unlink()
    write_report("Widget.cs")
    assert covered_lines(tmp_path, first, "csharp") == {7}


def test_scan_indexes_all_sources_once_for_selected_files(tmp_path, monkeypatch):
    from mutator.cli import _scan, parse_args
    from mutator.crapper_link import ensure_crapper

    first = tmp_path / "A/Widget.cs"
    second = tmp_path / "B/Widget.cs"
    for path in (first, second):
        path.parent.mkdir()
        path.write_text("class Widget {}")
    report = tmp_path / "target/coverage/csharp/coverage.cobertura.xml"
    report.parent.mkdir(parents=True)
    report.write_text('<coverage><packages><package><classes><class filename="Widget.cs">'
                      '<lines><line number="1" hits="1"/></lines></class>'
                      '</classes></package></packages></coverage>')
    discover = ensure_crapper().discover
    original = discover.iter_source_files
    scans = []
    def indexed(roots):
        scans.append(roots)
        return original(roots)
    seen = []
    monkeypatch.setattr(discover, "iter_source_files", indexed)
    monkeypatch.setattr("mutator.cli.scan_file", lambda *args, **kwargs: seen.append(kwargs["covered_lines"]) or "")
    assert _scan(parse_args(["--scan"]), tmp_path, [first, first]) == 0
    assert seen == [None, None]
    assert scans == [[tmp_path]]


def test_cli_loads_coverage_once_per_run_and_sees_report_changes(tmp_path, monkeypatch, capsys):
    from mutator.crapper_link import ensure_crapper
    from mutator.cli import run

    for name in ("A.cs", "B.cs"):
        (tmp_path / name).write_text("class C { bool F() => true; }")
    report = tmp_path / "target/coverage/csharp/coverage.cobertura.xml"
    report.parent.mkdir(parents=True)
    def write_report(hits):
        classes = ''.join(f'<class filename="{name}"><lines><line number="1" hits="{hits}"/>'
                          '</lines></class>' for name in ("A.cs", "B.cs"))
        report.write_text(f'<coverage><packages><package><classes>{classes}'
                          '</classes></package></packages></coverage>')
    coverage = ensure_crapper().coverage
    load, bind = coverage.load_bundle, coverage.CoverageBundle.bind_sources
    loads, binds, seen = [], [], []
    def load_spy(*args, **kwargs):
        loads.append(1)
        return load(*args, **kwargs)
    def bind_spy(self, paths):
        binds.append(1)
        return bind(self, paths)
    monkeypatch.setattr(coverage, "load_bundle", load_spy)
    monkeypatch.setattr(coverage.CoverageBundle, "bind_sources", bind_spy)
    monkeypatch.setattr("mutator.cli.scan_file", lambda *args, **kwargs: seen.append(kwargs["covered_lines"]) or "")
    for hits in (1, 0):
        write_report(hits)
        assert run(["--root", str(tmp_path), "--scan"]) == 0
    assert seen == [{1}, {1}, set(), set()]
    assert len(loads) == len(binds) == 2


def test_csharp_top_level_statements_have_a_mutable_synthetic_owner(tmp_path):
    source = "int value = 1;\nSystem.Console.WriteLine(value + 1);\n"
    path = tmp_path / "Program.cs"
    path.write_text(source)
    sites = sites_in_file(source, path, tmp_path, "Program.cs")
    assert sites
    assert {(site.namespace, site.form_id) for site in sites} == {("Program", "defn/<top-level>")}


def test_csharp_workers_copy_the_build_graph_and_isolate_build_outputs(tmp_path):
    source = tmp_path / "Library" / "Price.cs"
    source.parent.mkdir()
    original = b"class Price { public int Fee(int n) => n + 1; }\r\n"
    source.write_bytes(original)
    tests = tmp_path / "Tests"
    tests.mkdir()
    project = tests / "Tests.csproj"
    project.write_text('<Project Sdk="Microsoft.NET.Sdk"><ItemGroup><ProjectReference Include="../Library/Library.csproj" /></ItemGroup></Project>')
    (source.parent / "Library.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk" />')
    (tmp_path / "Directory.Build.props").write_text('<Project />')
    outputs = ("bin", "obj", "TestResults", ".artifacts", ".vs", "BenchmarkDotNet.Artifacts")
    for name in outputs:
        (tests / name).mkdir()
        (tests / name / "original").write_bytes(b"keep")
    base = new_run_dir(tmp_path)
    try:
        workers = create_workers(base, tmp_path, "Library/Price.cs", original, 2)
        for worker in workers:
            assert not (worker / "Tests").is_symlink()
            assert not (worker / "Tests" / "Tests.csproj").is_symlink()
            assert (worker / "Tests" / "Tests.csproj").read_bytes() == project.read_bytes()
            assert (worker / "Directory.Build.props").is_file()
            for name in outputs:
                assert not (worker / "Tests" / name).exists()
            (worker / "Tests" / "obj").mkdir()
            (worker / "Tests" / "obj" / "generated").write_text("worker")
        assert not (tests / "obj" / "generated").exists()
        assert source.read_bytes() == original
    finally:
        delete_tree(base)


def test_worker_copy_failures_name_both_source_and_destination(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_text("data")
    destination = tmp_path / "destination.txt"

    def fail_copy(*args, **kwargs):
        raise FileNotFoundError("copy failed")

    monkeypatch.setattr("mutator.workers.shutil.copy2", fail_copy)
    with pytest.raises(FileNotFoundError) as raised:
        _copy_tree(source, destination, frozenset())
    notes = "\n".join(getattr(raised.value, "__notes__", []))
    assert str(source) in notes
    assert str(destination) in notes


@pytest.mark.skipif(os.name != "nt", reason="Windows long-path I/O boundary")
def test_csharp_private_copy_and_cleanup_support_long_paths_without_system_changes(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "input.txt").write_bytes(b"compile input")
    destination = tmp_path / ("destination" * 8) / ("nested" * 16)
    assert len(str(destination)) > 260
    _copy_tree(source, destination, frozenset())
    extended = Path("\\\\?\\" + str(destination.absolute()))
    assert (extended / "input.txt").read_bytes() == b"compile input"
    delete_tree(destination.parent)
    assert not extended.exists()


def test_filesystem_boundary_leaves_posix_paths_relative(monkeypatch):
    from types import SimpleNamespace
    from mutator import workers

    monkeypatch.setattr(workers, "os", SimpleNamespace(name="posix"))
    source = Path("relative/input.txt")
    assert workers._filesystem_path(source) == source


@pytest.mark.skipif(os.name != "nt", reason="Windows UNC path boundary")
def test_filesystem_boundary_preserves_unc_server_and_share():
    from mutator.workers import _filesystem_path

    assert str(_filesystem_path(Path(r"\\server\share\folder\file.cs"))) == r"\\?\UNC\server\share\folder\file.cs"


@pytest.mark.parametrize("mode,current_file,expected,counts", [
    ([], "Other.cs", 0, (0, 1)),
    ([], "C.cs", 3, (1, 0)),
    (["--use-existing-coverage"], "Other.cs", 3, (1, 0)),
    (["--coverage-command", "user-collector"], "Other.cs", 3, (1, 0)),
])
def test_cli_uses_the_requested_report_scope_without_deleting_existing_reports(tmp_path, monkeypatch, mode, current_file, expected, counts):
    source = tmp_path / "C.cs"
    original = b"class C { public bool F() => true; }\r\n"
    source.write_bytes(original)
    xml = '<coverage><packages><package><classes><class filename="C.cs"><lines><line number="1" hits="1"/></lines></class></classes></package></packages></coverage>'
    old = tmp_path / "TestResults" / "old" / "coverage.cobertura.xml"
    old.parent.mkdir(parents=True)
    old.write_text(xml, encoding="utf-8")

    def collect(*args):
        fresh = tmp_path / "target" / "coverage" / "csharp" / "run.cobertura.xml"
        fresh.parent.mkdir(parents=True)
        fresh.write_text(xml.replace('filename="C.cs"', f'filename="{current_file}"'), encoding="utf-8")
        return 0

    # Collector and test process boundaries are controlled here; native .NET
    # rebuild/kill/survive behavior is exercised separately against real projects.
    monkeypatch.setattr("crapper.runners.run_coverage", collect)
    monkeypatch.setattr(CommandRunner, "run", lambda *args: CommandResult(0, False, 0.01, ""))
    assert run(["--root", str(tmp_path), "--max-workers", "1", "--mutate-all", *mode, str(source)]) == expected
    forms = loads((tmp_path / ".metrics/mutate/C.edn").read_text(encoding="utf-8"))["forms"]
    assert [(form["survived"], form["uncovered"]) for form in forms] == [counts]
    assert old.read_text(encoding="utf-8") == xml
    assert source.read_bytes() == original


@pytest.mark.parametrize("mode,expected", [([], None), (["--scan"], {1}),
    (["--use-existing-coverage"], {1}), (["--coverage-command", "collector"], {1})])
def test_coverage_policy_preserves_explicit_report_sources(tmp_path, mode, expected):
    from mutator.cli import _coverage_for, parse_args

    path = tmp_path / "C.cs"
    path.write_text("class C { bool F() => true; }")
    old = tmp_path / "TestResults" / "coverage.cobertura.xml"
    old.parent.mkdir()
    old.write_text('<coverage><packages><package><classes><class filename="C.cs"><lines><line number="1" hits="1"/></lines></class></classes></package></packages></coverage>')
    fresh = tmp_path / "target/coverage/csharp/coverage.cobertura.xml"
    fresh.parent.mkdir(parents=True)
    fresh.write_text(old.read_text().replace('filename="C.cs"', 'filename="Other.cs"'))
    assert _coverage_for(parse_args(mode), tmp_path, path) == expected


def test_copy_worker_preserves_build_inputs_and_isolates_outputs(tmp_path):
    import sys

    source = tmp_path / 'Program.cs'
    source.write_text('class C {}', encoding='utf-8')
    build = tmp_path / 'build'
    build.mkdir()
    (build / 'value.txt').write_text('required', encoding='utf-8')
    for name in ('dist', 'out', 'coverage', 'target'):
        folder = tmp_path / name
        folder.mkdir(exist_ok=True)
        (folder / 'input.txt').write_text(name, encoding='utf-8')
    (build / 'check.py').write_text(
        'from pathlib import Path\n'
        'assert Path("build/value.txt").read_text() == "required"\n'
        'Path("obj").mkdir(exist_ok=True)\nPath("obj/generated").write_text("worker")\n', encoding='utf-8')
    base = new_run_dir(tmp_path)
    try:
        worker, = create_workers(base, tmp_path, 'Program.cs', source.read_bytes(), 1)
        for name in ('dist', 'out', 'coverage', 'target'):
            assert (worker / name / 'input.txt').read_text() == name
        result = CommandRunner().run([sys.executable, 'build/check.py'], worker, None)
        assert result.code == 0, result.output
        assert (worker / 'obj/generated').read_text() == 'worker'
        assert not (tmp_path / 'obj/generated').exists()
    finally:
        delete_tree(base)


def test_copy_worker_does_not_inherit_duplicate_ancestor_globalconfig(tmp_path, monkeypatch):
    from mutator.engine import mutate_file
    from mutator import workers

    (tmp_path / '.globalconfig').write_text('is_global = true\ndotnet_diagnostic.CS0168.severity = none\n', encoding='utf-8')
    (tmp_path / 'Sample.csproj').write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net10.0</TargetFramework>'
        '<TreatWarningsAsErrors>true</TreatWarningsAsErrors></PropertyGroup></Project>', encoding='utf-8')
    source = tmp_path / 'Sample.cs'
    source.write_text('public class Sample { public int Add(int a, int b) { int unused; return a + b; } }', encoding='utf-8')
    created = []
    original = workers.new_run_dir
    def remember(*args, **kwargs):
        path = original(*args, **kwargs)
        created.append(path)
        return path
    monkeypatch.setattr(workers, 'new_run_dir', remember)
    result = mutate_file(source, tmp_path, runner=CommandRunner(), covered_lines=None,
                         ignore_coverage=True, mutate_all=True, lines=None,
                         test_command='dotnet build Sample.csproj -nologo -verbosity:quiet', timeout_factor=20,
                         mutation_warning=50, baselines={}, max_workers=1)
    assert not result.baseline_failed, result.baseline_message
    assert result.forms[0].survived == 1
    assert len(created) == 1 and not created[0].exists()

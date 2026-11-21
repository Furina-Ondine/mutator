from mutator.cli import run
from mutator.functions import form_spans, sites_in_file
from mutator.runner import test_plan as plan_command


def test_csharp_sites_follow_shared_member_ownership_and_tokens(tmp_path):
    source = """// Ω
namespace Example;
class Check {
    [Sample(0)]
    bool Hidden(int value) => value is > 1 and < 10;
    public int Read(int? value) => value ?? 0;
    public bool Toggle(bool value) => !value && true;
}
"""
    path = tmp_path / "Check.cs"
    forms = form_spans(source, path, tmp_path)
    assert ("Example.Check", "defn-/Hidden") in forms
    assert ("Example.Check", "defn/Read") in forms
    sites = sites_in_file(source, path, tmp_path, "Check.cs")
    pairs = {(site.form_id, site.original, site.mutant) for site in sites}
    assert ("defn-/Hidden", "and", "or") in pairs
    assert ("defn-/Hidden", ">", ">=") in pairs
    assert ("defn-/Hidden", "0", "1") not in pairs
    assert ("defn/Toggle", "!", "") in pairs
    assert ("defn/Toggle", "true", "false") in pairs
    assert ("defn/Read", "0", "1") in pairs
    assert not any(site.original == "??" for site in sites)
    data = source.encode("utf-8")
    assert all(data[s.start:s.end].decode("utf-8") == s.original for s in sites)


def test_scan_uses_csharp_source_selection(tmp_path, capsys):
    source = "class Widget { public bool Enabled() => true; }"
    for name in ("Widget.cs", "WidgetTests.cs", "Widget.g.cs", "obj/Generated.cs"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    assert run(["--root", str(tmp_path), "--scan", "--no-coverage"]) == 0
    captured = capsys.readouterr()
    assert "Widget.cs:1" in captured.out
    assert "WidgetTests.cs" not in captured.out
    assert "Widget.g.cs" not in captured.out
    assert "Generated.cs" not in captured.out
    assert not (tmp_path / ".metrics").exists()


def test_csharp_uses_dotnet_test_from_the_project_root(tmp_path):
    assert plan_command(tmp_path, tmp_path / "Widget.cs", "csharp", None) == (
        ["dotnet", "test"],
        tmp_path.resolve(),
    )


def test_scan_reports_csharp_parse_error_without_a_traceback(tmp_path, capsys):
    path = tmp_path / "Broken.cs"
    path.write_text("class Broken { int Run( { }", encoding="utf-8")
    assert run(["--root", str(tmp_path), "--scan", "--no-coverage", str(path)]) == 2
    captured = capsys.readouterr()
    assert "Broken.cs:1:" in captured.err
    assert "CS" in captured.err
    assert not (tmp_path / ".metrics").exists()

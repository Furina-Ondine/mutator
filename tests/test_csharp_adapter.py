from mutator.cli import run
from mutator.coverage import covered_lines
from mutator.functions import form_spans, sites_in_file


def test_csharp_sites_follow_shared_member_ownership_and_tokens(tmp_path):
    source = """// Ω
namespace Example;
class Check {
    bool Hidden(int value) => value is > 1 and < 10;
    public int Read(int? value) => value ?? 0;
    public bool Toggle(bool value) => !value;
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
    assert ("defn/Toggle", "!", "") in pairs
    assert ("defn/Read", "0", "1") in pairs
    assert not any(site.original == "??" for site in sites)
    data = source.encode("utf-8")
    assert all(data[s.start:s.end].decode("utf-8") == s.original for s in sites)


def test_cobertura_coverage_uses_project_source_identity(tmp_path):
    for directory in ("A", "B"):
        path = tmp_path / directory / "Widget.cs"
        path.parent.mkdir()
        path.write_text("class Widget {}\n", encoding="utf-8")
    report = tmp_path / "coverage/sample.cobertura.xml"
    report.parent.mkdir()

    def write(filename: str) -> None:
        report.write_text(
            f'<coverage><class filename="{filename}"><lines>'
            '<line number="1" hits="1"/></lines></class></coverage>',
            encoding="utf-8",
        )

    write("Widget.cs")
    assert covered_lines(tmp_path, tmp_path / "A/Widget.cs", "csharp") is None
    assert covered_lines(tmp_path, tmp_path / "B/Widget.cs", "csharp") is None
    write("A/Widget.cs")
    assert covered_lines(tmp_path, tmp_path / "A/Widget.cs", "csharp") == {1}
    assert covered_lines(tmp_path, tmp_path / "B/Widget.cs", "csharp") is None


def test_scan_reports_csharp_parse_error_without_a_traceback(tmp_path, capsys):
    path = tmp_path / "Broken.cs"
    path.write_text("class Broken { int Run( { }", encoding="utf-8")
    assert run(["--root", str(tmp_path), "--scan", "--no-coverage", str(path)]) == 2
    captured = capsys.readouterr()
    assert "Broken.cs:1:" in captured.err
    assert "CS" in captured.err
    assert not (tmp_path / ".metrics").exists()

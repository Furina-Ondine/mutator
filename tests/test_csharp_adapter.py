from mutator.cli import run
from mutator.functions import form_spans, sites_in_file
from mutator.runner import test_plan as plan_command
from mutator.sites import apply_site


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
    assert ("defn/Read", "value ?? 0", "(0)") in pairs
    data = source.encode("utf-8")
    assert all(data[s.start:s.end].decode("utf-8") == s.original for s in sites)


def test_csharp_coalesce_replaces_the_expression_with_its_fallback(tmp_path):
    source = """class Check {
    string Read(string? 主值, string? spare, string fallback) {
        /* 🧪 */ return 主值
            ?? spare
            ?? fallback;
    }
    string Adjacent(string fallback) { return(null)??fallback; }
    string Required(string? value) => value ?? throw new System.Exception();
    string Assign(string? value, string fallback) => value ??= fallback;
    string Text() => "value ?? fallback";
}
"""
    path = tmp_path / "Check.cs"
    sites = sites_in_file(source, path, tmp_path, "Check.cs")
    outer = "主值\n            ?? spare\n            ?? fallback"
    inner = "spare\n            ?? fallback"
    expected = {
        outer: "(spare\n            ?? fallback)",
        inner: "(fallback)",
        "(null)??fallback": "(fallback)",
        "??=": "=",
    }
    assert {site.original: site.mutant for site in sites} == expected
    for site in sites:
        assert apply_site(source, site.start, site.end, site.mutant) == source.replace(
            site.original, expected[site.original], 1
        )
    multiline = next(site for site in sites if site.original == inner)
    assert (multiline.line, multiline.anchor_line) == (4, 3)
    assert "\\n" in multiline.description
    assert "\n" not in multiline.description and "\r" not in multiline.description


def test_csharp_conditional_swaps_branches_preserving_condition_and_trivia(tmp_path):
    source = """class Check {
    string Select(bool ready, bool nested, string first, string second, string third)
        => ready /* condition */ ? /* true */ first
            : /* false */ nested ? second : third;
    ref int SelectRef(bool ready, ref int first, ref int second)
        => ref (ready ? ref first : ref second);
    int Same(bool ready, int value) => ready ? value : value;
}
"""
    path = tmp_path / "Check.cs"
    outer = "ready /* condition */ ? /* true */ first\n            : /* false */ nested ? second : third"
    expected = {
        outer: "ready /* condition */ ? /* true */ nested ? second : third\n            : /* false */ first",
        "nested ? second : third": "nested ? third : second",
        "ready ? ref first : ref second": "ready ? ref second : ref first",
    }
    sites = sites_in_file(source, path, tmp_path, "Check.cs")
    assert {site.original: site.mutant for site in sites} == expected
    for site in sites:
        patched = apply_site(source, site.start, site.end, site.mutant)
        assert patched == source.replace(site.original, expected[site.original], 1)
        assert sites_in_file(patched, path, tmp_path, "Check.cs")


def test_csharp_conditional_access_removes_each_question_token_independently(tmp_path):
    source = """class Check {
    readonly Check[] Children = [];
    string Name() => "";
    string? Read(Check? item, int index)
        => item? /* keep */ .Children?[index]?.Name();
    string Text() => "item?.Children?[index]?.Name()";
    string Plain(Check item, int index) => item.Children[index].Name();
}
"""
    path = tmp_path / "Check.cs"
    sites = sites_in_file(source, path, tmp_path, "Check.cs")
    assert [(site.form_id, site.original, site.mutant) for site in sites] == [
        ("defn-/Read", "?", ""),
        ("defn-/Read", "?", ""),
        ("defn-/Read", "?", ""),
    ]
    original = "item? /* keep */ .Children?[index]?.Name()"
    expected = [
        "item /* keep */ .Children?[index]?.Name()",
        "item? /* keep */ .Children[index]?.Name()",
        "item? /* keep */ .Children?[index].Name()",
    ]
    for site, expression in zip(sites, expected):
        patched = apply_site(source, site.start, site.end, site.mutant)
        assert patched == source.replace(original, expression, 1)
        assert len(sites_in_file(patched, path, tmp_path, "Check.cs")) == 2


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

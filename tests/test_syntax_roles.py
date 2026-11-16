"""Compiler syntax owns C# spans; mutation rules consume common capabilities."""

import pytest

from mutator.sites import discover_raw


def test_invalid_csharp_reports_the_compiler_diagnostic_without_partial_mutants():
    with pytest.raises(SyntaxError):
        discover_raw("class Demo { int F() => 1 + ; }", "csharp", "Broken.cs")


def test_csharp_ref_return_and_ref_assignment_use_the_shared_native_syntax_tree():
    from crapper.frontend import parse_document

    source = '''class Demo {
    int value;
    ref int Bind(ref int slot) { slot = ref value; return ref slot; }
    int Add(int n) => n + 1;
}
'''
    document = parse_document(source, "csharp", "Reference.cs")
    assert any(node.kind == "RefExpression" for node in document.nodes)
    sites = discover_raw(source, "csharp", "Reference.cs")
    assert {(site.original, site.mutant) for site in sites} == {("+", "-"), ("1", "0")}
    assert all(site.line == 4 for site in sites)


def test_csharp_patterns_and_interpolation_mutate_executable_tokens_with_utf8_spans():
    source = '''class Demo {
    // emoji 🧪 before the source spans
    string Show(int n) => $"literal true + 1 😀 {n + 1}";
    bool Match(int n) => n is > 2 and < 10 or not 100;
}
'''
    sites = discover_raw(source, "csharp", "Unicode.cs")
    by_line = {}
    for site in sites:
        assert source.encode("utf-8")[site.start:site.end].decode("utf-8") == site.original
        by_line.setdefault(site.line, set()).add((site.original, site.mutant))
    assert by_line == {
        3: {("+", "-"), ("1", "0")},
        4: {(">", ">="), ("<", "<="), ("and", "or"), ("or", "and"), ("not", "")},
    }


def test_csharp_metadata_has_no_executable_literal_mutations():
    source = 'class Demo { [Flag(true, 1)] bool F() => true; }'
    sites = discover_raw(source, "csharp", "Metadata.cs")
    assert [(site.original, site.mutant) for site in sites] == [("true", "false")]

import pytest

from mutator.sites import discover_raw


@pytest.mark.parametrize("language,path,source,expected", [
    ("typescript", "value.ts", "function value(a, b) { return a ?? b; }", {("??", "||")}),
    ("csharp", "Value.cs", 'class C { string Value(string a) => a ?? "fallback"; }', set()),
    ("go", "value.go", "package value; func Value(a, b int) int { return a / b }", set()),
    ("java", "Value.java", "class C { int value(int a, int b) { return a / b; } }", {("/", "*")}),
    ("python", "value.py", "def value(a, b):\n    return a and b\n", {("and", "or")}),
])
def test_languages_keep_their_operator_semantics(language, path, source, expected):
    assert {(site.original, site.mutant) for site in discover_raw(source, language, path)} == expected


def test_csharp_relational_and_logical_patterns_share_operator_mutations():
    source = 'class C { bool Match(int value) => value is > 2 and < 10 or not 100; }'
    sites = discover_raw(source, "csharp", "Value.cs")
    assert {(site.original, site.mutant) for site in sites} == {
        (">", ">="), ("<", "<="), ("and", "or"), ("or", "and"), ("not", ""),
    }
    for site in sites:
        assert source.encode()[site.start:site.end].decode() == site.original

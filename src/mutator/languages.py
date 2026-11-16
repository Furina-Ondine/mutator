"""Language capabilities used by the common mutation and workspace engines."""

from dataclasses import dataclass, field
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType


_BINARY = MappingProxyType({
    "+": "-", "-": "+", "*": "/", "/": "*",
    ">": ">=", ">=": ">", "<": "<=", "<=": "<",
    "==": "!=", "!=": "==", "&&": "||", "||": "&&",
})


@dataclass(frozen=True)
class LanguageProfile:
    grammar: str
    binary: Mapping[str, str] = field(default_factory=lambda: _BINARY)
    unary: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({"!": "", "not": "", "-": ""}))
    skip_nodes: frozenset = frozenset()
    grammar_variants: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    optional_access: bool = False
    workspace: str = "overlay"
    build: tuple[str, ...] = ()
    test: tuple[str, ...] = ()
    built_test: tuple[str, ...] = ()
    compiler_error: str = ""


_PROFILES = {
    "typescript": LanguageProfile(
        "typescript",
        binary=MappingProxyType({**_BINARY, "===": "!==", "!==": "===", "??": "||"}),
        skip_nodes=frozenset({"type_annotation", "type_alias_declaration", "interface_declaration",
                              "type_arguments", "type_parameters", "literal_type"}),
        grammar_variants=MappingProxyType({".tsx": "tsx", ".js": "javascript", ".jsx": "javascript",
                                           ".mjs": "javascript", ".cjs": "javascript"}),
        optional_access=True,
    ),
    "go": LanguageProfile("go", binary=MappingProxyType({key: value for key, value in _BINARY.items() if key != "/"})),
    "python": LanguageProfile("python", binary=MappingProxyType({**_BINARY, "and": "or", "or": "and"})),
    "csharp": LanguageProfile(
        "c_sharp",
        binary=MappingProxyType({**_BINARY, "and": "or", "or": "and"}),
        workspace="copy",
        build=("dotnet", "build"),
        test=("dotnet", "test"),
        built_test=("dotnet", "test", "--no-build"),
        compiler_error=r"(?im)^(?:[^\r\n]+\(\d+(?:,\d+){0,3}\)|CSC)\s*:\s*error\s+CS\d{4}\b",
    ),
}


def profile_for(language: str) -> LanguageProfile:
    return _PROFILES.get(language, LanguageProfile(language))


def grammar_for(language: str, path: str) -> str:
    profile = profile_for(language)
    return profile.grammar_variants.get(Path(path).suffix, profile.grammar)

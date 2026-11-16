"""Find mutation sites.

Clojure follows clj-mutate's symbol rules. Go follows mutate4go, including
one-way `*` to `/`. Java follows mutate4java. TypeScript, Rust, and Python
use the same decisions as Java, spelled in that language. TypeScript also
mutates `??` to `||` and `?.` to `.` (removed on a call or index). JavaScript
files use the TypeScript rules.
"""

from __future__ import annotations

from mutator.crapper_link import ensure_crapper
from mutator.languages import grammar_for, profile_for

_BOOLEANS = {"true": "false", "false": "true", "True": "False", "False": "True"}

_LITERAL_RULES = (
    ("boolean", _BOOLEANS, "boolean"),
    ("number", {"0": "1", "1": "0"}, "constant"),
)

_CLOJURE_HEAD = {
    "+": ("-", "arithmetic"),
    "-": ("+", "arithmetic"),
    "*": ("/", "arithmetic"),
    "inc": ("dec", "arithmetic"),
    "dec": ("inc", "arithmetic"),
    ">": (">=", "comparison"),
    ">=": (">", "comparison"),
    "<": ("<=", "comparison"),
    "<=": ("<", "comparison"),
    "=": ("not=", "equality"),
    "not=": ("=", "equality"),
    "if": ("if-not", "conditional"),
    "if-not": ("if", "conditional"),
    "when": ("when-not", "conditional"),
    "when-not": ("when", "conditional"),
    "and": ("or", "logical"),
    "or": ("and", "logical"),
    "double": ("int", "coercion"),
    "int": ("double", "coercion"),
    "first": ("second", "seq"),
    "second": ("first", "seq"),
    "filter": ("remove", "seq"),
    "remove": ("filter", "seq"),
    "take": ("drop", "seq"),
    "drop": ("take", "seq"),
    "rest": ("next", "seq"),
    "next": ("rest", "seq"),
    "every?": ("some", "seq"),
    "some": ("every?", "seq"),
    "min": ("max", "numeric"),
    "max": ("min", "numeric"),
    "pos?": ("neg?", "predicate"),
    "neg?": ("pos?", "predicate"),
    "even?": ("odd?", "predicate"),
    "odd?": ("even?", "predicate"),
    "nil?": ("some?", "predicate"),
    "some?": ("nil?", "predicate"),
}


class RawSite:
    def __init__(
        self, line: int, start: int, end: int, original: str, mutant: str, category: str, anchor_line: int = 0
    ):
        self.line = line
        # The line its statement starts on. LCOV marks only that line of a multi-line statement.
        self.anchor_line = anchor_line or line
        self.start = start
        self.end = end
        self.original = original
        self.mutant = mutant
        self.category = category


def _grammar(language: str, path: str) -> str:
    return grammar_for(language, path)


def _text(data: bytes, node) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8")


def _category(token: str) -> str:
    if token in {"+", "-", "*", "/"}:
        return "arithmetic"
    if token in {">", ">=", "<", "<="}:
        return "comparison"
    if token in {"==", "!=", "===", "!=="}:
        return "equality"
    if token in {"&&", "||", "??", "and", "or"}:
        return "logical"
    return "arithmetic"


def _optional_mutant(node) -> str | None:
    """`a?.b` becomes `a.b`. A call or index drops `?.`, so `a?.()` becomes `a()`."""

    parent = node.parent
    if parent is None:
        return None
    host = parent.parent if parent.type == "optional_chain" else parent
    if host is None:
        return None
    if host.type == "member_expression":
        return "."
    if host.type in {"call_expression", "subscript_expression"}:
        return ""
    return None


def _add(found: list[RawSite], node, original: str, mutant: str, category: str) -> None:
    if mutant == original:
        return
    found.append(
        RawSite(
            line=node.start_point[0] + 1,
            start=node.start_byte,
            end=node.end_byte,
            original=original,
            mutant=mutant,
            category=category,
            anchor_line=_statement_line(node),
        )
    )


def _statement_line(node) -> int:
    current = node.parent
    while current is not None:
        if current.roles & {"statement", "declaration"}:
            return current.start_point[0] + 1
        current = current.parent
    return node.start_point[0] + 1


def _clojure_head(node, data: bytes) -> str | None:
    for child in node.children:
        if child.type == "sym_lit":
            return _text(data, child)
    return None


def _is_head(node) -> bool:
    parent = node.parent
    if parent is None or parent.type != "list_lit":
        return False
    for child in parent.children:
        if child.type == "sym_lit":
            return child.id == node.id
    return False


def _clojure_skipped(node, data: bytes) -> bool:
    current = node
    while current is not None:
        if any(part in current.type for part in ("comment", "quot", "string", "str_lit", "char_lit")):
            return True
        if current.type == "list_lit":
            head = _clojure_head(current, data)
            if head in {"comment", "quote"}:
                return True
        current = current.parent
    return False


def _clojure_text(node, data: bytes) -> str:
    if node.child_count == 0 or node.type in {"sym_lit", "bool_lit", "num_lit"}:
        return _text(data, node)
    return ""


def _flip01(text: str) -> str:
    if text == "0":
        return "1"
    return "0"


def _clojure_node(node, data: bytes, found: list[RawSite]) -> None:
    if _clojure_skipped(node, data):
        return
    text = _clojure_text(node, data)
    if node.type == "sym_lit" and _is_head(node) and text in _CLOJURE_HEAD:
        mutant, category = _CLOJURE_HEAD[text]
        _add(found, node, text, mutant, category)
        return
    if node.type == "bool_lit" and text in _BOOLEANS:
        _add(found, node, text, _BOOLEANS[text], "boolean")
        return
    if node.type == "num_lit" and text in {"0", "1"}:
        _add(found, node, text, _flip01(text), "constant")


def _visit_clojure(node, data: bytes, found: list[RawSite]) -> None:
    # An explicit stack: a 1,200-deep form must not raise RecursionError.
    stack = [node]
    while stack:
        current = stack.pop()
        _clojure_node(current, data, found)
        stack.extend(reversed(current.children))


def _optional_rule(node, token: str, profile):
    if not profile.optional_access or token != "?.":
        return None
    optional = _optional_mutant(node)
    if optional is not None:
        return optional, "optional"
    return None


def _operator_rule(node, token: str, profile):
    if node.parent is None:
        return None
    rules = (("binary", profile.binary, _category(token)), ("unary", profile.unary, "unary"))
    for role, replacements, category in rules:
        if role in node.parent.roles and token in replacements:
            return replacements[token], category
    return None


def _literal_rule(node, token: str):
    for role, replacements, category in _LITERAL_RULES:
        if role in node.roles and token in replacements:
            return replacements[token], category
    return None


def _visit_node(node, data: bytes, profile, found: list[RawSite]) -> None:
    if node.child_count:
        return
    token = _text(data, node)
    replacement = _literal_rule(node, token) or _operator_rule(node, token, profile) or _optional_rule(node, token, profile)
    if replacement is not None:
        mutant, category = replacement
        _add(found, node, token, mutant, category)


def _visit(node, data: bytes, language: str, found: list[RawSite]) -> None:
    # An explicit stack: a 1,200-term expression must not raise RecursionError.
    stack = [node]
    profile = profile_for(language)
    while stack:
        current = stack.pop()
        if "metadata" in current.roles or current.type in profile.skip_nodes:
            continue
        _visit_node(current, data, profile, found)
        stack.extend(reversed(current.children))


def discover_raw(
    source: str, language: str, path: str, *, project_root: str | None = None,
) -> list[RawSite]:
    """Mutation sites in source order. Offsets are UTF-8 byte offsets."""

    ensure_crapper()
    from crapper.frontend import parse_document

    data = source.encode("utf-8")
    document = parse_document(source, language, path, project_root)
    found: list[RawSite] = []
    if language == "clojure":
        _visit_clojure(document.root, data, found)
    else:
        _visit(document.root, data, language, found)
    found.sort(key=lambda site: (site.line, site.start, site.end))
    return found


def apply_site(source: str, start: int, end: int, mutant: str) -> str:
    data = source.encode("utf-8")
    updated = data[:start] + mutant.encode("utf-8") + data[end:]
    return updated.decode("utf-8")

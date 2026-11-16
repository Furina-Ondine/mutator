"""Reports put untested operations and weaker test protection first."""

from mutator.model import FormResult
from mutator.report import format_results


def form(name, killed, survived):
    return FormResult(f"defn/{name}", "Example", name, False, "Example.cs", 1, 1, "hash",
                      killed=killed, survived=survived)


def test_report_orders_by_score_then_survivor_count():
    rows = [form("Alpha", 1, 1), form("High", 3, 1), form("Zero", 0, 2),
            form("Zeta", 2, 2), form("Quarter", 1, 3), form("Untested", 0, 0)]
    names = {row.name for row in rows}
    printed = format_results(rows)
    order = [line.split()[0] for line in printed.splitlines()
             if line.strip() and line.split()[0] in names]
    assert order == ["Untested", "Zero", "Quarter", "Zeta", "Alpha", "High"]

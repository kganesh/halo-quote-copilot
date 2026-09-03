"""The spend record, which has to survive the failures it is most needed for."""

from decimal import Decimal

from halo.platform.budget import Usage
from halo.platform.ledger import by_command, read, record, totals


def a_usage(usd: str, **kw) -> Usage:
    return Usage(
        input_tokens=kw.get("input_tokens", 1000),
        output_tokens=kw.get("output_tokens", 500),
        tool_calls=kw.get("tool_calls", 0),
        usd=Decimal(usd),
    )


def test_runs_accumulate(tmp_path):
    path = tmp_path / "spend.jsonl"
    record("quote", "sonnet", a_usage("0.05"), path=path)
    record("source", "sonnet", a_usage("0.20", tool_calls=7), path=path)

    total = totals(read(path))
    assert total.runs == 2
    assert total.usd == Decimal("0.25")
    assert total.tool_calls == 7


def test_spend_is_broken_down_by_command(tmp_path):
    path = tmp_path / "spend.jsonl"
    record("quote", "sonnet", a_usage("0.05"), path=path)
    record("source", "sonnet", a_usage("0.20"), path=path)
    record("source", "sonnet", a_usage("0.10"), path=path)

    grouped = by_command(read(path))
    assert grouped["source"].runs == 2
    assert grouped["source"].usd == Decimal("0.30")


def test_a_torn_line_does_not_lose_the_rest(tmp_path):
    """A crashed run can leave a half-written line; the history still matters."""
    path = tmp_path / "spend.jsonl"
    record("quote", "sonnet", a_usage("0.05"), path=path)
    with path.open("a") as handle:
        handle.write('{"command": "source", "usd": "0.9')

    assert totals(read(path)).usd == Decimal("0.05")


def test_a_ledger_failure_never_fails_the_run(tmp_path):
    """Losing a spend row is a nuisance; losing the quote is not acceptable."""
    unwritable = tmp_path / "nope" / "spend.jsonl"
    unwritable.parent.mkdir()
    unwritable.parent.chmod(0o500)
    try:
        record("quote", "sonnet", a_usage("0.05"), path=unwritable)
    finally:
        unwritable.parent.chmod(0o700)


def test_missing_ledger_reads_as_empty(tmp_path):
    assert read(tmp_path / "never-written.jsonl") == []

"""The backfill command: symbol parsing and exit code. P-002, DEC-026.

Covers what used to break silently. A file path went to the exchange
as a symbol name, the command printed "ERROR" for every stream
and returned zero — and the pipeline rebuilt the universe on data
that never loaded.
"""

from __future__ import annotations

from typer.testing import CliRunner

from src import cli

runner = CliRunner()


def test_partial_failure_code_does_not_collide_with_usage_error():
    """Typer reports a usage error with exit code 2. A script that tolerates
    partial failure would then also tolerate a typo in the arguments."""
    usage_error = 2
    assert usage_error not in {cli.EXIT_PARTIAL, cli.EXIT_FAILED}
    assert cli.EXIT_PARTIAL != cli.EXIT_FAILED
    assert 0 not in {cli.EXIT_PARTIAL, cli.EXIT_FAILED}


def test_path_to_missing_file_is_refused_before_touching_the_exchange():
    result = runner.invoke(
        cli.app, ["backfill", "--symbols", "var/reports/no_such_file.txt", "--years", "0.01"]
    )
    assert result.exit_code != 0
    assert "not symbol names" in result.output


def test_symbols_are_read_from_a_file(tmp_path):
    """Same as every other command: a string, a file or 'universe'."""
    import asyncio

    listing = tmp_path / "symbols.txt"
    listing.write_text("btcusdt\nETHUSDT\n\n")
    resolved = asyncio.run(cli._resolve_symbols(str(listing)))
    # Case is normalised the same way as for a comma-separated list.
    assert resolved == ["BTCUSDT", "ETHUSDT"]


def test_comma_list_is_upper_cased():
    import asyncio

    assert asyncio.run(cli._resolve_symbols("btcusdt, ethusdt")) == [
        "BTCUSDT",
        "ETHUSDT",
    ]


def test_estimate_does_not_need_the_exchange():
    result = runner.invoke(
        cli.app,
        ["backfill", "--symbols", "BTCUSDT", "--years", "1", "--estimate"],
    )
    assert result.exit_code == 0
    assert "Estimate" in result.output


def test_symbol_resolution_releases_the_database_before_the_next_loop(monkeypatch):
    """List parsing and loading run in different event loops: the database
    engine must be reset between them."""
    calls = []

    async def fake_resolve(spec):
        calls.append(("resolve", spec))
        return ["BTCUSDT"]

    async def fake_dispose():
        calls.append(("dispose",))

    monkeypatch.setattr(cli, "_resolve_symbols", fake_resolve)
    monkeypatch.setattr(cli, "dispose_engine", fake_dispose)
    from typer.testing import CliRunner

    CliRunner().invoke(cli.app, ["backfill", "--symbols", "universe", "--estimate"])
    assert calls[:2] == [("resolve", "universe"), ("dispose",)]

"""Tests for symbol enumeration via the archive (DEC-006)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from src.core.config import load_settings
from src.exchange.archive import ArchiveCatalog, SymbolCatalog, parse_archive_listing

LISTING = """
<html><body>
<a href="../">../</a>
<a href="BTCUSDT/">BTCUSDT/</a>
<a href="LUNAUSDT/">LUNAUSDT/</a>
<a href="FTTUSDT/">FTTUSDT/</a>
<a href="MATICUSDT/">MATICUSDT/</a>
<a href="BTCPERP/">BTCPERP/</a>
<a href="ETHUSDC/">ETHUSDC/</a>
</body></html>
"""


def test_parses_usdt_directories_only():
    symbols = parse_archive_listing(LISTING)
    assert symbols == {"BTCUSDT", "LUNAUSDT", "FTTUSDT", "MATICUSDT"}


def test_parent_directory_is_not_a_symbol():
    assert ".." not in parse_archive_listing(LISTING)


def test_delisted_is_archive_minus_live():
    """The point of the catalog: dead pairs are in the archive and absent from the API."""
    catalog = SymbolCatalog(
        archive_symbols=frozenset({"BTCUSDT", "LUNAUSDT", "FTTUSDT"}),
        live_symbols=frozenset({"BTCUSDT"}),
        fetched_at=datetime.now(UTC),
    )
    assert catalog.delisted == {"LUNAUSDT", "FTTUSDT"}
    assert catalog.all_symbols == {"BTCUSDT", "LUNAUSDT", "FTTUSDT"}


def test_live_symbol_absent_from_archive_still_counted():
    """A fresh listing may not have appeared in the archive yet."""
    catalog = SymbolCatalog(
        archive_symbols=frozenset({"BTCUSDT"}),
        live_symbols=frozenset({"BTCUSDT", "CASHCATUSDT"}),
        fetched_at=datetime.now(UTC),
    )
    assert "CASHCATUSDT" in catalog.all_symbols
    assert "CASHCATUSDT" not in catalog.delisted


# ---------------------------------------------------------------------------
# Announcement-parsing regex — against real names from the archive
# ---------------------------------------------------------------------------
# The archive is now the source of truth, so the parser is checked against its
# names rather than made-up cases. The sample was taken from the live archive on
# 2026-08-07 (991 USDT pairs) and covers every edge case found.
#
# The previous regex required at least two characters before USDT and silently
# lost ALL 16 single-character bases. This sample exists to catch exactly that
# class of bug up front.
ARCHIVE_SAMPLE = [
    # Single-character bases: 16 in the archive, none matched before.
    "4USDT",
    "AUSDT",
    "MUSDT",
    "WUSDT",
    # Two-character and ordinary ones.
    "0GUSDT",
    "BTCUSDT",
    "SOLUSDT",
    "HYPEUSDT",
    # The longest bases in the archive — 15 characters.
    "1000000BABYDOGEUSDT",
    "10000000AIDOGEUSDT",
    "1000NEIROCTOUSDT",
    # Starting with digits.
    "10000COQUSDT",
    "1000PEPEUSDT",
    # Delisted.
    "LUNAUSDT",
    "FTTUSDT",
    "MATICUSDT",
]


@pytest.mark.parametrize("symbol", ARCHIVE_SAMPLE)
def test_regex_matches_every_real_archive_name(symbol):
    from src.exchange.archive import _SYMBOL_IN_TITLE

    assert _SYMBOL_IN_TITLE.fullmatch(symbol) is not None, (
        f"{symbol} not recognised: its delisting announcement would be lost"
    )


def test_regex_extracts_symbols_from_realistic_titles():
    """Bybit titles come in two forms: single and list."""
    from src.exchange.archive import _SYMBOL_IN_TITLE

    titles = [
        "Delisting of AERGOUSDT Perpetual Contract",
        "Delisting of XTER,SCA,TOKEN,HPOS10I,PUMPBTC,AFC,INTER,XAVA,AO",
        "Delisting of 1000000BABYDOGEUSDT Perpetual Contract",
        "Delisting of 4USDT Perpetual Contract",
    ]
    found: set[str] = set()
    for title in titles:
        found |= set(_SYMBOL_IN_TITLE.findall(title))

    assert "AERGOUSDT" in found
    assert "1000000BABYDOGEUSDT" in found
    assert "4USDT" in found


def test_regex_does_not_match_partial_words():
    """Word boundaries prevent matching the tail of a longer token."""
    from src.exchange.archive import _SYMBOL_IN_TITLE

    assert _SYMBOL_IN_TITLE.findall("see NOTAUSDTX for details") == []


def test_regex_does_not_match_other_quote_currencies():
    from src.exchange.archive import _SYMBOL_IN_TITLE

    assert _SYMBOL_IN_TITLE.findall("Delisting of BTCUSDC and ETHPERP") == []


def test_archive_parser_handles_every_sample_name():
    """The same list via HTML-listing parsing, not just the regex."""
    html = "".join(f'<a href="{s}/">{s}/</a>' for s in ARCHIVE_SAMPLE)
    assert parse_archive_listing(html) == set(ARCHIVE_SAMPLE)


def test_announcement_coverage_is_measured_not_assumed():
    catalog = SymbolCatalog(
        archive_symbols=frozenset({"AUSDT", "BUSDT", "CUSDT", "DUSDT", "LIVEUSDT"}),
        live_symbols=frozenset({"LIVEUSDT"}),
        fetched_at=datetime.now(UTC),
    )
    titles = ["Delisting of AUSDT Perpetual Contract", "Delisting of BUSDT,CUSDT"]

    hit, pct = catalog.coverage_by_announcements(titles)

    assert hit == 3
    assert pct == pytest.approx(75.0)


def test_coverage_ignores_names_that_were_never_delisted():
    catalog = SymbolCatalog(
        archive_symbols=frozenset({"AUSDT", "LIVEUSDT"}),
        live_symbols=frozenset({"LIVEUSDT"}),
        fetched_at=datetime.now(UTC),
    )
    hit, pct = catalog.coverage_by_announcements(["Delisting of LIVEUSDT", "Delisting of AUSDT"])

    assert hit == 1
    assert pct == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
@pytest.fixture
def catalog_with_cache(tmp_path, monkeypatch):
    settings = load_settings()
    config = settings.bybit.model_copy(deep=True)
    config.bootstrap_cache.path = str(tmp_path)
    return ArchiveCatalog(config), tmp_path


async def test_network_is_not_touched_when_cache_is_fresh(catalog_with_cache):
    """The archive probe is one-off: a repeat run must not hit the network."""
    catalog, tmp_path = catalog_with_cache

    (tmp_path / "symbol_catalog.json").write_text(
        json.dumps(
            {
                "archive_symbols": ["AUSDT", "BUSDT"],
                "live_symbols": ["AUSDT"],
                "fetched_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    async def explode(*_a, **_k):
        raise AssertionError("the network must not be used when the cache is fresh")

    catalog.fetch_archive_symbols = explode  # type: ignore[method-assign]

    result = await catalog.load(frozenset({"AUSDT"}))
    assert result.delisted == {"BUSDT"}


async def test_expired_cache_is_refetched(catalog_with_cache):
    catalog, tmp_path = catalog_with_cache

    (tmp_path / "symbol_catalog.json").write_text(
        json.dumps(
            {
                "archive_symbols": ["OLDUSDT"],
                "live_symbols": [],
                "fetched_at": (datetime.now(UTC) - timedelta(days=365)).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    async def fresh(*_a, **_k):
        return frozenset({"NEWUSDT"})

    catalog.fetch_archive_symbols = fresh  # type: ignore[method-assign]

    result = await catalog.load(frozenset())
    assert result.archive_symbols == {"NEWUSDT"}


async def test_refresh_flag_bypasses_fresh_cache(catalog_with_cache):
    catalog, tmp_path = catalog_with_cache

    (tmp_path / "symbol_catalog.json").write_text(
        json.dumps(
            {
                "archive_symbols": ["STALEUSDT"],
                "live_symbols": [],
                "fetched_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    async def fresh(*_a, **_k):
        return frozenset({"FORCEDUSDT"})

    catalog.fetch_archive_symbols = fresh  # type: ignore[method-assign]

    result = await catalog.load(frozenset(), refresh=True)
    assert result.archive_symbols == {"FORCEDUSDT"}


async def test_corrupt_cache_does_not_break_bootstrap(catalog_with_cache):
    catalog, tmp_path = catalog_with_cache
    (tmp_path / "symbol_catalog.json").write_text("{ not json", encoding="utf-8")

    async def fresh(*_a, **_k):
        return frozenset({"AUSDT"})

    catalog.fetch_archive_symbols = fresh  # type: ignore[method-assign]

    result = await catalog.load(frozenset())
    assert result.archive_symbols == {"AUSDT"}


async def test_cache_is_written_after_fetch(catalog_with_cache):
    catalog, tmp_path = catalog_with_cache

    async def fresh(*_a, **_k):
        return frozenset({"AUSDT", "BUSDT"})

    catalog.fetch_archive_symbols = fresh  # type: ignore[method-assign]
    await catalog.load(frozenset({"AUSDT"}))

    saved = json.loads((tmp_path / "symbol_catalog.json").read_text(encoding="utf-8"))
    assert sorted(saved["archive_symbols"]) == ["AUSDT", "BUSDT"]

"""Enumerating symbols via the public.bybit.com archive (DEC-006).

`instruments-info` serves only live instruments, so a universe built from it
carries survivorship bias: pairs that died during the backtest period do not
get into the sample at all.

The archive lists 991 USDT pairs versus 800 live ones. The exchange keeps
the full history for dead pairs — LUNAUSDT for March 2022 is served with
candles, OI and funding.

Measured 2026-08-07: parsing delisting announcements recovers only 252 of
302 dead pairs (83.4%), so announcements are used only as a cross-check,
not as a source.

The enumeration is one-off and cached to disk: a repeat run does not hit
the network.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from src.core.config import BybitConfig
from src.core.logging import get_logger

log = get_logger(__name__)

#: Directories in the archive listing. The directory name is the symbol.
_DIR_PATTERN = re.compile(r'href="([^"/]+)/"')

#: Symbols from announcement titles: "Delisting of AERGOUSDT Perpetual
#: Contract", "Delisting of XTER,SCA,TOKEN,...".
#:
#: A single base character is allowed: the exchange has pairs like 4USDT.
#: Requiring at least two lowered coverage by silently dropping them. Word
#: boundaries keep it from catching the tail of a longer word.
_SYMBOL_IN_TITLE = re.compile(r"\b([A-Z0-9]{1,18}USDT)\b")


@dataclass(frozen=True)
class SymbolCatalog:
    """Full symbol catalog: live and dead."""

    archive_symbols: frozenset[str]
    live_symbols: frozenset[str]
    fetched_at: datetime

    @property
    def delisted(self) -> frozenset[str]:
        """In the archive, not among the live ones."""
        return self.archive_symbols - self.live_symbols

    @property
    def all_symbols(self) -> frozenset[str]:
        return self.archive_symbols | self.live_symbols

    def coverage_by_announcements(self, announcement_titles: list[str]) -> tuple[int, float]:
        """How many dead pairs are recovered from announcements.

        The quantity for which the report states either "coverage not
        confirmed" or a concrete percentage. Computed on live data, not
        taken from memory.
        """
        dead = self.delisted
        if not dead:
            return 0, 0.0
        names: set[str] = set()
        for title in announcement_titles:
            names |= set(_SYMBOL_IN_TITLE.findall(title))
        hit = names & dead
        return len(hit), len(hit) / len(dead) * 100


def parse_archive_listing(html: str, *, quote: str = "USDT") -> frozenset[str]:
    """Extracts symbol names from the HTML directory listing."""
    found = {
        name
        for name in _DIR_PATTERN.findall(html)
        if name.endswith(quote) and not name.startswith(".")
    }
    return frozenset(found)


class ArchiveCatalog:
    """Reads the archive and caches the result to disk."""

    def __init__(self, config: BybitConfig) -> None:
        self._config = config
        self._cache_dir = config.bootstrap_cache.resolved_path()
        self._ttl = timedelta(hours=config.bootstrap_cache.ttl_hours)

    @property
    def _cache_file(self) -> Path:
        return self._cache_dir / "symbol_catalog.json"

    def _read_cache(self) -> SymbolCatalog | None:
        path = self._cache_file
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            fetched = datetime.fromisoformat(raw["fetched_at"])
        except (ValueError, KeyError, OSError) as exc:
            # A broken cache must not bring down the bootstrap — just re-read.
            log.warning("archive.cache_unreadable", path=str(path), error=str(exc))
            return None

        if datetime.now(UTC) - fetched > self._ttl:
            log.info("archive.cache_expired", age_hours=(datetime.now(UTC) - fetched).days * 24)
            return None

        return SymbolCatalog(
            archive_symbols=frozenset(raw["archive_symbols"]),
            live_symbols=frozenset(raw["live_symbols"]),
            fetched_at=fetched,
        )

    def _write_cache(self, catalog: SymbolCatalog) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_file.write_text(
            json.dumps(
                {
                    "archive_symbols": sorted(catalog.archive_symbols),
                    "live_symbols": sorted(catalog.live_symbols),
                    "fetched_at": catalog.fetched_at.isoformat(),
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        log.info(
            "archive.cached",
            path=str(self._cache_file),
            archive=len(catalog.archive_symbols),
            delisted=len(catalog.delisted),
        )

    async def fetch_archive_symbols(self, *, quote: str = "USDT") -> frozenset[str]:
        """A single request to the archive listing."""
        url = self._config.public_archive_url
        timeout = self._config.rate_limit.timeout_seconds
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers={"User-Agent": "crypto-bot/0.1"})
            response.raise_for_status()
        symbols = parse_archive_listing(response.text, quote=quote)
        if not symbols:
            # An empty result almost certainly means a markup change, not
            # the disappearance of all pairs. Silently returning nothing here
            # means quietly building a universe without delisted pairs (P-001).
            raise ValueError(
                f"archive {url} returned no symbols: has the markup changed?"
            )
        return symbols

    async def load(
        self, live_symbols: frozenset[str], *, refresh: bool = False
    ) -> SymbolCatalog:
        """Catalog from cache or from the network.

        `refresh=True` forces a re-read of the archive.
        """
        if not refresh:
            cached = self._read_cache()
            if cached is not None:
                log.info(
                    "archive.from_cache",
                    archive=len(cached.archive_symbols),
                    delisted=len(cached.delisted),
                )
                return cached

        archive = await self.fetch_archive_symbols()
        catalog = SymbolCatalog(
            archive_symbols=archive,
            live_symbols=live_symbols,
            fetched_at=datetime.now(UTC),
        )
        self._write_cache(catalog)
        return catalog

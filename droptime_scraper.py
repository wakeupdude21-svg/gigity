"""
droptime_scraper.py - Scrape upcoming Minecraft name drop times from external sources.

Sources:
  1. 3name.xyz (/list)  - 3-letter names with "dropping soon" status
  2. NameMC (per-name)  - individual name lookup via /search?q=<name>
  3. Bulk NameMC         - scrape the full dropping-names list via browser

Since NameMC's drop list is JS-rendered, we use two strategies:
  - For bulk: scrape 3name.xyz list (static HTML, gives names dropping soon)
  - For each name: hit NameMC /search?q=<name> to get drop time details
  - Results merged into droptimes.json for the sniper to consume
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import aiohttp
from bs4 import BeautifulSoup

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_DB_FILE = Path("droptimes.json")

_3NAME_LIST = "https://3name.xyz/list"
_NAMEMC_SEARCH = "https://namemc.com/search?q={name}"
_NAMEMC_PROFILE = "https://namemc.com/profile/{name}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_TIMEOUT = aiohttp.ClientTimeout(total=15)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DB helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _load_db() -> dict[str, Any]:
    if _DB_FILE.exists():
        try:
            with open(_DB_FILE, encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            pass
    return {"names": {}, "drops": {}}


def _save_db(db: dict[str, Any]) -> None:
    with open(_DB_FILE, "w", encoding="utf-8") as fh:
        json.dump(db, fh, indent=2, default=str)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Date parsing helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# NameMC profile pages show drop time like:
#   "Available Apr 21, 2026 • 9:31 PM ± 7.2 h"
#   "Available 4/21/2026 • 3:00 AM ± 2.5 h"
_NAMEMC_DATE_PATTERNS = [
    # "Apr 21, 2026 • 9:31:32 PM ± 7.2 h"
    re.compile(
        r"(\w{3}\s+\d{1,2},?\s+\d{4})\s*[.]*\s*"
        r"(\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M)"
        r"(?:\s*[+-]\s*([\d.]+)\s*h?)?"
    ),
    # "4/21/2026 • 9:31 PM ± 7.2 h"
    re.compile(
        r"(\d{1,2}/\d{1,2}/\d{4})\s*[.]*\s*"
        r"(\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M)"
        r"(?:\s*[+-]\s*([\d.]+)\s*h?)?"
    ),
]

_3NAME_DT_RE = re.compile(
    r"(\w+\s+\d{1,2},?\s+\d{4})\s+"
    r"(\d{1,2}:\d{2}\s*(?:[AP]M)?)"
)


def _parse_namemc_time(text: str) -> tuple[datetime | None, float]:
    """Try to parse a NameMC drop-time string."""
    for pat in _NAMEMC_DATE_PATTERNS:
        m = pat.search(text)
        if m:
            date_str = m.group(1)
            time_str = m.group(2).strip()
            window = float(m.group(3)) if m.group(3) else 0.0

            for fmt in (
                "%b %d, %Y %I:%M:%S %p", "%b %d %Y %I:%M:%S %p",
                "%b %d, %Y %I:%M %p", "%b %d %Y %I:%M %p",
                "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p",
            ):
                try:
                    dt = datetime.strptime(f"{date_str} {time_str}", fmt)
                    dt = dt.replace(tzinfo=timezone.utc)
                    return dt, window
                except ValueError:
                    continue
    return None, 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3name.xyz scraper (static HTML - works without JS)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def scrape_3name(
    session: aiohttp.ClientSession | None = None,
) -> list[dict]:
    """Scrape the 3name.xyz /list page for 'Dropping Soon' names.

    Returns list of {name, source} dicts (drop times are JS-rendered
    so we only get the name list here).
    """
    owns = session is None
    if owns:
        session = aiohttp.ClientSession()

    results: list[dict] = []

    try:
        async with session.get(_3NAME_LIST, headers=_HEADERS, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                print(f"[3name] HTTP {resp.status}")
                return results
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")

        for link in soup.select("a[href*='/name/']"):
            href = link.get("href", "")
            name = href.rstrip("/").split("/")[-1] if "/name/" in href else ""
            if name and 2 <= len(name) <= 5:
                if not any(r["name"] == name for r in results):
                    results.append({"name": name, "source": "3name"})

        print(f"[3name] {len(results)} dropping-soon names found")
        return results

    except Exception as exc:
        print(f"[3name] Error: {exc}")
        return results

    finally:
        if owns:
            await session.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# NameMC per-name lookup (via curl_cffi to bypass Cloudflare)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def lookup_namemc(name: str) -> dict | None:
    """Look up a single name on NameMC for drop time info.

    Uses curl_cffi to impersonate Chrome TLS fingerprint.
    Returns {name, drop_time, window_hours, source} or None.
    """
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        print("[namemc] curl_cffi not installed - pip install curl_cffi")
        return None

    async with AsyncSession(impersonate="chrome") as s:
        try:
            url = _NAMEMC_PROFILE.format(name=name)
            resp = await s.get(url, headers=_HEADERS, timeout=15)
            if resp.status_code != 200:
                return None

            html = resp.text
            soup = BeautifulSoup(html, "html.parser")

            # Look for "Available" text with a date
            page_text = soup.get_text(" ", strip=True)

            # Search for drop time patterns
            drop_dt, window = _parse_namemc_time(page_text)

            if drop_dt:
                return {
                    "name": name,
                    "drop_time": drop_dt,
                    "window_hours": window,
                    "source": "namemc",
                }

            # Also check for <time> elements
            for te in soup.select("time[datetime]"):
                dt_str = te.get("datetime", "")
                if dt_str:
                    try:
                        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                        return {
                            "name": name,
                            "drop_time": dt.replace(tzinfo=timezone.utc),
                            "window_hours": 0,
                            "source": "namemc",
                        }
                    except (ValueError, TypeError):
                        pass

        except Exception as exc:
            print(f"[namemc] Error looking up '{name}': {exc}")

    return None


async def lookup_namemc_batch(
    names: list[str],
    delay: float = 1.5,
) -> list[dict]:
    """Look up multiple names on NameMC with rate limiting.

    Returns list of {name, drop_time, window_hours, source} dicts.
    """
    results = []
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        print("[namemc] curl_cffi not installed - pip install curl_cffi")
        return results

    async with AsyncSession(impersonate="chrome") as s:
        for i, name in enumerate(names):
            try:
                url = _NAMEMC_PROFILE.format(name=name)
                resp = await s.get(url, headers=_HEADERS, timeout=15)

                if resp.status_code == 429:
                    print(f"[namemc] Rate limited at {i}/{len(names)}, waiting 30s...")
                    await asyncio.sleep(30)
                    continue

                if resp.status_code != 200:
                    continue

                html = resp.text
                soup = BeautifulSoup(html, "html.parser")
                page_text = soup.get_text(" ", strip=True)

                drop_dt, window = _parse_namemc_time(page_text)
                if drop_dt:
                    results.append({
                        "name": name,
                        "drop_time": drop_dt,
                        "window_hours": window,
                        "source": "namemc",
                    })
                    print(f"[namemc] {name} -> drops {drop_dt.strftime('%Y-%m-%d %H:%M UTC')} (+/-{window}h)")
                else:
                    # Check <time> elements
                    for te in soup.select("time[datetime]"):
                        dt_str = te.get("datetime", "")
                        if dt_str:
                            try:
                                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                                results.append({
                                    "name": name,
                                    "drop_time": dt.replace(tzinfo=timezone.utc),
                                    "window_hours": 0,
                                    "source": "namemc",
                                })
                                print(f"[namemc] {name} -> drops {dt.strftime('%Y-%m-%d %H:%M UTC')}")
                                break
                            except (ValueError, TypeError):
                                pass

            except Exception as exc:
                print(f"[namemc] Error for '{name}': {exc}")

            if (i + 1) % 10 == 0:
                print(f"[namemc] Progress: {i+1}/{len(names)} checked, {len(results)} drops found")

            await asyncio.sleep(delay)

    print(f"[namemc] Batch done: {len(results)} drops from {len(names)} names")
    return results


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Unified scrape + merge
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def scrape_all_droptimes(
    session: aiohttp.ClientSession | None = None,
) -> dict[str, dict]:
    """Scrape 3name for dropping-soon names, then look up each on NameMC.

    Flow:
      1. Get list of dropping names from 3name.xyz
      2. For each name, query NameMC profile for exact drop time
      3. Merge into droptimes.json
    """
    owns = session is None
    if owns:
        session = aiohttp.ClientSession()

    try:
        # Step 1: Get dropping names from 3name
        three_name_results = await scrape_3name(session)
        dropping_names = [r["name"] for r in three_name_results]

        # Step 2: Look up each on NameMC for drop times
        namemc_results = []
        if dropping_names:
            print(f"[dropscrape] Looking up {len(dropping_names)} names on NameMC...")
            namemc_results = await lookup_namemc_batch(dropping_names, delay=1.5)

        # Step 3: Merge into DB
        db = _load_db()
        added = 0
        updated = 0
        now_iso = datetime.now(timezone.utc).isoformat()

        # First, add all 3name names as "dropping_soon" in the names tracker
        for entry in three_name_results:
            name = entry["name"]
            if name not in db["names"]:
                db["names"][name] = {
                    "uuid": "",
                    "last_checked": now_iso,
                    "status": "dropping_soon",
                    "source": "3name",
                }

        # Then merge any NameMC drop times
        for entry in namemc_results:
            name = entry["name"]
            drop_dt = entry["drop_time"]
            drop_iso = drop_dt.isoformat() if isinstance(drop_dt, datetime) else str(drop_dt)

            existing = db["drops"].get(name)
            if existing:
                old_window = existing.get("window_hours", 999)
                new_window = entry.get("window_hours", 0)
                if new_window < old_window:
                    db["drops"][name] = {
                        "drop_time": drop_iso,
                        "detected_at": now_iso,
                        "old_holder_uuid": existing.get("old_holder_uuid", ""),
                        "status": "scraped",
                        "source": entry["source"],
                        "window_hours": new_window,
                    }
                    updated += 1
            else:
                db["drops"][name] = {
                    "drop_time": drop_iso,
                    "detected_at": now_iso,
                    "old_holder_uuid": "",
                    "status": "scraped",
                    "source": entry["source"],
                    "window_hours": entry.get("window_hours", 0),
                }
                added += 1

            db["names"][name] = {
                "uuid": "",
                "last_checked": now_iso,
                "status": "dropping",
                "source": entry["source"],
            }

        # Also add 3name names without NameMC data as estimates
        for entry in three_name_results:
            name = entry["name"]
            if name not in db["drops"]:
                # No exact time, but we know it's dropping soon
                # Don't add to drops without a time - just mark in names
                pass

        _save_db(db)
        print(
            f"[dropscrape] Done: {added} new, {updated} updated, "
            f"{len(db['drops'])} total in DB, "
            f"{len(dropping_names)} names from 3name"
        )
        return db["drops"]

    finally:
        if owns:
            await session.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Background auto-scraper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_DEFAULT_INTERVAL = 600  # 10 min


class DropTimeScraper:
    """Background task that periodically scrapes for drop times."""

    def __init__(
        self,
        interval: float = _DEFAULT_INTERVAL,
        on_new_drop: Any | None = None,
    ):
        self.interval = interval
        self.on_new_drop = on_new_drop  # async callback(name, drop_info)
        self.running = False
        self._task: asyncio.Task | None = None
        self.last_scrape: float = 0
        self.total_scraped = 0

    def start(self) -> asyncio.Task:
        self.running = True
        self._task = asyncio.create_task(self._loop())
        return self._task

    def stop(self) -> None:
        self.running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _loop(self) -> None:
        print(f"[dropscrape] Auto-scraper started (interval={self.interval}s)")
        while self.running:
            try:
                db_before = _load_db()
                old_names = set(db_before.get("drops", {}).keys())

                drops = await scrape_all_droptimes()
                self.last_scrape = time.time()
                self.total_scraped += len(drops)

                new_names = set(drops.keys()) - old_names
                if new_names and self.on_new_drop:
                    for name in new_names:
                        try:
                            await self.on_new_drop(name, drops[name])
                        except Exception as exc:
                            print(f"[dropscrape] Callback error for '{name}': {exc}")

                if new_names:
                    print(f"[dropscrape] {len(new_names)} NEW drops: {', '.join(sorted(new_names))}")

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[dropscrape] Scrape cycle error: {exc}")

            await asyncio.sleep(self.interval)

    def get_stats(self) -> dict:
        age = int(time.time() - self.last_scrape) if self.last_scrape else -1
        db = _load_db()
        return {
            "running": self.running,
            "total_scraped": self.total_scraped,
            "last_scrape_ago": f"{age}s" if age >= 0 else "never",
            "interval": self.interval,
            "drops_in_db": len(db.get("drops", {})),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLI test
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _test():
    print("=== Scraping drop times ===")
    print()
    drops = await scrape_all_droptimes()
    print()
    if drops:
        for name, info in sorted(drops.items(), key=lambda x: x[1].get("drop_time", "")):
            dt = info.get("drop_time", "?")
            src = info.get("source", "?")
            win = info.get("window_hours", 0)
            print(f"  {name:>8s}  |  {dt}  |  +/-{win}h  |  {src}")
        print(f"\nTotal: {len(drops)} drops")
    else:
        print("No drops with exact times found.")
        print("3name names are tracked in droptimes.json 'names' section.")
        db = _load_db()
        dropping = [n for n, v in db.get("names", {}).items() if v.get("status") in ("dropping_soon", "dropping")]
        if dropping:
            print(f"Dropping soon ({len(dropping)}): {', '.join(sorted(dropping))}")


if __name__ == "__main__":
    asyncio.run(_test())

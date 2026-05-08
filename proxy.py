"""
proxy.py — Free proxy scraper and rotator for Mojang API polling.

Scrapes HTTP proxies from public free-proxy lists, validates them
against Mojang's API, and provides round-robin rotation so we can
poll thousands of usernames without getting 429'd.
"""

from __future__ import annotations

import asyncio
import random
import time

import aiohttp

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Free proxy list sources (HTTP, one ip:port per line)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_PROXY_SOURCES = [
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=all",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
]

_MOJANG_TEST_URL = "https://api.mojang.com/users/profiles/minecraft/Notch"
_TEST_TIMEOUT = aiohttp.ClientTimeout(total=8)
_MAX_FAILURES = 5          # drop proxy after N consecutive failures
_SCRAPE_COOLDOWN = 1800    # re-scrape every 30 min


class ProxyPool:
    """Round-robin proxy pool with auto-scraping and dead-proxy eviction."""

    def __init__(self):
        self.proxies: list[str] = []
        self._failures: dict[str, int] = {}
        self._idx = 0
        self._last_scrape: float = 0.0

    # ── Public API ───────────────────────────────────────────

    @property
    def count(self) -> int:
        return len(self.proxies)

    def get_next(self) -> str | None:
        """Return the next proxy URL (round-robin), or None if pool empty."""
        if not self.proxies:
            return None
        proxy = self.proxies[self._idx % len(self.proxies)]
        self._idx += 1
        return proxy

    def report_success(self, proxy: str) -> None:
        self._failures[proxy] = 0

    def report_failure(self, proxy: str) -> None:
        self._failures[proxy] = self._failures.get(proxy, 0) + 1
        if self._failures[proxy] >= _MAX_FAILURES:
            self._remove(proxy)

    def _remove(self, proxy: str) -> None:
        if proxy in self.proxies:
            self.proxies.remove(proxy)
            self._failures.pop(proxy, None)
            print(f"[proxy] Removed dead proxy ({self.count} left)")

    # ── Scrape + validate ────────────────────────────────────

    async def scrape_and_validate(self, max_test: int = 80) -> int:
        """Scrape free proxy lists, validate a subset against Mojang."""
        raw: set[str] = set()

        async with aiohttp.ClientSession() as sess:
            for url in _PROXY_SOURCES:
                try:
                    async with sess.get(url, timeout=aiohttp.ClientTimeout(total=12)) as r:
                        if r.status == 200:
                            for line in (await r.text()).splitlines():
                                line = line.strip()
                                if line and ":" in line and not line.startswith("#"):
                                    if not line.startswith("http"):
                                        line = f"http://{line}"
                                    raw.add(line)
                except Exception as exc:
                    print(f"[proxy] Source error ({url[:40]}…): {exc}")

        print(f"[proxy] Scraped {len(raw)} raw proxies")

        # Only test proxies we don't already have
        candidates = list(raw - set(self.proxies))
        random.shuffle(candidates)
        candidates = candidates[:max_test]

        if not candidates:
            self._last_scrape = time.time()
            return 0

        tasks = [self._test_one(p) for p in candidates]
        results = await asyncio.gather(*tasks)

        added = 0
        for proxy, ok in zip(candidates, results):
            if ok and proxy not in self.proxies:
                self.proxies.append(proxy)
                added += 1

        self._last_scrape = time.time()
        print(f"[proxy] +{added} valid proxies  (pool: {self.count})")
        return added

    async def _test_one(self, proxy: str) -> bool:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(_MOJANG_TEST_URL, proxy=proxy, timeout=_TEST_TIMEOUT) as r:
                    return r.status in (200, 204, 404)
        except Exception:
            return False

    async def ensure_fresh(self) -> None:
        """Re-scrape if the pool is empty or stale."""
        if not self.proxies or (time.time() - self._last_scrape > _SCRAPE_COOLDOWN):
            await self.scrape_and_validate()

    # ── File-based loading ───────────────────────────────────

    def load_file(self, path: str) -> int:
        """Load proxies from a local file (one per line)."""
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if not line.startswith("http"):
                            line = f"http://{line}"
                        if line not in self.proxies:
                            self.proxies.append(line)
            return self.count
        except FileNotFoundError:
            return 0

    # ── Summary ──────────────────────────────────────────────

    def status_text(self) -> str:
        alive = self.count
        failing = sum(1 for v in self._failures.values() if v > 0)
        age = int(time.time() - self._last_scrape) if self._last_scrape else -1
        age_s = f"{age}s ago" if age >= 0 else "never"
        return f"Alive: {alive}  |  Failing: {failing}  |  Last scrape: {age_s}"

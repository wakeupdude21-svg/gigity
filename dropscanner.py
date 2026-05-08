"""
dropscanner.py — Continuous Mojang poller for drop-time detection.

How it works
────────────
1.  Build a database of name → UUID for every tracked username.
2.  Continuously cycle through the list, hitting Mojang's profile API.
3.  When a name goes from 200→404 (holder changed away), the name enters
    the 37-day cooldown.  We record  drop_time = detection_time + 37 days.
4.  When a name is within minutes of its drop time, hand off to the
    sniper core for precise millisecond-level claiming.

All requests rotate through ProxyPool to dodge rate-limits.
Detected drop times persist to droptimes.json across restarts.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Awaitable

import aiohttp

from proxy import ProxyPool
from scrapercheck import (
    CheckResult,
    NameStatus,
    check_availability,
    check_mojang_profile,
    is_name_available,
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_MOJANG_PROFILE_URL = "https://api.mojang.com/users/profiles/minecraft/{name}"
_DROP_DAYS = 37                      # Mojang cooldown
_DB_FILE = Path("droptimes.json")
_REQ_TIMEOUT = aiohttp.ClientTimeout(total=8)
_RATE_LIMIT_WAIT = 5                 # seconds to sleep on 429
_BETWEEN_REQUESTS = 0.15             # seconds between requests (no proxy)
_BETWEEN_REQUESTS_PROXY = 0.05       # seconds between requests (with proxy)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Persistence helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _load_db() -> dict[str, Any]:
    """Load the drop-time database from disk."""
    if _DB_FILE.exists():
        try:
            with open(_DB_FILE, encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            pass
    return {"names": {}, "drops": {}}


def _save_db(db: dict[str, Any]) -> None:
    """Persist the database."""
    with open(_DB_FILE, "w", encoding="utf-8") as fh:
        json.dump(db, fh, indent=2, default=str)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DropScanner
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class DropScanner:
    """Continuously polls Mojang to detect name changes and predict drops.

    Parameters
    ----------
    proxy_pool : ProxyPool
        Rotating proxy pool (can be empty — falls back to direct).
    bearer_token : str | None
        MSA bearer token for the authenticated availability endpoint.
    on_drop_detected : callback(name, drop_info_dict) | None
        Fired the moment a new drop time is calculated.
    on_available_now : callback(name) | None
        Fired if a tracked name is already available RIGHT NOW.
    """

    def __init__(
        self,
        proxy_pool: ProxyPool,
        bearer_token: str | None = None,
        on_drop_detected: Callable[[str, dict], Awaitable[None]] | None = None,
        on_available_now: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.pool = proxy_pool
        self.bearer = bearer_token
        self.on_drop_detected = on_drop_detected
        self.on_available_now = on_available_now

        self.db = _load_db()          # {"names": {name: {uuid, last_checked}}, "drops": {name: {...}}}
        self.running = False
        self._task: asyncio.Task | None = None

        # Stats
        self.total_checked = 0
        self.cycle_count = 0
        self.drops_found = 0

    # ── Lifecycle ────────────────────────────────────────────

    def start(self, names: list[str]) -> asyncio.Task:
        """Start scanning the given name list in the background."""
        self.running = True
        self._task = asyncio.create_task(self._run_loop(names))
        return self._task

    def stop(self) -> None:
        """Stop the scanner gracefully."""
        self.running = False
        if self._task and not self._task.done():
            self._task.cancel()

    # ── Main loop ────────────────────────────────────────────

    async def _run_loop(self, names: list[str]) -> None:
        print(f"[dropscan] Starting — {len(names)} names to track")

        while self.running:
            # Ensure we have proxies
            await self.pool.ensure_fresh()

            cycle_start = time.time()
            self.cycle_count += 1
            found_this_cycle = 0

            async with aiohttp.ClientSession() as session:
                for i, name in enumerate(names):
                    if not self.running:
                        break

                    try:
                        result = await self._check_one(session, name)
                        self.total_checked += 1

                        if result == "drop_detected":
                            found_this_cycle += 1
                        elif result == "available_now":
                            found_this_cycle += 1

                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        print(f"[dropscan] Error checking '{name}': {exc}")

                    # Throttle
                    delay = _BETWEEN_REQUESTS_PROXY if self.pool.count > 0 else _BETWEEN_REQUESTS
                    await asyncio.sleep(delay)

                    # Progress log
                    if (i + 1) % 500 == 0:
                        elapsed = time.time() - cycle_start
                        rate = (i + 1) / elapsed if elapsed > 0 else 0
                        print(
                            f"[dropscan] Cycle {self.cycle_count}: "
                            f"{i+1}/{len(names)}  "
                            f"({rate:.1f}/s)  "
                            f"drops found: {found_this_cycle}  "
                            f"proxies: {self.pool.count}"
                        )

            cycle_time = time.time() - cycle_start
            print(
                f"[dropscan] Cycle {self.cycle_count} done — "
                f"{len(names)} names in {cycle_time:.0f}s  "
                f"({len(names)/cycle_time:.1f}/s)  "
                f"drops: {found_this_cycle}"
            )

            _save_db(self.db)

    # ── Single name check ────────────────────────────────────

    async def _check_one(self, session: aiohttp.ClientSession, name: str) -> str:
        """Check a single name against Mojang.  Returns status string:
        'taken', 'drop_detected', 'available_now', 'error', 'rate_limited'
        """
        proxy = self.pool.get_next()
        url = _MOJANG_PROFILE_URL.format(name=name)
        now_str = datetime.now(timezone.utc).isoformat()

        try:
            kwargs: dict[str, Any] = {"timeout": _REQ_TIMEOUT}
            if proxy:
                kwargs["proxy"] = proxy

            async with session.get(url, **kwargs) as resp:
                status = resp.status

                if status == 429:
                    if proxy:
                        self.pool.report_failure(proxy)
                    await asyncio.sleep(_RATE_LIMIT_WAIT)
                    return "rate_limited"

                if proxy:
                    self.pool.report_success(proxy)

                if status == 200:
                    data = await resp.json()
                    current_uuid = data.get("id", "")
                    return await self._handle_taken(name, current_uuid, now_str)

                elif status == 404:
                    return await self._handle_not_found(name, session, now_str)

                else:
                    return "error"

        except Exception as exc:
            if proxy:
                self.pool.report_failure(proxy)
            return "error"

    async def _handle_taken(self, name: str, uuid: str, now_str: str) -> str:
        """Profile returned 200 — name is currently held."""
        prev = self.db["names"].get(name, {})
        prev_uuid = prev.get("uuid", "")

        # Update DB
        self.db["names"][name] = {"uuid": uuid, "last_checked": now_str, "status": "taken"}

        if prev_uuid and prev_uuid != uuid:
            # UUID changed — someone else now holds this name.
            # The OLD holder changed away, name went through cooldown,
            # and a new person already claimed it.  We missed the drop.
            print(f"[dropscan] ⚠ '{name}' holder changed {prev_uuid[:8]}→{uuid[:8]} (missed drop)")

        return "taken"

    async def _handle_not_found(self, name: str, session: aiohttp.ClientSession, now_str: str) -> str:
        """Profile returned 404 — name is either free or in 37-day cooldown."""
        prev = self.db["names"].get(name, {})
        prev_uuid = prev.get("uuid", "")
        prev_status = prev.get("status", "")

        # If we already detected this drop, skip
        if name in self.db["drops"]:
            return "already_tracked"

        # Try the authenticated endpoint to distinguish FREE vs COOLDOWN
        if self.bearer:
            try:
                result = await check_availability(session, name, self.bearer)

                if result.status == NameStatus.AVAILABLE:
                    # Name is FREE right now!
                    self.db["names"][name] = {"uuid": "", "last_checked": now_str, "status": "available"}
                    print(f"[dropscan] ✓ '{name}' is AVAILABLE NOW!")

                    if self.on_available_now:
                        await self.on_available_now(name)
                    return "available_now"

                elif result.status == NameStatus.DUPLICATE:
                    # 404 on profile but DUPLICATE on availability = IN COOLDOWN
                    # The holder changed away, name is in 37-day hold
                    now_dt = datetime.now(timezone.utc)
                    drop_time = now_dt + timedelta(days=_DROP_DAYS)

                    drop_info = {
                        "drop_time": drop_time.isoformat(),
                        "detected_at": now_dt.isoformat(),
                        "old_holder_uuid": prev_uuid,
                        "status": "cooldown",
                    }
                    self.db["drops"][name] = drop_info
                    self.db["names"][name] = {"uuid": "", "last_checked": now_str, "status": "cooldown"}
                    self.drops_found += 1

                    print(
                        f"\n[dropscan] 🎯 DROP DETECTED: '{name}'\n"
                        f"           Drop time: {drop_time.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
                        f"           Old holder: {prev_uuid[:8] if prev_uuid else 'unknown'}…\n"
                    )

                    _save_db(self.db)

                    if self.on_drop_detected:
                        await self.on_drop_detected(name, drop_info)
                    return "drop_detected"

                elif result.status == NameStatus.NOT_ALLOWED:
                    self.db["names"][name] = {"uuid": "", "last_checked": now_str, "status": "blocked"}
                    return "blocked"

            except Exception as exc:
                print(f"[dropscan] Auth check error for '{name}': {exc}")

        # No bearer token — just record the 404
        # If previously taken, this is LIKELY a drop but we can't confirm cooldown
        if prev_status == "taken" and prev_uuid:
            now_dt = datetime.now(timezone.utc)
            drop_time = now_dt + timedelta(days=_DROP_DAYS)

            drop_info = {
                "drop_time": drop_time.isoformat(),
                "detected_at": now_dt.isoformat(),
                "old_holder_uuid": prev_uuid,
                "status": "cooldown_estimated",
            }
            self.db["drops"][name] = drop_info
            self.db["names"][name] = {"uuid": "", "last_checked": now_str, "status": "cooldown"}
            self.drops_found += 1

            print(
                f"\n[dropscan] 🎯 DROP DETECTED (estimated): '{name}'\n"
                f"           Drop time: ~{drop_time.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
            )
            _save_db(self.db)

            if self.on_drop_detected:
                await self.on_drop_detected(name, drop_info)
            return "drop_detected"

        # First time seeing a 404 — name might have never been taken
        self.db["names"][name] = {"uuid": "", "last_checked": now_str, "status": "not_found"}
        return "not_found"

    # ── Query helpers ────────────────────────────────────────

    def get_drops(self) -> dict[str, dict]:
        """Return all detected drops with countdowns."""
        now = datetime.now(timezone.utc)
        result = {}
        for name, info in self.db.get("drops", {}).items():
            drop_dt = datetime.fromisoformat(info["drop_time"])
            remaining = (drop_dt - now).total_seconds()
            result[name] = {
                **info,
                "drop_dt": drop_dt,
                "remaining_seconds": remaining,
                "remaining_human": self._format_remaining(remaining),
                "dropped": remaining <= 0,
            }
        return result

    @staticmethod
    def _format_remaining(seconds: float) -> str:
        if seconds <= 0:
            return "DROPPED!"
        d = int(seconds // 86400)
        h = int((seconds % 86400) // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        if d > 0:
            return f"{d}d {h}h {m}m"
        if h > 0:
            return f"{h}h {m}m {s}s"
        return f"{m}m {s}s"

    def get_stats(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "total_checked": self.total_checked,
            "cycles": self.cycle_count,
            "drops_found": self.drops_found,
            "tracked_names": len(self.db.get("names", {})),
            "active_drops": len(self.db.get("drops", {})),
            "proxies": self.pool.count,
        }

    def add_drop_manual(self, name: str, drop_time: datetime) -> None:
        """Manually add a known drop time (e.g. from NameMC)."""
        self.db["drops"][name] = {
            "drop_time": drop_time.isoformat(),
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "old_holder_uuid": "",
            "status": "manual",
        }
        _save_db(self.db)

    def remove_drop(self, name: str) -> bool:
        """Remove a tracked drop."""
        if name in self.db.get("drops", {}):
            del self.db["drops"][name]
            _save_db(self.db)
            return True
        return False

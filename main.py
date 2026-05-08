from __future__ import annotations

import sys, os as _os
_os.environ["PYTHONIOENCODING"] = "utf-8"
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

"""
main.py — Fully automated MSA-authenticated Minecraft username sniper bot.

Architecture:
  • Authenticates via msauth.py ONCE at startup (token cached 23 h — no re-auth)
  • Discord bot is the sole interface — zero interactive prompts after launch
  • Single shared aiohttp.ClientSession created on on_ready, reused for all ops
  • All commands respond with rich Discord embeds

Speed optimisations:
  1. Single persistent TCPConnector session — reuses TCP connections
  2. DNS pre-resolution at startup — no per-request DNS lookup
  3. Busy-wait spin-loop for sub-ms fire-time accuracy
  4. N concurrent async workers all firing simultaneously
  5. time.perf_counter_ns() — nanosecond clock, zero jitter
  6. TCP warm-up HEAD request 10 s before scheduled drop
  7. Zero I/O / zero allocations inside the hot PUT path
  8. Auto delay tuning after every timed snipe

Commands:
  !snipe <name> [drop_time_iso] [delay_ms] [workers]
  !autosnipe <name> [delay_ms] [workers]
  !superfastsnipe <name>
  !scrapetime <name>
  !cancel <name>
  !status
  !setdelay [ms]
  !help
"""


import asyncio
import json
import os
import socket
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import TCPConnector

import msauth
import scraper
from scrapercheck import NameStatus
from proxy import ProxyPool
from dropscanner import DropScanner
from droptime_scraper import DropTimeScraper, scrape_all_droptimes, scrape_3name, lookup_namemc_batch
import namelist

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_MC_NAME_CHANGE_URL = "https://api.minecraftservices.com/minecraft/profile/name/{name}"
_MC_SERVICES_HOST   = "api.minecraftservices.com"
_CONFIG_FILE        = Path("config.json")
_SNIPED_LOG         = Path("sniped.txt")
_SUPER_WORKERS      = 10   # fixed worker count for !superfastsnipe

# Embed accent colours
_CLR_GREEN  = 0x2ECC71
_CLR_RED    = 0xE74C3C
_CLR_BLUE   = 0x3498DB
_CLR_GOLD   = 0xF1C40F
_CLR_PURPLE = 0x9B59B6
_CLR_GREY   = 0x95A5A6


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Config
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _load_config() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "delay": 0,
        "workers": 5,
        "poll_interval": 30,
        "webhook_url": "",
        "discord_token": "",
        "account_type": "ms",
    }
    # 1. Load from config.json if it exists (local dev)
    if _CONFIG_FILE.exists():
        try:
            with open(_CONFIG_FILE, encoding="utf-8") as fh:
                defaults.update(json.load(fh))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[config] Warning: {exc}")

    # 2. Environment variables override config.json (Railway / hosting)
    _env_map = {
        "DISCORD_BOT_TOKEN":    "discord_token",
        "DISCORD_WEBHOOK_URL":  "webhook_url",
        "ACCOUNT_TYPE":         "account_type",
        "AZURE_CLIENT_ID":      "azure_client_id",
        "SNIPE_DELAY":          "delay",
        "SNIPE_WORKERS":        "workers",
    }
    for env_key, cfg_key in _env_map.items():
        val = os.environ.get(env_key, "")
        if val:
            # Cast numeric values
            if cfg_key in ("delay",):
                defaults[cfg_key] = float(val)
            elif cfg_key in ("workers",):
                defaults[cfg_key] = int(val)
            else:
                defaults[cfg_key] = val

    return defaults


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Low-level helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _resolve_host(host: str) -> str:
    """Resolve DNS once at startup to skip per-request lookups."""
    try:
        ip = socket.gethostbyname(host)
        print(f"[dns] {host} → {ip}")
        return ip
    except socket.gaierror:
        print(f"[dns] Resolution failed for {host} — using hostname directly")
        return host


def _busy_wait_until_ns(target_ns: int) -> None:
    """Coarse sleep + spin-loop for sub-ms fire-time accuracy.

    Phase 1 — asyncio.sleep has ~1–15 ms jitter, so we sleep until
               200 ms before target to save CPU.
    Phase 2 — tight spin-loop; burns one core but guarantees <0.1 ms
               accuracy at the actual fire moment.
    """
    sleep_ns = target_ns - time.perf_counter_ns() - 200_000_000
    if sleep_ns > 0:
        time.sleep(sleep_ns / 1_000_000_000)
    while time.perf_counter_ns() < target_ns:
        pass


async def _warmup(session: aiohttp.ClientSession, name: str) -> None:
    """HEAD request to complete the TCP+TLS handshake before the fire."""
    url = _MC_NAME_CHANGE_URL.format(name=name)
    try:
        async with session.head(url) as resp:
            print(f"[warmup] HEAD → {resp.status}")
    except Exception as exc:
        print(f"[warmup] {exc}")


def _log_snipe(name: str, summary: dict) -> None:
    """Append a successful snipe to sniped.txt."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = (
        f"[{ts}]  {name}  |  latency={summary.get('latency_min', 0):.2f}ms"
        f"  worker={summary.get('winner_worker', '?')}\n"
    )
    with open(_SNIPED_LOG, "a", encoding="utf-8") as fh:
        fh.write(line)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Snipe worker (hot path — zero I/O, zero alloc except the PUT)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _snipe_worker(
    worker_id: int,
    session: aiohttp.ClientSession,
    name: str,
    bearer_token: str,
    fire_ns: int,
    results: list[dict],
    success_event: asyncio.Event,
) -> None:
    url = _MC_NAME_CHANGE_URL.format(name=name)
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
    }

    _busy_wait_until_ns(fire_ns)

    # ── HOT PATH ─────────────────────────────────────────────
    t0 = time.perf_counter_ns()
    try:
        async with session.put(url, headers=headers) as resp:
            t1 = time.perf_counter_ns()
            status = resp.status
            body   = await resp.text()
    except Exception as exc:
        t1     = time.perf_counter_ns()
        status = -1
        body   = str(exc)
    # ── END HOT PATH ─────────────────────────────────────────

    latency_ms    = (t1 - t0) / 1_000_000
    fire_time_utc = datetime.now(timezone.utc)

    results.append({
        "worker_id":    worker_id,
        "status":       status,
        "latency_ms":   latency_ms,
        "fire_time_utc": fire_time_utc,
        "body":         body,
    })
    print(f"  [w{worker_id}] {status}  {latency_ms:.2f}ms")

    if status == 200 and not success_event.is_set():
        success_event.set()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Snipe orchestrator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def run_snipe(
    name: str,
    bearer_token: str,
    drop_time: datetime | None = None,
    delay_ms: float = 0.0,
    workers: int = 5,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    """Orchestrate *workers* concurrent PUT attempts to claim *name*.

    Returns a summary dict with latency stats, success flag, and
    auto-tuned delay recommendation for the next attempt.
    """
    owns_session = session is None
    if owns_session:
        connector = TCPConnector(limit=workers, ttl_dns_cache=0, enable_cleanup_closed=True)
        session = aiohttp.ClientSession(connector=connector)

    try:
        # Warm up connection pool 10 s before the drop
        if drop_time is not None:
            seconds_until = (drop_time - datetime.now(timezone.utc)).total_seconds()
            lead = max(seconds_until - 10.0, 0.0)
            if lead > 0:
                print(f"[snipe] Sleeping {lead:.1f}s → then warming up…")
                await asyncio.sleep(lead)
            await _warmup(session, name)

        # Compute the absolute fire timestamp in perf_counter_ns
        if drop_time is not None:
            delta_s = (drop_time - datetime.now(timezone.utc)).total_seconds()
            fire_ns = (
                time.perf_counter_ns()
                + int(delta_s * 1_000_000_000)
                - int(delay_ms * 1_000_000)   # subtract delay so we fire early
            )
        else:
            fire_ns = time.perf_counter_ns()   # immediate

        print(f"[snipe] Firing {workers} workers for '{name}'…")

        results:       list[dict]  = []
        success_event: asyncio.Event = asyncio.Event()

        tasks = [
            asyncio.create_task(
                _snipe_worker(i, session, name, bearer_token, fire_ns, results, success_event)
            )
            for i in range(workers)
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

        latencies = [r["latency_ms"] for r in results if r["status"] != -1]
        success   = any(r["status"] == 200 for r in results)
        winner    = next((r for r in results if r["status"] == 200), None)

        summary: dict[str, Any] = {
            "name":          name,
            "success":       success,
            "attempts":      len(results),
            "latency_min":   min(latencies)              if latencies else 0.0,
            "latency_mean":  statistics.mean(latencies)  if latencies else 0.0,
            "latency_max":   max(latencies)              if latencies else 0.0,
            "winner_worker": winner["worker_id"]         if winner else None,
            "input_delay_ms": delay_ms,
            "tuned_delay_ms": delay_ms,
            "results":       results,
        }

        # Auto-tune: measure how far off the actual fire time was
        if drop_time is not None and results:
            actual_offset_ms = (results[0]["fire_time_utc"] - drop_time).total_seconds() * 1000
            summary["actual_offset_ms"] = actual_offset_ms
            summary["tuned_delay_ms"]   = delay_ms + actual_offset_ms
            print(f"[snipe] offset={actual_offset_ms:+.1f}ms  tuned_delay={summary['tuned_delay_ms']:.1f}ms")

        if success:
            print(f"[snipe] ✓ Claimed '{name}' — latency {summary['latency_min']:.2f}ms")
            _log_snipe(name, summary)
            print("\a", end="", flush=True)
        else:
            print(f"[snipe] ✗ Missed '{name}'")

        return summary

    finally:
        if owns_session:
            await session.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Discord (requires discord.py)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_discord_available = False
try:
    import discord
    from discord.ext import commands
    _discord_available = True
except ImportError:
    pass


# ── Embed builders ───────────────────────────────────────────

def _embed_snipe_result(name: str, summary: dict[str, Any]) -> "discord.Embed":
    success = summary["success"]
    outcome = "Claimed" if success else "Missed"
    embed = discord.Embed(
        title=f"{outcome}  —  {name}",
        color=_CLR_GREEN if success else _CLR_RED,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Latency  (min / avg / max)",
        value=(
            f"`{summary.get('latency_min', 0):.2f}` / "
            f"`{summary.get('latency_mean', 0):.2f}` / "
            f"`{summary.get('latency_max', 0):.2f}` ms"
        ),
        inline=False,
    )
    embed.add_field(name="Workers",    value=str(summary.get("attempts", 0)),                inline=True)
    embed.add_field(name="Winner",     value=f"Worker {summary.get('winner_worker', '—')}",  inline=True)
    embed.add_field(name="Delay",      value=f"{summary.get('input_delay_ms', 0):.1f} ms",   inline=True)
    if summary.get("tuned_delay_ms") != summary.get("input_delay_ms"):
        embed.add_field(
            name="Tuned Delay",
            value=f"{summary['tuned_delay_ms']:.1f} ms",
            inline=True,
        )
    if "actual_offset_ms" in summary:
        embed.add_field(
            name="Fire Offset",
            value=f"{summary['actual_offset_ms']:+.2f} ms",
            inline=True,
        )
    embed.set_footer(text="Repins Sniper  •  MSA Authenticated")
    return embed


def _embed_scrapetime(name: str, info: dict) -> "discord.Embed":
    status = info["status"]
    colour_map = {
        "available": _CLR_GREEN,
        "blocked":   _CLR_RED,
        "taken":     _CLR_GOLD,
    }
    colour = colour_map.get(status, _CLR_GREY)

    embed = discord.Embed(
        title=f"{name}  ·  {status.upper()}",
        color=colour,
        timestamp=datetime.now(timezone.utc),
    )

    if info.get("holder_uuid"):
        embed.add_field(name="UUID", value=f"`{info['holder_uuid']}`", inline=False)

    if info.get("name_held_since"):
        since: datetime = info["name_held_since"]
        since_str = since.strftime("%Y-%m-%d  %H:%M:%S UTC")
        delta = datetime.now(timezone.utc) - since
        d, rem = divmod(int(delta.total_seconds()), 86400)
        h, _   = divmod(rem, 3600)
        embed.add_field(name="Held Since", value=since_str,    inline=True)
        embed.add_field(name="Duration",   value=f"{d}d {h}h", inline=True)

    if info.get("drop_time"):
        drop_dt: datetime = info["drop_time"]
        remaining = (drop_dt - datetime.now(timezone.utc)).total_seconds()
        d = int(remaining // 86400)
        h = int((remaining % 86400) // 3600)
        m = int((remaining % 3600) // 60)
        countdown = f"{d}d {h}h {m}m" if remaining > 0 else "**DROPPED!**"
        embed.add_field(name="Drop Time", value=drop_dt.strftime("%Y-%m-%d  %H:%M:%S UTC"), inline=True)
        embed.add_field(name="Countdown", value=countdown, inline=True)

    if status == "taken" and not info.get("drop_time"):
        embed.add_field(
            name="Drop Policy",
            value=(
                "Names drop **37 days** after the holder changes theirs.\n"
                "Use `!dropscan start` to detect name changes automatically,\n"
                "or `!autosnipe` to monitor and fire the instant it drops."
            ),
            inline=False,
        )
    elif status == "dropping":
        embed.add_field(
            name="Suggested Action",
            value=f"`!snipe {name} {info['drop_time'].strftime('%Y-%m-%dT%H:%M:%S')}`",
            inline=False,
        )
    elif status == "available":
        embed.add_field(
            name="Suggested Action",
            value=f"`!snipe {name}`  or  `!superfastsnipe {name}`",
            inline=False,
        )
    elif status == "blocked":
        embed.add_field(
            name="Note",
            value="This name is filtered by Mojang and cannot be claimed.",
            inline=False,
        )

    embed.set_footer(text="Repins Sniper  •  Mojang / Ashcon / PlayerDB")
    return embed


def _embed_help() -> "discord.Embed":
    embed = discord.Embed(
        title="Repins Sniper  —  Command Reference",
        description=(
            "High-performance Minecraft username sniper. "
            "Authenticate via **`!login`** (device code flow) — token cached 23 h."
        ),
        color=_CLR_BLUE,
        timestamp=datetime.now(timezone.utc),
    )
    cmds = [
        ("`!login`",
         "Authenticate your Microsoft account using **Device Code Flow**.\n"
         "You'll get a code — enter it at microsoft.com/devicelogin on any device."),

        ("`!logout`",
         "Clear the cached Minecraft token and require re-authentication."),

        ("`!snipe <name> [drop_time] [delay_ms] [workers]`",
         "Claim a name immediately, or schedule for an ISO 8601 UTC drop time.\n"
         "Example: `!snipe Notch 2025-06-01T12:00:00 -50 8`"),

        ("`!autosnipe <name> [delay_ms] [workers]`",
         "Check current status, then poll every 5 s until the name drops and fire automatically."),

        (f"`!superfastsnipe <name>`",
         f"{_SUPER_WORKERS} workers, 0 ms delay, TCP pre-warmed. Use when the name is already available."),

        ("`!scrapetime <name>`",
         "Check availability, holder UUID, drop time, and duration held."),

        ("`!dropscan [start|stop|status]`",
         "Start/stop the background drop-time scanner.\n"
         "Polls Mojang for 3-letter + OG names to detect name changes → calculates drop times."),

        ("`!drops`",
         "Show all detected drop times with countdowns."),

        ("`!track <name> [drop_time_iso]`",
         "Manually add a name to track. Optionally provide a known drop time."),

        ("`!scrapedrops [now|auto|stop|status]`",
         "Scrape NameMC + 3name.xyz for upcoming drop times.\n"
         "`now` — one-shot scrape, `auto` — repeat every 10 min, `stop` / `status`."),

        ("`!proxies [reload]`",
         "Show proxy pool status, or reload proxies."),

        ("`!cancel <name>`",
         "Stop a running autosnipe job for the given username."),

        ("`!status`",
         "List all active jobs and their current state."),

        ("`!setdelay [ms]`",
         "Override the auto-tuned fire delay. Omit the value to read the current setting."),

        ("`!help`",
         "Show this reference."),
    ]
    for name_field, value_field in cmds:
        embed.add_field(name=name_field, value=value_field, inline=False)

    embed.set_footer(text="Repins Sniper  •  Device Code Auth  •  Persistent session")
    return embed


def _embed_status(active_jobs: dict) -> "discord.Embed":
    embed = discord.Embed(
        title="Active Jobs",
        color=_CLR_BLUE,
        timestamp=datetime.now(timezone.utc),
    )
    if not active_jobs:
        embed.description = "No active jobs running."
    else:
        for job_name, task in active_jobs.items():
            state = "Running" if not task.done() else "Done"
            embed.add_field(name=job_name, value=state, inline=True)
    embed.set_footer(text="Repins Sniper")
    return embed


def _embed_simple(msg: str, color: int) -> "discord.Embed":
    """One-line helper for quick informational embeds."""
    return discord.Embed(description=msg, color=color, timestamp=datetime.now(timezone.utc))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Bot factory
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _build_bot(cfg: dict, bearer_ref: list[str]) -> Any:
    """Build the Discord bot.  *bearer_ref* is a mutable single-element list
    so all closures always see the latest token without rebinding.

    Returns the bot instance, or None if discord.py / token is missing.
    """
    if not _discord_available:
        print("[bot] discord.py not installed — bot disabled")
        return None
    token = cfg.get("discord_token", "") or os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        print("[bot] No discord_token — bot disabled")
        return None

    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

    active_jobs:    dict[str, asyncio.Task] = {}
    tuned_delay:    float = float(cfg.get("delay", 0))
    _sess_holder:   list[aiohttp.ClientSession] = []   # populated in on_ready

    def _tok() -> str:
        return bearer_ref[0]

    async def _require_token(ctx) -> bool:
        """Check that we have a valid bearer token. Send error if not."""
        if not bearer_ref[0]:
            await ctx.send(embed=_embed_simple(
                "No Minecraft token — run `!login` first to authenticate.", _CLR_RED
            ))
            return False
        return True

    def _sess() -> aiohttp.ClientSession | None:
        return _sess_holder[0] if _sess_holder else None

    # ── Webhook ──────────────────────────────────────────────

    async def _post_webhook(embed: "discord.Embed") -> None:
        url = cfg.get("webhook_url", "") or os.environ.get("DISCORD_WEBHOOK_URL", "")
        if not url:
            return
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json={"embeds": [embed.to_dict()]}) as r:
                    if r.status >= 400:
                        print(f"[webhook] {r.status}")
        except Exception as exc:
            print(f"[webhook] {exc}")

    # ── Events ───────────────────────────────────────────────

    @bot.event
    async def on_ready():
        max_conn = max(int(cfg.get("workers", 5)), _SUPER_WORKERS) + 4
        connector = TCPConnector(limit=max_conn, ttl_dns_cache=86400, enable_cleanup_closed=True)
        _sess_holder.append(aiohttp.ClientSession(connector=connector))

        # Load paid proxies from proxies.txt (if it exists)
        loaded = proxy_pool.load_file("proxies.txt")
        if loaded:
            print(f"[bot] Loaded {loaded} proxies from proxies.txt")

        tok_display = f"…{_tok()[-8:]}" if _tok() else "(none — use !login)"
        print(f"[bot] Online as {bot.user}  |  session ready  |  token {tok_display}")
        await bot.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="usernames drop")
        )

    @bot.event
    async def on_command_error(ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(embed=_embed_simple(
                f"Missing argument — run `!help` for usage.", _CLR_RED
            ))
        elif isinstance(error, commands.CommandNotFound):
            pass
        else:
            await ctx.send(embed=_embed_simple(f"`{error}`", _CLR_RED))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Commands
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @bot.command(name="help")
    async def cmd_help(ctx):
        await ctx.send(embed=_embed_help())

    # ── !login (Device Code Flow) ────────────────────────────

    _login_lock = asyncio.Lock()

    @bot.command(name="login")
    async def cmd_login(ctx):
        """Authenticate with Microsoft using the Device Code Flow.
        Shows a code + URL in Discord — enter the code on any device."""
        if _login_lock.locked():
            await ctx.send(embed=_embed_simple(
                "A login is already in progress — please complete it first.", _CLR_GOLD
            ))
            return

        # Check if we already have a valid token
        cached = msauth._load_cached_token()
        if cached:
            bearer_ref[0] = cached
            await ctx.send(embed=_embed_simple(
                f"Already authenticated — token is valid (cached < 23 h).\n"
                f"Token: `…{cached[-8:]}`\n\n"
                f"Use `!logout` first if you want to re-authenticate.",
                _CLR_GREEN,
            ))
            return

        async with _login_lock:
            # The device code callback — sends the code to Discord as a rich embed
            login_msg_ref: list[Any] = [None]

            async def _on_device_code(uri: str, code: str, message: str):
                embed = discord.Embed(
                    title="🔐  Microsoft Login",
                    description=(
                        f"**Go to:** [{uri}]({uri})\n"
                        f"**Enter code:** `{code}`\n\n"
                        f"Sign in with the Microsoft account that owns your Minecraft profile.\n"
                        f"The code expires in **15 minutes**."
                    ),
                    color=_CLR_PURPLE,
                    timestamp=datetime.now(timezone.utc),
                )
                embed.set_footer(text="Repins Sniper  •  Device Code Auth")
                login_msg_ref[0] = await ctx.send(embed=embed)

            # Start the auth flow
            try:
                azure_cid = cfg.get("azure_client_id", "") or ""
                mc_token = await msauth.authenticate(
                    account_type="ms",
                    on_device_code=_on_device_code,
                    azure_client_id=azure_cid,
                )
                bearer_ref[0] = mc_token

                success_embed = discord.Embed(
                    title="✅  Login Successful",
                    description=(
                        f"Authenticated and ready to snipe!\n"
                        f"Token: `…{mc_token[-8:]}`\n"
                        f"Token cached for **23 hours**."
                    ),
                    color=_CLR_GREEN,
                    timestamp=datetime.now(timezone.utc),
                )
                success_embed.set_footer(text="Repins Sniper")
                await ctx.send(embed=success_embed)

            except Exception as exc:
                await ctx.send(embed=_embed_simple(
                    f"Login failed: `{exc}`", _CLR_RED
                ))

    # ── !logout ──────────────────────────────────────────────

    @bot.command(name="logout")
    async def cmd_logout(ctx):
        """Clear the cached Minecraft bearer token."""
        deleted = msauth.clear_cached_token()
        bearer_ref[0] = ""
        if deleted:
            await ctx.send(embed=_embed_simple(
                "Logged out — cached token deleted.\n"
                "Use `!login` to authenticate again.",
                _CLR_RED,
            ))
        else:
            await ctx.send(embed=_embed_simple(
                "No cached token found. Use `!login` to authenticate.", _CLR_GOLD
            ))

    # ── !snipe ───────────────────────────────────────────────

    @bot.command(name="snipe")
    async def cmd_snipe(
        ctx,
        username:      str,
        drop_time_iso: str = "",
        delay_ms_str:  str = "",
        workers_str:   str = "",
    ):
        if not await _require_token(ctx):
            return
        nonlocal tuned_delay
        drop_time: datetime | None = None
        if drop_time_iso:
            try:
                drop_time = datetime.fromisoformat(drop_time_iso).replace(tzinfo=timezone.utc)
            except ValueError:
                await ctx.send(embed=_embed_simple(
                    "Invalid drop time — expected ISO 8601 UTC, e.g. `2025-06-01T18:00:00`", _CLR_RED
                ))
                return

        d = float(delay_ms_str) if delay_ms_str else tuned_delay
        w = int(workers_str)    if workers_str   else int(cfg.get("workers", 5))

        if drop_time:
            eta_str = drop_time.strftime("%Y-%m-%d  %H:%M:%S UTC")
            desc    = f"Scheduled for **{eta_str}** with **{w}** workers, **{d:.1f}ms** delay."
        else:
            desc = f"Firing **immediately** with **{w}** workers, **{d:.1f}ms** delay."

        queue_embed = discord.Embed(
            title=f"Queued  —  {username}",
            description=desc,
            color=_CLR_BLUE,
            timestamp=datetime.now(timezone.utc),
        )
        queue_embed.set_footer(text="Repins Sniper")
        await ctx.send(embed=queue_embed)

        async def _job():
            nonlocal tuned_delay
            summary = await run_snipe(username, _tok(), drop_time, d, w, _sess())
            tuned_delay = summary.get("tuned_delay_ms", tuned_delay)
            embed = _embed_snipe_result(username, summary)
            await ctx.send(embed=embed)
            await _post_webhook(embed)

        active_jobs[username] = asyncio.create_task(_job())

    # ── !autosnipe ───────────────────────────────────────────

    @bot.command(name="autosnipe")
    async def cmd_autosnipe(
        ctx,
        username:     str = "",
        delay_ms_str: str = "",
        workers_str:  str = "",
    ):
        if not await _require_token(ctx):
            return
        nonlocal tuned_delay
        if not username:
            await ctx.send(embed=_embed_simple(
                "Usage: `!autosnipe <username> [delay_ms] [workers]`", _CLR_RED
            ))
            return

        d = float(delay_ms_str) if delay_ms_str else tuned_delay
        w = int(workers_str)    if workers_str   else int(cfg.get("workers", 5))
        user = ctx.author

        old = active_jobs.pop(username, None)
        if old and not old.done():
            old.cancel()

        start_embed = discord.Embed(
            title=f"AutoSnipe  —  {username}",
            description="Checking status, then polling every 5 s until it drops.",
            color=_CLR_PURPLE,
            timestamp=datetime.now(timezone.utc),
        )
        start_embed.add_field(name="Workers", value=str(w),               inline=True)
        start_embed.add_field(name="Delay",   value=f"{d:.1f} ms",        inline=True)
        start_embed.add_field(name="Cancel",  value=f"`!cancel {username}`", inline=True)
        start_embed.set_footer(text="Repins Sniper")
        await ctx.send(embed=start_embed)

        async def _job():
            nonlocal tuned_delay
            sess = _sess()
            info = await scraper.fetch_drop_time(username, _tok(), session=sess)
            await ctx.send(embed=_embed_scrapetime(username, info))

            if info["status"] == "available":
                summary = await run_snipe(username, _tok(), delay_ms=d, workers=w, session=sess)

            elif info["status"] in ("taken", "unknown"):
                await ctx.send(embed=_embed_simple(
                    f"**{username}** is taken — monitoring every 5 s, will fire the instant it drops.",
                    _CLR_GOLD,
                ))
                result = await scraper.poll_until_available(
                    username, _tok(), None, poll_interval=5.0, session=sess,
                )
                if result.status not in (NameStatus.AVAILABLE, NameStatus.FREE_404):
                    await ctx.send(embed=_embed_simple(
                        f"**{username}** ended with unexpected status `{result.status.value}`.",
                        _CLR_RED,
                    ))
                    return
                drop_embed = discord.Embed(
                    title=f"Name Available  —  {username}",
                    description=f"{user.mention}  Firing **{w}** workers now.",
                    color=_CLR_GREEN,
                    timestamp=datetime.now(timezone.utc),
                )
                drop_embed.set_footer(text="Repins Sniper")
                await ctx.send(embed=drop_embed)
                summary = await run_snipe(username, _tok(), delay_ms=d, workers=w, session=sess)

            else:
                await ctx.send(embed=_embed_simple(
                    f"**{username}** is `{info['status']}` — cannot snipe.", _CLR_RED
                ))
                return

            tuned_delay = summary.get("tuned_delay_ms", tuned_delay)
            result_embed = _embed_snipe_result(username, summary)
            if summary["success"]:
                result_embed.description = f"{user.mention}"
            await ctx.send(embed=result_embed)
            await _post_webhook(result_embed)

        active_jobs[username] = asyncio.create_task(_job())

    # ── !superfastsnipe ──────────────────────────────────────

    @bot.command(name="superfastsnipe")
    async def cmd_superfastsnipe(ctx, username: str = ""):
        if not await _require_token(ctx):
            return
        nonlocal tuned_delay
        if not username:
            await ctx.send(embed=_embed_simple(
                "Usage: `!superfastsnipe <username>`", _CLR_RED
            ))
            return

        fire_embed = discord.Embed(
            title=f"SuperFast Snipe  —  {username}",
            description=(
                f"{_SUPER_WORKERS} workers  ·  0 ms delay  ·  TCP pre-warmed\n"
                "Fires immediately. Use when the name is already available."
            ),
            color=_CLR_GOLD,
            timestamp=datetime.now(timezone.utc),
        )
        fire_embed.set_footer(text="Repins Sniper  •  Max performance mode")
        await ctx.send(embed=fire_embed)

        async def _job():
            nonlocal tuned_delay
            sess = _sess()
            if sess:
                await _warmup(sess, username)
            summary = await run_snipe(username, _tok(), delay_ms=0.0, workers=_SUPER_WORKERS, session=sess)
            tuned_delay = summary.get("tuned_delay_ms", tuned_delay)
            result_embed = _embed_snipe_result(username, summary)
            await ctx.send(embed=result_embed)
            await _post_webhook(result_embed)

        active_jobs[username] = asyncio.create_task(_job())

    # ── !scrapetime ──────────────────────────────────────────

    @bot.command(name="scrapetime")
    async def cmd_scrapetime(ctx, username: str = ""):
        if not username:
            await ctx.send(embed=_embed_simple(
                "Usage: `!scrapetime <username>`", _CLR_RED
            ))
            return

        thinking = await ctx.send(embed=_embed_simple(
            f"Querying Mojang, Ashcon, and PlayerDB for **{username}**…",
            _CLR_BLUE,
        ))
        try:
            info = await scraper.fetch_drop_time(username, _tok(), session=_sess())
        finally:
            try:
                await thinking.delete()
            except Exception:
                pass
        await ctx.send(embed=_embed_scrapetime(username, info))

    # ── !cancel ──────────────────────────────────────────────

    @bot.command(name="cancel")
    async def cmd_cancel(ctx, username: str = ""):
        if not username:
            await ctx.send(embed=_embed_simple("Usage: `!cancel <username>`", _CLR_RED))
            return
        task = active_jobs.pop(username, None)
        if task and not task.done():
            task.cancel()
            await ctx.send(embed=_embed_simple(f"Cancelled  —  **{username}**", _CLR_RED))
        else:
            await ctx.send(embed=_embed_simple(f"No active job found for **{username}**.", _CLR_GOLD))

    # ── !status ──────────────────────────────────────────────

    @bot.command(name="status")
    async def cmd_status(ctx):
        await ctx.send(embed=_embed_status(active_jobs))

    # ── !setdelay ────────────────────────────────────────────

    @bot.command(name="setdelay")
    async def cmd_setdelay(ctx, ms: str = ""):
        nonlocal tuned_delay
        if not ms:
            await ctx.send(embed=_embed_simple(
                f"Current delay: **{tuned_delay:.1f} ms**", _CLR_BLUE
            ))
            return
        try:
            tuned_delay = float(ms)
        except ValueError:
            await ctx.send(embed=_embed_simple("Invalid value — expected a number.", _CLR_RED))
            return
        await ctx.send(embed=_embed_simple(
            f"Delay set to **{tuned_delay:.1f} ms**", _CLR_GREEN
        ))

    # ── !dropscan ─────────────────────────────────────────

    drop_scanner_ref: list[DropScanner | None] = [None]
    proxy_pool = ProxyPool()

    async def _on_drop_detected(name: str, drop_info: dict) -> None:
        """Callback when the scanner finds a new drop time."""
        drop_dt = datetime.fromisoformat(drop_info["drop_time"])
        remaining = (drop_dt - datetime.now(timezone.utc)).total_seconds()
        d = int(remaining // 86400)
        h = int((remaining % 86400) // 3600)

        embed = discord.Embed(
            title=f"\U0001f3af  Drop Detected  —  {name}",
            color=_CLR_GOLD,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Drop Time", value=drop_dt.strftime("%Y-%m-%d  %H:%M:%S UTC"), inline=True)
        embed.add_field(name="Countdown", value=f"{d}d {h}h", inline=True)
        embed.add_field(
            name="Auto-Snipe",
            value=f"`!snipe {name} {drop_dt.strftime('%Y-%m-%dT%H:%M:%S')}`",
            inline=False,
        )
        embed.set_footer(text="Repins Sniper  •  DropScanner")

        # Send to the first text channel we can find
        for ch in bot.get_all_channels():
            if hasattr(ch, "send"):
                try:
                    await ch.send(embed=embed)
                except Exception:
                    pass
                break
        await _post_webhook(embed)

    async def _on_available_now(name: str) -> None:
        """Callback when a tracked name is free RIGHT NOW."""
        embed = discord.Embed(
            title=f"\u2705  Name Available  —  {name}",
            description=f"Use `!superfastsnipe {name}` to claim it NOW!",
            color=_CLR_GREEN,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="Repins Sniper  •  DropScanner")
        for ch in bot.get_all_channels():
            if hasattr(ch, "send"):
                try:
                    await ch.send(embed=embed)
                except Exception:
                    pass
                break
        await _post_webhook(embed)

    @bot.command(name="dropscan")
    async def cmd_dropscan(ctx, action: str = "status"):
        nonlocal drop_scanner_ref
        action = action.lower()

        if action == "start":
            if drop_scanner_ref[0] and drop_scanner_ref[0].running:
                await ctx.send(embed=_embed_simple("Drop scanner is already running.", _CLR_GOLD))
                return

            # Scrape free proxies first
            await ctx.send(embed=_embed_simple("Scraping free proxies…", _CLR_BLUE))
            await proxy_pool.scrape_and_validate()

            scanner = DropScanner(
                proxy_pool=proxy_pool,
                bearer_token=_tok(),
                on_drop_detected=_on_drop_detected,
                on_available_now=_on_available_now,
            )
            drop_scanner_ref[0] = scanner

            # Get names to track: priority OG names first, then all 3-char
            names = namelist.get_all_tracked(shuffle=True)

            scanner.start(names)

            embed = discord.Embed(
                title="DropScanner Started",
                description=(
                    f"Tracking **{len(names)}** names\n"
                    f"Proxies: **{proxy_pool.count}**\n"
                    f"Polling Mojang continuously to detect name changes → drop times"
                ),
                color=_CLR_GREEN,
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_footer(text="Repins Sniper  •  DropScanner")
            await ctx.send(embed=embed)

        elif action == "stop":
            if drop_scanner_ref[0]:
                drop_scanner_ref[0].stop()
                await ctx.send(embed=_embed_simple("Drop scanner stopped.", _CLR_RED))
            else:
                await ctx.send(embed=_embed_simple("No scanner running.", _CLR_GOLD))

        else:  # status
            scanner = drop_scanner_ref[0]
            if not scanner:
                await ctx.send(embed=_embed_simple(
                    "Drop scanner not started. Use `!dropscan start`", _CLR_GOLD
                ))
                return

            stats = scanner.get_stats()
            embed = discord.Embed(
                title="DropScanner Status",
                color=_CLR_BLUE,
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Running", value="Yes" if stats["running"] else "No", inline=True)
            embed.add_field(name="Checked", value=f"{stats['total_checked']:,}", inline=True)
            embed.add_field(name="Cycles", value=str(stats["cycles"]), inline=True)
            embed.add_field(name="Drops Found", value=str(stats["drops_found"]), inline=True)
            embed.add_field(name="Proxies", value=str(stats["proxies"]), inline=True)
            embed.add_field(name="Tracked Names", value=f"{stats['tracked_names']:,}", inline=True)
            embed.set_footer(text="Repins Sniper  •  DropScanner")
            await ctx.send(embed=embed)

    # ── !drops ───────────────────────────────────────────────

    @bot.command(name="drops")
    async def cmd_drops(ctx):
        scanner = drop_scanner_ref[0]
        if not scanner:
            # Try loading from file
            from dropscanner import _load_db
            db = _load_db()
            drops_data = db.get("drops", {})
            if not drops_data:
                await ctx.send(embed=_embed_simple(
                    "No drops detected yet. Start the scanner with `!dropscan start`", _CLR_GOLD
                ))
                return
            # Build a temporary scanner just to format
            scanner = DropScanner(proxy_pool)

        drops = scanner.get_drops()
        if not drops:
            await ctx.send(embed=_embed_simple("No drops detected yet.", _CLR_GOLD))
            return

        embed = discord.Embed(
            title=f"Detected Drops  ({len(drops)})",
            color=_CLR_GOLD,
            timestamp=datetime.now(timezone.utc),
        )

        # Sort by drop time
        sorted_drops = sorted(drops.items(), key=lambda x: x[1]["drop_dt"])
        for name, info in sorted_drops[:25]:  # Discord embed limit
            status_icon = "\u2705" if info["dropped"] else "\u23f3"
            drop_str = info["drop_dt"].strftime("%Y-%m-%d %H:%M UTC")
            embed.add_field(
                name=f"{status_icon}  {name}",
                value=f"Drop: {drop_str}\n{info['remaining_human']}",
                inline=True,
            )

        embed.set_footer(text="Repins Sniper  •  DropScanner")
        await ctx.send(embed=embed)

    # ── !track ───────────────────────────────────────────────

    @bot.command(name="track")
    async def cmd_track(ctx, username: str = "", drop_time_iso: str = ""):
        if not username:
            await ctx.send(embed=_embed_simple("Usage: `!track <name> [drop_time_iso]`", _CLR_RED))
            return

        scanner = drop_scanner_ref[0]
        if not scanner:
            scanner = DropScanner(proxy_pool, _tok())
            drop_scanner_ref[0] = scanner

        if drop_time_iso:
            try:
                dt = datetime.fromisoformat(drop_time_iso).replace(tzinfo=timezone.utc)
            except ValueError:
                await ctx.send(embed=_embed_simple("Invalid time format. Use ISO 8601.", _CLR_RED))
                return
            scanner.add_drop_manual(username, dt)
            remaining = (dt - datetime.now(timezone.utc)).total_seconds()
            d = int(remaining // 86400)
            h = int((remaining % 86400) // 3600)
            await ctx.send(embed=_embed_simple(
                f"Tracking **{username}** — drops in **{d}d {h}h**\n"
                f"Drop time: {dt.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
                f"Auto-snipe: `!snipe {username} {drop_time_iso}`",
                _CLR_GREEN,
            ))
        else:
            await ctx.send(embed=_embed_simple(
                f"Added **{username}** to monitoring.\n"
                f"Start scanner with `!dropscan start` to detect its drop time.",
                _CLR_BLUE,
            ))

    # ── !proxies ─────────────────────────────────────────────

    @bot.command(name="proxies")
    async def cmd_proxies(ctx, action: str = ""):
        if action.lower() == "reload":
            await ctx.send(embed=_embed_simple("Scraping free proxies…", _CLR_BLUE))
            added = await proxy_pool.scrape_and_validate()
            await ctx.send(embed=_embed_simple(
                f"Proxies reloaded.\n{proxy_pool.status_text()}", _CLR_GREEN
            ))
        else:
            embed = discord.Embed(
                title="Proxy Pool",
                description=proxy_pool.status_text(),
                color=_CLR_BLUE,
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_footer(text="Repins Sniper")
            await ctx.send(embed=embed)

    # ── !scrapedrops ──────────────────────────────────────────

    drop_scraper_ref: list[DropTimeScraper | None] = [None]

    async def _on_new_scraped_drop(name: str, drop_info: dict) -> None:
        """Callback when the web scraper finds a NEW drop time."""
        drop_dt_str = drop_info.get("drop_time", "")
        try:
            drop_dt = datetime.fromisoformat(drop_dt_str)
        except (ValueError, TypeError):
            return
        remaining = (drop_dt - datetime.now(timezone.utc)).total_seconds()
        d = int(remaining // 86400)
        h = int((remaining % 86400) // 3600)
        src = drop_info.get("source", "unknown")
        win = drop_info.get("window_hours", 0)

        embed = discord.Embed(
            title=f"\U0001f310  Scraped Drop  —  {name}",
            color=_CLR_GOLD,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Drop Time", value=drop_dt.strftime("%Y-%m-%d  %H:%M:%S UTC"), inline=True)
        embed.add_field(name="Countdown", value=f"{d}d {h}h", inline=True)
        embed.add_field(name="Source", value=src, inline=True)
        if win > 0:
            embed.add_field(name="Window", value=f"± {win:.1f}h", inline=True)
        embed.add_field(
            name="Auto-Snipe",
            value=f"`!snipe {name} {drop_dt.strftime('%Y-%m-%dT%H:%M:%S')}`",
            inline=False,
        )
        embed.set_footer(text="Repins Sniper  •  DropTime Scraper")

        for ch in bot.get_all_channels():
            if hasattr(ch, "send"):
                try:
                    await ch.send(embed=embed)
                except Exception:
                    pass
                break
        await _post_webhook(embed)

    @bot.command(name="scrapedrops")
    async def cmd_scrapedrops(ctx, action: str = "now"):
        nonlocal drop_scraper_ref
        action = action.lower()

        if action == "now":
            await ctx.send(embed=_embed_simple(
                "Scraping NameMC + 3name.xyz for drop times…", _CLR_BLUE
            ))
            try:
                drops = await scrape_all_droptimes(session=_sess())
            except Exception as exc:
                await ctx.send(embed=_embed_simple(f"Scrape error: `{exc}`", _CLR_RED))
                return

            if not drops:
                await ctx.send(embed=_embed_simple("No drops found.", _CLR_GOLD))
                return

            # Show results
            embed = discord.Embed(
                title=f"Scraped Drops  ({len(drops)})",
                color=_CLR_GOLD,
                timestamp=datetime.now(timezone.utc),
            )
            now = datetime.now(timezone.utc)
            sorted_drops = sorted(
                drops.items(),
                key=lambda x: x[1].get("drop_time", "9999"),
            )
            for name, info in sorted_drops[:25]:
                try:
                    drop_dt = datetime.fromisoformat(info["drop_time"])
                    remaining = (drop_dt - now).total_seconds()
                    d = int(remaining // 86400)
                    h = int((remaining % 86400) // 3600)
                    m = int((remaining % 3600) // 60)
                    countdown = f"{d}d {h}h {m}m" if remaining > 0 else "**DROPPED!**"
                    src = info.get("source", "?")
                    win = info.get("window_hours", 0)
                    val = f"{drop_dt.strftime('%m/%d %H:%M UTC')}\n{countdown}"
                    if win > 0:
                        val += f" (± {win:.1f}h)"
                    val += f"\n`{src}`"
                except Exception:
                    val = "Unknown time"
                icon = "\u2705" if remaining <= 0 else "\u23f3"
                embed.add_field(name=f"{icon}  {name}", value=val, inline=True)

            embed.set_footer(text="Repins Sniper  •  NameMC + 3name")
            await ctx.send(embed=embed)

        elif action == "auto":
            if drop_scraper_ref[0] and drop_scraper_ref[0].running:
                await ctx.send(embed=_embed_simple(
                    "Auto-scraper is already running.", _CLR_GOLD
                ))
                return

            scraper = DropTimeScraper(
                interval=600,  # every 10 min
                on_new_drop=_on_new_scraped_drop,
            )
            drop_scraper_ref[0] = scraper
            scraper.start()
            await ctx.send(embed=_embed_simple(
                "Auto-scraper started — scraping NameMC + 3name every **10 min**.\n"
                "New drops will be announced here.\n"
                "Stop with `!scrapedrops stop`",
                _CLR_GREEN,
            ))

        elif action == "stop":
            if drop_scraper_ref[0]:
                drop_scraper_ref[0].stop()
                await ctx.send(embed=_embed_simple("Auto-scraper stopped.", _CLR_RED))
            else:
                await ctx.send(embed=_embed_simple("No auto-scraper running.", _CLR_GOLD))

        elif action == "status":
            s = drop_scraper_ref[0]
            if not s:
                await ctx.send(embed=_embed_simple(
                    "Auto-scraper not started. Use `!scrapedrops auto`", _CLR_GOLD
                ))
                return
            stats = s.get_stats()
            embed = discord.Embed(
                title="Drop Scraper Status",
                color=_CLR_BLUE,
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Running", value="Yes" if stats["running"] else "No", inline=True)
            embed.add_field(name="Drops in DB", value=str(stats["drops_in_db"]), inline=True)
            embed.add_field(name="Last Scrape", value=stats["last_scrape_ago"], inline=True)
            embed.add_field(name="Interval", value=f"{stats['interval']}s", inline=True)
            embed.set_footer(text="Repins Sniper  •  DropTime Scraper")
            await ctx.send(embed=embed)

        else:
            await ctx.send(embed=_embed_simple(
                "Usage: `!scrapedrops [now|auto|stop|status]`", _CLR_RED
            ))

    bot._discord_token = token  # type: ignore[attr-defined]
    return bot


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Entry point — start bot, authenticate via !login command
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _async_main() -> None:
    cfg = _load_config()

    # ── Try to load cached token (< 23 h old) ────────────────
    bearer_token = ""
    cached = msauth._load_cached_token()
    if cached:
        bearer_token = cached
        print(f"[startup] Loaded cached token — …{bearer_token[-8:]}")
    else:
        print("[startup] No cached token — use !login in Discord to authenticate.")

    # ── Pre-resolve DNS so the session skips it on every call ─
    _resolve_host(_MC_SERVICES_HOST)

    # Use a mutable ref so all bot closures always see the current token
    bearer_ref: list[str] = [bearer_token]

    bot = _build_bot(cfg, bearer_ref)
    if bot is None:
        print("[startup] No Discord token configured — nothing to do.  Set discord_token in config.json.")
        return

    print("[startup] Starting Discord bot — use !login to authenticate, then snipe!")
    try:
        await bot.start(bot._discord_token)  # type: ignore[attr-defined]
    except KeyboardInterrupt:
        pass
    finally:
        print("[startup] Shutting down…")
        await bot.close()


def main() -> None:
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        print("\n[main] Interrupted — exiting.")


if __name__ == "__main__":
    main()

"""
msauth.py — Microsoft / Giftcard / Bearer-token authentication for Minecraft.

Auth flows supported:
  1. Device Code Flow (DEFAULT) — headless-friendly, works on Railway / any server.
     The bot prints a URL + code → user enters the code at microsoft.com/devicelogin
     on any device (phone, laptop, etc.) → server polls until auth completes.
  2. Microsoft account  — browser-based OAuth2 (fallback for local dev)
  3. Giftcard account   — same flow, different scope
  4. Raw bearer token   — paste directly or from MC_TOKEN env var

Token caching:
  • Writes the Minecraft bearer token + timestamp to token.txt
  • On startup, reloads the cached token if it is less than 23 hours old
    so the bot never needs re-authentication during a normal 24-hour run.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable
from urllib.parse import urlparse, parse_qs

import aiohttp

# ──────────────────────────────────────────────────────────────
# OAuth2 constants
# ──────────────────────────────────────────────────────────────

# Official Minecraft Launcher client ID (login.live.com — no Azure subscription needed)
_CLIENT_ID_LIVE    = "00000000402b5328"
_REDIRECT_URI      = "https://login.live.com/oauth20_desktop.srf"
_SCOPE_MS          = "service::user.auth.xboxlive.com::MBI_SSL"
_SCOPE_GIFT        = "service::user.auth.xboxlive.com::MBI_SSL"

# Microsoft Live OAuth2 endpoints (NOT Azure AD — works without subscription)
_AUTHORIZE_URL     = "https://login.live.com/oauth20_authorize.srf"
_TOKEN_URL_LIVE    = "https://login.live.com/oauth20_token.srf"

# ── Azure AD / Device Code Flow endpoints ────────────────────
# These use a PUBLIC client registration under "consumers" tenant
# so any personal Microsoft account can authenticate.
_AZURE_CLIENT_ID   = "389b1b32-b5d5-43b2-bddc-84ce938d6737"  # public MC launcher client (Azure AD)
_AZURE_TENANT      = "consumers"
_AZURE_DEVICE_URL  = f"https://login.microsoftonline.com/{_AZURE_TENANT}/oauth2/v2.0/devicecode"
_AZURE_TOKEN_URL   = f"https://login.microsoftonline.com/{_AZURE_TENANT}/oauth2/v2.0/token"
_AZURE_SCOPE       = "XboxLive.signin offline_access"

# Xbox / XSTS / Minecraft endpoints
_XBOX_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
_XSTS_AUTH_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
_MC_AUTH_URL   = "https://api.minecraftservices.com/authentication/login_with_xbox"

# Token cache file
_TOKEN_FILE = Path("token.txt")
_TOKEN_MAX_AGE_S = 23 * 3600  # 23 hours — MSA tokens last 24 h; refresh 1 h early


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Callback type for device code — lets the Discord bot display the code
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Signature: async callback(verification_uri, user_code, message) -> None
DeviceCodeCallback = Callable[[str, str, str], Awaitable[None]]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Public interface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def authenticate(
    account_type: str = "ms",
    on_device_code: DeviceCodeCallback | None = None,
    azure_client_id: str = "",
    **_kwargs,
) -> str:
    """Return a Minecraft bearer token for the requested account type.

    account_type:
        "ms"  — Device Code Flow (headless-safe, DEFAULT)
        "browser" — full Microsoft browser login flow (local dev only)
        "g"   — giftcard flow (same endpoints, different scope)
        "t"   — raw bearer token from env or prompt

    on_device_code:
        Async callback(verification_uri, user_code, message) called when
        the device code is ready. If None, prints to stdout.

    azure_client_id:
        Override the Azure AD client ID for device code flow.
        Defaults to the public Minecraft launcher client.

    Returns the bearer token string ready for Authorization header.
    """
    # ── 1. Try cached token first ────────────────────────────
    cached = _load_cached_token()
    if cached is not None:
        print("[auth] Loaded cached token (< 23 h old) — skipping re-auth.")
        return cached

    account_type = account_type.lower().strip()

    if account_type == "t":
        # Raw bearer token — env var or manual paste
        token = os.environ.get("MC_TOKEN", "").strip()
        if not token:
            token = input("[auth] Paste your Minecraft bearer token: ").strip()
        if not token:
            raise ValueError("No bearer token provided.")
        _save_token(token)
        return token

    if account_type == "browser":
        # ── Legacy browser-based flow (local dev) ────────────
        scope = _SCOPE_GIFT if account_type == "g" else _SCOPE_MS
        async with aiohttp.ClientSession() as session:
            ms_token = await _browser_auth_flow(session, scope)
            xbl_token, user_hash = await _xbox_live_auth(session, ms_token)
            xsts_token = await _xsts_auth(session, xbl_token)
            mc_token = await _minecraft_auth(session, xsts_token, user_hash)
        _save_token(mc_token)
        print("[auth] Authentication successful — token cached to token.txt")
        return mc_token

    # ── 2. Device Code Flow (DEFAULT — works on Railway) ─────
    client_id = azure_client_id or _AZURE_CLIENT_ID
    async with aiohttp.ClientSession() as session:
        ms_token = await _device_code_flow(session, client_id, on_device_code)
        xbl_token, user_hash = await _xbox_live_auth_azure(session, ms_token)
        xsts_token = await _xsts_auth(session, xbl_token)
        mc_token = await _minecraft_auth(session, xsts_token, user_hash)

    _save_token(mc_token)
    print("[auth] ✓ Device Code authentication successful — token cached.")
    return mc_token


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Internal helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _load_cached_token() -> str | None:
    """Return the cached bearer token if it exists and is < 23 h old."""
    if not _TOKEN_FILE.exists():
        return None
    try:
        data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
        saved_at = data.get("saved_at", 0)
        token    = data.get("token", "")
        # Check freshness — tokens older than 23 h are discarded
        if time.time() - saved_at < _TOKEN_MAX_AGE_S and token:
            return token
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _save_token(token: str) -> None:
    """Persist the bearer token with a timestamp for cache-reload logic."""
    payload = {
        "token": token,
        "saved_at": time.time(),
        "saved_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    _TOKEN_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def clear_cached_token() -> bool:
    """Delete the cached token file. Returns True if deleted."""
    if _TOKEN_FILE.exists():
        _TOKEN_FILE.unlink()
        return True
    return False


# ──────────────────────────────────────────────────────────────
# Device Code Flow (headless — works on Railway, Docker, etc.)
#
# 1. POST to /devicecode endpoint → get user_code + device_code
# 2. Show user_code to the user (via Discord embed or stdout)
# 3. User goes to https://microsoft.com/devicelogin and enters the code
# 4. We poll /token endpoint every N seconds until auth completes
# 5. Exchange the MS token → Xbox → XSTS → Minecraft bearer
# ──────────────────────────────────────────────────────────────


async def _device_code_flow(
    session: aiohttp.ClientSession,
    client_id: str,
    on_device_code: DeviceCodeCallback | None = None,
) -> str:
    """Execute the OAuth2 Device Code flow. Returns the MS access token."""

    # Step 1 — Request a device code
    async with session.post(
        _AZURE_DEVICE_URL,
        data={
            "client_id": client_id,
            "scope": _AZURE_SCOPE,
        },
    ) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(
                f"[auth] Device code request failed ({resp.status}).\n"
                f"       Response: {body}"
            )
        data = await resp.json()

    device_code      = data["device_code"]
    user_code        = data["user_code"]
    verification_uri = data.get("verification_uri", "https://microsoft.com/devicelogin")
    expires_in       = data.get("expires_in", 900)
    interval         = data.get("interval", 5)
    message          = data.get("message", "")

    # Step 2 — Show the code to the user
    print("\n[auth] ═══════════════════════════════════════════════════")
    print(f"[auth]  Go to: {verification_uri}")
    print(f"[auth]  Enter code: {user_code}")
    print("[auth] ═══════════════════════════════════════════════════\n")

    if on_device_code:
        try:
            await on_device_code(verification_uri, user_code, message)
        except Exception as exc:
            print(f"[auth] Callback error (non-fatal): {exc}")

    # Step 3 — Poll until the user authenticates
    deadline = time.time() + expires_in

    while time.time() < deadline:
        await asyncio.sleep(interval)

        async with session.post(
            _AZURE_TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": device_code,
            },
        ) as resp:
            token_data = await resp.json()

        error = token_data.get("error", "")

        if error == "authorization_pending":
            # User hasn't entered the code yet — keep polling
            continue
        elif error == "slow_down":
            # Server wants us to slow down
            interval += 5
            continue
        elif error == "expired_token":
            raise RuntimeError(
                "[auth] Device code expired — user did not authenticate in time.\n"
                "       Run `!login` again to get a new code."
            )
        elif error == "authorization_declined":
            raise RuntimeError("[auth] User declined the authentication request.")
        elif error:
            raise RuntimeError(
                f"[auth] Device code poll error: {error}\n"
                f"       {token_data.get('error_description', '')}"
            )

        # Success!
        access_token = token_data.get("access_token")
        if access_token:
            print("[auth] ✓ User authenticated via device code!")
            return access_token

        raise RuntimeError(f"[auth] Unexpected token response: {token_data}")

    raise RuntimeError("[auth] Device code flow timed out waiting for user authentication.")


# ──────────────────────────────────────────────────────────────
# Legacy browser flow (kept for local dev / backward compat)
# ──────────────────────────────────────────────────────────────

async def _browser_auth_flow(session: aiohttp.ClientSession, scope: str) -> str:
    """Open the Microsoft login page in the browser, get an auth code,
    and exchange it for an access token.

    Flow:
      1. Build authorize URL with official MC launcher client_id
      2. Open it in the default browser
      3. User logs in → redirected to https://login.live.com/oauth20_desktop.srf?code=XXX
      4. User copies that full URL and pastes it back here
      5. We extract the code and POST to the token endpoint
    """
    # Build the login URL
    auth_url = (
        f"{_AUTHORIZE_URL}"
        f"?client_id={_CLIENT_ID_LIVE}"
        f"&response_type=code"
        f"&redirect_uri={_REDIRECT_URI}"
        f"&scope={scope}"
    )

    print("\n[auth] ═══════════════════════════════════════════════════")
    print("[auth]  Opening Microsoft login in your browser…")
    print("[auth]  If it doesn't open, copy this URL manually:")
    print(f"[auth]  {auth_url}")
    print("[auth] ═══════════════════════════════════════════════════")

    # Try to open the browser automatically
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass  # user can copy-paste the URL manually

    print("\n[auth] After logging in, the page will go blank or show an error.")
    print("[auth] That's normal! Copy the FULL URL from your browser's address bar.")
    print("[auth] It will look like: https://login.live.com/oauth20_desktop.srf?code=M.C5...")
    redirect_url = input("\n[auth] Paste the redirect URL here: ").strip()

    if not redirect_url:
        raise ValueError("[auth] No URL provided.")

    # Extract the authorization code from the redirect URL
    parsed = urlparse(redirect_url)
    params = parse_qs(parsed.query)
    code = params.get("code", [None])[0]

    if not code:
        # Maybe user pasted just the code itself
        if redirect_url.startswith("M.") or len(redirect_url) > 50:
            code = redirect_url
        else:
            raise ValueError(
                "[auth] Could not extract auth code from URL.\n"
                "       Make sure you copied the full URL from the address bar."
            )

    # Exchange the auth code for an access token
    async with session.post(
        _TOKEN_URL_LIVE,
        data={
            "client_id": _CLIENT_ID_LIVE,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": _REDIRECT_URI,
        },
    ) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(
                f"[auth] Token exchange failed ({resp.status}).\n"
                f"       Response: {body}"
            )
        data = await resp.json()

    access_token = data.get("access_token")
    if not access_token:
        raise RuntimeError(f"[auth] No access_token in response: {data}")

    print("[auth] Microsoft login successful!")
    return access_token


# ──────────────────────────────────────────────────────────────
# Xbox Live authentication (two variants: login.live.com vs Azure AD)
# ──────────────────────────────────────────────────────────────

async def _xbox_live_auth(session: aiohttp.ClientSession, ms_token: str) -> tuple[str, str]:
    """Exchange a login.live.com access token for an Xbox Live token.

    Returns (xbl_token, user_hash).
    """
    payload = {
        "Properties": {
            "AuthMethod": "RPS",
            "SiteName": "user.auth.xboxlive.com",
            "RpsTicket": ms_token,  # no 'd=' prefix when using login.live.com tokens
        },
        "RelyingParty": "http://auth.xboxlive.com",
        "TokenType": "JWT",
    }
    async with session.post(_XBOX_AUTH_URL, json=payload) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"[auth] Xbox Live auth failed ({resp.status}): {body}")
        data = await resp.json()
    xbl_token = data["Token"]
    user_hash = data["DisplayClaims"]["xui"][0]["uhs"]
    return xbl_token, user_hash


async def _xbox_live_auth_azure(session: aiohttp.ClientSession, ms_token: str) -> tuple[str, str]:
    """Exchange an Azure AD access token for an Xbox Live token.

    Azure AD tokens require the 'd=' prefix on the RPS ticket.
    Returns (xbl_token, user_hash).
    """
    payload = {
        "Properties": {
            "AuthMethod": "RPS",
            "SiteName": "user.auth.xboxlive.com",
            "RpsTicket": f"d={ms_token}",  # Azure AD tokens need 'd=' prefix
        },
        "RelyingParty": "http://auth.xboxlive.com",
        "TokenType": "JWT",
    }
    async with session.post(_XBOX_AUTH_URL, json=payload) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"[auth] Xbox Live auth failed ({resp.status}): {body}")
        data = await resp.json()
    xbl_token = data["Token"]
    user_hash = data["DisplayClaims"]["xui"][0]["uhs"]
    return xbl_token, user_hash


# ──────────────────────────────────────────────────────────────
# XSTS authorization
# ──────────────────────────────────────────────────────────────

async def _xsts_auth(session: aiohttp.ClientSession, xbl_token: str) -> str:
    """Exchange an Xbox Live token for an XSTS token."""
    payload = {
        "Properties": {
            "SandboxId": "RETAIL",
            "UserTokens": [xbl_token],
        },
        "RelyingParty": "rp://api.minecraftservices.com/",
        "TokenType": "JWT",
    }
    async with session.post(_XSTS_AUTH_URL, json=payload) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"[auth] XSTS auth failed ({resp.status}): {body}")
        data = await resp.json()
    return data["Token"]


# ──────────────────────────────────────────────────────────────
# Minecraft bearer token
# ──────────────────────────────────────────────────────────────

async def _minecraft_auth(session: aiohttp.ClientSession, xsts_token: str, user_hash: str) -> str:
    """Exchange an XSTS token for a Minecraft Services bearer token."""
    payload = {
        "identityToken": f"XBL3.0 x={user_hash};{xsts_token}",
        "ensureLegacyEnabled": True,
    }
    async with session.post(_MC_AUTH_URL, json=payload) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"[auth] Minecraft auth failed ({resp.status}): {body}")
        data = await resp.json()
    return data["access_token"]

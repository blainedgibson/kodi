# -*- coding: utf-8 -*-

'''
********************************************************cm*
* Midnight Add-on
*
* @file trakt.py
* @package script.module.midnight
*
* @copyright (c) 2026, Midnight
* @license GNU General Public License, version 3 (GPL-3.0)
*
********************************************************cm*
'''

import re
import os
import time
import json
import threading
from datetime import datetime, timedelta
from typing import Dict, Any
from functools import wraps
import traceback

from urllib.parse import urljoin, quote_plus
import sqlite3 as database
from sqlite3 import OperationalError


import requests

from requests.adapters import HTTPAdapter
# from requests.structures import CaseInsensitiveDict
from urllib3.util.retry import Retry

from . import cache
from . import cleandate
from . import client
from . import control
from . import keys
from . import utils
from . import crew_errors
from . import http_client
from .crewruntime import c

trakt_endpoints = {
    'settings': {

    },
    'shows': {

    },
    'movies': {
    }

}

trakt_response_codes = {
    '200': 'Success',
    '201': 'Success - new resource created (POST)',
    '204': 'Success - no content to return (DELETE)',
    '400': 'Bad Request - request couldn\'t be parsed',
    '401': 'Unauthorized - OAuth must be provided',
    '403': 'Forbidden - invalid API key or unapproved app',
    '404': 'Not Found - method exists but no record found',
    '405': 'Method Not Found - method doesn\'t exist',
    '409': 'Conflict - resource already created',
    '412': 'Precondition Failed - use application/json content type',
    '420': 'Account Limit Exceeded - list count, item count, etc',
    '422': 'Unprocessable Entity - validation errors',
    '423': 'Locked User Account - have the user contact support',
    '426': 'VIP Only - user must upgrade to VIP',
    '429': 'Rate Limit Exceeded',
    '500': 'Server Error - please open a support ticket',
    '502': 'Service Unavailable - server overloaded (try again in 30s)',
    '503': 'Service Unavailable - server overloaded (try again in 30s)',
    '504': 'Service Unavailable - server overloaded (try again in 30s)',
    '520': 'Service Unavailable - Cloudflare error',
    '521': 'Service Unavailable - Cloudflare error',
    '522': 'Service Unavailable - Cloudflare error'
}

BASE_URL = 'https://api.trakt.tv/'
CLIENT_ID = keys.trakt_id
CLIENT_SECRET = keys.trakt_secret
REDIRECT_URI = 'urn:ietf:wg:oauth:2.0:oob'
TRAKTUSER = c.get_setting('trakt.user').strip()
# Adopt a helpful User-Agent for Trakt API requests (recommended by docs)
try:
    USER_AGENT = f"{control.addonInfo('id')}/{control.addonInfo('version')}"
except Exception:
    USER_AGENT = 'script.module.midnight/unknown'

# Use shared HTTP client with connection pooling and retry logic
session = http_client.get_trakt_session()

# Global variable to store last pagination info from Trakt API responses
# Format: {'page': 4, 'limit': 40, 'page_count': 259, 'item_count': 10360}
_last_pagination_info = {}

# Module-level lock: ensures only one thread can execute token_refresh at a time.
# Replaces the window-property spin-wait which had a TOCTOU race — two threads
# could both read 'refreshing_token' as empty before either set it to 'true'.
_trakt_refresh_lock = threading.Lock()

# Cross-process window properties (shared across plugin + service + other Kodi processes).
PROP_TRAKT_REFRESHING = 'midnight.trakt_refreshing_token'
PROP_TRAKT_WAKE_QUIET_UNTIL = 'midnight.trakt_wake_quiet_until'
TRAKT_WAKE_QUIET_SECONDS = 45
PROP_TRAKT_RATE_LIMIT_UNTIL = 'midnight.trakt_rate_limit_until'
PROP_TRAKT_BOOT_QUIET_UNTIL = 'midnight.trakt_boot_quiet_until'

TRAKT_BOOT_QUIET_SECONDS = 90
TRAKT_DEFAULT_RATE_LIMIT_SECONDS = 120


def _trakt_prop_unix(name):
    """Read a window property as a Unix timestamp (0 if unset/invalid)."""
    try:
        raw = control.window.getProperty(name)
        return int(raw) if raw else 0
    except (ValueError, TypeError):
        return 0


def arm_trakt_wake_quiet(seconds=None):
    """Suppress Trakt ok/info dialogs briefly after sleep/wake (stampede protection)."""
    try:
        seconds = int(seconds if seconds is not None else TRAKT_WAKE_QUIET_SECONDS)
    except (TypeError, ValueError):
        seconds = TRAKT_WAKE_QUIET_SECONDS
    until = int(time.time()) + max(seconds, 1)
    control.window.setProperty(PROP_TRAKT_WAKE_QUIET_UNTIL, str(until))
    c.log(f'[Trakt] Wake quiet armed for {seconds}s', 1)


def should_suppress_trakt_ui():
    """True during playback, device idle, or post-wake quiet window."""
    try:
        import xbmc as _xbmc
        if _xbmc.Player().isPlaying():
            return True
    except Exception:
        pass
    # Same flag crewservice sets — avoid importing crewservice (circular).
    try:
        if control.window.getProperty('midnight.device.sleeping') == 'true':
            return True
    except Exception:
        pass
    return _trakt_prop_unix(PROP_TRAKT_WAKE_QUIET_UNTIL) > int(time.time())


def is_trakt_rate_limited():
    """True while a shared rate-limit backoff window is active (429 protection)."""
    return _trakt_prop_unix(PROP_TRAKT_RATE_LIMIT_UNTIL) > int(time.time())


def set_trakt_rate_limit(seconds):
    """Arm a cross-process backoff so plugin + service do not pile onto oauth/token."""
    try:
        seconds = max(int(seconds), 1)
    except (ValueError, TypeError):
        seconds = TRAKT_DEFAULT_RATE_LIMIT_SECONDS
    until = int(time.time()) + seconds
    current = _trakt_prop_unix(PROP_TRAKT_RATE_LIMIT_UNTIL)
    if until > current:
        control.window.setProperty(PROP_TRAKT_RATE_LIMIT_UNTIL, str(until))
        c.log(f'[Trakt] Rate-limit backoff armed for {seconds}s', 1)


def _parse_retry_after(response):
    """Parse Retry-After header (seconds) with a safe default."""
    try:
        if response is not None and getattr(response, 'headers', None):
            raw = response.headers.get('Retry-After', '')
            if raw:
                return max(int(raw), 1)
    except (ValueError, TypeError, AttributeError):
        pass
    return TRAKT_DEFAULT_RATE_LIMIT_SECONDS


def arm_trakt_boot_quiet(seconds=None, extend=False):
    """
    Defer background Trakt work so other addons (gears, script.trakt) can finish
    their startup token refresh without us joining the stampede.

    Armed once at service start. Callers must not re-arm (extend=False) or the
    quiet window stretches to 3+ minutes and UI deferred to the end of it.
    """
    if not extend and is_trakt_boot_quiet():
        return
    if seconds is None:
        seconds = TRAKT_BOOT_QUIET_SECONDS
        try:
            import xbmc as _xbmc_boot
            if _xbmc_boot.getCondVisibility('System.HasAddon(plugin.video.gears)'):
                seconds = max(seconds, 120)
            if _xbmc_boot.getCondVisibility('System.HasAddon(script.trakt)'):
                seconds = max(seconds, 90)
        except Exception:
            pass
    try:
        seconds = max(int(seconds), 1)
    except (ValueError, TypeError):
        seconds = TRAKT_BOOT_QUIET_SECONDS
    until = int(time.time()) + seconds
    current = _trakt_prop_unix(PROP_TRAKT_BOOT_QUIET_UNTIL)
    if until > current:
        control.window.setProperty(PROP_TRAKT_BOOT_QUIET_UNTIL, str(until))
        c.log(f'[Trakt] Boot quiet armed for {seconds}s — background API deferred', 1)


def is_trakt_boot_quiet():
    """True during post-startup quiet period for background Trakt work."""
    return _trakt_prop_unix(PROP_TRAKT_BOOT_QUIET_UNTIL) > int(time.time())


def should_defer_background_trakt():
    """Background tasks (scrobble queue, silent sync, monitor) should wait."""
    return is_trakt_boot_quiet() or is_trakt_rate_limited()


def with_db_connection(return_on_error=None, return_as_dict=True):
    """Decorator to handle database connections automatically (auto-injects dbcon/dbcur and auto-closes)."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            dbcon, dbcur = None, None
            try:
                dbcon = get_connection(control.traktsyncFile, return_as_dict)
                if not dbcon:
                    return return_on_error

                dbcur = get_connection_cursor(dbcon)
                if not dbcur:
                    return return_on_error

                # Inject dbcon and dbcur as first two arguments
                return func(dbcon, dbcur, *args, **kwargs)

            except database.Error as e:
                c.log(f"[Trakt DB] Error in {func.__name__}: {e}")
                return return_on_error
            finally:
                # Always close connections
                if dbcur:
                    try:
                        dbcur.close()
                    except Exception:
                        pass  # Safe to ignore DB cursor close errors
                if dbcon:
                    try:
                        dbcon.close()
                    except Exception:
                        pass  # Safe to ignore DB connection close errors

        return wrapper
    return decorator


def get_trakt(url, post=None, conditional_headers=None):
    """Make a request to the Trakt API with automatic token refresh."""
    try:
        if is_trakt_rate_limited():
            c.log('[Trakt] API call skipped — rate-limit backoff active', 1)
            return None
        # Check if token needs refresh before making request
        check_and_refresh_token()
        return _handle_the_request(url, post, conditional_headers)
    except requests.RequestException as e:
        c.log(f'get_trakt network error: {e}')
        # Notify user once per session — not during playback / wake quiet
        if not control.window.getProperty('midnight.trakt_network_notified'):
            try:
                if not should_suppress_trakt_ui():
                    control.window.setProperty('midnight.trakt_network_notified', 'true')
                    c.infoDialog('Trakt could not be reached. Check your internet connection.', sound=True)
            except Exception:
                pass


def check_and_refresh_token():
    """
    Check if access token is expired or about to expire.

    NOTE: We do NOT proactively refresh tokens anymore (switched to reactive-only approach).
    This function now only validates token expiration for logging purposes.
    Actual refresh happens reactively when Trakt returns 401 in msg_handler().

    This is more robust because:
    - Tokens last their full lifetime (~90 days)
    - Refresh tokens are only used when actually needed (on 401)
    - Reduces risk of burning refresh tokens due to crashes/race conditions
    """
    try:
        # Check if authentication completely failed (tokens were cleared by another thread)
        if control.window.getProperty('midnight.trakt_auth_failed') == 'true':
            return  # Auth is broken, don't check anything

        if not get_trakt_credentials_info():
            return  # No token configured

        expires_at_str = c.get_setting('trakt.expires_at')
        if not expires_at_str:
            # Fall back to legacy key written by trakt_api.py
            expires_at_str = c.get_setting('trakt.expires')
            if expires_at_str:
                # Migrate to canonical key
                c.set_setting('trakt.expires_at', expires_at_str)
                c.set_setting('trakt.expires', '')
            else:
                # No expiration data - log it but don't refresh proactively
                c.log('[Trakt] No token expiry stored - will use reactive refresh on first 401', 1)
                return

        try:
            expires_at = int(expires_at_str)
            current_time = int(time.time())
            time_remaining = expires_at - current_time

            # Just log the status - NO proactive refresh
            if time_remaining < 0:
                # Token expired - will be refreshed reactively on next API call (401)
                c.log(f'[Trakt] Access token expired {abs(time_remaining)}s ago - will refresh on next API call', 1)
            else:
                hours_remaining = time_remaining / 3600
                days_remaining = hours_remaining / 24
                # Only log once per session
                if not control.window.getProperty('midnight.trakt_token_logged'):
                    control.window.setProperty('midnight.trakt_token_logged', 'true')
                    c.log(f'[Trakt] Access token valid for {hours_remaining:.1f}h ({days_remaining:.1f} days)', 1)

        except (ValueError, TypeError) as e:
            c.log(f'[Trakt] Could not parse token expiration: {e}', 1)

    except Exception as e:
        c.log(f'[Trakt] Exception in check_and_refresh_token: {e}')
        # Don't fail - reactive refresh will handle auth issues


def check_trakt_health(force=False) -> str:
    """Make a lightweight API probe to determine Trakt connectivity and auth status at startup.

    Sets the midnight.trakt_status window property to one of:
      'ok'             — credentials valid and Trakt reachable
      'no_credentials' — user has not configured Trakt
      'no_network'     — network unreachable or timeout
      'locked'         — Trakt account is locked (423)
      'auth_dead'      — refresh token invalid / session expired

    Args:
        force: When True, run the probe even during boot-quiet (startup health check).

    Returns the status string.
    """
    if not get_trakt_credentials_info():
        # Distinguish "never set up" from "tokens were wiped after auth".
        # If the user name is still stored but the tokens are gone, treat it as
        # auth_dead so the startup dialog prompts re-authorization.
        user = c.get_setting('trakt.user').strip()
        if user:
            status = 'auth_dead'
            control.window.setProperty('midnight.trakt_status', status)
            c.log('[Trakt] Health check: user configured but tokens missing — re-auth required')
        else:
            status = 'no_credentials'
            control.window.setProperty('midnight.trakt_status', status)
            c.log('[Trakt] Health check: no credentials configured')
        return status

    if not force and (is_trakt_boot_quiet() or is_trakt_rate_limited()):
        status = 'ok'
        control.window.setProperty('midnight.trakt_status', status)
        c.log('[Trakt] Health check deferred — boot quiet / rate limit (assuming credentials ok)', 1)
        return status

    token = c.get_setting('trakt.token')
    headers = {
        'Content-Type': 'application/json',
        'trakt-api-key': CLIENT_ID,
        'trakt-api-version': '2',
        'User-Agent': USER_AGENT,
        'Authorization': f'Bearer {token}',
    }
    url = urljoin(BASE_URL, '/users/settings')

    try:
        response = requests.get(url, headers=headers, timeout=10)
        status_code = str(response.status_code)

        if status_code == '200':
            status = 'ok'
        elif status_code == '423':
            status = 'locked'
        elif status_code in ('401', '403'):
            # Try to refresh — 3-way result: True=ok, None=network, False=auth dead
            # Use the same lock as msg_handler() to prevent race condition with widgets/concurrent calls
            with _trakt_refresh_lock:
                # Check if another thread already handled auth failure
                if control.window.getProperty('midnight.trakt_auth_failed') == 'true':
                    status = 'auth_dead'
                else:
                    # Check if token was already rotated by another thread
                    current_token = c.get_setting('trakt.token')
                    if current_token and f'Bearer {current_token}' != headers.get('Authorization', ''):
                        # Token was refreshed by another thread - we're good
                        status = 'ok'
                    else:
                        # We're first - do the refresh
                        refresh_result = token_refresh(headers, url, None)
                        if refresh_result is True:
                            status = 'ok'
                        elif refresh_result is None:
                            status = 'no_network'
                        else:
                            status = 'auth_dead'
                            control.window.setProperty('midnight.trakt_auth_failed', 'true')
        else:
            c.log(f'[Trakt] Health check: unexpected status {status_code}, treating as ok', 1)
            status = 'ok'

    except requests.RequestException as e:
        c.log(f'[Trakt] Health check: network error: {e}', 1)
        status = 'no_network'

    control.window.setProperty('midnight.trakt_status', status)
    c.log(f'[Trakt] Health check result: {status}')
    return status


def _handle_the_request(url, post, conditional_headers=None):
    """Perform a Trakt API request and handle retries on token refresh."""
    # Normalize URL - if not absolute, use BASE_URL
    if not url.startswith('http'):
        url = urljoin(BASE_URL, url)

    # Use json= where possible so requests sets content-type and encodes safely
    payload = post if post else None

    headers = {
        'Content-Type': 'application/json',
        'trakt-api-key': CLIENT_ID,
        'trakt-api-version': '2',
        'User-Agent': USER_AGENT,
    }

    # OAuth endpoints don't use Authorization header (they use client_id/secret in body)
    is_oauth_endpoint = '/oauth/' in url
    if not is_oauth_endpoint and _should_attach_trakt_auth():
        headers['Authorization'] = f'Bearer {c.get_setting("trakt.token")}'

    # Debug logging for OAuth requests
    if is_oauth_endpoint:
        if payload:
            # Don't log secrets, just structure
            pass  # Debug logging removed

    # Add conditional request headers (If-Modified-Since, If-None-Match)
    if conditional_headers:
        headers.update(conditional_headers)

    # Try the request; if token refresh happens, retry once
    retried = False
    unauth_retried = False
    while True:
        try:
            if payload is not None:
                response = session.post(url, json=payload, headers=headers, timeout=30)
            else:
                response = session.get(url, headers=headers, timeout=30)
        except requests.RequestException as e:
            c.log(f"[Trakt] Network exception when requesting {url}: {e}")
            raise

        response.encoding = 'utf-8'
        status_code = response.status_code

        # Debug logging for OAuth responses
        if '/oauth/' in url:
            if response.text and len(response.text) < 500:
                pass  # Debug logging removed

        # Dead/revoked Bearer breaks public endpoints (trending/popular) with 401 +
        # non-JSON body. Drop Authorization once and retry unauthenticated.
        if (
            str(status_code) in ('401', '403')
            and not is_oauth_endpoint
            and not unauth_retried
            and headers.get('Authorization')
            and payload is None
            and _is_public_trakt_get(url)
        ):
            unauth_retried = True
            headers.pop('Authorization', None)
            c.log(f'[Trakt] Auth rejected - retrying public endpoint without Bearer: {url}', 1)
            continue

        # If not 2xx, let the message handler decide whether a retry is appropriate
        if not status_code or not str(status_code).startswith('2'):
            should_retry = msg_handler(url, response, str(status_code), payload, headers)
            if should_retry and not retried:
                retried = True
                # Update Authorization header if token was rotated in msg_handler/token_refresh
                # But skip for OAuth endpoints
                if not is_oauth_endpoint and _should_attach_trakt_auth():
                    headers['Authorization'] = f'Bearer {c.get_setting("trakt.token")}'
                # loop to retry once
                continue
        # Return raw text, headers and status for caller convenience
        return response.text, response.headers, status_code


def msg_handler(url, response, status_code, post, headers) -> bool:
    """Handle non-2xx responses. Return True when caller should retry the request once (e.g., after token refresh)."""
    # Default: do not retry
    if status_code in ['401', '403']:
        # Check if auth already failed (tokens cleared by another thread)
        if control.window.getProperty('midnight.trakt_auth_failed') == 'true':
            return False  # Don't retry, auth is broken

        # Cross-process guard: if another Kodi process (e.g. TraktMonitor service)
        # is already refreshing, wait briefly then use the tokens it saved.
        # Kodi window properties are shared across all addon processes.
        if control.window.getProperty(PROP_TRAKT_REFRESHING) == 'true':
            import xbmc as _xbmc_wait
            for _ in range(20):  # wait up to 2s
                _xbmc_wait.sleep(100)
                if control.window.getProperty(PROP_TRAKT_REFRESHING) != 'true':
                    break
            current_token = c.get_setting('trakt.token')
            if current_token and current_token != headers.get('Authorization', '').replace('Bearer ', ''):
                headers['Authorization'] = f'Bearer {current_token}'
                return True
            return False

        if is_trakt_rate_limited():
            c.log('[Trakt] Token refresh skipped — rate-limit backoff active', 1)
            return False

        # Claim the refreshing flag BEFORE acquiring the lock.  This shrinks the
        # cross-process TOCTOU window: a concurrent process that already passed the
        # getProperty check above will see 'true' on any subsequent check.
        # The outer try/finally guarantees the flag is always cleared on exit.
        control.window.setProperty(PROP_TRAKT_REFRESHING, 'true')
        try:
            # Use a threading.Lock to serialize token refresh across threads within this process.
            with _trakt_refresh_lock:
                # Re-check auth_failed: the thread that just held the lock may have
                # failed and cleared the tokens.
                if control.window.getProperty('midnight.trakt_auth_failed') == 'true':
                    return False

                # Check if the token was already rotated by the thread that just held
                # the lock. If our header is stale, update and retry without refreshing.
                current_token = c.get_setting('trakt.token')
                if current_token and f'Bearer {current_token}' != headers.get('Authorization', ''):
                    headers['Authorization'] = f'Bearer {current_token}'
                    return True

                # We are the first — do the refresh.
                refresh_result = token_refresh(headers, url, post)
                if refresh_result is True:
                    return True
                suppress_ui = should_suppress_trakt_ui()
                if refresh_result is None:
                    # Network error during token refresh — tokens may still be valid, don't clear them
                    c.log('[Trakt] Token refresh failed due to network error', 1)
                    if not control.window.getProperty('midnight.trakt_network_notified'):
                        control.window.setProperty('midnight.trakt_network_notified', 'true')
                        if not suppress_ui:
                            c.infoDialog('Trakt could not be reached. Check your internet connection.', sound=True)
                        else:
                            c.log('[Trakt] Suppressed network error dialog — playback/wake quiet', 1)
                else:
                    # Auth failure — refresh token invalid or expired
                    c.log('[Trakt] Token refresh failed - authentication required', 1)
                    control.window.setProperty('midnight.trakt_auth_failed', 'true')
                    if not control.window.getProperty('midnight.trakt_auth_notified'):
                        control.window.setProperty('midnight.trakt_auth_notified', 'true')
                        if not suppress_ui:
                            if is_trakt_boot_quiet():
                                control.window.setProperty('midnight.trakt_auth_pending', 'true')
                                c.log('[Trakt] Re-auth deferred — boot quiet (toast after startup)', 1)
                            else:
                                # Never okDialog here — it freezes all of Kodi (Gears, etc.).
                                c.infoDialog(
                                    'Trakt session expired - re-authorize in Midnight > Settings > Accounts > Trakt',
                                    sound=False,
                                    time=7000,
                                )
                        else:
                            control.window.setProperty('midnight.trakt_auth_pending', 'true')
                            c.log('[Trakt] Suppressed re-auth toast — playback/wake quiet', 1)
                return False
        finally:
            control.window.clearProperty(PROP_TRAKT_REFRESHING)
    elif status_code == '405':
        c.log(f'[Trakt] Method Not Allowed (405) for {url}', 1)
        return False
    elif not response:
        # Log server errors silently — startup health check handles user-facing messaging
        if status_code != '404':
            c.log(f'[Trakt] Server did not respond (status={status_code}): {trakt_response_codes.get(status_code, "No status")}', 1)
        return False
    elif status_code == '423':
        c.log(f'[Trakt] Account locked (423): {trakt_response_codes[status_code]}', 1)
        if control.window.getProperty('midnight.trakt_status') != 'locked':
            control.window.setProperty('midnight.trakt_status', 'locked')
            if not should_suppress_trakt_ui():
                # Toast only — okDialog would lock every addon on the box.
                c.infoDialog(
                    'Trakt account locked - unlock at trakt.tv, then re-authorize in Midnight settings',
                    sound=False,
                    time=7000,
                )
        return False
    elif status_code == '429':
        retry_time = _parse_retry_after(response)
        set_trakt_rate_limit(retry_time)
        c.log(f'[Trakt] Rate limit (429): backing off {retry_time}s before retry', 1)
        control.sleep((retry_time + 1) * 1000)
        return True
    elif status_code == '422':
        # Validation errors - log the response body for debugging
        try:
            error_details = response.text if response else 'No response body'
            c.log(f"[Trakt] 422 Validation Error - {trakt_response_codes[status_code]}")
            c.log(f"[Trakt] Error details: {error_details}")
            if c.devmode:
                c.infoDialog(f'Trakt validation error: {error_details[:100]}', sound=False, time=3000)
        except Exception:
            c.log(f"[Trakt] {trakt_response_codes[status_code]}")
        return False
    elif status_code in ['404']:
        # 404 is expected for unwatched episodes/movies - don't show notification
        c.log(f"trakt status = {trakt_response_codes[status_code]}")
        return False
    elif status_code:
        c.log(f"trakt status = {trakt_response_codes[status_code]}")
        return False
    else:
        return False



def _truthy_trakt_flag(value) -> bool:
    return (value or '').strip().lower() in ('true', '1', 'yes')


def is_trakt_revoked() -> bool:
    """True after explicit user revoke until a successful auth_trakt."""
    try:
        if control.window.getProperty('midnight.trakt_revoked') == 'true':
            return True
    except Exception:
        pass
    return _truthy_trakt_flag(c.get_setting('trakt.revoked'))


def mark_trakt_revoked():
    """Block Classy adopt + settings_repair token rehydrate after intentional clear."""
    try:
        control.window.setProperty('midnight.trakt_revoked', 'true')
    except Exception:
        pass
    c.set_setting('trakt.revoked', 'true')


def clear_trakt_revoked():
    try:
        control.window.clearProperty('midnight.trakt_revoked')
    except Exception:
        pass
    c.set_setting('trakt.revoked', 'false')


def _classy_has_trakt_tokens() -> bool:
    try:
        import xbmc
        import xbmcaddon
        if not xbmc.getCondVisibility('System.HasAddon(plugin.video.classy)'):
            return False
        sibling = xbmcaddon.Addon('plugin.video.classy')
        token = (sibling.getSetting('trakt.token') or '').strip()
        refresh = (sibling.getSetting('trakt.refresh') or '').strip()
        return bool(token and refresh)
    except Exception:
        return False


def _try_adopt_sibling_trakt_tokens(headers) -> bool:
    '''
    Classy shares Midnight Trakt OAuth client. When Classy refreshes, our refresh
    token is revoked. Copy Classy's live tokens into Midnight so both keep working.

    Skipped after explicit revoke (trakt.revoked) until the user re-authorizes.
    '''
    try:
        if is_trakt_revoked():
            c.log('[Trakt] Sibling adopt skipped — user revoked Midnight Trakt auth', 1)
            return False
        import xbmc
        import xbmcaddon
        if not xbmc.getCondVisibility('System.HasAddon(plugin.video.classy)'):
            return False
        sibling = xbmcaddon.Addon('plugin.video.classy')
        s_token = (sibling.getSetting('trakt.token') or '').strip()
        s_refresh = (sibling.getSetting('trakt.refresh') or '').strip()
        s_user = (sibling.getSetting('trakt.user') or '').strip()
        s_exp = (sibling.getSetting('trakt.expires_at') or '').strip()
        if not s_token or not s_refresh:
            return False
        our_user = (c.get_setting('trakt.user') or '').strip()
        if our_user and s_user and our_user.casefold() != s_user.casefold():
            c.log('[Trakt] Sibling Classy tokens present but username differs — skip adopt', 1)
            return False
        if s_refresh == (c.get_setting('trakt.refresh') or '').strip():
            return False
        c.set_setting('trakt.token', s_token)
        c.set_setting('trakt.refresh', s_refresh)
        if s_exp:
            c.set_setting('trakt.expires_at', s_exp)
        if s_user and not our_user:
            c.set_setting('trakt.user', s_user)
        headers['Authorization'] = f'Bearer {s_token}'
        control.window.clearProperty('midnight.trakt_auth_failed')
        c.log(
            f'[Trakt] Adopted live tokens from plugin.video.classy '
            f'(token_len={len(s_token)}, refresh_len={len(s_refresh)})',
            1,
        )
        return True
    except Exception as exc:
        c.log(f'[Trakt] Sibling token adopt skipped: {exc}', 1)
        return False


def token_refresh(headers, url, post) -> bool:
    """Refresh the Trakt OAuth access token using the refresh token."""
    try:
        if is_trakt_revoked():
            c.log('[Trakt] Token refresh skipped — user revoked Midnight Trakt auth', 1)
            return False

        if is_trakt_rate_limited():
            c.log('[Trakt] Token refresh deferred — rate-limit backoff active', 1)
            return None

        oauth = urljoin(BASE_URL, '/oauth/token')

        refresh_token = c.get_setting('trakt.refresh')
        if not refresh_token:
            # Empty Midnight tokens — Classy may still hold the live pair (shared OAuth client).
            if _try_adopt_sibling_trakt_tokens(headers):
                refresh_token = c.get_setting('trakt.refresh')
            if not refresh_token:
                c.log('[Trakt] No refresh token available', 1)
                return False

        c.log(f'[Trakt] Starting token refresh... (refresh_token length: {len(refresh_token)})', 1)

        opost = {
            'client_id': CLIENT_ID,
            'client_secret': CLIENT_SECRET,
            'redirect_uri': REDIRECT_URI,
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token
        }

        # Use fresh headers for token refresh (don't include Authorization)
        refresh_headers = {
            'Content-Type': 'application/json',
            'trakt-api-key': CLIENT_ID,
            'trakt-api-version': '2',
            'User-Agent': USER_AGENT,
        }

        # Use json= for safer encoding
        raw_response = session.post(oauth, json=opost, headers=refresh_headers, timeout=30)

        # Rate limit on oauth/token — transient; never clear credentials
        if raw_response.status_code == 429:
            retry_secs = _parse_retry_after(raw_response)
            set_trakt_rate_limit(retry_secs)
            c.log(f'[Trakt] Token refresh rate-limited (429) — backing off {retry_secs}s', 1)
            return None

        # Check HTTP status first
        if raw_response.status_code in (400, 401):
            # Another Kodi process may have spent the same refresh token in a concurrent
            # refresh (cross-process TOCTOU race).  Kodi's setSetting() can take up to
            # ~500 ms to propagate across processes.  Wait briefly, then use a *fresh*
            # Addon() instance to bypass the per-process settings cache.
            import time as _t
            _t.sleep(0.4)
            _xa_refresh = None
            try:
                import xbmcaddon as _xa_refresh
                current_refresh = _xa_refresh.Addon('plugin.video.midnight').getSetting('trakt.refresh')
            except Exception:
                current_refresh = c.get_setting('trakt.refresh')
            if current_refresh and current_refresh != refresh_token:
                c.log('[Trakt] Refresh 401/400 but token already rotated by another process — using new tokens', 1)
                try:
                    new_token = (
                        _xa_refresh.Addon('plugin.video.midnight').getSetting('trakt.token')
                        if _xa_refresh else c.get_setting('trakt.token')
                    )
                    if new_token:
                        headers['Authorization'] = f'Bearer {new_token}'
                except Exception:
                    pass
                return True

            # Same Trakt OAuth client as Classy — Classy refresh invalidates our refresh
            # token. Adopt Classy's live tokens when present instead of wiping ours.
            if _try_adopt_sibling_trakt_tokens(headers):
                return True

            if raw_response.status_code == 401:
                c.log('[Trakt] Refresh token expired or invalid - user must re-authenticate', 1)
            else:
                c.log(
                    '[Trakt] Invalid refresh token (HTTP 400) — NOT clearing credentials '
                    '(avoids default=true settings poison; re-auth when ready)',
                    1,
                )
            # Do NOT clear trakt.token / trakt.refresh here. setSetting('') + repair rewrite
            # leaves <setting default="true" id="trakt.token" /> which Kodi treats as empty
            # forever. Session flag is enough for the re-auth dialog.
            control.window.setProperty('midnight.trakt_auth_failed', 'true')
            return False

        if raw_response.status_code != 200:
            c.log(f'[Trakt] Token refresh failed with status {raw_response.status_code}', 1)
            # 5xx / gateway errors are transient — do not treat as auth death
            if raw_response.status_code >= 500:
                return None
            return False

        response = raw_response.json()

        # Check for error in response body
        if response and isinstance(response, dict) and 'error' in response:
            error_msg = response.get('error_description', response['error'])
            c.log(f'[Trakt] Token refresh error: {error_msg}', 1)
            return False

        # Validate required fields
        if not all(k in response for k in ['access_token', 'refresh_token', 'expires_in']):
            c.log('[Trakt] Token refresh response missing required fields', 1)
            return False

        # Save new tokens with expiration timestamp
        token = response['access_token']
        refresh = response['refresh_token']
        expires_in = int(response['expires_in'])  # Trakt now uses 86400 (1 day)

        # Calculate expiration timestamp (current time + expires_in)
        expiration_timestamp = int(time.time()) + expires_in

        # Log the actual expires_in value we got from Trakt
        expires_hours = expires_in / 3600
        expires_days = expires_hours / 24
        c.log(f'[Trakt] Token refresh successful! expires_in={expires_in}s ({expires_hours:.1f}h / {expires_days:.1f}days)', 1)
        c.log(f'[Trakt] New refresh_token length: {len(refresh)}', 1)

        c.set_setting('trakt.token', token)
        c.set_setting('trakt.refresh', refresh)
        c.set_setting('trakt.expires_at', str(expiration_timestamp))

        # Clear any previous auth failed flag since refresh succeeded
        control.window.clearProperty('midnight.trakt_auth_failed')

        # Update Authorization header for retry if provided
        try:
            headers['Authorization'] = f'Bearer {token}'
        except Exception:
            pass

        return True

    except requests.RequestException as e:
        c.log(f'[Trakt] Network error during token refresh: {e}', 1)
        return None  # Distinct sentinel: network error (token may still be valid)
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        c.log(f'[Trakt] Exception in token_refresh: {e}', 1)
        return False

def _maybe_notify_trakt_disconnected():
    """Show a one-time corner notification when Trakt is disconnected mid-session.

    Only fires when credentials are confirmed dead (not on genuine empty results or
    network hiccups).  Suppressed during playback and after it has fired once.
    """
    if control.window.getProperty('midnight.trakt_disconnected_notified') == 'true':
        return

    auth_dead = control.window.getProperty('midnight.trakt_auth_failed') == 'true'
    if not auth_dead:
        user = c.get_setting('trakt.user').strip()
        token = c.get_setting('trakt.token')
        auth_dead = bool(user and not token)

    if not auth_dead:
        return

    control.window.setProperty('midnight.trakt_disconnected_notified', 'true')
    try:
        if not should_suppress_trakt_ui():
            control.infoDialog(
                'Trakt not connected - go to Settings > Accounts > Trakt to re-authorize.',
                sound=False,
                time=6000,
            )
        else:
            c.log('[Trakt] Suppressed disconnected notification — playback/wake quiet', 1)
    except Exception:
        pass


def getTraktAsJson(url, post=None):
    """Make a request to the Trakt API and return the response as a JSON object/list."""
    try:
        if isinstance(url, str):
            result = get_trakt(url, post) if post else get_trakt(url)
        else:
            return None

        if result:
            # _sort_list_by_header expects (body, headers) - support (body, headers, status) too
            if isinstance(result, tuple) and len(result) >= 2:
                return _sort_list_by_header(result[:2])
            return _sort_list_by_header(result)
        # Handle the case where get_trakt returns None
        c.log('getTraktAsJson Error: get_trakt returned None')
        _maybe_notify_trakt_disconnected()
        return None
    except (TypeError, AttributeError, IndexError) as e:
        failure = traceback.format_exc()
        c.log(f'Traceback:: {failure}')
        c.log(f'getTraktAsJson exception: {e}')


# Stable cache keys (never use hash() — it is salted per Python process on 3.3+).
# Trakt #775: season/episode progress requires extended=progress + pagination (not full).
WATCHED_SHOWS_PATH = '/sync/watched/shows'
WATCHED_SHOWS_PROGRESS_PAGE_LIMIT = 100
WATCHED_SHOWS_MAX_PROGRESS_PAGES = 500
WATCHED_SHOWS_CACHE_KEY = 'trakt_watched_shows_extended'
WATCHED_SHOWS_CACHE_TTL = 300
PLAYBACK_EPISODES_EXTENDED_URL = urljoin(BASE_URL, 'sync/playback/episodes?extended=full')
PLAYBACK_EPISODES_CACHE_KEY = 'trakt_playback_episodes_extended'

_watched_shows_fetch_lock = threading.Lock()
_playback_episodes_fetch_lock = threading.Lock()


def watched_show_has_episode_detail(item):
    """True when a watched/shows row includes at least one positive-season episode list."""
    if not isinstance(item, dict):
        return False
    seasons = item.get('seasons')
    if not seasons:
        return False
    if isinstance(seasons, dict):
        seasons = list(seasons.values())
    for season in seasons:
        if not isinstance(season, dict):
            continue
        if int(season.get('number') or 0) <= 0:
            continue
        if season.get('episodes'):
            return True
    return False


def watched_shows_has_episode_detail(shows):
    """Empty library is valid; non-empty payloads must include season/episode rows."""
    if not isinstance(shows, list):
        return False
    if not shows:
        return True
    return any(watched_show_has_episode_detail(item) for item in shows)


def _watched_show_trakt_id(item):
    try:
        return str((item.get('show') or {}).get('ids', {}).get('trakt') or '')
    except Exception:
        return ''


def _merge_watched_shows_page(merged, seen, batch):
    added = 0
    for item in batch or []:
        if not isinstance(item, dict):
            continue
        tid = _watched_show_trakt_id(item)
        if tid and tid in seen:
            continue
        if tid:
            seen.add(tid)
        merged.append(item)
        added += 1
    return added


def fetch_watched_shows_progress(log_label=''):
    '''
    Paginated GET /sync/watched/shows?extended=progress (Trakt #775).

    Default/extended=full no longer includes season/episode rows on live Trakt API.
    '''
    label = f' ({log_label})' if log_label else ''
    merged = []
    seen = set()
    page = 1
    page_count = 0
    total_items = 0

    while page <= WATCHED_SHOWS_MAX_PROGRESS_PAGES:
        url = (
            f'{WATCHED_SHOWS_PATH}?extended=progress'
            f'&page={page}&limit={WATCHED_SHOWS_PROGRESS_PAGE_LIMIT}'
        )
        data = getTraktAsJson(url)
        if not isinstance(data, list) or not data:
            break

        pag = get_pagination_info()
        if pag.get('page_count'):
            page_count = pag['page_count']
        if pag.get('item_count'):
            total_items = pag['item_count']
        applied_limit = pag.get('limit') or len(data)

        added = _merge_watched_shows_page(merged, seen, data)
        if c.devmode:
            page_suffix = f'/{page_count}' if page_count else ''
            c.log(
                f'[Trakt] watched/shows progress{label} page {page}{page_suffix}: '
                f'+{added} shows (batch={len(data)} limit={applied_limit})'
            )

        if page_count and page >= page_count:
            break
        if len(data) < applied_limit:
            break
        page += 1

    if c.devmode and total_items and len(merged) != total_items:
        c.log(
            f'[Trakt] watched/shows progress{label}: merged {len(merged)} '
            f'vs header item_count={total_items}',
            1,
        )
    if merged:
        c.log(
            f'[Trakt] watched/shows{label}: {len(merged)} shows '
            f'(extended=progress, pages={page})'
        )
    return merged


def _read_watched_shows_cache():
    row = cache.cache_get(WATCHED_SHOWS_CACHE_KEY, table='trakt', timeout=WATCHED_SHOWS_CACHE_TTL)
    if not row:
        return None
    try:
        val = row.get('value')
        parsed = json.loads(val) if isinstance(val, str) else val
        return parsed if isinstance(parsed, list) else None
    except Exception:
        return None


def _store_watched_shows_cache(shows):
    if not isinstance(shows, list) or not watched_shows_has_episode_detail(shows):
        return False
    try:
        cache.cache_insert(
            WATCHED_SHOWS_CACHE_KEY,
            json.dumps(shows, separators=(',', ':')),
            table='trakt',
        )
        return True
    except Exception as exc:
        c.log(f'[Trakt] watched/shows cache write failed: {exc}', 1)
        return False


def invalidate_watched_shows_progress_cache():
    """Clear watched-show progress after scrobble / watch-state changes."""
    cache.cache_delete(WATCHED_SHOWS_CACHE_KEY, table='trakt')
    cache.cache_delete_by_prefix('trakt_next_episodes', table='trakt')


def invalidate_playback_episodes_cache():
    """Clear in-progress episode playback cache."""
    cache.cache_delete(PLAYBACK_EPISODES_CACHE_KEY, table='trakt')
    cache.cache_delete_by_prefix('trakt_playback_episodes', table='trakt')


def get_sync_watched_shows_extended(log_label=''):
    """Fetch sync/watched/shows (extended=progress, paginated) — serialized for widget bursts."""
    label = f' ({log_label})' if log_label else ''

    if not get_trakt_credentials_info():
        c.log(f'[Trakt] watched/shows{label}: skipped — Trakt not active tracker / not authorized', 1)
        return None

    with _watched_shows_fetch_lock:
        cached = _read_watched_shows_cache()
        if cached is not None and watched_shows_has_episode_detail(cached):
            if c.devmode:
                c.log(f'[Trakt] watched/shows{label}: cache hit ({len(cached)} shows)')
            return cached

        if cached is not None and not watched_shows_has_episode_detail(cached):
            c.log(
                f'[Trakt] watched/shows{label}: cached payload lacks episode detail — refetching',
                1,
            )
            invalidate_watched_shows_progress_cache()

        t0 = time.time()
        result = fetch_watched_shows_progress(log_label=log_label or 'watched_shows')
        if c.devmode:
            c.log(
                f'[Trakt] watched/shows{label}: fetch total_ms={int((time.time() - t0) * 1000)}'
            )

        if result and watched_shows_has_episode_detail(result):
            _store_watched_shows_cache(result)
            return result

        stale = cache.get_stale_json(WATCHED_SHOWS_CACHE_KEY, namespace='trakt')
        if stale and watched_shows_has_episode_detail(stale):
            c.log(f'[Trakt] watched/shows{label}: serving stale cache', 1)
            return stale

        if result and not watched_shows_has_episode_detail(result):
            c.log(
                f'[Trakt] watched/shows{label}: API response still lacks episode detail',
                1,
            )
        return None


def get_sync_playback_episodes_extended(log_label=''):
    """Fetch sync/playback/episodes once — serialized during home-widget reload bursts."""
    label = f' ({log_label})' if log_label else ''

    if not get_trakt_credentials_info():
        c.log(f'[Trakt] playback/episodes{label}: skipped — Trakt not active tracker / not authorized', 1)
        return None

    def _fetch(conditional_headers=None):
        return get_trakt(PLAYBACK_EPISODES_EXTENDED_URL, conditional_headers=conditional_headers)

    with _playback_episodes_fetch_lock:
        result = cache.get_with_etag(
            PLAYBACK_EPISODES_CACHE_KEY, _fetch, ttl_seconds=300, namespace='trakt',
        )
        if not result:
            c.log(f'[Trakt] playback/episodes etag miss — direct fetch{label}', 1)
            result = getTraktAsJson(PLAYBACK_EPISODES_EXTENDED_URL)
        if not result:
            result = cache.get_stale_json(PLAYBACK_EPISODES_CACHE_KEY, namespace='trakt')
            if result:
                c.log(f'[Trakt] playback/episodes serving stale cache{label}', 1)
    return result

def deleteTraktPlayback(playback_id):
    """Delete a playback progress item from Trakt."""
    try:
        if not playback_id:
            c.log('[Trakt] deleteTraktPlayback: No playback_id provided')
            return False

        if not get_trakt_credentials_info():
            c.log('[Trakt] deleteTraktPlayback: Not authenticated')
            return False

        check_and_refresh_token()

        url = urljoin(BASE_URL, f'/sync/playback/{playback_id}')

        headers = {
            'Content-Type': 'application/json',
            'trakt-api-key': CLIENT_ID,
            'trakt-api-version': '2',
            'User-Agent': USER_AGENT,
            'Authorization': f'Bearer {c.get_setting("trakt.token")}'
        }

        response = session.delete(url, headers=headers, timeout=30)
        status_code = response.status_code


        # 204 = Success - no content to return (standard for DELETE)
        # 404 = Item not found (already deleted or never existed)
        if status_code in [204, 404]:
            return True
        else:
            c.log(f'[Trakt] Failed to delete playback {playback_id}: {status_code}')
            if response.text:
                pass  # Could log response text for debugging
            return False

    except requests.RequestException as e:
        c.log(f'[Trakt] Exception in deleteTraktPlayback: {e}')
        c.log(f'[Trakt] Traceback: {traceback.format_exc()}')
        return False


def get_resume_id_from_trakt_api(imdb: str, media_type: str = 'movie') -> int:
    """Fetch playback progress from the Trakt API and return the resume_id (playback id)
    for the entry matching the given IMDB ID.  Returns 0 if not found or not authenticated."""
    try:
        if not get_trakt_credentials_info():
            return 0

        check_and_refresh_token()

        trakt_media = 'movies' if media_type == 'movie' else 'episodes'
        endpoint = f'sync/playback/{trakt_media}?extended=full'
        result = getTraktAsJson(endpoint)

        if not result:
            return 0

        for item in result:
            item_type = item.get('type')
            if not item_type:
                continue
            ids = item.get(item_type, {}).get('ids', {})
            if ids.get('imdb') == imdb:
                resume_id = item.get('id', 0)
                c.log(f'[Trakt] get_resume_id_from_trakt_api: found resume_id={resume_id} for imdb={imdb}')
                return resume_id

        c.log(f'[Trakt] get_resume_id_from_trakt_api: no match found for imdb={imdb}')
        return 0

    except Exception as e:
        c.log(f'[Trakt] Exception in get_resume_id_from_trakt_api: {e}')
        return 0


def _header_get(headers, name):
    """Case-insensitive header lookup (plain dict or CaseInsensitiveDict)."""
    if not headers:
        return None
    try:
        val = headers.get(name)
        if val is not None:
            return val
    except Exception:
        pass
    target = name.lower()
    try:
        for key, val in headers.items():
            if str(key).lower() == target:
                return val
    except Exception:
        pass
    return None


def _pagination_from_headers(headers) -> dict:
    """Extract X-Pagination-* fields without relying on the global racey store."""
    def _int(value, default=None):
        try:
            if value is None or value == '':
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    info = {}
    page = _int(_header_get(headers, 'X-Pagination-Page'))
    limit = _int(_header_get(headers, 'X-Pagination-Limit'))
    page_count = _int(_header_get(headers, 'X-Pagination-Page-Count'))
    item_count = _int(_header_get(headers, 'X-Pagination-Item-Count'))
    if page is not None:
        info['page'] = page
    if limit is not None:
        info['limit'] = limit
    if page_count is not None:
        info['page_count'] = page_count
    if item_count is not None:
        info['item_count'] = item_count
    return info


def _sort_list_by_header(result):
    global _last_pagination_info
    resp, res_headers = result
    # Support empty or invalid responses gracefully
    try:
        # If response is a string, strip whitespace and abort early on empty body
        if isinstance(resp, str):
            s = resp.strip()
            if not s:
                return None
            resp = json.loads(s)
    except json.JSONDecodeError as e:
        preview = ''
        try:
            preview = (resp if isinstance(resp, str) else '')[:80]
        except Exception:
            pass
        c.log(f"[Trakt] Failed to decode JSON from Trakt response: {e} body={preview!r}")
        return []  # Return empty list instead of None for consistent handling
    except (TypeError, AttributeError) as e:
        c.log(f"[Trakt] Unexpected error parsing Trakt response: {e}")
        return []  # Return empty list instead of None for consistent handling

    # Extract pagination info from headers (if present; case-insensitive)
    if res_headers and isinstance(res_headers, dict):
        try:
            pagination_info = _pagination_from_headers(res_headers)
            if pagination_info:
                _last_pagination_info = pagination_info
        except (KeyError, ValueError, TypeError) as e:
            c.log(f"[Trakt] Error extracting pagination headers: {e}")

        # Apply sorting if sort headers present
        sort_by = _header_get(res_headers, 'X-Sort-By')
        sort_how = _header_get(res_headers, 'X-Sort-How')
        if sort_by and sort_how:
            resp = sort_list(sort_by, sort_how, resp)

    return resp
    #except Exception as e:
        #c.log('getTraktAsJson Error: ' + str(e))
        #pass


def get_pagination_info():
    """Get pagination info from the last Trakt API response."""
    return _last_pagination_info.copy()


def is_own_lists_index_url(url) -> bool:
    """True for GET /users/me/lists (not /items)."""
    if not url or not isinstance(url, str):
        return False
    u = url.split('?', 1)[0]
    return u.endswith('/users/me/lists')


def get_all_user_lists():
    """Fetch all user-created Trakt lists (paginated — default API cap can be as low as 10)."""
    all_lists = []
    page = 1
    limit = 250
    while page <= 20:
        batch = getTraktAsJson(f'/users/me/lists?page={page}&limit={limit}')
        if not batch:
            break
        if isinstance(batch, list):
            all_lists.extend(batch)
        else:
            break
        info = get_pagination_info()
        page_count = int(info.get('page_count', 1) or 1)
        if page >= page_count:
            break
        page += 1
    if c.devmode and all_lists:
        names = ', '.join(item.get('name', '?') for item in all_lists)
        c.log(f'[Trakt] get_all_user_lists: {len(all_lists)} list(s) — {names}')
    return all_lists


# --- Trakt list usage vs account limits (prototype / soak) -------------------
LIST_USAGE_CACHE_KEY = 'trakt_list_usage'
LIST_USAGE_CACHE_TTL_SEC = 600  # 10 minutes
PROP_LIST_USAGE_JSON = 'midnight.trakt_list_usage_json'
PROP_LIST_USAGE_UNTIL = 'midnight.trakt_list_usage_until'
PROP_LIST_USAGE_LIMITS_LOGGED = 'midnight.trakt_list_usage_limits_logged'
_NEAR_CAP_RATIO = 0.90


def _as_int(value, default=None):
    """Best-effort int coercion; returns default on failure."""
    try:
        if value is None or value == '':
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _read_list_usage_cache():
    """Return cached usage dict if still within the short TTL window."""
    try:
        until = _as_int(control.window.getProperty(PROP_LIST_USAGE_UNTIL), 0) or 0
        raw = control.window.getProperty(PROP_LIST_USAGE_JSON) or ''
        if until and raw and time.time() < until:
            data = json.loads(raw)
            if isinstance(data, dict):
                data['cached'] = True
                return data
    except Exception:
        pass
    try:
        from . import cache
        row = cache.cache_get(LIST_USAGE_CACHE_KEY, table='trakt', timeout=None)
        if not row:
            return None
        age = int(time.time()) - int(row.get('date', 0) or 0)
        if age > LIST_USAGE_CACHE_TTL_SEC:
            return None
        data = json.loads(row.get('value') or '')
        if isinstance(data, dict):
            data['cached'] = True
            return data
    except Exception:
        pass
    return None


def _store_list_usage_cache(payload: dict) -> None:
    """Cache usage in window props (fast) and control cache (session-stable)."""
    try:
        blob = json.dumps(payload, separators=(',', ':'))
        until = int(time.time()) + LIST_USAGE_CACHE_TTL_SEC
        control.window.setProperty(PROP_LIST_USAGE_JSON, blob)
        control.window.setProperty(PROP_LIST_USAGE_UNTIL, str(until))
    except Exception as exc:
        c.log(f'[Trakt] list usage window cache write failed: {exc}', 1)
    try:
        from . import cache
        cache.cache_insert(
            LIST_USAGE_CACHE_KEY,
            json.dumps(payload, separators=(',', ':')),
            table='trakt',
        )
    except Exception as exc:
        c.log(f'[Trakt] list usage db cache write failed: {exc}', 1)


def invalidate_trakt_list_usage_cache() -> None:
    """Drop brief list-usage cache after list/watchlist/collection mutations."""
    try:
        control.window.clearProperty(PROP_LIST_USAGE_JSON)
        control.window.clearProperty(PROP_LIST_USAGE_UNTIL)
    except Exception:
        pass
    try:
        from . import cache
        cache.cache_delete(LIST_USAGE_CACHE_KEY, table='trakt')
    except Exception:
        pass


def _trakt_json_with_pagination(url):
    """
    GET url and return (body, pagination_dict).

    Prefer this over getTraktAsJson + get_pagination_info() when the item_count
    header must not be clobbered by concurrent Trakt calls.
    """
    result = get_trakt(url)
    if not result:
        return None, {}
    if not (isinstance(result, tuple) and len(result) >= 2):
        return None, {}
    headers = result[1]
    pagination = _pagination_from_headers(headers)
    body = _sort_list_by_header(result[:2])
    return body, pagination


def _usage_bucket_current(bucket):
    """Read current usage from a /v3/users/me/usage bucket."""
    if not isinstance(bucket, dict):
        return None
    return _as_int(bucket.get('current'))


def _usage_bucket_max(bucket, is_vip: bool):
    """
    Plan cap from a usage bucket.

    Website bars use free or vip depending on account; do not invent defaults
    when the key is missing.
    """
    if not isinstance(bucket, dict):
        return None
    key = 'vip' if is_vip else 'free'
    return _as_int(bucket.get(key))


def _parse_trakt_settings_limits(raw_limits) -> dict:
    """
    Normalize /users/settings limits (fallback + debug).

    Live Jul-2026 keys observed:
      collection, favorites, list, notes, recommendations,
      saved_filters, search, watchlist
    Website plan bars (static lists 5, watchlist 250, total list items 1000)
    come from /v3/users/me/usage - settings.list.count can be grandfathered
    higher than the displayed Free bar.
    """
    limits = raw_limits if isinstance(raw_limits, dict) else {}
    list_lim = limits.get('list') if isinstance(limits.get('list'), dict) else {}
    watch_lim = limits.get('watchlist') if isinstance(limits.get('watchlist'), dict) else {}
    collection_lim = limits.get('collection') if isinstance(limits.get('collection'), dict) else {}
    # Also accept library / digital / physical aliases if Trakt renames later.
    library_lim = limits.get('library') if isinstance(limits.get('library'), dict) else {}
    digital_lim = limits.get('digital') if isinstance(limits.get('digital'), dict) else {}
    digital_lib_lim = (
        limits.get('digital_library')
        if isinstance(limits.get('digital_library'), dict)
        else {}
    )
    physical_lim = limits.get('physical') if isinstance(limits.get('physical'), dict) else {}
    physical_lib_lim = (
        limits.get('physical_library')
        if isinstance(limits.get('physical_library'), dict)
        else {}
    )

    per_list = _as_int(list_lim.get('item_count'))
    total_items = None
    for key in ('item_count_total', 'total_item_count', 'total_items', 'items'):
        if key in list_lim:
            total_items = _as_int(list_lim.get(key))
            break

    digital_max = None
    for src in (digital_lib_lim, digital_lim, collection_lim, library_lim):
        if not src:
            continue
        for key in ('item_count', 'digital_item_count', 'count'):
            if key in src:
                digital_max = _as_int(src.get(key))
                break
        if digital_max is not None:
            break

    physical_max = None
    for src in (physical_lib_lim, physical_lim):
        if not src:
            continue
        for key in ('item_count', 'physical_item_count', 'count'):
            if key in src:
                physical_max = _as_int(src.get(key))
                break
        if physical_max is not None:
            break

    return {
        'list_count': _as_int(list_lim.get('count')),
        'list_item_count': per_list,
        'list_item_count_total': total_items,
        'watchlist_item_count': _as_int(watch_lim.get('item_count')),
        'digital_library_item_count': digital_max,
        'physical_library_item_count': physical_max,
        'raw': limits,
        'raw_keys': sorted(str(k) for k in limits.keys()),
    }


def _parse_trakt_usage_endpoint(raw_usage, is_vip: bool) -> dict:
    """
    Normalize /v3/users/me/usage - same bars trakt.tv VIP/limits UI uses.

    Observed keys (Jul 2026): history, ratings, watchlist_items, list_items,
    total_list_items, static_lists, dynamic_lists, digital_library, total_notes.
    Each bucket: {current, free, vip}. physical_library absent in live payload.
    """
    usage = raw_usage if isinstance(raw_usage, dict) else {}
    out = {
        'source': 'v3_usage',
        'raw_keys': sorted(str(k) for k in usage.keys()),
        'limits': {},
        'usage': {},
    }
    mapping = (
        ('static_lists', 'lists', 'list_count'),
        ('total_list_items', 'list_items', 'list_item_count_total'),
        ('list_items', None, 'list_item_count'),  # per-list max; current often null
        ('watchlist_items', 'watchlist_items', 'watchlist_item_count'),
        ('digital_library', 'digital_library_items', 'digital_library_item_count'),
        ('physical_library', 'physical_library_items', 'physical_library_item_count'),
        ('dynamic_lists', 'dynamic_lists', 'dynamic_list_count'),
    )
    for api_key, usage_key, limit_key in mapping:
        bucket = usage.get(api_key)
        if not isinstance(bucket, dict):
            continue
        cur = _usage_bucket_current(bucket)
        mx = _usage_bucket_max(bucket, is_vip)
        if usage_key and cur is not None:
            out['usage'][usage_key] = cur
        if mx is not None:
            out['limits'][limit_key] = mx
    return out


def _log_settings_limits_keys(raw_limits) -> None:
    """Log /users/settings limits keys once per Kodi session (DEBUG)."""
    try:
        if control.window.getProperty(PROP_LIST_USAGE_LIMITS_LOGGED) == 'true':
            return
        control.window.setProperty(PROP_LIST_USAGE_LIMITS_LOGGED, 'true')
    except Exception:
        pass
    limits = raw_limits if isinstance(raw_limits, dict) else {}
    top = sorted(str(k) for k in limits.keys())
    c.log(f'[Trakt] list usage settings.limits keys: {top}', 0)
    for key in top:
        val = limits.get(key)
        if isinstance(val, dict):
            nested = sorted(f'{nk}={val.get(nk)!r}' for nk in val.keys())
            c.log(f'[Trakt] list usage settings.limits.{key}: {", ".join(nested)}', 0)
        else:
            c.log(f'[Trakt] list usage settings.limits.{key}={val!r}', 0)


def _watchlist_item_count_cheap() -> tuple:
    """
    Count watchlist items via pagination header (limit=1 body).

    Returns (count_or_None, note). Prefer /users/me/watchlist (matches site).
    Fallback sums /sync/watchlist/{movies,shows,seasons,episodes}.
    """
    try:
        body, info = _trakt_json_with_pagination('/users/me/watchlist?page=1&limit=1')
        count = _as_int(info.get('item_count'))
        if count is not None:
            return count, 'pagination_header'
        if body == []:
            return 0, 'empty_body'
    except Exception as exc:
        c.log(f'[Trakt] watchlist count (users) failed: {exc}', 1)

    # Typed /users/me/watchlist/* headers currently echo total, not type counts.
    # /sync/watchlist/* headers are type-accurate (movies+shows+seasons+eps).
    total = 0
    saw_any = False
    try:
        for media in ('movies', 'shows', 'seasons', 'episodes'):
            _body, info = _trakt_json_with_pagination(
                f'/sync/watchlist/{media}?page=1&limit=1'
            )
            part = _as_int(info.get('item_count'))
            if part is None:
                continue
            saw_any = True
            total += part
        if saw_any:
            return total, 'sync_watchlist_sum'
    except Exception as exc:
        c.log(f'[Trakt] watchlist count (sync sum) failed: {exc}', 1)
    return None, 'no_header'


def _collection_item_counts_cheap() -> dict:
    """
    Cheap collection/library counts via pagination headers (page=1&limit=1).

    digital_library on /v3/users/me/usage matched movies collection item_count
    for the probed account (not movies+shows+episodes). Still return parts so
    fallback can show a useful total when the usage endpoint is unavailable.
    """
    out = {
        'movies': None,
        'shows': None,
        'method': None,
    }
    try:
        movies = shows = None
        for media, key in (('movies', 'movies'), ('shows', 'shows')):
            _body, info = _trakt_json_with_pagination(
                f'/sync/collection/{media}?page=1&limit=1'
            )
            out[key] = _as_int(info.get('item_count'))
        if out['movies'] is not None or out['shows'] is not None:
            out['method'] = 'collection_pagination'
        return out
    except Exception as exc:
        c.log(f'[Trakt] collection count failed: {exc}', 1)
        out['method'] = 'error'
        return out


def _format_usage_ratio(used, maximum) -> str:
    """Format used/max with optional OVER marker; omit invented caps."""
    if used is None and maximum is None:
        return '?/?'
    if used is None:
        return f'?/{maximum}'
    if maximum is None:
        return str(used)
    note = ' OVER' if maximum > 0 and used > maximum else ''
    return f'{used}/{maximum}{note}'


def get_trakt_list_usage(force: bool = False) -> dict:
    """
    Fetch Trakt list / watchlist / library usage vs account limits.

    Prefers GET /v3/users/me/usage (same numbers as trakt.tv bars). Falls back
    to /users/settings limits + cheap pagination counts.

    Returns dict with keys: ok, error, limits, usage, near_cap_lists,
    elapsed_ms, cached, source, watchlist_method. On auth failure ok=False.
    """
    if not force:
        cached = _read_list_usage_cache()
        if cached:
            return cached

    started = time.perf_counter()
    result = {
        'ok': False,
        'error': None,
        'limits': {},
        'usage': {
            'lists': None,
            'list_items': None,
            'watchlist_items': None,
            'digital_library_items': None,
            'physical_library_items': None,
        },
        'near_cap_lists': [],
        'elapsed_ms': 0,
        'cached': False,
        'source': None,
        'watchlist_method': None,
        'collection_method': None,
    }

    if not get_trakt_credentials_info():
        result['error'] = 'no_credentials'
        result['elapsed_ms'] = int((time.perf_counter() - started) * 1000)
        c.log('[Trakt] list usage: skipped - no credentials')
        return result

    if control.window.getProperty('midnight.trakt_auth_failed') == 'true':
        result['error'] = 'auth_dead'
        result['elapsed_ms'] = int((time.perf_counter() - started) * 1000)
        c.log('[Trakt] list usage: skipped - auth failed / revoked')
        return result

    try:
        settings = getTraktAsJson('/users/settings')
        if not isinstance(settings, dict):
            result['error'] = 'settings_failed'
            result['elapsed_ms'] = int((time.perf_counter() - started) * 1000)
            c.log('[Trakt] list usage: /users/settings failed')
            return result

        raw_limits = settings.get('limits')
        _log_settings_limits_keys(raw_limits)
        settings_limits = _parse_trakt_settings_limits(raw_limits)
        user = settings.get('user') if isinstance(settings.get('user'), dict) else {}
        is_vip = bool(user.get('vip') or user.get('vip_ep') or user.get('vip_og'))

        # Primary: website-aligned usage endpoint.
        usage_raw = getTraktAsJson('/v3/users/me/usage')
        if isinstance(usage_raw, dict) and usage_raw:
            parsed = _parse_trakt_usage_endpoint(usage_raw, is_vip)
            result['source'] = 'v3_usage'
            result['limits'] = dict(parsed.get('limits') or {})
            result['usage'] = {
                'lists': parsed.get('usage', {}).get('lists'),
                'list_items': parsed.get('usage', {}).get('list_items'),
                'watchlist_items': parsed.get('usage', {}).get('watchlist_items'),
                'digital_library_items': parsed.get('usage', {}).get('digital_library_items'),
                'physical_library_items': parsed.get('usage', {}).get(
                    'physical_library_items'
                ),
                'dynamic_lists': parsed.get('usage', {}).get('dynamic_lists'),
            }
            result['watchlist_method'] = 'v3_usage'
            result['collection_method'] = (
                'v3_usage' if result['usage'].get('digital_library_items') is not None else None
            )
            c.log(
                f'[Trakt] list usage v3 keys: {parsed.get("raw_keys")}',
                0,
            )
        else:
            # Fallback: settings caps + cheap pagination / list index.
            result['source'] = 'settings_fallback'
            result['limits'] = {
                'list_count': settings_limits.get('list_count'),
                'list_item_count': settings_limits.get('list_item_count'),
                'list_item_count_total': settings_limits.get('list_item_count_total'),
                'watchlist_item_count': settings_limits.get('watchlist_item_count'),
                'digital_library_item_count': settings_limits.get(
                    'digital_library_item_count'
                ),
                'physical_library_item_count': settings_limits.get(
                    'physical_library_item_count'
                ),
            }

            lists = get_all_user_lists() or []
            list_count = len(lists)
            list_items = 0
            near_cap = []
            per_list_max = result['limits'].get('list_item_count')
            for entry in lists:
                if not isinstance(entry, dict):
                    continue
                items = _as_int(entry.get('item_count'), 0) or 0
                list_items += items
                if per_list_max and per_list_max > 0 and items >= int(
                    per_list_max * _NEAR_CAP_RATIO
                ):
                    near_cap.append({
                        'name': entry.get('name')
                        or (entry.get('ids') or {}).get('slug')
                        or '?',
                        'item_count': items,
                        'limit': per_list_max,
                    })

            watch_count, watch_method = _watchlist_item_count_cheap()
            coll = _collection_item_counts_cheap()
            # v3 digital_library.current matched /sync/collection/movies item_count
            # (not movies+shows+episodes) for the probed account.
            digital_count = None
            if coll.get('movies') is not None:
                digital_count = coll['movies']
            elif coll.get('shows') is not None:
                digital_count = coll['shows']

            result['usage'] = {
                'lists': list_count,
                'list_items': list_items,
                'watchlist_items': watch_count,
                'digital_library_items': digital_count,
                'physical_library_items': None,
            }
            result['near_cap_lists'] = near_cap
            result['watchlist_method'] = watch_method
            result['collection_method'] = coll.get('method')

        result['ok'] = True
        result['elapsed_ms'] = int((time.perf_counter() - started) * 1000)

        lim = result['limits']
        use = result['usage']
        c.log(
            '[Trakt] list usage: '
            f'lists={_format_usage_ratio(use.get("lists"), lim.get("list_count"))} '
            f'items={_format_usage_ratio(use.get("list_items"), lim.get("list_item_count_total"))} '
            f'watchlist={_format_usage_ratio(use.get("watchlist_items"), lim.get("watchlist_item_count"))} '
            f'digital={_format_usage_ratio(use.get("digital_library_items"), lim.get("digital_library_item_count"))} '
            f'source={result.get("source")} '
            f'in {result["elapsed_ms"]}ms'
        )

        to_cache = dict(result)
        to_cache['cached'] = False
        # Keep settings raw keys for debugging without bloating the dialog path.
        to_cache['settings_limits_keys'] = settings_limits.get('raw_keys') or []
        _store_list_usage_cache(to_cache)
        return result

    except Exception as exc:
        result['error'] = 'exception'
        result['elapsed_ms'] = int((time.perf_counter() - started) * 1000)
        c.log(f'[Trakt] list usage exception: {exc}', 1)
        return result


def format_trakt_list_usage_lines(usage_data: dict) -> list:
    """ASCII summary lines for dialogs / toasts."""
    if not usage_data:
        return ['Trakt list usage unavailable']

    err = usage_data.get('error')
    if err == 'no_credentials':
        return ['Trakt not authorized', 'Authorize in Settings > Accounts > Trakt']
    if err == 'auth_dead':
        return ['Trakt session expired', 'Re-authorize in Settings > Accounts > Trakt']
    if not usage_data.get('ok'):
        return ['Unable to load Trakt list usage', 'See the_crew.log']

    limits = usage_data.get('limits') or {}
    usage = usage_data.get('usage') or {}
    elapsed = usage_data.get('elapsed_ms') or 0
    cached = usage_data.get('cached')

    lines = []

    lists_u = usage.get('lists')
    lists_m = limits.get('list_count')
    if lists_u is not None or lists_m is not None:
        lines.append(f'Lists: {_format_usage_ratio(lists_u, lists_m)}')

    items_u = usage.get('list_items')
    items_total_m = limits.get('list_item_count_total')
    items_per_m = limits.get('list_item_count')
    if items_u is not None or items_total_m is not None:
        if items_total_m is not None:
            lines.append(f'List items: {_format_usage_ratio(items_u, items_total_m)}')
            if items_per_m is not None:
                lines.append(f'Per-list max: {items_per_m}')
        else:
            lines.append(
                f'List items: {items_u if items_u is not None else "?"}'
                + (f' (per-list max {items_per_m})' if items_per_m is not None else '')
            )

    watch_u = usage.get('watchlist_items')
    watch_m = limits.get('watchlist_item_count')
    if watch_u is not None or watch_m is not None:
        lines.append(f'Watchlist: {_format_usage_ratio(watch_u, watch_m)}')

    digital_u = usage.get('digital_library_items')
    digital_m = limits.get('digital_library_item_count')
    if digital_u is not None or digital_m is not None:
        lines.append(f'Digital library: {_format_usage_ratio(digital_u, digital_m)}')

    physical_u = usage.get('physical_library_items')
    physical_m = limits.get('physical_library_item_count')
    if physical_u is not None or physical_m is not None:
        lines.append(f'Physical library: {_format_usage_ratio(physical_u, physical_m)}')

    if not lines:
        lines.append('No Trakt usage fields returned')

    secs = elapsed / 1000.0
    cache_note = ' (cached)' if cached else ''
    lines.append(f'Loaded in {secs:.1f}s{cache_note}')

    near = usage_data.get('near_cap_lists') or []
    if near:
        lines.append('Near cap (>=90% per list):')
        for entry in near[:8]:
            lines.append(
                f'  {entry.get("name", "?")}: '
                f'{entry.get("item_count", "?")}/{entry.get("limit", "?")}'
            )
    return lines


def format_trakt_list_usage_snippet(usage_data: dict) -> str:
    """Short one-line snippet for appending to toasts (empty if unusable)."""
    if not usage_data or not usage_data.get('ok'):
        return ''
    usage = usage_data.get('usage') or {}
    limits = usage_data.get('limits') or {}
    parts = []
    if usage.get('lists') is not None or limits.get('list_count') is not None:
        parts.append(
            f'lists {_format_usage_ratio(usage.get("lists"), limits.get("list_count"))}'
        )
    if usage.get('list_items') is not None:
        parts.append(
            f'items {_format_usage_ratio(usage.get("list_items"), limits.get("list_item_count_total"))}'
        )
    if usage.get('watchlist_items') is not None:
        parts.append(
            f'wl {_format_usage_ratio(usage.get("watchlist_items"), limits.get("watchlist_item_count"))}'
        )
    if usage.get('digital_library_items') is not None:
        parts.append(
            f'digital {_format_usage_ratio(usage.get("digital_library_items"), limits.get("digital_library_item_count"))}'
        )
    return ' | '.join(parts)


def show_list_usage_dialog():
    """Tools entry: show Trakt list usage vs limits (user-initiated okDialog)."""
    try:
        if not get_trakt_credentials_info():
            control.okDialog(
                'Authorize Trakt first - Settings > Accounts > Trakt',
                heading='Trakt list usage',
            )
            return

        control.busy()
        try:
            data = get_trakt_list_usage(force=True)
        finally:
            control.idle()

        lines = format_trakt_list_usage_lines(data)
        control.okDialog('\n'.join(lines), heading='Trakt list usage')
    except Exception as exc:
        c.log(f'[Trakt] show_list_usage_dialog: {exc}', 1)
        try:
            control.idle()
        except Exception:
            pass
        control.okDialog('Unable to show list usage - see the_crew.log', heading='Trakt list usage')


def generate_qr_code_local(data, size=280):
    """Generate QR code locally using segno library"""
    try:
        import os
        import tempfile
        from .segno import make as make_qr

        # Create temp directory if it doesn't exist
        temp_dir = os.path.join(tempfile.gettempdir(), 'midnight_qr')
        os.makedirs(temp_dir, exist_ok=True)

        # Generate unique filename
        qr_filename = os.path.join(temp_dir, 'trakt_auth_qr.png')

        # Generate QR code
        qr = make_qr(data, error='H')  # High error correction

        # Calculate scale to get approximately desired size
        # QR codes are typically 21-177 modules depending on version
        # We want final size to be around 'size' pixels
        scale = max(1, size // 50)  # Reasonable scale factor

        # Save as PNG
        qr.save(qr_filename, scale=scale, border=2, dark='#000000', light='#FFFFFF')

        return qr_filename
    except Exception as e:
        import traceback
        # Fallback to external API
        from urllib.parse import quote
        return f'https://api.qrserver.com/v1/create-qr-code/?data={quote(data)}&size={size}x{size}'


def show_trakt_qr_dialog(verification_url, user_code, qr_path, device_code, interval, expires_in):
    """Show QR code dialog for Trakt authentication with polling"""
    try:
        import xbmcgui
        import threading
        import time


        class TraktQRViewer(xbmcgui.WindowXMLDialog):
            def __init__(self, *args, **kwargs):
                header = kwargs.pop('header', 'Trakt Authentication')
                message = kwargs.pop('message', '')
                qr_path = kwargs.pop('qr_path', '')
                device_code = kwargs.pop('device_code', '')
                interval = kwargs.pop('interval', 1)
                expires_in = kwargs.pop('expires_in', 600)
                # WindowXML must construct before any self.* (Fire Stick SWIG is strict)
                xbmcgui.WindowXMLDialog.__init__(self, *args)
                self.header = header
                self.message = message
                self.qr_path = qr_path
                self.device_code = device_code
                self.interval = interval
                self.expires_in = expires_in
                self.authenticated = False
                self.access_token = None
                self.polling = True
                self.poll_thread = None

            def onInit(self):
                try:
                    # Control IDs from LogViewer_QR.xml
                    HEADERLABEL = 101
                    TEXT = 502
                    QR_IMAGE = 501
                    CLOSEBUTTON = 503

                    self.getControl(HEADERLABEL).setLabel(self.header)

                    self.getControl(TEXT).setText(self.message)

                    self.getControl(QR_IMAGE).setImage(self.qr_path)

                    # Set focus to close button
                    self.setFocusId(CLOSEBUTTON)


                    # Start polling thread
                    self.poll_thread = threading.Thread(target=self._poll_for_token)
                    self.poll_thread.daemon = True
                    self.poll_thread.start()
                except Exception as e:
                    import traceback

            def _poll_for_token(self):
                """Poll for token in background thread"""
                try:
                    start_time = time.time()
                    while self.polling and (time.time() - start_time) < self.expires_in:
                        time.sleep(self.interval)

                        try:
                            r = getTraktAsJson(
                                '/oauth/device/token',
                                {
                                    'client_id': CLIENT_ID,
                                    'client_secret': CLIENT_SECRET,
                                    'code': self.device_code
                                })

                            if r and isinstance(r, dict) and 'access_token' in r:
                                self.authenticated = True
                                self.access_token = r
                                self.close()
                                return
                        except Exception as e:
                            # Expected during polling (400 error = pending)
                            pass

                    if self.polling:
                        self.close()
                except Exception as e:
                    c.log(f'[Trakt QR] Polling error: {e}')

            def onAction(self, action):
                # Close on back/esc
                if action.getId() in [10, 92]:  # ACTION_NAV_BACK, ACTION_PREVIOUS_MENU
                    self.polling = False
                    self.close()

            def onClick(self, controlId):
                if controlId == 503:  # Close button
                    self.polling = False
                    self.close()

        # Show dialog
        xml_file = 'LogViewer_QR.xml'
        addon_path = c.get_artwork_path()
        skin = c.appearance() or 'midnight'

        message = (
            f'Scan QR code with your mobile device\n\n'
            f'OR manually visit:\n{verification_url}\n\n'
            f'And enter code: {user_code}\n\n'
            f'Waiting for authentication...'
        )

        dialog = TraktQRViewer(
            xml_file,
            addon_path,
            skin,
            '1080i',
            header='Trakt Authentication',
            message=message,
            qr_path=qr_path,
            device_code=device_code,
            interval=interval,
            expires_in=expires_in
        )
        dialog.doModal()

        # Return result
        authenticated = dialog.authenticated
        access_token = dialog.access_token
        dialog.polling = False
        del dialog

        return authenticated, access_token
    except Exception as e:
        import traceback
        return False, None


def auth_trakt():
    control.begin_dialog_route()
    try:
        # Already looks connected: offer re-authorize (clear + device flow), not "do nothing".
        if has_trakt_token() is True:
            user = (c.get_setting('trakt.user') or '').strip() or 'Trakt'
            if not c.yesnoDialog(
                f'Trakt is connected as [B]{user}[/B].[CR][CR]'
                f'Re-authorize now? Use this if sync, manager, or watchlists stopped working.',
                'Trakt',
                nolabel='Cancel',
                yeslabel='Re-authorize',
            ):
                return
            c.log('[Trakt] User chose re-authorize — clearing credentials and starting device flow', 1)
            # Block Classy re-adopt while device flow runs (or if user cancels).
            mark_trakt_revoked()
            set_trakt_credentials('', '', '')
            control.window.clearProperty('midnight.trakt_auth_failed')
            control.window.clearProperty('midnight.trakt_auth_notified')
            control.window.clearProperty('midnight.trakt_disconnected_notified')
            control.window.clearProperty('midnight.trakt_auth_pending')

        result = getTraktAsJson('/oauth/device/code', {'client_id': CLIENT_ID})
        if not result or not isinstance(result, dict) or 'device_code' not in result:
            c.log(f'[Trakt] Failed to get device code: {result}')
            c.infoDialog('Failed to connect to Trakt', sound=True)
            return

        verification_url = result.get('verification_url', 'https://trakt.tv/activate')
        user_code = result.get('user_code', '')
        expires_in = int(result.get('expires_in', 0))
        device_code = result.get('device_code', '')
        interval = result.get('interval', 1)

        # Generate QR code locally using segno
        activation_url = f'{verification_url}/{user_code}'

        qr_filepath = generate_qr_code_local(activation_url, size=280)

        # Show QR dialog with polling (replaces progress dialog)
        authenticated, r = show_trakt_qr_dialog(
            verification_url,
            user_code,
            qr_filepath,
            device_code,
            interval,
            expires_in
        )

        if not authenticated or not r or not isinstance(r, dict) or 'access_token' not in r:
            c.log('[Trakt] Failed to get access token')
            c.infoDialog('Trakt authorization failed or timed out', sound=True)
            return

        token = r['access_token']
        refresh = r['refresh_token']
        expires_in = int(r.get('expires_in', 86400))  # Default to 1 day if missing

        # Log actual expires_in from Trakt for debugging
        expires_hours = expires_in / 3600
        expires_days = expires_hours / 24
        c.log(f'[Trakt Auth] Got tokens from Trakt: expires_in={expires_in}s ({expires_hours:.1f}h / {expires_days:.1f}days)', 1)
        c.log(f'[Trakt Auth] access_token length: {len(token)}, refresh_token length: {len(refresh)}', 1)

        # Calculate expiration timestamp
        expiration_timestamp = int(time.time()) + expires_in

        headers = {
                    'Content-Type': 'application/json',
                    'trakt-api-key': CLIENT_ID,
                    'trakt-api-version': 2,
                    'Authorization': f'Bearer {token}'
                }

        # Initialize user to empty string in case /users/me fails
        user = ''
        authed = ''

        # Save tokens FIRST so getTraktAsJson can use them
        set_trakt_credentials('', token, refresh, expiration_timestamp)

        # Now get username with the new token
        try:
            result = getTraktAsJson('/users/me')
            if result and isinstance(result, dict) and 'username' in result:
                user = result.get('username')
                authed = '' if user == '' else 'yes'
                # Update username
                c.set_setting('trakt.user', user)
        except Exception as e:
            c.log(f'[Trakt] Failed to get username: {e}')
            # Continue anyway with empty username

        c.infoDialog(f'Trakt authorized{": " + user if user else ""}', sound=True)
    except Exception as e:
        c.log(f'[Trakt] Authorization error: {e}')
        c.log(traceback.format_exc())
        control.openSettings('5.0')
    finally:
        control.finish_dialog_route()
        control.schedule_busy_dismiss((0.5, 2.0))


def revoke_trakt():
    """
    Clear stored Trakt credentials without starting device/QR auth.
    Entry point for action=revokeTrakt in crew.py.

    Best-effort POST /oauth/revoke; local clear always proceeds even if
    the network call fails. Sets trakt.revoked so Classy sibling adopt and
    settings_repair will not rehydrate Midnight tokens until auth_trakt succeeds.
    """
    try:
        if not has_trakt_token():
            c.infoDialog('No Trakt account is authorized.', heading='Trakt', sound=False)
            return

        user = (c.get_setting('trakt.user') or '').strip()
        if not c.yesnoDialog(
            f'Remove Trakt authorization{" for " + user if user else ""}?',
            'Trakt',
            nolabel='Cancel',
            yeslabel='Revoke',
        ):
            return

        token = (c.get_setting('trakt.token') or '').strip()
        if token:
            try:
                session.post(
                    urljoin(BASE_URL, '/oauth/revoke'),
                    json={
                        'token': token,
                        'client_id': CLIENT_ID,
                        'client_secret': CLIENT_SECRET,
                    },
                    timeout=10,
                )
            except Exception:
                pass

        # Mark before clear so a concurrent refresh/adopt cannot win the race.
        mark_trakt_revoked()
        set_trakt_credentials('', '', '')
        control.window.clearProperty('midnight.trakt_auth_failed')
        control.window.clearProperty('midnight.trakt_auth_notified')
        control.window.clearProperty('midnight.trakt_disconnected_notified')
        control.window.clearProperty('midnight.trakt_auth_pending')
        c.log('[Trakt] Authorization revoked (adopt + repair rehydrate blocked)', 1)
        if _classy_has_trakt_tokens():
            c.infoDialog(
                'Removed from Midnight. Classy may still be authorized.',
                heading='Trakt',
                sound=False,
            )
        else:
            c.infoDialog('Trakt authorization removed.', heading='Trakt', sound=False)
    except Exception as e:
        c.log(f'[Trakt] revoke_trakt error: {e}', 1)


def set_trakt_credentials(value, arg1, arg2, expires_at=None):
    c.set_setting('trakt.user', value)
    c.set_setting('trakt.token', arg1)
    c.set_setting('trakt.refresh', arg2)
    if expires_at:
        c.set_setting('trakt.expires_at', str(expires_at))
    elif not (arg1 or '').strip() and not (arg2 or '').strip():
        # Intentional clear — drop expiry so repair/status cannot treat us as authed.
        c.set_setting('trakt.expires_at', '')
        try:
            c.set_setting('trakt.expires', '')
        except Exception:
            pass
        try:
            c.set_setting('trakt.auth', '')
        except Exception:
            pass
        # Block Classy adopt + backup rehydrate until a successful auth writes tokens again.
        mark_trakt_revoked()
        try:
            from resources.lib.modules.settings_repair import purge_trakt_credentials_from_backups
            purge_trakt_credentials_from_backups(log=c.log)
        except Exception as exc:
            c.log(f'[Trakt] Backup purge after credential clear skipped: {exc}', 1)
    if (arg1 or '').strip() and (arg2 or '').strip():
        # Successful token write (auth or refresh) — allow sibling adopt again.
        clear_trakt_revoked()
        try:
            from resources.lib.modules.settings_repair import sync_trakt_credentials_into_backups
            sync_trakt_credentials_into_backups(log=c.log)
        except Exception as exc:
            c.log(f'[Trakt] Backup sync after credential write skipped: {exc}', 1)
    # Clear any stale auth-failed flags so new tokens are used immediately
    control.window.clearProperty('midnight.trakt_auth_failed')
    control.window.clearProperty('midnight.trakt_auth_notified')

def _should_attach_trakt_auth() -> bool:
    """True when Bearer should be sent. Skip when revoked or auth is known-dead.

    Public discovery endpoints (trending/popular/etc.) work without auth. A stale
    Bearer after revoke or failed refresh makes Trakt return 401 'Unauthorized',
    which then fails JSON parse and empties those lists.
    """
    _purge_stale_credentials_if_revoked()
    if is_trakt_revoked():
        return False
    try:
        if control.window.getProperty('midnight.trakt_auth_failed') == 'true':
            return False
    except Exception:
        pass
    return get_trakt_credentials_info()


def _is_public_trakt_get(url: str) -> bool:
    """Endpoints that Trakt serves without user OAuth (discovery / catalogs)."""
    path = (url or '').split('?', 1)[0].lower()
    # Strip scheme/host when absolute
    if '://' in path:
        try:
            path = '/' + path.split('://', 1)[1].split('/', 1)[1]
        except Exception:
            pass
    public_prefixes = (
        '/shows/trending', '/shows/popular', '/shows/anticipated',
        '/shows/played', '/shows/watched', '/shows/collected',
        '/movies/trending', '/movies/popular', '/movies/anticipated',
        '/movies/boxoffice', '/movies/played', '/movies/watched', '/movies/collected',
        '/search/', '/calendars/all/',
    )
    return any(p in path for p in public_prefixes)


def _purge_stale_credentials_if_revoked() -> None:
    """settings_repair can restore tokens from .bak while keeping trakt.revoked=true."""
    if not is_trakt_revoked():
        return
    user = (c.get_setting('trakt.user') or '').strip()
    token = (c.get_setting('trakt.token') or '').strip()
    refresh = (c.get_setting('trakt.refresh') or '').strip()
    if not (user or token or refresh):
        return
    try:
        if control.window.getProperty('midnight.trakt_revoked_creds_purged') == 'true':
            return
        control.window.setProperty('midnight.trakt_revoked_creds_purged', 'true')
    except Exception:
        pass
    c.log('[Trakt] Revoked flag set - clearing restored/stale credentials', 1)
    set_trakt_credentials('', '', '')


def has_trakt_token() -> bool:
    """
    Raw check: Trakt user/token/refresh are stored.
    Ignores tracker.service — use for auth/revoke/repair.
    For menus/progress/API use get_trakt_credentials_info() (XOR-gated).
    """
    _purge_stale_credentials_if_revoked()
    if is_trakt_revoked():
        return False
    user = c.get_setting('trakt.user').strip()
    token = c.get_setting('trakt.token')
    refresh = c.get_setting('trakt.refresh')
    return user != '' and token != '' and refresh != ''


def get_trakt_credentials_info() -> bool:
    """
    True when Trakt is the active tracker (XOR) AND authorized.
    Returns False when tracker.service is Simkl — even if tokens remain stored.
    """
    try:
        from . import simkl
        if not simkl.use_trakt_tracker():
            return False
    except Exception:
        pass
    return has_trakt_token()

#cm - indicators
def getTraktIndicatorsInfo() -> bool:
    """Check if Trakt indicators are enabled (only when Trakt is active tracker)."""
    indicator_setting = c.get_setting('indicators')
    alternative_indicator_setting = c.get_setting('indicators.alt')
    indicators = alternative_indicator_setting if get_trakt_credentials_info() else indicator_setting
    return indicators == '1'

def use_trakt_bookmarks() -> bool:
    if getTraktIndicatorsInfo():
        setting = c.get_setting('indicators.alt')
        return setting != '32314'
    return False

def get_trakt_addon_movie_info():
    """Check if Trakt is enabled and authorized in the Trakt addon."""
    if not c.addon_exists('script.trakt'):
        return False

    try:
        scrobble = control.addon('script.trakt').getSetting('scrobble_movie') or ''
        exclude_http = control.addon('script.trakt').getSetting('ExcludeHTTP') or ''
        authorization = control.addon('script.trakt').getSetting('authorization') or ''

    except Exception as e:
        c.log(f"Exception in get_trakt_addon_movie_info: {e}")
        return False


    return scrobble == 'true' and exclude_http == 'false' and authorization

def getTraktAddonEpisodeInfo():
    """Check if Trakt is enabled and authorized in the Trakt addon for episodes."""
    try:
        scrobble = control.addon('script.trakt').getSetting('scrobble_episode') == 'true'
        exclude_http = control.addon('script.trakt').getSetting('ExcludeHTTP') == 'false'
        authorization = control.addon('script.trakt').getSetting('authorization') != ''
    except LookupError:
        return False
    return scrobble and exclude_http and authorization

def _trakt_response_ok(result_data) -> bool:
    """True when get_trakt() returned a 2xx status."""
    try:
        if not result_data or len(result_data) < 3:
            return False
        return str(result_data[2]).startswith('2')
    except Exception:
        return False


def _manager_fail_dialog(name: str, result_data, context: str = 'API') -> None:
    """Log a Manager API failure and toast once; HTTP 420 gets a clear account-limit message."""
    body = (result_data[0] if result_data else '') or ''
    headers = result_data[1] if result_data and len(result_data) > 1 else {}
    status = result_data[2] if result_data and len(result_data) > 2 else 'unknown'
    account_limit = ''
    upgrade_url = ''
    try:
        if headers:
            account_limit = str(headers.get('X-Account-Limit') or headers.get('x-account-limit') or '')
            upgrade_url = str(headers.get('X-Upgrade-URL') or headers.get('x-upgrade-url') or '')
    except Exception:
        pass
    c.log(
        f'[Trakt Manager] {context} failed: status={status} '
        f'X-Account-Limit={account_limit or "-"} X-Upgrade-URL={upgrade_url or "-"} '
        f'body={body[:500]}'
    )
    if str(status) == '420':
        detail = ''
        try:
            data = json.loads(body) if body else {}
            if isinstance(data, dict):
                for key in ('message', 'error_description', 'error'):
                    val = data.get(key)
                    if val and isinstance(val, str) and val.strip():
                        detail = val.strip()
                        break
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        if detail and len(detail) <= 90:
            msg = f'Trakt account limit exceeded: {detail}'
        elif account_limit:
            # Empty body is common on 420; surface X-Account-Limit (e.g. 1000 free tier).
            msg = (
                f'Trakt account limit exceeded ({account_limit} items). '
                'Remove collection/list items or upgrade VIP on trakt.tv'
            )
        else:
            msg = (
                'Trakt account limit exceeded. '
                'Remove collection/list items or upgrade VIP on trakt.tv'
            )
        # Append cached usage only — never refetch on Manager fail path.
        try:
            cached_usage = _read_list_usage_cache()
            snippet = format_trakt_list_usage_snippet(cached_usage) if cached_usage else ''
            if snippet and len(msg) + len(snippet) < 110:
                msg = f'{msg} [{snippet}]'
        except Exception:
            pass
        c.infoDialog(msg, heading=name, sound=True, icon='ERROR', time=5000)
        return
    c.infoDialog('Trakt request failed - see the_crew.log', heading=name, sound=True, icon='ERROR')


def invalidate_trakt_watchlist_caches() -> None:
    """Drop cached movie/TV watchlist menus after Trakt Manager mutates watchlist."""
    from . import cache
    for module_path, cls_name in (
        ('resources.lib.indexers.movies', 'Movies'),
        ('resources.lib.indexers.tvshows', 'TVShows'),
    ):
        try:
            mod = __import__(module_path, fromlist=[cls_name])
            inst = getattr(mod, cls_name)()
            links = {inst.traktwatchlist_link, inst.traktwatchlist_link.split('?')[0]}
            for link in links:
                cache.cache_delete(cache._hash_function(inst.trakt_list, link, inst.trakt_user))
            c.log(f'[Trakt] Invalidated watchlist cache ({cls_name})')
        except Exception as exc:
            c.log(f'[Trakt] invalidate watchlist cache ({cls_name}): {exc}')
    invalidate_trakt_list_usage_cache()


def invalidate_trakt_user_list_caches() -> None:
    """Drop cached Movie/TV/Episode user-list menus after Trakt list create/add/remove."""
    from . import cache
    indexers = (
        ('resources.lib.indexers.movies', 'Movies'),
        ('resources.lib.indexers.tvshows', 'TVShows'),
        ('resources.lib.indexers.episodes', 'Episodes'),
    )
    for module_path, cls_name in indexers:
        try:
            mod = __import__(module_path, fromlist=[cls_name])
            inst = getattr(mod, cls_name)()
            for link in (inst.traktlists_link, inst.traktlikedlists_link):
                key = cache._hash_function(inst.trakt_user_list, link, inst.trakt_user)
                cache.cache_delete(key)
        except Exception as exc:
            c.log(f'[Trakt] invalidate user list cache ({cls_name}): {exc}')
    invalidate_trakt_list_usage_cache()


def _invalidate_trakt_list_items_cache(endpoint: str) -> None:
    """Drop cached list contents when a custom list is mutated."""
    if '/users/me/lists/' not in endpoint:
        return
    try:
        slug = endpoint.split('/users/me/lists/', 1)[1].split('/')[0]
        if not slug:
            return
        from . import cache
        from ..indexers.movies import Movies
        from ..indexers.tvshows import TVShows
        m = Movies()
        t = TVShows()
        list_url = m.traktlist_link % ('me', slug)
        for inst, method in ((m, m.trakt_list), (t, t.trakt_list)):
            key = cache._hash_function(method, list_url, inst.trakt_user)
            cache.cache_delete(key)
        c.log(f'[Trakt] Invalidated list item cache for slug={slug}')
    except Exception as exc:
        c.log(f'[Trakt] invalidate list items cache: {exc}')


def _sync_local_after_manager(endpoint: str, content: str, imdb: str, tmdb: str) -> None:
    """Keep local Trakt mirrors in sync after Trakt Manager mutates collection/watch state."""
    if not endpoint:
        return
    try:
        if endpoint in ('/sync/collection', '/sync/collection/remove'):
            get_trakt_collection('movies' if content == 'movie' else 'shows')
            invalidate_trakt_list_usage_cache()
        elif endpoint in ('/sync/watchlist', '/sync/watchlist/remove'):
            invalidate_trakt_watchlist_caches()
        elif '/users/me/lists/' in endpoint:
            invalidate_trakt_user_list_caches()
            _invalidate_trakt_list_items_cache(endpoint)
    except Exception as exc:
        c.log(f'[Trakt] Local sync after manager ({endpoint}): {exc}')


def _manager_sync_post(content: str, imdb: str, tmdb: str):
    """Build Trakt /sync POST body with every ID we have."""
    if content == 'movie':
        ids = {}
        if imdb and str(imdb) not in ('0', 'None', ''):
            ids['imdb'] = imdb
        if tmdb and str(tmdb) not in ('0', 'None', ''):
            try:
                ids['tmdb'] = int(tmdb)
            except (TypeError, ValueError):
                pass
        return {'movies': [{'ids': ids}]} if ids else None
    ids = {}
    if tmdb and str(tmdb) not in ('0', 'None', ''):
        try:
            ids['tmdb'] = int(tmdb)
        except (TypeError, ValueError):
            pass
    if imdb and str(imdb) not in ('0', 'None', ''):
        ids['imdb'] = imdb
    return {'shows': [{'ids': ids}]} if ids else None


def manager(name: str, imdb: str, tmdb: str, content: str) -> None:
    """Opens a dialog to select a Trakt action to perform."""
    endpoint = None
    success_label = c.lang(32515)
    try:
        if not get_trakt_credentials_info():
            c.infoDialog(
                'Trakt not connected - authorize in Settings > Accounts > Trakt',
                heading=name,
                sound=True,
                icon='ERROR',
            )
            return
        if control.window.getProperty('midnight.trakt_auth_failed') == 'true':
            c.infoDialog(
                'Trakt session expired - re-authorize in Settings > Accounts > Trakt',
                heading=name,
                sound=True,
                icon='ERROR',
            )
            return

        post = _manager_sync_post(content, imdb, tmdb)
        if not post:
            c.infoDialog('No valid Trakt ID for this item', heading=name, sound=True, icon='ERROR')
            return

        actions = [
            (c.lang(90261), "rate"),  # Rate this
            (c.lang(90262), "remove_rating"),  # Remove Rating
            (c.lang(32516), "/sync/collection"),
            (c.lang(32517), "/sync/collection/remove"),
            (c.lang(32518), "/sync/watchlist"),
            (c.lang(32519), "/sync/watchlist/remove"),
            (c.lang(32520), "/users/me/lists/%s/items"),
        ]

        # Empty custom lists is fine — still show rate/collection/watchlist.
        # Auth failure already gated above; do not silent-return on [].
        result = get_all_user_lists() or []
        if result:
            c.log(
                f'[Trakt Manager] Loaded {len(result)} user list(s): '
                f'{", ".join(i.get("name", "?") for i in result)}'
            )
        else:
            c.log('[Trakt Manager] No custom user lists — showing built-in actions only')

        lists = [(i["name"], i["ids"]["slug"]) for i in result]
        lists = [lists[i // 2] for i in range(len(lists) * 2)]

        for i in range(0, len(lists), 2):
            lists[i] = ((c.lang(32521) % lists[i][0]), f"/users/me/lists/{lists[i][1]}/items")
        for i in range(1, len(lists), 2):
            lists[i] = ((c.lang(32522) % lists[i][0]), f"/users/me/lists/{lists[i][1]}/items/remove")
        actions += lists

        select = control.selectDialog([i[0] for i in actions], c.lang(32515))

        if select == -1:
            return

        # Handle rating actions
        if select == 0:  # Rate this
            # Show rating selector (1-10)
            rating_labels = [f"{i} - {['Terrible', 'Bad', 'Poor', 'Meh', 'Fair', 'Decent', 'Good', 'Great', 'Excellent', 'Masterpiece'][i-1]}" for i in range(1, 11)]
            rating_select = control.selectDialog(rating_labels, c.lang(90264))
            if rating_select == -1:
                return
            rating = rating_select + 1  # Convert index to rating (1-10)

            # Submit rating
            if content == "movie":
                rate_media('movie', imdb, rating, id_type='imdb')
            else:
                rate_media('show', tmdb, rating, id_type='tmdb')
            return

        elif select == 1:  # Remove Rating
            # Remove rating
            if content == "movie":
                remove_rating('movie', imdb, id_type='imdb')
            else:
                remove_rating('show', tmdb, id_type='tmdb')
            return

        # Handle list creation and management (existing code)
        if select == 6:  # Adjusted index (was 4, now 6 due to 2 new items at start)
            t = c.lang(32520)
            k = control.keyboard("", t)
            k.doModal()

            new = k.getText() if k.isConfirmed() else None
            if new is None or new == "":
                return

            create_result = get_trakt("/users/me/lists", post={"name": new, "privacy": "private"})
            if not _trakt_response_ok(create_result):
                _manager_fail_dialog(name, create_result, context='Create list')
                return
            result = create_result[0] if create_result else ''

            try:
                data = utils.json_loads_as_str(result)
                if isinstance(data, list) and data:
                    slug = data[0].get("ids", {}).get("slug")
                else:
                    slug = data.get("ids", {}).get("slug")
            except (json.JSONDecodeError, KeyError, AttributeError, TypeError, IndexError):
                c.log(f'[Trakt Manager] Could not parse create-list response: {result[:500]}')
                c.infoDialog('Trakt request failed - see the_crew.log', heading=name, sound=True, icon='ERROR')
                return

            c.log(f'[Trakt Manager] Created list "{new}" (slug={slug})')
            endpoint = actions[select][1] % slug
            success_label = f'Added to {new}'
            c.log(f'[Trakt Manager] POST {endpoint} for {name} (content={content}, imdb={imdb}, tmdb={tmdb})')
            result_data = get_trakt(endpoint, post=post)
            if not _trakt_response_ok(result_data):
                _manager_fail_dialog(name, result_data, context=f'POST {endpoint}')
                return
            result = result_data[0]
        else:
            endpoint = actions[select][1]
            success_label = actions[select][0]
            c.log(f'[Trakt Manager] POST {endpoint} for {name} (content={content}, imdb={imdb}, tmdb={tmdb})')
            result_data = get_trakt(endpoint, post=post)
            if not _trakt_response_ok(result_data):
                _manager_fail_dialog(name, result_data, context=f'POST {endpoint}')
                return
            result = result_data[0]
            if endpoint == '/sync/collection/remove':
                try:
                    payload = json.loads(result or '{}')
                    deleted = (payload.get('deleted') or {}).get('movies' if content == 'movie' else 'shows', 0)
                    c.log(f'[Trakt Manager] collection/remove deleted={deleted} response={result[:300]}')
                except Exception:
                    pass
            elif endpoint in ('/sync/watchlist', '/sync/watchlist/remove'):
                try:
                    payload = json.loads(result or '{}')
                    media_key = 'movies' if content == 'movie' else 'shows'
                    action_key = 'added' if endpoint == '/sync/watchlist' else 'deleted'
                    count = (payload.get(action_key) or {}).get(media_key, 0)
                    c.log(
                        f'[Trakt Manager] watchlist/{action_key} {media_key}={count} '
                        f'response={result[:300]}'
                    )
                    if endpoint == '/sync/watchlist' and count == 0:
                        c.infoDialog(
                            'Trakt did not add this item - see the_crew.log',
                            heading=name, sound=True, icon='ERROR',
                        )
                        return
                except Exception:
                    pass

        _sync_local_after_manager(endpoint, content, imdb, tmdb)
        control.refresh()

        icon = control.infoLabel("ListItem.Icon") if result is not None else "ERROR"
        c.infoDialog(success_label, heading=name, sound=True, icon=icon)
    except Exception as e:
        c.log(f"Exception in trakt manager: {e}")
    finally:
        control.idle()


def my_ratings_menu() -> None:
    """Display a menu to view user's ratings by type."""
    try:
        if not get_trakt_credentials_info():
            c.infoDialog("Trakt account required", sound=True, icon="ERROR")
            return

        menu_items = [
            (c.lang(32001), 'movies'),  # Movies
            (c.lang(32002), 'shows'),   # TV Shows
            (c.lang(32326), 'episodes') # Episodes
        ]

        labels = [item[0] for item in menu_items]
        select = control.selectDialog(labels, c.lang(90263))  # "My Ratings"

        if select == -1:
            return

        media_type = menu_items[select][1]
        display_ratings(media_type)

    except Exception as e:
        c.log(f"Exception in my_ratings_menu: {e}")
    finally:
        control.idle()


def display_ratings(media_type: str) -> None:
    """Display user's ratings for a specific media type."""
    try:
        ratings = get_my_ratings(media_type)

        if not ratings:
            c.infoDialog(f"No {media_type} rated yet", sound=True, icon="INFO")
            return

        # Create display list
        items = []
        for item in ratings:
            rating = item.get('rating', 0)
            rated_at = item.get('rated_at', '')

            if media_type == 'movies':
                movie = item.get('movie', {})
                title = movie.get('title', 'Unknown')
                year = movie.get('year', '')
                label = f"[STAR] {rating}/10 - {title} ({year})"
            elif media_type == 'shows':
                show = item.get('show', {})
                title = show.get('title', 'Unknown')
                year = show.get('year', '')
                label = f"[STAR] {rating}/10 - {title} ({year})"
            elif media_type == 'episodes':
                episode = item.get('episode', {})
                show = item.get('show', {})
                title = show.get('title', 'Unknown')
                season = episode.get('season', 0)
                ep_num = episode.get('number', 0)
                ep_title = episode.get('title', '')
                label = f"[STAR] {rating}/10 - {title} S{season:02d}E{ep_num:02d} - {ep_title}"
            else:
                label = f"[STAR] {rating}/10"

            items.append(label)

        # Show ratings list
        select = control.selectDialog(items, f"{c.lang(90263)} - {media_type.title()}")

        # Could add option to modify rating if user selects an item

    except Exception as e:
        c.log(f"Exception in display_ratings: {e}")
        c.infoDialog("Error loading ratings", sound=True, icon="ERROR")








def slug(title):
    """
    Convert a given title to a slug string.
    """
    if not isinstance(title, str):
        return ''
    title = title.strip().lower()
    title = re.sub('[^a-z0-9_]', '-', title)
    title = re.sub('-{2,}', '-', title)
    return title.rstrip('-')

def sort_list(sort_key, sort_direction, list_data) -> list:
    """
    Sort a list of trakt items based on the given key and direction.
    """
    reverse = sort_direction != 'asc'
    if sort_key == 'rank':
        return sorted(list_data, key=lambda x: x['rank'], reverse=reverse)
    elif sort_key == 'added':
        return sorted(list_data, key=lambda x: x['listed_at'], reverse=reverse)
    elif sort_key == 'title':
        return sorted(list_data, key=lambda x: utils.title_key(x[x['type']].get('title')), reverse=reverse)
    elif sort_key == 'released':
        return sorted(list_data, key=lambda x: released_key(x[x['type']]), reverse=reverse)
    elif sort_key == 'runtime':
        return sorted(list_data, key=lambda x: x[x['type']].get('runtime', 0), reverse=reverse)
    elif sort_key == 'popularity':
        return sorted(list_data, key=lambda x: x[x['type']].get('votes', 0), reverse=reverse)
    elif sort_key == 'percentage':
        return sorted(list_data, key=lambda x: x[x['type']].get('rating', 0), reverse=reverse)
    elif sort_key == 'votes':
        return sorted(list_data, key=lambda x: x[x['type']].get('votes', 0), reverse=reverse)
    else:
        return list_data

def released_key(item):
    """ Return the released or first_aired timestamp from a trakt item. """
    if 'released' in item:
        return item['released'] or '0'
    elif 'first_aired' in item:
        return item['first_aired'] or '0'
    else:
        return '0'

def _convert_and_get_latest_iso(values):
    """Convert a list of ISO timestamp-like values to integers (UTC) and return the latest value."""
    converted = []
    for v in values:
        try:
            if v is None:
                converted.append(0)
            else:
                converted.append(int(cleandate.new_iso_to_utc(v)))
        except ValueError:
            converted.append(0)
    return sorted(converted)[-1] if converted else 0


def getActivity() -> int:
    """Get latest activity timestamp from Trakt API (makes API call)."""
    try:
        i = getTraktAsJson('/sync/last_activities')

        if i and isinstance(i, dict):
            movies = i.get('movies', {})
            episodes = i.get('episodes', {})
            shows = i.get('shows', {})
            seasons = i.get('seasons', {})
            lists = i.get('lists', {})

            activity_values = [
                movies.get('collected_at', 0),
                movies.get('watchlisted_at', 0),
                shows.get('watchlisted_at', 0),
                episodes.get('collected_at', 0),
                episodes.get('watchlisted_at', 0),
                seasons.get('watchlisted_at', 0),
                lists.get('updated_at', 0),
                lists.get('liked_at', 0),
            ]
            activity = _convert_and_get_latest_iso(activity_values)
        else:
            activity = 0

        c.log(f"trakt activity = {activity}")

        return activity
    except ValueError:
        return 0


def getActivity_from_db(activity_type=None) -> int:
    """Get latest activity timestamp from database (server-friendly, no API call)."""
    try:
        activity_values = []

        if activity_type == 'watched':
            # Only watched activities
            activity_values = [
                get_trakt_table_value('movies', 'watched_at') or 0,
                get_trakt_table_value('episodes', 'watched_at') or 0,
            ]

        elif activity_type == 'episodes':
            # Episode-specific activities (watched, paused, collected, watchlisted)
            activity_values = [
                get_trakt_table_value('episodes', 'watched_at') or 0,
                get_trakt_table_value('episodes', 'paused_at') or 0,
                get_trakt_table_value('episodes', 'collected_at') or 0,
                get_trakt_table_value('episodes', 'watchlisted_at') or 0,
            ]

        elif activity_type == 'playback':
            # Resume / in-progress (bathroom -> bedroom scenario)
            activity_values = [
                get_trakt_table_value('movies', 'paused_at') or 0,
                get_trakt_table_value('episodes', 'paused_at') or 0,
            ]

        elif activity_type == 'movies':
            # Movie-specific activities
            activity_values = [
                get_trakt_table_value('movies', 'watched_at') or 0,
                get_trakt_table_value('movies', 'paused_at') or 0,
                get_trakt_table_value('movies', 'collected_at') or 0,
                get_trakt_table_value('movies', 'watchlisted_at') or 0,
            ]

        elif activity_type == 'collection':
            # Collection-related (what's in user's library)
            activity_values = [
                get_trakt_table_value('movies', 'collected_at') or 0,
                get_trakt_table_value('episodes', 'collected_at') or 0,
            ]

        elif activity_type == 'watchlist':
            # Watchlist-related (what user wants to watch)
            activity_values = [
                get_trakt_table_value('movies', 'watchlisted_at') or 0,
                get_trakt_table_value('shows', 'watchlisted_at') or 0,
                get_trakt_table_value('episodes', 'watchlisted_at') or 0,
                get_trakt_table_value('seasons', 'watchlisted_at') or 0,
            ]

        else:
            # 'all' or None - All activities (backward compatible, excludes lists.liked_at to avoid comment/like spam)
            activity_values = [
                get_trakt_table_value('movies', 'collected_at') or 0,
                get_trakt_table_value('movies', 'watchlisted_at') or 0,
                get_trakt_table_value('movies', 'watched_at') or 0,
                get_trakt_table_value('movies', 'paused_at') or 0,
                get_trakt_table_value('shows', 'watchlisted_at') or 0,
                get_trakt_table_value('episodes', 'collected_at') or 0,
                get_trakt_table_value('episodes', 'watchlisted_at') or 0,
                get_trakt_table_value('episodes', 'watched_at') or 0,
                get_trakt_table_value('episodes', 'paused_at') or 0,
                get_trakt_table_value('seasons', 'watchlisted_at') or 0,
                get_trakt_table_value('lists', 'updated_at') or 0,
                # NOTE: Intentionally excluding lists.liked_at to avoid invalidation on comment likes
            ]

        activity = _convert_and_get_latest_iso(activity_values)
        return activity
    except (database.Error, KeyError, ValueError, TypeError) as e:
        c.log(f"[Trakt] Error reading {activity_type or 'all'} activity from DB: {e}")
        return 0

# def getWatchedActivity():
#     try:
#         i = getTraktAsJson('/sync/last_activities')
def getWatchedActivity():
    """Return timestamp (ISO) of most recent watched activity from Trakt API (uses conditional ETag fetch, cached 60s)."""
    try:
        # fetcher that performs conditional requests via get_trakt and returns (body, headers, status)
        def fetcher(conditional_headers=None):
            res = get_trakt('/sync/last_activities', conditional_headers=conditional_headers)
            if not res:
                return None
            # get_trakt returns (text, headers, status)
            return res

        i = cache.get_with_etag('trakt.last_activities', fetcher, ttl_seconds=60, namespace='trakt')

        c.log(f"getWatchedActivity returned: {i}")

        if not i or not isinstance(i, dict):
            return 0

        activity_values = [
            i.get('movies', {}).get('watched_at'),
            i.get('episodes', {}).get('watched_at'),
        ]
        activity = _convert_and_get_latest_iso(activity_values)

        return activity

    except (KeyError, TypeError, AttributeError) as e:
        failure = traceback.format_exc()
        c.log(f'Traceback:: {failure}')
        c.log(f'Exception raised in trakt handler: {e}')
        return 0


def getWatchedActivity_from_db():
    """Return timestamp (ISO) of most recent watched activity from database (server-friendly, no API call)."""
    # Simple wrapper - calls getActivity_from_db with 'watched' type
    return getActivity_from_db(activity_type='watched')


def getPlaybackActivity_from_db():
    """Return unix timestamp of most recent playback pause activity from local DB (no API call)."""
    return getActivity_from_db(activity_type='playback')


def get_last_activity():
    """Return full activity data from Trakt /sync/last_activities endpoint (uses conditional ETag fetch, cached 60s)."""
    try:
        # fetcher that performs conditional requests via get_trakt and returns (body, headers, status)
        def fetcher(conditional_headers=None):
            res = get_trakt('/sync/last_activities', conditional_headers=conditional_headers)
            if not res:
                return None
            # get_trakt returns (text, headers, status)
            return res

        i = cache.get_with_etag('trakt.last_activities', fetcher, ttl_seconds=60, namespace='trakt')

        if c.devmode:
            c.log(f"[get_last_activity] Fetched activity data: {i}")

        if not i or not isinstance(i, dict):
            return None

        return i

    except (KeyError, TypeError, AttributeError) as e:
        failure = traceback.format_exc()
        c.log(f'[get_last_activity] Traceback:: {failure}')
        c.log(f'[get_last_activity] Exception raised: {e}')
        return None
@with_db_connection(return_on_error=[])
def get_queued_indicators_movies(dbcon, dbcur):
    """Get movie indicators from scrobble queue for items marked as watched (>92%)."""
    try:
        if not table_exists('scrobble_queue'):
            return []

        dbcur.execute("SELECT DISTINCT imdb FROM scrobble_queue WHERE media_type='movie' AND progress >= 92 AND action='stop'")
        results = dbcur.fetchall()

        return [row[0] for row in results] if results else []
    except database.Error as e:
        c.log(f"[Trakt] Error fetching queued movie indicators: {e}")
        return []


@with_db_connection(return_on_error={})
def get_queued_indicators_episodes(dbcon, dbcur):
    """Get episode indicators from scrobble queue for items marked as watched (>92%)."""
    try:
        if not table_exists('scrobble_queue'):
            return {}

        dbcur.execute("SELECT imdb, season, episode FROM scrobble_queue WHERE media_type='episode' AND progress >= 92 AND action='stop'")
        results = dbcur.fetchall()

        # Group by show IMDB
        episodes_by_show = {}
        if results:
            for imdb, season, episode in results:
                if imdb not in episodes_by_show:
                    episodes_by_show[imdb] = []
                try:
                    episodes_by_show[imdb].append((int(season), int(episode)))
                except (ValueError, TypeError):
                    continue

        return episodes_by_show
    except database.Error as e:
        c.log(f"[Trakt] Error fetching queued episode indicators: {e}")
        return {}


@with_db_connection(return_on_error=None)
def get_queued_progress(dbcon, dbcur, media_type, imdb, season='', episode=''):
    """Get progress for a specific item from the scrobble queue."""
    if not table_exists('scrobble_queue'):
        return None

    if media_type == 'movie':
        dbcur.execute("SELECT progress FROM scrobble_queue WHERE media_type='movie' AND imdb=?", (imdb,))
    else:  # episode
        dbcur.execute("SELECT progress FROM scrobble_queue WHERE media_type='episode' AND imdb=? AND season=? AND episode=?",
                    (imdb, str(season), str(episode)))

    result = dbcur.fetchone()
    return result[0] if result else None


def cachesyncMovies(timeout=None):
    return cache.get(syncMovies, timeout, TRAKTUSER)

def timeoutsyncMovies():
    timeout = cache.timeout(syncMovies, TRAKTUSER)
    return timeout

def syncMovies(user):
    try:
        if get_trakt_credentials_info() is False:
            c.log('getTraktCredentialsInfo is false')
            return

        # Get watched movies from Trakt
        trakt_watched = []
        if indicators := getTraktAsJson('/users/me/watched/movies'):
            if indicators := [i['movie']['ids'] for i in indicators]:
                trakt_watched = [str(i['imdb']) for i in indicators if 'imdb' in i]

        # Merge with queued indicators (movies marked as watched locally but not yet synced)
        queued_watched = get_queued_indicators_movies()

        # Combine both lists (use set to avoid duplicates)
        if trakt_watched or queued_watched:
            combined = list(set(trakt_watched + queued_watched))
            c.log(f"[Trakt] Movie indicators: {len(trakt_watched)} from Trakt + {len(queued_watched)} queued = {len(combined)} total")
            return combined

        return []
    except (KeyError, TypeError, AttributeError) as e:
        c.log(f'Exception raised in trakt operation: {e}')


def cachesyncTVShows(timeout=None):
    # Must use cache.get like cachesyncMovies. Calling syncTVShows() directly
    # forced a full paginated /sync/watched/shows?extended=progress pull on every
    # episode-list rebuild (Container.Refresh after Stop → ~20-30s busy cursor).
    return cache.get(syncTVShows, timeout, TRAKTUSER)


def timeoutsyncTVShows():
    timeout = cache.timeout(syncTVShows, TRAKTUSER) or 0
    return timeout

def syncTVShows(user):
    try:
        if not get_trakt_credentials_info():
            c.log('getTraktCredentialsInfo is false')
            return

        # Prefer shared watched-shows cache (widgets / next-episodes) over a
        # second uncached paginated pull on every indicator rebuild.
        watched_shows = get_sync_watched_shows_extended(log_label='indicators')
        if not watched_shows:
            c.log('[Trakt] No watched shows returned from API')
            return

        indicators = []
        for show in watched_shows:
            if not watched_show_has_episode_detail(show):
                continue
            show_body = show.get('show') or {}
            tmdb_id = (show_body.get('ids') or {}).get('tmdb')
            if not tmdb_id:
                continue
            aired = show_body.get('aired_episodes', 0)
            watched_eps = [
                (s['number'], e['number'])
                for s in (show.get('seasons') or [])
                for e in (s.get('episodes') or [])
                if int(s.get('number') or 0) > 0
            ]
            indicators.append((str(tmdb_id), aired, watched_eps))

        # Merge with queued episode indicators
        queued_episodes = get_queued_indicators_episodes()

        if queued_episodes:
            # Convert indicators to dict for easier merging
            indicators_dict = {tmdb_id: (aired_episodes, set(watched_episodes)) for tmdb_id, aired_episodes, watched_episodes in indicators}

            # For queued episodes, we need to look up TMDB ID from IMDB (requires additional data)
            # For now, add queued episodes to existing shows in indicators
            # Note: This is a simplified approach - full implementation would need IMDB->TMDB mapping
            for imdb, episodes in queued_episodes.items():
                # Find matching show in indicators (would need proper IMDB->TMDB lookup)
                # For now, log that we have queued episodes
                c.log(f"[Trakt] Found {len(episodes)} queued episodes for show {imdb}")

            # Convert back to list format
            indicators = [(tmdb_id, aired, list(watched)) for tmdb_id, (aired, watched) in indicators_dict.items()]

        return indicators
    except (KeyError, TypeError, AttributeError) as e:
        c.log(f"Exception raised: {e}")





def syncSeason(imdb):
    try:
        if get_trakt_credentials_info() is False:
            return
        indicators = getTraktAsJson(f'/shows/{imdb}/progress/watched?specials=false&hidden=false')
        indicators = indicators['seasons']
        indicators = [(i['number'], [x['completed'] for x in i['episodes']]) for i in indicators]
        #indicators = ['%01d' % int(i[0]) for i in indicators if False not in i[1]]
        indicators = [f"{int(i[0]):01d}" for i in indicators if False not in i[1]]
        return indicators
    except (KeyError, TypeError, IndexError, Exception):
        pass  # API failure or data parsing error


def syncTraktStatus(silent=False):
    try:
        cachesyncMovies()
        cachesyncTVShows()
        if not silent:
            c.infoDialog(c.lang(32092))
    except Exception as e:
        c.log(f'[Trakt] Sync failed: {e}')
        c.infoDialog('Trakt sync failed')

##########################################
# Movies
##########################################
def markMovieAsWatched(key, media_id):
    try:
        if key == 'imdb' and not media_id.startswith('tt'):
            media_id = f'tt{media_id}'
        result = get_trakt('/sync/history', {"movies": [{"ids": {key: media_id}}]})
        if result:
            delete_progress_from_database(key, media_id)

        return result[0] if result else None
    except (requests.RequestException, KeyError, TypeError):
        return None

def markMovieAsNotWatched(key, media_id):
    try:
        if key == 'imdb' and not media_id.startswith('tt'):
            media_id = f'tt{media_id}'
        result = get_trakt('/sync/history/remove', {"movies": [{"ids": {key: media_id}}]})
        return result[0] if result else None
    except (requests.RequestException, KeyError, TypeError):
        return None



###########################################
# TV Shows
###########################################
def markTVShowAsNotWatched(key, media_id):
    try:
        result = get_trakt('/sync/history/remove', {"shows": [{"ids": {key: media_id}}]})
        return result[0] if result else None
    except (requests.RequestException, KeyError, TypeError):
        return None

def markTVShowAsWatched(key, media_id):
    try:
        result = get_trakt('/sync/history/', {"shows": [{"ids": {key: media_id}}]})
        return result[0] if result else None
    except (requests.RequestException, KeyError, TypeError):
        return None


#############################################
# Seasons
#############################################
def markSeasonAsWatched(key, media_id, season):
    """Mark all episodes in a season as watched using a single batch API call."""
    c.log(f"[Trakt] Marking season {season} as watched for {key}={media_id}")
    try:
        # Get TMDB ID if we only have IMDB
        tmdb_id = media_id if key == 'tmdb' else None
        if key == 'imdb':
            # Look up TMDB ID from IMDB
            from ..indexers import episodes
            try:
                # Use TMDB's find endpoint to get TMDB ID from IMDB
                import requests
                tmdb_user = c.get_setting('tm.personal_user') or c.get_setting('tm.user') or keys.tmdb_key
                url = f'https://api.themoviedb.org/3/find/{media_id}?api_key={tmdb_user}&external_source=imdb_id'
                r = requests.get(url, timeout=10)
                result = r.json()
                tv_results = result.get('tv_results', [])
                if tv_results:
                    tmdb_id = str(tv_results[0]['id'])
                    c.log(f"[Trakt] Converted IMDB {media_id} to TMDB {tmdb_id}")
            except Exception as e:
                c.log(f"[Trakt] Failed to convert IMDB to TMDB: {e}")

        if not tmdb_id:
            c.log(f"[Trakt] No TMDB ID available, falling back to season-only sync")
            result = getTraktAsJson('/sync/history', {"shows": [{"seasons": [{"number": int(season)}], "ids": {key: media_id}}]})
            return result

        # Get all episodes in this season from TMDB
        tmdb_user = c.get_setting('tm.personal_user') or c.get_setting('tm.user') or keys.tmdb_key
        season_url = f'https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}?api_key={tmdb_user}'

        import requests
        r = requests.get(season_url, timeout=10)
        season_data = r.json()

        episodes_list = season_data.get('episodes', [])
        if not episodes_list:
            c.log(f"[Trakt] No episodes found for season {season}")
            return None

        # Build episodes array for batch sync (all episodes in one call)
        episode_numbers = [{"number": ep['episode_number']} for ep in episodes_list]

        c.log(f"[Trakt] Marking {len(episode_numbers)} episodes in season {season} as watched")

        # Single API call for entire season
        payload = {
            "shows": [{
                "seasons": [{
                    "number": int(season),
                    "episodes": episode_numbers
                }],
                "ids": {key: media_id}
            }]
        }

        result = getTraktAsJson('/sync/history', payload)
        c.log(f"[Trakt] Season {season} marked as watched successfully")
        return result

    except (requests.RequestException, json.JSONDecodeError, KeyError, TypeError) as e:
        c.log(f"[Trakt] Exception in markSeasonAsWatched: {e}")
        c.log(f"[Trakt] Traceback: {traceback.format_exc()}")
        return None

def markSeasonAsNotWatched(key, media_id, season):
    """Mark all episodes in a season as unwatched using a single batch API call."""
    c.log(f"[Trakt] Marking season {season} as unwatched for {key}={media_id}")
    try:
        # Get TMDB ID if we only have IMDB
        tmdb_id = media_id if key == 'tmdb' else None
        if key == 'imdb':
            # Look up TMDB ID from IMDB
            try:
                import requests
                tmdb_user = c.get_setting('tm.personal_user') or c.get_setting('tm.user') or keys.tmdb_key
                url = f'https://api.themoviedb.org/3/find/{media_id}?api_key={tmdb_user}&external_source=imdb_id'
                r = requests.get(url, timeout=10)
                result = r.json()
                tv_results = result.get('tv_results', [])
                if tv_results:
                    tmdb_id = str(tv_results[0]['id'])
                    c.log(f"[Trakt] Converted IMDB {media_id} to TMDB {tmdb_id}")
            except Exception as e:
                c.log(f"[Trakt] Failed to convert IMDB to TMDB: {e}")

        if not tmdb_id:
            c.log(f"[Trakt] No TMDB ID available, falling back to season-only sync")
            result = getTraktAsJson('/sync/history/remove', {"shows": [{"seasons": [{"number": int(season)}], "ids": {key: media_id}}]})
            c.log(f"[Trakt] Season-only unwatched result: {result}")
            return result

        # Get all episodes in this season from TMDB
        tmdb_user = c.get_setting('tm.personal_user') or c.get_setting('tm.user') or keys.tmdb_key
        season_url = f'https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}?api_key={tmdb_user}'

        import requests
        r = requests.get(season_url, timeout=10)
        season_data = r.json()

        episodes_list = season_data.get('episodes', [])
        if not episodes_list:
            c.log(f"[Trakt] No episodes found for season {season}")
            return None

        # Build episodes array for batch sync
        episode_numbers = [{"number": ep['episode_number']} for ep in episodes_list]

        c.log(f"[Trakt] Marking {len(episode_numbers)} episodes in season {season} as unwatched")

        # Single API call for entire season
        payload = {
            "shows": [{
                "seasons": [{
                    "number": int(season),
                    "episodes": episode_numbers
                }],
                "ids": {key: media_id}
            }]
        }

        c.log(f"[Trakt] Unwatched payload: {payload}")
        result = getTraktAsJson('/sync/history/remove', payload)
        c.log(f"[Trakt] Season {season} unwatched result: {result}")
        c.log(f"[Trakt] Season {season} marked as unwatched successfully")
        return result

    except (requests.RequestException, json.JSONDecodeError, KeyError, TypeError) as e:
        c.log(f"[Trakt] Exception in markSeasonAsNotWatched: {e}")
        c.log(f"[Trakt] Traceback: {traceback.format_exc()}")
        return None

#############################################
# Episodes
#############################################
def markEpisodeAsWatched(key, media_id, season, episode):
    season, episode = int('%01d' % int(season)), int('%01d' % int(episode))
    #season, episode = int(f'{season:01d}'), int(f'{episode:01d}')
    result = get_trakt('/sync/history', {"shows": [{"seasons": [{"episodes": [{"number": episode}], "number": season}], "ids": {key: media_id}}]})

    # Clear progress caches to force immediate update in UI
    invalidate_watched_shows_progress_cache()
    cache.cache_delete('trakt.last_activities', table='trakt')

    try:
        return result[0] if result else None
    except IndexError:
        # Handle the case when get_trakt returns None
        # For example, you can return a default value or raise an exception
        return None

def markEpisodeAsNotWatched(key, media_id, season, episode):
    season, episode = int('%01d' % int(season)), int('%01d' % int(episode))
    season, episode = int(f'{season:01d}'), int(f'{episode:01d}')
    result = get_trakt('/sync/history/remove', {"shows": [{"seasons": [{"episodes": [{"number": episode}], "number": season}], "ids": {key: media_id}}]})

    # Clear progress caches to force immediate update in UI
    invalidate_watched_shows_progress_cache()
    cache.cache_delete('trakt.last_activities', table='trakt')

    try:
        return result[0] if result else None
    except IndexError:
        # Handle the case when get_trakt returns None
        # For example, you can return a default value or raise an exception
        return None






##############################################
# Scrobble
##############################################
def _trakt_response_ok(response):
    """True when a get_trakt() call completed with a 2xx status."""
    try:
        if isinstance(response, tuple) and len(response) >= 3:
            return 200 <= int(response[2]) < 300
        return bool(response)
    except (TypeError, ValueError):
        return bool(response)


def _iso_timestamp_max(*values):
    """Return the latest ISO-8601 timestamp string from candidates (lexicographic Zulu compare)."""
    candidates = [v for v in values if v and isinstance(v, str)]
    return max(candidates) if candidates else None


@with_db_connection(return_on_error=False)
def set_trakt_table_value(dbcon, dbcur, table_name, key, value):
    """Upsert a single activity timestamp into the traktsync activity mirror."""
    if not table_exists(table_name):
        dbcur.execute(f"CREATE TABLE IF NOT EXISTS {table_name} (setting TEXT, value TEXT, UNIQUE(setting));")
    dbcur.execute(f"INSERT OR REPLACE INTO {table_name} VALUES (?, ?)", (key, value))
    dbcon.commit()
    return True


@with_db_connection(return_on_error=False)
def bump_local_activity_after_scrobble(dbcon, dbcur, media_type, action, watched_percent=None):
    """Advance local last_activities mirrors after a successful scrobble.

    Optimistic local update — does NOT invalidate the ETag cache. Trakt remains
    authoritative; the next conditional fetch reconciles if Trakt has something newer.
    """
    now_iso = get_now_in_iso()
    table = 'movies' if media_type == 'movie' else 'episodes'

    try:
        pct = float(watched_percent) if watched_percent is not None else 0.0
    except (TypeError, ValueError):
        pct = 0.0

    if action == 'stop' and pct >= 92:
        field = 'watched_at'
    else:
        field = 'paused_at'

    current = get_trakt_table_value(table, field)
    new_value = _iso_timestamp_max(current, now_iso) or now_iso
    set_trakt_table_value(table, field, new_value)

    if not table_exists('service'):
        dbcur.execute(sql_dict['sql_create_service'])
        dbcon.commit()

    stored_all = get_trakt_sync_value('all') or ''
    new_all = _iso_timestamp_max(stored_all, new_value) or new_value
    dbcur.execute("INSERT OR REPLACE INTO service VALUES (?, ?)", ('all', new_all))
    dbcur.execute("INSERT OR REPLACE INTO service VALUES (?, ?)", ('crew_last_sync', now_iso))
    dbcon.commit()

    if c.devmode:
        c.log(f"[Trakt] bump_local_activity_after_scrobble: {table}.{field}={new_value} all={new_all}")
    return True


@with_db_connection(return_on_error=False)
def queue_scrobble(dbcon, dbcur, media_type, imdb, watched_percent, action, season='', episode=''):
    """Queue a scrobble event instead of making immediate API call."""
    if not imdb.startswith('tt'):
        imdb = 'tt' + imdb

    timestamp = get_now_in_iso()

    # Ensure table exists
    if not table_exists('scrobble_queue'):
        dbcur.execute(sql_dict['sql_create_scrobble_queue'])
        dbcon.commit()

    # Queue the scrobble (replaces existing queue item for same content)
    sql = sql_dict['sql_insert_scrobble_queue']
    dbcur.execute(sql, (media_type, imdb, season, episode, watched_percent, action, timestamp))
    dbcon.commit()

    c.log(f"[Trakt] Queued scrobble: {media_type} {imdb} s{season}e{episode} - {watched_percent}% ({action})")
    return True


@with_db_connection(return_on_error=None)
def process_scrobble_queue(dbcon, dbcur, force=False):
    """Process queued scrobble events and send to Trakt."""
    if not get_trakt_credentials_info():
        return  # No credentials — never attempt scrobble calls

    if not force and should_defer_background_trakt():
        c.log('[Trakt] Scrobble queue deferred — boot quiet or rate-limit backoff', 1)
        return

    if not table_exists('scrobble_queue'):
        return

    # Get all queued scrobbles
    dbcur.execute(sql_dict['sql_select_scrobble_queue'])
    queue_items = dbcur.fetchall()

    if not queue_items:
        return

    c.log(f"[Trakt] Processing {len(queue_items)} queued scrobble events")

    for item in queue_items:
        item_id, media_type, imdb, season, episode, progress, action = item

        try:
            # Round progress to 2 decimal places to avoid Trakt validation errors
            progress_rounded = round(float(progress), 2)

            # Make the actual API call
            if media_type == 'movie':
                r = get_trakt(f'/scrobble/{action}', {"movie": {"ids": {"imdb": imdb}}, "progress": progress_rounded})
            else:  # episode
                r = get_trakt(f'/scrobble/{action}', {
                    "show": {"ids": {"imdb": imdb}},
                    "episode": {"season": int(season), "number": int(episode)},
                    "progress": progress_rounded
                })

            # Remove from queue on success
            dbcur.execute(sql_dict['sql_delete_scrobble_item'], (item_id,))
            dbcon.commit()

            if _trakt_response_ok(r):
                bump_local_activity_after_scrobble(media_type, action, progress_rounded)

            c.log(f"[Trakt] Processed scrobble: {media_type} {imdb} - {progress_rounded}% ({action})")

        except Exception as e:
            c.log(f'[Trakt] Failed to process scrobble {item_id}: {e}')
            # Keep in queue to retry later
            continue


def trakt_official_status(media_type):
    """Check whether the official script.trakt addon is installed, enabled, authorized and set to scrobble
    this media type. If it is, Midnight should skip its own scrobble call to avoid duplicate history entries.

    Returns True if Midnight should skip (script.trakt will handle it), False if Midnight should send its own call.
    """
    try:
        import xbmcaddon as _xbmcaddon
        import xbmc as _xbmc
        # Check installed and enabled
        if not _xbmc.getCondVisibility('System.HasAddon(script.trakt)'):
            return False
        trakt_addon = _xbmcaddon.Addon('script.trakt')
        # Check authorized (has a token stored)
        try:
            authorization = trakt_addon.getSetting('authorization')
        except Exception:
            authorization = ''
        if not authorization:
            return False
        # Check it's not set to exclude HTTP sources (which would mean it won't scrobble from us)
        try:
            exclude_http = trakt_addon.getSetting('ExcludeHTTP')
        except Exception:
            exclude_http = ''
        if exclude_http == 'true':
            return False
        # Check it's configured to scrobble this media type
        setting_key = 'scrobble_movie' if media_type in ('movie', 'movies') else 'scrobble_episode'
        try:
            scrobble_setting = trakt_addon.getSetting(setting_key)
        except Exception:
            scrobble_setting = ''
        if scrobble_setting == 'false':
            return False
        # All checks passed — script.trakt is active and will handle this scrobble
        c.log(f'[Trakt] trakt_official_status: script.trakt is active for {media_type}, skipping own scrobble')
        return True
    except Exception as e:
        c.log(f'[Trakt] trakt_official_status check failed: {e}')
        return False


def _scrobble_movie_ids(imdb='', tmdb=None):
    ids = {}
    if imdb and str(imdb) not in ('0', 'None', '', 'null'):
        imdb_val = str(imdb)
        ids['imdb'] = imdb_val if imdb_val.startswith('tt') else f'tt{imdb_val}'
    if tmdb and str(tmdb) not in ('0', 'None', '', 'null'):
        try:
            ids['tmdb'] = int(tmdb)
        except (TypeError, ValueError):
            pass
    return ids


def _scrobble_show_ids(imdb='', tmdb=None):
    return _scrobble_movie_ids(imdb=imdb, tmdb=tmdb)


def scrobbleMovie(imdb, watched_percent, action, tmdb=None):
    """Scrobble a movie to Trakt. Always sends immediately (direct API call).
    Skips if the official script.trakt addon is active and will handle it instead.
    """
    try:
        if not get_trakt_credentials_info():
            return None  # No credentials — never attempt the call
        if trakt_official_status('movie'):
            return True

        ids = _scrobble_movie_ids(imdb=imdb, tmdb=tmdb)
        if not ids:
            c.log('[Trakt] scrobbleMovie skipped — no valid imdb/tmdb id')
            return None

        watched_percent = round(float(watched_percent), 2)
        c.log(f'[Trakt] scrobbleMovie ids={ids} percent={watched_percent} action={action}')

        r = get_trakt(f'/scrobble/{action}', {"movie": {"ids": ids}, "progress": watched_percent})
        c.log(f'[Trakt] scrobbleMovie response: {r}')
        if _trakt_response_ok(r):
            bump_local_activity_after_scrobble('movie', action, watched_percent)
        return r
    except (requests.RequestException, KeyError, ValueError, TypeError) as e:
        c.log(f'[Trakt] Exception in scrobbleMovie: {e}')


def scrobbleEpisode(imdb, season, episode, watched_percent, action, tmdb=None):
    """Scrobble an episode to Trakt. Always sends immediately (direct API call).
    Skips if the official script.trakt addon is active and will handle it instead.
    """
    try:
        if not get_trakt_credentials_info():
            return None  # No credentials — never attempt the call
        if trakt_official_status('episode'):
            return True

        ids = _scrobble_show_ids(imdb=imdb, tmdb=tmdb)
        if not ids:
            c.log('[Trakt] scrobbleEpisode skipped — no valid imdb/tmdb id')
            return None

        season, episode = int(season), int(episode)
        watched_percent = round(float(watched_percent), 2)
        c.log(f'[Trakt] scrobbleEpisode ids={ids} S{season}E{episode} percent={watched_percent} action={action}')

        r = get_trakt(f'/scrobble/{action}', {
            "show": {"ids": ids},
            "episode": {"season": season, "number": episode},
            "progress": watched_percent,
        })
        c.log(f'[Trakt] scrobbleEpisode response: {r}')
        if _trakt_response_ok(r):
            bump_local_activity_after_scrobble('episode', action, watched_percent)
        return r
    except (requests.RequestException, KeyError, ValueError, TypeError) as e:
        c.log(f'[Trakt] Exception in scrobbleEpisode: {e}')


def get_watched_batch(media_type='shows', last_modified=None, etag=None):
    """Get watched movies via conditional batch request (shows use progress pagination)."""
    try:
        if media_type == 'shows':
            data = fetch_watched_shows_progress(log_label='batch')
            if not data:
                return None, last_modified, etag
            return data, last_modified, etag

        url = f'/sync/watched/{media_type}'

        # Add conditional request headers if we have cached validators
        conditional_headers = {}
        if last_modified:
            conditional_headers['If-Modified-Since'] = last_modified
        if etag:
            conditional_headers['If-None-Match'] = etag

        # Make request with conditional headers
        if not url.startswith('http'):
            url = urljoin(BASE_URL, url)

        headers = {
            'Content-Type': 'application/json',
            'trakt-api-key': CLIENT_ID,
            'trakt-api-version': '2'
        }

        if get_trakt_credentials_info():
            headers['Authorization'] = f'Bearer {c.get_setting("trakt.token")}'

        if conditional_headers:
            headers.update(conditional_headers)

        response = session.get(url, headers=headers, timeout=30)

        # 304 Not Modified - data hasn't changed
        if response.status_code == 304:
            c.log(f'[Trakt Batch] {media_type} not modified (304), using cached data')
            # Return cached validators
            return None, last_modified, etag

        # Success - return data and new cache validators
        if response.status_code == 200:
            new_last_modified = response.headers.get('Last-Modified')
            new_etag = response.headers.get('ETag')
            data = response.json()
            c.log(f'[Trakt Batch] Fetched {len(data)} {media_type}, Last-Modified: {new_last_modified}, ETag: {new_etag}')
            return data, new_last_modified, new_etag

        # Handle other status codes
        c.log(f'[Trakt Batch] Unexpected status {response.status_code} for {media_type}')
        return None, None, None

    except (requests.RequestException, json.JSONDecodeError, KeyError, TypeError) as e:
        c.log(f'[Trakt Batch] Exception in get_watched_batch: {e}')
        return None, None, None


def sync_watched_batch():
    """Sync all watched content from Trakt using optimized batch operations with conditional requests."""
    try:
        # Get last sync validators from cache (Last-Modified timestamps and ETags)
        shows_last_modified = cache.get(lambda: c.get_setting('trakt.shows.last_modified'), 0)
        shows_etag = cache.get(lambda: c.get_setting('trakt.shows.etag'), 0)
        movies_last_modified = cache.get(lambda: c.get_setting('trakt.movies.last_modified'), 0)
        movies_etag = cache.get(lambda: c.get_setting('trakt.movies.etag'), 0)

        result = {}

        # Fetch shows with conditional request (using both Last-Modified and ETag)
        shows_data, shows_timestamp, shows_new_etag = get_watched_batch('shows', shows_last_modified, shows_etag)
        if shows_data is not None:
            result['shows'] = shows_data
            # Save new cache validators
            if shows_timestamp:
                c.set_setting('trakt.shows.last_modified', shows_timestamp)
            if shows_new_etag:
                c.set_setting('trakt.shows.etag', shows_new_etag)
        else:
            c.log('[Trakt Batch] Shows data unchanged (304), using cached version')

        # Fetch movies with conditional request (using both Last-Modified and ETag)
        movies_data, movies_timestamp, movies_new_etag = get_watched_batch('movies', movies_last_modified, movies_etag)
        if movies_data is not None:
            result['movies'] = movies_data
            # Save new cache validators
            if movies_timestamp:
                c.set_setting('trakt.movies.last_modified', movies_timestamp)
            if movies_new_etag:
                c.set_setting('trakt.movies.etag', movies_new_etag)
        else:
            c.log('[Trakt Batch] Movies data unchanged (304), using cached version')

        return result if result else None

    except (KeyError, TypeError, AttributeError) as e:
        c.log(f'[Trakt Batch] Exception in sync_watched_batch: {e}')
        return None


def get_progress_batch(show_ids):
    """Get watch progress for multiple shows in optimized batches."""
    try:
        if not show_ids:
            return {}

        # Trakt doesn't have a true batch progress endpoint, but we can optimize
        # by requesting only hidden shows to reduce payload
        url = '/users/hidden/progress_watched'
        params = '?type=show&limit=1000'

        response = get_trakt(url + params)

        if not response:
            return {}

        # Process response into dict for quick lookup
        progress_dict = {}
        for item in response:
            show_id = item.get('show', {}).get('ids', {}).get('trakt')
            if show_id:
                progress_dict[show_id] = item

        c.log(f'[Trakt Batch] Fetched progress for {len(progress_dict)} shows')
        return progress_dict

    except (requests.RequestException, json.JSONDecodeError, KeyError, TypeError) as e:
        c.log(f'[Trakt Batch] Exception in get_progress_batch: {e}')
        return {}

def getMovieTranslation(_id, lang, full=False):
    url = f'/movies/{_id}/translations/{lang}'
    try:
        item = getTraktAsJson(url)[0]
        return item if full else item.get('title')
    except (IndexError, KeyError, TypeError, Exception):
        pass  # API failure or parsing error


def getTVShowTranslation(_id, lang, season=None, episode=None, full=False):
    if season and episode:
        url = f'/shows/{_id}/seasons/{season}/episodes/{episode}/translations/{lang}'
    else:
        url = f'/shows/{_id}/translations/{lang}'

    try:
        item = getTraktAsJson(url)[0]
        return item if full else item.get('title')
    except (IndexError, KeyError, TypeError, Exception):
        pass  # API failure or parsing error


def getMovieAliases(_id):
    try:
        return getTraktAsJson(f'/movies/{_id}/aliases')
    except (Exception):
        return []  # API failure


def getTVShowAliases(_id):
    try:
        return getTraktAsJson(f'/shows/{_id}/aliases')
    except (Exception):
        return []  # API failure


def getMovieSummary(_id, full=True):
    try:
        url = f'/movies/{_id}'
        if full:
            url += '?extended=full'
        return getTraktAsJson(url)
    except (Exception):
        return  # API failure


def getTVShowSummary(_id, full=True):
    try:
        url = f'/shows/{_id}'
        if full:
            url += '?extended=full'
        return getTraktAsJson(url)
    except (Exception):
        return  # API failure


def getPeople(_id, content_type, full=True):
    try:
        url = f'/{content_type}/{_id}/people'
        if full:
            url += '?extended=full'
        return getTraktAsJson(url)
    except (Exception):
        return  # API failure


def get_boxoffice():
    """Get weekend box office top 10 movies (updates Monday)"""
    try:
        if not get_trakt_credentials_info():
            return []
        return getTraktAsJson('/movies/boxoffice')
    except (Exception):
        return []  # API failure


def get_recommendations_movies(page=1, limit=20):
    """Get personalized movie recommendations based on watch history"""
    try:
        if not get_trakt_credentials_info():
            return []
        return getTraktAsJson(f'/recommendations/movies?page={page}&limit={limit}')
    except (Exception):
        return []  # API failure


def get_recommendations_shows(page=1, limit=20):
    """Get personalized TV show recommendations"""
    try:
        if not get_trakt_credentials_info():
            return []
        return getTraktAsJson(f'/recommendations/shows?page={page}&limit={limit}')
    except (Exception):
        return []  # API failure


def get_movies_popular(page=1, limit=40):
    """Get popular movies"""
    try:
        return getTraktAsJson(f'/movies/popular?page={page}&limit={limit}')
    except (Exception):
        return []  # API failure


def get_movies_anticipated(page=1, limit=40):
    """Get most anticipated upcoming movies"""
    try:
        return getTraktAsJson(f'/movies/anticipated?page={page}&limit={limit}')
    except (Exception):
        return []  # API failure


def get_movies_played(page=1, limit=40):
    """Get most played movies (by watch count)"""
    try:
        return getTraktAsJson(f'/movies/played?page={page}&limit={limit}')
    except (Exception):
        return []  # API failure


def get_movies_watched(page=1, limit=40):
    """Get most watched movies (by unique users)"""
    try:
        return getTraktAsJson(f'/movies/watched?page={page}&limit={limit}')
    except (Exception):
        return []  # API failure


def get_movies_collected(page=1, limit=40):
    """Get most collected movies (in user libraries)"""
    try:
        return getTraktAsJson(f'/movies/collected?page={page}&limit={limit}')
    except (Exception):
        return []  # API failure


def get_shows_popular(page=1, limit=40):
    """Get popular TV shows"""
    try:
        return getTraktAsJson(f'/shows/popular?page={page}&limit={limit}')
    except (Exception):
        return []  # API failure


def get_shows_anticipated(page=1, limit=40):
    """Get most anticipated upcoming TV shows"""
    try:
        return getTraktAsJson(f'/shows/anticipated?page={page}&limit={limit}')
    except (Exception):
        return []  # API failure


def get_shows_played(page=1, limit=40):
    """Get most played TV shows (by watch count)"""
    try:
        return getTraktAsJson(f'/shows/played?page={page}&limit={limit}')
    except (Exception):
        return []  # API failure


def get_shows_watched(page=1, limit=40):
    """Get most watched TV shows (by unique users)"""
    try:
        return getTraktAsJson(f'/shows/watched?page={page}&limit={limit}')
    except (Exception):
        return []  # API failure


def get_shows_collected(page=1, limit=40):
    """Get most collected TV shows (in user libraries)"""
    try:
        return getTraktAsJson(f'/shows/collected?page={page}&limit={limit}')
    except (Exception):
        return []  # API failure


def get_related_movies(movie_id, limit=10):
    """Get related movies based on Trakt's algorithm (uses Trakt ID, slug, or IMDB ID)."""
    try:
        return getTraktAsJson(f'/movies/{movie_id}/related?limit={limit}')
    except (Exception):
        return []  # API failure


def get_related_shows(show_id, limit=10):
    """Get related TV shows based on Trakt's algorithm (uses Trakt ID, slug, or IMDB ID)."""
    try:
        return getTraktAsJson(f'/shows/{show_id}/related?limit={limit}')
    except (Exception):
        return []  # API failure


def get_calendar_shows(days=7, start_date=None):
    """Get calendar of TV show episodes airing for the authenticated user."""
    try:
        if start_date is None:
            from datetime import datetime
            start_date = datetime.now().strftime('%Y-%m-%d')

        return getTraktAsJson(f'/calendars/my/shows/{start_date}/{days}')
    except (Exception):
        return []  # API failure


def get_calendar_movies(days=30, start_date=None):
    """Get calendar of movie releases for the authenticated user."""
    try:
        if start_date is None:
            from datetime import datetime
            start_date = datetime.now().strftime('%Y-%m-%d')

        return getTraktAsJson(f'/calendars/my/movies/{start_date}/{days}')
    except (Exception):
        return []  # API failure


def SearchAll(title, year, full=True):
    try:
        movies = SearchMovie(title, year, full) or []
        shows = SearchTVShow(title, year, full) or []

        # Ensure both are lists before concatenation
        if not isinstance(movies, list):
            movies = [movies]
        if not isinstance(shows, list):
            shows = [shows]

        return movies + shows
    except (Exception):
        return []  # Search failure


def SearchMovie(title, year, full=True):
    try:
        title = quote_plus(title)
        url = f'/search/movie?query={title}'

        if year:
            url += f'&year={year}'
        if full:
            url += '&extended=full'
        return getTraktAsJson(url)
    except Exception:
        return


def SearchTVShow(title, year, full=True):
    try:
        title = quote_plus(title)
        url = f'/search/show?query={title}'

        if year:
            url += f'&year={year}'
        if full:
            url += '&extended=full'
        return getTraktAsJson(url)
    except Exception:
        return

def IdLookup(content, _type, type_id):
    try:
        r = getTraktAsJson(f'/search/{_type}/{type_id}?type={content}')
        return r[0].get(content, {}).get('ids', []) if r is not None else {}
    except Exception:
        return {}

def getGenre(content, _type, type_id):
    try:
        r = f'/search/{_type}/{type_id}?type={content}&extended=full'
        c.log(f"trakt operation response: {r}")
        r = getTraktAsJson(r)
        return r[0].get(content, {}).get('genres', []) if r is not None else []
    except Exception:
        return []

def getEpisodeRating(imdb, season, episode):
    """Get the rating and votes for a given episode."""

    try:
        if not imdb.startswith('tt'):
            imdb = f'tt{imdb}'
        url = f'/shows/{imdb}/seasons/{season}/episodes/{episode}/ratings'
        r = getTraktAsJson(url)
        if r is None:
            return '0', '0'

        if isinstance(r, dict):
            r1 = r.get('rating', '0')
            r2 = r.get('votes', '0')
        return str(r1), str(r2)
    except crew_errors.GeneralError as e:
        c.log(f'Trakt getEpisodeRating Error: {e}')
        return '0', '0'


####################################################################################################
# Ratings - User ratings and community ratings (added 2026-03-10)
####################################################################################################

def get_my_ratings(media_type='all'):
    """Get user's personal ratings from Trakt."""
    try:
        if not get_trakt_credentials_info():
            c.log('[Trakt] Cannot get ratings - not authenticated')
            return []

        if media_type == 'all':
            url = '/sync/ratings'
        else:
            url = f'/sync/ratings/{media_type}'

        c.log(f'[Trakt] Fetching user ratings: {url}')
        return getTraktAsJson(url) or []
    except (requests.RequestException, json.JSONDecodeError, KeyError) as e:
        c.log(f'[Trakt] Error fetching ratings: {e}')
        return []


def rate_media(media_type, media_id, rating, id_type='tmdb', season=None, episode=None):
    """Submit a rating for a movie, show, season, or episode."""
    try:
        if not get_trakt_credentials_info():
            c.log('[Trakt] Cannot rate - not authenticated')
            c.infoDialog('Please authenticate with Trakt to rate', 'Trakt Not Connected', sound=False)
            return None

        # Validate rating (1-10)
        rating = int(rating)
        if rating < 1 or rating > 10:
            c.log(f'[Trakt] Invalid rating: {rating} (must be 1-10)')
            return None

        # Ensure IMDB IDs have 'tt' prefix
        if id_type == 'imdb' and not media_id.startswith('tt'):
            media_id = f'tt{media_id}'

        # Build payload based on media type
        if media_type == 'movie':
            payload = {
                'movies': [{
                    'ids': {id_type: media_id},
                    'rated_at': datetime.utcnow().isoformat() + 'Z',
                    'rating': rating
                }]
            }
        elif media_type == 'show':
            payload = {
                'shows': [{
                    'ids': {id_type: media_id},
                    'rated_at': datetime.utcnow().isoformat() + 'Z',
                    'rating': rating
                }]
            }
        elif media_type == 'season' and season is not None:
            payload = {
                'shows': [{
                    'ids': {id_type: media_id},
                    'seasons': [{
                        'number': int(season),
                        'rated_at': datetime.utcnow().isoformat() + 'Z',
                        'rating': rating
                    }]
                }]
            }
        elif media_type == 'episode' and season is not None and episode is not None:
            payload = {
                'shows': [{
                    'ids': {id_type: media_id},
                    'seasons': [{
                        'number': int(season),
                        'episodes': [{
                            'number': int(episode),
                            'rated_at': datetime.utcnow().isoformat() + 'Z',
                            'rating': rating
                        }]
                    }]
                }]
            }
        else:
            c.log(f'[Trakt] Invalid media_type or missing season/episode: {media_type}')
            return None

        c.log(f'[Trakt] Rating {media_type} {media_id} as {rating}/10')
        result = get_trakt('/sync/ratings', post=payload)

        if result:
            c.log(f'[Trakt] Rating submitted successfully: {result}')
            c.infoDialog(f'Rated {rating}/10', 'Trakt Rating Submitted', sound=False)
            return result
        else:
            c.log('[Trakt] Rating submission failed')
            return None
    except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError) as e:
        c.log(f'[Trakt] Error submitting rating: {e}')
        return None


def remove_rating(media_type, media_id, id_type='tmdb', season=None, episode=None):
    """Remove a rating for a movie, show, season, or episode."""
    try:
        if not get_trakt_credentials_info():
            c.log('[Trakt] Cannot remove rating - not authenticated')
            return None

        # Ensure IMDB IDs have 'tt' prefix
        if id_type == 'imdb' and not media_id.startswith('tt'):
            media_id = f'tt{media_id}'

        # Build payload (same structure as rate_media, but without rating/rated_at)
        if media_type == 'movie':
            payload = {'movies': [{'ids': {id_type: media_id}}]}
        elif media_type == 'show':
            payload = {'shows': [{'ids': {id_type: media_id}}]}
        elif media_type == 'season' and season is not None:
            payload = {
                'shows': [{
                    'ids': {id_type: media_id},
                    'seasons': [{'number': int(season)}]
                }]
            }
        elif media_type == 'episode' and season is not None and episode is not None:
            payload = {
                'shows': [{
                    'ids': {id_type: media_id},
                    'seasons': [{
                        'number': int(season),
                        'episodes': [{'number': int(episode)}]
                    }]
                }]
            }
        else:
            c.log(f'[Trakt] Invalid media_type or missing season/episode: {media_type}')
            return None

        c.log(f'[Trakt] Removing rating for {media_type} {media_id}')
        result = get_trakt('/sync/ratings/remove', post=payload)

        if result:
            c.log(f'[Trakt] Rating removed successfully: {result}')
            c.infoDialog('Rating removed', 'Trakt Rating', sound=False)
            return result
        else:
            c.log('[Trakt] Rating removal failed')
            return None
    except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError) as e:
        c.log(f'[Trakt] Error removing rating: {e}')
        return None


def get_community_rating_movie(movie_id, id_type='imdb'):
    """Get community rating and votes for a movie."""
    try:
        # Use movie ID directly in URL path
        url = f'/movies/{movie_id}/ratings'
        c.log(f'[Trakt] Fetching movie community rating: {url}')

        result = getTraktAsJson(url)
        if result and isinstance(result, dict):
            rating = str(result.get('rating', '0'))
            votes = str(result.get('votes', '0'))
            c.log(f'[Trakt] Movie community rating: {rating}/10 ({votes} votes)')
            return rating, votes
        return '0', '0'
    except (requests.RequestException, json.JSONDecodeError, KeyError, TypeError) as e:
        c.log(f'[Trakt] Error fetching movie community rating: {e}')
        return '0', '0'


def get_community_rating_show(show_id, id_type='imdb'):
    """Get community rating and votes for a TV show."""
    try:
        # Use show ID directly in URL path
        url = f'/shows/{show_id}/ratings'
        c.log(f'[Trakt] Fetching show community rating: {url}')

        result = getTraktAsJson(url)
        if result and isinstance(result, dict):
            rating = str(result.get('rating', '0'))
            votes = str(result.get('votes', '0'))
            c.log(f'[Trakt] Show community rating: {rating}/10 ({votes} votes)')
            return rating, votes
        return '0', '0'
    except (requests.RequestException, json.JSONDecodeError, KeyError, TypeError) as e:
        c.log(f'[Trakt] Error fetching show community rating: {e}')
        return '0', '0'


def get_user_stats():
    """Get user's watch statistics from Trakt."""
    try:
        if not get_trakt_credentials_info():
            c.log('[Trakt] Cannot get stats - not authenticated')
            return None

        c.log('[Trakt] Fetching user statistics')
        result = getTraktAsJson('/users/me/stats')

        if result and isinstance(result, dict):
            c.log(  f'[Trakt] Stats retrieved - Movies: {result.get("movies", {}).get("watched", 0)}, '
                        f'Shows: {result.get("shows", {}).get("watched", 0)}, '
                        f'Episodes: {result.get("episodes", {}).get("watched", 0)}')
            return result

        c.log('[Trakt] No stats data returned')
        return None
    except Exception as e:
        c.log(f'[Trakt] Error fetching user stats: {e}')
        return None


def show_stats_dialog():
    """Show the Trakt statistics dialog."""
    try:
        import xbmcaddon
        from ..windows.trakt_stats_dialog import TraktStatsDialog

        c.log("[Trakt Stats] Fetching user statistics...")

        # Check if user is authenticated
        if not get_trakt_credentials_info():
            c.infoDialog(
                'Authorize Trakt first - Settings > Accounts > Trakt',
                heading='Trakt Statistics',
                sound=True,
                time=5000,
            )
            return

        # Get username
        username = control.setting('trakt.user') or 'User'

        # Fetch stats
        stats = get_user_stats()

        if not stats:
            c.infoDialog(
                'Unable to fetch Trakt statistics - try again later',
                heading='Trakt Statistics',
                sound=True,
                time=5000,
            )
            return

        # Show dialog
        c.log("[Trakt Stats] Showing dialog")

        # Get addon path
        addon = xbmcaddon.Addon('script.midnight.artwork')

        dialog = TraktStatsDialog(
            'TraktStats.xml',
            addon.getAddonInfo('path'),
            'midnight',
            '1080i',
            stats=stats,
            username=username
        )
        dialog.doModal()
        del dialog

        c.log("[Trakt Stats] Dialog closed")

    except Exception as e:
        c.log(f"[Trakt Stats] Error showing dialog: {e}", 1)
        control.dialog.ok('Error', f'Unable to show statistics: {str(e)}')
    finally:
        control.idle()



####################################################################################################
# Database - 25-11-2024
#
# cm new from here to add functions for syncing with trakt
#
####################################################################################################
sql_dict = {
    'sql_create_movies_collection' :
        'CREATE TABLE IF NOT EXISTS movies_collection (last_collected_at TEXT, last_updated_at TEXT, Title TEXT, Year INT, trakt INT, slug TEXT, imdb TEXT, tmdb INT, UNIQUE(trakt, imdb, tmdb));',
    'sql_create_shows_collection' :
        'CREATE TABLE IF NOT EXISTS shows_collection (last_collected_at TEXT, last_updated_at TEXT, Title TEXT, Year INT, trakt INT, slug TEXT, tvdb INT, imdb TEXT, tmdb INT, tvrage TEXT, UNIQUE(trakt, tvdb, imdb, tmdb));',
    'sql_create_seasons_collection' :
        'CREATE TABLE IF NOT EXISTS seasons_collection (trakt INT, tvdb INT, imdb TEXT, tmdb INT, season INT, episode INT, collected_at TEXT, UNIQUE (trakt, tvdb, imdb, tmdb, season, episode));',
    'sql_create_trakt_progress' :
        'CREATE TABLE progress (media_type text not null, trakt integer primary key, imdb text, tmdb integer, tvdb integer, showtrakt integer, showimdb text, showtmdb integer, showtvdb integer, season integer, episode integer, resume_point real, curr_time text, last_played text, resume_id integer, tvshowtitle text, title text, year integer)',
    'sql_insert_trakt_progress' :
        'INSERT OR REPLACE INTO progress (media_type, trakt, imdb, tmdb, tvdb, showtrakt, showimdb, showtmdb, showtvdb, season, episode, resume_point, curr_time, last_played, resume_id, tvshowtitle, title, year) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,?, ?, ?, ?) ',
    'sql_create_service' :
        'CREATE TABLE IF NOT EXISTS service(setting TEXT, value TEXT, UNIQUE(setting));',
    'sql_update_service' :
        'UPDATE service SET value = ? where setting = ?',

    'sql_create_sync_data' :
        'CREATE TABLE sync_data (media_type TEXT NOT NULL, name TEXT NOT NULL, date TEXT);',
    'sql_insert_sync_data' :
        'INSERT OR REPLACE INTO sync_data (media_type, name, date) VALUES (?,?,?)',
    'sql_select_sync_data' :
        'SELECT date FROM sync_data WHERE media_type = ? AND name = ?',
    'sql_delete_sync_data' :
        'DELETE FROM sync_data WHERE media_type = ? AND name = ?',
    'sql_create_trakt_watched' :
        'CREATE TABLE IF NOT EXISTS watched (media_type text not null, trakt integer, tvdb integer, imdb TEXT, tmdb integer, season integer, episode integer, last_played text, title text, unique(media_type, trakt, season, episode))',
    'sql_insert_trakt_watched' :
        'INSERT OR REPLACE INTO watched (media_type, trakt, tvdb, imdb, tmdb, season, episode, last_played, title) VALUES (?,?,?,?,?,?,?,?,?)',
    'sql_create_trakt_history' :
        'CREATE TABLE IF NOT EXISTS trakt_history (history_id INTEGER PRIMARY KEY, watched_at TEXT NOT NULL, action TEXT, media_type TEXT NOT NULL, trakt_id INTEGER, imdb_id TEXT, tmdb_id INTEGER, tvdb_id INTEGER, title TEXT, year INTEGER, season INTEGER, episode INTEGER, show_trakt_id INTEGER, show_title TEXT)',
    'sql_insert_trakt_history' :
        'INSERT OR REPLACE INTO trakt_history (history_id, watched_at, action, media_type, trakt_id, imdb_id, tmdb_id, tvdb_id, title, year, season, episode, show_trakt_id, show_title) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
    'sql_create_scrobble_queue' :
        'CREATE TABLE IF NOT EXISTS scrobble_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, media_type TEXT, imdb TEXT, season TEXT, episode TEXT, progress REAL, action TEXT, timestamp TEXT, UNIQUE(media_type, imdb, season, episode))',
    'sql_insert_scrobble_queue' :
        'INSERT OR REPLACE INTO scrobble_queue (media_type, imdb, season, episode, progress, action, timestamp) VALUES (?,?,?,?,?,?,?)',
    'sql_select_scrobble_queue' :
        'SELECT id, media_type, imdb, season, episode, progress, action FROM scrobble_queue ORDER BY timestamp ASC',
    'sql_delete_scrobble_item' :
        'DELETE FROM scrobble_queue WHERE id = ?',
    'sql_clear_scrobble_queue' :
        'DELETE FROM scrobble_queue',
    'sql_create_next_episode_cache' :
        'CREATE TABLE IF NOT EXISTS next_episode_cache (show_tmdb INTEGER, season INTEGER, episode INTEGER, episode_exists BOOLEAN, last_checked TEXT, trakt_watched_at TEXT, trakt_paused_at TEXT, PRIMARY KEY (show_tmdb, season, episode))',
    'sql_insert_next_episode_cache' :
        'INSERT OR REPLACE INTO next_episode_cache (show_tmdb, season, episode, episode_exists, last_checked, trakt_watched_at, trakt_paused_at) VALUES (?,?,?,?,?,?,?)',
    'sql_select_next_episode_cache' :
        'SELECT episode_exists, last_checked, trakt_watched_at, trakt_paused_at FROM next_episode_cache WHERE show_tmdb = ? AND season = ? AND episode = ?',
    'sql_delete_old_cache_entries' :
        'DELETE FROM next_episode_cache WHERE last_checked < ?',
}

@with_db_connection(return_on_error=None, return_as_dict=True)
def check_next_episode_cache(dbcon, dbcur, show_tmdb, season, episode, cache_dict=None):
    """Check if an episode existence validation is cached and still valid.

    Args:
        cache_dict: Optional pre-loaded cache dict from batch_load_episode_cache_for_shows()
                   Format: {(show_tmdb, season, episode): cache_entry_dict}
    """
    try:
        from datetime import datetime, timedelta

        # Check pre-loaded cache first (10-50x faster than DB query)
        if cache_dict is not None:
            cache_key = (show_tmdb, season, episode)
            if cache_key in cache_dict:
                cached_entry = cache_dict[cache_key]
                episode_exists = cached_entry['episode_exists']
                last_checked = cached_entry['last_checked']
                cached_watched_at = cached_entry['cached_watched_at']
                cached_paused_at = cached_entry['cached_paused_at']

                # Validate the cached entry (same logic as DB path)
                try:
                    last_checked_dt = datetime.strptime(last_checked, "%Y-%m-%dT%H:%M:%S.%fZ")
                except ValueError:
                    return None

                # Check if cache is too old (>7 days)
                if datetime.utcnow() - last_checked_dt > timedelta(days=7):
                    return None

                # TMDB 404s are stable — watching other episodes does not create missing ones
                if not episode_exists:
                    return False

                # Get current Trakt activity timestamps
                current_watched, current_paused = get_trakt_activity_from_db()

                # Invalidate if activity changed
                if current_watched != cached_watched_at or current_paused != cached_paused_at:
                    return None

                # Cache is valid
                return bool(episode_exists)
            # Key not in cache, fall through to None

        # Fallback to DB query if cache_dict not provided or key not found
        sql = sql_dict['sql_select_next_episode_cache']
        dbcur.execute(sql, (show_tmdb, season, episode))
        result = dbcur.fetchone()

        if not result:
            return None

        episode_exists, last_checked, cached_watched_at, cached_paused_at = result

        # Parse last_checked timestamp
        try:
            last_checked_dt = datetime.strptime(last_checked, "%Y-%m-%dT%H:%M:%S.%fZ")
        except ValueError:
            # Invalid timestamp, invalidate cache
            return None

        # Check if cache is too old (>7 days) - new episodes might have aired
        if datetime.utcnow() - last_checked_dt > timedelta(days=7):
            c.log(f"[NextEpisodeCache] Cache expired for show {show_tmdb} S{season:02d}E{episode:02d} (>7 days old)")
            return None

        # TMDB 404s are stable — watching other episodes does not create missing ones
        if not episode_exists:
            return False

        # Get current Trakt activity timestamps from database (not API!)
        current_watched, current_paused = get_trakt_activity_from_db()

        # Invalidate if user watched or paused episodes since cache was created
        if current_watched != cached_watched_at or current_paused != cached_paused_at:
            c.log(f"[NextEpisodeCache] Trakt activity changed for show {show_tmdb} S{season:02d}E{episode:02d}")
            return None

        # Cache is valid
        # Verbose log commented out for performance (reduces log spam)
        # c.log(f"[NextEpisodeCache] Using cached result for show {show_tmdb} S{season:02d}E{episode:02d}: exists={episode_exists}")
        return bool(episode_exists)

    except Exception as e:
        c.log(f"[NextEpisodeCache] Error checking cache: {e}")
        return None

def get_trakt_activity_from_db():
    """Get Trakt activity timestamps from the database (already cached by syncTrakt)."""
    try:
        watched_at = get_trakt_table_value('episodes', 'watched_at') or ''
        paused_at = get_trakt_table_value('episodes', 'paused_at') or ''
        return (watched_at, paused_at)
    except Exception as e:
        c.log(f"[NextEpisodeCache] Error reading Trakt activity from DB: {e}")
        return ('', '')


@with_db_connection(return_on_error=None, return_as_dict=True)
def update_next_episode_cache(dbcon, dbcur, show_tmdb, season, episode, exists):
    """Update the cache with an episode existence validation result."""
    try:
        from datetime import datetime

        # Ensure table exists
        if not table_exists('next_episode_cache'):
            create_table('next_episode_cache')

        # Get current Trakt activity timestamps from database (not API!)
        watched_at, paused_at = get_trakt_activity_from_db()

        # Store result with current timestamp and Trakt activity
        last_checked = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
        sql = sql_dict['sql_insert_next_episode_cache']
        dbcur.execute(sql, (show_tmdb, season, episode, int(exists), last_checked, watched_at, paused_at))

        dbcon.commit()

        c.log(f"[NextEpisodeCache] Cached result for show {show_tmdb} S{season:02d}E{episode:02d}: exists={exists}")

    except Exception as e:
        c.log(f"[NextEpisodeCache] Error updating cache: {e}")
        if dbcon:
            dbcon.close()

@with_db_connection(return_on_error=None)
def init_next_episode_cache(dbcon, dbcur):
    """Initialize the next_episode_cache table and clean up old entries."""
    from datetime import datetime, timedelta

    # Create table if it doesn't exist
    if not table_exists('next_episode_cache'):
        c.log("[NextEpisodeCache] Creating next_episode_cache table")
        create_table('next_episode_cache')

    # Clean up old entries (>30 days)
    cutoff_date = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    sql = sql_dict['sql_delete_old_cache_entries']
    dbcur.execute(sql, (cutoff_date,))
    deleted_count = dbcur.rowcount

    if deleted_count > 0:
        c.log(f"[NextEpisodeCache] Cleaned up {deleted_count} old cache entries")

    dbcon.commit()

@with_db_connection(return_on_error=None, return_as_dict=True)
def batch_load_episode_cache_for_shows(dbcon, dbcur, show_ids):
    """Batch load all episode cache entries for multiple shows (10-50x faster than individual queries).

    Args:
        show_ids: List of show TMDB IDs

    Returns:
        dict: {(show_tmdb, season, episode): cache_entry_dict} for quick lookups
    """
    try:
        from datetime import datetime, timedelta

        if not show_ids:
            return {}

        # Ensure table exists
        if not table_exists('next_episode_cache'):
            return {}

        # Build SQL with IN clause for batch query
        placeholders = ','.join('?' * len(show_ids))
        sql = f"SELECT show_tmdb, season, episode, episode_exists, last_checked, trakt_watched_at, trakt_paused_at FROM next_episode_cache WHERE show_tmdb IN ({placeholders})"

        dbcur.execute(sql, show_ids)
        results = dbcur.fetchall()

        # Build lookup dict
        cache_dict = {}
        for row in results:
            show_tmdb, season, episode, episode_exists, last_checked, cached_watched, cached_paused = row
            cache_dict[(show_tmdb, season, episode)] = {
                'episode_exists': episode_exists,
                'last_checked': last_checked,
                'cached_watched_at': cached_watched,
                'cached_paused_at': cached_paused
            }

        c.log(f"[NextEpisodeCache] Batch loaded {len(cache_dict)} cache entries for {len(show_ids)} shows")
        return cache_dict

    except Exception as e:
        c.log(f"[NextEpisodeCache] Error batch loading cache: {e}")
        return {}

def syncTrakt(silent=False) -> None:
    """
    Syncs Kodi status with Trakt.tv

    Args:
        silent: When True, suppresses all dialogs (used for scheduled background syncs).
    """
    if not get_trakt_credentials_info():
        c.log('(def syncTrakt) Trakt not configured — skipping sync', 1)
        return

    if silent and should_defer_background_trakt():
        c.log('(def syncTrakt) Deferred — boot quiet or rate-limit backoff', 1)
        return

    if not silent and not control.player.isPlayingVideo() and c.devmode:
        c.infoDialog('Syncing with Trakt', 'Please wait', icon='main_classy.png', sound=False)
    else:
        c.log('(def syncTrakt) Syncing with Trakt', 1)

    try:
        # Initialize next episode cache on startup (creates table, cleans old entries)
        init_next_episode_cache()

        # MUST run first: stores fresh Trakt timestamps into the local DB so that
        # fill_history() and fill_trakt_watched() can use them for change detection.
        # Running this last (as before) caused fill_history() to compare stale timestamps
        # and skip incremental fetches even when new activity existed on Trakt.
        sync_last_activities()

        fill_progress_table()
        fill_trakt_watched()

        # Sync collection in background (movies + shows)
        get_trakt_collection('all')

        # Sync watch history (incremental - only fetches new events)
        fill_history()

        if not silent and not control.player.isPlayingVideo():
            return c.infoDialog('Syncing with Trakt Finished', 'Please wait', icon='main_classy.png', sound=False)
        else:
            c.log('Syncing with Trakt Finished', 1)

    except (Exception, OperationalError) as e:
        pass

        failure = traceback.format_exc()
        c.log(f'Traceback:: {failure}')
        c.log(f'Exception raised in trakt flow: {e}')



def get_show_extended_info(trakt_id):
    endpoint = f'/shows/{trakt_id}?extended=full'
    return getTraktAsJson(endpoint)

def fill_progress_table() -> None:
    """Fetches progress from trakt and inserts/updates it in the database (trakt_progress)."""
    start, end = get_start_end(diff=365)

    # Fetch BOTH movies and episodes from Trakt playback progress
    for media in ['movies', 'episodes']:
        endpoint = f'sync/playback/{media}?extended=full&start_at={start}&end_at={end}'
        c.log(f"[Trakt] fill_progress_table: Fetching {media} from {endpoint}")

        if result := getTraktAsJson(endpoint):
            if not table_exists('trakt_progress'):
                create_table('trakt_progress')

            c.log(f"[Trakt] fill_progress_table: Processing {len(result)} {media}")

            for item in result:
                media_type = item.get('type')
                progress = item.get('progress')
                if progress is not None and float(progress) >= 92:
                    ids = item.get(media_type, {}).get('ids', {})
                    imdb_id = ids.get('imdb')
                    trakt_id = ids.get('trakt')
                    season = item.get(media_type, {}).get('season') if media_type == 'episode' else 0
                    episode = item.get(media_type, {}).get('number') if media_type == 'episode' else 0
                    if imdb_id:
                        delete_progress_from_database('imdb', imdb_id, season=season or 0, episode=episode or 0)
                    elif trakt_id:
                        delete_progress_from_database('trakt', trakt_id, season=season or 0, episode=episode or 0)
                    continue
                ids = item.get(media_type).get('ids')
                trakt_id = ids.get('trakt')
                tvdb_id = ids.get('tvdb')
                tmdb_id = ids.get('tmdb')
                imdb_id = ids.get('imdb')
                year = item.get(media_type).get('year')
                title = item.get(media_type).get('title')
                season = item.get(media_type).get('season') if media_type == 'episode' else 0
                episode = item.get(media_type).get('number') if media_type == 'episode' else 0
                current_time = get_now_in_iso()
                last_played = item.get('paused_at')
                resume_id = item.get('id')

                # Convert Trakt's progress percentage to seconds using runtime.
                # resume_point must be stored in seconds so the player can seek correctly.
                runtime_minutes = item.get(media_type, {}).get('runtime', 0) or 0
                if runtime_minutes > 0 and progress is not None:
                    resume_seconds = int((float(progress) / 100.0) * runtime_minutes * 60)
                else:
                    resume_seconds = 0
                c.log(f"[Trakt] fill_progress_table: {media_type} progress={progress}% runtime={runtime_minutes}min -> resume_seconds={resume_seconds}s")

                if 'show' in item:
                    tvshowtitle = item.get('show').get('title')
                    show_trakt_id = item.get('show').get('ids').get('trakt')
                    show_imdb_id = item.get('show').get('ids').get('imdb')
                    show_tmdb_id = item.get('show').get('ids').get('tmdb')
                    show_tvdb_id = item.get('show').get('ids').get('tvdb')
                else:
                    tvshowtitle = ''
                    show_trakt_id = 0
                    show_imdb_id = 0
                    show_tmdb_id = 0
                    show_tvdb_id = 0

                insert_trakt_progress(
                    media_type,
                    trakt_id,
                    imdb_id,
                    tmdb_id,
                    tvdb_id,
                    show_trakt_id,
                    show_imdb_id,
                    show_tmdb_id,
                    show_tvdb_id,
                    season,
                    episode,
                    resume_seconds,
                    current_time,
                    last_played,
                    resume_id,
                    tvshowtitle,
                    title,
                    year,
                )

def update_progress_in_database(imdb_id: str = '', trakt_id: int = 0, tmdb_id: int = 0, season: int = 0, episode: int = 0, progress: int = 0, resume_point: float = None) -> None:
    """Update progress in the trakt sync database using UPSERT logic."""
    try:
        # If resume_point not provided, use progress percentage as 0-1 fraction
        # Otherwise use the provided value (should be in seconds from bookmarks.reset)
        if resume_point is None:
            resume_point = progress / 100.0        # Determine media type
        media_type = 'episode' if season and episode else 'movie'

        c.log(f"[Trakt] update_progress_in_database: {media_type} imdb={imdb_id} progress={progress}% resume_point={resume_point}")

        connection = get_connection(control.traktsyncFile, return_as_dict=True)
        if connection:
            cursor = connection.cursor()
        else:
            raise OperationalError("Could not establish database connection.")

        # Use INSERT OR REPLACE with minimal fields
        # For movies: trakt is primary key, so we use a dummy value if not provided
        # For episodes: we need show info which we might not have, so try UPDATE first

        if media_type == 'movie':
            # Try UPDATE first
            conditions = []
            if imdb_id:
                conditions.append(f"imdb = '{imdb_id}'")
            if trakt_id:
                conditions.append(f"trakt = {trakt_id}")
            if tmdb_id:
                conditions.append(f"tmdb = {tmdb_id}")

            if conditions:
                sql = 'UPDATE progress SET resume_point = ? WHERE media_type = ? AND (' + ' OR '.join(conditions) + ')'
                c.log(f"[Trakt] Executing UPDATE: {sql} with resume_point={resume_point}")
                cursor.execute(sql, (resume_point, media_type))
                rows_updated = cursor.rowcount
                c.log(f"[Trakt] UPDATE affected {rows_updated} rows")

                if rows_updated == 0:
                    # Row doesn't exist, insert minimal record using imdb as the key
                    # Use a synthetic trakt ID based on imdb hash to satisfy primary key constraint
                    c.log(f"[Trakt] No existing row, inserting new record with imdb={imdb_id}")
                    synthetic_trakt_id = abs(hash(imdb_id)) % (10 ** 8) if imdb_id else (trakt_id if trakt_id else 0)
                    sql = 'INSERT OR REPLACE INTO progress (media_type, trakt, imdb, tmdb, tvdb, showtrakt, showimdb, showtmdb, showtvdb, season, episode, resume_point, curr_time, last_played, resume_id, tvshowtitle, title, year) VALUES (?, ?, ?, ?, 0, 0, NULL, 0, 0, 0, 0, ?, NULL, NULL, 0, NULL, NULL, 0)'
                    cursor.execute(sql, (media_type, synthetic_trakt_id, imdb_id, tmdb_id, resume_point))
                    c.log(f"[Trakt] INSERT completed with synthetic trakt_id={synthetic_trakt_id}")
        else:
            # Episode - try UPDATE using show identifiers (showimdb/showtmdb) + season/episode
            show_conditions = []
            if imdb_id:
                show_conditions.append(f"showimdb = '{imdb_id}'")
            if trakt_id:
                show_conditions.append(f"showtrakt = {trakt_id}")
            if tmdb_id:
                show_conditions.append(f"showtmdb = {tmdb_id}")

            if show_conditions:
                where_show = '(' + ' OR '.join(show_conditions) + ')'
                sql = f"UPDATE progress SET resume_point = ? WHERE media_type = 'episode' AND {where_show} AND season = {season} AND episode = {episode}"
                c.log(f"[Trakt] Executing UPDATE: {sql}")
                cursor.execute(sql, (resume_point,))
                rows_updated = cursor.rowcount
                c.log(f"[Trakt] UPDATE affected {rows_updated} rows")

                if rows_updated == 0:
                    # No existing row, insert a minimal record for the episode
                    c.log(f"[Trakt] No existing row, inserting new episode record imdb={imdb_id} tmdb={tmdb_id} S{season}E{episode}")
                    synthetic_trakt_id = abs(hash(f"{imdb_id or tmdb_id}S{season:02d}E{episode:02d}")) % (10 ** 8)
                    sql = ('INSERT OR REPLACE INTO progress '
                           '(media_type, trakt, imdb, tmdb, tvdb, showtrakt, showimdb, showtmdb, showtvdb, '
                           'season, episode, resume_point, curr_time, last_played, resume_id, tvshowtitle, title, year) '
                           'VALUES (?, ?, ?, ?, 0, ?, ?, ?, 0, ?, ?, ?, NULL, NULL, 0, NULL, NULL, 0)')
                    cursor.execute(sql, ('episode', synthetic_trakt_id, imdb_id, tmdb_id,
                                         trakt_id, imdb_id, tmdb_id,
                                         season, episode, resume_point))
                    c.log(f"[Trakt] INSERT completed with synthetic trakt_id={synthetic_trakt_id}")
            else:
                c.log("[Trakt] update_progress_in_database: no identifiers for episode, skipping")

        connection.commit()
        cursor.close()
        connection.close()
        c.log("[Trakt] update_progress_in_database completed successfully")
    except Exception as e:
        c.log(f"[Trakt] ERROR in update_progress_in_database: {e}")
        c.log(f"[Trakt] Traceback: {traceback.format_exc()}")


def delete_progress_from_database(key, media_id, season: int = 0, episode: int = 0) -> None:
    """Delete progress from the trakt sync database."""
    try:
        if key == 'imdb' and not media_id.startswith('tt'):
            media_id = f'tt{media_id}'
        connection = get_connection(control.traktsyncFile)
        if connection:
            cursor = connection.cursor()
        else:
            raise OperationalError("Could not establish database connection.")

        conditions = []
        if key == 'imdb' and media_id:
            # Episodes may store the show IMDB in showimdb (not imdb) — match both
            if season and episode:
                conditions.append(f"(imdb = '{media_id}' OR showimdb = '{media_id}')")
            else:
                conditions.append(f"imdb = '{media_id}'")
        if key == 'trakt' and media_id:
            conditions.append(f"trakt = {media_id}")
        if key == 'tmdb' and media_id:
            conditions.append(f"tmdb = {media_id}")
        if season:
            conditions.append(f"season = {season}")
        if episode:
            conditions.append(f"episode = {episode}")

        if conditions:
            sql = 'DELETE FROM progress WHERE ' + ' AND '.join(conditions)
            c.log(f"[Trakt] Executing DELETE: {sql}")
            cursor.execute(sql)
            c.log(f"[Trakt] DELETE affected {cursor.rowcount} rows")

        connection.commit()
        cursor.close()
        connection.close()
        c.log("[Trakt] delete_progress_from_database completed successfully")
    except Exception as e:
        c.log(f"[Trakt] ERROR in delete_progress_from_database: {e}")
        c.log(f"[Trakt] Traceback: {traceback.format_exc()}")


@with_db_connection(return_on_error=None, return_as_dict=True)
def fill_trakt_watched(dbcon, dbcur) -> None:
    """Fetch watched data from Trakt and update database using optimized batch sync."""
    # Use optimized batch sync with conditional requests
    batch_result = sync_watched_batch()

    if not batch_result:
        c.log('[Trakt] No watched data to sync (unchanged or error)')
        return

    if not table_exists('trakt_watched'):
        create_table('trakt_watched')

    # Process shows from batch result
    if 'shows' in batch_result:
        for item in batch_result['shows']:
            if not watched_show_has_episode_detail(item):
                continue
            media_type = 'show'
            show = item.get('show') or {}
            ids = show.get('ids') or {}
            trakt_id = ids.get('trakt')
            if not trakt_id:
                continue
            tvdb_id = ids.get('tvdb', 0)
            imdb_id = ids.get('imdb')
            tmdb_id = ids.get('tmdb')
            title = show.get('title')

            for season in item.get('seasons') or []:
                season_nr = season.get('number')
                if season_nr is None:
                    continue
                for episodes in season.get('episodes') or []:
                    episode_nr = episodes.get('number')
                    if episode_nr is None:
                        continue
                    last_watched_at = episodes.get('last_watched_at')
                    dbcur.execute(sql_dict['sql_insert_trakt_watched'],
                                (media_type, trakt_id, tvdb_id, imdb_id, tmdb_id,
                                season_nr, episode_nr, last_watched_at, title))

    # Process movies from batch result
    if 'movies' in batch_result:
        media_type = 'movie'
        for item in batch_result['movies']:
            movie = item['movie']
            trakt_id = movie['ids']['trakt']
            tvdb_id = movie['ids'].get('tvdb', 0)
            imdb_id = movie['ids'].get('imdb')
            tmdb_id = movie['ids'].get('tmdb')
            title = movie['title']
            last_watched_at = item['last_watched_at']

            dbcur.execute(sql_dict['sql_insert_trakt_watched'],
                        (media_type, trakt_id, tvdb_id, imdb_id, tmdb_id,
                        0, 0, last_watched_at, title))

    dbcon.commit()
    c.log('[Trakt] Batch sync completed successfully')



@with_db_connection(return_on_error=None, return_as_dict=True)
def fill_trakt_watched_orig(dbcon, dbcur) -> None:
    """Fetch watched data from trakt and insert/update it in the database (original implementation)."""
    try:
        pass

        _types = ['movies', 'shows']
        #_types = ['shows']

        for _type in _types:
            c.log(f"type = {_type} (type={type(_type)})")
            if _type == 'shows':
                result = fetch_watched_shows_progress(log_label='fill_trakt_watched_orig')
            else:
                result = getTraktAsJson(f'sync/watched/{_type}')
            if result:
                if _type == 'movies':
                    indicators = [i['movie']['ids']['tmdb'] for i in result]
                    c.log(f"indicators = {indicators}")
                if not table_exists('trakt_watched'):
                    create_table('trakt_watched')

                for item in result:
                    media_type = _type
                    trakt_id = item.get(_type).get('ids').get('trakt')
                    tvdb_id = item.get(_type).get('ids').get('tvdb') or 0
                    imdb_id = item.get(_type).get('ids').get('imdb')
                    tmdb_id = item.get(_type).get('ids').get('tmdb')
                    title = item.get(_type).get('title')


                    if _type == 'movies':
                        seasons = 0
                        episode = 0
                        last_watched_at = item.get('last_watched_at')
                        dbcur.execute(sql_dict['sql_insert_trakt_watched'], (media_type, trakt_id, tvdb_id, imdb_id, tmdb_id, seasons, episode, last_watched_at, title))
                    else:
                        seasons = item.get('seasons')
                        c.log(f"seasons = {seasons}")
                        for season in seasons:
                            season_nr = season.get('number')
                            c.log(f"season = {season_nr}")
                            for episodes in season:
                                episode_nr = episodes.get('number')
                                c.log(f"episode_nr = {episode_nr}")
                                for item in episodes.get('episodes'):
                                    c.log(f"item = {item}")
                                    last_watched_at = item.get('last_watched_at')
                                    c.log(f"last_watched = {last_watched_at}")
                                    dbcur.execute(sql_dict['sql_insert_trakt_watched'], (media_type, trakt_id, tvdb_id, imdb_id, tmdb_id, season_nr, episode_nr, last_watched_at, title))
                dbcon.commit()

    except Exception as e:
        failure = traceback.format_exc()
        c.log(f'Traceback:: {failure}')
        c.log(f'Exception raised: {e}')


@with_db_connection(return_on_error=None, return_as_dict=True)
def fill_history(dbcon, dbcur, force_full_sync=False) -> None:
    """Fetch watch history from Trakt and store it in the database (uses incremental sync)."""
    try:
        # Ensure table exists
        if not table_exists('trakt_history'):
            create_table('trakt_history')
            # First time - do full sync
            force_full_sync = True

        # Check if watched activity has changed (movies.watched_at or episodes.watched_at)
        latest_watched = max(
            get_trakt_table_value('movies', 'watched_at') or '',
            get_trakt_table_value('episodes', 'watched_at') or ''
        )

        last_history_sync = get_trakt_sync_value('last_history_sync') or ''

        # Skip sync if nothing changed
        if not force_full_sync and latest_watched and latest_watched == last_history_sync:
            if c.devmode:
                c.log(f"[Trakt] No new watch history detected (last={last_history_sync}), skipping")
            return

        # Determine sync range
        if force_full_sync or not last_history_sync:
            # Full sync from 2016
            start_date = '2016-06-01T00:00:00.000Z'
            c.log("[Trakt] Performing FULL history sync from 2016")
        else:
            # Incremental sync - fetch only new events since last sync
            start_date = last_history_sync
            c.log(f"[Trakt] Performing INCREMENTAL history sync from {start_date}")

        from resources.lib.modules import cleandate
        end_date = cleandate.now_to_iso()

        _types = ['movies', 'episodes']
        total_new_events = 0

        for _type in _types:
            endpoint = f'/sync/history/{_type}?start_at={start_date}&end_at={end_date}'

            result = getTraktAsJson(endpoint)
            if not result:
                c.log(f"[Trakt] No history data returned for {_type}")
                continue

            c.log(f"[Trakt] Processing {len(result)} history entries for {_type}")
            total_new_events += len(result)

            for item in result:
                try:
                    # Common fields
                    history_id = item.get('id')
                    watched_at = item.get('watched_at')
                    action = item.get('action', 'watch')
                    media_type = item.get('type')

                    if _type == 'movies':
                        # Movie data
                        movie = item.get('movie', {})
                        trakt_id = movie.get('ids', {}).get('trakt')
                        imdb_id = movie.get('ids', {}).get('imdb')
                        tmdb_id = movie.get('ids', {}).get('tmdb')
                        tvdb_id = None
                        title = movie.get('title')
                        year = movie.get('year')
                        season = None
                        episode = None
                        show_trakt_id = None
                        show_title = None

                    else:  # episodes
                        # Episode data
                        episode_data = item.get('episode', {})
                        trakt_id = episode_data.get('ids', {}).get('trakt')
                        imdb_id = episode_data.get('ids', {}).get('imdb')
                        tmdb_id = episode_data.get('ids', {}).get('tmdb')
                        tvdb_id = episode_data.get('ids', {}).get('tvdb')
                        title = episode_data.get('title')
                        year = None
                        season = episode_data.get('season')
                        episode = episode_data.get('number')

                        # Show data
                        show = item.get('show', {})
                        show_trakt_id = show.get('ids', {}).get('trakt')
                        show_title = show.get('title')

                    # Insert into database (INSERT OR REPLACE handles duplicates)
                    dbcur.execute(sql_dict['sql_insert_trakt_history'],
                                (history_id, watched_at, action, media_type,
                                 trakt_id, imdb_id, tmdb_id, tvdb_id,
                                 title, year, season, episode,
                                 show_trakt_id, show_title))

                except Exception as e:
                    c.log(f"[Trakt] Error processing history item: {e}")
                    continue

            dbcon.commit()

        # Update last sync timestamp (use latest watched activity)
        if latest_watched:
            if not table_exists('service'):
                create_table('service')
            dbcur.execute(sql_dict['sql_update_service'], (latest_watched, 'last_history_sync'))
            dbcon.commit()
            c.log(f"[Trakt] History sync completed - {total_new_events} events processed, last_sync={latest_watched}")
        else:
            c.log(f"[Trakt] History sync completed - {total_new_events} events processed")

    except Exception as e:
        failure = traceback.format_exc()
        c.log(f'[Trakt] fill_history error - Traceback: {failure}')
        c.log(f'[Trakt] fill_history error - Exception: {e}')


def sync_last_activities():
    """Sync last activities from Trakt (uses 'all' timestamp as quick check, stores detailed activity timestamps if changed)."""
    try:
        # Fetch full activity data from Trakt
        i = getTraktAsJson('/sync/last_activities')
        if not i:
            c.log("[Trakt] sync_last_activities: No data returned from Trakt")
            return
        if not isinstance(i, dict):
            c.log(f"[Trakt] sync_last_activities: Unexpected response type {type(i).__name__}")
            return

        # Get the 'all' timestamp (latest across all activities)
        _all = i.get('all')
        if not _all:
            c.log("[Trakt] sync_last_activities: No 'all' timestamp in response")
            return

        # Check if anything changed by comparing 'all' timestamp
        stored_all = get_trakt_sync_value('all') or ''

        if _all == stored_all:
            # Nothing changed - skip updating
            if c.devmode:
                c.log(f"[Trakt] No activity changes detected (all={_all}), skipping sync")
            return

        # Something changed - store ALL the detailed activity timestamps
        if c.devmode:
            c.log(f"[Trakt] Activity changed ({stored_all} -> {_all}), storing all timestamps")

        create_trakt_tables(i)

        # Update crew_last_sync timestamp
        _crew = cleandate.now_to_iso()
        update_service(_all, _crew)

    except Exception as e:
        c.log(f"[Trakt] Error in sync_last_activities: {e}")
        import traceback
        c.log(f"[Trakt] Traceback: {traceback.format_exc()}")



@with_db_connection(return_on_error=None, return_as_dict=True)
def update_service(dbcon, dbcur, _all, _crew):
    try:
        d = {'all':_all, 'crew_last_sync': _crew}

        if not table_exists('service'):
            create_table('service')

        for k,v in d.items():
            sql = sql_dict['sql_update_service']
            dbcur.execute(sql, (v, k))
        dbcon.commit()
    except Exception as e:
        c.log(f"Exception raised: {e}")


@with_db_connection(return_on_error=None, return_as_dict=True)
def insert_trakt_progress(dbcon, dbcur, media_type, trakt, imdb, tmdb, tvdb, showtrakt, showimdb, showtmdb, showtvdb, season, episode, resume_point, curr_time, last_played, resume_id, tvshowtitle, title, year):
    "Insert progress data into trakt_progress table."
    sql = sql_dict['sql_insert_trakt_progress']
    dbcur.execute(sql, (media_type, trakt, imdb, tmdb, tvdb, showtrakt, showimdb, showtmdb, showtvdb, season, episode, resume_point, curr_time, last_played, resume_id, tvshowtitle, title, year))
    dbcon.commit()



def get_start_end(diff):
    """Returns a tuple of ISO 8601 formatted dates (start, end) for current date and given number of days ago."""
    start = datetime.now() - timedelta(days=diff)
    start = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return quote_plus(start), quote_plus(end)

def get_now_in_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000Z")

def get_connection(file, return_as_dict=False):
    """Establishes a connection to a SQLite database file."""
    try:
        pass

        if not os.path.isfile(control.dataPath):
            control.makeFile(control.dataPath)

        dbcon = database.connect(file)
        dbcon.execute('PRAGMA page_size = 32768')
        dbcon.execute('PRAGMA cache_size = 10000000')
        dbcon.execute('PRAGMA mmap_size = 30000000000')
        #dbcon.execute('PRAGMA journal_mode = OFF')
        dbcon.execute('PRAGMA journal_mode = MEMORY')
        dbcon.execute('PRAGMA temp_store = MEMORY')
        dbcon.execute('PRAGMA synchronous = OFF')

        if return_as_dict:
            dbcon.row_factory = _dict_factory
        return dbcon
    except Exception as e:
        c.log(f"Error getting database connection: {e}")
        return None

def get_connection_cursor(db_connection):
    """Returns a database cursor object from a connection."""
    if db_connection is None:
        c.log("Database connection is None")
        return None

    try:
        return db_connection.cursor()
    except Exception as e:
        c.log(f"Error getting database cursor: {e}")
        return None


def open_connection(file = control.traktsyncFile):
    """Opens a connection to a SQLite database file."""
    conn = get_connection(file)
    cursor = get_connection_cursor(conn)
    return None if conn is None or cursor is None else (conn, cursor)

def commit(db_connection) -> None:
    _commit(db_connection)

def close_connection(db_connection):
    """Closes a database connection."""
    if db_connection is not None:
        db_connection.close()
    else:
        c.log("Database connection is None")


def _commit(db_connection):
    """Commits the current transaction to the database."""
    if db_connection is None:
        c.log("Database connection is None")
        return
    else:
        c.log("Database connection is not None")
    try:
        if db_connection is not None:
            c.log(f"Database connection is of type(): {type(db_connection)}")
            db_connection.commit()
            c.log("Database connection is initialized")
        else:
            c.log("Database connection is not initialized")
    except AttributeError:
        c.log("Database connection does not have a connection attribute")
    except Exception as e:
        c.log(f"Error committing to the database: {e}")



def _dict_factory(cursor, row):
    """Construct a dictionary from a SQLite query result."""
    if cursor is None or row is None:
        raise ValueError("cursor and row must not be None")

    row_dict = {}
    for index, column in enumerate(cursor.description):
        try:
            row_dict[column[0]] = row[index]
        except Exception as e:
            c.log(f"Error constructing dictionary from SQLite query result: {e}")
    return row_dict

####
# end of core database functions


def get_db_connection(file=control.traktsyncFile, return_as_dict=True):
    """Return a database connection and cursor for the specified SQLite file."""
    try:
        dbcon = get_connection(file, return_as_dict)
        dbcur = get_connection_cursor(dbcon)

        # check if we have a valid connection and cursor
        if dbcur and dbcon:
            return dbcon, dbcur
        c.log("Error: dbcur or dbcon is None, cannot create connection")
        return None, None

    except (database.Error, database.OperationalError) as e:
        c.log(f"Error getting database connection: {e}")
        return None, None

####
# start of trakt functions

def check_sync_tables():
    """Check if the trakt sync tables exist and create them if not."""
    try:
        #fetch activities
        last_activities = getTraktAsJson('/sync/last_activities')
        c.log(f"last_activities type = {type(last_activities)}")

        if last_activities:
            create_trakt_tables(last_activities)
        else:
            c.log("Error: last_activities is None")
    except Exception as e:
        c.log(f"Error checking sync tables: {e}")

@with_db_connection(return_on_error=None, return_as_dict=True)
def fetch_last_service(dbcon, dbcur, fetch_all=False):
    """Retrieve the last sync timestamp from the trakt sync database."""
    try:
        sql = 'SELECT value FROM service where setting = "crew_last_sync"'
        if fetch_all:
            dbcur.execute(sql)
            return dbcur.fetchall()
        else:
            dbcur.execute(sql)
            return dbcur.fetchone()
    except (database.Error, database.OperationalError) as e:
        c.log(f"Error fetching last sync timestamp: {e}")
        return None

@with_db_connection(return_on_error=None, return_as_dict=True)
def fetch_last_activity(dbcon, dbcur, fetch_all=True, activity_type='all'):
    try:
        if fetch_all:
            sql = 'SELECT value FROM service where setting = "all"'
            dbcur.execute(sql)
            return dbcur.fetchall()
        else:
            sql = f'SELECT value FROM service where setting = "{activity_type}"'
            dbcur.execute(sql)
            return dbcur.fetchone()
    except (database.Error, database.OperationalError) as e:
        c.log(f"Error fetching last activity: {e}")
        return None

@with_db_connection(return_on_error=None, return_as_dict=True)
def create_trakt_tables(dbcon, dbcur, last_activities):
    """Create the tables in the trakt sync database if they don't exist."""
    try:
        for key, val in last_activities.items():
            if isinstance(val, str):
                if not table_exists('service'):
                    sql = "CREATE TABLE IF NOT EXISTS service(setting TEXT, value TEXT, UNIQUE(setting));"
                    dbcur.execute(sql)

                sql = f"INSERT OR REPLACE INTO service Values ('{key}', '{val}')"
                dbcur.execute(sql)

            elif isinstance(val, dict):
                if not table_exists(key):
                    sql = f"CREATE TABLE IF NOT EXISTS {key} (setting TEXT, value TEXT, UNIQUE(setting));"
                    dbcur.execute(sql)

                for k, v in val.items():
                    sql = f"INSERT OR REPLACE INTO {key} Values ('{k}', '{v}')"
                    dbcur.execute(sql)

                timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000Z")
                sql = f"INSERT OR REPLACE INTO service Values ('crew_last_sync', '{timestamp}')"
                dbcur.execute(sql)

        dbcon.commit()

    except Exception as e:
        c.log(f'Exception in create_trakt_tables(last_activities): Error = {e}')

@with_db_connection(return_on_error=None, return_as_dict=True)
def create_table(dbcon, dbcur, name='', query=''):
    """Create a table in the trakt sync database."""
    try:
        if not name and not query:
            c.log(f"Trying to create table in trakt::create_table without name or query. name = {name}, query = {query}, returning")
            return

        if table_exists(name):
            c.log(f"Table {name} already exists")
        elif f'sql_create_{name}' in sql_dict:
            sql = sql_dict[f'sql_create_{name}']
            # Ensure idempotent CREATE TABLE statements include IF NOT EXISTS to avoid race errors
            if isinstance(sql, str) and sql.lower().startswith('create table') and 'if not exists' not in sql.lower():
                sql = sql.replace('CREATE TABLE', 'CREATE TABLE IF NOT EXISTS', 1)
            dbcur.execute(sql)
            c.log(f"sql = {sql}")
        elif query:
            if query.lower().startswith('create table'):
                # Add IF NOT EXISTS to prevent OperationalError when table exists
                safe_query = query
                if 'if not exists' not in query.lower():
                    safe_query = query.replace('CREATE TABLE', 'CREATE TABLE IF NOT EXISTS', 1)
                dbcur.execute(safe_query)
            else:
                c.log(f"Trying to use invalid query in trakt::create_table, query = {query}, returning")
        #no return here, gracefully close the connection

        dbcon.commit()

    except OperationalError as e:
        # Some OperationalErrors are benign (e.g., 'table x already exists') when multiple
        # callers attempt to create the same table concurrently. Handle that case quietly.
        msg = str(e).lower()
        if 'already exists' in msg:
            c.log(f"Table {name} already exists, skipping")
        else:
            failure = traceback.format_exc()
            c.log(f'Traceback:: {failure}')

    except Exception as e:
        failure = traceback.format_exc()

@with_db_connection(return_on_error=False, return_as_dict=True)
def remove_from_local_collection(dbcon, dbcur, media_type, imdb='', tmdb=0, trakt_id=0):
    """Drop one title from the local collection mirror (movies_collection / shows_collection)."""
    table = f'{media_type}_collection'
    if media_type not in ('movies', 'shows') or not table_exists(table):
        return False

    conditions = []
    params = []
    if imdb:
        imdb_tt = imdb if str(imdb).startswith('tt') else f'tt{imdb}'
        imdb_raw = str(imdb).replace('tt', '')
        conditions.append('imdb IN (?, ?)')
        params.extend([imdb_tt, imdb_raw])
    if tmdb:
        conditions.append('tmdb = ?')
        params.append(int(tmdb))
    if trakt_id:
        conditions.append('trakt = ?')
        params.append(int(trakt_id))

    if not conditions:
        return False

    sql = f"DELETE FROM {table} WHERE {' OR '.join(conditions)}"
    dbcur.execute(sql, params)
    deleted = dbcur.rowcount
    dbcon.commit()
    if deleted:
        c.log(f'[Trakt] Removed {deleted} row(s) from local {table} (imdb={imdb}, tmdb={tmdb})')
    return deleted > 0


@with_db_connection(return_on_error=None, return_as_dict=True)
def get_trakt_collection(dbcon, dbcur, media_type="all") -> None:
    try:
        do_commit = False

        if media_type in ['movies', 'all']:
            if not table_exists('movies_collection'):
                sql = sql_dict['sql_create_movies_collection']
                dbcur.execute(sql)
                do_commit = True
            movie_collection = getTraktAsJson('/sync/collection/movies')
            if movie_collection is None:
                c.log('[Trakt] Movie collection fetch failed — keeping local mirror')
            else:
                dbcur.execute('DELETE FROM movies_collection')
                dbcon.commit()
                inserted, skipped = insert_collection(movie_collection, 'movies')
                c.log(
                    f'[Trakt] Movie collection mirror refreshed '
                    f'({inserted} inserted, {skipped} skipped, {len(movie_collection)} from API)'
                )

        if media_type in ['shows', 'all']:
            if not table_exists('shows_collection'):
                sql = sql_dict['sql_create_shows_collection']
                dbcur.execute(sql)
                do_commit= True

            if not table_exists('seasons_collection'):
                sql = sql_dict['sql_create_seasons_collection']
                dbcur.execute(sql)
                do_commit = True

            show_collection = getTraktAsJson('/sync/collection/shows')
            if show_collection is None:
                c.log('[Trakt] Show collection fetch failed — keeping local mirror')
            else:
                dbcur.execute('DELETE FROM shows_collection')
                dbcon.commit()
                inserted, skipped = insert_collection(show_collection, 'shows')
                c.log(
                    f'[Trakt] Show collection mirror refreshed '
                    f'({inserted} inserted, {skipped} skipped, {len(show_collection)} from API)'
                )

        if do_commit:
            dbcon.commit()
            if not control.player.isPlayingVideo():
                c.infoDialog('trakt collection updated', icon="main_orangehat.png", time=2000)

    except Exception as e:
        failure = traceback.format_exc()

@with_db_connection(return_on_error=None, return_as_dict=True)
def get_collection(dbcon, dbcur, media_type, trakt_id=0, imdb_id='', tmdb_id=0):
    """Retrieves collection entries from the trakt sync database for the specified media type and identifiers."""
    try:
        pass

        if media_type not in ['movies', 'shows']:
            return
        if not table_exists(f"{media_type}_collection"):
            create_table(f"{media_type}_collection", sql_dict[f"sql_create_{media_type}_collection"])
        query = []
        media_type = media_type.lower()


        if trakt_id == 0 and imdb_id == '' and tmdb_id == 0:
            sql = f"SELECT * FROM {media_type}_collection"
        else:
            if trakt_id != 0:
                query.append(f"trakt = {trakt_id}")
            if imdb_id != '':
                query.append(f"imdb = '{imdb_id}'")
            if tmdb_id != 0:
                query.append(f"tmdb = {tmdb_id}")
            sql = f"SELECT * FROM {media_type}_collection WHERE {' AND '.join(query)}"

        dbcur.execute(sql)
        rows = dbcur.fetchall()

        dbcon.commit()

        if len(rows) > 0:
            return rows
        else:
            return

    except OperationalError as e:
        c.log(f'Exception raised in get_collection. Error = {e}')
    except Exception as e:
        c.log(f'Exception raised in get_collection. Error = {e}')





@with_db_connection(return_on_error=None, return_as_dict=True)
def get_collection_orig(dbcon, dbcur, media_type, trakt=0, imdb='', tmdb=0):
    """Retrieves collection entries from the trakt sync database for the specified media type and identifiers."""
    try:
        sql = ''
        if media_type == 'movies':
            if trakt == 0 and imdb == '' and tmdb == 0:
                sql = "SELECT * FROM movies_collection"
            elif imdb == '' and tmdb == 0:
                sql = f"SELECT * FROM movies_collection WHERE trakt = {trakt}"
            elif trakt == 0 and tmdb == 0:
                sql = f"SELECT * FROM movies_collection WHERE imdb = '{imdb}'"
            elif trakt == 0 and imdb == '':
                sql = f"SELECT * FROM movies_collection WHERE tmdb = {tmdb}"
            elif trakt == 0:
                sql = f"SELECT * FROM movies_collection WHERE imdb = '{imdb}' AND tmdb = {tmdb}"
            elif imdb == '':
                sql = f"SELECT * FROM movies_collection WHERE trakt = {trakt} AND tmdb = {tmdb}"
            else:
                sql = f"SELECT * FROM movies_collection WHERE trakt = {trakt} OR imdb = '{imdb}' OR tmdb = {tmdb}"
        elif media_type == 'shows':
            sql = f"SELECT * FROM shows_collection WHERE trakt = '{trakt}' AND imdb = '{imdb}' AND tmdb = '{tmdb}'"

        dbcur.execute(sql)
        return dbcur.fetchall()
    except Exception as e:
        c.log(f'Exception raised in get_collection. Error = {e}')







def table_exists(table_name) -> bool:
    """Checks if a table exists in the trakt sync database."""
    try:
        dbcon = get_connection(control.traktsyncFile, return_as_dict=True)
        dbcur = get_connection_cursor(dbcon)

        sql = f"SELECT count(*) as aantal FROM sqlite_master WHERE type='table' AND name='{table_name}'"
        dbcur.execute(sql)
        row = dbcur.fetchone()
        result = row['aantal'] != 0

        dbcon.close()
        return result

    except Exception as e:
        c.log(f'Exception raised in tables_exists. Error = {e}')
        return False


def _str_field(value, default=''):
    if value is None:
        return default
    return str(value)


def _parse_collection_movie(item):
    """Return normalized movie row fields, or None if minimum identity is missing.

    Minimum identity: trakt id + title + year.
    """
    if not item or not isinstance(item, dict):
        return None
    movie = item.get('movie')
    if not isinstance(movie, dict):
        return None
    ids = movie.get('ids')
    if not isinstance(ids, dict):
        return None
    trakt = ids.get('trakt')
    if not trakt:
        return None
    title = movie.get('title')
    year = movie.get('year')
    if not title or year is None:
        return None
    title = str(title).strip()
    if not title:
        return None
    try:
        return {
            'last_collected_at': _str_field(item.get('collected_at')),
            'last_updated_at': _str_field(item.get('updated_at')),
            'title': title,
            'year': int(year),
            'trakt': int(trakt),
            'slug': _str_field(ids.get('slug')),
            'imdb': _str_field(ids.get('imdb')),
            'tmdb': int(ids.get('tmdb') or 0),
        }
    except (TypeError, ValueError):
        return None


def _parse_collection_show(item):
    """Return normalized show row + episode rows, or None if minimum identity is missing.

    Minimum identity: trakt id + title + year. Seasons/episodes are optional.
    """
    if not item or not isinstance(item, dict):
        return None
    show = item.get('show')
    if not isinstance(show, dict):
        return None
    ids = show.get('ids')
    if not isinstance(ids, dict):
        return None
    trakt = ids.get('trakt')
    if not trakt:
        return None
    title = show.get('title')
    year = show.get('year')
    if not title or year is None:
        return None
    title = str(title).strip()
    if not title:
        return None

    seasons = item.get('seasons')
    if seasons is None:
        seasons = []
    if not isinstance(seasons, list):
        return None

    episodes = []
    for season in seasons:
        if not isinstance(season, dict):
            continue
        season_num = season.get('number')
        if season_num is None:
            continue
        for ep in season.get('episodes') or []:
            if not isinstance(ep, dict):
                continue
            ep_num = ep.get('number')
            if ep_num is None:
                continue
            try:
                episodes.append({
                    'season': int(season_num),
                    'episode': int(ep_num),
                    'collected_at': _str_field(ep.get('collected_at')),
                })
            except (TypeError, ValueError):
                continue

    try:
        return {
            'last_collected_at': _str_field(item.get('last_collected_at')),
            'last_updated_at': _str_field(item.get('last_updated_at')),
            'title': title,
            'year': int(year),
            'trakt': int(trakt),
            'slug': _str_field(ids.get('slug')),
            'tvdb': int(ids.get('tvdb') or 0),
            'imdb': _str_field(ids.get('imdb')),
            'tmdb': int(ids.get('tmdb') or 0),
            'tvrage': _str_field(ids.get('tvrage')),
            'episodes': episodes,
        }
    except (TypeError, ValueError):
        return None


@with_db_connection(return_on_error=(0, 0), return_as_dict=True)
def insert_collection(dbcon, dbcur, collection, mediatype):
    inserted = 0
    skipped = 0
    try:
        table_name = ''

        if mediatype == 'movies':
            table_name = 'movies_collection'
        elif mediatype == 'shows':
            table_name = 'shows_collection'
        else:
            return inserted, skipped

        if not table_exists(table_name):
            sql = sql_dict[f'sql_create_{table_name}']
            dbcur.execute(sql)
            dbcon.commit()

        if not collection or not isinstance(collection, list):
            return inserted, skipped

        for item in collection:
            if mediatype == 'movies':
                row = _parse_collection_movie(item)
                if not row:
                    skipped += 1
                    continue
                dbcur.execute(
                    f'INSERT OR REPLACE INTO {table_name} VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    (
                        row['last_collected_at'],
                        row['last_updated_at'],
                        row['title'],
                        row['year'],
                        row['trakt'],
                        row['slug'],
                        row['imdb'],
                        row['tmdb'],
                    ),
                )
                dbcon.commit()
                inserted += 1

            elif mediatype == 'shows':
                row = _parse_collection_show(item)
                if not row:
                    skipped += 1
                    continue
                try:
                    dbcur.execute(
                        f'INSERT OR REPLACE INTO {table_name} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        (
                            row['last_collected_at'],
                            row['last_updated_at'],
                            row['title'],
                            row['year'],
                            row['trakt'],
                            row['slug'],
                            row['tvdb'],
                            row['imdb'],
                            row['tmdb'],
                            row['tvrage'],
                        ),
                    )

                    for ep in row['episodes']:
                        dbcur.execute(
                            'INSERT OR REPLACE INTO seasons_collection VALUES (?, ?, ?, ?, ?, ?, ?)',
                            (
                                row['trakt'],
                                row['tvdb'],
                                row['imdb'],
                                row['tmdb'],
                                ep['season'],
                                ep['episode'],
                                ep['collected_at'],
                            ),
                        )

                    dbcon.commit()
                    inserted += 1
                except Exception as exc:
                    skipped += 1
                    c.log(f'[Trakt] insert_collection shows row failed (trakt={row.get("trakt")}): {exc}')

        if skipped and c.devmode:
            c.log(
                f'[Trakt] insert_collection {mediatype}: inserted {inserted}, '
                f'skipped {skipped} malformed row(s) (API returned {len(collection)})'
            )

        return inserted, skipped

    except Exception as exc:
        c.log(f'[Trakt] insert_collection {mediatype} failed: {exc}')
        return inserted, skipped


@with_db_connection(return_on_error=[], return_as_dict=True)
def get_trakt_progress(dbcon, dbcur, media_type: str, trakt_id: int = 0) -> list:
    """Retrieves progress entries from the trakt sync database for the specified media type and ID."""
    try:
        sql = f"SELECT * FROM progress WHERE media_type = '{media_type}'"
        if trakt_id:
            sql += f" AND trakt_id = {trakt_id}"


        dbcur.execute(sql)
        rows = dbcur.fetchall()
        dbcon.commit()

        return rows

    except Exception as e:
        failure = traceback.format_exc()
        return []


@with_db_connection(return_on_error=None, return_as_dict=True)
def get_episode_progress(dbcon, dbcur, imdb: str, season: int, episode: int):
    """Get the progress percentage for a specific episode from the progress table."""
    try:
        # Use showimdb column since imdb parameter is the show's IMDB ID
        sql = "SELECT resume_point FROM progress WHERE media_type = 'episode' AND showimdb = ? AND season = ? AND episode = ?"
        dbcur.execute(sql, (imdb, season, episode))
        row = dbcur.fetchone()

        if row and row['resume_point'] is not None:
            progress = float(row['resume_point'])
            return progress
        return None
    except Exception as e:
        c.log(f"[Trakt] Error getting episode progress for {imdb} S{season}E{episode}: {e}\n{traceback.format_exc()}")
        return None






@with_db_connection(return_on_error=None, return_as_dict=True)
def update_trakt_sync_table(dbcon, dbcur, key, value):
    """Updates the value in the trakt sync table."""
    try:
        sql = "INSERT OR REPLACE INTO service Values (?, ?)"
        dbcur.execute(sql, (key, value))
        _commit(dbcon)
    except Exception as e:
        c.log(f"[Trakt] Error updating sync table {key}={value}: {e}")

@with_db_connection(return_on_error=None, return_as_dict=True)
def get_trakt_sync_value(dbcon, dbcur, key):
    """Returns the value from the trakt sync SERVICE table for the given key."""
    try:
        sql = "SELECT value FROM service WHERE setting = ?"
        result = dbcur.execute(sql, (key,))
        row = result.fetchone()
        # When using dict mode (return_as_dict=True), access by column name not index
        return row['value'] if row else None
    except Exception as e:
        c.log(f"[Trakt] Error getting sync value {key}: {e}")
        return None


@with_db_connection(return_on_error=None, return_as_dict=True)
def get_trakt_table_value(dbcon, dbcur, table_name, key):
    """Returns the value from a specific trakt sync table (movies, episodes, shows, seasons, lists)."""
    try:
        sql = f"SELECT value FROM {table_name} WHERE setting = ?"
        result = dbcur.execute(sql, (key,))
        row = result.fetchone()
        return row['value'] if row else None
    except Exception as e:
        c.log(f"[Trakt] Error getting table value {table_name}.{key}: {e}")
        return None


@with_db_connection(return_on_error=[], return_as_dict=True)
def get_watch_history(dbcon, dbcur, media_type=None, trakt_id=None, limit=None, offset=0):
    """Retrieve watch history from local database (no API calls)."""
    try:
        if not table_exists('trakt_history'):
            return []

        sql = "SELECT * FROM trakt_history"
        conditions = []
        params = []

        if media_type:
            conditions.append("media_type = ?")
            params.append(media_type)

        if trakt_id:
            conditions.append("trakt_id = ?")
            params.append(trakt_id)

        if conditions:
            sql += " WHERE " + " AND ".join(conditions)

        sql += " ORDER BY watched_at DESC"

        if limit:
            sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"

        result = dbcur.execute(sql, params)
        rows = result.fetchall()

        return rows

    except Exception as e:
        c.log(f"[Trakt] Error getting watch history: {e}")
        return []


@with_db_connection(return_on_error=0, return_as_dict=True)
def get_rewatch_count(dbcon, dbcur, trakt_id, media_type='movie'):
    """Gets the number of times a user has watched a specific item."""
    try:
        if not table_exists('trakt_history'):
            return 0

        sql = "SELECT COUNT(*) as count FROM trakt_history WHERE trakt_id = ? AND media_type = ?"
        result = dbcur.execute(sql, (trakt_id, media_type))
        row = result.fetchone()

        return row['count'] if row else 0

    except Exception as e:
        c.log(f"[Trakt] Error getting rewatch count: {e}")
        return 0


@with_db_connection(return_on_error=[], return_as_dict=True)
def get_recently_watched(dbcon, dbcur, days=7, media_type=None, limit=50, offset=0):
    """Gets recently watched items from local history (no API calls)."""
    try:
        if not table_exists('trakt_history'):
            return []

        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        sql = "SELECT * FROM trakt_history WHERE watched_at >= ?"
        params = [cutoff]

        if media_type:
            sql += " AND media_type = ?"
            params.append(media_type)

        sql += " ORDER BY watched_at DESC LIMIT ? OFFSET ?"
        params.append(limit)
        params.append(offset)

        result = dbcur.execute(sql, params)
        rows = result.fetchall()

        return rows

    except Exception as e:
        c.log(f"[Trakt] Error getting recently watched: {e}")
        return []

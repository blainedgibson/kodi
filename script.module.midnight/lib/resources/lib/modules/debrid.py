# -*- coding: utf-8 -*-

'''
********************************************************cm*
* Midnight Add-on
*
* @file debrid.py
* @package script.module.midnight
*
* @copyright (c) 2026, Midnight
* @license GNU General Public License, version 3 (GPL-3.0)
*
********************************************************cm*
'''

import re
import sys
import time
import json
import threading

import traceback
import requests

import xbmc
from . import control
from . import log_utils
from . import source_utils
from .crewruntime import c


# Constants for pack resolution
DEBRID_API_TIMEOUT = 10  # seconds
PACK_PROCESS_DELAY = 2   # seconds - wait for torrent processing
STATUS_CHECK_DELAY = 1   # seconds - wait before status check

# Module-level RD rate-limit backoff state
# When RD returns 429, we back off for RD_RATE_LIMIT_BACKOFF seconds before retrying
_rd_rate_limited_until = 0.0   # time.time() value; 0 = no active backoff
RD_RATE_LIMIT_BACKOFF = 3      # seconds to wait after confirmed 429 (measured: RD recovers in ~2.2s)
RD_RATE_LIMIT_RETRY_SLEEP = 3  # seconds to sleep before first retry on 429 (avoids second 429)
RD_451_BACKOFF = 3             # seconds to skip RD after 451 (avoids 451->429 cascade)

# Set to True the first time RD returns 451 (DMCA block) in this session.
# Used by errorForSources() to show a more informative failure message.
_rd_451_hit = False

# Services that returned 403 (invalid/expired credentials) this session.
# Keyed by normalised name (lowercase, hyphens, no dots).
# Checked by sources.py to skip all resolution attempts instead of hammering the API.
_debrid_auth_failed_services = set()
_debrid_unusable_services = set()
_ad_validated_apikey = None
_debrid_token_cache = {}

# TorBox: skip ResolveURL after timeout storm (Shield ~20s per attempt)
_torbox_resolve_blocked_until = 0.0
TORBOX_RESOLVE_BACKOFF = 120
SMART_RESOLVE_PROP = 'midnight.smart_resolve.active'


def is_torbox_resolve_blocked():
    """True after TorBox API timeout - skip remaining TB resolve attempts this pass."""
    return time.time() < _torbox_resolve_blocked_until


def mark_torbox_resolve_failed(reason='timeout'):
    global _torbox_resolve_blocked_until
    _torbox_resolve_blocked_until = time.time() + TORBOX_RESOLVE_BACKOFF
    c.log(f'[Debrid] TorBox resolve backoff {TORBOX_RESOLVE_BACKOFF}s ({reason})')


def _smart_resolve_active():
    try:
        return control.window.getProperty(SMART_RESOLVE_PROP) == 'true'
    except Exception:
        return False


def _extract_magnet_hash(url):
    if not url:
        return None
    match = re.search(r'btih:([a-fA-F0-9]{40})', url, re.I)
    return match.group(1).lower() if match else None


def _torbox_cached_only_setting():
    try:
        return control.addon('script.module.resolveurl').getSetting('TorBoxResolver_cached_only') == 'true'
    except Exception:
        return True


def _should_skip_torbox_resolve(url, debrid):
    """Lightweight cache check before ResolveURL (avoids 20s Shield timeouts on uncached)."""
    from . import debrid_names
    if debrid_names.normalize(debrid) != 'torbox':
        return False
    if is_torbox_resolve_blocked():
        c.log('[Debrid] TorBox skipped - session backoff after prior timeout/failure')
        return True
    if not _torbox_cached_only_setting():
        return False
    info_hash = _extract_magnet_hash(url)
    if not info_hash:
        return False
    try:
        from .debridcheck import torbox_hash_is_cached
        if torbox_hash_is_cached(info_hash, timeout=8):
            return False
        c.log(f'[Debrid] TorBox cached-only: {info_hash[:8]}… not cached - skipping ResolveURL')
        return True
    except Exception as e:
        c.log(f'[Debrid] TorBox cache pre-check failed: {e}', 1)
        return False


def is_debrid_auth_failed(service_name):
    """Return True if this service returned a 403 (invalid/expired token) this session."""
    if not service_name or not _debrid_auth_failed_services:
        return False
    normalized = str(service_name).lower().replace(' ', '-').replace('.', '')
    return normalized in _debrid_auth_failed_services


def is_debrid_unusable(service_name):
    """Return True when a debrid service is known broken for this Kodi session."""
    if not service_name or not _debrid_unusable_services:
        return False
    normalized = str(service_name).lower().replace(' ', '-').replace('.', '')
    return normalized in _debrid_unusable_services


def _mark_debrid_unusable(service_name, reason):
    """Mark a debrid service unusable for this session to avoid repeated bad retries."""
    if not service_name:
        return
    normalized = str(service_name).lower().replace(' ', '-').replace('.', '')
    if normalized in _debrid_unusable_services:
        return
    _debrid_unusable_services.add(normalized)
    c.log(f'[Debrid] Marking {service_name} unusable this session: {reason}', 1)


def clear_debrid_session_state(service_name=None):
    """Reset per-session debrid caches (e.g. after ResolveURL token save)."""
    global _ad_validated_apikey
    _ad_validated_apikey = None
    _debrid_token_cache.clear()
    if service_name:
        normalized = str(service_name).lower().replace(' ', '-').replace('.', '')
        _debrid_unusable_services.discard(normalized)
        _debrid_auth_failed_services.discard(normalized)
        try:
            control.window.clearProperty(f'midnight.debrid_auth_failed_notified.{normalized}')
        except Exception:
            pass
    else:
        _debrid_unusable_services.clear()
        _debrid_auth_failed_services.clear()


def _notify_debrid_auth_failed(service_name):
    """Show a once-per-session toast when a debrid service has invalid/expired credentials."""
    normalized = str(service_name).lower().replace(' ', '-').replace('.', '')
    prop = f'midnight.debrid_auth_failed_notified.{normalized}'

    def _log_reauth_block():
        c.log('[Debrid] ================================================', 1)
        c.log(f'[Debrid] REAUTHORIZE REQUIRED: {service_name}', 1)
        c.log(f'[Debrid] Please reauthorize your {service_name} account in ResolveURL settings.', 1)
        c.log('[Debrid] ================================================', 1)

    if not control.window.getProperty(prop):
        control.window.setProperty(prop, 'true')
        _log_reauth_block()
    else:
        c.log(f'[Debrid] {service_name} auth failure - reauthorize prompt already shown', 1)


def rd_451_was_hit():
    """Return True if Real-Debrid blocked at least one source (451) this session."""
    return _rd_451_hit


def rd_mark_451_backoff():
    """Record RD 451 and pause further RD attempts briefly (451 often triggers 429 next)."""
    global _rd_451_hit, _rd_rate_limited_until
    _rd_451_hit = True
    until = time.time() + RD_451_BACKOFF
    _rd_rate_limited_until = max(_rd_rate_limited_until, until)
    c.log(f'[RD] 451 backoff - skipping RD for {RD_451_BACKOFF}s while other services continue', 1)


def is_rd_in_cooldown():
    """Return True while RD should be skipped to avoid rate limits after 451/429."""
    return time.time() < _rd_rate_limited_until


def rd_cooldown_remaining():
    """Seconds remaining on the RD cooldown window."""
    return max(0.0, _rd_rate_limited_until - time.time())

try:
    import resolveurl
    debrid_resolvers = [resolver() for resolver in resolveurl.relevant_resolvers(order_matters=True) if resolver.isUniversal()]
except:
    debrid_resolvers = []


def get_debrid_resolvers():
    """Return current debrid resolvers, reflecting the *current* enabled state.

    The module-level ``debrid_resolvers`` is frozen at import time.  If the
    user enables or disables a service in ResolveURL after Kodi starts, that
    list is stale.  Call this function wherever the up-to-date resolver list
    is needed (e.g. sourcesFilter, resolver lookup).
    """
    try:
        import resolveurl as _ru
        return [r() for r in _ru.relevant_resolvers(order_matters=True) if r.isUniversal()]
    except Exception:
        return list(debrid_resolvers)


# Global lock to prevent multiple pack resolutions from running simultaneously
_pack_resolution_active = {}  # Dictionary to track active resolutions by hash

# Global progress dialog for resolution attempts (shared across sources)
_resolution_progress_dialog = {'dialog': None}

# Cooperative cancel - resolve runs synchronously on the plugin thread, so a background
# watcher polls dialog.iscanceled() while HTTP/sleep chunks check this flag.
_resolution_cancel_event = threading.Event()
_resolution_cancel_watcher_stop = threading.Event()
_resolution_cancel_watcher_thread = None
_CANCEL_POLL_MS = 250


def clear_resolution_cancel():
    """Reset cooperative cancel state (call when starting a new resolve pass)."""
    _resolution_cancel_event.clear()


# Manual source clicks (playItem) run as separate plugin invocations; only the latest
# click should resolve/play. Older attempts are cooperatively cancelled.
_play_item_generation = 0
_play_item_lock = threading.Lock()


def begin_play_item_attempt():
    """Mark a new manual source click; cancel any in-flight resolve from a prior click."""
    global _play_item_generation
    with _play_item_lock:
        _play_item_generation += 1
        generation = _play_item_generation
    request_resolution_cancel()
    clear_resolution_cancel()
    return generation


def is_play_item_superseded(generation):
    """Return True when a newer playItem click replaced this attempt."""
    return generation != _play_item_generation


def request_resolution_cancel():
    """Signal that the user cancelled resolution."""
    _resolution_cancel_event.set()


def is_resolution_cancel_requested():
    """Return True if cancel was requested (watcher or explicit)."""
    return _resolution_cancel_event.is_set()


def _shared_dialog_cancel_pressed():
    """Read cancel state from the shared progress dialog without side effects."""
    dialog = _resolution_progress_dialog.get('dialog')
    if dialog is None:
        return False
    try:
        return bool(dialog.iscanceled())
    except Exception:
        return False


def _resolution_cancel_watcher():
    """Daemon thread: poll progress dialog while main thread is inside resolve."""
    while not _resolution_cancel_watcher_stop.is_set():
        try:
            if _shared_dialog_cancel_pressed():
                request_resolution_cancel()
                c.log('[Debrid] Cancel watcher: user pressed cancel on resolve dialog')
                return
        except Exception:
            pass
        xbmc.sleep(_CANCEL_POLL_MS)


def start_resolution_cancel_watcher():
    """Start polling the shared resolve dialog for cancel presses."""
    global _resolution_cancel_watcher_thread
    stop_resolution_cancel_watcher()
    clear_resolution_cancel()
    _resolution_cancel_watcher_stop.clear()
    _resolution_cancel_watcher_thread = threading.Thread(
        target=_resolution_cancel_watcher,
        name='ResolutionCancelWatcher',
        daemon=True,
    )
    _resolution_cancel_watcher_thread.start()


def stop_resolution_cancel_watcher():
    """Stop the cancel watcher thread."""
    global _resolution_cancel_watcher_thread
    _resolution_cancel_watcher_stop.set()
    if _resolution_cancel_watcher_thread and _resolution_cancel_watcher_thread.is_alive():
        _resolution_cancel_watcher_thread.join(timeout=1.0)
    _resolution_cancel_watcher_thread = None


def _interruptible_sleep(seconds):
    """
    Sleep in short chunks so cancel can land between blocking RD API calls.
    Returns False if cancelled during the wait.
    """
    if seconds <= 0:
        return not is_resolution_cancel_requested()
    deadline = time.time() + seconds
    while time.time() < deadline:
        if is_resolution_cancel_requested():
            return False
        remaining_ms = int(max(1, (deadline - time.time()) * 1000))
        xbmc.sleep(min(_CANCEL_POLL_MS, remaining_ms))
    return not is_resolution_cancel_requested()


def _abort_resolution_if_cancelled(step):
    """Return True when resolve should stop because the user cancelled."""
    if is_resolution_cancel_requested():
        c.log(f'[Debrid] Resolution cancelled by user ({step})')
        return True
    return False


def create_shared_resolution_dialog(message='Resolving sources...'):
    """Create the shared resolution progress dialog."""
    if _resolution_progress_dialog['dialog'] is None:
        try:
            c.log('[Debrid] Creating shared resolution progress dialog')
            _resolution_progress_dialog['dialog'] = control.progressDialog if control.setting('progress.dialog') == '0' else control.progressDialogBG
            _resolution_progress_dialog['dialog'].create(control.addonInfo('name'), message)
            _resolution_progress_dialog['dialog'].update(0)
        except Exception as e:
            c.log(f'[Debrid] Error creating shared dialog: {e}', 1)
    return _resolution_progress_dialog['dialog']


def update_shared_resolution_dialog(message, percent=0):
    """Update the shared resolution progress dialog if it exists."""
    if _resolution_progress_dialog['dialog'] is not None:
        try:
            _resolution_progress_dialog['dialog'].update(percent, message)
        except Exception as e:
            c.log(f'[Debrid] Error updating shared dialog: {e}', 1)


def close_shared_resolution_dialog():
    """Close the shared resolution progress dialog if it exists."""
    stop_resolution_cancel_watcher()
    if _resolution_progress_dialog['dialog'] is not None:
        try:
            c.log('[Debrid] Closing shared resolution progress dialog')
            _resolution_progress_dialog['dialog'].close()
        except Exception as e:
            c.log(f'[Debrid] Error closing shared dialog: {e}', 1)
        finally:
            _resolution_progress_dialog['dialog'] = None


def is_shared_resolution_dialog_canceled():
    """Return True if the user cancelled the shared resolution progress dialog."""
    if is_resolution_cancel_requested():
        return True
    return _shared_dialog_cancel_pressed()


class _SilentProgressDialog(object):
    """
    Suppress ResolveURL-owned progress dialogs during Midnight resolve.

    TorBox (and others): Midnight already shows ResolveStatus / shared progress.
    is_canceled() mirrors Midnight cancel so ResolveURL exits its download loop
    instead of hanging until timeout.
    """

    def __init__(self, heading, line1='', line2='', line3='', background=False, active=True, timer=0):
        self.heading = heading

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def is_canceled(self):
        return is_resolution_cancel_requested()

    def iscanceled(self):
        return self.is_canceled()

    def update(self, percent, line1='', line2='', line3=''):
        return None

    def close(self):
        return None


class _CrewAwareProgressDialog(object):
    """
    Wrap ResolveURL ProgressDialog so Cancel sets Midnight's cancel flag.

    Without this, cancelling ResolveURL's RD download dialog does not stop
    Smart Resolve from trying the next sources — each one asks
    "Keep transferring … in the background?" again (triple yesno).
    """

    def __init__(self, original_cls, *args, **kwargs):
        self._inner = original_cls(*args, **kwargs)

    def __enter__(self):
        if hasattr(self._inner, '__enter__'):
            self._inner.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if hasattr(self._inner, '__exit__'):
            return self._inner.__exit__(exc_type, exc_val, exc_tb)
        return False

    def is_canceled(self):
        canceled = False
        try:
            canceled = bool(self._inner.is_canceled())
        except Exception:
            canceled = False
        if canceled:
            request_resolution_cancel()
        return canceled or is_resolution_cancel_requested()

    def iscanceled(self):
        return self.is_canceled()

    def update(self, *args, **kwargs):
        return self._inner.update(*args, **kwargs)

    def close(self):
        try:
            return self._inner.close()
        except Exception:
            return None


def _suppress_keep_transfer_yesno(*args, **kwargs):
    """
    ResolveURL asks 'Keep transferring to … Cloud in the background?' on cancel.
    Cancel already means abort — answer no without prompting, and arm Midnight cancel
    so Smart Resolve stops trying further links.
    """
    request_resolution_cancel()
    c.log('[Debrid] Suppressing ResolveURL keep-transfer prompt (cancel = abort)')
    return False


def _patch_resolver_progress_dialog(debrid_resolver, silent=False):
    """
    Patch ResolveURL kodi ProgressDialog (+ yesno) for the duration of get_media_url.

    silent=True  → no ResolveURL progress UI (TorBox; Midnight overlay owns UX)
    silent=False → wrap ProgressDialog so cancel propagates to Midnight; suppress
                   the keep-transfer yesno so it is not asked 1–N times
    """
    try:
        module = sys.modules.get(debrid_resolver.__class__.__module__)
        common = getattr(module, 'common', None)
        kodi = getattr(common, 'kodi', None)
        original_pd = getattr(kodi, 'ProgressDialog', None)
        original_yesno = getattr(kodi, 'yesnoDialog', None)
        if kodi is None or original_pd is None:
            return None

        if silent:
            kodi.ProgressDialog = _SilentProgressDialog
        else:
            _orig = original_pd

            def _factory(*args, **kwargs):
                return _CrewAwareProgressDialog(_orig, *args, **kwargs)

            kodi.ProgressDialog = _factory

        if original_yesno is not None:
            kodi.yesnoDialog = _suppress_keep_transfer_yesno

        return kodi, original_pd, original_yesno
    except Exception as e:
        c.log(f'[Debrid] Failed to patch resolver progress dialog: {e}', 1)
        return None


def _restore_resolver_progress_dialog(patch_state):
    """Restore ResolveURL ProgressDialog / yesnoDialog after resolve."""
    if not patch_state:
        return
    try:
        if len(patch_state) == 2:
            # Legacy (kodi, original_pd) from older TorBox-only patch
            kodi, original_pd = patch_state
            kodi.ProgressDialog = original_pd
            return
        kodi, original_pd, original_yesno = patch_state
        kodi.ProgressDialog = original_pd
        if original_yesno is not None:
            kodi.yesnoDialog = original_yesno
    except Exception as e:
        c.log(f'[Debrid] Failed to restore resolver progress dialog: {e}', 1)


def status(torrent=False):
    # Read settings fresh so a service disabled mid-session takes effect
    # without a Kodi restart.  Falls back to the import-time list on error.
    try:
        r_url = control.addon('script.module.resolveurl')
        debrid_pairs = [
            ('RealDebridResolver_enabled', 'RealDebridResolver_token'),
            ('AllDebridResolver_enabled', 'AllDebridResolver_token'),
            ('PremiumizeMeResolver_enabled', 'PremiumizeMeResolver_token'),
            ('TorBoxResolver_enabled', 'TorBoxResolver_apikey'),
        ]
        debrid_check = any(
            r_url.getSetting(ek) == 'true' and r_url.getSetting(tk)
            for ek, tk in debrid_pairs
        )
    except Exception:
        debrid_check = debrid_resolvers != []
    if debrid_check:
        if torrent:
            enabled = control.setting('torrent.enabled')
            if enabled == '' or enabled.lower() == 'true':
                return True
            else:
                return False
    return debrid_check


def _notify_debrid_unreachable(service_name: str) -> None:
    """Show a once-per-session toast when a debrid service can't be reached."""
    prop = 'midnight.debrid_unreachable_notified'
    if not control.window.getProperty(prop):
        control.window.setProperty(prop, 'true')
        c.log(f'[Debrid] {service_name} unreachable - notifying user', 1)
        c.infoDialog(f'{service_name} could not be reached. Check your internet connection.')
    else:
        c.log(f'[Debrid] {service_name} unreachable (notification already shown)', 1)


def get_debrid_token(service_name, midnight_setting_name):
    """Get debrid token from resolveurl first, then fall back to Midnight settings.

    Cached for the Kodi session (window property + module dict) so pack loops
    do not re-read ResolveURL settings on every source attempt.
    """
    prop = f'midnight.debrid_token.{service_name}'
    cached = control.window.getProperty(prop) or _debrid_token_cache.get(service_name)
    if cached:
        source = (
            control.window.getProperty(f'{prop}.source')
            or _debrid_token_cache.get(f'{service_name}.source')
            or 'session-cache'
        )
        return cached, source

    token = None
    source = None

    # Try resolveurl first
    try:
        resolveurl_addon = control.addon('script.module.resolveurl')
        token = resolveurl_addon.getSetting(service_name)
        if token:
            source = 'resolveurl'
            c.log(f'[Debrid Token] Got {service_name} token from resolveurl')
    except Exception as e:
        c.log(f'[Debrid Token] Could not get {service_name} token from resolveurl: {e}')

    # Fall back to Midnight settings
    if not token:
        token = control.setting(midnight_setting_name)
        if token:
            source = 'midnight'
            c.log(f'[Debrid Token] Got {service_name} token from Midnight settings')

    if token:
        _debrid_token_cache[service_name] = token
        _debrid_token_cache[f'{service_name}.source'] = source or 'unknown'
        control.window.setProperty(prop, token)
        control.window.setProperty(f'{prop}.source', source or 'unknown')

    return token, source


def _mask_debrid_token(token):
    """Return a safe log fragment for an API key (never log the full value)."""
    token = str(token or '').strip()
    if not token:
        return '(empty)'
    if len(token) <= 8:
        return f'len={len(token)}'
    return f'len={len(token)} {token[:4]}...{token[-4:]}'


def get_alldebrid_apikey():
    """Get AllDebrid API key from ResolveURL (primary) with safe fallbacks."""
    global _ad_validated_apikey

    if _ad_validated_apikey:
        return _ad_validated_apikey, 'session-cache'

    def _validate_apikey(candidate, candidate_source):
        """Return True only when the candidate key is accepted by AD API."""
        try:
            resp = requests.get(
                'https://api.alldebrid.com/v4/user',
                params={'agent': 'midnight', 'apikey': candidate},
                timeout=6
            )
            if resp.status_code != 200:
                c.log(
                    f'[Debrid Token] AD rejected {candidate_source} ({_mask_debrid_token(candidate)}): HTTP {resp.status_code}',
                    1,
                )
                return False
            data = resp.json()
            if data.get('status') == 'success':
                return True
            err = data.get('error') or {}
            c.log(
                f'[Debrid Token] AD rejected {candidate_source} ({_mask_debrid_token(candidate)}): '
                f'{err.get("code", "unknown")} {err.get("message", "")}'.strip(),
                1,
            )
            return False
        except Exception as e:
            c.log(
                f'[Debrid Token] AD validation error for {candidate_source} ({_mask_debrid_token(candidate)}): {e}',
                1,
            )
            return False

    def _add_candidate(candidates, value, source):
        if not value:
            return
        token = str(value).strip().strip('"').strip("'")
        if not token:
            return
        if token.lower().startswith('bearer '):
            token = token.split(' ', 1)[1].strip()
        if token.startswith('{') and token.endswith('}'):
            try:
                data = json.loads(token)
                if isinstance(data, dict):
                    token = str(data.get('apikey') or data.get('token') or '').strip()
            except Exception:
                token = ''
        if not token:
            return
        if all(existing[0] != token for existing in candidates):
            candidates.append((token, source))

    token = None
    source = None
    candidates = []

    # 1) Preferred: read from ResolveURL AllDebrid resolver API
    try:
        from resolveurl.plugins.alldebrid import AllDebridResolver
        _add_candidate(candidates, AllDebridResolver().get_setting('token'), 'resolveurl.resolver')
    except Exception as e:
        c.log(f'[Debrid Token] Could not read AD token from ResolveURL resolver: {e}')

    # 2) Fallback: direct ResolveURL addon setting ids
    try:
        r_addon = control.addon('script.module.resolveurl')
        for setting_id in ('AllDebridResolver_token', 'AllDebridResolver_apikey'):
            _add_candidate(candidates, r_addon.getSetting(setting_id), f'script.module.resolveurl.{setting_id}')
    except Exception as e:
        c.log(f'[Debrid Token] Could not read AD token from script.module.resolveurl: {e}')

    # 3) Legacy Midnight setting only when ResolveURL has no token at all
    if not candidates:
        _add_candidate(candidates, control.setting('alldebridtoken'), 'midnight')

    if not candidates:
        c.log('[Debrid Token] No AllDebrid token candidates found (ResolveURL + Midnight empty)', 1)
    else:
        for candidate, candidate_source in candidates:
            c.log(f'[Debrid Token] Candidate from {candidate_source}: {_mask_debrid_token(candidate)}', 1)

    # Validate candidates against AD API and pick first working key.
    for candidate, candidate_source in candidates:
        if _validate_apikey(candidate, candidate_source):
            _ad_validated_apikey = candidate
            _debrid_unusable_services.discard('alldebrid')
            c.log(f'[Debrid Token] Validated AllDebrid token from {candidate_source} ({_mask_debrid_token(candidate)})')
            return candidate, candidate_source

    c.log('[Debrid Token] No valid AllDebrid token found after validation checks', 1)
    return token, source


def _normalize_resolver_name(name):
    """Normalize resolver/service names for robust matching (e.g. Torbox vs TorBox)."""
    return re.sub(r'[^a-z0-9]+', '', str(name or '').lower())


def resolver(url, debrid, season=None, episode=None):
    # Import at function level to avoid namespace issues


    try:
        if _abort_resolution_if_cancelled('resolver entry'):
            return None

        c.log('[Debrid] ========== RESOLVER CALLED ==========')
        c.log(f'[Debrid] resolver() called: url={url[:60] if url else None}..., debrid={debrid}, season={season}, episode={episode}')

        # Check if this is a magnet link with season/episode (pack source)
        if url.startswith('magnet:') and season is not None and episode is not None:
            c.log(f'[Debrid] Pack source detected: magnet link with S{int(season):02d}E{int(episode):02d}')
            c.log('[Debrid] Starting pack resolution')

            progressDialog = None
            close_dialog_after = False
            smart_active = _smart_resolve_active()

            if not smart_active:
                # Check if there's an active persistent overlay (TV Evening/Up Next)
                try:
                    from .sources import get_active_overlay
                    active_overlay = get_active_overlay()
                    c.log(f'[Debrid] Overlay check: active_overlay={active_overlay}, type={type(active_overlay)}')

                    if active_overlay and hasattr(active_overlay, 'transition_to_state'):
                        # Use existing overlay - just update message
                        c.log('[Debrid] Using active overlay for resolution progress')
                        active_overlay.transition_to_state('resolving', 'Generating playable link...')
                        progressDialog = active_overlay
                    else:
                        # Check if we already have a global resolution dialog active
                        if _resolution_progress_dialog['dialog'] is not None:
                            c.log('[Debrid] Reusing existing resolution progress dialog')
                            progressDialog = _resolution_progress_dialog['dialog']
                            try:
                                progressDialog.update(0, f'Trying {debrid} for S{int(season):02d}E{int(episode):02d}...')
                            except Exception:
                                pass
                        else:
                            if active_overlay:
                                c.log('[Debrid] Overlay exists but missing transition_to_state method')
                            else:
                                c.log('[Debrid] No active overlay detected')
                            c.log('[Debrid] Creating shared progress dialog')
                            progressDialog = control.progressDialog if control.setting('progress.dialog') == '0' else control.progressDialogBG
                            progressDialog.create(control.addonInfo('name'), 'Resolving...')
                            progressDialog.update(0, f'Trying {debrid} for S{int(season):02d}E{int(episode):02d}...')
                            _resolution_progress_dialog['dialog'] = progressDialog
                except Exception as e:
                    c.log(f'[Debrid] Error checking for active overlay: {e}', 1)
                    c.log(f'[Debrid] Traceback: {traceback.format_exc()}')
                    if _resolution_progress_dialog['dialog'] is not None:
                        progressDialog = _resolution_progress_dialog['dialog']
                    else:
                        progressDialog = control.progressDialog if control.setting('progress.dialog') == '0' else control.progressDialogBG
                        progressDialog.create(control.addonInfo('name'), f'Resolving pack for S{int(season):02d}E{int(episode):02d}...')
                        progressDialog.update(0)
                        _resolution_progress_dialog['dialog'] = progressDialog
            else:
                c.log('[Debrid] Smart resolve active - using ResolveStatus overlay (no legacy progress dialog)')

            # Try direct pack resolution for supported debrid services
            # Normalize debrid service name (case-insensitive matching)
            debrid_normalized = debrid.lower().replace(' ', '-')
            if debrid_normalized in ('real-debrid', 'alldebrid', 'premiumize.me'):
                # Map to canonical name for resolve_pack
                canonical_name_map = {
                    'real-debrid': 'Real-Debrid',
                    'alldebrid': 'AllDebrid',
                    'premiumize.me': 'Premiumize.me'
                }
                canonical_name = canonical_name_map.get(debrid_normalized, debrid)
                c.log(f'[Debrid] Normalized "{debrid}" -> "{canonical_name}" for pack resolution')
                pack_url = resolve_pack(url, canonical_name, season, episode, progressDialog)

                # Close dialog only if we created it (not if using overlay)
                if close_dialog_after:
                    try:
                        if progressDialog:
                            progressDialog.close()
                    except Exception:
                        pass

                if pack_url:
                    c.log('[Debrid] Pack resolution successful: Selected correct episode file')
                    c.log('[Debrid] ========== RESOLVER RETURNING (PACK SUCCESS) ==========')
                    return pack_url
                else:
                    # Pack resolution failed - do NOT fall back to resolveurl for season/episode
                    # requests. resolveurl has no episode awareness and will play whatever file
                    # RD last had selected for this torrent (likely the wrong episode).
                    # Returning None lets the sources loop move on to the next source.
                    c.log('[Debrid] Pack resolution failed - skipping resolveurl fallback to avoid wrong episode', 1)
                    c.log('[Debrid] ========== RESOLVER RETURNING (PACK FAILED - NO FALLBACK) ==========')
                    return None

        if _should_skip_torbox_resolve(url, debrid):
            c.log('[Debrid] ========== RESOLVER RETURNING (TORBOX SKIPPED) ==========')
            return None

        c.log(f'[Debrid] Calling resolveurl standard resolver for {debrid}')

        # Look up the resolver in the import-time list first (fast path).
        # If not found there (service was disabled at boot and later enabled),
        # fall back to a fresh resolver list that reflects current settings.
        target_name = _normalize_resolver_name(debrid)
        matching = [r for r in debrid_resolvers if _normalize_resolver_name(getattr(r, 'name', '')) == target_name]
        if not matching:
            c.log(f'[Debrid] {debrid} not in cached resolver list - trying fresh lookup', 1)
            matching = [r for r in get_debrid_resolvers() if _normalize_resolver_name(getattr(r, 'name', '')) == target_name]
        if not matching:
            c.log(f'[Debrid] No resolver found for {debrid} - service may be disabled', 1)
            return None
        debrid_resolver = matching[0]

        debrid_resolver.login()
        _host, _media_id = debrid_resolver.get_host_and_id(url)

        progress_patch = None
        # Always patch ResolveURL UI during Midnight resolve:
        # - TorBox: silent (Midnight overlay owns progress)
        # - Others: wrap cancel + suppress keep-transfer yesno (was asked once per
        #   remaining Smart Resolve link — cancel felt like 3× "are you sure")
        silent = target_name == 'torbox'
        c.log(f'[Debrid] Patching ResolveURL progress/yesno for {debrid} (silent={silent})')
        progress_patch = _patch_resolver_progress_dialog(debrid_resolver, silent=silent)

        # Log season/episode info for debugging pack file selection
        if season is not None and episode is not None:
            c.log(f'[Debrid] Resolving with season={season}, episode={episode} for pack file selection')

        # Try to pass season/episode to get_media_url if it supports it
        try:
            # Some resolvers support season/episode parameters for pack file selection
            stream_url = debrid_resolver.get_media_url(_host, _media_id, season=season, episode=episode)
        except TypeError:
            # Fallback if resolver doesn't support season/episode parameters
            stream_url = debrid_resolver.get_media_url(_host, _media_id)
            if season is not None and episode is not None:
                c.log('[Debrid] Note: Resolver does not support season/episode parameters')
        finally:
            if progress_patch is not None:
                c.log('[Debrid] Restoring ResolveURL progress/yesno')
                _restore_resolver_progress_dialog(progress_patch)

        c.log('[Debrid] ========== RESOLVER RETURNING (RESOLVEURL SUCCESS) ==========')
        return stream_url
    except Exception as e:
        if isinstance(e, requests.RequestException):
            _notify_debrid_unreachable(debrid)
        c.log(f'{debrid} Resolve Failure: {e}', 1)
        err_text = str(e).lower()
        if '451' in err_text and 'real' in str(debrid).lower():
            rd_mark_451_backoff()
        # Known AllDebrid resolver bug path observed in the field:
        # "NoneType object is not iterable" repeats on every AD source.
        # Mark AD unusable for this session to prevent dozens of noisy retries.
        if str(debrid).lower().replace(' ', '-') in ('alldebrid', 'all-debrid'):
            err_text = str(e).lower()
            if 'nonetype' in err_text and 'iterable' in err_text:
                _mark_debrid_unusable(debrid, "resolver returned NoneType iterable failure")
        # 403 = invalid/expired credentials - flag this service so sources.py can
        # skip all further resolution attempts immediately (avoids 429 cascade).
        if '(403)' in str(e):
            normalized = str(debrid).lower().replace(' ', '-').replace('.', '')
            _debrid_auth_failed_services.add(normalized)
            _notify_debrid_auth_failed(debrid)
        if _normalize_resolver_name(debrid) == 'torbox':
            err_low = str(e).lower()
            if 'timed out' in err_low or 'timeout' in err_low:
                mark_torbox_resolve_failed('read timeout')
            elif 'only cached' in err_low or 'cached torrents only' in err_low:
                pass
        c.log('[Debrid] ========== RESOLVER RETURNING (EXCEPTION) ==========')
        return None


def resolve_pack(magnet_url, debrid_service, season, episode, progressDialog=None):
    """
    Resolve pack torrent by selecting correct episode file from magnet link.

    Args:
        magnet_url: Magnet link
        debrid_service: Debrid service name ('Real-Debrid', 'AllDebrid', 'Premiumize.me')
        season: Season number
        episode: Episode number
        progressDialog: Optional progress dialog to update during resolution

    Returns:
        str: Direct download URL for correct episode file, or None if failed
    """
    # Extract hash from magnet to use as lock key
    hash_match = re.search(r'btih:([a-fA-F0-9]+)', magnet_url)
    if hash_match:
        torrent_hash = hash_match.group(1).lower()

        # Check if this torrent is already being resolved
        if torrent_hash in _pack_resolution_active:
            c.log('[Pack] Another resolution is already in progress for this torrent, skipping')
            return None

        # Mark this torrent as being resolved
        _pack_resolution_active[torrent_hash] = True

    try:
        c.log(f'[Pack] Resolving with {debrid_service} for S{season:02d}E{episode:02d}')

        result = None
        if debrid_service == 'Real-Debrid':
            c.log('[Pack] Dispatching to resolve_pack_realdebrid()')
            result = resolve_pack_realdebrid(magnet_url, season, episode, progressDialog)
        elif debrid_service == 'AllDebrid':
            c.log('[Pack] Dispatching to resolve_pack_alldebrid()')
            result = resolve_pack_alldebrid(magnet_url, season, episode, progressDialog)
            c.log(f'[Pack] resolve_pack_alldebrid() returned: {result}')
        elif debrid_service == 'Premiumize.me':
            c.log('[Pack] Dispatching to resolve_pack_premiumize()')
            result = resolve_pack_premiumize(magnet_url, season, episode, progressDialog)
        else:
            c.log(f'[Pack] Resolution not implemented for {debrid_service}')

        return result
    except Exception as e:
        c.log(f'[Pack] Resolution error: {e}', 1)
        c.log(f'[Pack] Traceback: {traceback.format_exc()}', 1)
        return None
    finally:
        # Always clear the lock when done
        if hash_match and torrent_hash in _pack_resolution_active:
            del _pack_resolution_active[torrent_hash]
            c.log('[Pack] Released resolution lock')


def _rd_check_429(response, label):
    """Return True if response is a 429 rate-limit. Logs a clear message so it's easy to spot in logs."""
    if response is not None and response.status_code == 429:
        c.log(f'[RD] Rate limited (429) on {label} - backing off, no retry', 1)
        return True
    return False


def _rd_refresh_access_token():
    """
    Refresh the Real-Debrid access token using the stored refresh token and client credentials.
    Writes the new access token back into resolveurl settings so future calls are also fixed.
    Returns new access token string on success, None on failure.
    """
    try:
        resolveurl_addon = control.addon('script.module.resolveurl')
        refresh_token = resolveurl_addon.getSetting('RealDebridResolver_refresh')
        client_id = resolveurl_addon.getSetting('RealDebridResolver_client_id')
        client_secret = resolveurl_addon.getSetting('RealDebridResolver_client_secret')

        if not refresh_token:
            c.log('[RD Token Refresh] No refresh token found in resolveurl - cannot refresh')
            return None

        c.log('[RD Token Refresh] Attempting to refresh access token...')
        token_url = 'https://api.real-debrid.com/oauth/v2/token'
        data = {
            'client_id': client_id or 'X245A4XAIBGVM',
            'client_secret': client_secret or '',
            'code': refresh_token,
            'grant_type': 'http://oauth.net/grant_type/device/1.0',
        }
        response = requests.post(token_url, data=data, timeout=15)
        if response.status_code == 200:
            token_data = response.json()
            new_token = token_data.get('access_token')
            if new_token:
                # Persist back so resolveurl and future calls all use the fresh token
                try:
                    resolveurl_addon.setSetting('RealDebridResolver_token', new_token)
                    c.log('[RD Token Refresh] New access token stored in resolveurl settings')
                except Exception as _se:
                    c.log(f'[RD Token Refresh] Could not persist token to resolveurl: {_se}')
                c.log('[RD Token Refresh] Access token refreshed successfully')
                return new_token
            c.log('[RD Token Refresh] No access_token field in response')
        else:
            c.log(f'[RD Token Refresh] Refresh request failed: {response.status_code}')
    except Exception as _e:
        c.log(f'[RD Token Refresh] Exception: {_e}')
    return None


def resolve_pack_realdebrid(magnet_url, season, episode, progressDialog=None, _reselect=False):
    """Resolve pack with Real-Debrid API.

    _reselect: internal - after deleting a wrongly-selected torrent, retry once.
    """
    c.log(f'[RD Pack] Starting resolution for S{season:02d}E{episode:02d}')
    try:
        if _abort_resolution_if_cancelled('RD pack start'):
            return None

        # Get RD API token from resolveurl or settings
        if progressDialog:
            try:
                progressDialog.update(10, 'Getting Real-Debrid token...')
            except Exception:
                pass
        rd_token, token_source = get_debrid_token('RealDebridResolver_token', 'realdebridtoken')
        if not rd_token:
            c.log('[RD Pack] No Real-Debrid token found in resolveurl or Midnight settings', 1)
            return None
        c.log(f'[RD Pack] Token found from {token_source}')

        base_url = 'https://api.real-debrid.com/rest/1.0'
        headers = {'Authorization': f'Bearer {rd_token}'}

        # Note: RD's instantAvailability endpoint no longer reliably returns cached status,
        # so we skip the pre-check entirely and go straight to adding the magnet.

        # Add magnet to RD
        c.log('[RD Pack] Adding magnet to Real-Debrid')
        add_url = f'{base_url}/torrents/addMagnet'
        add_data = {'magnet': magnet_url}

        # Honour any active rate-limit backoff from a previous 429
        global _rd_rate_limited_until
        if time.time() < _rd_rate_limited_until:
            remaining = _rd_rate_limited_until - time.time()
            c.log(f'[RD Pack] Rate-limit backoff active - waiting {remaining:.1f}s before retrying', 1)
            if not _interruptible_sleep(remaining):
                return None

        if _abort_resolution_if_cancelled('before addMagnet'):
            return None

        add_response = requests.post(add_url, data=add_data, headers=headers, timeout=DEBRID_API_TIMEOUT)

        if add_response.status_code == 451:
            c.log('[RD Pack] Hash blocked (451 - DMCA/legal) - entering RD cooldown', 1)
            rd_mark_451_backoff()
            return None
        if add_response.status_code == 429:
            c.log(f'[RD Pack] 429 headers: {dict(add_response.headers)}', 1)
            retry_after = add_response.headers.get('Retry-After') or add_response.headers.get('X-RateLimit-Reset') or add_response.headers.get('X-Rate-Limit-Reset')
            c.log(f'[RD Pack] Retry-After/X-RateLimit-Reset value: {retry_after}', 1)
            c.log(f'[RD Pack] Rate limited by Real-Debrid (429) - sleeping {RD_RATE_LIMIT_RETRY_SLEEP}s then retrying once', 1)
            if not _interruptible_sleep(RD_RATE_LIMIT_RETRY_SLEEP):
                return None
            if _abort_resolution_if_cancelled('before addMagnet retry'):
                return None
            add_response = requests.post(add_url, data=add_data, headers=headers, timeout=DEBRID_API_TIMEOUT)
            if add_response.status_code == 429:
                c.log(f'[RD Pack] Second 429 headers: {dict(add_response.headers)}', 1)
                _rd_rate_limited_until = time.time() + RD_RATE_LIMIT_BACKOFF
                c.log(f'[RD Pack] Still rate limited after retry - backing off for {RD_RATE_LIMIT_BACKOFF}s', 1)
                return None
        if add_response.status_code == 401:
            c.log('[RD Pack] Add magnet returned 401 (token expired) - attempting token refresh and retry')
            new_token = _rd_refresh_access_token()
            if new_token:
                rd_token = new_token
                headers = {'Authorization': f'Bearer {rd_token}'}
                add_response = requests.post(add_url, data=add_data, headers=headers, timeout=DEBRID_API_TIMEOUT)
                c.log(f'[RD Pack] Retry after token refresh: {add_response.status_code}')
            else:
                c.log('[RD Pack] Token refresh failed - cannot add magnet', 1)
                return None
        if add_response.status_code not in (201, 202):
            c.log(f'[RD Pack] Failed to add magnet: {add_response.status_code}', 1)
            return None
        if add_response.status_code == 202:
            c.log('[RD Pack] Torrent already exists in Real-Debrid account (202)')

        torrent_id = add_response.json().get('id')
        if not torrent_id:
            c.log('[RD Pack] No torrent ID returned', 1)
            return None

        # Wait for torrent to be processed
        if progressDialog:
            try:
                progressDialog.update(40, 'Processing torrent...')
            except Exception:
                pass
        if not _interruptible_sleep(PACK_PROCESS_DELAY):
            return None

        if _abort_resolution_if_cancelled('before torrent info'):
            return None

        # Get torrent info
        if progressDialog:
            try:
                progressDialog.update(60, 'Finding episode file...')
            except Exception:
                pass
        info_url = f'{base_url}/torrents/info/{torrent_id}'
        info_response = requests.get(info_url, headers=headers, timeout=DEBRID_API_TIMEOUT)

        if _rd_check_429(info_response, 'torrents/info'):
            return None
        if info_response.status_code != 200:
            c.log(f'[RD Pack] Failed to get torrent info: {info_response.status_code}', 1)
            return None

        torrent_info = info_response.json()
        files = torrent_info.get('files', [])

        if not files:
            c.log('[RD Pack] No files in torrent', 1)
            return None

        # Filter video files
        video_extensions = source_utils.supported_video_extensions()
        video_files = [f for f in files if any(f['path'].lower().endswith(ext) for ext in video_extensions)]

        if not video_files:
            c.log('[RD Pack] No video files in torrent', 1)
            return None

        c.log(f'[RD Pack] Found {len(video_files)} video files, searching for S{season:02d}E{episode:02d}')

        # Find ALL matching files using seas_ep_filter
        matched_files = []
        for file in video_files:
            if source_utils.seas_ep_filter(season, episode, file['path']):
                matched_files.append(file)
                c.log(f'[RD Pack] Potential match: {file["path"]} ({file.get("bytes", 0) / 1024 / 1024:.1f} MB)')

        if not matched_files:
            c.log('[RD Pack] No matching episode file found', 1)
            return None

        # Filter out samples and proofs
        non_sample_files = [f for f in matched_files if not any(x in f['path'].lower() for x in ['sample', 'proof', '-trailer'])]
        if non_sample_files:
            matched_files = non_sample_files
        else:
            c.log('[RD Pack] Warning: All matches look like samples!')

        # Prefer provider language (e.g. English over French twins), then size
        matched_file = source_utils.pick_best_pack_file(
            matched_files, path_key='path', size_key='bytes'
        )
        c.log(f'[RD Pack] Selected best match: {matched_file["path"]} ({matched_file.get("bytes", 0) / 1024 / 1024:.1f} MB)')
        if progressDialog:
            try:
                progressDialog.update(80, 'Generating playable link...')
            except Exception:
                pass

        # Final validation that we have a matched file
        if not matched_file:
            c.log(f'[RD Pack] No file matched S{season:02d}E{episode:02d} pattern', 1)
            return None

        # Select the file (retry up to 3 times - transient RD API timeouts are common)
        file_id = str(matched_file['id'])
        select_url = f'{base_url}/torrents/selectFiles/{torrent_id}'
        select_data = {'files': file_id}
        select_response = None
        for _attempt in range(3):
            if _abort_resolution_if_cancelled(f'selectFiles attempt {_attempt + 1}'):
                return None
            try:
                select_response = requests.post(select_url, data=select_data, headers=headers, timeout=DEBRID_API_TIMEOUT)
                if _rd_check_429(select_response, 'selectFiles'):
                    return None
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as _exc:
                if _attempt < 2:
                    c.log(f'[RD Pack] selectFiles timeout (attempt {_attempt + 1}/3), retrying...')
                    if not _interruptible_sleep(2):
                        return None
                else:
                    c.log(f'[RD Pack] selectFiles failed after 3 attempts: {_exc}', 1)
                    raise

        if select_response is None or select_response.status_code not in (202, 204):
            status_code = select_response.status_code if select_response is not None else 'N/A'
            c.log(f'[RD Pack] Failed to select file: {status_code}', 1)
            return None
        if select_response.status_code == 202:
            c.log('[RD Pack] selectFiles returned 202 - files already selected, continuing')

        # Check if torrent is already cached (downloaded) or needs downloading
        c.log('[RD Pack] Checking torrent status...')
        if not _interruptible_sleep(STATUS_CHECK_DELAY):
            return None

        if _abort_resolution_if_cancelled('before status check'):
            return None

        info_response = requests.get(info_url, headers=headers, timeout=DEBRID_API_TIMEOUT)
        if _rd_check_429(info_response, 'torrents/info (status check)'):
            return None
        torrent_info = info_response.json()
        status = torrent_info.get('status')
        progress = torrent_info.get('progress', 0)

        c.log(f'[RD Pack] Status: {status}, progress: {progress}%')

        # If torrent is not cached, return None to let sources loop try next source
        # DO NOT fall back to resolveurl here - that would add the magnet again!
        # The sources loop will automatically try the next available source.
        if status in ('downloading', 'queued', 'magnet_conversion', 'waiting_files_selection'):
            c.log(f'WARNING: Pack not cached (status: {status}) - skipping to try next source')
            c.log(f'WARNING: Note: Torrent is queued but will take time. Trying alternative sources.')
            c.log('[Pack] Released resolution lock')
            return None

        if status == 'error':
            c.log('[RD Pack] Real-Debrid reported an error status', 1)
            return None

        # Torrent is cached (downloaded), get links
        c.log(f'[RD Pack] Torrent is cached (status: {status}), getting links...')

        links = torrent_info.get('links', [])
        if not links:
            c.log('[RD Pack] No download links available', 1)
            return None

        # Map our matched file to the correct link index.
        # RD's links[] corresponds 1:1 with the SELECTED files in the order
        # they appear in the files array. For fully-cached packs all episodes
        # may be selected, so links[0] is NOT necessarily our episode - find
        # the position of our specific file among the selected files.
        updated_files = torrent_info.get('files', [])
        selected_files_ordered = [f for f in updated_files if f.get('selected') == 1]
        link_index = None
        for i, f in enumerate(selected_files_ordered):
            if f['id'] == matched_file['id']:
                link_index = i
                c.log(f'[RD Pack] S{season:02d}E{episode:02d} is at link index {i} of {len(links)} links '
                      f'({len(selected_files_ordered)} selected files)')
                break

        if link_index is None:
            # Prior selectFiles left a different subset selected. links[0] would
            # play the first pack file (often wrong episode / French twin).
            c.log(
                f'[RD Pack] Matched file id={matched_file["id"]} not in selected set '
                f'({len(selected_files_ordered)} selected) - will not use links[0]',
                1,
            )
            if _reselect:
                c.log('[RD Pack] Already reselected once - aborting pack resolve', 1)
                return None
            try:
                del_url = f'{base_url}/torrents/delete/{torrent_id}'
                del_resp = requests.delete(del_url, headers=headers, timeout=DEBRID_API_TIMEOUT)
                c.log(f'[RD Pack] Deleted torrent {torrent_id} for reselect (HTTP {del_resp.status_code})')
            except Exception as del_exc:
                c.log(f'[RD Pack] Failed to delete torrent for reselect: {del_exc}', 1)
                return None
            if not _interruptible_sleep(1):
                return None
            return resolve_pack_realdebrid(
                magnet_url, season, episode, progressDialog=progressDialog, _reselect=True
            )

        if link_index >= len(links):
            c.log(f'[RD Pack] Link index {link_index} out of range ({len(links)} links)', 1)
            return None

        unrestrict_url = f'{base_url}/unrestrict/link'
        unrestrict_response = requests.post(unrestrict_url, data={'link': links[link_index]},
                                            headers=headers, timeout=DEBRID_API_TIMEOUT)

        if _rd_check_429(unrestrict_response, 'unrestrict/link'):
            return None
        if unrestrict_response.status_code != 200:
            c.log(f'[RD Pack] Failed to unrestrict link: {unrestrict_response.status_code}', 1)
            return None

        unrestrict_info = unrestrict_response.json()
        download_url = unrestrict_info.get('download')
        resolved_filename = unrestrict_info.get('filename', '')
        if download_url:
            # Final sanity check: confirm the resolved file actually matches our episode
            if resolved_filename and not source_utils.seas_ep_filter(season, episode, resolved_filename):
                c.log(f'[RD Pack] MISMATCH: resolved file "{resolved_filename}" does not match '
                      f'S{season:02d}E{episode:02d} - aborting to avoid wrong episode', 1)
                return None
            # Prefer provider language: reject if resolved name is clearly another audio
            preferred = source_utils._preferred_pack_langs()
            if (
                resolved_filename
                and source_utils.pack_path_language_score(resolved_filename, preferred) < 0
                and source_utils.pack_path_language_score(matched_file.get('path') or '', preferred) >= 0
            ):
                c.log(
                    f'[RD Pack] LANGUAGE MISMATCH: resolved "{resolved_filename}" vs preferred '
                    f'{preferred} - aborting',
                    1,
                )
                return None
            c.log(f'[RD Pack] Successfully resolved: {resolved_filename}')
            return download_url

        return None

    except requests.RequestException as e:
        c.log(f'[RD Pack] Network error: {e}', 1)
        _notify_debrid_unreachable('Real-Debrid')
        return None
    except Exception as e:
        c.log(f'[RD Pack] Exception: {e}', 1)
        c.log(f'[RD Pack] Traceback: {traceback.format_exc()}', 1)
        return None


def resolve_pack_alldebrid(magnet_url, season, episode, progressDialog=None):
    """Resolve pack with AllDebrid API (v4.1)."""
    c.log(f'[AD Pack] Function called for S{season:02d}E{episode:02d}')
    try:
        if progressDialog:
            try:
                progressDialog.update(10, 'Getting AllDebrid token...')
            except Exception:
                pass
        ad_token, token_source = get_alldebrid_apikey()
        if not ad_token:
            c.log('[AD Pack] No valid AllDebrid token found (ResolveURL/Midnight) - skipping', 1)
            _mark_debrid_unusable('AllDebrid', 'no valid API key')
            return None
        c.log(f'[AD Pack] Using AllDebrid token from {token_source}')

        # AllDebrid API requires apikey as a query param, NOT as a Bearer header.
        ad_agent = 'midnight'
        ad_auth = {'agent': ad_agent, 'apikey': ad_token}
        base_v4 = 'https://api.alldebrid.com/v4'

        # Step 1: POST magnet/upload - this acts as both cache check and upload.
        # The response immediately contains ready=True for cached content.
        # /magnet/instant no longer exists (returns 404 since ~2025).
        c.log('[AD Pack] Uploading magnet to AllDebrid')
        if progressDialog:
            try:
                progressDialog.update(20, 'Checking AllDebrid cache...')
            except Exception:
                pass
        add_response = requests.post(
            f'{base_v4}/magnet/upload',
            params=ad_auth,
            data={'magnets[]': magnet_url},
            timeout=DEBRID_API_TIMEOUT
        )

        if add_response.status_code != 200:
            c.log(f'[AD Pack] magnet/upload failed: {add_response.status_code}', 1)
            return None

        add_data = add_response.json()
        if add_data.get('status') != 'success':
            c.log(f'[AD Pack] magnet/upload error: {add_data}', 1)
            try:
                err = add_data.get('error', {}) if isinstance(add_data, dict) else {}
                if str(err.get('code', '')).upper() == 'AUTH_BAD_APIKEY':
                    _mark_debrid_unusable('AllDebrid', 'AUTH_BAD_APIKEY')
                    _notify_debrid_auth_failed('AllDebrid')
            except Exception:
                pass
            return None

        magnet_obj = add_data['data']['magnets'][0]
        if 'error' in magnet_obj:
            c.log(f'[AD Pack] Magnet error: {magnet_obj["error"]}', 1)
            return None

        magnet_id = magnet_obj['id']
        is_ready = magnet_obj.get('ready', False)
        c.log(f'[AD Pack] magnet/upload -> id={magnet_id}, ready={is_ready}')

        if not is_ready:
            c.log('[AD Pack] Not cached (ready=False) - skipping')
            return None

        # Step 3: Get file list via dedicated /magnet/files endpoint (v4.1 status no longer returns links)
        c.log(f'[AD Pack] Torrent is cached, fetching file list...')
        if progressDialog:
            try:
                progressDialog.update(55, 'Getting file list...')
            except Exception:
                pass

        files_response = requests.post(
            f'{base_v4}/magnet/files',
            params=ad_auth,
            data={'id[]': magnet_id},
            timeout=DEBRID_API_TIMEOUT
        )

        if files_response.status_code != 200:
            c.log(f'[AD Pack] Failed to get files: {files_response.status_code}', 1)
            return None

        files_data = files_response.json()
        if files_data.get('status') != 'success':
            c.log(f'[AD Pack] Files fetch failed: {files_data}', 1)
            return None

        files_tree = files_data.get('data', {}).get('magnets', [{}])[0].get('files', [])

        # Flatten the nested folder tree - keep full path so French/English
        # folder twins can be scored (basename-only loses language).
        def _flatten(nodes, prefix=''):
            result = []
            for node in nodes:
                name = node.get('n') or ''
                path = f'{prefix}/{name}' if prefix else name
                if 'e' in node:
                    result.extend(_flatten(node['e'], path))
                elif 'l' in node:
                    result.append({
                        'filename': name,
                        'path': path,
                        'link': node['l'],
                        'size': node.get('s', 0),
                    })
            return result

        all_files = _flatten(files_tree)

        # Filter video files
        video_extensions = source_utils.supported_video_extensions()
        video_files = [f for f in all_files if any(f['filename'].lower().endswith(ext) for ext in video_extensions)]

        if not video_files:
            c.log('[AD Pack] No video files found', 1)
            return None

        c.log(f'[AD Pack] Found {len(video_files)} video files, searching for S{season:02d}E{episode:02d}')
        if progressDialog:
            try:
                progressDialog.update(60, 'Finding episode file...')
            except Exception:
                pass

        matched_files = []
        for file in video_files:
            check_name = file.get('path') or file['filename']
            if source_utils.seas_ep_filter(season, episode, check_name):
                matched_files.append(file)
                c.log(f'[AD Pack] Potential match: {check_name} ({file.get("size", 0) / 1024 / 1024:.1f} MB)')

        if not matched_files:
            c.log('[AD Pack] No matching episode file found', 1)
            return None

        non_sample_files = [
            f for f in matched_files
            if not any(x in (f.get('path') or f['filename']).lower() for x in ['sample', 'proof', '-trailer'])
        ]
        if non_sample_files:
            matched_files = non_sample_files
        else:
            c.log('[AD Pack] Warning: All matches look like samples!')

        matched_file = source_utils.pick_best_pack_file(
            matched_files, path_key='path', size_key='size'
        )
        c.log(
            f'[AD Pack] Selected best match: {matched_file.get("path") or matched_file["filename"]} '
            f'({matched_file.get("size", 0) / 1024 / 1024:.1f} MB)'
        )
        if progressDialog:
            try:
                progressDialog.update(80, 'Generating playable link...')
            except Exception:
                pass

        # Step 4: Unrestrict the file link
        unrestrict_response = requests.post(
            f'{base_v4}/link/unlock',
            params=ad_auth,
            data={'link': matched_file['link']},
            timeout=DEBRID_API_TIMEOUT
        )

        if unrestrict_response.status_code != 200:
            c.log(f'[AD Pack] Failed to unrestrict: {unrestrict_response.status_code}', 1)
            return None

        unrestrict_data = unrestrict_response.json()
        if unrestrict_data.get('status') != 'success':
            c.log(f'[AD Pack] Unrestrict failed: {unrestrict_data}', 1)
            return None

        download_url = unrestrict_data['data']['link']
        if download_url:
            c.log('[AD Pack] Successfully resolved pack file')
            return download_url

        return None

    except requests.RequestException as e:
        c.log(f'[AD Pack] Network error: {e}', 1)
        _notify_debrid_unreachable('AllDebrid')
        return None
    except Exception as e:
        c.log(f'[AD Pack] Exception: {e}', 1)
        c.log(f'[AD Pack] Traceback: {traceback.format_exc()}', 1)
        return None


def resolve_pack_premiumize(magnet_url, season, episode, progressDialog=None):
    """Resolve pack with Premiumize API."""
    c.log(f'[PM Pack] Function called for S{season:02d}E{episode:02d}')
    try:
        if progressDialog:
            try:
                progressDialog.update(10, 'Checking Premiumize cache...')
            except Exception:
                pass
        pm_token, token_source = get_debrid_token('PremiumizeMeResolver_token', 'premiumizetoken')
        if not pm_token:
            c.log('[PM Pack] No Premiumize token found in resolveurl or Midnight settings', 1)
            return None

        base_url = 'https://www.premiumize.me/api'
        headers = {'Authorization': f'Bearer {pm_token}'}

        # Step 1: Use cache/check (lightweight, not rate-limited like transfer/create)
        # Extract btih hash from magnet URL
        import re as _re
        btih_match = _re.search(r'btih:([0-9a-fA-F]{32,40})', magnet_url, _re.IGNORECASE)
        if not btih_match:
            c.log('[PM Pack] Could not extract btih hash from magnet URL - skipping', 1)
            return None
        btih_hash = btih_match.group(1).lower()

        c.log(f'[PM Pack] Checking cache via cache/check for hash {btih_hash} (token from {token_source})')
        cache_response = requests.get(
            f'{base_url}/cache/check',
            params={'items[]': btih_hash},
            headers=headers,
            timeout=DEBRID_API_TIMEOUT
        )

        if cache_response.status_code != 200:
            c.log(f'[PM Pack] cache/check failed: {cache_response.status_code} - skipping', 1)
            return None

        cache_data = cache_response.json()
        if cache_data.get('status') != 'success':
            c.log(f'[PM Pack] cache/check error: {cache_data}', 1)
            return None

        is_cached = cache_data.get('response', [False])[0]
        if not is_cached:
            c.log(f'[PM Pack] Not cached (cache/check returned false) - skipping')
            return None

        # Step 2: Use transfer/directdl - returns file list directly without creating a
        # cloud transfer queue entry.  transfer/create is rate-limited (429) when called
        # rapidly for many sources; directdl has no such restriction for cached content.
        c.log(f'[PM Pack] Cache hit! Calling transfer/directdl to get file list...')
        if progressDialog:
            try:
                progressDialog.update(40, 'Fetching file list from Premiumize...')
            except Exception:
                pass

        directdl_response = requests.post(
            f'{base_url}/transfer/directdl',
            data={'src': magnet_url},
            headers=headers,
            timeout=DEBRID_API_TIMEOUT
        )

        if directdl_response.status_code != 200:
            c.log(f'[PM Pack] directdl failed: {directdl_response.status_code}', 1)
            return None

        directdl_data = directdl_response.json()
        if directdl_data.get('status') != 'success':
            c.log(f'[PM Pack] directdl error: {directdl_data}', 1)
            return None

        # directdl returns {"content":[{"path":"filename.mkv","link":"...","stream_link":"...","size":...}]}
        # Keep full path for seas_ep + language scoring (French/English folder twins).
        raw_content = directdl_data.get('content', [])
        content = []
        for item in raw_content:
            path = item.get('path', '') or ''
            name = path.rsplit('/', 1)[-1] if '/' in path else path
            content.append({
                'name': name,
                'path': path or name,
                'size': item.get('size', 0),
                'link': item.get('stream_link') or item.get('link', ''),
                'type': 'file',
            })

        # Filter video files
        video_extensions = source_utils.supported_video_extensions()
        video_files = [item for item in content if item.get('type') == 'file' and any(item['name'].lower().endswith(ext) for ext in video_extensions)]

        if not video_files:
            c.log('[PM Pack] No video files found', 1)
            return None

        c.log(f'[PM Pack] Found {len(video_files)} video files, searching for S{season:02d}E{episode:02d}')
        if progressDialog:
            try:
                progressDialog.update(60, 'Finding episode file...')
            except Exception:
                pass

        matched_files = []
        for file in video_files:
            check_name = file.get('path') or file['name']
            if source_utils.seas_ep_filter(season, episode, check_name):
                matched_files.append(file)
                c.log(f'[PM Pack] Potential match: {check_name} ({file.get("size", 0) / 1024 / 1024:.1f} MB)')

        if not matched_files:
            c.log('[PM Pack] No matching episode file found', 1)
            return None

        non_sample_files = [
            f for f in matched_files
            if not any(x in (f.get('path') or f['name']).lower() for x in ['sample', 'proof', '-trailer'])
        ]
        if non_sample_files:
            matched_files = non_sample_files
        else:
            c.log('[PM Pack] Warning: All matches look like samples!')

        matched_file = source_utils.pick_best_pack_file(
            matched_files, path_key='path', size_key='size'
        )
        c.log(
            f'[PM Pack] Selected best match: {matched_file.get("path") or matched_file["name"]} '
            f'({matched_file.get("size", 0) / 1024 / 1024:.1f} MB)'
        )
        if progressDialog:
            try:
                progressDialog.update(80, 'Generating playable link...')
            except Exception:
                pass

        download_url = matched_file.get('link')
        if download_url:
            c.log('[PM Pack] Successfully resolved pack file')
            return download_url

        return None

    except requests.RequestException as e:
        c.log(f'[PM Pack] Network error: {e}', 1)
        _notify_debrid_unreachable('Premiumize')
        return None
    except Exception as e:
        c.log(f'[PM Pack] Exception: {e}', 1)
        c.log(f'[PM Pack] Traceback: {traceback.format_exc()}', 1)
        return None

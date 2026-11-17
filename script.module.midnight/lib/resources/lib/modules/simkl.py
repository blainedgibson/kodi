# -*- coding: utf-8 -*-

'''
 ***********************************************************
 * Midnight Add-on
 *
 *
 * @file simkl.py
 * @package script.module.midnight
 *
 * @copyright (c) 2026, Midnight
 * @license GNU General Public License, version 3 (GPL-3.0)
 *
 * Simkl module — credential helpers, PIN auth dialog, scrobble helpers.
 * Analog to trakt.py but for Simkl.
 *
 * Tokens NEVER expire — no token refresh needed.
 *
 ********************************************************cm*
'''

import re
import time
import traceback

from .crewruntime import c
from . import keys
from . import control

# -------------------------------------------------------------------
# Constants
# -------------------------------------------------------------------
BASE_URL = 'https://api.simkl.com'
PIN_POLL_URL = BASE_URL + '/oauth/pin/{user_code}'
PIN_REQUEST_URL = BASE_URL + '/oauth/pin'

# Required query params — must appear on every Simkl API request
# https://simkl.docs.apiary.io/#introduction/required-query-parameters
_APP_NAME = 'TheCrew'


def _app_version() -> str:
    """Live module version for Simkl required app-version query param."""
    try:
        ver = getattr(c, 'moduleversion', None) or ''
        if ver:
            return str(ver)
    except Exception:
        pass
    return '3.0.9.11'


def _user_agent() -> str:
    return f'TheCrew/{_app_version()} Kodi'


def _get_client_id() -> str:
    """
    Return the active Simkl client_id.
    User-supplied value (simkl.client_id setting) takes priority over the
    built-in key — lets users register their own free app at
    https://simkl.com/settings/developer/new/ to avoid the shared daily limit
    while Midnight awaits production approval from Simkl.
    """
    try:
        override = c.get_setting('simkl.client_id') or ''
        if override.strip():
            return override.strip()
    except Exception:
        pass
    return keys.simkl_id


def _required_params() -> dict:
    return {
        'client_id': _get_client_id(),
        'app-name': _APP_NAME,
        'app-version': _app_version(),
    }


# -------------------------------------------------------------------
# Credential helpers
# -------------------------------------------------------------------

def get_simkl_credentials_info() -> bool:
    """
    Return True if a Simkl access token is stored in settings.
    Parallel to trakt.get_trakt_credentials_info().
    """
    try:
        token = c.get_setting('simkl.token') or ''
        return bool(token.strip())
    except Exception:
        return False


def get_simkl_username() -> str:
    """Return stored Simkl username, or empty string."""
    try:
        return (c.get_setting('simkl.user') or '').strip()
    except Exception:
        return ''


def get_tracker_service() -> int:
    """
    Exclusive tracker from settings (no "both"):
        0 = Trakt
        1 = Simkl

    Legacy value 2 (Both) is coerced to 0 (Trakt) once.
    """
    try:
        raw = int(c.get_setting('tracker.service') or 0)
    except Exception:
        return 0
    if raw == 1:
        return 1
    if raw == 2:
        # Dropped "Both" — fall back to Trakt; user can switch to Simkl.
        try:
            c.set_setting('tracker.service', '0')
            c.log('[Simkl] tracker.service Both→Trakt (exclusive tracker)', 1)
        except Exception:
            pass
        return 0
    return 0


def use_simkl_tracker() -> bool:
    """True when Simkl is the active tracker (exclusive)."""
    return get_tracker_service() == 1


def use_trakt_tracker() -> bool:
    """True when Trakt is the active tracker (exclusive)."""
    return get_tracker_service() == 0


def is_simkl_scrobble_enabled() -> bool:
    """
    Scrobble to Simkl when it is the selected tracker and authorized.
    Optional simkl.scrobble=false still disables (other addon already scrobbles).
    """
    try:
        if not use_simkl_tracker() or not get_simkl_credentials_info():
            return False
        scrobble = (c.get_setting('simkl.scrobble') or 'true').lower()
        return scrobble == 'true'
    except Exception:
        return False


def is_trakt_scrobble_enabled() -> bool:
    """
    Scrobble to Trakt when it is the selected tracker.
    Caller still checks Trakt credentials. Optional trakt.scrobble=false disables.
    """
    try:
        if not use_trakt_tracker():
            return False
        return (c.get_setting('trakt.scrobble') or 'true').lower() == 'true'
    except Exception:
        return False


def should_scrobble_playback() -> bool:
    """True when player/monitors should call bookmarks.set_scrobble()."""
    try:
        if use_simkl_tracker():
            return is_simkl_scrobble_enabled()
        if not is_trakt_scrobble_enabled():
            return False
        from . import trakt
        return bool(trakt.get_trakt_credentials_info())
    except Exception as e:
        c.log(f'[Simkl] should_scrobble_playback error: {e}', 1)
        return False


def scrobble_gate_debug() -> str:
    """One-line status for logs when diagnosing missed scrobbles."""
    try:
        return (
            f'tracker={get_tracker_service()} '
            f'name={"simkl" if use_simkl_tracker() else "trakt"} '
            f'simkl_token={get_simkl_credentials_info()} '
            f'simkl_scrobble={(c.get_setting("simkl.scrobble") or "").strip()!r} '
            f'trakt_scrobble={(c.get_setting("trakt.scrobble") or "").strip()!r} '
            f'gate={should_scrobble_playback()}'
        )
    except Exception as e:
        return f'gate-debug-error={e}'


def set_simkl_credentials(token: str, username: str = ''):
    """Save Simkl token + username to Kodi settings and keep backups in sync."""
    c.set_setting('simkl.token', token)
    c.set_setting('simkl.user', username)
    # Kodi often leaves default=\"true\" on first write — strip + persist on disk.
    try:
        from .settings_repair import (
            persist_setting_on_disk,
            sync_simkl_credentials_into_backups,
        )
        persist_setting_on_disk('simkl.token', token or '')
        persist_setting_on_disk('simkl.user', username or '')
        if (token or '').strip():
            sync_simkl_credentials_into_backups(log=c.log)
    except Exception as e:
        c.log(f'[Simkl] credential backup sync skipped: {e}', 1)


# -------------------------------------------------------------------
# PIN device flow — Kodi dialog
# -------------------------------------------------------------------

def show_simkl_pin_dialog(verification_url: str, user_code: str,
                          qr_path: str, interval: int, expires_in: int):
    """
    Show QR code dialog for Simkl PIN authentication and poll until token.

    Polls: GET https://api.simkl.com/oauth/pin/{user_code}?client_id={CLIENT_ID}
    Response when approved: {"result": "OK", "access_token": "..."}
    Response when pending:  {"result": "pending"}
    Response when expired:  {"result": "expired"}

    Re-uses the LogViewer_QR.xml dialog from script.midnight.artwork.

    Args:
        verification_url: Full URL user visits (https://simkl.com/pin/USERCODE)
        user_code:        PIN code to display
        qr_path:          Local path to QR image file (or None)
        interval:         Polling interval in seconds
        expires_in:       Seconds until PIN expires

    Returns:
        (bool, str): (success, access_token) — token is '' on failure
    """
    try:
        import xbmcgui
        import threading

        poll_url = PIN_POLL_URL.format(user_code=user_code)
        # Polling uses GET — include required params (no throttle needed for GET)
        poll_params = {**_required_params()}  # copy so we don't mutate

        class SimklPINViewer(xbmcgui.WindowXMLDialog):

            def __init__(self, *args, **kwargs):
                self.header = kwargs.get('header', 'Simkl Authorization')
                self.message = kwargs.get('message', '')
                self.qr_path = kwargs.get('qr_path', '') or ''
                self.interval = kwargs.get('interval', 5)
                self.expires_in = kwargs.get('expires_in', 600)
                self.authenticated = False
                self.access_token = ''
                self.polling = True
                self.poll_thread = None

            def onInit(self):
                try:
                    HEADERLABEL = 101
                    TEXT = 502
                    QR_IMAGE = 501
                    CLOSEBUTTON = 503

                    self.getControl(HEADERLABEL).setLabel(self.header)
                    self.getControl(TEXT).setText(self.message)

                    if self.qr_path:
                        self.getControl(QR_IMAGE).setImage(self.qr_path)

                    self.setFocusId(CLOSEBUTTON)

                    self.poll_thread = threading.Thread(target=self._poll_for_token)
                    self.poll_thread.daemon = True
                    self.poll_thread.start()

                except Exception as e:
                    c.log(f'[Simkl PIN] onInit error: {e}', 1)

            def _poll_for_token(self):
                """Poll GET /oauth/pin/{user_code} until token or timeout."""
                try:
                    from . import http_client
                    session = http_client.get_simkl_session()

                    start_time = time.time()
                    while self.polling and (time.time() - start_time) < self.expires_in:
                        time.sleep(self.interval)
                        try:
                            resp = session.get(poll_url, params=poll_params,
                                               headers={'User-Agent': _user_agent()},
                                               timeout=10)
                            if resp.status_code == 200:
                                data = resp.json()
                                if data.get('result') == 'OK':
                                    token = data.get('access_token', '')
                                    if token:
                                        self.authenticated = True
                                        self.access_token = token
                                        self.close()
                                        return
                                elif data.get('result') == 'expired':
                                    c.log('[Simkl PIN] PIN expired', 1)
                                    self.close()
                                    return
                            # 400 / "pending" = still waiting, keep polling
                        except Exception as poll_err:
                            c.log(f'[Simkl PIN] Poll request error: {poll_err}', 1)

                    if self.polling:
                        c.log('[Simkl PIN] Polling timed out', 1)
                        self.close()

                except Exception as e:
                    c.log(f'[Simkl PIN] Polling thread error: {e}', 1)

            def onAction(self, action):
                if action.getId() in [10, 92]:  # NAV_BACK, PREVIOUS_MENU
                    self.polling = False
                    self.close()

            def onClick(self, controlId):
                if controlId == 503:  # Close button
                    self.polling = False
                    self.close()

        xml_file = 'LogViewer_QR.xml'
        addon_path = c.get_artwork_path()
        skin = c.appearance() or 'midnight'

        message = (
            f'Scan QR code with your mobile device\n\n'
            f'OR visit:\n{verification_url}\n\n'
            f'Waiting for authorization... (expires in {expires_in}s)'
        )

        dialog = SimklPINViewer(
            xml_file,
            addon_path,
            skin,
            '1080i',
            header='Simkl Authorization',
            message=message,
            qr_path=qr_path or '',
            interval=interval,
            expires_in=expires_in
        )
        dialog.doModal()

        authenticated = dialog.authenticated
        access_token = dialog.access_token
        dialog.polling = False
        del dialog

        return authenticated, access_token

    except Exception as e:
        c.log(f'[Simkl PIN] Dialog error: {e}', 1)
        c.log(traceback.format_exc(), 1)
        return False, ''


# -------------------------------------------------------------------
# Auth entry points (called from crew.py router)
# -------------------------------------------------------------------

def auth_simkl():
    """
    Full Simkl PIN authorization flow.
    Entry point for action=authSimkl in crew.py.

    1. POST /oauth/pin to get user_code
    2. Show QR dialog + poll
    3. GET /users/settings to fetch username
    4. Save token + username
    """
    try:
        if get_simkl_credentials_info():
            username = get_simkl_username()
            prompt = f'Simkl account [{username}] already authorized.\n\nRe-authorize?'
            if not c.yesnoDialog(prompt, 'Simkl'):
                return
            set_simkl_credentials('', '')

        from . import http_client
        session = http_client.get_simkl_session()

        # Step 1: request PIN
        c.log('[Simkl Auth] Requesting PIN...', 1)
        client_id = _get_client_id()
        if not client_id:
            c.infoDialog(
                'No Simkl Client ID configured.\n\n'
                'Add your Client ID in Settings > API Keys > Simkl Client ID.\n'
                'Register free at simkl.com/settings/developer/new',
                heading='Simkl', sound=True)
            return
        pin_resp = session.post(PIN_REQUEST_URL, params={'client_id': client_id},
                                headers={'User-Agent': _user_agent()},
                                json={}, timeout=30)

        if pin_resp.status_code != 200:
            c.log(f'[Simkl Auth] PIN request failed: {pin_resp.status_code}', 1)
            c.infoDialog('Failed to connect to Simkl. Check your internet connection.',
                         heading='Simkl', sound=True)
            return

        pin_data = pin_resp.json()
        user_code = pin_data.get('user_code', '')
        expires_in = int(pin_data.get('expires_in', 600))
        interval = int(pin_data.get('interval', 5))

        if not user_code:
            c.log('[Simkl Auth] No user_code in response', 1)
            c.infoDialog('Simkl returned an invalid response.', heading='Simkl', sound=True)
            return

        pin_url = f'https://simkl.com/pin/{user_code}'
        c.log(f'[Simkl Auth] user_code={user_code}, url={pin_url}', 1)

        # Step 2: generate QR (same local segno path as Trakt auth)
        qr_path = None
        try:
            from .trakt import generate_qr_code_local
            qr_path = generate_qr_code_local(pin_url, size=280)
        except Exception as qr_err:
            c.log(f'[Simkl Auth] QR generation failed (non-fatal): {qr_err}', 1)

        # Step 3: show QR dialog + poll
        authenticated, access_token = show_simkl_pin_dialog(
            verification_url=pin_url,
            user_code=user_code,
            qr_path=qr_path,
            interval=interval,
            expires_in=expires_in
        )

        if not authenticated or not access_token:
            c.log('[Simkl Auth] Authorization failed or timed out', 1)
            c.infoDialog('Simkl authorization failed or timed out.', heading='Simkl', sound=True)
            return

        # Step 4: fetch username
        username = _fetch_username(session, access_token)

        # Step 5: save
        set_simkl_credentials(access_token, username)
        c.log(f'[Simkl Auth] Authorized as: {username}', 1)
        c.infoDialog(f'Simkl authorized{": " + username if username else ""}',
                     heading='Simkl', sound=False)

    except Exception as e:
        c.log(f'[Simkl Auth] Exception: {e}', 1)
        c.log(traceback.format_exc(), 1)
        c.infoDialog(f'Simkl authorization error:\n{e}', heading='Simkl', sound=True)
        # Category index for Trakt / Simkl in settings.xml (0-based)
        control.openSettings('5.0')
    finally:
        control.finish_dialog_route()


def revoke_simkl():
    """
    Clear stored Simkl credentials.
    Entry point for action=revokeSimkl in crew.py.

    Simkl has no API revoke endpoint — clearing locally is sufficient.
    """
    try:
        if not get_simkl_credentials_info():
            c.infoDialog('No Simkl account is authorized.', heading='Simkl', sound=False)
            return

        username = get_simkl_username()
        if c.yesnoDialog(
            f'Remove Simkl authorization{" for " + username if username else ""}?',
            'Simkl'
        ):
            set_simkl_credentials('', '')
            try:
                from .settings_repair import purge_simkl_credentials_from_backups
                purge_simkl_credentials_from_backups(log=c.log)
            except Exception as purge_exc:
                c.log(f'[Simkl Auth] Backup purge after revoke skipped: {purge_exc}', 1)
            c.log('[Simkl Auth] Authorization revoked', 1)
            c.infoDialog('Simkl authorization removed.', heading='Simkl', sound=False)
    except Exception as e:
        c.log(f'[Simkl Auth] revoke_simkl error: {e}', 1)


# -------------------------------------------------------------------
# Internal helpers
# -------------------------------------------------------------------

def _fetch_username(session, token: str) -> str:
    """
    Fetch display name from Simkl /users/settings.
    API returns user.name (not username). GET works; docs also list POST.
    """
    try:
        headers = {
            'Content-Type': 'application/json',
            'simkl-api-key': _get_client_id(),
            'Authorization': f'Bearer {token}',
            'User-Agent': _user_agent(),
        }
        resp = session.get(f'{BASE_URL}/users/settings',
                           headers=headers,
                           params=_required_params(),
                           timeout=15)
        if resp.status_code != 200:
            # Historical POST fallback
            resp = session.post(f'{BASE_URL}/users/settings',
                                headers=headers,
                                params=_required_params(),
                                json={},
                                timeout=15)
        if resp.status_code == 200:
            data = resp.json() or {}
            user = data.get('user') or {}
            name = (user.get('name') or user.get('username') or '').strip()
            if name:
                return name
            c.log(f'[Simkl] _fetch_username: no name in response keys={list(user.keys())}', 1)
        else:
            c.log(f'[Simkl] _fetch_username HTTP {resp.status_code}: {resp.text[:200]}', 1)
    except Exception as e:
        c.log(f'[Simkl] _fetch_username error: {e}', 1)
    return 'simkl_user'


# -------------------------------------------------------------------
# Scrobble helpers — called from bookmarks.set_scrobble
# -------------------------------------------------------------------

def simkl_scrobble_movie(imdb: str = '', title: str = '', year: int = 0,
                         progress: float = 0, action: str = 'stop', tmdb: int = 0):
    """
    Send a movie scrobble event to Simkl.

    Args:
        imdb:     IMDb ID (e.g. 'tt1375666')
        title:    Movie title (optional; IDs are enough)
        year:     Release year (optional)
        progress: Playback percentage 0–100
        action:   'start', 'pause', or 'stop'
        tmdb:     TMDB movie id (optional)
    """
    if not is_simkl_scrobble_enabled():
        return

    try:
        from ..apis.simkl_api import SimklAPI
        api = SimklAPI()
        movie_obj = {
            'movie': SimklAPI.build_movie_obj(title=title, year=year, imdb=imdb, tmdb=tmdb)
        }

        if action == 'start':
            api.scrobble_start(movie_obj, progress)
        elif action == 'pause':
            api.scrobble_pause(movie_obj, progress)
        else:
            api.scrobble_stop(movie_obj, progress)
            invalidate_movie_indicators()

        if c.devmode:
            c.log(f'[Simkl Scrobble] Movie {action} — imdb={imdb} tmdb={tmdb} @ {progress:.0f}%', 1)

    except Exception as e:
        c.log(f'[Simkl Scrobble] simkl_scrobble_movie error: {e}', 1)


def simkl_scrobble_episode(imdb: str = '', show_title: str = '', year: int = 0,
                           season: int = 0, episode: int = 0,
                           progress: float = 0, action: str = 'stop', tmdb: int = 0):
    """
    Send an episode scrobble event to Simkl.

    Args:
        imdb:       Show's IMDb ID (e.g. 'tt0903747')
        show_title: Show name (optional)
        year:       Show premiere year (optional)
        season:     Season number
        episode:    Episode number
        progress:   Playback percentage 0–100
        action:     'start', 'pause', or 'stop'
        tmdb:       TMDB show id (optional)
    """
    if not is_simkl_scrobble_enabled():
        return

    try:
        from ..apis.simkl_api import SimklAPI
        api = SimklAPI()

        episode_payload = {
            'show': SimklAPI.build_show_obj(title=show_title, year=year, imdb=imdb, tmdb=tmdb),
            'episode': {'season': int(season), 'number': int(episode)},
        }

        if action == 'start':
            api.scrobble_start(episode_payload, progress)
        elif action == 'pause':
            api.scrobble_pause(episode_payload, progress)
        else:
            api.scrobble_stop(episode_payload, progress)
            invalidate_tv_indicators()

        if c.devmode:
            c.log(
                f'[Simkl Scrobble] Episode {action} — '
                f'imdb={imdb} S{int(season):02d}E{int(episode):02d} @ {progress:.0f}%',
                1,
            )

    except Exception as e:
        c.log(f'[Simkl Scrobble] simkl_scrobble_episode error: {e}', 1)


# -------------------------------------------------------------------
# Personal library lists (navigator My Movies / My TV)
# -------------------------------------------------------------------

def _api():
    from ..apis.simkl_api import SimklAPI
    return SimklAPI()


def _norm_imdb(raw) -> str:
    """Normalize to tt#######, or '0' when missing/invalid (never tt0)."""
    if raw is None:
        return '0'
    s = str(raw).strip()
    if not s or s.lower() in ('0', 'none', 'null', 'tt', 'tt0'):
        return '0'
    digits = re.sub(r'[^0-9]', '', s)
    if not digits or int(digits) == 0:
        return '0'
    return f'tt{digits}'


def _norm_id(raw) -> str:
    if raw is None or raw == '':
        return '0'
    return str(raw)


def _poster_url(path) -> str:
    """Simkl returns a path fragment; build a displayable poster URL."""
    if not path:
        return '0'
    path = str(path).strip().lstrip('/')
    if path.startswith('http'):
        return path
    # Preferred CDN proxy per Simkl image conventions
    return f'https://wsrv.nl/?url=https://simkl.in/posters/{path}_m.webp&q=90'


def _fanart_url(path) -> str:
    """Simkl fanart path → displayable background URL (or '0' if absent)."""
    if not path:
        return '0'
    path = str(path).strip().lstrip('/')
    if path.startswith('http'):
        return path
    # _medium = 1920x1080; good Kodi fanart without an extra API hop
    return f'https://wsrv.nl/?url=https://simkl.in/fanart/{path}_medium.webp&q=90'


def _extract_entries(payload, media_type: str) -> list:
    """Normalize Simkl all-items response to a flat list of entries."""
    if not payload:
        return []
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    key = 'movies' if media_type == 'movies' else 'shows'
    items = payload.get(key)
    if isinstance(items, list):
        return items
    # Empty bucket often returns {}; some responses use anime/tv aliases
    for alt in (key, 'tv', 'anime', 'all'):
        block = payload.get(alt)
        if isinstance(block, list):
            return block
    # Single-key wrapper: {"movies": [...]} already handled; try first list value
    for value in payload.values():
        if isinstance(value, list):
            return value
    return []


def fetch_status_items(media_type: str, status: str) -> list:
    """
    GET /sync/all-items/{movies|shows}/{status} (phase-1 full pull).

    media_type: 'movies' or 'shows'
    status: plantowatch | watching | completed | hold | dropped

    Gated by POST /sync/activities: when movies.all / tv_shows.all is
    unchanged, reuse the cached bucket instead of hitting all-items again.
    Uses Simkl default summary (not extended=full) so watching lists stay
    small — full mode embeds seasons/episodes and can stall Kodi on big libs.
    """
    if not get_simkl_credentials_info():
        return []
    try:
        from . import cache
        stamp = _remote_activity_stamp(media_type) or 'unknown'
        entries = cache.get(_fetch_status_items_uncached, 168, media_type, status, stamp)
        if not isinstance(entries, list):
            entries = []
        c.log(
            f'[Simkl] fetch_status_items {media_type}/{status}: '
            f'{len(entries)} item(s) (act={stamp[:19]})',
            1,
        )
        return entries
    except Exception as e:
        c.log(f'[Simkl] fetch_status_items error ({media_type}/{status}): {e}', 1)
        return []


def _fetch_activities_uncached() -> dict:
    try:
        data = _api().get_last_activities()
        return data if isinstance(data, dict) else {}
    except Exception as e:
        c.log(f'[Simkl] get_last_activities error: {e}', 1)
        return {}


def _remote_activity_stamp(media_type: str) -> str:
    """
    Master stamp for movies or shows from /sync/activities.

    Cached ~20 minutes so list opens do not spam activities.
    media_type: 'movies' | 'shows'
    """
    from . import cache
    try:
        acts = cache.get(_fetch_activities_uncached, 0.33) or {}
    except Exception:
        acts = _fetch_activities_uncached()
    if not isinstance(acts, dict):
        return ''
    if media_type == 'movies':
        block = acts.get('movies') if isinstance(acts.get('movies'), dict) else {}
        stamp = block.get('all') or acts.get('all') or ''
    else:
        block = acts.get('tv_shows') if isinstance(acts.get('tv_shows'), dict) else {}
        stamp = block.get('all') or acts.get('all') or ''
    return str(stamp or '')


def _fetch_status_items_uncached(media_type: str, status: str, stamp: str) -> list:
    data = _api().get_items_by_status(
        media_type=media_type,
        status=status,
        extended=None,
        phase1_initial=True,
    )
    entries = _extract_entries(data, media_type)
    # Persist stamp so other helpers can tell "library moved"
    try:
        if stamp and stamp != 'unknown':
            c.set_setting(f'simkl.act.{media_type}', stamp)
    except Exception:
        pass
    return entries


def crew_movie_items(status: str = 'plantowatch') -> list:
    """Midnight movies.py list dicts for a Simkl status bucket."""
    out = []
    for entry in fetch_status_items('movies', status):
        try:
            movie = entry.get('movie') if isinstance(entry, dict) else None
            if not isinstance(movie, dict):
                movie = entry if isinstance(entry, dict) else {}
            ids = movie.get('ids') or {}
            title = (movie.get('title') or '').strip()
            if not title:
                continue
            year = re.sub(r'[^0-9]', '', str(movie.get('year') or '0')) or '0'
            imdb = _norm_imdb(ids.get('imdb'))
            tmdb = _norm_id(ids.get('tmdb'))
            out.append({
                'title': title,
                'originaltitle': title,
                'year': year,
                'progress': 0,
                'premiered': '0',
                'genre': '0',
                'duration': str(movie.get('runtime') or '90'),
                'rating': '0.0',
                'votes': '0',
                'mpaa': '0',
                'plot': movie.get('overview') or '',
                'tagline': '0',
                'imdb': imdb,
                'tmdb': tmdb,
                'country': '0',
                'tvdb': '0',
                'poster': _poster_url(movie.get('poster')),
                'fanart': _fanart_url(movie.get('fanart')),
                'next': '',
                'paused_at': '0',
            })
        except Exception as e:
            c.log(f'[Simkl] crew_movie_items skip: {e}', 1)
    return out


def crew_show_items(status: str = 'plantowatch') -> list:
    """Midnight tvshows.py list dicts for a Simkl status bucket."""
    out = []
    for entry in fetch_status_items('shows', status):
        try:
            show = entry.get('show') if isinstance(entry, dict) else None
            if not isinstance(show, dict):
                show = entry if isinstance(entry, dict) else {}
            ids = show.get('ids') or {}
            title = (show.get('title') or '').strip()
            if not title:
                continue
            year = re.sub(r'[^0-9]', '', str(show.get('year') or '0')) or '0'
            imdb = _norm_imdb(ids.get('imdb'))
            tmdb = _norm_id(ids.get('tmdb'))
            tvdb = _norm_id(ids.get('tvdb'))
            # Prefer Simkl summary counts; seasons array only present with extended=full
            ep_total = entry.get('total_episodes_count') if isinstance(entry, dict) else None
            seasons_block = entry.get('seasons') if isinstance(entry, dict) else None
            season_n = 0
            if isinstance(seasons_block, list):
                season_n = sum(1 for s in seasons_block if isinstance(s, dict) and int(s.get('number') or 0) > 0)
            out.append({
                'title': title,
                'year': year,
                'imdb': imdb,
                'tmdb': tmdb,
                'tvdb': tvdb,
                'trakt': '0',
                'slug': ids.get('slug') or '',
                'poster': _poster_url(show.get('poster')),
                'fanart': _fanart_url(show.get('fanart')),
                'seasons': str(season_n) if season_n else '0',
                'episodes': str(ep_total) if ep_total not in (None, '') else '0',
            })
        except Exception as e:
            c.log(f'[Simkl] crew_show_items skip: {e}', 1)
    return out


# -------------------------------------------------------------------
# Ratings (GET /sync/ratings)
# -------------------------------------------------------------------

def _ratings_activity_stamp(media_type: str) -> str:
    """movies.rated_at / tv_shows.rated_at (fallback to .all)."""
    from . import cache
    try:
        acts = cache.get(_fetch_activities_uncached, 0.33) or {}
    except Exception:
        acts = _fetch_activities_uncached()
    if not isinstance(acts, dict):
        return ''
    key = 'movies' if media_type == 'movies' else 'tv_shows'
    block = acts.get(key) if isinstance(acts.get(key), dict) else {}
    stamp = block.get('rated_at') or block.get('all') or acts.get('all') or ''
    return str(stamp or '')


def _fetch_ratings_uncached(media_type: str, stamp: str) -> list:
    data = _api().get_ratings(media_type=media_type)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in (media_type, 'movies', 'shows', 'tv', 'items'):
            block = data.get(key)
            if isinstance(block, list):
                return block
    return []


def _crew_rated_items(media_type: str) -> list:
    """Raw Simkl rating rows → Midnight movie/show list dicts (+ user_rating)."""
    if not get_simkl_credentials_info():
        return []
    from . import cache
    stamp = _ratings_activity_stamp(media_type) or 'unknown'
    try:
        rows = cache.get(_fetch_ratings_uncached, 168, media_type, stamp) or []
    except Exception as e:
        c.log(f'[Simkl] ratings fetch error ({media_type}): {e}', 1)
        rows = []
    if not isinstance(rows, list):
        return []

    out = []
    for entry in rows:
        try:
            if not isinstance(entry, dict):
                continue
            rating = entry.get('rating') or entry.get('user_rating') or 0
            try:
                rating = int(rating)
            except (TypeError, ValueError):
                rating = 0

            if media_type == 'movies':
                movie = entry.get('movie') if isinstance(entry.get('movie'), dict) else entry
                ids = movie.get('ids') or {}
                title = (movie.get('title') or '').strip()
                if not title:
                    continue
                year = re.sub(r'[^0-9]', '', str(movie.get('year') or '0')) or '0'
                out.append({
                    'title': title,
                    'originaltitle': title,
                    'year': year,
                    'progress': 0,
                    'premiered': '0',
                    'genre': '0',
                    'duration': str(movie.get('runtime') or '90'),
                    'rating': '0.0',
                    'votes': '0',
                    'mpaa': '0',
                    'plot': movie.get('overview') or '',
                    'tagline': '0',
                    'imdb': _norm_imdb(ids.get('imdb')),
                    'tmdb': _norm_id(ids.get('tmdb')),
                    'country': '0',
                    'tvdb': '0',
                    'poster': _poster_url(movie.get('poster')),
                    'fanart': _fanart_url(movie.get('fanart')),
                    'next': '',
                    'paused_at': '0',
                    'user_rating': rating,
                })
            else:
                show = entry.get('show') if isinstance(entry.get('show'), dict) else entry
                ids = show.get('ids') or {}
                title = (show.get('title') or '').strip()
                if not title:
                    continue
                year = re.sub(r'[^0-9]', '', str(show.get('year') or '0')) or '0'
                out.append({
                    'title': title,
                    'year': year,
                    'imdb': _norm_imdb(ids.get('imdb')),
                    'tmdb': _norm_id(ids.get('tmdb')),
                    'tvdb': _norm_id(ids.get('tvdb')),
                    'trakt': '0',
                    'slug': ids.get('slug') or '',
                    'poster': _poster_url(show.get('poster')),
                    'fanart': _fanart_url(show.get('fanart')),
                    'seasons': '0',
                    'episodes': '0',
                    'user_rating': rating,
                })
        except Exception as e:
            c.log(f'[Simkl] rated item skip: {e}', 1)
    c.log(f'[Simkl] ratings {media_type}: {len(out)} item(s)', 1)
    return out


def crew_rated_movies() -> list:
    return _crew_rated_items('movies')


def crew_rated_shows() -> list:
    return _crew_rated_items('shows')


# -------------------------------------------------------------------
# History mark / unmark (CM playcount → Simkl, not Trakt)
# -------------------------------------------------------------------

def _invalidate_activities_cache():
    try:
        from . import cache
        key = cache._hash_function(_fetch_activities_uncached)
        cache.cache_delete(key)
    except Exception:
        pass


def _history_write_ok(resp, kind: str = 'episode') -> bool:
    """
    True when POST /sync/history[/remove] did not hard-fail.

    Empty added counts still count as OK (already watched / no-op).
    Fail only on None response or a not_found hit with nothing applied.
    """
    if resp is None:
        return False
    if not isinstance(resp, dict):
        return True
    not_found = resp.get('not_found') if isinstance(resp.get('not_found'), dict) else {}
    added = (
        resp.get('added')
        or resp.get('deleted')
        or resp.get('removed')
        or {}
    )
    if not isinstance(added, dict):
        added = {}

    def _n(key):
        try:
            return int(added.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    applied = _n('movies') + _n('shows') + _n('episodes')
    if kind == 'movie':
        nf = not_found.get('movies') or []
        return applied > 0 or not nf
    nf = (not_found.get('shows') or []) or (not_found.get('episodes') or [])
    return applied > 0 or not nf


def mark_movie_history(imdb: str = '', tmdb: str = '', watched: bool = True) -> bool:
    """POST /sync/history or /sync/history/remove for a movie."""
    if not get_simkl_credentials_info():
        return False
    try:
        from ..apis.simkl_api import SimklAPI
        imdb_n, tmdb_n, _tvdb = _ids_for_history(imdb=imdb, tmdb=tmdb, media='movie')
        tmdb_int = int(tmdb_n) if tmdb_n not in ('0', '') else 0
        movie = SimklAPI.build_movie_obj(
            imdb=imdb_n if imdb_n != '0' else '',
            tmdb=tmdb_int,
        )
        if not movie.get('ids'):
            c.log('[Simkl] mark_movie_history: no ids', 1)
            return False
        if tmdb_n not in ('0', '') and isinstance(movie.get('ids'), dict):
            movie['ids']['tmdb'] = tmdb_n
        api = _api()
        resp = (
            api.add_to_history(movies=[movie])
            if watched else
            api.remove_from_history(movies=[movie])
        )
        ok = _history_write_ok(resp, 'movie')
        c.log(
            f'[Simkl] mark_movie_history watched={watched} ok={ok} '
            f'imdb={imdb_n} tmdb={tmdb_n} resp={resp}',
            1,
        )
        if ok:
            _invalidate_activities_cache()
            invalidate_movie_indicators()
        return ok
    except Exception as e:
        c.log(f'[Simkl] mark_movie_history error: {e}', 1)
        return False


def mark_episode_history(imdb: str = '', tmdb: str = '', season: int = 0,
                         episode: int = 0, watched: bool = True,
                         title: str = '', year: int = 0):
    """
    Mark/unmark a single episode in Simkl history.

    Returns:
        (ok: bool, detail: str) — detail set on failure for UI toast.
    """
    if not get_simkl_credentials_info():
        return False, 'Simkl not authorized'
    try:
        from ..apis.simkl_api import SimklAPI
        imdb_n, tmdb_n, tvdb_n = _ids_for_history(imdb=imdb, tmdb=tmdb, media='tv')
        tmdb_int = int(tmdb_n) if tmdb_n not in ('0', '') else 0
        tvdb_int = int(tvdb_n) if tvdb_n not in ('0', '') else 0
        season_n = int(season)
        episode_n = int(episode)
        show = SimklAPI.build_show_obj(
            title=title or '',
            year=int(year) if year else 0,
            imdb=imdb_n if imdb_n != '0' else '',
            tmdb=tmdb_int,
            tvdb=tvdb_int,
            seasons=[{
                'number': season_n,
                'episodes': [{'number': episode_n}],
            }],
        )
        if not show.get('ids') and not show.get('title'):
            c.log('[Simkl] mark_episode_history: no ids/title', 1)
            return False, 'Missing IDs'
        # Prefer string tmdb — matches Simkl search/history examples
        if isinstance(show.get('ids'), dict):
            if tmdb_n not in ('0', ''):
                show['ids']['tmdb'] = tmdb_n
            if tvdb_n not in ('0', ''):
                show['ids']['tvdb'] = tvdb_n
        api = _api()
        resp = (
            api.add_to_history(shows=[show])
            if watched else
            api.remove_from_history(shows=[show])
        )
        ok = _history_write_ok(resp, 'episode')
        c.log(
            f'[Simkl] mark_episode_history watched={watched} ok={ok} '
            f'{imdb_n}/{tmdb_n}/{tvdb_n} S{season_n:02d}E{episode_n:02d} resp={resp}',
            1,
        )
        if ok:
            _invalidate_activities_cache()
            invalidate_tv_indicators()
            return True, ''
        # Specific toast when Simkl has no catalog match
        not_found = (resp or {}).get('not_found') if isinstance(resp, dict) else {}
        if isinstance(not_found, dict) and (not_found.get('shows') or not_found.get('episodes')):
            return False, 'Not in Simkl catalog'
        return False, 'Failed to update Simkl'
    except Exception as e:
        c.log(f'[Simkl] mark_episode_history error: {e}', 1)
        return False, 'Simkl error'


def mark_season_history(imdb: str = '', tmdb: str = '', season: int = 0,
                        watched: bool = True) -> bool:
    """Mark/unmark a whole season (all eps) via show seasons payload."""
    if not get_simkl_credentials_info():
        return False
    try:
        from ..apis.simkl_api import SimklAPI
        imdb_n, tmdb_n, tvdb_n = _ids_for_history(imdb=imdb, tmdb=tmdb, media='tv')
        tmdb_int = int(tmdb_n) if tmdb_n not in ('0', '') else 0
        tvdb_int = int(tvdb_n) if tvdb_n not in ('0', '') else 0
        show = SimklAPI.build_show_obj(
            imdb=imdb_n if imdb_n != '0' else '',
            tmdb=tmdb_int,
            tvdb=tvdb_int,
            seasons=[{'number': int(season)}],
        )
        if not show.get('ids'):
            return False
        if isinstance(show.get('ids'), dict):
            if tmdb_n not in ('0', ''):
                show['ids']['tmdb'] = tmdb_n
            if tvdb_n not in ('0', ''):
                show['ids']['tvdb'] = tvdb_n
        api = _api()
        resp = (
            api.add_to_history(shows=[show])
            if watched else
            api.remove_from_history(shows=[show])
        )
        ok = _history_write_ok(resp, 'season')
        c.log(
            f'[Simkl] mark_season_history watched={watched} ok={ok} '
            f'{imdb_n}/{tmdb_n} S{int(season):02d} resp={resp}',
            1,
        )
        if ok:
            _invalidate_activities_cache()
            invalidate_tv_indicators()
        return ok
    except Exception as e:
        c.log(f'[Simkl] mark_season_history error: {e}', 1)
        return False


def mark_show_history(imdb: str = '', tmdb: str = '', watched: bool = True) -> bool:
    """Mark/unmark an entire show in Simkl history."""
    if not get_simkl_credentials_info():
        return False
    try:
        from ..apis.simkl_api import SimklAPI
        imdb_n, tmdb_n, tvdb_n = _ids_for_history(imdb=imdb, tmdb=tmdb, media='tv')
        tmdb_int = int(tmdb_n) if tmdb_n not in ('0', '') else 0
        tvdb_int = int(tvdb_n) if tvdb_n not in ('0', '') else 0
        show = SimklAPI.build_show_obj(
            imdb=imdb_n if imdb_n != '0' else '',
            tmdb=tmdb_int,
            tvdb=tvdb_int,
        )
        if not show.get('ids'):
            return False
        if isinstance(show.get('ids'), dict):
            if tmdb_n not in ('0', ''):
                show['ids']['tmdb'] = tmdb_n
            if tvdb_n not in ('0', ''):
                show['ids']['tvdb'] = tvdb_n
        api = _api()
        resp = (
            api.add_to_history(shows=[show])
            if watched else
            api.remove_from_history(shows=[show])
        )
        ok = _history_write_ok(resp, 'show')
        c.log(
            f'[Simkl] mark_show_history watched={watched} ok={ok} '
            f'{imdb_n}/{tmdb_n} resp={resp}',
            1,
        )
        if ok:
            _invalidate_activities_cache()
            invalidate_tv_indicators()
        return ok
    except Exception as e:
        c.log(f'[Simkl] mark_show_history error: {e}', 1)
        return False


# -------------------------------------------------------------------
# TV playcount indicators (Midnight shape, same as Trakt syncTVShows)
#   (tmdb_id_str, aired_episode_count, [(season, episode), ...])
# -------------------------------------------------------------------

def _sync_tv_indicators_bundle():
    """
    Pull watching + completed shows with per-episode watched_at and map to
    Midnight indicator tuples + fully-watched season lists keyed by imdb/tmdb.
    """
    if not get_simkl_credentials_info():
        return {'indicators': [], 'seasons_by_imdb': {}, 'seasons_by_tmdb': {}}

    indicators = []
    seasons_by_imdb = {}
    seasons_by_tmdb = {}
    seen_tmdb = set()

    try:
        api = _api()
        for status in ('watching', 'completed'):
            # completed/dropped omit seasons[].episodes[] unless include_all_episodes.
            # watching arrays are still watched-only (sparse) — never treat "all
            # listed eps watched" as season-complete (that false-marks SxE01 → season).
            data = api.get_items_by_status(
                media_type='shows',
                status=status,
                extended='full',
                phase1_initial=True,
                episode_watched_at=True,
                include_all_episodes=(status == 'completed'),
            )
            for entry in _extract_entries(data, 'shows'):
                try:
                    if not isinstance(entry, dict):
                        continue
                    show = entry.get('show') if isinstance(entry.get('show'), dict) else {}
                    ids = show.get('ids') or {}
                    tmdb = _norm_id(ids.get('tmdb'))
                    if tmdb == '0' or tmdb in seen_tmdb:
                        continue
                    seen_tmdb.add(tmdb)
                    imdb = _norm_imdb(ids.get('imdb'))

                    seasons_block = entry.get('seasons') or []
                    watched_eps = []
                    # Season-complete overlays for Simkl are computed at display
                    # time against TMDB episode_count (see playcount /
                    # season_directory). Keep these maps empty.
                    complete_seasons = []
                    if isinstance(seasons_block, list):
                        for season in seasons_block:
                            if not isinstance(season, dict):
                                continue
                            sn = int(season.get('number') or 0)
                            if sn <= 0:
                                continue
                            eps = season.get('episodes') or []
                            if not isinstance(eps, list) or not eps:
                                continue
                            for ep in eps:
                                if not isinstance(ep, dict):
                                    continue
                                en = int(ep.get('number') or 0)
                                if en <= 0:
                                    continue
                                # Sparse watching lists: every row is watched.
                                # Completed+include_all_episodes may include
                                # unwatched rows — only count watched_at.
                                if status == 'watching' or ep.get('watched_at'):
                                    watched_eps.append((sn, en))

                    aired = int(entry.get('total_episodes_count') or 0)
                    if aired <= 0:
                        aired = len(watched_eps)

                    indicators.append((tmdb, aired, watched_eps))
                    if imdb and imdb != '0':
                        seasons_by_imdb[imdb] = complete_seasons
                    seasons_by_tmdb[tmdb] = complete_seasons
                except Exception as e:
                    c.log(f'[Simkl] indicator entry skip: {e}', 1)

        c.log(
            f'[Simkl] sync_tv_indicators: {len(indicators)} show(s) with watched maps',
            1,
        )
    except Exception as e:
        c.log(f'[Simkl] sync_tv_indicators error: {e}', 1)

    return {
        'indicators': indicators,
        'seasons_by_imdb': seasons_by_imdb,
        'seasons_by_tmdb': seasons_by_tmdb,
    }


def cachesync_tv_indicators(timeout=24):
    """
    Cached Simkl → Midnight TV indicators list.
    timeout=None forces a fresh pull and rewrites the cache so season maps stay in sync.
    """
    from . import cache
    import json
    try:
        if timeout is None:
            bundle = _sync_tv_indicators_bundle() or {}
            try:
                if bundle.get('indicators'):
                    key = cache._hash_function(_sync_tv_indicators_bundle)
                    cache.cache_insert(key, json.dumps(bundle))
            except Exception:
                pass
            return bundle.get('indicators') or []
        bundle = cache.get(_sync_tv_indicators_bundle, timeout) or {}
        return bundle.get('indicators') or []
    except Exception as e:
        c.log(f'[Simkl] cachesync_tv_indicators error: {e}', 1)
        return []


def sync_season(imdb: str = '', tmdb: str = ''):
    """Season numbers that are fully watched (Midnight getSeasonIndicators shape)."""
    from . import cache
    try:
        bundle = cache.get(_sync_tv_indicators_bundle, 24) or {}
        imdb_n = _norm_imdb(imdb) if imdb else '0'
        tmdb_n = _norm_id(tmdb) if tmdb else '0'
        by_imdb = bundle.get('seasons_by_imdb') or {}
        by_tmdb = bundle.get('seasons_by_tmdb') or {}
        if imdb_n != '0' and imdb_n in by_imdb:
            return by_imdb[imdb_n]
        if tmdb_n != '0' and tmdb_n in by_tmdb:
            return by_tmdb[tmdb_n]
        return []
    except Exception as e:
        c.log(f'[Simkl] sync_season error: {e}', 1)
        return []


def invalidate_tv_indicators():
    """Drop cached Simkl TV indicator bundle (call after scrobble/mark)."""
    try:
        from . import cache
        key = cache._hash_function(_sync_tv_indicators_bundle)
        cache.cache_delete(key)
    except Exception as e:
        c.log(f'[Simkl] invalidate_tv_indicators error: {e}', 1)
    invalidate_next_episode_sessions()


def _sync_movie_indicators():
    """Completed Simkl movies → IMDB id list for playcount overlays."""
    if not get_simkl_credentials_info():
        return []
    out = []
    seen = set()
    try:
        for entry in fetch_status_items('movies', 'completed'):
            try:
                movie = entry.get('movie') if isinstance(entry, dict) else None
                if not isinstance(movie, dict):
                    movie = entry if isinstance(entry, dict) else {}
                ids = movie.get('ids') or {}
                imdb = _norm_imdb(ids.get('imdb'))
                if imdb == '0' or imdb in seen:
                    continue
                seen.add(imdb)
                out.append(imdb)
            except Exception:
                continue
        c.log(f'[Simkl] sync_movie_indicators: {len(out)} movie(s)', 1)
    except Exception as e:
        c.log(f'[Simkl] sync_movie_indicators error: {e}', 1)
    return out


def cachesync_movie_indicators(timeout=24):
    from . import cache
    try:
        if timeout is None:
            return _sync_movie_indicators() or []
        return cache.get(_sync_movie_indicators, timeout) or []
    except Exception as e:
        c.log(f'[Simkl] cachesync_movie_indicators error: {e}', 1)
        return _sync_movie_indicators() or []


def invalidate_movie_indicators():
    try:
        from . import cache
        key = cache._hash_function(_sync_movie_indicators)
        cache.cache_delete(key)
    except Exception as e:
        c.log(f'[Simkl] invalidate_movie_indicators error: {e}', 1)


# -------------------------------------------------------------------
# Continue Watching — GET /sync/playback[/movies|/episodes]
# Progress is percentage 0–100 (same as scrobble). Sessions exist for
# pause/stop with progress < 80% (Simkl retention by plan).
# -------------------------------------------------------------------

def fetch_playback(media_type: str = '') -> list:
    """
    Raw playback sessions from Simkl.

    media_type: 'movies' | 'episodes' | '' (all)
    """
    if not get_simkl_credentials_info():
        return []
    try:
        data = _api().get_playback(media_type=media_type) or []
        if not isinstance(data, list):
            return []
        c.log(f'[Simkl] fetch_playback {media_type or "all"}: {len(data)} session(s)', 1)
        return data
    except Exception as e:
        c.log(f'[Simkl] fetch_playback error ({media_type}): {e}', 1)
        return []


def find_playback_id(media_type: str, imdb: str = '', tmdb: str = '',
                     season=None, episode=None):
    """
    Resolve Simkl /sync/playback session id for clear-resume.

    media_type: 'movie' | 'episode'
    """
    try:
        mt = 'movies' if media_type == 'movie' else 'episodes'
        want_imdb = _norm_imdb(imdb) if imdb else '0'
        want_tmdb = _norm_id(tmdb) if tmdb else '0'
        want_s = int(season) if season not in (None, '', 'None') else None
        want_e = int(episode) if episode not in (None, '', 'None') else None
    except (TypeError, ValueError):
        return None

    for item in fetch_playback(mt):
        try:
            if not isinstance(item, dict):
                continue
            if media_type == 'movie':
                movie = item.get('movie') if isinstance(item.get('movie'), dict) else {}
                ids = movie.get('ids') or {}
                mid = _norm_imdb(ids.get('imdb'))
                tid = _norm_id(ids.get('tmdb'))
                if (want_imdb != '0' and mid == want_imdb) or (want_tmdb != '0' and tid == want_tmdb):
                    return item.get('id')
            else:
                show = item.get('show') if isinstance(item.get('show'), dict) else {}
                ep = item.get('episode') if isinstance(item.get('episode'), dict) else {}
                ids = show.get('ids') or {}
                mid = _norm_imdb(ids.get('imdb'))
                tid = _norm_id(ids.get('tmdb'))
                s = ep.get('season')
                n = ep.get('number') if ep.get('number') is not None else ep.get('episode')
                try:
                    s, n = int(s), int(n)
                except (TypeError, ValueError):
                    continue
                id_ok = (want_imdb != '0' and mid == want_imdb) or (want_tmdb != '0' and tid == want_tmdb)
                se_ok = want_s is None or want_e is None or (s == want_s and n == want_e)
                if id_ok and se_ok:
                    return item.get('id')
        except Exception:
            continue
    return None


def delete_playback(playback_id) -> bool:
    """DELETE /sync/playback/{id}."""
    if not playback_id:
        return False
    try:
        ok = bool(_api().delete_playback(int(playback_id)))
        c.log(f'[Simkl] delete_playback {playback_id}: {ok}', 1)
        return ok
    except Exception as e:
        c.log(f'[Simkl] delete_playback error: {e}', 1)
        return False


def clear_playback_session(media_type: str, imdb: str = '', tmdb: str = '',
                           season=None, episode=None) -> bool:
    """Find + delete a Simkl resume session (clear resume CM)."""
    pid = find_playback_id(media_type, imdb=imdb, tmdb=tmdb, season=season, episode=episode)
    if not pid:
        c.log('[Simkl] clear_playback_session: no matching playback id', 1)
        return False
    return delete_playback(pid)


# -------------------------------------------------------------------
# Calendar — CDN v2 TV + movie schedules ∩ personal lists (MVP)
# -------------------------------------------------------------------

_CALENDAR_CDN = 'https://data.simkl.in/calendar/v2/tv.json'
_MOVIE_CALENDAR_CDN = 'https://data.simkl.in/calendar/v2/movie_release.json'


def _fetch_calendar_cdn_uncached(url: str) -> dict:
    """Pull a Simkl CDN calendar file (no user token)."""
    import requests
    params = _required_params()
    headers = {'User-Agent': _user_agent(), 'Accept': 'application/json'}
    r = requests.get(url, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        return {'calendar': [], 'metadata': {}}
    return {
        'calendar': data.get('calendar') if isinstance(data.get('calendar'), list) else [],
        'metadata': data.get('metadata') if isinstance(data.get('metadata'), dict) else {},
    }


def _fetch_tv_calendar_cdn_uncached() -> dict:
    return _fetch_calendar_cdn_uncached(_CALENDAR_CDN)


def _fetch_movie_calendar_cdn_uncached() -> dict:
    return _fetch_calendar_cdn_uncached(_MOVIE_CALENDAR_CDN)


def fetch_tv_calendar_cdn(timeout_hours: float = 3) -> dict:
    from . import cache
    try:
        return cache.get(_fetch_tv_calendar_cdn_uncached, timeout_hours) or {
            'calendar': [], 'metadata': {}
        }
    except Exception as e:
        c.log(f'[Simkl] fetch_tv_calendar_cdn error: {e}', 1)
        return _fetch_tv_calendar_cdn_uncached()


def fetch_movie_calendar_cdn(timeout_hours: float = 3) -> dict:
    from . import cache
    try:
        return cache.get(_fetch_movie_calendar_cdn_uncached, timeout_hours) or {
            'calendar': [], 'metadata': {}
        }
    except Exception as e:
        c.log(f'[Simkl] fetch_movie_calendar_cdn error: {e}', 1)
        return _fetch_movie_calendar_cdn_uncached()


def _my_show_id_keys() -> set:
    """
    TMDB + IMDB keys for watching + plantowatch shows (My Calendar filter).
    """
    keys = set()
    for status in ('watching', 'plantowatch'):
        for entry in fetch_status_items('shows', status):
            try:
                show = entry.get('show') if isinstance(entry, dict) else None
                if not isinstance(show, dict):
                    show = entry if isinstance(entry, dict) else {}
                ids = show.get('ids') or {}
                tmdb = _norm_id(ids.get('tmdb'))
                imdb = _norm_imdb(ids.get('imdb'))
                if tmdb != '0':
                    keys.add(('tmdb', tmdb))
                if imdb != '0':
                    keys.add(('imdb', imdb))
            except Exception:
                continue
    return keys


def _my_movie_id_keys() -> set:
    """TMDB + IMDB keys for plantowatch movies (optional My filter)."""
    keys = set()
    for entry in fetch_status_items('movies', 'plantowatch'):
        try:
            movie = entry.get('movie') if isinstance(entry, dict) else None
            if not isinstance(movie, dict):
                movie = entry if isinstance(entry, dict) else {}
            ids = movie.get('ids') or {}
            tmdb = _norm_id(ids.get('tmdb'))
            imdb = _norm_imdb(ids.get('imdb'))
            if tmdb != '0':
                keys.add(('tmdb', tmdb))
            if imdb != '0':
                keys.add(('imdb', imdb))
        except Exception:
            continue
    return keys


def _calendar_date_window(window: str):
    """Return (today, predicate(air_local_date) -> bool)."""
    import datetime as _dt
    today = _dt.date.today()
    day_6 = today + _dt.timedelta(days=6)
    day_29 = today + _dt.timedelta(days=29)

    if window == 'today':
        return today, lambda d: d == today
    if window == 'week':
        return today, lambda d: today <= d <= day_6
    if window == 'month':
        return today, lambda d: today <= d <= day_29
    if window == 'nextweek':
        day_7 = today + _dt.timedelta(days=7)
        day_14 = today + _dt.timedelta(days=14)
        return today, lambda d: day_7 < d <= day_14
    # my / all in CDN rolling window
    return today, lambda d: True


def crew_calendar_episodes(window: str = 'week') -> list:
    """
    Slim episode dicts for Episodes.episodeDirectory / worker enrich.

    window: today | week | nextweek | my
    """
    import datetime as _dt

    data = fetch_tv_calendar_cdn()
    calendar = data.get('calendar') or []
    metadata = data.get('metadata') or {}
    my_keys = _my_show_id_keys() if window in ('today', 'week', 'nextweek', 'my') else set()
    if window in ('today', 'week', 'nextweek', 'my') and not my_keys:
        c.log('[Simkl] calendar: no watching/plantowatch shows — empty', 1)
        return []

    _, in_window = _calendar_date_window(window)

    out = []
    for entry in calendar:
        try:
            if not isinstance(entry, dict):
                continue
            ep = entry.get('episode') if isinstance(entry.get('episode'), dict) else {}
            if not ep:
                continue
            sid = entry.get('simkl_id')
            show = metadata.get(str(sid)) if sid is not None else None
            if not isinstance(show, dict):
                continue
            ids = show.get('ids') or {}
            tmdb = _norm_id(ids.get('tmdb'))
            imdb = _norm_imdb(ids.get('imdb'))
            tvdb = _norm_id(ids.get('tvdb'))
            if my_keys and (('tmdb', tmdb) not in my_keys) and (('imdb', imdb) not in my_keys):
                continue

            raw_date = entry.get('date') or ''
            try:
                air_dt = _dt.datetime.fromisoformat(str(raw_date).replace('Z', '+00:00'))
                air_local = air_dt.astimezone().date() if air_dt.tzinfo else air_dt.date()
            except Exception:
                continue
            if not in_window(air_local):
                continue

            try:
                season = int(ep.get('season') or 0)
                episode = int(ep.get('episode') or 0)
            except (TypeError, ValueError):
                continue
            if season < 0 or episode < 1:
                continue

            title = (ep.get('title') or '').strip() or f'Episode {episode}'
            year = '0'
            rel = show.get('release_date') or ''
            if isinstance(rel, str) and len(rel) >= 4 and rel[:4].isdigit():
                year = rel[:4]

            out.append({
                'tmdb': tmdb,
                'imdb': imdb,
                'tvdb': tvdb,
                'tvshowtitle': (show.get('title') or '').strip(),
                'title': title,
                'season': season,
                'episode': episode,
                'premiered': air_local.strftime('%Y-%m-%d'),
                'year': year,
                'poster': _poster_url(show.get('poster')),
                'fanart': _fanart_url(show.get('fanart')),
                'metacache': False,
            })
        except Exception:
            continue

    out.sort(key=lambda x: x.get('premiered') or '')
    c.log(f'[Simkl] calendar window={window}: {len(out)} episode(s)', 1)
    return out


def crew_movie_calendar(window: str = 'week', mine_only: bool = False) -> list:
    """
    Movie release calendar from CDN → Midnight movie list dicts.

    window: week | month
    mine_only: if True, intersect plantowatch; else all CDN releases in window.
    """
    import datetime as _dt

    data = fetch_movie_calendar_cdn()
    calendar = data.get('calendar') or []
    metadata = data.get('metadata') or {}
    my_keys = _my_movie_id_keys() if mine_only else set()
    if mine_only and not my_keys:
        return []

    win = 'week' if window == 'week' else 'month'
    _, in_window = _calendar_date_window(win)

    out = []
    seen = set()
    for entry in calendar:
        try:
            if not isinstance(entry, dict):
                continue
            sid = entry.get('simkl_id')
            movie = metadata.get(str(sid)) if sid is not None else None
            if not isinstance(movie, dict):
                continue
            ids = movie.get('ids') or {}
            tmdb = _norm_id(ids.get('tmdb'))
            imdb = _norm_imdb(ids.get('imdb'))
            if mine_only and (('tmdb', tmdb) not in my_keys) and (('imdb', imdb) not in my_keys):
                continue

            # Match TMDB calendars: English originals only (drops hi/ta/ja/ko/etc.)
            lang = (movie.get('original_language') or '').strip().lower()
            if lang and lang != 'en':
                continue

            raw_date = entry.get('date') or entry.get('release_date') or ''
            try:
                if 'T' in str(raw_date):
                    air_dt = _dt.datetime.fromisoformat(str(raw_date).replace('Z', '+00:00'))
                    air_local = air_dt.astimezone().date() if air_dt.tzinfo else air_dt.date()
                else:
                    air_local = _dt.date.fromisoformat(str(raw_date)[:10])
            except Exception:
                continue
            if not in_window(air_local):
                continue

            dedupe = tmdb if tmdb != '0' else imdb
            if dedupe in ('0', '') or dedupe in seen:
                continue
            seen.add(dedupe)

            title = (movie.get('title') or '').strip()
            if not title:
                continue
            year = '0'
            rel = movie.get('release_date') or ''
            if isinstance(rel, str) and len(rel) >= 4 and rel[:4].isdigit():
                year = rel[:4]
            elif air_local:
                year = str(air_local.year)

            out.append({
                'title': title,
                'originaltitle': title,
                'year': year,
                'premiered': air_local.strftime('%Y-%m-%d'),
                'genre': '0',
                'duration': '90',
                'rating': '0.0',
                'votes': '0',
                'mpaa': '0',
                'plot': movie.get('overview') or '',
                'tagline': '0',
                'imdb': imdb,
                'tmdb': tmdb,
                'country': '0',
                'tvdb': '0',
                'poster': _poster_url(movie.get('poster')),
                'fanart': _fanart_url(movie.get('fanart')),
                'progress': 0,
                'next': '',
                'paused_at': '0',
            })
        except Exception:
            continue

    out.sort(key=lambda x: x.get('premiered') or '')
    c.log(f'[Simkl] movie calendar window={win} mine={mine_only}: {len(out)}', 1)
    return out


def _resolve_tmdb_id(ids: dict, media: str = 'movie') -> str:
    """Prefer ids.tmdb; else TMDB find by imdb (cached 24h). media: movie|tv."""
    from . import cache
    from . import http_client
    from . import keys

    ids = ids or {}
    tmdb = _norm_id(ids.get('tmdb'))
    if tmdb != '0':
        return tmdb
    imdb = _norm_imdb(ids.get('imdb'))
    if imdb == '0':
        return '0'
    try:
        find_url = (
            f'https://api.themoviedb.org/3/find/{imdb}'
            f'?api_key={keys.tmdb_key}&external_source=imdb_id'
        )
        found = cache.get(http_client.tmdb_get_json, 24, find_url, 10) or {}
        key = 'movie_results' if media == 'movie' else 'tv_results'
        results = found.get(key) or []
        if results:
            return _norm_id(results[0].get('id'))
    except Exception as e:
        c.log(f'[Simkl] _resolve_tmdb_id({imdb}/{media}) error: {e}', 1)
    return '0'


def _enrich_external_ids(tmdb: str, media: str = 'tv') -> dict:
    """
    TMDB /{movie|tv}/{id}/external_ids → imdb + tvdb (cached 24h).

    Fills gaps when Midnight only has TMDB and Simkl matching needs IMDb/TVDB.
    """
    tmdb = _norm_id(tmdb)
    if tmdb == '0':
        return {'imdb': '0', 'tvdb': '0'}
    from . import cache
    from . import http_client
    from . import keys

    path = 'tv' if media == 'tv' else 'movie'
    url = (
        f'https://api.themoviedb.org/3/{path}/{tmdb}/external_ids'
        f'?api_key={keys.tmdb_key}'
    )
    try:
        data = cache.get(http_client.tmdb_get_json, 24, url, 10) or {}
        if not isinstance(data, dict):
            data = {}
        out = {
            'imdb': _norm_imdb(data.get('imdb_id')),
            'tvdb': _norm_id(data.get('tvdb_id')),
        }
        c.log(
            f'[Simkl] TMDB external_ids {path}/{tmdb}: '
            f'imdb={out["imdb"]} tvdb={out["tvdb"]}',
            1,
        )
        return out
    except Exception as e:
        c.log(f'[Simkl] _enrich_external_ids({tmdb}/{media}) error: {e}', 1)
        return {'imdb': '0', 'tvdb': '0'}


def _ids_for_history(imdb: str = '', tmdb: str = '', tvdb: str = '',
                     media: str = 'tv'):
    """Normalize IDs; pull missing imdb/tvdb from TMDB when only tmdb is known."""
    imdb_n = _norm_imdb(imdb)
    tmdb_n = _norm_id(tmdb)
    tvdb_n = _norm_id(tvdb)
    need_imdb = imdb_n == '0'
    need_tvdb = media == 'tv' and tvdb_n == '0'
    if tmdb_n != '0' and (need_imdb or need_tvdb):
        ext = _enrich_external_ids(tmdb_n, media=media)
        if need_imdb and ext.get('imdb') not in (None, '', '0'):
            imdb_n = ext['imdb']
        if need_tvdb and ext.get('tvdb') not in (None, '', '0'):
            tvdb_n = ext['tvdb']
    return imdb_n, tmdb_n, tvdb_n


def crew_movie_playback_items() -> list:
    """Midnight movie list dicts for Continue Watching (Simkl movie playback)."""
    out = []
    for item in fetch_playback('movies'):
        try:
            if not isinstance(item, dict):
                continue
            progress = float(item.get('progress') or 0)
            # Match Trakt Continue Watching band; Simkl sessions are typically < 80
            if progress <= 0 or progress >= 92:
                continue
            movie = item.get('movie') if isinstance(item.get('movie'), dict) else {}
            ids = movie.get('ids') or {}
            title = (movie.get('title') or '').strip()
            if not title:
                continue
            year = re.sub(r'[^0-9]', '', str(movie.get('year') or '0')) or '0'
            imdb = _norm_imdb(ids.get('imdb'))
            tmdb = _resolve_tmdb_id(ids, media='movie')
            if imdb == '0' and tmdb == '0':
                continue
            out.append({
                'title': title,
                'originaltitle': title,
                'year': year,
                'progress': progress,
                # movie_directory treats 1–100 as percent → seconds
                'resume_point': progress,
                'premiered': '0',
                'genre': '0',
                'duration': str(movie.get('runtime') or '90'),
                'rating': '0.0',
                'votes': '0',
                'mpaa': '0',
                'plot': movie.get('overview') or '',
                'tagline': '0',
                'imdb': imdb,
                'tmdb': tmdb,
                'country': '0',
                'tvdb': '0',
                'poster': _poster_url(movie.get('poster')),
                'fanart': _fanart_url(movie.get('fanart')),
                'next': '',
                'paused_at': item.get('paused_at') or '0',
                'simkl_playback_id': item.get('id'),
            })
        except Exception as e:
            c.log(f'[Simkl] crew_movie_playback_items skip: {e}', 1)
    out.sort(key=lambda k: k.get('paused_at') or '0', reverse=True)
    return out


def crew_episode_playback_sessions() -> list:
    """
    Normalized episode playback sessions for Progress Continue Watching.

    Each item: {
      show_data, episode_data, progress, paused_at, playback_id
    }
    show_data/episode_data shaped for Episode.from_trakt_progress.
    """
    out = []
    for item in fetch_playback('episodes'):
        try:
            if not isinstance(item, dict):
                continue
            progress = float(item.get('progress') or 0)
            if progress <= 0 or progress >= 92:
                continue
            show = item.get('show') if isinstance(item.get('show'), dict) else {}
            ep = item.get('episode') if isinstance(item.get('episode'), dict) else {}
            season = ep.get('season')
            number = ep.get('number') if ep.get('number') is not None else ep.get('episode')
            try:
                season = int(season)
                number = int(number)
            except (TypeError, ValueError):
                continue
            if season < 0 or number < 1:
                continue
            ids = show.get('ids') or {}
            tmdb = _resolve_tmdb_id(ids, media='tv')
            imdb = _norm_imdb(ids.get('imdb'))
            tvdb = _norm_id(ids.get('tvdb'))
            if tmdb == '0' and imdb == '0' and tvdb == '0':
                continue
            show_data = {
                'title': (show.get('title') or '').strip(),
                'year': show.get('year'),
                'ids': {
                    'imdb': imdb if imdb != '0' else None,
                    'tmdb': int(tmdb) if tmdb not in ('0', '') else None,
                    'tvdb': int(tvdb) if tvdb not in ('0', '') else None,
                },
            }
            episode_data = {
                'title': ep.get('title') or f'Episode {number}',
                'season': season,
                'number': number,
                'ids': {},
            }
            out.append({
                'show_data': show_data,
                'episode_data': episode_data,
                'season': season,
                'episode': number,
                'progress': progress,
                'paused_at': item.get('paused_at') or '',
                'playback_id': item.get('id'),
            })
        except Exception as e:
            c.log(f'[Simkl] crew_episode_playback_sessions skip: {e}', 1)
    out.sort(key=lambda k: k.get('paused_at') or '', reverse=True)
    return out


def _parse_simkl_ep_marker(raw) -> tuple:
    """
    Parse Simkl last_watched / next_to_watch / next_to_watch_info.

    Returns (season:int, episode:int, title:str, air_date:str)
    air_date is YYYY-MM-DD or ''.
    """
    if not raw:
        return 0, 0, '', ''
    if isinstance(raw, str):
        # Rare: "S01E05" style
        m = re.search(r'[Ss]?(\d+)\s*[EeXx](\d+)', raw)
        if m:
            return int(m.group(1)), int(m.group(2)), '', ''
        return 0, 0, '', ''
    if not isinstance(raw, dict):
        return 0, 0, '', ''
    season = raw.get('season')
    episode = raw.get('episode')
    if episode is None:
        episode = raw.get('number')
    title = (raw.get('title') or '').strip()
    air_raw = raw.get('date') or raw.get('aired') or raw.get('first_aired') or ''
    air_date = ''
    if air_raw:
        m = re.search(r'(\d{4}-\d{2}-\d{2})', str(air_raw))
        if m:
            air_date = m.group(1)
    try:
        season = int(season) if season is not None else 0
        episode = int(episode) if episode is not None else 0
    except (TypeError, ValueError):
        return 0, 0, title, air_date
    # Anime sometimes omits season — treat as season 1
    if episode > 0 and season <= 0:
        season = 1
    return season, episode, title, air_date


def crew_next_episode_sessions() -> list:
    """
    Next-to-watch episodes for Simkl watching shows.

    Uses GET /sync/all-items/shows/watching?next_watch_info=yes (summary,
    not extended=full). Prefer next_to_watch_info / next_to_watch; fall back
    to last_watched + 1 when Simkl only returns the last marker.

    Skips markers with a known future air date (Simkl often points at the
    next season premiere months early).

    Cached ~15 minutes so pagination / widget reloads do not re-pull the
    full watching library each page.

    Returns list of {
        show_data, season, episode, last_watched_at, episode_title, air_date
    } shaped for Progress.get_next_episodes Phase 0.
    """
    from . import cache
    try:
        return cache.get(_crew_next_episode_sessions_uncached, 0.25) or []
    except Exception as e:
        c.log(f'[Simkl] crew_next_episode_sessions cache error: {e}', 1)
        return _crew_next_episode_sessions_uncached()


def _crew_next_episode_sessions_uncached() -> list:
    if not get_simkl_credentials_info():
        return []
    from . import cleandate
    out = []
    skipped_future = 0
    try:
        data = _api().get_items_by_status(
            media_type='shows',
            status='watching',
            extended=None,
            phase1_initial=True,
            next_watch_info=True,
        )
        entries = _extract_entries(data, 'shows')
        c.log(f'[Simkl] crew_next_episode_sessions: {len(entries)} watching show(s)', 1)
        for entry in entries:
            try:
                if not isinstance(entry, dict):
                    continue
                show = entry.get('show') if isinstance(entry.get('show'), dict) else {}
                ids = show.get('ids') or {}
                title = (show.get('title') or '').strip()
                if not title:
                    continue
                tmdb = _resolve_tmdb_id(ids, media='tv')
                if tmdb == '0':
                    continue
                imdb = _norm_imdb(ids.get('imdb'))
                tvdb = _norm_id(ids.get('tvdb'))

                info = entry.get('next_to_watch_info')
                if not isinstance(info, dict):
                    info = entry.get('next_to_watch')
                season, episode, ep_title, air_date = _parse_simkl_ep_marker(info)

                if season <= 0 or episode <= 0:
                    last = entry.get('last_watched')
                    ls, le, _, _ = _parse_simkl_ep_marker(last)
                    if ls > 0 and le > 0:
                        season, episode = ls, le + 1
                        ep_title = ''
                        air_date = ''

                if season <= 0 or episode <= 0:
                    continue

                # Simkl often returns next season premiere with a future date —
                # do not put those on Next Episodes.
                if cleandate.is_known_future(air_date):
                    skipped_future += 1
                    c.log(
                        f'[Simkl] Skip unaired next: {title} S{season:02d}E{episode:02d} '
                        f'(airs {air_date})',
                        1,
                    )
                    continue

                # last_watched_at only — never next_to_watch.date (that is often air date)
                last_watched_at = entry.get('last_watched_at') or ''

                show_data = {
                    'title': title,
                    'year': show.get('year'),
                    'ids': {
                        'imdb': imdb if imdb != '0' else None,
                        'tmdb': int(tmdb) if tmdb not in ('0', '') else None,
                        'tvdb': int(tvdb) if tvdb not in ('0', '') else None,
                    },
                }
                out.append({
                    'show_data': show_data,
                    'season': season,
                    'episode': episode,
                    'last_watched_at': last_watched_at or '',
                    'episode_title': ep_title,
                    'air_date': air_date,
                })
            except Exception as e:
                c.log(f'[Simkl] crew_next_episode_sessions skip: {e}', 1)
    except Exception as e:
        c.log(f'[Simkl] crew_next_episode_sessions error: {e}', 1)
        return []

    if skipped_future:
        c.log(f'[Simkl] crew_next_episode_sessions dropped {skipped_future} unaired next marker(s)', 1)
    out.sort(key=lambda k: k.get('last_watched_at') or '', reverse=True)
    return out


def invalidate_next_episode_sessions():
    """Drop cached Simkl next-episode list (call after scrobble/mark)."""
    try:
        from . import cache
        key = cache._hash_function(_crew_next_episode_sessions_uncached)
        cache.cache_delete(key)
    except Exception as e:
        c.log(f'[Simkl] invalidate_next_episode_sessions error: {e}', 1)


# -------------------------------------------------------------------
# Simkl Manager (context menu) — status moves only, no collection
# -------------------------------------------------------------------

def _manager_imdb(imdb) -> str:
    s = (imdb or '').strip()
    return '' if s in ('0', 'None', '') else s


def _manager_tmdb(tmdb) -> int:
    try:
        if tmdb and str(tmdb) not in ('0', 'None', ''):
            return int(tmdb)
    except (TypeError, ValueError):
        pass
    return 0


def manager(name: str, imdb: str, tmdb: str, content: str) -> None:
    """
    Context-menu dialog: move a title between Simkl watchlist statuses.

    No Trakt-style collection / custom lists (API has neither for us yet).
    Movies skip watching/hold (Simkl movie library has no those buckets).
    """
    from . import tracker
    from ..apis.simkl_api import SimklAPI

    try:
        if not tracker.simkl_live():
            c.infoDialog(
                'Simkl not connected - authorize in Settings > Accounts > Simkl',
                heading=name or 'Simkl',
                sound=True,
                icon='ERROR',
            )
            return

        imdb_s = _manager_imdb(imdb)
        tmdb_i = _manager_tmdb(tmdb)
        if not imdb_s and not tmdb_i:
            c.infoDialog(
                'No valid ID for this item',
                heading=name or 'Simkl',
                sound=True,
                icon='ERROR',
            )
            return

        is_movie = (content or '').lower() == 'movie'
        title = (name or '').strip()

        # Rate first (parity with Trakt Manager), then status buckets, then remove.
        actions = [
            (c.lang(90261), 'rate'),           # Rate this
            (c.lang(90262), 'remove_rating'),  # Remove Rating
        ]
        if is_movie:
            actions += [
                (c.lang(90315), 'plantowatch'),  # Plan to Watch
                (c.lang(90319), 'completed'),    # Completed
                (c.lang(90318), 'dropped'),      # Dropped
                (c.lang(90320), 'remove'),       # Remove from list
            ]
        else:
            actions += [
                (c.lang(90315), 'plantowatch'),  # Plan to Watch
                (c.lang(90316), 'watching'),     # Watching
                (c.lang(90317), 'hold'),         # On Hold
                (c.lang(90318), 'dropped'),      # Dropped
                (c.lang(90319), 'completed'),    # Completed
                (c.lang(90320), 'remove'),       # Remove from list
            ]

        select = control.selectDialog([a[0] for a in actions], c.lang(90314))
        if select == -1:
            return

        action_key = actions[select][1]
        success_label = actions[select][0]
        api = SimklAPI()
        media = (
            SimklAPI.build_movie_obj(title=title, imdb=imdb_s, tmdb=tmdb_i)
            if is_movie
            else SimklAPI.build_show_obj(title=title, imdb=imdb_s, tmdb=tmdb_i)
        )

        result = None
        if action_key == 'rate':
            rating_labels = [
                f"{i} - {['Terrible', 'Bad', 'Poor', 'Meh', 'Fair', 'Decent', 'Good', 'Great', 'Excellent', 'Masterpiece'][i - 1]}"
                for i in range(1, 11)
            ]
            rating_select = control.selectDialog(rating_labels, c.lang(90264))
            if rating_select == -1:
                return
            rated = dict(media)
            rated['rating'] = rating_select + 1
            if is_movie:
                result = api.add_rating(movies=[rated])
            else:
                result = api.add_rating(shows=[rated])
            success_label = f'Rated {rated["rating"]}/10'

        elif action_key == 'remove_rating':
            if is_movie:
                result = api.remove_rating(movies=[media])
            else:
                result = api.remove_rating(shows=[media])

        elif action_key == 'remove':
            if is_movie:
                result = api.remove_from_list(movies=[media])
            else:
                result = api.remove_from_list(shows=[media])

        else:
            # plantowatch | watching | hold | dropped | completed
            if is_movie:
                result = api.add_to_watchlist(movies=[media], status=action_key)
            else:
                result = api.add_to_watchlist(shows=[media], status=action_key)

        if result is None:
            c.infoDialog(
                'Simkl request failed - see the_crew.log',
                heading=title or 'Simkl',
                sound=True,
                icon='ERROR',
            )
            return

        c.log(
            f'[Simkl Manager] {action_key} ok for "{title}" '
            f'(content={"movie" if is_movie else "tvshow"}, imdb={imdb_s}, tmdb={tmdb_i})',
            1,
        )
        invalidate_tv_indicators()
        if is_movie:
            invalidate_movie_indicators()
        control.refresh()
        icon = control.infoLabel('ListItem.Icon') or 'INFO'
        c.infoDialog(success_label, heading=title or 'Simkl', sound=True, icon=icon)

    except Exception as e:
        c.log(f'[Simkl Manager] Exception: {e}', 1)
        c.log(traceback.format_exc(), 1)
        try:
            c.infoDialog(
                'Simkl Manager error - see the_crew.log',
                heading=name or 'Simkl',
                sound=True,
                icon='ERROR',
            )
        except Exception:
            pass
    finally:
        control.idle()

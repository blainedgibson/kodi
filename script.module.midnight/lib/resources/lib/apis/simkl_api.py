# -*- coding: utf-8 -*-

'''
 ***********************************************************
 * Midnight Add-on
 *
 *
 * @file simkl_api.py
 * @package script.module.midnight
 *
 * @copyright (c) 2026, Midnight
 * @license GNU General Public License, version 3 (GPL-3.0)
 *
 * Simkl API wrapper — PIN device flow, personal tracking
 * Tokens NEVER expire (unlike Trakt). No refresh logic needed.
 *
 * API Documentation: https://simkl.docs.apiary.io/
 *
 ********************************************************cm*
'''

import threading
import time
from typing import Optional, Dict, List, Any

from ..modules.crewruntime import c
from ..modules import http_client
from ..modules import keys

# ---------------------------------------------------------------------------
# Required by Simkl API policy — must appear on every request URL
# https://simkl.docs.apiary.io/#introduction/required-query-parameters
# ---------------------------------------------------------------------------
_APP_NAME = 'TheCrew'


def _app_version() -> str:
    try:
        ver = getattr(c, 'moduleversion', None) or ''
        if ver:
            return str(ver)
    except Exception:
        pass
    return '3.0.9.11'


def _user_agent() -> str:
    return f'TheCrew/{_app_version()} Kodi'


class SimklAuthError(Exception):
    """Raised when authentication fails or token is missing"""
    pass


class SimklRateLimitError(Exception):
    """Raised when Simkl API rate limit is exceeded"""
    pass


class SimklAPIError(Exception):
    """Generic Simkl API error"""
    pass


class SimklAPI:
    """
    Simkl API wrapper for Midnight addon — personal tracking only.

    Key advantage over Trakt: access tokens NEVER expire.
    One-time PIN authorization, no refresh tokens, no daily breakage.

    Handles:
    - PIN device flow authorization (https://simkl.com/pin/)
    - Scrobble: start / pause / stop
    - Sync: history, watchlist, ratings, playback progress
    - Last activities (for smart incremental sync)
    - User settings / account info

    API base: https://api.simkl.com/
    Docs: https://simkl.docs.apiary.io/
    """

    BASE_URL = 'https://api.simkl.com'
    PIN_URL = 'https://simkl.com/pin/'

    # POST rate-limit: Simkl enforces 1 request/sec per client_id
    _post_lock = threading.Lock()
    _last_post_time: float = 0.0
    _POST_MIN_INTERVAL = 1.05  # seconds (5% margin over the 1/s limit)

    def __init__(self):
        self.name = 'Simkl'
        # Prefer user-supplied client_id over built-in key.
        # Users can register a free app at simkl.com/settings/developer/new
        # to avoid the shared daily limit while Midnight awaits production approval.
        try:
            override = c.get_setting('simkl.client_id') or ''
            self.client_id = override.strip() if override.strip() else keys.simkl_id
        except Exception:
            self.client_id = keys.simkl_id

        # Shared HTTP session (connection pooling + retry)
        self.session = http_client.HTTPClient.get_session('simkl')

        self._load_credentials()

        if c.devmode:
            c.log('[Simkl API] Initialized', 1)
            c.log(f'[Simkl API] Authenticated: {self.is_authenticated()}', 1)

    # =========================================================================
    # CREDENTIALS
    # =========================================================================

    def _load_credentials(self):
        """Load token + username from Kodi settings."""
        try:
            self.token = c.get_setting('simkl.token').strip()
            self.username = c.get_setting('simkl.user').strip()
        except Exception as e:
            if c.devmode:
                c.log(f'[Simkl API] Error loading credentials: {e}', 1)
            self.token = ''
            self.username = ''

    def _save_credentials(self, token: str = '', username: str = ''):
        """Save token + username to Kodi settings."""
        try:
            c.set_setting('simkl.token', token)
            c.set_setting('simkl.user', username)
            self.token = token
            self.username = username
            if c.devmode:
                c.log(f'[Simkl API] Credentials saved for user: {username}', 1)
        except Exception as e:
            if c.devmode:
                c.log(f'[Simkl API] Error saving credentials: {e}', 1)
            raise SimklAuthError(f'Failed to save credentials: {e}')

    def is_authenticated(self) -> bool:
        """Return True if a token is stored."""
        return bool(self.token)

    def _ensure_auth(self):
        """
        Ensure token exists. Simkl tokens never expire so no refresh needed.
        Raises SimklAuthError if not authenticated.
        """
        self._load_credentials()
        if not self.is_authenticated():
            raise SimklAuthError('Not authenticated — please authorize Simkl first')

    # =========================================================================
    # PIN DEVICE FLOW AUTHORIZATION
    # =========================================================================

    def authorize(self) -> bool:
        """
        Start Simkl PIN device flow authorization.
        Delegates to simkl.auth_simkl() (the standalone module)
        then reloads credentials into this instance.

        Returns:
            bool: True if authorization succeeded
        """
        try:
            from ..modules import simkl as simkl_module
            simkl_module.auth_simkl()
            # Reload credentials after auth_simkl() saved them
            self._load_credentials()
            return self.is_authenticated()
        except Exception as e:
            if c.devmode:
                c.log(f'[Simkl API] authorize() error: {e}', 1)
            return False

    def _get_pin_code(self) -> Optional[Dict]:
        """
        POST /oauth/pin — get user_code for PIN flow.

        Returns:
            dict with user_code, verification_url, expires_in, interval
        """
        try:
            url = f'{self.BASE_URL}/oauth/pin'
            # PIN endpoint only needs client_id (not the full required params)
            params = {'client_id': self.client_id}

            if c.devmode:
                c.log(f'[Simkl API] POST {url}', 1)

            headers = {'User-Agent': _user_agent()}
            response = self.session.post(url, params=params, headers=headers,
                                         json={}, timeout=30)

            if response.status_code == 200:
                return response.json()
            else:
                if c.devmode:
                    c.log(f'[Simkl API] PIN request failed: {response.status_code}', 1)
                return None

        except Exception as e:
            if c.devmode:
                c.log(f'[Simkl API] Error getting PIN code: {e}', 1)
            return None

    def _poll_for_token(self, user_code: str, interval: int, expires_in: int,
                        verification_url: str = '', qr_path: str = None) -> Optional[str]:
        """
        Poll GET /oauth/pin/{user_code} until access_token or timeout.
        Uses simkl.show_simkl_pin_dialog() for the Kodi QR dialog.

        Returns:
            str: access_token on success
            None: on failure or timeout
        """
        try:
            from ..modules import simkl as simkl_module

            authenticated, access_token = simkl_module.show_simkl_pin_dialog(
                verification_url=verification_url,
                user_code=user_code,
                qr_path=qr_path,
                interval=interval,
                expires_in=expires_in
            )

            return access_token if authenticated else None

        except Exception as e:
            if c.devmode:
                c.log(f'[Simkl API] _poll_for_token error — falling back: {e}', 1)
            return self._poll_headless(user_code, interval, expires_in)

    def _poll_headless(self, user_code: str, interval: int, expires_in: int) -> Optional[str]:
        """
        Fallback: poll without a dialog. Used during development/testing.
        Shows a simple info dialog with the PIN and polls in the background.
        """
        try:
            url = f'{self.BASE_URL}/oauth/pin/{user_code}'
            params = {'client_id': self.client_id}
            pin_url = f'{self.PIN_URL}{user_code}'

            c.infoDialog(
                f'Visit {pin_url}\nEnter code: {user_code}\n(Authorization window: {expires_in}s)',
                heading='Simkl Authorization',
                sound=False
            )

            deadline = time.time() + expires_in
            while time.time() < deadline:
                time.sleep(interval)
                try:
                    resp = self.session.get(url, params=params, timeout=10)
                    if resp.status_code == 200:
                        data = resp.json()
                        token = data.get('access_token')
                        if token:
                            return token
                except Exception:
                    pass

            return None

        except Exception as e:
            if c.devmode:
                c.log(f'[Simkl API] Headless poll error: {e}', 1)
            return None

    def _fetch_username(self, token: str) -> str:
        """
        Fetch display name from /users/settings.
        Simkl returns user.name (not username).
        """
        try:
            url = f'{self.BASE_URL}/users/settings'
            headers = self._build_headers(token=token)
            params = self._required_params()
            resp = self.session.get(url, headers=headers, params=params, timeout=15)
            if resp.status_code != 200:
                resp = self.session.post(url, headers=headers, params=params, json={}, timeout=15)
            if resp.status_code == 200:
                data = resp.json() or {}
                user = data.get('user') or {}
                name = (user.get('name') or user.get('username') or '').strip()
                if name:
                    return name
                if c.devmode:
                    c.log(f'[Simkl API] _fetch_username: no name in {list(user.keys())}', 1)
            elif c.devmode:
                c.log(f'[Simkl API] _fetch_username HTTP {resp.status_code}', 1)
        except Exception as e:
            if c.devmode:
                c.log(f'[Simkl API] Error fetching username: {e}', 1)
        return 'simkl_user'

    def _generate_qr_code(self, url: str) -> Optional[str]:
        """Generate QR code image for the PIN URL."""
        try:
            from ..modules.trakt import generate_qr_code_local
            return generate_qr_code_local(url, size=280)
        except Exception as e:
            if c.devmode:
                c.log(f'[Simkl API] QR code generation failed: {e}', 1)
            return None

    def revoke(self):
        """Clear stored credentials. (Simkl has no API revoke endpoint.)"""
        self._save_credentials('', '')
        if c.devmode:
            c.log('[Simkl API] Credentials cleared', 1)

    # =========================================================================
    # HTTP HELPERS
    # =========================================================================

    def _required_params(self) -> Dict:
        """
        Query params required on EVERY Simkl API request.
        https://simkl.docs.apiary.io/#introduction/required-query-parameters
        """
        return {
            'client_id': self.client_id,
            'app-name': _APP_NAME,
            'app-version': _app_version(),
        }

    def _build_headers(self, token: str = None) -> Dict:
        """
        Build standard Simkl request headers.
        Uses self.token if token not provided.
        """
        t = token or self.token
        headers = {
            'Content-Type': 'application/json',
            'simkl-api-key': self.client_id,
            'User-Agent': _user_agent(),
        }
        if t:
            headers['Authorization'] = f'Bearer {t}'
        return headers

    def _get(self, endpoint: str, params: Dict = None, auth_required: bool = True) -> Optional[Any]:
        """
        Authenticated GET request.

        Args:
            endpoint: e.g. '/users/settings'
            params: query params dict
            auth_required: raise if no token

        Returns:
            parsed JSON or None
        """
        try:
            if auth_required:
                self._ensure_auth()

            url = f'{self.BASE_URL}{endpoint}'
            headers = self._build_headers()

            # Merge caller params with required params (caller values take precedence)
            merged_params = dict(self._required_params())
            if params:
                merged_params.update(params)

            if c.devmode:
                c.log(f'[Simkl API] GET {endpoint}', 1)

            response = self.session.get(url, headers=headers, params=merged_params, timeout=30)

            if response.status_code == 200:
                return response.json() if response.content else None
            elif response.status_code == 204:
                return None
            elif response.status_code == 401:
                if c.devmode:
                    c.log(f'[Simkl API] 401 on GET {endpoint} — token may have been revoked', 1)
                # Simkl tokens don't expire but can be revoked by user
                # Clear token so user knows they need to re-authorize
                self._save_credentials('', '')
                raise SimklAuthError('Simkl token revoked — please re-authorize')
            elif response.status_code == 412:
                if c.devmode:
                    c.log(f'[Simkl API] 412 on GET {endpoint} — invalid client_id or rate limit', 1)
                raise SimklAPIError('Simkl client_id invalid or request limit exceeded')
            elif response.status_code == 429:
                raise SimklRateLimitError('Simkl rate limit exceeded')
            else:
                if c.devmode:
                    c.log(f'[Simkl API] GET {endpoint} — {response.status_code}: {response.text[:200]}', 1)
                return None

        except (SimklAuthError, SimklRateLimitError, SimklAPIError):
            raise
        except Exception as e:
            if c.devmode:
                c.log(f'[Simkl API] GET {endpoint} exception: {e}', 1)
            return None

    def _post(self, endpoint: str, data: Dict = None, auth_required: bool = True) -> Optional[Any]:
        """
        Authenticated POST request.

        Args:
            endpoint: e.g. '/sync/history'
            data: JSON body
            auth_required: raise if no token

        Returns:
            parsed JSON or None
        """
        try:
            if auth_required:
                self._ensure_auth()

            url = f'{self.BASE_URL}{endpoint}'
            headers = self._build_headers()

            # Required query params on every request
            req_params = self._required_params()

            if c.devmode:
                c.log(f'[Simkl API] POST {endpoint}', 1)

            # Enforce 1 POST/sec per client_id (Simkl hard limit)
            with SimklAPI._post_lock:
                elapsed = time.monotonic() - SimklAPI._last_post_time
                if elapsed < SimklAPI._POST_MIN_INTERVAL:
                    time.sleep(SimklAPI._POST_MIN_INTERVAL - elapsed)
                response = self.session.post(url, headers=headers, params=req_params,
                                             json=data or {}, timeout=30)
                SimklAPI._last_post_time = time.monotonic()

            if response.status_code in (200, 201):
                return response.json() if response.content else {}
            elif response.status_code == 204:
                return {}
            elif response.status_code == 401:
                if c.devmode:
                    c.log(f'[Simkl API] 401 on POST {endpoint} — token revoked', 1)
                self._save_credentials('', '')
                raise SimklAuthError('Simkl token revoked — please re-authorize')
            elif response.status_code == 409:
                # Duplicate scrobble within 1 hour — not an error, just skip
                if c.devmode:
                    c.log(f'[Simkl API] 409 on POST {endpoint} — duplicate within 1h, skipping', 1)
                return {}
            elif response.status_code == 412:
                raise SimklAPIError('Simkl client_id invalid or request limit exceeded')
            elif response.status_code == 429:
                raise SimklRateLimitError('Simkl rate limit exceeded')
            else:
                if c.devmode:
                    c.log(f'[Simkl API] POST {endpoint} — {response.status_code}: {response.text[:200]}', 1)
                return None

        except (SimklAuthError, SimklRateLimitError, SimklAPIError):
            raise
        except Exception as e:
            if c.devmode:
                c.log(f'[Simkl API] POST {endpoint} exception: {e}', 1)
            return None

    def _delete(self, endpoint: str, auth_required: bool = True) -> bool:
        """
        Authenticated DELETE request.

        Returns:
            True on success (200/204), False otherwise
        """
        try:
            if auth_required:
                self._ensure_auth()

            url = f'{self.BASE_URL}{endpoint}'
            headers = self._build_headers()

            if c.devmode:
                c.log(f'[Simkl API] DELETE {endpoint}', 1)

            response = self.session.delete(url, headers=headers, timeout=30)

            if response.status_code in (200, 204):
                return True
            elif response.status_code == 404:
                if c.devmode:
                    c.log(f'[Simkl API] DELETE {endpoint} — 404 not found', 1)
                return False
            else:
                if c.devmode:
                    c.log(f'[Simkl API] DELETE {endpoint} — {response.status_code}', 1)
                return False

        except (SimklAuthError, SimklRateLimitError, SimklAPIError):
            raise
        except Exception as e:
            if c.devmode:
                c.log(f'[Simkl API] DELETE {endpoint} exception: {e}', 1)
            return False

    # =========================================================================
    # ACCOUNT INFO
    # =========================================================================

    def account_info(self) -> Optional[Dict]:
        """
        Get account info for Account Manager display.

        Returns:
            dict with username, stats, etc. or None
        """
        try:
            if not self.is_authenticated():
                return None

            settings = self._get('/users/settings')
            if not settings:
                return None

            user = settings.get('user', {}) or {}
            acct = settings.get('account', {}) or {}
            avatar = user.get('avatar') or ''
            if isinstance(avatar, dict):
                avatar = avatar.get('medium') or avatar.get('full') or ''
            name = (user.get('name') or user.get('username') or 'N/A').strip() or 'N/A'
            account = {
                'active': True,
                'username': name,
                'name': name,
                'joined': user.get('joined_at', 'N/A'),
                'timezone': acct.get('timezone') or user.get('timezone') or 'N/A',
                'plan': acct.get('type', 'free'),
                'cover_image': avatar,
            }

            # Stats — separate endpoint
            stats = self._get('/users/stats')
            if stats:
                account.update({
                    'movies_watched': stats.get('movies', {}).get('watched', 0),
                    'shows_watched': stats.get('tv', {}).get('watched', 0),
                    'episodes_watched': stats.get('tv', {}).get('watched_episodes', 0),
                    'total_minutes': stats.get('movies', {}).get('minutes', 0)
                                   + stats.get('tv', {}).get('minutes', 0),
                })

            if c.devmode:
                c.log(f'[Simkl API] Account info for: {account["username"]}', 1)

            return account

        except Exception as e:
            if c.devmode:
                c.log(f'[Simkl API] account_info error: {e}', 1)
            return None

    # =========================================================================
    # HEALTH CHECK
    # =========================================================================

    def check_health(self) -> str:
        """
        Lightweight probe to check Simkl connectivity + auth.

        Returns one of:
            'ok'             — authenticated and reachable
            'no_credentials' — no token stored
            'no_network'     — network unreachable
            'auth_dead'      — token was revoked
        """
        if not self.is_authenticated():
            return 'no_credentials'

        try:
            url = f'{self.BASE_URL}/users/settings'
            headers = self._build_headers()
            response = self.session.get(url, headers=headers,
                                         params=self._required_params(), timeout=10)

            if response.status_code == 200:
                return 'ok'
            elif response.status_code == 401:
                # Token revoked — clear it
                self._save_credentials('', '')
                return 'auth_dead'
            else:
                if c.devmode:
                    c.log(f'[Simkl API] Health check: unexpected {response.status_code}', 1)
                return 'ok'  # Unexpected but not auth failure

        except Exception as e:
            if c.devmode:
                c.log(f'[Simkl API] Health check network error: {e}', 1)
            return 'no_network'

    # =========================================================================
    # LAST ACTIVITIES (for smart incremental sync)
    # =========================================================================

    def get_last_activities(self) -> Optional[Dict]:
        """
        POST /sync/activities — get timestamps of last user activity per category.

        ALWAYS call this before any sync. Compare timestamps with locally
        saved values. If nothing changed, skip the sync entirely.

        Per Simkl policy:
        - Mobile/TV: call on startup or wake, throttle to once per 15-30 min
        - Kodi: hook into playback-end / return-to-home events
        - NEVER run unconditional background polling timers

        Key response fields:
            all              — master timestamp (any change)
            movies.all       — any movie list change
            movies.rated_at  — movie rating change
            tv_shows.all     — any show list change
            tv_shows.rated_at

        Returns:
            dict of activity timestamps, or None
        """
        return self._post('/sync/activities')

    # =========================================================================
    # WATCHLIST / ALL ITEMS
    # =========================================================================

    def get_all_items(self, media_type: str = '', status: str = '',
                      extended: str = 'full',
                      date_from: str = None,
                      phase1_initial: bool = False,
                      episode_watched_at: bool = False,
                      include_all_episodes: bool = False,
                      next_watch_info: bool = False) -> Optional[Any]:
        """
        GET /sync/all-items[/{type}[/{status}]] — user's library.

        Simkl policy (client_id will be SUSPENDED if violated):
        - Phase 1 (initial sync): call /sync/all-items/movies, /shows, /anime
          SEPARATELY (not in parallel). Use phase1_initial=True to skip the
          date_from guard for the very first sync only.
        - Phase 2 (all subsequent syncs): ALWAYS pass date_from.
          Get the timestamp from get_last_activities() first, compare with
          your locally saved value. Only call this if activities changed.
        - NEVER poll unconditionally in the background.

        Args:
            media_type:      'movies', 'shows', 'anime', or '' for all
            status:          optional bucket — 'watching', 'plantowatch',
                             'hold', 'dropped', 'completed' (or 'all').
                             Movies have no watching/hold.
            extended:        'full' (default), 'ids_only', 'simkl_ids_only',
                             or ''/None for Simkl default summary
            date_from:       ISO 8601 UTC datetime string, e.g.
                             '2023-10-12T09:03:45Z'. Required unless
                             phase1_initial=True.
            phase1_initial:  Set True ONLY for the very first full sync.
                             Omits date_from and fetches the complete library.
            episode_watched_at: When True, include per-episode watched_at
                             (needed for Midnight playcount indicators).
            include_all_episodes: When True, load seasons[].episodes[] for
                             completed/dropped (and synthesize virtual rows).
            next_watch_info: When True, attach next_to_watch_info (title,
                             season, episode, date) on watching items.

        Returns:
            list/dict of watchlist items, or None
        """
        if not phase1_initial and not date_from:
            # Enforce the policy: log and bail rather than risk suspension
            c.log('[Simkl API] get_all_items() called without date_from and '
                  'phase1_initial=False — refusing to call to avoid client_id suspension. '
                  'Pass date_from from get_last_activities(), or set phase1_initial=True '
                  'for the very first sync.', 1)
            return None

        endpoint = '/sync/all-items'
        if media_type:
            endpoint = f'{endpoint}/{media_type}'
            if status:
                endpoint = f'{endpoint}/{status}'

        params = {}
        if extended:
            params['extended'] = extended
        if date_from:
            params['date_from'] = date_from
        if episode_watched_at:
            params['episode_watched_at'] = 'yes'
        if include_all_episodes:
            params['include_all_episodes'] = 'yes'
        if next_watch_info:
            params['next_watch_info'] = 'yes'

        return self._get(endpoint, params=params)

    def get_items_by_status(self, media_type: str, status: str,
                            extended: str = None,
                            date_from: str = None,
                            phase1_initial: bool = False,
                            episode_watched_at: bool = False,
                            include_all_episodes: bool = False,
                            next_watch_info: bool = False) -> Optional[Any]:
        """
        GET /sync/all-items/{type}/{status} — one library bucket.

        Midnight mapping (no Trakt collection equivalent):
          plantowatch ≈ watchlist
          watching    ≈ in-progress shows
          completed   ≈ watched (library status; history has timestamps)

        extended=None -> Simkl default rich summary (poster/ids/counts).
        Use extended='full' only when episode trees are required.
        """
        return self.get_all_items(
            media_type=media_type,
            status=status,
            extended=extended,
            date_from=date_from,
            phase1_initial=phase1_initial,
            episode_watched_at=episode_watched_at,
            include_all_episodes=include_all_episodes,
            next_watch_info=next_watch_info,
        )

    def get_watched_movies(self, date_from: str = None,
                            phase1_initial: bool = False) -> Optional[List]:
        """
        GET /sync/all-items/movies?extended=full — all watched/listed movies.

        Equivalent to Trakt /users/me/watched/movies (no 100-item limit!).
        Pass date_from for all syncs after the initial Phase 1 load.
        """
        return self.get_all_items('movies', extended='full',
                                    date_from=date_from, phase1_initial=phase1_initial)

    def get_watched_shows(self, date_from: str = None,
                            phase1_initial: bool = False) -> Optional[List]:
        """
        GET /sync/all-items/shows?extended=full — all watched/listed shows.

        Equivalent to Trakt /users/me/watched/shows (no 100-item limit!).
        Pass date_from for all syncs after the initial Phase 1 load.
        """
        return self.get_all_items('shows', extended='full',
                                    date_from=date_from, phase1_initial=phase1_initial)

    def add_to_watchlist(self, movies: List = None, shows: List = None,
                        status: str = 'plantowatch') -> Optional[Dict]:
        """
        POST /sync/add-to-list — add items to a watchlist category.

        Args:
            movies: list of movie media objects
            shows: list of show media objects
            status: 'plantowatch', 'watching', 'completed', 'hold', 'dropped'

        Returns:
            API response dict or None
        """
        items_movies = [dict(m, to=status) for m in (movies or [])]
        items_shows = [dict(s, to=status) for s in (shows or [])]

        data = {}
        if items_movies:
            data['movies'] = items_movies
        if items_shows:
            data['shows'] = items_shows

        if not data:
            return None

        return self._post('/sync/add-to-list', data=data)

    # =========================================================================
    # HISTORY (WATCHED)
    # =========================================================================

    def add_to_history(self, movies: List = None, shows: List = None,
                        episodes: List = None) -> Optional[Dict]:
        """
        POST /sync/history — mark items as watched.

        Each item should include 'ids' dict with at least one of:
        imdb, tmdb, tvdb, simkl. Include 'title' and 'year' for best matching.

        Args:
            movies: list of movie objects e.g. [{'title': '...', 'ids': {'imdb': 'tt...'}}]
            shows:  list of show objects with optional 'seasons' / 'episodes'
            episodes: list of raw episode objects

        Returns:
            dict with added/not_found counts, or None
        """
        data = {}
        if movies:
            data['movies'] = movies
        if shows:
            data['shows'] = shows
        if episodes:
            data['episodes'] = episodes

        if not data:
            return None

        return self._post('/sync/history', data=data)

    def remove_from_history(self, movies: List = None, shows: List = None,
                            episodes: List = None) -> Optional[Dict]:
        """
        POST /sync/history/remove — unmark watched and/or remove from library.

        Simkl canonical "remove from list": send the title with no seasons/
        episodes — clears history AND the watchlist bucket (not add-to-list
        with to=remove). Episode-level payloads only un-watch those episodes.

        Returns:
            dict with deleted counts, or None
        """
        data = {}
        if movies:
            data['movies'] = movies
        if shows:
            data['shows'] = shows
        if episodes:
            data['episodes'] = episodes

        if not data:
            return None

        return self._post('/sync/history/remove', data=data)

    def remove_from_list(self, movies: List = None, shows: List = None) -> Optional[Dict]:
        """
        Remove titles from the user's Simkl library (watchlist + history).

        Alias of remove_from_history() for full titles — Simkl has no separate
        /sync/watchlist/remove; history/remove is the documented path.
        """
        return self.remove_from_history(movies=movies, shows=shows)

    def check_watched(self, movies: List = None, shows: List = None) -> Optional[Dict]:
        """
        POST /sync/watched — bulk "have I watched these?" lookup.

        Pass media objects with ids (imdb/tmdb/simkl). Response indicates
        watched state per item (and per-episode when seasons are included).
        """
        data = {}
        if movies:
            data['movies'] = movies
        if shows:
            data['shows'] = shows
        if not data:
            return None
        return self._post('/sync/watched', data=data)

    # =========================================================================
    # RATINGS
    # =========================================================================

    def get_ratings(self, media_type: str = 'movies', date_from: str = None) -> Optional[List]:
        """
        GET /sync/ratings — all user ratings, filtered by type.

        Args:
            media_type: 'movies' or 'shows'
            date_from: ISO 8601 UTC datetime for incremental fetch

        Returns:
            list of rated items, or None
        """
        params = {'type': media_type}
        if date_from:
            params['date_from'] = date_from
        return self._get('/sync/ratings', params=params)

    def add_rating(self, movies: List = None, shows: List = None) -> Optional[Dict]:
        """
        POST /sync/ratings — rate movies or shows (1–10 scale).

        Each item should include 'rating' field + 'ids'.

        Returns:
            API response dict or None
        """
        data = {}
        if movies:
            data['movies'] = movies
        if shows:
            data['shows'] = shows

        if not data:
            return None

        return self._post('/sync/ratings', data=data)

    def remove_rating(self, movies: List = None, shows: List = None) -> Optional[Dict]:
        """
        POST /sync/ratings/remove — remove ratings.

        Returns:
            API response dict or None
        """
        data = {}
        if movies:
            data['movies'] = movies
        if shows:
            data['shows'] = shows

        if not data:
            return None

        return self._post('/sync/ratings/remove', data=data)

    # =========================================================================
    # PLAYBACK PROGRESS (Continue Watching)
    # =========================================================================

    def get_playback(self, media_type: str = '') -> Optional[List]:
        """
        GET /sync/playback[/{type}] — paused playback sessions.

        Use to build "Continue Watching" list.
        Progress values are percentages (0-100).

        Args:
            media_type: 'movies', 'episodes', or '' for all

        Returns:
            list of playback sessions, or None
        """
        endpoint = '/sync/playback'
        if media_type:
            # Normalize singular -> plural path segments (API uses movies/episodes)
            mt = str(media_type).strip().lower()
            if mt in ('movie', 'movies'):
                mt = 'movies'
            elif mt in ('episode', 'episodes'):
                mt = 'episodes'
            endpoint = f'{endpoint}/{mt}'

        return self._get(endpoint)

    def delete_playback(self, playback_id: int) -> bool:
        """
        DELETE /sync/playback/{id} — remove a paused playback session.

        Returns:
            True on success
        """
        return self._delete(f'/sync/playback/{playback_id}')

    # =========================================================================
    # SCROBBLE (real-time playback tracking)
    # =========================================================================

    def scrobble_start(self, media_obj: Dict, progress: float) -> Optional[Dict]:
        """
        POST /scrobble/start — report playback started.

        Args:
            media_obj: dict with 'movie' OR ('show'+'episode') keys + progress
            progress: float 0–100

        Returns:
            API response or None

        Example media_obj for movie:
            {'movie': {'title': 'Inception', 'year': 2010, 'ids': {'imdb': 'tt1375666'}}}

        Example for episode:
            {'show': {'title': 'Breaking Bad', 'ids': {'imdb': 'tt0903747'}},
             'episode': {'season': 1, 'number': 1}}
        """
        data = dict(media_obj, progress=progress)
        return self._post('/scrobble/start', data=data)

    def scrobble_pause(self, media_obj: Dict, progress: float) -> Optional[Dict]:
        """
        POST /scrobble/pause — save progress without marking as watched.

        Returns:
            API response or None
        """
        data = dict(media_obj, progress=progress)
        return self._post('/scrobble/pause', data=data)

    def scrobble_stop(self, media_obj: Dict, progress: float) -> Optional[Dict]:
        """
        POST /scrobble/stop — end session.
        Marks as watched if progress >= 80%, otherwise saves as paused.

        Returns:
            API response or None
        """
        data = dict(media_obj, progress=progress)
        return self._post('/scrobble/stop', data=data)

    # =========================================================================
    # ID HELPERS
    # =========================================================================

    @staticmethod
    def build_movie_obj(title: str = '', year: int = 0,
                        imdb: str = '', tmdb: int = 0) -> Dict:
        """
        Build a Simkl-compatible movie object from common IDs.
        Pass as much as you have — Simkl uses all of it for matching.

        Returns:
            dict ready for history/rating/scrobble payloads
        """
        ids = {}
        if imdb:
            ids['imdb'] = imdb
        if tmdb:
            ids['tmdb'] = tmdb

        obj = {'ids': ids}
        if title:
            obj['title'] = title
        if year:
            obj['year'] = year
        return obj

    @staticmethod
    def build_show_obj(title: str = '', year: int = 0,
                       imdb: str = '', tmdb: int = 0, tvdb: int = 0,
                       seasons: List = None, episodes: List = None) -> Dict:
        """
        Build a Simkl-compatible show object from common IDs.

        Args:
            seasons: list of season dicts e.g. [{'number': 1, 'episodes': [{'number': 1}]}]
            episodes: list of episode dicts (used in scrobble, not sync)

        Returns:
            dict ready for history/scrobble payloads
        """
        ids = {}
        if imdb:
            ids['imdb'] = imdb
        if tmdb:
            ids['tmdb'] = tmdb
        if tvdb:
            ids['tvdb'] = tvdb

        obj = {'ids': ids}
        if title:
            obj['title'] = title
        if year:
            obj['year'] = year
        if seasons:
            obj['seasons'] = seasons
        if episodes:
            obj['episodes'] = episodes
        return obj

# -*- coding: utf-8 -*-

'''
 *******************************************************cm**
 * Midnight Add-on - Progress Indexer
 *
 * @package script.module.midnight
 *
 * Clean implementation of Trakt progress features using new model classes.
 * Provides three main features:
 *   1. In Progress Shows - TV shows you're watching (alphabetical, show posters)
 *   2. Next Episodes - Next unwatched episodes to watch (auto-advance)
 *   3. In Progress Episodes - Episodes with resume points (partially watched)
 *
 * @copyright (c) 2025-2026, Midnight
 * @license GNU General Public License, version 3 (GPL-3.0)
 *
 *******************************************************cm**
'''

import concurrent.futures
import re
import threading
import traceback
from contextlib import suppress

from ..models.tvshow import TVShow
from ..models.episode import Episode
from ..modules import trakt
from ..modules import simkl
from ..modules import cache
from ..modules import control
from ..modules import workers
from ..modules import keys
from ..modules.crewruntime import c
from ..modules import tracker
from ..modules import cleandate
from resources.lib.ui_theme import color



def _trakt_progress_active() -> bool:
    """XOR: Trakt progress/API only when Trakt is the selected tracker."""
    return tracker.is_trakt()


def _run_pool(tasks, worker_fn, max_workers, label='HTTP'):
    """
    Run tasks in a thread pool.

    On Kodi abort, stop waiting for in-flight HTTP so plugin invokers do not
    block 'Stopping all' for tens of seconds (quit hang / crash feel).
    """
    if not tasks:
        return []
    n = max(1, min(len(tasks), max_workers))
    results = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=n)
    try:
        futs = [pool.submit(worker_fn, t) for t in tasks]
        for f in concurrent.futures.as_completed(futs):
            if control.monitor.abortRequested():
                c.log(f'[Progress] {label} aborted (Kodi shutting down)')
                break
            with suppress(Exception):
                results.append(f.result())
    finally:
        pool.shutdown(wait=not control.monitor.abortRequested())
    return results


class Progress:
    """
    Indexer for Trakt progress features.
    Uses clean model classes (TVShow, Episode) instead of monolithic approach.
    """

    def __init__(self):
        self.list = []
        self.trakt_user = c.get_setting('trakt.username').strip()
        self.lang = control.apiLanguage()['trakt']
        self.hidecinema = c.get_setting('hidecinema') == 'true'

        # Trakt API endpoints
        self.progress_link = 'https://api.trakt.tv/sync/watched/shows'
        self.trakthistory_link = 'https://api.trakt.tv/sync/history/episodes?limit=25'

    def _defer_widget_poster_enrich(self, enrich_fn, log_label=''):
        """Warm cold widget items off the main route, then request Container.Refresh."""
        refresh_url = control.get_widget_refresh_url()

        def _run():
            try:
                c.log(f"[Progress] Widget deferred enrich started{f': {log_label}' if log_label else ''}")
                enrich_fn()
                control.schedule_widget_refresh(refresh_url)
                c.log(f"[Progress] Widget deferred enrich done{f': {log_label}' if log_label else ''}")
            except Exception as ex:
                c.log(f"[Progress] Widget deferred enrich error: {ex}")

        threading.Thread(target=_run, daemon=True, name="CrewWidgetPoster").start()

    @staticmethod
    def _finalize_episode_artwork(ep):
        """Episode widgets render icon/thumb — use show poster when still is missing."""
        if ep.thumb not in ('0', '', None):
            return
        if ep.poster not in ('0', '', None):
            ep.thumb = ep.poster
        elif ep.fanart not in ('0', '', None):
            ep.thumb = ep.fanart

    @staticmethod
    def _apply_show_artwork_from_cache(ep, show_artwork, lang):
        """
        Apply TMDB show poster/fanart onto the episode.

        Important: do NOT insert an empty {} on SQLite miss. Warm episodes used to
        poison show_artwork[sid] = {} which then blocked the cold-path art HTTP —
        so popular warm shows (TNG) showed no poster while obscure cold shows looked fine.
        """
        from ..modules import http_client
        sid = ep.showtmdb
        if not sid or sid in ('0', 'None'):
            return

        resp = show_artwork.get(sid)
        if resp is None:
            art_url = (f"https://api.themoviedb.org/3/tv/{sid}"
                       f"?api_key={keys.tmdb_key}&language={lang}&append_to_response=external_ids")
            cached = cache.cache_get(cache._hash_function(http_client.tmdb_get_json, art_url, 16), timeout=None)
            if cached:
                import json as _json
                try:
                    parsed = _json.loads(cached['value']) if isinstance(cached['value'], str) else cached['value']
                except Exception:
                    parsed = None
                if isinstance(parsed, dict) and (parsed.get('poster_path') or parsed.get('backdrop_path') or parsed.get('id')):
                    show_artwork[sid] = parsed
                    resp = parsed
            # Cache miss: leave sid absent so build_http_tasks can still fetch art.

        if not isinstance(resp, dict):
            return
        if resp.get('poster_path'):
            ep.poster = f"https://image.tmdb.org/t/p/{c.tmdb_postersize}{resp['poster_path']}"
        if resp.get('backdrop_path'):
            ep.fanart = f"https://image.tmdb.org/t/p/{c.tmdb_fanartsize}{resp['backdrop_path']}"
        # Do not call _finalize_episode_artwork here — poster must not become thumb
        # until after the episode still (still_path) fetch has run.

    @staticmethod
    def _collect_show_tmdb_ids(items, ep_key='ep'):
        ids = set()
        for row in items:
            ep = row.get(ep_key)
            if not ep:
                continue
            sid = str(getattr(ep, 'showtmdb', '0'))
            if sid in ('0', 'None', '', None):
                continue
            try:
                ids.add(int(sid))
            except (TypeError, ValueError):
                pass
        return sorted(ids)

    @staticmethod
    def _remember_episode_404(ep, episode_cache_dict):
        """Persist and in-memory-mark a TMDB 404 so boot does not re-fetch it."""
        try:
            show_tmdb = int(ep.showtmdb)
        except (TypeError, ValueError):
            return
        trakt.update_next_episode_cache(show_tmdb, ep.season, ep.episode, False)
        if episode_cache_dict is None:
            return
        from datetime import datetime
        watched_at, paused_at = trakt.get_trakt_activity_from_db()
        episode_cache_dict[(show_tmdb, ep.season, ep.episode)] = {
            'episode_exists': 0,
            'last_checked': datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            'cached_watched_at': watched_at,
            'cached_paused_at': paused_at,
        }

    @staticmethod
    def _advance_past_cached_404(ni, episode_cache_dict, max_steps=4):
        """Advance past TMDB gaps only when season cache confirms beyond-finale."""
        from ..modules import tv_season_cache

        if 'show_data' not in ni or 'ep' not in ni:
            return
        try:
            showtmdb = int(ni['ep'].showtmdb)
        except (TypeError, ValueError):
            return
        season, episode = ni['season'], ni['episode']
        advanced = False
        for _ in range(max_steps):
            if trakt.check_next_episode_cache(showtmdb, season, episode, cache_dict=episode_cache_dict) is not False:
                break
            cached = tv_season_cache.get_season_status(showtmdb, season)
            finale = cached.get('finale_ep') if cached else None
            try:
                past_finale = finale is not None and int(episode) > int(finale)
            except (TypeError, ValueError):
                past_finale = False
            # Bare episode_exists=0 without a known finale can be a soft-fail poison —
            # do not invent S+1 (that skipped real eps like 4400 S01E02).
            if not past_finale:
                break
            season += 1
            episode = 1
            advanced = True
        if not advanced or (season == ni['season'] and episode == ni['episode']):
            return
        ni['season'], ni['episode'] = season, episode
        ep = Episode.from_trakt_progress(ni['show_data'], season, episode)
        if ep:
            ep.last_watched_at = ni.get('last_watched_at')
            ni['ep'] = ep
            c.log(f"[Progress] Advanced past cached TMDB gap -> show {showtmdb} S{season:02d}E{episode:02d}")

    @staticmethod
    def _next_episode_has_meta(ep) -> bool:
        """
        True only when TMDB confirmed the episode (not Simkl title alone).

        Simkl seeds titles for Lost S06E18 / Voyager S07E26 / Jury Duty S02
        (split finales / id_split) which 404 on TMDB — those must scrub.
        Sparse TMDB rows (Trauma: title only) still count via _tmdb_ok.
        """
        if not ep:
            return False
        if getattr(ep, '_tmdb_ok', False):
            return True
        aired = (
            getattr(ep, 'first_aired', None)
            or getattr(ep, 'premiered', None)
            or ''
        )
        aired = str(aired).strip()
        plot = (getattr(ep, 'plot', None) or '').strip()
        if aired not in ('', '0') or plot not in ('', '0'):
            return True
        return False

    @staticmethod
    def _next_episode_air_date(ni) -> str:
        """Best-known YYYY-MM-DD (or '') for a next-episode row."""
        air = (ni.get('air_date') or '').strip()
        ep = ni.get('ep')
        if ep:
            for cand in (
                getattr(ep, 'first_aired', None),
                getattr(ep, 'premiered', None),
            ):
                if cand and str(cand).strip() not in ('', '0'):
                    air = str(cand).strip()
                    break
        if air:
            m = re.search(r'(\d{4}-\d{2}-\d{2})', air)
            return m.group(1) if m else air[:10]
        return ''

    def _next_episode_is_unaired(self, ni) -> bool:
        """
        True when this next-episode marker is known to be in the future.

        Uses session air_date, Episode premiere after enrich, and season-cache
        premiere dates. Unknown dates → False (keep; do not over-drop).
        """
        air = self._next_episode_air_date(ni)
        if cleandate.is_known_future(air):
            return True

        ep = ni.get('ep')
        if not ep:
            return False
        try:
            from ..modules import tv_season_cache
            season = int(ni.get('season') or ep.season or 0)
            episode = int(ni.get('episode') or ep.episode or 0)
            cached = tv_season_cache.get_season_status(int(ep.showtmdb), season)
            if not cached:
                return False
            if cached.get('status') == 'future':
                return True
            # Season premiere not yet aired (E01… with scheduled next_air_date)
            nad = cached.get('next_air_date')
            if nad and episode <= 1 and cleandate.is_known_future(nad):
                return True
        except Exception:
            pass
        return False

    def _apply_season_boundary(self, ni, episode_cache_dict=None) -> bool:
        """
        Drop or advance past completed seasons using tv_season_cache / show-root TMDB.

        Notebook probe: Hijack S02 finale=E08, The Pitt S02=E15, Alien: Earth S01=E08.
        Simkl/Trakt still invent E09/E16 → empty listitems. Same rules as TV Evening.
        Returns True to keep ni, False to drop.
        """
        from ..modules import tv_season_cache

        ep = ni.get('ep')
        show_data = ni.get('show_data') or {}
        if not ep:
            return False

        try:
            show_tmdb = int(ep.showtmdb)
        except (TypeError, ValueError):
            return True

        season = int(ni.get('season') or ep.season or 0)
        episode = int(ni.get('episode') or ep.episode or 0)
        show_title = show_data.get('title') or getattr(ep, 'tvshowtitle', '?')

        def _rebuild(new_season, new_episode) -> bool:
            rebuilt = Episode.from_trakt_progress(show_data, new_season, new_episode)
            if not rebuilt:
                return False
            rebuilt.last_watched_at = ni.get('last_watched_at')
            ni['season'], ni['episode'], ni['ep'] = new_season, new_episode, rebuilt
            return True

        cached = tv_season_cache.get_season_status(show_tmdb, season)
        if cached:
            status_val = cached.get('status')
            finale_ep = cached.get('finale_ep')
            show_ended = cached.get('show_ended', 0)

            if show_ended or status_val == 'ended':
                c.log(f"[Progress] Drop {show_title} — show ended (season cache)")
                return False

            if status_val == 'future':
                air = cached.get('next_air_date') or 'TBD'
                c.log(f"[Progress] Drop {show_title} S{season:02d} — future season (premiere {air})")
                return False

            # Airing/future premiere date on this season but episode not out yet
            nad = cached.get('next_air_date')
            if nad and episode <= 1 and cleandate.is_known_future(nad):
                c.log(
                    f"[Progress] Drop {show_title} S{season:02d}E{episode:02d} "
                    f"— unaired (premiere {nad})"
                )
                return False

            if status_val == 'complete' and finale_ep and episode > int(finale_ep):
                next_cached = tv_season_cache.get_season_status(show_tmdb, season + 1)
                if next_cached and next_cached.get('status') in ('future', 'ended'):
                    air = next_cached.get('next_air_date') or 'TBD'
                    c.log(
                        f"[Progress] Drop {show_title} S{season:02d}E{episode:02d} "
                        f"beyond finale E{int(finale_ep):02d}; next season not ready ({air})"
                    )
                    return False
                if _rebuild(season + 1, 1):
                    c.log(
                        f"[Progress] {show_title} beyond S{season:02d} finale "
                        f"E{int(finale_ep):02d} → S{season + 1:02d}E01"
                    )
                    return True
                return False

        return True

    def _resolve_ghost_next_episode(self, ni, episode_cache_dict=None) -> bool:
        """
        After TMDB enrich left an empty stub: learn season boundary, advance or drop.
        Returns True to keep, False to drop.
        """
        from ..modules import tv_season_cache

        ep = ni.get('ep')
        if not ep or self._next_episode_has_meta(ep):
            return bool(ep)

        show_data = ni.get('show_data') or {}
        show_title = show_data.get('title') or getattr(ep, 'tvshowtitle', '?')
        try:
            show_tmdb = int(ep.showtmdb)
        except (TypeError, ValueError):
            return False

        season = int(ep.season or 0)
        episode = int(ep.episode or 0)
        c.log(
            f"[Progress] Ghost next ep {show_title} S{season:02d}E{episode:02d} "
            f"— fetching season boundary"
        )
        with suppress(Exception):
            self._remember_episode_404(ep, episode_cache_dict)

        info = tv_season_cache.fetch_and_cache(show_tmdb, season, show_title)
        if info and info.get('show_ended'):
            return False

        # Prefer cache-driven drop/advance
        if not self._apply_season_boundary(ni, episode_cache_dict):
            return False

        # If still on a ghost (e.g. no cache write), try classic S+1 E01 once
        ep = ni.get('ep')
        if ep and not self._next_episode_has_meta(ep) and episode > 1:
            rebuilt = Episode.from_trakt_progress(show_data, season + 1, 1)
            if not rebuilt:
                return False
            rebuilt.last_watched_at = ni.get('last_watched_at')
            ni['season'], ni['episode'], ni['ep'] = season + 1, 1, rebuilt
            # Leave meta empty — caller will cold-fetch; if that fails too, drop later
            next_cached = tv_season_cache.get_season_status(show_tmdb, season + 1)
            if next_cached and next_cached.get('status') in ('future', 'ended'):
                return False
            return True

        return self._next_episode_has_meta(ni.get('ep'))

    def get_in_progress_shows(self, page=None):
        """
        Get list of TV shows user is currently watching.
        Returns show-level objects (not episodes) sorted by most recently watched.

        Filters out:
        - Shows that are both ended AND fully watched
        - Shows hidden from calendar via Trakt

        When set to ICONS view, shows TV show posters (like Gears).
        User can select show to see seasons/episodes.

        Args:
            page: Page number (1-based). None/1 = first page.

        Returns:
            list: List of TVShow objects as dicts
        """
        PAGE_SIZE = 25
        try:
            page = max(1, int(page)) if page else 1
        except (ValueError, TypeError):
            page = 1
        try:
            if tracker.simkl_live():
                c.log("[Progress] Fetching In Progress Shows (Simkl watching)")
                all_items = simkl.crew_show_items('watching') or []
                all_items = sorted(
                    all_items,
                    key=lambda k: (k.get('title') or '').lower(),
                )
                start = (page - 1) * PAGE_SIZE
                end = start + PAGE_SIZE
                page_items = all_items[start:end]
                self.list = page_items
                next_url = None
                next_label = None
                if end < len(all_items):
                    import sys as _sys
                    _base = (
                        (_sys.argv[0] + '?action=progress_shows')
                        if _sys.argv else ''
                    )
                    next_url = f'{_base}&page={page + 1}'
                    next_label = f'Next Page ({page + 1})'
                c.log(
                    f"[Progress] Simkl watching shows: "
                    f"{len(page_items)}/{len(all_items)} (page {page})"
                )
                self.shows_directory(self.list, next_url=next_url, next_label=next_label)
                return self.list

            if not _trakt_progress_active():
                c.log("[Progress] In Progress Shows skipped — no active tracker", 1)
                self.list = []
                self.shows_directory(self.list)
                return self.list
            c.log("[Progress] Fetching In Progress Shows")
            is_widget = c.is_widget_listing()

            # Get hidden shows from Trakt (to filter them out)
            hidden_shows = set()
            try:
                hidden_url = "https://api.trakt.tv/users/hidden/calendar?type=show&limit=1000"
                hidden_result = trakt.getTraktAsJson(hidden_url)
                if hidden_result:
                    hidden_shows = {
                        str(item.get('show', {}).get('ids', {}).get('trakt'))
                        for item in hidden_result
                        if item.get('show', {}).get('ids', {}).get('trakt')
                    }
                    c.log(f"[Progress] Found {len(hidden_shows)} hidden shows")
            except Exception as e:
                c.log(f"[Progress] Could not fetch hidden shows: {e}")

            # Get progress from Trakt (serialized — shares one fetch with Next Episodes widget)
            result = trakt.get_sync_watched_shows_extended(log_label='in_progress_shows')

            if not result:
                c.log("[Progress] No Trakt progress data")
                self.list = []
                self.shows_directory(self.list)
                return self.list

            # --- Phase 0: Build TVShow objects from Trakt data only (no network) ---
            show_items = []  # [{'show': TVShow, 'last_watched_at': str}]
            filtered_count = 0
            for item in result:
                try:
                    show_data = item.get('show', {})
                    status = show_data.get('status', '').lower()
                    aired_episodes = int(show_data.get('aired_episodes', 0))
                    show_ids = show_data.get('ids', {})
                    trakt_id = str(show_ids.get('trakt', '0'))
                    showtmdb = str(show_ids.get('tmdb', '0'))

                    # Filter: hidden shows
                    if trakt_id in hidden_shows:
                        filtered_count += 1
                        continue

                    # Filter: ended/canceled AND fully watched
                    seasons = item.get('seasons', [])
                    if isinstance(seasons, dict):
                        seasons = list(seasons.values())
                    watched_count = sum(
                        len(s.get('episodes', []))
                        for s in seasons if s.get('number', 0) > 0
                    )
                    if watched_count >= aired_episodes and aired_episodes > 0:
                        filtered_count += 1
                        continue

                    if not showtmdb or showtmdb in ('None', '0'):
                        continue

                    show = TVShow.from_trakt_progress(item)
                    if not show:
                        continue

                    # Find most recent watched timestamp
                    last_timestamp = None
                    for s in sorted((s for s in seasons if s.get('number', 0) > 0), key=lambda s: s.get('number', 0)):
                        for e in (s.get('episodes') or []):
                            ts = e.get('last_watched_at')
                            if ts and (last_timestamp is None or ts > last_timestamp):
                                last_timestamp = ts
                    show.last_watched_at = last_timestamp

                    show_items.append({'show': show, 'last_watched_at': last_timestamp})
                except Exception as e:
                    c.log(f"[Progress] Phase 0 show error: {e}")

            if filtered_count:
                c.log(f"[Progress] Filtered out {filtered_count} hidden/completed shows")
            c.log(f"[Progress] Phase 0: {len(show_items)} in-progress shows")

            # Sort by most recently watched
            show_items.sort(
                key=lambda si: si['last_watched_at'] or '1900-01-01T00:00:00.000Z',
                reverse=True
            )

            # Slice to current page
            start = (page - 1) * PAGE_SIZE
            end = start + PAGE_SIZE
            page_items = show_items[start:end]
            bg_items = show_items[end:]  # background warm only

            # Warm/cold split: check SQLite cache for each show's TMDB data
            from ..modules import http_client
            import json as _json
            import time as _time

            def _show_url(si):
                tmdb = si['show'].tmdb
                return (f"https://api.themoviedb.org/3/tv/{tmdb}"
                        f"?api_key={keys.tmdb_key}&language={self.lang}")

            def _show_cache_key(si):
                return cache._hash_function(http_client.tmdb_get_json, _show_url(si), 16)

            warm_items, cold_items = [], []
            for si in page_items:
                ck = _show_cache_key(si)
                cached = cache.cache_get(ck, timeout=None)  # any cached data -> warm
                if cached:
                    warm_items.append((si, ck, cached))
                else:
                    cold_items.append((si, ck))
            c.log(f"[Progress] Page {page}: warm={len(warm_items)}, cold={len(cold_items)}, bg={len(bg_items)}")

            # Warm: apply from SQLite cache (no network)
            for si, ck, cached_entry in warm_items:
                try:
                    val = cached_entry.get('value')
                    resp = _json.loads(val) if isinstance(val, str) else val
                    if resp:
                        si['show']._parse_tmdb_response(resp)
                except Exception as e:
                    c.log(f"[Progress] Warm apply error: {e}")

            # Cold: parallel pure-HTTP fetch, then serial SQLite write
            def _show_fetch_raw(task):
                ck, url, si = task
                try:
                    resp = http_client.tmdb_get_json(url)
                    return ck, url, si, resp
                except Exception:
                    return ck, url, si, None

            def _enrich_show_cold(items, max_workers):
                if not items:
                    return
                cold_tasks = [(ck, _show_url(si), si) for si, ck in items]
                n = min(len(cold_tasks), max_workers)
                c.log(f"[Progress] Show inline HTTP: {len(cold_tasks)} requests ({n} workers)")
                t0 = _time.time()
                cold_results = []
                with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
                    futs = [pool.submit(_show_fetch_raw, t) for t in cold_tasks]
                    for f in concurrent.futures.as_completed(futs):
                        with suppress(Exception):
                            cold_results.append(f.result())
                c.log(f"[Progress] Show inline HTTP done: {sum(1 for r in cold_results if r[3])}/{len(cold_tasks)} in {_time.time()-t0:.2f}s")
                for ck, url, si, resp in cold_results:
                    if resp:
                        with suppress(Exception):
                            cache.cache_insert(ck, _json.dumps(resp))
                        with suppress(Exception):
                            si['show']._parse_tmdb_response(resp)

            if cold_items:
                # Widget must enrich before building list items — deferred enrich painted
                # posterless rows and Container.Refresh often lost behind queued routes.
                _enrich_show_cold(cold_items, 6 if is_widget else 25)

            # Background warm: cache pages beyond current page for instant next-page loads
            if bg_items and not is_widget:
                def _show_bg_warm(items):
                    try:
                        bg_tasks = []
                        for si in items:
                            ck = _show_cache_key(si)
                            if not cache.cache_get(ck, timeout=24):  # skip if already fresh
                                bg_tasks.append((ck, _show_url(si), si))
                        if not bg_tasks:
                            c.log("[Progress] Show bg warm: all already cached")
                            return
                        n_bg = min(len(bg_tasks), 20)
                        bg_results = []
                        with concurrent.futures.ThreadPoolExecutor(max_workers=n_bg) as pool:
                            futs = [pool.submit(_show_fetch_raw, t) for t in bg_tasks]
                            for f in concurrent.futures.as_completed(futs):
                                with suppress(Exception):
                                    bg_results.append(f.result())
                        for ck, url, si, resp in bg_results:
                            if resp:
                                with suppress(Exception):
                                    cache.cache_insert(ck, _json.dumps(resp))
                        c.log(f"[Progress] Show bg warm done: {sum(1 for r in bg_results if r[3])}/{len(bg_tasks)} cached")
                    except Exception as ex:
                        c.log(f"[Progress] Show bg warm error: {ex}")

                threading.Thread(target=_show_bg_warm, args=(bg_items,), daemon=True, name="CrewShowWarm").start()

            # Build final list sliced to current page
            all_shows = [si['show'].to_dict() for si in show_items]
            all_shows.sort(
                key=lambda x: x.get('last_watched_at') or '1900-01-01T00:00:00.000Z',
                reverse=True
            )
            page_shows = all_shows[start:end]
            has_next = end < len(all_shows)

            import sys as _sys
            _base_url = (_sys.argv[0] + '?action=progress_shows') if _sys.argv else ''
            next_url = (f'{_base_url}&page={page + 1}') if has_next else None
            _total_pages = (len(all_shows) + PAGE_SIZE - 1) // PAGE_SIZE
            next_label = (f"{c.lang(30500) or 'Next Page'} (Page {page + 1} of {_total_pages})") if has_next else None

            self.list = page_shows
            c.log(f"[Progress] Rendering page {page}: items {start+1}-{min(end, len(all_shows))} of {len(all_shows)} ({len(warm_items)} warm, {len(cold_items)} cold)")
            self.shows_directory(self.list, next_url=next_url, next_label=next_label)
            return self.list

        except Exception as e:
            c.log(f"[Progress] Error in get_in_progress_shows: {e}")
            self.list = []
            self.shows_directory(self.list)
            return self.list

    def get_next_episodes(self, page=None):
        """
        Get next unwatched episodes user should watch.
        Auto-advances to first unwatched episode of each show.

        Args:
            page: Page number (1-based string or int). None/1 = first page.

        Returns:
            list: List of Episode objects as dicts
        """
        PAGE_SIZE = 25
        try:
            page = max(1, int(page)) if page else 1
        except (ValueError, TypeError):
            page = 1
        try:
            if tracker.simkl_live():
                c.log("[Progress] Fetching Next Episodes (Simkl watching + next_to_watch)")
                is_widget = c.is_widget_listing()
                import xbmcgui as _xbmcgui
                if not is_widget and _xbmcgui.Window(10000).getProperty('crew.widget.context') == '1':
                    is_widget = True
                _xbmcgui.Window(10000).clearProperty('crew.widget.context')
                c.log(f"[Progress] NextEpisodes Widget Detection: is_widget={is_widget}")

                simkl_next = simkl.crew_next_episode_sessions() or []
                if not simkl_next:
                    c.log("[Progress] No Simkl next-episode data")
                    self.list = []
                    self.episodes_directory(self.list)
                    return self.list

                next_items = []
                for session in simkl_next:
                    try:
                        show_data = session.get('show_data') or {}
                        season_num = session.get('season')
                        episode_num = session.get('episode')
                        showtmdb = str((show_data.get('ids') or {}).get('tmdb') or '0')
                        if not showtmdb or showtmdb in ('None', '0'):
                            continue
                        ep_title = session.get('episode_title') or ''
                        episode_data = {'title': ep_title} if ep_title else None
                        ep = Episode.from_trakt_progress(
                            show_data, season_num, episode_num, episode_data
                        )
                        if not ep:
                            continue
                        ep.last_watched_at = session.get('last_watched_at')
                        air_date = (session.get('air_date') or '').strip()
                        if air_date:
                            ep.first_aired = air_date
                            ep.premiered = air_date
                        next_items.append({
                            'show_data': show_data,
                            'season': season_num,
                            'episode': episode_num,
                            'last_watched_at': session.get('last_watched_at'),
                            'air_date': air_date,
                            'ep': ep,
                        })
                    except Exception as e:
                        c.log(f"[Progress] Simkl next-episode build error: {e}")

                before = len(next_items)
                next_items = [ni for ni in next_items if not self._next_episode_is_unaired(ni)]
                if len(next_items) != before:
                    c.log(
                        f"[Progress] Simkl unaired filter (session dates): "
                        f"{before} → {len(next_items)}"
                    )

                next_items.sort(
                    key=lambda ni: ni.get('last_watched_at') or '1900-01-01T00:00:00.000Z',
                    reverse=True,
                )
                c.log(f"[Progress] Phase 0 (Simkl): {len(next_items)} shows with next episodes")

            elif not _trakt_progress_active():
                c.log("[Progress] Next Episodes skipped — no active tracker progress source", 1)
                self.list = []
                self.episodes_directory(self.list)
                return self.list
            else:
                c.log("[Progress] Fetching Next Episodes")

                # Serialized Trakt fetch — widget reload bursts queue here instead of stampeding API
                result = trakt.get_sync_watched_shows_extended(log_label='next_episodes')

                if not result:
                    auth_failed = control.window.getProperty('midnight.trakt_auth_failed') == 'true'
                    if not auth_failed:
                        c.log("[Progress] No Trakt progress data - retrying in 3s")
                        import time as _time
                        _time.sleep(3)
                        result = trakt.get_sync_watched_shows_extended(log_label='next_episodes-retry')

                if not result:
                    c.log("[Progress] No Trakt progress data - check Trakt authentication")
                    auth_failed = control.window.getProperty('midnight.trakt_auth_failed') == 'true'
                    already_notified = control.window.getProperty('midnight.trakt_auth_notified') == 'true'
                    is_widget = c.is_widget_listing()
                    self.list = []
                    if is_widget:
                        if auth_failed:
                            # Permanent auth failure — do not schedule refresh (infinite Loading… loop).
                            c.log("[Progress] Next Episodes widget: Trakt auth failed — empty widget", 1)
                            self.episodes_directory(self.list)
                        else:
                            # Transient Trakt/network failure — show Loading… once, then refresh.
                            c.log("[Progress] Next Episodes widget: Trakt unavailable — loading placeholder + scheduled refresh", 1)
                            self.episodes_directory(self.list, suppress_empty_dialog=True)
                            control.schedule_widget_refresh()
                    else:
                        if auth_failed and not already_notified:
                            control.window.setProperty('midnight.trakt_auth_notified', 'true')
                            control.infoDialog(
                                "Please check Trakt authentication in Midnight settings",
                                heading="Next Episodes Unavailable",
                            )
                        self.episodes_directory(self.list)
                    return self.list

                # Widget context detection (preserved: after Container.Refresh PluginName changes)
                is_widget = c.is_widget_listing()
                import xbmcgui as _xbmcgui
                if not is_widget and _xbmcgui.Window(10000).getProperty('crew.widget.context') == '1':
                    is_widget = True
                _xbmcgui.Window(10000).clearProperty('crew.widget.context')
                c.log(f"[Progress] NextEpisodes Widget Detection: is_widget={is_widget}")

                # --- Phase 0: Build list from Trakt data alone (~0ms, no network) ---
                # Pure math: find next unwatched episode for each show.
                next_items = []  # [{'show_data', 'season', 'episode', 'last_watched_at'}]
                for item in result:
                    try:
                        show_data = item.get('show', {})
                        seasons = item.get('seasons', [])
                        if isinstance(seasons, dict):
                            seasons = list(seasons.values())

                        num_watched = sum(len(s.get('episodes', [])) for s in seasons if s.get('number', 0) > 0)
                        aired_episodes = int(show_data.get('aired_episodes', 0))
                        if num_watched >= aired_episodes and aired_episodes > 0:
                            continue  # fully watched

                        max_season = max_episode = 0
                        last_watched_at = None
                        for s in [s for s in seasons if s.get('number', 0) > 0]:
                            eps = s.get('episodes') or []
                            if eps:
                                max_ep = max(e.get('number', 0) for e in eps)
                                sn = s.get('number')
                                if sn > max_season or (sn == max_season and max_ep > max_episode):
                                    max_season, max_episode = sn, max_ep
                                    for e in eps:
                                        if e.get('number') == max_ep:
                                            last_watched_at = e.get('last_watched_at')
                                            break

                        if max_season <= 0 or max_episode <= 0:
                            if c.devmode:
                                c.log(
                                    f"[Progress] Skipping show without watched episode detail: "
                                    f"{show_data.get('title', '?')}"
                                )
                            continue

                        next_season = max_season
                        next_episode = max_episode + 1
                        showtmdb = str(show_data.get('ids', {}).get('tmdb') or '0')
                        if not showtmdb or showtmdb in ('None', '0'):
                            continue

                        next_items.append({
                            'show_data': show_data,
                            'season': next_season,
                            'episode': next_episode,
                            'last_watched_at': last_watched_at,
                        })
                    except Exception as e:
                        c.log(f"[Progress] Error scanning show: {e}")

                # Build Episode objects from Trakt data (no TMDB fetch)
                for ni in next_items:
                    ep = Episode.from_trakt_progress(ni['show_data'], ni['season'], ni['episode'])
                    if ep:
                        ep.last_watched_at = ni['last_watched_at']
                        ni['ep'] = ep
                next_items = [ni for ni in next_items if 'ep' in ni]
                next_items.sort(key=lambda ni: ni['last_watched_at'] or '1900-01-01T00:00:00.000Z', reverse=True)
                c.log(f"[Progress] Phase 0: {len(next_items)} shows with next episodes")

            episode_cache_dict = trakt.batch_load_episode_cache_for_shows(
                self._collect_show_tmdb_ids(next_items)
            ) or {}
            for ni in next_items:
                self._advance_past_cached_404(ni, episode_cache_dict)

            # Season-complete / returning-series boundary (notebook: Hijack E09, Pitt E16, Alien E09)
            kept = []
            for ni in next_items:
                try:
                    if not self._apply_season_boundary(ni, episode_cache_dict):
                        continue
                    if self._next_episode_is_unaired(ni):
                        show_title = (ni.get('show_data') or {}).get('title') or '?'
                        air = self._next_episode_air_date(ni) or 'unknown'
                        c.log(
                            f"[Progress] Drop unaired next (pre-enrich): {show_title} "
                            f"S{ni.get('season')}E{ni.get('episode')} (airs {air})"
                        )
                        continue
                    kept.append(ni)
                except Exception as e:
                    c.log(f"[Progress] season boundary skip: {e}", 1)
                    kept.append(ni)
            if len(kept) != len(next_items):
                c.log(
                    f"[Progress] Season boundary filter: {len(next_items)} → {len(kept)} next episodes"
                )
            next_items = kept

            # Slice to current page before any network/SQLite work
            start = (page - 1) * PAGE_SIZE
            end = start + PAGE_SIZE
            page_items = next_items[start:end]
            bg_items = next_items[end:]  # everything beyond this page — background warm only

            # Single SQL query: which page episodes are already in metadata cache?
            meta_keys = [
                Episode.get_metadata_cache_key_minimal(ni['ep'].showtmdb, ni['ep'].season, ni['ep'].episode, self.lang)
                for ni in page_items
            ]
            meta_ttl = None if is_widget else 24
            metadata_cache_dict = cache.cache_get_many(meta_keys, timeout=meta_ttl) if meta_keys else {}

            import json as _json
            import time as _time

            def _cached_episode_payload(cache_key):
                row = metadata_cache_dict.get(cache_key)
                if not row:
                    return None
                value = row.get('value') if isinstance(row, dict) else row
                if isinstance(value, str):
                    try:
                        value = _json.loads(value)
                    except Exception:
                        return None
                return value if Episode.tmdb_episode_payload_ok(value) else None

            warm_items, cold_items = [], []
            for ni in page_items:
                key = Episode.get_metadata_cache_key_minimal(
                    ni['ep'].showtmdb, ni['ep'].season, ni['ep'].episode, self.lang
                )
                if _cached_episode_payload(key) is not None:
                    warm_items.append(ni)
                else:
                    cold_items.append(ni)
            c.log(f"[Progress] Page {page}: warm={len(warm_items)}, cold={len(cold_items)}, bg_warm={len(bg_items)}")

            # Enrich warm episodes inline from SQLite (no network, instant)
            from ..modules import http_client
            show_artwork = {}  # artwork dedup by showtmdb

            def _apply_artwork(ep):
                self._apply_show_artwork_from_cache(ep, show_artwork, self.lang)

            for ni in warm_items:
                ok = ni['ep'].fetch_tmdb_metadata_minimal(metadata_cache_dict=metadata_cache_dict)
                if not ok:
                    cold_items.append(ni)
                    continue
                _apply_artwork(ni['ep'])
                self._finalize_episode_artwork(ni['ep'])

            def fetch_raw(task):
                ck, url, tag, ni = task
                try:
                    resp = http_client.tmdb_get_json(url)
                    return ck, url, tag, ni, resp
                except Exception:
                    return ck, url, tag, ni, None

            def _fetch_missing_show_artwork(items):
                """
                Warm episodes skip cold HTTP — still need show poster/fanart.
                Popular long-runners (TNG) are often warm; obscure shows are cold and
                already pull art in build_http_tasks. Bridge that gap here.
                """
                art_tasks = []
                seen = set()
                for ni in items:
                    ep = ni.get('ep')
                    if not ep:
                        continue
                    sid = ep.showtmdb
                    if not sid or sid in ('0', 'None') or sid in seen or sid in show_artwork:
                        continue
                    if ep.poster not in ('0', '', None):
                        continue
                    seen.add(sid)
                    art_url = (f"https://api.themoviedb.org/3/tv/{sid}"
                               f"?api_key={keys.tmdb_key}&language={self.lang}"
                               f"&append_to_response=external_ids")
                    art_key = cache._hash_function(http_client.tmdb_get_json, art_url, 16)
                    art_tasks.append((art_key, art_url, 'art', ni))
                if not art_tasks:
                    return
                n = min(len(art_tasks), 10)
                results = []
                with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
                    futs = [pool.submit(fetch_raw, t) for t in art_tasks]
                    for f in concurrent.futures.as_completed(futs):
                        with suppress(Exception):
                            results.append(f.result())
                for ck, url, tag, ni, resp in results:
                    if not resp or not isinstance(resp, dict):
                        continue
                    with suppress(Exception):
                        cache.cache_insert(ck, _json.dumps(resp))
                    sid = ni['ep'].showtmdb
                    show_artwork[sid] = resp
                    _apply_artwork(ni['ep'])
                    self._finalize_episode_artwork(ni['ep'])
                # Re-apply to every warm ep sharing those show ids
                for ni in items:
                    ep = ni.get('ep')
                    if ep and ep.showtmdb in show_artwork:
                        _apply_artwork(ep)
                        self._finalize_episode_artwork(ep)

            _fetch_missing_show_artwork(warm_items)

            def _beyond_known_finale(show_tmdb, season, episode) -> bool:
                """
                Trust cached 404 only past a known season finale (Lost E18, Voyager E26).
                Mid-season false poisons (empty {} written as 404) must re-fetch.
                """
                from ..modules import tv_season_cache
                cached = tv_season_cache.get_season_status(show_tmdb, season)
                if not cached:
                    return False
                finale = cached.get('finale_ep')
                try:
                    if finale is not None and int(episode) > int(finale):
                        return True
                except (TypeError, ValueError):
                    pass
                return False

            def build_http_tasks(items, seen_art_set):
                tasks = []
                skipped_404 = 0
                for ni in items:
                    ep = ni['ep']
                    try:
                        show_tmdb = int(ep.showtmdb)
                    except (TypeError, ValueError):
                        show_tmdb = 0
                    cached_miss = (
                        show_tmdb
                        and trakt.check_next_episode_cache(
                            show_tmdb, ep.season, ep.episode, cache_dict=episode_cache_dict
                        )
                        is False
                    )
                    if cached_miss and _beyond_known_finale(show_tmdb, ep.season, ep.episode):
                        skipped_404 += 1
                        # Simkl may still have seeded a title — force ghost scrub later
                        setattr(ep, '_tmdb_ok', False)
                    else:
                        ep_url = (f"https://api.themoviedb.org/3/tv/{ep.showtmdb}"
                                  f"/season/{ep.season}/episode/{ep.episode}"
                                  f"?api_key={keys.tmdb_key}&language={self.lang}")
                        ep_key = cache._hash_function(http_client.tmdb_get_json, ep_url, 16)
                        tasks.append((ep_key, ep_url, 'ep', ni))
                    sid = ep.showtmdb
                    if sid and sid not in ('0', 'None') and sid not in seen_art_set and sid not in show_artwork:
                        seen_art_set.add(sid)
                        art_url = (f"https://api.themoviedb.org/3/tv/{sid}"
                                   f"?api_key={keys.tmdb_key}&language={self.lang}"
                                   f"&append_to_response=external_ids")
                        art_key = cache._hash_function(http_client.tmdb_get_json, art_url, 16)
                        tasks.append((art_key, art_url, 'art', ni))
                if skipped_404:
                    c.log(f"[Progress] Skipped {skipped_404} cached TMDB 404 episode fetch(es)")
                return tasks

            def apply_results(raw_results, target_items):
                """Apply HTTP results to Episode objects and write to SQLite. Returns list of 404 items."""
                ep_404s = []
                for ck, url, tag, ni, resp in raw_results:
                    if tag == 'ep' and not Episode.tmdb_episode_payload_ok(resp):
                        # None = real 404; {{}} / junk = soft fail (do not poison exists cache)
                        if resp is None:
                            self._remember_episode_404(ni['ep'], episode_cache_dict)
                        setattr(ni['ep'], '_tmdb_ok', False)
                        ep_404s.append(ni)
                        continue
                    if resp is None:
                        continue
                    with suppress(Exception):
                        cache.cache_insert(ck, _json.dumps(resp))
                    if tag == 'ep':
                        ep = ni['ep']
                        ep._parse_tmdb_response_minimal(resp)
                        if getattr(ep, '_tmdb_ok', False):
                            with suppress(Exception):
                                trakt.update_next_episode_cache(int(ep.showtmdb), ep.season, ep.episode, True)
                        else:
                            ep_404s.append(ni)
                    elif tag == 'art':
                        sid = ni['ep'].showtmdb
                        show_artwork[sid] = resp if isinstance(resp, dict) else {}
                # Fill missing artwork entries so _apply_artwork skips SQLite lookup
                # Only mark "fetched empty" for sids we actually attempted; leave others
                # absent so a later warm-art pass can still request them.
                attempted_art = {
                    ni['ep'].showtmdb
                    for ck, url, tag, ni, resp in raw_results
                    if tag == 'art' and ni.get('ep') is not None
                }
                for ni in target_items:
                    sid = ni['ep'].showtmdb
                    if sid and sid in attempted_art and sid not in show_artwork:
                        show_artwork[sid] = {}
                return ep_404s

            def run_fallbacks(ep_404_items, fetch_fn):
                """Season-boundary fallbacks: episode 404 -> try season+1 E01."""
                fallback_tasks = []
                for ni in ep_404_items:
                    ep = ni['ep']
                    if ep.episode > 1:
                        fb_url = (f"https://api.themoviedb.org/3/tv/{ep.showtmdb}"
                                  f"/season/{ep.season + 1}/episode/1"
                                  f"?api_key={keys.tmdb_key}&language={self.lang}")
                        fb_key = cache._hash_function(http_client.tmdb_get_json, fb_url, 16)
                        fallback_tasks.append((fb_key, fb_url, 'ep_fallback', ni))
                    else:
                        with suppress(Exception):
                            trakt.update_next_episode_cache(int(ep.showtmdb), ep.season, ep.episode, False)
                if not fallback_tasks:
                    return
                n_fb = min(len(fallback_tasks), 20)
                fb_results = []
                with concurrent.futures.ThreadPoolExecutor(max_workers=n_fb) as pool:
                    futs = [pool.submit(fetch_fn, t) for t in fallback_tasks]
                    for f in concurrent.futures.as_completed(futs):
                        with suppress(Exception):
                            fb_results.append(f.result())
                for fb_key, fb_url, tag, ni, resp in fb_results:
                    ep = ni['ep']
                    if resp:
                        ep.season += 1
                        ep.episode = 1
                        ep._parse_tmdb_response_minimal(resp)
                        with suppress(Exception):
                            cache.cache_insert(fb_key, _json.dumps(resp))
                            trakt.update_next_episode_cache(int(ep.showtmdb), ep.season, ep.episode, True)
                    else:
                        with suppress(Exception):
                            trakt.update_next_episode_cache(int(ep.showtmdb), ep.season, ep.episode, False)

            def _enrich_episode_cold(items, max_workers):
                if not items:
                    return
                if control.monitor.abortRequested():
                    return
                seen_art_inline = set()
                inline_tasks = build_http_tasks(items, seen_art_inline)
                c.log(f"[Progress] Inline HTTP: {len(inline_tasks)} requests ({min(len(inline_tasks), max_workers)} workers)")
                t0 = _time.time()
                inline_results = _run_pool(inline_tasks, fetch_raw, max_workers, label='Inline HTTP')
                c.log(f"[Progress] Inline HTTP done: {sum(1 for r in inline_results if r[4])}/{len(inline_tasks)} in {_time.time()-t0:.2f}s")
                ep_404s = apply_results(inline_results, items)
                run_fallbacks(ep_404s, fetch_raw)
                for ni in items:
                    _apply_artwork(ni['ep'])
                    self._finalize_episode_artwork(ni['ep'])

            # Cold page items: capped sync fetch (widgets use fewer workers to avoid stampede).
            if cold_items:
                _enrich_episode_cold(cold_items, 6 if is_widget else 25)

            # Drop / resolve empty stubs (404 beyond finale — Hijack E09 / Pitt E16 / Alien E09)
            scrubbed = []
            page_hi = min(end, len(next_items))
            for idx, ni in enumerate(next_items):
                if control.monitor.abortRequested():
                    break
                on_page = start <= idx < page_hi
                try:
                    if not on_page:
                        if self._apply_season_boundary(ni, episode_cache_dict):
                            scrubbed.append(ni)
                        continue

                    if self._next_episode_has_meta(ni.get('ep')):
                        scrubbed.append(ni)
                        continue

                    if self._resolve_ghost_next_episode(ni, episode_cache_dict):
                        if not self._next_episode_has_meta(ni.get('ep')):
                            _enrich_episode_cold([ni], 4)
                        if self._next_episode_has_meta(ni.get('ep')):
                            scrubbed.append(ni)
                        else:
                            c.log(
                                f"[Progress] Dropped ghost after resolve: "
                                f"{(ni.get('show_data') or {}).get('title')} "
                                f"S{ni.get('season')}E{ni.get('episode')}"
                            )
                    else:
                        c.log(
                            f"[Progress] Dropped ghost next ep: "
                            f"{(ni.get('show_data') or {}).get('title')} "
                            f"S{ni.get('season')}E{ni.get('episode')}"
                        )
                except Exception as e:
                    c.log(f"[Progress] ghost scrub error: {e}", 1)
                    if on_page and self._next_episode_has_meta(ni.get('ep')):
                        scrubbed.append(ni)

            if len(scrubbed) != len(next_items):
                c.log(f"[Progress] Ghost scrub: {len(next_items)} → {len(scrubbed)}")
            next_items = scrubbed

            # Drop known-future premieres after TMDB enrich (Simkl often has no date
            # until meta fill — Grey's S23 / 9-1-1 S10 / Ghosts S06 etc.)
            aired_kept = []
            for ni in next_items:
                if self._next_episode_is_unaired(ni):
                    show_title = (ni.get('show_data') or {}).get('title') or '?'
                    air = self._next_episode_air_date(ni) or 'unknown'
                    c.log(
                        f"[Progress] Drop unaired next: {show_title} "
                        f"S{ni.get('season')}E{ni.get('episode')} (airs {air})"
                    )
                    continue
                aired_kept.append(ni)
            if len(aired_kept) != len(next_items):
                c.log(
                    f"[Progress] Unaired filter: {len(next_items)} → {len(aired_kept)}"
                )
            next_items = aired_kept

            page_items = next_items[start:end]
            bg_items = next_items[end:]

            # --- Background cache-warming: items beyond this page ---
            # Silently warms SQLite so next pages load instantly.
            if bg_items and not control.monitor.abortRequested():
                def _bg_warm(items):
                    try:
                        if control.monitor.abortRequested():
                            return
                        seen_bg = set(show_artwork.keys())
                        bg_tasks = build_http_tasks(items, seen_bg)
                        bg_results = _run_pool(bg_tasks, fetch_raw, min(len(bg_tasks), 20), label='Background warm')
                        bg_404s = apply_results(bg_results, items)
                        if not control.monitor.abortRequested():
                            run_fallbacks(bg_404s, fetch_raw)
                        c.log(f"[Progress] Background warm done: {sum(1 for r in bg_results if r[4])}/{len(bg_tasks)} cached")
                    except Exception as ex:
                        c.log(f"[Progress] Background warm error: {ex}")

                threading.Thread(target=_bg_warm, args=(bg_items,), daemon=True, name="CrewWidgetWarm").start()

            # Build final sorted list, slice to current page
            all_episodes = []
            for ni in next_items:
                if 'ep' not in ni:
                    continue
                self._finalize_episode_artwork(ni['ep'])
                all_episodes.append(ni['ep'].to_dict())
            all_episodes.sort(key=lambda x: x.get('last_watched_at') or '1900-01-01T00:00:00.000Z', reverse=True)
            page_episodes = all_episodes[start:end]
            has_next = end < len(all_episodes)

            import sys as _sys
            _base_url = (_sys.argv[0] + '?action=progress_next_episodes') if _sys.argv else ''
            next_url = (f'{_base_url}&page={page + 1}') if has_next else None
            _total_pages = (len(all_episodes) + PAGE_SIZE - 1) // PAGE_SIZE
            next_label = (f"{c.lang(30500) or 'Next Page'} (Page {page + 1} of {_total_pages})") if has_next else None

            self.list = page_episodes
            c.log(f"[Progress] Rendering page {page}: items {start+1}-{min(end, len(all_episodes))} of {len(all_episodes)} ({len(warm_items)} warm, {len(cold_items)} inline-cold, {len(bg_items)} bg)")
            self.episodes_directory(self.list, next_url=next_url, next_label=next_label)
            return self.list

        except Exception as e:
            c.log(f"[Progress] Error in get_next_episodes: {e}")
            self.list = []
            self.episodes_directory(self.list)
            return self.list

    def get_in_progress_episodes(self, page=None):
        """
        Get episodes with resume points (partially watched).
        Shows episodes user started but hasn't finished (0% < progress < 92%).

        Args:
            page: Page number (1-based). None/1 = first page.

        Returns:
            list: List of Episode objects as dicts
        """
        PAGE_SIZE = 25
        try:
            page = max(1, int(page)) if page else 1
        except (ValueError, TypeError):
            page = 1
        try:
            if tracker.simkl_live():
                c.log("[Progress] Fetching In Progress Episodes (Simkl playback)")
                is_widget = c.is_widget_listing()
                result = None  # Phase 0 uses simkl sessions below
                simkl_sessions = simkl.crew_episode_playback_sessions() or []
            elif not _trakt_progress_active():
                c.log("[Progress] In Progress Episodes skipped — no active tracker progress source", 1)
                self.list = []
                self.episodes_directory(self.list)
                return self.list
            else:
                c.log("[Progress] Fetching In Progress Episodes")
                is_widget = c.is_widget_listing()
                simkl_sessions = None
                # Get playback progress from Trakt (serialized during widget reload bursts)
                result = trakt.get_sync_playback_episodes_extended(log_label='in_progress_episodes')

            if tracker.simkl_live():
                if not simkl_sessions:
                    c.log("[Progress] No Simkl playback progress data")
                    self.list = []
                    self.episodes_directory(self.list)
                    return self.list
            elif not result:
                c.log("[Progress] No Trakt playback progress data")
                self.list = []
                self.episodes_directory(self.list)
                return self.list

            # --- Phase 0: Build Episode objects from tracker data only (no network) ---
            prog_items = []  # [{'ep': Episode, 'last_watched_at': str}]
            if tracker.simkl_live():
                for session in simkl_sessions:
                    try:
                        progress = float(session.get('progress', 0))
                        if progress <= 0 or progress >= 92:
                            continue
                        season_num = session.get('season')
                        episode_num = session.get('episode')
                        show_data = session.get('show_data') or {}
                        episode_data = session.get('episode_data') or {}
                        showtmdb = str((show_data.get('ids') or {}).get('tmdb') or '0')
                        if not showtmdb or showtmdb in ('None', '0'):
                            continue
                        ep = Episode.from_trakt_progress(
                            show_data, season_num, episode_num, episode_data
                        )
                        if not ep:
                            continue
                        ep.resume_point = progress
                        paused_at = session.get('paused_at', '')
                        ep.last_watched_at = paused_at
                        prog_items.append({'ep': ep, 'last_watched_at': paused_at})
                    except Exception as e:
                        c.log(f"[Progress] Phase 0 Simkl episode error: {e}")
            else:
                for item in result:
                    try:
                        progress = float(item.get('progress', 0))
                        if progress <= 0 or progress >= 92:
                            continue

                        episode_data = item.get('episode', {})
                        show_data = item.get('show', {})
                        season_num = episode_data.get('season')
                        episode_num = episode_data.get('number')
                        if not season_num or not episode_num:
                            continue

                        showtmdb = str(show_data.get('ids', {}).get('tmdb', '0'))
                        if not showtmdb or showtmdb in ('None', '0'):
                            continue

                        ep = Episode.from_trakt_progress(show_data, season_num, episode_num, episode_data)
                        if not ep:
                            continue

                        ep.resume_point = progress
                        paused_at = item.get('paused_at', '')
                        ep.last_watched_at = paused_at

                        prog_items.append({'ep': ep, 'last_watched_at': paused_at})
                    except Exception as e:
                        c.log(f"[Progress] Phase 0 episode error: {e}")

            c.log(f"[Progress] Phase 0: {len(prog_items)} in-progress episodes")

            # Sort by most recently paused
            prog_items.sort(
                key=lambda pi: pi['last_watched_at'] or '1900-01-01T00:00:00.000Z',
                reverse=True
            )

            # Slice to current page
            start = (page - 1) * PAGE_SIZE
            end = start + PAGE_SIZE
            page_items = prog_items[start:end]
            bg_items = prog_items[end:]  # background warm only

            episode_cache_dict = trakt.batch_load_episode_cache_for_shows(
                self._collect_show_tmdb_ids(page_items)
            ) or {}

            # Single SQL query: which page episodes are already in metadata cache?
            meta_keys = [
                Episode.get_metadata_cache_key_minimal(pi['ep'].showtmdb, pi['ep'].season, pi['ep'].episode, self.lang)
                for pi in page_items
            ]
            meta_ttl = None if is_widget else 24
            metadata_cache_dict = cache.cache_get_many(meta_keys, timeout=meta_ttl) if meta_keys else {}

            warm_items, cold_items = [], []
            for pi in page_items:
                key = Episode.get_metadata_cache_key_minimal(pi['ep'].showtmdb, pi['ep'].season, pi['ep'].episode, self.lang)
                if key in metadata_cache_dict:
                    warm_items.append(pi)
                else:
                    cold_items.append(pi)
            c.log(f"[Progress] Page {page}: warm={len(warm_items)}, cold={len(cold_items)}, bg_warm={len(bg_items)}")

            # Enrich warm episodes inline from SQLite (no network, instant)
            from ..modules import http_client
            show_artwork = {}  # artwork dedup by showtmdb

            def _apply_artwork(ep):
                self._apply_show_artwork_from_cache(ep, show_artwork, self.lang)

            for pi in warm_items:
                pi['ep'].fetch_tmdb_metadata_minimal(metadata_cache_dict=metadata_cache_dict)
                _apply_artwork(pi['ep'])
                self._finalize_episode_artwork(pi['ep'])

            import json as _json
            import time as _time

            def fetch_raw(task):
                ck, url, tag, pi = task
                try:
                    resp = http_client.tmdb_get_json(url)
                    return ck, url, tag, pi, resp
                except Exception:
                    return ck, url, tag, pi, None

            def build_http_tasks(items, seen_art_set):
                tasks = []
                skipped_404 = 0
                for pi in items:
                    ep = pi['ep']
                    try:
                        show_tmdb = int(ep.showtmdb)
                    except (TypeError, ValueError):
                        show_tmdb = 0
                    if show_tmdb and trakt.check_next_episode_cache(
                        show_tmdb, ep.season, ep.episode, cache_dict=episode_cache_dict
                    ) is False:
                        skipped_404 += 1
                    else:
                        ep_url = (f"https://api.themoviedb.org/3/tv/{ep.showtmdb}"
                                  f"/season/{ep.season}/episode/{ep.episode}"
                                  f"?api_key={keys.tmdb_key}&language={self.lang}")
                        ep_key = cache._hash_function(http_client.tmdb_get_json, ep_url, 16)
                        tasks.append((ep_key, ep_url, 'ep', pi))
                    sid = ep.showtmdb
                    if sid and sid not in ('0', 'None') and sid not in seen_art_set and sid not in show_artwork:
                        seen_art_set.add(sid)
                        art_url = (f"https://api.themoviedb.org/3/tv/{sid}"
                                   f"?api_key={keys.tmdb_key}&language={self.lang}"
                                   f"&append_to_response=external_ids")
                        art_key = cache._hash_function(http_client.tmdb_get_json, art_url, 16)
                        tasks.append((art_key, art_url, 'art', pi))
                if skipped_404:
                    c.log(f"[Progress] Skipped {skipped_404} cached TMDB 404 episode fetch(es)")
                return tasks

            def apply_results(raw_results, target_items):
                for ck, url, tag, pi, resp in raw_results:
                    if resp is None:
                        if tag == 'ep':
                            self._remember_episode_404(pi['ep'], episode_cache_dict)
                        continue
                    with suppress(Exception):
                        cache.cache_insert(ck, _json.dumps(resp))
                    if tag == 'ep':
                        pi['ep']._parse_tmdb_response_minimal(resp)
                        with suppress(Exception):
                            trakt.update_next_episode_cache(int(pi['ep'].showtmdb), pi['ep'].season, pi['ep'].episode, True)
                    elif tag == 'art':
                        sid = pi['ep'].showtmdb
                        show_artwork[sid] = resp if isinstance(resp, dict) else {}
                for pi in target_items:
                    sid = pi['ep'].showtmdb
                    if sid and sid not in show_artwork:
                        show_artwork[sid] = {}

            def _enrich_inprogress_cold(items, max_workers):
                if not items:
                    return
                seen_art_inline = set()
                inline_tasks = build_http_tasks(items, seen_art_inline)
                n = min(len(inline_tasks), max_workers)
                c.log(f"[Progress] InProgress inline HTTP: {len(inline_tasks)} requests ({n} workers)")
                t0 = _time.time()
                inline_results = []
                with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
                    futs = [pool.submit(fetch_raw, t) for t in inline_tasks]
                    for f in concurrent.futures.as_completed(futs):
                        with suppress(Exception):
                            inline_results.append(f.result())
                c.log(f"[Progress] InProgress inline HTTP done: {sum(1 for r in inline_results if r[4])}/{len(inline_tasks)} in {_time.time()-t0:.2f}s")
                apply_results(inline_results, items)
                for pi in items:
                    _apply_artwork(pi['ep'])
                    self._finalize_episode_artwork(pi['ep'])

            if cold_items:
                _enrich_inprogress_cold(cold_items, 6 if is_widget else 25)

            # Background cache-warming: items beyond this page
            if bg_items:
                def _bg_warm(items):
                    try:
                        seen_bg = set(show_artwork.keys())
                        bg_tasks = build_http_tasks(items, seen_bg)
                        n_bg = min(len(bg_tasks), 20)
                        bg_results = []
                        with concurrent.futures.ThreadPoolExecutor(max_workers=n_bg) as pool:
                            futs = [pool.submit(fetch_raw, t) for t in bg_tasks]
                            for f in concurrent.futures.as_completed(futs):
                                with suppress(Exception):
                                    bg_results.append(f.result())
                        for ck, url, tag, pi, resp in bg_results:
                            if resp:
                                with suppress(Exception):
                                    cache.cache_insert(ck, _json.dumps(resp))
                        c.log(f"[Progress] InProgress bg warm done: {sum(1 for r in bg_results if r[4])}/{len(bg_tasks)} cached")
                    except Exception as ex:
                        c.log(f"[Progress] InProgress bg warm error: {ex}")

                threading.Thread(target=_bg_warm, args=(bg_items,), daemon=True, name="CrewInProgWarm").start()

            # Build final sorted list, slice to current page
            all_episodes = []
            for pi in prog_items:
                self._finalize_episode_artwork(pi['ep'])
                all_episodes.append(pi['ep'].to_dict())
            all_episodes.sort(
                key=lambda x: x.get('last_watched_at') or '1900-01-01T00:00:00.000Z',
                reverse=True
            )
            page_episodes = all_episodes[start:end]
            has_next = end < len(all_episodes)

            import sys as _sys
            _base_url = (_sys.argv[0] + '?action=progress_in_progress_episodes') if _sys.argv else ''
            next_url = (f'{_base_url}&page={page + 1}') if has_next else None
            _total_pages = (len(all_episodes) + PAGE_SIZE - 1) // PAGE_SIZE
            next_label = (f"{c.lang(30500) or 'Next Page'} (Page {page + 1} of {_total_pages})") if has_next else None

            self.list = page_episodes
            c.log(f"[Progress] Rendering page {page}: items {start+1}-{min(end, len(all_episodes))} of {len(all_episodes)} ({len(warm_items)} warm, {len(cold_items)} cold)")
            self.episodes_directory(self.list, next_url=next_url, next_label=next_label)
            return self.list

        except Exception as e:
            c.log(f"[Progress] Error in get_in_progress_episodes: {e}")
            self.list = []
            self.episodes_directory(self.list)
            return self.list

    def shows_directory(self, items, next_url=None, next_label=None):
        """
        Create Kodi directory listing for TV shows.
        Shows show posters in ICONS view.

        Args:
            items (list): List of show dictionaries
            next_url (str): Optional URL for the next-page item
        """
        if not items:
            import sys as _sys
            syshandle = int(_sys.argv[1])
            control.add_empty_directory_placeholder(syshandle)
            control.close_directory(
                handle=syshandle,
                content_type='tvshows',
                cacheToDisc=False,
            )
            return

        from ..indexers import tvshows
        from ..modules import playcount
        from ..modules.listitem import ListItemInfoTag
        import sys
        import json
        from urllib.parse import quote_plus

        sysaddon = sys.argv[0]
        syshandle = int(sys.argv[1])

        trakt_credentials = trakt.get_trakt_credentials_info()
        indicators = playcount.get_tvshow_indicators()
        addon_poster = c.addon_poster()

        for i in items:
            try:
                label = i.get('tvshowtitle', i.get('title', ''))
                imdb = i.get('imdb', '')
                tmdb = i.get('tmdb', '')
                tvdb = i.get('tvdb', '')
                year = i.get('year', '')

                # Use addon poster/fanart as fallback for missing artwork ('0' means not found)
                poster = i.get('poster', addon_poster)
                if poster in ['0', '', None]:
                    poster = addon_poster

                fanart = i.get('fanart', c.addon_fanart())
                if fanart in ['0', '', None]:
                    fanart = c.addon_fanart()

                systitle = quote_plus(label)

                # URL to open seasons list
                url = f'{sysaddon}?action=seasons&tvshowtitle={systitle}&year={year}&imdb={imdb}&tmdb={tmdb}&tvdb={tvdb}'

                # Create listitem
                try:
                    listitem = control.item(label=label, offscreen=True)
                except Exception:
                    listitem = control.item(label=label)

                # Set artwork
                listitem.setArt({
                    'icon': poster,
                    'thumb': poster,
                    'poster': poster,
                    'tvshow.poster': poster,
                    'fanart': fanart
                })

                # Set info using InfoTag
                info_tag = ListItemInfoTag(listitem, 'video')

                infodata = {
                    'title': label,
                    'tvshowtitle': label,
                    'year': year,
                    'plot': i.get('plot', ''),
                    'mediatype': 'tvshow'
                }

                info_tag.set_info(control.tagdataClean(infodata))
                info_tag.set_unique_ids({'imdb': imdb, 'tmdb': tmdb, 'tvdb': tvdb})

                # Add to directory
                control.addItem(handle=syshandle, url=url, listitem=listitem, isFolder=True)

            except Exception as e:
                c.log(f"[Progress] Error adding show to directory: {e}")
                continue

        if next_url:
            import os as _os
            _art = c.get_art_path()
            _next_icon = _os.path.join(_art, 'next.png') if _art else c.addon_poster()
            next_item = control.item(label=next_label or c.lang(30500) or 'Next Page', offscreen=True)
            next_item.setArt({'icon': _next_icon, 'thumb': _next_icon, 'poster': _next_icon})
            control.addItem(handle=syshandle, url=next_url, listitem=next_item, isFolder=True)

        control.content(syshandle, 'tvshows')
        control.directory(syshandle, cacheToDisc=True)

    def episodes_directory(self, items, suppress_empty_dialog=False, next_url=None, next_label=None):
        """
        Create Kodi directory listing for episodes.
        Shows episode stills in ICONS view.

        Args:
            items (list): List of episode dictionaries
            suppress_empty_dialog (bool): When True and items is empty, end directory
                silently (no info dialog). Used when background loading will follow.
        """
        if not items:
            if suppress_empty_dialog:
                # Cold-cache first load: show a "Loading..." placeholder so the user
                # sees SOMETHING immediately instead of a blank widget.
                # Background thread will call Container.Refresh once real data is cached.
                import sys
                syshandle = int(sys.argv[1])
                try:
                    placeholder = control.item(label=c.lang(32099) or 'Loading…', offscreen=True)
                except Exception:
                    placeholder = control.item(label='Loading your shows…')
                placeholder.setArt({'icon': c.addon_poster(), 'thumb': c.addon_poster(), 'poster': c.addon_poster()})
                hint_url = sys.argv[0] + sys.argv[2]  # Same plugin URL -> refresh re-runs us
                control.addItem(handle=syshandle, url=hint_url, listitem=placeholder, isFolder=False)
                control.content(syshandle, 'episodes')
                control.directory(syshandle, cacheToDisc=False)
                return
            import sys
            syshandle = int(sys.argv[1])
            control.add_empty_directory_placeholder(syshandle)
            control.close_directory(
                handle=syshandle,
                content_type='episodes',
                cacheToDisc=False,
            )
            return

        from ..modules import playcount
        from ..modules import bookmarks
        from ..modules.listitem import ListItemInfoTag
        import sys
        import json
        from urllib.parse import quote_plus

        sysaddon = sys.argv[0]
        syshandle = int(sys.argv[1])

        indicators = playcount.get_tvshow_indicators()
        is_playable = 'plugin' not in control.infoLabel('Container.PluginName')

        # Check tracker credentials (XOR)
        trakt_credentials = trakt.get_trakt_credentials_info()
        simkl_live = tracker.simkl_live()

        # Context menu labels (language strings)
        playback_menu = control.lang(32063) if control.setting('hosts.mode') == '2' else control.lang(32064)
        watched_menu, unwatched_menu = playcount.watched_menu_labels()
        queue_menu = control.lang(32065)
        trakt_manager_menu = control.lang(32515)
        simkl_manager_menu = control.lang(90314)
        add_to_library = control.lang(32551)
        clear_resume_menu = control.lang(90237)
        clear_providers = control.lang(32098)
        infoMenu = control.lang(32101)
        # Widget + in-addon: playcount/clear-resume need this URL to refresh the list
        current_container = quote_plus(sysaddon + sys.argv[2])

        for i in items:
            try:
                label = i.get('title', i.get('label', ''))
                season = str(i.get('season', 0)).zfill(2)
                episode = str(i.get('episode', 0)).zfill(2)
                tvshowtitle = i.get('tvshowtitle', '')

                # Build episode label with TV show title (includes show name for multi-show views)
                ep_label = f'{tvshowtitle} (S{season}E{episode}) : {label}'

                # For episode playback, we need SHOW IDs (episodes don't have their own IMDB/TMDB IDs)
                # If episode-level IDs are missing/invalid, use show-level IDs
                imdb = i.get('imdb', '0')
                if imdb in ['0', '', None]:
                    imdb = i.get('showimdb', '0')

                # Ensure we never pass invalid imdb (use empty string for URL param consistency)
                if imdb in ['0', '', None]:
                    imdb = ''

                tmdb = i.get('tmdb', '0')
                if tmdb in ['0', '', None]:
                    tmdb = i.get('showtmdb', '0')

                # Ensure valid tmdb (should always be available from Trakt)
                if tmdb in ['0', '', None]:
                    tmdb = ''

                tvdb = i.get('tvdb', '0')
                if tvdb in ['0', '', None]:
                    tvdb = i.get('showtvdb', '0')

                year = i.get('year', '')
                duration = i.get('duration', 45)
                try:
                    duration = int(duration)
                except (TypeError, ValueError):
                    duration = 45
                if duration <= 0:
                    duration = 45

                # Episode widgets use thumb/icon; fall back to show poster then fanart
                thumb = i.get('thumb', '0')
                poster = i.get('poster', '0')
                fanart = i.get('fanart', '0')
                display_art = c.addon_poster()
                for candidate in (thumb, poster, fanart):
                    if candidate not in ['0', '', None]:
                        display_art = candidate
                        break
                if thumb in ['0', '', None]:
                    thumb = display_art
                if poster in ['0', '', None]:
                    poster = display_art
                if fanart in ['0', '', None]:
                    fanart = c.addon_fanart()

                # Build playback URL (match existing episodes.py format)
                systitle = quote_plus(label)
                systvshowtitle = quote_plus(tvshowtitle)

                premiered = i.get('premiered', i.get('first_aired', ''))
                syspremiered = quote_plus(premiered) if premiered else '0'

                # Build meta dict - keep required artwork fields even if '0' for old episodes.py compatibility
                required_fields = {'banner', 'clearlogo', 'clearart', 'landscape'}
                meta = {
                    k: v for k, v in i.items()
                    if v not in [None, ''] or k in required_fields
                }
                # Ensure required fields exist with '0' default
                for field in required_fields:
                    if field not in meta:
                        meta[field] = '0'

                sysmeta = quote_plus(json.dumps(meta))

                # Import time for unique cache-busting parameter
                import time
                systime = str(int(time.time()))

                url = (f'{sysaddon}?action=play&title={systitle}&year={year}&imdb={imdb}&tmdb={tmdb}'
                       f'&season={i.get("season")}&episode={i.get("episode")}'
                       f'&tvshowtitle={systvshowtitle}&premiered={syspremiered}&meta={sysmeta}&t={systime}')

                # Create listitem
                try:
                    listitem = control.item(label=ep_label, offscreen=True)
                except Exception:
                    listitem = control.item(label=ep_label)

                if is_playable:
                    # Home widget resolve cannot Directory/Container.Update - force Autoplay.
                    url = control.stamp_widget_autoplay(url)
                    listitem.setProperty('IsPlayable', 'true')

                # Set artwork
                listitem.setArt({
                    'icon': thumb,
                    'thumb': thumb,
                    'poster': poster,
                    'tvshow.poster': poster,
                    'season.poster': poster,
                    'fanart': fanart
                })

                # Get watch status
                try:
                    overlay = int(playcount.get_episode_overlay(indicators, imdb, tmdb, i.get('season'), i.get('episode')))
                except Exception:
                    overlay = 6

                # Prepare comprehensive metadata for InfoTag
                infodata = {
                    'title': label,
                    'tvshowtitle': tvshowtitle,
                    'season': i.get('season'),
                    'episode': i.get('episode'),
                    'year': year,
                    'plot': i.get('plot', ''),
                    'duration': str(int(duration) * 60),  # convert to seconds
                    'mediatype': 'episode',
                    'playcount': 1 if overlay == 7 else 0,
                    'overlay': overlay,
                    'rating': i.get('rating', '0'),
                    'votes': i.get('votes', '0'),
                    'premiered': i.get('premiered', ''),
                    'aired': i.get('first_aired', i.get('premiered', '')),
                    'mpaa': i.get('mpaa', ''),
                }

                # Add lists (genre, studio, director, writer)
                if genre := i.get('genre'):
                    infodata['genre'] = c.string_split_to_list(genre)
                if studio := i.get('studio'):
                    infodata['studio'] = c.string_split_to_list(studio)
                if director := i.get('director'):
                    infodata['director'] = c.string_split_to_list(director)
                if writer := i.get('writer'):
                    infodata['writer'] = c.string_split_to_list(writer)

                # Build trailer URL for episode (uses show's IDs)
                trailer = i.get('trailer', '')
                if not trailer or trailer in ('0', '', None):
                    # Build trailer plugin URL from show TMDB/IMDB with metadata for poster display
                    trailer = f'{sysaddon}?action=trailer&name={quote_plus(tvshowtitle)}&imdb={imdb}&tmdb={tmdb}&mediatype=episode&season={i.get("season")}&episode={i.get("episode")}&meta={sysmeta}'
                infodata['trailer'] = trailer

                info_tag = ListItemInfoTag(listitem, 'video')
                info_tag.set_info(control.tagdataClean(infodata))
                info_tag.set_unique_ids({'imdb': imdb, 'tmdb': tmdb, 'tvdb': tvdb})

                # Set trailer explicitly on InfoTag for information dialog
                if trailer and trailer not in ('0', '', None):
                    # c.log(f"[CM Debug @ progress.py] Setting trailer on InfoTag: {trailer}")
                    info_tag._info_tag.setTrailer(trailer)

                # Set cast separately (required format: list of dicts with name, role, thumbnail)
                if castwiththumb := i.get('castwiththumb'):
                    if castwiththumb and castwiththumb != '0':
                        info_tag.set_cast(castwiththumb)

                # Get resume point early (needed for context menu)
                resume_point = i.get('resume_point')
                has_resume = resume_point is not None and resume_point > 0

                # Build context menu in logical order (matching episodes.py)
                cm = []

                # 1. Queue
                cm.append((queue_menu, f'RunPlugin({sysaddon}?action=queueItem)'))

                # 2. Browse Series (all seasons) — ids only; episode sysmeta is huge and
                # incomplete for Episodes.tmdb_list art enrichment (status etc.).
                browse_series_label = control.lang(32071)
                browse_series_url = (
                    f'ActivateWindow(Videos,{sysaddon}?action=seasons'
                    f'&tvshowtitle={systvshowtitle}&year={year}&imdb={imdb}&tmdb={tmdb},return)'
                )
                cm.append((browse_series_label, browse_series_url))

                # 3. Browse Season (all episodes in this season)
                browse_season_label = f'Browse Season {i.get("season")}'
                browse_season_url = (
                    f'ActivateWindow(Videos,{sysaddon}?action=episodes'
                    f'&tvshowtitle={systvshowtitle}&year={year}&imdb={imdb}&tmdb={tmdb}'
                    f'&season={i.get("season")},return)'
                )
                cm.append((browse_season_label, browse_season_url))

                # 4. Information (replaces system "Information" menu)
                cm.append((infoMenu, 'Action(Info)'))

                # 5. Watch status (watched/unwatched) - use already calculated overlay
                if overlay == 7:
                    cm.append((unwatched_menu, f'RunPlugin({sysaddon}?action=episodePlaycount&imdb={imdb}&tmdb={tmdb}&season={i.get("season")}&episode={i.get("episode")}&query=6&redirect={current_container})'))
                else:
                    cm.append((watched_menu, f'RunPlugin({sysaddon}?action=episodePlaycount&imdb={imdb}&tmdb={tmdb}&season={i.get("season")}&episode={i.get("episode")}&query=7&redirect={current_container})'))

                # 6. Clear Resume Point (if there's a resume point)
                if has_resume:
                    cm.append((clear_resume_menu, f'RunPlugin({sysaddon}?action=episodeClearBookmark&imdb={imdb}&tmdb={tmdb}&season={i.get("season")}&episode={i.get("episode")}&redirect={current_container})'))

                # 7. Tracker Manager (Trakt XOR Simkl)
                if simkl_live:
                    cm.append((simkl_manager_menu, f'RunPlugin({sysaddon}?action=simklManager&name={systvshowtitle}&imdb={imdb}&tmdb={tmdb}&content=tvshow)'))
                elif trakt_credentials:
                    cm.append((trakt_manager_menu, f'RunPlugin({sysaddon}?action=traktManager&name={systvshowtitle}&tmdb={tmdb}&content=tvshow)'))

                # 8. Playback Menu (Alter Sources)
                cm.append((playback_menu, f'RunPlugin({sysaddon}?action=alter_sources&url={quote_plus(url)}&meta={sysmeta})'))

                # 9. Add to Library
                cm.append((add_to_library, f'RunPlugin({sysaddon}?action=tvshowToLibrary&tvshowtitle={systvshowtitle}&year={year}&imdb={imdb}&tmdb={tmdb})'))

                # 10. Clear Providers Cache
                cm.append((clear_providers, f'RunPlugin({sysaddon}?action=clearSources)'))

                # Note: System context menus cannot be removed (Kodi API limitation since v17/2016)
                listitem.addContextMenuItems(cm, replaceItems=True)

                # Handle resume point if present (for In Progress Episodes)
                if has_resume:
                    # resume_point is a percentage (0-100)
                    duration_seconds = int(duration) * 60

                    # Skip if duration is 0 or invalid (prevents division by zero)
                    if duration_seconds > 0:
                        # Convert percentage to seconds
                        resume_seconds = (duration_seconds * resume_point) / 100.0

                        # Calculate remaining time in minutes
                        remaining_seconds = duration_seconds - resume_seconds
                        remaining_minutes = int(remaining_seconds / 60)

                        # Add remaining time to label in gold
                        if remaining_minutes > 0:
                            ep_label = f'{ep_label} {color("(" + str(remaining_minutes) + " mins left)", 'secondary')}'
                            listitem.setLabel(ep_label)

                        # Set resume point using InfoTag (Kodi v19+ method)
                        # This automatically sets the overlay indicator (arrow vs checkmark)
                        try:
                            infodata['offset'] = resume_seconds
                            info_tag.set_resume_point(infodata, 'offset', 'duration', False)
                        except Exception as e:
                            c.log(f"[Progress] Error setting resume point: {e}")


                # Add to directory
                control.addItem(handle=syshandle, url=url, listitem=listitem, isFolder=False)

            except Exception as e:
                c.log(f"[Progress] Error adding episode to directory: {e}")

                c.log(f"[Progress] Traceback: {traceback.format_exc()}")
                continue

        if next_url:
            try:
                import os as _os
                _art = c.get_art_path()
                _next_icon = _os.path.join(_art, 'next.png') if _art else c.addon_poster()
                next_item = control.item(label=next_label or c.lang(30500) or 'Next Page', offscreen=True)
                next_item.setArt({'icon': _next_icon, 'thumb': _next_icon, 'poster': _next_icon})
                control.addItem(handle=syshandle, url=next_url, listitem=next_item, isFolder=True)
            except Exception as e:
                c.log(f"[Progress] Error adding next page item: {e}")

        control.content(syshandle, 'episodes')
        control.directory(syshandle, cacheToDisc=True)

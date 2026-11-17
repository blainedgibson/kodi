# -*- coding: utf-8 -*-

'''
********************************************************cm*
* Midnight Add-on
*
* @file tvevening.py
* @package script.module.midnight
*
* @copyright (c) 2026, Midnight
* @license GNU General Public License, version 3 (GPL-3.0)
*
********************************************************cm*
TV Evening - Watch your shows in sequence
'''

import json
import random
import sys
import traceback
from datetime import date, datetime, timedelta
from urllib.parse import quote_plus

import xbmc
import xbmcgui
import xbmcplugin

from . import control
from . import trakt
from . import simkl
from . import tracker
from .crewruntime import c
from . import tvevening_playlist_db
from . import tvevening_monitor
from . import tv_season_cache
from ..models.episode import Episode


def _close_plugin_handle(succeeded=False):
    """Dismiss Kodi's plugin busy state when a dialog-only route finishes."""
    try:
        handle = int(sys.argv[1])
    except (IndexError, ValueError, TypeError):
        return
    try:
        xbmcplugin.endOfDirectory(handle, succeeded=succeeded)
    except Exception:
        pass
    try:
        control.idle()
    except Exception:
        pass


def tracker_ready():
    """Active XOR tracker (Simkl or Trakt) is authorized."""
    try:
        return tracker.simkl_live() or tracker.trakt_live()
    except Exception:
        return False


def is_trakt_enabled():
    """Compat: Trakt authorized when it is the live tracker."""
    try:
        return tracker.trakt_live()
    except Exception:
        return False


def _prompt_tracker_setup():
    """Prompt auth for the active tracker. Returns True when live after setup."""
    # Honour "Not Now" for this Kodi session so Home / widget reloads do not
    # re-open the authorize dialog every boot (#253).
    try:
        if control.window.getProperty('midnight.tvevening.auth_declined') == '1':
            return False
    except Exception:
        pass

    if tracker.is_simkl():
        setup_now = c.yesnoDialog(
            'TV Evening needs Simkl to find next episodes from shows you\'re watching.\n\n'
            'Would you like to set up Simkl now?',
            'Simkl Required',
            nolabel='Not Now',
            yeslabel='Set Up Simkl',
        )
        if setup_now:
            simkl.auth_simkl()
            return tracker.simkl_live()
        try:
            control.window.setProperty('midnight.tvevening.auth_declined', '1')
        except Exception:
            pass
        return False

    setup_now = c.yesnoDialog(
        'TV Evening needs Trakt to track what you have watched and find your next episodes.\n\n'
        'Would you like to set up Trakt now?',
        'Trakt Required',
        nolabel='Not Now',
        yeslabel='Set Up Trakt',
    )
    if setup_now:
        trakt.auth_trakt()
        return tracker.trakt_live()
    try:
        control.window.setProperty('midnight.tvevening.auth_declined', '1')
    except Exception:
        pass
    return False


def _prompt_trakt_setup():
    """Compat alias for older call sites."""
    return _prompt_tracker_setup()


def get_show_playlist_setting():
    """Check if playlist preview should be shown."""
    try:
        return control.setting('tvevening.show_playlist') == 'true'
    except:
        return True


def get_gap_countdown():
    """Get gap countdown duration in seconds (default 60)."""
    try:
        return int(control.setting('tvevening.gap_countdown'))
    except:
        return 60


def get_randomize():
    """Check if show order should be randomized."""
    try:
        return control.setting('tvevening.randomize') == 'true'
    except:
        return False


def get_source_count():
    """Get number of shows to include (default 15)."""
    try:
        return int(control.setting('tvevening.source_count'))
    except:
        return 15


def get_min_episode_age():
    """Get minimum episode age in hours (default 48)."""
    try:
        return int(control.setting('tvevening.min_episode_age'))
    except:
        return 48


def _parse_episode_air_date(value):
    """Parse TMDB/Trakt air-date values to a calendar date (YYYY-MM-DD or ISO)."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s in ('0', 'none', 'None'):
        return None
    head = s[:10]
    if len(head) == 10 and head[4] == '-' and head[7] == '-':
        try:
            return date(int(head[0:4]), int(head[5:7]), int(head[8:10]))
        except (ValueError, TypeError):
            pass
    try:
        return date.fromisoformat(head)
    except (ValueError, TypeError):
        return None


def episode_passes_air_date_filter(ep, min_age_hours, now=None):
    """
    Decide whether an episode is eligible for TV Evening based on TMDB air date.

    When min_age_hours > 0:
    - reject missing/invalid air_date (unaired or unknown)
    - reject future calendar dates
    - reject episodes newer than the min-age cutoff

    Returns:
        tuple(bool, str): (eligible, reason)
    """
    if min_age_hours <= 0:
        return True, 'filter_disabled'

    now = now or datetime.utcnow()
    cutoff_date = (now - timedelta(hours=min_age_hours)).date()
    today = now.date()

    raw = ep.get('air_date') or ep.get('first_aired')
    aired = _parse_episode_air_date(raw)
    if not aired:
        if raw:
            if c.devmode:
                c.log(f"[TV Evening] Unparseable air_date value: {raw!r}")
            return False, 'invalid_air_date'
        return False, 'no_air_date'

    if aired > today:
        return False, 'future_air_date'

    if aired > cutoff_date:
        return False, 'too_recent'

    return True, 'ok'


def _log_air_date_decision(ep, eligible, reason, min_age_hours):
    """Log accept/skip decisions for TV Evening air-date filtering."""
    title = ep.get('tvshowtitle', 'Unknown')
    season = ep.get('season', 0)
    episode = ep.get('episode', 0)
    air_date_str = ep.get('air_date') or ep.get('first_aired') or 'none'
    if eligible:
        c.log(f"[TV Evening] ACCEPTED {title} S{season:02d}E{episode:02d} - air_date={air_date_str} ({reason})")
    else:
        c.log(f"[TV Evening] SKIPPING {title} S{season:02d}E{episode:02d} - air_date={air_date_str} ({reason}, min={min_age_hours}h)")


def _episode_short_label(ep, max_show_len=32):
    """Compact SxxExx label for notifications (single-line UI only)."""
    show = (ep.get('tvshowtitle') or 'Unknown').strip()
    if len(show) > max_show_len:
        show = show[:max_show_len - 1] + '\u2026'
    season = int(ep.get('season') or 0)
    episode = int(ep.get('episode') or 0)
    return f"{show} S{season:02d}E{episode:02d}"


def show_skipped_notification(skipped_episodes, min_age_hours):
    """
    Show notification about skipped too-recent episodes.

    :param list skipped_episodes: List of skipped episode dicts
    :param int min_age_hours: Minimum age threshold that was used
    """
    try:
        if not skipped_episodes:
            return

        c.log(f"[TV Evening] Showing notification for {len(skipped_episodes)} skipped episodes")

        # Kodi notifications support one heading + one message line; no newlines.
        title = 'TV Evening'
        if len(skipped_episodes) == 1:
            message = f"{_episode_short_label(skipped_episodes[0])} too recent — replaced"
        elif len(skipped_episodes) == 2:
            labels = ' and '.join(_episode_short_label(ep, max_show_len=22) for ep in skipped_episodes)
            message = f"{labels} too recent — replaced"
        else:
            message = f"{len(skipped_episodes)} too-recent episodes replaced (<{min_age_hours}h)"

        control.dialog.notification(
            title,
            message,
            icon=control.addonIcon(),
            time=8000,
            sound=False
        )

    except Exception as e:
        c.log(f"[TV Evening] Error showing skipped notification: {e}")


def ask_duration():
    """
    Ask user for TV Evening duration using beautiful fullscreen dialog.

    :return: Duration in minutes or None if cancelled
    :rtype: int or None
    """
    try:
        # Get random backdrop from user's shows for visual appeal
        fanart = get_random_show_backdrop()

        c.log(f"[TV Evening] Showing duration dialog with backdrop: {fanart}")

        # Show custom duration dialog
        from . import tv_evening_duration_dialog
        duration = tv_evening_duration_dialog.show_tv_evening_duration_dialog(fanart or '')

        if duration:
            c.log(f"[TV Evening] User selected duration: {duration} minutes")
        else:
            c.log("[TV Evening] Duration selection cancelled")

        return duration

    except Exception as e:
        c.log(f"[TV Evening] Error showing duration dialog: {e}")
        c.log(f"[TV Evening] Traceback: {traceback.format_exc()}")

        # Fallback to simple dialog
        c.log("[TV Evening] Falling back to simple dialog")
        options = ['30 minutes', '1 hour', '2 hours', '3 hours', '4 hours', 'Custom']
        dialog = xbmcgui.Dialog()
        choice = dialog.select('How long is your TV Evening?', options)

        if choice == -1:  # Cancelled
            return None
        elif choice == 0:  # 30 minutes
            return 30
        elif choice == 1:  # 1 hour
            return 60
        elif choice == 2:  # 2 hours
            return 120
        elif choice == 3:  # 3 hours
            return 180
        elif choice == 4:  # 4 hours
            return 240
        elif choice == 5:  # Custom
            custom = dialog.numeric(0, 'Enter duration in minutes', '60')
            if custom:
                return int(custom)
            return None


def ask_duration_with_progress():
    """
    Ask user for TV Evening duration and return dialog for progress display.

    :return: Tuple of (duration in minutes, dialog object) or (None, None) if cancelled
    :rtype: tuple (int or None, dialog or None)
    """
    try:
        # Get random backdrop from user's shows for visual appeal
        fanart = get_random_show_backdrop()

        c.log(f"[TV Evening] Showing duration dialog with backdrop: {fanart}")

        # Show custom duration dialog with progress retention
        from . import tv_evening_duration_dialog
        duration, dialog = tv_evening_duration_dialog.show_tv_evening_duration_with_progress(fanart or '')

        if duration:
            c.log(f"[TV Evening] User selected duration: {duration} minutes, dialog retained")
        else:
            c.log("[TV Evening] Duration selection cancelled")

        return (duration, dialog)

    except Exception as e:
        c.log(f"[TV Evening] Error showing duration dialog: {e}")
        c.log(f"[TV Evening] Traceback: {traceback.format_exc()}")

        # Fallback returns no dialog
        return (None, None)


def get_random_show_backdrop():
    """
    Get a random backdrop from the live tracker watching library.

    :return: Backdrop URL or None
    :rtype: str or None
    """
    try:
        from . import keys, client
        import random

        show_tmdb = '0'
        show_title = 'Unknown'
        fanart_hint = ''

        if tracker.simkl_live():
            c.log("[TV Evening] Getting random backdrop from Simkl watching")
            items = simkl.crew_show_items('watching') or []
            if not items:
                c.log("[TV Evening] No Simkl watching shows for backdrop")
                return None
            pick = random.choice(items)
            show_tmdb = str(pick.get('tmdb') or '0')
            show_title = pick.get('title') or 'Unknown'
            fanart_hint = pick.get('fanart') or ''
            if fanart_hint and fanart_hint not in ('0', 'None'):
                c.log(f"[TV Evening] Got Simkl fanart from {show_title}")
                return fanart_hint
        else:
            c.log("[TV Evening] Getting random backdrop from Trakt shows")
            result = trakt.get_sync_watched_shows_extended(log_label='tvevening_backdrop')
            if not result:
                c.log("[TV Evening] No Trakt shows for backdrop")
                return None
            random_show = random.choice(result)
            show_data = random_show.get('show', {})
            show_tmdb = str(show_data.get('ids', {}).get('tmdb', '0'))
            show_title = show_data.get('title', 'Unknown')

        if not show_tmdb or show_tmdb == '0':
            c.log("[TV Evening] No TMDB ID for random show")
            return None

        tmdb_key = keys.tmdb_key
        show_url = f'https://api.themoviedb.org/3/tv/{show_tmdb}?api_key={tmdb_key}'
        show_response = client.request(show_url, timeout='10')

        if show_response:
            show_json = json.loads(show_response)
            backdrop_path = show_json.get('backdrop_path', '')
            if backdrop_path:
                backdrop_url = f'https://image.tmdb.org/t/p/original{backdrop_path}'
                c.log(f"[TV Evening] Got backdrop from {show_title}")
                return backdrop_url

        c.log("[TV Evening] Could not get backdrop")
        return None

    except Exception as e:
        c.log(f"[TV Evening] Error getting backdrop: {e}")
        return None


def _load_locally_watched_episode_keys():
    """One SQLite read for all locally-watched episodes (overlay=7)."""
    watched = set()
    try:
        from . import database
        control.makeFile(control.dataPath)
        dbcon = database.connect(control.bookmarksFile)
        dbcur = dbcon.cursor()
        dbcur.execute(
            "SELECT imdb, season, episode FROM bookmarks "
            "WHERE overlay = 7 AND season != '' AND episode != ''"
        )
        for imdb, season, episode in dbcur.fetchall():
            watched.add((str(imdb), str(season), str(episode)))
        dbcon.close()
        c.log("[TV Evening] Batch-loaded {} locally-watched episode keys".format(len(watched)))
    except Exception as e:
        c.log("[TV Evening] Could not batch-load watched bookmarks: {}".format(e))
    return watched


def _skip_past_locally_watched(show_imdb, show_title, season, episode, watched_keys, max_steps=20):
    """Advance past episodes marked watched locally when Trakt sync lags."""
    steps = 0
    while steps < max_steps:
        if (str(show_imdb), str(season), str(episode)) not in watched_keys:
            break
        c.log("[TV Evening] Skipping locally-watched {} S{:02d}E{:02d}".format(
            show_title, int(season), int(episode)))
        episode += 1
        steps += 1
    return season, episode


def _collect_show_tmdb_ids(episodes):
    ids = set()
    for ep in episodes:
        sid = str(ep.get('showtmdb') or '0')
        if sid in ('0', 'None', ''):
            continue
        try:
            ids.add(int(sid))
        except (TypeError, ValueError):
            pass
    return sorted(ids)


def _advance_episode_past_cached_404(ep, episode_cache_dict, max_steps=4):
    """Skip Trakt guesses that TMDB has already confirmed do not exist."""
    try:
        show_tmdb = int(ep.get('showtmdb') or 0)
    except (TypeError, ValueError):
        return
    if not show_tmdb:
        return
    season = int(ep.get('season') or 0)
    episode_num = int(ep.get('episode') or 0)
    orig_season, orig_episode = season, episode_num
    advanced = False
    for _ in range(max_steps):
        if trakt.check_next_episode_cache(
            show_tmdb, season, episode_num, cache_dict=episode_cache_dict
        ) is not False:
            break
        season += 1
        episode_num = 1
        advanced = True
    if not advanced or (season == orig_season and episode_num == orig_episode):
        return
    c.log("[TV Evening] Advanced {} past cached 404: S{:02d}E{:02d} -> S{:02d}E{:02d}".format(
        ep.get('tvshowtitle', 'Unknown'),
        orig_season, orig_episode,
        season, episode_num,
    ))
    ep['season'] = season
    ep['episode'] = episode_num
    ep['title'] = "Episode {}".format(episode_num)


def _prefetch_episode_metadata_parallel(episodes, max_workers=10):
    """Fetch air dates (and runtime when available) for a shortlist in parallel."""
    pending = [
        ep for ep in episodes
        if not _parse_episode_air_date(ep.get('air_date') or ep.get('first_aired'))
    ]
    if not pending:
        return
    from concurrent.futures import ThreadPoolExecutor
    workers = min(max_workers, len(pending))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(fetch_episode_air_date, pending))


def _select_episodes_for_playlist(all_episodes, duration_minutes, min_age_hours, progress_dialog=None):
    """
    Pick episodes until duration target is met.

    When min-age filtering is on, TMDB metadata is fetched in parallel chunks —
    only for candidates we are actively considering, not the entire library.
    """
    now = datetime.utcnow()
    reserve_buffer = 45
    target_runtime = duration_minutes + reserve_buffer
    playlist = []
    skipped = []
    total_runtime = 0
    total = len(all_episodes)
    chunk_size = 12
    idx = 0

    c.log("[TV Evening] Target: {}min + {}min reserve = {}min (parallel shortlist scan)".format(
        duration_minutes, reserve_buffer, target_runtime))

    while total_runtime < target_runtime and idx < total:
        chunk = all_episodes[idx:idx + chunk_size]
        idx += len(chunk)
        if not chunk:
            break

        if min_age_hours > 0:
            _prefetch_episode_metadata_parallel(chunk)
            if progress_dialog:
                pct = 25 + int((min(idx, total) / max(total, 1)) * 14)
                progress_dialog.update_progress(
                    'Checking availability...\n\n{} of {}'.format(min(idx, total), total),
                    min(39, pct),
                )

        for ep in chunk:
            if total_runtime >= target_runtime:
                break
            if min_age_hours > 0:
                fetch_episode_air_date(ep)
            eligible, reason = episode_passes_air_date_filter(ep, min_age_hours, now)
            _log_air_date_decision(ep, eligible, reason, min_age_hours)
            if not eligible:
                skipped.append(ep)
                continue
            playlist.append(ep)
            total_runtime += int(ep.get('duration', 45))

    c.log("[TV Evening] Selected {} episode(s) after scanning {}/{} candidates".format(
        len(playlist), min(idx, total), total))
    return playlist, skipped, total_runtime


def fetch_next_episodes_data():
    """
    Fetch next episodes for TV Evening (live tracker XOR).

    Simkl: watching + next_to_watch (same pool as Progress → Next Episodes).
    Trakt: sync/watched progress math (same as Progress indexer).

    :return: List of episode data dicts
    :rtype: list
    """
    try:
        if tracker.simkl_live():
            return _fetch_next_episodes_simkl()
        return _fetch_next_episodes_trakt()
    except Exception as e:
        c.log("[TV Evening] Error fetching next episodes: {e}".format(e=e))
        c.log(f"[TV Evening] Traceback: {traceback.format_exc()}")
        return []


def _fetch_next_episodes_simkl():
    """Build TV Evening candidates from Simkl watching / next_to_watch."""
    c.log("[TV Evening] Fetching next episodes from Simkl watching")
    sessions = simkl.crew_next_episode_sessions() or []
    if not sessions:
        c.log("[TV Evening] No Simkl next-episode data")
        return []

    c.log(f"[TV Evening] Got Simkl next-episode sessions for {len(sessions)} shows")
    watched_keys = _load_locally_watched_episode_keys()
    episodes = []

    for session in sessions:
        try:
            show_data = session.get('show_data') or {}
            ids = show_data.get('ids') or {}
            show_title = (show_data.get('title') or 'Unknown').strip() or 'Unknown'
            show_imdb = ids.get('imdb') or '0'
            if show_imdb in (None, '', 'None'):
                show_imdb = '0'
            show_tmdb = str(ids.get('tmdb') or '0')
            show_year = str(show_data.get('year') or '')
            if not show_tmdb or show_tmdb in ('0', 'None'):
                continue

            next_season = int(session.get('season') or 0)
            next_episode = int(session.get('episode') or 0)
            if next_season <= 0 or next_episode <= 0:
                continue

            next_season, next_episode = _skip_past_locally_watched(
                show_imdb, show_title, next_season, next_episode, watched_keys,
            )

            ep_title = (session.get('episode_title') or '').strip()
            if not ep_title:
                ep_title = f'Episode {next_episode}'

            episodes.append({
                'tvshowtitle': show_title,
                'title': ep_title,
                'season': next_season,
                'episode': next_episode,
                'showimdb': show_imdb,
                'showtmdb': show_tmdb,
                'year': show_year,
                'duration': 45,
                'plot': '',
                'thumb': '',
                'poster': '',
                'fanart': '',
                'last_watched_at': session.get('last_watched_at') or '1900-01-01T00:00:00.000Z',
            })
        except Exception as e:
            c.log(f"[TV Evening] Error processing Simkl show: {e}")
            continue

    episodes.sort(
        key=lambda x: x.get('last_watched_at', '1900-01-01T00:00:00.000Z'),
        reverse=True,
    )

    episode_cache_dict = trakt.batch_load_episode_cache_for_shows(
        _collect_show_tmdb_ids(episodes)
    ) or {}
    for ep in episodes:
        _advance_episode_past_cached_404(ep, episode_cache_dict)

    c.log(f"[TV Evening] Returning {len(episodes)} Simkl next episodes")
    return episodes


def _fetch_next_episodes_trakt():
    """Build TV Evening candidates from Trakt watched progress (legacy path)."""
    c.log("[TV Evening] Fetching next episodes from Trakt progress API")

    result = trakt.get_sync_watched_shows_extended(log_label='tv_evening')

    if not result:
        c.log("[TV Evening] No Trakt progress data")
        return []

    c.log(f"[TV Evening] Got progress data for {len(result)} shows")

    watched_keys = _load_locally_watched_episode_keys()
    episodes = []

    for item in result:
        try:
            show_data = item.get('show', {})
            show_title = show_data.get('title', 'Unknown')
            show_imdb = show_data.get('ids', {}).get('imdb', '0')
            show_tmdb = str(show_data.get('ids', {}).get('tmdb', '0'))
            show_year = str(show_data.get('year', ''))

            seasons = item.get('seasons', [])
            if isinstance(seasons, dict):
                seasons = list(seasons.values())

            num_watched = sum(len(s.get('episodes', [])) for s in seasons if s.get('number', 0) > 0)
            aired_episodes = int(show_data.get('aired_episodes', 0) or 0)
            if num_watched >= aired_episodes and aired_episodes > 0:
                continue  # fully watched — same rule as Progress indexer

            # Find highest watched episode
            max_season = 0
            max_episode = 0
            last_watched_at = None

            for s in [s for s in seasons if s.get('number', 0) > 0]:
                eps = s.get('episodes') or []
                if eps:
                    max_ep_in_season = max(e.get('number', 0) for e in eps)
                    season_num = s.get('number')
                    if season_num > max_season or (season_num == max_season and max_ep_in_season > max_episode):
                        max_season = season_num
                        max_episode = max_ep_in_season
                        for ep in eps:
                            if ep.get('number') == max_ep_in_season:
                                ep_timestamp = ep.get('last_watched_at')
                                if ep_timestamp:
                                    last_watched_at = ep_timestamp
                                break

            if max_season <= 0 or max_episode <= 0:
                continue

            next_season = max_season
            next_episode = max_episode + 1

            next_season, next_episode = _skip_past_locally_watched(
                show_imdb, show_title, next_season, next_episode, watched_keys,
            )

            episode = {
                'tvshowtitle': show_title,
                'title': f"Episode {next_episode}",
                'season': next_season,
                'episode': next_episode,
                'showimdb': show_imdb,
                'showtmdb': show_tmdb,
                'year': show_year,
                'duration': 45,
                'plot': '',
                'thumb': '',
                'poster': '',
                'fanart': '',
                'last_watched_at': last_watched_at or '1900-01-01T00:00:00.000Z'
            }

            episodes.append(episode)

        except Exception as e:
            c.log("[TV Evening] Error processing show: {e}".format(e=e))
            continue

    # Sort by last_watched_at (most recent first)
    episodes.sort(key=lambda x: x.get('last_watched_at', '1900-01-01T00:00:00.000Z'), reverse=True)

    episode_cache_dict = trakt.batch_load_episode_cache_for_shows(
        _collect_show_tmdb_ids(episodes)
    ) or {}
    for ep in episodes:
        _advance_episode_past_cached_404(ep, episode_cache_dict)

    c.log(f"[TV Evening] Returning {len(episodes)} next episodes (metadata fetched only for playlist shortlist)")
    return episodes


def validate_episode_exists(episode):
    """
    Check if episode exists on TMDB (lightweight validation).
    If not, tries next season episode 1.

    :param dict episode: Episode dict with showtmdb, season, episode
    :return: Updated episode dict or None if validation failed
    :rtype: dict or None
    """
    try:
        show_title = episode.get('tvshowtitle', 'Unknown')
        show_tmdb = episode.get('showtmdb', '0')
        season = episode.get('season', 0)
        ep_num = episode.get('episode', 0)

        if show_tmdb == '0':
            c.log(f"[TV Evening] {show_title} - No TMDB ID, skipping validation")
            return episode  # Can't validate without TMDB ID, return as-is

        # ── Season cache pre-flight ───────────────────────────────────────────
        # Check if we already know this season/next-season state so we can skip
        # impossible episodes without making any TMDB API calls.
        cached = tv_season_cache.get_season_status(show_tmdb, season)
        if cached:
            status_val  = cached.get('status')
            finale_ep   = cached.get('finale_ep')
            show_ended  = cached.get('show_ended', 0)

            if show_ended or status_val == 'ended':
                c.log(f"[TV Evening] {show_title} S{season:02d} — show ended, skipping (cache)")
                return None

            if status_val == 'complete' and finale_ep and ep_num > finale_ep:
                c.log(f"[TV Evening] {show_title} S{season:02d}E{ep_num:02d} beyond finale E{finale_ep:02d} — skipping (cache)")
                return None

            if status_val == 'future':
                next_air = cached.get('next_air_date') or 'TBD'
                c.log(f"[TV Evening] {show_title} S{season:02d} not yet available (premiere: {next_air}) — skipping (cache)")
                return None

        # ── Standard TMDB validation ──────────────────────────────────────────
        episode_obj = Episode.from_trakt_progress(
            {
                'title': episode.get('tvshowtitle', ''),
                'year': episode.get('year', ''),
                'ids': {
                    'imdb': episode.get('showimdb', '0'),
                    'tmdb': show_tmdb
                }
            },
            season,
            ep_num
        )

        if not episode_obj:
            c.log(f"[TV Evening] Failed to create Episode object for {show_title}")
            return None

        # Try to fetch metadata (validates existence)
        c.log(f"[TV Evening] Validating {show_title} S{season:02d}E{ep_num:02d}")
        validation_success = episode_obj.fetch_tmdb_metadata()

        # If episode doesn't exist and not first episode, try next season E01
        if not validation_success and ep_num > 1:
            # Before blindly jumping to the next season, record what TMDB tells us
            # about the current season boundary (one show-root call).
            c.log(f"[TV Evening] S{season:02d}E{ep_num:02d} doesn't exist — checking season cache for {show_title}")
            cache_info = tv_season_cache.fetch_and_cache(show_tmdb, season, show_title)

            if cache_info:
                if cache_info.get('show_ended'):
                    c.log(f"[TV Evening] {show_title} — show ended, no more seasons")
                    return None
                # If we know the next season is 'future', don't burn an API call on S(n+1)E01
                next_cached = tv_season_cache.get_season_status(show_tmdb, season + 1)
                if next_cached and next_cached.get('status') == 'future':
                    air = next_cached.get('next_air_date') or 'TBD'
                    c.log(f"[TV Evening] {show_title} S{season+1:02d} not yet available (premiere: {air}) — skipping")
                    return None

            c.log(f"[TV Evening] S{season:02d}E{ep_num:02d} doesn't exist, trying S{season+1:02d}E01")
            season += 1
            ep_num = 1

            # Try validating next season E01
            episode_obj = Episode.from_trakt_progress(
                {
                    'title': episode.get('tvshowtitle', ''),
                    'year': episode.get('year', ''),
                    'ids': {
                        'imdb': episode.get('showimdb', '0'),
                        'tmdb': show_tmdb
                    }
                },
                season,
                ep_num
            )

            if episode_obj:
                validation_success = episode_obj.fetch_tmdb_metadata()

        # Skip if still failed
        if not validation_success:
            c.log(f"[TV Evening] Validation failed for {show_title} - skipping")
            return None

        c.log(f"[TV Evening] (OK) Validated {show_title} S{season:02d}E{ep_num:02d}")

        # Parse cast data (castwiththumb contains guest_stars from TMDB)
        # Split into leads (first 3) and guests (next 3) for countdown display - 6 total (2 rows of 3)
        all_cast = episode_obj.castwiththumb if episode_obj else []
        cast_leads = all_cast[:3]  # First 3 as main cast
        cast_guests = all_cast[3:6]  # Next 3 as guest stars (6 total for 2x3 grid)

        c.log(f"[TV Evening] Cast data: {len(cast_leads)} leads, {len(cast_guests)} guests")

        # Prepare cast data with full TMDB URLs
        cast_leads_data = [
            {
                'name': c.get('name', ''),
                'character': c.get('role', ''),
                'thumb': c.get('thumbnail', '')
            }
            for c in cast_leads
        ]

        # Log first lead's thumbnail to verify full URL
        if cast_leads_data:
            c.log(f"[TV Evening] Sample lead thumb URL: {cast_leads_data[0].get('thumb', 'none')[:80]}")

        # Update episode dict with validated data INCLUDING cast
        episode.update({
            'season': season,
            'episode': ep_num,
            'title': episode_obj.title or f"Episode {ep_num}",
            'duration': episode_obj.duration or 45,
            'plot': episode_obj.plot or '',
            'thumb': episode_obj.thumb or '',
            'air_date': episode_obj.first_aired or episode.get('air_date', ''),
            'cast_leads': cast_leads_data,
            'cast_guests': [
                {
                    'name': c.get('name', ''),
                    'character': c.get('role', ''),
                    'thumb': c.get('thumbnail', '')
                }
                for c in cast_guests
            ]
        })

        min_age_hours = get_min_episode_age()
        eligible, reason = episode_passes_air_date_filter(episode, min_age_hours)
        if not eligible:
            _log_air_date_decision(episode, False, f'validation_{reason}', min_age_hours)
            return None

        return episode

    except Exception as e:
        c.log(f"[TV Evening] Error validating episode: {e}")
        return None


def validate_episodes_parallel(episodes, progress_dialog=None, progress_start=40, progress_span=10):
    """
    Validate episodes in parallel using ThreadPool.
    Much faster than sequential validation when checking 5-10 episodes.

    :param list episodes: List of episode dicts to validate
    :param progress_dialog: Optional duration dialog for live progress text
    :param int progress_start: Progress bar % at start of this phase
    :param int progress_span: Progress bar % range to cover during validation
    :return: List of validated episode dicts
    :rtype: list
    """
    total = len(episodes)
    if not total:
        return []

    def _report_validation_progress(done):
        if not progress_dialog or total <= 0:
            return
        pct = progress_start + int((done / total) * progress_span)
        pct = min(progress_start + progress_span, pct)
        progress_dialog.update_progress(
            f'Confirming your lineup...\n\n{done} of {total}',
            pct
        )

    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        c.log(f"[TV Evening] Validating {total} episodes in parallel...")

        validated = []
        done = 0

        # Use ThreadPool to validate episodes in parallel
        # Max 10 workers to avoid overwhelming API
        with ThreadPoolExecutor(max_workers=min(10, total)) as executor:
            # Submit all validation tasks
            future_to_ep = {executor.submit(validate_episode_exists, ep): ep for ep in episodes}

            # Collect results as they complete
            for future in as_completed(future_to_ep):
                result = future.result()
                done += 1
                if done == 1 or done % 2 == 0 or done == total:
                    _report_validation_progress(done)
                if result:
                    validated.append(result)

        c.log(f"[TV Evening] Validated {len(validated)}/{total} episodes successfully")
        return validated

    except Exception as e:
        c.log(f"[TV Evening] Error in parallel validation: {e}")
        c.log(f"[TV Evening] Traceback: {traceback.format_exc()}")
        # Fallback to sequential validation
        c.log("[TV Evening] Falling back to sequential validation...")
        validated = []
        for i, ep in enumerate(episodes, 1):
            result = validate_episode_exists(ep)
            if i == 1 or i % 2 == 0 or i == total:
                _report_validation_progress(i)
            if result:
                validated.append(result)
        return validated


def get_recently_watched_shows():
    """
    Get recently watched shows from Trakt.

    :return: List of show progress data
    :rtype: list
    """
    try:
        if not tracker_ready():
            if not _prompt_tracker_setup():
                return []
            # Tracker is now configured, proceed

        if tracker.simkl_live():
            c.log("[TV Evening] Getting recently watched shows from Simkl")
            limit = get_source_count()
            items = simkl.crew_show_items('watching') or []
            shows_list = []
            for item in items[:limit]:
                shows_list.append({
                    'show': {
                        'title': item.get('title', 'Unknown Show'),
                        'year': item.get('year', ''),
                        'ids': {
                            'imdb': item.get('imdb', '0'),
                            'tmdb': item.get('tmdb', ''),
                            'tvdb': item.get('tvdb', ''),
                        }
                    },
                    'last_watched': '',
                })
            c.log(f"[TV Evening] Got {len(shows_list)} Simkl watching shows")
            return shows_list

        c.log("[TV Evening] Getting recently watched shows from Trakt")

        # Get number of shows from settings
        limit = get_source_count()

        # Get progress data from Trakt sync database
        # This returns shows with watch progress from local database
        progress_data = trakt.get_trakt_progress('episode')

        if not progress_data:
            c.log("[TV Evening] No progress data from Trakt")
            return []

        # Group episodes by show and get unique shows
        shows_dict = {}
        for ep in progress_data:
            show_imdb = ep.get('showimdb')
            if show_imdb and show_imdb not in shows_dict:
                shows_dict[show_imdb] = {
                    'show': {
                        'title': ep.get('tvshowtitle', 'Unknown Show'),
                        'year': ep.get('showyear', ''),
                        'ids': {
                            'imdb': show_imdb,
                            'tmdb': ep.get('showtmdb', ''),
                            'tvdb': ep.get('showtvdb', '')
                        }
                    },
                    'last_watched': ep.get('paused_at', '')
                }

        # Sort by last watched and limit
        shows_list = sorted(
            shows_dict.values(),
            key=lambda x: x.get('last_watched', ''),
            reverse=True
        )[:limit]

        c.log(f"[TV Evening] Got {len(shows_list)} unique shows from Trakt")
        return shows_list

    except Exception as e:
        c.log(f"[TV Evening] Error getting recently watched shows: {e}")
        c.log(f"[TV Evening] Traceback: {traceback.format_exc()}")
        return []


def get_next_episode_for_show(show_data):
    """
    Get next unwatched episode for a show.

    :param dict show_data: Show progress data from Trakt
    :return: Episode data dict or None
    :rtype: dict or None
    """
    try:
        show = show_data.get('show', {})
        show_imdb = show.get('ids', {}).get('imdb', '')
        show_tmdb = show.get('ids', {}).get('tmdb', '')

        if not show_imdb:
            c.log(f"[TV Evening] No IMDB for {show.get('title', 'Unknown')}")
            return None

        c.log(f"[TV Evening] Getting next episode for {show.get('title', 'Unknown')} (IMDB: {show_imdb})")

        # Try to get from Trakt progress API
        try:
            from . import client
            url = f'https://api.trakt.tv/shows/{show_imdb}/progress/watched?hidden=false&specials=false&count_specials=false'
            headers = {
                'Content-Type': 'application/json',
                'trakt-api-version': '2',
                'trakt-api-key': trakt.CLIENT_ID
            }

            # Add auth if available
            token = control.setting('trakt.token')
            if token:
                headers['Authorization'] = f'Bearer {token}'

            result = client.request(url, headers=headers)
            if result:
                progress_data = json.loads(result)
                next_ep = progress_data.get('next_episode')

                if next_ep:
                    episode_data = {
                        'tvshowtitle': show.get('title', 'Unknown Show'),
                        'year': show.get('year', ''),
                        'imdb': show_imdb,
                        'tmdb': show_tmdb,
                        'tvdb': show.get('ids', {}).get('tvdb', ''),
                        'season': next_ep.get('season', 1),
                        'episode': next_ep.get('number', 1),
                        'title': next_ep.get('title', 'Unknown Episode'),
                        'plot': next_ep.get('overview', ''),
                        'thumb': '',
                        'runtime': next_ep.get('runtime', 45)
                    }

                    c.log(f"[TV Evening] Next episode: {episode_data['tvshowtitle']} S{episode_data['season']:02d}E{episode_data['episode']:02d}")
                    return episode_data
        except Exception as e:
            c.log(f"[TV Evening] Error getting next episode from Trakt API: {e}")

        # Fallback: No next episode found
        c.log(f"[TV Evening] No next episode for {show.get('title', 'Unknown')}")
        return None

    except Exception as e:
        c.log(f"[TV Evening] Error getting next episode: {e}")
        c.log(f"[TV Evening] Traceback: {traceback.format_exc()}")
        return None


def fetch_episode_air_date(episode):
    """
    Ensure episode dict has air_date (uses TMDB cache — still a live fetch on cache miss).

    :param dict episode: Episode data dict with showtmdb, season, and episode
    :return: None (updates episode dict with air_date)
    """
    if _parse_episode_air_date(episode.get('air_date') or episode.get('first_aired')):
        return

    show_tmdb = episode.get('showtmdb')
    season = episode.get('season')
    ep_num = episode.get('episode')
    show_title = episode.get('tvshowtitle', 'Unknown')

    if not show_tmdb or show_tmdb == '0':
        return

    try:
        from . import cache as caching
        details = caching.get_episode_details(show_tmdb, season, ep_num)
        if details:
            parsed = _parse_episode_air_date(details.get('air_date'))
            if parsed:
                episode['air_date'] = parsed.isoformat()
            if details.get('duration'):
                episode['duration'] = details['duration']
    except Exception as e:
        c.log("[TV Evening] Error fetching air date for {} S{:02d}E{:02d}: {}".format(
            show_title, season, ep_num, e))


def fetch_episode_artwork(episode):
    """
    Fetch season poster, show fanart, episode title and plot from TMDB.

    :param dict episode: Episode data dict with showtmdb, season, and episode
    :return: None (updates episode dict in place)
    """
    show_tmdb = episode.get('showtmdb')
    season = episode.get('season')
    ep_num = episode.get('episode')
    show_title = episode.get('tvshowtitle', 'Unknown')

    if not show_tmdb or show_tmdb == '0':
        return

    try:
        from . import keys, client
        import json

        tmdb_key = keys.tmdb_key

        # Fetch episode details (title, plot, runtime) - CACHED
        from . import cache as caching
        details = caching.get_episode_details(show_tmdb, season, ep_num)
        if details:
            if details.get('title'):
                episode['title'] = details['title']
            if details.get('plot'):
                episode['plot'] = details['plot']
            if details.get('duration'):
                episode['duration'] = details['duration']
            if details.get('air_date'):
                episode['air_date'] = details['air_date']
                c.log(f"[TV Evening] Got air date for {show_title} S{season:02d}E{ep_num:02d}: {details['air_date']}")

            c.log(f"[TV Evening] Got episode details for {show_title} S{season:02d}E{ep_num:02d}")

        # Fetch season poster - CACHED
        poster_url = caching.get_season_artwork(show_tmdb, season)
        if poster_url:
            episode['thumb'] = poster_url
            episode['poster'] = poster_url
            c.log(f"[TV Evening] Got season {season} poster for {show_title}")

        # Fetch show fanart for background - CACHED
        backdrop_url = caching.get_show_artwork(show_tmdb)
        if backdrop_url:
            episode['fanart'] = backdrop_url

    except Exception as art_err:
        c.log(f"[TV Evening] Could not fetch artwork for {show_title}: {art_err}")


def build_playlist(duration_minutes, progress_dialog=None):
    """
    Build TV Evening playlist using existing Next Episodes data.
    Filters out episodes that aired too recently (< min_episode_age hours).
    OPTIMIZED: Only fetches artwork for final selected episodes.

    :param int duration_minutes: Target duration in minutes
    :param progress_dialog: Optional dialog to update progress messages
    :return: Dict with 'visible' and 'reserve' episode lists, or None on failure
    :rtype: dict or None
    """
    try:
        c.log(f"[TV Evening] Building playlist for {duration_minutes} minutes")

        # One-time migration: pre-populate season cache from existing next_episode_cache data.
        # Returns immediately (< 1 ms) on every subsequent run once the table has rows.
        try:
            seeded = tv_season_cache.seed_from_next_episode_cache()
            if seeded:
                c.log(f"[TV Evening] Season cache seeded for {seeded} shows — future runs will skip TMDB calls for finished seasons")
        except Exception as _seed_err:
            c.log(f"[TV Evening] Season cache seed failed (non-fatal): {_seed_err}")
        c.log("[TV Evening] ========== FIRST PROGRESS UPDATE ==========")
        if progress_dialog:
            c.log("[TV Evening] Calling update_progress('Fetching your watched shows from Trakt...', 10%)")
            progress_dialog.update_progress('Preparing your TV Evening...', 10)
            c.log("[TV Evening] update_progress() returned")
        else:
            c.log("[TV Evening] WARNING: progress_dialog is None!")

        # Get minimum episode age setting
        min_age_hours = get_min_episode_age()
        c.log(f"[TV Evening] Minimum episode age: {min_age_hours} hours")

        # Fetch next episodes from live tracker (Simkl XOR Trakt)
        c.log(f"[TV Evening] Fetching next episodes ({tracker.active_name()})")
        all_episodes = fetch_next_episodes_data()
        c.log("[TV Evening] ========== SECOND PROGRESS UPDATE ==========")
        if progress_dialog:
            msg = 'Choosing tonight\'s lineup...'
            c.log(f"[TV Evening] Calling update_progress('{msg}', 25%)")
            progress_dialog.update_progress(msg, 25)
            c.log("[TV Evening] update_progress() returned")
        else:
            c.log("[TV Evening] WARNING: progress_dialog is None!")

        if not all_episodes:
            control.dialog.ok('No Episodes Found', 'Could not find any unwatched episodes.\n\nYou\'re all caught up!')
            return None

        c.log(f"[TV Evening] Found {len(all_episodes)} next episodes available")

        # Randomize if enabled
        if get_randomize():
            c.log("[TV Evening] Randomizing episode order")
            random.shuffle(all_episodes)

        now = datetime.utcnow()
        if min_age_hours > 0:
            c.log("[TV Evening] Current UTC time: {}".format(now.strftime('%Y-%m-%d %H:%M:%S')))
            c.log("[TV Evening] Cutoff time ({}h ago): {}".format(
                min_age_hours,
                (now - timedelta(hours=min_age_hours)).strftime('%Y-%m-%d %H:%M:%S'),
            ))
        elif progress_dialog:
            progress_dialog.update_progress('Choosing tonight\'s lineup...', 35)

        playlist, skipped, total_runtime = _select_episodes_for_playlist(
            all_episodes,
            duration_minutes,
            min_age_hours,
            progress_dialog=progress_dialog,
        )

        c.log("[TV Evening] Selected {} episodes for playlist".format(len(playlist)))

        # VALIDATE episodes in parallel (checks if they exist on TMDB)
        # This catches issues like The Rookie S07E19 (doesn't exist, finale was S07E18)
        c.log(f"[TV Evening] Validating {len(playlist)} episodes in parallel...")
        c.log("[TV Evening] ========== VALIDATION PROGRESS UPDATE ==========")
        if progress_dialog:
            msg = 'Confirming your lineup...'
            c.log(f"[TV Evening] Calling update_progress('{msg}', 40%)")
            progress_dialog.update_progress(msg, 40)
            c.log("[TV Evening] update_progress() returned")
        else:
            c.log("[TV Evening] WARNING: progress_dialog is None!")

        validated_playlist = validate_episodes_parallel(
            playlist,
            progress_dialog=progress_dialog,
            progress_start=40,
            progress_span=10,
        )

        # Track which episodes failed validation so they are excluded from the replacement pool
        failed_episodes = set((ep['showimdb'], ep['season'], ep['episode']) for ep in playlist) - \
                          set((ep['showimdb'], ep['season'], ep['episode']) for ep in validated_playlist)

        if len(validated_playlist) < len(playlist):
            c.log(f"[TV Evening] WARNING: {len(playlist) - len(validated_playlist)} episodes failed validation")
            # If we lost episodes, try to add more from remaining pool
            # Exclude: already validated, air-date skipped, AND validation failures
            shortage = len(playlist) - len(validated_playlist)
            if shortage > 0:
                used_episodes = set((ep['showimdb'], ep['season'], ep['episode']) for ep in (validated_playlist + skipped))
                used_episodes |= failed_episodes  # Also exclude previously failed episodes
                replacement_pool = [ep for ep in all_episodes
                                  if (ep['showimdb'], ep['season'], ep['episode']) not in used_episodes]

                if replacement_pool:
                    candidates = []
                    for candidate in replacement_pool:
                        if len(candidates) >= shortage * 2:
                            break
                        candidates.append(candidate)
                    if min_age_hours > 0:
                        _prefetch_episode_metadata_parallel(candidates)
                    eligible_candidates = []
                    for candidate in candidates:
                        eligible, reason = episode_passes_air_date_filter(candidate, min_age_hours, now)
                        if eligible:
                            eligible_candidates.append(candidate)
                        else:
                            _log_air_date_decision(
                                candidate, False, 'validation_replacement_{}'.format(reason), min_age_hours)
                    c.log("[TV Evening] Trying {} replacement candidates for {} needed".format(
                        len(eligible_candidates), shortage))
                    additional = validate_episodes_parallel(
                        eligible_candidates,
                        progress_dialog=progress_dialog,
                        progress_start=48,
                        progress_span=2,
                    )
                    validated_playlist.extend(additional)

        playlist = validated_playlist
        c.log(f"[TV Evening] Validated playlist now has {len(playlist)} episodes")

        # Recalculate total runtime after validation
        total_runtime = sum(int(ep.get('duration', 45)) for ep in playlist)

        # Trim the playlist so total runtime doesn't overshoot the user's target by more
        # than one episode (~45 min). The reserve_buffer above over-selects intentionally;
        # clipped episodes become hidden reserve slots for playback-time swap.
        TRIM_TOLERANCE = 15  # allow at most 15 min over the user's chosen duration
        reserve_episodes = []
        if total_runtime > duration_minutes + TRIM_TOLERANCE:
            trim_runtime = 0
            trim_playlist = []
            for i, ep in enumerate(playlist):
                ep_dur = int(ep.get('duration', 45))
                if trim_runtime + ep_dur > duration_minutes + TRIM_TOLERANCE and trim_playlist:
                    reserve_episodes = playlist[i:]
                    break
                trim_playlist.append(ep)
                trim_runtime += ep_dur
            c.log(f"[TV Evening] Trimmed playlist from {len(playlist)} to {len(trim_playlist)} episodes "
                  f"({total_runtime}min -> {trim_runtime}min, target={duration_minutes}min); "
                  f"kept {len(reserve_episodes)} reserve episode(s)")
            playlist = trim_playlist
            total_runtime = trim_runtime

        # Keep at most one hidden reserve for playback-time swap (not the entire trim tail)
        MAX_RESERVE = 1
        if len(reserve_episodes) > MAX_RESERVE:
            c.log(f"[TV Evening] Capping reserve from {len(reserve_episodes)} to {MAX_RESERVE} episode(s)")
            reserve_episodes = reserve_episodes[:MAX_RESERVE]

        # OPTIMIZATION: NOW fetch artwork only for the final visible + reserve episodes
        artwork_targets = playlist + reserve_episodes
        c.log(f"[TV Evening] Fetching artwork for {len(artwork_targets)} episodes "
              f"({len(playlist)} visible + {len(reserve_episodes)} reserve)")
        c.log("[TV Evening] ========== ARTWORK PROGRESS UPDATE ==========")
        if progress_dialog:
            msg = 'Getting things ready...'
            c.log(f"[TV Evening] Calling update_progress('{msg}', 50%)")
            progress_dialog.update_progress(msg, 50)
            c.log("[TV Evening] update_progress() returned")
        else:
            c.log("[TV Evening] WARNING: progress_dialog is None!")
        for i, ep in enumerate(artwork_targets, 1):
            if progress_dialog and i % 2 == 0:  # Update every 2 episodes to avoid too many updates
                # Calculate progress: 50% at start, 100% at end
                progress_pct = 50 + int((i / len(artwork_targets)) * 50)
                progress_dialog.update_progress(
                    f'Getting things ready...\n\n{i} of {len(artwork_targets)}',
                    progress_pct
                )
            fetch_episode_artwork(ep)

        # Final progress update - 100% complete
        if progress_dialog:
            progress_dialog.update_progress('Your evening is ready.', 100)

        c.log(f"[TV Evening] Built playlist with {len(playlist)} visible episode(s), "
              f"{len(reserve_episodes)} reserve, total runtime: {total_runtime} minutes")

        return {'visible': playlist, 'reserve': reserve_episodes, 'skipped': skipped, 'min_age_hours': min_age_hours}

    except Exception as e:
        c.log(f"[TV Evening] Error building playlist: {e}")
        c.log(f"[TV Evening] Traceback: {traceback.format_exc()}")
        return None


def show_playlist_preview(playlist):
    """
    Show playlist preview to user with options using custom dialog.

    :param list playlist: List of episode data dicts
    :return: User choice ('play', 'first', or None for cancel)
    :rtype: str or None
    """
    try:
        from . import tv_evening_dialog

        user_action = tv_evening_dialog.show_tv_evening_playlist(playlist)

        c.log(f"[TV Evening] User action from dialog: {user_action}")
        return user_action

    except Exception as e:
        c.log(f"[TV Evening] Error showing preview: {e}")
        c.log(f"[TV Evening] Traceback: {traceback.format_exc()}")
        return 'play'  # Default to play on error


def _build_episode_play_url(episode):
    """Build plugin play URL for a TV Evening episode dict."""
    url = 'plugin://plugin.video.midnight/?action=play'
    url += f"&title={quote_plus(str(episode.get('title', '')))}"
    url += f"&year={quote_plus(str(episode.get('year', '')))}"
    url += f"&imdb={quote_plus(str(episode.get('showimdb', episode.get('imdb', '0'))))}"
    url += f"&tmdb={quote_plus(str(episode.get('showtmdb', episode.get('tmdb', '0'))))}"
    url += f"&season={quote_plus(str(episode.get('season', '')))}"
    url += f"&episode={quote_plus(str(episode.get('episode', '')))}"
    url += f"&tvshowtitle={quote_plus(str(episode.get('tvshowtitle', '')))}"
    premiered = episode.get('premiered') or episode.get('air_date') or ''
    if premiered:
        url += f"&premiered={quote_plus(str(premiered))}"
    url += "&select=2"
    return url


def play_playlist(playlist, reserve_episodes=None):
    """
    Play TV Evening playlist using database-backed playlist with Kodi playback.

    Stores visible episodes in SQLite for reliable state tracking,
    plus hidden reserve episode(s) for playback-time swap on scrape failure.
    Uses Kodi's playlist for actual playback of the current episode only (single item).

    :param list playlist: Visible episode data dicts (shown in preview)
    :param list reserve_episodes: Hidden reserve episode(s), not shown to user
    """
    try:
        if not playlist:
            return

        c.log(f"[TV Evening] Building playlist with {len(playlist)} visible episode(s)"
              + (f", {len(reserve_episodes or [])} reserve" if reserve_episodes else ""))

        from . import tvevening_session
        tvevening_session.clear_completing()
        control.window.clearProperty('midnight.tvevening.transitioning')
        control.window.clearProperty(tvevening_session.PROP_COMPLETE_PENDING)

        # Get database instance
        db = tvevening_playlist_db.get_playlist_db()

        # Clear both Kodi playlist and database
        c.log("[TV Evening] Clearing playlist database and Kodi playlist")
        db.clear_playlist()

        # Create Kodi video playlist — ONE item only; monitor drives each transition
        kodi_playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
        kodi_playlist.clear()

        # Store all visible episodes in database; Kodi gets the first item only
        for i, episode in enumerate(playlist):
            c.log(f"[TV Evening] Adding episode {i+1}/{len(playlist)}: {episode['tvshowtitle']} S{episode['season']:02d}E{episode['episode']:02d}")

            url = _build_episode_play_url(episode)
            episode['url'] = url
            db.add_episode(i, episode, is_reserve=False)
            c.log(f"[TV Evening] Stored visible episode {i} in database")

        first_episode = playlist[0]
        first_url = first_episode['url']
        listitem = control.item(label=f"{first_episode['tvshowtitle']} - S{first_episode['season']:02d}E{first_episode['episode']:02d}")
        listitem.setInfo('video', {
            'title': first_episode.get('title', ''),
            'tvshowtitle': first_episode.get('tvshowtitle', ''),
            'season': first_episode['season'],
            'episode': first_episode['episode'],
            'plot': first_episode.get('plot', '')
        })
        if first_episode.get('thumb'):
            listitem.setArt({'thumb': first_episode['thumb']})
        kodi_playlist.add(first_url, listitem)
        c.log(f"[TV Evening] Kodi playlist: single item only (monitor controls transitions)")

        # Store hidden reserve episode(s) in DB only — not in Kodi playlist or preview
        reserve_episodes = reserve_episodes or []
        reserve_base = len(playlist)
        for j, episode in enumerate(reserve_episodes):
            url = _build_episode_play_url(episode)
            episode['url'] = url
            db.add_episode(reserve_base + j, episode, is_reserve=True)
            c.log(f"[TV Evening] Stored reserve episode {j + 1}/{len(reserve_episodes)} in database (hidden): "
                  f"{episode['tvshowtitle']} S{episode['season']:02d}E{episode['episode']:02d}")

        c.log(f"[TV Evening] Playlist built: Kodi={kodi_playlist.size()} items, "
              f"DB visible={len(playlist)}, reserve={len(reserve_episodes)}")

        # Store metadata about this playlist session
        db.set_metadata('playlist_started', xbmc.getInfoLabel('System.Date'))
        db.set_metadata('playlist_total', len(playlist))
        db.set_metadata('visible_count', len(playlist))
        db.set_metadata('reserve_count', len(reserve_episodes))

        from . import tvevening_session
        tvevening_session.set_state(tvevening_session.STATE_START, position=0)

        # Verify playlist contents
        for i in range(kodi_playlist.size()):
            item = kodi_playlist[i]
            if item:
                path = item.getPath() if hasattr(item, 'getPath') else 'unknown'
                label = item.getLabel() if hasattr(item, 'getLabel') else 'unknown'
                c.log(f"[TV Evening] Kodi Playlist[{i}]: {label} - {path[:100]}...")

        # Verify playlist was created
        if kodi_playlist.size() == 0 or not playlist:
            c.log("[TV Evening] ERROR: Playlist is empty after building!")
            control.dialog.ok('Error', 'Failed to build playlist.\n\nPlease try again.')
            return

        # Start TV Evening Monitor BEFORE playback
        # This ensures player.onPlayBackStarted sees active monitor and skips legacy system
        c.log("[TV Evening] Starting TV Evening Monitor (BEFORE playback)")

        try:
            # Get all episodes from database
            monitor_episodes = db.get_visible_episodes()
            c.log(f"[TV Evening] Loaded {len(monitor_episodes)} visible episode(s) for monitoring")
            if reserve_episodes:
                c.log(f"[TV Evening] {len(reserve_episodes)} reserve episode(s) held for playback swap")

            # Create and start monitor BEFORE player.play()
            monitor = tvevening_monitor.start_tv_evening_session(monitor_episodes)
            c.log("[TV Evening] Monitor started successfully with session_active=True")

        except Exception as monitor_error:
            c.log(f"[TV Evening] Error starting monitor: {monitor_error}")
            c.log(f"[TV Evening] Traceback: {traceback.format_exc()}")
            control.dialog.ok('Error', 'Failed to start TV Evening monitor.\n\nPlease try again.')
            return

        # NOW start playlist playback - monitor is ready
        import time
        control.window.setProperty('midnight.tvevening.last_play_started', str(time.time()))
        player = xbmc.Player()
        player.play(kodi_playlist, startpos=0)

        c.log("[TV Evening] Started playback, waiting for player to initialize...")

        # Wait a moment for playback to initiate
        time.sleep(0.5)

        # Verify playback started
        playlist_after = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
        c.log(f"[TV Evening] After play() call - Playlist size: {playlist_after.size()}, Player active: {player.isPlaying()}")

        # Skip info notification - fullscreen overlay handles this
        # control.infoDialog(f'Playing {len(playlist)} episodes', heading='TV Evening', time=3000)
        c.log(f"[TV Evening] Skipping infoDialog (overlay active)")

    except Exception as e:
        c.log(f"[TV Evening] Error playing playlist: {e}")
        c.log(f"[TV Evening] Traceback: {traceback.format_exc()}")


def show_gap_countdown(next_episode, countdown_seconds):
    """
    Show countdown between playlist episodes.
    NOTE: This is not currently used with native Kodi playlists.
    Kodi handles playlist transitions automatically.
    Keeping this function for future enhancement if needed.

    :param dict next_episode: Next episode data
    :param int countdown_seconds: Countdown duration
    """
    try:
        c.log("[TV Evening] Gap countdown feature (currently unused)")
        return True

    except Exception as e:
        c.log(f"[TV Evening] Error in gap countdown: {e}")
        return True


def show_current_playlist():
    """
    Show the current Kodi video playlist.
    """
    try:
        c.log("[TV Evening] Showing current playlist")

        # Get current video playlist
        playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
        playlist_size = playlist.size()

        # Check player status
        player = xbmc.Player()
        is_playing = player.isPlaying()
        c.log(f"[TV Evening] Playlist size: {playlist_size}, Player active: {is_playing}")

        if playlist_size == 0:
            c.log("[TV Evening] Playlist is empty - showing dialog")
            control.dialog.ok('Playlist Empty', 'There are no items in the current playlist.\n\nAdd episodes using "My TV Evening" feature.')
            return

        # Get current playing position
        current_pos = playlist.getposition()

        # Build playlist info
        lines = []

        for i in range(playlist_size):
            item = playlist[i]
            label = item.getLabel()

            # Mark currently playing item
            if i == current_pos and xbmc.Player().isPlayingVideo():
                lines.append(f"> {i+1}. {label}")
            else:
                lines.append(f"  {i+1}. {label}")

        lines.append('')
        lines.append(f"Total: {playlist_size} episode{'s' if playlist_size != 1 else ''}")
        if current_pos >= 0:
            lines.append(f"Currently at position: {current_pos + 1}")

        playlist_text = '\n'.join(lines)

        # Show playlist with options
        dialog = xbmcgui.Dialog()

        # First show the playlist
        dialog.textviewer('Current Video Playlist', playlist_text)

        # Ask what to do
        options = [
            'Continue Playing',
            'Clear Playlist',
            'Jump to Episode...'
        ]

        choice = dialog.select('Playlist Options', options)

        if choice == 1:  # Clear playlist
            if dialog.yesno('Clear Playlist', 'Are you sure you want to clear the entire playlist?'):
                playlist.clear()
                control.infoDialog('Playlist cleared', time=2000)
        elif choice == 2:  # Jump to episode
            # Build list of episodes for selection
            episode_list = []
            for i in range(playlist_size):
                item = playlist[i]
                label = item.getLabel()
                episode_list.append(f"{i+1}. {label}")

            selected = dialog.select('Jump to Episode', episode_list)
            if selected >= 0:
                xbmc.Player().stop()
                xbmc.sleep(500)
                xbmc.Player().play(playlist, startpos=selected)

    except Exception as e:
        c.log(f"[TV Evening] Error showing playlist: {e}")
        c.log(f"[TV Evening] Traceback: {traceback.format_exc()}")
        control.dialog.ok('Error', f'Failed to show playlist.\n\n{str(e)}')




def resume_playlist(session_info):
    """
    Resume a resumable TV Evening playlist after recovery dialog.
    """
    try:
        from . import tvevening_session

        c.log("[TV Evening] Resuming playlist from saved session")

        playlist_size = session_info['playlist_size']
        current_position = session_info['current_position']
        episodes = session_info['episodes']
        initial_state = session_info.get('session_state') or tvevening_session.STATE_PAUSED

        stored = session_info.get('stored_position', current_position)
        if current_position != stored:
            c.log(
                "[TV Evening] Resume adjusted from stored position {} to {} of {}".format(
                    stored, current_position, playlist_size
                )
            )
        c.log(f"[TV Evening] Resuming at position {current_position} of {playlist_size} (state={initial_state})")

        db = tvevening_playlist_db.get_playlist_db()
        for i in range(current_position):
            if tvevening_session._episode_false_skip(episodes[i]):
                db.clear_episode_skipped(i)

        kodi_playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
        kodi_playlist.clear()

        if current_position < 0 or current_position >= len(episodes):
            c.log("[TV Evening] Invalid resume position")
            control.dialog.ok('Error', 'Invalid resume position.')
            return

        ep = episodes[current_position]
        url = ep.get('url', '')
        if not url:
            url = _build_episode_play_url(ep)
            ep['url'] = url
        if not url:
            c.log(f"[TV Evening] Episode {current_position} has no URL")
            control.dialog.ok('Error', 'Could not build play URL for resume episode.')
            return

        listitem = xbmcgui.ListItem(
            label=f"{ep.get('tvshowtitle', 'Unknown')} S{ep.get('season', 0):02d}E{ep.get('episode', 0):02d}"
        )
        listitem.setInfo('video', {
            'title': ep.get('title', 'Unknown'),
            'tvshowtitle': ep.get('tvshowtitle', 'Unknown'),
            'season': ep.get('season', 0),
            'episode': ep.get('episode', 0)
        })
        kodi_playlist.add(url, listitem)
        c.log(f"[TV Evening] Kodi playlist: single item at resume position {current_position}")

        monitor = tvevening_monitor.start_tv_evening_session(
            episodes,
            start_position=current_position,
            initial_state=initial_state,
        )
        if not monitor:
            c.log("[TV Evening] Failed to start monitor for resume")
            control.dialog.ok('Error', 'Failed to resume TV Evening monitor.')
            return

        player = xbmc.Player()
        player.play(kodi_playlist, startpos=0)

        if initial_state == tvevening_session.STATE_PAUSED:
            tvevening_session.set_state(tvevening_session.STATE_PAUSED, position=current_position)
            # Kodi resume bookmark applies on play(); re-pause so dinner-break state is preserved
            xbmc.sleep(1500)
            if player.isPlayingVideo():
                player.pause()
        else:
            tvevening_session.set_state(tvevening_session.STATE_PLAYING, position=current_position)

        c.log(f"[TV Evening] Started playback at position {current_position}")

        control.infoDialog('TV Evening resumed', time=2000)

    except Exception as e:
        c.log(f"[TV Evening] Error resuming playlist: {e}")
        import traceback
        c.log(f"[TV Evening] Traceback: {traceback.format_exc()}")
        control.dialog.ok('Error', f'Failed to resume playlist.\n\n{str(e)}')


def start_tv_evening():
    """
    Main entry point for TV Evening feature.
    """
    progress_dialog = None
    try:
        c.log("[TV Evening] Starting TV Evening")

        # Check live tracker (Simkl XOR Trakt)
        if not tracker_ready():
            if not _prompt_tracker_setup():
                return

        # Check for stale session (Kodi crashed/restarted during playback)
        from . import tvevening_recovery
        recovery_choice = tvevening_recovery.handle_recovery()

        if recovery_choice == 'continue':
            # Resume existing playlist
            c.log("[TV Evening] Resuming stale session")
            session_info = tvevening_recovery.get_session_info()
            if session_info:
                resume_playlist(session_info)
            return

        elif recovery_choice == 'delete':
            c.log("[TV Evening] User deleted stale session, not starting new one")
            return

        if recovery_choice == 'fresh':
            c.log("[TV Evening] Clearing database for fresh start")
            from . import tvevening_recovery
            tvevening_recovery.clear_stale_session()
        elif recovery_choice is None:
            from . import tvevening_session
            if tvevening_session.is_resumable_session():
                c.log("[TV Evening] Resumable session still present — aborting new start")
                return

        # Ask user for duration and keep dialog for progress
        duration, progress_dialog = ask_duration_with_progress()
        if duration is None:
            c.log("[TV Evening] User cancelled duration selection")
            # Make sure database is cleared if user cancels
            c.log("[TV Evening] Clearing database after user cancelled")
            from . import tvevening_recovery
            tvevening_recovery.clear_stale_session()
            return

        c.log(f"[TV Evening] User selected {duration} minutes")

        # Transform the duration dialog into "building" state
        c.log("[TV Evening] ========== CALLING SHOW_BUILDING() ==========")
        c.log(f"[TV Evening] progress_dialog object: {progress_dialog}")
        if progress_dialog:
            c.log("[TV Evening] Calling progress_dialog.show_building()...")
            progress_dialog.show_building()
            c.log("[TV Evening] show_building() returned")
        else:
            c.log("[TV Evening] WARNING: progress_dialog is None! Dialog not showing!")

        # Build playlist
        built = build_playlist(duration, progress_dialog)

        # Close the progress dialog
        c.log("[TV Evening] ========== CLOSING PROGRESS DIALOG ==========")
        if progress_dialog:
            c.log("[TV Evening] Calling progress_dialog.close()...")
            progress_dialog.close()
            c.log("[TV Evening] Dialog closed, deleting object...")
            progress_dialog = None
            c.log("[TV Evening] Dialog deleted")
        else:
            c.log("[TV Evening] WARNING: progress_dialog is None!")

        if not built or not built.get('visible'):
            c.log("[TV Evening] Empty playlist, cleaning up")
            from . import tvevening_recovery
            tvevening_recovery.clear_stale_session()
            skipped = (built or {}).get('skipped') or []
            if skipped:
                min_age = (built or {}).get('min_age_hours') or get_min_episode_age()
                if min_age > 0:
                    control.dialog.ok(
                        'TV Evening',
                        'No episodes passed the minimum-air-age filter ({} hours).\n\n'
                        '{} candidate episode(s) were skipped.\n\n'
                        'Lower TV Evening -> Minimum episode age in settings, '
                        'or wait until recently aired episodes are old enough.'.format(
                            min_age, len(skipped)),
                    )
                else:
                    control.dialog.ok(
                        'TV Evening',
                        'Could not build a playlist from your watch progress.',
                    )
            return

        playlist = built['visible']
        reserve_episodes = built.get('reserve') or []

        # Show playlist preview if enabled (visible episodes only — reserve stays hidden)
        if get_show_playlist_setting():
            choice = show_playlist_preview(playlist)
            if choice is None:
                c.log("[TV Evening] User cancelled at preview")
                # Clear database when user cancels
                from . import tvevening_recovery
                tvevening_recovery.clear_stale_session()
                return
            elif choice == 'first':
                c.log("[TV Evening] User chose to play first episode only")
                playlist = [playlist[0]]  # Keep only first episode
                reserve_episodes = []

        skipped = built.get('skipped') or []
        if skipped:
            show_skipped_notification(skipped, built.get('min_age_hours') or 48)

        # Play playlist
        play_playlist(playlist, reserve_episodes=reserve_episodes)

    except Exception as e:
        c.log(f"[TV Evening] Error in start_tv_evening: {e}")
        c.log(f"[TV Evening] Traceback: {traceback.format_exc()}")
        control.dialog.ok('Error', f'An error occurred building your TV Evening.\n\n{str(e)}')
    finally:
        if progress_dialog:
            try:
                progress_dialog.close()
            except Exception:
                pass
        _close_plugin_handle(succeeded=False)

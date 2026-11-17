# -*- coding: utf-8 -*-

'''
 ***********************************************************
 * Midnight Add-on
 *
 *
 * @file playcount.py
 * @package script.module.midnight
 *
 * @copyright (c) 2023, Midnight
 * @license GNU General Public License, version 3 (GPL-3.0)
 *
 ********************************************************cm*
'''

import contextlib
import traceback

from . import bookmarks
from . import cache
from . import control
from . import trakt
from . import simkl
from . import tracker
from .crewruntime import c


def use_cloud_tv_indicators() -> bool:
    """True when season/episode overlays should use cloud watched maps (Trakt or Simkl)."""
    if tracker.simkl_live():
        return True
    return bool(trakt.getTraktIndicatorsInfo())


def get_movie_indicators(refresh=False):
    try:
        if tracker.simkl_live():
            # Always use TTL cache. invalidate_movie_indicators() drops the key
            # after mark/scrobble-stop; never map refresh=True → cold sync (stalls UI).
            return simkl.cachesync_movie_indicators(timeout=24)

        if trakt.getTraktIndicatorsInfo():
            raise Exception()
        return bookmarks.get_indicators()
    except Exception:
        pass

    try:
        if not trakt.getTraktIndicatorsInfo():
            raise Exception()
        if not refresh:
            timeout = 720
        elif trakt.getWatchedActivity_from_db() < trakt.timeoutsyncMovies():
            timeout = 720
        else:
            timeout = None

        return trakt.cachesyncMovies(timeout=timeout)
    except Exception:
        pass


def get_movie_indicators_soft():
    """
    Cached movie indicators only — never cold-sync Trakt/Simkl.

    Movie search was waiting on full /users/me/watched/movies after clear-cache
    before any results appeared. Soft miss → empty overlays; next normal list syncs.
    """
    try:
        if tracker.simkl_live():
            return cache.peek(simkl._sync_movie_indicators, 24) or []

        if not trakt.getTraktIndicatorsInfo():
            return bookmarks.get_indicators() or []

        return cache.peek(trakt.syncMovies, 720, trakt.TRAKTUSER) or []
    except Exception:
        return []


def use_cloud_movie_indicators() -> bool:
    if tracker.simkl_live():
        return True
    return bool(trakt.getTraktIndicatorsInfo())


def watched_menu_labels():
    """
    CM labels for mark watched/unwatched — Simkl XOR Trakt XOR local.
    Returns (watched_label, unwatched_label).
    """
    if tracker.simkl_live():
        watched = control.lang(90321) or 'Watched in Simkl'
        unwatched = control.lang(90322) or 'Unwatched in Simkl'
        return watched, unwatched
    if trakt.getTraktIndicatorsInfo():
        return control.lang(32068), control.lang(32069)
    return control.lang(32066), control.lang(32067)


def _notify_playcount(ok: bool, watched: bool, where: str, detail: str = ''):
    try:
        if ok:
            msg = f'Marked watched on {where}' if watched else f'Marked unwatched on {where}'
            control.infoDialog(msg, sound=False, time=2500)
        else:
            msg = detail.strip() if detail else f'Failed to update {where}'
            control.infoDialog(msg, sound=True, time=3500)
    except Exception:
        pass


def get_tvshow_indicators(refresh=False):
    try:
        if tracker.simkl_live():
            # Always use TTL cache. invalidate_tv_indicators() drops the key after
            # mark/scrobble-stop; never map refresh=True → cold sync (every
            # Container.Refresh after stop was re-pulling the full library ~20-30s).
            return simkl.cachesync_tv_indicators(timeout=24)

        if not trakt.getTraktIndicatorsInfo():
            raise Exception()
        if not refresh:
            timeout = 720
        elif trakt.getWatchedActivity_from_db() < trakt.timeoutsyncTVShows():
            timeout = 720
        else:
            timeout = None
        return trakt.cachesyncTVShows(timeout=timeout)
    except Exception as e:
        failure = traceback.format_exc()
        pass
    #except Exception:
    #    pass


def getSeasonIndicators(imdb, tmdb=None):
    with contextlib.suppress(Exception):
        if tracker.simkl_live():
            return simkl.sync_season(imdb=imdb or '', tmdb=tmdb or '')
        if not trakt.getTraktIndicatorsInfo():
            raise Exception()
        return trakt.syncSeason(imdb)


def get_movie_overlay(indicators, imdb):
    try:
        if not use_cloud_movie_indicators():
            overlay = bookmarks.get_watched('movie', imdb, '', '')
        else:
            playcount = [i for i in indicators if i == imdb]
            overlay = 7 if playcount else 6
        return str(overlay)
    except Exception:
        return '6'

def movie_has_indicator(indicators, imdb):
    try:
        return False if not use_cloud_movie_indicators() else imdb in indicators
    except Exception:
        return False

def getTVShowOverlay(indicators, tmdb):
    try:
        tmdb = str(tmdb)
        playcount = [i[0] for i in indicators if str(i[0]) == tmdb and len(i[2]) >= int(i[1])]
        #playcount = 7 if len(playcount) > 0 else 6
        playcount = 7 if playcount else 6
        return str(playcount)
    except (IndexError, ValueError, TypeError):
        return '6'

def getSeasonOverlay(indicators, season):
    try:
        playcount = [i for i in indicators if int(season) == int(i)]
        #playcount = 7 if len(playcount) > 0 else 6
        playcount = 7 if playcount else 6
        return str(playcount)
    except (ValueError, TypeError):
        return '6'


def get_simkl_season_overlay(show_indicators, tmdb, season, episode_count=0):
    """
    Season overlay from Simkl watched episode map + TMDB season episode_count.

    Simkl watching seasons[].episodes[] is watched-only (sparse), so it cannot
    be used alone to decide season-complete. Require watched_count >= catalog
    episode_count (when known).
    """
    try:
        tmdb = str(tmdb)
        season = int(season)
        try:
            need = int(episode_count or 0)
        except (ValueError, TypeError):
            need = 0
        watched = []
        for row in show_indicators or []:
            try:
                if str(row[0]) == tmdb:
                    watched = row[2] or []
                    break
            except (IndexError, TypeError):
                continue
        n_watched = sum(1 for s, _e in watched if int(s) == season)
        if need > 0 and n_watched >= need:
            return '7'
        return '6'
    except (ValueError, TypeError):
        return '6'

def get_episode_overlay(indicators, imdb, tmdb, season, episode):
    """
    Return the overlay value for a given episode. If cloud indicators are off,
    looks up the overlay in the bookmarks database. Otherwise uses Trakt/Simkl maps.

    :param indicators: List of playcount indicators from trakt/simkl.
    :param imdb: The IMDB ID of the TV show.
    :param tmdb: The TMDB ID of the TV show.
    :param season: The season number of the episode.
    :param episode: The episode number of the episode.
    :return: The overlay value, as a string.

    overlay 6: Not watched
    overlay 7: Watched
    """
    try:
        # Cloud watched status is authoritative - check it first.
        # Do NOT check the progress table here: fill_progress_table() stores
        # Trakt's in-progress percentage which would block the watched check.
        if not use_cloud_tv_indicators():
            overlay = bookmarks.get_watched('episode', imdb, season, episode)
        else:
            tmdb = str(tmdb)
            playcount = [i[2] for i in (indicators or []) if str(i[0]) == tmdb]
            playcount = playcount[0] if playcount else []
            playcount = [i for i in playcount if int(season) == int(i[0]) and int(episode) == int(i[1])]
            overlay = 7 if playcount else 6
        return str(overlay)
    except (IndexError, ValueError, TypeError, KeyError) as e:
        return '6'

def markMovieDuringPlayback(imdb, watched):
    try:
        watched_n = int(watched)
        if tracker.simkl_live():
            simkl.mark_movie_history(imdb=imdb, watched=(watched_n == 7))
        elif trakt.getTraktIndicatorsInfo():
            if watched_n == 7:
                trakt.markMovieAsWatched('imdb', imdb)
            else:
                trakt.markMovieAsNotWatched('imdb', imdb)
            trakt.cachesyncMovies()
            if trakt.get_trakt_addon_movie_info():
                trakt.markMovieAsNotWatched('imdb', imdb)
    except Exception:
        pass

    try:
        if int(watched) == 7:
            bookmarks.reset(1, 1, 'movie', imdb, '', '')
    except Exception:
        pass

def markEpisodeDuringPlayback(imdb, tmdb, season, episode, watched):
    from resources.lib.modules import control as c

    try:
        watched_n = int(watched)
        if tracker.simkl_live():
            simkl.mark_episode_history(
                imdb=imdb, tmdb=tmdb, season=season, episode=episode,
                watched=(watched_n == 7),
            )
        elif trakt.get_trakt_credentials_info() and trakt.getTraktIndicatorsInfo():
            key = 'imdb' if imdb else 'tmdb'
            media_id = imdb or tmdb

            if watched_n == 7:
                trakt.markEpisodeAsWatched(key, media_id, season, episode)
            else:
                trakt.markEpisodeAsNotWatched(key, media_id, season, episode)
            trakt.cachesyncTVShows()

            if trakt.getTraktAddonEpisodeInfo():
                trakt.markEpisodeAsNotWatched(key, media_id, season, episode)
    except Exception as e:
        c.log(f"[Playcount] Tracker marking failed: {e}")

    try:
        if int(watched) == 7:
            c.log(f"[Playcount] Calling bookmarks.reset() for {imdb} S{season}E{episode}")
            bookmarks.reset(1, 1, 'episode', imdb, season, episode)
            c.log(f"[Playcount] (OK) bookmarks.reset() completed successfully")

            # Set flag for player to refresh container when user exits playback
            # (Can't refresh now - no visible container during fullscreen playback)
            from resources.lib.modules import control
            control.window.setProperty('midnight.container.needs_refresh', 'true')
            c.log("[Playcount] Set container refresh flag")
    except Exception as e:
        c.log(f"[Playcount] ERROR in bookmarks.reset(): {e}")
        import traceback
        c.log(f"[Playcount] Traceback: {traceback.format_exc()}")

#TC 2/01/19 started
def movies(imdb, query, redirect=None):
    """
    Mark a movie watched/unwatched in the active tracker and update local bookmarks.
    Provides explicit logging and isolates failures so one subsystem failing doesn't stop the other.
    """

    # validate/normalize input
    try:
        watched = int(query)
    except (ValueError, TypeError) as e:
        return

    tracker_ok = False
    where = 'local'
    # Active tracker: Simkl XOR Trakt
    try:
        if tracker.simkl_live():
            where = 'Simkl'
            tracker_ok = bool(simkl.mark_movie_history(imdb=imdb, watched=(watched == 7)))
        elif trakt.getTraktIndicatorsInfo():
            where = 'Trakt'
            if watched == 7:
                trakt.markMovieAsWatched('imdb', imdb)
            else:
                trakt.markMovieAsNotWatched('imdb', imdb)

            try:
                trakt.cachesyncMovies()
            except Exception as e:
                pass
            tracker_ok = True
    except Exception as e:
        pass

    # Update local bookmarks independently of tracker outcome.
    try:
        if watched == 7:
            bookmarks.reset(1, 1, 'movie', imdb, '', '')
        else:
            bookmarks.delete_record('movie', imdb, '', '')
        if where == 'local':
            tracker_ok = True
    except Exception as e:
        pass

    if where != 'local':
        _notify_playcount(tracker_ok, watched == 7, where)

    # Refresh UI (best-effort) — redirect keeps home widgets in sync
    try:
        control.refresh_listing(redirect)
    except Exception as e:
        pass


def episode(imdb, tmdb, season, episode, query, redirect=None):
    try:
        watched = int(query)
    except (ValueError, TypeError):
        return

    tracker_ok = False
    where = 'local'
    detail = ''
    try:
        if tracker.simkl_live():
            where = 'Simkl'
            tracker_ok, detail = simkl.mark_episode_history(
                imdb=imdb, tmdb=tmdb, season=season, episode=episode,
                watched=(watched == 7),
            )
        elif trakt.getTraktIndicatorsInfo():
            where = 'Trakt'
            key = 'imdb' if imdb else 'tmdb'
            media_id = imdb or tmdb

            if watched == 7:
                trakt.markEpisodeAsWatched(key, media_id, season, episode)
            else:
                trakt.markEpisodeAsNotWatched(key, media_id, season, episode)
            trakt.cachesyncTVShows()
            tracker_ok = True
    except Exception as e:
        c.log(f'[Playcount] episode tracker error: {e}', 1)
        detail = str(e)

    try:
        if watched == 7:
            bookmarks.reset(1, 1, 'episode', imdb, season, episode)
        else:
            bookmarks.delete_record('episode', imdb, season, episode)
        if where == 'local':
            tracker_ok = True
    except Exception as e:
        failure = traceback.format_exc()
        pass

    if where != 'local':
        _notify_playcount(tracker_ok, watched == 7, where, detail=detail)

    try:
        control.refresh_listing(redirect)
    except Exception:
        pass

def season(imdb, tmdb, season, query, redirect=None):
    """
    Marks a TV season as watched or not watched based on the provided parameters.

    Args:
        imdb (str): The IMDb ID of the TV show.
        tmdb (str): The TMDB ID of the TV show.
        season_id (int): The season number to be marked. Needs other name than season to avoid
                            confusion with the season parameter.
        watched (int): Indicator whether the season is watched (7) or not watched.

    Raises:
        Exception: If there is an error during processing, logs the exception details.
    """

    control.busy()

    try:
        watched = int(query)
        where = 'Trakt'
        ok = True
        if tracker.simkl_live():
            where = 'Simkl'
            ok = bool(simkl.mark_season_history(
                imdb=imdb, tmdb=tmdb, season=season, watched=(watched == 7),
            ))
        else:
            key = 'imdb' if imdb else 'tmdb'
            media_id = imdb or tmdb

            if watched == 7:
                trakt.markSeasonAsWatched(key, media_id, season)
            else:
                trakt.markSeasonAsNotWatched(key, media_id, season)

            # Clear cached Trakt activity AND syncTVShows cache so TV shows list refreshes indicators
            cache.cache_delete('trakt.last_activities', table='trakt')
            # Also clear the syncTVShows function cache to force immediate refresh
            sync_cache_key = cache._hash_function(trakt.syncTVShows, trakt.TRAKTUSER)
            cache.cache_delete(sync_cache_key)

            # Force immediate sync with timeout=None to get fresh data from Trakt
            trakt.cachesyncTVShows(timeout=None)

        _notify_playcount(ok, watched == 7, where)

    except Exception as e:
        failure = traceback.format_exc()
        pass

    control.refresh_listing(redirect)
    control.idle()


def tvshows(imdb, tmdb, watched, redirect=None):
    control.busy()

    try:
        watched_n = int(watched)
        where = 'Trakt'
        ok = True
        if tracker.simkl_live():
            where = 'Simkl'
            ok = bool(simkl.mark_show_history(imdb=imdb, tmdb=tmdb, watched=(watched_n == 7)))
        else:
            key = 'imdb' if imdb else 'tmdb'
            media_id = imdb or tmdb

            if not key or not media_id:
                raise Exception()

            if watched_n == 7:
                trakt.markTVShowAsWatched(key, media_id)
            else:
                trakt.markTVShowAsNotWatched(key, media_id)
            trakt.cachesyncTVShows()

        _notify_playcount(ok, watched_n == 7, where)

    except Exception as e:
        failure = traceback.format_exc()
        pass


    control.refresh_listing(redirect)
    control.idle()

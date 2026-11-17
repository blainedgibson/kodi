# -*- coding: utf-8 -*-

'''
 ***********************************************************
 * Midnight Add-on
 *
 *
 * @file navigator.py
 * @package script.module.midnight
 *
 * @copyright (c) 2023, Midnight
 * @license GNU General Public License, version 3 (GPL-3.0)
 *
 ********************************************************cm*
'''


import os
import sys
import traceback
from datetime import date, datetime
import importlib

import xbmc

from ..modules.listitem import ListItemInfoTag

from ..modules import control
from ..modules import trakt
from ..modules import simkl
from ..modules import tracker
from ..modules import cache
from ..modules import views
from ..modules import adult_pin
from ..modules import keys

from ..modules.crewruntime import c
from resources.lib.ui_theme import color



month = int(date.today().strftime('%m'))






oa = None
orion_credentials = False
try:
    import xbmcvfs as _xbmcvfs
    _orion_lib_path = _xbmcvfs.translatePath('special://home/addons/script.module.orion/lib')
    if os.path.isdir(_orion_lib_path) and _orion_lib_path not in sys.path:
        sys.path.insert(0, _orion_lib_path)
    from orion import Orion as _OrionClass
    _orion_inst = _OrionClass(keys.orion_key)
    orion_credentials = _orion_inst.userEnabled() and _orion_inst.userValid()
except Exception:
    orion_credentials = False

sysaddon = sys.argv[0] if len(sys.argv) > 0 else 'plugin://plugin.video.midnight/'
try:
    syshandle = int(sys.argv[1]) if len(sys.argv) > 1 else -1
except (TypeError, ValueError):
    syshandle = -1

def art_path():
    return getattr(c, 'get_art_path', lambda: '')()


def addon_fanart():
    return getattr(c, 'addon_fanart', lambda: '')()

imdbCredentials = c.get_setting('imdb.user') != ''

traktCredentials = trakt.get_trakt_credentials_info()
traktIndicators = trakt.getTraktIndicatorsInfo()


queueMenu = c.lang(32065)


def _devmode():
    """Read dev password at call time — import-time caching hid Developers after settings change."""
    return c.get_setting('dev_pw') == keys.dev_password


ADULT = c.get_setting('adult_pw') == keys.adult_password
def _download_path_ok(path):
    path = (path or '').strip()
    if not path or path.lower() in ('true', 'false'):
        return False
    try:
        return len(control.listDir(path)[0]) > 0
    except Exception:
        return False


DOWNLOADS = (
    c.get_setting('downloads') == 'true'
    and (
        _download_path_ok(c.get_setting('movie.download.path'))
        or _download_path_ok(c.get_setting('tv.download.path'))
    )
)


# ============================================================================
# DICTIONARY-DRIVEN MENU SYSTEM - POC Implementation
# ============================================================================
# Proof of concept for converting 91 repetitive methods to data-driven approach
# See docs/POC_DICTIONARY_NAVIGATOR.py for full architecture documentation
#
# Benefits:
# - Single source of truth for menu definitions
# - ~60-70% code reduction for static menus
# - Easy to maintain, search, and modify
# - Settings checks in data, not scattered in code
# ============================================================================

MENUS = {
    # Tools auth = top of menu for the ACTIVE tracker only when it has no token.
    # (Showing Simkl Authorize while Tracking service is Trakt looked like a forced
    # nag with no way to dismiss without touching Trakt — issue #253.)
    # Deliberate reauth + revoke stay in Settings → Accounts.
    'tools': [
        {'label': 32073, 'action': 'authTrakt', 'icon': 'trakt.png', 'icon2': 'DefaultAddonProgram.png', 'is_folder': False,
            'condition': lambda: tracker.is_trakt() and not trakt.has_trakt_token()},
        {'label': 90310, 'action': 'authSimkl', 'icon': 'simkl.png', 'icon2': 'DefaultAddonProgram.png', 'is_folder': False,
            'condition': lambda: tracker.is_simkl() and not simkl.get_simkl_credentials_info()},
        {'label': 32609, 'action': 'ResolveUrlTorrent', 'icon': 'resolveurl.png', 'icon2': 'DefaultAddonProgram.png', 'is_folder': False},
        {'label': 32128, 'action': 'OrionNavigator', 'icon': 'Orion.png', 'icon2': 'DefaultAddonProgram.png'},
        {'label': 32043, 'action': 'openSettings', 'icon': 'tools.png', 'icon2': 'DefaultAddonProgram.png', 'is_folder': False},
        {'label': 90231, 'action': 'debridManagement', 'icon': 'tools.png', 'icon2': 'DefaultAddonProgram.png'},
        {'label': 32556, 'action': 'libraryNavigator', 'icon': 'tools.png', 'icon2': ' DefaultAddonProgram.png'},
        {'label': 32049, 'action': 'viewsNavigator', 'icon': 'tools.png', 'icon2': 'DefaultAddonProgram.png', 'is_folder': False},
        {'label': 32713, 'action': 'cachingTools', 'icon': 'tools.png', 'icon2': 'DefaultAddonProgram.png'},
        {'label': 32715, 'action': 'scraperStatus', 'icon': 'tools.png', 'icon2': 'DefaultAddonProgram.png', 'is_folder': False},
        {'label': f'[B]{color("MIDNIGHT", 'header')}[/B] : {color("Reset settings Backups", 'primary')}',
            'action': 'resetSettingsBackups', 'icon': 'tools.png', 'icon2': 'DefaultAddonProgram.png', 'is_folder': False},
        {'label': 90283, 'action': 'uploadLogs', 'icon': 'tools.png', 'icon2': 'DefaultAddonProgram.png', 'is_folder': True},
        {'label': 32714, 'action': 'changelog', 'icon': 'tools.png', 'icon2': 'Default AddonProgram.png', 'is_folder': False},
        #{'label': 90308, 'action': 'revokeTrakt', 'icon': 'trakt.png', 'icon2': 'DefaultAddonProgram.png', 'is_folder': False},
        {'label': 90309, 'action': 'traktListUsage', 'icon': 'trakt.png', 'icon2': 'DefaultAddonProgram.png', 'is_folder': False,
            'condition': lambda: tracker.is_trakt()}
    ],
    'uploadLogs': [
        {'label': 90270, 'action': 'uploadKodiLog', 'icon': 'tools.png', 'icon2': 'DefaultAddonProgram.png', 'is_folder': False},
        {'label': 90271, 'action': 'uploadCrewLog', 'icon': 'tools.png', 'icon2': 'DefaultAddonProgram.png', 'is_folder': False}
    ],
    'movies': [
        {'label': 32003, 'action': 'mymovieliteNavigator', 'icon': 'mymovies.png', 'icon2': 'DefaultVideoPlaylists.png'},
        {'label': 90160, 'action': 'movies&url=xristmas', 'icon': 'holidays.png', 'icon2': 'DefaultMovies.png',
            'condition': lambda: (c.get_setting('dev_pw') == keys.dev_password) or (month == 12)},
        {'label': 32005, 'action': 'movieWidget', 'icon': 'latest-movies.png', 'icon2': 'DefaultMovies.png', 'setting': 'navi.moviewidget'},
        {'label': 32022, 'action': 'movies&url=theaters', 'icon': 'in-theaters.png', 'icon2': 'DefaultMovies.png', 'setting': 'navi.movietheaters'},
        {'label': 32017, 'action': 'movies&url=trending', 'icon': 'trending.png', 'icon2': 'DefaultMovies.png', 'setting': 'navi.movietrending'},
        {'label': 32018, 'action': 'movies&url=popular', 'icon': 'most-popular.png', 'icon2': 'DefaultMovies.png', 'setting': 'navi.moviepopular'},
        {'label': 90253, 'action': 'movies&url=tmdb_movie_latest_releases', 'icon': 'latest-releases.png', 'icon2': 'latest-releases.png', 'setting': 'navi.latestrelease'},
        {'label': 90254, 'action': 'movies&url=tmdb_movie_premieres', 'icon': 'premieres.png', 'icon2': 'premieres.png', 'setting': 'navi.premieres'},
        {'label': 90260, 'action': 'movies_calendars_menu', 'icon': 'simkl.png', 'icon2': 'DefaultMovies.png',
            'condition': lambda: tracker.simkl_live()},
        {'label': 90260, 'action': 'movies_calendars_menu', 'icon': 'moviecalendar.png', 'icon2': 'DefaultMovies.png',
            'condition': lambda: not tracker.simkl_live()},
        {'label': 90166, 'action': 'movies&url=tmdb_networks_no_unaired&tid=337', 'icon': 'disney.png', 'icon2': 'disney.png', 'setting': 'navi.disneym'},
        {'label': 90051, 'action': 'traktlist', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png', 'setting': 'navi.traktlist'},
        {'label': 90210, 'action': 'tmdbmovieslist', 'icon': 'tmdb.png', 'icon2': 'DefaultMovies.png', 'setting': 'navi.tvTmdb'},
        {'label': 32020, 'action': 'movies&url=boxoffice', 'icon': 'box-office.png', 'icon2': 'DefaultMovies.png', 'setting': 'navi.movieboxoffice'},
        {'label': 32021, 'action': 'movies&url=oscars', 'icon': 'oscar-winners.png', 'icon2': 'oscar-winners.png', 'setting': 'navi.movieoscars'},
        {'label': 32011, 'action': 'movieGenres', 'icon': 'genres.png', 'icon2': 'DefaultMovies.png', 'setting': 'navi.moviegenre'},
        {'label': 32015, 'action': 'movieCertificates', 'icon': 'certificates.png', 'icon2': 'certificates.png', 'setting': 'navi.movieCertificates'},
        {'label': 32012, 'action': 'movieYears', 'icon': 'years.png', 'icon2': 'DefaultMovies.png', 'setting': 'navi.movieyears'},
        {'label': 32014, 'action': 'movieLanguages', 'icon': 'international.png', 'icon2': 'DefaultMovies.png', 'setting': 'navi.movielanguages'},
        {'label': 32019, 'action': 'movies&url=views', 'icon': 'most-voted.png', 'icon2': 'DefaultMovies.png', 'setting': 'navi.movieviews'},
        {'label': 32013, 'action': 'moviePersons', 'icon': 'people.png', 'icon2': 'DefaultMovies.png', 'setting': 'navi.moviepersons'},
        {'label': 32028, 'action': 'moviePerson', 'icon': 'people-search.png', 'icon2': 'DefaultMovies.png', 'is_folder': False},
        {'label': 32010, 'action': 'movieSearch', 'icon': 'search.png', 'icon2': 'DefaultMovies.png'},
        # Folder so results list into this handle — Back returns to Movies (not a dialog trap).
        {'label': 90223, 'action': 'movieAdvancedSearch', 'icon': 'search.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90225, 'action': 'movieSavedFilters', 'icon': 'saved-filters.png', 'icon2': 'DefaultMovies.png'}
    ],
    'tvshows': [
        {'label': 32004, 'action': 'mytvliteNavigator', 'icon': 'mytvshows.png', 'icon2': 'DefaultVideoPlaylists.png'},
        {'label': 32730, 'action': 'tvevening', 'icon': 'tv_evening.png', 'icon2': 'tv_evening.png', 'is_folder': False},
        {'label': 32026, 'action': 'tvshows&url=premiere', 'icon': 'new-tvshows.png', 'icon2': 'DefaultTVShows.png', 'setting': 'navi.tvPremier'},
        {'label': 32024, 'action': 'tvshows&url=airing', 'icon': 'airing-today.png', 'icon2': 'DefaultTVShows.png', 'setting': 'navi.tvAiring'},
        {'label': 32017, 'action': 'tvshows&url=trending', 'icon': 'trending.png', 'icon2': 'DefaultRecentlyAddedEpisodes.png', 'setting': 'navi.tvTrending'},
        {'label': 32018, 'action': 'tvshows&url=popular', 'icon': 'most-popular.png', 'icon2': 'DefaultTVShows.png', 'setting': 'navi.tvPopular'},
        {'label': 32027, 'action': 'calendars_menu', 'icon': 'tvcalendar.png', 'icon2': 'DefaultRecentlyAddedEpisodes.png', 'setting': 'navi.tvCalendar'},
        {'label': c.lang(90285), 'action': 'calendarsAllNavigator', 'icon': 'tvcalendar.png', 'icon2': 'DefaultRecentlyAddedEpisodes.png'},
        {'label': 90210, 'action': 'tmdbtvlist', 'icon': 'tmdb.png', 'icon2': 'DefaultVideoPlaylists.png', 'setting': 'navi.tvTmdb'},
        {'label': 90166, 'action': 'tvshows&url=tmdb_networks&tid=2739', 'icon': 'disney.png', 'icon2': 'disney.png', 'setting': 'navi.disney'},
        {'label': 90218, 'action': 'tvshows&url=tmdb_networks&tid=213', 'icon': 'netflix.png', 'icon2': 'netflix.png', 'setting': 'navi.netflix'},
        {'label': 90219, 'action': 'tvshows&url=tmdb_networks&tid=49', 'icon': 'hbo.png', 'icon2': 'hbo.png', 'setting': 'navi.hbo'},
        {'label': 90170, 'action': 'tvshows&url=tmdb_networks&tid=2552', 'icon': 'apple.png', 'icon2': 'apple.png', 'setting': 'navi.apple'},
        {'label': 32700, 'action': 'docuNavigator', 'icon': 'documentaries.png', 'icon2': 'DefaultMovies.png'},
        {'label': 32011, 'action': 'tvGenres', 'icon': 'genres.png', 'icon2': 'DefaultTVShows.png', 'setting': 'navi.tvGenres'},

        {'label': 32015, 'action': 'tvCertificates', 'icon': 'certificates.png', 'icon2': 'certificates.png', 'setting': 'navi.tvCertificates'},
        {'label': 32016, 'action': 'tvNetworks', 'icon': 'networks.png', 'icon2': 'DefaultTVShows.png', 'setting': 'navi.tvNetworks'},
        {'label': 32023, 'action': 'tvshows&url=rating', 'icon': 'highly-rated.png', 'icon2': 'DefaultTVShows.png', 'setting': 'navi.tvRating'},
        {'label': 32019, 'action': 'tvshows&url=views', 'icon': 'most-voted2.png', 'icon2': 'DefaultTVShows.png', 'setting': 'navi.tvViews'},
        {'label': 32014, 'action': 'tvLanguages', 'icon': 'international.png', 'icon2': 'DefaultTVShows.png', 'setting': 'navi.tvLanguages'},
        {'label': 32025, 'action': 'tvshows&url=active', 'icon': 'returning-tvshows.png', 'icon2': 'DefaultTVShows.png', 'setting': 'navi.tvActive'},
        {'label': 32028, 'action': 'tvPerson', 'icon': 'people-search.png', 'icon2': 'DefaultTVShows.png', 'is_folder': False},
        {'label': 32010, 'action': 'tvSearch', 'icon': 'search.png', 'icon2': 'DefaultTVShows.png'},
        # Folder so results list into this handle — Back returns to TV (not a dialog trap).
        {'label': 90223, 'action': 'tvAdvancedSearch', 'icon': 'search.png', 'icon2': 'DefaultTVShows.png'},
        {'label': 90225, 'action': 'tvSavedFilters', 'icon': 'saved-filters.png', 'icon2': 'DefaultTVShows.png'}
    ],
    'debridManagement': [
        {'label': 90232, 'action': 'debridAccountManager', 'icon': 'tools.png', 'icon2': 'DefaultAddonProgram.png', 'is_folder': False},
        {'label': 90233, 'action': 'debridCloud', 'icon': 'tools.png', 'icon2': 'DefaultAddonProgram.png'}
    ],
    'debridCloud': [
        {'label': 90234, 'action': 'rd_cloud', 'icon': 'tools.png', 'icon2': 'DefaultAddonProgram.png'},
        {'label': 90235, 'action': 'ad_cloud', 'icon': 'tools.png', 'icon2': 'DefaultAddonProgram.png'},
        {'label': 90236, 'action': 'pm_cloud', 'icon': 'tools.png', 'icon2': 'DefaultAddonProgram.png'},
        {'label': 90284, 'action': 'tb_cloud', 'icon': 'tools.png', 'icon2': 'DefaultAddonProgram.png'}
    ],
    'cachingTools': [
        {'label': 32050, 'action': 'clearSources', 'icon': 'tools.png', 'icon2': 'DefaultAddonProgram.png', 'is_folder': False},
        {'label': 32604, 'action': 'clearCacheSearch', 'icon': 'tools.png', 'icon2': 'DefaultAddonProgram.png', 'is_folder': False},
        {'label': 32052, 'action': 'clearCache', 'icon': 'tools.png', 'icon2': 'DefaultAddonProgram.png', 'is_folder': False},
        {'label': 32614, 'action': 'clearMetaCache', 'icon': 'tools.png', 'icon2': 'DefaultAddonProgram.png', 'is_folder': False},
        {'label': 32613, 'action': 'clearAllCache', 'icon': 'tools.png', 'icon2': 'DefaultAddonProgram.png', 'is_folder': False}
    ],
    'search': [
        {'label': 32001, 'action': 'movieSearch', 'icon': 'search.png', 'icon2': 'DefaultMovies.png'},
        {'label': 32002, 'action': 'tvSearch', 'icon': 'search.png', 'icon2': 'DefaultTVShows.png'},
        {'label': 32029, 'action': 'moviePerson', 'icon': 'people-search.png', 'icon2': 'DefaultMovies.png', 'is_folder': False},
        {'label': 32030, 'action': 'tvPerson', 'icon': 'people-search.png', 'icon2': 'DefaultTVShows.png', 'is_folder': False}
    ],
    'bluehat': [
        # FULL LIVE SITES first (White 2026-10-18-2). Label hardcoded until next plugin ship (#90155).
        # Misc Sports (#90114) parked - Xbox pain; router root_misc_sports still exists.
        {'label': f'{color("¤", 'header')} [B]{color("FULL LIVE SITES", 'header')}[/B] {color("¤", 'header')}', 'action': 'fullsites', 'icon': 'misc_sports.png', 'icon2': 'misc_sports.png'},
        {'label': 90025, 'action': 'nfl', 'icon': 'nfl.png', 'icon2': 'nfl.png'},
        {'label': 90026, 'action': 'nhl', 'icon': 'nhl.png', 'icon2': 'nhl.png'},
        {'label': 90027, 'action': 'nba', 'icon': 'nba.png', 'icon2': 'nba.png'},
        {'label': 90024, 'action': 'mlb', 'icon': 'mlb.png', 'icon2': 'mlb.png'},
        {'label': 90023, 'action': 'ncaa', 'icon': 'ncaa.png', 'icon2': 'ncaa.png'},
        {'label': 90156, 'action': 'ncaab', 'icon': 'ncaab.png', 'icon2': 'ncaab.png'},
        {'label': 90028, 'action': 'ufc', 'icon': 'ufc.png', 'icon2': 'ufc.png'},
        {'label': 90049, 'action': 'wwe', 'icon': 'wwe.png', 'icon2': 'wwe.png'},
        {'label': 90115, 'action': 'boxing', 'icon': 'boxing.png', 'icon2': 'boxing.png'},
        {'label': 90046, 'action': 'fifa', 'icon': 'fifa.png', 'icon2': 'fifa.png'},
        {'label': 90136, 'action': 'tennis', 'icon': 'tennis.png', 'icon2': 'tennis.png'},
        {'label': 90047, 'action': 'motogp', 'icon': 'motogp.png', 'icon2': 'motogp.png'},
        {'label': 90151, 'action': 'f1', 'icon': 'f1.png', 'icon2': 'f1.png'},
        {'label': 90153, 'action': 'pga', 'icon': 'pga.png', 'icon2': 'pga.png'},
        #{'label': 90154, 'action': 'cricket', 'icon': 'cricket.png', 'icon2': 'cricket.png'},
        {'label': 90152, 'action': 'nascar', 'icon': 'nascar.png', 'icon2': 'nascar.png'},
        #{'label': 90142, 'action': 'lfl', 'icon': 'lfl.png', 'icon2': 'lfl.png'},
        {'label': 90031, 'action': 'sreplays', 'icon': 'sports_replays.png', 'icon2': 'sports_replays.png'}
    ],
    'tmdbmovieslist': [
        {'label': 90211, 'action': 'movies&url=tmdb_movie_top_rated', 'icon': 'tmdb.png', 'icon2': 'DefaultTVShows.png'},
        {'label': 90212, 'action': 'movies&url=tmdb_movie_popular', 'icon': 'tmdb.png', 'icon2': 'DefaultTVShows.png'},
        {'label': 90250, 'action': 'movies&url=tmdb_movie_now_playing', 'icon': 'tmdb.png', 'icon2': 'DefaultTVShows.png'},
        {'label': 90251, 'action': 'movies&url=tmdb_movie_upcoming', 'icon': 'tmdb.png', 'icon2': 'DefaultTVShows.png'},
        {'label': 90253, 'action': 'movies&url=tmdb_movie_latest_releases', 'icon': 'tmdb.png', 'icon2': 'DefaultTVShows.png'},
        {'label': 90254, 'action': 'movies&url=tmdb_movie_premieres', 'icon': 'tmdb.png', 'icon2': 'DefaultTVShows.png'},
        {'label': 90215, 'action': 'movies&url=tmdb_movie_trending_day', 'icon': 'tmdb.png', 'icon2': 'DefaultTVShows.png'},
        {'label': 90216, 'action': 'movies&url=tmdb_movie_trending_week', 'icon': 'tmdb.png', 'icon2': 'DefaultTVShows.png'},
        {'label': 90217, 'action': 'movies&url=tmdb_movie_discover_year', 'icon': 'tmdb.png', 'icon2': 'DefaultTVShows.png'}
    ],
    'tmdbtvlist': [
        {'label': 90211, 'action': 'tvshows&url=tmdb_tv_top_rated', 'icon': 'tmdb.png', 'icon2': 'DefaultTVShows.png'},
        {'label': 90212, 'action': 'tvshows&url=tmdb_tv_popular_tv', 'icon': 'tmdb.png', 'icon2': 'DefaultTVShows.png'},
        {'label': 90213, 'action': 'tvshows&url=tmdb_tv_on_the_air', 'icon': 'tmdb.png', 'icon2': 'DefaultTVShows.png'},
        {'label': 90214, 'action': 'tvshows&url=tmdb_tv_airing_today', 'icon': 'tmdb.png', 'icon2': 'DefaultTVShows.png'},
        {'label': 90252, 'action': 'tvshows&url=tmdb_tv_upcoming', 'icon': 'tmdb.png', 'icon2': 'DefaultTVShows.png'},
        {'label': 90215, 'action': 'tvshows&url=tmdb_tv_trending_day', 'icon': 'tmdb.png', 'icon2': 'DefaultTVShows.png'},
        {'label': 90216, 'action': 'tvshows&url=tmdb_tv_trending_week', 'icon': 'tmdb.png', 'icon2': 'DefaultTVShows.png'},
        {'label': 90217, 'action': 'tvshows&url=tmdb_tv_discover_year', 'icon': 'tmdb.png', 'icon2': 'DefaultTVShows.png'}
    ],
    'orionoid': [
        {'label': 32143, 'action': 'installOrion', 'icon': 'orion.png', 'icon2': 'orion.png'},
        {'label': 32136, 'action': 'authorizeOrion', 'icon': 'orion.png', 'icon2': 'orion.png'},
        {'label': 32142, 'action': 'registerOrion', 'icon': 'orion.png', 'icon2': 'orion.png'},
        {'label': 32137, 'action': 'userInfoOrion', 'icon': 'orion.png', 'icon2': 'orion.png'},
        {'label': 32139, 'action': 'settingsOrion', 'icon': 'orion.png', 'icon2': 'orion.png'},
        {'label': 32138, 'action': 'filtersOrion', 'icon': 'orion.png', 'icon2': 'orion.png'},
        {'label': 32140, 'action': 'globalFiltersOrion', 'icon': 'orion.png', 'icon2': 'orion.png'},
        {'label': 32141, 'action': 'serverOrion', 'icon': 'orion.png', 'icon2': 'orion.png'}
    ],
    'holidays': [
        {'label': 90161, 'action': 'movies&url=top50_holiday', 'icon': 'holidays.png', 'icon2': 'holidays.png'},
        {'label': 90162, 'action': 'movies&url=best_holiday', 'icon': 'holidays.png', 'icon2': 'holidays.png'},
        {'label': 90158, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/christmas-movies/items?', 'icon': 'holidays.png', 'icon2': 'holidays.png'},
        {'label': 90159, 'action': 'movies&url=https://api.trakt.tv/users/cjcope/lists/hallmark-christmas/items?', 'icon': 'holidays.png', 'icon2': 'holidays.png'},
        {'label': 90160, 'action': 'movies&url=https://api.trakt.tv/users/mkadam68/lists/christmas-list/items?', 'icon': 'holidays.png', 'icon2': 'holidays.png'}
    ],
    'halloween': [
        {'label': 32203, 'action': 'movies&url=https://api.trakt.tv/users/istoit/lists/halloween-fun-frights/items?', 'icon': 'halloween.png', 'icon2': 'halloween.png'},
        {'label': 32204, 'action': 'movies&url=https://trakt.tv/users/29zombies/lists/halloween/items?', 'icon': 'halloween.png', 'icon2': 'halloween.png'},
        {'label': 32205, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/halloween-movies/items?', 'icon': 'halloween.png', 'icon2': 'halloween.png'}
    ],
    'kids': [
        {'label': 90272, 'action': 'movies&url=kids_movies_all', 'icon': 'genres.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90273, 'action': 'movies&url=kids_movies_animation', 'icon': 'genres.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90274, 'action': 'movies&url=kids_movies_family', 'icon': 'genres.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90275, 'action': 'tvshows&url=kids_tv_all', 'icon': 'genres2.png', 'icon2': 'DefaultTVShows.png'},
        {'label': 90276, 'action': 'tvshows&url=kids_tv_animation', 'icon': 'genres2.png', 'icon2': 'DefaultTVShows.png'},
        {'label': 90277, 'action': 'tvshows&url=kids_tv_family', 'icon': 'genres2.png', 'icon2': 'DefaultTVShows.png'},
        {'label': 90278, 'action': 'tvshows&url=kids_tv_kids', 'icon': 'genres2.png', 'icon2': 'DefaultTVShows.png'}
    ],
    'calendars_menu': [
        {'label': 32024, 'action': 'calendar&url=calendar_today', 'icon': 'tvcalendar.png', 'icon2': 'DefaultRecentlyAddedEpisodes.png', 'queue': True},
        {'label': 90266, 'action': 'calendar&url=calendar_thisweek', 'icon': 'tvcalendar.png', 'icon2': 'DefaultRecentlyAddedEpisodes.png', 'queue': True},
        {'label': 90269, 'action': 'calendar&url=calendar_nextweek', 'icon': 'tvcalendar.png', 'icon2': 'DefaultRecentlyAddedEpisodes.png', 'queue': True},
        {'label': 32038, 'action': 'calendar&url=mycalendar', 'icon': 'tvcalendar.png', 'icon2': 'DefaultRecentlyAddedEpisodes.png', 'queue': True},
    ],
    'calendars_all_menu': [
        {'label': c.lang(90286), 'action': 'calendar&url=calendar_all_today',      'icon': 'tvcalendar.png', 'icon2': 'DefaultRecentlyAddedEpisodes.png', 'queue': True},
        {'label': c.lang(90287), 'action': 'calendar&url=calendar_all_thisweek',   'icon': 'tvcalendar.png', 'icon2': 'DefaultRecentlyAddedEpisodes.png', 'queue': True},
        {'label': c.lang(90288), 'action': 'calendar&url=calendar_all_thismonth',  'icon': 'tvcalendar.png', 'icon2': 'DefaultRecentlyAddedEpisodes.png', 'queue': True},
        {'label': 90268, 'action': 'calendar&url=added', 'icon': 'tvcalendar.png', 'icon2': 'DefaultRecentlyAddedEpisodes.png', 'queue': True}
    ],
    'movies_calendars_menu': [
        {'label': 90279, 'action': 'movies_calendar&url=calendar_thisweek', 'icon': 'simkl.png', 'icon2': 'DefaultMovies.png', 'queue': True,
            'condition': lambda: tracker.simkl_live()},
        {'label': 90279, 'action': 'movies_calendar&url=calendar_thisweek', 'icon': 'moviecalendar.png', 'icon2': 'DefaultMovies.png', 'queue': True,
            'condition': lambda: not tracker.simkl_live()},
        {'label': 90280, 'action': 'movies_calendar&url=calendar_thismonth', 'icon': 'simkl.png', 'icon2': 'DefaultMovies.png', 'queue': True,
            'condition': lambda: tracker.simkl_live()},
        {'label': 90280, 'action': 'movies_calendar&url=calendar_thismonth', 'icon': 'moviecalendar.png', 'icon2': 'DefaultMovies.png', 'queue': True,
            'condition': lambda: not tracker.simkl_live()},
        {'label': 90281, 'action': 'movies_calendar&url=calendar_dvd_thisweek', 'icon': 'moviecalendar.png', 'icon2': 'DefaultMovies.png', 'queue': True},
        {'label': 90282, 'action': 'movies_calendar&url=calendar_dvd_thismonth', 'icon': 'moviecalendar.png', 'icon2': 'DefaultMovies.png', 'queue': True}
    ],
    'root': [
        {'label': f'{color("¤", 'header')} [B]{color("Developers", 'header')}[/B] {color("¤", 'header')}', 'action': 'developers', 'icon': 'main_orangehat.png', 'icon2': 'main_orangehat.png',
            'condition': lambda: _devmode()},
        {'label': 90157, 'action': 'holidaysNavigator', 'icon': 'holidays.png', 'icon2': 'holidays.png',
            'condition': lambda self: self.get_menu_enabled('navi.holidays')},
        {'label': 30201, 'action': 'halloweenNavigator', 'icon': 'halloween.png', 'icon2': 'halloween.png',
            'condition': lambda self: self.get_menu_enabled('navi.halloween')},
        {'label': 32001, 'action': 'movieNavigator', 'icon': 'main_movies.png', 'icon2': 'DefaultMovies.png', 'setting': 'navi.movies'},
        {'label': 32002, 'action': 'tvNavigator', 'icon': 'main_tvshows.png', 'icon2': 'DefaultTVShows.png', 'setting': 'navi.tvshows'},
        {'label': 90006, 'action': 'bluehat', 'icon': 'main_bluehat.png', 'icon2': 'DefaultMovies.png', 'setting': 'navi.sports'},
        {'label': 90009, 'action': 'kidsNavigator', 'icon': 'main_greyhat.png', 'icon2': 'DefaultTVShows.png', 'setting': 'navi.kids'},
        {'label': 90011, 'action': 'greenhat', 'icon': 'main_greenhat.png', 'icon2': 'DefaultMovies.png', 'setting': 'navi.1clicks'},
        {'label': 90189, 'action': 'purplehat', 'icon': 'main_purplehat.png', 'icon2': 'DefaultMovies.png', 'setting': 'navi.purplehat'},
        # ADULT SECTION: Handled separately in root() due to dynamic label from AdultPIN.get_section_name()
        {'label': 90167, 'action': 'plist', 'icon': 'userlists.png', 'icon2': 'userlists.png', 'setting': 'navi.personal.list'},
        {'label': 32008, 'action': 'toolNavigator', 'icon': 'main_tools.png', 'icon2': 'DefaultAddonProgram.png'},
        {'label': 32009, 'action': 'downloadNavigator', 'icon': 'downloads.png', 'icon2': 'DefaultFolder.png',
            'condition': lambda: DOWNLOADS},
        {'label': 32010, 'action': 'searchNavigator', 'icon': 'main_search.png', 'icon2': 'DefaultFolder.png'}
    ],
    'mymovies': [
        # Trakt Credentials required items
        {'label': 32624, 'action': 'movieProgress', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png',
            'condition': lambda: trakt.get_trakt_credentials_info()},
        {'label': 32032, 'action': 'movies&url=traktcollection', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png', 'queue': True,
            'context': (32551, 'moviesToLibrary&url=traktcollection'), 'condition': lambda: trakt.get_trakt_credentials_info()},
        {'label': 32033, 'action': 'movies&url=traktwatchlist', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png', 'queue': True,
            'context': (32551, 'moviesToLibrary&url=traktwatchlist'), 'condition': lambda: trakt.get_trakt_credentials_info()},
        {'label': 32035, 'action': 'movies&url=traktfeatured', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png', 'queue': True,
            'condition': lambda: trakt.get_trakt_credentials_info()},
        # New Trakt discovery features (added 2026-02-24)
        {'label': 90238, 'action': 'movies&url=traktrecommendations', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png', 'queue': True,
            'condition': lambda: trakt.get_trakt_credentials_info() and c.get_setting('navi.traktrecommendations') != 'false'},
        {'label': 90239, 'action': 'movies&url=traktpopular', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png', 'queue': True,
            'condition': lambda: trakt.get_trakt_credentials_info() and c.get_setting('navi.traktpopular') != 'false'},
        {'label': 90240, 'action': 'movies&url=traktanticipated', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png', 'queue': True,
            'condition': lambda: trakt.get_trakt_credentials_info() and c.get_setting('navi.traktanticipated') != 'false'},
        {'label': 90241, 'action': 'movies&url=traktplayed', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png', 'queue': True,
            'condition': lambda: trakt.get_trakt_credentials_info() and c.get_setting('navi.traktplayed') != 'false'},
        {'label': 90242, 'action': 'movies&url=traktwatched', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png', 'queue': True,
            'condition': lambda: trakt.get_trakt_credentials_info() and c.get_setting('navi.traktwatched') != 'false'},
        {'label': 90243, 'action': 'movies&url=traktcollected', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png', 'queue': True,
            'condition': lambda: trakt.get_trakt_credentials_info() and c.get_setting('navi.traktcollected') != 'false'},
        # My Ratings and Statistics menus (added 2026-03-10)
        {'label': 90263, 'action': 'traktMyRatings', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png',
            'is_folder': False, 'condition': lambda: trakt.get_trakt_credentials_info()},
        {'label': 90265, 'action': 'traktMyStatistics', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png',
            'is_folder': False, 'condition': lambda: trakt.get_trakt_credentials_info()},
        # Trakt Indicators required items
        {'label': 32036, 'action': 'movies&url=trakthistory', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png', 'queue': True,
            'condition': lambda: trakt.getTraktIndicatorsInfo()},
        # Simkl personal lists (Continue Watching + plantowatch / completed / dropped)
        {'label': 32624, 'action': 'movieProgress', 'icon': 'simkl.png', 'icon2': 'DefaultMovies.png', 'queue': True,
            'condition': lambda: tracker.simkl_live()},
        {'label': 32033, 'action': 'movies&url=simklwatchlist', 'icon': 'simkl.png', 'icon2': 'DefaultMovies.png', 'queue': True,
            'condition': lambda: tracker.simkl_live()},
        {'label': 32036, 'action': 'movies&url=simklhistory', 'icon': 'simkl.png', 'icon2': 'DefaultMovies.png', 'queue': True,
            'condition': lambda: tracker.simkl_live()},
        {'label': 90313, 'action': 'movies&url=simkldropped', 'icon': 'simkl.png', 'icon2': 'DefaultMovies.png', 'queue': True,
            'condition': lambda: tracker.simkl_live()},
        {'label': 90263, 'action': 'movies&url=simklratings', 'icon': 'simkl.png', 'icon2': 'DefaultMovies.png', 'queue': True,
            'condition': lambda: tracker.simkl_live()},
        {'label': 32039, 'action': 'movieUserlists', 'icon': 'userlists.png', 'icon2': 'DefaultMovies.png',
            'condition': lambda: trakt.get_trakt_credentials_info()},
        # lite=False items (shown when not in lite mode)
        {'label': 32031, 'action': 'movieliteNavigator', 'icon': 'movies.png', 'icon2': 'DefaultMovies.png',
            'condition': lambda self: not self.lite_mode},
        {'label': 32028, 'action': 'moviePerson', 'icon': 'people-search.png', 'icon2': 'DefaultMovies.png',
            'condition': lambda self: not self.lite_mode, 'is_folder': False},
        {'label': 32010, 'action': 'movieSearch', 'icon': 'search.png', 'icon2': 'DefaultMovies.png',
            'condition': lambda self: not self.lite_mode}
    ],
    'mytvshows': [
        # Trakt Credentials required items
        {'label': 32006, 'action': 'calendar&url=added', 'icon': 'latest-episodes.png', 'icon2': 'DefaultRecentlyAddedEpisodes.png', 'queue': True,
            'setting': 'navi.tvAdded', 'condition': lambda: trakt.get_trakt_credentials_info()},
        {'label': 32032, 'action': 'tvshows&url=traktcollection', 'icon': 'trakt.png', 'icon2': 'DefaultTVShows.png',
            'context': (32551, 'tvshowsToLibrary&url=traktcollection'), 'condition': lambda: trakt.get_trakt_credentials_info()},
        {'label': 32033, 'action': 'tvshows&url=traktwatchlist', 'icon': 'trakt.png', 'icon2': 'DefaultTVShows.png',
            'context': (32551, 'tvshowsToLibrary&url=traktwatchlist'), 'condition': lambda: trakt.get_trakt_credentials_info()},
        {'label': 32035, 'action': 'tvshows&url=traktfeatured', 'icon': 'trakt.png', 'icon2': 'DefaultTVShows.png',
            'condition': lambda: trakt.get_trakt_credentials_info()},
        # New Trakt discovery features for TV shows (added 2026-02-24)
        {'label': 90244, 'action': 'tvshows&url=traktrecommendations', 'icon': 'trakt.png', 'icon2': 'DefaultTVShows.png', 'queue': True,
            'condition': lambda: trakt.get_trakt_credentials_info() and c.get_setting('navi.traktrecommendations_tv') != 'false'},
        {'label': 90245, 'action': 'tvshows&url=traktpopular', 'icon': 'trakt.png', 'icon2': 'DefaultTVShows.png', 'queue': True,
            'condition': lambda: trakt.get_trakt_credentials_info() and c.get_setting('navi.traktpopular_tv') != 'false'},
        {'label': 90246, 'action': 'tvshows&url=traktanticipated', 'icon': 'trakt.png', 'icon2': 'DefaultTVShows.png', 'queue': True,
            'condition': lambda: trakt.get_trakt_credentials_info() and c.get_setting('navi.traktanticipated_tv') != 'false'},
        {'label': 90247, 'action': 'tvshows&url=traktplayed', 'icon': 'trakt.png', 'icon2': 'DefaultTVShows.png', 'queue': True,
            'condition': lambda: trakt.get_trakt_credentials_info() and c.get_setting('navi.traktplayed_tv') != 'false'},
        # My Ratings, Statistics, and Calendar menus (added 2026-03-10)
        {'label': 90263, 'action': 'traktMyRatings', 'icon': 'trakt.png', 'icon2': 'DefaultTVShows.png',
            'is_folder': False, 'condition': lambda: trakt.get_trakt_credentials_info()},
        {'label': 90265, 'action': 'traktMyStatistics', 'icon': 'trakt.png', 'icon2': 'DefaultTVShows.png',
            'is_folder': False, 'condition': lambda: trakt.get_trakt_credentials_info()},
        {'label': 32027, 'action': 'calendars_menu', 'icon': 'tvcalendar.png', 'icon2': 'DefaultTVShows.png',
            'condition': lambda: trakt.get_trakt_credentials_info()},
        {'label': 32027, 'action': 'calendars_menu', 'icon': 'simkl.png', 'icon2': 'DefaultRecentlyAddedEpisodes.png',
            'condition': lambda: tracker.simkl_live()},
        {'label': 90248, 'action': 'tvshows&url=traktwatched', 'icon': 'trakt.png', 'icon2': 'DefaultTVShows.png', 'queue': True,
            'condition': lambda: trakt.get_trakt_credentials_info() and c.get_setting('navi.traktwatched_tv') != 'false'},
        {'label': 90249, 'action': 'tvshows&url=traktcollected', 'icon': 'trakt.png', 'icon2': 'DefaultTVShows.png', 'queue': True,
            'condition': lambda: trakt.get_trakt_credentials_info() and c.get_setting('navi.traktcollected_tv') != 'false'},
        # Trakt Indicators required items
        {'label': 32036, 'action': 'calendar&url=trakthistory', 'icon': 'trakt.png', 'icon2': 'DefaultTVShows.png', 'queue': True,
            'condition': lambda: trakt.getTraktIndicatorsInfo()},
        {'label': 32591, 'action': 'progress_shows', 'icon': 'trakt.png', 'icon2': 'DefaultTVShows.png', 'queue': True,
            'condition': lambda: trakt.getTraktIndicatorsInfo()},
        {'label': 32592, 'action': 'progress_next_episodes', 'icon': 'trakt.png', 'icon2': 'DefaultRecentlyAddedEpisodes.png', 'queue': True,
            'condition': lambda: trakt.getTraktIndicatorsInfo()},
        {'label': 32593, 'action': 'progress_in_progress_episodes', 'icon': 'trakt.png', 'icon2': 'DefaultRecentlyAddedEpisodes.png', 'queue': True,
            'condition': lambda: trakt.getTraktIndicatorsInfo()},
        {'label': 32038, 'action': 'calendar&url=mycalendar', 'icon': 'trakt.png', 'icon2': 'DefaultRecentlyAddedEpisodes.png', 'queue': True,
            'condition': lambda: trakt.getTraktIndicatorsInfo()},
        {'label': 32679, 'action': 'tvshows&url=trakthidden', 'icon': 'trakt.png', 'icon2': 'DefaultTVShows.png',
            'condition': lambda: trakt.getTraktIndicatorsInfo()},
        # Simkl personal lists
        # In Progress Shows / Next Episodes / In Progress Episodes (resume)
        # then watchlist / hold / history / dropped
        {'label': 32591, 'action': 'tvshows&url=simklwatching', 'icon': 'simkl.png', 'icon2': 'DefaultTVShows.png',
            'condition': lambda: tracker.simkl_live()},
        {'label': 32592, 'action': 'progress_next_episodes', 'icon': 'simkl.png', 'icon2': 'DefaultRecentlyAddedEpisodes.png', 'queue': True,
            'condition': lambda: tracker.simkl_live()},
        {'label': 32593, 'action': 'progress_in_progress_episodes', 'icon': 'simkl.png', 'icon2': 'DefaultRecentlyAddedEpisodes.png', 'queue': True,
            'condition': lambda: tracker.simkl_live()},
        {'label': 32033, 'action': 'tvshows&url=simklwatchlist', 'icon': 'simkl.png', 'icon2': 'DefaultTVShows.png',
            'condition': lambda: tracker.simkl_live()},
        {'label': 90312, 'action': 'tvshows&url=simklhold', 'icon': 'simkl.png', 'icon2': 'DefaultTVShows.png',
            'condition': lambda: tracker.simkl_live()},
        {'label': 32036, 'action': 'tvshows&url=simklhistory', 'icon': 'simkl.png', 'icon2': 'DefaultTVShows.png',
            'condition': lambda: tracker.simkl_live()},
        {'label': 90313, 'action': 'tvshows&url=simkldropped', 'icon': 'simkl.png', 'icon2': 'DefaultTVShows.png',
            'condition': lambda: tracker.simkl_live()},
        {'label': 90263, 'action': 'tvshows&url=simklratings', 'icon': 'simkl.png', 'icon2': 'DefaultTVShows.png',
            'condition': lambda: tracker.simkl_live()},
        # Trakt custom lists only
        {'label': 32040, 'action': 'tvUserlists', 'icon': 'userlists.png', 'icon2': 'DefaultTVShows.png',
            'condition': lambda: trakt.get_trakt_credentials_info()},
        # Episode userlists (Trakt only)
        {'label': 32041, 'action': 'episodeUserlists', 'icon': 'userlists.png', 'icon2': 'DefaultTVShows.png',
            'condition': lambda: trakt.get_trakt_credentials_info()},
        # lite=False items (shown when not in lite mode)
        {'label': 32031, 'action': 'tvliteNavigator', 'icon': 'tvshows.png', 'icon2': 'DefaultTVShows.png',
            'condition': lambda self: not self.lite_mode},
        {'label': 32028, 'action': 'tvPerson', 'icon': 'people-search2.png', 'icon2': 'DefaultTVShows.png',
            'condition': lambda self: not self.lite_mode},
        {'label': 32010, 'action': 'tvSearch', 'icon': 'search.png', 'icon2': 'DefaultTVShows.png',
            'condition': lambda self: not self.lite_mode}
    ],
    'library': [
        {'label': 32557, 'action': 'openSettings&query=11.0', 'icon': 'tools.png', 'icon2': 'DefaultAddonProgram.png', 'is_folder': False},
        {'label': f'[B]{color("MIDNIGHT", 'header')}[/B] : {color("Setup Library Sources", 'header')}', 'action': 'setupLibrarySources', 'icon': 'library_update.png', 'icon2': 'DefaultAddonProgram.png', 'is_folder': False},
        {'label': 32558, 'action': 'updateLibrary&query=tool', 'icon': 'library_update.png', 'icon2': 'DefaultAddonProgram.png', 'is_folder': False},
        # Filesystem folder links (using special:// paths from settings)
        {'label': 32559, 'action': lambda self: c.get_setting('library.movie'), 'icon': 'movies.png', 'icon2': 'DefaultMovies.png', 'is_action': False, 'is_folder': True},
        {'label': 32560, 'action': lambda self: c.get_setting('library.tv'), 'icon': 'tvshows.png', 'icon2': 'DefaultTVShows.png', 'is_action': False, 'is_folder': True},
        # Trakt library integration (conditional)
        {'label': 32561, 'action': 'moviesToLibrary&url=traktcollection', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png', 'is_folder': False,
         'condition': lambda: trakt.get_trakt_credentials_info()},
        {'label': 32562, 'action': 'moviesToLibrary&url=traktwatchlist', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png', 'is_folder': False,
         'condition': lambda: trakt.get_trakt_credentials_info()},
        {'label': 32563, 'action': 'tvshowsToLibrary&url=traktcollection', 'icon': 'trakt.png', 'icon2': 'DefaultTVShows.png', 'is_folder': False,
         'condition': lambda: trakt.get_trakt_credentials_info()},
        {'label': 32564, 'action': 'tvshowsToLibrary&url=traktwatchlist', 'icon': 'trakt.png', 'icon2': 'DefaultTVShows.png', 'is_folder': False,
         'condition': lambda: trakt.get_trakt_credentials_info()}
    ],
    'kidsgrey': [
        {'label': f'{color("¤", 'header')} [B]{color("Debrid Kids", 'header')}[/B] {color("¤", 'header')}', 'action': 'debridkids', 'icon': 'debrid_kids.png', 'icon2': 'DefaultMovies.png'},
        {'label': f'{color("¤", 'header')} [B]{color("Kids Trending", 'header')}[/B] {color("¤", 'header')}', 'action': 'movies&url=advancedsearchtrending', 'icon': 'kids_trending.png', 'icon2': 'DefaultMovies.png'},
        {'label': f'{color("¤", 'header')} [B]{color("Action Hero", 'header')}[/B] {color("¤", 'header')}', 'action': 'movies&url=collectionsactionhero', 'icon': 'action_hero.png', 'icon2': 'DefaultMovies.png'},
        {'label': f'{color("¤", 'header')} [B]{color("DC vs Marvel", 'header')}[/B] {color("¤", 'header')}', 'action': 'movies&url=advancedsearchdcvsmarvel', 'icon': 'dc_marvel.png', 'icon2': 'DefaultMovies.png'},
        {'label': f'{color("¤", 'header')} [B]{color("Walt Disney", 'header')}[/B] {color("¤", 'header')}', 'action': 'waltdisney', 'icon': 'walt_disney.png', 'icon2': 'DefaultMovies.png'},
        {'label': f'{color("¤", 'header')} [B]{color("Learning TV", 'header')}[/B] {color("¤", 'header')}', 'action': 'learning', 'icon': 'learning_tv.png', 'icon2': 'DefaultMovies.png'},
        {'label': f'{color("¤", 'header')} [B]{color("Kids Songs", 'header')}[/B] {color("¤", 'header')}', 'action': 'songs', 'icon': 'kids_songs.png', 'icon2': 'DefaultMovies.png'}
    ],
    'traktlist': [
        {'label': 90041, 'action': 'movies&url=https://api.trakt.tv/users/giladg/lists/latest-releases/items?', 'icon': 'fhd_releases.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90042, 'action': 'movies&url=https://api.trakt.tv/users/giladg/lists/latest-4k-releases/items?', 'icon': '4k_releases.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90043, 'action': 'movies&url=boxoffice', 'icon': 'top_10.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90044, 'action': 'movies&url=cinematography_oscars', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90045, 'action': 'movies&url=standup_comedy', 'icon': 'standup.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90052, 'action': 'movies&url=https://trakt.tv/users/29zombies/lists/halloween/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90053, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/action/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90054, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/adventure/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90055, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/animation/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90056, 'action': 'movies&url=https://api.trakt.tv/users/hdlists/lists/comedy-movies-top-rated-from-1960-to-today/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90057, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/crime/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90058, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/drama/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90059, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/family/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 32036, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/history/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90061, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/horror/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90062, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/music/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90063, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/mystery/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90064, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/romance/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90065, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/science-fiction/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90066, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/thriller/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90067, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/war/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90068, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/western/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90069, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/marvel/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90070, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/walt-disney-animated-feature-films/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90071, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/batman/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90072, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/superman/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90073, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/star-wars/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90074, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/007/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90075, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/pixar-animation-studios/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90076, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/quentin-tarantino-collection/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90077, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/rocky/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90078, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/dreamworks-animation/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90079, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/dc-comics/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90080, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/the-30-best-romantic-comedies-of-all-time/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90081, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/88th-academy-awards-winners/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90082, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/most-sexy-movies/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90083, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/dance-movies/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'},
        {'label': 90084, 'action': 'movies&url=https://api.trakt.tv/users/movistapp/lists/halloween-movies/items?', 'icon': 'trakt.png', 'icon2': 'DefaultMovies.png'}
    ],
    'developers': [
        {'label': f'[B]{color("Run Progress Test", 'header')}[/B]', 'action': 'progresstest', 'icon': 'main_classy.png', 'icon2': 'main_classy.png'},
        {'label': f'[B]{color("Test Sources Architecture (Episode)", 'header')}[/B]', 'action': 'testSourcesArchEpisode', 'icon': 'main_classy.png', 'icon2': 'main_classy.png'},
        {'label': f'[B]{color("Test Sources Architecture (Movie)", 'header')}[/B]', 'action': 'testSourcesArchMovie', 'icon': 'main_classy.png', 'icon2': 'main_classy.png'},
        {'label': f'{color("[B]Keep[/B]", 'header')}', 'action': 'devKeep', 'icon': 'main_orangehat.png', 'icon2': 'main_orangehat.png'},
        {'label': f'{color("[B]Purplehat[/B]", 'header')}', 'action': 'devPurplehat', 'icon': 'main_purplehat.png', 'icon2': 'main_purplehat.png'},
        {'label': f'{color("[B]DevTools[/B]", 'header')}', 'action': 'devTools', 'icon': 'devtools.png', 'icon2': 'devtools.png'}
    ],
    # TMDB Documentary hub (genre 99) — replaces topdocumentaryfilms / docu.py
    'documentaries': [
        {'label': 'Movie Documentaries', 'action': 'movies&url=tmdb_documentary', 'icon': 'documentaries.png', 'icon2': 'DefaultMovies.png'},
        {'label': 'TV Documentaries', 'action': 'tvshows&url=tmdb_documentary', 'icon': 'documentaries.png', 'icon2': 'DefaultTVShows.png'},
    ],
    'devKeep': [
        {'label': 'Documentaries', 'action': 'docuNavigator', 'icon': 'main_classy.png', 'icon2': 'main_classy.png'},
        {'label': 'Get QR Code', 'action': 'get_qrcode', 'icon': 'main_orangehat.png', 'icon2': 'main_classy.png'},
        {'label': 'Run Trakt Sync setup', 'action': 'traktSyncsetup', 'icon': 'main_orangehat.png', 'icon2': 'main_orangehat.png'},
        {'label': 'Check sync Tables', 'action': 'traktchecksync', 'icon': 'main_classy.png', 'icon2': 'main_classy.png'},
        {'label': 'Get Collections', 'action': 'traktgetcollections', 'icon': 'main_classy.png', 'icon2': 'main_classy.png'},
        {'label': 'Sync Progress', 'action': 'syncTrakt', 'icon': 'main_classy.png', 'icon2': 'main_classy.png'},
        {'label': 32624, 'action': 'movieProgress', 'icon': 'main_classy.png', 'icon2': 'main_classy.png'}
    ],
    'devPurplehat': [
        {'label': 'Trending Sci-Fi', 'action': 'movies&url=https://trakt.tv/movies/trending?genres=science-fiction/', 'icon': 'main_purplehat.png', 'icon2': 'main_purplehat.png'},
        {'label': 'Best of Sci-Fi', 'action': 'movies&url=https://trakt.tv/lists/31916837/items', 'icon': 'main_purplehat.png', 'icon2': 'main_purplehat.png'},
        {'label': 'Alien Invasion', 'action': 'movies&url=https://trakt.tv/lists/10178267/items', 'icon': 'main_purplehat.png', 'icon2': 'main_purplehat.png'},
        {'label': 'Futuristic / Post Apocalyptic', 'action': 'movies&url=https://trakt.tv/lists/24148690/items', 'icon': 'main_purplehat.png', 'icon2': 'main_purplehat.png'},
        {'label': 'Star Wars Film Collection', 'action': 'movies&url=https://trakt.tv/lists/24149935/items', 'icon': 'main_purplehat.png', 'icon2': 'main_purplehat.png'},
        {'label': 'Star Trek Film Collection', 'action': 'movies&url=https://trakt.tv/lists/19761669/items', 'icon': 'main_purplehat.png', 'icon2': 'main_purplehat.png'},
        {'label': 'Marvel Universe', 'action': 'movies&url=https://trakt.tv/lists/9554261/items', 'icon': 'main_purplehat.png', 'icon2': 'main_purplehat.png'},
        {'label': 'DC Universe', 'action': 'movies&url=https://trakt.tv/lists/9554826/items', 'icon': 'main_purplehat.png', 'icon2': 'main_purplehat.png'}
    ],
    'devTools': [
        {'label': f'{color("Essential Boxsets", 'header')}', 'action': 'collectionBoxset', 'icon': 'main_classy.png', 'icon2': 'DefaultMovies.png'},
        {'label': 'Update Services', 'action': 'update_service', 'icon': 'main_classy.png', 'icon2': 'main_classy.png'},
        {'label': f'{color("Encode API Key", 'header')}', 'action': 'encodeApiKey', 'icon': 'devtools.png', 'icon2': 'devtools.png', 'is_folder': False},
        {'label': f'{color("Test API Classes", 'header')}', 'action': 'testAPIClasses', 'icon': 'devtools.png', 'icon2': 'devtools.png'}
    ]
}


def _should_show(item, navigator_instance=None):
    """
    Determine if menu item should be displayed based on setting or condition.
    Part of dictionary-driven menu system - replaces scattered if/setting checks.

    Args:
        item (dict): Menu item definition with optional 'setting' or 'condition' keys
        navigator_instance (Navigator): Required for lambdas that need self (e.g., self.get_menu_enabled)

    Returns:
        bool: True if item should be shown, False otherwise
    """
    # Check setting-based visibility (e.g., 'navi.movies': 'false' hides item)
    if 'setting' in item:
        if c.get_setting(item['setting']) == 'false':
            return False

    # Check custom condition (lambda function for complex logic)
    if 'condition' in item:
        try:
            import inspect
            condition_func = item['condition']
            sig = inspect.signature(condition_func)

            # If lambda expects argument (self), pass navigator_instance
            if len(sig.parameters) > 0:
                if not condition_func(navigator_instance):
                    return False
            else:
                if not condition_func():
                    return False
        except Exception:
            # If condition fails, hide item (defensive)
            return False

    return True


def _load_menu(menu_key):
    """
    Load menu definition from MENUS dictionary.
    Part of dictionary-driven menu system - single source of truth.

    Args:
        menu_key (str): Key name from MENUS dict ('tools', 'movies', etc.)

    Returns:
        list: Menu item definitions, or empty list if not found
    """
    return MENUS.get(menu_key, [])


def _filter_items(items, navigator_instance=None):
    """
    Filter menu items based on settings and conditions.
    Part of dictionary-driven menu system - applies visibility rules.

    Args:
        items (list): Raw menu item definitions
        navigator_instance (Navigator): Required for lambdas that need self

    Returns:
        list: Filtered items that should be displayed
    """
    return [item for item in items if _should_show(item, navigator_instance)]


def _render_menu(navigator_instance, menu_key):
    """
    Render menu by loading, filtering, and displaying items.
    Part of dictionary-driven menu system - replaces repetitive addDirectoryItem code.

    This function replaces the pattern of 5-10 lines per menu method across ~80 methods.
    See docs/POC_DICTIONARY_NAVIGATOR.py for full architecture details.

    Args:
        navigator_instance: Instance of Navigator class (for addDirectoryItem/endDirectory)
        menu_key (str): Menu to render ('tools', 'movies', etc.)
    """
    # Load menu definition (single source of truth)
    items = _load_menu(menu_key)

    # Filter based on settings/conditions (pass navigator_instance for self-lambdas)
    visible_items = _filter_items(items, navigator_instance)

    # Render each visible item
    for item in visible_items:
        # Handle lambda actions (e.g., dynamic paths from settings)
        action = item['action']
        if callable(action):
            try:
                import inspect
                sig = inspect.signature(action)
                # If lambda expects self parameter, pass navigator_instance
                action = action(navigator_instance) if len(sig.parameters) > 0 else action()
            except Exception:
                continue  # Skip items with failed lambda evaluation

        navigator_instance.addDirectoryItem(
            item['label'],
            action,
            item['icon'],
            item.get('icon2', ''),  # Default to empty if not specified
            queue=item.get('queue', False),
            is_folder=item.get('is_folder', True),  # Default to folder
            context=item.get('context'),
            is_action=item.get('is_action', True)  # Support direct paths (is_action=False)
        )

    # Finalize directory listing
    navigator_instance.endDirectory()


class Navigator:
    def root(self) -> None:
        """
        Main entry point menu - Converted to dictionary-driven approach.

        BEFORE (38 lines): 13 conditional addDirectoryItem calls + endDirectory
        AFTER (24 lines): _load_menu + custom rendering with Adult section injection

        Hybrid approach: Most items in MENUS['root'], Adult section handled separately
        due to dynamic label generation from AdultPIN.get_section_name().

        Adult section appears after purplehat, before plist (or before toolNavigator if plist is hidden).
        """
        # Load and filter menu items
        items = _load_menu('root')
        visible_items = _filter_items(items, self)

        # Track if Adult section has been inserted
        adult_inserted = False

        # Render all items, inserting Adult section at correct position
        for item in visible_items:
            # Insert Adult section before plist or toolNavigator (whichever comes first)
            if ADULT and not adult_inserted and item['action'] in ('plist', 'toolNavigator'):
                section_name = adult_pin.AdultPIN.get_section_name()
                formatted_name = f'{color("¤", 'header')} [B]{color(section_name, 'header')}[/B]'
                self.addDirectoryItem(formatted_name, 'porn_check_pin', 'main_pinkhat.png', 'DefaultMovies.png', is_folder=True)
                adult_inserted = True

            # Render the regular item
            self.addDirectoryItem(
                item['label'],
                item['action'],
                item['icon'],
                item.get('icon2', ''),
                queue=item.get('queue', False),
                is_folder=item.get('is_folder', True),
                context=item.get('context')
            )

        self.endDirectory()


    def porn_check_pin(self):
        """Adult section with optional PIN protection"""
        routed = False
        try:
            control.idle()
            c.log('[Adult PIN] porn_check_pin called - checking access')
            if adult_pin.AdultPIN.check_access():
                c.log('[Adult PIN] Access granted - routing to root_porn')
                from ..indexers.lists import Indexer
                Indexer().root_porn()
                routed = True
            else:
                c.log('[Adult PIN] Access denied')
        finally:
            if not routed:
                control.finish_dialog_route()
                control.schedule_busy_dismiss((0.5, 2.0))


    def movies(self, lite=False):
        """
        Movies menu - Converted to dictionary-driven approach.

        BEFORE (62 lines): 27 addDirectoryItem calls with scattered if/setting checks + endDirectory
        AFTER (7 lines): Single _render_menu() call

        Features:
        - Christmas menu visible in December or for devs (lambda condition)
        - All other items controlled by settings (setting: 'navi.*')
        - See MENUS['movies'] definition at top of file for menu structure

        """
        _render_menu(self, 'movies')

    def mymovies(self, lite=False):
        """
        My Movies menu - Converted to dictionary-driven approach.

        Personal movie lists from Trakt: Collection, Watchlist, Progress, etc.
        Includes context menus for "Add to Library" on Collection/Watchlist items.

        BEFORE (42 lines): Multiple if/setting checks with addDirectoryItem calls
        AFTER (5 lines): account_check() + lite_mode tracking + _render_menu()

        See MENUS['mymovies'] definition for menu structure with context/queue support.
        """
        if not self.account_check():
            return
        self.lite_mode = lite
        _render_menu(self, 'mymovies')

    def tvshows(self, lite=False):
        """
        TV Shows menu - Converted to dictionary-driven approach.

        BEFORE (48 lines): 25 addDirectoryItem calls + endDirectory
        AFTER (7 lines): Single _render_menu() call

        See MENUS['tvshows'] definition for menu structure.
        """
        _render_menu(self, 'tvshows')

    def documentaries(self):
        """TMDB Documentary hub (genre 99) — Movie Documentaries + TV Documentaries."""
        _render_menu(self, 'documentaries')

    def mytvshows(self, lite=False):
        """
        My TV Shows menu - Converted to dictionary-driven approach.

        Personal TV show lists from Trakt: Collection, Watchlist, Progress, etc.
        Includes context menus for "Add to Library" on Collection/Watchlist items.

        BEFORE (50 lines): Multiple if/setting checks with addDirectoryItem calls
        AFTER (8 lines): try/except wrapper + account_check() + lite_mode tracking + _render_menu()

        See MENUS['mytvshows'] definition for menu structure with context/queue support.
        """
        try:
            if not self.account_check():
                return
            self.lite_mode = lite
            _render_menu(self, 'mytvshows')
        except Exception as e:
            c.log(f'[Navigator] mytvshows error: {e}')
            self.endDirectory(cacheToDisc=False)

    def tools(self):
        """
        Tools menu - POC converted to dictionary-driven approach.

        BEFORE (13 lines): 12 addDirectoryItem calls + endDirectory
        AFTER (1 line): Single _render_menu() call

        See MENUS['tools'] definition at top of file for menu structure.
        """
        _render_menu(self, 'tools')

    def uploadLogs(self):
        """Upload Logs submenu - Anonymous log uploads."""
        _render_menu(self, 'uploadLogs')

    def debridManagement(self):
        """Debrid management main menu - Converted to dictionary-driven approach."""
        _render_menu(self, 'debridManagement')

    def debridCloud(self):
        """Debrid cloud storage browser menu - Converted to dictionary-driven approach."""
        _render_menu(self, 'debridCloud')

    def cachingTools(self):
        """Caching Tools menu - Converted to dictionary-driven approach."""
        _render_menu(self, 'cachingTools')

    def library(self):
        """
        Library management menu - Converted to dictionary-driven approach.
        Includes setup, update, folder links, and Trakt integration.
        Uses lambda actions for dynamic paths from settings.
        """
        _render_menu(self, 'library')

    def downloads(self):
        movie_downloads = c.get_setting('movie.download.path')
        tv_downloads = c.get_setting('tv.download.path')

        if len(control.listDir(movie_downloads)[0]) > 0:
            self.addDirectoryItem(32001, movie_downloads, 'movies.png', 'DefaultMovies.png', is_action=False)
        if len(control.listDir(tv_downloads)[0]) > 0:
            self.addDirectoryItem(32002, tv_downloads, 'tvshows.png', 'DefaultTVShows.png', is_action=False)

        self.endDirectory()

    def search(self):
        """Search menu - Converted to dictionary-driven approach."""
        _render_menu(self, 'search')

    def views(self):
        """Pick content type, then open a folder route to save the view (handle -1 on is_folder=False)."""
        try:
            control.idle()

            items = [
                (c.lang(32001), 'movies'),
                (c.lang(32002), 'tvshows'),
                (c.lang(32105), 'seasons'),
                (c.lang(32106), 'episodes')
            ]
            select = control.selectDialog([i[0] for i in items], c.lang(32049))
            if select == -1:
                return

            content = items[select][1]
            plugin_url = f'{sys.argv[0]}?action=viewsSaveMenu&content={content}'
            c.log(f'[Navigator] views -> viewsSaveMenu content={content}', 1)
            control.execute(f'ActivateWindow(Videos,{plugin_url},return)')
        except Exception as e:
            c.log(f'[Navigator] views error: {e}')
            c.log(traceback.format_exc())
        finally:
            control.finish_dialog_route()
            control.schedule_busy_dismiss((0.5, 2.0))

    def views_save_menu(self, content):
        """Folder route: one row to persist the skin view mode for *content*."""
        try:
            if not content:
                c.log('[Navigator] views_save_menu: missing content param', 1)
                self.endDirectory(cacheToDisc=False)
                return

            title = c.lang(32059) or 'CLICK HERE TO SAVE VIEW'
            url = f'{sys.argv[0]}?action=addView&content={content}'

            poster, banner, fanart = c.addon_poster(), c.addon_banner(), c.addon_fanart()
            item = control.item(label=title)

            info_tag = ListItemInfoTag(item, 'video')
            info_tag.set_info({'title': title})
            item.setArt({'icon': poster, 'thumb': poster, 'poster': poster, 'banner': banner})
            item.setProperty('fanart', fanart)

            handle = int(sys.argv[1])
            control.addItem(handle=handle, url=url, listitem=item, isFolder=False)
            control.content(handle, content)
            control.directory(handle, cacheToDisc=True)
            views.set_view(content, {})
        except Exception as e:
            c.log(f'[Navigator] views_save_menu error: {e}')
            c.log(traceback.format_exc())
            self.endDirectory(cacheToDisc=False)


    def get_menu_enabled(self, menu_item) -> bool:
        """Checks if a menu item is enabled based on the current month and DEVMODE status."""
        if _devmode():
            return True
        if menu_item == 'navi.holidays'and datetime.now().month == 12:
            return True
        return menu_item == 'navi.halloween' and datetime.now().month == 10


    def account_check(self) -> bool:
        """Allow My Movies / My TV when the active tracker (or IMDb) is authorized."""
        from ..modules import tracker
        from ..modules import simkl as simkl_mod

        has_imdb = c.get_setting('imdb.user') != ''
        # Home widgets: empty directory only — no toast every Home reload (#253).
        try:
            quiet = bool(c.is_widget_listing())
        except Exception:
            quiet = False
        if tracker.is_simkl():
            has_tracker = simkl_mod.get_simkl_credentials_info()
            if not has_tracker and not has_imdb:
                control.idle()
                if not quiet:
                    c.infoDialog('No Simkl account found.', sound=True, icon='WARNING')
                self.endDirectory(cacheToDisc=False)
                return False
            return True

        # Trakt active (XOR): existing check
        has_trakt = trakt.get_trakt_credentials_info()
        if not has_trakt and not has_imdb:
            control.idle()
            if not quiet:
                c.infoDialog(c.lang(32042), sound=True, icon='WARNING')
            self.endDirectory(cacheToDisc=False)
            return False
        return True

    def clearCache(self):
        try:
            control.idle()
            yes = c.yesnoDialog(c.lang(32084))
            if not yes:
                return
            cache.cache_clear()
            c.infoDialog(c.lang(32081), sound=True, icon='INFO')
        finally:
            control.finish_dialog_route()
            control.schedule_busy_dismiss((0.5, 2.0))

    def clearCacheMeta(self):
        try:
            control.idle()
            yes = c.yesnoDialog(c.lang(32082))
            if not yes:
                return
            cache.cache_clear_meta()
            c.infoDialog(c.lang(32083), sound=True, icon='INFO')
        finally:
            control.finish_dialog_route()
            control.schedule_busy_dismiss((0.5, 2.0))


    def clearCacheSearch(self):
        try:
            control.idle()
            yes = c.yesnoDialog(c.lang(32078))
            if not yes:
                return
            cache.cache_clear_search()
            c.infoDialog(c.lang(32079), sound=True, icon='INFO')
        finally:
            # Legacy: clear item was is_folder=True → navigates into this action,
            # finish_dialog_route paints an empty list stuck in the Back stack.
            try:
                folder = xbmc.getInfoLabel('Container.FolderPath') or ''
            except Exception:
                folder = ''
            if 'action=clearCacheSearch' in folder:
                control.close_directory(cacheToDisc=False, succeeded=False)
                control.execute('Action(Back)')
            else:
                control.finish_dialog_route()
                control.refresh()
            control.schedule_busy_dismiss((0.5, 2.0))

    def reset_settings_backups(self):
        """Prune stale settings.xml mirrors and commit live as the recovery copy."""
        try:
            control.idle()
            yes = c.yesnoDialog(
                'Remove old settings backups (health_backup_*, pre_simkl leftovers) '
                'and save your current settings as the only recovery copy?[CR][CR]'
                'Use this after authorizing debrid (Premiumize / RD / etc.) if a '
                'restart kept undoing your changes.',
                'MIDNIGHT : Reset settings Backups',
            )
            if not yes:
                return
            from ..modules.settings_repair import reset_settings_backups
            removed = reset_settings_backups(log=c.log)
            c.infoDialog(
                f'Settings backups reset ({removed} old file(s) removed).',
                sound=True,
                icon='INFO',
            )
        except Exception as e:
            c.log(f'[Navigator] reset_settings_backups error: {e}', 1)
            c.infoDialog('Could not reset settings backups.', sound=True, icon='WARNING')
        finally:
            control.finish_dialog_route()
            control.schedule_busy_dismiss((0.5, 2.0))

    def clearDebridCheck(self):
        try:
            control.idle()
            yes = c.yesnoDialog(c.lang(32078))
            if not yes:
                return
            cache.cache_clear_debrid()
            c.infoDialog(c.lang(32079), sound=True, icon='INFO')
        finally:
            control.finish_dialog_route()
            control.schedule_busy_dismiss((0.5, 2.0))

    def clearCacheAll(self):
        try:
            control.idle()
            yes = c.yesnoDialog(c.lang(32080))
            if not yes:
                return
            cache.cache_clear_all()
            c.infoDialog(c.lang(32081), sound=True, icon='INFO')
        finally:
            control.finish_dialog_route()
            control.schedule_busy_dismiss((0.5, 2.0))

    def scraperStatus(self):
        """Open the scraper status window."""
        control.begin_dialog_route()
        try:
            from resources.lib.windows.scraper_status_window import open_scraper_status
            open_scraper_status()
        except Exception as e:
            c.log(f'[Navigator] Error opening scraper status: {e}')
            c.infoDialog(f'Error: {str(e)}', sound=True, icon='ERROR')
        finally:
            control.finish_dialog_route()
            control.schedule_busy_dismiss((0.5, 2.0))


    def uploadKodiLog(self):
        """Upload Kodi log anonymously to dpaste.com"""
        try:
            control.idle()
            from resources.lib.modules.log_uploader import LogUploader
            LogUploader.upload_kodi_log()
        except Exception as e:
            c.log(f'[Navigator] Error uploading Kodi log: {e}', 1)
            import traceback
            c.log(f'[Navigator] Traceback: {traceback.format_exc()}', 1)
            c.infoDialog(f'Upload Error: {str(e)}', sound=True, icon='ERROR')
        finally:
            control.finish_dialog_route()
            control.schedule_busy_dismiss((0.5, 2.0))


    def uploadCrewLog(self):
        """Upload Midnight log anonymously to dpaste.com"""
        try:
            control.idle()
            from resources.lib.modules.log_uploader import LogUploader
            LogUploader.upload_crew_log()
        except Exception as e:
            c.log(f'[Navigator] Error uploading Midnight log: {e}', 1)
            import traceback
            c.log(f'[Navigator] Traceback: {traceback.format_exc()}', 1)
            c.infoDialog(f'Upload Error: {str(e)}', sound=True, icon='ERROR')
        finally:
            control.finish_dialog_route()
            control.schedule_busy_dismiss((0.5, 2.0))


    def bluehat(self):
        """Sports menu (Bluehat) - Converted to dictionary-driven approach."""
        _render_menu(self, 'bluehat')

    def tmdbmovieslist(self):
        """TMDb Movies Lists menu - Converted to dictionary-driven approach."""
        _render_menu(self, 'tmdbmovieslist')

    def tmdbtvlist(self):
        """TMDb TV Shows Lists menu - Converted to dictionary-driven approach."""
        _render_menu(self, 'tmdbtvlist')



    #######
    # cm - Devs only, don't run these if you don't know what you are doing! It can screw your setup up really bad!
    # cm - Please don't ask for help if you don't know what you are doing
    #######
    def developers(self):
        """
        Developers menu - Converted to dictionary-driven approach.
        Testing tools, submenu access (Keep, Purplehat, DevTools).
        """
        _render_menu(self, 'developers')

    def devKeep(self):
        """
        Dev Keep menu - Converted to dictionary-driven approach.
        Features to preserve for developers: Documentaries hub, QR Code, Trakt Sync tools.
        """
        _render_menu(self, 'devKeep')

    def devPurplehat(self):
        """
        Dev Purplehat menu - Converted to dictionary-driven approach.
        Purple hat sci-fi collections: Star Wars, Star Trek, Marvel, DC, etc.
        """
        _render_menu(self, 'devPurplehat')

    def devTools(self):
        """
        Dev Tools menu - Converted to dictionary-driven approach.
        Development utilities: Update services, API key encoding, API class testing.
        """
        _render_menu(self, 'devTools')

    #######
    # cm - Devs only, don't run these if you don't know what you are doing! It can screw your setup up really bad!
    # cm - Please don't ask for help if you don't know what you are doing!
    #######



    def orionoid(self):
        """Orionoid menu - Converted to dictionary-driven approach."""
        _render_menu(self, 'orionoid')

    def holidays(self):
        """Holidays (Christmas) menu - Converted to dictionary-driven approach."""
        _render_menu(self, 'holidays')

    def halloween(self):
        """Halloween menu - Converted to dictionary-driven approach."""
        _render_menu(self, 'halloween')

    def traktlist(self):
        """
        Trakt Lists menu - Converted to dictionary-driven approach.
        Curated Trakt lists: Latest releases, Top 10, genres, collections, etc.
        39 curated movie lists from various Trakt users.
        """
        _render_menu(self, 'traktlist')

    def collectionBoxset(self):
        """Essential curated catalogs — movies (TMDb collections), series, miniseries."""
        self.addDirectoryItem(
            f'{color("Essential Movies", 'header')}',
            'essentialCatalog&kind=movies',
            'movies.png',
            'DefaultMovies.png',
        )
        self.addDirectoryItem(
            f'{color("Essential Series", 'header')}',
            'essentialCatalog&kind=series',
            'tvshows.png',
            'DefaultTVShows.png',
        )
        self.addDirectoryItem(
            f'{color("Essential Miniseries", 'header')}',
            'essentialCatalog&kind=miniseries',
            'tvshows.png',
            'DefaultTVShows.png',
        )
        self.endDirectory()

    def collectionBoxsetKids(self):
        """Kids variant — same local essential catalogs for now."""
        self.collectionBoxset()

    def essentialCatalog(self, kind='movies'):
        """Render one essential JSON catalog from the Kodi profile folder."""
        from ..modules import essential_catalog as essential

        kind = (kind or 'movies').lower()
        entries = essential.load_kind(kind)
        fallback_icon = 'movies.png' if kind == 'movies' else 'tvshows.png'

        if not entries:
            c.log(f'[Essential] No entries loaded for kind={kind} — place essential_{kind}.json next to kodi.log or in userdata.')

        added = 0
        for entry in entries:
            try:
                prepared = essential.prepare_entry(entry, kind)
            except Exception as exc:
                c.log(f'[Essential] Failed to prepare {entry.get("title")}: {exc}')
                continue
            if not prepared:
                continue
            label, action, poster, fanart, plot = prepared
            thumb = poster if poster not in ('0', '', None) else os.path.join(art_path(), fallback_icon)
            item = control.item(label=label)
            if plot:
                info_tag = ListItemInfoTag(item, 'video')
                info_tag.set_info({'title': label, 'plot': plot})
            _fallback_fanart = addon_fanart()
            item.setArt({
                'icon': thumb,
                'thumb': thumb,
                'poster': thumb,
                'fanart': fanart if fanart not in ('0', '', None) else _fallback_fanart,
            })
            if _fallback_fanart:
                item.setProperty('fanart', _fallback_fanart)
            url = f'{sysaddon}?action={action}'
            is_folder = not action.startswith('tvSearchterm')
            control.addItem(handle=syshandle, url=url, listitem=item, isFolder=is_folder)
            added += 1

        c.log(f'[Essential] Rendered {added}/{len(entries)} items for kind={kind}')

        control.content(syshandle, 'movies' if kind == 'movies' else 'tvshows')
        control.directory(syshandle, cacheToDisc=True)


    def kidsgrey(self, lite=False):
        """
        Kids Grey Hat menu - Converted to dictionary-driven approach.
        Special kids content: Debrid Kids, Action Heroes, DC vs Marvel, etc.
        """
        _render_menu(self, 'kidsgrey')

    def kids(self, lite=False):
        """
        Kids menu - Converted to dictionary-driven approach.
        Family-friendly content (G, PG, TV-Y, TV-Y7, TV-G ratings).
        Now uses internationalized strings (90272-90278).
        """
        _render_menu(self, 'kids')


    def addDirectoryItem(self, name, query, thumb, icon, context=None, queue=False, is_action=True, is_folder=True):
        try:
            if isinstance(name, int) or (isinstance(name, str) and name.isdigit()):
                name = c.lang(int(name))
        except Exception:
            pass

        url = f'{sysaddon}?action={query}' if is_action else query
        thumb = os.path.join(art_path(), thumb) if art_path() else icon

        cm = []
        if queue:
            cm.append((queueMenu, f'RunPlugin({sysaddon}?action=queueItem)'))

        if context:
            if isinstance(context, list):
                context = context[0]

            if isinstance(context[0], str):
                cm.append((context[0], f'RunPlugin({sysaddon}?action={context[1]})'))
            else:
                cm.append((c.lang(context[0]), f'RunPlugin({sysaddon}?action={context[1]})'))



        item = control.item(label=name)
        item.addContextMenuItems(cm)
        item.setArt({'icon': thumb, 'thumb': thumb, 'fanart': addon_fanart()})

        fanart = addon_fanart()
        if fanart:
            item.setProperty('fanart', fanart)

        control.addItem(handle=syshandle, url=url, listitem=item, isFolder=is_folder)


    #cm-changed cacheToDisc v1.2.0 bool
    def endDirectory(self, cacheToDisc=True):
        control.content(syshandle, 'addons')
        control.directory(syshandle, cacheToDisc=cacheToDisc)

    def calendars_menu(self):
        """
        TV Calendar submenu - Converted to dictionary-driven approach.
        What's airing: Today, This Week, Next Week, My Calendar, Added.
        """
        _render_menu(self, 'calendars_menu')

    def calendarsAllNavigator(self):
        """
        Global TV schedule (Trakt /calendars/all/) - Today, This Week, This Month.
        Episodes from shows the user follows on Trakt are marked with a green ★.
        No Trakt auth required; markers are added silently when auth is available.
        """
        _render_menu(self, 'calendars_all_menu')

    def movies_calendars_menu(self):
        """
        Movie Calendar submenu - Converted to dictionary-driven approach.
        What's releasing: Theatrical & DVD/Blu-ray (This Week, This Month).
        Uses TMDb Discovery API. Now uses internationalized strings (90279-90282).
        """
        _render_menu(self, 'movies_calendars_menu')




navigator = Navigator()

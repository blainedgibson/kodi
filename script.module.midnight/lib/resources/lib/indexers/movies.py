# -*- coding: utf-8 -*-
'''
 ***********************************************************
 * Midnight Add-on
 *
 * @file movies.py
 * @package script.module.midnight
 *
 * @copyright (c) 2023, Midnight
 * @license GNU General Public License, version 3 (GPL-3.0)
 *
 * Updated on 2026-03-16
 *
 ********************************************************cm*
'''

import os
import sys
import re
import datetime
import traceback
import json

from urllib.parse import quote_plus, parse_qsl, urlparse, urlsplit, urlencode
import sqlite3 as database

from sqlite3 import OperationalError

import concurrent.futures
import threading

import requests

from ..modules import trakt
from ..modules import simkl
from ..modules import tracker
from ..modules import keys
from ..modules import bookmarks
from ..modules import fanart as fanart_tv
from ..modules import cleangenre
from ..modules import control
from ..modules import client
from ..modules import cache
from ..modules import metacache
from ..modules import playcount
from ..modules import workers
from ..modules import views
from ..modules import utils
from ..modules import http_client
from ..modules.listitem import ListItemInfoTag
from . import navigator
from ..modules.crewruntime import c
from resources.lib.ui_theme import color, role_for_name


parameters = dict(parse_qsl(sys.argv[2].replace('?', ''))) if len(sys.argv) > 1 else {}
action = parameters.get('action')

class Movies:
    """Movies indexer for Midnight addon."""
    def __init__(self):

        self.count = int(c.get_setting('page.item.limit'))
        self.list = self.on_deck_list=[]
        self.session = requests.Session()
        self.showunaired = c.get_setting('showunaired') or 'true'
        # Search / other snappy lists: skip Trakt cold-sync + per-row bookmark I/O
        self._light_listing = False

        # Pagination info from last API call (for showing "Page X of Y")
        self.pagination = {}

        # Cache TTL constants (in hours)
        self.CACHE_TTL_REALTIME = None       # No caching - always fresh (None = bypass cache)
        self.CACHE_TTL_SHORT = 0.25          # 15 minutes - frequently updated lists
        self.CACHE_TTL_HOUR = 1              # 1 hour - time-sensitive data (calendars)
        self.CACHE_TTL_SEARCH = 1            # 1 hour - search results
        self.CACHE_TTL_MEDIUM = 6            # 6 hours - moderately dynamic (trending, history)
        self.CACHE_TTL_DAY = 24              # 24 hours - daily updates (popular, collections)
        self.CACHE_TTL_2DAY = 48             # 48 hours - weekly updates (boxoffice, recommendations)
        self.CACHE_TTL_MONTH = 720           # 30 days - stable data (user collections, static lists)

        self.imdb_link:str = 'https://www.imdb.com'
        self.trakt_link: str = 'https://api.trakt.tv'
        self.tmdb_link:str = 'https://api.themoviedb.org/3/'

        #####
        # dates
        self.datetime = datetime.datetime.now()
        self.systime = (self.datetime).strftime('%Y%m%d%H%M%S%f')
        self.year_date = (self.datetime - datetime.timedelta(days=365)).strftime('%Y-%m-%d')
        self.month_date = (self.datetime - datetime.timedelta(days=30)).strftime('%Y-%m-%d')
        self.today_date = (self.datetime).strftime('%Y-%m-%d')
        self.year = self.datetime.strftime('%Y')
        self.country = c.get_setting('official.country') or 'US'
        # ~15y floor for Documentary (genre 99) — matches Classy tmdb_discover
        self.years_15_ago = (self.datetime - datetime.timedelta(days=int(15 * 365.25))).strftime('%Y-%m-%d')
        # Future dates for calendar endpoints (added 2026-03-10)
        self.week_ahead_date = (self.datetime + datetime.timedelta(days=7)).strftime('%Y-%m-%d')
        self.month_ahead_date = (self.datetime + datetime.timedelta(days=30)).strftime('%Y-%m-%d')

        #####
        # users
        self.trakt_user = c.get_setting('trakt.user').strip()
        self.imdb_user = c.get_setting('imdb.user').replace('ur', '')
        self.fanart_tv_user = c.get_setting('fanart.tv.user')

        self.tmdb_user = c.get_setting('tm.personal_user') or c.get_setting('tm.user')
        if not self.tmdb_user:
            self.tmdb_user = keys.tmdb_key

        self.user = self.tmdb_user

        #####
        # Settings
        self.lang = control.apiLanguage()['trakt']

        #####cm#
        # define links
        self.search_link = f'{self.trakt_link}/search/movie?limit=20&page=1&query='
        self.fanart_tv_art_link = 'https://webservice.fanart.tv/v3/movies/%s'
        self.fanart_tv_level_link = 'https://webservice.fanart.tv/v3/level'
        self.tmdb_img_link = 'https://image.tmdb.org/t/p/%s%s'
        self.tm_art_link = (f'{self.tmdb_link}movie/%s/images?api_key={self.tmdb_user}&language=en-US&include_image_language=en{self.lang},null')
        self.tmdb_external_ids_by_tmdb = (f'{self.tmdb_link}movie/%s/external_ids?api_key={self.tmdb_user}&language=en-US')

        ######
        # imdb
        self.keyword_link = f'https://www.imdb.com/search/title?title_type=feature,tv_movie,documentary&num_votes=100,&keywords=%s&sort=moviemeter,asc&count={self.count}&start=1'
        # Deprecated IMDb scraping link (no longer works - IMDb changed their URL structure)
        # self.oscarsnominees_link = f'https://www.imdb.com/search/title?title_type=feature,tv_movie&production_status=released&groups=oscar_best_picture_nominees&sort=year,desc&count={self.count}&start=1'
        self.certification_link = f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}&certification=%s&certification_country=US&language=en-US&with_original_language=en&sort_by=primary_release_date.desc&page=1'

        ######
        # tmdb
        self.person_link = (f'{self.tmdb_link}search/person?api_key={self.tmdb_user}&query=%s&include_adult=false&language=en-US&page=1')
        self.person_search_link = (f'{self.tmdb_link}person/%s?api_key={self.tmdb_user}&?language=en-US')
        self.persons_link = f'{self.tmdb_link}person/popular?api_key={self.tmdb_user}&language=en-US&page=1'
        self.personlist_link = (f'{self.tmdb_link}trending/person/day?api_key={self.tmdb_user}&language=en-US')
        self.personmovies_link = (f'{self.tmdb_link}person/%s/movie_credits?api_key={self.tmdb_user}&language=en-US')

        # TMDB official list 28 - Best Picture Winners - The Academy Awards (98 items)
        # NOTE: List 7105823 was incorrectly identified as Oscar winners but is actually an anime list
        self.oscars_link = (f'{self.tmdb_link}list/28?api_key={self.tmdb_user}&language=en-US&page=1')
        # Trakt curated list: Academy Award Winners for Best Cinematography (giladg, 120 items)
        # NOTE: keyword 31569 was incorrect (belonged to a TV series) — restored to original list
        self.cinematography_oscars_link = (
            'https://api.trakt.tv/users/giladg/lists/academy-award-winners-for-best-cinematography/items?'
        )
        self.standup_comedy_link = (
            f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}'
            f'&with_genres=35&sort_by=popularity.desc&language=en-US&page=1'
        )
        # Incorrectly used anime list (DO NOT USE):
        # self.oscars_link = (f'{self.tmdb_link}list/7105823?api_key={self.tmdb_user}&language=en-US&page=1')
        # Trakt list (may require authentication):
        # self.oscars_link = f'{self.trakt_link}/users/maxwelldeux/lists/academy-awards-best-picture-winners-1927-present/items'
        # TMDB keyword approach (experimental):
        # self.oscars_link = (f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}&with_keywords=158572&sort_by=primary_release_date.desc&language=en-US&page=1')

        self.xristmas_link = (f'{self.tmdb_link}list/8280352?api_key={self.tmdb_user}&language=en-US&page=1' )
        self.theaters_link = (f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}&now_playing?language=en-US&page=1&region=US|UK&sort_by=popularity.desc')
        # TMDB year link — same discover + English/country filters as genres
        # (Trakt /movies/trending?years= was sparse/empty for users without live Trakt)
        self.year_link = (
            f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}'
            f'&include_adult=false&include_video=false&language=en-US'
            f'&sort_by=popularity.desc&with_original_language=en'
            f'&with_origin_country=US|GB|CA|AU|NZ'
            f'&primary_release_year=%s&page=1'
        )
        # Old Trakt year link (tiny trending pool; needs auth)
        # self.year_link = (f'{self.trakt_link}/movies/trending?years=%s&limit={self.count}&page=1')
        # Older TMDB year link without origin-country filter (too many foreign titles)
        # self.year_link = (f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}&include_adult=false&include_video=false&language=en-US&region=US&sort_by=release_date.desc&year=%s&page=1')
        self.language_link = (f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}&include_adult=false&include_video=false&sort_by=popularity.desc&with_original_language=%s&page=1')
        self.featured_link = (f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}&include_adult=false&include_video=false&language=en-US&page=1&sort_by=popularity.desc&with_release_type=1|2|3&release_date.gte=date[60]&release_date.lte=date[0]')
        self.popular_link = (f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}&include_adult=false&include_video=false&language=en-US&page=1&sort_by=popularity.desc&with_release_type=1|2|3&release_date.gte=date[60]&release_date.lte=date[0]')
        self.views_link = (f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}&include_adult=false&include_video=false&language=en-US&page=1&sort_by=popularity.desc&vote_average.gte=8&vote_average.lte=10&with_original_language=en&release_date.lte={self.today_date}')
        # TMDB genre link - same discover endpoint as TV shows, filtered to English-language/English-speaking-country films
        self.genre_link = (f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}&include_adult=false&include_video=false&language=en-US&sort_by=popularity.desc&with_original_language=en&with_origin_country=US|GB|CA|AU|NZ&with_genres=%s&page=1')
        # Documentary (99): 15y + vote floor — same filters as Classy Genres/Documentaries hub
        self.tmdb_documentary_link = (
            f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}'
            f'&include_adult=false&include_video=false&language=en-US'
            f'&sort_by=popularity.desc&with_original_language=en'
            f'&with_origin_country=US|GB|CA|AU|NZ&with_genres=99'
            f'&primary_release_date.gte={self.years_15_ago}'
            f'&vote_count.gte=20&page=1'
        )
        # Trakt genre link (old, only ~2 pages since Trakt trending pool is tiny)
        # self.genre_link = (f'{self.trakt_link}/movies/trending?genres=%s&limit={self.count}&page=1')
        # Old TMDB genre link with different date parameter format - kept for reference
        # self.genre_link = ('{}{}/{}?api_key={}&include_adult=false&include_video=false&language=en-US&sort_by=primary_release_date.desc&with_original_language=en&primary_release_date.lte={}&with_genres={}&page=1').format(self.tmdb_link, 'discover', 'movie', self.tmdb_user, self.today_date, '%s')


        # TMDB lookup and art endpoints
        self.tmdb_by_imdb = f'{self.tmdb_link}find/%s?api_key={self.tmdb_user}&external_source=imdb_id'
        self.tmdb_providers_link = f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}&sort_by=popularity.desc&with_watch_providers=%s&watch_region=%s&page=1'
        self.tmdb_art_link = f'{self.tmdb_link}movie/%s/images?api_key={self.tmdb_user}&language=en-US&include_image_language=en,%s,null'
        self.related_link = f'{self.tmdb_link}movie/%s/similar?api_key={self.tmdb_user}&language=en-US&page=1'

        # TMDB API and search (overwrites Trakt search_link from line 101)
        self.tmdb_search_link = f'{self.tmdb_link}search/movie?api_key={self.tmdb_user}&language=en-US&query=%s&page=1'
        self.search_link = self.tmdb_search_link  # Alias for compatibility
        self.tmdb_api_link = f'{self.tmdb_link}movie/%s?api_key={self.tmdb_user}&language={self.lang}&append_to_response=credits,release_dates,external_ids'
        self.tmdb_info_tvshow_link = f'{self.tmdb_link}movie/%s?api_key={self.tmdb_user}&language={self.lang}&append_to_response=images'
        self.tmdb_networks_link = f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}&with_networks=%s&language=en-US&release_date.lte={self.today_date}&sort_by=primary_release_date.desc&page=1'
        self.tmdb_networks_no_unaired_link = f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}&release_date.lte={self.today_date}&sort_by=primary_release_date.desc&with_watch_providers=%s&watch_region={self.country}&page=1'

        self.tmdb_movie_top_rated_link = (f'{self.tmdb_link}movie/top_rated?api_key={self.tmdb_user}&language={self.lang}&sort_by=popularity.desc&page=1')
        self.tmdb_movie_popular_link = (f'{self.tmdb_link}movie/popular?api_key={self.tmdb_user}&language={self.lang}&page=1')
        self.tmdb_movie_trending_day_link = (f'{self.tmdb_link}trending/movie/day?api_key={self.tmdb_user}')
        self.tmdb_movie_trending_week_link = (f'{self.tmdb_link}trending/movie/week?api_key={self.tmdb_user}')
        self.tmdb_movie_discover_year_link = (f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}&language=%s&sort_by=popularity.desc&primary_release_year={self.year}&with_original_language=en&page=1')
        # New TMDB discovery endpoints (added 2026-02-24)
        self.tmdb_movie_now_playing_link = (f'{self.tmdb_link}movie/now_playing?api_key={self.tmdb_user}&language={self.lang}&region=US&page=1')
        self.tmdb_movie_upcoming_link = (f'{self.tmdb_link}movie/upcoming?api_key={self.tmdb_user}&language={self.lang}&region=US&page=1')
        # JustWatch-equivalent endpoints (added 2026-02-24) - Release types: 4=Digital, 5=Physical, 6=TV, 1=Premiere, 2=Theatrical Limited, 3=Theatrical
        self.tmdb_movie_latest_releases_link = (f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}&language={self.lang}&region=US&vote_count.gte=10&sort_by=release_date.desc&release_date.gte=date[31]&release_date.lte={self.today_date}&with_release_type=4|5|6&page=1')
        self.tmdb_movie_premieres_link = (f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}&language={self.lang}&region=US&vote_count.gte=10&sort_by=release_date.desc&release_date.gte=date[31]&release_date.lte={self.today_date}&with_release_type=1|3|2&page=1')


        self.halloween_link = f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}&with_genres=27&language=en-US&sort_by=popularity.desc&page=1'
        self.halloween_fun_link = f'{self.trakt_link}/users/istoit/lists/halloween-fun-frights?sort=rank,asc/items'

        # Kids Movies (added 2026-03-02) - Family-friendly content with certifications G, PG
        # Genre 16 = Animation, Genre 10751 = Family
        self.kids_movies_animation_link = (f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}&language={self.lang}&'
                                        f'certification_country=US&certification=G|PG&'
                                        f'sort_by=popularity.desc&with_genres=16&'
                                        f'vote_count.gte=50&page=1')
        self.kids_movies_family_link = (f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}&language={self.lang}&'
                                        f'certification_country=US&certification=G|PG&'
                                        f'sort_by=popularity.desc&with_genres=10751&'
                                        f'vote_count.gte=50&page=1')
        self.kids_movies_all_link = (f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}&language={self.lang}&'
                                        f'certification_country=US&certification=G|PG&'
                                        f'sort_by=popularity.desc&with_genres=16,10751&'
                                        f'vote_count.gte=50&page=1')

        ###cm#
        # Trakt
        self.trending_link = f'{self.trakt_link}/movies/trending?limit={self.count}&page=1'
        self.traktlists_link = f'{self.trakt_link}/users/me/lists?limit=250'
        self.traktlikedlists_link = f'{self.trakt_link}/users/likes/lists?limit=1000000'
        self.traktlist_link = f'{self.trakt_link}/users/%s/lists/%s/items'
        self.traktcollection_link = f'{self.trakt_link}/users/me/collection/movies'
        self.traktwatchlist_link = f'{self.trakt_link}/users/me/watchlist/movies?limit=2000'
        self.traktfeatured_link = f'{self.trakt_link}/recommendations/movies?limit={self.count}'
        self.trakthistory_link = f'{self.trakt_link}/users/me/history/movies?limit={self.count}&page=1'
        self.trakt_related_link = f'{self.trakt_link}/movies/%s/related?limit=10'
        # New Trakt discovery endpoints
        self.boxoffice_link = f'{self.trakt_link}/movies/boxoffice'
        self.traktrecommendations_link = f'{self.trakt_link}/recommendations/movies?limit={self.count}'
        self.traktpopular_link = f'{self.trakt_link}/movies/popular?limit={self.count}'
        self.traktanticipated_link = f'{self.trakt_link}/movies/anticipated?limit={self.count}'
        self.traktplayed_link = f'{self.trakt_link}/movies/played/weekly?limit={self.count}'
        self.traktwatched_link = f'{self.trakt_link}/movies/watched/weekly?limit={self.count}'
        self.traktcollected_link = f'{self.trakt_link}/movies/collected/weekly?limit={self.count}'
        self.onDeck_link = f'{self.trakt_link}/sync/playback/movies?extended=full&limit={self.count}'

        self.movieProgress_link = f'{self.trakt_link}/sync/playback/movies?extended=full&limit={self.count}'
        self.collection_link = f'{self.trakt_link}/users/me/collection/movies?extended=full&limit={self.count}'

        # Movie calendar endpoints (added 2026-03-10) - "What's releasing?" feature
        # Using TMDb Discovery API since Trakt's /calendars/all/ endpoints are broken (HTTP 500)
        # Release types: 2=Theatrical Limited, 3=Theatrical, 5=Physical (DVD/Blu-ray)
        # Shows ALL upcoming releases (not just personal watchlist like Trakt /my/ endpoints)
        # NOTE: Using 'release_date' (not 'primary_release_date') for upcoming releases
        # NOTE: No vote_count filter for upcoming movies (they haven't been released yet!)
        # NOTE: Filtered to English language originals to avoid foreign films (Korean, Japanese, Indian, etc.)
        self.calendar_thisweek_link = (f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}&language={self.lang}&region={self.country}'
                                        f'&release_date.gte={self.today_date}&release_date.lte={self.week_ahead_date}'
                                        f'&with_release_type=2|3&with_original_language=en&sort_by=release_date.asc&page=1')
        self.calendar_thismonth_link = (f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}&language={self.lang}&region={self.country}'
                                        f'&release_date.gte={self.today_date}&release_date.lte={self.month_ahead_date}'
                                        f'&with_release_type=2|3&with_original_language=en&sort_by=release_date.asc&page=1')
        self.calendar_dvd_thisweek_link = (f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}&language={self.lang}&region={self.country}'
                                        f'&release_date.gte={self.today_date}&release_date.lte={self.week_ahead_date}'
                                        f'&with_release_type=5&with_original_language=en&sort_by=release_date.asc&page=1')
        self.calendar_dvd_thismonth_link = (f'{self.tmdb_link}discover/movie?api_key={self.tmdb_user}&language={self.lang}&region={self.country}'
                                        f'&release_date.gte={self.today_date}&release_date.lte={self.month_ahead_date}'
                                        f'&with_release_type=5&with_original_language=en&sort_by=release_date.asc&page=1')

    def __del__(self):
        self.session.close()

    def get(self, url: str, tid: int = 0, idx: bool = True, create_directory: bool = True) -> list:
        """
        Get a list of movies from the given url.

        Args:
            url: URL or shortcut name to fetch movies from
            tid: Network/provider ID for TMDb discover endpoints
            idx: Whether to enrich with worker metadata
            create_directory: Whether to create Kodi directory listing

        Returns:
            List of movie dictionaries
        """
        c.log(f"[Movies.get] ENTRY - url={url}, tid={tid}, idx={idx}")
        try:
            # Validate URL and handle special cases
            if not url:
                c.log('[Movies.get] No URL provided, returning empty list.')
                return []

            # Special case: movieProgress uses local DB instead of Trakt API
            if url == 'movieProgress':
                return self._handle_special_case_movieProgress(idx, create_directory)

            # Simkl personal lists (plantowatch / completed / dropped / ratings) — no Trakt URL
            # url may be "simklwatchlist" or "simklwatchlist&page=2"
            simkl_base = (url or '').split('&')[0].split('?')[0]
            if simkl_base in ('simklwatchlist', 'simklhistory', 'simkldropped', 'simklratings'):
                page = 1
                m = re.search(r'[?&]page=(\d+)', url or '')
                if m:
                    try:
                        page = max(1, int(m.group(1)))
                    except (TypeError, ValueError):
                        page = 1
                return self._handle_simkl_list(simkl_base, idx, create_directory, page=page)

            # Simkl theatrical release calendar (CDN) — week / month; DVD stays TMDB
            # url may be "calendar_thismonth" or "calendar_thismonth&page=2"
            cal_base = (url or '').split('&')[0].split('?')[0]
            if tracker.simkl_live() and cal_base in ('calendar_thisweek', 'calendar_thismonth'):
                page = 1
                m = re.search(r'[?&]page=(\d+)', url or '')
                if m:
                    try:
                        page = max(1, int(m.group(1)))
                    except (TypeError, ValueError):
                        page = 1
                return self._handle_simkl_movie_calendar(cal_base, idx, create_directory, page=page)

            # Resolve URL shortcuts and date placeholders
            url, original_url = self._validate_and_resolve_url(url)
            if not url:
                return []

            essential_id = self._essential_route_id(url, 'essential_collection')
            if essential_id:
                return self._essential_collection(essential_id, idx, create_directory)
            essential_id = self._essential_route_id(url, 'essential_single')
            if essential_id:
                return self._essential_single(essential_id, idx, create_directory)

            # Parse URL to determine source
            parsed_url_netloc = urlparse(url).netloc.lower()
            c.log(f"[Movies.get] Parsed URL netloc: {parsed_url_netloc}")

            # Route to appropriate fetch method based on URL source
            assert self.trakt_link is not None

            if self.trakt_link in url and url == self.onDeck_link:
                # On Deck special case: realtime playback progress
                self.list = cache.get(self.trakt_list, self.CACHE_TTL_REALTIME, url, self.trakt_user)
                c.log(f"[Movies.get] OnDeck list length: {len(self.list)}")
                self.list = sorted(self.list, key=lambda k: int(k['year']), reverse=True)

            elif '/users/me/collection/' in url:
                # Personal Trakt collection only — do not match list slugs like *-collection
                c.log(f"[Movies.get] Fetching collection from: {url}")
                self.list = self.collection_list()
                self.list = sorted(self.list, key=lambda k: utils.title_key(k.get('title', '')))

            elif '/users/me/watchlist/' in url:
                # Personal watchlist — always live from Trakt (never stale cache)
                c.log(f"[Movies.get] Fetching watchlist (live) from: {url}")
                self.list = self.trakt_list(url, self.trakt_user) or []
                c.log(f"[Movies.get] Watchlist returned {len(self.list)} item(s)")
                self.list = sorted(self.list, key=lambda k: utils.title_key(k.get('title', '')))

            elif parsed_url_netloc in self.trakt_link and '/users/' in url:
                # Trakt user endpoints (collections, watchlists, etc.)
                self._fetch_from_trakt_users(url)

            elif parsed_url_netloc in self.search_link and isinstance(url, str) and '/search/movie' in url:
                # TMDb search endpoint — time the fetch; results use light listing path
                import time as _time
                _t0 = _time.perf_counter()
                self.list = cache.get(self.tmdb_list, self.CACHE_TTL_SEARCH, url) or []
                self.list = sorted(self.list, key=lambda k: int(k['year']), reverse=True)
                c.log(
                    f'[Movies] search TMDB fetch {(_time.perf_counter() - _t0):.2f}s '
                    f'n={len(self.list)}'
                )

            elif parsed_url_netloc in self.trakt_link and '/sync/playback/' in url:
                # Trakt playback progress (realtime, no caching)
                self.list = self.trakt_list(url, self.trakt_user)
                self.list = sorted(self.list, key=lambda k: int(k['paused_at']), reverse=True)

            elif parsed_url_netloc in self.trakt_link:
                # General Trakt endpoints (popular, trending, boxoffice, etc.)
                self._fetch_from_trakt_general(url)


            elif parsed_url_netloc in self.tmdb_networks_link and int(tid) > 0:
                # TMDb network/provider discover endpoints (Disney+, Netflix, etc.)
                # Substitute %s BEFORE cache.get_with_etag so the cache key and the
                # HTTP fetch both use the fully-resolved URL — otherwise TMDB returns
                # HTTP 200 with an empty results list for the literal "%s" URL, which
                # get_with_etag stores and replays forever.
                if '%s' in url:
                    url = url % tid
                try:
                    parsed = cache.get_with_etag(url, lambda cond: http_client.tmdb_get_conditional(url, cond, timeout=15),
                                                  ttl_seconds=int(self.CACHE_TTL_SHORT * 3600))
                    if parsed:
                        self.list = self.tmdb_list(url, tid=tid, response=parsed)
                    else:
                        self.list = self.tmdb_list(url, tid=tid)
                except Exception as e:
                    c.log(f"[Movies] TMDB network cache fetch failed, falling back: {e}")
                    self.list = self.tmdb_list(url, tid)

            elif parsed_url_netloc in self.tmdb_link and ('/user/' in url or '/list/' in url):
                # TMDb user lists - no caching (realtime)
                self.list = cache.get(self.list_tmdb_list, self.CACHE_TTL_REALTIME, url)
                self.list = sorted(self.list, key=lambda k: int(k['year']), reverse=True)

            elif parsed_url_netloc in self.tmdb_link and '/movie_credits' in url:
                # TMDb cast/crew credits - daily cache
                self.list = cache.get(self.tmdb_cast_list, self.CACHE_TTL_DAY, url)
                self.list = sorted(self.list, key=lambda k: int(k['year']), reverse=True)

            elif parsed_url_netloc in self.tmdb_link:
                # General TMDb endpoints (popular, trending, discover, etc.)
                self._fetch_from_tmdb(url, original_url)

            # Apply worker metadata enrichment and create directory
            self._apply_worker_and_create_directory(url, idx, create_directory)
            return self.list
        except Exception as e:
            failure = traceback.format_exc()
            c.log(f'[Movies @ get] Traceback: {failure}')
            c.log(f'[Movies @ get] Exception: {e}')
            if create_directory:
                try:
                    control.directory(int(sys.argv[1]), cacheToDisc=False)
                except Exception:
                    pass
            c.log(f'Exception raised in movies.get(), error = {e}', 1)

    def widget(self):
        """Load featured movies for widget display."""
        self.get(self.featured_link)

    def _validate_and_resolve_url(self, url: str):
        """
        Validate and resolve URL shortcuts, handling date placeholders.

        Args:
            url: URL or shortcut name

        Returns:
            tuple: (resolved_url, original_template_url) - empty strings if validation fails
        """
        # If starts with http, it's already a full URL
        if url.startswith('http'):
            c.log(f'[Movies] Full URL provided: {url}')
            original_url = url
        elif url.startswith('essential_'):
            c.log(f'[Movies] Essential catalog route: {url}')
            original_url = url
        else:
            # Handle calendar shortcuts and other link attributes
            if url in ['calendar_thisweek', 'calendar_thismonth', 'calendar_dvd_thisweek', 'calendar_dvd_thismonth']:
                url_link = getattr(self, f"{url}_link", None)
                if url_link is None:
                    c.log(f'[Movies] ERROR: Failed to find calendar attribute {url}_link')
                    return '', ''
                c.log(f"[Movies] Calendar shortcut '{url}' -> {url_link}")
                url = url_link
            else:
                url_link = getattr(self, f"{url}_link", None)
                if url_link is None:
                    c.log(f'[Movies] ERROR: Failed to find attribute {url}_link')
                    return '', ''
                url = url_link
            original_url = url

        # Replace date[N] placeholders with actual dates
        url = url or ''
        for days_offset in re.findall(r'date\[(\d+)\]', url):
            replacement_date = (self.datetime - datetime.timedelta(days=int(days_offset))).strftime('%Y-%m-%d')
            url =url.replace(f'date[{days_offset}]', replacement_date)

        return url, original_url

    @staticmethod
    def _essential_route_id(url, prefix):
        for sep in ('.', '/'):
            token = f'{prefix}{sep}'
            if url.startswith(token):
                return url[len(token):]
        return None

    def _handle_special_case_movieProgress(self, idx: bool, create_directory: bool):
        """
        Continue Watching movies.

        Trakt: local progress DB. Simkl: GET /sync/playback/movies (light enrich).
        """
        if tracker.simkl_live():
            return self._handle_simkl_movie_playback(idx, create_directory)

        c.log("[Movies] Using local movie_progress_list for 'movieProgress' menu")
        self.list = cache.get(self.movie_progress_list, self.CACHE_TTL_REALTIME)
        self.list = sorted(self.list, key=lambda k: int(k.get('year', 0)), reverse=True)

        if idx:
            self.worker()
        if idx and create_directory:
            self.movie_directory(self.list)
        return self.list

    def _handle_simkl_movie_playback(self, idx: bool, create_directory: bool):
        """Simkl Continue Watching — paused movie sessions with resume %."""
        c.log("[Movies] Simkl movie playback (Continue Watching)")
        self.list = simkl.crew_movie_playback_items() or []
        self.pagination = {}
        self._simkl_list_enrich()
        c.log(f"[Movies] Simkl playback: {len(self.list)} movie(s)")
        if idx and create_directory:
            self.movie_directory(self.list)
        return self.list

    def _handle_simkl_movie_calendar(self, url: str, idx: bool, create_directory: bool, page: int = 1):
        """Simkl CDN movie_release calendar → movie directory (40/page)."""
        window = 'week' if url == 'calendar_thisweek' else 'month'
        page_size = 40
        try:
            page = max(1, int(page) or 1)
        except (TypeError, ValueError):
            page = 1

        c.log(f"[Movies] Simkl movie calendar window={window} page={page}")
        all_items = simkl.crew_movie_calendar(window=window, mine_only=False) or []
        total = len(all_items)
        total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
        start = (page - 1) * page_size
        end = start + page_size
        self.list = all_items[start:end]
        self.pagination = {'page': page, 'page_count': total_pages}

        next_url = f'{url}&page={page + 1}' if end < total else ''
        for item in self.list:
            item['next'] = next_url

        self._simkl_list_enrich()
        c.log(
            f"[Movies] Simkl movie calendar {window}: "
            f"{len(self.list)}/{total} (page {page}/{total_pages})"
        )
        if idx and create_directory:
            self.movie_directory(self.list)
        return self.list

    def _handle_simkl_list(self, url: str, idx: bool, create_directory: bool, page: int = 1):
        """Simkl plantowatch / completed / dropped / ratings → movie directory.

        Skip full worker. Paginate at 40/page, then light TMDB + optional Fanart.tv.
        """
        status_map = {
            'simklwatchlist': 'plantowatch',
            'simklhistory': 'completed',
            'simkldropped': 'dropped',
        }
        page_size = 40
        try:
            page = max(1, int(page) or 1)
        except (TypeError, ValueError):
            page = 1

        if url == 'simklratings':
            c.log(f"[Movies] Simkl ratings (page={page})")
            all_items = simkl.crew_rated_movies() or []
            all_items = sorted(
                all_items,
                key=lambda k: (-int(k.get('user_rating') or 0), utils.title_key(k.get('title', ''))),
            )
            status = 'ratings'
        else:
            status = status_map.get(url, 'plantowatch')
            c.log(f"[Movies] Simkl list '{url}' (status={status}, page={page})")
            all_items = simkl.crew_movie_items(status) or []
            all_items = sorted(all_items, key=lambda k: utils.title_key(k.get('title', '')))
        total = len(all_items)
        total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
        start = (page - 1) * page_size
        end = start + page_size
        self.list = all_items[start:end]
        self.pagination = {'page': page, 'page_count': total_pages}

        next_url = f'{url}&page={page + 1}' if end < total else ''
        for item in self.list:
            item['next'] = next_url

        self._simkl_list_enrich()

        c.log(
            f"[Movies] Simkl {status}: showing {len(self.list)}/{total} "
            f"(page {page}/{total_pages})"
        )
        if idx and create_directory:
            self.movie_directory(self.list)
        return self.list

    @staticmethod
    def _simkl_gap(value):
        return value in (None, '', '0', 0)

    @staticmethod
    def _mpaa_gap(value):
        """True when MPAA is missing (skin then shows NR). Real Unrated/NR from TMDB is kept."""
        return value in (None, '', '0', 0)

    @staticmethod
    def _mpaa_from_tmdb(item):
        """
        US MPAA from TMDB release_dates (append_to_response=release_dates).
        Prefer theatrical (3) then limited (2), then any non-empty US certification.
        """
        try:
            results = ((item or {}).get('release_dates') or {}).get('results') or []
            us = next((r for r in results if r.get('iso_3166_1') == 'US'), None)
            if not us:
                return '0'
            dates = us.get('release_dates') or []
            for prefer_type in (3, 2, 4, 5, 6, 1):
                for entry in dates:
                    if entry.get('type') == prefer_type:
                        cert = (entry.get('certification') or '').strip()
                        if cert:
                            return cert
            for entry in dates:
                cert = (entry.get('certification') or '').strip()
                if cert:
                    return cert
        except Exception:
            pass
        return '0'

    def _simkl_list_enrich(self):
        """Gap-fill Simkl movie rows via TMDB (incl. credits) + optional Fanart.tv logos."""
        items = self.list
        if not items:
            return

        self.meta = []
        for item in items:
            item['metacache'] = False
        self.list = metacache.fetch(self.list, self.lang, self.user)
        items = self.list

        meta_keys = (
            'fanart', 'plot', 'tagline', 'genre', 'studio', 'rating', 'votes',
            'premiered', 'duration', 'mpaa', 'clearlogo', 'clearart', 'banner',
            'landscape', 'discart',
        )
        fanart_on = c.get_setting('fanart') == 'true'
        meta_lock = threading.Lock()

        def _has_cast(item):
            cast = item.get('castwiththumb')
            return isinstance(cast, list) and bool(cast)

        def _has_people(value):
            if not value or value in ('0', 'None'):
                return False
            if isinstance(value, (list, tuple)):
                return any(bool(x) and str(x) not in ('0',) for x in value)
            return True

        def _missing_credits(item):
            return (
                not _has_cast(item)
                or not _has_people(item.get('director'))
                or not _has_people(item.get('writer'))
            )

        def needs_work(item):
            # Credits + MPAA are required for Info dialog; slim Simkl caches often lack them.
            if _missing_credits(item) or self._mpaa_gap(item.get('mpaa')):
                return True
            if item.get('metacache') and not self._simkl_gap(item.get('plot')):
                if not fanart_on:
                    return False
                return any(
                    self._simkl_gap(item.get(k))
                    for k in ('clearlogo', 'clearart', 'banner', 'landscape', 'discart')
                )
            return any(self._simkl_gap(item.get(k)) for k in meta_keys)

        def fill_gap(item, key, value):
            if self._simkl_gap(item.get(key)) and value not in (None, '', '0', 0):
                item[key] = str(value) if isinstance(value, (int, float)) else value

        def enrich_one(index):
            item = items[index]
            try:
                if not needs_work(item):
                    return
                tmdb = str(item.get('tmdb') or '0')
                imdb = str(item.get('imdb') or '0')
                if tmdb == '0' and imdb == '0':
                    return

                if tmdb == '0' and imdb != '0':
                    try:
                        find_url = self.tmdb_by_imdb % imdb
                        found = cache.get(http_client.tmdb_get_json, 24, find_url, 10) or {}
                        results = found.get('movie_results') or []
                        if results:
                            tmdb = str(results[0].get('id') or '0')
                            item['tmdb'] = tmdb
                    except Exception:
                        pass

                if tmdb == '0':
                    return

                url = (
                    f'{self.tmdb_link}movie/{tmdb}?api_key={self.tmdb_user}'
                    f'&language={self.lang}&append_to_response=credits,release_dates,external_ids'
                )
                result = cache.get(http_client.tmdb_get_json, 24, url, 10) or {}
                if not result:
                    return

                if self._simkl_gap(item.get('poster')) and result.get('poster_path'):
                    item['poster'] = self.tmdb_img_link % (c.tmdb_postersize, result['poster_path'])
                if self._simkl_gap(item.get('fanart')) and result.get('backdrop_path'):
                    item['fanart'] = self.tmdb_img_link % (c.tmdb_fanartsize, result['backdrop_path'])

                fill_gap(item, 'plot', result.get('overview'))
                fill_gap(item, 'tagline', result.get('tagline'))
                genres = result.get('genres') or []
                if genres:
                    fill_gap(item, 'genre', ' / '.join(g.get('name', '') for g in genres if g.get('name')))
                companies = result.get('production_companies') or []
                if companies:
                    fill_gap(item, 'studio', companies[0].get('name'))
                fill_gap(item, 'rating', result.get('vote_average'))
                fill_gap(item, 'votes', result.get('vote_count'))
                fill_gap(item, 'premiered', result.get('release_date'))
                fill_gap(item, 'duration', result.get('runtime'))
                if self._mpaa_gap(item.get('mpaa')):
                    fill_gap(item, 'mpaa', self._mpaa_from_tmdb(result))

                credits = result.get('credits') if isinstance(result.get('credits'), dict) else {}
                if not _has_cast(item):
                    castwiththumb = []
                    for idx, person in enumerate((credits.get('cast') or [])[:30]):
                        if not isinstance(person, dict):
                            continue
                        name = (person.get('name') or '').strip()
                        if not name:
                            continue
                        profile = person.get('profile_path') or ''
                        icon = self.tmdb_img_link % (c.tmdb_profilesize, profile) if profile else ''
                        castwiththumb.append({
                            'name': name,
                            'role': (person.get('character') or '').strip(),
                            'order': int(person.get('order') if person.get('order') is not None else idx),
                            'thumbnail': icon,
                        })
                    if castwiththumb:
                        item['castwiththumb'] = castwiththumb
                crew = credits.get('crew') or []
                if isinstance(crew, list):
                    if not _has_people(item.get('director')):
                        directors = [
                            d.get('name') for d in crew
                            if isinstance(d, dict) and d.get('job') == 'Director' and d.get('name')
                        ]
                        if directors:
                            item['director'] = ', '.join(directors)
                    if not _has_people(item.get('writer')):
                        writers = [
                            w.get('name') for w in crew
                            if isinstance(w, dict)
                            and w.get('job') in ('Writer', 'Screenplay', 'Author', 'Novel')
                            and w.get('name')
                        ]
                        if writers:
                            item['writer'] = ', '.join(writers)

                ext = result.get('external_ids') or {}
                if self._simkl_gap(item.get('imdb')) and ext.get('imdb_id'):
                    item['imdb'] = ext['imdb_id']
                    imdb = item['imdb']

                if fanart_on and imdb not in ('0', '', None):
                    art = cache.get(
                        fanart_tv.get_fanart_tv_art, 72, '0', imdb, 'en', 'movie'
                    ) or {}
                    for key in ('clearlogo', 'clearart', 'banner', 'landscape', 'discart'):
                        fill_gap(item, key, art.get(key))
                    fill_gap(item, 'fanart', art.get('fanart'))

                # Include credits so slim Simkl re-cache does not wipe cast/director/writer
                cache_item = {}
                for k in (
                    'title', 'year', 'imdb', 'tmdb', 'poster', 'fanart', 'banner',
                    'clearlogo', 'clearart', 'landscape', 'discart', 'premiered',
                    'studio', 'genre', 'duration', 'mpaa', 'rating', 'votes',
                    'plot', 'tagline', 'director', 'writer', 'castwiththumb',
                ):
                    if k not in item:
                        continue
                    v = item[k]
                    if k == 'castwiththumb':
                        if isinstance(v, list) and v:
                            cache_item[k] = v
                    elif v not in (None, '', '0'):
                        cache_item[k] = v
                row = {
                    'imdb': item.get('imdb') or '0',
                    'tmdb': tmdb,
                    'tvdb': '0',
                    'lang': self.lang,
                    'user': self.user,
                    'item': cache_item,
                }
                with meta_lock:
                    self.meta.append(row)
            except Exception as e:
                c.log(f'[Movies] Simkl list enrich skip: {e}', 1)

        try:
            max_nr = c.get_max_threads(len(items))
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_nr) as executor:
                list(executor.map(enrich_one, range(len(items))))
        except Exception as e:
            c.log(f'[Movies] Simkl list enrich error: {e}', 1)
            for i in range(len(items)):
                enrich_one(i)

        if self.meta:
            metacache.insert(self.meta)

    def _essential_collection(self, collection_id, idx, create_directory):
        """Fetch a TMDb movie collection (essential catalog boxset drill-in)."""
        url = f'{self.tmdb_link}collection/{collection_id}?api_key={self.tmdb_user}&language={self.lang}'
        c.log(f'[Movies] Essential collection: {collection_id}')
        self.list = cache.get(self.tmdb_list, self.CACHE_TTL_MONTH, url) or []
        c.log(f'[Movies] Essential collection {collection_id} returned {len(self.list)} movie(s)')
        self.list = sorted(self.list, key=lambda k: k.get('premiered') or '9999-99-99')
        self._apply_worker_and_create_directory(url, idx, create_directory)
        return self.list

    def _essential_single(self, movie_id, idx, create_directory):
        """Fetch one movie from the essential catalog (non-collection entry)."""
        url = f'{self.tmdb_link}movie/{movie_id}?api_key={self.tmdb_user}&language={self.lang}'
        c.log(f'[Movies] Essential single movie: {movie_id}')
        self.list = cache.get(self.tmdb_single_movie, self.CACHE_TTL_MONTH, url) or []
        self._apply_worker_and_create_directory(url, idx, create_directory)
        return self.list

    def _fetch_from_trakt_users(self, url: str):
        """
        Fetch movies from Trakt user endpoints (collections, watchlists, history).
        Uses smart TTL based on activity timestamps.
        """
        try:
            if '/users/me/watchlist/' in url:
                self.list = cache.get(self.trakt_list, None, url, self.trakt_user) or []
                self.list = sorted(self.list, key=lambda k: utils.title_key(k.get('title', '')))
                return

            # Decide cache TTL explicitly for user-related trakt requests
            if url == self.trakthistory_link:
                # History should be relatively fresh
                ttl = self.CACHE_TTL_MEDIUM
                c.log(f"[Movies] Trakt history requested, TTL={ttl}h")
            else:
                activity = trakt.getActivity_from_db()
                last_fetch = cache.timeout(self.trakt_list, url, self.trakt_user)
                # Use long TTL when activity is older than our cached timestamp
                ttl = self.CACHE_TTL_MONTH if activity <= last_fetch else self.CACHE_TTL_MEDIUM
                c.log(f"[Movies] Using TTL={ttl}h (activity={activity}, last_fetch={last_fetch}) for url={url}")

            self.list = cache.get(self.trakt_list, ttl, url, self.trakt_user) or []
        except Exception as e:
            c.log(f"[Movies] Error selecting trakt cache TTL: {e}")
            self.list = cache.get(self.trakt_list, self.CACHE_TTL_MEDIUM, url, self.trakt_user) or []

        # Third-party curated lists keep Trakt rank order; other user endpoints sort by title
        if '/lists/' not in url:
            self.list = sorted(self.list, key=lambda k: utils.title_key(k.get('title', '')))

    def _fetch_from_trakt_general(self, url: str):
        """
        Fetch movies from general Trakt endpoints (trending, popular, boxoffice).
        Uses endpoint-specific TTL based on update frequency.
        """
        try:
            if '/sync/playback/' in url or '/sync/' in url or '/users/me/' in url:
                # Don't cache playback or highly dynamic sync endpoints
                self.list = self.trakt_list(url, self.trakt_user)
            elif '/calendars/' in url:
                # Calendar endpoints are time-sensitive
                c.log(f"[Movies Calendar] Fetching calendar from: {url}")
                self.list = cache.get(self.trakt_list, self.CACHE_TTL_HOUR, url, self.trakt_user)
                c.log(f"[Movies Calendar] Received {len(self.list) if self.list else 0} results")
                if not self.list:
                    calendar_type = "DVD/Blu-ray" if "/dvd/" in url else "Theatrical"
                    days = "30" if "/30" in url else "7"
                    c.log(f"[Movies Calendar] No {calendar_type} releases in next {days} days from YOUR Trakt watchlist/collection")
                    c.log(f"[Movies Calendar] Tip: Add movies to your Trakt watchlist at https://trakt.tv to see them here")
            elif '/movies/boxoffice' in url or '/recommendations/' in url:
                # Box office updates weekly, recommendations change slowly
                self.list = cache.get(self.trakt_list, self.CACHE_TTL_2DAY, url, self.trakt_user)
            elif '/movies/popular' in url or '/movies/anticipated' in url:
                # Popular/anticipated update daily
                self.list = cache.get(self.trakt_list, self.CACHE_TTL_DAY, url, self.trakt_user)
            elif '/movies/played' in url or '/movies/watched' in url or '/movies/collected' in url:
                # Played/watched/collected update daily
                self.list = cache.get(self.trakt_list, self.CACHE_TTL_DAY, url, self.trakt_user)
            elif '/movies/trending' in url:
                # Trending is more dynamic
                self.list = cache.get(self.trakt_list, self.CACHE_TTL_MEDIUM, url, self.trakt_user)
            else:
                # Cache general public trakt lists for 24 hours (default)
                self.list = cache.get(self.trakt_list, self.CACHE_TTL_DAY, url, self.trakt_user)
        except Exception as e:
            c.log(f"[Movies] Trakt fetch/cache failed for {url}: {e}")
            c.log(f"[Movies] Traceback: {traceback.format_exc()}")
            self.list = []

        # Ensure self.list is valid before sorting
        if self.list is None:
            self.list = []
            c.log(f"[Movies] Warning: trakt_list returned None for {url}")
        elif self.list:
            # Sort the list based on the specific endpoint
            if '/calendars/' in url:
                # Calendar: sort by release date ascending (earliest releases first)
                self.list = sorted(self.list, key=lambda k: k.get('premiered', '') or k.get('released_digital', '') or '9999-12-31')
                c.log(f"[Movies Calendar] Sorted {len(self.list)} movies by release date (ascending)")
            elif '/movies/anticipated' in url:
                # Anticipated: sort by release date ascending (earliest first)
                self.list = sorted(self.list, key=lambda k: k.get('premiered', '9999-12-31'))
                c.log(f"[Movies] Sorted {len(self.list)} anticipated movies by premiered date (ascending)")
            else:
                # All other Trakt lists: sort by year descending (most recent first)
                self.list = sorted(self.list, key=lambda k: int(k['year']), reverse=True)

    def _fetch_from_tmdb(self, url: str, original_url: str):
        """
        Fetch movies from TMDb endpoints (popular, trending, discover).
        Uses smart caching based on endpoint type.
        """
        try:
            # Determine caching strategy based on URL patterns
            is_calendar = ('release_date.gte' in url and 'release_date.lte' in url and 'with_release_type' in url)
            should_short_cache = any(x in url for x in ('/movie/popular', '/trending/movie', 'now_playing'))
            should_day_cache = any(x in url for x in ('/movie/upcoming', 'latest_releases', 'premieres'))
            discover_filters = ('with_genres', 'year=', 'first_air_date_year', 'with_watch_providers', 'with_keywords')

            if not should_short_cache:
                should_short_cache = ('/discover/movie' in url and any(f in url for f in discover_filters))

            if is_calendar:
                # Calendar endpoints are time-sensitive
                c.log(f"[Movies Calendar] Fetching TMDb calendar from: {url}")
                self.list = cache.get(self.tmdb_list, self.CACHE_TTL_HOUR, url)
                c.log(f"[Movies Calendar] Received {len(self.list) if self.list else 0} results")
                if not self.list:
                    calendar_type = "DVD/Blu-ray" if "with_release_type=5" in url else "Theatrical"
                    days = "30" if self.month_ahead_date in url else "7"
                    c.log(f"[Movies Calendar] No {calendar_type} releases in next {days} days")
            elif should_day_cache:
                # Cache upcoming movies for 24 hours
                self.list = cache.get(self.tmdb_list, self.CACHE_TTL_DAY, url)
            elif should_short_cache:
                # Use get_with_etag for discover endpoints to keep cache key stable
                if '/discover/movie' in original_url and any(f in original_url for f in discover_filters):
                    parsed = cache.get_with_etag(
                        original_url,
                        lambda cond: http_client.tmdb_get_conditional(url, cond, timeout=15),
                        ttl_seconds=int(self.CACHE_TTL_SHORT * 3600)
                    )
                    if parsed:
                        self.list = self.tmdb_list(url, response=parsed)
                    else:
                        self.list = self.tmdb_list(url)
                else:
                    # Short-cache frequent TMDB list endpoints
                    self.list = cache.get(self.tmdb_list, self.CACHE_TTL_SHORT, url)
            else:
                self.list = self.tmdb_list(url)
        except Exception as e:
            c.log(f"[Movies] TMDB cache fetch failed, falling back to direct fetch: {e}")
            self.list = self.tmdb_list(url)

        # Sort by year for most TMDb lists, but skip for calendar views (already sorted by release date)
        if not ('release_date.gte' in url and 'release_date.lte' in url and 'with_release_type' in url):
            self.list = sorted(self.list, key=lambda k: int(k['year']), reverse=True)

    def _apply_worker_and_create_directory(self, url: str, idx: bool, create_directory: bool):
        """
        Apply worker metadata enrichment and create Kodi directory listing.
        For calendar views, preserves fresh metadata from API while adding cached artwork.
        Movie search uses a light path (no per-item TMDB/Fanart.tv) — full enrich is too slow on Xbox.
        """
        # Check if this is a calendar view (needs special metadata preservation)
        is_calendar_view = '/calendars/' in url or ('release_date.gte' in url and 'release_date.lte' in url and 'with_release_type' in url)
        is_movie_search = isinstance(url, str) and '/search/movie' in url

        widget_listing = c.is_widget_listing()
        if idx:
            if widget_listing:
                self.widget_worker()
            elif is_movie_search:
                self.search_worker()
            elif is_calendar_view:
                # Save original metadata from Trakt/TMDB (fresh data, not stale cache)
                preserved_metadata = {}
                for item in self.list:
                    if item:
                        key_id = item.get('imdb') or item.get('tmdb')
                        title = item.get('title', '')
                        year = item.get('year', '0')
                        if key_id:
                            # Preserve critical metadata fields
                            preserved_metadata[(key_id, title, year)] = {
                                'premiered': item.get('premiered', ''),
                                'plot': item.get('plot', ''),
                                'rating': item.get('rating', '0'),
                                'votes': item.get('votes', '0'),
                                'unaired': item.get('unaired', '')
                            }

                # Call worker for metadata enrichment (gets cached artwork)
                self.worker()

                # Restore original metadata
                for item in self.list:
                    if item:
                        key_id = item.get('imdb') or item.get('tmdb')
                        title = item.get('title', '')
                        year = item.get('year', '0')
                        if key_id and (key_id, title, year) in preserved_metadata:
                            for field, value in preserved_metadata[(key_id, title, year)].items():
                                # Special case: 'unaired' can be '' (falsy) which is valid
                                if field == 'unaired':
                                    item[field] = value
                                elif value and value != '0':
                                    item[field] = value
            else:
                self.worker()

        if create_directory:
            self.movie_directory(self.list)

    def search(self) -> None:
        """Executes a search operation for TV shows."""

        dbcon = database.connect(control.searchFile)
        dbcur = dbcon.cursor()

        navigator.navigator.addDirectoryItem(32603, 'movieSearchnew', 'search.png', 'DefaultMovies.png', is_folder=False)

        try:
            sql = "SELECT count(*) as aantal FROM sqlite_master WHERE type='table' AND name='movies'"
            dbcur.execute(sql)
            dbcon.commit()
            if dbcur.fetchone()[0] == 0:
                # table does not exist
                sql = "CREATE TABLE movies (id INTEGER PRIMARY KEY AUTOINCREMENT, term TEXT)"
                dbcur.execute(sql)
            dbcon.commit()
        except OperationalError as e:
            c.log(f"[Movies @ search] OperationalError in search database: {e}", 1)


        dbcur.execute("SELECT * FROM movies ORDER BY id DESC")
        dbcon.commit()
        cm = []

        search_terms = []
        context_menu_items = []
        rows = dbcur.fetchall()
        delete_option = False
        for _id, term in rows:
            if not term or not str(term).strip():
                continue
            if term not in search_terms:
                delete_option = True
                cm = ((32070, f'movieDeleteTerm&id={_id}'))

                navigator.navigator.addDirectoryItem(
                    f'{term}',
                    f'movieSearchterm&name={quote_plus(term)}',
                    'search.png',
                    'DefaultTVShows.png',
                    context=cm,
                )
                search_terms.append(term)
        dbcur.close()

        if delete_option:
            navigator.navigator.addDirectoryItem(
                32605, 'clearCacheSearch', 'tools.png', 'DefaultAddonProgram.png', is_folder=False
            )

        navigator.navigator.endDirectory(cacheToDisc=False)


    def search_new(self) -> None:
        """Search for a Movie.

        movieSearchnew is is_folder=False (handle -1), so results must be
        opened via Container.Update → movieSearchterm (same race as TV:
        Container.Refresh-before-get left users on history only).
        """
        try:
            control.idle()

            keyboard_header = c.lang(32010)
            keyboard = control.keyboard('', keyboard_header)
            keyboard.doModal()
            search_query = keyboard.getText() if keyboard.isConfirmed() else None

            if search_query is None:
                return

            search_query = (search_query or '').strip().lower()
            if not search_query:
                return

            db_connection = database.connect(control.searchFile)
            db_cursor = db_connection.cursor()
            db_cursor.execute("DELETE FROM movies WHERE term = ?", (search_query,))
            db_cursor.execute("INSERT INTO movies VALUES (?,?)", (None, search_query))
            db_connection.commit()
            db_cursor.close()

            control.execute(
                f'Container.Update({sys.argv[0]}?action=movieSearchterm&name={quote_plus(search_query)})'
            )
        finally:
            control.finish_dialog_route()

    def search_term(self, query: str) -> None:
        control.idle()
        query = (query or '').strip().lower()
        if not query:
            # Empty term (bad history / accidental OK) — stay on search menu, don't paint empty results
            c.log('[Movies] search_term ignored empty query; showing search history')
            self.search()
            return
        cleaned_query = utils.title_key(query)

        db_connection = database.connect(control.searchFile)
        db_cursor = db_connection.cursor()
        db_cursor.execute("DELETE FROM movies WHERE term = ?", (query,))
        db_cursor.execute("INSERT INTO movies VALUES (?, ?)", (None, query))
        db_connection.commit()
        db_cursor.close()

        search_url = self.search_link % quote_plus(cleaned_query)
        self.get(search_url)

    def delete_search_term(self, search_term_id: int) -> None:
        """
        Deletes a search term from the database.

        This method takes the ID of a search term as an argument, deletes the
        corresponding record from the database, and refreshes the Kodi UI.

        :param search_term_id: The ID of the search term to delete.
        :type search_term_id: int
        """
        try:
            db_connection = database.connect(control.searchFile)
            db_cursor = db_connection.cursor()
            db_cursor.execute("DELETE FROM movies WHERE ID = ?", (search_term_id,))
            db_connection.commit()
            db_cursor.close()
            control.refresh()
        except Exception as e:

            error_traceback = traceback.format_exc()
            c.log(f'[Error in delete_search_term] Traceback: {error_traceback}')
            c.log(f'[Error in delete_search_term] Exception: {e}')

    def advanced_search(self, filter_id=None):
        """
        Show advanced search dialog, then list results.

        Menu / Saved Filters open this as a folder — emit into that handle so
        Back returns to Movies. RunPlugin / CM (handle < 0) uses Container.Update.
        """
        try:
            handle = int(sys.argv[1])
        except (TypeError, ValueError, IndexError):
            handle = -1

        try:
            from resources.lib.modules import advanced_search as adv_search

            control.idle()

            filter_data = None
            if filter_id:
                manager = adv_search.FilterManager()
                filter_data = manager.get_filter(filter_id)

            result, save_filter = adv_search.show_advanced_search('movie', filter_data)

            if not result:
                if handle >= 0:
                    control.close_directory(handle=handle, cacheToDisc=False, succeeded=False)
                else:
                    control.finish_dialog_route()
                return

            if save_filter:
                keyboard = control.keyboard('', 'Name this filter')
                keyboard.doModal()
                filter_name = keyboard.getText() if keyboard.isConfirmed() else None

                if filter_name:
                    manager = adv_search.FilterManager()
                    manager.save_filter(filter_name, 'movie', result)

            if handle < 0:
                control.execute(
                    adv_search.results_update_url(
                        sys.argv[0], 'movieAdvancedResults', result
                    )
                )
                control.finish_dialog_route()
                return

            # Folder click: list into this handle (Back → Movies / Saved Filters).
            self.advanced_search_execute(result)

        except Exception as e:
            c.log(f'[Movies] Error in advanced_search: {e}')
            c.log(f'[Movies] Traceback: {traceback.format_exc()}')
            if handle >= 0:
                control.close_directory(handle=handle, cacheToDisc=False, succeeded=False)
            else:
                control.finish_dialog_route()

    def advanced_search_results(self, filter_param=None):
        """Folder-handle entry for advanced search results (after dialog)."""
        try:
            from resources.lib.modules import advanced_search as adv_search
            filter_data = adv_search.decode_filter(filter_param)
            if not filter_data:
                # Legacy fallback if an older path stashed on Window(10000)
                filter_data = adv_search.pop_filter()
            if not filter_data:
                c.infoDialog('Advanced search expired — try again', sound=True, icon='INFO')
                control.finish_dialog_route()
                return
            self.advanced_search_execute(filter_data)
        except Exception as e:
            c.log(f'[Movies] Error in advanced_search_results: {e}')
            c.log(f'[Movies] Traceback: {traceback.format_exc()}')
            control.finish_dialog_route()

    def advanced_search_execute(self, filter_data):
        """Execute advanced search with given filter criteria"""
        try:
            from resources.lib.modules import advanced_search as adv_search

            # Build discover or search URL
            url = adv_search.build_discover_url(
                self.tmdb_link,
                self.tmdb_user,
                'movie',
                filter_data
            )

            if not url:
                return

            if c.devmode:
                c.log(f"[Movies] Executing advanced search: {url}")

            # Check if we're using search endpoint (has 'keyword' in filter)
            is_search = '/search/' in url

            if is_search and (filter_data.get('genre_ids') or filter_data.get('min_rating') or
                            adv_search.needs_year_postfilter(filter_data)):
                # Search endpoint doesn't support genre/rating filters, need to post-filter
                self._advanced_search_with_postfilter(url, filter_data)
            else:
                # Regular discover or search without additional filters
                self.get(url)

        except Exception as e:
            c.log(f'[Movies] Error in advanced_search_execute: {e}')
            c.log(f'[Movies] Traceback: {traceback.format_exc()}')

    def _advanced_search_with_postfilter(self, url, filter_data):
        """Fetch search results and apply post-filtering for genre/rating/year"""
        try:
            if c.devmode:
                c.log(f"[Movies] Fetching search results for post-filtering")

            response = self.session.get(url, timeout=10)
            if response.status_code != 200:
                c.log(f"[Movies] Search request failed with status {response.status_code}")
                return

            data = response.json()
            results = data.get('results', [])

            # Calculate next page URL for pagination
            try:
                page = int(data.get('page', 1))
                total_pages = int(data.get('total_pages', 1))

                # Store pagination info for "Page X of Y" display
                if page > 0 and total_pages > 0:
                    self.pagination = {
                        'page': page,
                        'page_count': total_pages
                    }

                if page >= total_pages or 'page=' not in url:
                    _next = '0'
                else:
                    _next = '%s&page=%s' % (url.split('&page=', 1)[0], page + 1)
            except Exception:
                _next = '0'

            if c.devmode:
                c.log(f"[Movies] Got {len(results)} results, applying filters...")

            # Apply post-filters
            filtered_results = []
            for item in results:
                movie_name = item.get('title', 'Unknown')
                passed_filters = True
                filter_reasons = []

                # Filter by year range
                if filter_data.get('year_from') or filter_data.get('year_to'):
                    release_date = item.get('release_date', '')
                    if release_date:
                        try:
                            year = int(release_date.split('-')[0])
                            if filter_data.get('year_from') and year < filter_data['year_from']:
                                passed_filters = False
                                filter_reasons.append(f"year {year} < {filter_data['year_from']}")
                            if filter_data.get('year_to') and year > filter_data['year_to']:
                                passed_filters = False
                                filter_reasons.append(f"year {year} > {filter_data['year_to']}")
                        except (ValueError, TypeError):
                            pass

                # Filter by rating
                if filter_data.get('min_rating'):
                    vote_average = item.get('vote_average', 0)
                    if vote_average < filter_data['min_rating']:
                        passed_filters = False
                        filter_reasons.append(f"rating {vote_average} < {filter_data['min_rating']}")

                # Filter by genre
                if filter_data.get('genre_ids'):
                    item_genre_ids = item.get('genre_ids', [])
                    required_genres = [int(gid) for gid in filter_data['genre_ids'].split(',') if gid.strip()]
                    # Check if any of the required genres match
                    if not any(gid in item_genre_ids for gid in required_genres):
                        passed_filters = False
                        filter_reasons.append(f"no matching genre (has {item_genre_ids}, need one of {required_genres})")

                if passed_filters:
                    filtered_results.append(item)
                    if c.devmode:
                        c.log(f"[Movies] (OK) '{movie_name}' passed all filters")
                else:
                    if c.devmode:
                        c.log(f"[Movies] (X) '{movie_name}' filtered out: {', '.join(filter_reasons)}")

            if c.devmode:
                c.log(f"[Movies] After filtering: {len(filtered_results)} results")

            if not filtered_results:
                # Show helpful message about why no results
                msg = "No results found. Try:\n"
                msg += "• Removing genre filters (TMDB metadata can be incomplete)\n"
                msg += "• Expanding year range\n"
                msg += "• Lowering minimum rating"
                c.infoDialog(msg, sound=True, icon='INFO', time=5000)
                return

            # Convert filtered results to the format expected by tmdb_list
            self.list = []
            for item in filtered_results:
                try:
                    movie_item = {
                        'tmdb': str(item.get('id', '')),
                        'title': item.get('title', ''),
                        'originaltitle': item.get('original_title', ''),
                        'year': item.get('release_date', '')[:4] if item.get('release_date') else '',
                        'premiered': item.get('release_date', ''),
                        'rating': str(item.get('vote_average', '')),
                        'votes': str(item.get('vote_count', '')),
                        'plot': item.get('overview', ''),
                        'poster': f"https://image.tmdb.org/t/p/w500{item.get('poster_path')}" if item.get('poster_path') else '',
                        'fanart': f"https://image.tmdb.org/t/p/original{item.get('backdrop_path')}" if item.get('backdrop_path') else '',
                        'imdb': '0',
                        'tvdb': '0',
                        'next': _next,
                        'duration': '0'  # Will be filled by worker()
                    }
                    self.list.append(movie_item)
                except Exception as e:
                    c.log(f"[Movies] Error processing item: {e}")
                    continue

            if c.devmode:
                c.log(f"[Movies] Built list with {len(self.list)} items")
                if self.list:
                    c.log(f"[Movies] First item: {self.list[0]}")

            # Enrich items with metadata
            # c.log(f"[Movies] About to call worker to enrich {len(self.list)} items")
            self.worker()
            # c.log(f"[Movies] Worker completed, list now has {len(self.list)} items")

            # Display movie items
            # c.log(f"[Movies] About to call movie_directory with {len(self.list)} items")
            self.movie_directory(self.list)
            # c.log(f"[Movies] Returned from movie_directory")

        except Exception as e:
            c.log(f'[Movies] Error in _advanced_search_with_postfilter: {e}')
            c.log(f'[Movies] Traceback: {traceback.format_exc()}')

    def saved_filters_list(self):
        """Show list of saved filters for movies"""
        try:
            from resources.lib.modules import advanced_search as adv_search

            manager = adv_search.FilterManager()
            filters = manager.list_filters('movie')

            # Always show "New Advanced Search" button
            navigator.navigator.addDirectoryItem(
                90223,
                'movieAdvancedSearch',
                'search.png',
                'DefaultMovies.png'
            )

            # Show saved filters if any exist
            for filter_id, filter_name in filters:
                cm = [
                    (32070, f'movieDeleteFilter&id={filter_id}'),  # Delete
                    (90224, f'movieAdvancedSearch&filter_id={filter_id}')
                ]

                navigator.navigator.addDirectoryItem(
                    filter_name,
                    f'movieExecuteFilter&id={filter_id}',
                    'search.png',
                    'DefaultMovies.png',
                    context=cm
                )

            # Add "Clear all filters" option if there are any filters
            if filters:
                navigator.navigator.addDirectoryItem(
                    32604,  # "Clear search history..."
                    'movieClearAllFilters',
                    'tools.png',
                    'DefaultAddonProgram.png'
                )

            navigator.navigator.endDirectory()

        except Exception as e:
            c.log(f'[Movies] Error in saved_filters_list: {e}')

    def execute_saved_filter(self, filter_id):
        """Execute a saved filter by ID"""
        try:
            from resources.lib.modules import advanced_search as adv_search

            manager = adv_search.FilterManager()
            filter_data = manager.get_filter(filter_id)

            if filter_data:
                self.advanced_search_execute(filter_data)
            else:
                c.infoDialog('Filter not found', sound=True, icon='ERROR')

        except Exception as e:
            c.log(f'[Movies] Error in execute_saved_filter: {e}')

    def delete_saved_filter(self, filter_id):
        """Delete a saved filter by ID"""
        try:
            from resources.lib.modules import advanced_search as adv_search

            yes = control.yesnoDialog('Delete this filter?')
            if not yes:
                return

            manager = adv_search.FilterManager()
            manager.delete_filter(filter_id)

        except Exception as e:
            c.log(f'[Movies] Error in delete_saved_filter: {e}')

    def clear_all_saved_filters(self):
        """Clear all saved filters for movies"""
        try:
            from resources.lib.modules import advanced_search as adv_search

            yes = control.yesnoDialog('Delete all saved filters?', 'This action cannot be undone.')
            if not yes:
                return

            manager = adv_search.FilterManager()
            count = manager.delete_all_filters('movie')

            c.infoDialog(f'Deleted {count} filter(s)', sound=True, icon='INFO')
            control.refresh()

        except Exception as e:
            c.log(f'[Movies] Error in clear_all_saved_filters: {e}')



    def person(self):
        """
        Prompts for a person name, then Container.Update into moviePersons.

        moviePerson is is_folder=False (dialog handle). Listing results via
        self.persons() here fails the same way title search used to — navigate
        into moviePersons&url=… on a real folder handle instead.
        """
        try:
            control.idle()
            search_header = c.lang(32010)
            keyboard = control.keyboard('', search_header)
            keyboard.doModal()
            search_query = keyboard.getText() if keyboard.isConfirmed() else None

            if not search_query:
                return

            search_query = (search_query or '').strip().lower()
            if not search_query:
                return

            # Keep the typed name for TMDB person search (do not strip articles).
            # replace=True: Back skips moviePerson dialog (else keyboard loops).
            url = self.person_link % quote_plus(search_query)
            control.execute(
                f'Container.Update({sys.argv[0]}?action=moviePersons&url={quote_plus(url)},replace)'
            )
        except Exception as e:
            c.log(f'Error in person method: {e}')
        finally:
            control.finish_dialog_route()


# TC 2/01/19 started

    #####cm#
    # Completely redone for compatibility with tmdb
    #
    def genres(self):
        # TMDB genre IDs — matches the TMDB discover/movie genre_link
        genres = [
            {"id": 28, "name": "Action"},
            {"id": 12, "name": "Adventure"},
            {"id": 16, "name": "Animation"},
            {"id": 35, "name": "Comedy"},
            {"id": 80, "name": "Crime"},
            {"id": 99, "name": "Documentary"},
            {"id": 18, "name": "Drama"},
            {"id": 10751, "name": "Family"},
            {"id": 14, "name": "Fantasy"},
            {"id": 36, "name": "History"},
            {"id": 27, "name": "Horror"},
            {"id": 10402, "name": "Music"},
            {"id": 9648, "name": "Mystery"},
            {"id": 10749, "name": "Romance"},
            {"id": 878, "name": "Science Fiction"},
            {"id": 53, "name": "Thriller"},
            {"id": 10752, "name": "War"},
            {"id": 37, "name": "Western"}
        ]
        # Trakt genre slugs (old, kept for reference — only ~2 pages since Trakt trending pool is tiny)
        # genres = [
        #     {"slug": "action", "name": "Action"}, {"slug": "adventure", "name": "Adventure"},
        #     {"slug": "animation", "name": "Animation"}, {"slug": "anime", "name": "Anime"},
        #     {"slug": "comedy", "name": "Comedy"}, {"slug": "crime", "name": "Crime"},
        #     {"slug": "documentary", "name": "Documentary"}, {"slug": "drama", "name": "Drama"},
        #     {"slug": "family", "name": "Family"}, {"slug": "fantasy", "name": "Fantasy"},
        #     {"slug": "history", "name": "History"}, {"slug": "horror", "name": "Horror"},
        #     {"slug": "music", "name": "Music"}, {"slug": "mystery", "name": "Mystery"},
        #     {"slug": "romance", "name": "Romance"}, {"slug": "science-fiction", "name": "Science Fiction"},
        #     {"slug": "thriller", "name": "Thriller"}, {"slug": "war", "name": "War"},
        #     {"slug": "western", "name": "Western"}
        # ]

        for i in genres:
            url = self.tmdb_documentary_link if i['id'] == 99 else self.genre_link % i['id']
            self.list.append(
                {
                    'name': cleangenre.lang(i['name'], self.lang),
                    'url': url,
                    'image': 'genres.png',
                    'action': 'movies'
                })

        self.addDirectory(self.list)
        return self.list

    def languages(self):
        language_data = [
            ('Arabic', 'ar'),
            ('Bosnian', 'bs'),
            ('Bulgarian', 'bg'),
            ('Chinese', 'zh'),
            ('Croatian', 'hr'),
            ('Dutch', 'nl'),
            ('English', 'en'),
            ('Finnish', 'fi'),
            ('French', 'fr'),
            ('German', 'de'),
            ('Greek', 'el'),
            ('Hebrew', 'he'),
            ('Hindi', 'hi'),
            ('Hungarian', 'hu'),
            ('Icelandic', 'is'),
            ('Italian', 'it'),
            ('Japanese', 'ja'),
            ('Korean', 'ko'),
            ('Macedonian', 'mk'),
            ('Norwegian', 'no'),
            ('Persian', 'fa'),
            ('Polish', 'pl'),
            ('Portuguese', 'pt'),
            ('Punjabi', 'pa'),
            ('Romanian', 'ro'),
            ('Russian', 'ru'),
            ('Serbian', 'sr'),
            ('Slovenian', 'sl'),
            ('Spanish', 'es'),
            ('Swedish', 'sv'),
            ('Turkish', 'tr'),
            ('Ukrainian', 'uk')
        ]

        for language_name, language_code in language_data:
            self.list.append({
                'name': language_name,
                'url': self.language_link % language_code,
                'image': 'international.png',
                'action': 'movies'
            })
        self.addDirectory(self.list)
        return self.list


    def certifications(self):
        """
        Create directory menu for US motion picture rating certifications.

        Ratings: G, PG, PG-13, R, NC-17
        TMDB API format: certification code (e.g., G, PG, PG-13)
        """
        # Define certifications: (code, display_name, icon)
        certificates = [
            ('G', 'G - General Audiences', 'certificates.png'),
            ('PG', 'PG - Parental Guidance Suggested', 'certificates.png'),
            ('PG-13', 'PG-13 - Parents Strongly Cautioned', 'certificates.png'),
            ('R', 'R - Restricted', 'certificates.png'),
            ('NC-17', 'NC-17 - Adults Only', 'certificates.png')
        ]

        self.list = [
            {
                'name': f'{color("[B]¤[/B]", 'header')} [B]{color(name, 'header')}[/B] {color("[B]¤[/B]", 'header')}',
                'url': self.certification_link % code,
                'image': icon,
                'action': 'movies'
            }
            for code, name, icon in certificates
        ]

        self.addDirectory(self.list)
        return self.list

    def years(self):
        year = self.datetime.strftime('%Y')

        for i in range(int(year)-0, 1900, -1):
            self.list.append({
                'name': str(i),
                'url': self.year_link % (str(i)),
                'image': 'years.png',
                'action': 'movies'
                })
        self.addDirectory(self.list)
        return self.list

    def persons(self, url) -> list:
        """
        Retrieve a list of persons (TMDB) and prepare them for directory display.

        - If url is None, use the default popular-persons endpoint (self.persons_link).
        - Ensures self.list is reset to avoid duplicates.
        - Validates tmdb_person_list return value and normalizes to a list.
        - Adds an 'action' key to each item only if missing/appropriate.
        - Returns a list (never None).
        """
        try:
            # Ensure we start from a clean list to avoid duplicates
            self.list = []

            # Choose the URL to fetch
            fetch_url = self.persons_link if not url else url

            # tmdb_person_list appends into self.list and returns it,
            # but wrap in try/except in case of unexpected errors.
            try:
                result = self.tmdb_person_list(fetch_url)
            except Exception as exc:
                c.log(f"[Movies @ persons] tmdb_person_list failed for {fetch_url}: {exc}")
                result = None

            # Normalize result to a list
            if result is None:
                self.list = []
            elif isinstance(result, list):
                self.list = result
            else:
                # If result is a single dict, wrap it; if it's iterable, try to convert
                if isinstance(result, dict):
                    self.list = [result]
                else:
                    try:
                        self.list = list(result)
                    except Exception:
                        c.log(f"[Movies @ persons] Unexpected result type from tmdb_person_list: {type(result)}")
                        self.list = []

            # Ensure each item has an action and minimal fields expected by callers/UI
            for item in self.list:
                if isinstance(item, dict):
                    item.setdefault('action', 'movies')
                    item.setdefault('name', item.get('name', 'Unknown'))
                    # ensure image/thumb/poster keys exist to avoid later KeyError
                    item.setdefault('image', item.get('image', c.addon_poster()))
                    item.setdefault('poster', item.get('poster', item.get('image')))
                    item.setdefault('thumb', item.get('thumb', item.get('image')))
                else:
                    c.log(f"[Movies @ persons] Skipping non-dict item in person list: {repr(item)}")

            # Only call addDirectory if we have items
            if self.list:
                self.addDirectory(self.list)
                return self.list

            return []
        except Exception as e:  # keep top-level safety
            c.log(f"[Movies @ persons] Unexpected error: {traceback.format_exc()}")
            return []

    def userlists(self) -> None:
        try:
            userlists = []
            activity = 0
            if trakt.get_trakt_credentials_info():
                activity = trakt.getActivity_from_db()

            try:
                if not trakt.get_trakt_credentials_info():
                    raise Exception()
                try:
                    if activity > cache.timeout(self.trakt_user_list, self.traktlists_link, self.trakt_user):
                        raise Exception()
                    userlists += cache.get(self.trakt_user_list, 720, self.traktlists_link, self.trakt_user)
                except Exception:
                    userlists += cache.get(self.trakt_user_list, 0, self.traktlists_link, self.trakt_user)
            except Exception as e:
                failure = traceback.format_exc()
                c.log(f'[Movies @ userlists] Traceback: {failure}')
                c.log(f'[Movies @ userlists] Exception: {e}')
            try:
                self.list = []
                if trakt.get_trakt_credentials_info() is False:
                    raise Exception()
                try:
                    if activity > cache.timeout(self.trakt_user_list, self.traktlikedlists_link, self.trakt_user):
                        raise Exception()
                    userlists += cache.get(self.trakt_user_list, 720, self.traktlikedlists_link, self.trakt_user)
                except Exception:
                    userlists += cache.get(self.trakt_user_list, 0, self.traktlikedlists_link, self.trakt_user)
            except Exception:
                pass

            self.list = userlists
            for i in range(len(self.list)):
                self.list[i].update({'image': 'userlists.png', 'action': 'movies'})
            self.addDirectory(self.list, queue=True)
            return self.list
        except Exception as e:
            failure = traceback.format_exc()
            c.log(f'[Movies @ userlists] Traceback: {failure}')
            c.log(f'[Movies @ userlists] Exception: {e}')
            pass

    def _fetch_trakt_curated_list_raw(self, path: str, page: int = 1) -> list:
        """Fetch ONE page of a Trakt user list for Kodi-level pagination.

        Returns exactly `limit` items (or fewer on the last page) so the caller
        can show a Next Page button.  One API call per Kodi page = fast every time.
        """
        limit = 100
        base_path = path.split('?', 1)[0]
        query = urlencode({'limit': limit, 'page': page, 'extended': 'full'})
        batch = trakt.getTraktAsJson(f'{base_path}?{query}') or []
        if not isinstance(batch, list):
            return []
        if batch:
            c.log(f"[Movies @ trakt_list] Fetched {len(batch)} item(s) from page {page} of {base_path}")
        return batch

    def _fetch_dead_trakt_list_replacement(self, replacement: dict, user: str) -> list:
        """Fallback when a curated Trakt user list is empty, private, or gone."""
        try:
            if replacement.get('type') == 'trakt' and replacement.get('endpoint') == 'boxoffice':
                c.log(f"[Movies @ trakt_list] Using replacement Trakt boxoffice -> {self.boxoffice_link}")
                return cache.get(self.trakt_list, self.CACHE_TTL_2DAY, self.boxoffice_link, user) or []

            if replacement.get('type') == 'tmdb' and replacement.get('endpoint'):
                ep = replacement.get('endpoint')
                link_attr = ep if ep.endswith('_link') else f"{ep}_link"
                url_link = getattr(self, link_attr, None)
                if not url_link:
                    c.log(f"[Movies @ trakt_list] Replacement endpoint attribute not found: {link_attr}")
                    return []
                c.log(f"[Movies @ trakt_list] Using replacement TMDB endpoint {link_attr} -> {url_link}")
                try:
                    return cache.get(self.tmdb_list, 0.25, url_link) or []
                except Exception as e:
                    c.log(f"[Movies @ trakt_list] Replacement TMDB fetch failed: {e}")
                    return self.tmdb_list(url_link) or []
        except Exception as e:
            c.log(f"[Movies @ trakt_list] Replacement fetch failed: {e}")
        return []

    def trakt_list(self, url, user):
        try:
            self.list = []
            is_ranked_list = '/lists/' in url
            #because we are also handling user_lists, we need to check if the url is a trakt list
            #a user list will always have a user in the url
            if '/lists/' in url:
                # list url = https://trakt.tv/lists/5308818
                if url.startswith('https://'):
                    #strip the first part until the next / and keep the rest after that
                    u = url.split('/', 3)[3]
                # Extract page number from the URL for Kodi-level pagination
                _q = dict(parse_qsl(urlsplit(url).query))
                _list_page = int(_q.get('page', 1))
                result = self._fetch_trakt_curated_list_raw(u, page=_list_page)
            else:
                q = dict(parse_qsl(urlsplit(url).query))
                q.update({'extended': 'full'})
                q = (urlencode(q)).replace('%2C', ',')
                u = url.replace('?' + urlparse(url).query, '') + '?' + q
                result = trakt.getTraktAsJson(u)

            # Capture pagination info from last Trakt API call
            self.pagination = trakt.get_pagination_info()

            if not result:
                c.log('No results found in Trakt List')

                # Attempt replacement via mapping skeleton (configurable)
                try:
                    from ..modules import list_mappings
                    replacement = list_mappings.get_replacement_for_trakt_url(u)
                    if replacement:
                        c.log(f"[Movies @ trakt_list] Replacement mapping found for {u}: {replacement}")
                        replacement_list = self._fetch_dead_trakt_list_replacement(replacement, user)
                        if replacement_list:
                            return replacement_list
                except Exception as e:
                    c.log(f"[Movies @ trakt_list] Error while looking up replacement mapping: {e}")

            else:
                pass

            items = []

            if result:
                items.extend(i for i in result if 'movie' in i)

            if not items:
                items = result
        except Exception as e:
            failure = traceback.format_exc()
            c.log(f'[Movies @ trakt_list] Traceback: {failure}')
            c.log(f'[Movies @ trakt_list] Exception: {e}')
            return []  # Return empty list instead of None

        if items is None:
            return []

        try:
            _CURATED_PAGE_LIMIT = 100
            if '/lists/' in url:
                # Kodi-level pagination for curated Trakt lists: one API call per page.
                # Check raw result length (not filtered items) — a partial raw page = last page.
                if len(result or []) >= _CURATED_PAGE_LIMIT:
                    _base = url.rstrip('?').split('?')[0]
                    next_page_url = f'{_base}?page={_list_page + 1}'
                else:
                    next_page_url = ''
            elif 'page' in url:
                query = dict(parse_qsl(urlsplit(url).query))
                if int(query['limit']) != len(items or []):
                    next_page_url = ''
                else:
                    query['page'] = str(int(query['page']) + 1)
                    next_page_url = url.replace('?' + urlparse(url).query, '') + '?' + urlencode(query)
            else:
                next_page_url = ''

        except Exception as e:
            failure = traceback.format_exc()
            c.log(f'[Movies @ trakt_list] Traceback: {failure}')
            c.log(f'[Movies @ trakt_list] Exception: {e}')

        def add_item(item):
            progress = item.get('progress', 0)

            item = item.get('movie') if 'movie' in item else item
            title = item.get('title')
            title = client.replaceHTMLCodes(title)

            year = item.get('year', '0')
            year = re.sub(r'[^0-9]', '', str(year))

            # Personal Trakt lists show everything the user saved; anticipated allows future years too.
            _personal_trakt = '/users/me/watchlist/' in url or '/users/me/collection/' in url
            if 'anticipated' not in url and not _personal_trakt:
                if int(year) > int((self.datetime).strftime('%Y')):
                    raise Exception()

            imdb = item.get('ids', {}).get('imdb', '')
            imdb = 'tt' + re.sub(r'[^0-9]', '', str(imdb)) if imdb else '0'
            tmdb = str(item.get('ids', {}).get('tmdb', '0'))


            release_date = item.get('released', '')
            if release_date:
                try:
                    premiered = re.compile(r'(\d{4}-\d{2}-\d{2})').findall(release_date)[0]
                except Exception:
                    premiered = '0'
            else:
                premiered = '0'

            genres = item.get('genres', [])
            genres = ' / '.join([g.title() for g in genres]) if genres else '0'
            duration = item.get('runtime', '90')
            if duration:
                duration = str(duration)

            rating = item.get('rating', '0.0')
            if rating and rating != '0.0':
                rating = str(rating)

            try:
                num_votes = int(item['votes'])
                votes = f'{num_votes:,}'
            except (KeyError, ValueError, TypeError):
                votes = '0'

            if 'anticipated' not in url and not _personal_trakt:
                if int(year) > int((self.datetime).strftime('%Y')):
                    raise ValueError()

            mpaa = item.get('certification', '0')
            overview = item.get('overview', c.lang(32623))
            overview = client.replaceHTMLCodes(overview)

            country_code = item.get('country_code', '0')
            if country_code != '0':
                country_code = country_code.upper()

            tagline = item.get('tagline', '0')
            if tagline != '0':
                tagline = client.replaceHTMLCodes(tagline)

            paused_at = item.get('paused_at', '0') or '0'
            paused_at = re.sub('[^0-9]+', '', paused_at)

            return({
                'title': title, 'originaltitle': title, 'year': year, 'progress': progress, 'premiered': premiered,
                'genre': genres, 'duration': duration, 'rating': rating, 'votes': votes,
                'mpaa': mpaa, 'plot': overview, 'tagline': tagline, 'imdb': imdb, 'tmdb': tmdb,
                'country': country_code, 'tvdb': '0', 'poster': '0', 'next': next_page_url,
                'paused_at': paused_at
                })

        if not items:
            return self.list if self.list else []

        try:
            result = []
            aantal = len(items)

            if is_ranked_list:
                for item in items:
                    try:
                        response = add_item(item)
                        if response is not None:
                            self.list.append(response)
                    except Exception as exc:
                        title = (item.get('movie') or item).get('title', '?')
                        c.log(f"[Movies @ trakt_list] Skipped {title!r}: {exc}")
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=c.get_max_threads(aantal, 50)) as executor:
                    futures = {executor.submit(add_item, item): item for item in items}

                    for future in concurrent.futures.as_completed(futures):
                        i = futures[future]
                        try:
                            response = future.result()
                            if response is not None:
                                self.list.append(response)
                        except Exception as exc:
                            title = (i.get('movie') or i).get('title', '?')
                            c.log(f"[Movies @ trakt_list] Skipped {title!r}: {exc}")
        except Exception as e:
            failure = traceback.format_exc()
            c.log(f'[Movies @ trakt_list] Traceback: {failure}')
            c.log(f'[Movies @ trakt_list] Exception: {e}')

        return self.list or []



    def trakt_user_list(self, url, user):
        try:
            if trakt.is_own_lists_index_url(url):
                items = trakt.get_all_user_lists()
            else:
                items = trakt.getTraktAsJson(url)
        except Exception:
            items = None

        for item in (items or []):
            try:
                try:
                    name = item['list']['name']
                except Exception:
                    name = item['name']
                name = client.replaceHTMLCodes(name)

                try:
                    url = (trakt.slug(item['list']['user']['username']), item['list']['ids']['slug'])
                except Exception:
                    url = ('me', item['ids']['slug'])
                url = self.traktlist_link % url
                url = str(url)

                self.list.append({'name': name, 'url': url, 'context': url})
            except Exception:
                pass

        self.list = sorted(self.list, key=lambda k: utils.title_key(k['name']))
        return self.list


    ####cm#
    # new def for tmdb lists
    def list_tmdb_list(self, url, tid=0):
        """
        Retrieves and processes a list of movies from a TMDB list URL.

        This function fetches a list of movies from the provided TMDB URL, processes each movie to extract relevant
        information such as title, original title, rating, votes, release date, and more, and appends the processed
        data to the `self.list` attribute. It handles pagination by constructing a URL for the next page of results.

        Args:
            url (str): The TMDB API URL to fetch the list of movies.
            tid (int, optional): The TMDB list ID to be embedded in the URL if not zero. Defaults to 0.

        Returns:
            list: A list of dictionaries, each containing information about a movie.
        """

        # Initialize a fresh list for this fetch
        self.list = []

        try:
            if tid != 0:
                url = url % tid

            result = http_client.tmdb_get_json(url, timeout=15) or {}
            items = result.get('items', result.get('results', []))
        except Exception as e:
            failure = traceback.format_exc()
            c.log(f'[Movies @ list_tmdb_list] Traceback: {failure}')
            c.log(f'[Movies @ list_tmdb_list] Exception: {e}')
            return

        try:
            page = int(result['page'])
            total = int(result['total_pages'])

            # Store pagination info for "Page X of Y" display
            if page > 0 and total > 0:
                self.pagination = {
                    'page': page,
                    'page_count': total
                }

            if page >= total or 'page=' not in url:
                raise Exception()
            _next = '%s&page=%s' % (url.split('&page=', 1)[0], page+1)
        except Exception:
            _next = ''

        for item in items:
            try:
                tmdb = str(item.get('id', '0'))
                title = item.get('title', '')

                originaltitle = item.get('original_title', title) if item.get('original_title') else title
                rating = str(item.get('vote_average', '0'))
                votes = str(item.get('vote_count', '0'))
                premiered = item.get('release_date') or '0'
                year_match = re.findall(r'(\d{4})', premiered)
                year = year_match[0] if year_match else '0'
                unaired = 'false'
                if premiered != '0' and int(re.sub('[^0-9]', '', str(premiered))) > int(re.sub('[^0-9]', '', str(self.today_date))):
                    unaired = 'true'
                    if self.showunaired != 'true':
                        raise ValueError()

                plot = item.get('overview') if item.get('overview') else (c.lang(32084) if hasattr(c, 'lang') and callable(c.lang) else '')
                #plot = c.lang(32084)

                # Prepend release date for JustWatch-equivalent endpoints (latest releases, premieres)
                if any(x in url for x in ('latest_releases', 'premieres')) and premiered != '0':
                    try:
                        from datetime import datetime
                        release_dt = datetime.strptime(premiered, '%Y-%m-%d')
                        formatted_date = release_dt.strftime('%B %d, %Y')  # e.g., "February 24, 2026"
                        plot = f"[B]Released:[/B] {formatted_date}\n\n{plot}"
                    except (ValueError, TypeError):
                        pass

                poster = '0'
                poster_path = item.get('poster_path', '')
                if poster_path:
                    poster = self.tmdb_img_link % (c.tmdb_postersize, poster_path)

                fanart = '0'
                backdrop_path = item.get('backdrop_path', '')
                if backdrop_path:
                    fanart = self.tmdb_img_link % (c.tmdb_fanartsize, backdrop_path)

                self.list.append({
                                'title': title, 'originaltitle': originaltitle, 'unaired': unaired,
                                'premiered': premiered, 'year': year, 'rating': rating,
                                'votes': votes, 'plot': plot, 'imdb': '0', 'tmdb': tmdb,
                                'tvdb': '0', 'fanart': fanart, 'poster': poster, 'next': _next
                                })

            except Exception as e:
                c.log(f"[Movies @ list_tmdb_list] Failed to parse item: {e}")
                pass

        return self.list

    def collection_list(self):
        # Always refresh the local mirror from Trakt — INSERT-only sync left removed items behind.
        trakt.get_trakt_collection('movies')
        collection = trakt.get_collection('movies') or []
        if len(collection) == 0:
            return

        for item in collection:
            try:
                tmdb = str(item['tmdb'])
                imdb = item['imdb']
                _trakt = str(item['trakt'])
                slug = item['slug']
                title = item['Title']
                year = str(item['Year'])

                self.list.append({
                                'title': title, 'year': year, 'imdb': imdb, 'tmdb': tmdb,
                                'trakt': _trakt, 'slug': slug
                                })
            except Exception as e:
                c.log(f"Exception raised in collection_list() with e = {e}")

        return self.list

    def movie_progress_list(self)-> list:
        """Return a list of dictionaries containing information about the user's
        movie progress on trakt.tv."""

        try:
            progress = trakt.get_trakt_progress('movie')

            for item in progress:
                tmdb = str(item['tmdb'])
                tvdb = str(item['tvdb'])
                imdb = item['imdb']
                trakt_id = str(item['trakt'])
                title = item['title']
                season = item['season']
                episode = item['episode']
                resume_point = item['resume_point']
                year = item['year']


                self.list.append({
                                'title': title, 'imdb': imdb, 'tmdb': tmdb, 'tvdb': tvdb,
                                'trakt': trakt_id, 'season': season, 'episode': episode,
                                'resume_point': resume_point, 'year': year

                                })
        except Exception as e:
            c.log(f"Exception raised in movie_progress_list() with e = {e}")
            #pass
        return self.list

    def tmdb_cast_list(self, url):
        try:
            result = http_client.tmdb_get_json(url, timeout=15) or {}
            items = result.get('cast', [])
        except Exception:
            return

        for item in items:

            try:
                tmdb = str(item['id'])
                title = item['title']
                originaltitle = item['original_title'] if 'original_title' in item else title


                rating = str(item.get('vote_average', '0'))

                vote_count = str(item.get('vote_count', '0'))


                premiered = item.get('release_date', '0')

                # Extract year from release_date
                if premiered and premiered != '0':
                    year = premiered.split('-')[0]
                else:
                    year = '0'
                    premiered = ''

                if year != '0' and year > self.today_date[:4]:
                    if self.showunaired != 'true':
                        raise Exception()

                plot = item.get('overview', '')

                poster_path = item.get('poster_path', '')
                if poster_path:
                    poster = self.tmdb_img_link.format(c.tmdb_postersize, poster_path)
                else:
                    poster = '0'

                backdrop_path = item.get('backdrop_path', '')
                if backdrop_path:
                    fanart = self.tmdb_img_link.format(c.tmdb_fanartsize, backdrop_path)
                else:
                    fanart = ''

                self.list.append({'title': title, 'originaltitle': originaltitle,
                                    'premiered': premiered, 'year': year, 'rating': rating,
                                    'votes': vote_count, 'plot': plot, 'imdb': '0', 'tmdb': tmdb,
                                    'tvdb': '0', 'fanart': fanart, 'poster': poster})
            except Exception as e:
                c.log(f'Exception raised: error = {e}')


        return self.list

    def trakt_collection(self, collection_type='movies'):
        collection = trakt.get_collection(collection_type)

        for item in collection:
            pass  # Debug statement removed

    def tmdb_list(self, url, tid=0, response=None):
        """Retrieves and processes a list of movies from a TMDB list URL.

        If `response` is provided (parsed JSON dict), it will be used directly
        instead of making an HTTP request. This enables conditional GET flows
        where the parsed JSON can be obtained from `cache.get_with_etag`.
        """
        try:
            # Start with a fresh list for a single TMDB list fetch
            self.list = []

            if int(tid) > 0 and '%s' in url:
                url = url % tid

            if response is None:
                response = http_client.tmdb_get_json(url, timeout=15) or {}

            items = response.get('items', response.get('results', response.get('parts', [])))
        except Exception as e:
            failure = traceback.format_exc()
            c.log(f'[Movies @ tmdb_list] Exception while fetching/parsing response: {failure}')
            return []

        try:
            page = int(response.get('page', 0))
            total_pages = int(response.get('total_pages', 0))

            # Store pagination info for "Page X of Y" display
            if page > 0 and total_pages > 0:
                self.pagination = {
                    'page': page,
                    'page_count': total_pages
                }

            if page >= total_pages or 'page=' not in url:
                raise ValueError("Invalid page or URL format")
            next_page_url = f"{url.split('&page=', 1)[0]}&page={page + 1}"

        except Exception:
            next_page_url = ''
        for item in items:

            try:
                movie_id = str(item.get('id', '0'))
                title = item.get('title', '')
                original_title = item.get('original_title', title)
                rating = str(item.get('vote_average', '0'))
                votes = str(item.get('vote_count', '0'))
                release_date = item.get('release_date', '0')
                year_match = re.findall(r'(\d{4})', release_date)
                year = year_match[0] if year_match else '0'
                unaired = 'false'
                if release_date != '0' and int(re.sub(r'\D', '', release_date)) > int(re.sub(r'\D', '', self.today_date)):
                    unaired = 'true'
                    if self.showunaired != 'true':
                        continue
                plot = item.get('overview') if item.get('overview') is not None else (c.lang(32084) if hasattr(c, 'lang') and callable(c.lang) else '')
                poster_path = item.get('poster_path', '')
                poster = self.tmdb_img_link % (c.tmdb_postersize, poster_path) if poster_path else '0'
                backdrop_path = item.get('backdrop_path', '')
                fanart = self.tmdb_img_link % (c.tmdb_fanartsize, backdrop_path) if backdrop_path else '0'

                self.list.append({
                    'title': title, 'originaltitle': original_title,
                    'premiered': release_date, 'year': year, 'rating': rating, 'votes': votes,
                    'plot': plot, 'imdb': '0', 'tmdb': movie_id, 'tvdb': '0', 'fanart': fanart,
                    'poster': poster, 'unaired': unaired, 'next': next_page_url
                })

            except Exception as e:
                c.log(f"[Movies @ tmdb_list] Failed to parse item: {e}")
                continue

        return self.list

    def tmdb_single_movie(self, url):
        """Fetch one TMDb movie detail URL and return a one-item movie list."""
        try:
            item = http_client.tmdb_get_json(url, timeout=15) or {}
            parsed = self._parse_tmdb_movie_dict(item)
            return [parsed] if parsed else []
        except Exception as exc:
            c.log(f'[Movies @ tmdb_single_movie] {exc}')
            return []

    def _parse_tmdb_movie_dict(self, item):
        if not item or not item.get('id'):
            return None
        movie_id = str(item.get('id', '0'))
        title = item.get('title', '')
        original_title = item.get('original_title', title)
        rating = str(item.get('vote_average', '0'))
        votes = str(item.get('vote_count', '0'))
        release_date = item.get('release_date', '0')
        year_match = re.findall(r'(\d{4})', release_date)
        year = year_match[0] if year_match else '0'
        unaired = 'false'
        if release_date != '0' and int(re.sub(r'\D', '', release_date)) > int(re.sub(r'\D', '', self.today_date)):
            unaired = 'true'
            if self.showunaired != 'true':
                return None
        plot = item.get('overview') if item.get('overview') is not None else (c.lang(32084) if hasattr(c, 'lang') and callable(c.lang) else '')
        poster_path = item.get('poster_path', '')
        poster = self.tmdb_img_link % (c.tmdb_postersize, poster_path) if poster_path else '0'
        backdrop_path = item.get('backdrop_path', '')
        fanart = self.tmdb_img_link % (c.tmdb_fanartsize, backdrop_path) if backdrop_path else '0'
        return {
            'title': title, 'originaltitle': original_title,
            'premiered': release_date, 'year': year, 'rating': rating, 'votes': votes,
            'plot': plot, 'imdb': '0', 'tmdb': movie_id, 'tvdb': '0', 'fanart': fanart,
            'poster': poster, 'unaired': unaired, 'next': ''
        }

    ####cm#
    # New def to hande tmdb persons listings
    def tmdb_person_list(self, url):
        try:
            result = http_client.tmdb_get_json(url, timeout=15) or {}
            items = result.get('results', [])
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            c.log(f'[Movies @ tmdb_person_list] Exception: {e}')
            return

        # Handle pagination
        try:
            page = int(result.get('page', 1))
            total = int(result.get('total_pages', 1))

            # Store pagination info for "Page X of Y" display
            if page > 0 and total > 0:
                self.pagination = {
                    'page': page,
                    'page_count': total
                }

            if page >= total or 'page=' not in url:
                _next = ''
            else:
                _next = f"{url.split('&page=', 1)[0]}&page={page+1}"
        except Exception:
            _next = ''

        for item in items:
            try:
                name = item['name']
                _id = item['id']
                profile_img = item['profile_path'] if 'profile_path' in item else ''
                if profile_img:
                    image = self.tmdb_img_link % (c.tmdb_profilesize, profile_img)
                else:
                    image = c.addon_poster
                url = self.personmovies_link % _id
                self.list.append({'name': name, 'url': url, 'image': image, 'poster': image, 'thumb': image, 'next': _next})
            except Exception:
                pass

        return self.list

    def widget_worker(self):
        """Widget listing: SQLite metacache first, then capped TMDB for missing posters."""
        if not self.list:
            return

        self.meta = []
        total = len(self.list)
        for i in range(total):
            self.list[i].update({'metacache': False})

        self.list = metacache.fetch(self.list, self.lang, self.user)

        misses = [
            i for i in range(total)
            if self.list[i].get('poster', '0') in ('0', '', None)
        ]
        cached = total - len(misses)
        if not misses:
            c.log(f'[Movies] Widget worker: {total} items, all posters from cache')
            return

        cap = min(len(misses), 6)
        c.log(
            f'[Movies] Widget worker: {total} items, {cached} cached, '
            f'fetching {len(misses)} posters ({cap} workers)'
        )
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=cap) as executor:
                futures = {executor.submit(self.super_info, i): i for i in misses}
                for future in concurrent.futures.as_completed(futures):
                    i = futures[future]
                    try:
                        future.result()
                    except Exception as exc:
                        c.log(f"[Movies @ widget_worker] Error processing item {i}: {exc}")
        except Exception as e:
            failure = traceback.format_exc()
            c.log(f'[Movies @ widget_worker] Traceback: {failure}')
            c.log(f'[Movies @ widget_worker] Exception: {e}')

        if self.meta:
            metacache.insert(self.meta)

    def search_worker(self):
        """
        Light enrich for movie search results.

        TMDB search already supplies title/year/plot/poster. Skip metacache and
        full super_info — directory uses _light_listing (no Trakt cold-sync /
        per-row bookmark I/O). Fill IMDb via lightweight external_ids only
        (scrapers need tt*). Full enrich stays on sources/info.
        """
        self._light_listing = True
        total = len(self.list)
        if total == 0:
            c.infoDialog('List returned no relevant results', icon='INFO', sound=False)
            return

        import time as _time
        from ..apis.tmdb_api import imdb_from_tmdb_movie

        _t0 = _time.perf_counter()
        need = []
        for i in range(total):
            item = self.list[i] or {}
            imdb = str(item.get('imdb') or '0')
            tmdb = str(item.get('tmdb') or '0')
            if imdb in ('0', 'None', '') and tmdb not in ('0', 'None', ''):
                need.append(i)

        filled = 0
        if need:
            workers = min(len(need), 12)
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(imdb_from_tmdb_movie, self.list[i].get('tmdb')): i
                    for i in need
                }
                for future in concurrent.futures.as_completed(futures):
                    i = futures[future]
                    try:
                        imdb = future.result() or '0'
                        if imdb and imdb != '0':
                            self.list[i]['imdb'] = imdb
                            filled += 1
                    except Exception as exc:
                        c.log(f'[Movies] search_worker imdb fill item {i}: {exc}')

        c.log(
            f'[Movies] search_worker light path n={total} '
            f'imdb_need={len(need)} imdb_filled={filled} '
            f'{(_time.perf_counter() - _t0):.2f}s'
        )
    def worker(self, level=0):


        self.meta = []
        total = len(self.list)

        if total == 0:
            c.infoDialog('List returned no relevant results', icon='INFO', sound=False)
            return

        for i in range(total):
            self.list[i].update({'metacache': False})

        self.list = metacache.fetch(self.list, self.lang, self.user)

        try:
            result = []
            #cm - changed worker 21-04-2025
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(total, 20)) as executor:
                if level == 1:
                    futures = {executor.submit(self.no_info, i): i for i in range(total)}
                else:
                    futures = {executor.submit(self.super_info, i): i for i in range(total)}

                # Wait for all futures to complete to ensure all items have TMDB data before directory display
                for future in concurrent.futures.as_completed(futures):
                    i = futures[future]
                    try:
                        # Just wait for completion, super_info updates self.list[i] in place
                        future.result()
                    except Exception as exc:
                        c.log(f"[Movies @ worker] Error processing item {i}: {exc}")
        except Exception as e:
            failure = traceback.format_exc()
            c.log(f'[Movies @ worker] Traceback: {failure}')
            c.log(f'[Movies @ worker] Exception: {e}')
            pass


        # cm changed worker - 2024-05-14
        #for r in range(0, total, 40): #cm increment 40 but why?
        #    threads = []
        #    for i in range(r, r+40):
        #        if i < total:
        #            if level == 1:
        #                threads.append(workers.Thread(self.no_info(i)))
        #            else:
        #                threads.append(workers.Thread(self.super_info(i)))
        #    [i.start() for i in threads]
        #    [i.join() for i in threads]
            #for thread in threads:
            #    thread.start()
            #for thread in threads:
            #    thread.join()

        if self.meta:
            metacache.insert(self.meta)
    def no_info(self, i) -> None:
        return

    def super_info(self, i) -> None:
        '''
        Filling missing pieces
        '''
        try:
            if self.list[i].get('metacache') is True:
                # Stale/slim cache can have art but no cast/director/writer/mpaa — refresh those.
                _cast = self.list[i].get('castwiththumb')
                _has_cast = isinstance(_cast, list) and bool(_cast)

                def _has_people(value):
                    if not value or value in ('0', 'None'):
                        return False
                    if isinstance(value, (list, tuple)):
                        return any(bool(x) and str(x) not in ('0',) for x in value)
                    return True

                if (
                    _has_cast
                    and _has_people(self.list[i].get('director'))
                    and _has_people(self.list[i].get('writer'))
                    and not self._mpaa_gap(self.list[i].get('mpaa'))
                ):
                    return

            lst = self.list[i]
            imdb = lst['imdb'] if 'imdb' in lst else '0'
            tmdb = lst['tmdb'] if 'tmdb' in lst else '0'
            list_title = lst['title']

            if imdb == '0' and tmdb != '0':
                try:
                    from ..apis.tmdb_api import imdb_from_tmdb_movie
                    imdb = imdb_from_tmdb_movie(tmdb) or '0'
                except Exception:
                    imdb = '0'


            if tmdb == '0' and imdb != '0':
                try:
                    url = self.tmdb_by_imdb % imdb
                    result = http_client.tmdb_get_json(url, timeout=15) or {}
                    movie_results = result.get('movie_results') or []
                    if movie_results:
                        movie_result = movie_results[0]
                        tmdb = movie_result.get('id') or '0'
                        tmdb = str(tmdb) if tmdb != '0' else '0'
                except Exception:
                    pass

            _id = tmdb if tmdb != '0' else imdb
            if _id in ['0', None]:
                raise Exception()


            en_url = self.tmdb_api_link % _id
            trans_url = f'{en_url},translations'
            url = en_url if self.lang == 'en' else trans_url

            item = http_client.tmdb_get_json(url, timeout=15) or {}

            if imdb == '0':
                imdb =  item.get('external_ids', {}).get('imdb_id') if item.get('external_ids', {}).get('imdb_id', '').startswith('tt') else '0'

            # TMDB movie details have no top-level mpaa — use release_dates (US theatrical)
            mpaa = self._mpaa_from_tmdb(item)
            if self._mpaa_gap(mpaa):
                mpaa = lst.get('mpaa') if not self._mpaa_gap(lst.get('mpaa')) else '0'

            original_language = item.get('original_language', '')

            if self.lang == 'en':
                en_trans_item = None
            else:
                try:
                    translations = item['translations']['translations']
                    en_trans_item = [x['data'] for x in translations if x['iso_639_1'] == 'en'][0]
                except Exception:
                    en_trans_item = {}

            en_trans_item = {}

            name = item.get('title', '')
            original_title = item.get('original_title', '')
            en_trans_name = (
                en_trans_item.get('title', '')
                if en_trans_item is not None and self.lang != 'en'
                else None
            )

            if self.lang == 'en':
                title = label = name
            else:
                title = en_trans_name or original_title
                label = name if original_language == self.lang else en_trans_name or name
            if not title:
                title = list_title
            if not label:
                label = list_title

            plot = item.get('overview') or (lst['plot'] if 'plot' in lst else c.lang(32623))

            tagline = item.get('tagline') or '0'

            if self.lang != 'en':
                if plot == '0':
                    if en_plot := en_trans_item.get('overview', ''):
                        plot = en_plot

                if tagline == '0':
                    if en_tagline := en_trans_item.get('tagline', ''):
                        tagline = en_tagline

            premiered = item.get('release_date') or '0'

            try:
                _year = re.findall(r'(\d{4})', premiered)[0]
            except Exception:
                _year = ''
            if not _year:
                _year = '0'
            year = lst['year'] if lst['year'] != '0' else _year

            status = item.get('status') or '0'

            try:
                studio = item['production_companies'][0]['name']
            except Exception:
                studio = ''
            if not studio:
                studio = '0'

            try:
                genre = item['genres']
                genre = [d['name'] for d in genre]
                genre = ' / '.join(genre)
            except Exception:
                genre = ''
            if not genre:
                genre = '0'

            try:
                countries = item.get('production_countries')
                country = [c['name'] for c in countries]
                country = ' / '.join(country)
            except Exception:
                country = ''
            if not country:
                country = '0'


            duration = str(item.get('runtime', "90"))
            rating = item.get('vote_average', '0')
            votes = item.get('vote_count', '0') #votes ?

            castwiththumb = []
            try:
                credits = item.get('credits') if isinstance(item.get('credits'), dict) else {}
                cast = credits.get('cast') or []
                if isinstance(cast, list):
                    for idx, person in enumerate(cast[:30]):
                        if not isinstance(person, dict):
                            continue
                        name = (person.get('name') or '').strip()
                        if not name:
                            continue
                        profile = person.get('profile_path') or ''
                        icon = self.tmdb_img_link % (c.tmdb_profilesize, profile) if profile else ''
                        castwiththumb.append({
                            'name': name,
                            'role': (person.get('character') or '').strip(),
                            'order': int(person.get('order') if person.get('order') is not None else idx),
                            'thumbnail': icon,
                        })
            except Exception as e:
                c.log(f"[Movies @ super_info] cast error: {e}")

            crew = []
            try:
                credits = item.get('credits') if isinstance(item.get('credits'), dict) else {}
                crew = credits.get('crew') or []
                if not isinstance(crew, list):
                    crew = []
            except Exception:
                crew = []

            director = '0'
            writer = '0'
            if crew:
                try:
                    director = ', '.join(
                        d['name'] for d in crew
                        if isinstance(d, dict) and d.get('job') == 'Director' and d.get('name')
                    ) or '0'
                except (KeyError, TypeError, AttributeError):
                    director = '0'

                try:
                    writer = ', '.join(
                        w['name'] for w in crew
                        if isinstance(w, dict)
                        and w.get('job') in ('Writer', 'Screenplay', 'Author', 'Novel')
                        and w.get('name')
                    ) or '0'
                except (KeyError, TypeError, AttributeError):
                    writer = '0'

            lst_poster = lst['poster'] if 'poster' in lst else ''

            poster_path = item.get('poster_path', '')
            if poster_path:
                tmdb_poster = self.tmdb_img_link % (c.tmdb_postersize, poster_path)
            else:
                tmdb_poster = "0"


            backdrop_path = item.get('backdrop_path', '')#backdrop_path

            if backdrop_path:
                tmdb_fanart = self.tmdb_img_link % (c.tmdb_fanartsize, backdrop_path)
            else:
                tmdb_fanart = '0'

            fanart_poster = fanart_fanart = ''
            banner = clearlogo = clearart = landscape = discart = '0'

            if imdb not in ['0', None]:
                tempart = fanart_tv.get_fanart_tv_art(imdb=imdb, tvdb='0', mediatype='movie') or {}
                if isinstance(tempart, dict):
                    fanart_poster = tempart.get('poster', '0')
                    fanart_fanart = tempart.get('fanart', '0')
                    banner = tempart.get('banner', '0')
                    clearlogo = tempart.get('clearlogo', '0')
                    clearart = tempart.get('clearart', '0')
                    landscape = tempart.get('landscape', '0')
                    discart = tempart.get('discart', '0')

            poster = tmdb_poster or fanart_poster or lst_poster
            fanart = tmdb_fanart or fanart_fanart

            item = {
                'title': title, 'originaltitle': title, 'year': year, 'imdb': imdb,
                'tmdb': tmdb, 'status': status, 'studio': studio, 'poster': poster,
                'banner': banner, 'fanart': fanart, 'fanart2': fanart_fanart, 'landscape': landscape,
                'discart': discart,'clearlogo': clearlogo, 'clearart': clearart,
                'premiered': premiered, 'genre': genre,
                'duration': duration, 'rating': rating, 'votes': votes, 'mpaa': mpaa,
                'director': director, 'writer': writer, 'castwiththumb': castwiththumb,
                'plot': plot, 'tagline': tagline
                }

            item = {k: v for k, v in item.items() if v != '0'}
            lst.update(item)

            meta = {
                'imdb': imdb, 'tmdb': tmdb, 'tvdb': '0', 'lang': self.lang,
                'user': self.user, 'item': item}
            self.meta.append(meta)
        except Exception as e:
            failure = traceback.format_exc()
            c.log(f'[Movies @ super_info] Traceback: {failure}')
            c.log(f'[Movies @ super_info] Exception: {e}')



    def movie_directory(self, items):
        '''create the directory'''
        c.log(f"[Movies] movie_directory called with {len(items) if items else 0} items")
        sysaddon = sys.argv[0]
        syshandle = int(sys.argv[1])

        if items is None or len(items) == 0:
            c.log(f"[Movies] Items is None or empty, closing directory")
            control.add_empty_directory_placeholder(syshandle)
            control.close_directory(
                handle=syshandle,
                content_type='movies',
                cacheToDisc=False,
            )
            return
        addon_poster, addon_banner = c.addon_poster(), c.addon_banner()
        addon_fanart, setting_fanart = c.addon_fanart(), c.get_setting('fanart')
        addon_clearlogo, addon_clearart = c.addon_clearlogo(), c.addon_clearart()
        addon_discart = c.addon_discart()

        trakt_credentials = trakt.get_trakt_credentials_info()
        simkl_live = tracker.simkl_live()

        isplayable = 'true' if 'plugin' not in control.infoLabel( 'Container.PluginName') else 'false'
        light = bool(getattr(self, '_light_listing', False))
        if light:
            # Soft indicators only — never block search on Trakt/Simkl watched sync
            indicators = playcount.get_movie_indicators_soft()
            c.log(f'[Movies] movie_directory light listing n={len(items)} indicators={len(indicators) if indicators else 0}')
        else:
            indicators = playcount.get_movie_indicators(refresh=True) if action == 'movies' else playcount.get_movie_indicators()

        find_similar = c.lang(32100)
        playtrailer = c.lang(32062)
        playback_menu = c.lang(32063) if c.get_setting('hosts.mode') == '2' else c.lang(32064)
        watched_menu, unwatched_menu = playcount.watched_menu_labels()
        queue_menu = c.lang(32065)
        trakt_manager_menu = c.lang(32515)
        simkl_manager_menu = c.lang(90314)
        next_menu = c.lang(32053)
        addToLibrary = c.lang(32551)
        infoMenu = c.lang(32101)
        clear_resume_menu = c.lang(90237)
        current_container = quote_plus(sysaddon + sys.argv[2])

        for i in items:

            try:
                imdb = i['imdb']
                tmdb = i['tmdb']
                title = i['originaltitle']
                year = i['year']
                label = i['label'] if 'label' in i and i['label'] != '0' else title
                label = f"{label} ({year})"
                status = i['status'] if 'status' in i else '0'

                meta = {k: v for k, v in i.items() if v != '0'}

                # Normalize duration to SECONDS for accurate resume/remaining calculations
                duration_min = meta.get('duration') or i.get('duration')
                if not duration_min or duration_min == '0':
                    duration_min = 90
                else:
                    try:
                        duration_min = int(duration_min)
                    except (TypeError, ValueError):
                        duration_min = 90
                meta.update({'duration': str(duration_min * 60)})

                resume_seconds = 0.0
                if 'resume_point' in meta and meta['resume_point']:
                    val = float(meta['resume_point'])
                    duration = float(meta['duration']) if 'duration' in meta and meta['duration'] != '0' else 0

                    # Detect corrupted data: values between 0.01-100.0 are likely percentages stored incorrectly
                    # Real seconds would be > 100 for any movie longer than 1m40s
                    # Real percentages (decimals) would be 0.0-1.0
                    if 0.01 <= val < 1.0:
                        # Decimal percentage (0.49 = 49%), convert to seconds
                        if duration > 0:
                            resume_seconds = duration * val
                    elif 1.0 <= val <= 100.0:
                        # Suspicious: might be percentage stored as 1-100 instead of seconds
                        # Only treat as percentage if it would make sense (< 92% watched)
                        if duration > 0 and val < 92:
                            c.log(f"[Movies] Detected corrupted resume_point {val} for {imdb}, converting from percentage to seconds")
                            resume_seconds = duration * (val / 100.0)
                        else:
                            # Treat as seconds (might be a very short video)
                            resume_seconds = val
                    else:
                        # val > 100.0, definitely seconds
                        resume_seconds = val

                if not resume_seconds and not light:
                    # Get from bookmarks/Trakt (returns seconds) — skip on search
                    # listings (imdb often '0'; also avoids N× traktsync opens).
                    resume_seconds = float(bookmarks.get('movie', imdb=imdb, tmdb=tmdb))

                duration_secs = float(meta['duration']) if meta.get('duration') and meta['duration'] != '0' else 0.0
                fully_watched_by_progress = bookmarks.is_fully_watched_progress(resume_seconds, duration_secs)
                # Watched (cloud indicator or completed progress) must not keep a
                # resume chip — leftover 1s bookmarks from mark-watched hid overlay 7.
                watched_indicator = playcount.movie_has_indicator(indicators, imdb)
                if fully_watched_by_progress or watched_indicator:
                    resume_seconds = 0.0

                # offset is the actual seek position in seconds
                offset = resume_seconds

                # Calculate percentage for display and metadata
                percentage_played = 0.0
                if resume_seconds and duration_secs > 0:
                    percentage_played = (resume_seconds / duration_secs) * 100

                meta.update({'offset': offset})
                meta.update({'resume_point': resume_seconds})

                if resume_seconds and duration_secs > 0:
                    # Calculate remaining time
                    remaining_seconds = float(meta['duration']) - resume_seconds
                    remaining_minutes = remaining_seconds / 60.0
                    label += f' {color("(" + str(int(remaining_minutes)) + " min. remaining)", 'secondary')} '
                    #label += f' [COLOR FF56B6B0]({int(resume_point)}%)[/COLOR] '
                # Add cyan release date for future movies (anticipated/upcoming)
                try:
                    premiered = i.get('premiered', '0')
                    if premiered and premiered != '0':
                        # Check if this is a future release
                        premiered_digits = re.sub('[^0-9]', '', premiered)
                        if premiered_digits and len(premiered_digits) >= 8:
                            premiered_int = int(premiered_digits)
                            today_int = int(re.sub('[^0-9]', '', str(self.today_date)))
                            if premiered_int > today_int:
                                # Add the cyan date for future movies
                                try:
                                    from datetime import datetime as dt
                                    release_dt = dt.strptime(premiered, '%Y-%m-%d')
                                    formatted_date = release_dt.strftime('%b %d, %Y')
                                    label += f' {color("(" + str(formatted_date) + ")", 'secondary')}'
                                except Exception:
                                    pass
                except Exception:
                    pass

                # Apply unaired color formatting (if user has enabled showunaired)
                try:
                    premiered = i.get('premiered', '0')
                    if (premiered == '0' and status in ['Upcoming', 'In Production', 'Planned']) or \
                                (premiered and int(re.sub('[^0-9]', '', premiered)) > int(re.sub('[^0-9]', '', str(self.today_date)))):

                        # Same map as settings unaired.identify (COLOR_STRING_IDS / 32753+)
                        from resources.lib.modules.crewruntime import COLOR_STRING_IDS, COLOR_NAMES
                        try:
                            _unaired_idx = int(c.get_setting('unaired.identify') or 1)
                        except Exception:
                            _unaired_idx = 1
                        _unaired_idx = max(0, min(_unaired_idx, len(COLOR_STRING_IDS) - 1))
                        color_template = re.sub(
                            r"\][\w\s]*\[", "][I]%s[/I][",
                            c.lang(COLOR_STRING_IDS[_unaired_idx]),
                        )
                        if color_template and '%s' in color_template:
                            formatted_label = color_template % label
                        else:
                            cname = COLOR_NAMES[_unaired_idx]
                            if cname == 'nocolor':
                                formatted_label = f'[I]{label}[/I]'
                            else:
                                formatted_label = color(f'[I]{label}[/I]', role_for_name(cname))

                        label = formatted_label

                except Exception:
                    pass

                syslabel = quote_plus(f"{title} ({year})")
                if resume_seconds and percentage_played:
                    syslabel = quote_plus(f"{title} ({year}) [{int(percentage_played)}%]")
                systitle = quote_plus(title)
                systrailer = quote_plus(i['trailer']) if 'trailer' in i else '0'

                meta.update({'code': imdb, 'imdbnumber': imdb})
                meta.update({'tmdb_id': tmdb})
                meta.update({'imdb_id': imdb})
                meta.update({'mediatype': 'movie'})

                # duration already normalized to seconds earlier
                #meta.update({'genre': cleangenre.lang(meta['genre'], self.lang)})

                poster = i['poster'] if 'poster' in i and i['poster'] != '0' else addon_poster
                fanart = i['fanart'] if 'fanart' in i and i['fanart'] != '0' else addon_fanart
                banner = i['banner'] if 'banner' in i and i['banner'] != '0' else addon_banner
                landscape = i['landscape'] if 'landscape' in i and i['landscape'] != '0' else fanart
                clearlogo = i['clearlogo'] if 'clearlogo' in i and i['clearlogo'] != '0' else addon_clearlogo
                clearart = i['clearart'] if 'clearart' in i and i['clearart'] != '0' else addon_clearart
                discart = i['discart'] if 'discart' in i and i['discart'] != '0' else addon_discart

                poster = [i[x] for x in ['poster3', 'poster', 'poster2'] if i.get(x, '0') != '0']
                poster = poster[0] if poster else addon_poster

                meta['poster'] = poster

                # Trailer plugin URL must be inside play/sources meta (was set after sysmeta — missing on sources info)
                _trailer_art = quote_plus(json.dumps({'poster': poster, 'fanart': fanart}))
                if systrailer == '0':
                    trailer_url = (
                        f'{sysaddon}?action=trailer&name={systitle}&imdb={imdb}&tmdb={tmdb}'
                        f'&mediatype=movie&meta={_trailer_art}'
                    )
                else:
                    trailer_url = (
                        f'{sysaddon}?action=trailer&name={systitle}&url={systrailer}'
                        f'&imdb={imdb}&tmdb={tmdb}&mediatype=movie&meta={_trailer_art}'
                    )
                meta['trailer'] = trailer_url

                # Slim cast for play/sources URL (full thumbs blow past Kodi URL limits)
                _play_meta = dict(meta)
                _cast = _play_meta.get('castwiththumb')
                if isinstance(_cast, list) and _cast:
                    _play_meta['castwiththumb'] = [
                        {
                            'name': x.get('name'),
                            'role': x.get('role') or x.get('character') or '',
                            'order': x.get('order', i),
                        }
                        for i, x in enumerate(_cast[:25])
                        if isinstance(x, dict) and (x.get('name') or '').strip()
                    ]
                sysmeta = quote_plus(json.dumps(_play_meta))

                url = f'{sysaddon}?action=play&title={systitle}&year={year}&imdb={imdb}&tmdb={tmdb}&meta={sysmeta}&t={self.systime}'
                #url = f'{sysaddon}action=play&title={systitle}&year={year}&imdb={imdb}&tmdb={tmdb}&meta={sysmeta}&t={self.systime}'
                if isplayable == 'true':
                    # Home widget resolve cannot Directory/Container.Update - force Autoplay.
                    url = control.stamp_widget_autoplay(url)
                sysurl = quote_plus(url)

                # Build context menu in logical order (replaceItems=True removes system menus)
                cm = []

                # 1. Playback control
                cm.append((playback_menu, f'RunPlugin({sysaddon}?action=alter_sources&url={sysurl}&meta={sysmeta})'))

                # 2. Discovery - Related Content (TMDB similar)
                if tmdb and str(tmdb) not in ('0', ''):
                    cm.append((
                        find_similar,
                        f'Container.Update({sysaddon}?action=movies&url={quote_plus(self.related_link % tmdb)})',
                    ))

                # 3. Watched/Unwatched status
                try:
                    overlay = int(playcount.get_movie_overlay(indicators, imdb))
                    if fully_watched_by_progress:
                        overlay = 7
                    if overlay == 7:
                        cm.append((unwatched_menu, f'RunPlugin({sysaddon}?action=moviePlaycount&imdb={imdb}&query=6)'))
                        meta.update({'playcount': 1, 'overlay': 7})
                    else:
                        cm.append((watched_menu, f'RunPlugin({sysaddon}?action=moviePlaycount&imdb={imdb}&query=7)'))
                        meta.update({'playcount': 0, 'overlay': 6})
                except Exception:
                    pass

                # 5. Clear resume point (if applicable)
                if offset > 0:
                    cm.append((clear_resume_menu, f'RunPlugin({sysaddon}?action=movieClearBookmark&imdb={imdb}&redirect={current_container})'))

                # 6. Library management
                cm.append((addToLibrary, f'RunPlugin({sysaddon}?action=movieToLibrary&name={syslabel}&title={systitle}&year={year}&imdb={imdb}&tmdb={tmdb})'))

                # 7. Tracker manager (Trakt XOR Simkl)
                if simkl_live:
                    cm.append((simkl_manager_menu, f'RunPlugin({sysaddon}?action=simklManager&name={syslabel}&imdb={imdb}&tmdb={tmdb}&content=movie)'))
                elif trakt_credentials is True:
                    cm.append((trakt_manager_menu, f'RunPlugin({sysaddon}?action=traktManager&name={syslabel}&imdb={imdb}&tmdb={tmdb}&content=movie)'))

                try:
                    item = control.item(label=label, offscreen=True)
                except Exception:
                    item = control.item(label=label)

                art = {}
                art.update({
                    'icon': poster,
                    'thumb': poster,
                    'poster': poster,
                    **({'fanart': fanart} if setting_fanart == 'true' else {}),
                    'banner': banner,
                    'clearlogo': clearlogo,
                    'clearart': clearart,
                    'landscape': landscape,
                    'discart': discart
                })

                item.setArt(art)
                item.setProperty('IsPlayable', isplayable)

                item.setProperty('imdb_id', imdb)
                item.setProperty('tmdb_id', tmdb)
                #item.setInfo(type='Video', infoLabels=control.metadataClean(meta))

                meta['studio'] = c.string_split_to_list(meta['studio']) if 'studio' in meta else []
                meta['genre'] = c.string_split_to_list(meta['genre']) if 'genre' in meta else []
                meta['director'] = c.string_split_to_list(meta['director']) if 'director' in meta else []
                meta['writer'] = c.string_split_to_list(meta['writer']) if 'writer' in meta else []

                # Pass listitem to the infotagger module and specify tag type
                info_tag = ListItemInfoTag(item, 'video')
                infolabels = control.tagdataClean(meta)

                info_tag.set_info(infolabels)
                unique_ids = {'imdb': imdb, 'tmdb': str(tmdb)}
                info_tag.set_unique_ids(unique_ids)
                _cast = meta.get('castwiththumb')
                if isinstance(_cast, list) and _cast:
                    try:
                        info_tag.set_cast(_cast)
                    except Exception as cast_err:
                        c.log(f'[Movies @ movieDirectory] set_cast failed: {cast_err}')

                if(offset > 0):
                    info_tag.set_resume_point(meta, 'offset', 'duration', False)

                stream_info = {'codec': 'h264'}
                info_tag.add_stream_info('video', stream_info)  # (stream_details)

                item.addContextMenuItems(cm, replaceItems=True)

                control.addItem(handle=syshandle, url=url, listitem=item, isFolder=False)
            except Exception as e:
                failure = traceback.format_exc()
                c.log(f'[Movies @ movieDirectory] Traceback: {failure}')
                c.log(f'[Movies @ movieDirectory] Exception: {e}')
                pass

        try:
            url = items[0]['next']
            if url == '':
                raise ValueError('No next page URL found')

            icon = control.addonNext()
            url = '%s?action=moviePage&url=%s' % (sysaddon, quote_plus(url))

            # Enhanced pagination label: Show "Page X of Y" if info available
            label = next_menu
            if self.pagination and 'page' in self.pagination and 'page_count' in self.pagination:
                current_page = self.pagination['page']
                total_pages = self.pagination['page_count']
                # Show "Next Page (Page 4 of 259)" instead of just "Next Page"
                label = f"{next_menu} (Page {current_page + 1} of {total_pages})"

            try:
                item = control.item(label=label, offscreen=True)
            except Exception:
                item = control.item(label=label)

            item.setArt({
                'icon': icon, 'thumb': icon, 'poster': icon, 'banner': icon, 'fanart': addon_fanart
                })

            control.addItem(handle=syshandle, url=url, listitem=item, isFolder=True)
        except (Exception, ValueError):
            pass

        control.content(syshandle, 'movies')
        control.directory(syshandle, cacheToDisc=True)
        views.set_view('movies', {'skin.estuary': 55, 'skin.confluence': 500})

    def addDirectory(self, items, queue=False):
        if items is None or len(items) == 0:
            syshandle = int(sys.argv[1])
            control.add_empty_directory_placeholder(syshandle)
            control.close_directory(handle=syshandle, content_type='addons', cacheToDisc=False)
            return []

        sysaddon = sys.argv[0]
        syshandle = int(sys.argv[1])
        addonFanart, addonThumb, artPath = control.addonFanart(), control.addonThumb(), control.artPath()
        queueMenu = c.lang(32065)
        playRandom = c.lang(32535)
        addToLibrary = c.lang(32551)

        for i in items:
            try:
                name = i['name']

                plot = i.get('plot') or '[CR]'
                if i['image'].startswith('http'):
                    thumb = i['image']
                elif artPath is not None:
                    thumb = os.path.join(artPath, i['image'])
                else:
                    thumb = addonThumb

                url = '%s?action=%s' % (sysaddon, i['action'])

                if 'url' in i and i['url']:
                    url += '&url=%s' % quote_plus(i['url'])

                cm = [
                    (
                        playRandom,
                        f'RunPlugin({sysaddon}?action=random&rtype=movie&url={quote_plus(i["url"])}'
                    )
                ]

                # Note: "Play next" (Queue) removed - not compatible with source scraping architecture
                # if queue is True:
                #     cm.append((queueMenu, f'RunPlugin({sysaddon}?action=queueItem)'))

                if 'context' in i and i['context']:
                    cm.append((addToLibrary, f'RunPlugin({sysaddon}?action=moviesToLibrary&url={quote_plus(i["context"])})'))


                try:
                    item = control.item(label=name, offscreen=True)
                except Exception:
                    item = control.item(label=name)

                item.setArt({'icon': thumb, 'thumb': thumb, 'poster': thumb, 'fanart': addonFanart})
                item.setInfo(type='video', infoLabels={'plot': plot})

                # Note: System context menus cannot be removed via Python API (limitation since Kodi v17/2016)
                item.addContextMenuItems(cm)

                control.addItem(handle=syshandle, url=url, listitem=item, isFolder=True)
            except Exception:
                c.log('mov_addDir', 1)
                pass

        control.content(syshandle, 'movies')
        control.directory(syshandle, cacheToDisc=True)

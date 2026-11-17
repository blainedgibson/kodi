# -*- coding: utf-8 -*-

'''
 ***********************************************************
 * Midnight Add-on
 *
 *
 * @file tvshows.py
 * @package script.module.midnight
 *
 * @copyright (c) 2023, Midnight
 * @license GNU General Public License, version 3 (GPL-3.0)
 *
 * Updated on 2026-03-15
 *
 ********************************************************cm*
'''

import os
import sys
import re
import datetime
import json
import traceback
import concurrent.futures
import threading

import sqlite3 as database
from sqlite3 import OperationalError
from urllib.parse import quote, quote_plus, parse_qsl, urlparse, urlsplit, urlencode

import requests

from ..modules import trakt
from ..modules import simkl
from ..modules import tracker
from ..modules import keys
from ..modules import cleantitle
from ..modules import cleangenre
from ..modules import control
from ..modules import client
from ..modules import cache
from ..modules import metacache
from ..modules import playcount
from ..modules import workers
from ..modules import utils
from ..modules import fanart as fanart_tv
from ..modules import http_client
from ..indexers import navigator
from ..modules.listitem import ListItemInfoTag
from ..modules.crewruntime import c, COLOR_STRING_IDS
from ..models.tvshow import TVShow
from resources.lib.ui_theme import color




# safely parse query params from sys.argv[2] (ensure index exists)
# params = dict(parse_qsl(sys.argv[2].replace('?',''))) if len(sys.argv) > 1 else {}
query = sys.argv[2] if len(sys.argv) > 2 else ''
params = dict(parse_qsl(query.lstrip('?')))
action = params.get('action')


class TVShows:
    '''TV shows indexer for Midnight addon.'''
    def __init__(self):
        self.list = []
        self.artwork_cache = {}  # Cache artwork by show TMDB ID to avoid redundant fetches

        # Pagination info from last API call (for showing "Page X of Y")
        self.pagination = {}

        self.session = requests.Session()
        #self.artwork = artwork.artwork()

        self.imdb_link = 'https://www.imdb.com'
        self.trakt_link = 'https://api.trakt.tv'
        self.tvmaze_link = 'https://www.tvmaze.com'
        self.tmdb_link = 'https://api.themoviedb.org/3'
        self.logo_link = 'https://image.tmdb.org/t/p/original'
        self.tvdb_key = control.setting('tvdb.user')
        if not self.tvdb_key:
            self.tvdb_key = keys.tvdb_key

        #cm - correct and current date & time of the user, not utcnow with timedelta-5
        self.datetime = datetime.datetime.now()
        self.year = self.datetime.strftime('%Y')
        self.count = int(control.setting('page.item.limit'))
        self.items_per_page = str(control.setting('items.per.page')) or '20'
        self.trailer_source = control.setting('trailer.source') or '2'
        self.country = control.setting('official.country') or 'US'
        self.lang = control.apiLanguage()['tmdb'] or 'en'


        self.today_date = self.datetime.strftime('%Y-%m-%d')
        # ~15y floor for Documentary (genre 99) — matches Classy tmdb_discover
        self.years_15_ago = (self.datetime - datetime.timedelta(days=int(15 * 365.25))).strftime('%Y-%m-%d')
        self.specials = control.setting('tv.specials') or 'true'
        self.showunaired = control.setting('showunaired') or 'true'
        self.hq_artwork = control.setting('hq.artwork') or 'true'

        ####cm##
        # users & keys
        #
        self.fanart_tv_user = control.setting('fanart.tv.user')
        self.trakt_user = control.setting('trakt.user').strip()
        self.imdb_user = control.setting('imdb.user').replace('ur', '')
        self.tmdb_user = control.setting('tm.personal_user') or control.setting('tm.user') or keys.tmdb_key
        self.user = self.tmdb_user

        ####cm##
        # headers
        #
        self.fanart_tv_headers = {'api-key': keys.fanart_key}
        if self.fanart_tv_user:
            self.fanart_tv_headers.update({'client-key': self.fanart_tv_user})

        self.search_link = f'{self.trakt_link}/search/show?limit=20&page=1&query=%s'
        self.tvmaze_info_link = 'https://api.tvmaze.com/shows/%s'
        self.fanart_tv_art_link = 'http://webservice.fanart.tv/v3/tv/%s'
        self.fanart_tv_level_link = 'http://webservice.fanart.tv/v3/level'

        self.tmdb_link = 'https://api.themoviedb.org/3'
        self.tmdb_img_link = 'https://image.tmdb.org/t/p/%s%s'
        self.tmdb_img_prelink = 'https://image.tmdb.org/t/p/{}{}'

        ####cm##
        #trakt
        #
        self.trending_link = f'{self.trakt_link}/shows/trending?limit=40&page=1'
        self.trakthidden_link = f'{self.trakt_link}/users/hidden'  # base; trakthidden_list queries all sections
        self.traktlists_link = f'{self.trakt_link}/users/me/lists?limit=250'
        self.traktlikedlists_link = f'{self.trakt_link}/users/likes/lists?limit=1000000'
        self.traktlist_link = f'{self.trakt_link}/users/%s/lists/%s/items'
        self.traktcollection_link = f'{self.trakt_link}/users/me/collection/shows'
        self.traktwatchlist_link = f'{self.trakt_link}/users/me/watchlist/shows?limit=2000&page=1'
        self.traktfeatured_link = f'{self.trakt_link}/recommendations/shows?limit=40'
        self.trakt_related_link = f'{self.trakt_link}/shows/%s/related?limit=10'
        # New Trakt discovery endpoints for TV shows (added 2026-02-24)
        self.traktrecommendations_link = f'{self.trakt_link}/recommendations/shows?limit=40'
        self.traktpopular_link = f'{self.trakt_link}/shows/popular?limit=40'
        self.traktanticipated_link = f'{self.trakt_link}/shows/anticipated?limit=40'
        self.traktplayed_link = f'{self.trakt_link}/shows/played/weekly?limit=40'
        self.traktwatched_link = f'{self.trakt_link}/shows/watched/weekly?limit=40'
        self.traktcollected_link = f'{self.trakt_link}/shows/collected/weekly?limit=40'

        ####cm##
        # tmdb status tvshow
        # ['Returning Series', 'Planned', 'In Production', 'Ended', 'Canceled', 'Pilot']
        #
        self.person_link = f'{self.tmdb_link}/search/person?api_key={self.tmdb_user}&query=%s&include_adult=false&language=en-US&page=1'
        self.persons_link = f'{self.tmdb_link}/person/%s?api_key={self.tmdb_user}&?language=en-US'
        self.personlist_link = f'{self.tmdb_link}/trending/person/day?api_key={self.tmdb_user}&language=en-US'
        self.person_show_link = f'{self.tmdb_link}/person/%s/tv_credits?api_key={self.tmdb_user}&language=en-US'
        # New TV Shows — TMDB discover (en/US/GB/AU, popular new series; vote floor drops TMDB junk)
        self.premiere_link = (
            f'{self.tmdb_link}/discover/tv?api_key={self.tmdb_user}'
            f'&language={self.lang}&region={self.country}'
            f'&include_adult=false&include_null_first_air_dates=false'
            f'&first_air_date.gte=date[60]&first_air_date.lte={self.today_date}'
            f'&sort_by=popularity.desc&vote_count.gte=2'
            f'&with_origin_country=US|GB|AU&with_original_language=en&page=1'
        )
        self.airing_link = f'{self.tmdb_link}/discover/tv?api_key={self.tmdb_user}&language=en-US&with_origin_country=US|UK|AU&with_original_language=en&sort_by=popularity.desc&air_date.lte=date[0]&air_date.gte=date[1]&page=1'
        self.popular_link = f'{self.tmdb_link}/discover/tv?api_key={self.tmdb_user}&include_adult=false&include_null_first_air_dates=false&sort_by=popularity.desc&vote_count.gte=1000&with_origin_country=US|UK|AU&air_date.gte=date[1]&language=en-US&with_original_language=en&page=1'
        self.genre_link = f'{self.tmdb_link}/discover/tv?api_key={self.tmdb_user}&include_adult=false&include_null_first_air_dates=false&language=en-US&sort_by=popularity.desc&with_origin_country=US|UK|AU&with_original_language=en&with_genres=%s&page=1'
        # Sci-Fi & Fantasy (10765): dual discover (TMDB cannot OR origin/lang).
        # Home = EN + US|UK|AU at normal vote floor; intl = no origin/lang lock at higher vote floor.
        # genres() stores home URL; get() calls tmdb_scifi_fantasy_list to fetch both + merge.
        self.tmdb_scifi_fantasy_home_link = (
            f'{self.tmdb_link}/discover/tv?api_key={self.tmdb_user}'
            f'&include_adult=false&include_null_first_air_dates=false'
            f'&language={self.lang}&sort_by=popularity.desc'
            f'&with_genres=10765&without_genres=16,10762,99'
            f'&with_original_language=en&with_origin_country=US|UK|AU'
            f'&vote_count.gte=25&page=1'
        )
        self.tmdb_scifi_fantasy_intl_link = (
            f'{self.tmdb_link}/discover/tv?api_key={self.tmdb_user}'
            f'&include_adult=false&include_null_first_air_dates=false'
            f'&language={self.lang}&sort_by=popularity.desc'
            f'&with_genres=10765&without_genres=16,10762,99'
            f'&vote_count.gte=100&page=1'
        )
        self.tmdb_scifi_fantasy_link = self.tmdb_scifi_fantasy_home_link
        # Documentary (99): 15y + vote floor — same filters as Classy Genres/Documentaries hub
        self.tmdb_documentary_link = (
            f'{self.tmdb_link}/discover/tv?api_key={self.tmdb_user}'
            f'&include_adult=false&include_null_first_air_dates=false&language=en-US'
            f'&sort_by=popularity.desc&with_origin_country=US|GB|AU'
            f'&with_original_language=en&with_genres=99'
            f'&first_air_date.gte={self.years_15_ago}'
            f'&vote_count.gte=5&page=1'
        )
        self.rating_link = f'{self.tmdb_link}/discover/tv?api_key={self.tmdb_user}&include_adult=false&include_null_first_air_dates=false&language=en-US&with_origin_country=US|UK|AU&with_original_language=en&sort_by=vote_average.desc&vote_count.gte=200&page=1'
        self.views_link = f'{self.tmdb_link}/discover/tv?api_key={self.tmdb_user}&include_adult=false&include_null_first_air_dates=false&language=en-US&with_origin_country=US|UK|AU&with_original_language=en&sort_by=vote_count.desc&vote_count.gte=1500&first_air_date.lte={self.today_date}&page=1'
        self.language_link = f'{self.tmdb_link}/discover/tv?api_key={self.tmdb_user}&include_adult=false&include_video=false&sort_by=popularity.desc&with_original_language=%s&page=1'
        self.active_link = f'{self.tmdb_link}/discover/tv?api_key={self.tmdb_user}&include_adult=false&include_null_first_air_dates=false&language=en-US&sort_by=popularity.desc&with_origin_country=US|UK|AU&with_original_language=en&with_status=0|2&page=1'
        self.tmdb_api_link = f'{self.tmdb_link}/tv/%s?api_key={self.tmdb_user}&language={self.lang}&append_to_response=aggregate_credits,content_ratings,external_ids'
        self.tmdb_networks_link = f'{self.tmdb_link}/discover/tv?api_key={self.tmdb_user}&sort_by=first_air_date.desc&with_networks=%s&page=1'
        self.tmdb_networks_link_no_unaired = f'{self.tmdb_link}/discover/tv?api_key={self.tmdb_user}&first_air_date.lte={self.today_date}&sort_by=first_air_date.desc&with_networks=%s&page=1'
        self.tmdb_search_tvshow_link = f'{self.tmdb_link}/search/tv?api_key={self.tmdb_user}&language=en-US&query=%s&page=1'
        #self.search_link = f'{self.tmdb_link}/search/tv?api_key={self.tmdb_user}&language=en-US&query=%s&page=1'
        # Simkl XOR: browse tree is TMDB — never hit Trakt search while Simkl is live
        if tracker.simkl_live():
            self.search_link = self.tmdb_search_tvshow_link
        self.related_link = f'{self.tmdb_link}/tv/%s/similar?api_key={self.tmdb_user}&page=1'
        self.certification_link = f'{self.tmdb_link}/discover/tv?api_key={self.tmdb_user}&certification=%s&certification_country=US&language=en-US&sort_by=first_air_date.desc&append_to_response=aggregate_credits,content_ratings,external_ids&page=1'
        self.tmdb_info_tvshow_link = f'{self.tmdb_link}/tv/%s?api_key={self.tmdb_user}&language={self.lang}&append_to_response=images'
        self.tmdb_by_imdb = f'{self.tmdb_link}/find/%s?api_key={self.tmdb_user}&external_source=imdb_id'
        self.tmdb_tv_top_rated_link = f'{self.tmdb_link}/tv/top_rated?api_key={self.tmdb_user}&language={self.lang}&sort_by=popularity.desc&page=1'
        self.tmdb_tv_popular_tv_link = f'{self.tmdb_link}/tv/popular?api_key={self.tmdb_user}&language={self.lang}&page=1'
        self.tmdb_tv_on_the_air_link = f'{self.tmdb_link}/tv/on_the_air?api_key={self.tmdb_user}&language={self.lang}&page=1'
        self.tmdb_tv_airing_today_link = f'{self.tmdb_link}/tv/airing_today?api_key={self.tmdb_user}&language={self.lang}&page=1'
        self.tmdb_tv_trending_day_link = f'{self.tmdb_link}/trending/tv/day?api_key={self.tmdb_user}'
        self.tmdb_tv_trending_week_link = f'{self.tmdb_link}/trending/tv/week?api_key={self.tmdb_user}'
        self.tmdb_tv_discover_year_link = f'{self.tmdb_link}/discover/tv?api_key={self.tmdb_user}&language=%s&sort_by=popularity.desc&first_air_date_year={self.year}&include_null_first_air_dates=false&with_original_language=en&append_to_response=aggregate_credits,content_ratings,external_ids&page=1'
        # Upcoming TV — same en/US|GB|AU filters as New TV Shows (premiere_link); region alone does not filter titles
        self.tmdb_tv_upcoming_link = (
            f'{self.tmdb_link}/discover/tv?api_key={self.tmdb_user}'
            f'&language={self.lang}&region={self.country}'
            f'&include_adult=false&include_null_first_air_dates=false'
            f'&sort_by=first_air_date.asc&first_air_date.gte={self.today_date}'
            f'&with_origin_country=US|GB|AU&with_original_language=en&page=1'
        )

        # Kids TV Shows (added 2026-03-02) - Family-friendly content with certifications TV-Y, TV-Y7, TV-G
        # Genre 16 = Animation, Genre 10751 = Family, Genre 10762 = Kids
        self.kids_tv_animation_link = (f'{self.tmdb_link}/discover/tv?api_key={self.tmdb_user}&language={self.lang}&'
                                    f'content_rating_country=US&content_rating=TV-Y|TV-Y7|TV-G&'
                                    f'sort_by=popularity.desc&with_genres=16&'
                                    f'vote_count.gte=20&include_null_first_air_dates=false&page=1')
        self.kids_tv_family_link = (f'{self.tmdb_link}/discover/tv?api_key={self.tmdb_user}&language={self.lang}&'
                                    f'content_rating_country=US&content_rating=TV-Y|TV-Y7|TV-G&'
                                    f'sort_by=popularity.desc&with_genres=10751&'
                                    f'vote_count.gte=20&include_null_first_air_dates=false&page=1')
        self.kids_tv_kids_link = (f'{self.tmdb_link}/discover/tv?api_key={self.tmdb_user}&language={self.lang}&'
                                    f'content_rating_country=US&content_rating=TV-Y|TV-Y7|TV-G&'
                                    f'sort_by=popularity.desc&with_genres=10762&'
                                    f'vote_count.gte=20&include_null_first_air_dates=false&page=1')
        self.kids_tv_all_link = (f'{self.tmdb_link}/discover/tv?api_key={self.tmdb_user}&language={self.lang}&'
                                    f'content_rating_country=US&content_rating=TV-Y|TV-Y7|TV-G&'
                                    f'sort_by=popularity.desc&with_genres=16,10751,10762&'
                                    f'vote_count.gte=20&include_null_first_air_dates=false&page=1')

    def __del__(self) -> None:
        self.session.close()

    def get(self, url: str, tid: int = 0, idx: bool = True, create_directory: bool = True) -> list:
        try:
            # Simkl personal lists — before Trakt link getattr
            # url may be "simklwatching" or "simklwatching&page=2"
            simkl_base = (url or '').split('&')[0].split('?')[0]
            if simkl_base in (
                'simklwatchlist', 'simklhistory', 'simklwatching',
                'simklhold', 'simkldropped', 'simklratings',
            ):
                page = 1
                m = re.search(r'[?&]page=(\d+)', url or '')
                if m:
                    try:
                        page = max(1, int(m.group(1)))
                    except (TypeError, ValueError):
                        page = 1
                return self._handle_simkl_list(simkl_base, idx, create_directory, page=page)

            if 'trakt' in url and '/search' in url:
                pass
            else:
                if url.startswith('http'):
                    pass
                else:
                    url = getattr(self, f'{url}_link')

            ####cm#
            # Making it possible to use date[xx] in url's where xx is a str(int)
            for i in re.findall(r'date\[(\d+)\]', url):
                url = url.replace(f'date[{i}]', (self.datetime - datetime.timedelta(days=int(i))).strftime('%Y-%m-%d'))

            if(self.showunaired) == 'false' and url == self.tmdb_networks_link:
                url = self.tmdb_networks_link_no_unaired

            u = urlparse(url).netloc.lower()
            if not u:
                raise Exception()

            if u in self.trakt_link and '/users/me/collection/' in url:
                    c.log(f"[TVShows.get] Fetching collection from: {url}")
                    self.list = self.collection_list()
                    if self.list is None:
                        self.list = []
                    if self.list:
                        self.list = sorted(self.list, key=lambda k: utils.title_key(k['title']))

            elif u in self.trakt_link and '/users/me/watchlist/' in url:
                c.log(f"[TVShows.get] Fetching watchlist (live) from: {url}")
                prefetched = self._fetch_personal_watchlist_show_items()
                self.list = self.trakt_list(url, self.trakt_user, prefetched_items=prefetched) or []
                c.log(f"[TVShows.get] Watchlist returned {len(self.list)} item(s)")
                self.list = sorted(self.list, key=lambda k: utils.title_key(k.get('title', '')))

            elif u in self.trakt_link and '/users/hidden' in url:
                self.list = cache.get(self.trakthidden_list, 0, url, self.trakt_user)

            elif u in self.trakt_link and '/users/' in url:
                try:
                    if '/users/me/' not in url:
                        raise Exception()
                    if trakt.getActivity_from_db() > cache.timeout(self.trakt_list, url, self.trakt_user):
                        raise Exception()
                    #self.list = cache.get(self.trakt_list, 720, url, self.trakt_user)
                    self.list = cache.get(self.trakt_list, 0, url, self.trakt_user)
                except Exception:
                    #self.list = cache.get(self.trakt_list, 1, url, self.trakt_user)
                    self.list = cache.get(self.trakt_list, 0, url, self.trakt_user)
                self.list = sorted(self.list, key=lambda k: utils.title_key(k.get('title', '')))



            elif u in self.trakt_link:
                # Smart caching for Trakt TV show endpoints (similar to movies.py)
                # Different endpoints update at different frequencies
                if '/search/' in url:
                    # Search must stay live + relevance-ordered (never 4h cache / A-Z)
                    self.list = self.trakt_list(url, self.trakt_user) or []
                elif '/recommendations/' in url:
                    # Personalized recommendations change slowly - cache 48 hours
                    self.list = cache.get(self.trakt_list, 48, url, self.trakt_user)
                elif '/shows/popular' in url or '/shows/anticipated' in url:
                    # Popular/anticipated update daily - cache 24 hours
                    self.list = cache.get(self.trakt_list, 24, url, self.trakt_user)
                elif '/shows/played' in url or '/shows/watched' in url or '/shows/collected' in url:
                    # Played/watched/collected update daily - cache 24 hours
                    self.list = cache.get(self.trakt_list, 24, url, self.trakt_user)
                elif '/shows/trending' in url:
                    # Trending is more dynamic - cache 4 hours (existing behavior)
                    self.list = cache.get(self.trakt_list, 4, url, self.trakt_user)
                else:
                    # Cache general public trakt lists for 4 hours (default)
                    self.list = cache.get(self.trakt_list, 4, url, self.trakt_user)

            #elif u in self.imdb_link and ('/user/' in url or '/list/' in url):
            #    self.list = cache.get(self.imdb_list, 1, url)
            #    if idx is True:
            #        self.worker()

            #elif u in self.imdb_link: #checked
            #    self.list = cache.get(self.imdb_list, 24, url)
            #    if idx is True:
            #        self.worker()

            elif u in self.tvmaze_link:
                self.list = cache.get(self.tvmaze_list, 168, url)
                if idx is True:
                    self.worker()

            elif u in self.tmdb_link and 'tv_credits' in url:
                self.list = cache.get(self.tmdb_cast_list, 24, url)
                self.list = sorted(self.list, key=lambda k: int(k['year']), reverse=True)

            #elif u in self.tmdb_link and self.search_link in url:
            elif u in self.tmdb_link and '/search/' in url:
                #self.list = cache.get(self.tmdb_list, 1, url)
                self.list = self.tmdb_list(url)

            elif u in self.tmdb_networks_link and 'with_networks' in url and 'first_air_date.lte' not in url:
                self.list = cache.get(self.tmdb_list, 24, url, tid)

            elif u in self.tmdb_networks_link_no_unaired and 'with_networks' in url and 'first_air_date.lte' in url:
                self.list = cache.get(self.tmdb_list, 24, url, tid)

            elif u in self.tmdb_link and 'with_genres=10765' in url and 'without_genres=' in url:
                # Sci-Fi & Fantasy: dual home+intl discover, merge by id, keep popularity order (no year re-sort)
                self.list = self.tmdb_scifi_fantasy_list(url)

            elif u in self.tmdb_link:
                #self.list = cache.get(self.tmdb_list, 24, url)
                #self.list = cache.get(self.tmdb_list, 0, url)
                self.list = self.tmdb_list(url)
                # Preserve TMDB first-air-date ordering (New TV Shows / network lists)
                if 'sort_by=first_air_date.desc' in url and 'first_air_date.gte' in url:
                    self.list = sorted(
                        self.list,
                        key=lambda k: k.get('premiered', '') or '0000-01-01',
                        reverse=True,
                    )
                # Sort genre lists by year (newest first). Sci-Fi uses dual-fetch branch above.
                elif 'with_genres=' in url and 'without_genres=' not in url:
                    self.list = sorted(self.list, key=lambda k: int(k.get('year', 0) or 0), reverse=True)

            widget_listing = c.is_widget_listing()
            if idx:
                if widget_listing:
                    self.widget_worker()
                else:
                    self.worker()
            if create_directory:
                self.tvshowDirectory(self.list)
            return self.list
        except Exception as e:
            failure = traceback.format_exc()
            c.log(f'[TVShows @ get] Traceback: {failure}')
            c.log(f'[TVShows @ get] Exception: {e}')
            if create_directory:
                try:
                    control.directory(int(sys.argv[1]), cacheToDisc=False)
                except Exception:
                    pass



#TC 2/01/19 started
    def search(self) -> None:
        """Executes a search operation for TV shows."""

        dbcon = database.connect(control.searchFile)
        dbcur = dbcon.cursor()

        navigator.navigator.addDirectoryItem(32603, 'tvSearchnew', 'search.png', 'DefaultTVShows.png', is_folder=False)

        try:
            sql = "SELECT count(*) as aantal FROM sqlite_master WHERE type='table' AND name='tvshow'"
            dbcur.execute(sql)
            dbcon.commit()
            if dbcur.fetchone()[0] == 0:
                sql = 'CREATE TABLE tvshow (id INTEGER PRIMARY KEY AUTOINCREMENT, term TEXT)'
            dbcur.execute(sql)
        except OperationalError as e:
            c.log(f"[TVShows @ search] OperationalError in search database: {e}", 1)

        dbcur.execute("SELECT * FROM tvshow ORDER BY id DESC")

        search_terms = []
        cm = []
        delete_option = False
        rows = dbcur.fetchall()
        for _id, term in rows:
            if not term or not str(term).strip():
                continue
            if term not in search_terms:
                delete_option = True
                cm = ((32070, f'tvDeleteTerm&id={_id}'))
                navigator.navigator.addDirectoryItem(
                    term,
                    f'tvSearchterm&name={quote_plus(term)}',
                    'search.png',
                    'DefaultTVShows.png',
                    context=cm
                )
                search_terms.append(term)

        dbcur.close()

        if delete_option:
            navigator.navigator.addDirectoryItem(
                32605, 'clearCacheSearch', 'tools.png', 'DefaultAddonProgram.png', is_folder=False
            )

        navigator.navigator.endDirectory(cacheToDisc=False)

    def create_db_connection(self):
        """Creates and returns a database connection to the search database."""
        db_connection = database.connect(control.searchFile)
        db_cursor = db_connection.cursor()
        return db_connection, db_cursor

    def close_db_connection(self, db_connection, db_cursor):
        """Closes the given database connection and cursor."""
        db_cursor.close()
        db_connection.close()

    def search_new(self):
        """Search for a TV show.

        tvSearchnew is is_folder=False (handle -1), so we cannot list results
        via self.get() here. Persist the term, then Container.Update into
        tvSearchterm (real folder handle). Container.Refresh-before-get was
        added in 3.0.8 and left users on the history list with no results.
        """
        try:
            control.idle()

            keyboard_header = control.lang(32010)
            keyboard = control.keyboard('', keyboard_header)
            keyboard.doModal()
            search_query = keyboard.getText() if keyboard.isConfirmed() else None

            if search_query is None:
                return

            search_query = (search_query or '').strip().lower()
            if not search_query:
                return

            db_connection, db_cursor = self.create_db_connection()
            db_cursor.execute("DELETE FROM tvshow WHERE term = ?", (search_query,))
            db_cursor.execute("INSERT INTO tvshow VALUES (?,?)", (None, search_query))
            db_connection.commit()
            self.close_db_connection(db_connection, db_cursor)

            control.execute(
                f'Container.Update({sys.argv[0]}?action=tvSearchterm&name={quote_plus(search_query)})'
            )
        finally:
            control.finish_dialog_route()

    def search_term(self, query: str) -> None:
        """Search for TV shows by query term."""
        query = (query or '').strip().lower()
        if not query:
            c.log('[TVShows] search_term ignored empty query; showing search history')
            self.search()
            return
        cleaned_query = utils.title_key(query)

        db_connection, db_cursor = self.create_db_connection()
        db_cursor.execute("DELETE FROM tvshow WHERE term = ?", (query,))
        db_cursor.execute("INSERT INTO tvshow VALUES (?, ?)", (None, query))
        db_connection.commit()
        self.close_db_connection(db_connection, db_cursor)

        search_url = self.search_link % quote_plus(cleaned_query)
        self.get(search_url)

    def delete_search_term(self, search_term_id: int) -> None:
        """Delete a search term from the database."""
        try:
            db_connection, db_cursor = self.create_db_connection()
            db_cursor.execute("DELETE FROM tvshow WHERE ID = ?", (search_term_id,))
            db_connection.commit()
            self.close_db_connection(db_connection, db_cursor)
            control.refresh()
        except Exception as e:

            error_traceback = traceback.format_exc()
            c.log(f'[Error in delete_search_term] Traceback: {error_traceback}')
            c.log(f'[Error in delete_search_term] Exception: {e}')

    def advanced_search(self, filter_id=None):
        """
        Show advanced search dialog, then list results.

        Menu / Saved Filters open this as a folder — emit into that handle so
        Back returns to TV. RunPlugin / CM (handle < 0) uses Container.Update.
        """
        try:
            handle = int(sys.argv[1])
        except (TypeError, ValueError, IndexError):
            handle = -1

        try:
            from resources.lib.modules import advanced_search as adv_search

            control.idle()
            c.log(f'[TV Shows] advanced_search called with filter_id={filter_id} handle={handle}')

            filter_data = None
            if filter_id:
                manager = adv_search.FilterManager()
                filter_data = manager.get_filter(filter_id)
                c.log(f'[TV Shows] Loaded filter data: {filter_data}')

            result, save_filter = adv_search.show_advanced_search('tv', filter_data)

            c.log(f'[TV Shows] Dialog returned: result={result}, save_filter={save_filter}')

            if not result:
                c.log(f'[TV Shows] No result, returning')
                if handle >= 0:
                    control.close_directory(handle=handle, cacheToDisc=False, succeeded=False)
                else:
                    control.finish_dialog_route()
                return

            if save_filter:
                c.log(f'[TV Shows] Prompting for filter name')
                keyboard = control.keyboard('', 'Name this filter')
                keyboard.doModal()
                filter_name = keyboard.getText() if keyboard.isConfirmed() else None

                c.log(f'[TV Shows] Filter name entered: {filter_name}')

                if filter_name:
                    manager = adv_search.FilterManager()
                    success = manager.save_filter(filter_name, 'tv', result)
                    c.log(f'[TV Shows] save_filter returned: {success}')

            if handle < 0:
                control.execute(
                    adv_search.results_update_url(
                        sys.argv[0], 'tvAdvancedResults', result
                    )
                )
                control.finish_dialog_route()
                return

            # Folder click: list into this handle (Back → TV / Saved Filters).
            self.advanced_search_execute(result)

        except Exception as e:
            c.log(f'[TV Shows] Error in advanced_search: {e}')
            import traceback
            c.log(f'[TV Shows] Traceback: {traceback.format_exc()}')
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
                filter_data = adv_search.pop_filter()
            if not filter_data:
                control.infoDialog('Advanced search expired — try again', sound=True, icon='INFO')
                control.finish_dialog_route()
                return
            self.advanced_search_execute(filter_data)
        except Exception as e:
            c.log(f'[TV Shows] Error in advanced_search_results: {e}')
            import traceback
            c.log(f'[TV Shows] Traceback: {traceback.format_exc()}')
            control.finish_dialog_route()

    def advanced_search_execute(self, filter_data):
        """Execute advanced search with given filter criteria"""
        try:
            from resources.lib.modules import advanced_search as adv_search

            # Build discover or search URL
            url = adv_search.build_discover_url(
                self.tmdb_link,
                self.tmdb_user,
                'tv',
                filter_data
            )

            if not url:
                return

            if c.devmode:
                c.log(f"[TV Shows] Executing advanced search: {url}")

            # Check if we're using search endpoint (has 'keyword' in filter)
            is_search = '/search/' in url

            if c.devmode:
                c.log(f"[TV Shows] is_search={is_search}, has genres={bool(filter_data.get('genre_ids'))}, has rating={bool(filter_data.get('min_rating'))}")

            if is_search and (filter_data.get('genre_ids') or filter_data.get('min_rating') or
                            adv_search.needs_year_postfilter(filter_data)):
                # Search endpoint doesn't support genre/rating filters, need to post-filter
                if c.devmode:
                    c.log(f"[TV Shows] Using post-filter path")
                self._advanced_search_with_postfilter(url, filter_data)
            else:
                # Regular discover or search without additional filters
                if c.devmode:
                    c.log(f"[TV Shows] Using regular get() path")
                self.get(url)

        except Exception as e:
            c.log(f'[TV Shows] Error in advanced_search_execute: {e}')
            import traceback
            c.log(f'[TV Shows] Traceback: {traceback.format_exc()}')

    def _advanced_search_with_postfilter(self, url, filter_data):
        """Fetch search results and apply post-filtering for genre/rating/year"""
        try:
            import requests
            import traceback

            if c.devmode:
                c.log(f"[TV Shows] Fetching search results for post-filtering")

            response = self.session.get(url, timeout=10)
            if response.status_code != 200:
                c.log(f"[TV Shows] Search request failed with status {response.status_code}")
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
                c.log(f"[TV Shows] Got {len(results)} results, applying filters...")
                # Log first 3 results for debugging
                for i, item in enumerate(results[:3]):
                    c.log(f"[TV Shows] Result {i+1}: '{item.get('name')}' ({item.get('first_air_date', 'no date')[:4] if item.get('first_air_date') else '????'}) rating={item.get('vote_average', 0)} genres={item.get('genre_ids', [])}")

            # Apply post-filters
            filtered_results = []
            for item in results:
                show_name = item.get('name', 'Unknown')
                passed_filters = True
                filter_reasons = []

                # Filter by year range
                if filter_data.get('year_from') or filter_data.get('year_to'):
                    first_air_date = item.get('first_air_date', '')
                    if first_air_date:
                        try:
                            year = int(first_air_date.split('-')[0])
                            if filter_data.get('year_from') and year < filter_data['year_from']:
                                passed_filters = False
                                filter_reasons.append(f"year {year} < {filter_data['year_from']}")
                            if filter_data.get('year_to') and year > filter_data['year_to']:
                                passed_filters = False
                                filter_reasons.append(f"year {year} > {filter_data['year_to']}")
                        except (ValueError, TypeError, IndexError, AttributeError):
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
                        c.log(f"[TV Shows] (OK) '{show_name}' passed all filters")
                else:
                    if c.devmode:
                        c.log(f"[TV Shows] (X) '{show_name}' filtered out: {', '.join(filter_reasons)}")

            if c.devmode:
                c.log(f"[TV Shows] After filtering: {len(filtered_results)} results")

            if not filtered_results:
                # Show helpful message about why no results
                msg = "No results found. Try:\n"
                msg += "• Removing genre filters (TMDB metadata can be incomplete)\n"
                msg += "• Expanding year range\n"
                msg += "• Lowering minimum rating"
                control.infoDialog(msg, sound=True, icon='INFO', time=5000)
                return

            # Convert filtered results to the format expected by tmdb_list
            self.list = []
            for item in filtered_results:
                try:
                    show_item = {
                        'tmdb': str(item.get('id', '')),
                        'title': item.get('name', ''),
                        'originaltitle': item.get('original_name', ''),
                        'year': item.get('first_air_date', '')[:4] if item.get('first_air_date') else '',
                        'premiered': item.get('first_air_date', ''),
                        'rating': str(item.get('vote_average', '')),
                        'votes': str(item.get('vote_count', '')),
                        'plot': item.get('overview', ''),
                        'poster': f"https://image.tmdb.org/t/p/w500{item.get('poster_path')}" if item.get('poster_path') else '',
                        'fanart': f"https://image.tmdb.org/t/p/original{item.get('backdrop_path')}" if item.get('backdrop_path') else '',
                        'imdb': '0',
                        'tvdb': '0',
                        'next': _next,
                        'seasons': '0',  # Will be filled by worker()
                        'episodes': '0'  # Will be filled by worker()
                    }
                    self.list.append(show_item)
                except Exception as e:
                    c.log(f"[TV Shows] Error processing item: {e}")
                    continue

            if c.devmode:
                c.log(f"[TV Shows] Built list with {len(self.list)} items")
                if self.list:
                    c.log(f"[TV Shows] First item: {self.list[0]}")

            # Enrich items with metadata (seasons, episodes, etc.)
            c.log(f"[TV Shows] About to call worker to enrich {len(self.list)} items")
            self.worker()
            c.log(f"[TV Shows] Worker completed, list now has {len(self.list)} items")

            # Display TV show items
            c.log(f"[TV Shows] About to call tvshowDirectory with {len(self.list)} items")
            self.tvshowDirectory(self.list)
            c.log(f"[TV Shows] Returned from tvshowDirectory")

        except Exception as e:
            c.log(f'[TV Shows] Error in _advanced_search_with_postfilter: {e}')
            import traceback
            c.log(f'[TV Shows] Traceback: {traceback.format_exc()}')

    def saved_filters_list(self):
        """Show list of saved filters for TV shows"""
        try:
            from resources.lib.modules import advanced_search as adv_search

            manager = adv_search.FilterManager()
            filters = manager.list_filters('tv')

            # Always show "New Advanced Search" button
            navigator.navigator.addDirectoryItem(
                90223,
                'tvAdvancedSearch',
                'search.png',
                'DefaultTVShows.png'
            )

            # Show saved filters if any exist
            for filter_id, filter_name in filters:
                cm = [
                    (32070, f'tvDeleteFilter&id={filter_id}'),  # Delete
                    (90224, f'tvAdvancedSearch&filter_id={filter_id}')
                ]

                navigator.navigator.addDirectoryItem(
                    filter_name,
                    f'tvExecuteFilter&id={filter_id}',
                    'search.png',
                    'DefaultTVShows.png',
                    context=cm
                )

            # Add "Clear all filters" option if there are any filters
            if filters:
                navigator.navigator.addDirectoryItem(
                    32604,  # "Clear search history..."
                    'tvClearAllFilters',
                    'tools.png',
                    'DefaultAddonProgram.png'
                )

            navigator.navigator.endDirectory()

        except Exception as e:
            c.log(f'[TV Shows] Error in saved_filters_list: {e}')

    def execute_saved_filter(self, filter_id):
        """Execute a saved filter by ID"""
        try:
            from resources.lib.modules import advanced_search as adv_search

            manager = adv_search.FilterManager()
            filter_data = manager.get_filter(filter_id)

            if filter_data:
                self.advanced_search_execute(filter_data)
            else:
                control.infoDialog('Filter not found', sound=True, icon='ERROR')

        except Exception as e:
            c.log(f'[TV Shows] Error in execute_saved_filter: {e}')

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
            c.log(f'[TV Shows] Error in delete_saved_filter: {e}')

    def clear_all_saved_filters(self):
        """Clear all saved filters for TV shows"""
        try:
            from resources.lib.modules import advanced_search as adv_search

            yes = control.yesnoDialog('Delete all saved filters?', 'This action cannot be undone.')
            if not yes:
                return

            manager = adv_search.FilterManager()
            count = manager.delete_all_filters('tv')

            control.infoDialog(f'Deleted {count} filter(s)', sound=True, icon='INFO')
            control.refresh()

        except Exception as e:
            c.log(f'[TV Shows] Error in clear_all_saved_filters: {e}')

    def person(self):
        """
        Prompts for a person name, then Container.Update into tvPersons.

        tvPerson is is_folder=False (dialog handle). Listing via self.persons()
        here fails the same way title search used to — navigate into
        tvPersons&url=… on a real folder handle instead.
        """
        try:
            control.idle()
            prompt_text = control.lang(32010)
            keyboard = control.keyboard('', prompt_text)
            keyboard.doModal()
            query = keyboard.getText() if keyboard.isConfirmed() else None

            if not query:
                return

            query = (query or '').strip()
            if not query:
                return

            # replace=True: Back skips tvPerson dialog (else keyboard loops).
            person_url = self.person_link % quote_plus(query)
            control.execute(
                f'Container.Update({sys.argv[0]}?action=tvPersons&url={quote_plus(person_url)},replace)'
            )
        except Exception as e:
            c.log(f'Error in person method: {e}')
        finally:
            control.finish_dialog_route()

    #####cm#
    # Completely redone for compatibility with tmdb
    # source reference/genre-tv-list
    # Official /genre/tv/list has no Thriller (movie-only id 53). Cop/Detective are
    # not TMDB TV genres either — custom discover links use real genre OR/AND or keywords.
    def genres(self):
        # Same EN + US|UK|AU filters as genre_link (not Sci-Fi dual / not Anime JP).
        _std = (
            f'{self.tmdb_link}/discover/tv?api_key={self.tmdb_user}'
            f'&include_adult=false&include_null_first_air_dates=false'
            f'&language=en-US&sort_by=popularity.desc'
            f'&with_origin_country=US|UK|AU&with_original_language=en'
        )
        genre_list = [
            {"id": 10759, "name": "Action & Adventure"},
            {"id": 16, "name": "Animation"},
            {"id": "anime", "name": "Anime", "custom_link": f'{self.tmdb_link}/discover/tv?api_key={self.tmdb_user}&include_adult=false&include_null_first_air_dates=false&language=en-US&sort_by=popularity.desc&with_origin_country=JP&with_genres=16&page=1'},
            {"id": 35, "name": "Comedy"},
            # Cop Shows: keywords police (6149) | cop (8015) | police detective (15167)
            {"id": "cop", "name": "Cop Shows", "custom_link": f'{_std}&with_keywords=6149|8015|15167&page=1'},
            {"id": 80, "name": "Crime"},
            # Detective: Crime AND Mystery (no dedicated TMDB TV genre)
            {"id": "detective", "name": "Detective", "custom_link": f'{_std}&with_genres=80,9648&page=1'},
            {"id": 99, "name": "Documentary"},
            {"id": 18, "name": "Drama"},
            {"id": 10751, "name": "Family"},
            {"id": 10762, "name": "Kids"},
            {"id": 9648, "name": "Mystery"},
            {"id": 10763, "name": "News"},
            {"id": 10764, "name": "Reality"},
            {"id": 10765, "name": "Sci-Fi & Fantasy"},
            {"id": 10766, "name": "Soap"},
            {"id": 10767, "name": "Talk"},
            # Thriller: not a TV genre id — Crime OR Mystery as movie-Thriller stand-in
            {"id": "thriller", "name": "Thriller", "custom_link": f'{_std}&with_genres=80|9648&page=1'},
            {"id": 10768, "name": "War & Politics"},
            {"id": 37, "name": "Western"}
        ]

        for genre in genre_list:
            # Anime: JP animation. Documentary (99): Classy 15y/vote floor.
            # Sci-Fi (10765): home URL marker; get() dual-fetches EN/US|UK|AU + high-vote intl.
            # Cop/Detective/Thriller: custom_link discover (genres or keywords).
            if 'custom_link' in genre:
                url = genre['custom_link']
            elif genre['id'] == 99:
                url = self.tmdb_documentary_link
            elif genre['id'] == 10765:
                url = self.tmdb_scifi_fantasy_link
            else:
                url = self.genre_link % genre['id']
            self.list.append({
                'name': cleangenre.lang(genre['name'], self.lang),
                'url': url,
                'image': 'genres.png',
                'action': 'tvshows'
            })

        self.addDirectory(self.list)
        return self.list

    def networks(self):
        try:
            network_data = [
                (129, "A&E", f'{self.logo_link}/ptSTdU4GPNJ1M8UVEOtA0KgtuNk.png'),
                (2, "ABC", f'{self.logo_link}/an88sKsFz0KX5CQngAM95WkncX4.png'),
                (1024, "Amazon", f'{self.logo_link}/uK6yuqMkUvKhCgVJjg5JWDUoabA.png'),
                (174, "AMC", f'{self.logo_link}/alqLicR1ZMHMaZGP3xRQxn9sq7p.png'),
                (91, "Animal Planet", f'{self.logo_link}/xQ25rzpv83d74V1zpOzSHbYlwJq.png'),
                (173, "AT-X", f'{self.logo_link}/fERjndErEpveJmQZccJbJDi93rj.png'),
                (493, "BBC America", f'{self.logo_link}/8Js4sUaxjE3RSxJcOCDjfXvhZqz.png'),
                (4, "BBC One", f'{self.logo_link}/uJjcCg3O4DMEjM0xtno9OWFciRP.png'),
                (332, "BBC Two", f'{self.logo_link}/7HVPn1p2w1nC5oRKBehXVHpss7e.png'),
                (3, "BBC Three", f'{self.logo_link}/s22fRhj8xFPbiexrJwiAOcDEIrS.png'),
                (100, "BBC Four", f'{self.logo_link}/AgsOSxGvfxIonhPgrfkWCmsOKfA.png'),
                (24, "BET", f'{self.logo_link}/gaouRlJrfZlEA5EPHhO5qqZ1Fgu.png'),
                (74, "Bravo", f'{self.logo_link}/wX5HsfS47u6UUCSpYXqaQ1x2qdu.png'),
                (56, "Cartoon Network", f'{self.logo_link}/c5OC6oVCg6QP4eqzW6XIq17CQjI.png'),
                (201, "CBC", f'{self.logo_link}/qNooLje0YQh1y3y9LUM2Y5QCtiF.png'),
                (16, "CBS", f'{self.logo_link}/wju8KhOUsR5y4bH9p3Jc50hhaLO.png'),
                (26, "Channel 4", f'{self.logo_link}/zCUWm0Xb6AnjUbxzjL5OkzmHhd7.png'),
                (99, "Channel 5", f'{self.logo_link}/bMuKs6xuhI0GHSsq4WWd9FsntUN.png'),
                (47, "Comedy Central", f'{self.logo_link}/6ooPjtXufjsoskdJqj6pxuvHEno.png'),
                (2548, "CBC", f'{self.logo_link}/qe2RYSTCxbPh3jCaM1tk9E4uJZ6.png'),
                (403, "CTV", f'{self.logo_link}/volHUxY1MHjSPI4ju7j36EdhR2m.png'),
                (928, "Crackle", f'{self.logo_link}/bR8S6Fjv3VGtEKyKF5lvvRJ5xfw.png'),
                (71, "The CW", f'{self.logo_link}/ge9hzeaU7nMtQ4PjkFlc68dGAJ9.png'),
                (1049, "CW seed", f'{self.logo_link}/wwo3PZyBpHL3Wz8eg4cr3kqVZQY.png'),
                (64, "Discovery", f'{self.logo_link}/8qkdZlbrTSVfkJ73DjOBrwYtMSC.png'),
                (4883, "discovery+", f'{self.logo_link}/iKvdFk5lpbvs4g0vd6yVUcV36i3.png'),
                (244, "Discovery ID", f'{self.logo_link}/yfkdPLHjsed7vwUNuh20eMuDiDO.png'),
                (2739, "Disney+", f'{self.logo_link}/PQxvkeK8cTtD7vjataBsNpjbJ5.png'),
                (54, "Disney Channel", f'{self.logo_link}/gvhBea9OGqChmGKHa5CntbmsDBp.png'),
                (44, "Disney XD", f'{self.logo_link}/nKM9EnV7jTpt3MKRbhBusJ03lAY.png'),
                (2087, "Discovery Channel", f'{self.logo_link}/8qkdZlbrTSVfkJ73DjOBrwYtMSC.png'),
                (76, "E! Entertainment", f'{self.logo_link}/ptpx2Ag52sYJG6LiX9zBlnKsQOS.png'),
                (136, "E4", f'{self.logo_link}/fJPM9Rj12us4HF03N3qvakz7WuZ.png'),
                (19, "FOX", f'{self.logo_link}/1DSpHrWyOORkL9N2QHX7Adt31mQ.png'),
                (1267, "Freeform", f'{self.logo_link}/jk2Z7WH6JnHSZrxouYh4sireM3a.png'),
                (88, "FX", f'{self.logo_link}/aexGjtcs42DgRtZh7zOxayiry4J.png'),
                (384, "Hallmark Channel", f'{self.logo_link}/9JTL7HcaiVxq7M6eu5m7giFqaxR.png'),
                (65, "History", f'{self.logo_link}/9fGgdJz17aBX7dOyfHJtsozB7bf.png'),
                (49, "HBO", f'{self.logo_link}/hizvY65SpyF3BPY2qsBZMgUOxjs.png'),
                (3186, "HBO Max", f'{self.logo_link}/nmU0UMDJB3dRRQSTUqawzF2Od1a.png'),
                (210, "HGTV", f'{self.logo_link}/tzTtKdQ7vC2FkBvJDUErOhBPdKJ.png'),
                (453, "Hulu", f'{self.logo_link}/pqUTCleNUiTLAVlelGxUgWn1ELh.png'),
                (9, "ITV", f'{self.logo_link}/j3KAlTmxGDCHQZqs1A2hagzjYqu.png'),
                (34, "Lifetime", f'{self.logo_link}/kU18GafTybg4uMhkj3wvsGBgn8s.png'),
                (33, "MTV USA", f'{self.logo_link}/w4qtv7xBkSVsbOQdSzjUjlyOuSr.png'),
                (488, "MTV UK", f'{self.logo_link}/w4qtv7xBkSVsbOQdSzjUjlyOuSr.png'),
                (43, "National Geographic", f'{self.logo_link}/q9rPBG1rHbUjII1Qn98VG2v7cFa.png'),
                (6, "NBC", f'{self.logo_link}/cm111bsDVlYaC1foL0itvEI4yLG.png'),
                (213, "Netflix", f'{self.logo_link}/wwemzKWzjKYJFfCeiB57q3r4Bcm.png'),
                (13, "Nickelodeon", f'{self.logo_link}/aYkLXz4dxHgOrFNH7Jv7Cpy56Ms.png'),
                (14, "PBS", f'{self.logo_link}/hp2Fs7AIdsMlEjiDUC1V8Ows2jM.png'),
                (67, "Showtime", f'{self.logo_link}/Allse9kbjiP6ExaQrnSpIhkurEi.png'),
                (1755, "Sky History", f'{self.logo_link}/mzLlbqnnLiDIzriohlvfSbWlEfR.png'),
                (1431, "Sky One", f'{self.logo_link}/dVBHOr0nYCx9GSNesTVb1TT52Xj.png'),
                (318, "Starz", f'{self.logo_link}/GMDGZk9iDG4WDijY3VgUgJeyus.png'),
                (270, "SundanceTV", f'{self.logo_link}/xhTdszjVRy1tABMix2dffBcdDJ1.png'),
                (77, "Syfy", f'{self.logo_link}/iYfrkobwDhTOFJ4AXYPSLIEeaAT.png'),
                (68, "TBS", f'{self.logo_link}/9PYsQf3YbDUJo1rg3pgtaiOrb6s.png'),
                (84, "TLC", f'{self.logo_link}/6GRfZSrYh9D6C88n9kWlyrySB2l.png'),
                (41, "TNT", f'{self.logo_link}/6ISsKwa2XUhSC6oBtHZjYf6xFqv.png'),
                (209, "Travel Channel", f'{self.logo_link}/8SwN81R7P5vD5mhtOE0taw5mji4.png'),
                (364, "truTV", f'{self.logo_link}/c48pVcWAEYhEFXrWFsYxx343mjx.png'),
                (30, "USA Network", f'{self.logo_link}/g1e0H0Ka97IG5SyInMXdJkHGKiH.png'),
                (158, "VH1", f'{self.logo_link}/w9oUxxUiXTC1O1MzJSvsMjQbgft.png'),
                (202, "WGN America", f'{self.logo_link}/kCNFRiqVRMgNWKSWu0LzAIpy9um.png'),
                (247, "YouTube", f'{self.logo_link}/9Ga8A5QegQmiSVHp4hyusfMfpVk.png'),
                (1436, "YouTube Premium", f'{self.logo_link}/3p05CgodUb9gPayuliuhawNj1Wo.png'),
            ]

            self.list = [
                {
                    'name': name,
                    'url': self.tmdb_networks_link % network_id,
                    'image': logo,
                    'action': 'tvshows'
                }
                for network_id, name, logo in network_data
            ]

            self.addDirectory(self.list)
            #return self.list
        except Exception as e:

            error_traceback = traceback.format_exc()
            c.log(f'[Error in networks] Traceback: {error_traceback}')
            c.log(f'[Error in networks] Exception: {e}')

    def languages(self):
        languages = [
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
            ('Norwegian', 'no'),
            ('Persian', 'fa'),
            ('Polish', 'pl'),
            ('Portuguese', 'pt'),
            ('Punjabi', 'pa'),
            ('Romanian', 'ro'),
            ('Russian', 'ru'),
            ('Serbian', 'sr'),
            ('Spanish', 'es'),
            ('Swedish', 'sv'),
            ('Turkish', 'tr'),
            ('Ukrainian', 'uk')
        ]


        for i in languages:
            self.list.append({'name': str(i[0]), 'url': self.language_link % i[1], 'image': 'international2.png', 'action': 'tvshows'})
        self.addDirectory(self.list)
        return self.list

    def certifications(self):
        certificates = [
            ('TV-Y', 'All Children', 'tv_y.png'),
            ('TV-G', 'General Audiences', 'tv_g.png'),
            ('TV-PG', 'Parental Guidance', 'tv_pg.png'),
            ('TV-14', 'Parents Strongly Cautioned', 'tv_14.png'),
            ('TV-MA', 'Mature Audiences', 'tv_ma.png'),
            ]


        self.list = [
                {
                    'name': name,
                    'url': self.certification_link % code,
                    'image': icon,
                    'action': 'tvshows'
                }
                for code, name, icon in certificates
            ]


        #for i in certificates:
            #self.list.append({'name': str(i), 'url': self.certification_link % str(i), 'image': 'certificates.png', 'action': 'tvshows'})
        self.addDirectory(self.list)
        return self.list

    def persons(self, url):
        if url is None:
            #self.list = cache.get(self.tmdb_person_list, 24, self.personlist_link)
            self.tmdb_person_list(self.personlist_link)

        else:
            #self.list = cache.get(self.tmdb_person_list, 1, url)
            self.tmdb_person_list (url)

        #for i in range(len(self.list)):
            #self.list[i].update({'action': 'tvshows'})
        for item in self.list:
            item.update({'action': 'tvshows'})
        self.addDirectory(self.list)
        return self.list

    def tmdb_person_list(self, url):
        """
        Fetch and format person search results from TMDB.
        Lists actors/directors/etc with their photos and known_for info.
        Clicking a person shows their TV shows via tmdb_cast_list.
        """
        try:
            result = http_client.tmdb_get_json(url, timeout=15) or {}
            items = result.get('results', [])
        except Exception as e:
            c.log(f'[TVShows @ tmdb_person_list] Error fetching person list: {e}')
            return

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

        self.list = []

        for item in items:
            try:
                # Person ID and name
                person_id = str(item.get('id', '0'))
                name = item.get('name', '')
                if not name:
                    continue

                # Profile photo
                profile_path = item.get('profile_path', '')
                if profile_path:
                    image = self.tmdb_img_link % (c.tmdb_postersize, profile_path)
                else:
                    image = '0'

                # Known for (department)
                known_for_dept = item.get('known_for_department', 'Acting')

                # Build URL to show this person's TV shows
                person_url = f'{self.tmdb_link}/person/{person_id}/tv_credits?api_key={self.tmdb_user}&language={self.lang}'

                self.list.append({
                    'name': name,
                    'url': person_url,
                    'image': image,
                    'person_id': person_id,
                    'known_for': known_for_dept,
                    'next': _next
                })

            except Exception as e:
                c.log(f'[TVShows @ tmdb_person_list] Error processing person: {e}')
                continue

        c.log(f'[TVShows @ tmdb_person_list] Returning {len(self.list)} people')
        return self.list

    def userlists(self):
        try:
            userlists = []
            if trakt.get_trakt_credentials_info() is False:
                raise Exception()
            activity = trakt.getActivity_from_db()
        except Exception:
            pass

        try:
            if trakt.get_trakt_credentials_info() is False:
                raise Exception()
            try:
                if activity > cache.timeout(self.trakt_user_list, self.traktlists_link, self.trakt_user):
                    raise Exception()
                userlists += cache.get(self.trakt_user_list, 720, self.traktlists_link, self.trakt_user)
            except Exception:
                userlists += cache.get(self.trakt_user_list, 0, self.traktlists_link, self.trakt_user)
        except Exception:
            pass
        try:
            self.list = []
            if trakt.get_trakt_credentials_info() is False:
                raise Exception()
            try:
                if activity > cache.timeout(self.trakt_user_list, self.traktlikedlists_link, self.trakt_user): raise Exception()
                userlists += cache.get(self.trakt_user_list, 720, self.traktlikedlists_link, self.trakt_user)
            except Exception:
                userlists += cache.get(self.trakt_user_list, 0, self.traktlikedlists_link, self.trakt_user)
        except Exception:
            pass

        self.list = userlists

        #for i in range(len(self.list)):
            #self.list[i].update({'image': 'userlists.png', 'action': 'tvshows'})

        for item in self.list:
            item.update({'image': 'userlists.png', 'action': 'tvshows'})

        self.addDirectory(self.list)
        return self.list

    def trakthidden_list(self, url, user):
        """Fetch shows hidden from all Trakt sections and return as a browsable list."""
        try:
            base = 'https://api.trakt.tv/users/hidden'
            sections = ['calendar', 'progress_watched', 'progress_collected', 'recommendations', 'dropped']
            seen_trakt_ids = set()
            combined = []
            for section in sections:
                section_url = f'{base}/{section}?type=show&limit=1000'
                section_result = trakt.getTraktAsJson(section_url)
                if section_result:
                    for item in section_result:
                        trakt_id = item.get('show', {}).get('ids', {}).get('trakt')
                        if trakt_id and trakt_id not in seen_trakt_ids:
                            seen_trakt_ids.add(trakt_id)
                            combined.append(item)
            result = combined
            c.log(f'[TVShows @ trakthidden_list] Got {len(result)} unique hidden shows across all sections')
            if not result:
                c.log('[TVShows @ trakthidden_list] No hidden shows found in any section')
                return

            from ..models.tvshow import TVShow
            dupes = []

            def add_to_list(item):
                try:
                    show_data = item.get('show')
                    if not show_data:
                        return None
                    show = TVShow.from_trakt_data(show_data)
                    if not show:
                        return None
                    if show.tmdb in dupes:
                        return None
                    dupes.append(show.tmdb)
                    if show.tmdb != '0':
                        if show.tmdb in self.artwork_cache:
                            cached = self.artwork_cache[show.tmdb]
                            show.poster = cached.get('poster', show.poster)
                            show.fanart = cached.get('fanart', show.fanart)
                            show.total_seasons = cached.get('seasons', show.total_seasons)
                            show.total_episodes = cached.get('episodes', show.total_episodes)
                            show.director = cached.get('director', show.director)
                            show.writer = cached.get('writer', show.writer)
                            show.cast = cached.get('cast', show.cast)
                            show.castwiththumb = cached.get('castwiththumb', show.castwiththumb)
                            show.genre = cached.get('genre', show.genre)
                            show.studio = cached.get('studio', show.studio)
                            show.country = cached.get('country', show.country)
                            show.rating = cached.get('rating', show.rating)
                            show.votes = cached.get('votes', show.votes)
                            show.mpaa = cached.get('mpaa', show.mpaa)
                        else:
                            show.fetch_tmdb_metadata()
                            self.artwork_cache[show.tmdb] = {
                                'poster': show.poster,
                                'fanart': show.fanart,
                                'seasons': show.total_seasons,
                                'episodes': show.total_episodes,
                                'director': show.director,
                                'writer': show.writer,
                                'cast': show.cast,
                                'castwiththumb': show.castwiththumb,
                                'genre': show.genre,
                                'studio': show.studio,
                                'country': show.country,
                                'rating': show.rating,
                                'votes': show.votes,
                                'mpaa': show.mpaa,
                            }
                    show_dict = show.to_dict()
                    show_dict['next'] = ''
                    show_dict['seasons'] = str(show.total_seasons)
                    show_dict['episodes'] = str(show.total_episodes)
                    return show_dict
                except Exception as e:
                    c.log(f'[TVShows @ trakthidden_list] Error processing item: {e}')
                    return None

            max_nr = c.get_max_threads(len(result))
            c.log(f'[TVShows @ trakthidden_list] Processing {len(result)} items with {max_nr} workers')

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_nr) as executor:
                futures = {executor.submit(add_to_list, item): item for item in result}
                for future in concurrent.futures.as_completed(futures):
                    try:
                        show_dict = future.result()
                        if show_dict:
                            self.list.append(show_dict)
                    except Exception as exc:
                        c.log(f'[TVShows @ trakthidden_list] Worker error: {exc}')

            c.log(f'[TVShows @ trakthidden_list] Returning {len(self.list)} items')
            self.list = sorted(self.list, key=lambda k: utils.title_key(k['title']))
            return self.list

        except Exception as e:
            failure = traceback.format_exc()
            c.log(f'[TVShows @ trakthidden_list] Exception: {e}\n{failure}')
            return

    def _fetch_personal_watchlist_show_items(self):
        """
        Build unique show rows for the personal Trakt watchlist.

        Trakt's /watchlist/shows only returns show-level entries. Many users
        watchlist seasons or episodes instead — those appear on /watchlist only.
        """
        shows = {}

        def _register(raw):
            if not isinstance(raw, dict):
                return
            trakt_id = str((raw.get('ids') or {}).get('trakt') or '')
            if trakt_id and trakt_id not in shows:
                shows[trakt_id] = raw

        fetch_urls = (
            f'{self.trakt_link}/users/me/watchlist/shows?limit=250&page=1&extended=full',
            f'{self.trakt_link}/users/me/watchlist?limit=250&page=1&extended=full',
        )
        for fetch_url in fetch_urls:
            try:
                q = dict(parse_qsl(urlsplit(fetch_url).query))
                q['extended'] = 'full'
                encoded = urlencode(q).replace('%2C', ',')
                api_url = fetch_url.split('?', 1)[0] + '?' + encoded
                result = trakt.getTraktAsJson(api_url) or []
            except Exception as exc:
                c.log(f'[TVShows] Watchlist fetch failed ({fetch_url}): {exc}')
                continue

            for entry in result:
                if not isinstance(entry, dict):
                    continue
                if 'show' in entry:
                    _register(entry.get('show'))
                elif 'season' in entry:
                    _register((entry.get('season') or {}).get('show'))
                elif 'episode' in entry:
                    _register((entry.get('episode') or {}).get('show'))

        out = list(shows.values())
        c.log(f'[TVShows] Personal watchlist: {len(out)} unique show(s) from Trakt')
        return out

    def trakt_list(self, url, user, prefetched_items=None):
        self.list = []
        _next = ''
        try:
            if prefetched_items is not None:
                items = list(prefetched_items)
                self.pagination = trakt.get_pagination_info()
            else:
                q = dict(parse_qsl(urlsplit(url).query))
                q['extended'] = 'full'
                q = (urlencode(q)).replace('%2C', ',')
                u = url.replace(f'?{urlparse(url).query}', '') + '?' + q
                result = trakt.getTraktAsJson(u)

                # Capture pagination info from last Trakt API call
                self.pagination = trakt.get_pagination_info()

                items = []
                if result:
                    for i in result:
                        try:
                            items.append(i['show'])
                        except Exception:
                            pass
                if not items:
                    items = result or []
        except Exception as exc:
            c.log(f'[TVShows @ trakt_list] Fetch failed: {exc}')
            c.log(f'[TVShows @ trakt_list] Traceback: {traceback.format_exc()}')
            return []

        if prefetched_items is None:
            try:
                q = dict(parse_qsl(urlsplit(url).query))
                if 'page' in q and items and int(q.get('limit', 0) or 0) == len(items):
                    q['page'] = str(int(q['page']) + 1)
                    q = (urlencode(q)).replace('%2C', ',')
                    _next = url.replace(f'?{urlparse(url).query}', '') + '?' + q
                    _next = str(_next)
            except Exception:
                _next = ''

        dupes = []

        # Import TVShow model
        from ..models.tvshow import TVShow

        def add_to_list(item):
            try:
                # Create TVShow from Trakt data
                show = TVShow.from_trakt_data(item)
                if not show:
                    c.log("[TVShows] Failed to create show from Trakt data")
                    return None

                # Skip duplicates
                if show.tmdb in dupes:
                    return None
                dupes.append(show.tmdb)

                # Fetch TMDB basics (seasons, episodes, artwork) if we have TMDB ID
                if show.tmdb != '0':
                    # Check artwork cache first
                    if show.tmdb in self.artwork_cache:
                        cached = self.artwork_cache[show.tmdb]
                        show.poster = cached.get('poster', show.poster)
                        show.fanart = cached.get('fanart', show.fanart)
                        show.total_seasons = cached.get('seasons', show.total_seasons)
                        show.total_episodes = cached.get('episodes', show.total_episodes)
                        c.log(f"[TVShows] (OK) Cache hit for: {show.title}")
                    else:
                        # Fetch from TMDB
                        show.fetch_tmdb_basics()

                        # Cache the artwork
                        self.artwork_cache[show.tmdb] = {
                            'poster': show.poster,
                            'fanart': show.fanart,
                            'seasons': show.total_seasons,
                            'episodes': show.total_episodes
                        }

                # Convert to dict and add next page link
                show_dict = show.to_dict()
                show_dict['next'] = _next
                show_dict['seasons'] = str(show.total_seasons)
                show_dict['episodes'] = str(show.total_episodes)

                return show_dict

            except Exception as e:

                c.log(f'[TVShows @ trakt_list] Error processing item: {e}')
                c.log(f'[TVShows @ trakt_list] Traceback: {traceback.format_exc()}')
                return None

        try:
            if not items:
                c.log('[TVShows @ trakt_list] No items to process')
                return []

            max_nr = c.get_max_threads(len(items))
            is_search = '/search/' in (url or '')
            c.log(f"[TVShows @ trakt_list] Processing {len(items)} items with {max_nr} workers")

            # Preserve API order (Trakt search relevance). as_completed alone scrambles it.
            slotted = [None] * len(items)
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_nr) as executor:
                futures = {
                    executor.submit(add_to_list, item): idx
                    for idx, item in enumerate(items)
                }
                for future in concurrent.futures.as_completed(futures):
                    idx = futures[future]
                    try:
                        result = future.result()
                        if result:
                            slotted[idx] = result
                        else:
                            c.log(f"[TVShows @ trakt_list] Item returned None")
                    except Exception as exc:
                        c.log(f"[TVShows @ trakt_list] Error processing item: {exc}")
                        c.log(f"[TVShows @ trakt_list] Traceback: {traceback.format_exc()}")
            self.list = [row for row in slotted if row]
        except Exception as e:
            failure = traceback.format_exc()
            c.log(f'[TVShows @ trakt_list] Traceback: {failure}')
            c.log(f'[TVShows @ trakt_list] Exception: {e}')
            is_search = '/search/' in (url or '')

        c.log(f"[TVShows @ trakt_list] Returning {len(self.list)} items")

        # Browse lists: A-Z. Search: keep Trakt relevance (Company Retreat ≠ buried).
        if not is_search:
            self.list = sorted(self.list, key=lambda k: utils.title_key(k['title']))

        return self.list

    def trakt_user_list(self, url, user):
        try:
            if trakt.is_own_lists_index_url(url):
                items = trakt.get_all_user_lists()
            else:
                items = trakt.getTraktAsJson(url)
        except Exception:
            items = None

        if not items:
            return

        for item in items:
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




    def _handle_simkl_list(self, url: str, idx: bool, create_directory: bool, page: int = 1):
        """Simkl plantowatch / completed / watching / ratings → TV show directory.

        Skip full worker (credits/translations). Paginate locally (40/page),
        then light enrich: TMDB basics + optional Fanart.tv logos.
        """
        status_map = {
            'simklwatchlist': 'plantowatch',
            'simklhistory': 'completed',
            'simklwatching': 'watching',
            'simklhold': 'hold',
            'simkldropped': 'dropped',
        }
        page_size = 40
        try:
            page = max(1, int(page) or 1)
        except (TypeError, ValueError):
            page = 1

        if url == 'simklratings':
            c.log(f"[TVShows] Simkl ratings (page={page})")
            all_items = simkl.crew_rated_shows() or []
            all_items = sorted(
                all_items,
                key=lambda k: (-int(k.get('user_rating') or 0), utils.title_key(k.get('title', ''))),
            )
            status = 'ratings'
        else:
            status = status_map.get(url, 'plantowatch')
            c.log(f"[TVShows] Simkl list '{url}' (status={status}, page={page})")
            all_items = simkl.crew_show_items(status) or []
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
            f"[TVShows] Simkl {status}: showing {len(self.list)}/{total} "
            f"(page {page}/{total_pages})"
        )
        if create_directory:
            self.tvshowDirectory(self.list)
        return self.list

    @staticmethod
    def _simkl_gap(value):
        return value in (None, '', '0', 0)

    def _simkl_list_enrich(self):
        """
        Gap-fill Simkl TV list rows: TMDB basics (+ Fanart.tv logos when enabled).

        Keeps Simkl poster / episode totals. Uses metacache so warm opens skip
        network. Never runs full super_info (no credits / translations crawl).
        """
        items = self.list
        if not items:
            return

        self.meta = []
        # Simkl library totals (e.g. franchise 16) must survive metacache.merge —
        # shared metacache stores TMDB catalog counts for search/browse honesty.
        simkl_episode_totals = {
            idx: item.get('episodes')
            for idx, item in enumerate(items)
            if not self._simkl_gap(item.get('episodes'))
        }
        for item in items:
            item['metacache'] = False
        self.list = metacache.fetch(self.list, self.lang, self.user)
        items = self.list
        for idx, eps in simkl_episode_totals.items():
            if idx < len(items):
                items[idx]['episodes'] = eps

        meta_keys = (
            'seasons', 'episodes', 'fanart', 'plot', 'tagline', 'genre',
            'studio', 'rating', 'votes', 'premiered', 'status', 'duration',
            'clearlogo', 'clearart', 'banner', 'landscape',
        )
        fanart_on = c.get_setting('fanart') == 'true'
        meta_lock = threading.Lock()

        def needs_work(item):
            if item.get('metacache') and not self._simkl_gap(item.get('seasons')):
                # Warm metacache usually has enough; still fill logo gaps if fanart on
                if not fanart_on:
                    return False
                return any(self._simkl_gap(item.get(k)) for k in ('clearlogo', 'clearart', 'banner', 'landscape'))
            return any(self._simkl_gap(item.get(k)) for k in meta_keys)

        def fill_gap(item, key, value):
            if self._simkl_gap(item.get(key)) and value not in (None, '', '0', 0):
                item[key] = value if not isinstance(value, int) else str(value)

        def enrich_one(index):
            item = items[index]
            try:
                if not needs_work(item):
                    return
                tmdb = str(item.get('tmdb') or '0')
                if tmdb == '0':
                    return

                show = TVShow({
                    'tmdb': tmdb,
                    'imdb': item.get('imdb') or '0',
                    'tvdb': item.get('tvdb') or '0',
                    'title': item.get('title') or '',
                    'poster': item.get('poster') or '0',
                    'fanart': item.get('fanart') or '0',
                })
                show.fetch_tmdb_basics()

                # Never stomp Simkl episode totals when present
                fill_gap(item, 'seasons', str(show.total_seasons) if show.total_seasons else None)
                if self._simkl_gap(item.get('episodes')) and show.total_episodes:
                    item['episodes'] = str(show.total_episodes)

                fill_gap(item, 'fanart', show.fanart)
                fill_gap(item, 'plot', show.plot)
                fill_gap(item, 'tagline', show.tagline)
                fill_gap(item, 'genre', show.genre)
                fill_gap(item, 'studio', show.studio)
                fill_gap(item, 'rating', show.rating)
                fill_gap(item, 'votes', show.votes)
                fill_gap(item, 'premiered', show.premiered)
                fill_gap(item, 'status', show.status)
                fill_gap(item, 'duration', show.duration)
                fill_gap(item, 'imdb', show.imdb)
                fill_gap(item, 'tvdb', show.tvdb)
                if self._simkl_gap(item.get('poster')) and show.poster not in (None, '', '0'):
                    item['poster'] = show.poster

                if fanart_on:
                    tvdb = str(item.get('tvdb') or '0')
                    if tvdb != '0':
                        art = cache.get(fanart_tv.get_fanart_tv_art, 72, tvdb) or {}
                        for key in ('clearlogo', 'clearart', 'banner', 'landscape'):
                            fill_gap(item, key, art.get(key))
                        fill_gap(item, 'fanart', art.get('fanart'))

                # Persist for warm reopen (same shape as super_info metacache rows).
                # Episode/season counts in metacache = TMDB catalog, not Simkl franchise totals.
                cache_item = {
                    k: item[k] for k in (
                        'title', 'year', 'imdb', 'tmdb', 'tvdb', 'poster', 'fanart',
                        'banner', 'clearlogo', 'clearart', 'landscape', 'premiered',
                        'studio', 'genre', 'duration', 'rating', 'votes', 'plot',
                        'status', 'tagline',
                    ) if k in item and item[k] not in (None, '', '0')
                }
                if show.total_seasons:
                    cache_item['seasons'] = str(show.total_seasons)
                if show.total_episodes:
                    cache_item['episodes'] = str(show.total_episodes)
                elif not self._simkl_gap(item.get('episodes')):
                    # No TMDB basics this pass — omit episodes so we don't poison search
                    pass
                row = {
                    'imdb': item.get('imdb') or '0',
                    'tmdb': tmdb,
                    'tvdb': item.get('tvdb') or '0',
                    'lang': self.lang,
                    'user': self.user,
                    'item': cache_item,
                }
                with meta_lock:
                    self.meta.append(row)
            except Exception as e:
                c.log(f"[TVShows] Simkl list enrich skip: {e}", 1)

        try:
            max_nr = c.get_max_threads(len(items))
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_nr) as executor:
                list(executor.map(enrich_one, range(len(items))))
        except Exception as e:
            c.log(f"[TVShows] Simkl list enrich error: {e}", 1)
            for i in range(len(items)):
                enrich_one(i)

        if self.meta:
            metacache.insert(self.meta)

    def collection_list(self):
        # collection = trakt.get_collection('shows')
        self.list = []

        trakt.get_trakt_collection('shows')
        collection = trakt.get_collection('shows') or []
        if len(collection) == 0:
            return self.list

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
                                'trakt': _trakt, 'slug': slug,
                                'seasons': '0', 'episodes': '0',
                                })
            except Exception as e:
                c.log(f"Exception raised in collection_list() with e = {e}")

        c.log(f"[TVShows.collection_list] {len(self.list)} show(s) from local mirror")
        return self.list





    ####cm#
    # new def for tmdb lists
    def list_tmdb_list(self, url, tid=0):
        try:
            if tid != 0:
                url = url % tid

            result = http_client.tmdb_get_json(url, timeout=15) or {}
            items = result.get('items', [])
        except Exception:
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
                _next = ''
            else:
                _next = '%s&page=%s' % (url.split('&page=', 1)[0], page+1)
        except Exception:
            _next = ''

        for item in items:
            try:
                tmdb = str(item['id'])
                title = item['title']

                originaltitle = item['original_title']
                if not originaltitle: originaltitle = title

                try: rating = str(item['vote_average'])
                except Exception: rating = ''
                if not rating: rating = '0'

                try: votes = str(item['vote_count'])
                except Exception: votes = ''
                if not votes: votes = '0'

                try: premiered = item['release_date']
                except Exception: premiered = ''
                if not premiered: premiered = '0'

                try: year = re.findall(r'(\d{4})', premiered)[0]
                except Exception: year = ''
                if not year: year = '0'

                if premiered == '0':
                    pass
                elif int(re.sub('[^0-9]', '', str(premiered))) > int(re.sub('[^0-9]', '', str(self.today_date))):
                    if self.showunaired != 'true':
                        raise Exception()

                try: plot = item['overview']
                except Exception:  plot = ''
                if not plot: plot = '0'

                try:  poster_path = item['poster_path']
                except Exception: poster_path = ''
                if poster_path:
                    poster = self.tmdb_img_prelink.format('w500', poster_path)
                else:
                    poster = '0'

                backdrop_path = item['backdrop_path'] if 'backdrop_path' in item else ''
                if backdrop_path:
                    fanart = self.tmdb_img_prelink.format('w1280', 'backdrop_path')
                else:
                    fanart = ''

                self.list.append({'title': title, 'originaltitle': originaltitle,
                                    'premiered': premiered, 'year': year, 'rating': rating,
                                    'votes': votes, 'plot': plot, 'imdb': '0', 'tmdb': tmdb,
                                    'tvdb': '0', 'fanart': fanart, 'poster': poster, 'next': _next
                                })
            except Exception:
                pass

        return self.list

    def tmdb_cast_list(self, url):
        try:
            result = http_client.tmdb_get_json(url, timeout=15) or {}
            items = result.get('cast', [])
        except Exception:
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

            if page >= total:
                raise Exception()
            if 'page=' not in url:
                raise Exception()
            _next = f"{url.split('&page=', 1)[0]}&page={page+1}"
        except Exception:
            _next = ''

        c.log(f"[TVShows @ tmdb_cast_list] Processing {len(items)} items")

        def process_show(item):
            """Process single show item using TVShow model"""
            try:
                # Create TVShow from TMDB data
                show = TVShow.from_tmdb_data(item)
                if not show:
                    return None

                # Check unaired filtering
                if show.unaired == 'true' and self.showunaired != 'true':
                    return None

                # Check artwork cache
                tmdb_id = show.tmdb
                if tmdb_id in self.artwork_cache:
                    cached = self.artwork_cache[tmdb_id]
                    show.poster = cached.get('poster', show.poster)
                    show.fanart = cached.get('fanart', show.fanart)
                else:
                    # Cache this show's artwork
                    self.artwork_cache[tmdb_id] = {
                        'poster': show.poster,
                        'fanart': show.fanart
                    }

                # Convert to dict and add next URL
                show_dict = show.to_dict()
                show_dict['next'] = _next
                return show_dict

            except Exception as e:
                c.log(f'[TVShows @ tmdb_cast_list] Error processing show: {e}')
                return None

        # Process shows in parallel with thread cap
        from concurrent.futures import ThreadPoolExecutor, as_completed
        results = []
        max_workers = c.get_max_threads(len(items))

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(process_show, item): item for item in items}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    results.append(result)

        self.list = results
        c.log(f"[TVShows @ tmdb_cast_list] Returning {len(self.list)} items")

        return self.list

    def tvmaze_list(self, url):
        try:
            result = client.request(url)
            result = client.parseDom(result, 'section', attrs={'id': 'this-seasons-shows'})

            items = client.parseDom(result, 'div', attrs={'class': 'content auto cell'})
            items = [client.parseDom(i, 'a', ret='href') for i in items]
            items = [i[0] for i in items if len(i) > 0]
            items = [re.findall(r'/(\d+)/', i) for i in items]
            items = [i[0] for i in items if len(i) > 0]

            _next = ''
            last = []
            nextp = []
            page = int(str(url.split('&page=', 1)[1]))
            _next = '%s&page=%s' % (url.split('&page=', 1)[0], page+1)
            last = client.parseDom(result, 'li', attrs = {'class': 'last disabled'})
            nextp = client.parseDom(result, 'li', attrs = {'class': 'next'})
            if last != [] or nextp == []:
                _next = ''
        except Exception:
            return

        c.log(f"[TVShows @ tvmaze_list] Processing {len(items)} items")

        def process_show(tvmaze_id):
            """Process single TVMaze show by ID"""
            try:
                url = self.tvmaze_info_link % tvmaze_id
                item = self.session.get(url, timeout=16).json()

                # Create TVShow from TVMaze data
                show = TVShow.from_tvmaze_data(item)
                if not show:
                    return None

                # Convert to dict and add next URL and content type
                show_dict = show.to_dict()
                show_dict['next'] = _next

                # Add content type (not in base model but used by TVMaze)
                try:
                    content = item.get('type', '0')
                    show_dict['content'] = content.lower() if content and content is not None else '0'
                except Exception:
                    show_dict['content'] = '0'

                return show_dict

            except Exception as e:
                c.log(f'[TVShows @ tvmaze_list] Error processing show {tvmaze_id}: {e}')
                return None

        # Process shows in parallel with thread cap
        from concurrent.futures import ThreadPoolExecutor, as_completed
        results = []
        max_workers = c.get_max_threads(len(items))

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(process_show, tvmaze_id): tvmaze_id for tvmaze_id in items}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    results.append(result)

        # Sort by title
        self.list = sorted(results, key=lambda k: k.get('title', '').lower())
        c.log(f"[TVShows @ tvmaze_list] Returning {len(self.list)} items")

        return self.list

    def tmdb_scifi_fantasy_list(self, url):
        """Sci-Fi & Fantasy dual discover: EN/US|UK|AU (vote>=25) + intl (vote>=100); merge by id, popularity order."""
        try:
            page = 1
            m = re.search(r'[?&]page=(\d+)', url or '')
            if m:
                page = int(m.group(1))

            def with_page(link):
                if re.search(r'[?&]page=\d+', link):
                    return re.sub(r'([?&]page=)\d+', rf'\g<1>{page}', link)
                return f'{link}&page={page}'

            home_url = with_page(self.tmdb_scifi_fantasy_home_link)
            intl_url = with_page(self.tmdb_scifi_fantasy_intl_link)

            home = http_client.tmdb_get_json(home_url, timeout=16) or {}
            intl = http_client.tmdb_get_json(intl_url, timeout=16) or {}

            by_id = {}
            for item in (home.get('results') or []) + (intl.get('results') or []):
                tid = item.get('id')
                if tid is None:
                    continue
                prev = by_id.get(tid)
                if prev is None or float(item.get('popularity') or 0) > float(prev.get('popularity') or 0):
                    by_id[tid] = item

            items = sorted(
                by_id.values(),
                key=lambda i: float(i.get('popularity') or 0),
                reverse=True,
            )

            home_pages = int(home.get('total_pages') or 0)
            intl_pages = int(intl.get('total_pages') or 0)
            total_pages = max(home_pages, intl_pages, 1)
            page_num = int(home.get('page') or intl.get('page') or page)

            c.log(
                f'[TVShows @ tmdb_scifi_fantasy_list] page={page_num} '
                f'home={len(home.get("results") or [])} intl={len(intl.get("results") or [])} '
                f'merged={len(items)}'
            )

            # Prefer home URL for next-page so get() keeps hitting this dual-fetch path
            list_url = with_page(self.tmdb_scifi_fantasy_home_link)
            return self.tmdb_list(
                list_url,
                items=items,
                page=page_num,
                total_pages=total_pages,
            )
        except Exception as e:
            failure = traceback.format_exc()
            c.log(f'[TVShows @ tmdb_scifi_fantasy_list] Traceback: {failure}')
            c.log(f'[TVShows @ tmdb_scifi_fantasy_list] Exception: {e}')
            return []

    def tmdb_list(self, url, tid=0, items=None, page=None, total_pages=None):
        try:
            if tid != 0:
                url = url % tid

            for i in re.findall(r'date\[(\d+)\]', url):
                url = url.replace(
                    f'date[{i}]',
                    (self.datetime - datetime.timedelta(days=int(i))).strftime(
                        '%Y-%m-%d'
                    ),
                )

            if items is None:
                result = http_client.tmdb_get_json(url, timeout=16) or {}
                items = result.get('results', [])
                page = int(result.get('page') or 0)
                total_pages = int(result.get('total_pages') or 0)
            else:
                page = int(page or 0)
                total_pages = int(total_pages or 0)
        except Exception:
            return

        try:
            # Store pagination info for "Page X of Y" display
            if page > 0 and total_pages > 0:
                self.pagination = {
                    'page': page,
                    'page_count': total_pages
                }

            if page >= total_pages or 'page=' not in url:
                raise Exception() # sourcery skip: raise-specific-error
            _next = f"{(url.split('&page=', 1)[0])}&page={(page+1)}"
        except Exception:
            _next = ''

        c.log(f"[TVShows @ tmdb_list] showunaired setting = {self.showunaired}")

        # Import TVShow model
        from ..models.tvshow import TVShow

        def add_to_list(item):
            try:
                # Get need_extended flag
                need_extended = '/search/' in url or 'number_of_episodes' not in item

                # Create TVShow from TMDB data
                show = TVShow.from_tmdb_data(item)
                if not show:
                    c.log("[TVShows] Failed to create show from TMDB data")
                    return None

                # Fetch extended info if needed (seasons, episodes)
                if need_extended or show.total_seasons == 0:
                    # Check artwork cache first
                    if show.tmdb in self.artwork_cache:
                        cached = self.artwork_cache[show.tmdb]
                        show.total_seasons = cached.get('seasons', show.total_seasons)
                        show.total_episodes = cached.get('episodes', show.total_episodes)
                        # Keep TMDB poster/fanart if already set, only use cache if missing
                        if show.poster in ['0', '', None]:
                            show.poster = cached.get('poster', show.poster)
                        if show.fanart in ['0', '', None]:
                            show.fanart = cached.get('fanart', show.fanart)
                        c.log(f"[TVShows] (OK) Cache hit for: {show.title}")
                    else:
                        # Fetch extended info from TMDB (with aggregate_credits for cast/crew)
                        r_extended = http_client.tmdb_get_json(
                            self.tmdb_api_link % show.tmdb,
                            timeout=16
                        ) or {}

                        if r_extended:
                            # Parse the full TMDB response to get all metadata including crew
                            show._parse_tmdb_response(r_extended)
                            # Cache the data
                            self.artwork_cache[show.tmdb] = {
                                'poster': show.poster,
                                'fanart': show.fanart,
                                'seasons': show.total_seasons,
                                'episodes': show.total_episodes
                            }

                # Check if unaired and skip if setting says so
                if show.unaired == 'true' and self.showunaired != 'true':
                    c.log(f"[TVShows @ tmdb_list] Skipping unaired show: {show.title}")
                    return None

                # Convert to dict and add next page link
                show_dict = show.to_dict()
                show_dict['next'] = _next
                show_dict['seasons'] = str(show.total_seasons)
                show_dict['episodes'] = str(show.total_episodes)

                return show_dict

            except Exception as e:

                c.log(f'[TVShows @ tmdb_list] Error processing item: {e}')
                c.log(f'[TVShows @ tmdb_list] Traceback: {traceback.format_exc()}')
                return None

        try:
            max_nr = c.get_max_threads(len(items))
            c.log(f"[TVShows @ tmdb_list] Processing {len(items)} items with {max_nr} workers")

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_nr) as executor:
                future_to_index = {
                    executor.submit(add_to_list, item): idx
                    for idx, item in enumerate(items)
                }
                ordered = [None] * len(items)

                for future in concurrent.futures.as_completed(future_to_index):
                    idx = future_to_index[future]
                    item = items[idx]
                    try:
                        result = future.result()
                        if result:
                            ordered[idx] = result
                        else:
                            c.log(f"[TVShows @ tmdb_list] Item returned None: {item.get('name', 'Unknown')}")
                    except Exception as exc:
                        c.log(f"[TVShows @ tmdb_list] Error processing item {item.get('name', 'Unknown')}: {exc}")

                        c.log(f"[TVShows @ tmdb_list] Traceback: {traceback.format_exc()}")

                # Keep TMDB API order (parallel workers otherwise scramble sort_by)
                self.list = [entry for entry in ordered if entry is not None]
        except Exception as e:
            failure = traceback.format_exc()
            c.log(f'[TVShows @ tmdb_list] Traceback: {failure}')
            c.log(f'[TVShows @ tmdb_list] Exception: {e}')
            pass

        c.log(f"[TVShows @ tmdb_list] Returning {len(self.list)} items")
        return self.list

    def widget_worker(self):
        """Widget listing: SQLite metacache first, then capped TMDB for missing posters."""
        if self.list is None:
            self.list = []
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
            c.log(f'[TVShows] Widget worker: {total} items, all posters from cache')
            return

        cap = min(len(misses), 6)
        c.log(
            f'[TVShows] Widget worker: {total} items, {cached} cached, '
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
                        c.log(f"[TVShows @ widget_worker] Error processing item {i}: {exc}")
        except Exception as e:
            failure = traceback.format_exc()
            c.log(f'[TVShows @ widget_worker] Traceback: {failure}')
            c.log(f'[TVShows @ widget_worker] Exception: {e}')

        if self.meta:
            metacache.insert(self.meta)

    def worker(self):
        self.meta = []
        if self.list is None:
            self.list = []
        total = len(self.list)

        if total == 0:
            control.infoDialog('List returned no relevant results', icon='INFO', sound=False)
            return

        for i in range(total):
            self.list[i].update({'metacache': False})

        self.list = metacache.fetch(self.list, self.lang, self.user)

        # cm changed worker - 15-04-2025
        #for r in range(0, total, 40):
        #    threads = []
        #    for i in range(r, r+40):
        #        if i < total:
        #            threads.append(workers.Thread(self.super_info(i)))

        #[i.start() for i in threads]
        #[i.join() for i in threads]

        try:
            result = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(total, 40)) as executor:
                #futures = {executor.submit(self.super_info, i): item for item in items}
                futures = {executor.submit(self.super_info, i): i for i in range(total)}

                #for future in concurrent.futures.as_completed(futures):
                #    i = futures[future]
                #    try:
                #        result = future.result()
                #        result.append(result)

                #    except Exception as exc:
                #        c.log(f"Error processing item {i}: {exc}")
        except Exception as e:
            failure = traceback.format_exc()
            c.log(f'[TVShows @ worker] Traceback: {failure}')
            c.log(f'[TVShows @ worker] Exception: {e}')
            pass












        if self.meta:
            metacache.insert(self.meta)

    def super_info(self, i):
        try:
            item_data = self.list[i]
            if item_data.get('metacache'):
                # Older metacache rows predate aggregate_credits / rating enrichment.
                if item_data.get('rating') and item_data.get('poster') not in (None, '', '0'):
                    return
                item_data['metacache'] = False

            imdb = item_data.get('imdb', '0')
            tmdb = item_data.get('tmdb', '0')
            tvdb = item_data.get('tvdb', '0')
            list_title = item_data['title']

            if tmdb == '0' and imdb != '0':
                try:
                    url = self.tmdb_by_imdb % imdb
                    result = self.session.get(url, timeout=10).json()
                    tmdb = str(result['tv_results'][0].get('id', '0'))
                except Exception:
                    pass

            if tmdb == '0':
                try:
                    search_url = self.search_link % quote(list_title) + '&first_air_date_year=' + item_data['year']
                    result = client.request(search_url)
                    results = json.loads(result)['results']
                    show = next((r for r in results if cleantitle.get(r.get('name')) == cleantitle.get(list_title)), {})
                    tmdb = str(show.get('id', '0'))
                except Exception:
                    pass

            if tmdb == '0':
                raise Exception()

            url = self.tmdb_api_link % tmdb
            url = url if self.lang == 'en' else url + ',translations'
            item = self.session.get(url, timeout=10).json()

            if not item:
                raise Exception()

            if imdb == '0':
                imdb = item['external_ids'].get('imdb_id', '0')

            if tvdb == '0':
                tvdb = str(item['external_ids'].get('tvdb_id', '0'))

            original_language = item.get('original_language', '')
            translations = item.get('translations', {}).get('translations', [])
            en_trans_item = next((x['data'] for x in translations if x['iso_639_1'] == 'en'), {})

            name = item.get('name', '')
            original_name = item.get('original_name', '')
            en_trans_name = en_trans_item.get('name', '') if self.lang != 'en' else None

            title = label = name if self.lang == 'en' else en_trans_name or original_name
            if original_language != self.lang:
                label = en_trans_name or name

            title = title or list_title
            label = label or list_title

            plot = item.get('overview', item_data.get('plot', ''))
            tagline = item.get('tagline', '0')

            if self.lang != 'en':
                en_plot = en_trans_item.get('overview', '')
                plot = plot if plot != '0' else en_plot

                en_tagline = en_trans_item.get('tagline', '')
                tagline = tagline if tagline != '0' else en_tagline

            premiered = item.get('first_air_date', '0')
            year_match = re.search(r'(\d{4})', premiered)
            year = year_match.group(1) if year_match else '0'

            status = item.get('status', '0')
            networks = item.get('networks') or []
            studio = networks[0].get('name', '0') if networks else '0'

            genres = [d['name'] for d in item.get('genres', [])] or ['0']
            genre = ' / '.join(genres)

            countries = [c['name'] for c in item.get('production_countries', [])] or ['0']
            country = ' / '.join(countries)

            aggregate_credits = item.get('aggregate_credits', {})
            crew_list = aggregate_credits.get('crew', [])
            directors = []
            writers = []
            for person in crew_list:
                for job_info in person.get('jobs', []):
                    job = job_info.get('job', '') if isinstance(job_info, dict) else str(job_info)
                    if job == 'Director':
                        directors.append(person.get('name', ''))
                    elif job in ('Writer', 'Screenplay', 'Story'):
                        writers.append(person.get('name', ''))
            if not directors:
                directors = [p.get('name', '') for p in item.get('created_by', []) if p.get('name')]
            director = ' / '.join(list(dict.fromkeys(d for d in directors if d))) if directors else '0'
            writer = ' / '.join(list(dict.fromkeys(w for w in writers if w))) if writers else '0'

            episode_runtimes = item.get('episode_run_time') or []
            duration = str(episode_runtimes[0]) if episode_runtimes else '0'

            rating = str(item.get('vote_average') or '0')
            votes = str(item.get('vote_count') or '0')

            ratings = item.get('content_ratings', {}).get('results', [])
            mpaa = next((d['rating'] for d in ratings if d['iso_3166_1'] == 'US'), '0')

            plot = item.get('overview', item_data.get('plot', control.lang(32623)))
            plot = client.replaceHTMLCodes(plot)

            tagline = client.replaceHTMLCodes(tagline) if tagline != '0' else '0'

            if self.lang != 'en':
                en_title = en_trans_item.get('name', '')
                if en_title and original_language != 'en':
                    title = label = en_title

                en_plot = en_trans_item.get('overview', '')
                plot = plot if plot != '0' else client.replaceHTMLCodes(en_plot)

                en_tagline = en_trans_item.get('tagline', '')
                tagline = tagline if tagline != '0' else client.replaceHTMLCodes(en_tagline)

            cast = item.get('aggregate_credits', {}).get('cast', [])[:30]
            castwiththumb = [
                {'name': person['name'], 'role': person['roles'][0]['character'], 'thumbnail': self.tmdb_img_link % (c.tmdb_profilesize, person['profile_path']) if person.get('profile_path') else ''}
                for person in cast
            ] or '0'

            poster1 = item_data.get('poster', '0')
            poster_path = item.get('poster_path')
            poster2 = self.tmdb_img_link % (c.tmdb_postersize, poster_path) if poster_path else None
            if not poster2 and tmdb != '0':
                for season_num in (1, 2, 3):
                    try:
                        season_url = (
                            f'{self.tmdb_link}/tv/{tmdb}/season/{season_num}'
                            f'?api_key={self.tmdb_user}&language={self.lang}'
                        )
                        season_data = cache.get(http_client.tmdb_get_json, 24, season_url, 16)
                        season_poster = season_data.get('poster_path') if season_data else None
                        if season_poster:
                            poster2 = self.tmdb_img_link % (c.tmdb_postersize, season_poster)
                            break
                    except Exception:
                        continue

            fanart_path = item.get('backdrop_path')
            fanart1 = self.tmdb_img_link % (c.tmdb_fanartsize, fanart_path) if fanart_path else '0'

            poster3 = fanart2 = None
            banner = clearlogo = clearart = landscape = '0'
            if tvdb != '0':
                temp_art = fanart_tv.get_fanart_tv_art(tvdb=tvdb)
                # clearlogo/clearart: only available from fanart.tv — always fetch
                clearlogo = temp_art.get('clearlogo', '0')
                clearart = temp_art.get('clearart', '0')
                if self.hq_artwork == 'true':
                    # HQ artwork: also use fanart.tv for poster/fanart/banner/landscape
                    poster3 = temp_art.get('poster', '0')
                    fanart2 = temp_art.get('fanart', '0')
                    banner = temp_art.get('banner', '0')
                    landscape = temp_art.get('landscape', '0')

            # '0' is truthy in Python — must not block TMDB poster2/fanart1
            poster = next((p for p in (poster3, poster2, poster1) if p not in (None, '', '0')), '0')
            fanart = next((f for f in (fanart2, fanart1) if f not in (None, '', '0')), '0')

            number_of_seasons = str(item.get('number_of_seasons') or 0)
            number_of_episodes = str(item.get('number_of_episodes') or 0)

            item = {
                'title': title, 'originaltitle': title, 'label': label, 'year': year,
                'imdb': imdb, 'tmdb': tmdb, 'tvdb': tvdb, 'poster': poster, 'fanart': fanart,
                'banner': banner, 'clearlogo': clearlogo, 'clearart': clearart,
                'landscape': landscape, 'premiered': premiered, 'studio': studio,
                'genre': genre, 'duration': duration, 'mpaa': mpaa, 'writer': writer,
                'director': director, 'country': country, 'rating': rating, 'votes': votes,
                'castwiththumb': castwiththumb, 'plot': plot, 'status': status, 'tagline': tagline
            }

            item = {k: v for k, v in item.items() if v != '0'}

            self.list[i].update(item)
            self.list[i]['seasons'] = number_of_seasons or self.list[i].get('seasons') or '0'
            self.list[i]['episodes'] = number_of_episodes or self.list[i].get('episodes') or '0'
            if rating != '0':
                self.list[i]['rating'] = rating
            if votes != '0':
                self.list[i]['votes'] = votes
            meta = {'imdb': imdb, 'tmdb': tmdb, 'tvdb': tvdb, 'lang': self.lang, 'user': self.user, 'item': item}
            self.meta.append(meta)

        except Exception as e:

            c.log(f'[CM Error] Traceback: {traceback.format_exc()}')
            c.log(f'[CM Error] Exception: {e}')

    def super_info_original(self, i):
        try:
            if self.list[i]['metacache'] is True:
                return

            imdb = self.list[i]['imdb'] if 'imdb' in self.list[i] else '0'
            tmdb = self.list[i]['tmdb'] if 'tmdb' in self.list[i] else '0'
            tvdb = self.list[i]['tvdb'] if 'tvdb' in self.list[i] else '0'

            list_title = self.list[i]['title']

            #trying to fetch a missing tmdb id
            if tmdb == '0' and imdb != '0':
                try:
                    url = self.tmdb_by_imdb % imdb
                    result = self.session.get(url, timeout=10).json()

                    tv_results = result['tv_results'][0]
                    tmdb = str(tv_results['id']) or '0'
                except Exception:
                    pass

            if tmdb == '0':
                try:
                    url = self.search_link % (quote(self.list[i]['title'])) + '&first_air_date_year=' + self.list[i]['year']
                    result = client.request(url)
                    result = json.loads(result)
                    results = result['results']
                    show = [r for r in results if cleantitle.get(r.get('name'))\
                        == cleantitle.get(list_title)][0]# and re.findall('(\d{4})', r.get('first_air_date'))[0] == self.list[i]['year']][0]
                    tmdb = str(show.get('id')) or '0'

                except Exception:
                    pass

            if tmdb == '0':
                raise Exception()


            en_url = self.tmdb_api_link % (tmdb)
            foreign_url = en_url + ',translations'

            url = en_url if self.lang == 'en' else foreign_url
            r = self.session.get(url, timeout=10)
            r.raise_for_status()
            r.encoding = 'utf-8'
            item = r.json()

            if item is None:
                raise Exception()

            if imdb == '0' and 'imdb_id' in item['external_ids']:
                imdb = item['external_ids']['imdb_id'] or '0'

            if tvdb == '0' and 'tvdb_id' in item['external_ids']:
                tvdb = str(item['external_ids']['tvdb_id']) or '0'

            original_language = item.get('original_language', '')

            if self.lang == 'en':
                en_trans_item = {}
            else:
                try:
                    translations = item['translations']['translations']
                    en_trans_item = [x['data'] for x in translations if x['iso_639_1'] == 'en'][0]
                except Exception:
                    en_trans_item = {}

            name = item.get('name', '')
            original_name = item.get('original_name', '')
            #en_trans_name = en_trans_item.get('name', '') if not self.lang == 'en' else None
            en_trans_name = None if self.lang == 'en' else en_trans_item.get('name', '')

            if self.lang == 'en':
                title = label = name
            else:
                title = en_trans_name or original_name
                if original_language == self.lang:
                    label = name
                else:
                    label = en_trans_name or name
            if not title:
                title = list_title
            if not label:
                label = list_title

            plot = item['overview'] or self.list[i]['plot'] or ''
            tagline = item.get('tagline', '') or '0'

            if not self.lang == 'en':
                if plot == '0':
                    en_plot = en_trans_item.get('overview') or ''
                    if en_plot:
                        plot = en_plot

                if tagline == '0':
                    en_tagline = en_trans_item.get('tagline') or ''
                    if en_tagline:
                        tagline = en_tagline

            premiered = item['first_air_date'] or '0'

            try:
                year = re.findall(r'(\d{4})', premiered)[0]
            except Exception:
                year = ''
            if not year :
                year = '0'

            status = item['status'] or '0'
            studio = item['networks'][0]['name'] or '0'

            genres = item['genres']
            if genres:
                genres = [d['name'] for d in genres]
                genre = ' / '.join(genres)
            else:
                genre = '0'

            countries = item['production_countries'] or []
            if countries:
                countries = [c['name'] for c in countries]
                country = ' / '.join(countries)
            else:
                country = '0'

            directors = item.get('crew', [])
            if directors:
                directors = [d['name'] for d in directors if d['job'] == 'Director']
                director = ' / '.join(directors)
            else:
                director = '0'

            writers = item.get('crew', [])
            if writers:
                writers = [d['name'] for d in writers if d['job'] == 'Writer']
                writer = ' / '.join(writers)
            else:
                writer = '0'

            duration = str(item['episode_run_time'][0]) or '0'

            m = item['content_ratings']['results']
            if m:
                mpaa = [d['rating'] for d in m if d['iso_3166_1'] == 'US'][0]
            else:
                mpaa = '0'

            try:
                status = item['status']
            except Exception:
                status = ''
            if not status:
                status = '0'

            plot = item['overview'] if 'overview' in item and item['overview'] != ''\
                else self.list[i]['plot']
            if not plot:
                plot = c.lang(32623)
            plot = client.replaceHTMLCodes(str(plot))

            tagline = item['tagline'] or '0'
            if tagline != '0':
                tagline = client.replaceHTMLCodes(str(tagline))

            if not self.lang == 'en':
                try:
                    translations = item.get('translations', {})
                    translations = translations.get('translations', [])
                    trans_item = [x['data'] for x in translations if x.get('iso_639_1') == 'en'][0]

                    en_title = trans_item.get('name', '')
                    if en_title and not original_language == 'en':
                        title = label = str(en_title)

                    if plot == '0':
                        en_plot = trans_item.get('overview', '')
                        if en_plot:
                            plot = client.replaceHTMLCodes(str(en_plot))

                    if tagline == '0':
                        en_tagline = trans_item.get('tagline', '')
                        if en_tagline:
                            tagline = client.replaceHTMLCodes(str(en_tagline))
                except Exception:
                    pass

            castwiththumb = []
            try:
                cast = item['aggregate_credits']['cast'][:30]
                for person in cast:
                    _icon = person['profile_path']
                    icon = self.tmdb_img_link % (c.tmdb_profilesize, _icon) if _icon else ''
                    castwiththumb.append({
                        'name': person['name'],
                        'role': person['roles'][0]['character'],
                        'thumbnail': icon
                        })
            except Exception:
                pass
            if not castwiththumb:
                castwiththumb = '0'

            poster1 = self.list[i].get('poster', '0') or '0'

            poster_path = item.get('poster_path')
            if poster_path:
                poster2 = self.tmdb_img_link % (c.tmdb_postersize, poster_path)
                poster2 = str(poster2)
            else:
                poster2 = None

            fanart_path = item.get('backdrop_path')
            if fanart_path:
                fanart1 = self.tmdb_img_link % (c.tmdb_fanartsize, fanart_path)
                fanart1 = str(fanart1)
            else:
                fanart1 = '0'

            poster3 = fanart2 = None
            banner = clearlogo = clearart = landscape = '0'
            if not tvdb == '0':
                temp_art = fanart_tv.get_fanart_tv_art(tvdb=tvdb)
                # clearlogo/clearart: only available from fanart.tv — always fetch
                clearlogo = temp_art.get('clearlogo', '0')
                clearart = temp_art.get('clearart', '0')
                if self.hq_artwork == 'true':
                    # HQ artwork: also use fanart.tv for poster/fanart/banner/landscape
                    poster3 = temp_art.get('poster', '0')
                    fanart2 = temp_art.get('fanart', '0')
                    banner = temp_art.get('banner', '0')
                    landscape = temp_art.get('landscape', '0')

            # '0' is truthy in Python — must not block TMDB poster2/fanart1
            poster = next((p for p in (poster3, poster2, poster1) if p not in (None, '', '0')), '0')
            fanart = next((f for f in (fanart2, fanart1) if f not in (None, '', '0')), '0')

            item = {
                    'title': title, 'originaltitle': title, 'label': label, 'year': year,
                    'imdb': imdb, 'tmdb': tmdb, 'tvdb': tvdb, 'poster': poster, 'fanart': fanart,
                    'banner': banner, 'clearlogo': clearlogo, 'clearart': clearart,
                    'landscape': landscape, 'premiered': premiered, 'studio': studio,
                    'genre': genre, 'duration': duration, 'mpaa': mpaa, 'writer': writer,
                    'director': director, 'country': country,
                    'castwiththumb': castwiththumb, 'plot': plot, 'status': status, 'tagline': tagline
                    }

            item = dict((k,v) for k, v in item.items() if not v == '0')

            self.list[i].update(item)

            meta = {'imdb': imdb, 'tmdb': tmdb, 'tvdb': tvdb, 'lang': self.lang, 'user': self.user, 'item': item}
            self.meta.append(meta)
        except Exception as e:
            failure = traceback.format_exc()
            c.log(f'[TVShows @ super_info] Traceback: {failure}')
            c.log(f'[TVShows @ super_info] Exception: {e}')
            pass

    def tvshowDirectory(self, items):

        c.log(f"[TV Shows] tvshowDirectory called with {len(items) if items else 0} items")
        sysaddon = sys.argv[0]
        syshandle = int(sys.argv[1])

        if items is None or len(items) == 0:
            c.log(f"[TV Shows] Items is None or empty, closing directory")
            control.add_empty_directory_placeholder(syshandle)
            control.close_directory(
                handle=syshandle,
                content_type='tvshows',
                cacheToDisc=False,
            )
            return

        traktIndicatorInfo = trakt.getTraktIndicatorsInfo()
        addon_poster, addon_banner = c.addon_poster(), c.addon_banner()
        addon_fanart, setting_fanart = c.addon_fanart(), control.setting('fanart')
        addon_clearlogo, addon_clearart = c.addon_clearlogo(), c.addon_clearart()
        addon_discart = c.addon_discart()

        traktCredentials = trakt.get_trakt_credentials_info()
        simkl_live = tracker.simkl_live()
        indicators = playcount.get_tvshow_indicators(refresh=True) if action == 'tvshows' else playcount.get_tvshow_indicators()
        flatten = control.setting('flatten.tvshows') or 'false'

        #cm - menus
        findSimilar = c.lang(32100)
        playRandom = c.lang(32535)
        queueMenu = c.lang(32065)
        watchedMenu, unwatchedMenu = playcount.watched_menu_labels()
        traktManagerMenu = c.lang(32515)
        simklManagerMenu = c.lang(90314)
        addToLibrary = c.lang(32551)
        infoMenu = c.lang(32101)
        nextMenu = c.lang(32053)
        playtrailermenu = c.lang(32381)

        for i in items:
            try:
                # Trakt user-list fallback can still pass movie-shaped rows (no season metadata).
                # trakt_list() normally unwraps i['show']; collection_list supplies seasons/episodes placeholders.
                if 'seasons' not in i:
                    continue

                label = i['label'] if 'label' in i and i['label'] != '0' else i['title']
                status = i.get('status', '')
                try:
                    premiered = i['premiered']
                    if (premiered == '0' and status in ['Upcoming', 'In Production', 'Planned']) or (int(re.sub('[^0-9]', '', premiered)) > int(re.sub('[^0-9]', '', str(self.today_date)))):

                        # Add cyan premiere date if known (e.g. from TMDB for anticipated shows)
                        try:
                            premiered_digits = re.sub('[^0-9]', '', premiered)
                            if premiered != '0' and premiered_digits and len(premiered_digits) >= 8:
                                from datetime import datetime as dt
                                release_dt = dt.strptime(premiered, '%Y-%m-%d')
                                formatted_date = release_dt.strftime('%b %d, %Y')
                                label += f' {color("(" + str(formatted_date) + ")", 'secondary')}'
                        except Exception:
                            pass

                        # Get color for unaired shows from centralized constants
                        colornr = COLOR_STRING_IDS[int(control.setting('unaired.identify'))]
                        unairedcolor = re.sub(r"\][\w\s]*\[", "][I]%s[/I][", control.lang(int(colornr)))
                        if not unairedcolor or '%s' not in unairedcolor:
                            unairedcolor = f'{color("[I]%s[/I]", 'warning')}'
                        label = unairedcolor % label
                except Exception:
                    pass


                _empty = [None, '0', '']

                poster = i['poster'] if 'poster' in i and i['poster'] not in _empty else addon_poster
                fanart = i['fanart'] if 'fanart' in i and i['fanart'] not in _empty else addon_fanart
                clearlogo = i['clearlogo'] if 'clearlogo' in i and i['clearlogo'] not in _empty else addon_clearlogo
                clearart = i['clearart'] if 'clearart' in i and i['clearart'] not in _empty else addon_clearart
                discart = i['discart'] if 'discart' in i and i['discart'] not in _empty else addon_discart

                # Banner fallback chain: banner -> fanart -> addon_banner
                banner1 = i.get('banner', '')
                banner = banner1 if banner1 not in _empty else (fanart or addon_banner)

                # Landscape fallback chain: landscape -> fanart
                if 'landscape' in i and i['landscape'] not in _empty:
                    landscape = i['landscape']
                else:
                    landscape = fanart

                systitle = quote_plus(i['title'])

                meta = {
                    'poster': poster,
                    'fanart': fanart,
                    'banner': banner,
                    'clearlogo': clearlogo,
                    'clearart': clearart,
                    'discart': discart,
                    'landscape': landscape
                    }

                sysmeta = quote_plus(json.dumps(meta))

                imdb = i.get('imdb')
                tmdb = i.get('tmdb')
                year = i.get('year')

                # meta = dict((k,v) for k, v in i.items() if not v == '0')
                # build meta by filtering out '0' values and merging extra keys in one step
                meta = {
                    **{k: v for k, v in i.items() if v != '0'},
                    'code': tmdb,
                    'imdbnumber': imdb,
                    'mediatype': 'tvshow',
                    'tvshowtitle': i.get('title'),
                    'tmdb_id': str(tmdb),
                    'imdb_id': imdb,
                    'dev': 'Classy'
                }

                trailer = c.to_str(i['trailer']) if 'trailer' in i and i['trailer'] not in ('', None) else '0'

                trailer_url = quote(trailer) if trailer != '0' else '0'
                search_name = systitle

                if trailer_url == '0':
                    meta['trailer'] = f'{sysaddon}?action=trailer&name={search_name}&imdb={imdb}&tmdb={tmdb}&mediatype=tvshow&meta={sysmeta}'
                else:
                    meta['trailer'] = f'{sysaddon}?action=trailer&name={search_name}&url={trailer_url}&imdb={imdb}&tmdb={tmdb}&mediatype=tvshow&meta={sysmeta}'

                if 'duration' not in meta or meta['duration'] == '0':
                    meta['duration'] = '45'

                try:
                    #meta.update({'duration': str(int(meta['duration']) * 60)})
                    meta['duration'] = str(int(meta['duration']) * 60)

                except Exception:
                    pass
                try:
                    #meta.update({'genre': cleangenre.lang(meta['genre'], self.lang)})
                    meta['genre'] = cleangenre.lang(meta['genre'], self.lang)
                except Exception:
                    pass

                if 'castwiththumb' in i and i['castwiththumb'] != '0':
                    meta.pop('cast', '0')

                try:
                    overlay = int(playcount.getTVShowOverlay(indicators, tmdb))
                    if overlay == 7:
                        meta.update({'playcount': 1, 'overlay': 7})
                    else:
                        meta.update({'playcount': 0, 'overlay': 6})
                except Exception:
                    overlay = 6
                    meta.update({'playcount': 0, 'overlay': 6})

                cm = [
                    (
                        playtrailermenu,
                        f'RunPlugin({sysaddon}?action=trailer&name={systitle}&imdb={imdb}&tmdb={tmdb}&mediatype=tvshow&meta={sysmeta})',
                    ),
                    (
                        playRandom,
                        f'RunPlugin({sysaddon}?action=random&rtype=season&tvshowtitle={systitle}&imdb={imdb}&tmdb={tmdb})',
                    ),
                    (
                        queueMenu,
                        f'RunPlugin({sysaddon}?action=queueItem)',
                    ),
                ]
                if tmdb and str(tmdb) not in ('0', ''):
                    cm.insert(1, (
                        findSimilar,
                        f'Container.Update({sysaddon}?action=tvshows&url={quote_plus(self.related_link % tmdb)})',
                    ))

                if overlay == 6:
                    cm.append((watchedMenu, f'RunPlugin({sysaddon}?action=tvPlaycount&name={systitle}&imdb={imdb}&tmdb={tmdb}&query=7)'))
                else:
                    cm.append((unwatchedMenu, f'RunPlugin({sysaddon}?action=tvPlaycount&name={systitle}&imdb={imdb}&tmdb={tmdb}&query=6)'))
                if simkl_live:
                    cm.append((simklManagerMenu, f'RunPlugin({sysaddon}?action=simklManager&name={systitle}&imdb={imdb}&tmdb={tmdb}&content=tvshow)'))
                elif traktCredentials is True:
                    cm.append((traktManagerMenu, f'RunPlugin({sysaddon}?action=traktManager&name={systitle}&tmdb={tmdb}&content=tvshow)'))
                cm.append((addToLibrary, f'RunPlugin({sysaddon}?action=tvshowToLibrary&tvshowtitle={systitle}&year={year}&imdb={imdb}&tmdb={tmdb})'))

                art ={
                    'icon': poster,
                    'thumb': poster,
                    'poster': poster,
                    'tvshow.poster': poster,
                    'season.poster': poster,
                    'banner': banner,
                    'landscape': landscape
                    }

                art['fanart'] = fanart if setting_fanart == 'true' else c.addon_fanart()
                if 'clearlogo' in i and i['clearlogo'] != '0':
                    art['clearlogo'] = i['clearlogo']
                    art['tvshow.clearlogo'] = i['clearlogo']
                if 'clearart' in i and i['clearart'] != '0':
                    art['clearart'] = i['clearart']

                meta['art'] = art

                try:
                    item = control.item(label=label, offscreen=True)
                except Exception:
                    item = control.item(label=label)


                item.setArt(art)

                # Consolidate index lookup - treat None, 0, and -1 as "not found"
                index = c.search_tmdb_index_in_indicators(tmdb, indicators)
                if index in [None, 0, -1]:
                    watched_episodes = 0
                else:
                    watched_episodes = c.count_watched_items_in_indicators(index, indicators) or 0

                # Get total episodes - validate metadata value
                if 'episodes' in i and i['episodes'] not in [None, 0, '0', '']:
                    try:
                        total_episodes = int(i['episodes'])
                    except (ValueError, TypeError):
                        total_episodes = 0
                else:
                    # Fallback to counting from indicators if available
                    total_episodes = c.count_total_items_in_indicators(index, indicators) or 0

                # Ensure numeric ints to avoid type errors
                try:
                    total_episodes = int(total_episodes)
                except (ValueError, TypeError):
                    total_episodes = 0
                try:
                    watched_episodes = int(watched_episodes)
                except (ValueError, TypeError):
                    watched_episodes = 0

                # Clamp to valid ranges
                total_episodes = max(total_episodes, 0)
                watched_episodes = max(watched_episodes, 0)

                # Compute unwatched safely
                unwatched_episodes = max(total_episodes - watched_episodes, 0)

                # Fix edge case: if total is known but unwatched is 0, mark all as watched
                # But only if total > 0 (empty shows should stay at 0/0, not 0/0 marked as watched)
                if total_episodes > 0 and unwatched_episodes == 0:
                    watched_episodes = total_episodes

                # Validate before setting properties - ensure str conversion for Kodi
                try:
                    item.setProperties({
                        'WatchedEpisodes': str(watched_episodes),
                        'UnWatchedEpisodes': str(unwatched_episodes)
                    })
                except Exception as e:
                    c.log(f'[TVShows] Error setting watched properties for {systitle}: {e}', 1)

                # Set total counts - handle string '0' vs None
                try:
                    season_count = i.get('seasons')
                    if season_count in [None, '0', '', 0]:
                        season_count = 0
                    else:
                        season_count = int(season_count)

                    item.setProperties({
                        'TotalSeasons': str(season_count),
                        'TotalEpisodes': str(total_episodes)
                    })
                except Exception as e:
                    c.log(f'[TVShows] Error setting total properties for {systitle}: {e}', 1)

                genre = i.get('genre') or '0'

                genres = c.string_split_to_list(genre) if genre != '0' else []
                studio = i.get('studio') or '0'

                studios = c.string_split_to_list(studio) if studio != '0' else []
                country = i.get('country') or '0'

                if country != '0':
                    countries = c.string_split_to_list(country)
                    countries = [x.upper() for x in countries]
                else:
                    countries = []

                director = i.get('director') or '0'

                if director != '0':
                    directors = c.string_split_to_list(director)
                else:
                    directors = []

                writer = i.get('writer') or '0'

                if writer != '0':
                    writers = c.string_split_to_list(writer)
                else:
                    writers = []


                info_tag = ListItemInfoTag(item, 'video')
                infolabels = control.tagdataClean(meta)

                infolabels.update({'genre': genres, 'studio': studios, 'country': countries, 'director': directors, 'writer': writers})

                info_tag.set_info(infolabels)
                unique_ids = {'imdb': imdb, 'tmdb': str(tmdb)}
                info_tag.set_unique_ids(unique_ids)

                # Set trailer explicitly on InfoTag for information dialog
                trailer_val = meta.get('trailer', 'NOT_IN_META')
                if 'trailer' in meta and meta['trailer'] not in ('0', '', None):
                    info_tag._info_tag.setTrailer(meta['trailer'])

                if 'cast' in meta:
                    cast = meta.get('cast')
                    info_tag.set_cast(cast)
                elif 'castwiththumb' in meta:
                    cast = meta.get('castwiththumb')
                    info_tag.set_cast(meta.get('castwiththumb'))
                else:
                    info_tag.set_cast([])

                item.addContextMenuItems(cm, replaceItems=True)

                if flatten == 'true':
                    url = f"{sysaddon}?action=episodes&tvshowtitle={systitle}&year={year}&imdb={imdb}&tmdb={tmdb}&fanart={fanart}&duration={i['duration']}&meta={sysmeta}"
                    #url = f'{sysaddon}?action=episodes&tvshowtitle={systitle}&year={year}&imdb={imdb}&tmdb={tmdb}&fanart={fanart}&duration={meta["duration"]}&meta={sysmeta}'
                else:
                    #url = '%s?action=seasons&tvshowtitle=%s&year=%s&imdb=%s&tmdb=%s&meta=%s' % (sysaddon, systitle, year, imdb, tmdb, sysmeta)
                    url = f'{sysaddon}?action=seasons&tvshowtitle={systitle}&year={year}&imdb={imdb}&tmdb={tmdb}&meta={sysmeta}'

                control.addItem(handle=syshandle, url=url, listitem=item, isFolder=True)
            except Exception as e:
                failure = traceback.format_exc()
                c.log(f'[TVShows @ tvshowDirectory] Traceback: {failure}')
                c.log(f'[TVShows @ tvshowDirectory] Exception: {e}')




        try:
            url = items[0]['next']
            if url not in ['0', '', None, 'None']:
                icon = c.addon_next()
                q_url = quote_plus(url)
                url = f'{sysaddon}?action=tvshowPage&url={q_url}'

                # Enhanced pagination label: Show "Page X of Y" if info available
                label = nextMenu
                if self.pagination and 'page' in self.pagination and 'page_count' in self.pagination:
                    current_page = self.pagination['page']
                    total_pages = self.pagination['page_count']
                    # Show "Next Page (Page 4 of 259)" instead of just "Next Page"
                    label = f"{nextMenu} (Page {current_page + 1} of {total_pages})"

                try:
                    item = control.item(label=label, offscreen=True)
                except Exception:
                    item = control.item(label=label)

                item.setArt({
                    'icon': icon,
                    'thumb': icon,
                    'poster': icon,
                    'banner': icon,
                    'fanart': addon_fanart
                    })

                control.addItem(handle=syshandle, url=url, listitem=item, isFolder=True)
        except Exception as e:
            c.log(f"Exception in tvshows.adddirectory() #2: error = {e}")


        control.content(syshandle, 'tvshows')
        control.directory(syshandle, cacheToDisc=True)

    def addDirectory(self, items, queue=False):
        syshandle = int(sys.argv[1])
        if items is None or len(items) == 0:
            control.add_empty_directory_placeholder(syshandle)
            control.close_directory(handle=syshandle, content_type='addons', cacheToDisc=False)
            return

        sysaddon = sys.argv[0]
        syshandle = int(sys.argv[1])
        addon_fanart = getattr(c, 'addon_fanart', lambda: '')()
        art_path = getattr(c, 'get_art_path', lambda: '')()


        queueMenu = control.lang(32065)
        playRandom = control.lang(32535)
        addToLibrary = control.lang(32551)

        #{
        # 'name': 'A&E',
        # 'url': 'https://api.themoviedb.org/3/discover/tv?api_key=0049795edb57568b95240bc9e61a9dfc&sort_by=first_air_date.desc&with_networks=129&page=1',
        # 'image': 'https://image.tmdb.org/t/p/original/ptSTdU4GPNJ1M8UVEOtA0KgtuNk.png',
        # 'action': 'tvshows'
        # }
        for i in items:
            try:
                name = i['name']
                plot = i.get('plot') or c.lang(32623)

                if i['image'].startswith('http'):
                    thumb = i['image']
                elif art_path is not None:
                    thumb = os.path.join(art_path, i['image'])
                else:
                    thumb = c.addon_thumb()

                url = f'{sysaddon}?action={i["action"]}'

                url += f'&url={quote_plus(i["url"])}'

                cm = []
                if 'context' in i:
                    cm.append((playRandom, f'RunPlugin({sysaddon}?action=random&rtype=show&url={quote_plus(i["context"])})'))

                if queue is True:
                    cm.append((queueMenu, f'RunPlugin({sysaddon}?action=queueItem)'))

                if 'context' in i:
                    cm.append((addToLibrary, f'RunPlugin({sysaddon}?action=tvshowsToLibrary&url={quote_plus(i["context"])})'))

                try:
                    item = control.item(label=name, offscreen=True)
                except Exception:
                    item = control.item(label=name)

                item.setArt({'icon': thumb, 'thumb': thumb, 'poster': thumb, 'fanart': addon_fanart})
                item.setInfo(type='video', infoLabels={'plot': plot})

                item.addContextMenuItems(cm)

                control.addItem(handle=syshandle, url=url, listitem=item, isFolder=True)
            except Exception as e:
                failure = traceback.format_exc()
                c.log(f'[TVShows @ addDirectory] Traceback: {failure}')
                c.log(f'[TVShows @ addDirectory] Exception: {e}')
                pass
            #except Exception:

                #pass

        control.content(syshandle, 'tvshows')
        control.directory(syshandle, cacheToDisc=True)

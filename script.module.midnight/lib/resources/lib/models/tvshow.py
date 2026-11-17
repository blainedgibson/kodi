# -*- coding: utf-8 -*-
'''
 ***********************************************************
 * Midnight Add-on
 *
 * @package script.module.midnight
 *
 * @copyright (c) 2023-2026, Midnight
 * @license GNU General Public License, version 3 (GPL-3.0)
 *
 ***********************************************************
'''

import json
from .tv_item import TVItem
from ..modules import cache
from ..modules import http_client
from ..modules.crewruntime import c


class TVShow(TVItem):
    """
    Represents a TV show (not individual episodes).
    Used for show-level listings like "In Progress Shows".
    """

    def __init__(self, data=None):
        """
        Initialize TV show instance.

        Args:
            data (dict, optional): Dictionary containing show metadata
        """
        super().__init__(data)

        self.mediatype = 'tvshow'

        # Show-specific properties
        self.tvshowtitle = ''
        self.total_seasons = 0
        self.total_episodes = 0
        self.aired_episodes = 0

        # Progress tracking (for In Progress Shows)
        self.next_season = 0
        self.next_episode = 0
        self.last_watched_season = 0
        self.last_watched_episode = 0
        self.last_watched_at = None  # Timestamp of last watch
        self.progress_percentage = 0  # % of show watched

        if data:
            self.load_from_dict(data)

    def load_from_dict(self, data):
        """
        Load show properties from dictionary.

        Args:
            data (dict): Dictionary containing show metadata
        """
        # Load base properties
        super().load_from_dict(data)

        # Show-specific
        self.tvshowtitle = data.get('tvshowtitle', data.get('title', ''))
        self.title = self.tvshowtitle  # Alias
        self.total_seasons = int(data.get('total_seasons', 0))
        self.total_episodes = int(data.get('total_episodes', 0))
        self.aired_episodes = int(data.get('aired_episodes', 0))

        # Progress tracking
        self.next_season = int(data.get('next_season', 0))
        self.next_episode = int(data.get('next_episode', 0))
        self.last_watched_season = int(data.get('last_watched_season', 0))
        self.last_watched_episode = int(data.get('last_watched_episode', 0))
        self.last_watched_at = data.get('last_watched_at')
        self.progress_percentage = int(data.get('progress_percentage', 0))

    def to_dict(self):
        """
        Convert show to dictionary for Kodi ListItem.

        Returns:
            dict: Dictionary representation of show
        """
        d = super().to_dict()
        d.update({
            'tvshowtitle': self.tvshowtitle,
            'total_seasons': self.total_seasons,
            'total_episodes': self.total_episodes,
            'aired_episodes': self.aired_episodes,
            'next_season': self.next_season,
            'next_episode': self.next_episode,
            'last_watched_season': self.last_watched_season,
            'last_watched_episode': self.last_watched_episode,
            'last_watched_at': self.last_watched_at,
            'progress_percentage': self.progress_percentage,
        })
        return d

    def fetch_tmdb_metadata(self, extended=True):
        """
        Fetch show metadata from TMDB.

        Args:
            extended (bool): Whether to include extended info (external IDs, credits, etc.)

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # Ensure we have TMDB ID
            if self.tmdb == '0':
                self.resolve_tmdb_id()

            if self.tmdb == '0':
                c.log(f"[TVShow] Cannot fetch metadata - no TMDB ID for {self.tvshowtitle}")
                return False

            # Build URL with optional extended info
            if extended:
                url = f"{self.tmdb_link}tv/{self.tmdb}?api_key={self.tmdb_user}&language={self.lang}&append_to_response=external_ids,aggregate_credits,content_ratings,videos"
            else:
                url = f"{self.tmdb_link}tv/{self.tmdb}?api_key={self.tmdb_user}&language={self.lang}"

            # Fetch from TMDB (cached 24 hours)
            response = cache.get(http_client.tmdb_get_json, 24, url, 16)
            if not response:
                c.log(f"[TVShow] Failed to fetch TMDB data for {self.tmdb}")
                return False

            # Parse response
            self._parse_tmdb_response(response)

            # If no show poster, try season posters as fallback (try seasons 1-3)
            if self.poster in ['0', '', None]:
                for season_num in [1, 2, 3]:
                    try:
                        season_url = f"{self.tmdb_link}tv/{self.tmdb}/season/{season_num}?api_key={self.tmdb_user}&language={self.lang}"
                        season_data = cache.get(http_client.tmdb_get_json, 24, season_url, 16)
                        if season_data and (season_poster := season_data.get('poster_path')):
                            self.poster = self.format_tmdb_image(c.tmdb_postersize, season_poster)
                            c.log(f"[TVShow] (OK) Using season {season_num} poster for: {self.tvshowtitle}")
                            break  # Found a poster, stop trying
                    except Exception as e:
                        c.log(f"[TVShow] Season {season_num} fetch failed for {self.tvshowtitle}: {e}")
                        continue  # Try next season

            # Fetch fanart.tv artwork if enabled
            if self.show_fanart and self.tvdb and self.tvdb != '0':
                self.fetch_fanart_tv_art()

            return True

        except Exception as e:
            c.log(f"[TVShow] Error fetching TMDB metadata: {e}")
            return False

    def fetch_tmdb_basics(self):
        """
        Fetch list-level TMDB info in one cached call (no credits / Fanart.tv).

        Fills seasons/episodes, backdrop, plot, tagline, genre, studio, rating,
        premiered, status, duration, and external ids when present.
        Used by Trakt supplemental paths and Simkl list enrich.

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            if self.tmdb == '0':
                c.log(f"[TVShow] Cannot fetch TMDB basics - no TMDB ID for {self.tvshowtitle}")
                return False

            # external_ids keeps this a single hop while supplying tvdb for Fanart.tv
            url = (
                f"{self.tmdb_link}tv/{self.tmdb}?api_key={self.tmdb_user}"
                f"&language={self.lang}&append_to_response=external_ids"
            )

            result = cache.get(http_client.tmdb_get_json, 24, url, 10)
            if not result:
                return False

            self.total_episodes = int(result.get('number_of_episodes') or 0)
            self.total_seasons = int(result.get('number_of_seasons') or 0)

            if self.poster in ['0', '', None]:
                if poster_path := result.get('poster_path'):
                    self.poster = self.format_tmdb_image(c.tmdb_postersize, poster_path)

            if self.fanart in ['0', '', None]:
                if backdrop_path := result.get('backdrop_path'):
                    self.fanart = self.format_tmdb_image(c.tmdb_fanartsize, backdrop_path)

            if self.premiered in ['0', '', None]:
                if first_air_date := result.get('first_air_date'):
                    self.premiered = first_air_date
                    if self.year in ['0', '', None]:
                        self.year = first_air_date[:4]

            if self.plot in ['0', '', None]:
                overview = result.get('overview') or ''
                if overview:
                    self.plot = self.clean_plot(overview)

            if self.tagline in ['0', '', None]:
                tagline = result.get('tagline') or ''
                if tagline:
                    self.tagline = tagline

            if self.genre in ['0', '', None]:
                genres = result.get('genres') or []
                if genres:
                    self.genre = ' / '.join(g.get('name', '') for g in genres if g.get('name'))

            if self.studio in ['0', '', None]:
                networks = result.get('networks') or []
                if networks:
                    self.studio = networks[0].get('name', '') or ''
                    self.network = self.studio

            if self.country in ['0', '', None]:
                origin = result.get('origin_country') or []
                if origin:
                    self.country = origin[0]

            if self.duration in ['0', '', None]:
                runtimes = result.get('episode_run_time') or []
                if runtimes:
                    self.duration = str(runtimes[0])

            if self.rating in ['0', '', None, '0.0']:
                vote_avg = result.get('vote_average')
                if vote_avg is not None:
                    self.rating = str(vote_avg)

            if self.votes in ['0', '', None]:
                vote_count = result.get('vote_count')
                if vote_count is not None:
                    self.votes = str(vote_count)

            if self.status in ['0', '', None]:
                status = result.get('status') or ''
                if status:
                    self.status = status

            external_ids = result.get('external_ids') or {}
            if self.imdb in ['0', '', None] and external_ids.get('imdb_id'):
                self.imdb = external_ids['imdb_id']
            if self.tvdb in ['0', '', None] and external_ids.get('tvdb_id'):
                self.tvdb = str(external_ids['tvdb_id'])

            return True

        except Exception as e:
            c.log(f"[TVShow] Error fetching TMDB basics: {e}")
            return False

    def _parse_tmdb_response(self, data):
        """
        Parse TMDB API response and populate show properties.

        Args:
            data (dict): TMDB API response data
        """
        try:
            # Basic info
            self.title = data.get('name', self.title)
            self.tvshowtitle = self.title
            self.year = data.get('first_air_date', '0')[:4] if data.get('first_air_date') else '0'
            self.premiered = data.get('first_air_date', '')
            self.status = data.get('status', '')

            # Plot
            self.plot = self.clean_plot(data.get('overview', ''))
            self.tagline = data.get('tagline', '')

            # Ratings
            self.rating = str(data.get('vote_average', 0))
            self.votes = str(data.get('vote_count', 0))

            # Episode counts
            self.total_seasons = int(data.get('number_of_seasons', 0))
            self.total_episodes = int(data.get('number_of_episodes', 0))

            # Genres
            genres = data.get('genres', [])
            if genres:
                self.genre = ' / '.join(g.get('name', '') for g in genres)

            # Networks/Studio
            networks = data.get('networks', [])
            if networks:
                self.studio = networks[0].get('name', '')
                self.network = self.studio

            # Country
            origin_countries = data.get('origin_country', [])
            if origin_countries:
                self.country = origin_countries[0]

            # Runtime
            episode_runtimes = data.get('episode_run_time', [])
            if episode_runtimes:
                self.duration = str(episode_runtimes[0])

            # Content rating (US rating from content_ratings)
            content_ratings = data.get('content_ratings', {}).get('results', [])
            for rating in content_ratings:
                if rating.get('iso_3166_1') == 'US':
                    self.mpaa = rating.get('rating', '')
                    break

            # External IDs
            external_ids = data.get('external_ids', {})
            if external_ids:
                if imdb_id := external_ids.get('imdb_id'):
                    self.imdb = imdb_id
                if tvdb_id := external_ids.get('tvdb_id'):
                    self.tvdb = str(tvdb_id)

            # Artwork from TMDB
            if poster_path := data.get('poster_path'):
                self.poster = self.format_tmdb_image(c.tmdb_postersize, poster_path)

            if backdrop_path := data.get('backdrop_path'):
                self.fanart = self.format_tmdb_image(c.tmdb_fanartsize, backdrop_path)

            # Cast and crew from aggregate_credits
            aggregate_credits = data.get('aggregate_credits', {})

            # Extract cast with thumbnails (limit to 30)
            cast_list = aggregate_credits.get('cast', [])[:30]
            if cast_list:
                from ..modules import client
                self.castwiththumb = [
                    {
                        'name': person.get('name', ''),
                        'role': person.get('roles', [{}])[0].get('character', '') if person.get('roles') else '',
                        'thumbnail': self.format_tmdb_image(c.tmdb_profilesize, person['profile_path']) if person.get('profile_path') else ''
                    }
                    for person in cast_list
                ]
                # Also create simple cast list (names only)
                self.cast = [person.get('name', '') for person in cast_list]

            # Extract crew (director and writer)
            crew_list = aggregate_credits.get('crew', [])
            if crew_list:
                # Get directors
                directors = []
                writers = []
                for person in crew_list:
                    jobs = person.get('jobs', [])
                    for job_info in jobs:
                        job = job_info.get('job', '') if isinstance(job_info, dict) else str(job_info)
                        if job == 'Director':
                            directors.append(person.get('name', ''))
                        elif job in ['Writer', 'Screenplay', 'Story']:
                            writers.append(person.get('name', ''))

                # Remove duplicates and join
                if directors:
                    self.director = ' / '.join(list(dict.fromkeys(directors)))
                if writers:
                    self.writer = ' / '.join(list(dict.fromkeys(writers)))

            # Extract trailer from videos
            videos = data.get('videos', {}).get('results', [])
            if videos:
                # Prefer official trailers, then any trailer
                for video in videos:
                    if video.get('site') == 'YouTube' and video.get('type') == 'Trailer':
                        if video.get('official', False):  # Prefer official trailers
                            self.trailer = f"plugin://plugin.video.youtube/play/?video_id={video.get('key')}"
                            break
                # If no official trailer found, use first trailer
                if self.trailer == '0':
                    for video in videos:
                        if video.get('site') == 'YouTube' and video.get('type') == 'Trailer':
                            self.trailer = f"plugin://plugin.video.youtube/play/?video_id={video.get('key')}"
                            break

        except Exception as e:
            c.log(f"[TVShow] Error parsing TMDB response: {e}")

    @classmethod
    def from_trakt_progress(cls, trakt_item):
        """
        Create TVShow instance from Trakt progress data.

        Args:
            trakt_item (dict): Trakt API progress item

        Returns:
            TVShow: New TVShow instance
        """
        try:
            show_data = trakt_item.get('show', {})

            # Extract basic info
            show = cls()
            show.title = show_data.get('title', '')
            show.tvshowtitle = show.title
            show.year = str(show_data.get('year', '0'))
            show.plot = cls.clean_plot(show_data.get('overview', ''))
            show.status = show_data.get('status', '')
            show.aired_episodes = int(show_data.get('aired_episodes', 0))

            # Extract IDs
            ids = show_data.get('ids', {})
            show.imdb = str(ids.get('imdb', '0'))
            show.tmdb = str(ids.get('tmdb', '0'))
            show.tvdb = str(ids.get('tvdb', '0'))
            show.trakt = str(ids.get('trakt', '0'))

            # Calculate progress from seasons data
            seasons = trakt_item.get('seasons', [])
            if isinstance(seasons, dict):
                seasons = list(seasons.values())

            total_watched = 0
            for s in seasons:
                if s.get('number', 0) > 0:
                    eps = s.get('episodes') or []
                    total_watched += len(eps)

            # Calculate progress percentage
            if show.aired_episodes > 0:
                show.progress_percentage = int((total_watched / show.aired_episodes) * 100)

            return show

        except Exception as e:
            c.log(f"[TVShow] Error creating show from Trakt data: {e}")
            return None

    @classmethod
    def from_trakt_data(cls, item):
        """
        Create TVShow instance from Trakt browsing data (trending, popular, etc).

        Args:
            item (dict): Trakt API show item (from trending, popular, etc.)

        Returns:
            TVShow: New TVShow instance
        """
        try:
            show = cls()

            # Extract basic info
            show.title = item.get('title', '')
            if show.title:
                # Clean up title (remove country/year suffixes)
                import re
                show.title = re.sub(r'\s(|[(])(UK|US|AU|\d{4})(|[)])$', '', show.title)
                from ..modules import client
                show.title = client.replaceHTMLCodes(show.title)

            show.tvshowtitle = show.title
            show.originaltitle = show.title

            # Year
            year = item.get('year', '0')
            if year != '0':
                year = re.sub('[^0-9]', '', str(year))
            show.year = year

            # Extract IDs
            ids = item.get('ids', {})
            if ids:
                from ..modules.utils import normalize_imdb
                show.imdb = normalize_imdb(ids.get('imdb'))
                show.tmdb = str(ids.get('tmdb') or '0')
                show.tvdb = str(ids.get('tvdb') or '0')
                show.trakt = str(ids.get('trakt') or '0')
            else:
                show.imdb = show.tmdb = show.tvdb = show.trakt = '0'

            # Premiered
            premiered = item.get('first_aired', '0')
            if premiered != '0':
                import re
                premiered_match = re.compile(r'(\d{4}-\d{2}-\d{2})').findall(premiered)
                if premiered_match:
                    premiered = premiered_match[0]
            show.premiered = premiered

            # Studio/Network
            show.studio = item.get('network', '0')

            # Genre
            genre = item.get('genres', '0')
            if genre != '0' and isinstance(genre, list):
                genre = [g.title() for g in genre]
                show.genre = ' / '.join(genre)
            elif isinstance(genre, str):
                show.genre = genre
            else:
                show.genre = '0'

            # Duration (runtime in minutes)
            duration = item.get('runtime', '0')
            show.duration = str(duration) if duration != '0' else '0'

            # Rating
            rating = item.get('rating', '0')
            if rating == '0.0':
                rating = '0'
            show.rating = str(rating)

            # Votes
            votes = item.get('votes', '0')
            if votes != '0':
                votes = str(format(int(votes), ',d'))
            show.votes = str(votes)

            # MPAA
            show.mpaa = item.get('certification', '0')

            # Plot
            plot = item.get('overview', 'Midnight - No plot available')
            from ..modules import client
            show.plot = client.replaceHTMLCodes(plot)

            # Country
            country = item.get('country', '0')
            if country and country != '0':
                show.country = country.upper()

            # Status
            show.status = item.get('status', '0')

            # Trailer
            show.trailer = item.get('trailer', '0')

            # Initialize artwork (will be fetched separately)
            show.poster = '0'
            show.fanart = '0'

            # Season/Episode counts (will be fetched from TMDB if needed)
            show.total_seasons = 0
            show.total_episodes = 0

            return show

        except Exception as e:
            c.log(f"[TVShow] Error creating show from Trakt browsing data: {e}")

            c.log(f"[TVShow] Traceback: {traceback.format_exc()}")
            return None

    @classmethod
    def from_tmdb_data(cls, item):
        """
        Create TVShow instance from TMDB browsing data (popular, trending, etc).

        Args:
            item (dict): TMDB API show item

        Returns:
            TVShow: New TVShow instance
        """
        try:
            show = cls()

            # TMDB ID (required)
            show.tmdb = str(item['id'])

            # Basic info
            show.title = item.get('name', '')
            show.tvshowtitle = show.title
            show.originaltitle = item.get('original_name', show.title) if item.get('original_name') else show.title

            # Rating & Votes
            show.rating = str(item.get('vote_average', '0'))
            show.votes = str(item.get('vote_count', '0'))

            # Premiered & Year
            premiered = item.get('first_air_date', '0')
            show.premiered = premiered

            # Calculate year from premiered
            if premiered and premiered != '0':
                import re
                year_match = re.findall(r'(\d{4})', premiered)
                show.year = year_match[0] if year_match else '0'
            else:
                show.year = '0'

            # Check if unaired
            if premiered and premiered != '0':
                import re
                from datetime import datetime
                premiered_int = int(re.sub('[^0-9]', '', premiered))
                today = datetime.now().strftime('%Y-%m-%d')
                today_int = int(re.sub('[^0-9]', '', today))
                show.unaired = 'true' if premiered_int > today_int else ''
            else:
                show.unaired = ''

            # Plot
            show.plot = item.get('overview', '0')

            # Artwork from TMDB
            poster_path = item.get('poster_path', '')
            if poster_path:
                show.poster = show.format_tmdb_image(c.tmdb_postersize, poster_path)
            else:
                show.poster = '0'

            backdrop_path = item.get('backdrop_path', '')
            if backdrop_path:
                if not backdrop_path.startswith('/'):
                    backdrop_path = f'/{backdrop_path}'
                show.fanart = show.format_tmdb_image(c.tmdb_fanartsize, backdrop_path)
            else:
                show.fanart = '0'

            # Initialize other IDs (will be fetched via external_ids if needed)
            show.imdb = '0'
            show.tvdb = '0'

            # Season/Episode counts (initialize to 0, will be fetched if extended=True)
            show.total_seasons = 0
            show.total_episodes = 0

            return show

        except Exception as e:
            c.log(f"[TVShow] Error creating show from TMDB data: {e}")

            c.log(f"[TVShow] Traceback: {traceback.format_exc()}")
            return None

    @classmethod
    def from_tvmaze_data(cls, item):
        """
        Create TVShow instance from TVMaze API data.

        Args:
            item (dict): TVMaze API show item

        Returns:
            TVShow: New TVShow instance
        """
        try:
            show = cls()
            from ..modules import client

            # Title
            show.title = item.get('name', '')
            show.title = re.sub(r'\s(|[(])(UK|US|AU|\d{4})(|[)])$', '', show.title)
            show.title = client.replaceHTMLCodes(show.title)
            show.tvshowtitle = show.title
            show.originaltitle = show.title

            # Premiered & Year
            premiered = item.get('premiered', '0')
            if premiered and premiered != '0':
                premiered_match = re.findall(r'(\d{4}-\d{2}-\d{2})', premiered)
                show.premiered = premiered_match[0] if premiered_match else '0'

                year_match = re.findall(r'(\d{4})', premiered)
                show.year = year_match[0] if year_match else '0'
            else:
                show.premiered = '0'
                show.year = '0'

            # IDs from externals
            externals = item.get('externals', {})

            # IMDB
            imdb = externals.get('imdb', '0')
            if imdb and imdb != '0' and imdb is not None:
                if not str(imdb).startswith('tt'):
                    imdb = 'tt' + re.sub('[^0-9]', '', str(imdb))
                show.imdb = str(imdb)
            else:
                show.imdb = '0'

            # TVDB
            tvdb = externals.get('thetvdb', '0')
            if tvdb and tvdb != '0' and tvdb is not None:
                show.tvdb = str(tvdb)
            else:
                show.tvdb = '0'

            # No TMDB from TVMaze
            show.tmdb = '0'
            show.trakt = '0'

            # Poster
            try:
                poster = item.get('image', {}).get('original', '0')
                show.poster = poster if poster else '0'
            except Exception:
                show.poster = '0'

            # Studio/Network
            try:
                studio = item.get('network', {}).get('name', '0')
                show.studio = studio if studio and studio is not None else '0'
            except Exception:
                show.studio = '0'

            # Genre
            try:
                genres = item.get('genres', [])
                if genres and isinstance(genres, list):
                    genres_titled = [g.title() for g in genres]
                    show.genre = ' / '.join(genres_titled)
                else:
                    show.genre = '0'
            except Exception:
                show.genre = '0'

            # Duration (runtime)
            try:
                duration = item.get('runtime', '0')
                show.duration = str(duration) if duration and duration is not None else '0'
            except Exception:
                show.duration = '0'

            # Rating
            try:
                rating = item.get('rating', {}).get('average', '0')
                if rating and rating is not None and rating != '0.0':
                    show.rating = str(rating)
                else:
                    show.rating = '0'
            except Exception:
                show.rating = '0'

            # Plot
            try:
                plot = item.get('summary', '0')
                if plot and plot is not None:
                    # Remove HTML tags
                    plot = re.sub(r'<.+?>|</.+?>|\n', '', plot)
                    plot = client.replaceHTMLCodes(plot)
                    show.plot = plot
                else:
                    show.plot = '0'
            except Exception:
                show.plot = '0'

            # Initialize missing fields
            show.votes = '0'
            show.fanart = '0'

            return show

        except Exception as e:
            c.log(f"[TVShow] Error creating show from TVMaze data: {e}")

            c.log(f"[TVShow] Traceback: {traceback.format_exc()}")
            return None

    def get_action_url(self, action='seasons'):
        """
        Get plugin URL for this show.

        Args:
            action (str): Action to perform (e.g., 'seasons', 'episodes')

        Returns:
            str: Plugin URL
        """
        params = {
            'action': action,
            'tvshowtitle': self.tvshowtitle,
            'year': self.year,
            'imdb': self.imdb,
            'tmdb': self.tmdb,
            'tvdb': self.tvdb,
        }
        from urllib.parse import urlencode
        return f"plugin://plugin.video.midnight/?{urlencode(params)}"

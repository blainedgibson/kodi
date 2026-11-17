# -*- coding: utf-8 -*-

'''
 ***********************************************************
 * Midnight Add-on - Scraper Testing Module
 *
 * @file scraper_test.py
 * @package script.module.midnight
 *
 * @copyright (c) 2026, Midnight
 * @license GNU General Public License, version 3 (GPL-3.0)
 *
 * Exercises scrapers with fixed well-known titles (real IMDB ids)
 * so Test Scrapers mirrors live movie/TV scrape paths instead of
 * crude domain GETs that false-positive API/POST scrapers.
 ***********************************************************
'''

import os
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import sqlite3 as database

from resources.lib.modules import control
from resources.lib.modules.crewruntime import c

INTERNAL_SCRAPER_PREFIX = 'resources.lib.scrapers.'
MIN_TEST_WORKERS = 5
MAX_TEST_WORKERS = 10
SCRAPE_TEST_TIMEOUT = 20

# Stable fixtures for scrape probes (ASCII titles; popular / well-indexed).
TEST_MOVIE = {
    'title': 'Inception',
    'localtitle': 'Inception',
    'aliases': [],
    'year': '2010',
    'imdb': 'tt1375666',
    'tmdb': '27205',
}

TEST_TV = {
    'title': 'Pilot',
    'year': '2008',
    'imdb': 'tt0903747',
    'tmdb': '1396',
    'tvdb': '81189',
    'season': '1',
    'episode': '1',
    'tvshowtitle': 'Breaking Bad',
    'localtvshowtitle': 'Breaking Bad',
    'aliases': [],
    'premiered': '2008-01-20',
}

# Scrapers that only make sense on one media type (no hasMovies/hasEpisodes).
_MOVIE_ONLY_NAMES = frozenset({
    'yify', 'anymovies',
})
_TV_ONLY_NAMES = frozenset({
    'eztv', 'watchepisodes', 'onlineseries',
})

_BLOCKED_HINTS = (
    'cloudflare',
    'cf-ray',
    'cf_ray',
    'just a moment',
    'attention required',
    'challenge-platform',
    'captcha',
    'access denied',
    'geo-blocked',
    'geoblocked',
)


def is_internal_scraper(module_name):
    """True for Midnight-owned scrapers under resources.lib.scrapers/."""
    return bool(module_name) and str(module_name).startswith(INTERNAL_SCRAPER_PREFIX)


PACK_SCRAPER_PREFIXES = (
    'cocoscrapers.',
    'gearsscrapers.',
    'viperscrapers.',
)


def is_pack_scraper(module_name):
    """True for external scraper packs integrated into Midnight source searches."""
    module_name = str(module_name or '')
    return any(module_name.startswith(prefix) for prefix in PACK_SCRAPER_PREFIXES)


def is_user_toggle_eligible(module_name):
    """Scrapers that can be user-disabled via Scraper Status / registry."""
    return is_internal_scraper(module_name) or is_pack_scraper(module_name)


def is_auto_disable_candidate(scraper):
    """
    Scrapers to turn off with Auto Disable Blocked:
    - internal or pack scrapers (e.g. zilean, prowlarr)
    - not already user-disabled, code-disabled, or defunct
    - not fully working (includes hard-blocked and accessible-but-failing)
    """
    module = scraper.get('module', '')
    if not is_user_toggle_eligible(module):
        return False
    if scraper.get('user_disabled'):
        return False
    if scraper.get('disabled') or scraper.get('defunct'):
        return False
    return not scraper.get('working')


class ScraperTester:
    """Test scraper connectivity and cache results."""

    def __init__(self):
        self.cache_file = control.cacheFile
        self.cache_table = 'scraper_status'
        self._ensure_cache_table()

    def _ensure_cache_table(self):
        """Ensure the scraper status cache table exists."""
        try:
            control.makeFile(control.dataPath)
            dbcon = database.connect(self.cache_file)
            dbcur = dbcon.cursor()
            dbcur.execute('''
                CREATE TABLE IF NOT EXISTS scraper_status (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scraper_name TEXT UNIQUE,
                    category TEXT,
                    module_name TEXT,
                    accessible INTEGER,
                    working INTEGER,
                    disabled INTEGER,
                    defunct INTEGER,
                    pack_capable INTEGER,
                    response_time REAL,
                    error TEXT,
                    last_tested INTEGER,
                    user_disabled INTEGER DEFAULT 0
                )
            ''')
            try:
                dbcur.execute('ALTER TABLE scraper_status ADD COLUMN user_disabled INTEGER DEFAULT 0')
            except Exception:
                pass
            dbcon.commit()
            dbcur.close()
            dbcon.close()
        except Exception as e:
            c.log(f'[ScraperTest] Error creating cache table: {e}', 1)

    def _test_worker_count(self):
        """Parallel connectivity test workers (5–10, derived from max.threads)."""
        try:
            raw = int(c.get_setting('max.threads') or 25)
        except (TypeError, ValueError):
            raw = 25
        return max(MIN_TEST_WORKERS, min(MAX_TEST_WORKERS, max(1, raw // 3)))

    def _userdata_settings_path(self):
        appdata = os.environ.get('APPDATA') or os.path.expanduser('~')
        return os.path.join(appdata, 'Kodi', 'userdata', 'addon_data', 'plugin.video.midnight', 'settings.xml')

    def _load_user_disabled_map(self):
        """module_name -> user_disabled (0/1) from current cache."""
        mapping = {}
        try:
            dbcon = database.connect(self.cache_file)
            dbcur = dbcon.cursor()
            dbcur.execute('SELECT module_name, user_disabled FROM scraper_status')
            for module_name, user_disabled in dbcur.fetchall():
                if module_name:
                    mapping[module_name] = int(user_disabled or 0)
            dbcur.close()
            dbcon.close()
        except Exception as exc:
            c.log(f'[ScraperTest] Could not load user_disabled map: {exc}', 1)
        return mapping

    def migrate_legacy_provider_settings(self):
        """
        One-time import of provider.<scraper>=false from userdata into user_disabled.
        """
        path = self._userdata_settings_path()
        if not os.path.isfile(path):
            return 0
        try:
            root = ET.parse(path).getroot()
        except Exception:
            return 0

        migrated = 0
        for setting in root.findall('setting'):
            sid = setting.attrib.get('id') or ''
            if not sid.startswith('provider.'):
                continue
            value = (setting.text or setting.attrib.get('default') or '').strip().lower()
            if value != 'false':
                continue
            short = sid.replace('provider.', '', 1)
            module_name = f'{INTERNAL_SCRAPER_PREFIX}{short}'
            if self.set_user_disabled(module_name, True, scraper_name=short.upper()):
                migrated += 1

        if migrated:
            c.log(f'[ScraperTest] Migrated {migrated} legacy provider.* disable(s) to user_disabled')
        return migrated

    def iter_auto_disable_candidates(self, status_data=None):
        """
        Internal and pack scrapers that are not working (blocked or failing)
        and not already off via code, defunct flag, or user toggle.
        """
        status = status_data if status_data is not None else self.load_status()
        if not status:
            return
        for bucket in ('direct', 'torrent', 'usenet'):
            for scraper in status.get(bucket, []):
                if is_auto_disable_candidate(scraper):
                    yield scraper

    def auto_disable_blocked_internal(self, status_data=None):
        """Set user_disabled on all non-working eligible scrapers. Returns count disabled."""
        disabled = 0
        for scraper in self.iter_auto_disable_candidates(status_data):
            module = scraper.get('module', '')
            name = scraper.get('name')
            if self.set_user_disabled(module, True, scraper_name=name):
                disabled += 1
        return disabled

    def set_user_disabled(self, module_name, disabled=True, scraper_name=None):
        """Set user_disabled for an internal or pack scraper module."""
        if not is_user_toggle_eligible(module_name):
            return False

        flag = 1 if disabled else 0
        name = scraper_name or module_name.split('.')[-1].replace('_', ' ').upper()
        category = 'torrent' if 'torrent' in module_name.lower() else 'direct'
        try:
            dbcon = database.connect(self.cache_file)
            dbcur = dbcon.cursor()
            dbcur.execute('SELECT id FROM scraper_status WHERE module_name = ?', (module_name,))
            row = dbcur.fetchone()
            if row:
                dbcur.execute(
                    'UPDATE scraper_status SET user_disabled = ? WHERE module_name = ?',
                    (flag, module_name),
                )
            else:
                dbcur.execute('''
                    INSERT INTO scraper_status
                    (scraper_name, category, module_name, accessible, working,
                     disabled, defunct, pack_capable, response_time, error,
                     last_tested, user_disabled)
                    VALUES (?, ?, ?, 0, 0, 0, 0, 0, NULL, NULL, 0, ?)
                ''', (name, category, module_name, flag))
            dbcon.commit()
            dbcur.close()
            dbcon.close()
            try:
                from resources.lib.modules.scraper_registry import get_registry
                get_registry().invalidate()
            except Exception:
                pass
            return True
        except Exception as exc:
            c.log(f'[ScraperTest] set_user_disabled failed for {module_name}: {exc}', 1)
            return False

    def _scraper_category(self, module_name):
        if 'torrent' in module_name.lower() or 'en_tor' in module_name.lower():
            return 'torrent'
        if 'usenet' in module_name.lower():
            return 'usenet'
        return 'direct'

    def _organize_results(self, results, current_time):
        organized = {
            'direct': [],
            'torrent': [],
            'usenet': [],
            'summary': {},
        }
        total_count = working_count = accessible_count = blocked_count = 0
        disabled_count = user_disabled_count = 0

        for result in results:
            cat = result['category']
            if cat == 'torrent':
                organized['torrent'].append(result)
            elif cat == 'usenet':
                organized['usenet'].append(result)
            else:
                organized['direct'].append(result)

            total_count += 1
            if result.get('user_disabled'):
                user_disabled_count += 1
            if result['disabled']:
                disabled_count += 1
            elif result['working']:
                working_count += 1
            elif result['accessible']:
                accessible_count += 1
            else:
                blocked_count += 1

        organized['summary'] = {
            'total': total_count,
            'working': working_count,
            'accessible': accessible_count,
            'blocked': blocked_count,
            'disabled': disabled_count,
            'user_disabled': user_disabled_count,
            'last_updated': datetime.fromtimestamp(current_time).isoformat(),
        }
        return organized

    def _persist_results(self, results, user_disabled_map, current_time):
        try:
            dbcon = database.connect(self.cache_file)
            dbcur = dbcon.cursor()
            dbcur.execute('DELETE FROM scraper_status')
            for result in results:
                module_name = result['module']
                user_flag = user_disabled_map.get(module_name, int(result.get('user_disabled', 0)))
                dbcur.execute('''
                    INSERT OR REPLACE INTO scraper_status
                    (scraper_name, category, module_name, accessible, working,
                     disabled, defunct, pack_capable, response_time, error,
                     last_tested, user_disabled)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    result['name'],
                    result['category'],
                    module_name,
                    1 if result['accessible'] else 0,
                    1 if result['working'] else 0,
                    1 if result['disabled'] else 0,
                    1 if result['defunct'] else 0,
                    1 if result['pack_capable'] else 0,
                    result['response_time'],
                    result['error'],
                    current_time,
                    user_flag,
                ))
            dbcon.commit()
            dbcur.close()
            dbcon.close()
        except Exception as exc:
            c.log(f'[ScraperTest] Error persisting results: {exc}', 1)

    def _evaluate_scraper(self, module_name, scraper_obj, pack_flags):
        """Build a test result dict for one scraper (thread-safe)."""
        cocoscrapers_enabled, gearsscrapers_enabled, viperscrapers_enabled = pack_flags
        name = getattr(scraper_obj, 'name', module_name.split('.')[-1])
        category = self._scraper_category(module_name)

        pack_disabled = False
        if 'cocoscrapers' in module_name.lower() and not cocoscrapers_enabled:
            pack_disabled = True
        elif 'gearsscrapers' in module_name.lower() and not gearsscrapers_enabled:
            pack_disabled = True
        elif 'viperscrapers' in module_name.lower() and not viperscrapers_enabled:
            pack_disabled = True

        disabled = pack_disabled or getattr(scraper_obj, 'disabled', False)
        defunct = getattr(scraper_obj, 'defunct', False)
        pack_capable = getattr(scraper_obj, 'pack_capable', False)

        accessible = False
        working = False
        response_time = None
        error = None

        if disabled:
            error = 'DISABLED'
        elif defunct:
            error = 'Defunct'
        else:
            accessible, working, response_time, error = self._test_scraper_scrape(
                scraper_obj, name, module_name
            )

        return {
            'name': name,
            'category': category,
            'module': module_name,
            'accessible': accessible,
            'working': working,
            'disabled': disabled,
            'defunct': defunct,
            'pack_capable': pack_capable,
            'response_time': response_time,
            'error': error,
            'internal': is_internal_scraper(module_name),
            'user_disabled': 0,
        }

    def load_status(self):
        """Load cached scraper status from database."""
        try:
            self.migrate_legacy_provider_settings()

            dbcon = database.connect(self.cache_file)
            dbcur = dbcon.cursor()
            dbcur.execute('SELECT * FROM scraper_status')
            rows = dbcur.fetchall()
            colnames = [d[0] for d in dbcur.description] if dbcur.description else []
            dbcur.close()
            dbcon.close()

            if not rows:
                return None

            result = {
                'direct': [],
                'torrent': [],
                'usenet': [],
                'summary': {},
            }

            total = working = accessible = blocked = disabled = 0
            user_disabled = 0
            last_updated = 0

            for row in rows:
                data = dict(zip(colnames, row)) if colnames else {}
                if not colnames:
                    data = {
                        'scraper_name': row[1],
                        'category': row[2],
                        'module_name': row[3],
                        'accessible': row[4],
                        'working': row[5],
                        'disabled': row[6],
                        'defunct': row[7],
                        'pack_capable': row[8],
                        'response_time': row[9],
                        'error': row[10],
                        'last_tested': row[11],
                        'user_disabled': row[12] if len(row) > 12 else 0,
                    }

                module_name = data.get('module_name') or ''
                scraper = {
                    'name': data.get('scraper_name'),
                    'category': data.get('category'),
                    'module': module_name,
                    'accessible': bool(data.get('accessible')),
                    'working': bool(data.get('working')),
                    'disabled': bool(data.get('disabled')),
                    'defunct': bool(data.get('defunct')),
                    'pack_capable': bool(data.get('pack_capable')),
                    'response_time': data.get('response_time'),
                    'error': data.get('error'),
                    'user_disabled': bool(data.get('user_disabled')),
                    'internal': is_internal_scraper(module_name),
                }

                tested_at = data.get('last_tested') or 0
                if tested_at and tested_at > last_updated:
                    last_updated = tested_at

                category = data.get('category') or 'direct'
                if category.startswith('torrent') or category == 'en_tor':
                    result['torrent'].append(scraper)
                elif category.startswith('usenet'):
                    result['usenet'].append(scraper)
                else:
                    result['direct'].append(scraper)

                total += 1
                if scraper['user_disabled']:
                    user_disabled += 1
                if scraper['disabled']:
                    disabled += 1
                elif scraper['working']:
                    working += 1
                elif scraper['accessible']:
                    accessible += 1
                else:
                    blocked += 1

            result['summary'] = {
                'total': total,
                'working': working,
                'accessible': accessible,
                'blocked': blocked,
                'disabled': disabled,
                'user_disabled': user_disabled,
                'last_updated': datetime.fromtimestamp(last_updated).isoformat() if last_updated else None,
            }

            return result

        except Exception as e:
            c.log(f'[ScraperTest] Error loading status: {e}', 1)
            return None

    def is_cache_valid(self, max_age_hours=24):
        """Check if cached data is still valid."""
        try:
            dbcon = database.connect(self.cache_file)
            dbcur = dbcon.cursor()
            dbcur.execute('SELECT MAX(last_tested) FROM scraper_status')
            result = dbcur.fetchone()
            dbcur.close()
            dbcon.close()

            if not result or not result[0]:
                return False

            age_seconds = time.time() - result[0]
            max_age_seconds = max_age_hours * 3600
            return age_seconds < max_age_seconds

        except Exception as e:
            c.log(f'[ScraperTest] Error checking cache validity: {e}', 1)
            return False

    def test_all_scrapers(self, test_type='scrape', progress_callback=None):
        """
        Test all scrapers with a real scrape against fixed IMDB fixtures
        (parallel, 5-10 workers, ~20s timeout per scraper).

        :param test_type: Kept for callers; scrape probe is always used
        :param progress_callback: Callback function(current, total, name) for progress
        :return: Dictionary with test results organized by category
        """
        _ = test_type  # legacy arg; always scrape
        c.log(
            '[ScraperTest] Starting scraper scrape tests '
            f'(movie={TEST_MOVIE["imdb"]}, tv={TEST_TV["imdb"]} S{TEST_TV["season"]}E{TEST_TV["episode"]})'
        )

        cocoscrapers_enabled = c.get_setting('cocoscrapers.enabled') == 'true'
        gearsscrapers_enabled = c.get_setting('gearsscrapers.enabled') == 'true'
        viperscrapers_enabled = c.get_setting('viperscrapers.enabled') == 'true'
        pack_flags = (cocoscrapers_enabled, gearsscrapers_enabled, viperscrapers_enabled)

        c.log(
            f'[ScraperTest] Scraper pack settings: coco={cocoscrapers_enabled}, '
            f'gears={gearsscrapers_enabled}, viper={viperscrapers_enabled}'
        )

        try:
            from resources.lib.sources import sources as load_sources
            source_list = load_sources()
        except Exception as e:
            c.log(f'[ScraperTest] Error loading scrapers: {e}', 1)
            return None

        if not source_list:
            c.log('[ScraperTest] No scrapers found', 1)
            return None

        workers = self._test_worker_count()
        total = len(source_list)
        c.log(f'[ScraperTest] Testing {total} scrapers with {workers} parallel workers')

        user_disabled_map = self._load_user_disabled_map()
        self.migrate_legacy_provider_settings()
        for module_name, flag in list(user_disabled_map.items()):
            if flag:
                continue
            try:
                short = module_name.split('.')[-1]
                legacy = c.get_setting('provider.' + short)
                if legacy == 'false':
                    user_disabled_map[module_name] = 1
            except Exception:
                pass

        current_time = int(time.time())
        results = []
        cancelled = False
        completed = 0

        def _run_one(item):
            module_name, scraper_obj = item
            return self._evaluate_scraper(module_name, scraper_obj, pack_flags)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run_one, item): item[0] for item in source_list}
            for future in as_completed(futures):
                module_name = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    c.log(f'[ScraperTest] Error testing {module_name}: {exc}', 1)
                    continue

                if result:
                    result['user_disabled'] = user_disabled_map.get(
                        result['module'],
                        int(result.get('user_disabled', 0)),
                    )
                    results.append(result)

                completed += 1
                if progress_callback:
                    label = result['name'] if result else module_name
                    if not progress_callback(completed, total, label):
                        c.log('[ScraperTest] Test cancelled by user')
                        cancelled = True
                        for pending in futures:
                            pending.cancel()
                        break

        if results:
            self._persist_results(results, user_disabled_map, current_time)

        organized = self._organize_results(results, current_time)
        summary = organized['summary']
        c.log(
            f'[ScraperTest] Testing complete ({workers} workers'
            f'{", cancelled" if cancelled else ""}): '
            f'{summary.get("working", 0)} working, '
            f'{summary.get("accessible", 0)} accessible, '
            f'{summary.get("blocked", 0)} blocked, '
            f'{summary.get("disabled", 0)} disabled, '
            f'{summary.get("user_disabled", 0)} user-disabled'
        )

        try:
            from resources.lib.modules.scraper_registry import get_registry
            get_registry().invalidate()
            get_registry().load(force=True)
        except Exception:
            pass

        return organized

    def _scraper_probe_modes(self, scraper_obj, module_name):
        """
        Decide movie and/or TV fixtures for this scraper.
        Dual-capable scrapers get movie only (one of each media type across the suite)
        unless they are TV-only / movie-only.
        """
        short = (getattr(scraper_obj, 'name', None) or str(module_name).split('.')[-1] or '').lower()

        has_movies = getattr(scraper_obj, 'hasMovies', None)
        has_episodes = getattr(scraper_obj, 'hasEpisodes', None)
        if has_movies is not None or has_episodes is not None:
            modes = []
            if has_movies:
                modes.append('movie')
            if has_episodes:
                modes.append('tv')
            if not modes:
                modes = ['movie']
            # Dual pack providers: one fixture for speed
            if len(modes) > 1:
                return ['movie']
            return modes

        if short in _TV_ONLY_NAMES:
            return ['tv']
        if short in _MOVIE_ONLY_NAMES:
            return ['movie']

        has_movie_m = callable(getattr(scraper_obj, 'movie', None))
        has_tvshow_m = callable(getattr(scraper_obj, 'tvshow', None))
        if has_tvshow_m and not has_movie_m:
            return ['tv']
        if has_movie_m and not has_tvshow_m:
            return ['movie']

        # BaseScraper / TorrentBase: movie fixture covers most dual scrapers
        return ['movie']

    def _looks_blocked(self, text):
        """True only when message/body strongly suggests CF/challenge/geo block."""
        if not text:
            return False
        low = str(text).lower()
        return any(hint in low for hint in _BLOCKED_HINTS)

    def _call_sources(self, scraper_obj, *args):
        """
        Call sources() with production arity fallback (sources.py _call_sources).

        Tries: (all args) -> (drop last) -> (first arg only).
        Pack providers typically accept (data, hostDict); legacy Midnight may take
        (url/data, hostDict, hostprDict); some accept data alone.
        """
        sources_fn = getattr(scraper_obj, 'sources', None)
        if not callable(sources_fn):
            raise RuntimeError('no sources() method')

        try:
            attempts = [args, args[: len(args) - 1], args[:1]]
        except Exception:
            attempts = [args, args[:1]]

        last_type_error = None
        for attempt in attempts:
            if not attempt:
                continue
            try:
                res = sources_fn(*attempt)
                return res or []
            except TypeError as exc:
                # Signature mismatch -- try fewer args (same as sources.py).
                last_type_error = exc
                continue
        if last_type_error is not None:
            raise last_type_error
        return []

    def _invoke_scraper_sources(self, scraper_obj, mode):
        """
        Call scraper the same way sources.py does: movie()/tvshow()+episode()
        URL path when present, else sources(data dict), with arity fallback.
        """
        host_dict = {}
        hostpr_dict = {}
        if not callable(getattr(scraper_obj, 'sources', None)):
            raise RuntimeError('no sources() method')

        if mode == 'movie':
            data = dict(TEST_MOVIE)
            url = None
            movie_fn = getattr(scraper_obj, 'movie', None)
            if callable(movie_fn):
                try:
                    url = movie_fn(
                        data['imdb'],
                        data['title'],
                        data.get('localtitle'),
                        data.get('aliases') or [],
                        data['year'],
                    )
                except Exception as exc:
                    c.log(f'[ScraperTest] {getattr(scraper_obj, "name", "?")}.movie() error: {exc}')
                    url = None
            payload = url if url else data
            return self._call_sources(scraper_obj, payload, host_dict, hostpr_dict)

        data = dict(TEST_TV)
        url = None
        tvshow_fn = getattr(scraper_obj, 'tvshow', None)
        episode_fn = getattr(scraper_obj, 'episode', None)
        if callable(tvshow_fn):
            try:
                url = tvshow_fn(
                    data['imdb'],
                    data.get('tmdb') or data.get('tvdb'),
                    data['tvshowtitle'],
                    data.get('localtvshowtitle'),
                    data.get('aliases') or [],
                    data['year'],
                )
            except Exception as exc:
                c.log(f'[ScraperTest] {getattr(scraper_obj, "name", "?")}.tvshow() error: {exc}')
                url = None
            if url and callable(episode_fn):
                try:
                    url = episode_fn(
                        url,
                        data['imdb'],
                        None,
                        data.get('title'),
                        data.get('premiered'),
                        data['season'],
                        data['episode'],
                    )
                except Exception as exc:
                    c.log(f'[ScraperTest] {getattr(scraper_obj, "name", "?")}.episode() error: {exc}')
                    url = None
        payload = url if url else data
        return self._call_sources(scraper_obj, payload, host_dict, hostpr_dict)

    def _classify_scrape_outcome(self, sources, exc, elapsed, mode_label):
        """
        Map scrape result to (accessible, working, response_time, error).

        Status vocabulary (stored in error field; flags drive UI buckets):
          ok (N sources)  -> working
          empty           -> accessible (reachable, no matches)
          timeout / error -> accessible (failure, not "blocked")
          blocked (...)   -> not accessible (CF/challenge only)
        """
        if exc is not None:
            msg = str(exc)
            if 'timeout' in msg.lower() or isinstance(exc, TimeoutError):
                return True, False, elapsed, f'timeout ({mode_label})'
            if self._looks_blocked(msg):
                return False, False, elapsed, f'blocked ({mode_label}): {msg[:60]}'
            return True, False, elapsed, f'error ({mode_label}): {msg[:60]}'

        count = len(sources) if isinstance(sources, (list, tuple)) else 0
        if count > 0:
            return True, True, elapsed, f'ok ({count} sources, {mode_label})'
        return True, False, elapsed, f'empty ({mode_label})'

    def _test_scraper_scrape(self, scraper_obj, name, module_name):
        """
        Run a real scrape against fixed fixtures.

        :return: (accessible, working, response_time, error)
        """
        short = (getattr(scraper_obj, 'name', None) or str(module_name).split('.')[-1] or '').lower()
        modes = list(self._scraper_probe_modes(scraper_obj, module_name))
        allow_tv_fallback = (
            modes == ['movie']
            and short not in _MOVIE_ONLY_NAMES
            and getattr(scraper_obj, 'hasMovies', None) is None
        )
        c.log(f'[ScraperTest] Scraping {name} modes={modes} tv_fallback={allow_tv_fallback}')

        any_ok = None
        any_reachable = None
        any_blocked = None
        tried = set()
        queue = list(modes)

        while queue:
            mode = queue.pop(0)
            if mode in tried:
                continue
            tried.add(mode)

            mode_label = (
                f'movie {TEST_MOVIE["imdb"]}'
                if mode == 'movie'
                else f'tv {TEST_TV["imdb"]} S{TEST_TV["season"]}E{TEST_TV["episode"]}'
            )
            start = time.time()
            sources = None
            exc = None
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    fut = pool.submit(self._invoke_scraper_sources, scraper_obj, mode)
                    sources = fut.result(timeout=SCRAPE_TEST_TIMEOUT)
            except TimeoutError:
                exc = TimeoutError(f'scrape exceeded {SCRAPE_TEST_TIMEOUT}s')
            except Exception as err:
                exc = err

            elapsed = time.time() - start
            outcome = self._classify_scrape_outcome(sources, exc, elapsed, mode_label)
            accessible, working, _rt, error = outcome
            c.log(
                f'[ScraperTest] {name} {mode_label}: '
                f'accessible={accessible} working={working} error={error!r} ({elapsed:.2f}s)'
            )

            if working:
                any_ok = outcome
                break
            if accessible:
                any_reachable = outcome
                if (
                    allow_tv_fallback
                    and mode == 'movie'
                    and error.startswith('empty')
                    and 'tv' not in tried
                ):
                    queue.append('tv')
            else:
                any_blocked = outcome

        if any_ok:
            return any_ok
        if any_reachable:
            return any_reachable
        if any_blocked:
            return any_blocked
        return False, False, None, 'No probe run'

    def clear_cache(self):
        """Clear all cached scraper test results."""
        try:
            dbcon = database.connect(self.cache_file)
            dbcur = dbcon.cursor()
            dbcur.execute('DELETE FROM scraper_status')
            dbcon.commit()
            dbcur.close()
            dbcon.close()
            c.log('[ScraperTest] Cache cleared')
            try:
                from resources.lib.modules.scraper_registry import get_registry
                get_registry().invalidate()
            except Exception:
                pass
            return True
        except Exception as e:
            c.log(f'[ScraperTest] Error clearing cache: {e}', 1)
            return False

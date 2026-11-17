# -*- coding: utf-8 -*-
"""
Load essential movie/series/miniseries catalogs from the Kodi profile folder.

Local JSON files (next to kodi.log or in userdata):
  essential_movies.json, essential_series.json, essential_miniseries.json
"""

import json
import os
from urllib.parse import quote_plus

import xbmcvfs

from . import control
from . import cache
from . import http_client
from . import keys
from .crewruntime import c

CATALOG_FILES = {
    'movies': 'essential_movies.json',
    'series': 'essential_series.json',
    'miniseries': 'essential_miniseries.json',
}

TMDB_IMG = 'https://image.tmdb.org/t/p/%s%s'
CACHE_TTL = 720  # 30 days — stable curated lists


def _catalog_search_dirs():
    """Kodi logpath (next to kodi.log) first, then userdata profile."""
    dirs = []
    for special in ('special://logpath', 'special://profile'):
        try:
            base = control.transPath(special)
        except Exception:
            base = ''
        if base:
            base = base.rstrip('\\/')
            if base not in dirs:
                dirs.append(base)
    return dirs


def _find_catalog_path(filename):
    for base in _catalog_search_dirs():
        path = os.path.join(base, filename)
        if xbmcvfs.exists(path):
            c.log(f'[Essential] Using catalog file: {path}')
            return path
    searched = ', '.join(_catalog_search_dirs()) or '(none)'
    c.log(f'[Essential] Missing {filename} — searched: {searched}')
    return None


def _read_json_file(path):
    handle = xbmcvfs.File(path)
    try:
        raw = handle.read()
    finally:
        handle.close()
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', errors='replace')
    return json.loads(raw)


def _tmdb_api_key():
    return control.setting('tm.personal_user') or control.setting('tm.user') or keys.tmdb_key


def load_kind(kind):
    """Return parsed JSON entries for movies, series, or miniseries."""
    filename = CATALOG_FILES.get(kind)
    if not filename:
        return []
    path = _find_catalog_path(filename)
    if not path:
        return []
    try:
        data = _read_json_file(path)
        entries = data if isinstance(data, list) else []
        c.log(f'[Essential] Loaded {len(entries)} {kind} entries from {path}')
        return entries
    except Exception as exc:
        c.log(f'[Essential] Failed to read {path}: {exc}')
        return []


def _tmdb_get(endpoint, extra_params=None):
    from urllib.parse import urlencode
    params = {'api_key': _tmdb_api_key(), 'language': 'en-US'}
    if extra_params:
        params.update(extra_params)
    url = f'https://api.themoviedb.org/3/{endpoint}?{urlencode(params)}'
    return http_client.tmdb_get_json(url, timeout=15) or {}


def _poster_url(path_value):
    if path_value:
        return TMDB_IMG % (c.tmdb_postersize, path_value)
    return '0'


def _fanart_url(path_value):
    if path_value:
        return TMDB_IMG % (c.tmdb_fanartsize, path_value)
    return '0'


def _fetch_collection_meta(collection_id):
    data = _tmdb_get(f'collection/{collection_id}')
    if not data.get('id'):
        return None
    parts = data.get('parts') or []
    return {
        'title': data.get('name') or '',
        'poster': _poster_url(data.get('poster_path')),
        'fanart': _fanart_url(data.get('backdrop_path')),
        'count': len(parts),
    }


def _fetch_movie_meta(movie_id):
    data = _tmdb_get(f'movie/{movie_id}')
    if not data.get('id'):
        return None
    year = (data.get('release_date') or '0')[:4]
    return {
        'title': data.get('title') or '',
        'year': year,
        'poster': _poster_url(data.get('poster_path')),
        'fanart': _fanart_url(data.get('backdrop_path')),
    }


def _search_tv_meta(title):
    data = _tmdb_get('search/tv', {'query': title})
    results = data.get('results') or []
    if not results:
        return None
    wanted = title.lower().strip()
    pick = results[0]
    for candidate in results:
        if (candidate.get('name') or '').lower().strip() == wanted:
            pick = candidate
            break
    year = (pick.get('first_air_date') or '0')[:4]
    return {
        'title': pick.get('name') or title,
        'tmdb': str(pick.get('id') or '0'),
        'year': year,
        'poster': _poster_url(pick.get('poster_path')),
        'fanart': _fanart_url(pick.get('backdrop_path')),
    }


def _fetch_tv_meta(tmdb_id):
    data = _tmdb_get(f'tv/{tmdb_id}', {'append_to_response': 'external_ids'})
    if not data.get('id'):
        return None
    ext = data.get('external_ids') or {}
    year = (data.get('first_air_date') or '0')[:4]
    return {
        'title': data.get('name') or '',
        'tmdb': str(data.get('id')),
        'year': year,
        'imdb': (ext.get('imdb_id') or '0').replace('tt', '') if ext.get('imdb_id') else '0',
        'poster': _poster_url(data.get('poster_path')),
        'fanart': _fanart_url(data.get('backdrop_path')),
    }


def resolve_tv_entry(entry):
    """Resolve a series/miniseries JSON entry to metadata with a TMDb id."""
    tmdb_id = entry.get('tmdb_id')
    if tmdb_id:
        meta = cache.get(_fetch_tv_meta, CACHE_TTL, str(tmdb_id))
    else:
        meta = cache.get(_search_tv_meta, CACHE_TTL, entry.get('title', ''))
    return meta


def entry_label(entry, kind, meta=None):
    title = entry.get('title') or 'Unknown'
    if kind == 'movies' and entry.get('type') == 'movie_collection':
        count = (meta or {}).get('count')
        if count:
            return f'{title} ({count})'
    return title


def entry_action(entry, kind, tv_meta=None):
    """Build the Midnight router action string for a catalog entry."""
    if kind == 'movies':
        if entry.get('type') == 'movie_collection':
            return f'movies&url=essential_collection.{entry["tmdb_collection_id"]}'
        tmdb_id = entry.get('tmdb_id')
        return f'movies&url=essential_single.{tmdb_id}' if tmdb_id else None

    meta = tv_meta or resolve_tv_entry(entry)
    if not meta or meta.get('tmdb') in (None, '', '0'):
        title = entry.get('title')
        return f'tvSearchterm&name={quote_plus(title)}' if title else None

    title = quote_plus(meta.get('title') or entry.get('title') or '0')
    year = meta.get('year') or '0'
    imdb = meta.get('imdb') or '0'
    tmdb = meta.get('tmdb')
    return f'seasons&tvshowtitle={title}&year={year}&imdb={imdb}&tmdb={tmdb}&meta=0'


def prepare_entry(entry, kind):
    """
    Resolve one catalog row for menu display.

    Returns:
        tuple(label, action, poster, fanart, plot) or None when the row cannot be shown.
    """
    plot = entry.get('notes') or ''
    tv_meta = None
    movie_meta = None

    if kind == 'movies':
        if entry.get('type') == 'movie_collection':
            movie_meta = cache.get(_fetch_collection_meta, CACHE_TTL, str(entry['tmdb_collection_id']))
        else:
            movie_meta = cache.get(_fetch_movie_meta, CACHE_TTL, str(entry.get('tmdb_id', 0)))
        poster = (movie_meta or {}).get('poster', '0')
        fanart = (movie_meta or {}).get('fanart', '0')
        label = entry_label(entry, kind, movie_meta)
        action = entry_action(entry, kind)
    else:
        tv_meta = resolve_tv_entry(entry)
        poster = (tv_meta or {}).get('poster', '0')
        fanart = (tv_meta or {}).get('fanart', '0')
        label = entry.get('title') or (tv_meta or {}).get('title') or 'Unknown'
        action = entry_action(entry, kind, tv_meta)

    if not action:
        c.log(f'[Essential] Skipping entry without action: {entry.get("title")}')
        return None

    return label, action, poster, fanart, plot

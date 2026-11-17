# -*- coding: utf-8 -*-
"""Fill movie cast / director / writer / trailer for playback & sources info."""

from __future__ import annotations

import json
import sys
from urllib.parse import quote_plus

from . import http_client
from . import keys
from .crewruntime import c


def _has_people(value) -> bool:
    if not value or value in ('0', 'None'):
        return False
    if isinstance(value, (list, tuple)):
        return any(bool(x) and str(x) not in ('0',) for x in value)
    return True


def ensure_movie_credits(meta: dict) -> dict:
    """
    Ensure director/writer/cast/trailer on movie meta for InfoTag / sources / player.
    Play URLs often omit them (URL size, stale cache, trailer set after sysmeta).
    """
    if not isinstance(meta, dict):
        return meta

    mediatype = (meta.get('mediatype') or 'movie').lower()
    if mediatype != 'movie':
        return meta

    cast = meta.get('castwiththumb')
    has_cast = isinstance(cast, list) and bool(cast)
    has_director = _has_people(meta.get('director'))
    has_writer = _has_people(meta.get('writer'))
    trailer = meta.get('trailer')
    has_trailer = bool(trailer) and str(trailer) not in ('0', '', 'None')

    tmdb = meta.get('tmdb') or meta.get('tmdb_id') or '0'
    imdb = meta.get('imdb') or meta.get('imdb_id') or '0'

    if not has_trailer and str(tmdb) not in ('0', '', 'None', 'null'):
        try:
            name = quote_plus((meta.get('title') or meta.get('originaltitle') or 'Trailer').strip())
            art = quote_plus(json.dumps({
                'poster': meta.get('poster') or '',
                'fanart': meta.get('fanart') or meta.get('fanart2') or '',
            }))
            addon = ''
            try:
                addon = sys.argv[0] if sys.argv else ''
            except Exception:
                addon = ''
            if not addon:
                addon = 'plugin://plugin.video.midnight/'
            meta['trailer'] = (
                f'{addon}?action=trailer&name={name}&imdb={imdb}&tmdb={tmdb}'
                f'&mediatype=movie&meta={art}'
            )
        except Exception as e:
            c.log(f'[movie_credits] trailer seed failed: {e}')

    if has_cast and has_director and has_writer:
        return meta

    if str(tmdb) in ('0', '', 'None', 'null'):
        return meta

    try:
        api = c.get_setting('tm.personal_user') or c.get_setting('tm.user') or keys.tmdb_key
        lang = c.get_setting('language') or 'en'
        url = (
            f'https://api.themoviedb.org/3/movie/{tmdb}'
            f'?api_key={api}&language={lang}&append_to_response=credits'
        )
        item = http_client.tmdb_get_json(url, timeout=12) or {}
        credits = item.get('credits') if isinstance(item.get('credits'), dict) else {}

        if not has_cast:
            castwiththumb = []
            profile_size = getattr(c, 'tmdb_profilesize', None) or 'w185'
            for idx, person in enumerate((credits.get('cast') or [])[:30]):
                if not isinstance(person, dict):
                    continue
                name = (person.get('name') or '').strip()
                if not name:
                    continue
                profile = person.get('profile_path') or ''
                icon = f'https://image.tmdb.org/t/p/{profile_size}{profile}' if profile else ''
                castwiththumb.append({
                    'name': name,
                    'role': (person.get('character') or '').strip(),
                    'order': int(person.get('order') if person.get('order') is not None else idx),
                    'thumbnail': icon,
                })
            if castwiththumb:
                meta['castwiththumb'] = castwiththumb

        crew = credits.get('crew') or []
        if isinstance(crew, list):
            if not has_director:
                directors = [
                    d.get('name') for d in crew
                    if isinstance(d, dict) and d.get('job') == 'Director' and d.get('name')
                ]
                if directors:
                    meta['director'] = ', '.join(directors)
            if not has_writer:
                writers = [
                    w.get('name') for w in crew
                    if isinstance(w, dict)
                    and w.get('job') in ('Writer', 'Screenplay', 'Author', 'Novel')
                    and w.get('name')
                ]
                if writers:
                    meta['writer'] = ', '.join(writers)
    except Exception as e:
        c.log(f'[movie_credits] ensure failed: {e}')

    return meta

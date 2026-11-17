# -*- coding: utf-8 -*-

'''
********************************************************cm*
* Midnight Add-on
*
* @file metacache.py
* @package script.module.midnight
*
* @copyright (c) 2025, Midnight
* @license GNU General Public License, version 3 (GPL-3.0)
*
********************************************************cm*
'''


import time
import json
import sqlite3 as database



from . import control
from .crewruntime import c

CACHE_VERSION = 1  # Increment when cache schema changes
DEFAULT_TTL_HOURS = 720  # 30 days default


def get_content_ttl(item_data):
    """
    Calculate dynamic TTL based on content metadata.

    Args:
        item_data: Dict with show/movie metadata

    Returns:
        int: TTL in hours
    """
    try:
        status = item_data.get('status', '').lower()

        # Airing shows: 6 hours
        if status in ['returning series', 'in production', 'continuing']:
            return 6

        # Ended shows: 30 days
        elif status in ['ended', 'canceled', 'cancelled']:
            return 720

        # Check release date for movies
        premiered = item_data.get('premiered') or item_data.get('year')
        if premiered:
            try:
                import datetime
                if len(str(premiered)) == 4:  # Year only
                    year = int(premiered)
                    current_year = datetime.datetime.now().year
                    if current_year - year < 1:
                        return 6  # Recent: 6 hours
                    else:
                        return 720  # Old: 30 days
                else:  # Full date
                    release_date = datetime.datetime.strptime(str(premiered)[:10], '%Y-%m-%d')
                    days_old = (datetime.datetime.now() - release_date).days
                    if days_old < 365:
                        return 6
                    else:
                        return 720
            except:
                pass

        # Default
        return DEFAULT_TTL_HOURS
    except:
        return DEFAULT_TTL_HOURS


def fetch(items, lang='en', user=''):
    try:
        t2 = int(time.time())
        dbcon = database.connect(control.metacacheFile)
        dbcur = dbcon.cursor()
    except:
        return items

    def _imdb_usable(imdb) -> bool:
        """Reject bare 'tt' / empty — those collide across all no-IMDb titles."""
        if not imdb or imdb in ('0', '', 'None', 'tt'):
            return False
        s = str(imdb)
        return s.startswith('tt') and any(ch.isdigit() for ch in s)

    # Collect all non-zero IDs for two batch lookups instead of N individual queries
    imdb_ids = [
        item.get('imdb', '0') for item in items
        if _imdb_usable(item.get('imdb', '0'))
    ]
    tmdb_ids = [item.get('tmdb', '0') for item in items if item.get('tmdb', '0') not in ('0', '', None)]

    rows_by_imdb = {}
    rows_by_tmdb = {}
    try:
        if imdb_ids:
            ph = ','.join('?' * len(imdb_ids))
            dbcur.execute(
                f"SELECT * FROM meta WHERE imdb IN ({ph}) AND lang=? AND user=? AND imdb!='0'",
                imdb_ids + [lang, user]
            )
            for row in dbcur.fetchall():
                if _imdb_usable(row[0]):
                    rows_by_imdb[row[0]] = row
        if tmdb_ids:
            ph = ','.join('?' * len(tmdb_ids))
            dbcur.execute(
                f"SELECT * FROM meta WHERE tmdb IN ({ph}) AND lang=? AND user=? AND tmdb!='0'",
                tmdb_ids + [lang, user]
            )
            for row in dbcur.fetchall():
                rows_by_tmdb[row[1]] = row
    except Exception:
        pass

    for item in items:
        try:
            imdb = item.get('imdb', '0')
            tmdb = item.get('tmdb', '0')
            # Prefer TMDB — IMDb null→'tt' used to merge unrelated franchise titles.
            match = None
            if tmdb and tmdb != '0':
                match = rows_by_tmdb.get(str(tmdb))
            if match is None and _imdb_usable(imdb):
                match = rows_by_imdb.get(imdb)
            if match is None:
                continue

            t1 = int(match[6])
            raw = c.to_str(match[5])
            try:
                item_data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                # backward compat: old entries were stored with repr() instead of json.dumps()
                try:
                    item_data = eval(raw)  # noqa: S307
                except Exception:
                    continue

            ttl_hours = get_content_ttl(item_data)
            if (abs(t2 - t1) / 3600) >= ttl_hours:
                continue

            # Preserve episode-specific fields before applying the show-level cache entry.
            # The metacache is keyed per-show (tmdb/imdb), so a single cached entry is shared
            # across all episodes of that show.  Overwriting season/episode/title/premiered
            # with whatever happened to be stored for episode N breaks all other episodes.
            _saved_season    = item.get('season')
            _saved_episode   = item.get('episode')
            _saved_title     = item.get('title')
            _saved_label     = item.get('label')
            _saved_premiered = item.get('premiered')

            item_data = {k: v for k, v in item_data.items() if v != '0'}
            item.update(item_data)
            item.update({'metacache': True})

            # Restore episode-specific fields that must not be clobbered by show cache.
            if _saved_season    is not None: item['season']    = _saved_season
            if _saved_episode   is not None: item['episode']   = _saved_episode
            if _saved_title     is not None: item['title']     = _saved_title
            if _saved_label     is not None: item['label']     = _saved_label
            if _saved_premiered is not None: item['premiered'] = _saved_premiered
        except Exception:
            pass

    return items


def insert(meta):
    try:
        from .utils import normalize_imdb
        control.makeFile(control.dataPath)
        dbcon = database.connect(control.metacacheFile)
        dbcur = dbcon.cursor()
        dbcur.execute("""
                                CREATE TABLE IF NOT EXISTS meta (
                                    imdb TEXT, tmdb TEXT, tvdb TEXT, lang TEXT, user TEXT, item TEXT, time TEXT,
                                    UNIQUE(imdb, tmdb, tvdb, user, lang)
                                    );
                        """)
        t = int(time.time())
        for m in meta:
            try:
                if "user" not in m:
                    m["user"] = ''
                if "lang" not in m:
                    m["lang"] = 'en'
                m['imdb'] = normalize_imdb(m.get('imdb'))
                if isinstance(m.get('item'), dict) and 'imdb' in m['item']:
                    m['item']['imdb'] = normalize_imdb(m['item'].get('imdb'))
                i = json.dumps(m['item'], ensure_ascii=False)
                try:
                    # dbcur.execute("DELETE * FROM meta WHERE (imdb = '%s' and lang = '%s' and user = '%s' and not imdb = '0') or \
                    #                                         (tvdb = '%s' and lang = '%s' and user = '%s' and not tvdb = '0' or \
                    #                                         (tmdb = '%s' and lang = '%s' and user = '%s' and not tmdb = '0'))" % \
                    #                                         (m['imdb'], m['lang'], m['user'], m['tvdb'], m['lang'], m['user'], m['tmdb'], m['lang'], m['user']))


                    dbcur.execute("""
                                        DELETE FROM meta
                                        WHERE (imdb = ? AND lang = ? AND user = ? AND imdb != '0')
                                        OR (tvdb = ? AND lang = ? AND user = ? AND tvdb != '0')
                                        OR (tmdb = ? AND lang = ? AND user = ? AND tmdb != '0')
                                    """, (
                                        m['imdb'], m['lang'], m['user'],
                                        m['tvdb'], m['lang'], m['user'],
                                        m['tmdb'], m['lang'], m['user']
                                    ))
                except:
                    pass
                dbcur.execute("""
                                        INSERT OR REPLACE INTO meta Values (?, ?, ?, ?, ?, ?, ?)""",
                                        (
                                        m['imdb'], m['tmdb'], m['tvdb'], m['lang'], m['user'], i, t
                                        ))
            except:
                pass

        dbcon.commit()
    except:
        return


def local(items, link, poster, fanart):
    try:
        dbcon = database.connect(control.metaFile())
        dbcur = dbcon.cursor()
        # args = [i['imdb'] for i in items]
        # dbcur.execute('SELECT * FROM mv WHERE imdb IN (%s)'  % ', '.join(list(map(lambda arg:  "'%s'" % arg, args))))

        args = [i['imdb'] for i in items]
        placeholders = ', '.join(['?'] * len(args))
        # dbcur.execute('SELECT * FROM mv WHERE imdb IN (%s)' % placeholders, args)
        dbcur.execute(f'SELECT * FROM mv WHERE imdb IN ({placeholders})', args)
        data = dbcur.fetchall()
    except:
        return items

    for item in items:
        try:
            match = [x for x in data if x[1] == item['imdb']][0]

            try:
                if poster in item and item[poster] != '0':
                    raise Exception()
                if match[2] == '0':
                    raise Exception()
                # item.update({poster: link % ('300', '/%s.jpg' % match[2])})
                item.update({poster: f'{link}300/{match[2]}.jpg'})
            except:
                pass
            try:
                if fanart in item and item[fanart] != '0':
                    raise Exception()
                if match[3] == '0':
                    raise Exception()
                item.update({fanart: f'{link}1280/{match[3]}.jpg'})
            except:
                pass
        except:
            pass

    return items
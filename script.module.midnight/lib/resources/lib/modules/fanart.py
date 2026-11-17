# -*- coding: utf-8 -*-

'''
 ***********************************************************
 * Midnight Add-on
 *
 *
 * @file fanart.py
 * @package script.module.midnight
 *
 * @copyright (c) 2024, Midnight
 * @license GNU General Public License, version 3 (GPL-3.0)
 *
 ********************************************************cm*
'''


#cm - 2024/11/12 - new file
import json
import re
import time
import traceback

import sqlite3 as database
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


from . import keys
from . import control
from .crewruntime import c




#######cm#
# housekeeping
#
fanart_tv_user = c.get_setting('fanart.tv.user') or ''
fanart_tv_headers = {'api-key': keys.fanart_key}
if fanart_tv_user != '':
    fanart_tv_headers['client-key'] = fanart_tv_user

#######cm#
# constants
#
DAY = 86400
WEEK = 604800
TWOWEEKS = 1209600
MONTH = 2592000

#######cm#
# url's
#
fanart_tv_art_link = 'http://webservice.fanart.tv/v3/tv/%s'
fanart_movie_art_link = 'https://webservice.fanart.tv/v3/movies/%s'

# Kodi listitem art: append HTTP headers after '|' (fanart.tv requires UA + Referer).
# XML examples use &amp;; in Python/Kodi URLs use plain '&'.
FANART_KODI_HEADERS = 'User-Agent=Mozilla/5.0&Referer=https://fanart.tv/'


def _with_fanart_kodi_headers(url):
    """Attach User-Agent + Referer so Kodi's curl fetches fanart.tv assets correctly."""
    if not url or url in ('0', 'None'):
        return '0'
    text = str(url).strip()
    if 'fanart.tv' not in text.lower():
        return text
    base = text.split('|', 1)[0].strip()
    if not base:
        return '0'
    return f'{base}|{FANART_KODI_HEADERS}'


def get_fanart_tv_art(tvdb, imdb = '0', lang='en', mediatype='tv'):
    pass

    """
    Gets fanart.tv artwork for a given tvdb or imdb id

    Args:
        tvdb (str): The TVDB ID of the tv show
        imdb (str, optional): The IMDB ID of the tv show. Defaults to '0'.
        lang (str, optional): The language to retrieve the artwork in. Defaults to 'en'.
        type (str, optional): The type of artwork to retrieve. Defaults to 'tv'.
            Valid options are 'tv' or 'movie'

    Returns:
        tuple: A tuple containing the following artwork in this order:
            poster, fanart, banner, landscape, clearlogo, clearart
            If type is 'movie', the last item is instead discart
    """

    zero_str = {}

    def _normalize_asset_url(url, art_key, media_id):
        """
        fanart.tv API often returns short CDN URLs:
          https://assets.fanart.tv/fanart/{filename}
        Those already 200 — keep them. Only rebuild when the path is clearly
        incomplete relative to the classic tv/movies folder layout.

        Then append Kodi header options (User-Agent + Referer) as required by fanart.tv.
        """
        if not url or url == '0':
            return '0'
        text = str(url).strip().split('|', 1)[0].strip()
        if 'fanart.tv' not in text.lower():
            return text

        if text.startswith('http://'):
            text = 'https://' + text[len('http://'):]

        lower = text.lower()
        # Already has /fanart/tv/… or /fanart/movies/…
        if '/fanart/tv/' in lower or '/fanart/movies/' in lower:
            return _with_fanart_kodi_headers(text)

        # Short CDN form: /fanart/{filename}.png — use as-is (SNW logos, etc.)
        if re.search(r'/fanart/[^/]+\.(png|jpe?g|webp)(?:\?|$)', lower):
            return _with_fanart_kodi_headers(text)

        filename = text.rstrip('/').split('/')[-1]
        if not filename or '.' not in filename:
            return _with_fanart_kodi_headers(text)

        if not media_id or str(media_id) in ('0', 'None'):
            return _with_fanart_kodi_headers(text)

        kind = 'tv' if mediatype == 'tv' else 'movies'
        rebuilt = f'https://assets.fanart.tv/fanart/{kind}/{media_id}/{art_key}/{filename}'
        return _with_fanart_kodi_headers(rebuilt)

    def _extract_artwork(art, key, lang, media_id):
        try:
            items = art.get(key, [])
            # Prefer requested lang, then en, then likes (SNW: 626698d2d926e = top en logo)
            sorted_items = sorted(
                items,
                key=lambda x: (
                    x.get('lang') != lang,
                    x.get('lang') != 'en',
                    x.get('lang') not in ['00', ''],
                    -int(x.get('likes') or 0),
                ),
            )
            result = sorted_items[0] if sorted_items else {}

            if isinstance(result, dict):
                raw = result.get('url', '0')
            elif isinstance(result, str):
                raw = result
            else:
                raw = '0'
            return _normalize_asset_url(raw, key, media_id)
        except Exception:
            return '0'

    try:
        headers = {'api-key': keys.fanart_key}
        if fanart_tv_user:
            headers['client-key'] = fanart_tv_user


        if mediatype=='tv':
            if not tvdb or tvdb == '0':
                raise ValueError('Invalid TVDB ID')
            url = f'http://webservice.fanart.tv/v3/tv/{tvdb}'
            media_id = tvdb
        else:
            if not imdb or imdb == '0':
                raise ValueError('Invalid IMDB ID')
            url = f'http://webservice.fanart.tv/v3/movies/{imdb}'
            media_id = imdb


        response = get_cached_fanart(tvdb, imdb, url, headers, 15)
        if response is None:
            return zero_str

        art = json.loads(response) if isinstance(response, str) else response
        if isinstance(art, dict) and 'status' in art and art.get('status') == 'error':
            return zero_str

    except ValueError as e:
        return zero_str

    except Exception as e:
        return zero_str

    if mediatype == 'tv':
        poster = _extract_artwork(art, 'tvposter', lang, media_id)
        fanart = _extract_artwork(art, 'showbackground', lang, media_id)
        banner = _extract_artwork(art, 'tvbanner', lang, media_id)
        clearlogo = _extract_artwork(art, 'hdtvlogo' if 'hdtvlogo' in art else 'clearlogo', lang, media_id)
        clearart = _extract_artwork(art, 'hdclearart' if 'hdclearart' in art else 'clearart', lang, media_id)
        landscape = _extract_artwork(art, 'tvthumb' if 'tvthumb' in art else 'showbackground', lang, media_id)
        discart = '0'
    elif mediatype == 'movie':
        poster = _extract_artwork(art, 'movieposter', lang, media_id)
        fanart = _extract_artwork(art, 'moviebackground' if 'moviebackground' in art else 'moviethumb', lang, media_id)
        banner = _extract_artwork(art, 'moviebanner', lang, media_id)
        clearlogo = _extract_artwork(art, 'hdmovielogo', lang, media_id)
        clearart = _extract_artwork(art, 'hdmovieclearart', lang, media_id)
        landscape = _extract_artwork(art, 'moviethumb' if 'moviethumb' in art else 'moviebackground', lang, media_id)
        discart = _extract_artwork(art, 'moviedisc', lang, media_id)
    else:
        return zero_str

    if mediatype == 'tv':
        return {
            'poster': poster, 'fanart': fanart, 'banner': banner, 'landscape': landscape,
            'clearlogo': clearlogo, 'clearart': clearart
            } #poster, fanart, banner, landscape, clearlogo, clearart
    if mediatype == 'movie':
        return {
            'poster': poster, 'fanart': fanart, 'banner': banner, 'landscape': landscape,
            'clearlogo': clearlogo, 'clearart': clearart, 'discart': discart
            } #poster, fanart, banner, landscape, clearlogo, clearart, discart

def get_cached_fanart(tvdb, imdb, url, headers, timeout=30):
    try:
        if has_table('fanart_cache'):
            control.makeFile(control.dataPath)
            dbcon = database.connect(control.cacheFile)
            dbcur = dbcon.cursor()

            # Keep rows fresher than TWOWEEKS (added >= now - TTL). Old inverted
            # comparison (added < now - TTL) only returned stale rows — laughably wrong.
            cutoff = int(time.time()) - TWOWEEKS
            dbcur.execute(
                'SELECT data FROM fanart_cache WHERE url = ? AND CAST(added AS INTEGER) >= ?',
                (url, cutoff),
            )
            if result := dbcur.fetchone():
                try:
                    return json.loads(result[0])
                except Exception:
                    pass
            response = requests.get(url, headers=headers, timeout=timeout)
            response.encoding = 'utf-8'
            txt = json.loads(response.text)

            if response.status_code == 200:
                sql = "INSERT or REPLACE INTO fanart_cache (tvdb, imdb, url, data, added) Values (?,?,?,?,?)"

                if isinstance(txt, dict):
                    txt = json.dumps(txt, indent=4, sort_keys=True)
                dbcur.execute(sql, (tvdb, imdb, url, txt, int(time.time())))
                dbcon.commit()
                dbcon.close()
                return txt
            else:
                dbcon.close()
                return None
    except Exception as e:
        try:
            dbcon.close()
        except Exception:
            pass
        return None

def has_table(table):
    try:
        control.makeFile(control.dataPath)
        dbcon = database.connect(control.cacheFile)
        dbcur = dbcon.cursor()

        sql = f"create table if not exists {table} (tvdb text, imdb text, url text, data text, added text)"
        dbcur.execute(sql)

        sql =  f"SELECT count(*) as cnt FROM sqlite_master WHERE type='table' AND name='{table}'"
        dbcur.execute(sql)
        cnt = dbcur.fetchone()

        dbcon.close()

        return cnt
    except Exception:
        return False
# -*- coding: utf-8 -*-

'''
 ***********************************************************
 * Midnight Add-on
 *
 * tv_season_cache.py — Season boundary cache for TV Evening
 *
 * Stores per-season completion status so validate_episode_exists()
 * can skip impossible episodes (beyond a season finale, or a future
 * season not yet on TMDB) without making any API calls.
 *
 * Schema: tv_season_status
 *   tmdb_id      INTEGER  ─┐ PK
 *   season       INTEGER  ─┘
 *   status       TEXT        'complete'|'future'|'ended'|'airing'
 *   finale_ep    INTEGER     last known episode number in this season (or NULL)
 *   next_air_date TEXT       ISO date of next season premiere, if TMDB knows it
 *   show_ended   INTEGER     1 if show.status is Ended/Canceled — never re-check
 *   last_checked TEXT        ISO datetime (UTC) — for TTL decisions
 *
 * @package script.module.midnight
 * @copyright (c) 2026, Midnight
 * @license GNU General Public License, version 3 (GPL-3.0)
 ********************************************************cm*
'''

from sqlite3 import dbapi2 as database
from datetime import datetime, timezone, timedelta

from resources.lib.modules import control
from resources.lib.modules.crewruntime import c


_DB_PATH = None


def _get_db_path():
    global _DB_PATH
    if not _DB_PATH:
        _DB_PATH = control.tvseasonstatusFile
    return _DB_PATH


def _connect():
    path = _get_db_path()
    con = database.connect(path, timeout=10)
    con.row_factory = database.Row
    return con


def _ensure_table(con):
    con.execute('''
        CREATE TABLE IF NOT EXISTS tv_season_status (
            tmdb_id       INTEGER NOT NULL,
            season        INTEGER NOT NULL,
            status        TEXT    NOT NULL DEFAULT 'unknown',
            finale_ep     INTEGER,
            next_air_date TEXT,
            show_ended    INTEGER NOT NULL DEFAULT 0,
            last_checked  TEXT    NOT NULL,
            PRIMARY KEY (tmdb_id, season)
        )
    ''')
    con.commit()


def _now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _is_stale(row):
    """Return True if the cached entry needs a re-check."""
    status     = row['status']
    show_ended = row['show_ended']

    if show_ended or status == 'ended':
        return False  # Never re-check

    try:
        last = datetime.strptime(row['last_checked'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except Exception:
        return True  # Unparseable -> treat as stale

    now = datetime.now(timezone.utc)

    if status == 'complete':
        return (now - last) > timedelta(days=90)

    if status == 'future':
        next_air = row['next_air_date']
        if next_air:
            try:
                premiere = datetime.strptime(next_air, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                # Re-check 1 day after the known premiere date
                return now >= premiere + timedelta(days=1)
            except Exception:
                pass
        # No known premiere date -> re-check every 14 days
        return (now - last) > timedelta(days=14)

    # 'airing' or 'unknown' -> re-check every 7 days
    return (now - last) > timedelta(days=7)


def get_season_status(tmdb_id, season):
    """
    Look up cached season status. Returns a dict or None.
    Returns None if not cached or if the entry is stale (TTL expired).

    :param str|int tmdb_id: TMDB show ID
    :param int season: Season number
    :return: dict with keys status/finale_ep/next_air_date/show_ended, or None
    :rtype: dict or None
    """
    try:
        con = _connect()
        _ensure_table(con)
        row = con.execute(
            'SELECT * FROM tv_season_status WHERE tmdb_id=? AND season=?',
            (int(tmdb_id), int(season))
        ).fetchone()
        con.close()

        if not row:
            return None
        if _is_stale(row):
            c.log(f"[SeasonCache] (tmdb={tmdb_id} S{season:02d}) stale — will refresh")
            return None

        return dict(row)

    except Exception as e:
        c.log(f"[SeasonCache] get error: {e}")
        return None


def set_season_status(tmdb_id, season, status, finale_ep=None, next_air_date=None, show_ended=0):
    """
    Write or update a season status entry.

    :param str|int tmdb_id: TMDB show ID
    :param int season: Season number
    :param str status: 'complete'|'future'|'ended'|'airing'
    :param int|None finale_ep: Episode number of the season finale
    :param str|None next_air_date: ISO date string (e.g. '2027-03-01')
    :param int show_ended: 1 if show is Ended/Cancelled
    """
    try:
        con = _connect()
        _ensure_table(con)
        con.execute('''
            INSERT OR REPLACE INTO tv_season_status
                (tmdb_id, season, status, finale_ep, next_air_date, show_ended, last_checked)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (int(tmdb_id), int(season), status, finale_ep, next_air_date, int(show_ended), _now_iso()))
        con.commit()
        con.close()
        c.log(f"[SeasonCache] wrote (tmdb={tmdb_id} S{season:02d}) status={status} finale_ep={finale_ep} show_ended={show_ended} next_air={next_air_date}")
    except Exception as e:
        c.log(f"[SeasonCache] set error: {e}")


def fetch_and_cache(tmdb_id, season, show_title='?'):
    """
    Make one TMDB show-root call (GET /tv/{tmdb_id}) and cache what we learn
    about both the current season boundary and the next season availability.

    This is called after validate_episode_exists() fails, so we can record:
    - Is the current season complete (finale marked)?
    - Is the show still returning or has it ended?
    - Is the next season premiere already scheduled?

    :param str|int tmdb_id: TMDB show ID
    :param int season: The season whose "next episode" failed validation
    :param str show_title: For logging
    :return: dict with status info, or None on API failure
    :rtype: dict or None
    """
    try:
        import requests
        from resources.lib.modules.keys import tmdb_key

        url = f"https://api.themoviedb.org/3/tv/{tmdb_id}"
        params = {'api_key': tmdb_key, 'language': 'en-US'}
        r = requests.get(url, params=params, timeout=10)

        if r.status_code != 200:
            c.log(f"[SeasonCache] TMDB show root HTTP {r.status_code} for tmdb={tmdb_id}")
            return None

        data = r.json()
        show_status     = data.get('status', '')              # 'Returning Series', 'Ended', 'Canceled', ...
        last_ep         = data.get('last_episode_to_air') or {}
        next_ep         = data.get('next_episode_to_air')

        show_ended      = 1 if show_status in ('Ended', 'Canceled') else 0

        last_ep_type    = last_ep.get('episode_type', '')
        last_ep_season  = last_ep.get('season_number', 0)
        last_ep_num     = last_ep.get('episode_number', 0)

        # Determine if the current season has a marked finale
        season_is_complete = (
            last_ep_type == 'finale' and last_ep_season == int(season)
        )

        next_air_date   = next_ep.get('air_date') if next_ep else None
        next_season_num = next_ep.get('season_number') if next_ep else None

        c.log(f"[SeasonCache] {show_title}: show_status={show_status} last=S{last_ep_season:02d}E{last_ep_num:02d} type={last_ep_type} next={next_ep}")

        if season_is_complete or show_ended:
            set_season_status(
                tmdb_id, season,
                status='ended' if show_ended else 'complete',
                finale_ep=last_ep_num if last_ep_season == int(season) else None,
                show_ended=show_ended
            )

        # Cache next season info if we have it
        next_season = int(season) + 1
        if show_ended:
            set_season_status(tmdb_id, next_season, status='ended', show_ended=1)
        elif next_season_num and next_season_num == next_season:
            # TMDB already knows about the next season premiere
            set_season_status(tmdb_id, next_season, status='future',
                              next_air_date=next_air_date, show_ended=0)
        elif season_is_complete:
            # Season is done but no premiere scheduled yet
            set_season_status(tmdb_id, next_season, status='future',
                              next_air_date=None, show_ended=0)

        return {
            'show_status': show_status,
            'show_ended': show_ended,
            'season_is_complete': season_is_complete,
            'finale_ep': last_ep_num if season_is_complete else None,
            'next_air_date': next_air_date,
        }

    except Exception as e:
        c.log(f"[SeasonCache] fetch_and_cache error for tmdb={tmdb_id}: {e}")
        return None


def seed_from_next_episode_cache():
    """
    One-time migration: pre-populate tv_season_status from next_episode_cache in traktsync.db.

    Runs ONLY when tv_season_status has zero rows (first-ever run after this feature
    was added).  For each show that has episode_exists=False entries we determine
    the correct "current season" and call fetch_and_cache(), so that the very first
    TV Evening run benefits from zero-API-call skips just like all subsequent runs.

    Season derivation logic:
    - episode > 1 and exists=False  ->  that season is complete (finale was earlier)
    - only episode = 1 and exists=False -> next-season probe failed; target = season - 1

    :return: number of shows seeded (0 if already seeded or on error)
    :rtype: int
    """
    try:
        # Fast path: already seeded
        con = _connect()
        _ensure_table(con)
        count = con.execute("SELECT COUNT(*) FROM tv_season_status").fetchone()[0]
        con.close()
        if count > 0:
            return 0

        # Read traktsync.db for all episode_exists=False rows
        trakt_path = control.traktsyncFile
        con2 = database.connect(trakt_path, timeout=10)
        con2.row_factory = database.Row
        try:
            table_exists = con2.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='next_episode_cache'"
            ).fetchone()
            if not table_exists:
                c.log("[SeasonCache] seed: next_episode_cache table not found — nothing to seed")
                return 0

            rows = con2.execute(
                "SELECT show_tmdb, season, episode FROM next_episode_cache WHERE episode_exists = 0"
            ).fetchall()
        finally:
            con2.close()

        if not rows:
            c.log("[SeasonCache] seed: no episode_exists=False entries found")
            return 0

        # Group by show and derive target season
        from collections import defaultdict
        show_entries = defaultdict(list)
        for row in rows:
            show_entries[row['show_tmdb']].append((int(row['season']), int(row['episode'])))

        c.log(f"[SeasonCache] Seeding tv_season_status for {len(show_entries)} shows from next_episode_cache")

        seeded = 0
        for show_tmdb, ep_list in show_entries.items():
            # Episodes > 1 that don't exist -> the season listed is the complete one
            ep_gt1 = [(s, e) for s, e in ep_list if e > 1]
            # Episode = 1 that don't exist -> the season BEFORE that is the complete one
            ep_eq1 = [(s, e) for s, e in ep_list if e == 1]

            if ep_gt1:
                target_season = min(s for s, e in ep_gt1)
            elif ep_eq1:
                target_season = min(s for s, e in ep_eq1) - 1
                if target_season < 1:
                    continue
            else:
                continue

            try:
                result = fetch_and_cache(show_tmdb, target_season, f"tmdb:{show_tmdb}")
                if result:
                    seeded += 1
            except Exception as e:
                c.log(f"[SeasonCache] seed error for tmdb:{show_tmdb} S{target_season:02d}: {e}")

        c.log(f"[SeasonCache] Initial seeding complete: {seeded}/{len(show_entries)} shows populated")
        return seeded

    except Exception as e:
        c.log(f"[SeasonCache] seed_from_next_episode_cache error: {e}")
        return 0

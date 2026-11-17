# -*- coding: utf-8 -*-

'''
TV Evening session lifecycle — single authority for state in DB + window properties.
States: start, playing, pausescreen, paused, ended
'''

import xbmc

from . import control
from . import tvevening_playlist_db
from .crewruntime import c

STATE_START = 'start'
STATE_PLAYING = 'playing'
STATE_PAUSESCREEN = 'pausescreen'
STATE_PAUSED = 'paused'
STATE_ENDED = 'ended'

RESUMABLE_STATES = frozenset({
    STATE_START,
    STATE_PLAYING,
    STATE_PAUSESCREEN,
    STATE_PAUSED,
})

PROP_STATE = 'midnight.tvevening.state'
PROP_RESUMABLE = 'midnight.tvevening.resumable'
PROP_COMPLETING = 'midnight.tvevening.completing'
PROP_COMPLETE_PENDING = 'midnight.tvevening.complete.pending'
PROP_USER_STOPPED = 'midnight.tvevening.user_stopped'
META_STATE = 'session_state'
META_POSITION = 'current_position'


def _db():
    return tvevening_playlist_db.get_playlist_db()


def get_state():
    """Live state: window property first, then DB."""
    prop = control.window.getProperty(PROP_STATE)
    if prop:
        return prop
    return _db().get_metadata(META_STATE) or ''


def get_current_position():
    """0-based playlist index from DB metadata or last started episode."""
    db = _db()
    raw = db.get_metadata(META_POSITION)
    if raw is not None and raw != '':
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    prop = control.window.getProperty('midnight.tvevening.position')
    if prop:
        try:
            return int(prop)
        except (TypeError, ValueError):
            pass
    return db.get_current_position()


def record_scrape_failure():
    """Increment scrape-failure counter for the current playlist slot."""
    try:
        db = _db()
        pos = get_current_position()
        key = f'scrape_failures_at_{pos}'
        count = int(db.get_metadata(key) or 0) + 1
        db.set_metadata(key, str(count))
        c.log(f"[TV Evening Session] Scrape failure #{count} at position {pos}")
        return count
    except Exception as e:
        c.log(f"[TV Evening Session] Scrape failure counter error: {e}")
        return 0


def clear_scrape_failures(position=None):
    """Reset scrape-failure counter after playback starts."""
    try:
        db = _db()
        pos = get_current_position() if position is None else position
        db.set_metadata(f'scrape_failures_at_{pos}', '0')
    except Exception:
        pass


def _episode_watched(ep):
    return bool(ep.get('watched') or ep.get('completed'))


def _episode_false_skip(ep):
    """Skipped without playback ever starting (legacy startup-timeout false skip)."""
    return bool(ep.get('skipped')) and not _episode_watched(ep) and not ep.get('started')


def get_effective_resume_position(episodes, stored_position):
    """
    Pick the playlist index to resume from after recovery.

    Rewinds past falsely skipped prefix episodes (skipped but never started).
    Honors stored position when the user was mid-episode (started, not finished).
    """
    if not episodes:
        return 0
    try:
        stored = int(stored_position)
    except (TypeError, ValueError):
        stored = 0
    stored = max(0, min(stored, len(episodes) - 1))

    if not any(_episode_watched(ep) for ep in episodes) and _episode_false_skip(episodes[0]):
        c.log("[TV Evening Session] Resume at 0 — nothing watched and first episode was falsely skipped")
        return 0

    stored_ep = episodes[stored]
    if stored_ep.get('started') and not _episode_watched(stored_ep):
        return stored

    for i in range(stored):
        if _episode_false_skip(episodes[i]):
            c.log(
                "[TV Evening Session] Resume rewound to {} (episode {} skipped without playback)".format(
                    i, i + 1
                )
            )
            return i

    db = _db()
    for i, ep in enumerate(episodes):
        if _episode_watched(ep):
            continue
        if ep.get('skipped') and not _episode_false_skip(ep):
            continue
        try:
            fails = int(db.get_metadata(f'scrape_failures_at_{i}') or 0)
            if fails >= 2 and not ep.get('started'):
                c.log(
                    "[TV Evening Session] Position {} skipped on resume ({} scrape failures)".format(
                        i, fails
                    )
                )
                continue
        except Exception:
            pass
        return i

    return stored


def _episode_identity(season, episode, tmdb=None, imdb=None):
    """Stable key for matching an episode dict to play params."""
    sid = str(tmdb or imdb or '').strip()
    if sid.lower() in ('none', '0', ''):
        sid = ''
    try:
        s = int(season)
        e = int(episode)
    except (TypeError, ValueError):
        return ''
    return f'{sid}:{s}:{e}'


def _episode_dict_matches(ep, season, episode, tmdb=None, imdb=None):
    if not ep:
        return False
    try:
        if int(ep.get('season', -1)) != int(season):
            return False
        if int(ep.get('episode', -1)) != int(episode):
            return False
    except (TypeError, ValueError):
        return False
    req = _episode_identity(season, episode, tmdb, imdb)
    if not req or req.startswith(':'):
        return True
    ep_key = _episode_identity(
        ep.get('season'),
        ep.get('episode'),
        ep.get('tmdb') or ep.get('showtmdb'),
        ep.get('imdb') or ep.get('showimdb'),
    )
    return req == ep_key


def matches_current_slot(season, episode, tmdb=None, imdb=None):
    """True when play params match the episode at the monitor's current DB position."""
    ep = _db().get_episode_at_position(get_current_position())
    return _episode_dict_matches(ep, season, episode, tmdb, imdb)


def is_stale_episode_play(season, episode, tmdb=None, imdb=None):
    """Block Kodi auto-replay of an episode the monitor already advanced past."""
    if control.window.getProperty('midnight.tvevening.monitor.active') != 'true':
        return False
    pos = get_current_position()
    if pos <= 0:
        return False
    db = _db()
    for i in range(pos):
        ep = db.get_episode_at_position(i)
        if _episode_dict_matches(ep, season, episode, tmdb, imdb):
            return True
    return False


def is_completing():
    """True while the last episode is finishing or the completion dialog is queued."""
    if control.window.getProperty(PROP_COMPLETING) == 'true':
        return True
    if control.window.getProperty(PROP_COMPLETE_PENDING) == 'true':
        return True
    return False


def set_completing():
    """Arm cross-process gate so Kodi cannot replay the last plugin URL."""
    control.window.setProperty(PROP_COMPLETING, 'true')


def clear_completing():
    control.window.clearProperty(PROP_COMPLETING)


def should_block_play(season, episode, tmdb=None, imdb=None):
    """
    Reject rogue plugin play during TV Evening.

    No spoofable token — uses session state + DB position only:
    - completing / completion dialog pending: block all plays (incl. last-ep replay)
    - paused/ended or session deactivated: never block (regular Midnight play OK)
    - current slot: always allow (monitor-intended scrape/play, even during gap)
    - gap (pausescreen / transitioning): block other episodes
    - stale slot (index < current position): block replays of finished episodes
    - playing/start: only the episode at current_position may start
    """
    if control.window.getProperty('midnight.tvevening.monitor.active') != 'true':
        return False
    state = get_state()
    if state in (STATE_PAUSED, STATE_ENDED):
        return False
    try:
        if _db().get_metadata('session_active') == 'false':
            return False
    except Exception:
        pass
    if is_completing():
        return True
    if matches_current_slot(season, episode, tmdb, imdb):
        return False
    if is_gap_active():
        return True
    if is_stale_episode_play(season, episode, tmdb, imdb):
        return True
    if state in (STATE_PLAYING, STATE_START) and season and episode:
        if not matches_current_slot(season, episode, tmdb, imdb):
            if _release_idle_monitor_for_regular_play():
                return False
            return True
    return False


def _release_idle_monitor_for_regular_play():
    """
    Zombie monitor: active flag set but nothing playing and user picked another show.
    Soft-pause so regular Midnight navigation works without wiping the playlist.
    """
    if control.window.getProperty('midnight.tvevening.transitioning') == 'true':
        return False
    try:
        if xbmc.Player().isPlayingVideo():
            return False
    except Exception:
        pass
    c.log('[TV Evening Session] Idle monitor + different episode — releasing play gate (soft pause)')
    soft_pause_session()
    return True


def clear_kodi_video_playlist():
    """Remove Kodi playlist items so ended episodes cannot auto-replay."""
    try:
        xbmc.PlayList(xbmc.PLAYLIST_VIDEO).clear()
        c.log('[TV Evening Session] Cleared Kodi video playlist')
        return True
    except Exception as e:
        c.log(f'[TV Evening Session] Could not clear Kodi playlist: {e}')
        return False


def is_gap_active():
    """True during between-episode countdown / transition."""
    if is_completing():
        return True
    if get_state() == STATE_PAUSESCREEN:
        return True
    if control.window.getProperty('midnight.tvevening.transitioning') == 'true':
        return True
    return False


def is_resumable_session(db=None):
    """Playlist exists and session is not explicitly ended."""
    db = db or _db()
    if db.get_playlist_size() <= 0:
        return False
    state = db.get_metadata(META_STATE)
    if state == STATE_ENDED:
        return False
    if state in RESUMABLE_STATES:
        return True
    # Legacy rows before session_state existed — preserve for recovery dialog
    if not state:
        return True
    return False


def set_state(state, position=None, sync_episode_props=True):
    """Write session state to DB and window; optionally sync position."""
    db = _db()
    db.set_metadata(META_STATE, state)
    control.window.setProperty(PROP_STATE, state)
    if state in RESUMABLE_STATES:
        control.window.setProperty(PROP_RESUMABLE, 'true')
    elif state == STATE_ENDED:
        control.window.clearProperty(PROP_RESUMABLE)

    if position is not None:
        db.set_metadata(META_POSITION, str(position))
        control.window.setProperty('midnight.tvevening.position', str(position))

    if sync_episode_props and position is not None:
        _sync_episode_window_props(position)

    c.log(f"[TV Evening Session] state={state} position={position if position is not None else get_current_position()}")


def _sync_episode_window_props(position):
    ep = _db().get_episode_at_position(position)
    if not ep:
        return
    control.window.setProperty('midnight.tvevening.current.title', ep.get('title', '') or '')
    control.window.setProperty('midnight.tvevening.current.show', ep.get('tvshowtitle', '') or '')
    if ep.get('imdb'):
        _db().set_metadata('current_imdb', ep.get('imdb', ''))
        _db().set_metadata('current_season', str(ep.get('season', '')))
        _db().set_metadata('current_episode', str(ep.get('episode', '')))


def hydrate_session_properties_from_db():
    """Startup / pre-monitor: sync DB -> window. Does not start monitor or block_binge."""
    db = _db()
    if not is_resumable_session(db):
        return False

    state = db.get_metadata(META_STATE) or STATE_PAUSED
    position = get_current_position()
    total = db.get_playlist_size()

    control.window.setProperty(PROP_STATE, state)
    control.window.setProperty(PROP_RESUMABLE, 'true')
    control.window.setProperty('midnight.tvevening.position', str(position))
    control.window.setProperty('midnight.tvevening.total', str(total))
    _sync_episode_window_props(position)

    c.log(f"[TV Evening Session] Hydrated properties: state={state} pos={position}/{total}")
    return True


def activate_live_session(position, total):
    """Monitor thread running — block UpNext and expose monitor.active."""
    control.window.setProperty('midnight.tvevening.monitor.active', 'true')
    control.window.setProperty('midnight.tvevening.block_binge', 'true')
    control.window.setProperty('midnight.tvevening.position', str(position))
    control.window.setProperty('midnight.tvevening.total', str(total))
    db = _db()
    db.set_metadata('session_active', 'true')


def load_full_session_from_db():
    """Snapshot for recovery / resume."""
    db = _db()
    episodes = db.get_visible_episodes()
    stored = get_current_position()
    position = get_effective_resume_position(episodes, stored)
    state = db.get_metadata(META_STATE) or STATE_PAUSED
    if state == STATE_ENDED and episodes:
        state = STATE_PAUSED
    return {
        'playlist_size': len(episodes),
        'stored_position': stored,
        'current_position': position,
        'session_state': state,
        'episodes': episodes,
        'episode_titles': [
            f"{ep.get('tvshowtitle', 'Unknown')} S{int(ep.get('season', 0)):02d}E{int(ep.get('episode', 0)):02d}"
            for ep in episodes
        ],
    }


def is_resumable_and_monitor_inactive():
    if control.window.getProperty('midnight.tvevening.monitor.active') == 'true':
        return False
    return is_resumable_session()


def deactivate_live_session():
    """Monitor thread stopped — release live flags but keep DB playlist for recovery."""
    control.window.clearProperty('midnight.tvevening.monitor.active')
    control.window.clearProperty('midnight.tvevening.block_binge')
    control.window.clearProperty('midnight.tvevening.transitioning')
    try:
        _db().set_metadata('session_active', 'false')
    except Exception:
        pass


def soft_pause_session(position=None):
    """
    User paused/stopped for now — keep playlist + Trakt resume points in DB.
    Recovery dialog offers Continue / Fresh / Delete on next TV Evening start.
    """
    pos = get_current_position() if position is None else int(position)
    c.log("[TV Evening Session] Soft pause at position {} — playlist preserved".format(pos))
    set_state(STATE_PAUSED, position=pos)
    deactivate_live_session()
    clear_kodi_video_playlist()
    control.window.setProperty(PROP_RESUMABLE, 'true')
    control.window.clearProperty(PROP_USER_STOPPED)


def end_session(clear_kodi_playlist=True, clear_block_binge=True):
    """End evening — vacuum playlist DB and clear session properties."""
    c.log("[TV Evening Session] Ending session")
    db = _db()
    try:
        db.set_metadata(META_STATE, STATE_ENDED)
    except Exception:
        pass

    db.clear_playlist()

    for prop in (
        PROP_STATE,
        PROP_RESUMABLE,
        PROP_COMPLETING,
        PROP_COMPLETE_PENDING,
        'midnight.tvevening.monitor.active',
        'midnight.tvevening.position',
        'midnight.tvevening.total',
        'midnight.tvevening.current.title',
        'midnight.tvevening.current.show',
        'midnight.tvevening.next.title',
        'midnight.tvevening.next.show',
        'midnight.tvevening.status',
        'midnight.tvevening.transitioning',
        PROP_USER_STOPPED,
    ):
        control.window.clearProperty(prop)
    clear_completing()

    if clear_block_binge:
        control.window.clearProperty('midnight.tvevening.block_binge')

    if clear_kodi_playlist:
        try:
            xbmc.PlayList(xbmc.PLAYLIST_VIDEO).clear()
        except Exception:
            pass

    c.log("[TV Evening Session] Session ended and cleared")


def startup_check():
    """
    Kodi boot maintenance: preserve resumable evenings, delete ended/orphan data.
    Does not start monitor or show dialogs.
    """
    db = _db()
    if is_resumable_session(db):
        hydrate_session_properties_from_db()
        c.log("[TV Evening Session] Startup: preserved resumable playlist")
        return 'preserved'

    if db.get_playlist_size() > 0:
        c.log("[TV Evening Session] Startup: removing non-resumable playlist data")
        end_session()
        return 'cleared'

    c.log("[TV Evening Session] Startup: no TV Evening data")
    return 'none'

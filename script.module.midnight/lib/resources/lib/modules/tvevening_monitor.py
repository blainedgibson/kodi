# -*- coding: utf-8 -*-

'''
********************************************************cm*
* Midnight Add-on
*
* @file tvevening_monitor.py
* @package script.module.midnight
*
* @copyright (c) 2026, Midnight
* @license GNU General Public License, version 3 (GPL-3.0)
*
********************************************************cm*
TV Evening Monitor
Manages the entire TV Evening session lifecycle with pre-scraping during countdowns.
'''


import threading
import time
from urllib.parse import quote_plus

import xbmc
import xbmcgui

from . import control
from .crewruntime import c
from . import tvevening_playlist_db
from . import tvevening_countdown
from . import tvevening_complete
from . import tvevening_session
from . import upnext
from . import bookmarks
from . import trakt
from . import simkl


# sources imported lazily to avoid circular dependency

# Shared with player.onAVStarted — Kodi delivers AV callbacks to registered Player
# subclasses (service player + plugin player); monitor uses a plain helper instead.
# threading.Event only works in-process; plugin play runs in a separate subprocess,
# so we also mirror AV start via a window property (cross-process safe).
_av_started_event = threading.Event()
_AV_STARTED_PROP = 'midnight.tvevening.av_started_ts'


def _av_started_property_ts():
    try:
        val = control.window.getProperty(_AV_STARTED_PROP)
        return float(val) if val else 0.0
    except (TypeError, ValueError):
        return 0.0


def signal_av_started():
    """Called from player.onAVStarted when TV Evening monitor is active."""
    ts = str(time.time())
    control.window.setProperty(_AV_STARTED_PROP, ts)
    _av_started_event.set()
    global _monitor_instance
    if _monitor_instance and _monitor_instance.session_active:
        _monitor_instance.transition_handled = False
        _monitor_instance._transition_handled_at = 0.0
        _monitor_instance._episode_playback_started_at = 0.0
        _monitor_instance._playback_confirmed_at = time.time()
        _monitor_instance._last_early_trigger_position = -1
        tvevening_session.set_state(
            tvevening_session.STATE_PLAYING,
            position=_monitor_instance.current_position,
        )
        c.log("[TV Evening Monitor] AV started — reset transition state, state=playing")


def clear_av_started():
    control.window.clearProperty(_AV_STARTED_PROP)
    _av_started_event.clear()


def wait_av_started(timeout, since=0.0, playing_check=None):
    """Wait for AV start; polls window property for cross-process plugin play."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _av_started_event.is_set():
            return True
        if _av_started_property_ts() > since:
            return True
        if playing_check and playing_check():
            return True
        time.sleep(0.25)
    return False


# Set after class definition — used by signal_av_started()
_monitor_instance = None


class _PlaybackHelper:
    """Delegate playback to xbmc.Player — never subclass Player (duplicate callbacks)."""

    def play(self, playlist, startpos=0):
        xbmc.Player().play(playlist, startpos=startpos)

    def stop(self):
        try:
            xbmc.Player().stop()
        except Exception:
            pass


class TVEveningMonitor:
    """
    Dedicated monitor for TV Evening sessions.
    Responsibilities:
    - Monitors playback progress for all episodes in session
    - Pre-scrapes sources during countdown (uses 60 seconds productively!)
    - Manages episode transitions with countdown dialog
    - Handles errors (no sources, playback failures, etc.)
    - Maintains session state across all episodes
    - Provides real-time feedback to user
    """

    def __init__(self, playlist_episodes, test_mode=False, start_position=0, initial_state=tvevening_session.STATE_START):
        """
        Initialize TV Evening monitor.

        Args:
            playlist_episodes: List of episode dicts from database
            test_mode: If True, handle direct video URLs without source resolution
            start_position: Zero-based index when resuming an existing session
            initial_state: Session state from DB on recovery (start/playing/paused/...)
        """
        self.playlist_episodes = playlist_episodes
        self.current_position = max(0, int(start_position or 0))
        self.initial_state = initial_state or tvevening_session.STATE_START
        self.session_active = False
        self.player = _PlaybackHelper()
        self.monitor_thread = None
        self.stop_monitoring = False
        self.test_mode = test_mode
        if c.devmode:
            c.log(f"[TV Evening Monitor] Test mode: {self.test_mode}")
        self.transition_handled = False  # Flag to prevent double-incrementing position
        self._transition_handled_at = 0.0
        self.last_seek_time = 0  # Track when last seek occurred to stabilize after seeks
        self.last_current_time = 0  # Track last current_time to detect seeks
        self._last_total_time = 0.0
        self._last_early_trigger_position = -1  # One UpNext-aligned transition per episode slot
        self._episode_playback_started_at = 0.0
        self._playback_confirmed_at = 0.0
        self._soft_stop_done = False
        if c.devmode:
            c.log(f"[TV Evening Monitor] Initialized with {len(playlist_episodes)} episodes at position {self.current_position} state={self.initial_state}")

    def start_session(self):
        """Start the TV Evening monitoring session."""
        self.session_active = True
        self.stop_monitoring = False
        c.log("[TV Evening Monitor] Starting session")
        c.log("[TV Evening Monitor] Transition trigger: {} (UpNext settings)".format(
            upnext.get_transition_trigger_mode_label()))

        tvevening_session.set_state(self.initial_state, position=self.current_position)
        tvevening_session.activate_live_session(
            self.current_position,
            len(self.playlist_episodes),
        )
        if c.devmode:
            c.log("[TV Evening Monitor] Set session state={} position={}/{}".format(
                self.initial_state, self.current_position, len(self.playlist_episodes)))

        if self.playlist_episodes and 0 <= self.current_position < len(self.playlist_episodes):
            current_ep = self.playlist_episodes[self.current_position]
            if c.devmode:
                c.log(f"[TV Evening Monitor] Current episode: {current_ep.get('tvshowtitle', '')} - {current_ep.get('title', '')}")

        # Start monitoring in background thread
        self.monitor_thread = threading.Thread(target=self._monitor_loop)
        self.monitor_thread.daemon = True
        self.monitor_thread.start()

        return True

    def stop_session(self, clear_block_binge=True):
        """Stop the TV Evening monitoring session and end persisted session data."""
        c.log("[TV Evening Monitor] Stopping session")
        self.session_active = False
        self.stop_monitoring = True

        tvevening_session.end_session(clear_kodi_playlist=True, clear_block_binge=clear_block_binge)
        c.log("[TV Evening Monitor] Session ended via tvevening_session")

        # Only join thread if we're not calling from within the thread itself
        # (prevents "cannot join current thread" error)
        current_thread = threading.current_thread()
        if self.monitor_thread and self.monitor_thread.is_alive() and current_thread != self.monitor_thread:
            if c.devmode:
                c.log("[TV Evening Monitor] Waiting for monitor thread to finish...")
            self.monitor_thread.join(timeout=2)
        elif c.devmode:
            c.log("[TV Evening Monitor] Thread will exit naturally (called from within thread)")

        global _monitor_instance
        if _monitor_instance is self:
            _monitor_instance = None
            c.log("[TV Evening Monitor] Cleared monitor singleton")

    def soft_stop_session(self, reason='user stop'):
        """Stop viewing for now — keep playlist + resume points for recovery."""
        if self._soft_stop_done:
            return
        self._soft_stop_done = True
        c.log("[TV Evening Monitor] Soft stop ({}) — playlist preserved".format(reason))
        self.session_active = False
        self.stop_monitoring = True
        control.window.setProperty(tvevening_session.PROP_USER_STOPPED, 'true')
        control.window.clearProperty('midnight.tvevening.transitioning')
        try:
            control.execute('Dialog.Close(all,true)')
        except Exception:
            pass
        try:
            from . import tvevening_countdown as _countdown
            control.window.clearProperty(_countdown.COUNTDOWN_PENDING_PROP)
            control.window.setProperty(_countdown.COUNTDOWN_RESULT_PROP, 'cancel')
            control.window.setProperty(_countdown.COUNTDOWN_READY_PROP, 'true')
        except Exception:
            pass
        tvevening_session.soft_pause_session(self.current_position)
        current_thread = threading.current_thread()
        if self.monitor_thread and self.monitor_thread.is_alive() and current_thread != self.monitor_thread:
            self.monitor_thread.join(timeout=2)
        global _monitor_instance
        if _monitor_instance is self:
            _monitor_instance = None
            c.log("[TV Evening Monitor] Cleared monitor singleton (soft stop)")

    def _looks_like_user_stop_mid_episode(self):
        """Idle player mid-episode is usually Stop, not natural end."""
        if tvevening_session.get_state() == tvevening_session.STATE_PAUSESCREEN:
            return False
        if control.window.getProperty('midnight.tvevening.transitioning') == 'true':
            return False
        if self._playback_confirmed_at <= 0:
            return False
        total = self._last_total_time
        current = self.last_current_time
        if total <= 0:
            return False
        percent = (current / total) * 100.0
        if percent >= 88:
            return False
        return percent > 2

    def _set_status_message(self, message):
        """Show a short TV Evening status line on the overlay / window properties."""
        try:
            control.window.setProperty('midnight.tvevening.status', message)
            c.log(f"[TV Evening Monitor] Status: {message}")
        except Exception:
            pass

    def _current_episode_had_meaningful_playback(self):
        """True when the current slot was actually watched, not just briefly started."""
        ep = tvevening_playlist_db.get_playlist_db().get_episode_at_position(self.current_position)
        if not ep:
            return False
        return bool(ep.get('completed') or ep.get('watched'))

    def _try_reserve_swap(self, position):
        """
        Swap a failed visible slot with a hidden reserve episode, in-place.
        Returns True if a reserve was applied and playback should be retried.
        """
        if position < 0 or position >= len(self.playlist_episodes):
            return False

        if self._is_video_playing():
            c.log("[TV Evening Monitor] Refusing reserve swap — playback is active")
            return False

        db = tvevening_playlist_db.get_playlist_db()
        failed = self.playlist_episodes[position]
        reserve = db.take_first_reserve()
        if not reserve:
            c.log("[TV Evening Monitor] No reserve episode available for swap")
            return False

        db.mark_episode_skipped(position)
        url = self._build_episode_url(reserve)
        reserve['url'] = url
        if not db.replace_episode_at_position(position, reserve):
            c.log("[TV Evening Monitor] Failed to replace episode in database")
            return False

        failed_show = failed.get('tvshowtitle', 'Unknown')
        reserve_show = reserve.get('tvshowtitle', 'Unknown')
        self.playlist_episodes[position] = reserve
        self._rebuild_kodi_playlist(position)
        c.log("[TV Evening Monitor] Reserve swap at position {}: {} -> {}".format(
            position, failed_show, reserve_show))
        # Use normal transition flow so user gets countdown + scrape overlay
        self._handle_episode_transition(reserve, finalize_previous=False)
        return True

    def _skip_to_next_episode(self, reason='playback_failed', mark_skipped=True):
        """Advance past the current episode after failure; returns True if another episode exists."""
        db = tvevening_playlist_db.get_playlist_db()
        if mark_skipped:
            db.mark_episode_skipped(self.current_position)
        else:
            c.log("[TV Evening Monitor] Startup skip at {} — not marking skipped (retryable on recovery)".format(
                self.current_position))
        failed = self.playlist_episodes[self.current_position] if self.current_position < len(self.playlist_episodes) else {}
        next_episode = self._get_next_episode()
        if not next_episode:
            return False

        total = len(self.playlist_episodes)
        next_index = self.current_position + 1
        self._set_status_message(
            f"Couldn't start {failed.get('tvshowtitle', 'this episode')} — "
            f"trying next ({next_index + 1} of {total})..."
        )
        self._advance_position_for_next_episode(next_episode)
        self._mark_transition_handled()
        self._handle_episode_transition(next_episode)
        return True

    def _active_player(self):
        """Global playback state — do not rely on _PlaybackWatcher instance callbacks."""
        return xbmc.Player()

    def _is_video_playing(self):
        try:
            return self._active_player().isPlayingVideo()
        except Exception:
            return False

    def _mark_transition_handled(self):
        self.transition_handled = True
        self._transition_handled_at = time.time()

    def _clear_transition_handled(self):
        self.transition_handled = False
        self._transition_handled_at = 0.0

    def _on_av_wait_timeout(self, context='transition'):
        """Play never started after transition — unblock monitor and skip or soft-stop."""
        c.log("[TV Evening Monitor] AV wait timed out after {} — clearing transition flag".format(context))
        self._clear_transition_handled()
        if self._is_video_playing():
            return
        sf = control.window.getProperty('midnight.tvevening.source_failure') or ''
        if sf and sf not in ('none', 'blocked'):
            try:
                control.window.clearProperty('midnight.tvevening.source_failure')
            except Exception:
                pass
            if not self._skip_to_next_episode(reason='av_timeout'):
                self.soft_stop_session('episode failed to start')

    def _on_av_confirmed(self):
        """Apply monitor state after AV start is detected (any process)."""
        self._clear_transition_handled()
        self._episode_playback_started_at = 0.0
        self._playback_confirmed_at = time.time()
        self._last_early_trigger_position = -1
        try:
            tvevening_session.clear_scrape_failures(self.current_position)
        except Exception:
            pass
        tvevening_session.set_state(
            tvevening_session.STATE_PLAYING,
            position=self.current_position,
        )

    def _wait_for_playback_start(self, timeout, allow_reserve_swap=True):
        """Wait for AV start; reserve swap only when allow_reserve_swap=True (mid-session)."""
        if self._is_video_playing():
            c.log("[TV Evening Monitor] Playback already active — continuing")
            return True

        if _av_started_event.is_set():
            c.log("[TV Evening Monitor] AV start already signaled — continuing")
            clear_av_started()
            self._on_av_confirmed()
            return True

        since = time.time()
        clear_av_started()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _av_started_event.is_set():
                clear_av_started()
                self._on_av_confirmed()
                c.log("[TV Evening Monitor] Playback confirmed after wait")
                return True
            if self._is_video_playing():
                c.log("[TV Evening Monitor] Playback active (AV signal missed) — continuing")
                self._on_av_confirmed()
                return True
            failure = control.window.getProperty('midnight.tvevening.source_failure')
            if failure:
                c.log("[TV Evening Monitor] Source failure ({}) — playback will not start".format(failure))
                return False
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            chunk = min(0.5, remaining)
            if wait_av_started(chunk, since=since, playing_check=self._is_video_playing):
                clear_av_started()
                self._on_av_confirmed()
                c.log("[TV Evening Monitor] Playback confirmed after wait")
                return True

        c.log("[TV Evening Monitor] Playback failed to start after {}s at position {}".format(
            timeout, self.current_position))

        if not allow_reserve_swap:
            return False

        if self._is_video_playing():
            c.log("[TV Evening Monitor] Aborting reserve swap — video is playing")
            return True

        # Prefer the next visible playlist slot (e.g. 4400) over a hidden reserve swap.
        if self._get_next_episode():
            c.log("[TV Evening Monitor] Playback failed — skipping to next visible episode")
            if self._skip_to_next_episode(reason='playback_failed'):
                return True
            return False

        if self._try_reserve_swap(self.current_position):
            if not self.session_active:
                return False
            # _handle_episode_transition already ran countdown + play; monitor loop continues.
            return True

        return False

    def _monitor_loop(self):
        """Main monitoring loop - runs in background thread."""
        c.log("[TV Evening Monitor] Monitor loop started")

        startup_timeout = 45
        if not self._wait_for_playback_start(startup_timeout, allow_reserve_swap=False):
            source_failed = bool(control.window.getProperty('midnight.tvevening.source_failure'))
            control.window.clearProperty('midnight.tvevening.source_failure')
            if not self._skip_to_next_episode(mark_skipped=source_failed):
                self.stop_session()
                return
            # _skip_to_next_episode already ran countdown + play — do NOT re-arm 45s wait
            # (that would clear the AV signal and falsely trigger reserve swap mid-episode)
            self.transition_handled = False
            self._episode_playback_started_at = 0.0
            c.log("[TV Evening Monitor] Continuing after startup skip transition")

        try:
            while self.session_active and not self.stop_monitoring:
                # Tick rate for ongoing position monitoring
                time.sleep(1)

                session_state = tvevening_session.get_state()
                if session_state in (
                    tvevening_session.STATE_PAUSED,
                    tvevening_session.STATE_PAUSESCREEN,
                ):
                    continue

                # Paused video still has an active item — do not treat as episode ended
                if self._is_video_playing():
                    continue

                # Check if player is active
                if not self._active_player().isPlaying():
                        if control.window.getProperty(tvevening_session.PROP_USER_STOPPED) == 'true':
                            if self.session_active:
                                self.soft_stop_session('user stop flag')
                            continue
                        if self._looks_like_user_stop_mid_episode():
                            xbmc.sleep(300)
                            if control.window.getProperty(tvevening_session.PROP_USER_STOPPED) == 'true' or not self.session_active:
                                continue
                            self.soft_stop_session('mid-episode stop')
                            continue
                        if control.window.getProperty('midnight.tvevening.transitioning') == 'true':
                            continue
                        if self._playback_confirmed_at > 0 and (time.time() - self._playback_confirmed_at) < 12:
                            continue
                        if self.transition_handled:
                            if not self._get_next_episode():
                                if self._current_episode_had_meaningful_playback():
                                    c.log("[TV Evening Monitor] Stale transition flag on last episode — running completion")
                                    self.transition_handled = False
                                else:
                                    c.log("[TV Evening Monitor] Stale transition but episode never played — retrying")
                                    self.transition_handled = False
                                    if self.current_position < len(self.playlist_episodes):
                                        self._play_episode(self.playlist_episodes[self.current_position])
                                    continue
                            else:
                                stale_at = self._transition_handled_at or 0.0
                                if stale_at and (time.time() - stale_at) > 90:
                                    c.log("[TV Evening Monitor] Stale transition (>90s, next ep exists) — recovering")
                                    self._on_av_wait_timeout('stale transition')
                                else:
                                    c.log("[TV Evening Monitor] Playback stopped but transition already handled - ignoring")
                                continue

                        next_episode = self._get_next_episode()

                        if next_episode:
                            # Episode ended (even if manually seeked to end) - continue to next
                            c.log("[TV Evening Monitor] Episode ended - transitioning to next episode")
                            control.window.setProperty('midnight.tvevening.transitioning', 'true')
                            self._clear_kodi_playlist()
                            self.current_position += 1
                            c.log(f"[TV Evening Monitor] Position moved to {self.current_position}")

                            tvevening_session.set_state(
                                tvevening_session.get_state(),
                                position=self.current_position,
                            )
                            if self.current_position >= len(self.playlist_episodes):
                                self._finish_session()
                                break
                            next_episode = self.playlist_episodes[self.current_position]

                            # Mark transition as handled BEFORE calling transition (prevents race condition)
                            self._mark_transition_handled()

                            # Trigger countdown with pre-scraping
                            self._handle_episode_transition(next_episode)

                            # Event-based: wait for new episode to start (up to 5 seconds)
                            c.log("[TV Evening Monitor] Waiting for new episode to start...")
                            clear_av_started()
                            av_since = time.time()
                            if wait_av_started(timeout=120, since=av_since, playing_check=self._is_video_playing):
                                c.log("[TV Evening Monitor] New episode started, continuing monitor loop")
                                self._clear_transition_handled()
                                self._episode_playback_started_at = 0.0
                                self._last_early_trigger_position = -1
                            else:
                                self._on_av_wait_timeout('natural end transition')
                            continue
                        else:
                            self._finish_session()
                        break

                # Get playback times
                try:
                    current_time = self._active_player().getTime()
                    total_time = self._active_player().getTotalTime()
                except Exception as e:
                    if c.devmode:
                        c.log(f"[TV Evening Monitor] Error getting playback times: {e}")
                    continue

                # Detect seeks: if current_time jumped > 5 seconds (forward or backward)
                if self.last_current_time > 0:
                    time_diff = abs(current_time - self.last_current_time)
                    # Normal playback is ~1 second per check, seek would be a big jump
                    if time_diff > 5:
                        c.log(f"[TV Evening Monitor] SEEK DETECTED: time jumped {time_diff:.1f}s (from {self.last_current_time:.1f}s to {current_time:.1f}s)")
                        self.last_seek_time = time.time()

                self.last_current_time = current_time
                self._last_total_time = total_time

                # DIAGNOSTIC: Log playback times every 10 checks (~10 seconds)
                if not hasattr(self, '_time_log_counter'):
                    self._time_log_counter = 0
                self._time_log_counter += 1
                if self._time_log_counter >= 10:
                    c.log(f"[TV Evening Monitor] Playback times: current={current_time:.1f}s, total={total_time:.1f}s, percent={current_time/total_time*100 if total_time > 0 else 0:.1f}%")
                    self._time_log_counter = 0

                if total_time <= 0:
                    if c.devmode:
                        c.log("[TV Evening Monitor] Invalid total_time (<=0), skipping check")
                    continue

                # CRITICAL FIX: Ignore timing right after seek (resume point)
                # Wait 15 seconds after a seek for player to stabilize
                if self.last_seek_time > 0 and (time.time() - self.last_seek_time) < 15:
                    if c.devmode:
                        c.log(f"[TV Evening Monitor] Ignoring timing - too soon after seek ({time.time() - self.last_seek_time:.1f}s ago)")
                    continue

                # Ignore unstable duration reports briefly after each episode starts (streams
                # often report totalTime=0 or very short until metadata arrives).
                if self._episode_playback_started_at <= 0:
                    self._episode_playback_started_at = time.time()
                if total_time < 600 and (time.time() - self._episode_playback_started_at) < 120:
                    continue

                # Middle ground: use UpNext trigger settings (percent or seconds-before-end).
                # Last episode always plays to natural end; fallback path handles manual stop/seek.
                trigger_time = upnext.get_transition_trigger_time(total_time)
                if trigger_time is not None and current_time >= trigger_time:
                    if current_time < 30:
                        continue

                    next_episode = self._get_next_episode()
                    if not next_episode:
                        continue

                    if self._last_early_trigger_position == self.current_position:
                        continue
                    self._last_early_trigger_position = self.current_position
                    c.log("[TV Evening Monitor] UpNext-aligned trigger at {:.1f}s / {:.1f}s ({:.1f}%)".format(
                        current_time, total_time, current_time / total_time * 100))

                    control.window.setProperty('midnight.tvevening.transitioning', 'true')
                    self._clear_kodi_playlist()
                    self.current_position += 1
                    if c.devmode:
                        c.log(f"[TV Evening Monitor] Position incremented to {self.current_position} before transition")

                    tvevening_session.set_state(
                        tvevening_session.get_state(),
                        position=self.current_position,
                    )
                    control.window.setProperty('midnight.tvevening.current.title', next_episode.get('title', ''))
                    control.window.setProperty('midnight.tvevening.current.show', next_episode.get('tvshowtitle', ''))

                    peek_next = self._get_next_episode()
                    if peek_next:
                        control.window.setProperty('midnight.tvevening.next.title', peek_next.get('title', ''))
                        control.window.setProperty('midnight.tvevening.next.show', peek_next.get('tvshowtitle', ''))
                    else:
                        control.window.clearProperty('midnight.tvevening.next.title')
                        control.window.clearProperty('midnight.tvevening.next.show')

                    self._mark_transition_handled()
                    self._handle_episode_transition(next_episode)

                    clear_av_started()
                    av_since = time.time()
                    if wait_av_started(timeout=120, since=av_since, playing_check=self._is_video_playing):
                        self._clear_transition_handled()
                        self._episode_playback_started_at = 0.0
                        self._last_early_trigger_position = -1
                    else:
                        self._on_av_wait_timeout('early trigger')

        except Exception as e:
            c.log("[TV Evening Monitor] Fatal error in monitor loop: {}".format(e))
        finally:
            c.log("[TV Evening Monitor] Monitor loop exited")

    def _get_next_episode(self):
        """Get the next episode from the playlist."""
        next_position = self.current_position + 1
        if c.devmode:
            c.log(f"[TV Evening Monitor] Getting next episode at position {self.current_position}, next_position={next_position}")

        if next_position < len(self.playlist_episodes):
            return self.playlist_episodes[next_position]

        return None

    def _build_episode_url(self, episode):
        """Build plugin play URL for an episode dict."""
        url = episode.get('url')
        if url:
            return url
        url = 'plugin://plugin.video.midnight/?action=play'
        url += f"&title={quote_plus(str(episode.get('title', '')))}"
        url += f"&year={quote_plus(str(episode.get('year', '')))}"
        url += f"&imdb={quote_plus(str(episode.get('showimdb', episode.get('imdb', '0'))))}"
        url += f"&tmdb={quote_plus(str(episode.get('showtmdb', episode.get('tmdb', '0'))))}"
        url += f"&season={quote_plus(str(episode.get('season', '')))}"
        url += f"&episode={quote_plus(str(episode.get('episode', '')))}"
        url += f"&tvshowtitle={quote_plus(str(episode.get('tvshowtitle', '')))}"
        premiered = episode.get('premiered') or episode.get('air_date') or ''
        if premiered:
            url += f"&premiered={quote_plus(str(premiered))}"
        url += "&select=2"
        return url

    def _rebuild_kodi_playlist(self, start_index):
        """Keep Kodi's playlist to a single item — monitor drives every transition."""
        try:
            if start_index < 0 or start_index >= len(self.playlist_episodes):
                c.log("[TV Evening Monitor] No episode at index {} to rebuild Kodi playlist".format(start_index))
                return False

            episode = self.playlist_episodes[start_index]
            kodi_playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
            kodi_playlist.clear()

            url = self._build_episode_url(episode)
            label = "{} - S{:02d}E{:02d}".format(
                episode.get('tvshowtitle', ''),
                int(episode.get('season', 0) or 0),
                int(episode.get('episode', 0) or 0),
            )
            listitem = control.item(label=label)
            listitem.setInfo('video', {
                'title': episode.get('title', ''),
                'tvshowtitle': episode.get('tvshowtitle', ''),
                'season': episode.get('season', 0),
                'episode': episode.get('episode', 0),
                'plot': episode.get('plot', ''),
            })
            if episode.get('thumb'):
                listitem.setArt({'thumb': episode['thumb']})
            kodi_playlist.add(url, listitem)

            c.log("[TV Evening Monitor] Kodi playlist set to single item at index {}: {}".format(
                start_index, label))
            return True
        except Exception as e:
            c.log("[TV Evening Monitor] Error rebuilding Kodi playlist: {}".format(e))
            return False

    def _advance_position_for_next_episode(self, next_episode):
        """Move monitor state to the episode we are about to play."""
        self.current_position += 1
        c.log("[TV Evening Monitor] Position advanced to {} for {} S{:02d}E{:02d}".format(
            self.current_position,
            next_episode.get('tvshowtitle', ''),
            int(next_episode.get('season', 0) or 0),
            int(next_episode.get('episode', 0) or 0),
        ))
        tvevening_session.set_state(
            tvevening_session.get_state(),
            position=self.current_position,
        )
        self._rebuild_kodi_playlist(self.current_position)

    def _finalize_finished_episode(self, finished_position):
        """Scrobble/save progress for the episode that just finished before we stop playback."""
        if finished_position < 0 or finished_position >= len(self.playlist_episodes):
            return

        ep = self.playlist_episodes[finished_position]
        show_imdb = ep.get('showimdb') or ep.get('imdb') or ''
        show_tmdb = ep.get('showtmdb') or ep.get('tmdb') or 0
        season = ep.get('season', 0)
        episode_num = ep.get('episode', 0)

        try:
            kodi_player = xbmc.Player()
            if kodi_player.isPlayingVideo():
                current_time = kodi_player.getTime()
                total_time = kodi_player.getTotalTime()
            elif self._last_total_time > 0:
                current_time = self.last_current_time
                total_time = self._last_total_time
                c.log("[TV Evening Monitor] Using last polled position for finalize ({:.0f}s/{:.0f}s)".format(
                    current_time, total_time))
            else:
                c.log("[TV Evening Monitor] No playback position available for finalize at position {}".format(
                    finished_position))
                return
        except Exception as e:
            c.log("[TV Evening Monitor] Could not read playback position for finalize: {}".format(e))
            return

        if total_time <= 0:
            return

        percent = (current_time / total_time) * 100
        c.log("[TV Evening Monitor] Finalizing S{:02d}E{:02d} {} at {:.1f}% ({:.0f}s/{:.0f}s)".format(
            int(season or 0), int(episode_num or 0), ep.get('tvshowtitle', ''), percent, current_time, total_time))

        try:
            tmdb_key = int(show_tmdb) if show_tmdb and str(show_tmdb) not in ('None', '0', '') else 0
            imdb_key = str(show_imdb) if show_imdb and str(show_imdb) not in ('None', '0', '') else ''
            if imdb_key or tmdb_key:
                bookmarks.reset(
                    int(current_time), int(total_time), 'episode',
                    imdb_key, str(season), str(episode_num), tmdb=tmdb_key,
                )
                if simkl.should_scrobble_playback():
                    bookmarks.set_scrobble(
                        int(current_time), int(total_time), 'episode',
                        imdb_key, str(season), str(episode_num), 'stop', tmdb=tmdb_key,
                    )
        except Exception as e:
            c.log("[TV Evening Monitor] Error saving/scrobbling finished episode: {}".format(e))

        if percent >= 92:
            db = tvevening_playlist_db.get_playlist_db()
            db.mark_episode_watched(finished_position)

    def _stop_stray_playback(self):
        """Stop Kodi replay of the last plugin URL while completion UI is shown."""
        try:
            player = self._active_player()
            if player.isPlayingVideo():
                player.stop()
                c.log("[TV Evening Monitor] Stopped stray playback during session completion")
        except Exception as e:
            c.log("[TV Evening Monitor] Could not stop playback during completion: {}".format(e))

    def _finish_session(self):
        """Last episode done — scrobble, tear down, show completion dialog."""
        c.log("[TV Evening Monitor] Last episode finished - showing completion dialog")

        # Must run before finalize/teardown — Kodi replays the plugin URL instantly
        # when playback stops, which races Sources.play and dismisses the completion UI.
        control.window.setProperty('midnight.tvevening.transitioning', 'true')
        tvevening_session.set_completing()
        self._clear_kodi_playlist()
        self._stop_stray_playback()

        total_episodes = len(self.playlist_episodes)
        last_episode = self.playlist_episodes[-1] if self.playlist_episodes else {}
        fanart = last_episode.get('fanart', '')
        c.log("[TV Evening Monitor] Completion dialog: {} episodes watched".format(total_episodes))
        tvevening_complete.request_completion_dialog(
            episodes_watched=total_episodes,
            fanart=fanart
        )

        self._finalize_finished_episode(self.current_position)
        xbmc.sleep(500)
        self.stop_session(clear_block_binge=False)

    def _clear_kodi_playlist(self):
        """Prevent Kodi from auto-replaying the ended plugin URL."""
        tvevening_session.clear_kodi_video_playlist()

    def _handle_episode_transition(self, next_episode, finalize_previous=True):
        """
        Handle transition to next episode with pause dialog.
        After pause dialog, normal scraping with dialog will occur.

        Args:
            next_episode: Episode dict with metadata
            finalize_previous: Scrobble the episode that just finished (False for reserve swap retries)
        """
        c.log("[TV Evening Monitor] Handling transition to: {} S{:02d}E{:02d}".format(
            next_episode.get('tvshowtitle'),
            next_episode.get('season', 0),
            next_episode.get('episode', 0)
        ))

        finished_position = self.current_position - 1
        control.window.setProperty('midnight.tvevening.transitioning', 'true')
        tvevening_session.set_state(
            tvevening_session.STATE_PAUSESCREEN,
            position=self.current_position,
        )
        # Clear playlist before stop — blocks Kodi single-item auto-replay of the ended episode
        self._clear_kodi_playlist()
        try:
            if finalize_previous:
                self._finalize_finished_episode(finished_position)

            # Stop current playback
            self.player.stop()
            xbmc.sleep(500)

            # Create pause dialog (60 second countdown by default)
            # After this dialog, scraping will occur with normal scraping dialog visible

            # Use 1 second countdown in test mode for fast automated testing
            # Otherwise read from user setting (default: 60 seconds, range: 10-300)
            if self.test_mode:
                countdown_seconds = 1
            else:
                try:
                    countdown_seconds = int(c.get_setting('tvevening.gap_countdown') or '60')
                except (ValueError, TypeError):
                    countdown_seconds = 60
            if c.devmode:
                c.log(f"[TV Evening Monitor] Creating countdown dialog ({countdown_seconds}s)")

            # Queue countdown on service thread — doModal from monitor thread crashes Kodi
            tvevening_countdown.request_countdown_dialog(next_episode, countdown_seconds)
            user_choice = tvevening_countdown.wait_countdown_result(
                timeout_seconds=countdown_seconds + 120
            )

            c.log(f"[TV Evening Monitor] Countdown finished with choice: {user_choice}")

            # CRITICAL: Check if session is still active (could have been stopped during countdown)
            if not self.session_active:
                c.log("[TV Evening Monitor] WARNING: Session was stopped during countdown - aborting playback")
                return

            if user_choice == 'cancel':
                c.log("[TV Evening Monitor] User cancelled TV Evening (soft stop)")
                self.soft_stop_session('countdown cancel')
            elif user_choice == 'play' or user_choice is None:
                # Exit gap before plugin play — sources.play blocks during pausescreen
                control.window.clearProperty('midnight.tvevening.transitioning')
                tvevening_session.set_state(
                    tvevening_session.STATE_PLAYING,
                    position=self.current_position,
                )
                self._play_episode(next_episode)
        finally:
            control.window.clearProperty('midnight.tvevening.transitioning')

    def _play_episode(self, episode_data):
        """Play the episode using pre-scraped sources or fallback."""
        try:
            control.window.setProperty('midnight.tvevening.last_play_started', str(time.time()))
            # CRITICAL SAFETY CHECK: Abort if session is no longer active
            # This prevents zombie playback after session ends
            if not self.session_active:
                c.log("[TV Evening Monitor] ABORT: Playback requested but session is no longer active (zombie prevention)")
                return

            # Test mode: Direct URL playback using Kodi playlist
            if self.test_mode:
                url = episode_data.get('url')
                if url:
                    if c.devmode:
                        c.log(f"[TV Evening Monitor] TEST MODE: Playing via Kodi playlist (URL: {url})")
                    # Get Kodi playlist
                    playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)

                    # Play from current position in playlist
                    # This maintains playlist position tracking
                    if c.devmode:
                        c.log("[TV Evening Monitor] TEST MODE: Playing position {} from playlist".format(self.current_position))
                    self.player.play(playlist, startpos=self.current_position)
                    if c.devmode:
                        c.log("[TV Evening Monitor] TEST MODE: Playlist playback started")
                    return
                else:
                    if c.devmode:
                        c.log(f"[TV Evening Monitor] TEST MODE: No URL found in episode_data (keys: {list(episode_data.keys())})")
                    self.stop_session()
                    return

            # Normal mode: play via rebuilt Kodi playlist so position stays in sync
            # and Kodi does not surface stale "unplayable item" errors from skipped eps.
            c.log("[TV Evening Monitor] Using Kodi playlist for source scraping")
            self._rebuild_kodi_playlist(self.current_position)
            playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
            if playlist.size() > 0:
                clear_av_started()
                try:
                    control.set_playback_owner(control.PLAYBACK_OWNER_SOURCES)
                except Exception:
                    pass
                self.player.play(playlist, startpos=0)
                c.log("[TV Evening Monitor] Playlist playback started at position 0 (monitor index {})".format(
                    self.current_position))
            else:
                url = self._build_episode_url(episode_data)
                if url:
                    clear_av_started()
                    try:
                        control.set_playback_owner(control.PLAYBACK_OWNER_SOURCES)
                    except Exception:
                        pass
                    xbmc.executebuiltin('PlayMedia({})'.format(url))
                    c.log("[TV Evening Monitor] Fallback PlayMedia started (empty playlist)")
                else:
                    c.log("[TV Evening Monitor] No URL available")
                    self.stop_session()
                    return

        except Exception as e:
            c.log(f"[TV Evening Monitor] Error playing episode: {e}")
            self.stop_session()


def get_tv_evening_monitor(playlist_episodes=None, test_mode=False, start_position=0, initial_state=tvevening_session.STATE_START):
    """Get or create TV Evening monitor instance."""
    global _monitor_instance

    if playlist_episodes is not None:
        if c.devmode:
            c.log(f"[Monitor Singleton] Creating NEW monitor instance (test_mode={test_mode})")
        _monitor_instance = TVEveningMonitor(
            playlist_episodes,
            test_mode=test_mode,
            start_position=start_position,
            initial_state=initial_state,
        )
        if c.devmode:
            c.log(f"[Monitor Singleton] Created monitor: {_monitor_instance}, session_active={_monitor_instance.session_active if _monitor_instance else 'N/A'}")
    elif c.devmode:
        c.log(f"[Monitor Singleton] GET request - Returning existing monitor: {_monitor_instance}, session_active={_monitor_instance.session_active if _monitor_instance else 'N/A'}")

    return _monitor_instance


def start_tv_evening_session(playlist_episodes, test_mode=False, start_position=0, initial_state=tvevening_session.STATE_START):
    """
    Start a new TV Evening monitoring session.

    Args:
        playlist_episodes: List of episode dicts
        test_mode: If True, enable test mode (direct URL playback)
        start_position: Zero-based resume index
        initial_state: Session state when resuming from DB

    Returns:
        TVEveningMonitor instance, or None if session already active
    """
    global _monitor_instance

    if _monitor_instance and _monitor_instance.session_active:
        c.log("[Monitor Singleton] WARNING: Cannot start new session - another session is already active")
        if c.devmode:
            c.log("[Monitor Singleton] Active session: {}, position {}/{}".format(
                _monitor_instance,
                _monitor_instance.current_position,
                len(_monitor_instance.playlist_episodes)
            ))
        return None

    monitor = get_tv_evening_monitor(
        playlist_episodes,
        test_mode=test_mode,
        start_position=start_position,
        initial_state=initial_state,
    )
    monitor.start_session()
    return monitor

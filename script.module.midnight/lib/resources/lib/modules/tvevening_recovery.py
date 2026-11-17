# -*- coding: utf-8 -*-

'''
********************************************************cm*
* Midnight Add-on
*
* @file tvevening_recovery.py
* @package script.module.midnight
*
* @copyright (c) 2026, Midnight
* @license GNU General Public License, version 3 (GPL-3.0)
*
********************************************************cm*
TV Evening Recovery System

Detects and handles resumable TV Evening sessions after Kodi crashes or restarts.
Provides user options to continue, start fresh, or delete the stale playlist.
'''

from . import tvevening_session
from . import tv_evening_recovery_dialog
from .crewruntime import c


def has_stale_session():
    """
    Resumable playlist exists but no live monitor thread (restart / crash / pause).
    """
    try:
        if tvevening_session.is_resumable_and_monitor_inactive():
            info = tvevening_session.load_full_session_from_db()
            c.log(
                "[TV Evening Recovery] Resumable session: {} episodes, state={}, position={}".format(
                    info.get('playlist_size', 0),
                    info.get('session_state'),
                    info.get('current_position'),
                )
            )
            return True
        return False

    except Exception as e:
        c.log(f"[TV Evening Recovery] Error checking for stale session: {e}")
        return False


def get_session_info():
    """Session snapshot from DB (not window properties)."""
    try:
        return tvevening_session.load_full_session_from_db()
    except Exception as e:
        c.log(f"[TV Evening Recovery] Error getting session info: {e}")
        return None


def handle_recovery():
    """
    Main recovery handler - shows dialog and handles user choice.

    :return: User choice ('continue', 'fresh', 'delete', or None if no resumable session)
    """
    try:
        if not has_stale_session():
            return None

        c.log("[TV Evening Recovery] Handling resumable session recovery")

        session_info = get_session_info()
        if not session_info or not session_info.get('episodes'):
            c.log("[TV Evening Recovery] Could not get session info")
            return None

        user_choice = tv_evening_recovery_dialog.show_recovery_dialog(
            session_info['playlist_size'],
            session_info['current_position'],
            session_info['episode_titles'],
            session_state=session_info.get('session_state'),
        )

        c.log(f"[TV Evening Recovery] User choice: {user_choice}")

        if user_choice == 'continue':
            c.log("[TV Evening Recovery] User chose to continue playlist")
            return 'continue'

        if user_choice == 'fresh':
            c.log("[TV Evening Recovery] User chose to start fresh")
            clear_stale_session()
            return 'fresh'

        if user_choice == 'delete':
            c.log("[TV Evening Recovery] User chose to delete playlist")
            clear_stale_session()
            return 'delete'

        c.log("[TV Evening Recovery] Dialog cancelled or error")
        return None

    except Exception as e:
        c.log(f"[TV Evening Recovery] Error in handle_recovery: {e}")
        import traceback
        c.log(f"[TV Evening Recovery] Traceback: {traceback.format_exc()}")
        return None


def clear_stale_session():
    """End session and remove all TV Evening data."""
    try:
        tvevening_session.end_session()
        c.log("[TV Evening Recovery] Session cleared")
        return True
    except Exception as e:
        c.log(f"[TV Evening Recovery] Error clearing stale session: {e}")
        return False


def resume_session(session_info):
    """Log resume intent — playback handled by tvevening.resume_playlist."""
    try:
        c.log(
            "[TV Evening Recovery] Resume info: position={} total={} state={}".format(
                session_info.get('current_position'),
                session_info.get('playlist_size'),
                session_info.get('session_state'),
            )
        )
        return True
    except Exception as e:
        c.log(f"[TV Evening Recovery] Error resuming session: {e}")
        return False

# -*- coding: utf-8 -*-

'''
********************************************************cm*
* Midnight Add-on
*
* @file tvevening_complete.py
* @package script.module.midnight
*
* @copyright (c) 2026, Midnight
* @license GNU General Public License, version 3 (GPL-3.0)
*
********************************************************cm*
TV Evening Completion Dialog
Shows a friendly completion message when TV Evening playlist finishes.
'''

import datetime
import json
import threading
import time

import xbmc
import xbmcaddon
import xbmcgui

from . import control
from .crewruntime import c


COMPLETE_PAYLOAD_PROP = 'midnight.tvevening.complete.params'
COMPLETE_PENDING_PROP = 'midnight.tvevening.complete.pending'


class TVEveningComplete(xbmcgui.WindowXMLDialog):
    """
    Completion dialog shown when TV Evening playlist finishes.
    Features:
    - Time-based greeting (morning/afternoon/evening)
    - Auto-close countdown (30 seconds)
    - Stats about episodes watched
    """

    def __init__(self, *args, **kwargs):
        """Initialize the completion dialog."""
        super(TVEveningComplete, self).__init__(*args, **kwargs)

        self.episodes_watched = kwargs.get('episodes_watched', 0)
        self.fanart = kwargs.get('fanart', '')

        self.countdown_seconds = 30
        self.countdown_active = True
        self.user_closed = False
        self._stop_event = threading.Event()

    def onInit(self):
        """Initialize the dialog UI."""
        c.log("[TV Evening Complete] Initializing completion dialog")

        # Set fanart background with fallback to addon fanart
        fanart = self.fanart if self.fanart else c.addon_fanart()
        self.setProperty('fanart', fanart)

        # Set time-based greeting
        greeting = self._get_time_based_greeting()
        self.setProperty('greeting', greeting)

        # Set stats
        if self.episodes_watched > 0:
            self.setProperty('stats', 'You watched {} episode{}'.format(
                self.episodes_watched,
                's' if self.episodes_watched > 1 else ''
            ))

        # Start countdown timer
        self._start_countdown()

    def _get_time_based_greeting(self):
        """Get greeting based on time of day."""
        now = datetime.datetime.now()
        hour = now.hour

        if 5 <= hour < 12:
            return "Have a great day!"
        elif 12 <= hour < 17:
            return "Enjoy your day!"
        elif 17 <= hour < 22:
            return "Have a wonderful evening!"
        else:
            return "Sleep well!"

    def _start_countdown(self):
        """Start the auto-close countdown."""
        self._stop_event.clear()
        countdown_thread = threading.Thread(target=self._countdown_loop)
        countdown_thread.daemon = True
        countdown_thread.start()

    def _countdown_loop(self):
        """Countdown loop from 30 to 0."""
        for remaining in range(self.countdown_seconds, 0, -1):
            if not self.countdown_active or self.user_closed:
                break

            # Update timer display
            self.setProperty('timer', 'Closing in {} second{}...'.format(
                remaining,
                's' if remaining > 1 else ''
            ))

            # Wait 1 second — wakes immediately if stop is requested
            self._stop_event.wait(1)

        # Auto-close if user didn't close manually
        if self.countdown_active and not self.user_closed:
            c.log("[TV Evening Complete] Auto-closing after {} seconds".format(self.countdown_seconds))
            self.close()

    def onClick(self, controlID):
        """Handle button clicks."""
        if controlID == 5:  # Close button
            c.log("[TV Evening Complete] User clicked Close")
            self.user_closed = True
            self.countdown_active = False
            self._stop_event.set()
            self.close()

    def onAction(self, action):
        """Handle user actions."""
        # Close on back/escape
        if action.getId() in (9, 10, 92, 216, 247, 257, 275, 61467, 61448):
            c.log("[TV Evening Complete] User pressed back/escape")
            self.user_closed = True
            self.countdown_active = False
            self._stop_event.set()
            self.close()


def _run_completion_modal(episodes_watched=0, fanart=''):
    """Show completion dialog on the main/service thread."""
    c.log("[TV Evening Complete] Showing completion dialog: {} episodes watched".format(episodes_watched))

    artwork_addon = xbmcaddon.Addon('script.midnight.artwork')
    addon_path = artwork_addon.getAddonInfo('path')
    theme = c.appearance() or 'midnight'
    c.log("[TV Evening Complete] Using theme: {}".format(theme))

    dialog = TVEveningComplete(
        'TVEveningComplete.xml',
        addon_path,
        theme,
        '1080i',
        episodes_watched=episodes_watched,
        fanart=fanart,
    )
    dialog.doModal()
    del dialog

    try:
        from . import tvevening_playlist_db
        tvevening_playlist_db.get_playlist_db().clear_playlist()
        xbmc.PlayList(xbmc.PLAYLIST_VIDEO).clear()
        c.log("[TV Evening Complete] Playlist cleared after completion")
    except Exception as e:
        c.log("[TV Evening Complete] Error clearing playlist after completion: {}".format(e))

    control.window.clearProperty('midnight.tvevening.block_binge')
    try:
        from . import tvevening_session
        tvevening_session.clear_completing()
        control.window.clearProperty('midnight.tvevening.transitioning')
    except Exception as e:
        c.log("[TV Evening Complete] Error clearing completion gate: {}".format(e))
    c.log("[TV Evening Complete] Completion dialog closed — binge block cleared")


def request_completion_dialog(episodes_watched=0, fanart=''):
    """Queue completion dialog for CrewMonitor service dispatch."""
    payload = json.dumps({
        'episodes_watched': episodes_watched,
        'fanart': fanart,
    })
    control.window.setProperty(COMPLETE_PAYLOAD_PROP, payload)
    control.window.setProperty(COMPLETE_PENDING_PROP, 'true')
    c.log("[TV Evening Complete] Queued for service dispatch ({} episodes)".format(episodes_watched))


def dispatch_pending_completion_dialog():
    """Called from CrewMonitor service loop."""
    if control.window.getProperty(COMPLETE_PENDING_PROP) != 'true':
        return False
    control.window.clearProperty(COMPLETE_PENDING_PROP)
    raw = control.window.getProperty(COMPLETE_PAYLOAD_PROP)
    control.window.clearProperty(COMPLETE_PAYLOAD_PROP)
    if not raw:
        c.log("[TV Evening Complete] Dispatch: no queued params")
        return False
    try:
        data = json.loads(raw)
    except Exception as e:
        c.log("[TV Evening Complete] Dispatch: bad payload: {}".format(e))
        return False
    c.log("[TV Evening Complete] Service dispatch: showing queued dialog")
    _run_completion_modal(
        episodes_watched=int(data.get('episodes_watched') or 0),
        fanart=data.get('fanart') or '',
    )
    return True


def run_completion_from_property():
    """Plugin router entry — same as dispatch but callable via RunPlugin."""
    return dispatch_pending_completion_dialog()


def show_completion_dialog(episodes_watched=0, fanart=''):
    """Show completion dialog (main thread only)."""
    _run_completion_modal(episodes_watched=episodes_watched, fanart=fanart)

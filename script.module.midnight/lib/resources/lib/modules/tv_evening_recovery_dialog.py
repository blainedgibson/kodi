# -*- coding: utf-8 -*-

'''
********************************************************cm*
* Midnight Add-on
*
* @file tv_evening_recovery_dialog.py
* @package script.module.midnight
*
* @copyright (c) 2026, Midnight
* @license GNU General Public License, version 3 (GPL-3.0)
*
********************************************************cm*
TV Evening Recovery Dialog - Handle stale session recovery
'''

import xbmcgui
from . import control
from .crewruntime import c


class TVEveningRecoveryDialog(xbmcgui.WindowXMLDialog):
    """
    Dialog for recovering stale TV Evening sessions.

    Shows user options when a playlist is found after Kodi restart/crash:
    - Continue Playing (resume from where they left off)
    - Start Fresh (clear and create new playlist)
    - Delete Playlist (just remove the stale data)
    """

    # Button IDs
    BUTTON_CONTINUE = 9101
    BUTTON_START_FRESH = 9102
    BUTTON_DELETE = 9103

    def __init__(self, *args, **kwargs):
        """Initialize recovery dialog."""
        super(TVEveningRecoveryDialog, self).__init__(*args, **kwargs)
        self.message1 = kwargs.get('message1', '')
        self.message2 = kwargs.get('message2', '')
        self.user_choice = None

    def onInit(self):
        """Initialize dialog controls."""
        try:
            # Set messages
            self.setProperty('message1', self.message1)
            self.setProperty('message2', self.message2)

            # Set focus to Continue button
            self.setFocusId(self.BUTTON_CONTINUE)

        except Exception as e:
            control.log(f"[TV Evening Recovery] Error in onInit: {e}")

    def onClick(self, controlId):
        """Handle button clicks."""
        try:
            if controlId == self.BUTTON_CONTINUE:
                self.user_choice = 'continue'
                self.close()
            elif controlId == self.BUTTON_START_FRESH:
                self.user_choice = 'fresh'
                self.close()
            elif controlId == self.BUTTON_DELETE:
                self.user_choice = 'delete'
                self.close()

        except Exception as e:
            control.log(f"[TV Evening Recovery] Error in onClick: {e}")

    def onAction(self, action):
        """Handle actions like back button."""
        try:
            # Back/Escape = treat as Delete (don't show dialog again)
            if action.getId() in (9, 10, 92):  # ACTION_NAV_BACK, ACTION_PREVIOUS_MENU, ACTION_MOUSE_RIGHT_CLICK
                self.user_choice = 'delete'
                self.close()

        except Exception as e:
            control.log(f"[TV Evening Recovery] Error in onAction: {e}")

    def get_user_choice(self):
        """Get the user's choice after dialog closes."""
        return self.user_choice


def show_recovery_dialog(playlist_size, current_position, episode_titles=None, session_state=None):
    """
    Show recovery dialog and return user choice.

    :param int playlist_size: Total episodes in playlist
    :param int current_position: Last known position
    :param list episode_titles: Optional list of episode titles
    :return: User choice ('continue', 'fresh', 'delete', or None)
    :rtype: str or None
    """
    try:
        # Build message
        remaining = playlist_size - current_position
        if remaining > 0:
            message1 = f"You have an unfinished playlist with {remaining} episode{'s' if remaining != 1 else ''} remaining."
        else:
            message1 = f"You have a completed playlist with {playlist_size} episode{'s' if playlist_size != 1 else ''}."

        # Add context about which episode and session state
        ep_num = current_position + 1
        state_hint = ''
        if session_state == 'paused':
            state_hint = ' (paused)'
        elif session_state == 'pausescreen':
            state_hint = ' (between episodes)'
        elif session_state == 'playing':
            state_hint = ' (in progress)'

        resume_label = ''
        if episode_titles and 0 <= current_position < len(episode_titles):
            resume_label = episode_titles[current_position]

        if resume_label and playlist_size > 0:
            message2 = f"Continue with: {resume_label}{state_hint}"
        elif current_position >= 0 and playlist_size > 0:
            message2 = f"Episode {ep_num} of {playlist_size}{state_hint}"
        else:
            message2 = f"Playlist contains {playlist_size} episode{'s' if playlist_size != 1 else ''}"

        addon_path = c.get_artwork_path()
        skin = c.appearance() or 'midnight'
        if skin in ['-', '']:
            skin = 'midnight'

        control.log(f"[TV Evening Recovery] Using skin: {skin}, addon path: {addon_path}")

        # Create and show dialog
        dialog = TVEveningRecoveryDialog(
            'TVEveningRecovery.xml',
            addon_path,
            skin,
            '1080i',
            message1=message1,
            message2=message2
        )
        dialog.doModal()

        choice = dialog.get_user_choice()
        del dialog

        return choice

    except Exception as e:
        control.log(f"[TV Evening Recovery] Error showing dialog: {e}")
        return None

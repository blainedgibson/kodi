# -*- coding: utf-8 -*-

'''
********************************************************cm*
* Midnight Add-on
*
* @file version_announcement.py
* @package script.module.midnight
*
* @copyright (c) 2026, Midnight
* @license GNU General Public License, version 3 (GPL-3.0)
*
********************************************************cm*
Version Announcement Dialog
Custom dialog for new version announcements, alpha warnings, and highlights
'''

from __future__ import annotations

import os
import time
from typing import Tuple
import xbmc
import xbmcaddon
import xbmcgui

from . import control
from . import version_config
from . import changelog
from .crewruntime import c

PROP_BOOT_STARTED = 'midnight.boot_started_at'
PROP_VERSION_HANDLED = 'midnight.version_announcement.handled'
BOOT_ANNOUNCE_MAX_SECONDS = 45


def _last_announced_version() -> str:
    """Read from userdata XML first — Kodi in-memory settings can lag behind disk."""
    try:
        from .settings_repair import parse_userdata_settings, _userdata_path, _file_exists
        user_path = _userdata_path()
        if _file_exists(user_path):
            disk_val = (parse_userdata_settings(user_path).get('last_version_announcement') or '').strip()
            if disk_val:
                return disk_val
    except Exception:
        pass
    return (control.setting('last_version_announcement') or '').strip()


def _persist_announced_version(version: str) -> bool:
    '''
    Persist to disk AND Kodi in-memory settings.

    Disk-only writes get stomped when a later setSetting() flushes the addon
    settings cache (still holding the previous last_version) back to settings.xml.
    '''
    version = (version or '').strip()
    if not version:
        return False

    disk_ok = False
    try:
        from .settings_repair import persist_setting_on_disk
        disk_ok = persist_setting_on_disk('last_version_announcement', version, log=c.log)
    except Exception as exc:
        c.log(f'[Version Announcement] Disk persist failed: {exc}')

    memory_ok = False
    try:
        control.setSetting('last_version_announcement', version)
        memory_ok = True
    except Exception as exc:
        c.log(f'[Version Announcement] setSetting failed: {exc}')

    if disk_ok or memory_ok:
        c.log(
            f'[Version Announcement] Marked last_version_announcement={version} '
            f'(disk={disk_ok}, memory={memory_ok})'
        )
        return True
    return False


def _boot_age_seconds() -> float:
    try:
        raw = control.window.getProperty(PROP_BOOT_STARTED)
        if not raw:
            return 9999.0
        return max(0.0, time.time() - float(raw))
    except (TypeError, ValueError):
        return 9999.0


def mark_boot_started() -> None:
    """Record service start time — used to skip late interrupt dialogs."""
    if not control.window.getProperty(PROP_BOOT_STARTED):
        control.window.setProperty(PROP_BOOT_STARTED, str(int(time.time())))


SETTING_BUILDERS_SUPPRESS_STARTUP_UI = 'builders.suppress_startup_ui'


def suppress_startup_ui() -> bool:
    """Builders / Kodi-builds kill switch — mute all Midnight startup UI.

    Independent of silent.boot (end users may still want debrid expiry toasts).
    """
    try:
        return control.setting(SETTING_BUILDERS_SUPPRESS_STARTUP_UI) == 'true'
    except Exception:
        return False


def boot_interrupt_ok() -> Tuple[bool, str]:
    """False when the user is already engaged — do not show blocking boot dialogs."""
    if suppress_startup_ui():
        return False, 'builders_suppress_startup_ui'
    try:
        if xbmc.Player().isPlaying():
            return False, 'playback_active'
    except Exception:
        pass
    age = _boot_age_seconds()
    if age > BOOT_ANNOUNCE_MAX_SECONDS:
        return False, f'boot_window_passed ({age:.0f}s)'
    return True, ''


def _acknowledge_version(current_version: str, reason: str, persist: bool) -> None:
    control.window.setProperty(PROP_VERSION_HANDLED, 'true')
    if persist:
        _persist_announced_version(current_version)
    line = f'Acknowledged v{current_version} ({reason}) — no dialog'
    c.log(f'[Version Announcement] {line}')
    xbmc.log(f'[Midnight][Version Announcement] {line}', xbmc.LOGINFO)


class VersionAnnouncementDialog(xbmcgui.WindowXMLDialog):
    """
    Custom dialog for version announcements.
    """

    def __init__(self, *args, **kwargs):
        """Initialize dialog with announcement data."""
        self.version = kwargs.get('version', '')
        self.announcement_type = kwargs.get('announcement_type', 'NEW VERSION')
        self.highlights = kwargs.get('highlights', [])
        self.show_alpha_warning = kwargs.get('show_alpha_warning', False)
        self.changelog_path = kwargs.get('changelog_path', '')
        self.github_url = kwargs.get('github_url', 'https://github.com/classymouse/script.module.midnight')
        self.action = None
        self._persist_on_close = kwargs.get('persist_on_close', False)
        self._version_to_persist = kwargs.get('version_to_persist', '')

    def onInit(self):
        """Initialize the dialog window."""
        try:
            # Set version
            self.setProperty('version', self.version)

            # Set announcement type
            self.setProperty('announcement_type', self.announcement_type)

            # Set highlights (up to 3)
            for i, highlight in enumerate(self.highlights[:3], 1):
                self.setProperty(f'highlight{i}', highlight)

            # Set alpha warning
            if self.show_alpha_warning:
                self.setProperty('show_alpha_warning', 'true')

            # Set focus on Continue button by default
            self.setFocusId(103)

        except Exception as e:
            c.log(f"[Version Announcement] Error in onInit: {e}")

    def onClick(self, control_id):
        """Handle button clicks."""
        try:
            c.log(f"[Version Announcement] Button clicked: {control_id}")

            if control_id == 101:
                # View Changelog - show but keep dialog open
                c.log("[Version Announcement] Opening changelog...")
                if self.changelog_path and os.path.exists(self.changelog_path):
                    try:
                        with open(self.changelog_path, 'r', encoding='utf-8') as f:
                            changelog_text = f.read()
                        # Use custom LogViewer dialog instead of basic textviewer
                        changelog.log_viewer(changelog_text, 'Midnight Changelog')
                    except Exception as e:
                        c.log(f"[Version Announcement] Error showing changelog: {e}")
                else:
                    control.dialog.ok('Changelog', 'Changelog file not found.')

            elif control_id == 102:
                # Beta/Alpha Info - open browser but keep dialog open
                # Detect if this is beta or alpha based on current version info

                addon = xbmcaddon.Addon('script.module.midnight')
                current_version = addon.getAddonInfo('version')
                is_beta = 'beta' in current_version.lower()

                info_type = 'Beta' if is_beta else 'Alpha'
                c.log(f"[Version Announcement] Opening {info_type.lower()} info page...")
                info_url = 'https://classymouse.github.io'
                try:
                    import webbrowser
                    webbrowser.open(info_url)
                except:
                    # Fallback: show URL in dialog
                    control.dialog.ok(f'{info_type} Info', f'Visit our website for {info_type.lower()} version information:\n\n{info_url}')
            elif control_id == 103:
                # Close button - close the dialog
                c.log("[Version Announcement] Closing dialog...")
                self.action = 'close'
                if self._persist_on_close and self._version_to_persist:
                    _persist_announced_version(self._version_to_persist)
                self.close()

        except Exception as e:
            c.log(f"[Version Announcement] Error in onClick: {e}")
            import traceback
            c.log(f"[Version Announcement] Traceback: {traceback.format_exc()}")

    def onAction(self, action):
        """Handle keyboard/remote actions."""
        if action.getId() in (9, 10, 92, 216, 247, 257, 275, 61467, 61448):
            # Back/Escape actions - close dialog
            self.action = 'close'
            if self._persist_on_close and self._version_to_persist:
                _persist_announced_version(self._version_to_persist)
            self.close()


def show_version_announcement(version, announcement_type='NEW VERSION', highlights=None,
                            show_alpha_warning=False, changelog_path='',
                            persist_on_close=False, version_to_persist=''):
    """
    Show the version announcement dialog.

    Args:
        version (str): Version string to display (e.g., "v2.3.0", "2.2.5-alpha")
        announcement_type (str): Type of announcement (e.g., "NEW VERSION", "UPDATE AVAILABLE")
        highlights (list): List of highlight strings (max 3)
        show_alpha_warning (bool): Whether to show alpha version warning
        changelog_path (str): Path to changelog file

    Returns:
        str: User action ('changelog', 'github', 'close', or None)
    """
    try:
        if highlights is None:
            highlights = []

        # Determine skin path based on user's appearance setting
        appearance = control.setting('appearance.1') or 'modern'
        if appearance not in ['modern', 'midnight']:
            appearance = 'modern'

        addon = xbmcaddon.Addon('script.midnight.artwork')
        xml_file = 'VersionAnnouncement.xml'
        skin_path = os.path.join(addon.getAddonInfo('path'), 'resources', 'skins', appearance, '1080i')

        c.log(f"[Version Announcement] Opening dialog: version={version}, type={announcement_type}")
        c.log(f"[Version Announcement] Highlights: {highlights}")
        c.log(f"[Version Announcement] Alpha warning: {show_alpha_warning}")

        dialog = VersionAnnouncementDialog(
            xml_file,
            addon.getAddonInfo('path'),
            appearance,
            '1080i',
            version=version,
            announcement_type=announcement_type,
            highlights=highlights,
            show_alpha_warning=show_alpha_warning,
            changelog_path=changelog_path,
            persist_on_close=persist_on_close,
            version_to_persist=version_to_persist,
        )

        dialog.doModal()
        action = dialog.action

        c.log(f"[Version Announcement] Dialog closed with action: {action}")

        # All actions are now handled inside onClick, so we just clean up
        del dialog
        return action

    except Exception as e:
        c.log(f"[Version Announcement] Error showing dialog: {e}")
        import traceback
        c.log(f"[Version Announcement] Traceback: {traceback.format_exc()}")
        return None


def check_and_show_version_announcement():
    """
    Check if version announcement should be shown and display it.

    Shows only during the first ~45s of service boot while the user is idle.
    If the service starts late (user already watching / testing), skip the dialog
    and persist last_version on disk so we do not interrupt mid-session.

    Returns:
        bool: True if announcement was shown, False otherwise
    """
    try:
        if control.window.getProperty(PROP_VERSION_HANDLED) == 'true':
            xbmc.log('[Midnight][Version Announcement] Already handled this boot', xbmc.LOGINFO)
            return False

        # Get current versions
        module_addon = xbmcaddon.Addon('script.module.midnight')
        current_version = module_addon.getAddonInfo('version')

        # Check if version contains 'alpha' or 'beta'
        is_alpha = 'alpha' in current_version.lower()
        is_beta = 'beta' in current_version.lower()

        # Get last shown version from settings (disk-first)
        last_shown_version = _last_announced_version()

        # Check if we should show announcement
        should_show = False
        reason = ''

        # For alpha/beta, show once per Kodi session (not persistent)
        if is_alpha or is_beta:
            session_shown = control.window.getProperty('midnight.version_announcement.shown')
            if session_shown != 'true':
                should_show = True
                reason = 'alpha_session'
        # For stable releases, show once when version changes
        elif current_version != last_shown_version:
            should_show = True
            reason = 'new_version'

        if not should_show:
            msg = f'No announcement needed (current: {current_version}, last: {last_shown_version})'
            c.log(f"[Version Announcement] {msg}")
            xbmc.log(f'[Midnight][Version Announcement] {msg}', xbmc.LOGINFO)
            control.window.setProperty(PROP_VERSION_HANDLED, 'true')
            return False

        ok, skip_reason = boot_interrupt_ok()
        if not ok:
            _acknowledge_version(
                current_version,
                f'skipped — {skip_reason}',
                persist=(reason == 'new_version'),
            )
            return False

        c.log(f"[Version Announcement] Showing announcement for {current_version} (reason: {reason})")

        # Claim this version before doModal — Close/Esc/restart must not leave us
        # stuck on last_version forever (and avoids setSetting flush races after).
        if reason == 'new_version':
            _persist_announced_version(current_version)

        # Get plugin version too
        try:
            plugin_addon = xbmcaddon.Addon('plugin.video.midnight')
            plugin_version = plugin_addon.getAddonInfo('version')
            version_display = f"Module v{current_version} | Plugin v{plugin_version}"
        except:
            # Fallback if plugin not found
            version_display = f"v{current_version}"

        # Get announcement data from config
        announcement_data = version_config.get_announcement_data(current_version)

        # Get changelog path
        changelog_path = os.path.join(module_addon.getAddonInfo('path'), 'changelog.txt')

        # Show the dialog
        action = show_version_announcement(
            version=version_display,
            announcement_type=announcement_data['announcement_type'],
            highlights=announcement_data['highlights'],
            show_alpha_warning=announcement_data['show_warning'],
            changelog_path=changelog_path,
            persist_on_close=False,
            version_to_persist='',
        )

        # Mark as shown for this boot
        control.window.setProperty(PROP_VERSION_HANDLED, 'true')
        if reason == 'new_version':
            # Re-assert after dialog — later boot setSetting flushes must see memory value.
            _persist_announced_version(current_version)
        elif reason == 'alpha_session':
            control.window.setProperty('midnight.version_announcement.shown', 'true')

        return True

    except Exception as e:
        c.log(f"[Version Announcement] Error in check_and_show: {e}")
        import traceback
        c.log(f"[Version Announcement] Traceback: {traceback.format_exc()}")
        return False

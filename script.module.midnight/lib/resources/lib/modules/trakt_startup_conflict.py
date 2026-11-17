# -*- coding: utf-8 -*-

'''
********************************************************cm*
* Midnight Add-on
*
* @file trakt_startup_conflict.py
* @package script.module.midnight
*
* @copyright (c) 2026, Midnight
* @license GNU General Public License, version 3 (GPL-3.0)
*
********************************************************cm*

Detect addons that run immediate TraktMonitor sync at Kodi startup and log a
one-time neutral notice to kodi.log (no addon names in the user-visible message).
'''

import json
import os
import sqlite3
import xml.etree.ElementTree as ET

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from resources.lib.modules import control

_LOG_TAG = '[script.module.midnight][Trakt]'

LOG_MESSAGE_LINES = (
    'Startup conflict: Other installed addons hit Trakt immediately when Kodi starts, '
    'without taking other addons into consideration. This behavior can block or even '
    'break Trakt for everything else during the first few minutes after startup.',
    'Midnight defers background Trakt until that rush eases. For a stable box, disable '
    'background Trakt in those addons, or do not authorize Trakt there.',
)

PROP_SESSION_LOGGED = 'midnight.trakt_startup_conflict_logged'

_SKIP_ADDON_IDS = frozenset({
    'plugin.video.midnight',
    'script.module.midnight',
    'script.midnight.artwork',
    'script.trakt',
    'script.module.trakt',
})

_TRAKT_USER_KEYS = ('trakt.user', 'trakt_user')
_TRAKT_TOKEN_KEYS = ('trakt.token', 'trakt.refresh')

_EMPTY_VALUES = frozenset({'', '0', 'empty_setting', 'false', 'none', 'null'})

_SERVICE_REL_PATHS = (
    'service.py',
    os.path.join('resources', 'lib', 'service.py'),
    os.path.join('resources', 'service.py'),
)


def _read_service_bytes(service_path):
    """Read service source; xbmcvfs and plain open for Android compatibility."""
    try:
        local_path = xbmcvfs.translatePath(service_path)
        with open(local_path, 'rb') as handle:
            return handle.read(131072)
    except Exception:
        pass
    try:
        handle = xbmcvfs.File(service_path)
        try:
            raw = handle.read()
        finally:
            handle.close()
        if isinstance(raw, str):
            return raw.encode('utf-8', errors='ignore')
        return raw or b''
    except Exception:
        return b''


def _addon_has_service_extension(addon_path):
    addon_xml = os.path.join(addon_path, 'addon.xml')
    if not xbmcvfs.exists(addon_xml):
        return False
    try:
        root = ET.parse(addon_xml).getroot()
        for ext in root.findall('.//extension'):
            if ext.get('point') == 'xbmc.service':
                return True
    except Exception:
        pass
    return False


def _fen_like_service_fingerprint(addon_path):
    """Fen-lineage tell: TraktMonitor + immediate sync in the service module."""
    for rel in _SERVICE_REL_PATHS:
        service_path = os.path.join(addon_path, rel)
        if not xbmcvfs.exists(service_path):
            continue
        content = _read_service_bytes(service_path)
        if b'TraktMonitor' not in content:
            continue
        if b'trakt_sync_activities' in content or b'trakt_sync' in content:
            return True
    return False


def _addon_short_name(addon_id):
    if addon_id.startswith('plugin.video.') and addon_id.count('.') >= 2:
        return addon_id.split('.', 2)[2]
    return ''


def _trakt_via_window_properties(addon_id):
    short = _addon_short_name(addon_id)
    if not short:
        return False
    win = xbmcgui.Window(10000)
    for prop in (
        f'{short}.trakt.user',
        f'{short}.trakt.token',
        f'{short}.trakt.refresh',
        f'{short}.trakt_user',
        'trakt.user',
        'trakt.token',
        'trakt.refresh',
    ):
        val = (win.getProperty(prop) or '').strip()
        if val.lower() not in _EMPTY_VALUES:
            return True
    return False


def _trakt_via_settings_db(addon_id):
    """Fen-lineage stores runtime Trakt creds in settings.db, not settings.xml."""
    try:
        profile = xbmcaddon.Addon(addon_id).getAddonInfo('profile')
        db_path = os.path.join(profile, 'settings.db')
        if not xbmcvfs.exists(db_path):
            return False
        db_path = xbmcvfs.translatePath(db_path)
        dbcon = sqlite3.connect(db_path)
        try:
            rows = dbcon.execute(
                "SELECT setting_id, setting_value FROM settings "
                "WHERE setting_id LIKE '%trakt%'"
            ).fetchall()
        finally:
            dbcon.close()
        for _sid, value in rows:
            if not _sid:
                continue
            if 'client' in _sid.lower() or 'secret' in _sid.lower():
                continue
            if (str(value or '').strip().lower() not in _EMPTY_VALUES):
                if 'user' in _sid.lower() or 'token' in _sid.lower() or 'refresh' in _sid.lower():
                    return True
    except Exception:
        pass
    return False


def _trakt_authorized_in_addon(addon_id):
    try:
        addon = xbmcaddon.Addon(addon_id)
    except Exception:
        return False

    for key in _TRAKT_USER_KEYS + _TRAKT_TOKEN_KEYS:
        try:
            value = (addon.getSetting(key) or '').strip()
        except Exception:
            value = ''
        if value.lower() not in _EMPTY_VALUES:
            return True

    if _trakt_via_window_properties(addon_id):
        return True
    if _trakt_via_settings_db(addon_id):
        return True

    try:
        profile = addon.getAddonInfo('profile')
        settings_xml = os.path.join(profile, 'settings.xml')
        if not xbmcvfs.exists(settings_xml):
            return False
        root = ET.parse(xbmcvfs.translatePath(settings_xml)).getroot()
        for node in root.findall('setting'):
            sid = node.get('id', '')
            if sid not in _TRAKT_USER_KEYS + _TRAKT_TOKEN_KEYS:
                continue
            if node.get('default') == 'true':
                continue
            text = (node.text or '').strip()
            if text.lower() not in _EMPTY_VALUES:
                return True
    except Exception:
        pass

    return False


def _is_fen_like_aggressor(addon_id, addon_path):
    if not _addon_has_service_extension(addon_path):
        return False
    if not _fen_like_service_fingerprint(addon_path):
        return False
    # Fen video addons with TraktMonitor are the risk class even when creds live
    # only in settings.db / window properties (Gears on Android).
    if addon_id.startswith('plugin.video.'):
        if _trakt_authorized_in_addon(addon_id):
            return True
        # Installed fen-lineage video addon with TraktMonitor service = warn anyway.
        return True
    return _trakt_authorized_in_addon(addon_id)


def _get_installed_addon_ids():
    """List enabled addons — xbmcaddon.Addons() is not available on all Kodi builds."""
    try:
        raw = xbmc.executeJSONRPC(json.dumps({
            'jsonrpc': '2.0',
            'method': 'Addons.GetAddons',
            'params': {'enabled': True},
            'id': 1,
        }))
        payload = json.loads(raw)
        addons = payload.get('result', {}).get('addons', [])
        return [item.get('addonid') for item in addons if item.get('addonid')]
    except Exception as e:
        xbmc.log(f'[script.module.midnight][Trakt] JSON-RPC addon list failed: {e}', xbmc.LOGWARNING)

    # Fallback: probe known fen-like video targets on the box.
    candidates = []
    for addon_id in ('plugin.video.gears', 'plugin.video.fen', 'plugin.video.fenlight',
                       'plugin.video.fenlightam', 'plugin.video.pov', 'plugin.video.umbrella'):
        try:
            if xbmc.getCondVisibility(f'System.HasAddon({addon_id})'):
                candidates.append(addon_id)
        except Exception:
            pass
    return candidates


def detect_fen_like_startup_aggressors():
    """
    Return addon IDs (for internal logs) of addons that stampede Trakt at startup.
    User-visible kodi.log lines must not name them.
    """
    found = []
    addon_ids = _get_installed_addon_ids()
    if not addon_ids:
        xbmc.log('[script.module.midnight][Trakt] Startup conflict scan: no addons listed', xbmc.LOGINFO)
        return found

    for addon_id in addon_ids:
        if addon_id in _SKIP_ADDON_IDS:
            continue
        try:
            addon_path = xbmcaddon.Addon(addon_id).getAddonInfo('path')
        except Exception:
            continue

        if not _is_fen_like_aggressor(addon_id, addon_path):
            continue

        found.append(addon_id)
        xbmc.log(
            f'[script.module.midnight][Trakt] Startup conflict: fen-like aggressor ({addon_id})',
            xbmc.LOGINFO,
        )

    return found


def log_startup_conflict_if_needed():
    """Log a one-time neutral notice to kodi.log after boot quiet (any thread)."""
    if control.window.getProperty(PROP_SESSION_LOGGED) == 'true':
        return

    from resources.lib.modules import trakt

    if not trakt.get_trakt_credentials_info():
        xbmc.log(f'{_LOG_TAG} Startup conflict: Midnight Trakt not configured', xbmc.LOGINFO)
        return

    aggressors = detect_fen_like_startup_aggressors()
    if not aggressors:
        xbmc.log(f'{_LOG_TAG} Startup conflict scan: none found', xbmc.LOGINFO)
        return

    control.window.setProperty(PROP_SESSION_LOGGED, 'true')
    xbmc.log(
        f'{_LOG_TAG} Startup conflict detected ({len(aggressors)} addon(s) with immediate startup Trakt sync)',
        xbmc.LOGINFO,
    )
    for line in LOG_MESSAGE_LINES:
        xbmc.log(f'{_LOG_TAG} {line}', xbmc.LOGWARNING)


def queue_startup_conflict_warning_if_needed():
    """Backward-compatible alias."""
    log_startup_conflict_if_needed()


def dispatch_pending_startup_conflict_warning():
    """Backward-compatible no-op (notice is kodi.log only)."""
    return


def maybe_warn_trakt_startup_conflict():
    """Backward-compatible entry."""
    log_startup_conflict_if_needed()

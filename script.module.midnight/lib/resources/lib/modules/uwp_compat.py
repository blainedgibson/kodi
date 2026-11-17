# -*- coding: utf-8 -*-

'''
********************************************************cm*
* Midnight Add-on
*
* @file uwp_compat.py
* @package script.module.midnight
*
* @copyright (c) 2026, Midnight (cm)
* @license GNU General Public License, version 3 (GPL-3.0)
*
********************************************************cm*

Xbox / UWP-safe platform helpers.

Kodi on Xbox ships a restricted Python where ``_winapi.CreateProcess`` is
missing. ``platform.uname()`` / ``win32_ver()`` call ``subprocess`` and crash
with AttributeError before any try/except around nodename checks can help.

Never call platform.uname() to detect Xbox. Use this module instead.
'''

from __future__ import annotations

import sys

_IS_XBOX = None


def is_xbox():
    '''True on Xbox One / Series (Kodi UWP). Cached after first call.'''
    global _IS_XBOX
    if _IS_XBOX is not None:
        return _IS_XBOX
    _IS_XBOX = _detect_xbox()
    return _IS_XBOX


def _detect_xbox():
    try:
        import xbmc

        info = (xbmc.getInfoLabel('System.OSVersionInfo') or '').lower()
        if 'xbox' in info:
            return True
    except Exception:
        pass
    try:
        import _winapi

        # Stripped on Xbox UWP Python  -  uname() would explode via subprocess.
        if not hasattr(_winapi, 'CreateProcess'):
            return True
    except Exception:
        pass
    return False


def platform_label():
    '''Human platform string for logs / headers. Safe on Xbox.'''
    bits = '64bits' if sys.maxsize > 2 ** 32 else '32bits'
    os_info = ''
    try:
        import xbmc

        os_info = (xbmc.getInfoLabel('System.OSVersionInfo') or '').strip()
    except Exception:
        pass

    if is_xbox():
        if os_info:
            return f'{os_info} ({bits})'
        return f'Xbox ({bits})'

    try:
        import platform

        uname = platform.uname()
        system = uname[0]
        release = uname[2]
        version = uname[3]
        if system == 'Windows':
            try:
                build = int(str(version).split('.')[-1])
                if build > 22000:
                    release = '11'
            except (TypeError, ValueError):
                pass
        return f'{system} {release} v.{version} ({bits})'
    except Exception:
        if os_info:
            return f'{os_info} ({bits})'
        return f'Unknown ({bits})'

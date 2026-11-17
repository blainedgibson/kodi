# -*- coding: utf-8 -*-

'''
 ***********************************************************
 * Midnight Add-on
 *
 *
 * @file crewruntime.py
 * @package script.module.midnight
 *
 * @copyright (c) 2023-2026, Midnight
 * @license GNU General Public License, version 3 (GPL-3.0)
 *
 ********************************************************cm*
'''


import os
import sys
import re
import platform
import json
import base64
import datetime
import importlib
import traceback
import time

from io import open
from inspect import getframeinfo, stack
from functools import wraps

import xbmc
import xbmcvfs
import xbmcaddon
import xbmcgui
import xbmcplugin

from . import keys
from resources.lib.ui_theme import color



# ========================================================================cm=
# CONSTANTS SECTION
# ========================================================================cm=

# API Keys (imported from keys.py for backward compatibility)
TMDB_KEY = keys.tmdb_key
TVDB_KEY = keys.tvdb_key
FANART_KEY = keys.fanart_key
ORION_KEY = keys.orion_key
YT_KEY = keys.yt_key

PLUGIN_ID = 'plugin.video.midnight'
CREW_LOG_FILENAME = 'the_crew.log'

_REMOTE_VFS_PREFIXES = (
    'smb://', 'nfs://', 'ftp://', 'sftp://', 'dav://', 'davs://', 'zip://',
)


def _is_remote_vfs_path(path: str) -> bool:
    lower = (path or '').lower()
    return any(lower.startswith(prefix) for prefix in _REMOTE_VFS_PREFIXES)


_RESOLVED_CREW_LOG_PATH = None
_PRIMARY_CREW_LOG_PATH = None


def _log_write_prefers_vfs() -> bool:
    """macOS/Android Kodi sandboxes Python open(); write via xbmcvfs instead."""
    try:
        kodi_platform = (xbmc.getInfoLabel('System.Platform.OS') or '').lower()
    except Exception:
        kodi_platform = ''
    if kodi_platform in ('android', 'ios', 'osx', 'darwin'):
        return True
    return platform.system().lower() in ('darwin', 'android')


def _crew_log_path_candidates():
    candidates = []
    seen = set()
    for special in (
        'special://logpath',
        'special://profile',
        f'special://profile/addon_data/{PLUGIN_ID}',
    ):
        try:
            base = xbmcvfs.translatePath(special)
        except Exception:
            base = ''
        if not base:
            continue
        base = base.rstrip('\\/')
        path = os.path.join(base, CREW_LOG_FILENAME)
        if path not in seen:
            seen.add(path)
            candidates.append(path)
    return candidates


def _probe_log_path_writable(path: str) -> bool:
    probe = f'{path}.writeprobe'
    try:
        handle = xbmcvfs.File(probe, 'w')
        if not handle:
            return False
        handle.write('1')
        handle.close()
        try:
            xbmcvfs.delete(probe)
        except Exception:
            pass
        return True
    except Exception:
        try:
            if xbmcvfs.exists(probe):
                xbmcvfs.delete(probe)
        except Exception:
            pass
        return False


def primary_crew_log_path() -> str:
    global _PRIMARY_CREW_LOG_PATH
    if not _PRIMARY_CREW_LOG_PATH:
        candidates = _crew_log_path_candidates()
        _PRIMARY_CREW_LOG_PATH = candidates[0] if candidates else ''
    return _PRIMARY_CREW_LOG_PATH


def resolve_crew_log_path(force_refresh=False) -> str:
    """Return a writable the_crew.log path (logpath, then profile, then addon_data)."""
    global _RESOLVED_CREW_LOG_PATH
    if _RESOLVED_CREW_LOG_PATH and not force_refresh:
        return _RESOLVED_CREW_LOG_PATH

    candidates = _crew_log_path_candidates()
    global _PRIMARY_CREW_LOG_PATH
    _PRIMARY_CREW_LOG_PATH = candidates[0] if candidates else ''

    for path in candidates:
        if _probe_log_path_writable(path):
            _RESOLVED_CREW_LOG_PATH = path
            return path

    fallback = _PRIMARY_CREW_LOG_PATH or os.path.join(
        xbmcvfs.translatePath('special://logpath'), CREW_LOG_FILENAME
    )
    _RESOLVED_CREW_LOG_PATH = fallback
    return fallback


def crew_file_write(path, content, mode='a') -> None:
    """
    Write to a Midnight log path.

    xbmcvfs.File only supports read (default) and 'w' (overwrite) — there is no
    append mode. Passing 'a' opens read-only and fails on Windows with
    CWin32File::Write. Use native open() on Windows/Linux local paths; on
    macOS/Android use xbmcvfs with read-modify-write append emulation.
    """
    if content is None:
        content = ''

    use_native = (
        not _is_remote_vfs_path(path)
        and not _log_write_prefers_vfs()
    )
    if use_native:
        try:
            py_mode = 'w' if mode == 'w' else 'a'
            with open(path, py_mode, encoding='utf-8') as handle:
                handle.write(content)
            return
        except OSError:
            pass

    payload = content
    if mode == 'a' and xbmcvfs.exists(path):
        read_handle = xbmcvfs.File(path)
        if read_handle:
            try:
                payload = (read_handle.read() or '') + content
            finally:
                read_handle.close()

    write_handle = xbmcvfs.File(path, 'w')
    if not write_handle:
        raise OSError(f'xbmcvfs.File could not open for write: {path}')
    try:
        if payload:
            write_handle.write(payload)
    finally:
        write_handle.close()

# Color Identifier String IDs (localized strings for color selection dropdowns)
# Used by: unaired.identify, prem.identify, torrent.identify settings
# Maps to strings: Blue, Red, Yellow, Deep Pink, Cyan, Lawn Green, Gold, Magenta, Yellow Green, No Color
COLOR_STRING_IDS = [32753, 32754, 32755, 32756, 32757, 32758, 32759, 32760, 32761, 32762]

# Color names for direct color tag generation (alternative to localized strings)
COLOR_NAMES = ['blue', 'red', 'yellow', 'deeppink', 'cyan', 'lawngreen', 'gold', 'magenta', 'yellowgreen', 'nocolor']

# Kodi API function references (shared across all instances)
transpath = xbmcvfs.translatePath
lang = xbmcaddon.Addon('plugin.video.midnight').getLocalizedString
listItem = xbmcgui.ListItem
addon = xbmcaddon.Addon
addonInfo = xbmcaddon.Addon().getAddonInfo

# ========================================================================cm=
# DECORATOR FUNCTIONS
# ========================================================================cm=

def log_api_call(func):
    """
    Decorator to log API calls with timing information.
    Logs the function name, endpoint, and elapsed time.

    Usage:
        @log_api_call
        def getTraktAsJson(url, ...):
            # existing code
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        start = time.time()
        endpoint = args[0] if args else kwargs.get('url', 'unknown')

        # Get the global CrewRuntime instance for logging
        # Note: This will be available after module initialization
        try:
            from . import crewruntime
            crewruntime.c.log(f"[API] Calling {func.__name__}: {endpoint}")
        except (ImportError, AttributeError, Exception):
            pass  # Logging may fail during module initialization

        result = func(*args, **kwargs)

        elapsed = time.time() - start
        try:
            from . import crewruntime
            crewruntime.c.log(f"[API] {func.__name__} completed in {elapsed:.2f}s")
        except (ImportError, AttributeError, Exception):
            pass  # Logging may fail during module initialization

        return result
    return wrapper


def warn_if_slow(threshold_seconds=1.0):
    """
    Decorator to log performance warnings for slow operations.
    Logs a warning if the function execution exceeds the threshold.

    Usage:
        @warn_if_slow(threshold_seconds=2.0)
        def some_database_function(...):
            # existing code
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start = time.time()
            result = func(*args, **kwargs)
            elapsed = time.time() - start

            if elapsed > threshold_seconds:
                try:
                    from . import crewruntime
                    crewruntime.c.log(f"[PERF WARNING] {func.__name__} took {elapsed:.2f}s (threshold: {threshold_seconds}s)")
                except (ImportError, AttributeError, Exception):
                    pass  # Logging may fail during module initialization

            return result
        return wrapper
    return decorator


def retry_on_failure(max_attempts=3, delay=1.0, exceptions=(Exception,)):
    """
    Decorator to retry a function on failure with exponential backoff.
    Useful for network calls that may fail temporarily.

    Args:
        max_attempts: Maximum number of retry attempts (default: 3)
        delay: Initial delay between retries in seconds (default: 1.0)
        exceptions: Tuple of exception types to catch (default: all exceptions)

    Usage:
        @retry_on_failure(max_attempts=3, delay=1.0)
        def network_call(...):
            # existing code
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            current_delay = delay

            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e

                    if attempt == max_attempts - 1:
                        # Last attempt failed, re-raise the exception
                        try:
                            from . import crewruntime
                            crewruntime.c.log(f"[RETRY FAILED] {func.__name__} failed after {max_attempts} attempts: {e}")
                        except (ImportError, AttributeError, Exception):
                            pass  # Logging may fail during module initialization
                        raise

                    # Log retry attempt
                    try:
                        from . import crewruntime
                        crewruntime.c.log(f"[RETRY] {func.__name__} failed (attempt {attempt+1}/{max_attempts}): {e}. Retrying in {current_delay}s...")
                    except (ImportError, AttributeError, Exception):
                        pass  # Logging may fail during module initialization

                    time.sleep(current_delay)
                    current_delay *= 2  # Exponential backoff

            # This should never be reached, but just in case
            if last_exception:
                raise last_exception

        return wrapper
    return decorator


# ========================================================================cm=
# INTERNAL HELPER CLASSES
# ========================================================================cm=

class _SettingsManager:
    """
    Internal class to manage addon settings.
    Handles the complex logic of reading/writing settings with fallbacks.
    """

    def __init__(self, runtime):
        self.runtime = runtime
        self._cache = {}
        self._disk_mtime = 0.0
        self._disk_values = {}
        self._disk_untrusted = None

    def _disk_settings_path(self) -> str:
        try:
            profile = xbmcvfs.translatePath(
                xbmcaddon.Addon('plugin.video.midnight').getAddonInfo('profile')
            )
            if profile:
                return os.path.join(profile, 'settings.xml')
        except Exception:
            pass
        return ''

    def _kodi_settings_untrusted(self) -> bool:
        """Formerly treated missing <?xml as untrusted; Omega userdata has no declaration."""
        return False

    def _load_disk_settings(self) -> dict:
        path = self._disk_settings_path()
        if not path:
            return {}
        try:
            from .settings_repair import parse_userdata_settings, _file_exists
            if not _file_exists(path):
                return {}
        except Exception:
            if not os.path.isfile(path):
                return {}
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            try:
                import xbmcvfs
                st = xbmcvfs.Stat(path)
                mtime = st.st_mtime() if st else 0
            except Exception:
                mtime = 0
        if mtime == self._disk_mtime and self._disk_values:
            return self._disk_values
        try:
            from .settings_repair import parse_userdata_settings
            self._disk_values = parse_userdata_settings(path)
            self._disk_mtime = mtime
        except Exception:
            self._disk_values = {}
        return self._disk_values

    def invalidate(self) -> None:
        """Drop programmatic + disk caches (call after Kodi settings UI save)."""
        self._cache.clear()
        self._disk_mtime = 0.0
        self._disk_values = {}

    def get(self, setting: str) -> str:
        """Get a setting value with caching and fallback logic."""
        try:
            # settings.xml mtime changed (Kodi UI save / other process) → drop
            # programmatic cache. Long-lived service Addon() can lag behind disk.
            path = self._disk_settings_path()
            if path:
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    mtime = 0.0
                if self._disk_mtime and mtime and mtime != self._disk_mtime:
                    self._cache.clear()

            # Check programmatic cache first (our own set_setting writes)
            if setting in self._cache:
                return self._cache.get(setting, '')

            val = ''
            # Prefer a fresh Addon() in the service process — held instances
            # often keep pre-flip values for tracker.service after UI save.
            try:
                val = xbmcaddon.Addon(PLUGIN_ID).getSetting(id=setting)
            except Exception:
                try:
                    val = xbmcaddon.Addon(PLUGIN_ID).getSetting(setting)
                except Exception:
                    val = ''

            if val == '':
                addon_inst = self._get_addon_instance()
                if addon_inst and hasattr(addon_inst, 'getSetting'):
                    try:
                        val = addon_inst.getSetting(id=setting)
                    except Exception:
                        try:
                            val = addon_inst.getSetting(setting)
                        except Exception:
                            val = ''

            disk_val = self._load_disk_settings().get(setting)
            if disk_val is not None and disk_val != '':
                # Disk is authoritative when Addon lag disagrees (XOR tracker flips).
                if not val or disk_val != val:
                    return disk_val
            return val if val is not None else ''
        except Exception:
            return ''

    def set(self, setting: str, val: str) -> None:
        """Set a setting value and update cache."""
        try:
            # Update cache
            self._cache[setting] = val
            self._disk_mtime = 0.0

            # Try to get addon instance
            addon_inst = self._get_addon_instance()

            if addon_inst and hasattr(addon_inst, 'setSetting'):
                try:
                    addon_inst.setSetting(id=setting, value=val)
                    return
                except Exception:
                    pass

            # Fallback to creating fresh instance
            try:
                xbmcaddon.Addon(PLUGIN_ID).setSetting(id=setting, value=val)
            except Exception:
                pass
        except Exception:
            pass

    def _get_addon_instance(self):
        """Get the plugin addon instance (all user settings live in plugin.video.midnight)."""
        try:
            raw_addon = getattr(self.runtime, 'addon', None)
            if raw_addon and hasattr(raw_addon, 'getSetting'):
                return raw_addon
            return xbmcaddon.Addon(PLUGIN_ID)
        except Exception:
            try:
                return xbmcaddon.Addon(PLUGIN_ID)
            except Exception:
                return None


class _EncodingHelper:
    """
    Internal class to handle text encoding/decoding operations.
    Consolidates multiple similar methods into a clean interface.
    """

    @staticmethod
    def to_str(data, encoding='utf-8', errors='replace') -> str:
        """
        Convert bytes/str/other to str.
        Main method for safe string conversion.
        """
        if isinstance(data, str):
            return data
        if isinstance(data, bytes):
            return data.decode(encoding, errors)
        return str(data)

    @staticmethod
    def to_bytes(s, encoding='utf-8', errors='replace') -> bytes:
        """Convert str/bytes to bytes. Matches six_encode behavior."""
        if isinstance(s, bytes):
            return s
        if isinstance(s, str):
            return s.encode(encoding, errors)
        return str(s).encode(encoding, errors)  # Fallback for other types

    @staticmethod
    def safe_decode(data, encoding='utf-8', errors='replace') -> str:
        """
        Safely decode data with error handling.
        Logs errors if they occur.
        """
        try:
            return _EncodingHelper.to_str(data, encoding, errors)
        except Exception as e:
            # If logging is needed, caller should handle
            return str(data)


class _PathManager:
    """
    Internal class to manage addon artwork and resource paths.
    Consolidates all addon_* methods for cleaner organization.
    """

    def __init__(self, runtime):
        self.runtime = runtime

    def _get_art_path(self, filename: str, fallback: str = '') -> str:
        """Helper to build artwork path with fallback."""
        base = self.runtime.get_art_path()
        if base:
            return os.path.join(base, filename)
        return fallback

    def icon(self) -> str:
        """Get addon icon path."""
        fallback = xbmcaddon.Addon().getAddonInfo('icon')
        return self._get_art_path('icon.png', fallback)

    def thumb(self) -> str:
        """Get addon thumb path."""
        return self._get_art_path('thumb.jpg', '')

    def poster(self) -> str:
        """Get addon poster path."""
        return self._get_art_path('poster.png', 'DefaultVideo.png')

    def banner(self) -> str:
        """Get addon banner path."""
        return self._get_art_path('banner.png', 'DefaultVideo.png')

    def fanart(self) -> str:
        """Get addon fanart path."""
        path = self._get_art_path('fanart.jpg', '')
        if not path:
            return xbmcaddon.Addon().getAddonInfo('fanart')
        # Handle tuple case (legacy)
        if isinstance(path, tuple):
            return path[0]
        return path

    def clearart(self) -> str:
        """Get addon clearart path."""
        return self._get_art_path('clearart.png', '')

    def discart(self) -> str:
        """Get addon discart path."""
        return self._get_art_path('discart.png', '')

    def clearlogo(self) -> str:
        """Get addon clearlogo path."""
        return self._get_art_path('clearlogo.png', '')

    def next_icon(self) -> str:
        """Get next icon path."""
        return self._get_art_path('next.png', 'DefaultVideo.png')

    def adult_icon(self) -> str:
        """Get adult icon path."""
        return self._get_art_path('adult.png', 'DefaultVideo.png')


class _LogManager:
    """Internal class to manage logging operations."""

    def __init__(self, runtime):
        self.runtime = runtime
        self._log_path_announced = False

    @staticmethod
    def _is_true_setting(value) -> bool:
        return str(value).lower() == 'true'

    def _crew_log_path(self) -> str:
        return resolve_crew_log_path()

    def _vfs_write(self, path, content, mode='a') -> None:
        crew_file_write(path, content, mode=mode)

    def _ensure_crew_log_header(self, log_file: str) -> None:
        if xbmcvfs.exists(log_file):
            return

        x = "=" * 68 + "cm=\n"
        system_info = self.runtime.get_anonymized_system_info()
        header = ''.join([
            x,
            "Midnight Add-on - Debug Log\n",
            "Created: " + datetime.datetime.now().strftime("%d-%m-%Y %H:%M:%S") + "\n",
            x,
            "\n",
            "System Information (anonymized for log uploads):\n",
            "-" * 68 + "\n",
            system_info + "\n",
            "-" * 68 + "\n",
            "\n",
            x,
            "\n",
        ])
        self._vfs_write(log_file, header, 'w')

    def _announce_log_path_once(self, log_file: str) -> None:
        if self._log_path_announced:
            return
        self._log_path_announced = True
        xbmc.log(f'[Midnight] Debug log file: {log_file}', xbmc.LOGINFO)
        primary = primary_crew_log_path()
        if primary and log_file != primary:
            xbmc.log(
                f'[Midnight] Primary log path not writable ({primary}); using fallback.',
                xbmc.LOGWARNING,
            )

    def log(self, msg, trace=0) -> None:
        '''
        General new log messages

        Args:
            msg (str): The message to log
            trace (int): If 1, includes caller information from stack
        '''
        # Early exit if logging disabled - avoid string processing
        if not self._is_true_setting(self.runtime.get_setting('addon_debug')):
            return

        debug_prefix = f' DEBUG [{self.runtime.name} {self.runtime.pluginversion} | {self.runtime.moduleversion} | {self.runtime.pyversion} | {self.runtime.kodiversion} | {self.runtime.platform}]'
        info_prefix = f' INFO [{self.runtime.name} {self.runtime.pluginversion}/{self.runtime.moduleversion} | {self.runtime.pyversion}]'

        log_file = self._crew_log_path()
        debug_log = str(self.runtime.get_setting('debug.location'))

        try:
            if not isinstance(msg, str):
                raise TypeError('c.log() msg not of type str!')

            if trace == 1:
                caller = getframeinfo(stack()[1][0])

                head = debug_prefix
                _msg = f'\n     {msg}:\n    \n--> called from file {caller.filename} @ {caller.lineno}'
            else:
                head = info_prefix
                _msg = f'\n    {msg}'

            line = f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} {head}: {msg}'

            if debug_log == '1':
                self._announce_log_path_once(log_file)
                self._ensure_crew_log_header(log_file)
                self._vfs_write(log_file, line.rstrip('\r\n') + '\n', 'a')
            elif debug_log == '0':
                xbmc.log(f'[Midnight] {line}', xbmc.LOGINFO)

        except (TypeError, OSError, UnicodeError) as exc:
            xbmc.log(
                f'[ {self.runtime.name} ] Logging Failure ({log_file}): {exc}',
                xbmc.LOGERROR
            )
            if debug_log == '1':
                try:
                    xbmc.log(f'[Midnight] {line}', xbmc.LOGINFO)
                except Exception:
                    pass

    def scraper_error(self, msg, scraper=None, trace=0, exc_info=None):
        """
        Logs an error message associated with a specific scraper.

        Improvements:
        - Auto-detects scraper name from filename if not provided
        - Accepts Exception objects and automatically captures full traceback
        - Better formatting for readability

        Args:
            msg (str or Exception): The error message or exception object to log
            scraper (str, optional): The name of the scraper. Auto-detected if None
            trace (int, optional): If set to 1, includes caller info. Defaults to 0
            exc_info (Exception, optional): Exception object for full traceback capture

        Examples:
            c.scraper_error("Custom error message", "my_scraper")
            c.scraper_error(exception_object)  # Auto-detects scraper name
            c.scraper_error("Error context", exc_info=e)  # Combines message + traceback
        """
        # Early exit if logging disabled
        if not self._is_true_setting(self.runtime.get_setting('addon_debug')):
            return

        # Handle exception objects passed as msg
        if isinstance(msg, Exception):
            exc_info = msg
            msg = str(msg)

        # Auto-detect scraper name from call stack if not provided
        if scraper is None:
            try:
                caller_frame = stack()[1]
                caller_file = caller_frame[0].f_code.co_filename
                # Extract scraper name from filename (e.g., "/path/to/filmxy.py" -> "filmxy")
                scraper = os.path.splitext(os.path.basename(caller_file))[0]
            except (Exception):
                scraper = "unknown"  # Fallback if frame inspection fails

        # Build error message with traceback if exception available
        error_lines = [
            '\n' + '='*70,
            f'SCRAPER ERROR: {scraper}',
            '='*70
        ]

        if exc_info:
            # Capture full traceback
            import sys
            tb_lines = traceback.format_exception(type(exc_info), exc_info, exc_info.__traceback__)
            error_lines.append(f'Exception Type: {type(exc_info).__name__}')
            error_lines.append(f'Exception Message: {msg}')
            error_lines.append('\nFull Traceback:')
            error_lines.extend(['  ' + line.rstrip() for line in tb_lines])
        else:
            error_lines.append(f'Error: {msg}')

        error_lines.append('='*70)

        formatted_msg = '\n'.join(error_lines)
        self.log(formatted_msg, trace)


class _DialogManager:
    """Internal class to manage Kodi dialog operations."""

    def __init__(self):
        self._dialog = xbmcgui.Dialog()

    def select(self, heading, list, autoclose=0):
        """Display a selection dialog with list of items."""
        return self._dialog.select(heading, list, autoclose)

    def ok(self, message, heading='Info'):
        """Display a simple OK dialog with the given message and optional heading."""
        return self._dialog.ok(heading, message)

    def yesno(self, message, heading, nolabel='', yeslabel=''):
        """Display a Yes/No dialog with the given message and optional heading."""
        return self._dialog.yesno(heading, message, nolabel, yeslabel)

    def notification(self, heading, message, icon, time=3000, sound=False):
        """Display a notification popup."""
        return self._dialog.notification(heading, message, icon, time, sound=sound)


# ========================================================================cm=


class CrewRuntime:
    '''
    Global new superclass starting to run alongside the old code

    '''

    # Class constants - TMDB image size configurations
    IMAGE_SIZES = (
        {'poster': 'w185', 'fanart': 'w300', 'still': 'w300', 'profile': 'w185'},
        {'poster': 'w342', 'fanart': 'w780', 'still': 'w500', 'profile': 'w342'},
        {'poster': 'w780', 'fanart': 'w1280', 'still': 'w780', 'profile': 'h632'},
        {'poster': 'original', 'fanart': 'original', 'still': 'original', 'profile': 'original'}
    )

    # Class variables that reference module-level functions (for backward compatibility with c.lang() calls)
    transpath = transpath
    lang = lang
    listItem = listItem
    addonInfo = addonInfo

    def __init__(self):
        pass

        '''
        # cm - can later be used on a child class as temp obj to super crewRuntime
        # super().__init__(self)
        '''

        # Initialize internal managers first (needed for initialize_all)
        self._settings = _SettingsManager(self)
        self._art_paths = _PathManager(self)
        self._logger = _LogManager(self)
        self._dialogs = _DialogManager()

        self.name = None
        self.platform = None
        self.kodiversion = None
        self.int_kodiversion = None

        self.moduleversion = None
        self.pluginversion = None
        self.addon = None
        self.toggle = None
        self.has_silent_boot = None

        self.artPoster = None
        self.artThumb = None
        self.artIcon = None
        self.artFanart = None
        self.artClearlogo = None
        self.artClearart = None
        self.artDiscart = None

        self.tmdb_postersize = ''
        self.tmdb_fanartsize = ''
        self.tmdb_stillsize = ''
        self.tmdb_profilesize = ''
        self.devmode = False

        self._theme = ''
        self.art = ''

        self.cores = 0
        self._orion_installed = None  # Cache for expensive Orion check

        self.initialize_all()

    def __del__(self):
        '''
        On destruction of the class
        '''
        self.deinit()

    def deinit(self):
        '''
        cleanup
        '''
        self.toggle = None
        self.addon = None

    def initialize_all(self):
        '''
        initialize all vars
        '''
        # Cache a single Addon instance to ensure settings persist across calls
        try:
            self.addon = xbmcaddon.Addon(PLUGIN_ID)
            self.plugin_id = self.addon.getAddonInfo("id")
            self.pluginversion = self.addon.getAddonInfo("version")
            self.name = self.addon.getAddonInfo('name')
        except Exception:
            # Fallback (prior behavior) - keep the class reference if instance creation fails
            self.addon = xbmcaddon.Addon
            self.plugin_id = PLUGIN_ID
            self.pluginversion = self.addon(PLUGIN_ID).getAddonInfo("version")
            self.name = self.addon(PLUGIN_ID).getAddonInfo('name')

        self.name = self.strip_tags(text=self.name).title()
        self.platform = self._get_current_platform()

        # Note: Settings cache now managed by _SettingsManager

        self.module_addon = xbmcaddon.Addon("script.module.midnight")
        self.module_id = self.module_addon.getAddonInfo(id="id")
        self.moduleversion = self.module_addon.getAddonInfo(id="version")

        self.kodiversion = self._get_kodi_version(as_string=True, as_full=True)
        self.int_kodiversion = self._get_kodi_version(as_string=False, as_full=False)
        self.pyversion = self._get_python_version(as_string=True)
        self.int_pyversion = self._get_python_version(as_string=False)
        self.has_silent_boot = self._has_silent_boot()
        self.artwork_path = self.get_artwork_path()
        self.cores = os.cpu_count() or 1

        self.devmode = self.get_setting('dev_pw') == self.to_str(base64.b64decode(b'dGhlY3Jldw=='))
        self.is_orion_disabled = self.orion_disabled()


        self._theme = self.appearance()
        self.art = self.get_art_path()

        # Initialize paths
        self.datapath = self._compute_datapath()
        self._paths = self._init_paths()

        self.toggle = 1 # cm - internal debugging

        self.set_imagesizes()
        self.check_orion()

    def _compute_datapath(self):
        """Compute addon profile data path."""
        return xbmcvfs.translatePath(self.addonInfo('profile'))

    def _init_paths(self):
        """Initialize all file paths using a dict pattern."""
        return {
            'settings': os.path.join(self.datapath, 'settings.xml'),
            'views': os.path.join(self.datapath, 'views.db'),
            'bookmarks': os.path.join(self.datapath, 'bookmarks.db'),
            'providercache': os.path.join(self.datapath, 'providers.13.db'),
            'metacache': os.path.join(self.datapath, 'meta.5.db'),
            'search': os.path.join(self.datapath, 'search.1.db'),
            'libcache': os.path.join(self.datapath, 'library.db'),
            'cache': os.path.join(self.datapath, 'cache.db'),
            'debridcache': os.path.join(self.datapath, 'debridcache.db'),
            'dbsettings': os.path.join(self.datapath, 'settings.db'),
            'traktsync': os.path.join(self.datapath, 'traktsync.db'),
            'meta': os.path.join(self.datapath, 'meta.db'),
            'tvseasonstatus': os.path.join(self.datapath, 'tv_season_status.db'),
        }

    def get_path(self, path_key):
        """Get a file path by key. Cached in dict for performance."""
        return self._paths.get(path_key, '')

    def kodi_version(self, as_string=False, as_full=False):
        """Get Kodi version - public method wrapper."""
        return self._get_kodi_version(as_string, as_full)

    def get_max_threads(self,total_items: int, default_cap: int = 50) -> int:
        """Determine an appropriate number of threads for concurrent tasks."""
        try:
            user_defined = int(self.get_setting('max.threads') or 0)
        except (ValueError, TypeError):
            user_defined = 0

        cpu_cores = os.cpu_count() or 1
        thread_count = user_defined if user_defined > 0 else cpu_cores * 2
        thread_count = min(thread_count, default_cap, total_items)
        return max(5, thread_count)

    def orion_disabled(self) -> bool:
        '''Check if Orion is disabled'''

        if(self.get_setting('disable.orion') == 'true'):
            return True
        return False
    @staticmethod
    def addon_exists(script_name) -> bool:
        """
        Check if an add-on with the given script name is installed and enabled.

        Args:
            script_name (str): The name of the script or add-on to check.

        Returns:
            bool: True if the add-on is installed and enabled, False otherwise.
        """

        if not script_name:
            return False
        return xbmc.getCondVisibility(f'System.HasAddon({script_name})') == 1

    def _has_silent_boot(self) -> bool:
        return self.get_setting('silent.boot') == 'true'

    def log_boot_option(self) -> None:
        if self.has_silent_boot:
            self.log('User enabled silent boot option')
        else:
            self.log('User disabled silent boot option')

    @staticmethod
    def addon_path() -> str:
        """Returns the path to the current add-on's resources directory"""
        return xbmcaddon.Addon().getAddonInfo('path')

    def addon_path_for(self, addon_id) -> str:
        """Returns the path to a specific add-on's resources directory"""
        try:
            return xbmcaddon.Addon(addon_id).getAddonInfo('path')
        except Exception as e:
            self.log(f'Error getting path for add-on {addon_id}: {e}', 1)
            return ''

    @staticmethod
    def get_artwork_path() -> str:
        """Returns the path to the script.midnight.artwork addon's resources directory"""
        return xbmcaddon.Addon('script.midnight.artwork').getAddonInfo('path')

    def resolve_dialog_skin(self, xml_filename, preferred=None):
        """Return (addon_path, skin) for a dialog XML, with fallback when the active skin lacks the file."""
        addon_path = self.get_artwork_path()
        skin = (preferred or self.appearance() or 'midnight').lower()
        if skin in ('-', ''):
            skin = 'midnight'
        fallbacks = [skin]
        for fb in ('midnight', 'modern', 'stylish'):
            if fb not in fallbacks:
                fallbacks.append(fb)
        for candidate in fallbacks:
            path = os.path.join(addon_path, 'resources', 'skins', candidate, '1080i', xml_filename)
            if os.path.isfile(path):
                return addon_path, candidate
        return addon_path, 'modern'

    def _get_current_platform(self):
        # Xbox UWP: platform.uname() -> win32_ver -> subprocess -> CreateProcess
        # (missing) and kills service/plugin open. Use uwp_compat instead.
        try:
            from .uwp_compat import platform_label
            return platform_label()
        except Exception:
            bits = '64bits' if sys.maxsize > 2**32 else '32bits'
            try:
                info = (xbmc.getInfoLabel('System.OSVersionInfo') or '').strip()
                if info:
                    return f'{info} ({bits})'
            except Exception:
                pass
            return f'Unknown ({bits})'

    def get_anonymized_system_info(self):
        """
        Generate anonymized system information for log file headers.

        Returns system configuration details useful for debugging uploaded logs,
        while excluding any personally identifiable information (no username,
        hostname, IP address, or MAC address).

        Returns:
            str: Formatted multi-line string with anonymized system info

        Example output:
            OS: Windows 11 v.10.0.26300 (64bits)
            Kodi: v.21.3 (Omega)
            Python: v.3.8.15
            CPU Cores: 8
            Addon: plugin.video.midnight v.2.2.9-alpha
            Module: script.module.midnight v.2.3.10-alpha
        """
        try:
            # Get platform info (already anonymized - no username/hostname)
            platform_info = self._get_current_platform()

            # Get Kodi version info
            kodi_version = self.kodiversion if hasattr(self, 'kodiversion') else self._get_kodi_version(as_string=True, as_full=True)

            # Get Kodi codename if available
            kodi_codename = xbmc.getInfoLabel("System.BuildVersionName")
            kodi_display = f"v.{kodi_version}"
            if kodi_codename and kodi_codename.strip():
                kodi_display += f" ({kodi_codename})"

            # Get Python version
            python_version = self.pyversion if hasattr(self, 'pyversion') else self._get_python_version(as_string=True)

            # Get CPU count (useful for debugging thread pool issues)
            cpu_cores = self.cores if hasattr(self, 'cores') else (os.cpu_count() or 1)

            # Get addon versions
            plugin_version = self.pluginversion if hasattr(self, 'pluginversion') else "unknown"
            module_version = self.moduleversion if hasattr(self, 'moduleversion') else "unknown"

            # Format the output
            lines = [
                f"OS: {platform_info}",
                f"Kodi: {kodi_display}",
                f"Python: v.{python_version}",
                f"CPU Cores: {cpu_cores}",
                f"Addon: plugin.video.midnight v.{plugin_version}",
                f"Module: script.module.midnight v.{module_version}",
            ]

            return "\n".join(lines)

        except Exception as e:
            # Fallback in case of any errors
            return f"System info unavailable: {str(e)}"

    def _get_kodi_version(self, as_string=False, as_full=False):
        version_raw = xbmc.getInfoLabel("System.BuildVersion").split(" ")

        v_temp = version_raw[0]

        if as_full is False:
            version = v_temp.split(".")[0]
            fversion = ''
        else:
            v_major = v_temp.split(".")[0]
            v_minor = v_temp.split(".")[1]
            fversion = f"{v_major}.{v_minor}"
            version = ''

        if as_string is True:
            return version if as_full is False else fversion

        return int(version)

    def _get_python_version(self, as_string=False, as_full=False):
        """
        Get the python version

        :param as_string: bool Return the python version as a string
        :param as_full: bool Return the full python version string
        :return: str or int
        """
        version = platform.python_version_tuple()

        if as_string and as_full:
            return sys.version

        return '.'.join(version[:3]) if as_string else float('.'.join(version[:2]))

    def log(self, msg, trace=0) -> None:
        """Delegate to _LogManager for all logging operations."""
        return self._logger.log(msg, trace)

    def scraper_error(self, msg, scraper=None, trace=0, exc_info=None):
        """Delegate to _LogManager for scraper error logging."""
        return self._logger.scraper_error(msg, scraper, trace, exc_info)

    def in_addon(self) -> bool:
        '''
        returns bool if we are inside addon
        '''
        return xbmc.getInfoLabel('Container.PluginName') == "plugin.video.midnight"

    def get_setting(self, setting) -> str:
        '''
        Return a setting value.
        Delegates to _SettingsManager for clean logic with caching.
        '''
        return self._settings.get(setting)

    def invalidate_settings_cache(self) -> None:
        """Clear settings caches after Kodi saves userdata (service must see flips)."""
        try:
            self._settings.invalidate()
        except Exception:
            pass
        # Force next Addon access to use a fresh instance
        try:
            self.addon = xbmcaddon.Addon(PLUGIN_ID)
        except Exception:
            pass

    def set_setting(self, setting, val) -> None:
        '''
        Set a setting value.
        Delegates to _SettingsManager for clean logic with caching.
        .getSettingString
        .getSettingBool
        .getSettingNumber
        .setSettingInt
        .setSettingBool
        '''
        self._settings.set(setting, val)

    def strip_tags(self, text) -> str:
        '''
        Strip the tags, added to the name in the addon.xml file
        '''

        clean = re.compile(r'\[.*?\]')
        return re.sub(clean, '', text)




    def check_orion(self):
        #check if Orion is installed
        if self.is_orion_installed():
            try:
                OrionClass = getattr(self, 'Orion', None)
                if OrionClass is None:
                    import importlib
                    module = importlib.import_module('orion')
                    OrionClass = getattr(module, 'Orion', None)
                    self.Orion = OrionClass
                if OrionClass is None:
                    raise ImportError('Orion class not found in orion module')

                result = OrionClass(keys.orion_key).user()
                self.set_setting('orion.installed', f'{color("Installed", 'accent')}')
                self.set_setting('orion.boolinstalled', 'true')

                if result.get('username') is not None:
                    temp = result.get("username")
                else:
                    temp = self.obscure_email(result.get('email'))
                self.set_setting('orion.username', temp)
                package = result.get('subscription').get('package').get('name')
                self.set_setting('orion.package', package)
                expiration = result.get('subscription').get('time').get('expiration')
                exp = datetime.datetime.fromtimestamp(expiration).strftime('%A %d %b, %Y')
                self.set_setting('orion.expiration', str(exp))

            except Exception as e:
                self.log(f'Error checking Orion installation: {e}', 1)
                self.set_setting('orion.installed', f'{color("Not Installed!", 'error')}')
                self.set_setting('orion.boolinstalled', 'false')


    def is_orion_installed(self):# -> Any:
        """
        Safely check if Orion add-on is installed and import its Orion class if available.
        This avoids a bare module import at parse time and handles missing or broken modules.
        Cached to avoid repeated expensive system calls.
        """
        # Return cached result if available
        if self._orion_installed is not None:
            return self._orion_installed

        # Perform expensive check and cache result
        if not xbmc.getCondVisibility('System.HasAddon(script.module.orion)'):
            self._orion_installed = False
            return False

        # Ensure script.module.orion/lib is on sys.path before importing
        try:
            import xbmcvfs as _xbmcvfs
            _orion_lib_path = _xbmcvfs.translatePath('special://home/addons/script.module.orion/lib')
            if os.path.isdir(_orion_lib_path) and _orion_lib_path not in sys.path:
                sys.path.insert(0, _orion_lib_path)
        except Exception:
            pass

        try:
            module = importlib.import_module('orion')
            OrionClass = getattr(module, 'Orion', None)
            if OrionClass is None:
                self.log('Orion module found but Orion class missing', 1)
                self._orion_installed = False
                return False
            # attach the class to the instance so other methods can access it
            self.Orion = OrionClass
            self._orion_installed = True
            return True
        except Exception as e:
            self.log(f'Failed to import Orion module: {e}', 1)
            self.set_setting('orion.installed', f'{color("Not Installed!", 'error')}')
            self.set_setting('orion.boolinstalled', 'false')
            self._orion_installed = False
            return False


    def obscure_email(self, email):
        return email[:2] + '*' * (len(email) - 4) + email[-2:]


    #######
    # cm - replacing the six standard functions with own code


    @staticmethod
    def encode(s, encoding='utf-8') -> bytes:
        """
        Encodes a string to bytes using the specified encoding.
        Delegates to _EncodingHelper.to_bytes() for consistent behavior.
        Legacy method kept for backwards compatibility.

        Parameters:
            s (str): The string to be encoded. It can be a str (Python 3) or unicode (Python 2).
            encoding (str): The encoding type. Default is 'utf-8'.

        Returns:
            bytes: The encoded byte string.
        """
        return _EncodingHelper.to_bytes(s, encoding, errors='strict')




    def to_unicode(self, data, encoding: str = 'utf-8', errors: str = 'replace') -> str:
        """
        Safely return a unicode (str) from bytes/str/other.
        Delegates to _EncodingHelper with error logging.
        - If already str -> return unchanged.
        - If bytes -> decode using encoding/errors.
        - Otherwise -> convert via str() (safe fallback).
        Logs and re-raises decoding errors.
        """
        try:
            return _EncodingHelper.to_str(data, encoding, errors)
        except Exception as e:
            failure = traceback.format_exc()

            # UnicodeDecodeError requires (encoding, object, start, end, reason)
            # use a safe empty bytes object with start=end=0 and include the original error message
            raise UnicodeDecodeError(encoding, b'', 0, 0, f"Decoding failed: {e}") from e

    def decode(self, data, encoding: str = 'utf-8', errors: str = 'replace') -> str:
        """Alias for to_unicode() for backwards compatibility."""
        return self.to_unicode(data, encoding, errors)

    def to_str(self, data, encoding: str = 'utf-8', errors: str = 'replace') -> str:
        """
        Convert bytes/str/other to str. Clean modern API.
        Replaces: ensure_text, ensure_str, decode_text, six_decode.
        Delegates to _EncodingHelper for consistent behavior.
        """
        return _EncodingHelper.to_str(data, encoding, errors)

    def to_bytes(self, data, encoding: str = 'utf-8', errors: str = 'replace') -> bytes:
        """
        Convert str to bytes. Clean modern API.
        Replaces: encode_text, six_encode.
        Delegates to _EncodingHelper for consistent behavior.
        """
        return _EncodingHelper.to_bytes(data, encoding, errors)




    def set_imagesizes(self) -> None:
        '''
        Return the correct image sizes according to settings
        '''
        # Be tolerant of missing or invalid settings during test collection/imports
        try:
            q = self.get_setting('fanart.quality')
            idx = int(q) if q not in (None, '') else 2
        except (ValueError, TypeError):
            idx = 2

        # IMAGE_SIZES is a tuple of dicts; index it safely with bounds checking
        if isinstance(idx, int) and 0 <= idx < len(self.IMAGE_SIZES):
            resolutions = self.IMAGE_SIZES[idx]
        else:
            resolutions = self.IMAGE_SIZES[2]
        self.tmdb_postersize = resolutions.get('poster')
        self.tmdb_fanartsize = resolutions.get('fanart')
        self.tmdb_stillsize = resolutions.get('still')
        self.tmdb_profilesize = resolutions.get('profile')
        old_imagesetting = self._read_fanart_quality_stamp()
        cur_imagesetting = self.get_setting('fanart.quality')
        if old_imagesetting != cur_imagesetting:
            self.clear_imagecaches()

    def _fanart_quality_stamp_path(self) -> str:
        return os.path.join(self.datapath, 'fanart_quality.stamp')

    def _read_fanart_quality_stamp(self) -> str:
        path = self._fanart_quality_stamp_path()
        try:
            if os.path.isfile(path):
                with open(path, 'r', encoding='utf-8') as fh:
                    return fh.read().strip()
        except OSError:
            pass
        # One-time read from retired Kodi setting (removed from schema).
        legacy = self.get_setting('fanart.quality.old')
        return str(legacy) if legacy not in (None, '') else ''

    def _write_fanart_quality_stamp(self, value) -> None:
        path = self._fanart_quality_stamp_path()
        try:
            os.makedirs(self.datapath, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(str(value))
        except OSError:
            pass

    def clear_imagecaches(self) -> None:
        '''
        Clear the image cache
        '''
        self._write_fanart_quality_stamp(self.get_setting('fanart.quality'))


    def now(self):
        '''
        Return the current time
        '''
        return datetime.datetime.now()



    def is_widget_listing(self) -> bool:
        """Check if the current Kodi listing is a home-screen widget (not in-addon browse)."""
        try:
            if len(sys.argv) > 2:
                argv_query = sys.argv[2].lower()
                if 'widget=true' in argv_query or 'reload=' in argv_query:
                    return True
        except Exception:
            pass
        try:
            if xbmcgui.Window(10000).getProperty('crew.widget.context') == '1':
                return True
        except Exception:
            pass
        # Shield / Android: Container.PluginName is often empty on Next Page while
        # sys.argv still points at this plugin. Treating that as a widget forced
        # page=1 and broke discover pagination. Trust plugin:// argv first.
        try:
            argv0 = (sys.argv[0] or '').lower()
            addon_id = (self.addon_id or '').lower()
            if argv0.startswith('plugin://') and addon_id and addon_id in argv0:
                return False
        except Exception:
            pass
        plugin_name = xbmc.getInfoLabel('Container.PluginName') or ''
        if not plugin_name:
            return True
        return 'plugin' not in plugin_name


    #---Add Directory Method---#
    def addDirectoryItem(self, name, url, mode, icon, fanart, thumb, description='', page='', dir_name='', cm=None, labels=None, cast=None, the_id = '', season = '', episode = '', isAction = True, isFolder = True):
        sys_url = sys.argv[0]
        sys_handle = int(sys.argv[1])

        if isinstance(name, int):
            name = xbmcaddon.Addon().getLocalizedString(name)

        if description == '':
            description = name
        if cast is None:
            cast = []
        if labels is None:
            labels = {'title': name, 'plot': description, 'mediatype': 'video'}

        if mode == 'navigator':
            pass

            if sys_url not in url:
                url = f'{sys_url}?action={url}'

            #url = '%s?action=%s' % (sys_url, url) if isAction is True else query
            thumb = os.path.join(self.art, thumb) if self.art is not None else icon

            fanart = self.addon_fanart()

            li = self.listItem(name)
            vtag = li.getVideoInfoTag()
            vtag.setMediaType(labels.get("mediatype", "video"))
            vtag.setTitle(labels.get("title", self.lang(32566)))
            if cm is not None:
                li.addContextMenuItems(cm)
            li.setArt({'icon': icon, 'thumb': thumb, 'fanart': fanart, 'poster': thumb})

            if fanart is not None:
                li.setProperty('fanart', fanart)

        xbmcplugin.addDirectoryItem(handle=sys_handle, url=url, listitem=li, isFolder=isFolder)


    def setContent(self, content: str) -> None:
        return xbmcplugin.setContent(int(sys.argv[1]), content)


    def endDirectory(self) -> None:
        xbmcplugin.endOfDirectory(int(sys.argv[1]))

    ######
    #
    # moving this over from control with better code
    def addon_icon(self) -> str:
        """Get addon icon path - delegates to _PathManager."""
        return self._art_paths.icon()

    def addon_thumb(self) -> str:
        """Get addon thumb path - delegates to _PathManager."""
        return self._art_paths.thumb()

    def addon_poster(self) -> str:
        """Get addon poster path - delegates to _PathManager."""
        return self._art_paths.poster()

    def addon_banner(self) -> str:
        """Get addon banner path - delegates to _PathManager."""
        return self._art_paths.banner()

    def addon_fanart(self) -> str:
        """Get addon fanart path - delegates to _PathManager."""
        return self._art_paths.fanart()

    def addon_clearart(self) -> str:
        """Get addon clearart path - delegates to _PathManager."""
        return self._art_paths.clearart()

    def addon_discart(self) -> str:
        """Get addon discart path - delegates to _PathManager."""
        return self._art_paths.discart()

    def addon_clearlogo(self) -> str:
        """Get addon clearlogo path - delegates to _PathManager."""
        return self._art_paths.clearlogo()

    def addon_next(self) -> str:
        """Get next icon path - delegates to _PathManager."""
        return self._art_paths.next_icon()

    def addon_adult_icon(self) -> str:
        """Get adult icon path - delegates to _PathManager."""
        return self._art_paths.adult_icon()

    def get_art_path(self) -> str:
        """Get artwork path for current theme."""
        theme = self.appearance()
        if theme in ['-', '']:
            return ''
        if xbmc.getCondVisibility('System.HasAddon(script.midnight.artwork)'):
            return os.path.join(
                xbmcaddon.Addon('script.midnight.artwork').getAddonInfo('path'),
                'resources',
                'media',
                str(theme)
            )
        return ''  # Fallback if addon not available

    def appearance(self):
        theme = (self.get_setting('appearance.1') or 'modern').strip().lower()
        if theme in ('', '-'):
            theme = 'modern'
        if not xbmc.getCondVisibility('System.HasAddon(script.midnight.artwork)'):
            return 'midnight'
        return theme

    def artwork(self) -> None:
        xbmc.executebuiltin('RunPlugin(plugin://script.midnight.artwork)')

    def capitalize_word(self, string) -> str:
        pass

        return string.title()

    def string_split_to_list(self, string) -> list:
        pass

        if string in ['0', None]:
            return []
        if(isinstance(string, list)):
            return string
        elif(isinstance(string, tuple)):
            return list(string)
        elif(isinstance(string, str)):
            string = string.strip()
        lst = string.split('/')
        lst = [s.strip() for s in lst]
        lst = [self.capitalize_word(s) for s in lst]

        return lst

    def search_tmdb_index_in_indicators(self, tmdb_id, indicator_list):
        try:
            pass

            if not indicator_list:
                return -1

            tmdb_id = str(tmdb_id)
            indices = [index for index, value in enumerate(indicator_list) if value[0] == tmdb_id]

            return indices[0] if indices else -1
        except Exception as e:
            failure = traceback.format_exc()
            pass

    def search_tmdb_index_in_indicators2(self, tmdb, indicators):
        pass

        if not indicators:
            return -1

        if not isinstance(tmdb, str):
            tmdb = str(tmdb)

        lst = [i for i, v in enumerate(indicators) if v[0] == tmdb]

        if len(lst) == 0:
            return -1
        else:
            return lst[0]


    def count_watched_items_in_indicators(self, index, indicators):
        """Count watched items in indicators list."""
        try:
            if not indicators[index]:
                return -1
            else:
                return len(indicators[index][2])

        except Exception as e:
            failure = traceback.format_exc()
            pass

    # Backwards compatibility alias (typo fix)
    def count_wachted_items_in_indicators(self, index, indicators):
        """Deprecated: typo in method name. Use count_watched_items_in_indicators instead."""
        return self.count_watched_items_in_indicators(index, indicators)


    @staticmethod
    def count_total_items_in_indicators(index, indicators):
        if not indicators[index]:
            return -1
        return indicators[index][1]

    @staticmethod
    def string_to_tuple(string):
        return tuple(map(str, string.split(',')))


    @staticmethod
    def unicode_art(_str) -> str:
        _str = re.sub('\\\\\\\\u([\\da-f]{4})', lambda x: chr(int(x.group(1), 16)), _str)
        return json.dumps(_str)

    def okDialog(self, message, heading='Info'):
        """Delegate to _DialogManager for OK dialog."""
        return self._dialogs.ok(message, heading)

    def yesnoDialog(self, message, heading=None, nolabel='', yeslabel=''):
        """Delegate to _DialogManager for Yes/No dialog."""
        if heading is None:
            heading = self.name
        return self._dialogs.yesno(message, heading, nolabel, yeslabel)

    def infoDialog(self, message, heading=None, icon='', time=3000, sound=False) -> None:
        """Delegate to _DialogManager for notification with icon handling."""
        if not str(message or '').strip():
            self.log('[infoDialog] Suppressed empty notification', 1)
            return
        if heading is None:
            heading = self.name
        if icon == '':
            icon = self.addon_icon()
        elif icon == 'INFO':
            icon = xbmcgui.NOTIFICATION_INFO
        elif icon == 'WARNING':
            icon = xbmcgui.NOTIFICATION_WARNING
        elif icon == 'ERROR':
            icon = xbmcgui.NOTIFICATION_ERROR
        elif icon.endswith('.png'):
            icon = os.path.join(self.art, icon)
        return self._dialogs.notification(heading, message, icon, time, sound)


c = CrewRuntime()

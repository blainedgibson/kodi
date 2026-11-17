# -*- coding: utf-8 -*-

'''
 ***********************************************************
 * Midnight Add-on
 *
 * Background service implementation for script.module.midnight.
 * Launched by plugin.video.midnight/service.py at Kodi startup.
 *
 * @file crewservice.py
 * @package script.module.midnight
 *
 * @copyright (c) 2023 - 2026, Midnight
 * @license GNU General Public License, version 3 (GPL-3.0)
 *
 ********************************************************cm*
'''

# CM - 06/07/2021  (original service.py)
# cm - 06/20/2023
# cm - 10/19/2024 - conversion check and fix from module v. 1.x to v. > 2.0.0
# cm - 11/01/2024 - added TraktMonitor for scrobble
# cm - 02/20/2026 - added CrewMonitor for global player monitoring (TV Evening playlist)
# cm - 02/22/2026 - added conversion check and fix for bookmarks file
# cm - 02/28/2026 - v22.26 ZERO-THREAD-GROWTH: CrewMonitor persistent worker thread for infinite episodes
# cm - 04/25/2026 - moved from plugin.video.midnight/service.py into script.module.midnight/lib
# wh - 08/26/2026 - Added persistent segment proxy run to strip tictok cdn

import os
import re
import sys
import time
import traceback
import glob
import queue
import threading

from threading import Thread
from shutil import rmtree
from resources.lib.modules import control
from resources.lib.modules import trakt
from resources.lib.modules import simkl
from resources.lib.modules import segmentproxy
from resources.lib.modules.crewruntime import c

import xbmc
import xbmcvfs
import xbmcgui
import xbmcaddon

QUARTERLY = 60 * 15
HALF_HOUR = 60 * 30
HOURLY = 60 * 60
FIVE_MINUTES = 60 * 5

# Device idle detection (screensaver, CEC TV standby, OS sleep).
# Other modules can skip bg warm / heavy sync while this is 'true'.
DEVICE_SLEEP_PROP = 'midnight.device.sleeping'
DEVICE_SLEEP_REASON_PROP = 'midnight.device.sleep_reason'
_SLEEP_NOTIFY_METHODS = frozenset({'GUI.OnScreensaverActivated', 'System.OnSleep'})
_WAKE_NOTIFY_METHODS = frozenset({'GUI.OnScreensaverDeactivated', 'System.OnWake'})

_trakt_monitor_instance = None
_last_settings_changed_at = 0.0
_SETTINGS_CHANGED_DEBOUNCE_SEC = 2.0


def _dismiss_busy_spinners():
    """Close both busy dialog variants (Kodi 21 uses either depending on context)."""
    try:
        control.idle()
    except Exception:
        pass


def _idle_after_settings_change():
    """Home widgets reload after any addon settings save — clear stale busy cursors."""
    for delay in (0.5, 2.0, 5.0):
        if control.monitor.abortRequested():
            return
        time.sleep(delay)
        _dismiss_busy_spinners()


def _ensure_trakt_monitor_for_tracker():
    """Start TraktMonitor if tracker flipped to Trakt while service was already running."""
    global _trakt_monitor_instance
    try:
        want_trakt = simkl.use_trakt_tracker()
    except Exception:
        want_trakt = True
    if want_trakt and _trakt_monitor_instance is None:
        _trakt_monitor_instance = TraktMonitor()
        Thread(target=_trakt_monitor_instance.run, name='CrewTraktMonitor', daemon=True).start()
        c.log('[CrewMonitor] TraktMonitor started after tracker flip to Trakt', 1)
    elif not want_trakt and _trakt_monitor_instance is not None:
        # Leave the monitor thread; it only does Trakt work gated elsewhere.
        # Stopping xbmc.Monitor cleanly mid-session is awkward — log only.
        c.log(
            '[CrewMonitor] tracker.service is Simkl — TraktMonitor still alive but Trakt '
            'bg work should no-op via XOR gates',
            1,
        )


def _handle_settings_changed(source: str = 'CrewMonitor'):
    """
    Always run from CrewMonitor (even when TraktMonitor was skipped at Simkl boot).

    Without this, flipping Simkl→Trakt left the long-lived service on stale
    tracker.service and kept scrobbling Simkl while widgets (fresh plugin) used Trakt.
    """
    global _last_settings_changed_at
    now = time.time()
    if now - _last_settings_changed_at < _SETTINGS_CHANGED_DEBOUNCE_SEC:
        return
    _last_settings_changed_at = now
    try:
        from .settings_repair import settings_sync_in_progress
        if settings_sync_in_progress():
            return
    except Exception:
        pass

    try:
        c.invalidate_settings_cache()
    except Exception as e:
        c.log(f'[{source}] settings cache invalidate skipped: {e}', 1)

    try:
        from .debrid import clear_debrid_session_state
        clear_debrid_session_state()
        c.log(f'[{source}] Cleared debrid session caches after settings save', 1)
    except Exception as e:
        c.log(f'[{source}] debrid cache clear on change skipped: {e}', 1)

    try:
        from .settings_repair import commit_live_settings_to_backups
        # CRUD update: live intent wins — mirrors cannot resurrect old debrid/tracker state.
        commit_live_settings_to_backups(log=c.log)
    except Exception as e:
        c.log(f'[{source}] settings backup commit skipped: {e}', 1)

    try:
        _ensure_trakt_monitor_for_tracker()
        if c.devmode:
            c.log(f'[{source}] tracker after settings save: {simkl.scrobble_gate_debug()}', 1)
    except Exception as e:
        c.log(f'[{source}] tracker reconcile skipped: {e}', 1)

    _dismiss_busy_spinners()
    threading.Thread(
        target=_idle_after_settings_change,
        name='CrewSettingsIdle',
        daemon=True,
    ).start()


def is_device_idle():
    """True when Kodi screensaver, CEC standby, or OS sleep has marked the box idle."""
    try:
        return xbmcgui.Window(10000).getProperty(DEVICE_SLEEP_PROP) == 'true'
    except Exception:
        return False


class _PlaybackWatcher(xbmc.Player):
    """Thin xbmc.Player subclass that signals a threading.Event the instant
    Kodi reports A/V decoding has started (onAVStarted).  Used in the
    CrewMonitor worker to replace the old 240-iteration poll loop."""
    def __init__(self):
        super().__init__()
        self.started = threading.Event()

    def onAVStarted(self):
        self.started.set()


def conversion():
    # cm - 10/19/2024 Removed dialogs as being obsolete and not needed
    conversionFile = os.path.join(control.dataPath, 'conversion.v')
    bookmarkFile = control.bookmarksFile
    curVersion = c.moduleversion

    if not os.path.exists(conversionFile):
        f_path = xbmcvfs.translatePath('special://home/addons/script.midnight.metadata')
        rmtree(f_path, ignore_errors=True)

        if os.path.isfile(bookmarkFile):
            os.remove(bookmarkFile)
            c.log(f'removing {str(bookmarkFile)}')

        if not os.path.isfile(bookmarkFile):
            with open(conversionFile, 'w', encoding="utf8") as fh:
                fh.write(curVersion)
            if c.devmode:
                c.log(f'File written, Conversion successful, version = {curVersion}')


def readProviders(scraper_path_fill, msg1, catnr):
    """
    Legacy entry point kept for compatibility. Scrapers now live under
    resources/lib/scrapers/ and are synced in one pass by
    sync_all_internal_providers().
    """
    del scraper_path_fill, msg1, catnr


def sync_all_internal_providers():
    """
    Legacy startup hook — provider.* schema sync retired (ScraperRegistry).
    Kept so existing service callers do not break.
    """
    if c.devmode:
        c.log('[crewservice] provider schema sync retired — using ScraperRegistry')


def _openfile(path_to_the_file):
    try:
        with open(path_to_the_file, 'r', encoding="utf8") as fh:
            contents = fh.read()
        return contents
    except (OSError, IOError) as e:
        c.log(f"Service Open File Exception - {path_to_the_file}: {e}")
        return None


def _savefile(path_to_the_file, content):
    try:
        with open(path_to_the_file, 'w', encoding="utf8") as fh:
            fh.write(content)
    except (OSError, IOError) as e:
        c.log(f"Service Save File Exception - {path_to_the_file}: {e}")


# CDN files that need a fresh disk cache kept every 15 minutes.
# crewschedule updates ~every 15 min (live event list).
# crewstreamer updates whenever stream sources change.
# crewreplays is less urgent but kept in sync anyway.
_CDN_FILES = [
    ('https://raw.githubusercontent.com/posadka/xmls2/main/crewstreamer.xml', 'crewstreamer'),
    ('https://raw.githubusercontent.com/posadka/xmls2/main/crewschedule.xml', 'crewschedule'),
    ('https://raw.githubusercontent.com/posadka/xmls2/main/crewreplays.xml',  'crewreplays'),
]


def _refresh_cdn_caches():
    """Silently download all CDN files to disk so the fallback copy stays fresh.
    Called every QUARTERLY seconds by TraktMonitor.  Does NOT import/execute
    the files — that happens on-demand in cdnImport() when a user opens the section."""
    from resources.lib.modules import client as _client
    _modules_dir = os.path.dirname(os.path.abspath(
        os.path.join(control.addonPath, 'lib', 'resources', 'lib', 'modules', 'control.py')
    ))
    local_overrides_dir = os.path.normpath(os.path.join(_modules_dir, '..', 'local_overrides'))

    for uri, name in _CDN_FILES:
        # Skip if a local .py override is in place — CDN is bypassed in cdnImport anyway.
        if os.path.isfile(os.path.join(local_overrides_dir, f'{name}.py')):
            if c.devmode:
                c.log(f'[CDN refresh] Skipping {name}: local override present')
            continue
        # Skip if a local XML override is in place.
        if os.path.isfile(os.path.join(control.addonPath, f'{name}.xml')):
            if c.devmode:
                c.log(f'[CDN refresh] Skipping {name}: local XML override present')
            continue
        try:
            control.makeFile(control.dataPath)
            r = _client.request(uri)
            cached_file = os.path.join(control.dataPath, f'{name}.py')
            f = control.openFile(cached_file, 'w')
            f.write(r)
            f.close()
            if c.devmode:
                c.log(f'[CDN refresh] Refreshed disk cache for {name}')
        except Exception as e:
            c.log(f'[CDN refresh] Failed to refresh {name}: {e}')


def syncTraktLibrary():
    control.execute('RunPlugin(plugin://plugin.video.midnight/?action=tvshowsToLibrarySilent&url=traktcollection')
    control.execute('RunPlugin(plugin://plugin.video.midnight/?action=moviesToLibrarySilent&url=traktcollection')


def syncTrakt(silent=False):
    trakt.syncTrakt(silent=silent)


def main():
    """
    Run one-time startup tasks.
    Executes once when Kodi starts, NOT continuously.
    Scheduled/continuous tasks are in TraktMonitor.
    """
    try:
        # Rotate previous session log first — otherwise early boot lines (version, health)
        # land in the_crew.old.log and look missing when tailing the_crew.log.
        try:
            from .crewruntime import resolve_crew_log_path
            log_path = resolve_crew_log_path()
            log_dir = os.path.dirname(log_path)
            old_log_path = os.path.join(log_dir, 'the_crew.old.log')
            if xbmcvfs.exists(log_path):
                stat = xbmcvfs.Stat(log_path)
                if stat.st_size() > 0:
                    try:
                        if xbmcvfs.exists(old_log_path):
                            xbmcvfs.delete(old_log_path)
                    except Exception:
                        pass
                    try:
                        xbmcvfs.rename(log_path, old_log_path)
                    except Exception:
                        pass
        except Exception:
            pass  # Don't fail startup if log rotation fails

        c.log_boot_option()

        # Repair userdata before any setSetting() — Kodi 21 rejects orphan setting IDs.
        try:
            from .settings_repair import (
                repair_if_needed,
                prune_obsolete_settings_backups,
                commit_live_settings_to_backups,
            )
            repair_if_needed(log=c.log)
            # Drop stale health_backup_* / pre_simkl leftovers, then mirror live (CRUD).
            prune_obsolete_settings_backups(log=c.log)
            commit_live_settings_to_backups(log=c.log)
        except Exception as e:
            xbmc.log(f'[crewservice] settings_repair skipped: {e}', 2)

        # Version announcement: right after settings repair, before heavy boot work.
        # Skips automatically if user is already playing or boot window passed.
        try:
            from .version_announcement import check_and_show_version_announcement
            shown = check_and_show_version_announcement()
            xbmc.log(
                f'[Midnight][Boot] Version announcement handled (shown={shown})',
                xbmc.LOGINFO,
            )
        except Exception as e:
            xbmc.log(f'[crewservice] version_announcement skipped: {e}', 2)

        # One-time 3.0.5 health check (settings, DB schemas, scraper migration).
        try:
            from .upgrade_health_check import run_upgrade_health_check_if_needed
            from .version_announcement import suppress_startup_ui
            quiet = suppress_startup_ui()
            if quiet:
                xbmc.log('[Midnight][Boot] Builders kill switch on — startup UI muted', xbmc.LOGINFO)
            run_upgrade_health_check_if_needed(log=c.log, notify=not quiet)
            xbmc.log('[Midnight][Boot] Upgrade health check finished', xbmc.LOGINFO)
        except Exception as e:
            xbmc.log(f'[crewservice] upgrade_health_check skipped: {e}', 2)

        # Re-assert after health check — profile/backup restores used to wipe
        # last_version_announcement and re-show the welcome dialog next boot.
        try:
            from .version_announcement import PROP_VERSION_HANDLED, _persist_announced_version
            if control.window.getProperty(PROP_VERSION_HANDLED) == 'true':
                module_ver = xbmcaddon.Addon('script.module.midnight').getAddonInfo('version')
                if module_ver:
                    _persist_announced_version(module_ver)
        except Exception as e:
            xbmc.log(f'[crewservice] last_version re-assert skipped: {e}', 2)

        c.initialize_all()
        conversion()
        if simkl.use_trakt_tracker():
            syncTrakt(silent=control.setting('silent.boot') == 'true')
        else:
            c.log('[crewservice] Skipping boot Trakt sync — tracker.service is Simkl-only', 1)

        control.startupMaintenance()

        module_version = xbmcaddon.Addon('script.module.midnight').getAddonInfo('version')
        plugin_version = xbmcaddon.Addon('plugin.video.midnight').getAddonInfo('version')
        prev_module = (control.setting('module_base') or '').strip()
        prev_plugin = (control.setting('plugin_base') or '').strip()
        xbmcaddon.Addon('plugin.video.midnight').setSetting('module_base', module_version)
        xbmcaddon.Addon('plugin.video.midnight').setSetting('plugin_base', plugin_version)

        # New version → refresh emergency profile backup so restores don't roll markers back.
        if prev_module != module_version or prev_plugin != plugin_version:
            try:
                from .settings_repair import (
                    prune_obsolete_settings_backups,
                    commit_live_settings_to_backups,
                )
                prune_obsolete_settings_backups(log=c.log)
                commit_live_settings_to_backups(log=c.log)
                c.log(
                    f'[crewservice] Refreshed emergency settings backup '
                    f'(module {prev_module or "?"}→{module_version}, '
                    f'plugin {prev_plugin or "?"}→{plugin_version})',
                    1,
                )
            except Exception as e:
                xbmc.log(f'[crewservice] emergency backup refresh skipped: {e}', 2)

        sync_all_internal_providers()

        try:
            from .scraper_registry import get_registry
            get_registry().load()
        except Exception as e:
            c.log(f'[crewservice] ScraperRegistry warm load skipped: {e}')

        if c.devmode:
            c.log('Providers done')

        if control.setting('autoTraktOnStart') == 'true' and simkl.use_trakt_tracker():
            if c.devmode:
                c.log('autoTraktOnStart Enabled: synctraktlib started')
            syncTraktLibrary()
        elif control.setting('autoTraktOnStart') == 'true':
            c.log('[crewservice] Skipping autoTraktOnStart — tracker.service is Simkl-only', 1)

    except Exception as e:
        failure = traceback.format_exc()
        c.log(f'Traceback:: {str(failure)}')
        c.log(f'Exception raised: {str(e)}')


class TraktMonitor(xbmc.Monitor):
    def __init__(self):
        xbmc.Monitor.__init__(self)

    def onSettingsChanged(self):
        """Delegates to shared handler (also run from CrewMonitor when TraktMonitor is off)."""
        _handle_settings_changed(source='TraktMonitor')

    def _set_device_idle(self, reason):
        """Mark box idle and flush pending scrobbles before sleep/standby."""
        win = xbmcgui.Window(10000)
        if win.getProperty(DEVICE_SLEEP_PROP) == 'true':
            return
        win.setProperty(DEVICE_SLEEP_PROP, 'true')
        win.setProperty(DEVICE_SLEEP_REASON_PROP, reason)
        c.log(f'[TraktMonitor] Device idle ({reason}) — flushing scrobble queue to Trakt')
        xbmc.log(f'[script.module.midnight][TraktMonitor] Device idle ({reason})', 2)
        try:
            trakt.process_scrobble_queue(force=True)  # type: ignore
        except Exception as e:
            c.log(f'[TraktMonitor] Scrobble flush on idle failed: {e}')

    def _set_device_awake(self, reason):
        """Clear idle flag and lightly reconcile Trakt activity timestamps."""
        win = xbmcgui.Window(10000)
        was_idle = win.getProperty(DEVICE_SLEEP_PROP) == 'true'
        win.clearProperty(DEVICE_SLEEP_PROP)
        win.clearProperty(DEVICE_SLEEP_REASON_PROP)
        if not was_idle:
            return
        c.log(f'[TraktMonitor] Device awake ({reason}) — reconciling last_activities')
        xbmc.log(f'[script.module.midnight][TraktMonitor] Device awake ({reason})', 2)
        try:
            trakt.arm_trakt_wake_quiet()
        except Exception:
            pass
        if trakt.should_defer_background_trakt():
            c.log('[TraktMonitor] Wake reconcile deferred — boot quiet or rate-limit backoff', 1)
            return
        try:
            trakt.sync_last_activities()
        except Exception as e:
            c.log(f'[TraktMonitor] Wake reconcile failed: {e}')

    def onScreensaverActivated(self):
        self._set_device_idle('screensaver')

    def onScreensaverDeactivated(self):
        self._set_device_awake('screensaver')

    def onNotification(self, sender, method, data):
        # System.OnSleep / OnWake cover CEC TV-off and OS hibernate paths.
        if method in _SLEEP_NOTIFY_METHODS:
            self._set_device_idle(method)
        elif method in _WAKE_NOTIFY_METHODS:
            self._set_device_awake(method)

    def run(self):
        if c.devmode:
            c.log('[TraktMonitor] Service starting')
        xbmc.log('[script.module.midnight][TraktMonitor] Service Starting', 2)

        # Clear any stale window properties left over from a previous Kodi session or crash.
        # midnight.trakt_refreshing_token is a mutex guard - if it was left set it causes
        # every subsequent Trakt call to spin-wait 30 seconds before timing out.
        _win = xbmcgui.Window(10000)
        _win.clearProperty(trakt.PROP_TRAKT_REFRESHING)
        _win.clearProperty('midnight.trakt_auth_failed')
        _win.clearProperty('midnight.trakt_auth_notified')
        _win.clearProperty('midnight.trakt_token_logged')
        _win.clearProperty('midnight.trakt_status')
        _win.clearProperty('midnight.trakt_network_notified')
        _win.clearProperty('midnight.debrid_unreachable_notified')
        _win.clearProperty(DEVICE_SLEEP_PROP)
        _win.clearProperty(DEVICE_SLEEP_REASON_PROP)
        if c.devmode:
            c.log('[TraktMonitor] Cleared stale Trakt window properties')

        # Let gears / script.trakt finish their startup oauth/token refresh first.
        # Quiet is armed once in crewservice.start() — do not re-arm here (extends delay).
        while trakt.is_trakt_boot_quiet() and not self.abortRequested():
            if self.waitForAbort(5):
                break
        if not self.abortRequested():
            c.log('[TraktMonitor] Boot quiet ended — resuming background Trakt work', 1)
            try:
                from resources.lib.modules import trakt_startup_conflict
                trakt_startup_conflict.log_startup_conflict_if_needed()
            except Exception as e:
                c.log(f'[TraktMonitor] Startup conflict log skipped: {e}')

        try:
            trakt.process_scrobble_queue() # type: ignore
            if c.devmode:
                c.log('[TraktMonitor] Processed startup scrobble queue')
        except Exception as e:
            c.log(f'[TraktMonitor] Error processing startup scrobble queue: {e}')

        scrobble_counter = 0
        token_check_counter = 0
        trakt_sync_counter = 0
        cdn_refresh_counter = 0
        trakt_sync_interval = int(control.setting('schedTraktTime')) * 3600

        if c.devmode and trakt_sync_interval > 0:
            c.log(f'[TraktMonitor] Scheduled Trakt sync enabled: every {trakt_sync_interval // 3600} hours')

        while not self.abortRequested():
            if self.waitForAbort(60):
                break

            if is_device_idle():
                continue

            if trakt.should_defer_background_trakt():
                continue

            scrobble_counter += 60
            trakt_sync_counter += 60
            token_check_counter += 60
            cdn_refresh_counter += 60

            if token_check_counter >= HOURLY:
                try:
                    trakt.check_and_refresh_token()
                except Exception as e:
                    c.log(f'[TraktMonitor] Error in proactive token check: {e}')
                token_check_counter = 0

            if scrobble_counter >= FIVE_MINUTES:
                if c.devmode:
                    c.log('[TraktMonitor] Processing scrobble queue')
                try:
                    trakt.process_scrobble_queue() # type: ignore
                except Exception as e:
                    c.log(f'[TraktMonitor] Error processing scrobble queue: {e}')
                scrobble_counter = 0

            if trakt_sync_interval > 0 and trakt_sync_counter >= trakt_sync_interval:
                if c.devmode:
                    c.log(f'[TraktMonitor] Running scheduled Trakt sync (every {trakt_sync_interval // 3600} hours)')
                try:
                    syncTrakt(silent=True)
                    syncTraktLibrary()
                except Exception as e:
                    c.log(f'[TraktMonitor] Error in scheduled Trakt sync: {e}')
                trakt_sync_counter = 0

            if cdn_refresh_counter >= QUARTERLY:
                try:
                    _refresh_cdn_caches()
                except Exception as e:
                    c.log(f'[TraktMonitor] Error in CDN cache refresh: {e}')
                cdn_refresh_counter = 0

        try:
            trakt.process_scrobble_queue() # type: ignore
        except Exception as e:
            c.log(f'[TraktMonitor] Error processing final scrobble queue: {e}')

        if c.devmode:
            c.log('[TraktMonitor] Service finished')
        xbmc.log('[script.module.midnight][TraktMonitor] Service Finished', 2)


# v22.26: Module-level shutdown event for worker thread.
# Must be module-level so it survives CrewMonitor instance deletion during module reloads.
# Worker thread checks this instead of self.abortRequested() to avoid thread death
# from a deleted instance.
_worker_shutdown_event = threading.Event()

# Module-level monitor reference — set when CrewMonitor is instantiated.
_crewmonitor_instance = None


def get_crew_monitor():
    """Return the active CrewMonitor instance, or None if not yet running."""
    return _crewmonitor_instance


class CrewMonitor(xbmc.Monitor):
    def __init__(self):
        xbmc.Monitor.__init__(self)
        self.crew_player = None

        # v22.26: ZERO-THREAD-GROWTH — one persistent worker thread for the entire session.
        # Carry over the episode counter if an older instance existed.
        old_instance = getattr(queue, '_midnight_crewmonitor_singleton', None)
        self.episodes_processed = old_instance.episodes_processed if old_instance else 0

        global _crewmonitor_instance
        _crewmonitor_instance = self
        setattr(queue, '_midnight_crewmonitor_singleton', self)

        if not control.window.getProperty('midnight.upnext.ready'):
            control.window.setProperty('midnight.upnext.ready', 'true')
            if c.devmode:
                c.log("[CrewMonitor] Initialized upnext window properties")

        self.startServices()

    def onSettingsChanged(self):
        """
        Must live on CrewMonitor — TraktMonitor is not started when booting on Simkl.
        Clears settings cache so scrobble XOR tracks tracker.service flips without restart.
        """
        _handle_settings_changed(source='CrewMonitor')

    @classmethod
    def get_or_create_instance(cls):
        """Always create new instance — worker thread persists independently."""
        if c.devmode:
            c.log("[CrewMonitor] Creating instance")
        return cls()

    def _persistent_monitoring_worker(self):
        """
        v22.26: Window property-polling worker.

        ONE thread for the entire Kodi session:
        - Polls midnight.upnext.active every second
        - When active: monitors playback, triggers UpNext, saves resume, scrobbles
        - Persists across module reloads (non-daemon thread)
        - Only exits on _worker_shutdown_event or Kodi shutdown
        """
        import time

        c.log("[CrewMonitor-Worker] UpNext monitoring started")
        if c.devmode:
            c.log("[CrewMonitor-Worker] Polling window properties for episode triggers")

        episodes_handled = 0

        global _worker_shutdown_event
        while not _worker_shutdown_event.is_set():
            try:
                # Kodi abort is independent of our Event — honour both so exit is clean.
                if control.monitor.abortRequested():
                    break

                upnext_active = control.window.getProperty('midnight.upnext.active')

                if upnext_active == 'true':
                    try:
                        from resources.lib.modules import upnext as _upnext_owner
                        if not _upnext_owner._crew_owns_upnext():
                            if c.devmode:
                                c.log(
                                    f"[CrewMonitor-Worker] UpNext owned by "
                                    f"{_upnext_owner._upnext_plugin_owner()} — skip"
                                )
                            if _worker_shutdown_event.wait(1.0):
                                break
                            continue
                    except Exception:
                        pass
                    episodes_handled += 1

                    player = xbmc.Player()

                    if c.devmode:
                        c.log("[CrewMonitor-Worker] Waiting for playback to start...")
                    started = False
                    for _ in range(240):
                        if player.isPlayingVideo():
                            started = True
                            break
                        if _worker_shutdown_event.is_set() or control.monitor.abortRequested():
                            break
                        # Stale arm (e.g. Classy left midnight.upnext.active) — abort wait
                        if control.window.getProperty('midnight.upnext.active') != 'true':
                            break
                        if _worker_shutdown_event.wait(1.0):
                            break

                    if not started:
                        if control.window.getProperty('midnight.upnext.active') != 'true':
                            if c.devmode:
                                c.log("[CrewMonitor-Worker] UpNext disarmed while waiting — skip")
                            continue
                        c.log(f"[CrewMonitor-Worker] ! Playback timeout for episode {episodes_handled}")
                        try:
                            from resources.lib.modules import upnext as _upnext_timeout
                            _upnext_timeout.clear_upnext_state('playback timeout')
                        except Exception:
                            control.window.clearProperty('midnight.upnext.active')
                        continue

                    # Capture identity AFTER playback is live. Reading before wait lets a
                    # prior show (or Classy dual-install props) stick while a new title plays.
                    title = control.window.getProperty('midnight.upnext.title')
                    year = control.window.getProperty('midnight.upnext.year')
                    season = control.window.getProperty('midnight.upnext.season')
                    episode = control.window.getProperty('midnight.upnext.episode')
                    imdb = control.window.getProperty('midnight.upnext.imdb')
                    tmdb = control.window.getProperty('midnight.upnext.tmdb')
                    content = control.window.getProperty('midnight.upnext.content') or 'episode'
                    is_movie = content == 'movie'

                    # Prefer Midnight player global meta when present (beats foreign Home props)
                    try:
                        from resources.lib.modules import player as _player_mod
                        with _player_mod._metadata_lock:
                            _meta = dict(_player_mod._latest_episode_metadata)
                        _meta_age = time.time() - float(_meta.get('timestamp') or 0)
                        if (
                            _meta_age < 120.0
                            and _meta.get('content') == 'episode'
                            and _meta.get('imdb')
                            and _meta.get('season')
                            and _meta.get('episode')
                        ):
                            title = _meta.get('title') or title
                            year = str(_meta.get('year') or year or '')
                            season = str(_meta.get('season'))
                            episode = str(_meta.get('episode'))
                            imdb = str(_meta.get('imdb') or '')
                            tmdb = str(_meta.get('tmdb') or tmdb or '')
                            content = 'episode'
                            is_movie = False
                            if c.devmode:
                                c.log(
                                    f"[CrewMonitor-Worker] Bound session from player meta: "
                                    f"{title} S{season}E{episode} imdb={imdb}"
                                )
                    except Exception:
                        pass

                    if is_movie:
                        c.log(f"[CrewMonitor-Worker] Movie {episodes_handled}: {title} ({year})")
                    else:
                        c.log(f"[CrewMonitor-Worker] Episode {episodes_handled}: {title} S{season}E{episode}")

                    if c.devmode:
                        c.log("[CrewMonitor-Worker] Playback active, monitoring for UpNext trigger")

                    # Read UpNext settings once before the monitoring loop
                    _upnext_end_seconds = int(float(control.setting('upnext.end_seconds') or 0))
                    _upnext_countdown = int(float(control.setting('upnext.countdown_seconds') or 15))
                    _upnext_trigger_pct = int(float(control.setting('upnext.trigger_percent') or 97)) / 100.0

                    c.log(f"[CrewMonitor-Worker] UpNext settings: end_seconds={_upnext_end_seconds}, countdown={_upnext_countdown}, trigger_pct={_upnext_trigger_pct:.2f}")
                    if _upnext_end_seconds > 0:
                        c.log(f"[CrewMonitor-Worker] UpNext mode: TIME-BASED (dialog at totalTime-{_upnext_end_seconds + _upnext_countdown}s, prefetch at totalTime-{_upnext_end_seconds + _upnext_countdown + 90}s)")
                    else:
                        c.log(f"[CrewMonitor-Worker] UpNext mode: PERCENT-BASED (dialog at {_upnext_trigger_pct*100:.0f}%)")

                    upnext_triggered = False
                    prefetch_triggered = False
                    last_current_time = 0
                    last_total_time = 0

                    while player.isPlayingVideo():
                        try:
                            if _worker_shutdown_event.is_set() or control.monitor.abortRequested():
                                break

                            totalTime = player.getTotalTime()
                            currentTime = player.getTime()

                            if totalTime <= 0:
                                if _worker_shutdown_event.wait(2.0):
                                    break
                                continue

                            last_current_time = currentTime
                            last_total_time = totalTime

                            percent = currentTime / totalTime

                            # Compute trigger conditions based on settings
                            if _upnext_end_seconds > 0:
                                _dialog_time = totalTime - _upnext_end_seconds - _upnext_countdown
                                should_trigger_upnext = currentTime >= _dialog_time
                                should_trigger_prefetch = currentTime >= (_dialog_time - 90)
                            else:
                                should_trigger_upnext = percent >= _upnext_trigger_pct
                                should_trigger_prefetch = percent >= max(0.75, _upnext_trigger_pct - 0.10)

                            # Pre-scrape trigger: start background scraping ~90s before dialog fires
                            if not prefetch_triggered and should_trigger_prefetch and not is_movie:
                                tv_evening_active = control.window.getProperty('midnight.tvevening.monitor.active') == 'true'
                                if tv_evening_active:
                                    if c.devmode:
                                        c.log("[CrewMonitor-Worker] Skipping UpNext prefetch — TV Evening playlist is active")
                                    prefetch_triggered = True
                                else:
                                    prefetch_triggered = True
                                    try:
                                        from resources.lib.modules import episode_container as _ec_mod
                                        _next_ep_num = int(episode) + 1
                                        if c.devmode:
                                            c.log(f"[CrewMonitor-Worker] Starting prefetch S{int(season):02d}E{_next_ep_num:02d}")
                                        _ec_mod.start_prefetch(
                                            title=title,
                                            year=year,
                                            season=int(season),
                                            episode=_next_ep_num,
                                            imdb=imdb,
                                            tmdb=tmdb
                                        )
                                    except Exception as e:
                                        c.log(f"[CrewMonitor-Worker] ⚠ Prefetch start error: {e}")

                            if not upnext_triggered and should_trigger_upnext and not is_movie:
                                tv_evening_active = control.window.getProperty('midnight.tvevening.monitor.active') == 'true'
                                if tv_evening_active:
                                    if c.devmode:
                                        c.log("[CrewMonitor-Worker] Skipping UpNext - TV Evening playlist is active")
                                    upnext_triggered = True
                                    continue

                                try:
                                    from resources.lib.modules import upnext as _upnext_owner
                                    if not _upnext_owner._crew_owns_upnext():
                                        upnext_triggered = True
                                        continue
                                except Exception:
                                    pass

                                # Re-check identity: refuse wrong-show dialog if props/meta moved.
                                # Incomplete live props (cleared mid-session) are noise — keep session.
                                from resources.lib.modules import upnext as _upnext_id

                                _live_imdb = control.window.getProperty('midnight.upnext.imdb') or ''
                                _live_tmdb = control.window.getProperty('midnight.upnext.tmdb') or ''
                                _live_season = control.window.getProperty('midnight.upnext.season') or ''
                                _live_episode = control.window.getProperty('midnight.upnext.episode') or ''
                                _live_title = control.window.getProperty('midnight.upnext.title') or title
                                _live_year = control.window.getProperty('midnight.upnext.year') or year
                                try:
                                    from resources.lib.modules import player as _player_mod
                                    with _player_mod._metadata_lock:
                                        _meta = dict(_player_mod._latest_episode_metadata)
                                    _meta_age = time.time() - float(_meta.get('timestamp') or 0)
                                    if (
                                        _meta_age < 120.0
                                        and _meta.get('content') == 'episode'
                                        and _upnext_id.identity_complete(
                                            _meta.get('imdb'), _meta.get('tmdb'),
                                            _meta.get('season'), _meta.get('episode'),
                                        )
                                    ):
                                        _live_imdb = str(_meta.get('imdb') or _live_imdb)
                                        _live_tmdb = str(_meta.get('tmdb') or _live_tmdb)
                                        _live_season = str(_meta.get('season'))
                                        _live_episode = str(_meta.get('episode'))
                                        _live_title = _meta.get('title') or _live_title
                                        _live_year = str(_meta.get('year') or _live_year or '')
                                except Exception:
                                    pass

                                _live_ok = _upnext_id.identity_complete(
                                    _live_imdb, _live_tmdb, _live_season, _live_episode,
                                )
                                _sess_ok = _upnext_id.identity_complete(imdb, tmdb, season, episode)
                                _sess_imdb = str(imdb or '')
                                _sess_tmdb = str(tmdb or '')

                                if not _sess_ok:
                                    c.log(
                                        f"[CrewMonitor-Worker] UpNext skip — session identity incomplete "
                                        f"({title} S{season}E{episode} imdb={imdb} tmdb={tmdb})"
                                    )
                                    upnext_triggered = True
                                    continue

                                if _live_ok and (
                                    (_live_imdb and _sess_imdb and _live_imdb != _sess_imdb)
                                    or (_live_tmdb and _sess_tmdb and _live_tmdb != _sess_tmdb)
                                    or (str(season) != str(_live_season))
                                    or (str(episode) != str(_live_episode))
                                ):
                                    c.log(
                                        f"[CrewMonitor-Worker] UpNext identity changed mid-session "
                                        f"(had {title} S{season}E{episode} imdb={imdb}; "
                                        f"now {_live_title} S{_live_season}E{_live_episode} "
                                        f"imdb={_live_imdb}) — rebinding, skip this trigger"
                                    )
                                    try:
                                        from resources.lib.modules import episode_container as _ec_rebind
                                        _ec_rebind.cancel_upnext()
                                    except Exception:
                                        pass
                                    title, year = _live_title, _live_year
                                    season, episode = _live_season, _live_episode
                                    imdb, tmdb = _live_imdb, _live_tmdb
                                    upnext_triggered = False
                                    prefetch_triggered = False
                                    continue

                                if not _live_ok:
                                    c.log(
                                        f"[CrewMonitor-Worker] UpNext live props incomplete "
                                        f"(S{_live_season}E{_live_episode} imdb={_live_imdb}) — "
                                        f"keeping session {title} S{season}E{episode}"
                                    )

                                if c.devmode:
                                    c.log(f"[CrewMonitor-Worker] UpNext trigger at {percent*100:.1f}% ({currentTime:.0f}s/{totalTime:.0f}s)")
                                upnext_triggered = True

                                try:
                                    if c.devmode:
                                        c.log(f"[CrewMonitor-Worker] Showing UpNext dialog for {title} S{season}E{episode}")
                                    _upnext_id.request_upnext_dialog(
                                        current_title=title,
                                        current_year=year,
                                        current_imdb=imdb,
                                        current_tmdb=tmdb,
                                        current_season=int(season),
                                        current_episode=int(episode)
                                    )
                                    if c.devmode:
                                        c.log("[CrewMonitor-Worker] UpNext dialog queued for service dispatch")
                                except Exception as e:
                                    c.log(f"[CrewMonitor-Worker] ⚠ UpNext error: {e}")
                                    if c.devmode:
                                        c.log(f"[CrewMonitor-Worker] Traceback: {traceback.format_exc()}")

                            if _worker_shutdown_event.wait(2.0):
                                break

                        except Exception as e:
                            c.log(f"[CrewMonitor-Worker] ⚠ Error checking playback: {e}")
                            break

                    # Playback ended — save resume point and scrobble
                    try:
                        if last_total_time > 0:
                            resume_percent = last_current_time / last_total_time
                            is_fully_watched = resume_percent >= 0.92
                            is_in_progress = last_current_time > 60 and not is_fully_watched
                            raw_imdb = imdb if imdb and imdb not in ('None', 'null', '', '0') else ''
                            raw_tmdb = int(tmdb) if tmdb and tmdb not in ('None', 'null', '', '0') else 0

                            if is_in_progress or is_fully_watched:
                                from resources.lib.modules import bookmarks as bm
                                bm.reset(
                                    int(last_current_time), int(last_total_time), content,
                                    raw_imdb, season, episode, tmdb=raw_tmdb
                                )
                                c.log(f"[CrewMonitor-Worker] (OK) Resume saved: {last_current_time:.0f}s/{last_total_time:.0f}s ({resume_percent*100:.1f}%) imdb={raw_imdb} tmdb={raw_tmdb} content={content}")

                                if is_in_progress:
                                    xbmc.executebuiltin('Container.Refresh')
                                    c.log("[CrewMonitor-Worker] Container refreshed to show resume indicator")

                                if simkl.should_scrobble_playback():
                                    scrobble_action = 'stop' if is_fully_watched else 'pause'
                                    bm.set_scrobble(
                                        int(last_current_time), int(last_total_time), content,
                                        raw_imdb, season, episode, scrobble_action, tmdb=raw_tmdb,
                                    )
                                    c.log(f"[CrewMonitor-Worker] Scrobbled: {resume_percent*100:.1f}% ({scrobble_action})")
                                else:
                                    c.log(f"[CrewMonitor-Worker] Scrobble skipped — {simkl.scrobble_gate_debug()}")
                            else:
                                c.log(f"[CrewMonitor-Worker] Skipping resume save: current={last_current_time:.0f}s, too short")
                    except Exception as e:
                        c.log(f"[CrewMonitor-Worker] Error saving resume: {e}")

                    # Autoplay fallback: dialog was queued but service never dispatched it
                    # (e.g. RunPlugin era, or doModal failed). Player.onPlayBackEnded is not
                    # reliable in the CrewMonitor architecture.
                    try:
                        from resources.lib.modules import upnext
                        from resources.lib.modules import tvevening_session
                        tv_evening_gap = (
                            control.window.getProperty('midnight.tvevening.block_binge') == 'true'
                            or tvevening_session.is_gap_active()
                        )
                        if tv_evening_gap:
                            if c.devmode:
                                c.log('[CrewMonitor-Worker] Skipping UpNext autoplay fallback — TV Evening gap active')
                        elif not upnext.is_binge_allowed():
                            if c.devmode:
                                c.log('[CrewMonitor-Worker] Skipping UpNext autoplay fallback — binge disarmed/cancelled')
                        elif (not is_movie and upnext_triggered and
                                control.window.getProperty(upnext.UPNEXT_DIALOG_PENDING_PROP) == 'true'):
                            c.log('[CrewMonitor-Worker] UpNext still pending at episode end — autoplay fallback')
                            control.window.clearProperty(upnext.UPNEXT_DIALOG_PENDING_PROP)
                            control.window.clearProperty(upnext.UPNEXT_DIALOG_PROP)
                            if upnext.is_enabled() and upnext.get_autoplay():
                                next_ep = upnext.get_next_episode(
                                    imdb, tmdb, int(season), int(episode),
                                    tvshowtitle=title, year=year
                                )
                                if next_ep:
                                    c.log(f"[CrewMonitor-Worker] Autoplay fallback: S{next_ep['season']:02d}E{next_ep['episode']:02d}")
                                    upnext.play_next_episode(next_ep)
                    except Exception as e:
                        c.log(f'[CrewMonitor-Worker] UpNext fallback error: {e}')

                    from resources.lib.modules import upnext as _upnext_clear
                    _upnext_clear.clear_upnext_state('episode session finished')
                    if c.devmode:
                        c.log(f"[CrewMonitor-Worker] Episode {episodes_handled} finished")

                else:
                    # Check for widget refresh requests from background populate threads
                    # (they can't call executebuiltin safely from a non-service thread).
                    if control.window.getProperty('crew.widget.needs_refresh'):
                        refresh_url = control.window.getProperty('crew.widget.refresh_url') or ''
                        control.window.clearProperty('crew.widget.needs_refresh')
                        control.window.clearProperty('crew.widget.refresh_url')
                        control.window.setProperty('crew.widget.context', '1')
                        if refresh_url:
                            c.log(f"[CrewMonitor-Worker] Widget refresh requested - calling Container.Refresh({refresh_url})")
                            xbmc.executebuiltin(f'Container.Refresh({refresh_url})')
                        else:
                            c.log("[CrewMonitor-Worker] Widget refresh requested - calling Container.Refresh")
                            xbmc.executebuiltin('Container.Refresh')
                    if _worker_shutdown_event.wait(1.0):
                        break

            except Exception as e:
                c.log(f"[CrewMonitor-Worker] ✗ Error: {e}")
                if c.devmode:
                    c.log(f"[CrewMonitor-Worker] Traceback: {traceback.format_exc()}")
                if _worker_shutdown_event.wait(1.0):
                    break

        c.log(f"[CrewMonitor-Worker] Shutdown (handled {episodes_handled} episodes)")

    def __del__(self):
        c.log('[CrewMonitor] Instance being deleted (module reload)')
        if self.crew_player:
            del self.crew_player
        del self

    def startServices(self):
        xbmc.log('[script.module.midnight][CrewMonitor] Service Starting', 2)
        global _trakt_monitor_instance
        if simkl.use_trakt_tracker():
            _trakt_monitor_instance = TraktMonitor()
            Thread(target=_trakt_monitor_instance.run, name='CrewTraktMonitor', daemon=True).start()
        else:
            _trakt_monitor_instance = None
            c.log(
                '[CrewMonitor] TraktMonitor not started — tracker.service is Simkl-only '
                '(no Trakt scrobble queue / token refresh / scheduled sync)',
                1,
            )

        # Start the persistent monitoring worker thread only once per Kodi session.
        if not hasattr(queue, '_midnight_worker_started'):
            worker_thread = threading.Thread(
                target=self._persistent_monitoring_worker,
                name="CrewMonitor-Worker",
                daemon=False  # Must survive module reloads
            )
            worker_thread.start()
            setattr(queue, '_midnight_worker_started', True)
            if c.devmode:
                c.log("[CrewMonitor] Started persistent monitoring worker")
        else:
            if c.devmode:
                c.log("[CrewMonitor] Worker thread already running")

        try:
            from resources.lib.modules.player import player as crewPlayer, register_global_player
            self.crew_player = crewPlayer()
            register_global_player(self.crew_player)
            xbmc.log('[script.module.midnight][CrewMonitor] Global crewPlayer instance created and registered', xbmc.LOGINFO)
        except Exception as e:
            xbmc.log(f'[script.module.midnight][CrewMonitor] Error creating global crewPlayer: {e}', xbmc.LOGERROR)
            xbmc.log(traceback.format_exc(), xbmc.LOGERROR)


def start():
    """Entry point called by plugin.video.midnight/service.py."""
    try:
        segmentproxy.start()
    except Exception as e:
        c.log(f'[crewservice] segmentproxy start skipped: {e}', 1)

    try:
        from .version_announcement import mark_boot_started
        mark_boot_started()
    except Exception:
        pass

    # Arm boot quiet once — Trakt API deferral only, never tied to UI dialogs.
    try:
        if simkl.use_trakt_tracker():
            trakt.arm_trakt_boot_quiet()
        else:
            c.log('[crewservice] Skipping Trakt boot quiet — tracker.service is Simkl-only', 1)
    except Exception:
        pass

    # Boot tasks first (settings repair, etc.) — library thread used to race ahead
    # and die on empty/non-plugin sys.argv during parallel plugin traffic.
    main()

    # Library update used to RunPlugin(action=service) → libepisodes.service()
    # which is an infinite loop holding a plugin invoker for the whole session.
    # On quit, Kodi waited on that invoker ("Stopping all" hang / crash feel).
    # Run it as a daemon from the real xbmc.service instead — after main().
    def _library_service_thread():
        try:
            c.log('[crewservice] Starting CrewLibraryService thread')
            from .libtools import libepisodes
            libepisodes().service()
        except Exception as e:
            c.log(f'[crewservice] Library service thread exited: {e}', 1)
            c.log(traceback.format_exc(), 1)

    threading.Thread(
        target=_library_service_thread,
        name='CrewLibraryService',
        daemon=True,
    ).start()

    monitor = CrewMonitor.get_or_create_instance()

    # Service loop: dispatch dialogs queued by background workers (UpNext, TV Evening).
    # RunPlugin does not run during fullscreen playback, so workers only set
    # window properties and this loop calls doModal() from the service context.
    c.log('[CrewMonitor] Service loop started (dialog dispatch)')
    try:
        while not monitor.waitForAbort(0.5):
            try:
                from resources.lib.modules import upnext
                from resources.lib.modules import tvevening_countdown
                from resources.lib.modules import tvevening_complete
                upnext.dispatch_pending_upnext_dialog()
                tvevening_countdown.dispatch_pending_countdown_dialog()
                tvevening_complete.dispatch_pending_completion_dialog()
            except Exception as e:
                c.log(f'[CrewMonitor] TV Evening / UpNext dispatch error: {e}')
    finally:
        # Non-daemon CrewMonitor-Worker blocks Python invoker shutdown unless signaled.
        _worker_shutdown_event.set()
        c.log('[CrewMonitor] Service loop stopped — worker shutdown signaled')

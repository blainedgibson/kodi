# -*- coding: utf-8 -*-

'''
********************************************************cm*
* Midnight Add-on
*
* @file upgrade_health_check.py
* @package script.module.midnight
*
* Post-install health check: verify settings, databases, and that addon
* files unpacked correctly (e.g. artwork PNG/XML counts). Runs once per
* module version at startup; retries next boot if critical checks fail.
*
* @copyright (c) 2026, Midnight
* @license GNU General Public License, version 3 (GPL-3.0)
*
********************************************************cm*
'''

from __future__ import annotations

import json
import os
import sqlite3 as database
import time
import traceback

import xbmcaddon
import xbmcgui

from . import control
from .crewruntime import c
from .health_check_versions import (
    CREW_ADDON_IDS,
    format_version_mismatch_note,
    module_missing_reason,
)

# Installed artwork must match what the repo zip ships — Kodi often leaves hollow folders on update.
ARTWORK_MIN_MODERN_PNG = 170
ARTWORK_MIN_MODERN_1080I_XML = 18
ARTWORK_SENTINEL_FILES = (
    'resources/media/modern/main_movies.png',
    'resources/skins/modern/1080i/ScraperStatus.xml',
)


def _installed_crew_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for addon_id in CREW_ADDON_IDS:
        try:
            versions[addon_id] = xbmcaddon.Addon(addon_id).getAddonInfo('version') or ''
        except Exception:
            versions[addon_id] = ''
    return versions


def _installed_module_version() -> str:
    return _installed_crew_versions().get('script.module.midnight', '')


def _health_check_version() -> str:
    """Suite identity follows installed module version (addon.xml) — no manual constant."""
    return _installed_module_version()


def _version_gate_ok(log=None) -> tuple[bool, str | None]:
    """Only hard-fail when the module is missing — mismatched plugin/artwork versions are OK."""
    reason = module_missing_reason(_installed_crew_versions())
    if reason:
        return False, reason
    return True, None


def _log_version_notes(log=None) -> None:
    note = format_version_mismatch_note(_installed_crew_versions())
    if note:
        _log(f'Note — {note}', log)


def _marker_path() -> str:
    control.makeFile(control.dataPath)
    return os.path.join(control.dataPath, f'health_check_{_health_check_version()}.done')


def needs_health_check() -> bool:
    """True until this module version's suite has completed (always in devmode)."""
    gate_ok, _reason = _version_gate_ok()
    if not gate_ok:
        return False
    if c.devmode:
        return True
    return not os.path.isfile(_marker_path())


def _log(msg: str, log=None) -> None:
    line = f'[HealthCheck] {msg}'
    if log:
        log(line)
    else:
        c.log(line)


def _notify(title: str, message: str, duration: int = 5000) -> None:
    try:
        from . import version_announcement
        if version_announcement.suppress_startup_ui():
            _log(f'Notify suppressed (builders): {title} — {message}')
            return
        # Kodi toast notifications are single-line; collapse any embedded newlines.
        message = ' '.join(message.split())
        xbmcgui.Dialog().notification(title, message, xbmcgui.NOTIFICATION_INFO, duration)
    except Exception:
        pass


def _clean_repair_names(repairs: list[str]) -> list[str]:
    """Drop empty / whitespace-only entries so we never claim a phantom count."""
    return [str(r).strip() for r in (repairs or []) if r and str(r).strip()]


def _repairs_notify_message(elapsed: float, repairs: list[str]) -> str | None:
    """
    Toast text that names what was repaired, or None when there is nothing to claim.
    """
    names = _clean_repair_names(repairs)
    if not names:
        return None
    detail = '; '.join(names)
    # Keep toast readable; full list is always in the_crew.log.
    max_detail = 140
    if len(detail) > max_detail:
        detail = detail[: max_detail - 3].rstrip() + '...'
    if len(names) == 1:
        return f'Health check repaired ({elapsed:.1f}s): {detail}'
    return f'Health check ({elapsed:.1f}s) - {len(names)} repairs: {detail}'


def _set_active(active: bool) -> None:
    try:
        prop = 'midnight.health_check.active'
        if active:
            control.window.setProperty(prop, '1')
        else:
            control.window.clearProperty(prop)
    except Exception:
        pass


def _repair_settings(log=None) -> tuple[list[str], int]:
    """Run settings upgrade repair. Returns (named repairs, merged count)."""
    from .settings_repair import repair_user_settings_for_upgrade
    summary = repair_user_settings_for_upgrade(log=log, sync_kodi=False)
    names: list[str] = []
    if summary.get('imported'):
        names.append('settings imported from profile backup')
    if summary.get('repaired'):
        names.append('settings.xml repaired')
    merged = int(summary.get('merged') or 0)
    if merged:
        names.append(f'merged {merged} setting value(s) from backup')
    return names, merged


def _verify_settings_loadable(log=None) -> tuple[bool, str | None]:
    """Settings must parse and contain a real userdata payload — not empty/wiped XML."""
    try:
        from .settings_repair import parse_userdata_settings, _userdata_path, _file_exists

        user_path = _userdata_path()
        if not _file_exists(user_path):
            return False, 'settings.xml missing'

        vals = parse_userdata_settings(user_path)
        if len(vals) < 20:
            return False, f'settings.xml nearly empty ({len(vals)} key(s))'

        addon = xbmcaddon.Addon('plugin.video.midnight')
        addon.getSetting('trakt.user')
        return True, None
    except Exception as exc:
        _log(f'Settings still not loadable after repair: {exc}', log)
        return False, str(exc)


def _verify_artwork_install(log=None) -> tuple[bool, str | None]:
    """Detect partial script.midnight.artwork installs (empty modern theme on Shield/Android)."""
    try:
        addon = xbmcaddon.Addon('script.midnight.artwork')
        base = addon.getAddonInfo('path')
        version = addon.getAddonInfo('version') or '?'
    except Exception as exc:
        return False, f'artwork addon not installed: {exc}'

    modern_dir = os.path.join(base, 'resources', 'media', 'modern')
    dialog_dir = os.path.join(base, 'resources', 'skins', 'modern', '1080i')

    png_count = 0
    if os.path.isdir(modern_dir):
        png_count = len([f for f in os.listdir(modern_dir) if f.lower().endswith('.png')])

    xml_count = 0
    if os.path.isdir(dialog_dir):
        xml_count = len([f for f in os.listdir(dialog_dir) if f.lower().endswith('.xml')])

    missing_sentinels = [
        rel for rel in ARTWORK_SENTINEL_FILES
        if not os.path.isfile(os.path.join(base, *rel.split('/')))
    ]

    ok = (
        png_count >= ARTWORK_MIN_MODERN_PNG
        and xml_count >= ARTWORK_MIN_MODERN_1080I_XML
        and not missing_sentinels
    )
    detail = (
        f'artwork v{version}: modern PNG={png_count} (need {ARTWORK_MIN_MODERN_PNG}+), '
        f'1080i XML={xml_count} (need {ARTWORK_MIN_MODERN_1080I_XML}+)'
    )
    if missing_sentinels:
        detail += f', missing: {", ".join(missing_sentinels)}'
    _log(detail if ok else f'INCOMPLETE artwork install — {detail}', log)
    if ok:
        return True, None
    return False, detail


def _ensure_table(db_path: str, create_sql: str, table_name: str, log=None) -> bool:
    if not db_path:
        return False
    created = False
    try:
        control.makeFile(control.dataPath)
        if not os.path.exists(db_path):
            _log(f'Creating database file: {os.path.basename(db_path)}', log)
        dbcon = database.connect(db_path)
        dbcur = dbcon.cursor()
        dbcur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        existed = dbcur.fetchone() is not None
        dbcur.execute(create_sql)
        if not existed:
            created = True
            _log(f'Created table {table_name} in {os.path.basename(db_path)}', log)
        dbcon.commit()
        dbcur.close()
        dbcon.close()
    except Exception as exc:
        _log(f'Table {table_name} on {db_path}: {exc}', log)
        raise
    return created


def _ensure_databases(log=None) -> list[str]:
    """Create or verify core SQLite schemas. Returns named repairs (empty if nothing changed)."""
    from . import trakt

    repairs: list[str] = []

    db_specs = [
        (control.metacacheFile, 'meta', '''
            CREATE TABLE IF NOT EXISTS meta (
                imdb TEXT, tmdb TEXT, tvdb TEXT, lang TEXT, user TEXT, item TEXT, time TEXT,
                UNIQUE(imdb, tmdb, tvdb, user, lang)
            )
        '''),
        (control.bookmarksFile, 'bookmarks', '''
            CREATE TABLE IF NOT EXISTS bookmarks (
                timeInSeconds TEXT, type TEXT, imdb TEXT, season TEXT, episode TEXT,
                playcount INTEGER, overlay INTEGER,
                UNIQUE(imdb, season, episode)
            )
        '''),
        (control.cacheFile, 'cache', '''
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY, value TEXT, date INTEGER, namespace TEXT,
                etag TEXT, last_modified TEXT
            )
        '''),
        (control.cacheFile, 'scraper_status', '''
            CREATE TABLE IF NOT EXISTS scraper_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scraper_name TEXT UNIQUE,
                category TEXT,
                module_name TEXT,
                accessible INTEGER,
                working INTEGER,
                disabled INTEGER,
                defunct INTEGER,
                pack_capable INTEGER,
                response_time REAL,
                error TEXT,
                last_tested INTEGER,
                user_disabled INTEGER DEFAULT 0
            )
        '''),
        (control.cacheFile, 'schema_migrations', '''
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY, applied_at INTEGER
            )
        '''),
    ]

    for db_path, table_name, create_sql in db_specs:
        if _ensure_table(db_path, create_sql, table_name, log=log):
            repairs.append(f'created table {table_name} in {os.path.basename(db_path)}')

    # Scraper user_disabled column (3.0.5)
    try:
        dbcon = database.connect(control.cacheFile)
        dbcur = dbcon.cursor()
        dbcur.execute('PRAGMA table_info(scraper_status)')
        columns = {row[1] for row in dbcur.fetchall()}
        if 'user_disabled' not in columns:
            dbcur.execute('ALTER TABLE scraper_status ADD COLUMN user_disabled INTEGER DEFAULT 0')
            dbcon.commit()
            repairs.append('added scraper_status.user_disabled column')
            _log('Added scraper_status.user_disabled column', log)
        dbcur.close()
        dbcon.close()
    except Exception as exc:
        _log(f'scraper_status column migrate: {exc}', log)

    # Cache v2 schema migration (namespace / etag columns)
    try:
        from . import cache as crew_cache
        with crew_cache._get_connection_cursor() as cursor:
            crew_cache._ensure_cache_table_schema(cursor)
    except Exception as exc:
        _log(f'cache schema migrate: {exc}', log)

    # Trakt tables + next_episode_cache
    try:
        trakt.init_next_episode_cache()
        _log('Trakt next_episode_cache initialized', log)
    except Exception as exc:
        _log(f'Trakt cache init: {exc}', log)

    # Broader trakt DB tables via startup maintenance helper
    try:
        from . import startup_maintenance
        maint = startup_maintenance.StartupMaintenance()
        maint._comprehensive_database_maintenance()
    except Exception as exc:
        _log(f'Comprehensive DB maintenance: {exc}', log)

    return repairs


def _migrate_scraper_settings(log=None) -> int:
    try:
        from .scraper_test import ScraperTester
        tester = ScraperTester()
        migrated = tester.migrate_legacy_provider_settings()
        if migrated:
            _log(f'Migrated {migrated} legacy provider.* disable(s)', log)
        return int(migrated or 0)
    except Exception as exc:
        _log(f'Scraper settings migrate: {exc}', log)
        return 0


def _warm_scraper_registry(log=None) -> None:
    try:
        from .scraper_registry import get_registry
        get_registry().load(force=True)
        _log('ScraperRegistry loaded', log)
    except Exception as exc:
        _log(f'ScraperRegistry warm: {exc}', log)


def _write_marker(summary: dict) -> None:
    with open(_marker_path(), 'w', encoding='utf-8') as fh:
        json.dump(summary, fh, indent=2)


def run_upgrade_health_check(log=None, notify: bool = True) -> bool:
    """
    Run the full 3.0.5 health-check suite.
    Returns True when completed without critical failure.
    """
    start = time.time()
    repairs = []
    errors = []
    critical = False
    artwork_ok = True
    _set_active(True)

    plugin_version = ''
    module_version = _installed_module_version()
    try:
        plugin_version = xbmcaddon.Addon('plugin.video.midnight').getAddonInfo('version')
    except Exception:
        pass

    gate_ok, gate_reason = _version_gate_ok(log)
    if not gate_ok:
        _log(f'Abort — {gate_reason}', log)
        _set_active(False)
        return False

    _log_version_notes(log)

    if plugin_version and plugin_version != module_version:
        _log(
            f'Note: plugin v{plugin_version} != module v{module_version}',
            log,
        )

    dev_label = ' [devmode]' if c.devmode else ''
    suite_version = _health_check_version()
    if notify:
        _notify(
            'Midnight',
            f'Running v{suite_version} health check{dev_label} — verifying settings and databases.',
            6000,
        )

    _log(
        f'Starting upgrade health check (module v{suite_version}, plugin {plugin_version}{dev_label})',
        log,
    )

    settings_merged = 0
    try:
        settings_repairs, settings_merged = _repair_settings(log)
        repairs.extend(settings_repairs)
    except Exception as exc:
        errors.append(f'settings repair: {exc}')
        _log(f'settings repair failed: {exc}', log)

    ok, err = _verify_settings_loadable(log)
    if not ok:
        from .settings_repair import (
            _file_exists,
            _userdata_path,
            health_backup_path,
            import_userdata_from_profile_backup,
            merge_user_values_from_backup,
            original_backup_path,
            restore_user_settings_from_backup,
        )
        user_path = _userdata_path()
        if import_userdata_from_profile_backup(user_path, log=log):
            ok, err = _verify_settings_loadable(log)
            if ok:
                repairs.append('settings imported from profile backup')
        if not ok:
            recovered = False
            for backup in (health_backup_path(user_path), original_backup_path(user_path)):
                if not _file_exists(backup):
                    continue
                merged_count = merge_user_values_from_backup(user_path, backup, log=log)
                if merged_count:
                    settings_merged += merged_count
                    recovered = True
            ok, err = _verify_settings_loadable(log)
            if not ok and not recovered:
                if restore_user_settings_from_backup(user_path, log=log):
                    ok, err = _verify_settings_loadable(log)
                    if ok:
                        _log('Settings recovered from .bak after merge retry failed', log)
                        repairs.append('settings restored from backup')
        if not ok:
            errors.append(f'settings not loadable: {err}')
            if notify:
                try:
                    control.okDialog(
                        'Midnight could not load your saved settings after repair.\n\n'
                        'Backups may exist as:\n'
                        '• settings.xml.health_backup_* in addon_data/plugin.video.midnight\n'
                        '• settings.xml.bak (original pre-repair snapshot)\n'
                        '• midnight_settings_backup/settings.xml next to your Kodi userdata folder\n\n'
                        'Open Midnight Settings once Kodi is ready, or reinstall from a profile backup.',
                        'Midnight — Settings Repair',
                    )
                except Exception:
                    pass

    artwork_ok, artwork_err = _verify_artwork_install(log)
    if not artwork_ok:
        errors.append(f'artwork incomplete: {artwork_err}')
        critical = True
        if notify:
            try:
                control.okDialog(
                    'Midnight artwork install is incomplete (missing menu icons and/or dialog skins).\n\n'
                    'This usually happens when Kodi updates the addon in-place instead of a full reinstall.\n\n'
                    'Fix: uninstall script.midnight.artwork, restart Kodi, then install it again from the repository.\n\n'
                    f'Details: {artwork_err}',
                    'Midnight — Artwork Repair Required',
                )
            except Exception:
                pass

    try:
        repairs.extend(_ensure_databases(log))
    except Exception as exc:
        errors.append(f'database repair: {exc}')
        _log(traceback.format_exc(), log)

    try:
        migrated = _migrate_scraper_settings(log)
        if migrated:
            repairs.append(f'migrated {migrated} scraper toggle(s)')
    except Exception as exc:
        errors.append(f'scraper migrate: {exc}')

    try:
        _warm_scraper_registry(log)
    except Exception as exc:
        errors.append(f'scraper registry: {exc}')

    repairs = _clean_repair_names(repairs)
    elapsed = time.time() - start
    summary = {
        'version': suite_version,
        'plugin_version': plugin_version,
        'module_version': module_version,
        'completed_at': int(time.time()),
        'elapsed_seconds': round(elapsed, 2),
        'repairs': repairs,
        'errors': errors,
        'settings_ok': ok,
        'artwork_ok': artwork_ok,
    }

    critical = critical or not ok
    if not critical:
        if not c.devmode:
            _write_marker(summary)
        else:
            _log('devmode: skipping health_check marker (runs every boot while dev pass active)', log)
        _log(
            f'Health check complete in {elapsed:.2f}s — '
            f'{len(repairs)} repair(s), {len(errors)} error(s)',
            log,
        )
        for name in repairs:
            _log(f'  repair: {name}', log)
        repair_msg = _repairs_notify_message(elapsed, repairs)
        if notify and repair_msg:
            _notify('Midnight', repair_msg, 6500)
        elif notify:
            _notify('Midnight', f'Health check complete ({elapsed:.1f}s). All good.', 3500)
    else:
        _log(f'Health check finished with critical errors in {elapsed:.2f}s — will retry next boot', log)

    _set_active(False)
    return not critical


def run_upgrade_health_check_if_needed(log=None, notify: bool = True) -> bool:
    """Run the suite once per install until the 3.0.5 marker exists (every boot in devmode)."""
    gate_ok, gate_reason = _version_gate_ok(log)
    if not gate_ok:
        _log(f'Skipped — {gate_reason}', log)
        try:
            if notify and control.window.getProperty('midnight.health_check.version_warn') != '1':
                control.window.setProperty('midnight.health_check.version_warn', '1')
                _notify(
                    'Midnight',
                    f'Health check skipped: {gate_reason}',
                    8000,
                )
        except Exception:
            pass
        return True

    if not needs_health_check():
        return True
    if c.devmode:
        _log('devmode active — running health check (ignores one-time marker)', log)
    return run_upgrade_health_check(log=log, notify=notify)

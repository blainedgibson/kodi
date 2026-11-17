# -*- coding: utf-8 -*-

'''
********************************************************cm*
* Midnight Add-on
*
* @file startup_maintenance.py
* @package script.module.midnight
*
* @copyright (c) 2026, Midnight
* @license GNU General Public License, version 3 (GPL-3.0)
*
********************************************************cm*
Comprehensive Startup Maintenance System
Runs on every Kodi startup to ensure addon health and readiness
'''

import json
import os
import time
import traceback
import sqlite3 as db
from contextlib import suppress
import xbmc
import xbmcaddon

from . import control
from .crewruntime import c
from . import http_client
from . import keys
from resources.lib.ui_theme import color



class StartupMaintenance:
    """
    Comprehensive startup maintenance system for Midnight addon.

    Handles:
    - Database integrity checks
    - Version tracking and updates
    - Cache cleanup
    - Stale data removal
    - API configuration updates
    """

    def __init__(self):
        """Initialize maintenance system."""
        self.maintenance_log = []
        self.errors = []

    def log(self, message, level='INFO'):
        """Log maintenance message."""
        full_msg = f"[Startup Maintenance] {message}"
        c.log(full_msg)
        self.maintenance_log.append(f"{level}: {message}")

    def log_error(self, message, exception=None):
        """Log maintenance error."""
        self.log(message, 'ERROR')
        self.errors.append(message)
        if exception:
            c.log(f"[Startup Maintenance] Exception: {exception}")
            c.log(f"[Startup Maintenance] Traceback: {traceback.format_exc()}")

    def run_all(self):
        """Run all maintenance tasks."""
        self.log("========================================")
        self.log("Starting comprehensive maintenance check")
        self.log("========================================")

        start_time = time.time()

        # Run maintenance tasks in order
        tasks = [
            ('Database Schema Validation', self._comprehensive_database_maintenance),
            ('Stale Sessions', self._clear_stale_sessions),
            ('TMDB Configuration', self._update_tmdb_config),
            ('Cache Cleanup', self._cleanup_old_caches),
            ('One-Time Cache Purge', self._one_time_cache_purge),
            ('CDN Modules', self._prewarm_cdn_modules),
            ('External Scraper Packs', self._sync_external_scraper_pack_settings),
            ('Changelog File', self._ensure_changelog_file),
            ('Version Tracking', self._track_versions),
            ('Database Integrity', self._check_database_integrity),
            ('Debrid Account Expiration', self._check_debrid_expiration),
            ('Trakt Authorization', self._check_trakt_auth),
        ]

        for task_name, task_func in tasks:
            try:
                self.log(f"Running: {task_name}")
                task_func()
                self.log(f"(OK) {task_name} completed")
            except Exception as e:
                self.log_error(f"(X) {task_name} failed: {e}", e)

        elapsed = time.time() - start_time
        self.log("========================================")
        self.log(f"Maintenance completed in {elapsed:.2f}s")
        if self.errors:
            self.log(f"Completed with {len(self.errors)} error(s)")
        else:
            self.log("All tasks completed successfully")
        self.log("========================================")

        # Version announcement runs early from crewservice.main() (after settings repair).
        try:
            self._show_trakt_startup_dialog_if_needed()
        except Exception as e:
            self.log_error(f"Trakt startup dialog failed: {e}", e)

        return len(self.errors) == 0

    def _clear_stale_sessions(self):
        """Preserve resumable TV Evening sessions; remove ended/orphan playlist data."""
        try:
            from . import tvevening_session

            result = tvevening_session.startup_check()
            if result == 'preserved':
                self.log("TV Evening: resumable session preserved for recovery")
            elif result == 'cleared':
                self.log("TV Evening: non-resumable playlist data cleared")
            else:
                self.log("No TV Evening session data")

        except Exception as e:
            # TV Evening might not be initialized yet, that's OK
            self.log(f"TV Evening check skipped: {e}")

    def _update_tmdb_config(self):
        """Update TMDB image configuration cache if stale (> 7 days old)."""
        try:
            days = 7
            diff_time = (86400 * days)

            tmdb_user = control.setting('tm.personal_user') or control.setting('tm.user')
            if not tmdb_user:
                tmdb_user = keys.tmdb_key

            settings_table = 'settings'
            control.makeFile(control.dataPath)
            dbcon = db.connect(control.dbSettings)
            dbcur = dbcon.cursor()

            now = int(time.time())

            self._ensure_tmdb_settings_schema(dbcur, settings_table)

            # Check if TMDB config cache is missing or stale (> 7 days old)
            dbcur.execute(f"SELECT added FROM {settings_table} WHERE id = 1")
            row = dbcur.fetchone()
            try:
                is_stale = row is None or int(row[0] or 0) < (now - diff_time)
            except Exception:
                is_stale = True

            if is_stale:
                self.log("TMDB config cache is stale, fetching fresh data...")

                # Fetch fresh TMDB configuration
                url = f"https://api.themoviedb.org/3/configuration?api_key={tmdb_user}"
                result = http_client.tmdb_get_json(url, timeout=16) or {}

                images = result.get('images', {})
                s_base_url = images.get('secure_base_url', '')
                b_sizes = images.get('backdrop_sizes', [])
                b_sizes = b_sizes[-4:] if len(b_sizes) >= 4 else b_sizes
                l_sizes = images.get('logo_sizes', [])
                l_sizes = l_sizes[-4:] if len(l_sizes) >= 4 else l_sizes
                p_sizes = images.get('poster_sizes', [])
                p_sizes = p_sizes[-4:] if len(p_sizes) >= 4 else p_sizes
                pr_sizes = images.get('profile_sizes', [])
                pr_sizes = pr_sizes[-4:] if len(pr_sizes) >= 4 else pr_sizes
                s_sizes = images.get('still_sizes', [])
                s_sizes = s_sizes[-4:] if len(s_sizes) >= 4 else s_sizes

                # Store TMDB configuration with parameterized query
                dbcur.execute(
                    f"INSERT OR REPLACE INTO {settings_table} "
                    "(id, secure_base_url, backdrop_sizes, logo_sizes, poster_sizes, profile_sizes, still_sizes, added) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (1, s_base_url, json.dumps(b_sizes), json.dumps(l_sizes), json.dumps(p_sizes),
                        json.dumps(pr_sizes), json.dumps(s_sizes), now)
                )

                # Store user's fanart quality preference
                fanart_quality = control.setting('fanart.quality')
                if fanart_quality and fanart_quality.isdigit():
                    quality_idx = int(fanart_quality)
                    dbcur.execute(
                        f"INSERT OR REPLACE INTO {settings_table} "
                        "(id, secure_base_url, backdrop_sizes, logo_sizes, poster_sizes, profile_sizes, still_sizes, added) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (2, fanart_quality,
                        b_sizes[quality_idx] if quality_idx < len(b_sizes) else '',
                        l_sizes[quality_idx] if quality_idx < len(l_sizes) else '',
                        p_sizes[quality_idx] if quality_idx < len(p_sizes) else '',
                        pr_sizes[quality_idx] if quality_idx < len(pr_sizes) else '',
                        s_sizes[quality_idx] if quality_idx < len(s_sizes) else '',
                        now)
                    )

                self.log("TMDB configuration updated successfully")
            else:
                self.log("TMDB config cache is current (< 7 days old)")

            dbcon.commit()
            dbcon.close()

        except Exception as e:
            self.log_error(f"TMDB config update failed: {e}", e)

    def _ensure_tmdb_settings_schema(self, dbcur, settings_table='settings'):
        """Create or migrate the TMDB settings cache table."""
        expected_columns = {
            'id': 'INTEGER',
            'secure_base_url': 'TEXT',
            'backdrop_sizes': 'TEXT',
            'logo_sizes': 'TEXT',
            'poster_sizes': 'TEXT',
            'profile_sizes': 'TEXT',
            'still_sizes': 'TEXT',
            'added': 'TEXT',
        }

        dbcur.execute(
            f"CREATE TABLE IF NOT EXISTS {settings_table} ("
            "id INTEGER, secure_base_url TEXT, backdrop_sizes TEXT, logo_sizes TEXT, "
            "poster_sizes TEXT, profile_sizes TEXT, still_sizes TEXT, added TEXT, "
            "UNIQUE(id))"
        )

        dbcur.execute(f"PRAGMA table_info({settings_table})")
        existing_columns = {row[1] for row in dbcur.fetchall()}

        for column, column_type in expected_columns.items():
            if column not in existing_columns:
                self.log(f"Repairing TMDB settings table: adding missing column '{column}'")
                dbcur.execute(f"ALTER TABLE {settings_table} ADD COLUMN {column} {column_type}")

        dbcur.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{settings_table}_id ON {settings_table} (id)")

    def _cleanup_old_caches(self):
        """Clean up old cache files and temporary data."""
        try:
            cache_dir = control.dataPath
            if not os.path.exists(cache_dir):
                self.log("Cache directory doesn't exist, skipping cleanup")
                return

            # Clean up old subtitle files (older than 1 day)
            try:
                now = time.time()
                one_day = 86400
                removed_count = 0

                for filename in os.listdir(cache_dir):
                    if filename.startswith('TemporarySubs') and filename.endswith('.srt'):
                        filepath = os.path.join(cache_dir, filename)
                        if os.path.isfile(filepath):
                            file_age = now - os.path.getmtime(filepath)
                            if file_age > one_day:
                                os.remove(filepath)
                                removed_count += 1

                if removed_count > 0:
                    self.log(f"Removed {removed_count} old subtitle cache file(s)")
                else:
                    self.log("No old cache files to remove")

            except Exception as e:
                self.log(f"Cache cleanup warning: {e}")

        except Exception as e:
            self.log_error(f"Cache cleanup failed: {e}", e)

    def _prewarm_cdn_modules(self):
        """Pre-download CDN modules so they are ready before the user navigates to sports.

        Also removes any stale .py files left over from a previous caching strategy
        before attempting the fresh download — cdnImport will write new ones on success.
        """
        cdn_modules = [
            ('crewstreamer', 'https://raw.githubusercontent.com/posadka/xmls2/main/crewstreamer.xml'),
            ('crewschedule',  'https://raw.githubusercontent.com/posadka/xmls2/main/crewschedule.xml'),
            ('crewreplays',   'https://raw.githubusercontent.com/posadka/xmls2/main/crewreplays.xml'),
        ]

        # Remove stale cached .py files so cdnImport always writes a fresh copy.
        # With the current strategy (download-first, disk-as-fallback) this is mainly
        # housekeeping for files left by older TTL-based logic.
        for name, _ in cdn_modules:
            stale = os.path.join(control.dataPath, f'{name}.py')
            if os.path.isfile(stale):
                try:
                    os.remove(stale)
                except Exception:
                    pass

        ok, fail = [], []
        for name, uri in cdn_modules:
            try:
                control.cdnImport(uri, name)
                ok.append(name)
            except Exception as e:
                fail.append(name)
                self.log(f'CDN pre-warm failed for {name}: {e}')

        if ok:
            self.log(f'CDN pre-warmed: {", ".join(ok)}')
        if fail:
            self.log(f'CDN unavailable (will use disk fallback if present): {", ".join(fail)}')

    def _comprehensive_database_maintenance(self):
        """
        Validate and auto-repair all database schemas.
        Creates missing tables, verifies integrity, logs actions taken.
        """
        try:
            from . import trakt

            self.log("========================================")
            self.log("Starting comprehensive database maintenance...")
            self.log("========================================")

            repairs_made = []

            # Define all expected tables per database
            database_schemas = {
                'traktsync.db': {
                    'file': control.traktsyncFile,
                    'tables': {
                        'movies_collection': trakt.sql_dict.get('sql_create_movies_collection'),
                        'shows_collection': trakt.sql_dict.get('sql_create_shows_collection'),
                        'seasons_collection': trakt.sql_dict.get('sql_create_seasons_collection'),
                        'progress': trakt.sql_dict.get('sql_create_trakt_progress'),
                        'service': trakt.sql_dict.get('sql_create_service'),
                        'sync_data': trakt.sql_dict.get('sql_create_sync_data'),
                        'watched': trakt.sql_dict.get('sql_create_trakt_watched'),
                        'scrobble_queue': trakt.sql_dict.get('sql_create_scrobble_queue'),
                        'next_episode_cache': trakt.sql_dict.get('sql_create_next_episode_cache'),
                    }
                },
                'metacache.db': {
                    'file': control.metacacheFile,
                    'tables': {
                        'meta': '''CREATE TABLE IF NOT EXISTS meta (
                            imdb TEXT, tmdb TEXT, tvdb TEXT, lang TEXT, user TEXT, item TEXT, time TEXT,
                            UNIQUE(imdb, tmdb, tvdb, user, lang)
                        )'''
                    }
                },
                'cache.db': {
                    'file': control.cacheFile,
                    'tables': {
                        'cache': '''CREATE TABLE IF NOT EXISTS cache (
                            key TEXT PRIMARY KEY, value TEXT, date INTEGER, namespace TEXT,
                            etag TEXT, last_modified TEXT
                        )''',
                        'scraper_status': '''CREATE TABLE IF NOT EXISTS scraper_status (
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
                        )''',
                        'backdrop_cache': '''CREATE TABLE IF NOT EXISTS backdrop_cache (
                            imdb TEXT PRIMARY KEY,
                            tmdb TEXT,
                            tvdb TEXT,
                            backdrop TEXT,
                            cached_at INTEGER
                        )''',
                        'schema_migrations': 'CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at INTEGER)'
                    }
                },
                'providercache.db': {
                    'file': control.providercacheFile,
                    'tables': {
                        'rel_url': '''CREATE TABLE IF NOT EXISTS rel_url (
                            source TEXT,
                            imdb_id TEXT,
                            season TEXT,
                            episode TEXT,
                            rel_url TEXT,
                            UNIQUE(source, imdb_id, season, episode)
                        )''',
                        'rel_src': '''CREATE TABLE IF NOT EXISTS rel_src (
                            source TEXT,
                            imdb_id TEXT,
                            season TEXT,
                            episode TEXT,
                            hosts TEXT,
                            added TEXT,
                            UNIQUE(source, imdb_id, season, episode)
                        )''',
                        'source_cache': '''CREATE TABLE IF NOT EXISTS source_cache (
                            cache_key TEXT PRIMARY KEY,
                            imdb_id TEXT,
                            tmdb_id TEXT,
                            season TEXT,
                            episode TEXT,
                            sources_json TEXT,
                            cached_at INTEGER,
                            UNIQUE(cache_key)
                        )'''
                    }
                }
            }

            # Process each database
            for db_name, db_config in database_schemas.items():
                db_file = db_config['file']

                # Skip if database file doesn't exist yet
                if not os.path.exists(db_file):
                    self.log(f"Database {db_name} doesn't exist yet, will be created on first use")
                    continue

                try:
                    # Check integrity
                    dbcon = db.connect(db_file)
                    dbcur = dbcon.cursor()

                    # Quick integrity check
                    dbcur.execute("PRAGMA integrity_check")
                    integrity_result = dbcur.fetchone()
                    if integrity_result and integrity_result[0] != 'ok':
                        self.log(f"WARNING: {db_name} integrity check failed: {integrity_result[0]}")
                        repairs_made.append(f"{db_name}: Integrity issue detected")

                    # Get existing tables
                    dbcur.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    existing_tables = {row[0] for row in dbcur.fetchall()}

                    # Check and create missing tables
                    for table_name, create_sql in db_config['tables'].items():
                        if table_name not in existing_tables:
                            self.log(f"Creating missing table: {db_name}/{table_name}")
                            dbcur.execute(create_sql)
                            repairs_made.append(f"{db_name}: Created table '{table_name}'")
                        else:
                            # Table exists - could add column validation here in future
                            pass

                    dbcon.commit()
                    dbcur.close()
                    dbcon.close()

                except Exception as e:
                    self.log(f"Error checking {db_name}: {e}")
                    repairs_made.append(f"{db_name}: Error during check - {str(e)[:100]}")

            # Summary
            self.log("========================================")
            if repairs_made:
                self.log(f"Database maintenance completed: {len(repairs_made)} action(s) taken")
                for repair in repairs_made:
                    self.log(f"  - {repair}")
            else:
                self.log("Database maintenance completed: All schemas valid, no repairs needed")
            self.log("========================================")

        except Exception as e:
            self.log_error(f"Comprehensive database maintenance failed: {e}", e)

    def _track_versions(self):
        """Track current addon versions for monitoring."""
        try:
            module_version = xbmcaddon.Addon('script.module.midnight').getAddonInfo('version')
            plugin_version = xbmcaddon.Addon('plugin.video.midnight').getAddonInfo('version')

            # Store in settings for reference
            xbmcaddon.Addon('plugin.video.midnight').setSetting('module_base', module_version)
            xbmcaddon.Addon('plugin.video.midnight').setSetting('plugin_base', plugin_version)

            self.log(f"Plugin version: {plugin_version}")
            self.log(f"Module version: {module_version}")

        except Exception as e:
            self.log_error(f"Version tracking failed: {e}", e)

    def _check_database_integrity(self):
        """Run basic database integrity checks."""
        try:
            # Check main databases exist and are accessible
            databases = [
                ('Settings DB', control.dbSettings),
                ('Meta DB', c.get_path('meta')),
                ('Bookmarks DB', control.bookmarksFile),
            ]

            for db_name, db_path in databases:
                if os.path.exists(db_path):
                    # Try to open and query the database
                    try:
                        dbcon = db.connect(db_path)
                        dbcur = dbcon.cursor()
                        dbcur.execute("SELECT name FROM sqlite_master WHERE type='table'")
                        tables = dbcur.fetchall()
                        dbcon.close()
                        self.log(f"{db_name}: OK ({len(tables)} tables)")
                    except Exception as e:
                        self.log(f"{db_name}: Warning - {e}")
                else:
                    self.log(f"{db_name}: Not yet created")

        except Exception as e:
            self.log_error(f"Database integrity check failed: {e}", e)

    _DEBRID_TOAST_HEADING = (
        f'{color("Midnight", 'header')} - {color("Debrid manager", 'header')}'
    )

    def _crew_toast_icon(self):
        """Resolve a notification icon path Kodi will actually display (not NOTIFICATION_INFO)."""
        import xbmcaddon
        import xbmcvfs

        candidates = []
        for addon_id in ('plugin.video.midnight', 'script.midnight.artwork'):
            try:
                raw = xbmcaddon.Addon(addon_id).getAddonInfo('icon') or ''
            except Exception:
                raw = ''
            if raw:
                candidates.append(xbmcvfs.translatePath(raw))

        art_icon = c.addon_icon()
        if art_icon:
            candidates.append(xbmcvfs.translatePath(art_icon))

        for path in candidates:
            if path and xbmcvfs.exists(path):
                return path
        return ''

    def _notify_debrid_subscription(self, message, duration_ms=8000):
        """Midnight-branded toast: explicit icon path + orchid/orange heading, single-line body."""
        try:
            from . import version_announcement
            if version_announcement.suppress_startup_ui():
                self.log(f'Debrid notify suppressed (builders kill switch): {message}')
                return
            icon = self._crew_toast_icon()
            control.dialog.notification(
                self._DEBRID_TOAST_HEADING,
                (message or '').replace('\n', ' ').strip(),
                icon=icon,
                time=duration_ms,
                sound=False,
            )
        except Exception as exc:
            self.log(f'Debrid subscription notify failed: {exc}')

    def _check_debrid_expiration(self):
        """
        Check debrid account expirations and notify user if any are expiring soon.
        Checks all configured debrid services and shows notification for accounts
        expiring within configured threshold.
        """
        try:
            # Import debrid_manager here to avoid circular imports
            from . import debrid_manager
            from datetime import datetime

            # Configurable threshold in days (can be made a setting later)
            warning_threshold = 30  # Show warning if expiring within 30 days

            manager = debrid_manager.DebridAccountManager()
            expiring_soon = []
            expired = []

            # Check each service
            services = {
                'Real-Debrid': manager.get_realdebrid_info(),
                'AllDebrid': manager.get_alldebrid_info(),
                'Premiumize': manager.get_premiumize_info(),
                'Orion': manager.get_orion_info()
            }

            for service_name, info in services.items():
                if not info or not info.get('active'):
                    continue

                expiration = info.get('expiration')
                if not expiration or expiration == 'N/A':
                    continue

                # Calculate days until expiration
                try:
                    # Convert Unix timestamp to datetime
                    if isinstance(expiration, (int, float)):
                        exp_date = datetime.fromtimestamp(expiration)
                    else:
                        # Try parsing as ISO date string
                        exp_date = datetime.fromisoformat(str(expiration).replace('Z', '+00:00'))

                    now = datetime.now()
                    days_left = (exp_date - now).days

                    self.log(f"{service_name}: {days_left} days until expiration")

                    if days_left < 0:
                        expired.append(service_name)
                    elif days_left <= warning_threshold:
                        expiring_soon.append((service_name, days_left))

                except Exception as e:
                    self.log(f"Unable to parse expiration for {service_name}: {e}")

            # Show notification if any accounts are expiring soon or expired
            if expired:
                message = f"{'|'.join(expired)} account(s) have EXPIRED!"
                self._notify_debrid_subscription(message, duration_ms=10000)
                self.log(f'ALERT: {message}')

            elif expiring_soon:
                # Find account expiring soonest
                service_name, days_left = min(expiring_soon, key=lambda x: x[1])

                if days_left <= 7:
                    message = f'{service_name} expires in {days_left} day(s)!'
                    self._notify_debrid_subscription(message)
                    self.log(f'WARNING: {message}')
                else:
                    # Info notification - within threshold but not critical
                    message = f"{service_name} expires in {days_left} days"
                    self.log(f"INFO: {message}")
                    # Don't show notification for > 7 days to avoid annoying users daily

            else:
                self.log("All debrid accounts have valid subscriptions (> 30 days or no expiration data)")

        except Exception as e:
            # Don't fail maintenance if debrid check has issues
            self.log(f"Debrid expiration check skipped: {e}")

    def _check_trakt_auth(self):
        """Probe Trakt auth at startup; defer user dialogs until after version announcement."""
        try:
            from . import trakt

            if not trakt.get_trakt_credentials_info():
                self.log("Trakt not configured — skipping health check")
                return

            self.log("Running Trakt health check...")
            status = trakt.check_trakt_health(force=True)
            self.log(f"Trakt health check result: {status}")

            if status in ('no_network', 'locked', 'auth_dead'):
                control.window.setProperty('midnight.trakt_startup_status', status)

        except Exception as e:
            self.log(f"Trakt health check skipped: {e}")

    def _show_trakt_startup_dialog_if_needed(self):
        """One Trakt notice after maintenance — never interrupt mid-playback or late boot."""
        try:
            import xbmc as _xbmc

            if _xbmc.Player().isPlaying():
                return

            from . import version_announcement
            ok, skip_reason = version_announcement.boot_interrupt_ok()
            if not ok:
                c.log(f'[Startup Maintenance] Trakt startup dialog skipped ({skip_reason})')
                return

            from . import trakt

            status = control.window.getProperty('midnight.trakt_startup_status') or ''
            if not status and control.window.getProperty('midnight.trakt_auth_pending') == 'true':
                status = 'auth_dead'

            if not status:
                return

            if control.window.getProperty('midnight.trakt_startup_notified') == 'true':
                return

            control.window.setProperty('midnight.trakt_startup_notified', 'true')
            control.window.clearProperty('midnight.trakt_startup_status')
            control.window.clearProperty('midnight.trakt_auth_pending')

            if status == 'no_network':
                control.infoDialog('Trakt could not be reached. Check your internet connection.')
            elif status == 'locked':
                # Never okDialog from service/startup — it overlays the whole Kodi UI.
                control.infoDialog(
                    'Trakt account locked - unlock at trakt.tv, then re-authorize in Midnight settings',
                    time=7000,
                )
            elif status == 'auth_dead':
                control.infoDialog(
                    'Trakt session expired - re-authorize in Midnight > Settings > Accounts > Trakt',
                    time=7000,
                )
        except Exception as e:
            self.log(f"Trakt startup dialog skipped: {e}")

    def _one_time_cache_purge(self):
        """Version-gated one-time purge of poisoned empty cache entries.

        Pre-3.0.9.2 bugs caused two types of cache poisoning:
          1. cache.get() stored empty Trakt lists (auth dead / network error)
          2. cache.get_with_etag() fetched TMDB Disney+ with a literal '%s' URL,
             got HTTP 200 / empty results, and cached that forever.

        This runs once per version bump (flag stored in plugin settings) and
        removes all empty-value entries so they are re-fetched cleanly.
        """
        PURGE_VERSION = '3.0.9.5'
        FLAG_SETTING  = 'cache.purge.version'

        try:
            last_purge = control.setting(FLAG_SETTING) or ''
            if last_purge == PURGE_VERSION:
                self.log(f'One-time cache purge already done for v{PURGE_VERSION} — skipping')
                return

            cache_file = control.cacheFile
            if not os.path.exists(cache_file):
                self.log('cache.db not found — marking purge complete')
                control.setSetting(FLAG_SETTING, PURGE_VERSION)
                return

            dbcon = db.connect(cache_file)
            dbcur = dbcon.cursor()

            # 1. Remove entries whose value is an empty list/dict/null —
            #    these are the poisoned results from pre-fix cache behaviour.
            dbcur.execute(
                "DELETE FROM cache WHERE value IN ('[]', '{}', 'null', '')"
                " OR value LIKE '%\"results\":[]%'"
                " OR value LIKE '%\"results\": []%'"
            )
            n_empty = dbcur.rowcount

            # 2. Remove the specific Disney+ entry that was cached with a
            #    literal '%s' in the key (with_watch_providers=%s URL).
            #    In SQLite LIKE, '%' is the wildcard so escape it with ESCAPE '\'.
            dbcur.execute(
                r"DELETE FROM cache WHERE key LIKE '%with\_watch\_providers=\%s%' ESCAPE '\'"
            )
            n_disney = dbcur.rowcount

            # 3. Drop Trakt watched-show payloads cached without season/episode detail
            #    (batch sync used /sync/watched/shows without extended=progress).
            dbcur.execute(
                "DELETE FROM cache WHERE namespace='trakt' AND ("
                "key='trakt_watched_shows_extended' OR key LIKE 'trakt_next_episodes%'"
                ")"
            )
            n_trakt = dbcur.rowcount

            dbcon.commit()
            dbcon.close()

            for setting_key in ('trakt.shows.last_modified', 'trakt.shows.etag'):
                with suppress(Exception):
                    control.setSetting(setting_key, '')

            control.setSetting(FLAG_SETTING, PURGE_VERSION)
            self.log(
                f'One-time cache purge complete (v{PURGE_VERSION}): '
                f'{n_empty} empty entries + {n_disney} Disney+ poisoned + '
                f'{n_trakt} Trakt watched-show entries removed'
            )

        except Exception as e:
            self.log_error(f'One-time cache purge failed: {e}', e)

    def _sync_external_scraper_pack_settings(self):
        """Turn off pack master toggles when the scraper module is not installed."""
        try:
            from .pack_client import install_pack_client_shims
            install_pack_client_shims()
            self.log('External pack client shims ready (Midnight client)')
        except Exception as e:
            self.log(f'External pack client shims skipped: {e}')

        packs = (
            ('script.module.cocoscrapers', 'cocoscrapers.enabled', 'CocoScrapers'),
            ('script.module.gearsscrapers', 'gearsscrapers.enabled', 'GearsScrapers'),
            ('script.module.viperscrapers', 'viperscrapers.enabled', 'ViperScrapers'),
        )
        for addon_id, setting_id, label in packs:
            if xbmc.getCondVisibility(f'System.HasAddon({addon_id})'):
                self.log(f'{label}: {addon_id} installed')
                continue
            current = control.setting(setting_id) or ''
            if current.lower() in ('false', '0', ''):
                self.log(f'{label}: not installed (toggle already off)')
                continue
            control.setSetting(setting_id, 'false')
            self.log(f'{label}: not installed — disabled {setting_id}')

    def _ensure_changelog_file(self):
        """Create a minimal changelog.txt when missing (e.g. incomplete install zip)."""
        from datetime import datetime

        from .crewruntime import crew_file_write

        try:
            module_addon = xbmcaddon.Addon('script.module.midnight')
            module_path = module_addon.getAddonInfo('path')
            version = module_addon.getAddonInfo('version')
        except Exception as exc:
            self.log(f'Changelog check skipped: cannot resolve module addon ({exc})')
            return

        changelog_path = os.path.join(module_path, 'changelog.txt')
        if os.path.isfile(changelog_path):
            self.log('changelog.txt present')
            return

        month_year = datetime.now().strftime('%B %Y')
        content = (
            f'{color("[B]Midnight - Changelog[/B]", 'header')}\n'
            '\n'
            '=====================================================\n'
            f'Version {version} - "Missing changelog" ({month_year})\n'
            '=====================================================\n'
            f'- {color("Fix", 'header')} Changelog missing — replacement changelog inserted by startup maintenance.\n'
        )
        crew_file_write(changelog_path, content, mode='w')
        self.log(f'Created replacement changelog.txt (v{version})')

    def _show_version_announcement_if_needed(self):
        """
        Show eye-catching version announcement dialog if needed.

        Shows for:
        - New versions (once per version)
        - Alpha versions (once per Kodi session)
        """
        try:
            from . import version_announcement

            self.log("Checking if version announcement should be shown...")
            was_shown = version_announcement.check_and_show_version_announcement()

            if was_shown:
                self.log("Version announcement dialog was shown to user")
            else:
                self.log("No version announcement needed")

        except Exception as e:
            # Don't fail maintenance if announcement has issues
            self.log(f"Version announcement check skipped: {e}")


def run_startup_maintenance():
    """
    Main entry point for startup maintenance.
    Call this from service.py on Kodi startup.

    Returns:
        bool: True if all maintenance tasks succeeded, False otherwise
    """
    try:
        maintenance = StartupMaintenance()
        return maintenance.run_all()
    except Exception as e:
        c.log(f"[Startup Maintenance] CRITICAL ERROR: {e}")
        c.log(f"[Startup Maintenance] Traceback: {traceback.format_exc()}")
        return False


# Backwards compatibility - keep existing function name
def startupMaintenance():
    """Legacy function name for backwards compatibility."""
    return run_startup_maintenance()

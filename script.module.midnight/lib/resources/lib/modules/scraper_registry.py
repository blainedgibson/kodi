# -*- coding: utf-8 -*-

'''
 ***********************************************************
 * Midnight Add-on - Scraper Registry
 *
 * @file scraper_registry.py
 * @package script.module.midnight
 *
 * @copyright (c) 2026, Midnight
 * @license GNU General Public License, version 3 (GPL-3.0)
 *
 * Central enable/disable decisions backed by ScraperTester SQLite cache,
 * with fallback to legacy provider.* Kodi settings on cache miss.
 ***********************************************************
'''

import sqlite3 as database

from resources.lib.modules import control
from resources.lib.modules.crewruntime import c


class ScraperRegistry:
    """Load scraper enablement from scraper_status cache; fall back to provider.* settings."""

    _instance = None

    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._cache_file = control.cacheFile
        self._by_module = {}
        self._by_name = {}
        self._loaded = False
        self._has_user_disabled = False

    def invalidate(self):
        """Drop in-memory index so the next lookup reloads from SQLite."""
        self._loaded = False
        self._by_module.clear()
        self._by_name.clear()

    def load(self, force=False):
        """Read scraper_status rows into memory (no network tests)."""
        if self._loaded and not force:
            return

        self._by_module.clear()
        self._by_name.clear()
        self._has_user_disabled = False

        try:
            dbcon = database.connect(self._cache_file)
            dbcur = dbcon.cursor()
            dbcur.execute('SELECT * FROM scraper_status')
            rows = dbcur.fetchall()
            colnames = [d[0] for d in dbcur.description] if dbcur.description else []
            self._has_user_disabled = 'user_disabled' in colnames
            dbcur.close()
            dbcon.close()
        except Exception as exc:
            c.log(f'[ScraperRegistry] load failed: {exc}', 1)
            self._loaded = True
            return

        for row in rows:
            entry = self._row_to_entry(row, colnames)
            module_name = entry.get('module')
            name = entry.get('name')
            if module_name:
                self._by_module[module_name] = entry
            if name:
                self._by_name[name] = entry

        self._loaded = True
        c.log(f'[ScraperRegistry] loaded {len(self._by_module)} scraper(s) from cache')

    def _row_to_entry(self, row, colnames):
        """Map a sqlite row to a dict regardless of column order."""
        if colnames:
            data = dict(zip(colnames, row))
            return {
                'name': data.get('scraper_name'),
                'module': data.get('module_name'),
                'accessible': bool(data.get('accessible')),
                'working': bool(data.get('working')),
                'disabled': bool(data.get('disabled')),
                'defunct': bool(data.get('defunct')),
                'user_disabled': bool(data.get('user_disabled', 0)),
            }

        # Legacy column order from CREATE TABLE
        return {
            'name': row[1],
            'module': row[3],
            'accessible': bool(row[4]),
            'working': bool(row[5]),
            'disabled': bool(row[6]),
            'defunct': bool(row[7]),
            'user_disabled': False,
        }

    def get_entry(self, module_name):
        """Return cached status dict or None."""
        self.load()
        if not module_name:
            return None
        entry = self._by_module.get(module_name)
        if entry:
            return entry
        short = module_name.split('.')[-1]
        return self._by_name.get(short)

    def _provider_setting_enabled(self, module_name):
        """Legacy provider.<shortname> Kodi setting; default enabled."""
        short = module_name.split('.')[-1]
        setting = ''
        try:
            if callable(getattr(control, 'setting', None)):
                setting = control.setting('provider.' + short) or ''
        except Exception:
            setting = ''

        if not setting:
            try:
                import xbmcaddon
                addon_inst = xbmcaddon.Addon()
                if hasattr(addon_inst, 'getSetting'):
                    setting = addon_inst.getSetting(id='provider.' + short) or ''
            except Exception:
                pass

        if not setting:
            try:
                import sys as _sys
                for mod_key in ('modules.control', 'resources.lib.modules.control'):
                    alt = _sys.modules.get(mod_key)
                    if alt and callable(getattr(alt, 'setting', None)):
                        setting = alt.setting('provider.' + short) or ''
                        if setting:
                            break
            except Exception:
                pass

        if setting == 'false':
            return False
        return True

    def _scraper_obj_disabled(self, scraper_obj):
        if scraper_obj is None:
            return False
        return bool(getattr(scraper_obj, 'defunct', False) or getattr(scraper_obj, 'disabled', False))

    def is_enabled(self, module_name, scraper_obj=None):
        """
        Decide whether a scraper should run.

        Cache hit: off when disabled, defunct, user_disabled, or not accessible.
        Cache miss: optimistic unless scraper_obj is disabled/defunct, then provider.* fallback.
        """
        entry = self.get_entry(module_name)
        if entry:
            if entry.get('user_disabled'):
                return False
            if entry['defunct'] or entry['disabled']:
                return False
            tested = bool(
                entry.get('working')
                or entry.get('accessible')
                or (entry.get('error') not in (None, '', 'None'))
            )
            if tested and not entry.get('accessible'):
                return False
            return True

        if self._scraper_obj_disabled(scraper_obj):
            return False

        return self._provider_setting_enabled(module_name)

    def is_enabled_as_setting(self, module_name, scraper_obj=None):
        """String form expected by legacy sources filtering ('true' / 'false')."""
        return 'true' if self.is_enabled(module_name, scraper_obj=scraper_obj) else 'false'

    def set_user_disabled(self, module_name, disabled=True):
        """Persist user enable/disable for an internal or pack scraper (SQLite user_disabled)."""
        from resources.lib.modules.scraper_test import ScraperTester, is_user_toggle_eligible
        if not is_user_toggle_eligible(module_name):
            return False
        ok = ScraperTester().set_user_disabled(module_name, disabled)
        if ok:
            self.invalidate()
            self.load(force=True)
        return ok

    def toggle_user_disabled(self, module_name):
        """Flip user_disabled for an internal scraper; returns new disabled state."""
        entry = self.get_entry(module_name)
        current = bool(entry.get('user_disabled')) if entry else False
        new_state = not current
        if self.set_user_disabled(module_name, new_state):
            return new_state
        return current

    def auto_disable_blocked(self, status_data=None):
        """User-disable all blocked internal scrapers from cached test results."""
        from resources.lib.modules.scraper_test import ScraperTester
        count = ScraperTester().auto_disable_blocked_internal(status_data)
        if count:
            self.invalidate()
            self.load(force=True)
        return count


def get_registry():
    return ScraperRegistry.get()

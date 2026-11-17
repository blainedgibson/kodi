# -*- coding: utf-8 -*-

'''
********************************************************cm*
* Midnighty Add-on
*
* @file __init__.py
* @package script.module.midnight
*
* @copyright (c) 2025, Midnight
* @license GNU General Public License, version 3 (GPL-3.0)
*
********************************************************cm*
'''

import pkgutil
import os.path
import importlib.util
import importlib.abc
import sys
import traceback

from ..modules.crewruntime import c
from ..modules.pack_client import install_pack_client_shims

# Process-lifetime cache: each play used to re-import 70+ scraper modules (seconds).
_PROVIDER_CACHE = None
_PROVIDER_CACHE_KEY = None


def clear_provider_cache():
    """Drop cached provider instances (settings flip / pack enable)."""
    global _PROVIDER_CACHE, _PROVIDER_CACHE_KEY
    _PROVIDER_CACHE = None
    _PROVIDER_CACHE_KEY = None


def _provider_cache_key():
    try:
        return (
            c.get_setting('crewscrapers.enabled'),
            c.get_setting('cocoscrapers.enabled'),
            c.get_setting('gearsscrapers.enabled'),
            c.get_setting('viperscrapers.enabled'),
            c.get_setting('classyscrapers.enabled'),
        )
    except Exception:
        return None


def _load_sources_from_dir(src_dir, prefix=None):
    """
    Safely load .py modules from an arbitrary directory and call their source().
    Uses importlib.spec_from_file_location to avoid clashing module names.
    """
    results = []
    if not os.path.isdir(src_dir):
        return results

    # Add parent directories to sys.path for proper imports
    parent_dir = os.path.dirname(src_dir)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)

    for fname in os.listdir(src_dir):
        if not fname.endswith('.py') or fname == '__init__.py':
            continue

        module_basename = os.path.splitext(fname)[0]
        load_name = f"{prefix}.{module_basename}" if prefix else module_basename

        path = os.path.join(src_dir, fname)

        try:
            spec = importlib.util.spec_from_file_location(load_name, path)
            if spec is None or spec.loader is None:
                c.log(f'Could not load spec for "{path}": spec or loader is None', 1)
                continue
            module = importlib.util.module_from_spec(spec)
            try:
                # Preferred API
                spec.loader.exec_module(module)
            except AttributeError:
                # Fallback for legacy loaders that only implement load_module
                if hasattr(spec.loader, 'load_module'):
                    try:
                        module = spec.loader.load_module(load_name)
                    except Exception as e:
                        c.log(f'Could not load module "{path}" via load_module: {e}', 1)
                        continue
                else:
                    c.log(f'Loader for "{path}" does not support exec_module or load_module', 1)
                    continue
            # Prefix external pack sources to differentiate from crew sources
            source_name = f"{prefix}.{module_basename}" if prefix else module_basename

            # Only instantiate modules that expose a `source` factory
            if hasattr(module, 'source') and callable(getattr(module, 'source')):
                try:
                    results.append((source_name, module.source()))
                except Exception as e:
                    c.log(f'[Sources] Failed to instantiate source "{source_name}": {e}', 1)
            else:
                # Skip utility modules that do not implement a source() class/function
                c.log(f'[Sources] Skipping non-source module: {source_name}', 2)
            # c.log(f"[CM Debug @ 64 in __init__.py] appended sourcename = {source_name}")  # --- IGNORE ---
        except Exception as e:
            c.log(f'[Sources] Failed to load module "{path}": {e}', 1)
            c.log(f'[Sources] Traceback: {traceback.format_exc()}', 1)
            continue

    return results


def sources(force_reload=False):
    """
    Load scrapers from resources.lib.scrapers/ directory (single source of truth)
    and external addon packs (CocoScrapers, Gearsscrapers, Viperscrapers, ClassyScrapers).

    All legacy crew scrapers have been refactored to BaseScraper and moved
    to the scrapers/ directory for unified architecture.

    Results are cached for the Kodi process so cold re-imports do not block
    every play behind the scrape dialog.
    """
    global _PROVIDER_CACHE, _PROVIDER_CACHE_KEY

    cache_key = _provider_cache_key()
    if (
        not force_reload
        and _PROVIDER_CACHE is not None
        and cache_key is not None
        and cache_key == _PROVIDER_CACHE_KEY
    ):
        c.log(f'[Sources] Reusing cached provider list ({len(_PROVIDER_CACHE)} scrapers)')
        return list(_PROVIDER_CACHE)

    sources = []
    t_load = __import__('time').time()

    # External packs must use Midnight HTTP client (Xbox SSL, cfscrape, etc.)
    # before any cocoscrapers/gearsscrapers/viperscrapers/classyscrapers module imports client.
    try:
        install_pack_client_shims()
    except Exception as e:
        c.log(f'[Sources] Pack client shim install failed: {e}', 1)

    # Load refactored scrapers from resources.lib.scrapers/ directory
    # This is the single source of truth for all Midnight scrapers
    try:
        crew_enabled = c.get_setting('crewscrapers.enabled') != 'false'
        if not crew_enabled:
            c.log('[Sources] Built-in Midnight scrapers disabled in settings — skipping scrapers/')
        else:
            scrapers_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'scrapers')
            if os.path.isdir(scrapers_dir):
                pkg_prefix = "resources.lib.scrapers"
                loaded = _load_sources_from_dir(scrapers_dir, prefix=pkg_prefix)
                if loaded:
                    sources.extend(loaded)
                    try:
                        short_names = [ (n.split('.')[-1].replace('_', ' ').title() if isinstance(n, str) else str(n)) for n, _ in loaded[:8] ]
                        c.log(f'[Sources] Loaded {len(loaded)} scrapers from scrapers/: {short_names}')
                    except Exception:
                        c.log(f'[Sources] Loaded {len(loaded)} scrapers from scrapers/')
                else:
                    c.log('[Sources] No scrapers found in scrapers/', 2)
    except Exception as e:
        c.log(f'[Sources] Failed loading scrapers: {e}', 1)
        c.log(f'[Sources] Traceback: {traceback.format_exc()}', 1)

    c.log(f'[Sources] Midnight scrapers loaded: {len(sources)}')

    # Try to load CocoScrapers if installed and enabled
    try:
        import xbmc
        cocoscrapers_enabled = c.get_setting('cocoscrapers.enabled') == 'true'

        if xbmc.getCondVisibility('System.HasAddon(script.module.cocoscrapers)') and cocoscrapers_enabled:
            c.log('[Sources] CocoScrapers detected and enabled, attempting to load scrapers')

            # Discover addons directory
            cur = os.path.dirname(__file__)
            addons_dir = None
            for _ in range(6):
                if os.path.basename(cur).lower() == 'addons':
                    addons_dir = cur
                    break
                parent = os.path.dirname(cur)
                if parent == cur:
                    break
                cur = parent

            if addons_dir:
                cocos_base_dir = os.path.join(addons_dir, 'script.module.cocoscrapers', 'lib', 'cocoscrapers', 'sources_cocoscrapers')
                cocos_lib_dir = os.path.join(addons_dir, 'script.module.cocoscrapers', 'lib')

                # Add cocoscrapers lib to path for imports
                if cocos_lib_dir not in sys.path:
                    sys.path.insert(0, cocos_lib_dir)

                # Load from all source subdirectories (torrents, hosters, etc.)
                if os.path.isdir(cocos_base_dir):
                    for subdir in os.listdir(cocos_base_dir):
                        full_subdir_path = os.path.join(cocos_base_dir, subdir)
                        if os.path.isdir(full_subdir_path) and not subdir.startswith('_'):
                            try:
                                loaded = _load_sources_from_dir(full_subdir_path, prefix=f'cocoscrapers.{subdir}')
                                sources.extend(loaded)
                                c.log(f'[Sources] Loaded {len(loaded)} scrapers from cocoscrapers/{subdir}')
                            except Exception as e:
                                c.log(f'[Sources] Failed to load cocoscrapers/{subdir}: {e}', 1)
                else:
                    c.log('[Sources] CocoScrapers sources directory not found', 1)
        elif not cocoscrapers_enabled:
            c.log('[Sources] CocoScrapers installed but disabled in settings')
        else:
            c.log('[Sources] CocoScrapers not installed, using crew sources only')
    except Exception as e:
        c.log(f'[Sources] Error during CocoScrapers detection: {e}', 1)
        c.log(f'[Sources] Traceback: {traceback.format_exc()}', 1)

    c.log(f'[Sources] Total sources loaded: {len(sources)}')

    # Try to load Gearsscrapers if installed and enabled (same treatment as Coco/Viper — no host shims)
    try:
        import xbmc
        gearsscrapers_enabled = c.get_setting('gearsscrapers.enabled') == 'true'

        if xbmc.getCondVisibility('System.HasAddon(script.module.gearsscrapers)') and gearsscrapers_enabled:
            c.log('[Sources] Gearsscrapers detected and enabled, attempting to load scrapers')

            cur = os.path.dirname(__file__)
            addons_dir = None
            for _ in range(6):
                if os.path.basename(cur).lower() == 'addons':
                    addons_dir = cur
                    break
                parent = os.path.dirname(cur)
                if parent == cur:
                    break
                cur = parent

            if addons_dir:
                gear_base_dir = os.path.join(addons_dir, 'script.module.gearsscrapers', 'lib', 'gearsscrapers')
                gear_lib_dir = os.path.join(addons_dir, 'script.module.gearsscrapers', 'lib')
                if gear_lib_dir not in sys.path:
                    sys.path.insert(0, gear_lib_dir)

                if os.path.isdir(gear_base_dir):
                    for subdir in os.listdir(gear_base_dir):
                        full_subdir_path = os.path.join(gear_base_dir, subdir)
                        if not os.path.isdir(full_subdir_path) or subdir.startswith('_'):
                            continue
                        # Only providers/ — modules/ is helpers (was re-imported every play)
                        if subdir != 'providers':
                            continue
                        try:
                            for provider_group in os.listdir(full_subdir_path):
                                provider_path = os.path.join(full_subdir_path, provider_group)
                                if os.path.isdir(provider_path) and not provider_group.startswith('_'):
                                    try:
                                        loaded = _load_sources_from_dir(
                                            provider_path,
                                            prefix=f'gearsscrapers.providers.{provider_group}',
                                        )
                                        sources.extend(loaded)
                                        c.log(
                                            f'[Sources] Loaded {len(loaded)} scrapers from '
                                            f'gearsscrapers/providers/{provider_group}'
                                        )
                                    except Exception as e:
                                        c.log(
                                            f'[Sources] Failed to load '
                                            f'gearsscrapers/providers/{provider_group}: {e}',
                                            1,
                                        )
                        except Exception as e:
                            c.log(f'[Sources] Failed to load gearsscrapers/{subdir}: {e}', 1)
                else:
                    c.log('[Sources] Gearsscrapers sources directory not found', 1)
            else:
                c.log('[Sources] Could not locate addons directory for Gearsscrapers', 1)
        elif not gearsscrapers_enabled:
            c.log('[Sources] Gearsscrapers installed but disabled in settings')
        else:
            c.log('[Sources] Gearsscrapers not installed, skipping')
    except Exception as e:
        c.log(f'[Sources] Error during Gearsscrapers detection: {e}', 1)
        c.log(f'[Sources] Traceback: {traceback.format_exc()}', 1)

    # Try to load Viperscrapers if installed and enabled
    try:
        import xbmc
        viperscrapers_enabled = c.get_setting('viperscrapers.enabled') == 'true'

        if xbmc.getCondVisibility('System.HasAddon(script.module.viperscrapers)') and viperscrapers_enabled:
            c.log('[Sources] Viperscrapers detected and enabled, attempting to load scrapers')

            # Discover addons directory
            cur = os.path.dirname(__file__)
            addons_dir = None
            for _ in range(6):
                if os.path.basename(cur).lower() == 'addons':
                    addons_dir = cur
                    break
                parent = os.path.dirname(cur)
                if parent == cur:
                    break
                cur = parent

            if addons_dir:
                vipers_base_dir = os.path.join(addons_dir, 'script.module.viperscrapers', 'lib', 'viperscrapers', 'sources_viperscrapers')
                vipers_lib_dir = os.path.join(addons_dir, 'script.module.viperscrapers', 'lib')

                # Add viperscrapers lib to path for imports
                if vipers_lib_dir not in sys.path:
                    sys.path.insert(0, vipers_lib_dir)

                # Load from all source subdirectories (torrents, hosters, etc.)
                if os.path.isdir(vipers_base_dir):
                    for subdir in os.listdir(vipers_base_dir):
                        full_subdir_path = os.path.join(vipers_base_dir, subdir)
                        if os.path.isdir(full_subdir_path) and not subdir.startswith('_'):
                            try:
                                loaded = _load_sources_from_dir(full_subdir_path, prefix=f'viperscrapers.{subdir}')
                                sources.extend(loaded)
                                c.log(f'[Sources] Loaded {len(loaded)} scrapers from viperscrapers/{subdir}')
                            except Exception as e:
                                c.log(f'[Sources] Failed to load viperscrapers/{subdir}: {e}', 1)
                else:
                    c.log('[Sources] Viperscrapers sources directory not found', 1)
        elif not viperscrapers_enabled:
            c.log('[Sources] Viperscrapers installed but disabled in settings')
        else:
            c.log('[Sources] Viperscrapers not installed, skipping')
    except Exception as e:
        c.log(f'[Sources] Error during Viperscrapers detection: {e}', 1)
        c.log(f'[Sources] Traceback: {traceback.format_exc()}', 1)

    # Try to load ClassyScrapers if installed and enabled
    try:
        import xbmc
        classyscrapers_enabled = c.get_setting('classyscrapers.enabled') == 'true'
        classy_addon_id = 'script.module.classyscrapers'

        has_classy = False
        try:
            has_classy = bool(xbmc.getCondVisibility(f'System.HasAddon({classy_addon_id})'))
        except Exception:
            has_classy = False

        # Dropped-in packs are often discovered disabled; HasAddon=true but Addon() fails.
        if not has_classy:
            cur = os.path.dirname(__file__)
            for _ in range(6):
                if os.path.basename(cur).lower() == 'addons':
                    if os.path.isdir(os.path.join(cur, classy_addon_id)):
                        has_classy = True
                        c.log('[Sources] ClassyScrapers folder found (filesystem fallback)')
                    break
                parent = os.path.dirname(cur)
                if parent == cur:
                    break
                cur = parent

        if has_classy and classyscrapers_enabled:
            try:
                kodi_enabled = bool(xbmc.getCondVisibility(f'System.AddonIsEnabled({classy_addon_id})'))
            except Exception:
                kodi_enabled = False
            if not kodi_enabled:
                c.log('[Sources] ClassyScrapers is installed but disabled in Kodi — enabling it')
                try:
                    xbmc.executeJSONRPC(
                        '{"jsonrpc":"2.0","method":"Addons.SetAddonEnabled",'
                        f'"params":{{"addonid":"{classy_addon_id}","enabled":true}},"id":1}}'
                    )
                    xbmc.sleep(750)
                except Exception as e:
                    c.log(f'[Sources] Failed to enable ClassyScrapers in Kodi: {e}', 1)

            c.log('[Sources] ClassyScrapers detected and enabled, attempting to load scrapers')

            cur = os.path.dirname(__file__)
            addons_dir = None
            for _ in range(6):
                if os.path.basename(cur).lower() == 'addons':
                    addons_dir = cur
                    break
                parent = os.path.dirname(cur)
                if parent == cur:
                    break
                cur = parent

            if addons_dir:
                classy_base_dir = os.path.join(
                    addons_dir, classy_addon_id, 'lib', 'classyscrapers', 'sources_classyscrapers')
                classy_lib_dir = os.path.join(addons_dir, classy_addon_id, 'lib')

                if classy_lib_dir not in sys.path:
                    sys.path.insert(0, classy_lib_dir)

                if os.path.isdir(classy_base_dir):
                    for subdir in os.listdir(classy_base_dir):
                        full_subdir_path = os.path.join(classy_base_dir, subdir)
                        if os.path.isdir(full_subdir_path) and not subdir.startswith('_'):
                            try:
                                loaded = _load_sources_from_dir(full_subdir_path, prefix=f'classyscrapers.{subdir}')
                                sources.extend(loaded)
                                c.log(f'[Sources] Loaded {len(loaded)} scrapers from classyscrapers/{subdir}')
                            except Exception as e:
                                c.log(f'[Sources] Failed to load classyscrapers/{subdir}: {e}', 1)
                else:
                    c.log('[Sources] ClassyScrapers sources directory not found', 1)
        elif not classyscrapers_enabled:
            c.log('[Sources] ClassyScrapers installed but disabled in Midnight settings')
        else:
            c.log('[Sources] ClassyScrapers not installed, skipping')
    except Exception as e:
        c.log(f'[Sources] Error during ClassyScrapers detection: {e}', 1)
        c.log(f'[Sources] Traceback: {traceback.format_exc()}', 1)
    # Finalize: report and return collected sources
    try:
        elapsed = __import__('time').time() - t_load
        c.log(f'[Sources] Final total scrapers loaded: {len(sources)} in {elapsed:.2f}s')
    except Exception:
        c.log('[Sources] Final total scrapers loaded: <unknown>', 1)

    _PROVIDER_CACHE = sources
    _PROVIDER_CACHE_KEY = cache_key
    return list(sources)

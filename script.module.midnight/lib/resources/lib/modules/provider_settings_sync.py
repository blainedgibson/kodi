# -*- coding: utf-8 -*-
"""
Sync internal Midnight scraper on/off toggles into plugin.video.midnight/resources/settings.xml.

Scrapers live under resources/lib/scrapers/ (post-refactor). The legacy service
scanned sources/en*, which are empty — this module restores that behaviour.
"""

from __future__ import annotations

import glob
import os
import re
from typing import Dict, List, Tuple

_SKIP_MODULES = frozenset({'__init__', 'base', 'torrent_base'})

# Historical en_de category (debrid-oriented hosters)
_DEBRID_NAMES = frozenset({
    'myvideolinks', 'rapidmoviez', 'rlsbb', 'scenerls', 'scenerls_v2',
    'onlineseries', 'maxrls', '300mbfilms', '2ddl', 'easynews', 'ororo',
})

_CATEGORY_LABELS = {
    'free': '32345',
    'debrid': '90004',
    'torrent': '90005',
}


def _module_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))


def _default_scrapers_dir() -> str:
    return os.path.join(_module_root(), 'lib', 'resources', 'lib', 'scrapers')


def _default_settings_path() -> str:
    addons_dir = os.path.dirname(_module_root())
    return os.path.join(addons_dir, 'plugin.video.midnight', 'resources', 'settings.xml')


def _classify_provider(basename: str, source: str) -> str:
    lowered = basename.lower()
    if 'torrent_base' in source or 'TorrentBaseScraper' in source:
        return 'torrent'
    if lowered in _DEBRID_NAMES:
        return 'debrid'
    if lowered == 'x1337' or lowered.endswith('torrent') or 'magnet' in source.lower():
        return 'torrent'
    # Heuristic: torrent scrapers usually tag sources as torrent/debridonly
    if re.search(r"['\"]source['\"]\s*:\s*['\"]torrent['\"]", source):
        return 'torrent'
    if re.search(r"['\"]debridonly['\"]\s*:\s*True", source):
        return 'debrid'
    return 'free'


def discover_internal_providers(scrapers_dir: str | None = None) -> Dict[str, List[Tuple[str, str]]]:
    """
    Return providers grouped by category key: free | debrid | torrent.
    Each item is (setting_id, display_label) e.g. ('provider.dbgo', 'DBGO').
    """
    scrapers_dir = scrapers_dir or _default_scrapers_dir()
    grouped: Dict[str, List[Tuple[str, str]]] = {'free': [], 'debrid': [], 'torrent': []}

    if not os.path.isdir(scrapers_dir):
        return grouped

    for path in sorted(glob.glob(os.path.join(scrapers_dir, '*.py'))):
        basename = os.path.splitext(os.path.basename(path))[0]
        if basename in _SKIP_MODULES:
            continue
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                source = fh.read()
        except OSError:
            continue
        category = _classify_provider(basename, source)
        setting_id = f'provider.{basename.lower()}'
        label = basename.replace('_', ' ').upper()
        grouped[category].append((setting_id, label))

    return grouped


def _provider_lines(entries: List[Tuple[str, str]]) -> str:
    if not entries:
        return '\n'
    lines = ['\n']
    for setting_id, label in entries:
        short = setting_id.replace('provider.', '', 1)
        lines.append(
            f'        <setting id="provider.{short}" type="bool" label="{label}" default="true" />\n'
        )
    return ''.join(lines)


def sync_internal_provider_settings(
    settings_path: str | None = None,
    scrapers_dir: str | None = None,
    log=None,
) -> bool:
    """
    Inject provider.<scraper> bool settings into resources/settings.xml categories.
    Returns True when the schema file was modified.
    """
    settings_path = settings_path or _default_settings_path()
    providers = discover_internal_providers(scrapers_dir)

    if not any(providers.values()):
        if log:
            log('[provider_settings_sync] No scrapers discovered — skip')
        return False

    try:
        with open(settings_path, 'r', encoding='utf-8') as fh:
            xml = fh.read()
    except OSError as exc:
        if log:
            log(f'[provider_settings_sync] Cannot read settings.xml: {exc}')
        return False

    original = xml
    for cat_key, cat_label in _CATEGORY_LABELS.items():
        block = _provider_lines(providers.get(cat_key, []))
        pattern = (
            rf'(<category label="{re.escape(cat_label)}">)'
            r'[\s\S]*?'
            r'(<\/category>)'
        )

        def _replace_category(match, content=block):
            return match.group(1) + content + '    </category>\n'

        new_xml, count = re.subn(pattern, _replace_category, xml, count=1, flags=re.DOTALL)
        if count == 0:
            if log:
                log(f'[provider_settings_sync] Category {cat_label} not found — skip')
            continue
        xml = new_xml

    if xml == original:
        return False

    try:
        with open(settings_path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(xml)
    except OSError as exc:
        if log:
            log(f'[provider_settings_sync] Cannot write settings.xml: {exc}')
        return False

    total = sum(len(v) for v in providers.values())
    if log:
        log(
            f'[provider_settings_sync] Updated settings.xml: '
            f'{len(providers["free"])} free, {len(providers["debrid"])} debrid, '
            f'{len(providers["torrent"])} torrent ({total} total)'
        )
    return True


def restore_provider_prefs_from_backup(
    userdata_path: str,
    backup_path: str | None = None,
    log=None,
) -> bool:
    """Merge provider.* on/off values from settings.xml.bak into current userdata."""
    from .settings_repair import load_schema_ids

    backup_path = backup_path or (userdata_path + '.bak')
    if not os.path.isfile(userdata_path) or not os.path.isfile(backup_path):
        return False

    with open(userdata_path, 'r', encoding='utf-8') as fh:
        current = fh.read()
    with open(backup_path, 'r', encoding='utf-8') as fh:
        backup = fh.read()

    prefs = {}
    for m in re.finditer(r'<setting id="(provider\.[^"]+)"[^>]*>([^<]*)</setting>', backup):
        prefs[m.group(1)] = m.group(2).strip()
    for m in re.finditer(r'<setting id="(provider\.[^"]+)"([^>]*)/>', backup):
        prefs[m.group(1)] = ''

    if not prefs:
        return False

    changed = False
    for sid, value in prefs.items():
        if sid not in load_schema_ids():
            continue
        if f'id="{sid}"' in current:
            continue
        if value:
            line = f'    <setting id="{sid}">{value}</setting>\n'
        else:
            line = f'    <setting id="{sid}" />\n'
        current = current.replace('</settings>\n', line + '</settings>\n', 1)
        changed = True

    if changed:
        with open(userdata_path, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(current)
        if log:
            log(f'[provider_settings_sync] Restored provider preference(s) from backup')
    return changed

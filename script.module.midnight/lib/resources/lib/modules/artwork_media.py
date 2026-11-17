# -*- coding: utf-8 -*-

'''
Theme media resolver — manifest.json aliases per artwork theme folder.

POC scope: root main menu doubles (main_search/search, main_tools/tools).
'''

from __future__ import annotations

import json
import os

ARTWORK_ADDON_ID = 'script.midnight.artwork'
DEFAULT_ART_THEME = 'midnight'

_MANIFEST_CACHE: dict[str, dict] = {}


def _media_dir_for_root(addon_root: str, theme: str) -> str:
    if not addon_root:
        return ''
    return os.path.join(addon_root, 'resources', 'media', theme)


def _artwork_addon_root() -> str:
    try:
        import xbmcaddon
        return xbmcaddon.Addon(ARTWORK_ADDON_ID).getAddonInfo('path') or ''
    except Exception:
        return ''


def _media_dir(theme: str, addon_root: str | None = None) -> str:
    root = addon_root or _artwork_addon_root()
    return _media_dir_for_root(root, theme)


def _load_manifest(theme: str, addon_root: str | None = None) -> dict:
    theme = (theme or DEFAULT_ART_THEME).strip().lower() or DEFAULT_ART_THEME
    cache_key = f'{addon_root or ""}:{theme}'
    cached = _MANIFEST_CACHE.get(cache_key)
    if cached is not None:
        return cached

    manifest: dict = {}
    path = os.path.join(_media_dir(theme, addon_root), 'manifest.json')
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                raw = json.load(handle)
            if isinstance(raw, dict):
                manifest = raw
        except Exception:
            manifest = {}

    _MANIFEST_CACHE[cache_key] = manifest
    return manifest


def clear_manifest_cache() -> None:
    _MANIFEST_CACHE.clear()


def _candidate_names(filename: str, aliases: dict) -> list[str]:
    name = os.path.basename(filename or '').strip()
    if not name:
        return []
    canonical = aliases.get(name) or aliases.get(name.lower())
    if canonical and canonical != name:
        return [canonical, name]
    return [name]


def _first_existing(media_dir: str, names: list[str]) -> str:
    if not media_dir or not names:
        return ''
    for name in names:
        path = os.path.join(media_dir, name)
        if os.path.isfile(path):
            return path
    return ''


def resolve_artwork_media(filename: str, theme: str | None = None, addon_root: str | None = None) -> str:
    """Resolve a theme media file, applying manifest aliases when present."""
    name = os.path.basename(filename or '').strip()
    if not name:
        return ''

    themes: list[str] = []
    active = (theme or DEFAULT_ART_THEME).strip().lower() or DEFAULT_ART_THEME
    themes.append(active)
    if DEFAULT_ART_THEME not in themes and not addon_root:
        themes.append(DEFAULT_ART_THEME)

    for theme_name in themes:
        media_dir = _media_dir(theme_name, addon_root)
        if not media_dir:
            continue
        aliases = (_load_manifest(theme_name, addon_root).get('aliases') or {})
        if not isinstance(aliases, dict):
            aliases = {}
        path = _first_existing(media_dir, _candidate_names(name, aliases))
        if path:
            return path

    return ''


def missing_required_media(theme: str, addon_root: str) -> list[str]:
    """Return required manifest entries that cannot be resolved on disk."""
    manifest = _load_manifest(theme, addon_root)
    required = manifest.get('required') or []
    if not isinstance(required, list):
        return []
    missing: list[str] = []
    for entry in required:
        name = os.path.basename(str(entry or '').strip())
        if not name:
            continue
        if not resolve_artwork_media(name, theme=theme, addon_root=addon_root):
            missing.append(name)
    return missing

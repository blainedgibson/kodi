# -*- coding: utf-8 -*-

'''
Midnight addon version helpers for upgrade health check (no Kodi imports).

Health check cares about install integrity (files on disk), not semver alignment.
Version notes here are advisory only — e.g. artwork v3.0.2 with module v3.0.9 is fine
if the artwork integrity probe passes.
'''

from __future__ import annotations

CREW_ADDON_IDS = (
    'script.module.midnight',
    'plugin.video.midnight',
    'script.midnight.artwork',
)


def module_missing_reason(versions: dict[str, str]) -> str | None:
    if versions.get('script.module.midnight', ''):
        return None
    return 'script.module.midnight is not installed'


def format_version_mismatch_note(versions: dict[str, str]) -> str | None:
    """Advisory note when Midnight addons are on different versions — does not block health check."""
    module_ver = versions.get('script.module.midnight', '')
    if not module_ver:
        return None

    notes: list[str] = []
    for addon_id in ('plugin.video.midnight', 'script.midnight.artwork'):
        ver = versions.get(addon_id, '')
        label = addon_id.rsplit('.', 1)[-1]
        if not ver:
            notes.append(f'{label} not installed')
        elif ver != module_ver:
            notes.append(f'{label} v{ver} (module v{module_ver})')

    if not notes:
        return None
    return 'Midnight addon versions differ: ' + ', '.join(notes)

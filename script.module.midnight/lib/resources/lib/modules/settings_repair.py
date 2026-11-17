# -*- coding: utf-8 -*-

'''
********************************************************cm*
* Midnight Add-on
*
* @file settings_repair.py
* @package script.module.midnight
*
* @copyright (c) 2026, Midnight
* @license GNU General Public License, version 3 (GPL-3.0)
*
********************************************************cm*

Repair plugin.video.midnight userdata settings.xml when it contains setting IDs
that no longer exist in resources/settings.xml (Kodi 21 rejects the file).

Kodi 21 Omega userdata settings.xml has NO <?xml declaration — healthy files
start with <settings version="2">. Do not prepend a declaration: Kodi strips it
and can leave an empty settings.xml, which retriggers repair in a loop.

'''

from __future__ import annotations

import os
import re
import threading
import xml.etree.ElementTree as ET
from typing import Set

_SCHEMA_VERSION = '3.0.7'
_repair_lock = threading.Lock()
_repair_attempted = False
_repair_in_progress = False
_synced_this_process = False


def _use_vfs_io() -> bool:
    """Android/macOS Kodi sandboxes Python open(); use xbmcvfs for userdata I/O."""
    try:
        import platform as _platform
        import xbmc
        kodi_platform = (xbmc.getInfoLabel('System.Platform.OS') or '').lower()
        if kodi_platform in ('android', 'ios', 'osx', 'darwin'):
            return True
        return _platform.system().lower() in ('darwin', 'android')
    except Exception:
        return False


def _file_exists(path: str) -> bool:
    if not path:
        return False
    if _use_vfs_io():
        try:
            import xbmcvfs
            return bool(xbmcvfs.exists(path))
        except Exception:
            pass
    return os.path.isfile(path)


def _read_text(path: str, limit: int | None = None) -> str:
    if not _file_exists(path):
        raise OSError(f'missing: {path}')
    if _use_vfs_io():
        import xbmcvfs
        handle = xbmcvfs.File(path)
        if not handle:
            raise OSError(f'xbmcvfs read failed: {path}')
        try:
            raw = handle.read() or ''
        finally:
            handle.close()
        return raw[:limit] if limit is not None else raw
    with open(path, 'r', encoding='utf-8') as fh:
        return fh.read(limit) if limit is not None else fh.read()


def _write_text(path: str, content: str) -> None:
    """Write UTF-8 text. Works outside Kodi (restart_kodi_clean.ps1 repair step)."""
    try:
        from .crewruntime import crew_file_write
        crew_file_write(path, content, mode='w')
        return
    except Exception:
        pass
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(content)


def _copy_file(src: str, dst: str) -> None:
    _write_text(dst, _read_text(src))


def _parse_xml_file(path: str) -> ET.ElementTree:
    raw = _read_text(path)
    if not raw.lstrip().startswith('<?xml'):
        raw = '<?xml version="1.0" encoding="utf-8"?>\n' + raw
    return ET.ElementTree(ET.fromstring(raw))


def _schema_path() -> str:
    module_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
    addons_dir = os.path.dirname(module_root)
    return os.path.join(addons_dir, 'plugin.video.midnight', 'resources', 'settings.xml')


def _userdata_path(profile_path: str | None = None) -> str:
    """Resolve plugin.video.midnight userdata settings.xml on any platform."""
    if profile_path:
        return os.path.join(profile_path, 'settings.xml')
    try:
        import xbmcaddon
        import xbmcvfs
        profile = xbmcvfs.translatePath(
            xbmcaddon.Addon('plugin.video.midnight').getAddonInfo('profile')
        )
        if profile:
            return os.path.join(profile, 'settings.xml')
    except Exception:
        pass
    appdata = os.environ.get('APPDATA')
    if appdata:
        base = os.path.join(appdata, 'Kodi', 'userdata')
    else:
        base = os.path.join(os.path.expanduser('~'), '.kodi', 'userdata')
    return os.path.join(base, 'addon_data', 'plugin.video.midnight', 'settings.xml')


def load_schema_ids(schema_path: str | None = None) -> Set[str]:
    path = schema_path or _schema_path()
    if not _file_exists(path):
        return set()
    try:
        text = _read_text(path)
    except OSError:
        return set()
    return set(re.findall(r'<setting id="([^"]+)"', text))


def parse_userdata_settings(user_path: str) -> dict[str, str]:
    """Read setting id/value pairs from on-disk userdata (tolerates missing XML header)."""
    if not user_path or not _file_exists(user_path):
        return {}
    try:
        raw = _read_text(user_path)
    except OSError:
        return {}
    if not raw.strip():
        return {}
    if not raw.lstrip().startswith('<?xml'):
        raw = '<?xml version="1.0" encoding="utf-8"?>\n' + raw
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {}
    out: dict[str, str] = {}
    for setting in root:
        if setting.tag != 'setting':
            continue
        sid = setting.attrib.get('id')
        if sid:
            out[sid] = setting.text if setting.text is not None else ''
    return out


def _setting_line(setting: ET.Element) -> str:
    attrs = dict(setting.attrib)
    text = setting.text or ''
    sid = attrs.get('id') or ''
    # Kodi treats default="true" as "use schema default" and ignores element text.
    # Never keep default= on sensitive keys or folder paths — empty + default="true" is poison.
    if text.strip() or sid in _SENSITIVE_SETTING_IDS or sid in _FOLDER_SETTING_IDS:
        attrs.pop('default', None)
    attrs_str = ''.join(f' {k}="{v}"' for k, v in sorted(attrs.items()))
    if text or len(setting) == 0:
        if text:
            return f'    <setting{attrs_str}>{_xml_escape(text)}</setting>'
        return f'    <setting{attrs_str} />'
    # Should not happen in Kodi userdata; keep element text if present.
    return f'    <setting{attrs_str}>{_xml_escape(text)}</setting>'


def _xml_escape(value: str) -> str:
    return (
        value.replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
        .replace("'", '&apos;')
    )


def _strip_xml_declaration(raw: str) -> str:
    """Remove <?xml ...?> if present. Kodi Omega userdata has no declaration."""
    body = (raw or '').lstrip()
    if body.startswith('<?xml'):
        end = body.find('?>')
        if end != -1:
            body = body[end + 2:].lstrip('\r\n')
    return body


def _serialize_userdata_settings(setting_elems) -> str:
    """Write userdata settings.xml in the format Kodi 21 Omega keeps on disk.

    No XML declaration — adding <?xml triggers a strip/wipe cycle on Omega.
    """
    lines = ['<settings version="2">']
    lines.extend(_setting_line(s) for s in setting_elems)
    lines.append('</settings>')
    return '\n'.join(lines) + '\n'


def settings_sync_in_progress() -> bool:
    """True while repair is pushing values via setSetting (ignore onSettingsChanged)."""
    return _repair_in_progress


def original_backup_path(userdata_path: str) -> str:
    """Path to the one-time preserved original userdata snapshot."""
    return userdata_path + '.bak'


def preserve_original_backup(userdata_path: str, log=None) -> str | None:
    """
    Copy userdata to settings.xml.bak only when .bak does not exist yet.

    The .bak file is the user's pre-repair original — never overwrite it on
    later repairs, Kodi saves, or reboots.
    """
    if not userdata_path or not _file_exists(userdata_path):
        return None
    backup = original_backup_path(userdata_path)
    if _file_exists(backup):
        return None
    try:
        _copy_file(userdata_path, backup)
    except OSError:
        return None
    if log:
        log(f'[settings_repair] Preserved original backup: {backup}')
    return backup


def restore_user_settings_from_backup(
    userdata_path: str | None = None,
    backup_path: str | None = None,
    profile_path: str | None = None,
    log=None,
) -> bool:
    """Restore settings.xml after a wiped/corpse file.

    Prefer profile backup over the frozen one-time settings.xml.bak.
    """
    user_path = userdata_path or _userdata_path(profile_path)
    if backup_path:
        backup = backup_path
    else:
        # Disaster recovery order: profile (current-ish) → health → never prefer .bak
        backup = find_settings_backup_for_import() or ''
        if not backup or not _file_exists(backup):
            health = health_backup_path(user_path)
            backup = health if health and _file_exists(health) else original_backup_path(user_path)
        if _is_frozen_original_bak(backup) and log:
            log(
                '[settings_repair] Falling back to frozen settings.xml.bak for corpse restore '
                '— will re-merge sensitive keys from profile afterward if available'
            )
    if not _file_exists(backup):
        if log:
            log(f'[settings_repair] No backup to restore: {backup}')
        return False

    # Keep progress markers from the live file — .bak / health backups are often older.
    preserve: dict[str, str] = {}
    if _file_exists(user_path):
        try:
            current = parse_userdata_settings(user_path)
            for sid in _FORWARD_ONLY_SETTING_IDS:
                val = (current.get(sid) or '').strip()
                if val:
                    preserve[sid] = val
        except Exception:
            preserve = {}

    try:
        raw = _strip_xml_declaration(_read_text(backup))
    except OSError as exc:
        if log:
            log(f'[settings_repair] Backup read failed: {exc}')
        return False
    if raw and not raw.endswith('\n'):
        raw += '\n'
    try:
        _write_text(user_path, raw)
    except OSError as exc:
        if log:
            log(f'[settings_repair] Restore write failed: {exc}')
        return False

    if preserve:
        try:
            tree = _parse_xml_file(user_path)
            root = tree.getroot()
            seen = set()
            for setting in root:
                if setting.tag != 'setting':
                    continue
                sid = setting.attrib.get('id')
                if sid in preserve:
                    setting.text = preserve[sid]
                    setting.attrib.pop('default', None)
                    seen.add(sid)
            for sid, val in preserve.items():
                if sid in seen:
                    continue
                elem = ET.Element('setting', {'id': sid})
                elem.text = val
                root.append(elem)
            _write_text(
                user_path,
                _serialize_userdata_settings(s for s in root if s.tag == 'setting'),
            )
            if log:
                keys = ', '.join(sorted(preserve.keys()))
                log(f'[settings_repair] Preserved forward-only settings after restore: {keys}')
        except Exception as exc:
            if log:
                log(f'[settings_repair] Failed to preserve forward-only settings: {exc}')

    # Full .bak restore reintroduces tokens; if revoke was preserved, strip them.
    try:
        restored_vals = parse_userdata_settings(user_path)
        if _trakt_intentionally_revoked(restored_vals):
            tree = _parse_xml_file(user_path)
            root = tree.getroot()
            cleared = []
            for setting in root:
                if setting.tag != 'setting':
                    continue
                sid = setting.attrib.get('id')
                if sid in _TRAKT_CREDENTIAL_SETTING_IDS and (setting.text or '').strip():
                    setting.text = ''
                    setting.attrib.pop('default', None)
                    cleared.append(sid)
            if cleared:
                _write_text(
                    user_path,
                    _serialize_userdata_settings(s for s in root if s.tag == 'setting'),
                )
                if log:
                    log(
                        '[settings_repair] Cleared Trakt credentials after restore '
                        f'(trakt.revoked=true): {", ".join(sorted(cleared))}'
                    )
    except Exception as exc:
        if log:
            log(f'[settings_repair] Failed to clear Trakt credentials after restore: {exc}')

    if log:
        log(f'[settings_repair] Restored settings from {os.path.basename(backup)}')
    # If we had to use a stale snapshot, re-fill auth keys from profile/health.
    try:
        sanitize_sensitive_settings(user_path, log=log)
    except Exception as exc:
        if log:
            log(f'[settings_repair] Post-restore sanitize failed: {exc}')
    _clear_runtime_settings_cache()
    _refresh_runtime_theme()
    return True


def health_backup_path(userdata_path: str, version: str | None = None) -> str:
    """Per-upgrade snapshot taken immediately before repair (overwritten each version)."""
    ver = version or _SCHEMA_VERSION
    return f'{userdata_path}.health_backup_{ver}'


def kodi_root_from_addons_home(home_path: str) -> str:
    """Parent of special://home — Kodi install root (sibling to userdata/, not inside it)."""
    home = (home_path or '').rstrip('\\/')
    if not home:
        return ''
    return os.path.dirname(home)


def _legacy_profile_settings_backup_path() -> str:
    """Old location under special://profile — kept for one-time import only."""
    try:
        import xbmcvfs
        profile = xbmcvfs.translatePath('special://profile')
    except Exception:
        profile = ''
    if not profile:
        return ''
    return os.path.join(profile.rstrip('\\/'), 'midnight_settings_backup', 'settings.xml')


def external_settings_backup_dir() -> str:
    """
    KodiRoot/midnight_settings_backup — outside userdata/ and addon_data/.

    Path is derived from special://home (parent of addons/) so it works on every
    platform Kodi supports, e.g.:
      Windows  …/AppData/Roaming/Kodi/midnight_settings_backup
      Linux    ~/.kodi/midnight_settings_backup
      macOS    ~/Library/Application Support/Kodi/midnight_settings_backup
      Android  …/.kodi/midnight_settings_backup (under Kodi app storage)

    Survives uninstall of plugin.video.midnight (addon_data is wiped).
    """
    try:
        import xbmcvfs
        home = xbmcvfs.translatePath('special://home')
    except Exception:
        home = ''
    root = kodi_root_from_addons_home(home)
    if not root:
        return ''
    return os.path.join(root, 'midnight_settings_backup')


def profile_settings_backup_path() -> str:
    """External settings snapshot path (not under userdata or addon_data)."""
    dest_dir = external_settings_backup_dir()
    if not dest_dir:
        return ''
    return os.path.join(dest_dir, 'settings.xml')


def _ensure_external_backup_dir(log=None) -> str:
    """Create backup dir via xbmcvfs (works on Android/macOS sandbox)."""
    dest_dir = external_settings_backup_dir()
    if not dest_dir:
        return ''
    try:
        import xbmcvfs
        xbmcvfs.mkdirs(dest_dir)
    except Exception:
        try:
            from . import control
            control.makeFile(dest_dir)
        except Exception as exc:
            if log:
                log(f'[settings_repair] Backup dir create failed: {exc}')
            return ''
    return dest_dir


def find_settings_backup_for_import() -> str:
    """Prefer external backup; fall back to legacy profile-folder copy."""
    for path in (profile_settings_backup_path(), _legacy_profile_settings_backup_path()):
        if path and _file_exists(path):
            return path
    return ''


def _settings_file_fingerprint(path: str) -> tuple[int, str] | None:
    try:
        text = _read_text(path)
    except OSError:
        return None
    import hashlib
    digest = hashlib.md5(text.encode('utf-8')).hexdigest()
    return len(text), digest


def maybe_export_settings_backup(
    userdata_path: str | None = None,
    force: bool = False,
    log=None,
) -> str | None:
    """
    Copy settings.xml to KodiRoot/midnight_settings_backup when changed.

    Emergency restore only helps when this snapshot stays current — especially after
    a module/plugin version bump (call with force=True once last_version / bases settle).

    Safe to call on every service boot and after settings saves.
    """
    user_path = userdata_path or _userdata_path()
    dest = profile_settings_backup_path()
    if not dest or not _file_exists(user_path):
        return None
    if not force and _file_exists(dest):
        src_fp = _settings_file_fingerprint(user_path)
        dst_fp = _settings_file_fingerprint(dest)
        if src_fp and dst_fp and src_fp == dst_fp:
            return dest
    return export_profile_settings_backup(user_path, log=log, force=force)


def commit_live_settings_to_backups(log=None) -> bool:
    """
    CRUD Update: live settings.xml is the source of truth for mirrors.

    Overwrites the current health_backup_* and the profile emergency backup.
    Call after auth/revoke/settings save and once per healthy boot so old
    snapshots cannot resurrect a prior intent (Premiumize / debrid / tracker).
    """
    user_path = _userdata_path()
    if not _file_exists(user_path):
        return False
    try:
        if _userdata_is_header_only_corpse(user_path):
            if log:
                log('[settings_repair] commit skipped — live settings.xml is a corpse')
            return False
    except Exception:
        pass
    try:
        create_version_health_backup(user_path, _SCHEMA_VERSION, log=log)
    except Exception as exc:
        if log:
            log(f'[settings_repair] Health commit failed: {exc}')
    try:
        export_profile_settings_backup(user_path, log=log, force=True)
    except Exception as exc:
        if log:
            log(f'[settings_repair] Profile commit failed: {exc}')
        return False
    if log:
        log('[settings_repair] Committed live settings into backups (CRUD update)')
    return True


def prune_obsolete_settings_backups(log=None) -> int:
    """
    Delete stale addon_data settings mirrors that fight live intent.

    Keeps: settings.xml, settings.xml.bak (frozen original), current
    health_backup_{schema}. Removes older health_backup_* and leftovers such as
    settings.xml.pre_simkl_restore. Profile backup is refreshed via commit, not deleted.
    """
    user_path = _userdata_path()
    parent = os.path.dirname(user_path.rstrip('\\/')) or ''
    if not parent or not os.path.isdir(parent):
        return 0
    keep = {
        os.path.normcase('settings.xml'),
        os.path.normcase('settings.xml.bak'),
        os.path.normcase(os.path.basename(health_backup_path(user_path))),
    }
    removed = 0
    try:
        names = os.listdir(parent)
    except OSError:
        return 0
    for name in names:
        lower = name.lower()
        if os.path.normcase(name) in keep:
            continue
        drop = (
            '.health_backup_' in lower
            or 'pre_simkl' in lower
            or (
                lower.startswith('settings')
                and lower.endswith('.bak')
                and os.path.normcase(name) != os.path.normcase('settings.xml.bak')
            )
        )
        if not drop:
            continue
        path = os.path.join(parent, name)
        try:
            if os.path.isfile(path):
                os.remove(path)
                removed += 1
                if log:
                    log(f'[settings_repair] Pruned obsolete backup: {name}')
        except OSError as exc:
            if log:
                log(f'[settings_repair] Prune skipped ({name}): {exc}')
    if log and removed:
        log(f'[settings_repair] Pruned {removed} obsolete settings backup(s)')
    return removed


def reset_settings_backups(log=None) -> int:
    """Tools action: prune stale mirrors, then commit live as the only recovery copy."""
    n = prune_obsolete_settings_backups(log=log)
    commit_live_settings_to_backups(log=log)
    return n


# Auth / identity keys: restore from backup whenever current is empty but backup has a value.
_SENSITIVE_SETTING_IDS = frozenset({
    'trakt.user', 'trakt.token', 'trakt.refresh', 'trakt.expires_at', 'trakt.auth',
    'trakt.client_id', 'trakt.client_secret',
    'simkl.user', 'simkl.token', 'simkl.client_id',
    'tm.user', 'tm.personal_user',
    'personal.list', 'adult_pin', 'devmode.password',
})

# Folder paths: empty + default="true" makes Kodi ignore later path values (same poison as tokens).
_FOLDER_SETTING_IDS = frozenset({
    'movie.download.path', 'tv.download.path',
    'library.movie', 'library.tv',
})

# Cleared by intentional Trakt revoke — never rehydrate these while trakt.revoked is set.
_TRAKT_CREDENTIAL_SETTING_IDS = frozenset({
    'trakt.user', 'trakt.token', 'trakt.refresh', 'trakt.expires_at', 'trakt.auth',
})

_SIMKL_CREDENTIAL_SETTING_IDS = frozenset({
    'simkl.user', 'simkl.token',
})

# Never merge these from older backups — would revert forward-only markers.
_FORWARD_ONLY_SETTING_IDS = frozenset({
    'last_version_announcement',
    'settings.schema_version',
    'module_base',
    'plugin_base',
    'trakt.revoked',
    # User choice — never resurrect from a stale backup over a deliberate switch.
    'tracker.service',
})


def _is_frozen_original_bak(path: str | None) -> bool:
    """True for the one-time settings.xml.bak (never overwritten after first create).

    That file is a June-era snapshot of dead tokens / old preferences. Never merge
    ANY values from it into live userdata — profile + health_backup_* only.
    """
    if not path:
        return False
    name = os.path.basename(path.replace('\\', '/'))
    return name == 'settings.xml.bak'


def _patch_trakt_credentials_in_backup(backup_path: str, creds: dict[str, str], log=None) -> bool:
    """Update Trakt credential keys inside an existing backup XML (in place)."""
    if not backup_path or not _file_exists(backup_path) or not creds:
        return False
    try:
        tree = _parse_xml_file(backup_path)
    except ET.ParseError:
        return False
    root = tree.getroot()
    seen = set()
    changed = False
    for setting in root:
        if setting.tag != 'setting':
            continue
        sid = setting.attrib.get('id')
        if sid not in creds:
            continue
        setting.text = creds[sid]
        setting.attrib.pop('default', None)
        seen.add(sid)
        changed = True
    for sid, val in creds.items():
        if sid in seen:
            continue
        elem = ET.Element('setting', {'id': sid})
        elem.text = val
        root.append(elem)
        changed = True
    if not changed:
        return False
    try:
        _write_text(
            backup_path,
            _serialize_userdata_settings(s for s in root if s.tag == 'setting'),
        )
    except OSError as exc:
        if log:
            log(f'[settings_repair] Failed to patch Trakt creds in backup: {exc}')
        return False
    if log:
        log(f'[settings_repair] Patched Trakt credentials in {os.path.basename(backup_path)}')
    return True


def sync_trakt_credentials_into_backups(log=None) -> None:
    """After a successful auth/refresh, keep backups from resurrecting dead tokens."""
    _sync_credential_ids_into_backups(_TRAKT_CREDENTIAL_SETTING_IDS, log=log)


def sync_simkl_credentials_into_backups(log=None) -> None:
    """After Simkl auth, keep profile/health backups from wiping live tokens."""
    _sync_credential_ids_into_backups(_SIMKL_CREDENTIAL_SETTING_IDS, log=log)
    commit_live_settings_to_backups(log=log)


def purge_simkl_credentials_from_backups(log=None) -> None:
    """CRUD Delete: wipe Simkl tokens from every settings backup after revoke."""
    user_path = _userdata_path()
    empties = {sid: '' for sid in _SIMKL_CREDENTIAL_SETTING_IDS}
    patched = 0
    for path in _iter_settings_backup_paths(user_path):
        try:
            if _patch_trakt_credentials_in_backup(path, empties, log=log):
                patched += 1
        except Exception as exc:
            if log:
                log(f'[settings_repair] Simkl backup purge skipped ({path}): {exc}')
    try:
        commit_live_settings_to_backups(log=log)
    except Exception as exc:
        if log:
            log(f'[settings_repair] Commit after Simkl purge failed: {exc}')
    if log:
        log(f'[settings_repair] Purged Simkl credentials from {patched} backup file(s)')


def _iter_settings_backup_paths(user_path: str | None = None) -> list[str]:
    """All known settings backup XML paths that can resurrect Trakt tokens."""
    user_path = user_path or _userdata_path()
    paths: list[str] = []
    seen: set[str] = set()

    def _add(path: str | None) -> None:
        if not path:
            return
        try:
            norm = os.path.normcase(os.path.abspath(path))
        except Exception:
            norm = path
        if norm in seen:
            return
        if _file_exists(path):
            seen.add(norm)
            paths.append(path)

    _add(health_backup_path(user_path))
    _add(original_backup_path(user_path))
    try:
        _add(find_settings_backup_for_import())
    except Exception:
        pass
    try:
        _add(profile_settings_backup_path())
    except Exception:
        pass
    try:
        _add(_legacy_profile_settings_backup_path())
    except Exception:
        pass

    # Leftovers users still delete by hand: settings_3.0.7.xml.bak, etc.
    parent = os.path.dirname(user_path.rstrip('\\/')) or ''
    base = os.path.basename(user_path)
    if parent and os.path.isdir(parent):
        try:
            for name in os.listdir(parent):
                lower = name.lower()
                if not (lower.endswith('.bak') or '.health_backup_' in lower):
                    continue
                if not (lower.startswith('settings') or lower.startswith(base.lower())):
                    continue
                _add(os.path.join(parent, name))
        except OSError:
            pass
    return paths


def purge_trakt_credentials_from_backups(log=None) -> None:
    """
    Wipe Trakt tokens from every settings backup and stamp trakt.revoked=true.

    Intentional clear/revoke must not leave bak/health/profile snapshots that
    repair can rehydrate — users should never have to delete XMLs by hand.
    """
    user_path = _userdata_path()
    empties = {sid: '' for sid in _TRAKT_CREDENTIAL_SETTING_IDS}
    empties['trakt.revoked'] = 'true'
    patched = 0
    for path in _iter_settings_backup_paths(user_path):
        try:
            if _patch_trakt_credentials_in_backup(path, empties, log=log):
                patched += 1
        except Exception as exc:
            if log:
                log(f'[settings_repair] Trakt backup purge skipped ({path}): {exc}')
    try:
        commit_live_settings_to_backups(log=log)
    except Exception as exc:
        if log:
            log(f'[settings_repair] Commit after Trakt purge failed: {exc}')
    if log:
        log(f'[settings_repair] Purged Trakt credentials from {patched} backup file(s)')


def _sync_credential_ids_into_backups(ids: frozenset, log=None) -> None:
    user_path = _userdata_path()
    if not _file_exists(user_path):
        return
    try:
        current = parse_userdata_settings(user_path)
    except Exception:
        return
    creds = {sid: (current.get(sid) or '') for sid in ids}
    # Require at least one non-empty credential value
    if not any((v or '').strip() for v in creds.values()):
        return
    try:
        commit_live_settings_to_backups(log=log)
    except Exception as exc:
        if log:
            log(f'[settings_repair] Commit after credential sync failed: {exc}')
    for path in _iter_settings_backup_paths(user_path):
        try:
            _patch_trakt_credentials_in_backup(path, creds, log=log)
        except Exception as exc:
            if log:
                log(f'[settings_repair] Credential backup patch skipped ({path}): {exc}')


def _setting_is_true(value: str) -> bool:
    return (value or '').strip().lower() in ('true', '1', 'yes')


def _trakt_intentionally_revoked(vals: dict | None) -> bool:
    """Honor explicit Midnight Trakt revoke — do not rehydrate tokens from backups."""
    if not vals:
        return False
    return _setting_is_true(vals.get('trakt.revoked', ''))


def load_schema_defaults(schema_path: str | None = None) -> dict[str, str]:
    """Default values from resources/settings.xml (default= attribute or element text)."""
    path = schema_path or _schema_path()
    if not _file_exists(path):
        return {}
    try:
        text = _read_text(path)
    except OSError:
        return {}
    defaults: dict[str, str] = {}
    for m in re.finditer(
        r'<setting id="([^"]+)"([^>]*?)(?:/>|>([^<]*)</setting>)',
        text,
        flags=re.DOTALL,
    ):
        sid = m.group(1)
        attrs = m.group(2) or ''
        body = m.group(3) or ''
        dm = re.search(r'default="([^"]*)"', attrs)
        defaults[sid] = dm.group(1) if dm is not None else body
    return defaults


def create_version_health_backup(
    userdata_path: str | None = None,
    version: str | None = None,
    log=None,
) -> str | None:
    """Snapshot current userdata before repair for this schema version."""
    user_path = userdata_path or _userdata_path()
    if not _file_exists(user_path):
        return None
    backup = health_backup_path(user_path, version)
    try:
        _copy_file(user_path, backup)
    except OSError as exc:
        if log:
            log(f'[settings_repair] Health backup failed: {exc}')
        return None
    if log:
        log(f'[settings_repair] Health backup: {os.path.basename(backup)}')
    return backup


def export_profile_settings_backup(
    userdata_path: str | None = None,
    log=None,
    force: bool = False,
) -> str | None:
    """Copy userdata to KodiRoot/midnight_settings_backup/ (outside userdata/addon_data)."""
    user_path = userdata_path or _userdata_path()
    dest = profile_settings_backup_path()
    if not dest or not _file_exists(user_path):
        return None
    if not _ensure_external_backup_dir(log=log):
        return None

    # Never export a live file that would wipe non-empty auth keys already in the
    # profile backup (classic boot race: Kodi left default=\"true\" empty poison).
    # force=True (CRUD commit after auth/revoke) bypasses — live is intentional.
    if not force and _file_exists(dest):
        try:
            live = parse_userdata_settings(user_path)
            backed = parse_userdata_settings(dest)
            would_wipe = []
            for sid in _SENSITIVE_SETTING_IDS:
                bval = (backed.get(sid) or '').strip()
                lval = (live.get(sid) or '').strip()
                if bval and not lval:
                    would_wipe.append(sid)
            if would_wipe:
                if log:
                    keys = ', '.join(sorted(would_wipe))
                    log(
                        f'[settings_repair] Skipping profile export — live file would wipe '
                        f'auth keys still present in backup: {keys}'
                    )
                return dest
        except Exception as exc:
            if log:
                log(f'[settings_repair] Profile export guard skipped: {exc}')

    try:
        _copy_file(user_path, dest)
    except OSError as exc:
        if log:
            log(f'[settings_repair] Profile export failed: {exc}')
        return None
    if log:
        log(f'[settings_repair] Profile settings backup: {dest}')
    return dest


def import_userdata_from_profile_backup(
    userdata_path: str | None = None,
    log=None,
) -> bool:
    """
    Seed userdata from profile backup when missing or auth keys were wiped.

    Fresh install (no settings.xml): full copy from profile backup.
    Existing userdata with empty auth keys: merge those keys only — never replace
    the whole file (that used to stomp last_version_announcement / schema markers
    back to stale backup values and re-trigger the welcome dialog every boot).
    """
    user_path = userdata_path or _userdata_path()
    profile_backup = find_settings_backup_for_import()
    if not profile_backup or not _file_exists(profile_backup):
        return False

    # Missing userdata — full seed is correct.
    if not _file_exists(user_path):
        try:
            raw = _strip_xml_declaration(_read_text(profile_backup))
        except OSError as exc:
            if log:
                log(f'[settings_repair] Profile backup read failed: {exc}')
            return False
        if raw and not raw.endswith('\n'):
            raw += '\n'
        try:
            from . import control
            control.makeFile(os.path.dirname(user_path))
            _write_text(user_path, raw)
        except OSError as exc:
            if log:
                log(f'[settings_repair] Profile import write failed: {exc}')
            return False
        if log:
            log('[settings_repair] Seeded userdata from profile settings backup')
        return True

    # Existing file — only fill empty sensitive keys from backup.
    current = parse_userdata_settings(user_path)
    backup_vals = parse_userdata_settings(profile_backup)
    revoked = _trakt_intentionally_revoked(current)
    backup_revoked = _trakt_intentionally_revoked(backup_vals)
    needs_merge = False
    for sid in _SENSITIVE_SETTING_IDS:
        if sid in _TRAKT_CREDENTIAL_SETTING_IDS and (revoked or backup_revoked):
            continue
        if backup_vals.get(sid) and not (current.get(sid) or '').strip():
            needs_merge = True
            break
    if not needs_merge:
        return False

    merged = merge_user_values_from_backup(user_path, profile_backup, log=log)
    if merged and log:
        log(
            f'[settings_repair] Merged {merged} auth/setting value(s) from profile backup '
            '(kept forward-only markers)'
        )
    return bool(merged)


def _values_differ(current: str, backup: str, default: str) -> bool:
    """True when backup holds a user value that current lost (empty or reverted to default)."""
    if backup == current:
        return False
    if not backup.strip():
        return False
    if not current.strip() or current == default:
        return backup != default or bool(backup.strip())
    return False


def merge_user_values_from_backup(
    userdata_path: str,
    backup_path: str,
    schema_path: str | None = None,
    log=None,
) -> int:
    """
    Merge customized values from a backup into current userdata.

    Only restores keys still in the schema. Returns count of values merged.
    """
    if not _file_exists(userdata_path) or not _file_exists(backup_path):
        return 0

    schema_ids = load_schema_ids(schema_path)
    if not schema_ids:
        return 0

    defaults = load_schema_defaults(schema_path)
    backup_vals = parse_userdata_settings(backup_path)
    current_vals = parse_userdata_settings(userdata_path)
    if not backup_vals:
        return 0

    to_apply: dict[str, str] = {}
    revoked = _trakt_intentionally_revoked(current_vals)
    backup_revoked = _trakt_intentionally_revoked(backup_vals)
    if _is_frozen_original_bak(backup_path):
        if log:
            log(
                f'[settings_repair] Refusing merge from frozen {os.path.basename(backup_path)} '
                '(use profile/health backup only)'
            )
        return 0
    for sid in schema_ids:
        if sid not in backup_vals:
            continue
        bval = backup_vals[sid]
        cval = current_vals.get(sid, '')
        dval = defaults.get(sid, '')
        if sid in _SENSITIVE_SETTING_IDS:
            if sid in _TRAKT_CREDENTIAL_SETTING_IDS and (revoked or backup_revoked):
                continue
            if bval.strip() and not cval.strip():
                to_apply[sid] = bval
        elif sid in _FORWARD_ONLY_SETTING_IDS:
            continue
        elif _values_differ(cval, bval, dval):
            to_apply[sid] = bval

    if not to_apply:
        return 0

    try:
        tree = _parse_xml_file(userdata_path)
    except ET.ParseError as exc:
        if log:
            log(f'[settings_repair] Merge parse failed: {exc}')
        return 0

    root = tree.getroot()
    ordered = []
    seen = set()
    for setting in root:
        if setting.tag != 'setting':
            continue
        sid = setting.attrib.get('id')
        if sid in to_apply:
            setting.text = to_apply[sid]
            setting.attrib.pop('default', None)
            seen.add(sid)
        ordered.append(setting)
    for sid, val in to_apply.items():
        if sid in seen:
            continue
        elem = ET.Element('setting', {'id': sid})
        elem.text = val
        ordered.append(elem)
    try:
        _write_text(userdata_path, _serialize_userdata_settings(ordered))
    except OSError as exc:
        if log:
            log(f'[settings_repair] Merge write failed: {exc}')
        return 0

    if log:
        log(f'[settings_repair] Merged {len(to_apply)} setting(s) from {os.path.basename(backup_path)}')
    return len(to_apply)


def persist_setting_on_disk(setting_id: str, value: str, log=None) -> bool:
    """Write one setting to userdata XML without Kodi setSetting (safe during boot races)."""
    user_path = _userdata_path()
    if not setting_id or not _file_exists(user_path):
        return False
    try:
        tree = _parse_xml_file(user_path)
    except ET.ParseError as exc:
        if log:
            log(f'[settings_repair] persist_setting_on_disk parse failed: {exc}')
        return False
    root = tree.getroot()
    seen = False
    for setting in root:
        if setting.tag != 'setting':
            continue
        if setting.attrib.get('id') == setting_id:
            setting.text = value
            setting.attrib.pop('default', None)
            seen = True
            break
    if not seen:
        elem = ET.Element('setting', {'id': setting_id})
        elem.text = value
        root.append(elem)
    try:
        _write_text(
            user_path,
            _serialize_userdata_settings(s for s in root if s.tag == 'setting'),
        )
    except OSError as exc:
        if log:
            log(f'[settings_repair] persist_setting_on_disk write failed: {exc}')
        return False
    if log:
        log(f'[settings_repair] Persisted {setting_id} on disk (no setSetting)')
    return True


def repair_user_settings_for_upgrade(
    userdata_path: str | None = None,
    schema_path: str | None = None,
    version: str | None = None,
    log=None,
    sync_kodi: bool = False,
) -> dict:
    """
    Full upgrade path: profile import -> health backup -> repair -> merge back.

    Returns summary dict with keys: imported, backup, repaired, merged, profile_export.
    """
    user_path = userdata_path or _userdata_path()
    ver = version or _SCHEMA_VERSION
    summary = {
        'imported': False,
        'backup': None,
        'repaired': False,
        'merged': 0,
        'profile_export': None,
    }

    summary['imported'] = import_userdata_from_profile_backup(user_path, log=log)
    summary['backup'] = create_version_health_backup(user_path, ver, log=log)

    summary['repaired'] = repair_user_settings(
        userdata_path=user_path,
        schema_path=schema_path,
        log=log,
        sync_kodi=sync_kodi,
    )

    backup = summary['backup'] or health_backup_path(user_path, ver)
    if _file_exists(backup):
        summary['merged'] = merge_user_values_from_backup(
            user_path, backup, schema_path=schema_path, log=log,
        )

    if _file_exists(user_path):
        summary['profile_export'] = export_profile_settings_backup(user_path, log=log)

    return summary


def ensure_settings_header(userdata_path: str, log=None) -> bool:
    """No-op: Kodi 21 Omega userdata settings.xml has no XML declaration.

    We used to prepend <?xml ...?> because we thought Kodi required it. Wrong for
    Omega: Kodi strips the declaration and can leave an empty settings.xml, which
    retriggers repair every boot / ~30s. Healthy files start with <settings version="2">.
    """
    return False


def repair_all_known_headers(log=None) -> int:
    """No-op kept for callers. See ensure_settings_header."""
    return 0


def _sync_userdata_to_kodi(user_path: str, log=None) -> int:
    """
    Push on-disk settings into Kodi's in-memory addon settings.

    Kodi loads userdata once at addon start; after we repair orphans on disk,
    sync values in-process so the running session sees them.
    """
    global _repair_in_progress
    try:
        import xbmcaddon
        addon = xbmcaddon.Addon('plugin.video.midnight')
    except Exception:
        return 0

    synced = 0
    _repair_in_progress = True
    try:
        tree = _parse_xml_file(user_path)
        for setting in tree.getroot():
            if setting.tag != 'setting':
                continue
            sid = setting.attrib.get('id')
            if not sid:
                continue
            val = setting.text if setting.text is not None else ''
            try:
                addon.setSetting(id=sid, value=val)
            except Exception:
                try:
                    addon.setSetting(sid, val)
                except Exception:
                    continue
            synced += 1
    except Exception as exc:
        if log:
            log(f'[settings_repair] Kodi sync skipped: {exc}')
        return 0
    finally:
        _repair_in_progress = False

    if log and synced:
        log(f'[settings_repair] Synced {synced} setting(s) into Kodi runtime')
    return synced


def _clear_runtime_settings_cache() -> None:
    try:
        from .crewruntime import c
        c._settings._cache.clear()
    except Exception:
        pass


def repair_user_settings(
    userdata_path: str | None = None,
    schema_path: str | None = None,
    profile_path: str | None = None,
    log=None,
    sync_kodi: bool = False,
) -> bool:
    """
    Drop orphan setting elements from userdata settings.xml.
    Returns True when the file was modified.
    """
    global _repair_in_progress
    if _repair_in_progress:
        return False

    user_path = userdata_path or _userdata_path(profile_path)
    if not _file_exists(user_path):
        return False

    schema_ids = load_schema_ids(schema_path)
    if not schema_ids:
        if log:
            log('[settings_repair] Schema IDs unavailable — skip')
        return False

    try:
        tree = _parse_xml_file(user_path)
    except ET.ParseError as exc:
        if log:
            log(f'[settings_repair] Parse failed: {exc}')
        return False
    except OSError as exc:
        if log:
            log(f'[settings_repair] Read failed: {exc}')
        return False
    root = tree.getroot()
    removed = 0
    kept = []
    for setting in list(root):
        if setting.tag != 'setting':
            continue
        sid = setting.attrib.get('id')
        if not sid or sid not in schema_ids or re.match(r'^label\d+$', sid or ''):
            root.remove(setting)
            removed += 1
        else:
            kept.append(setting)

    if removed == 0:
        return False

    if not kept:
        if log:
            log('[settings_repair] Refusing to write empty settings.xml — restoring from .bak')
        return restore_user_settings_from_backup(user_path, log=log)

    preserve_original_backup(user_path, log=log)

    try:
        _write_text(user_path, _serialize_userdata_settings(kept))
    except OSError as exc:
        if log:
            log(f'[settings_repair] Write failed: {exc}')
        return False

    if log:
        log(f'[settings_repair] Repaired userdata settings.xml (removed {removed} orphan(s))')

    if sync_kodi:
        global _synced_this_process
        if not _synced_this_process:
            _sync_userdata_to_kodi(user_path, log=log)
            _synced_this_process = True
        _clear_runtime_settings_cache()
        _refresh_runtime_theme()
    return True


def _refresh_runtime_theme() -> None:
    try:
        from .crewruntime import c
        c._theme = c.appearance()
        c.art = c.get_art_path()
    except Exception:
        pass


def userdata_missing_header(userdata_path: str | None = None) -> bool:
    """Always False. Missing <?xml is normal for Kodi 21 Omega userdata."""
    return False


def _userdata_is_header_only_corpse(user_path: str) -> bool:
    """True when file parses but has almost no setting elements (empty/wiped)."""
    if not user_path or not _file_exists(user_path):
        return False
    return len(parse_userdata_settings(user_path)) < 5


# Kodi 21+ rejects self-closing text settings with no body (Omega settings load failure).
_OMEGA_TEXT_SETTING_DEFAULTS = {
    'adult_pin': ' ',
}


def normalize_omega_text_settings(userdata_path: str | None = None, log=None) -> bool:
    """Ensure known text settings have non-empty element bodies for Kodi 21."""
    user_path = userdata_path or _userdata_path()
    if not _file_exists(user_path):
        return False
    try:
        tree = _parse_xml_file(user_path)
    except ET.ParseError:
        return False
    root = tree.getroot()
    changed = False
    for setting in root:
        if setting.tag != 'setting':
            continue
        sid = setting.attrib.get('id')
        default_text = _OMEGA_TEXT_SETTING_DEFAULTS.get(sid)
        if default_text is None:
            continue
        if (setting.text or '').strip():
            continue
        setting.text = default_text
        setting.attrib.pop('default', None)
        changed = True
    if not changed:
        return False
    try:
        _write_text(
            user_path,
            _serialize_userdata_settings(s for s in root if s.tag == 'setting'),
        )
    except OSError as exc:
        if log:
            log(f'[settings_repair] Omega text normalize write failed: {exc}')
        return False
    if log:
        log('[settings_repair] Normalized empty text settings for Kodi 21')
    return True


def sanitize_sensitive_settings(userdata_path: str | None = None, log=None) -> bool:
    """
    Strip default=\"true\" poison from auth keys and restore empty tokens from backups.

    Empty <setting default=\"true\" id=\"trakt.token\" /> makes Kodi ignore any later
    text value until default= is removed. Often left behind after a failed refresh clear.
    """
    user_path = userdata_path or _userdata_path()
    if not _file_exists(user_path):
        return False

    current = parse_userdata_settings(user_path)
    restored: dict[str, str] = {}
    revoked = _trakt_intentionally_revoked(current)
    live_healthy = len(current) >= 20
    try:
        live_mtime = os.path.getmtime(user_path) if live_healthy else 0.0
    except OSError:
        live_mtime = 0.0

    # Prefer newest usable backup for sensitive keys (profile → health).
    # Never read the frozen one-time settings.xml.bak.
    candidates = []
    try:
        profile = find_settings_backup_for_import()
        if profile:
            candidates.append(profile)
    except Exception:
        pass
    health = health_backup_path(user_path)
    if health and _file_exists(health):
        candidates.append(health)

    for backup in candidates:
        if _is_frozen_original_bak(backup):
            continue
        # Newer healthy live beats older mirrors — empty keys are intentional
        # (auth/clear), not an accident to rehydrate from a stale snapshot.
        if live_mtime:
            try:
                if os.path.getmtime(backup) + 2.0 < live_mtime:
                    if log:
                        log(
                            f'[settings_repair] Skip rehydrate from older backup '
                            f'{os.path.basename(backup)} (live settings are newer)'
                        )
                    continue
            except OSError:
                pass
        try:
            bvals = parse_userdata_settings(backup)
        except Exception:
            continue
        # Never pull tokens from a backup that itself records an intentional revoke.
        if _trakt_intentionally_revoked(bvals):
            continue
        for sid in _SENSITIVE_SETTING_IDS:
            if revoked and sid in _TRAKT_CREDENTIAL_SETTING_IDS:
                continue
            if (current.get(sid) or '').strip():
                continue
            if sid in restored:
                continue
            bval = (bvals.get(sid) or '').strip()
            if bval:
                restored[sid] = bval

    try:
        tree = _parse_xml_file(user_path)
    except ET.ParseError:
        return False
    root = tree.getroot()
    changed = False
    seen = set()
    for setting in root:
        if setting.tag != 'setting':
            continue
        sid = setting.attrib.get('id')
        if sid not in _SENSITIVE_SETTING_IDS and sid not in _FOLDER_SETTING_IDS:
            continue
        seen.add(sid)
        if 'default' in setting.attrib:
            setting.attrib.pop('default', None)
            changed = True
        if sid in _FOLDER_SETTING_IDS:
            raw = (setting.text or '').strip()
            if raw.lower() in ('true', 'false'):
                setting.text = ''
                changed = True
        if sid in _SENSITIVE_SETTING_IDS and sid in restored and not (setting.text or '').strip():
            setting.text = restored[sid]
            changed = True

    for sid, val in restored.items():
        if sid in seen:
            continue
        elem = ET.Element('setting', {'id': sid})
        elem.text = val
        root.append(elem)
        changed = True

    if not changed:
        return False

    try:
        _write_text(
            user_path,
            _serialize_userdata_settings(s for s in root if s.tag == 'setting'),
        )
    except OSError as exc:
        if log:
            log(f'[settings_repair] sanitize_sensitive write failed: {exc}')
        return False

    if restored:
        try:
            import xbmcaddon
            addon = xbmcaddon.Addon('plugin.video.midnight')
            for sid, val in restored.items():
                addon.setSetting(sid, val)
        except Exception:
            pass
        if log:
            keys = ', '.join(sorted(restored.keys()))
            log(f'[settings_repair] Restored sensitive settings from backup: {keys}')
    elif log:
        log('[settings_repair] Stripped default= poison from sensitive settings')

    _clear_runtime_settings_cache()
    return True


def repair_if_needed(log=None) -> bool:
    """
    Repair userdata settings when needed (orphans, wiped file, sensitive keys).

    Do not add <?xml declarations — Kodi 21 Omega keeps settings.xml without them.
    Never sync via setSetting here — that triggers onSettingsChanged storms on Kodi 21.
    """
    global _repair_attempted
    user_path = _userdata_path()
    if not _file_exists(user_path):
        return False

    if _userdata_is_header_only_corpse(user_path):
        if log:
            log('[settings_repair] Empty/wiped settings.xml detected — restoring from profile/health backup')
        return restore_user_settings_from_backup(user_path, log=log)

    # Always run — cheap, and fixes default="true" token wipes from failed refresh clears.
    sanitized = sanitize_sensitive_settings(user_path, log=log)

    if normalize_omega_text_settings(user_path, log=log):
        return True

    if sanitized:
        return True

    if _repair_attempted:
        return False

    with _repair_lock:
        try:
            repaired = repair_user_settings(log=log, sync_kodi=False)
            if repaired:
                _repair_attempted = True
            return repaired
        except Exception as exc:
            if log:
                log(f'[settings_repair] Failed: {exc}')
            return False

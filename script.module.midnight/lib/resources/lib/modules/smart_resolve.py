# -*- coding: utf-8 -*-

'''
********************************************************cm*
* Midnight Add-on
*
* @file smart_resolve.py
* @package script.module.midnight
*
* @copyright (c) 2026, Midnight
* @license GNU General Public License, version 3 (GPL-3.0)
*
********************************************************cm*

Smart source resolution — unified queue, RD cooldown awareness, and resolve status overlay.

Used by playItem, sourcesDirect, and sourcesDialog so list-click and autoplay share one path.

'''
from __future__ import annotations

import json
import traceback

import xbmc
import xbmcgui

from . import control
from .crewruntime import c
from . import debrid_names
from .smart_resolve_queue import build_click_queue, build_smart_queue, is_realdebrid
from resources.lib.ui_theme import color



def _display_title(meta, fallback_title=''):
    meta = meta or {}
    tvshow = meta.get('tvshowtitle') or ''
    season = meta.get('season')
    episode = meta.get('episode')
    if tvshow and season is not None and episode is not None:
        try:
            return f"{tvshow} - S{int(season):02d}E{int(episode):02d}"
        except (TypeError, ValueError):
            return tvshow
    return meta.get('title') or fallback_title or ''


def _display_subtitle(meta):
    meta = meta or {}
    if meta.get('tvshowtitle'):
        return str(meta.get('title') or '')
    year = meta.get('year')
    return str(year) if year else ''


def _resolve_fanart(meta):
    meta = meta or {}
    for key in ('fanart', 'fanart2', 'thumb', 'poster'):
        val = meta.get(key)
        if val and str(val) not in ('0', ''):
            return str(val)
    try:
        return c.addon_fanart()
    except Exception:
        return ''


def _format_service(debrid_name):
    if not debrid_name:
        return 'Direct'
    name = str(debrid_name)
    low = name.lower()
    if 'real' in low and 'debrid' in low:
        return 'Real-Debrid'
    if 'premiumize' in low:
        return 'Premiumize'
    if 'alldebrid' in low or 'all-debrid' in low:
        return 'AllDebrid'
    if 'torbox' in low:
        return debrid_names.canonical('TorBox')
    return name


def load_container_sources(item_property='plugin.video.midnight.container.items'):
    """Load the full source list stored when the directory was built."""
    try:
        raw = control.window.getProperty(item_property)
        if not raw:
            return []
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception as e:
        c.log(f'[SmartResolve] Could not load container sources: {e}', 1)
        return []


def decode_play_item_source(source):
    """Normalize playItem source payload to a list with provider paths restored."""
    if isinstance(source, str):
        items = json.loads(source)
    elif isinstance(source, list):
        items = source
    elif isinstance(source, dict):
        items = [source]
    else:
        raise TypeError(f'Unsupported source payload type: {type(source)}')

    try:
        import base64 as _base64
        for it in items:
            enc = it.pop('_provider_enc', None)
            if enc:
                try:
                    it['provider'] = _base64.b64decode(enc.encode('ascii')).decode('utf-8')
                except Exception:
                    it['provider'] = it.get('provider') or ''
    except Exception:
        pass
    return items


class ResolveStatusOverlay(xbmcgui.WindowXMLDialog):
    """Fullscreen resolve status overlay with fanart background."""

    CANCEL_BUTTON = 6

    def __init__(self, *args, **kwargs):
        self._cancelled = False
        self._title = kwargs.pop('title', '')
        self._subtitle = kwargs.pop('subtitle', '')
        self._fanart = kwargs.pop('fanart', '')
        xbmcgui.WindowXMLDialog.__init__(self, *args, **kwargs)

    def onInit(self):
        try:
            self.setProperty('fanart', self._fanart)
            self.setProperty('title', self._title)
            self.setProperty('subtitle', self._subtitle)
            self.setProperty('status.main', 'Finding your stream…')
            self.setProperty('status.sub', '')
            self.setProperty('status.attempt', '')
            self.setProperty('resolve.spinning', '1')
        except Exception as e:
            c.log(f'[ResolveStatusOverlay] onInit error: {e}', 1)

    def update_status(self, main, sub='', attempt=''):
        try:
            if main:
                self.setProperty('status.main', main)
            self.setProperty('status.sub', sub or '')
            self.setProperty('status.attempt', attempt or '')
        except Exception as e:
            c.log(f'[ResolveStatusOverlay] update_status error: {e}', 1)

    def is_cancelled(self):
        if self._cancelled:
            return True
        try:
            if self.getProperty('resolve.action') == 'cancel':
                self._cancelled = True
        except Exception:
            pass
        return self._cancelled

    def onClick(self, controlID):
        if controlID == self.CANCEL_BUTTON:
            self._cancelled = True
            self.setProperty('resolve.action', 'cancel')
            self.close()

    def onAction(self, action):
        if action.getId() in (9, 10, 92, 216, 247, 257, 275, 61467, 61448):
            self._cancelled = True
            self.setProperty('resolve.action', 'cancel')
            self.close()

    def close(self):
        self._cancelled = True
        try:
            self.clearProperty('resolve.spinning')
            self.clearProperty('resolve.action')
        except Exception:
            pass
        try:
            super(ResolveStatusOverlay, self).close()
        except Exception:
            pass


class ExistingOverlayUI:
    """Adapter for the persistent FullscreenOverlay / OverlayDialog scrape UI."""

    def __init__(self, overlay_obj):
        self._overlay = overlay_obj

    def show(self):
        return

    def update_status(self, main, sub='', attempt=''):
        try:
            from .sources import update_active_overlay
            parts = [f'{color(main, 'header')}']
            if sub:
                parts.append(sub)
            if attempt:
                parts.append(attempt)
            update_active_overlay('\n'.join(parts), state='resolving')
        except Exception as e:
            c.log(f'[SmartResolve] ExistingOverlayUI update error: {e}', 1)

    def is_cancelled(self):
        try:
            if hasattr(self._overlay, 'iscanceled') and self._overlay.iscanceled():
                return True
        except Exception:
            pass
        return False

    def close(self):
        return


def open_resolve_overlay(meta, fallback_title=''):
    """Open the fullscreen resolve status overlay."""
    skin = c.appearance() or 'midnight'
    if skin in ('-', ''):
        skin = 'midnight'
    addon_path = c.get_artwork_path()
    overlay = ResolveStatusOverlay(
        'ResolveStatus.xml',
        addon_path,
        skin,
        '1080i',
        title=_display_title(meta, fallback_title),
        subtitle=_display_subtitle(meta),
        fanart=_resolve_fanart(meta),
    )
    overlay.show()
    return overlay


def _should_abort(play_attempt=None, overlay=None, debrid_mod=None):
    try:
        if control.monitor.abortRequested():
            return True
    except Exception:
        pass
    if overlay and overlay.is_cancelled():
        if debrid_mod is not None:
            debrid_mod.request_resolution_cancel()
        return True
    if play_attempt is not None and debrid_mod is not None:
        if debrid_mod.is_play_item_superseded(play_attempt):
            return True
    return False


def run_smart_resolve(
    sources_inst,
    primary_item,
    all_items=None,
    meta=None,
    fallback_title='',
    play_attempt=None,
    reuse_existing_overlay=False,
    show_failure_dialog=True,
    honour_click=False,
    start_index=None,
):
    """
    Walk a resolve queue until a source resolves or options are exhausted.

    honour_click=True (Directory / Dialog): start at the selected row, then walk
    the visible list. Auto Play keeps quality/debrid smart ranking.

    Returns resolved URL string, None on failure, 'close://' when user cancels.
    """
    meta = meta or {}
    if not primary_item and start_index is None:
        return None

    if not all_items:
        all_items = [primary_item] if primary_item else []

    season = getattr(sources_inst, 'season', None) or meta.get('season')
    episode = getattr(sources_inst, 'episode', None) or meta.get('episode')
    if honour_click:
        queue, list_indices = build_click_queue(
            all_items, primary=primary_item, start_index=start_index
        )
        c.log(
            f'[SmartResolve] Click queue start_index={start_index!r} '
            f'first_row={(list_indices[0] + 1) if list_indices else "?"} '
            f'size={len(queue)}',
            1,
        )
    else:
        if not primary_item:
            return None
        queue = build_smart_queue(all_items, primary_item, season, episode)
        list_indices = list(range(len(queue)))

    try:
        from resources.lib.modules import debrid as debrid_mod
    except Exception:
        debrid_mod = None

    overlay = None
    smart_prop = 'midnight.smart_resolve.active'
    try:
        control.window.setProperty(smart_prop, 'true')
        if reuse_existing_overlay:
            try:
                from .sources import get_active_overlay, is_overlay_active
                if is_overlay_active():
                    overlay = ExistingOverlayUI(get_active_overlay())
            except Exception:
                overlay = None
        if overlay is None:
            overlay = open_resolve_overlay(meta, fallback_title=fallback_title)

        if debrid_mod is not None:
            debrid_mod.clear_resolution_cancel()
            debrid_mod.close_shared_resolution_dialog()

        c.log(f'[SmartResolve] Starting resolve queue ({len(queue)} links)', 1)

        for index, item in enumerate(queue):
            if _should_abort(play_attempt, overlay, debrid_mod):
                c.log('[SmartResolve] Resolve aborted (cancel/supersede/exit)')
                return 'close://' if overlay and overlay.is_cancelled() else None

            debrid_name = item.get('debrid') or ''
            if debrid_mod is not None and debrid_names.is_torbox(debrid_name) and debrid_mod.is_torbox_resolve_blocked():
                c.log('[SmartResolve] Skipping TorBox link — session backoff after API timeout')
                continue

            quality = str(item.get('quality') or 'SD')
            service = _format_service(debrid_name)
            row_no = (list_indices[index] + 1) if index < len(list_indices) else (index + 1)
            if honour_click:
                attempt_label = f'Row {row_no:02d} | try {index + 1}/{len(queue)} | {quality}'
                try_main = f'Trying row {row_no:02d} | {quality} | {service}'
            else:
                attempt_label = f'Link {index + 1} of {len(queue)} | {quality}'
                try_main = f'Trying {quality} | {service}...'

            if debrid_mod is not None and is_realdebrid(debrid_name) and debrid_mod.is_rd_in_cooldown():
                remaining = debrid_mod.rd_cooldown_remaining()
                overlay.update_status(
                    'Switching to another service...',
                    f'Real-Debrid cooling down ({remaining:.0f}s) - avoiding rate limits',
                    attempt_label,
                )
                c.log(f'[SmartResolve] Skipping RD row during cooldown ({remaining:.1f}s left)')
                continue

            overlay.update_status(
                try_main,
                'Working through your source list',
                attempt_label,
            )
            # Brief hold so Directory clicks can be read before resolve finishes.
            xbmc.sleep(450 if honour_click else 50)

            try:
                url = sources_inst.sourcesResolve(item)
            except Exception as e:
                c.log(f'[SmartResolve] sourcesResolve error: {e}', 1)
                url = None

            if _should_abort(play_attempt, overlay, debrid_mod):
                return 'close://' if overlay and overlay.is_cancelled() else None

            if debrid_mod is not None and debrid_mod.is_resolution_cancel_requested():
                c.log('[SmartResolve] Resolution cancelled during attempt')
                return 'close://'

            if url:
                sources_inst.url = url
                if honour_click:
                    overlay.update_status(
                        f'Playing row {row_no:02d} | {quality} | {service}',
                        'Stream ready - starting playback...',
                        attempt_label,
                    )
                    xbmc.sleep(1600)
                else:
                    overlay.update_status('Stream found - starting playback...', '', attempt_label)
                    xbmc.sleep(400)
                c.log(
                    f'[SmartResolve] Success row {row_no:02d} '
                    f'(try {index + 1}/{len(queue)}, {service})',
                    1,
                )
                return url

            if debrid_mod is not None and is_realdebrid(debrid_name) and debrid_mod.is_rd_in_cooldown():
                overlay.update_status(
                    'This link is not on Real-Debrid',
                    'Trying another service...',
                    attempt_label,
                )

        c.log(f'[SmartResolve] Queue exhausted ({len(queue)} links, none playable)')
        if show_failure_dialog and hasattr(sources_inst, 'errorForSources'):
            if play_attempt is None or debrid_mod is None or not debrid_mod.is_play_item_superseded(play_attempt):
                sources_inst.errorForSources()
        return None
    except Exception as e:
        c.log(f'[SmartResolve] run_smart_resolve exception: {e}', 1)
        c.log(traceback.format_exc(), 1)
        return None
    finally:
        try:
            control.window.clearProperty(smart_prop)
        except Exception:
            pass
        if overlay is not None and not isinstance(overlay, ExistingOverlayUI):
            try:
                overlay.close()
            except Exception:
                pass

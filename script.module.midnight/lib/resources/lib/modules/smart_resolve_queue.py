# -*- coding: utf-8 -*-

'''
********************************************************cm*
* Midnight Add-on
*
* @file smart_resolve_queue.py
* @package script.module.midnight
*
* @copyright (c) 2026, Midnight
* @license GNU General Public License, version 3 (GPL-3.0)
*
********************************************************cm*
Pure queue logic for smart source resolution (no Kodi imports).

'''


from __future__ import annotations

import re

QUALITY_RANK = {
    '4k': 0,
    '1440p': 1,
    '1080p': 2,
    'hd': 3,
    '720p': 3,
    'sd': 4,
    'scr': 5,
    'cam': 6,
}

_CACHE_KEYS = ('cached', 'is_cached', 'debrid_cached', 'cached_rd', 'cache')


def normalize_debrid(name):
    if not name:
        return ''
    return re.sub(r'[^a-z0-9]+', '', str(name).lower())


def is_realdebrid(name):
    n = normalize_debrid(name)
    return n in ('realdebrid', 'rd')


def quality_rank(quality):
    return QUALITY_RANK.get(str(quality or 'sd').lower(), 4)


def source_key(item):
    return (item.get('url') or '', normalize_debrid(item.get('debrid')))


def is_cached(item):
    for key in _CACHE_KEYS:
        if item.get(key) in (True, 'true', 1, '1', 'cached'):
            return True
    return False


def episode_score(item, season, episode):
    score = 0
    if season is None or episode is None:
        return score
    try:
        s_int = int(season)
        e_int = int(episode)
    except (TypeError, ValueError):
        return score
    combined = ' '.join(
        str(item.get(k) or '') for k in ('name', 'label', 'info')
    ).lower()
    exact = f's{s_int:02d}e{e_int:02d}'
    if exact in combined:
        score += 80
    elif f'episode {e_int}' in combined:
        score += 30
    if any(t in combined for t in ('complete', 'season pack', 'full season', 's01-s')):
        score -= 20
    return score


def find_click_index(all_items, primary=None, start_index=None):
    """Resolve which visible row the user clicked (index preferred, then URL key)."""
    items = list(all_items or [])
    if not items:
        return 0

    if start_index is not None and str(start_index).strip() != '':
        try:
            idx = int(start_index)
            if 0 <= idx < len(items):
                return idx
        except (TypeError, ValueError):
            pass

    if primary is not None:
        primary_key = source_key(primary)
        if primary_key[0]:
            for idx, item in enumerate(items):
                if source_key(item) == primary_key:
                    return idx
        # Soft match when URL payload was truncated in the plugin URL.
        want = (
            (primary.get('name') or primary.get('label') or '').lower(),
            str(primary.get('quality') or '').lower(),
            normalize_debrid(primary.get('debrid')),
            str(primary.get('source') or '').lower(),
            str(primary.get('provider') or '').lower(),
        )
        if want[0]:
            for idx, item in enumerate(items):
                got = (
                    (item.get('name') or item.get('label') or '').lower(),
                    str(item.get('quality') or '').lower(),
                    normalize_debrid(item.get('debrid')),
                    str(item.get('source') or '').lower(),
                    str(item.get('provider') or '').lower(),
                )
                if got == want:
                    return idx

    return 0


def build_click_queue(all_items, primary=None, start_index=None):
    """
    Directory / dialog click: honour the selected row, then walk the visible list
    downward and wrap (Classy-style). Does not re-rank by quality.

    Returns (queue, list_indices) where list_indices[i] is the 0-based row in
    the visible source list for queue[i].
    """
    items = list(all_items or [])
    if not items:
        if primary:
            return [primary], [0]
        return [], []

    start = find_click_index(items, primary=primary, start_index=start_index)
    order = list(range(start, len(items))) + list(range(0, start))

    seen = set()
    queue = []
    list_indices = []
    for idx in order:
        item = items[idx]
        key = source_key(item)
        if not key[0] or key in seen:
            continue
        seen.add(key)
        queue.append(item)
        list_indices.append(idx)
    if not queue and primary:
        return [primary], [start]
    return queue, list_indices


def build_smart_queue(all_items, primary, season=None, episode=None, debrid_mod=None):
    """Build a debrid- and quality-aware resolve queue (Auto Play / sourcesDirect)."""
    if not all_items:
        return [primary] if primary else []

    if debrid_mod is None:
        try:
            from resources.lib.modules import debrid as debrid_mod
        except Exception:
            debrid_mod = None

    primary_key = source_key(primary)
    primary_q = quality_rank(primary.get('quality'))

    def score_item(item):
        if source_key(item) == primary_key:
            return 10_000_000

        score = 0
        q = quality_rank(item.get('quality'))
        if q == primary_q:
            score += 5000
        elif q > primary_q:
            score += 3000 - (q - primary_q) * 200
        else:
            score += 1000 - (primary_q - q) * 100

        if is_cached(item):
            score += 400
        if item.get('debrid'):
            score += 50
        score += episode_score(item, season, episode)

        if debrid_mod is not None:
            if debrid_mod.is_rd_in_cooldown() and is_realdebrid(item.get('debrid')):
                score -= 8000
            elif debrid_mod.rd_451_was_hit() and is_realdebrid(item.get('debrid')):
                score -= 500

        return score

    ranked = sorted(all_items, key=score_item, reverse=True)
    # Always try the clicked/selected primary first, even when URL-key match failed
    # (truncated plugin URLs on Fire Stick / long magnets).
    head = None
    click_idx = find_click_index(all_items, primary=primary)
    if 0 <= click_idx < len(all_items):
        head = all_items[click_idx]
    elif primary:
        head = primary

    seen = set()
    queue = []
    if head is not None:
        key = source_key(head)
        if key[0]:
            seen.add(key)
            queue.append(head)
        elif head:
            queue.append(head)

    for item in ranked:
        key = source_key(item)
        if not key[0] or key in seen:
            continue
        seen.add(key)
        queue.append(item)
    return queue

# -*- coding: utf-8 -*-

'''
 ***********************************************************
 * Midnight Add-on
 *
 * @file tracker.py
 * @package script.module.midnight
 *
 * Exclusive watch-tracker facade: Trakt XOR Simkl.
 * Navigator / menus should ask this module — not scrape
 * tracker.service / dual scrobble flags themselves.
 *
 ********************************************************cm*
'''

from . import simkl

# Setting enum values (tracker.service) — Trakt=0, Simkl=1. No "both".
TRACKER_TRAKT = 0
TRACKER_SIMKL = 1

# Simkl library status buckets (no Trakt-style collection)
SIMKL_STATUS_WATCHING = 'watching'
SIMKL_STATUS_PLANTOWATCH = 'plantowatch'
SIMKL_STATUS_HOLD = 'hold'
SIMKL_STATUS_DROPPED = 'dropped'
SIMKL_STATUS_COMPLETED = 'completed'


def active_service() -> int:
    """Return TRACKER_TRAKT or TRACKER_SIMKL."""
    return simkl.get_tracker_service()


def active_name() -> str:
    """'trakt' or 'simkl' — stable id for logs / conditionals."""
    return 'simkl' if active_service() == TRACKER_SIMKL else 'trakt'


def is_simkl() -> bool:
    return active_service() == TRACKER_SIMKL


def is_trakt() -> bool:
    return active_service() == TRACKER_TRAKT


def supports_collection() -> bool:
    """Trakt collection (owned media). Simkl has no equivalent — hide menus."""
    return is_trakt()


def supports_watchlist() -> bool:
    """Both: Trakt watchlist / Simkl plantowatch."""
    return True


def supports_progress() -> bool:
    """Continue watching / in-progress — both (different endpoints)."""
    return True


def trakt_live() -> bool:
    """
    Trakt is the active tracker and authorized.
    Prefer this (or trakt.get_trakt_credentials_info after XOR gate)
    for menus / progress / sync — not raw token checks.
    """
    if not is_trakt():
        return False
    from . import trakt
    return bool(trakt.has_trakt_token())


def simkl_live() -> bool:
    """Simkl is the active tracker and authorized."""
    if not is_simkl():
        return False
    return bool(simkl.get_simkl_credentials_info())


def watchlist_status_for_active() -> str:
    """
    Status/bucket id for "watchlist-like" menus.
    Trakt callers ignore this; Simkl uses plantowatch.
    """
    return SIMKL_STATUS_PLANTOWATCH


def display_label_prefix() -> str:
    """Short brand for menu titles (Trakt / Simkl)."""
    return 'Simkl' if is_simkl() else 'Trakt'

# -*- coding: utf-8 -*-

'''
Canonical debrid service name matching — avoids TorBox/Torbox/Real-Debrid casing bugs.
'''

from __future__ import annotations

import re

# Display names ResolveURL / Midnight use in source rows
CANONICAL = {
    'realdebrid': 'Real-Debrid',
    'alldebrid': 'AllDebrid',
    'premiumizeme': 'Premiumize.me',
    'torbox': 'TorBox',
}

KNOWN_DEBRIDS = frozenset(CANONICAL.keys())


def normalize(name):
    """Case/punctuation insensitive key (torbox, TorBox, Torbox -> torbox)."""
    return re.sub(r'[^a-z0-9]+', '', str(name or '').lower())


def match(a, b):
    na, nb = normalize(a), normalize(b)
    return bool(na) and na == nb


def is_known_debrid(name):
    return normalize(name) in KNOWN_DEBRIDS


def canonical(name):
    """Best display name for a debrid service string."""
    return CANONICAL.get(normalize(name), str(name or '').strip())


def is_torbox(name):
    return normalize(name) == 'torbox'


def is_realdebrid(name):
    n = normalize(name)
    return n in ('realdebrid', 'rd')


def is_alldebrid(name):
    return normalize(name) == 'alldebrid'


def is_premiumize(name):
    return normalize(name) == 'premiumizeme'

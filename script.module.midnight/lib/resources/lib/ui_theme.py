# -*- coding: utf-8 -*-
"""
Midnight theme — central colour palette for the Midnight Kodi addon.

All UI colours go through this module so the whole addon can be
recoloured from one place. Colours are Kodi AARRGGBB hex strings
(FF = fully opaque).

Roles:
  header    - main menu section names, service names, dialog titles
  primary   - ordinary content labels, normal listings
  secondary - metadata, descriptions, disabled/greyed text
  accent    - positive states (installed, active, working, found)
  warning   - cautions (nothing found yet, expiring soon, user-disabled)
  error     - failures (blocked, errors, expired, not installed)
"""

COLORS = {
    'header': 'FF56B6B0',      # muted teal
    'primary': 'FFE5E9EF',     # soft white
    'secondary': 'FF8C98A8',   # slate
    'accent': 'FF56B6B0',      # muted teal (same as header)
    'warning': 'FFE5B86A',     # warm amber
    'error': 'FFD97979',       # muted red
}

# Legacy Kodi colour names mapped onto the Midnight roles. Used where
# the stored value is a colour name chosen by the user (torrent /
# premium identify colours, unaired identify colour). 'nocolor' maps
# to None (no colour). The mapping matches the colour each name was
# displayed in (for example the "Yellow Green" option was shown in
# yellow, so it maps to warning).
_NAME_ROLES = {
    'orchid': 'header',
    'white': 'primary',
    'red': 'error',
    'crimson': 'error',
    'firebrick': 'error',
    'orange': 'warning',
    'yellow': 'warning',
    'yellowgreen': 'warning',
    'gold': 'header',
    'grey': 'secondary',
    'gray': 'secondary',
    'cyan': 'secondary',
    'lightblue': 'header',
    'lightskyblue': 'header',
    'skyblue': 'header',
    'blue': 'secondary',
    'lime': 'accent',
    'lawngreen': 'accent',
    'green': 'accent',
    'limegreen': 'accent',
    'forestgreen': 'accent',
    'mediumseagreen': 'accent',
    'purple': 'header',
    'magenta': 'header',
    'deeppink': 'header',
    'pink': 'header',
}


def color(text, name='primary'):
    """Wrap text in a Kodi [COLOR] tag using the Midnight palette."""
    return '[COLOR {}]{}[/COLOR]'.format(COLORS[name], text)


def role_for_name(name):
    """Map a legacy Kodi colour name to a Midnight role (or None)."""
    if not name:
        return None
    name = str(name).strip().lower()
    if name in ('nocolor', 'none', ''):
        return None
    return _NAME_ROLES.get(name, 'secondary')

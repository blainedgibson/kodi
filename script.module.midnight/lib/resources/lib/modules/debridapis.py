# -*- coding: utf-8 -*-

'''
 ***********************************************************
 * Midnight Add-on
 *
 *
 * @file debridapis.py (API Compatibility Layer)
 * @package script.module.midnight
 *
 * @copyright (c) 2023-2026, Midnight
 * @license GNU General Public License, version 3 (GPL-3.0)
 *
 * Thin re-export layer: maps lowercase aliases (realdebrid, alldebrid, premiumize)
 * to the canonical API classes in apis/. Used by debrid_cloud.py.
 *
 ********************************************************cm*
'''

# Import the new API classes from dedicated modules
from ..apis.realdebrid_api import RealDebridAPI
from ..apis.alldebrid_api import AllDebridAPI
from ..apis.premiumize_api import PremiumizeAPI
from ..apis.torbox_api import TorBoxAPI

# Backward compatibility aliases (lowercase names)
realdebrid = RealDebridAPI
alldebrid = AllDebridAPI
premiumize = PremiumizeAPI
torbox = TorBoxAPI

# Export for import * statements
__all__ = ['realdebrid', 'alldebrid', 'premiumize', 'torbox',
           'RealDebridAPI', 'AllDebridAPI', 'PremiumizeAPI', 'TorBoxAPI']

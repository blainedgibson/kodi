# -*- coding: utf-8 -*-

'''
 ***********************************************************
 * Midnight Add-on
 *
 * Thin launcher - all service logic lives in
 * script.module.midnight/lib/resources/lib/modules/crewservice.py
 *
 * @file service.py
 * @package plugin.video.midnight
 *
 * @copyright (c) 2023 - 2026, Midnight
 * @license GNU General Public License, version 3 (GPL-3.0)
 *
 ********************************************************cm*
'''

# cm - 04/25/2026 - reduced to launcher; all logic moved to crewservice.py in the module

import os
import sys
import xbmcaddon

_crew_addon = xbmcaddon.Addon('script.module.midnight')
_lib_path = os.path.join(_crew_addon.getAddonInfo('path'), 'lib')
if _lib_path not in sys.path:
    sys.path.insert(0, _lib_path)

try:
    from resources.lib.modules.settings_repair import repair_if_needed
    repair_if_needed()
except Exception:
    pass

from resources.lib.modules import crewservice
crewservice.start()

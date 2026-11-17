# -*- coding: utf-8 -*-

'''
********************************************************cm*
* Midnight Add-on
*
* @file midnight.py
* @package plugin.video.midnight
*
* @copyright (c) 2026, Midnight
* @license GNU General Public License, version 3 (GPL-3.0)
*
********************************************************cm*
'''

import sys
import time
import os
import xbmcaddon
from urllib.parse import parse_qsl

# Repair userdata before any Midnight import reads settings (Kodi 21 + boot widget race).
try:
	import sys as _sys
	_crew_mod = xbmcaddon.Addon('script.module.midnight').getAddonInfo('path')
	_lib = os.path.join(_crew_mod, 'lib')
	if _lib not in _sys.path:
		_sys.path.insert(0, _lib)
	from resources.lib.modules.settings_repair import repair_if_needed
	repair_if_needed()
except Exception:
	pass

from resources.lib.modules.crewruntime import c

_entry_started = time.perf_counter()

if c.devmode:
	c.log('[MIDNIGHT ENTRY] midnight.py STARTED - argv=' + str(sys.argv), trace=1)

_import_started = time.perf_counter()

from resources.lib.modules import crew
from resources.lib.modules.perf import perf_log

perf_log('import crew module', _import_started)

try:
	if c.devmode:
		c.log('[MIDNIGHT ENTRY] crew module imported successfully', 1)
except Exception:
	pass

try:
	if c.devmode:
		c.log('[MIDNIGHT ENTRY] Parsing params from argv[2]=' + str(sys.argv[2] if len(sys.argv) > 2 else 'MISSING'), 1)
	params = dict(parse_qsl(sys.argv[2].replace('?', ''))) if len(sys.argv) > 2 else {}
	if c.devmode:
		c.log('[MIDNIGHT ENTRY] Parsed params=' + str(params), 1)
except Exception as e:
	if c.devmode:
		c.log('[MIDNIGHT ENTRY] Error parsing params: ' + str(e), 1)
	params = {}

_router_started = time.perf_counter()
try:
	if c.devmode:
		c.log('[MIDNIGHT ENTRY] Calling crew.router with params=' + str(params), 1)
	crew.router(params)
	perf_log('plugin.router action=' + str(params.get('action', '<default>')), _router_started)
	perf_log('plugin.total', _entry_started)
	if c.devmode:
		c.log('[MIDNIGHT ENTRY] crew.router completed', 1)
except Exception as e:
	perf_log('plugin.router FAILED action=' + str(params.get('action', '<default>')), _router_started)
	perf_log('plugin.total', _entry_started)
	if c.devmode:
		c.log('[MIDNIGHT ENTRY] crew.router FAILED: ' + str(e), 1)
		import traceback
		c.log('[MIDNIGHT ENTRY] Traceback: ' + traceback.format_exc(), 1)
	# Always close the directory handle so Kodi does not spin on Cursor.busy() forever.
	try:
		from resources.lib.modules import control
		if len(sys.argv) > 1:
			control.idle()
			control.directory(int(sys.argv[1]), cacheToDisc=False)
	except Exception:
		pass

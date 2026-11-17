# -*- coding: utf-8 -*-

'''
********************************************************cm*
* Midnight Add-on - External pack client bridge
*
* @file pack_client.py
* @package script.module.midnight
*
* Route Coco/Gears/Viper scraper HTTP through Midnight client (SSL, cfscrape,
* Xbox UWP fixes) while keeping pack scraper modules unchanged.
********************************************************cm*
'''

import sys
import types

from .crewruntime import c

_PACK_CLIENT_MODULES = (
    'cocoscrapers.modules.client',
    'gearsscrapers.modules.client',
    'viperscrapers.modules.client',
    'classyscrapers.modules.client',
)

_adapter = None
_installed = False


def _build_adapter(crew_client):
    """Module-like object exposing Fenom/Coco/Gears client API on top of Midnight client."""
    mod = types.ModuleType('midnight.pack_client_adapter')

    def request(
        url,
        close=True,
        redirect=True,
        error=False,
        proxy=None,
        post=None,
        headers=None,
        mobile=False,
        XHR=False,
        limit=None,
        referer=None,
        cookie=None,
        compression=True,
        output='',
        timeout='30',
        verifySsl=True,
        flare=True,
        ignoreErrors=None,
        as_bytes=False,
        **kwargs,
    ):
        if not url:
            return None
        # Pack kwargs we intentionally ignore: flare (Midnight request handles 503/cfscrape).
        del flare
        if ignoreErrors:
            error = True
        try:
            return crew_client.request(
                url,
                close=close,
                redirect=redirect,
                error=error,
                verify=verifySsl,
                proxy=proxy,
                post=post,
                headers=headers,
                mobile=mobile,
                XHR=XHR,
                limit=limit,
                referer=referer,
                cookie=cookie,
                compression=compression,
                output=output,
                timeout=timeout,
                as_bytes=as_bytes,
                **kwargs,
            )
        except Exception as exc:
            if error or ignoreErrors:
                return exc if error else None
            if getattr(c, 'devmode', False):
                c.log(f'[PackClient] request failed url=({url}): {exc}', 1)
            return None

    mod.request = request
    mod.parseDOM = crew_client.parseDom
    mod.parseDom = crew_client.parseDom
    mod.replaceHTMLCodes = crew_client.replaceHTMLCodes
    mod.agent = crew_client.agent
    mod.randomagent = crew_client.randomagent

    for name in ('parseHTML', 'selectHTML', 'randommobileagent'):
        if hasattr(crew_client, name):
            setattr(mod, name, getattr(crew_client, name))

    return mod


def install_pack_client_shims(force=False):
    """
    Install Midnight client as the HTTP layer for external scraper packs.

    Must run before pack provider modules are imported (sources() does this).
    Importing Midnight client also applies the global Xbox SSL monkeypatch.
    """
    global _adapter, _installed

    if _installed and not force:
        return _adapter

    from . import client as crew_client

    _adapter = _build_adapter(crew_client)
    for target in _PACK_CLIENT_MODULES:
        sys.modules[target] = _adapter

    _installed = True
    try:
        c.log('[Sources] External pack client shims installed (Midnight client)')
    except Exception:
        pass
    return _adapter

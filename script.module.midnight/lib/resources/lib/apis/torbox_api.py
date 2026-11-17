# -*- coding: utf-8 -*-

'''
 ***********************************************************
 * Midnight Add-on
 *
 *
 * @file torbox_api.py
 * @package script.module.midnight.apis
 *
 * @copyright (c) 2023-2026, Midnight
 * @license GNU General Public License, version 3 (GPL-3.0)
 *
 * TorBox API Documentation: https://api.torbox.app/v1/api/
 *
 ********************************************************cm*
'''

import requests

from ..modules import control
from ..modules.crewruntime import c


class TorBoxAPI:
    """
    TorBox API wrapper for Midnight addon

    API Base: https://api.torbox.app/v1/api/
    Authentication: Authorization: Bearer <token>
    Credentials: Read from ResolveURL addon (TorBoxResolver_apikey)
    """

    def __init__(self):
        self.name = 'TorBox'
        self.base_url = 'https://api.torbox.app/v1/api/'
        resolver_addon = control.addon('script.module.resolveurl')
        self.token = resolver_addon.getSetting('TorBoxResolver_apikey') if resolver_addon else ''
        c.log(f'[TorBox API] Token from ResolveURL: {"SET" if self.token else "EMPTY"}', 1)

    def _headers(self):
        return {'Authorization': f'Bearer {self.token}'}

    def _get(self, endpoint, **params):
        try:
            url = self.base_url + endpoint
            response = requests.get(url, params=params, headers=self._headers(), timeout=30).json()
            if response.get('success'):
                return response.get('data')
            c.log(f'[TorBox API] GET {endpoint} failed: {response.get("detail", response)}', 1)
            return None
        except Exception as e:
            c.log(f'[TorBox API] GET error on {endpoint}: {e}', 1)
            return None

    def _post(self, endpoint, data=None):
        try:
            url = self.base_url + endpoint
            response = requests.post(url, data=data or {}, headers=self._headers(), timeout=30).json()
            if response.get('success'):
                return response.get('data')
            c.log(f'[TorBox API] POST {endpoint} failed: {response.get("detail", response)}', 1)
            return None
        except Exception as e:
            c.log(f'[TorBox API] POST error on {endpoint}: {e}', 1)
            return None

    # ===== ACCOUNT =====

    def get_user(self):
        """GET /user/me — account info"""
        return self._get('user/me')

    # ===== CLOUD BROWSING =====

    def user_cloud(self):
        """
        GET /torrents/mylist — list all torrents in cloud.
        Returns list of torrent dicts or [].
        """
        data = self._get('torrents/mylist', bypass_cache='true')
        if isinstance(data, list):
            return data
        return []

    def torrent_info(self, torrent_id):
        """
        GET /torrents/mylist?id=<torrent_id> — single torrent with files.
        Returns torrent dict or None.
        """
        data = self._get('torrents/mylist', id=torrent_id, bypass_cache='true')
        # API returns a list even for single-item query
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict):
            return data
        return None

    def request_dl(self, torrent_id, file_id):
        """
        GET /torrents/requestdl — get direct download URL for a file.
        Returns URL string or None.
        """
        return self._get('torrents/requestdl',
                         token=self.token,
                         torrent_id=torrent_id,
                         file_id=file_id,
                         zip_link='false')

    def delete_torrent(self, torrent_id):
        """POST /torrents/controltorrent with operation=delete"""
        result = self._post('torrents/controltorrent', {
            'torrent_id': torrent_id,
            'operation': 'delete',
        })
        return result is not None

    def add_magnet(self, magnet):
        """POST /torrents/createtorrent — add magnet to cloud"""
        return self._post('torrents/createtorrent', {'magnet': magnet})

    # ===== CACHE CHECKING =====

    def check_cache(self, hashes):
        """
        GET /torrents/checkcached?hash=h1,h2,...&format=object&list_files=false
        Returns dict {hash: entry} for cached hashes only, merged across batches.
        """
        if not hashes:
            return {}
        try:
            merged = {}
            batch_size = 10
            normalized = [h.lower() for h in hashes if h]
            for i in range(0, len(normalized), batch_size):
                chunk = normalized[i:i + batch_size]
                url = self.base_url + 'torrents/checkcached'
                response = requests.get(
                    url,
                    params={
                        'hash': ','.join(chunk),
                        'format': 'object',
                        'list_files': 'false',
                    },
                    headers=self._headers(),
                    timeout=30,
                ).json()
                if not response.get('success'):
                    c.log(f'[TorBox API] check_cache failed: {response.get("detail", response)}', 1)
                    return None
                merged.update(response.get('data', {}) or {})
            return merged
        except Exception as e:
            c.log(f'[TorBox API] check_cache error: {e}', 1)
            return None

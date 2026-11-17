# -*- coding: utf-8 -*-

'''
********************************************************cm*
* Midnight Add-on
*
* @file knaben.py
* @package script.module.midnight
*
* Knaben meta-search API (api.knaben.org/v1). Aggregates many torrent
* indexers behind a single JSON endpoint — strong complement to torrentio.
*
* @copyright (c) 2026, Midnight
* @license GNU General Public License, version 3 (GPL-3.0)
*
********************************************************cm*
'''

import json

import requests

from ..modules import cleantitle, source_utils
from .torrent_base import TorrentBaseScraper
from ..modules.crewruntime import c

# Knaben category IDs (https://api.knaben.org/v1)
KNABEN_CAT_TV = 2_000_000
KNABEN_CAT_MOVIES = 3_000_000


class source(TorrentBaseScraper):
    """Knaben API meta-search scraper."""

    def __init__(self):
        super().__init__(
            name='knaben',
            base_link='https://api.knaben.org',
            domains=['api.knaben.org'],
            min_seeders=0,
        )
        self.api_url = f'{self.base_link}/v1'
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

    def _knaben_search(self, query, categories):
        """POST search query; return list of hit dicts or []."""
        if not query:
            return []

        body = {
            'order_by': 'seeders',
            'query': query,
            'categories': categories,
        }
        c.log(f'[knaben] POST {self.api_url} query="{query}" categories={categories}')

        try:
            resp = requests.post(
                self.api_url,
                headers=self.headers,
                json=body,
                timeout=15,
            )
            if resp.status_code >= 400:
                c.log(f'[knaben] HTTP {resp.status_code}: {(resp.text or "")[:200]}')
                return []

            payload = resp.json()
            hits = payload.get('hits') if isinstance(payload, dict) else None
            if not isinstance(hits, list):
                c.log(f'[knaben] Unexpected response shape: {type(payload)}')
                return []

            c.log(f'[knaben] Received {len(hits)} hit(s)')
            return hits

        except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
            c.log(f'[knaben] Request/parse error: {exc}')
            return []

    def _process_hits(self, hits, data, pack_meta=None):
        """Convert Knaben hits into Midnight torrent source dicts."""
        sources = []
        if not hits:
            return sources

        is_movie = not data.get('tvshowtitle')
        season = data.get('season') if not is_movie else None
        episode = data.get('episode') if not is_movie else None

        rejected_title = 0

        for entry in hits:
            if not isinstance(entry, dict):
                continue

            try:
                name = entry.get('title') or ''
                if not name:
                    continue

                if not self._matches_search_result(data, name):
                    rejected_title += 1
                    continue

                seeders = int(entry.get('seeders') or 0)
                # Like torrentio: keep low/zero seeder rows; debrid/cache filters apply later.

                info_hash = (entry.get('hash') or '').lower()
                if len(info_hash) != 40:
                    continue

                magnet = entry.get('magnetUrl') or self._build_magnet(info_hash, name)
                if not magnet:
                    continue

                size_gb, size_label = self._parse_size_bytes(entry.get('bytes') or 0)

                source_dict = self._build_torrent_source(
                    name=name,
                    url=magnet,
                    seeders=seeders,
                    size=size_gb,
                    info_hash=info_hash,
                    season=season,
                    episode=episode,
                )
                if not source_dict:
                    continue

                if size_label and size_label not in (source_dict.get('info') or ''):
                    source_dict['info'] = f'{size_label} | {source_dict["info"]}'.strip(' |')

                tracker = entry.get('tracker') or entry.get('cachedOrigin')
                if tracker:
                    source_dict['info'] = f'{source_dict["info"]} | {tracker}'.strip(' |')

                if pack_meta:
                    source_dict.update(pack_meta)

                sources.append(source_dict)

            except Exception as exc:
                c.log(f'[knaben] Error processing hit: {exc}')
                continue

        c.log(
            f'[knaben] Results: {len(sources)} added | {rejected_title} title rejected'
        )
        return sources

    def _scrape_torrents(self, data, hostDict):
        query = self._build_query(data)
        categories = [KNABEN_CAT_TV] if data.get('tvshowtitle') else [KNABEN_CAT_MOVIES]
        hits = self._knaben_search(query, categories)
        return self._process_hits(hits, data)

    def sources_packs(self, data, hostDict, search_series=False, total_seasons=0, bypass_filter=0):
        """Search Knaben for season / complete-series pack releases."""
        sources = []

        try:
            tvshowtitle = data.get('tvshowtitle')
            year = data.get('year')
            season = int(data.get('season', 0))
            imdb = data.get('imdb')
            aliases = data.get('aliases', [])

            if not tvshowtitle:
                return sources

            title_query = cleantitle.get_query(tvshowtitle)
            queries = []

            if search_series:
                queries.extend([
                    f'{title_query} Complete Series',
                    f'{title_query} All Seasons',
                    f'{title_query} Season 1-{total_seasons}' if total_seasons > 0 else None,
                ])
            else:
                queries.extend([
                    f'{title_query} S{season:02d}',
                    f'{title_query} Season {season}',
                    f'{title_query} Season.{season}',
                ])

            queries = [q for q in queries if q]
            seen_hashes = set()

            for query in queries:
                hits = self._knaben_search(query, [KNABEN_CAT_TV])
                for entry in hits:
                    if not isinstance(entry, dict):
                        continue

                    name = entry.get('title') or ''
                    if not name:
                        continue

                    info_hash = (entry.get('hash') or '').lower()
                    if len(info_hash) != 40 or info_hash in seen_hashes:
                        continue

                    if search_series:
                        valid, last_season = source_utils.filter_show_pack(
                            tvshowtitle, aliases, imdb, year, season,
                            source_utils.release_title_format(name), total_seasons,
                        )
                        if not valid:
                            continue
                        pack_meta = {'package': 'show', 'last_season': last_season}
                    else:
                        valid, episode_start, episode_end = source_utils.filter_season_pack(
                            tvshowtitle, aliases, year, season,
                            source_utils.release_title_format(name),
                        )
                        if not valid:
                            continue
                        pack_meta = {
                            'package': 'season',
                            'episode_start': episode_start,
                            'episode_end': episode_end,
                        }

                    seeders = int(entry.get('seeders') or 0)

                    magnet = entry.get('magnetUrl') or self._build_magnet(info_hash, name)
                    size_gb, size_label = self._parse_size_bytes(entry.get('bytes') or 0)

                    source_dict = self._build_torrent_source(
                        name=name,
                        url=magnet,
                        seeders=seeders,
                        size=size_gb,
                        info_hash=info_hash,
                    )
                    if not source_dict:
                        continue

                    if size_label:
                        source_dict['info'] = f'{size_label} | {source_dict["info"]}'.strip(' |')

                    source_dict.update(pack_meta)
                    sources.append(source_dict)
                    seen_hashes.add(info_hash)

            return sources

        except Exception as exc:
            c.log(f'[knaben] Pack search error: {exc}')
            return sources

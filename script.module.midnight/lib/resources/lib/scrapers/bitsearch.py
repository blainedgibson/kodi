# -*- coding: utf-8 -*-

'''
********************************************************cm*
* Midnight Add-on
*
* @file bitsearch.py
* @package script.module.midnight
*
* Bitsearch DHT torrent search (bitsearch.to). Complements Knaben/torrentio
* with de-centralized swarm results.
*
* @copyright (c) 2026, Midnight
* @license GNU General Public License, version 3 (GPL-3.0)
*
********************************************************cm*
'''

import html
import re
from urllib.parse import quote_plus, urljoin

from ..modules import client
from .torrent_base import TorrentBaseScraper
from ..modules.crewruntime import c

# Bitsearch returns 20 results per page; two pages is a good speed/coverage tradeoff.
BITSEARCH_MAX_PAGES = 2
BITSEARCH_CARD_MARKER = 'p-6 hover:shadow-md'


class source(TorrentBaseScraper):
    """Bitsearch DHT HTML search scraper."""

    def __init__(self):
        super().__init__(
            name='bitsearch',
            base_link='https://bitsearch.to',
            domains=['bitsearch.to'],
            min_seeders=0,
        )
        self.search_link = '/search?q=%s&sort=seeders&page=%s'

    def _search_url(self, query, page=1):
        path = self.search_link % (quote_plus(query), int(page))
        return urljoin(f'{self.base_link}/', path.lstrip('/'))

    def _parse_search_page(self, page_html):
        """Parse Bitsearch result cards from HTML."""
        results = []
        if not page_html:
            return results

        text = html.unescape(page_html)
        for card in text.split(BITSEARCH_CARD_MARKER)[1:]:
            try:
                magnet_match = re.search(r'href="(magnet:\?[^"]+)"', card, re.I)
                if not magnet_match:
                    continue
                magnet = magnet_match.group(1).replace('&amp;', '&')

                title_match = re.search(
                    r'<h3[^>]*>\s*<a[^>]*>\s*(.*?)\s*</a>',
                    card,
                    re.I | re.S,
                )
                name = re.sub(r'\s+', ' ', title_match.group(1)).strip() if title_match else ''
                if not name:
                    continue

                seeders = 0
                seed_match = re.search(
                    r'font-medium">(\d+)</span>\s*<span>seeders</span>',
                    card,
                    re.I,
                )
                if seed_match:
                    seeders = int(seed_match.group(1))

                size_gb = 0.0
                size_label = ''
                size_match = re.search(
                    r'(\d+(?:\.\d+)?)\s*(GB|MB|GiB|MiB)',
                    card,
                    re.I,
                )
                if size_match:
                    size_gb, size_label = self._parse_size(size_match.group(0))

                hash_match = re.search(r'btih:([A-F0-9]{40})', magnet, re.I)
                if not hash_match:
                    continue
                info_hash = hash_match.group(1).lower()

                results.append({
                    'name': name,
                    'url': magnet,
                    'seeders': seeders,
                    'size_gb': size_gb,
                    'size_label': size_label,
                    'hash': info_hash,
                })
            except Exception as exc:
                c.log(f'[bitsearch] Card parse error: {exc}')
                continue

        return results

    def _fetch_results(self, query):
        """Fetch and dedupe results across the first N result pages."""
        merged = []
        seen_hashes = set()

        headers = {'User-Agent': client.agent()}
        for page in range(1, BITSEARCH_MAX_PAGES + 1):
            url = self._search_url(query, page)
            c.log(f'[bitsearch] GET {url}')
            try:
                page_html = client.request(url, headers=headers, timeout=10)
            except Exception as exc:
                c.log(f'[bitsearch] Request error page {page}: {exc}')
                break

            if not page_html:
                break

            page_rows = self._parse_search_page(page_html)
            c.log(f'[bitsearch] Page {page}: {len(page_rows)} row(s)')
            if not page_rows:
                break

            for row in page_rows:
                info_hash = row.get('hash')
                if not info_hash or info_hash in seen_hashes:
                    continue
                seen_hashes.add(info_hash)
                merged.append(row)

        return merged

    def _scrape_torrents(self, data, hostDict):
        sources = []
        query = self._build_query(data)
        if not query:
            return sources

        is_movie = not data.get('tvshowtitle')
        season = data.get('season') if not is_movie else None
        episode = data.get('episode') if not is_movie else None

        rows = self._fetch_results(query)
        rejected_title = 0

        for row in rows:
            name = row.get('name') or ''
            if not self._matches_search_result(data, name):
                rejected_title += 1
                continue

            source_dict = self._build_torrent_source(
                name=name,
                url=row.get('url'),
                seeders=row.get('seeders') or 0,
                size=row.get('size_gb') or 0,
                info_hash=row.get('hash'),
                season=season,
                episode=episode,
            )
            if not source_dict:
                continue

            size_label = row.get('size_label') or ''
            if size_label and size_label not in (source_dict.get('info') or ''):
                source_dict['info'] = f'{size_label} | {source_dict["info"]}'.strip(' |')

            sources.append(source_dict)

        c.log(f'[bitsearch] Results: {len(sources)} added | {rejected_title} title rejected')
        return sources

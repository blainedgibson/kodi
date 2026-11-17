# -*- coding: utf-8 -*-

'''
********************************************************cm*
* Midnight Add-on
*
* @file freeomovie.py
* @package script.module.midnight
*
* @copyright (c) 2026, Midnight
* @license GNU General Public License, version 3 (GPL-3.0)
*
********************************************************cm*
'''

import re
import json
from .base import CrewAdult
from ..modules import client, workers, control
from ..modules.crewruntime import c
from ..modules.http_client import HTTPClient
from resources.lib.ui_theme import color



class FreeOMovie(CrewAdult):
    def __init__(self):
        super().__init__(
            name='freeomovie',
            title=f'Porn Movies - {color("FOM", 'header')}'
        )
        self.base_url = 'https://www.freeomovie.to/'

    def get_categories(self):
        """Get movie categories"""
        c.log(f'[FreeOMovie] Fetching categories')

        try:
            session = HTTPClient.get_session('adult')
            response = session.get(self.base_url, timeout=15)
            response.raise_for_status()
            html = response.text
            if not html:
                return []
        except Exception as e:
            c.log(f'[FreeOMovie] Category fetch failed: {e}')
            return []

        # Extract categories (FreeOMovie categories don't have thumbnails)
        pattern = r'<li\s+class="cat-item[^"]*">\s*<a\s+href="([^"]+)"[^>]*>([^<]+)</a>'
        matches = re.findall(pattern, html)

        results = []
        # Add "Latest" as first category
        results.append((self.base_url, f'{color("Latest Movies", 'header')}', self.icon))

        for cat_url, cat_name in matches:
            if cat_url and cat_name:
                cat_name = self._cleanup_title(cat_name)
                results.append((cat_url, cat_name, self.icon))

        c.log(f'[FreeOMovie] Found {len(results)} categories')
        return results

    def get_videos(self, url, page=1):
        """Get videos from a category"""
        c.log(f'[FreeOMovie] Fetching videos from: {url}, page: {page}')

        # Build single page URL
        page_num = int(page)
        if page_num == 1:
            page_url = url
        else:
            # Ensure proper URL structure for pagination
            if url.endswith('/'):
                page_url = f'{url}page/{page_num}/'
            else:
                page_url = f'{url}/page/{page_num}/'

        c.log(f'[FreeOMovie] Fetching page URL: {page_url}')

        # Fetch single page
        try:
            session = HTTPClient.get_session('adult')
            response = session.get(page_url, timeout=15)
            response.raise_for_status()
            html = response.text
            if not html:
                return []
        except Exception as e:
            c.log(f'[FreeOMovie] Page fetch failed: {e}')
            return []

        # Extract movie data - Debug logging
        c.log(f'[FreeOMovie] HTML length: {len(html)} chars')
        thumi_count = html.count('class="thumi"')
        c.log(f'[FreeOMovie] Found {thumi_count} "thumi" elements in HTML')

        # Pattern with re.DOTALL to match across newlines
        pattern = r'<li[^>]*class="thumi"[^>]*>\s*<a\s+href="([^"]+)"[^>]*title="([^"]+)"[^>]*>.*?<img[^>]*src="([^"]+)"'
        matches = re.findall(pattern, html, re.DOTALL)

        results = []
        for video_url, title, thumb_url in matches:
            if title and video_url:
                title = self._cleanup_title(title)
                thumbnail = thumb_url if thumb_url else self.icon
                results.append((video_url, title, thumbnail))

        c.log(f'[FreeOMovie] Found {len(results)} videos')

        # Add next page if we have results (assume more pages exist)
        if results:
            results.append(('NEXT_PAGE', f'{color("Next Page (" + str(page_num + 1) + ")", 'header')}', ''))

        return results

    # Prefer these before flaky Doodstream / myvidplay clones.
    _HOST_FIRST = (
        'voe', 'mixdrop', 'mxdrop', 'streamtape', 'filemoon', 'streamwish',
        'vidhide', 'lulustream', 'vinovo', 'vidara',
    )
    _HOST_LAST = (
        'dood', 'myvidplay', 'ds2play', 'playmogo', 'dooood', 'doodstream',
    )

    def resolve(self, url):
        """Resolve movie page to playable URL"""
        c.log(f'[FreeOMovie] Resolving: {url}')

        try:
            session = HTTPClient.get_session('adult')
            response = session.get(url, timeout=15)
            response.raise_for_status()
            html = response.text
            if not html:
                return None
        except Exception as e:
            c.log(f'[FreeOMovie] Resolution failed: {e}')
            return None

        # Extract embed URLs — site uses a JS TABS array: var TABS = [{"label":"...","url":"..."}, ...]
        tabs_match = re.search(r'var\s+TABS\s*=\s*(\[.*?\]);', html, re.DOTALL)
        if tabs_match:
            try:
                tabs = json.loads(tabs_match.group(1))
                matches = [(t.get('label', 'Unknown'), t['url']) for t in tabs if t.get('url')]
            except Exception as e:
                c.log(f'[FreeOMovie] Failed to parse TABS JSON: {e}')
                matches = []
        else:
            # Fallback: legacy pattern (target="myIframe")
            legacy = re.findall(r'href="([^"]+)"\s*target="myIframe"', html)
            matches = [(self._detect_service(u), u) for u in legacy]

        if not matches:
            c.log(f'[FreeOMovie] No embed URLs found in page')
            return None

        sources = [(label or self._detect_service(embed), embed) for label, embed in matches]
        sources = self._rank_sources(sources)
        c.log(f'[FreeOMovie] {len(sources)} host(s): {[s[0] for s in sources]}')

        ordered = self._choose_source_order(sources)
        if not ordered:
            return None

        return self._resolve_with_fallback(ordered)

    def _host_rank(self, label, url):
        blob = f'{label} {url}'.lower()
        for idx, needle in enumerate(self._HOST_LAST):
            if needle in blob:
                return 1000 + idx
        for idx, needle in enumerate(self._HOST_FIRST):
            if needle in blob:
                return idx
        return 500

    def _rank_sources(self, sources):
        return sorted(sources, key=lambda item: self._host_rank(item[0], item[1]))

    def _choose_source_order(self, sources):
        '''Show picker when multiple hosts; Auto tries ranked list.'''
        if len(sources) == 1:
            return sources

        control.idle()
        labels = [f'{color("Auto - try all", 'header')}']
        labels.extend(f'{color(label, 'header')}' for label, _ in sources)
        try:
            select = control.selectDialog(labels, heading='Select Source')
        except Exception as exc:
            c.log(f'[FreeOMovie] selectDialog failed ({exc}) - Auto', 1)
            select = 0

        if select == -1:
            c.log('[FreeOMovie] Source select cancelled')
            return []
        if select == 0:
            c.log('[FreeOMovie] Auto - try all hosts')
            return sources

        pick = sources[select - 1]
        c.log(f'[FreeOMovie] User picked {pick[0]} (fallback to others on fail)')
        return [pick] + [item for item in sources if item[1] != pick[1]]

    def _resolve_with_fallback(self, ordered):
        for idx, (label, embed) in enumerate(ordered):
            c.log(f'[FreeOMovie] Trying {label}: {embed}')
            resolved = self._try_resolveurl(embed)
            if resolved:
                if idx > 0:
                    try:
                        control.infoDialog(f'Playing via {label}', sound=False)
                    except Exception:
                        pass
                return resolved
            c.log(f'[FreeOMovie] {label} failed - next host')
        c.log('[FreeOMovie] All hosts failed')
        return None

    def _detect_service(self, url):
        """Detect hosting service from URL"""
        url_lower = url.lower()

        # Check known video hosts
        if 'lulustream' in url_lower:
            return 'LuluStream'
        elif 'vidhide' in url_lower or 'vidhidepre' in url_lower:
            return 'Vidhide'
        elif 'filemoon' in url_lower:
            return 'Filemoon'
        elif 'streamtape' in url_lower:
            return 'Streamtape'
        elif 'doodstream' in url_lower or 'dood' in url_lower:
            return 'Doodstream'
        elif 'earnvids' in url_lower or 'mixdrop' in url_lower:
            return 'MixDrop'
        elif 'streamwish' in url_lower:
            return 'StreamWish'
        elif 'evoload' in url_lower:
            return 'EvoLoad'
        elif 'upstream' in url_lower:
            return 'UpStream'
        elif 'voe' in url_lower:
            return 'VOE'
        elif 'streamvid' in url_lower:
            return 'StreamVid'
        elif 'vidoza' in url_lower:
            return 'Vidoza'
        elif 'turbovid' in url_lower:
            return 'TurboVid'
        elif 'vidcloud' in url_lower:
            return 'VidCloud'
        elif 'streamsb' in url_lower or 'sbembed' in url_lower:
            return 'StreamSB'
        elif 'fembed' in url_lower:
            return 'Fembed'
        elif 'videovard' in url_lower:
            return 'VideoVard'
        elif 'streamhub' in url_lower:
            return 'StreamHub'
        elif 'gounlimited' in url_lower:
            return 'GoUnlimited'
        else:
            # Fallback: extract domain name from URL
            try:
                domain_match = re.search(r'https?://(?:www\.)?([^/]+)', url)
                if domain_match:
                    domain = domain_match.group(1)
                    # Clean up the domain (remove common subdomains and TLDs for readability)
                    domain = domain.split('.')[0].capitalize()
                    return domain
            except:
                pass
            return 'Unknown'


class PageFetcher:
    """Helper class for concurrent page fetching"""
    def __init__(self):
        self.results = []

    def run(self, urls):
        threads = []
        self.results = []
        indexed_urls = [(i+1, url) for i, url in enumerate(urls)]

        for idx, url in indexed_urls:
            thread = workers.Thread(self._fetch_page, (idx, url))
            threads.append(thread)

        [t.start() for t in threads]
        [t.join() for t in threads]

        # Sort by index and join results
        self.results.sort(key=lambda x: x[0])
        return ''.join([r[1] for r in self.results])

    def _fetch_page(self, data):
        idx, url = data
        try:
            html = client.request(url)
            if html:
                self.results.append((idx, html))
        except:
            pass


# Register the site
site = FreeOMovie()

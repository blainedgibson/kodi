#!/usr/bin/env python3
"""Build local_overrides/crewstreamer.py — EHD embed_st direct HLS only (dev soak)."""

import os
import shutil

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
OUT_DIR = os.path.join(ROOT, 'lib', 'resources', 'lib', 'local_overrides')
OUT_PATH = os.path.join(OUT_DIR, 'crewstreamer.py')
CACHED = os.path.join(
    os.environ.get('APPDATA', ''),
    'Kodi', 'userdata', 'addon_data', 'plugin.video.midnight', 'crewstreamer.py',
)

HELPER = '''

def _publish_direct_hls(stream_url, headers):
    """Direct HLS pipe with correct Kodi | separators (CDN still uses broken &)."""
    from resources.lib.modules import live_hls_publish
    return live_hls_publish.direct_hls_pipe(stream_url, headers)


def _publish_live_hls_auto(stream_url, headers, name_prefix):
    """Probe manifest: 200 -> segmentproxy, 403/fail -> direct HLS (EU path)."""
    from resources.lib.modules import live_hls_publish
    return live_hls_publish.publish_live_hls(
        stream_url, headers, name_prefix=name_prefix, mode='auto'
    )


def _publish_live_embed_st(embed_url, headers, name_prefix):
    """EHD embed.st: goat decrypt, then auto proxy-or-direct (EU 403 safe)."""
    from resources.lib.modules import live_hls_publish
    return live_hls_publish.publish_embed_st(
        embed_url, headers, name_prefix=name_prefix, mode='auto'
    )

'''

OLD_EMBED = """    def embed_st(self, url):
        try:
            origin = 'https://embed.st'
            url_no_frag = url.split('#')[0].rstrip('/')
            segments = url_no_frag.split('/')
            sc, stream_id, no = segments[-3], segments[-2], segments[-1]

            def _varint(n):
                out = bytearray()
                while True:
                    byte = n & 0x7F
                    n >>= 7
                    out.append(byte | 0x80 if n else byte)
                    if not n:
                        return bytes(out)

            def _field(number, value):
                tag = (number << 3) | 2
                return bytes([tag]) + _varint(len(value)) + value

            payload = _field(1, sc.encode()) + _field(2, stream_id.encode()) + _field(3, no.encode())
            headers = {
                'User-Agent': UA,
                'Referer': url,
                'Origin': origin,
                'Content-Type': 'application/octet-stream',
            }
            fetch = requests.post(origin + '/fetch', data=payload, headers=headers, timeout=TIMEOUT)
            if not fetch.content:
                log(f'embed_st: empty response | {url}', xbmc.LOGERROR)
                return None

            stream_url = self._decrypt_goat_response(fetch)
            if not stream_url:
                log(f'embed_st: decrypt failed or missing key header | {url}', xbmc.LOGERROR)
                return None

            return _publish_live_manifest(stream_url, {'User-Agent': UA, 'Referer': url, 'Origin': origin}, 'embed_st')
        except Exception as e:
            _log_error('embed_st', url, e)
            return None"""

NEW_EMBED = """    def embed_st(self, url):
        try:
            origin = 'https://embed.st'
            return _publish_live_embed_st(url, {'User-Agent': UA, 'Referer': url, 'Origin': origin}, 'embed_st')
        except Exception as e:
            _log_error('embed_st', url, e)
            return None"""


def main():
    src = CACHED if os.path.isfile(CACHED) else None
    if not src:
        raise SystemExit(f'missing cached crewstreamer: {CACHED}')
    with open(src, 'r', encoding='utf-8') as f:
        cs = f.read()
    if '_publish_live_embed_st' not in cs:
        anchor = 'class streamer:'
        if anchor not in cs:
            raise SystemExit('class streamer anchor missing')
        cs = cs.replace(anchor, HELPER + anchor, 1)
    if OLD_EMBED not in cs:
        if 'return _publish_live_embed_st' in cs:
            print('embed_st already patched')
        else:
            raise SystemExit('embed_st block not found — CDN crewstreamer changed')
    else:
        cs = cs.replace(OLD_EMBED, NEW_EMBED, 1)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8', newline='\n') as f:
        f.write(cs)
    print(f'wrote {OUT_PATH} ({len(cs)} bytes)')


if __name__ == '__main__':
    main()

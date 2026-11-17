# -*- coding: utf-8 -*-

import os
import json
import time
import threading
import http.server
from urllib.parse import urlparse, parse_qs, unquote, quote_plus, urljoin

import requests
import xbmc
import xbmcvfs
import xbmcgui

TAG = 'SegmentProxy'
TIMEOUT = 10

TS_PACKET_SIZE = 188
TS_SYNC_BYTE = 0x47

PORT_PROPERTY = 'midnight.segmentproxy.port'
DIR_PROPERTY = 'midnight.segmentproxy.manifestdir'
MANIFEST_REFRESH_INTERVAL = 2.0

_manifest_dir = None
_server_started = False
_start_lock = threading.Lock()
_manifest_cache = {}
_manifest_cache_lock = threading.Lock()
_manifest_sessions = {}


def log(msg, level=xbmc.LOGINFO):
    xbmc.log(f'{TAG} {msg}', level)


def _find_ts_start(data):
    scan_limit = min(4096, len(data) - (5 * TS_PACKET_SIZE))
    for offset in range(scan_limit):
        if data[offset] != TS_SYNC_BYTE:
            continue
        aligned = True
        for k in range(1, 5):
            idx = offset + k * TS_PACKET_SIZE
            if idx >= len(data) or data[idx] != TS_SYNC_BYTE:
                aligned = False
                break
        if aligned:
            return offset
    return 0


def _unwrap_segment(data):
    offset = _find_ts_start(data)
    return data[offset:]


def _rewrite_manifest(manifest_text, base_url, port):
    lines = []
    for line in manifest_text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith('#'):
            abs_url = urljoin(base_url, stripped)
            lines.append(f'http://127.0.0.1:{port}/seg?url=' + quote_plus(abs_url))
        else:
            lines.append(line)
    return '\n'.join(lines)


def _recipe_path(manifest_id):
    return os.path.join(_manifest_dir, f'{manifest_id}.json')


def _static_path(manifest_id):
    return os.path.join(_manifest_dir, f'{manifest_id}.m3u8')


def _get_manifest_session(manifest_id):
    with _manifest_cache_lock:
        s = _manifest_sessions.get(manifest_id)
        if s is None:
            s = requests.Session()
            _manifest_sessions[manifest_id] = s
        return s


def _fetch_and_rewrite(manifest_id, recipe_path):
    """Fetch the real upstream playlist, validate, rewrite segment lines through
    the proxy, and update the cache. Returns the rewritten text, or None on
    failure — on failure the existing cache entry (if any) is left untouched,
    so a transient origin hiccup doesn't take down an otherwise-fine stream."""
    try:
        with open(recipe_path, 'r', encoding='utf-8') as f:
            recipe = json.load(f)
        session = _get_manifest_session(manifest_id)
        resp = session.get(recipe['url'], headers=recipe.get('headers') or {}, timeout=TIMEOUT)
        text = resp.text
        if not text.lstrip().startswith('#EXTM3U'):
            log(f'live manifest refetch invalid | {recipe["url"]} | body: {text[:200]!r}', xbmc.LOGERROR)
            return None
        rewritten = _rewrite_manifest(text, resp.url, get_port())
        with _manifest_cache_lock:
            _manifest_cache[manifest_id] = {'data': rewritten, 'time': time.time(), 'refreshing': False}
        return rewritten
    except Exception as e:
        log(f'live manifest refetch error: {e}', xbmc.LOGERROR)
        return None


def _background_refresh(manifest_id, recipe_path):
    try:
        _fetch_and_rewrite(manifest_id, recipe_path)
    finally:
        with _manifest_cache_lock:
            entry = _manifest_cache.get(manifest_id)
            if entry is not None:
                entry['refreshing'] = False


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path.startswith('/manifest/'):
            manifest_id = parsed.path[len('/manifest/'):]
            if manifest_id.endswith('.m3u8'):
                manifest_id = manifest_id[:-5]

            recipe_path = _recipe_path(manifest_id)
            if os.path.exists(recipe_path):
                # Live source: a live HLS media playlist keeps sliding forward
                # with new segments, so the underlying content needs periodic
                # re-fetching. But blocking each client request on a fresh
                # network round-trip causes a visible stall on every reload —
                # instead, always serve whatever's cached immediately (even if
                # a little stale) and refresh it in the background, so the
                # client is never held up waiting on the real origin except
                # for the very first request.
                with _manifest_cache_lock:
                    cached = _manifest_cache.get(manifest_id)

                if cached is None:
                    rewritten = _fetch_and_rewrite(manifest_id, recipe_path)
                    if rewritten is None:
                        self.send_response(502)
                        self.end_headers()
                        return
                else:
                    rewritten = cached['data']
                    age = time.time() - cached['time']
                    if age >= MANIFEST_REFRESH_INTERVAL and not cached.get('refreshing'):
                        cached['refreshing'] = True
                        threading.Thread(
                            target=_background_refresh,
                            args=(manifest_id, recipe_path),
                            name='ManifestRefresh',
                            daemon=True,
                        ).start()
            else:
                # Static source: content has no separate upstream URL to re-fetch
                # (e.g. embedded page state) — serve the one-time snapshot as-is.
                try:
                    with open(_static_path(manifest_id), 'r', encoding='utf-8') as f:
                        rewritten = f.read()
                except Exception:
                    self.send_response(404)
                    self.end_headers()
                    return

            payload = rewritten.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        qs = parse_qs(parsed.query)
        target = qs.get('url', [None])[0]
        if not target:
            self.send_response(400)
            self.end_headers()
            return
        target = unquote(target)
        try:
            resp = requests.get(target, timeout=TIMEOUT)
            payload = _unwrap_segment(resp.content)
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp2t')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as e:
            log(f'segment fetch error {target}: {e}', xbmc.LOGERROR)
            try:
                self.send_response(502)
                self.end_headers()
            except Exception:
                pass


def start():
    """Start the persistent segment-unwrap proxy once for the whole Kodi session.
    Call from crewservice.start(). Safe to call more than once (no-op after first)."""
    global _manifest_dir, _server_started
    with _start_lock:
        if _server_started:
            return
        _manifest_dir = xbmcvfs.translatePath('special://temp/midnight_manifests/')
        try:
            os.makedirs(_manifest_dir, exist_ok=True)
        except Exception:
            pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, name='CrewSegmentProxy', daemon=True)
        thread.start()
        win = xbmcgui.Window(10000)
        win.setProperty(PORT_PROPERTY, str(port))
        win.setProperty(DIR_PROPERTY, _manifest_dir)
        _server_started = True
        log(f'started on 127.0.0.1:{port}, manifest dir={_manifest_dir}')


def get_port():
    """Read the running proxy's port from the shared window property.
    Returns None if the service hasn't started it yet."""
    val = xbmcgui.Window(10000).getProperty(PORT_PROPERTY)
    return int(val) if val else None


def get_manifest_dir():
    val = xbmcgui.Window(10000).getProperty(DIR_PROPERTY)
    return val or None


def register_live_manifest(upstream_url, headers, name_prefix):
    """Register an upstream media-playlist URL for LIVE re-fetching on every
    /manifest/<id>.m3u8 request, instead of caching one static snapshot.
    Use this for real live streams where the upstream playlist keeps sliding
    forward with new segments. Returns the local proxy URL, or None if the
    service hasn't started yet."""
    port = get_port()
    manifest_dir = get_manifest_dir()
    if not port or not manifest_dir:
        return None
    manifest_id = f'{name_prefix}_{int(time.time() * 1000)}'
    recipe = {'url': upstream_url, 'headers': headers or {}}
    with open(os.path.join(manifest_dir, f'{manifest_id}.json'), 'w', encoding='utf-8') as f:
        json.dump(recipe, f)
    return f'http://127.0.0.1:{port}/manifest/{manifest_id}.m3u8'


def register_static_manifest(manifest_text, name_prefix):
    """Register a one-time manifest snapshot with no separate upstream URL to
    re-fetch (e.g. content parsed out of embedded page state). Returns the
    local proxy URL, or None if the service hasn't started yet."""
    port = get_port()
    manifest_dir = get_manifest_dir()
    if not port or not manifest_dir:
        return None
    manifest_id = f'{name_prefix}_{int(time.time() * 1000)}'
    with open(os.path.join(manifest_dir, f'{manifest_id}.m3u8'), 'w', encoding='utf-8') as f:
        f.write(manifest_text)
    return f'http://127.0.0.1:{port}/manifest/{manifest_id}.m3u8'

# -*- coding: utf-8 -*-
import socket
import ssl
import zlib
import email.message
import http.cookiejar
import urllib.request
from urllib.parse import urlsplit, urljoin

import h2.config
import h2.connection
import h2.events

DEFAULT_TIMEOUT = 15
MAX_REDIRECTS = 8
_SKIP = ('host', 'connection', 'keep-alive', 'proxy-connection', 'transfer-encoding', 'upgrade', 'content-length')


class H2Error(Exception):
    pass


class Response(object):
    def __init__(self, url, status_code, headers, content, hops):
        self.url = url
        self.status_code = status_code
        self.headers = headers
        self.content = content
        self.hops = hops
        self.history = [None] * hops

    @property
    def text(self):
        return self.content.decode('utf-8', errors='replace')

    @property
    def ok(self):
        return 200 <= self.status_code < 400


class _CookieResponse(object):
    def __init__(self, header_list):
        self._msg = email.message.Message()
        for k, v in header_list:
            self._msg[k] = v

    def info(self):
        return self._msg


def _context(verify=True):
    cafile = None
    try:
        import certifi
        cafile = certifi.where()
    except Exception:
        cafile = None
    ctx = ssl.create_default_context(cafile=cafile)
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(['h2'])
    return ctx


def _decode(body, encoding):
    encoding = (encoding or '').lower().strip()
    try:
        if encoding == 'gzip':
            return zlib.decompress(body, 16 + zlib.MAX_WBITS)
        if encoding == 'deflate':
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except Exception:
        return body
    return body


def _fetch(url, headers, timeout, verify):
    parts = urlsplit(url)
    if parts.scheme != 'https':
        raise H2Error('https only: %s' % url)
    host = parts.hostname
    port = parts.port or 443
    path = (parts.path or '/') + ('?' + parts.query if parts.query else '')
    raw = socket.create_connection((host, port), timeout=timeout)
    sock = None
    try:
        sock = _context(verify).wrap_socket(raw, server_hostname=host)
        if sock.selected_alpn_protocol() != 'h2':
            raise H2Error('server did not negotiate h2: %s' % host)
        conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=True, header_encoding='utf-8'))
        conn.initiate_connection()
        stream_id = conn.get_next_available_stream_id()
        request_headers = [
            (':method', 'GET'),
            (':authority', host if port == 443 else '%s:%d' % (host, port)),
            (':scheme', 'https'),
            (':path', path),
        ]
        for k, v in headers.items():
            if k.lower() not in _SKIP:
                request_headers.append((k.lower(), str(v)))
        conn.send_headers(stream_id, request_headers, end_stream=True)
        sock.sendall(conn.data_to_send())
        response_headers = []
        body = bytearray()
        done = False
        while not done:
            data = sock.recv(65535)
            if not data:
                break
            for event in conn.receive_data(data):
                if isinstance(event, h2.events.ResponseReceived):
                    response_headers = event.headers
                elif isinstance(event, h2.events.DataReceived):
                    body += event.data
                    conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                elif isinstance(event, h2.events.StreamEnded):
                    done = True
                elif isinstance(event, h2.events.StreamReset):
                    raise H2Error('stream reset: %s' % event.error_code)
                elif isinstance(event, h2.events.ConnectionTerminated):
                    if not done and not response_headers:
                        raise H2Error('connection terminated: %s' % event.error_code)
                    done = True
            out = conn.data_to_send()
            if out:
                sock.sendall(out)
        if not response_headers:
            raise H2Error('no response headers: %s' % url)
        try:
            conn.close_connection()
            sock.sendall(conn.data_to_send())
        except Exception:
            pass
        return response_headers, bytes(body)
    finally:
        try:
            (sock or raw).close()
        except Exception:
            pass


class Session(object):
    def __init__(self, verify=True):
        self.cookies = http.cookiejar.CookieJar()
        self.verify = verify

    def get(self, url, headers=None, timeout=DEFAULT_TIMEOUT, allow_redirects=True):
        base_headers = {'accept': '*/*', 'accept-encoding': 'gzip, deflate'}
        for k, v in (headers or {}).items():
            base_headers[k.lower()] = v
        hops = 0
        while True:
            send = dict(base_headers)
            cookie_request = urllib.request.Request(url)
            self.cookies.add_cookie_header(cookie_request)
            cookie_value = cookie_request.get_header('Cookie')
            if cookie_value:
                send['cookie'] = cookie_value
            header_list, body = _fetch(url, send, timeout, self.verify)
            self.cookies.extract_cookies(_CookieResponse([(k, v) for k, v in header_list if not k.startswith(':')]), cookie_request)
            header_map = {}
            status = 0
            for k, v in header_list:
                if k == ':status':
                    status = int(v)
                elif not k.startswith(':'):
                    header_map[k.lower()] = v
            location = header_map.get('location')
            if allow_redirects and status in (301, 302, 303, 307, 308) and location:
                hops += 1
                if hops > MAX_REDIRECTS:
                    raise H2Error('too many redirects: %s' % url)
                url = urljoin(url, location)
                base_headers.pop('referer', None)
                continue
            return Response(url, status, header_map, _decode(body, header_map.get('content-encoding')), hops)


def get(url, headers=None, timeout=DEFAULT_TIMEOUT, allow_redirects=True, verify=True):
    return Session(verify=verify).get(url, headers=headers, timeout=timeout, allow_redirects=allow_redirects)

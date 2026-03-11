#!/usr/bin/env python3
"""go2web - A command-line HTTP client using raw sockets (no HTTP libraries)."""

import sys
import socket
import ssl
from html.parser import HTMLParser
from urllib.parse import urlparse, quote_plus, parse_qs

MAX_REDIRECTS = 10
REDIRECT_CODES = {301, 302, 303, 307, 308}


# ---------------------------------------------------------------------------
# Raw HTTP over TCP sockets
# ---------------------------------------------------------------------------

def decode_chunked(data: bytes) -> bytes:
    """Decode HTTP chunked transfer-encoding."""
    result = b''
    while data:
        crlf = data.find(b'\r\n')
        if crlf == -1:
            break
        try:
            size = int(data[:crlf].split(b';')[0].strip(), 16)
        except ValueError:
            break
        if size == 0:
            break
        data = data[crlf + 2:]
        result += data[:size]
        data = data[size + 2:]
    return result


def raw_request(url: str):
    """
    Perform a GET request using a raw TCP socket.
    Returns (status_code, headers_dict, body_str).
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname
    path = (parsed.path or '/') + (('?' + parsed.query) if parsed.query else '')
    port = parsed.port or (443 if scheme == 'https' else 80)

    req = (
        f'GET {path} HTTP/1.1\r\n'
        f'Host: {host}\r\n'
        f'User-Agent: Mozilla/5.0 (compatible; go2web/1.0)\r\n'
        f'Accept: text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8\r\n'
        f'Accept-Language: en-US,en;q=0.5\r\n'
        f'Connection: close\r\n'
        f'\r\n'
    )

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(15)
    try:
        if scheme == 'https':
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.connect((host, port))
        sock.sendall(req.encode())
        chunks = []
        while True:
            chunk = sock.recv(8192)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        sock.close()

    response = b''.join(chunks)
    sep = response.find(b'\r\n\r\n')
    if sep == -1:
        return 0, {}, response.decode('utf-8', errors='replace')

    raw_head = response[:sep].decode('utf-8', errors='replace')
    body_bytes = response[sep + 4:]

    lines = raw_head.split('\r\n')
    try:
        status_code = int(lines[0].split()[1])
    except (IndexError, ValueError):
        status_code = 0

    headers = {}
    for line in lines[1:]:
        if ':' in line:
            k, _, v = line.partition(':')
            headers[k.strip().lower()] = v.strip()

    if headers.get('transfer-encoding', '').lower() == 'chunked':
        body_bytes = decode_chunked(body_bytes)

    ct = headers.get('content-type', '')
    charset = 'utf-8'
    if 'charset=' in ct:
        charset = ct.split('charset=')[-1].split(';')[0].strip()
    try:
        body = body_bytes.decode(charset, errors='replace')
    except LookupError:
        body = body_bytes.decode('utf-8', errors='replace')

    return status_code, headers, body


def resolve_url(base: str, location: str) -> str:
    """Resolve a redirect Location header relative to the base URL."""
    if location.startswith(('http://', 'https://')):
        return location
    p = urlparse(base)
    if location.startswith('//'):
        return f'{p.scheme}:{location}'
    if location.startswith('/'):
        return f'{p.scheme}://{p.netloc}{location}'
    base_dir = p.path.rsplit('/', 1)[0]
    return f'{p.scheme}://{p.netloc}{base_dir}/{location}'


def fetch(url: str, _hops: int = 0):
    """GET a URL, following redirects. Returns (status, headers, body)."""
    if _hops >= MAX_REDIRECTS:
        print('Error: too many redirects', file=sys.stderr)
        return None, None, None
    status, headers, body = raw_request(url)
    if status in REDIRECT_CODES:
        loc = headers.get('location', '')
        if loc:
            return fetch(resolve_url(url, loc), _hops + 1)
    return status, headers, body


# ---------------------------------------------------------------------------
# HTML → plain text
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    _SKIP = frozenset({
        'script', 'style', 'noscript', 'head', 'meta',
        'link', 'iframe', 'svg', 'path',
    })
    _BLOCK = frozenset({
        'p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'tr', 'td', 'th', 'section', 'article', 'header', 'footer',
        'nav', 'main', 'blockquote',
    })

    def __init__(self):
        super().__init__()
        self._buf = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in self._SKIP:
            self._skip += 1
        elif t in self._BLOCK and self._skip == 0:
            self._buf.append('\n')

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif t in self._BLOCK and self._skip == 0:
            self._buf.append('\n')

    def handle_data(self, data):
        if self._skip == 0:
            self._buf.append(data)

    def get_text(self) -> str:
        raw = ''.join(self._buf)
        lines, prev_blank = [], False
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                if not prev_blank:
                    lines.append('')
                prev_blank = True
            else:
                lines.append(line)
                prev_blank = False
        return '\n'.join(lines).strip()


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    return p.get_text()


# ---------------------------------------------------------------------------
# DuckDuckGo HTML search-result parser
# ---------------------------------------------------------------------------

class _DDGParser(HTMLParser):
    """Parse DuckDuckGo HTML search results page."""

    def __init__(self):
        super().__init__()
        self.results = []
        self._cur = None
        self._div_depth = 0
        self._body_at = None    # div depth when result__body opened
        self._in_title = False
        self._in_snippet = False

    def _real_url(self, href: str) -> str:
        if not href:
            return ''
        if href.startswith('//'):
            href = 'https:' + href
        qs = parse_qs(urlparse(href).query)
        return qs.get('uddg', [href])[0]

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get('class', '')

        if tag == 'div':
            self._div_depth += 1
            if 'result__body' in cls:
                self._cur = {'title': '', 'url': '', 'snippet': ''}
                self._body_at = self._div_depth

        if self._cur is None:
            return

        if tag == 'a' and 'result__a' in cls:
            self._in_title = True
            self._cur['url'] = self._real_url(a.get('href', ''))
        elif tag in ('a', 'div', 'span') and 'result__snippet' in cls:
            self._in_snippet = True

    def handle_endtag(self, tag):
        if tag == 'a':
            self._in_title = False
            self._in_snippet = False

        if tag == 'div':
            if self._cur is not None and self._div_depth == self._body_at:
                if self._cur['title'] and self._cur['url']:
                    self.results.append(self._cur)
                self._cur = None
                self._body_at = None
                self._in_snippet = False
            self._div_depth -= 1

    def handle_data(self, data):
        if self._cur is None:
            return
        if self._in_title:
            self._cur['title'] += data
        elif self._in_snippet:
            self._cur['snippet'] += data


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_help():
    print(
        'go2web - A simple CLI HTTP client\n'
        '\n'
        'Usage:\n'
        '  go2web -u <URL>          Make an HTTP request to URL and print the response\n'
        '  go2web -s <search-term>  Search the web and print the top 10 results\n'
        '  go2web -h                Show this help message\n'
        '\n'
        'Examples:\n'
        '  go2web -u https://example.com\n'
        '  go2web -s python tutorial\n'
        '  go2web -s "web programming"\n'
    )


def cmd_url(url: str):
    status, headers, body = fetch(url)
    if body is None:
        return
    ct = (headers or {}).get('content-type', '')
    if 'json' in ct:
        print(body)
    else:
        print(html_to_text(body))


def cmd_search(term: str):
    url = f'https://html.duckduckgo.com/html/?q={quote_plus(term)}'
    _, _, body = fetch(url)
    if not body:
        print('Search failed.')
        return

    parser = _DDGParser()
    parser.feed(body)
    results = parser.results[:10]

    if not results:
        print('No results found.')
        return

    for i, r in enumerate(results, 1):
        title = r['title'].strip()
        link  = r['url'].strip()
        snip  = r['snippet'].strip()
        print(f'{i}. {title}')
        print(f'   {link}')
        if snip:
            print(f'   {snip}')
        print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    if not args or args[0] == '-h':
        cmd_help()
        return

    flag = args[0]
    if flag == '-u':
        if len(args) < 2:
            sys.exit('Error: -u requires a URL argument')
        cmd_url(args[1])
    elif flag == '-s':
        if len(args) < 2:
            sys.exit('Error: -s requires a search term')
        cmd_search(' '.join(args[1:]))
    else:
        print(f'Unknown option: {flag}')
        cmd_help()
        sys.exit(1)


if __name__ == '__main__':
    main()

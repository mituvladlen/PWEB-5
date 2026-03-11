#!/usr/bin/env python3
"""go2web - A command-line HTTP client using raw sockets (no HTTP libraries)."""

import sys
import os
import json
import socket
import ssl
import hashlib
import time
from html.parser import HTMLParser
from urllib.parse import urlparse, quote_plus, parse_qs

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

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


def raw_request(url: str, accept: str = 'text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8'):
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
        f'Accept: {accept}\r\n'
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


# ---------------------------------------------------------------------------
# File-based HTTP cache
# ---------------------------------------------------------------------------

CACHE_DIR = os.path.join(os.path.expanduser('~'), '.go2web_cache')


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def _cache_path(url: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, _cache_key(url) + '.json')


def _parse_max_age(headers: dict) -> int | None:
    cc = headers.get('cache-control', '')
    for part in cc.split(','):
        part = part.strip()
        if part.startswith('max-age='):
            try:
                return int(part[8:])
            except ValueError:
                pass
        if part in ('no-store', 'no-cache'):
            return 0
    return None


def _parse_expires(headers: dict) -> float | None:
    exp = headers.get('expires', '')
    if not exp:
        return None
    for fmt in (
        '%a, %d %b %Y %H:%M:%S %Z',
        '%A, %d-%b-%y %H:%M:%S %Z',
        '%a %b %d %H:%M:%S %Y',
    ):
        try:
            import calendar
            import email.utils
            t = email.utils.parsedate_to_datetime(exp)
            return t.timestamp()
        except Exception:
            pass
    return None


def cache_load(url: str):
    """Return (headers, body) from cache if fresh, else None."""
    path = _cache_path(url)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            entry = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    headers = entry.get('headers', {})
    stored_at = entry.get('stored_at', 0)
    max_age = _parse_max_age(headers)

    if max_age is not None:
        if max_age <= 0 or (time.time() - stored_at) > max_age:
            return None
    else:
        exp = _parse_expires(headers)
        if exp is not None:
            if time.time() > exp:
                return None
        else:
            # No explicit TTL — honour for 1 hour by default
            if (time.time() - stored_at) > 3600:
                return None

    return headers, entry.get('body', '')


def cache_store(url: str, headers: dict, body: str):
    """Persist a response to the cache."""
    path = _cache_path(url)
    entry = {
        'url': url,
        'stored_at': time.time(),
        'headers': headers,
        'body': body,
    }
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(entry, f, ensure_ascii=False)
    except OSError:
        pass


def fetch(url: str, _hops: int = 0, accept: str = 'text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8'):
    """GET a URL, following redirects, with cache support."""
    if _hops >= MAX_REDIRECTS:
        print('Error: too many redirects', file=sys.stderr)
        return None, None, None

    # Check cache before hitting the network
    cached = cache_load(url)
    if cached is not None:
        headers, body = cached
        return 200, headers, body

    status, headers, body = raw_request(url, accept=accept)
    if status in REDIRECT_CODES:
        loc = headers.get('location', '')
        if loc:
            return fetch(resolve_url(url, loc), _hops + 1, accept=accept)

    # Cache successful responses
    if status == 200 and headers and body:
        cache_store(url, headers, body)

    return status, headers, body


# ---------------------------------------------------------------------------
# HTML → plain text
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    # Non-void elements whose content should be fully skipped
    _SKIP = frozenset({
        'script', 'style', 'noscript', 'head', 'iframe', 'svg',
    })
    # Void elements (no closing tag) to silently ignore
    _VOID = frozenset({
        'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
        'link', 'meta', 'param', 'path', 'source', 'track', 'wbr',
    })
    _BLOCK = frozenset({
        'p', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'tr', 'td', 'th', 'section', 'article', 'header', 'footer',
        'nav', 'main', 'blockquote',
    })

    def __init__(self):
        super().__init__()
        self._buf = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in self._SKIP:          # non-void: track depth
            self._skip += 1
        elif t == 'br' or (t in self._BLOCK and self._skip == 0):
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
# Content rendering
# ---------------------------------------------------------------------------

def format_json(body: str) -> str:
    """Pretty-print JSON, or return raw string if parsing fails."""
    try:
        return json.dumps(json.loads(body), indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, ValueError):
        return body


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_help():
    print(
        'go2web - A simple CLI HTTP client\n'
        '\n'
        'Usage:\n'
        '  go2web -u <URL>          Make an HTTP request to URL and print the response\n'
        '  go2web -u <URL> --json   Request JSON from URL and pretty-print it\n'
        '  go2web -s <search-term>  Search the web and print the top 10 results\n'
        '                          (prompts to open a result after listing)\n'
        '  go2web -h                Show this help message\n'
        '\n'
        'Examples:\n'
        '  go2web -u https://example.com\n'
        '  go2web -u https://api.github.com/users/github --json\n'
        '  go2web -s python tutorial\n'
        '  go2web -s "web programming"\n'
    )


def cmd_url(url: str, prefer_json: bool = False):
    if prefer_json:
        accept = 'application/json,*/*;q=0.8'
    else:
        accept = 'text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8'

    status, headers, body = fetch(url, accept=accept)
    if body is None:
        return
    ct = (headers or {}).get('content-type', '')
    if 'json' in ct:
        print(format_json(body))
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

    # Allow the user to follow a result
    try:
        choice = input('Enter a result number to open it (or press Enter to quit): ').strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if not choice:
        return
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(results):
            print()
            cmd_url(results[idx]['url'])
        else:
            print('Invalid number.')
    except ValueError:
        print('Invalid input.')


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
        prefer_json = '--json' in args
        url = args[1]
        cmd_url(url, prefer_json=prefer_json)
    elif flag == '-s':
        if len(args) < 2:
            sys.exit('Error: -s requires a search term')
        cmd_search(' '.join(a for a in args[1:] if a != '--json'))
    else:
        print(f'Unknown option: {flag}')
        cmd_help()
        sys.exit(1)


if __name__ == '__main__':
    main()

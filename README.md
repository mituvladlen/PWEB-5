# go2web

A command-line HTTP client built with **raw TCP sockets** — no HTTP libraries used.

## Features

- Fetch any URL and print a clean, human-readable response (HTML stripped)
- Search the web via DuckDuckGo and print the top 10 results
- Follow search result links directly from the CLI
- Automatic HTTP redirect handling (301, 302, 303, 307, 308)
- File-based HTTP cache (respects `Cache-Control` and `Expires` headers)
- Content negotiation — request JSON from APIs with `--json` flag

## Requirements

- Python 3.10+

## Usage

```bash
go2web -h                        # show help
go2web -u <URL>                  # fetch URL and print readable response
go2web -u <URL> --json           # fetch URL, request JSON, pretty-print it
go2web -s <search-term>          # search and print top 10 results
```

### Examples

```bash
go2web -u https://example.com
go2web -u https://api.github.com/users/github --json
go2web -s python web programming
```

After `-s` prints results, you'll be prompted to enter a number to open that link.

## Demo

![go2web demo](demo.gif)

## Cache

Responses are cached in `~/.go2web_cache/`. The cache respects:

- `Cache-Control: max-age=N` — cached for N seconds
- `Cache-Control: no-store` / `no-cache` — not cached
- `Expires` header — cached until expiry date
- Default TTL of 1 hour when no cache headers are present

## How it works

All HTTP communication is done over raw `socket.socket` TCP connections (with `ssl.wrap_socket` for HTTPS). No `urllib`, `requests`, `httpx`, or any other HTTP library is used.

# SP SO Web Toolbox

A browser-based operations toolbox for the SP Support & Operations team.
Runs as a single-page application served via nginx, with tools loaded dynamically from a `.env` config file.

## Architecture

```
index.html        — Main shell (sidebar, tabs, welcome screen)
proxy.py          — Local CORS proxy (Flask) for PhenixRTS API calls
monitor.html      — PhenixRTS Channel Health Monitor tool
.env              — Tool registry and app config (not committed)
```

The proxy runs on `localhost:5050` and is exposed through nginx at `/phenix-proxy/`.

## Requirements

- Python 3.6+
- nginx (serving on port 443 with self-signed SSL)
- Git (for the Update button)

```bash
pip3 install flask flask-cors requests
```

## Usage

Start the proxy:

```bash
python3 proxy.py
```

Open the toolbox in the browser:

```
https://<server-ip>/
```

## Tool Registry (.env format)

```
APP_TITLE=SP SO Web Toolbox
APP_VERSION=1.5.0

# TOOL_n=file.html|Name|Description|icon|Category|BADGE
TOOL_1=monitor.html|Channel Monitor|PhenixRTS real-time health|📡|Monitoring|LIVE
```

## Version History

| Version | Date       | Changes |
|---------|------------|---------|
| 1.5.0   | 2026-03-27 | Git Pull button moved to index; auto-reload on success |
| 1.4.0   | 2026-03-27 | Lazy redraw — only update screen on state changes |
| 1.3.0   | 2026-03-27 | Switched terminal UI from rich to curses |
| 1.2.0   | 2026-03-25 | Added alphabetical channel sorting |
| 1.1.0   | 2026-03-25 | Full English translation + rich GUI + 1s refresh |
| 1.0.0   | 2026-03-25 | Initial real-time health monitor |

**Current Version: 1.5.0**

## Notes

- The proxy executes `git pull` in its own directory when the Update button is clicked.
- After a successful pull, the page reloads automatically after 2 seconds.
- The proxy process itself must be restarted manually if `proxy.py` changes.
- All PhenixRTS requests are read-only (GET). The git pull endpoint is the only write operation.

## License

MIT License

# SP SO Web Toolbox

A browser-based internal operations toolbox for the SP Support & Operations team. Runs as a single-page application served via nginx, with tools loaded dynamically and securely through a Flask proxy.

> **For server setup and rebuild instructions, see [`SERVER_REBUILD.md`](SERVER_REBUILD.md).**

---

## Architecture

```
index.html              — Main shell (sidebar, tabs, welcome screen, git update button)
proxy.py                — Flask CORS proxy: serves config, PhenixRTS API, MTR, Ingest Analyzer
nginx.conf              — Clean nginx config (CentOS/RHEL)
nginx-debian.conf       — nginx site config (Debian/Ubuntu/WSL)
so-proxy.service    — systemd service for the proxy
.env                    — Tool registry and credentials (not committed to Git)
mtr-results/            — Saved MTR trace results (JSON, persisted on disk)
ingest-results/         — Saved Ingest Analyzer results (ZIP + report directory)
```

The proxy runs on `localhost:5050` and is exposed through nginx at `/so-proxy/`. The `.env` file is blocked from direct browser access — only the proxy reads it and exposes safe keys via `/so-proxy/config`.

---

## Tools

### 📡 Channel Health Monitor (`monitor.html`)
Real-time dashboard for monitoring PhenixRTS live channels. Connects to the PhenixRTS API using credentials stored in `.env` (never exposed to the browser). Displays publisher count and source status per channel, with lazy rendering that only updates when state changes. Supports login form with configurable refresh interval, channel search, pagination, and auto-detection of new channels while running.

### 🔗 SRT URI Builder (`SRT-URI-Builder.html`)
A form-based tool for building valid SRT connection URIs. Supports all standard SRT parameters (mode, passphrase, latency, pbkeylen, and advanced options). Server presets are loaded from `SRT_SERVER_*` entries in `.env`. The passphrase is pre-filled from `SRT_PASSPHRASE` in `.env` and masked by default. Generates a URI that can be copied to clipboard or opened directly in VLC.

### 🔍 MTR Network Trace (`MTR-Trace.html`)
Runs `mtr` on the server and streams results back to the browser. Supports two modes: packet count or time duration (converted to cycles at 1 packet/second). Jobs run as background threads — closing the browser does not stop the trace. Results are saved to disk as JSON and persist across proxy restarts. Features include real-time countdown progress bar, tag labeling for filtering, a running jobs panel, and a searchable history.

### 📺 RMG RTS Multiview Dashboard (`RMG-RTS-Multiview.html`)
Launcher page for the PhenixRTS external multiview dashboard. Since `dashboard.phenixrts.app` blocks iframe embedding via `X-Frame-Options`, this tool provides a direct link that opens the dashboard in a new tab.

### 🔬 Ingest Analyzer (`Ingest-Analyzer.html`)
Frontend for the `run-ingest-analysis.sh` script. Accepts SRT, RTMP, UDP, custom URLs, or server-side `.ts` file paths. Runs the analysis as a background job (~2 minutes) and polls for completion. On finish, the full report directory (HTML + charts) and ZIP archive are copied to `ingest-results/`. Results can be opened directly in the browser or downloaded as a ZIP. Supports tag labeling. SRT local host presets are loaded from `SRT_LOCAL_*` in `.env`, and the passphrase is pre-filled from `SRT_PASSPHRASE`.

---

## .env Format

```env
APP_TITLE=SP SO Web Toolbox
APP_VERSION=2.0.0

# Tools — format: TOOL_n=file.html|Name|Description|icon|Category|BADGE
TOOL_1=monitor.html|Channel Monitor|PhenixRTS real-time health|📡|Monitoring|LIVE
TOOL_2=SRT-URI-Builder.html|SRT URI Builder|Build SRT connection strings|🔗|Streaming|
TOOL_3=MTR-Trace.html|MTR Network Trace|Server-side route tracing|🔍|Network|
TOOL_4=RMG-RTS-Multiview.html|RMG RTS Multiview|PhenixRTS live dashboard|📺|Monitoring|
TOOL_5=Ingest-Analyzer.html|Ingest Analyzer|Validate ingest stream quality|🔬|Streaming|

# SRT URI Builder — server presets (IP|Label)
SRT_SERVER_1=203.0.113.10|Ingest EU-West
SRT_SERVER_2=203.0.113.20|Ingest UK

# Ingest Analyzer / SRT Builder — local host presets (IP|Label)
SRT_LOCAL_1=10.0.0.1|INX01
SRT_LOCAL_2=10.0.0.2|INX02

# Shared SRT passphrase (pre-filled in SRT Builder and Ingest Analyzer)
SRT_PASSPHRASE=your-passphrase-here

# PhenixRTS credentials — server-side only, never sent to browser
PHENIXRTS_APP_ID=your-app-id
PHENIXRTS_PASSWORD=your-password
```

---

## Proxy endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/so-proxy/config` | GET | Safe config from `.env` (tools, presets, passphrase) |
| `/so-proxy/channels` | GET | PhenixRTS channel list |
| `/so-proxy/publishers/count/<id>` | GET | Publisher count for a channel |
| `/so-proxy/git-pull` | POST | Run `git pull` on the server |
| `/so-proxy/server-info` | GET | Local IPs, gateway, public IP |
| `/so-proxy/mtr/stream` | GET | SSE stream for MTR trace |
| `/so-proxy/mtr/running` | GET | List of running MTR jobs |
| `/so-proxy/mtr/results` | GET | List of completed MTR results |
| `/so-proxy/mtr/results/<file>` | GET | Download MTR result JSON |
| `/so-proxy/mtr/tag/<file>` | POST | Update tag on a result |
| `/so-proxy/ingest/run` | POST | Start an ingest analysis job |
| `/so-proxy/ingest/status/<id>` | GET | Poll job status |
| `/so-proxy/ingest/results` | GET | List saved ingest results |
| `/so-proxy/ingest/report/<dir>/<file>` | GET | Serve report file (HTML/charts) |
| `/so-proxy/ingest/download/<file>` | GET | Download ZIP |

---

## Version History

| Version | Date       | Changes |
|---------|------------|---------|
| 2.8.4   | 2026-04-23 | Re-run saves new JSON + appears in history; pollStatus sets _currentResultFile; loadHistory on all outcomes |
| 2.8.3   | 2026-04-23 | SO Video Analyser: Re-run button on loaded result + each history item |
| 2.8.2   | 2026-04-23 | override uses _currentResultFile, schedule full UTC, auto-refresh on complete, constrained baseline, hi-sub date/codec/res/fps, no res/fps badges, no latency field |
| 2.8.1   | 2026-04-23 | Always save log JSON on failure/error, FAILED/ERROR badges, log viewer |
| 2.7.0   | 2026-04-22 | SO Video Analyser rename, 50i FPS fix, override→JSON, .ts download, scheduled jobs, metadata header; index: clocks |
| 2.6.3   | 2026-04-21 | GOP: fix 50i→25fps, HLG SDR, AAC-LC, audio tracks via -show_programs; Generate Report; Override |
| 2.6.2   | 2026-04-16 | GOP: larger fonts, GOP avg hero, history filters, bitrate fallback, AAC FLTP |
| 2.6.1   | 2026-04-16 | GOP: bitrate fallback, AAC FLTP, GOP badge, tag split, server filter |
| 2.6.0   | 2026-04-16 | GOP compliance RAG, graceful timeout, NAL/IDR, open/closed GOP, FPS flag; MTR hops fix, bulk delete |
| 2.5.0   | 2026-04-14 | New: GOP Analyzer — SRT capture, IDR detection, GOP structure visualizer (I/P/B/S), full stream info |
| 2.4.0   | 2026-04-14 | MTR: ICMP/UDP-53 toggle, Country/ASN geo, -b flag |
| 2.3.0   | 2026-04-13 | Proxy renamed to so-proxy; ADMIN_PASSWORD; MTR host sanity check; README modal |
| 2.2.0   | 2026-04-10 | MTR kill/delete with confirmation, date picker filter, history panels |
| 2.1.0   | 2026-04-10 | MTR: history filters, multi-tag, Date End + HH:MM:SS duration, Time mode left, copy fix |
| 2.0.0   | 2026-04-07 | MTR background jobs with disk persistence, tags, running panel; Ingest Analyzer tags; bufsize fix; progress bar with countdown |
| 1.9.0   | 2026-04-06 | Ingest Analyzer: background jobs, ZIP copy, HTML report served via proxy; SRT_LOCAL presets |
| 1.8.0   | 2026-04-06 | MTR: time mode fix (cycles instead of timeout), SSE tick countdown, progress bar |
| 1.7.0   | 2026-04-01 | MTR Network Trace tool with SSE streaming, server info, history |
| 1.6.0   | 2026-04-01 | RMG RTS Multiview Dashboard launcher |
| 1.5.2   | 2026-04-01 | nginx proxy_buffering off for SSE; per-distro nginx configs |
| 1.5.1   | 2026-03-31 | Config loaded via proxy instead of public .env; SRT_PASSPHRASE secure |
| 1.5.0   | 2026-03-27 | Git Pull button in index with auto-reload; systemd service |
| 1.4.0   | 2026-03-27 | Lazy redraw — only update on state changes |
| 1.3.0   | 2026-03-27 | Switched Channel Monitor UI from rich to curses |
| 1.2.0   | 2026-03-25 | Alphabetical channel sorting |
| 1.1.0   | 2026-03-25 | Full English translation + rich GUI |
| 1.0.0   | 2026-03-25 | Initial PhenixRTS Channel Health Monitor |

**Current Version: 2.8.4**

---

## Notes

- The proxy executes `git pull` in its own directory when the Update button is clicked. The page reloads automatically after a successful pull. The proxy process itself must be restarted manually if `proxy.py` changes.
- MTR jobs run as background daemon threads and survive browser disconnects. State is written to `mtr-results/*.running.json` while running and renamed to `*.json` on completion.
- The Ingest Analyzer requires `run-ingest-analysis.sh` and its dependencies (`ffprobe`, `perl >= 5.36`, `gnuplot`, `jq`, `bc`) to be installed on the server.
- `generate-report.sh` must use perl >= 5.36 for the `-g` flag. If using perlbrew, ensure the correct perl path is set in `so-proxy.service`.

## License

MIT License

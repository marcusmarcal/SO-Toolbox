# SP SO Web Toolbox

A browser-based internal operations toolbox for the SP Support & Operations team. Runs as a single-page application served via nginx, with tools loaded dynamically and securely through a Flask proxy.

> **For server setup and rebuild instructions, see [`SERVER_REBUILD.md`](SERVER_REBUILD.md).**

---

## Architecture

```
index.html              — Main shell (sidebar, tabs, welcome screen, git update button, real-time server resource monitor in topbar)
CHANGELOG.md            — Version history (Keep a Changelog format); read by index.html at runtime for version badge and changelog modal
proxy.py                — Flask CORS proxy: serves config, PhenixRTS API, MTR, Ingest Analyzer, server resource stats
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

### ⛨ Id3as DC Monitor (`id3as-DC-Monitor.html`)

**Id3as DC Monitor** is a lightweight, browser-based monitoring dashboard designed to provide real-time visibility into distributed encoding infrastructure across multiple Data Centers (DCs).

It serves as a centralized operational tool for tracking channels, nodes, live events, scheduled services, and system logs, enabling fast troubleshooting and operational awareness for broadcast/streaming environments.

**Tech:**
Communicates with the id3as API via the SO proxy using `PRFAUTH` from `.env` — credentials never reach the browser. DC hostnames are stored in `.env` (`ID3AS_HOST_IX`, `ID3AS_HOST_EQ`) and served to the browser at startup via `/so-proxy/id3as/config` — no hostnames are hardcoded in source files. Routes are registered via a Flask Blueprint (`id3as_routes.py`).

### 📡 RTS Monitor (`RTS-Monitor.html`)

Real-time dashboard for monitoring PhenixRTS live channels. Connects to the PhenixRTS API using credentials stored in `.env` (never exposed to the browser). Displays publisher count and source status per channel, with lazy rendering that only updates when state changes. Supports login form with configurable refresh interval, channel search, pagination, and auto-detection of new channels while running.

All RTS-related proxy routes (`/channels`, `/publishers/count`, `/rts/viewing-report`) are isolated in `rts_routes.py` and registered as a Flask Blueprint, sharing the main `requests.Session` for connection-pool reuse.

**Tabs:**

- **Channels** — live channel table with publisher status, search, and pagination.
- **Viewing Report** — query Phenix session data by Event ID and time window (UTC). Results load in batches of 100 rows with a _Load more_ button to avoid blocking the browser on large reports. Timestamps always displayed in UTC. Full CSV download available.

Visual design aligned with the SO-Toolbox index: Space Mono + Syne fonts, purple accent (`#a18bf5`), animated hex logo mark, and noise texture overlay.

## BTV Video Analyser (`BTV-Video-Analyser.html`)

BTV Analyser (Better Than VISA) is a web-based tool designed to capture, inspect, and validate video streams—primarily over SRT—by analyzing their encoding structure, GOP behavior, and compliance against predefined broadcast specifications.

### Overview

The tool captures a short sample from an SRT stream (or accepts uploaded `.ts` files), runs a detailed analysis using `ffprobe`, and presents a comprehensive breakdown of the stream’s structure and quality.

It focuses on key aspects such as:

- GOP (Group of Pictures) structure and consistency
- Presence and frequency of IDR frames
- Frame types distribution (I / P / B frames)
- Bitrate, resolution, codec, and profile
- Audio and video stream characteristics
- AV sync and timestamp jitter
- Overall compliance against configurable specs

### 🔬 RTS Ingest Analyzer (`Ingest-Analyzer.html`)

Frontend for the `run-ingest-analysis.sh` script from RTS team. Accepts SRT, RTMP, UDP, custom URLs, or server-side `.ts` file paths. Runs the analysis as a background job (~2 minutes) and polls for completion. On finish, the full report directory (HTML + charts) and ZIP archive are copied to `ingest-results/`. Results can be opened directly in the browser or downloaded as a ZIP. Supports tag labeling. SRT local host presets are loaded from `SRT_LOCAL_*` in `.env`, and the passphrase is pre-filled from `SRT_PASSPHRASE`.

### 🔗 SRT URI Builder (`SRT-URI-Builder.html`)

A form-based tool for building valid SRT connection URIs. Supports all standard SRT parameters (mode, passphrase, latency, pbkeylen, and advanced options). Server presets are loaded from `SRT_SERVER_*` entries in `.env`. The passphrase is pre-filled from `SRT_PASSPHRASE` in `.env` and masked by default. Generates a URI that can be copied to clipboard or opened directly in VLC.

### 🔍 MTR Network Trace (`MTR-Trace.html`)

Runs `mtr` on the server and streams results back to the browser. Supports two modes: packet count or time duration (converted to cycles at 1 packet/second). Jobs run as background threads — closing the browser does not stop the trace. Results are saved to disk as JSON and persist across proxy restarts. Features include real-time countdown progress bar, tag labeling for filtering, a running jobs panel, and a searchable history.

### RTS Player (`RTS-Test-Player.html`)

Generates and launches RTS player URLs, automatically injecting the required viewer token.

### Jira Formatter (`jira-formatter.html`)

Transforms data copied from Service Now onboarding requests into a clean, structured table format for easier readability.

### RMG Purge URL Generator (`purge-url-generator.html`)

Builds cache purge URLs based on a list of Event IDs and a specified month/year.

### Chrome Extensions (`sp-extensions.html`)

A collection of Chrome extensions designed to streamline the daily workflow of the Streaming Operations team:

- **RITM Ticket Formatter**  
  Reads a ServiceNow RITM page and converts it into a clean, well-structured Jira ticket. One-click copy with rich text formatting, ready to paste into Jira.

- **TXEdge VLC Launcher**  
  Automatically detects SRT output streams on any TXEdge page and launches them in VLC with a single click. The passphrase is securely stored in Chrome.

- **SO Video Analyser for Chrome**  
  Detects SRT streams on TXEdge and TXCore pages and triggers a complete video analysis via the SP SO Proxy. Results are displayed inline with a single click. Proxy URL and passphrase are saved once and applied automatically thereafter.

---

## .env Format

```env
APP_TITLE=SP SO Web Toolbox

# Tools — format: TOOL_n=file.html|Name|Description|icon|Category|BADGE
TOOL_1=monitor.html|Channel Monitor|PhenixRTS real-time health|📡|Monitoring|LIVE
TOOL_2=SRT-URI-Builder.html|SRT URI Builder|Build SRT connection strings|🔗|Streaming|
TOOL_3=MTR-Trace.html|MTR Network Trace|Server-side route tracing|🔍|Network|
TOOL_4=RMG-RTS-Multiview.html|RMG RTS Multiview|PhenixRTS live dashboard|📺|Monitoring|
TOOL_5=Ingest-Analyzer.html|Ingest Analyzer|Validate ingest stream quality|🔬|Streaming|
TOOL_6=id3as-DC-Monitor.html|id3as DC Monitor|id3as channel & node monitoring|⛨|Monitoring|

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

# id3as authentication — server-side only, never sent to browser
PRFAUTH=your-prfauth-token-here

# id3as DC hosts — server-side only, never hardcoded in source files
ID3AS_HOST_IX=id3as-ix.example.co.uk
ID3AS_HOST_EQ=id3as-eq.example.co.uk
```

---

## Proxy endpoints

| Endpoint                                   | Method | Description                                                     |
| ------------------------------------------ | ------ | --------------------------------------------------------------- |
| `/so-proxy/config`                         | GET    | Safe config from `.env` (tools, presets, passphrase)            |
| `/so-proxy/channels`                       | GET    | PhenixRTS channel list                                          |
| `/so-proxy/publishers/count/<id>`          | GET    | Publisher count for a channel                                   |
| `/so-proxy/git-pull`                       | POST   | Run `git pull` on the server                                    |
| `/so-proxy/server-info`                    | GET    | Local IPs, gateway, public IP                                   |
| `/so-proxy/mtr/stream`                     | GET    | SSE stream for MTR trace                                        |
| `/so-proxy/mtr/running`                    | GET    | List of running MTR jobs                                        |
| `/so-proxy/mtr/results`                    | GET    | List of completed MTR results                                   |
| `/so-proxy/mtr/results/<file>`             | GET    | Download MTR result JSON                                        |
| `/so-proxy/mtr/tag/<file>`                 | POST   | Update tag on a result                                          |
| `/so-proxy/ingest/run`                     | POST   | Start an ingest analysis job                                    |
| `/so-proxy/ingest/status/<id>`             | GET    | Poll job status                                                 |
| `/so-proxy/ingest/results`                 | GET    | List saved ingest results                                       |
| `/so-proxy/ingest/report/<dir>/<file>`     | GET    | Serve report file (HTML/charts)                                 |
| `/so-proxy/ingest/download/<file>`         | GET    | Download ZIP                                                    |
| `/so-proxy/id3as/config`                   | GET    | DC GUI base URLs built from `ID3AS_HOST_*` in `.env`            |
| `/so-proxy/id3as/<dc>/channels/<variant>`  | GET    | id3as channel list (default \| racing_uk)                       |
| `/so-proxy/id3as/<dc>/flags/channels`      | GET    | Active warnings per channel                                     |
| `/so-proxy/id3as/<dc>/running_events`      | GET    | Currently running events                                        |
| `/so-proxy/id3as/<dc>/nodes`               | GET    | Node list with status                                           |
| `/so-proxy/id3as/<dc>/logs`                | GET    | System event log (today UTC)                                    |
| `/so-proxy/id3as/<dc>/logs/<y>/<m>/<d>`    | GET    | System event log for specific date                              |
| `/so-proxy/id3as/<dc>/channel/<id>/status` | GET    | Single channel enc/src state                                    |
| `/so-proxy/server-stats`                   | GET    | Live CPU, memory and disk usage (refreshed every 5 s in topbar) |

---

## Version History

See [CHANGELOG.md](CHANGELOG.md) for full version history following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

**Current version is always the first entry in `CHANGELOG.md`.** The `index.html` reads `CHANGELOG.md` at runtime to display the version badge and changelog modal — no hardcoding required.

---

## Notes

- For .ts file uploads via the SO Video Analyser, nginx must allow large request bodies. Add `client_max_body_size 2G;` inside the relevant `server {}` or `location /so-proxy/` block in your nginx config.
- The proxy executes `git pull` in its own directory when the Update button is clicked. The page reloads automatically after a successful pull. The proxy process itself must be restarted manually if `proxy.py` changes.
- MTR jobs run as background daemon threads and survive browser disconnects. State is written to `mtr-results/*.running.json` while running and renamed to `*.json` on completion.
- The Ingest Analyzer requires `run-ingest-analysis.sh` and its dependencies (`ffprobe`, `perl >= 5.36`, `gnuplot`, `jq`, `bc`) to be installed on the server.
- `generate-report.sh` must use perl >= 5.36 for the `-g` flag. If using perlbrew, ensure the correct perl path is set in `so-proxy.service`.

## License

MIT License

# SP SO Web Toolbox

Browser-based operations toolbox for the Streaming & Broadcast Operations team. A single-page application served by nginx, with tools loaded dynamically and backed by a Flask proxy (`so-proxy`) that keeps credentials server-side and aggregates third-party APIs.

> **Related documents**
> - Server setup and rebuild: [`SERVER_REBUILD.md`](SERVER_REBUILD.md)
> - id3as monitoring deployment: [`DEPLOY_id3as.md`](DEPLOY_id3as.md)
> - API reference: [`SO-Toolbox-API-Docs.html`](SO-Toolbox-API-Docs.html)
> - Version history: [`CHANGELOG.md`](CHANGELOG.md)

## Contents

- [Overview](#overview)
- [Tools](#tools)
- [Backend](#backend)
- [Configuration](#configuration)
- [Proxy Endpoints](#proxy-endpoints)
- [Directory Structure](#directory-structure)
- [Quick Start](#quick-start)
- [Requirements](#requirements)
- [Security Notes](#security-notes)
- [Troubleshooting](#troubleshooting)
- [Versioning](#versioning)
- [License](#license)

---

## Overview

The Toolbox provides real-time monitoring, stream analysis and operational management for broadcast and streaming infrastructure. The frontend (static HTML/JS) is separated from the backend (Flask proxy), so credentials never reach the browser and every external API is reached through one authenticated entry point.

| Component | Role |
|-----------|------|
| `index.html` | Application shell: navigation, tool registry, server monitoring, README/changelog viewers |
| `proxy.py` | Flask CORS proxy handling API requests, credentials and backend integrations; registers all Blueprints |
| `nginx.conf` / `nginx-debian.conf` | Web server configuration (RHEL/CentOS and Debian/Ubuntu) |
| `so-proxy.service` | systemd unit for the Flask proxy |
| `.env` | Tool registry, server presets and credentials (git-ignored, server-side only) |

---

## Tools

Tools are registered in `.env` (`TOOL_n=...`) and rendered in the sidebar and welcome cards. Each tool is a standalone HTML page that talks to the proxy.

### Video & Stream Analysis

#### BTV Video Analyser
`BTV-Video-Analyser.html` — Professional video stream analysis for SRT, RTMP and uploaded files.

- **Capabilities** — GOP analysis, frame type distribution, codec/profile detection, bitrate/resolution analysis
- **Compliance** — Configurable specs per workflow for IDR presence, GOP structure, B-frames, audio/video sync
- **Reports** — Visual compliance dashboard, text/Jira export, MediaInfo report viewer
- **Tech** — `ffprobe` / `mediainfo` backend, result storage with filtering and pagination

#### Ingest Analyser
`Ingest-Analyzer.html` — Stream quality validation for ingest sources (SRT, RTMP, UDP, file upload).

- **Capabilities** — Runs `run-ingest-analysis.sh` (~2 min) and generates reports with charts
- **Backend** — Background jobs with progress monitor, ZIP + HTML report download
- **Tech** — Requires `ffprobe`, `perl >= 5.36`, `gnuplot`, `jq`, `bc`

#### Live Probe
Integrated in BTV Video Analyser — Real-time network telemetry for SRT sources.

- **Metrics** — PCR interval (IAT), TS continuity-counter loss (MLR), bitrate
- **Display** — Live area chart with avg/max readouts and configurable alarm thresholds
- **Tech** — `srt-live-transmit` backend (Haivision SRT tools), no external probe dependency

### Real-Time Monitoring

#### RTS Monitor
`RTS-Monitor.html` — PhenixRTS channel and viewing statistics dashboard.

- **Channels** — Live channel table with publisher status, alias, channel ID, stream key and forked-from tracking
- **Viewing Report** — Session data by Event ID and UTC time window, paginated (100 rows per batch)
- **Fork Origin** — Fork events by date range with source/destination channel resolution
- **Features** — Search across all columns, supplier filter (RMG HA / RMG EBC), export to Excel
- **Tech** — PhenixRTS API via proxy, no credentials in the browser

#### id3as DC Monitor
`id3as-DC-Monitor.html` — Distributed encoding infrastructure monitoring across multiple data centres.

- **Channels** — Live channel state (encoder/source status), search and filter
- **Nodes** — Node list with health and alarm status, event-starting grace period
- **Events** — Running scheduled events with channel flag warnings
- **Logs** — System event log (by date, today UTC by default)
- **Features** — Real-time flag warnings per channel or event, 3-minute alarm suppression while an event is starting, drill-down to channel/node status
- **Tech** — id3as API via proxy using the PRFAUTH token; DC hostnames come from `.env`

#### Probe Monitoring
`ProbeMonitoring.html` — Control-room style view of distributed probe channels.

- **Features** — Two independent channel slots, 40 configurable Id3as AWS + Probe URL pairs, fixed RMG MV reference feeds
- **Tech** — `localStorage` persistence, dark control-room UI

### SRT & Ingest Control

#### SRT URI Builder
`SRT-URI-Builder.html` — Form-based SRT connection URI generator.

- **Features** — Mode, passphrase, latency, pbkeylen and advanced SRT options; server/local presets from `.env`
- **Output** — Copy-ready SRT URIs

#### SRT Ingest
`srt_tool.html` — SRT stream ingestion and management.

- **Capabilities** — Single and multi-destination ingest, shared ffmpeg mode (passthrough to many targets), file-based or B&T (colour bars + 1 kHz tone) source
- **Features** — Auto-retry on failure, per-job restart, error tracking, bitrate monitor, source picker with search
- **B&T mode** — Burns a live UTC clock overlay (HH:MM:SS.mmm) for latency measurement

#### SRT Push Monitor
`srt_push_monitor.html` — Manages concurrent SRT push services (static image or HTML page capture).

- **Features** — Per-service configuration, preview and log viewing, enable toggle, source type switching
- **Tech** — Multi-service `srt-push.py` daemon run as a systemd unit

### Broadcast Infrastructure

#### RTS Player
`RTS-Test-Player.html` — Generates and launches RTS player URLs with automatic viewer token injection.

#### TXCore Manager
`TXCore-Manager.html` — TXCore channel provisioning for the AVE / LMK / YER sites.

- **Features** — Category creation, bulk channel form, request preview, async job monitoring with live log
- **Config** — Site-specific IP prefixes, auto-fill from First CH#, live multicast address preview

#### RTS BC ConfigTool
`RTS-BC-ConfigTool.html` — Broadcast configuration management for RTS services.

#### RTS Stats Channel Publisher
`RTS-StatsChannelPublisher.html` — Loads StatsChannelPublisher with a given publishing token.

### Admin & Utilities

#### Jira Formatter
`jira-formatter.html` — Converts ServiceNow Requests (RITM) and Incidents (INC) into a clean, copy-ready Jira ticket format.

#### RMG Purge URL Generator
`purge-url-generator.html` — Builds cache purge URLs from Event IDs and month/year.

#### Chrome Extensions
`sp-extensions.html` — Browser extensions for operational workflows.

- **RITM Ticket Formatter** — Convert ServiceNow RITM pages to Jira tickets
- **TXEdge VLC Launcher** — Detect and launch SRT streams in VLC (passphrase stored securely)
- **SO Video Analyser** — Trigger video analysis from TXEdge/TXCore pages, results inline

#### SO Toolbox Admin
`so-toolbox-admin.html` — Administration console: users, live sessions and the server-side `.env`.

- **Users tab** — Roles admin, engineer, specialist, analyst, user; fields `rota_status` (active / inactive / observer), `team` (SOE / SOS / NA), `display_name`, `employee_id`
- **Online tab** — Currently logged-in users with session metadata; admins can kick a user
- **Environment tab** (admin only) — Manage every key in `.env`: add, edit inline, rename, delete; secrets masked with reveal-on-demand; file-order view with section headers and comments; search and filters (secrets, needs-restart, not referenced, empty, duplicates); LIVE / RESTART / REF badge per key showing which Blueprint reads it and whether a proxy restart is needed; raw editor with server-side validation and conflict detection; automatic timestamped backups before every write with diff, restore and delete; one-click proxy restart

#### WC2026 Rota Management
`wc2026_rota_management.html` — World Cup 2026 engineering rota planner.

- **Integration** — openfootball sync for match schedules, team names and kickoff times
- **Features** — Four engineer slots, auto-assign, bulk edit, score tracking, CSV import/export, filter by date/team

#### MTR Network Trace
`MTR-Trace.html` — Server-side network path tracing with streamed results.

- **Features** — Packet-count or time-duration mode, background jobs, tagged result storage, browsable history

---

## Backend

`proxy.py` is the entry point and registers the following Flask Blueprints:

| Blueprint | File | Purpose |
|-----------|------|---------|
| auth | `routes_auth.py` | Authentication, roles, sessions, user management |
| env | `routes_env.py` | Admin-only `.env` manager: parsed view, CRUD, raw editor, backups |
| GOP | `routes_gop.py` | Video compliance analysis, specs and workflow management |
| SRT | `routes_srt.py` | SRT ingest, multi-destination fan-out, B&T source |
| id3as | `id3as_routes.py` | DC monitoring: channels, nodes, events, logs |
| RTS | `rts_routes.py` | PhenixRTS channel list, publisher count, fork history, viewing report |
| TXCore | `routes_txcore.py` | Channel provisioning and category management |
| Live Probe | `routes_live_probe.py` | Real-time IAT/MLR monitor for SRT streams |
| Rota | `routes_rota.py` | Team rota: members, roster, schedule, leave, notes, draft lock |
| WC2026 | `wc2026_routes.py` | WC2026 assignments, scores and team names |

---

## Configuration

All configuration lives in `.env` on the server. The file is git-ignored and is never served to the browser; `GET /so-proxy/config` exposes only the safe subset. Admins can edit it from the **Environment** tab of `so-toolbox-admin.html`; every write takes a `.env.bak-<timestamp>` backup first (last 15 kept, git-ignored and blocked by nginx). Keys read at start-up by `routes_txcore.py` need a proxy restart; the UI flags them.

```env
# Application
APP_TITLE=SP SO Web Toolbox

# Tool Registry: TOOL_n=file.html|Name|Description|icon|Category|BADGE
TOOL_1=RTS-Monitor.html|RTS Monitor|PhenixRTS channel monitoring|📡|Monitoring|LIVE
TOOL_2=id3as-DC-Monitor.html|id3as DC Monitor|Distributed encoding infrastructure|⛨|Monitoring|
TOOL_3=BTV-Video-Analyser.html|Video Analyser|Stream compliance & GOP analysis|🔬|Streaming|
TOOL_4=SRT-URI-Builder.html|SRT URI Builder|Build SRT connection strings|🔗|Streaming|
TOOL_5=srt_tool.html|SRT Ingest|Single & multi-destination ingestion|📤|Streaming|
TOOL_6=MTR-Trace.html|MTR Trace|Network path analysis|🌐|Network|
TOOL_7=TXCore-Manager.html|TXCore Manager|Channel provisioning|📺|Broadcast|

# Server Presets (SRT URI Builder)
SRT_SERVER_1=203.0.113.10|Ingest EU-West
SRT_SERVER_2=203.0.113.20|Ingest UK

# Local Presets (SRT Builder & Ingest Analyzer)
SRT_LOCAL_1=10.0.0.1|INX01
SRT_LOCAL_2=10.0.0.2|INX02

# Shared Credentials (server-side only, never sent to the browser)
SRT_PASSPHRASE=your-passphrase-here
PHENIXRTS_APP_ID=your-app-id
PHENIXRTS_PASSWORD=your-password
PRFAUTH=your-prfauth-token-here

# id3as DC Hosts
ID3AS_HOST_IX=id3as-ix.example.co.uk
ID3AS_HOST_EQ=id3as-eq.example.co.uk

# Admin Authentication
ADMIN_PASSWORD=your-admin-password

# Authentication Backend
AUTH_BACKEND=ad  # or 'local' for file-based users.json
AD_DOMAIN=example.com
AD_SERVER=ldap.example.com

# RTS Backend
RTS_API_URL=https://rts-api.example.com
```

---

## Proxy Endpoints

The full reference, with request/response examples, is in [`SO-Toolbox-API-Docs.html`](SO-Toolbox-API-Docs.html). The most used routes:

#### Server info & status

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/so-proxy/config` | Safe config from `.env` (tools, presets) |
| GET | `/so-proxy/server-info` | Local IPs, gateway, public IP |
| GET | `/so-proxy/server-stats` | Live CPU, memory and disk usage (5 s refresh) |
| GET | `/so-proxy/me` | Current user profile (role, team, rota status) |
| GET | `/so-proxy/proxy/activity` | Active background jobs grouped by tool |

#### .env manager (admin only)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/so-proxy/env` | Parsed `.env` in file order (secrets masked) with stats and per-key consumer/reload info |
| GET | `/so-proxy/env/keys/<key>/reveal` | Real value of one key (audited) |
| POST | `/so-proxy/env/keys` | Add a variable (`key`, `value`, optional `after`, `comment`) |
| PUT | `/so-proxy/env/keys/<key>` | Update a value |
| PUT | `/so-proxy/env/keys/<key>/rename` | Rename a key |
| DELETE | `/so-proxy/env/keys/<key>` | Delete a key (`?all=1` removes duplicates too) |
| GET / PUT | `/so-proxy/env/raw` | Read / replace the whole file (validated, mtime conflict check) |
| GET / POST | `/so-proxy/env/backups` | List backups / create one now |
| GET | `/so-proxy/env/backups/<name>/diff` | Unified diff backup → current (secrets masked) |
| POST | `/so-proxy/env/backups/<name>/restore` | Restore a backup (current file is backed up first) |
| DELETE | `/so-proxy/env/backups/<name>` | Delete a backup |

#### PhenixRTS

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/so-proxy/channels` | Channel list |
| GET | `/so-proxy/publishers/count/<id>` | Publisher count for a channel |
| POST | `/so-proxy/rts/viewing-report` | Viewing sessions for a channel and time window (CSV) |
| GET | `/so-proxy/rts/fork-history` | Fork events by date range |

#### id3as DC monitoring

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/so-proxy/id3as/config` | DC base URLs |
| GET | `/so-proxy/id3as/<dc>/channels/<variant>` | Channel list (`default` or `racing_uk`) |
| GET | `/so-proxy/id3as/<dc>/flags/channels` | Active channel warnings |
| GET | `/so-proxy/id3as/<dc>/running_events` | Running scheduled events |
| GET | `/so-proxy/id3as/<dc>/nodes` | Node list with status |
| GET | `/so-proxy/id3as/<dc>/logs[/<y>/<m>/<d>]` | System event log |

#### Video analysis (GOP)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/gop/run` | Start an analysis job (SRT, RTMP or file) |
| GET | `/gop/jobs/running` | In-progress jobs |
| GET | `/gop/results` | History with pagination and filtering |
| PATCH | `/gop/result/<file>/workflow` | Change workflow and re-evaluate |
| GET | `/gop/specs` | Compliance specs for a workflow |
| POST | `/gop/specs` | Save specs (admin/engineer) |
| POST | `/gop/workflows/default` | Set the API default workflow |

#### SRT ingest

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/srt/ingest/single` | Single-destination ingest |
| POST | `/srt/ingest/multi` | Multi-destination fan-out (independent processes) |
| POST | `/srt/ingest/multi-shared` | Shared single ffmpeg process |
| GET | `/srt/status/<job_id>` | Job status and bitrate stats |

#### MTR network trace

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/so-proxy/mtr/stream` | SSE stream for a live trace |
| GET | `/so-proxy/mtr/results` | Completed traces |
| POST | `/so-proxy/mtr/tag/<file>` | Tag a result |

#### Files & administration

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/upload` | Accept `.ts` uploads (requires `client_max_body_size 2G` in nginx) |
| GET | `/so-proxy/ingest/download/<file>` | Download an analysis ZIP |
| GET | `/so-proxy/sessions` | Active sessions (admin/engineer) |
| DELETE | `/so-proxy/sessions/<username>` | Terminate a user's sessions (admin) |
| POST | `/so-proxy/git-pull` | Update from git (admin/engineer) |
| POST | `/so-proxy/restart-proxy` | Restart the Flask proxy (admin/engineer) |

---

## Directory Structure

```
/opt/web/
├── index.html                  Main application shell
├── proxy.py                    Flask proxy (entry point)
├── so-proxy.service            systemd service unit
├── nginx.conf                  RHEL/CentOS config
├── nginx-debian.conf           Debian/Ubuntu config
├── .env                        Credentials & config (git-ignored)
├── users.json.template         User template for local auth
│
├── Tool frontends
│   ├── BTV-Video-Analyser.html
│   ├── Ingest-Analyzer.html
│   ├── id3as-DC-Monitor.html
│   ├── RTS-Monitor.html
│   ├── RTS-Test-Player.html
│   ├── SRT-URI-Builder.html
│   ├── srt_tool.html
│   ├── srt_push_monitor.html
│   ├── MTR-Trace.html
│   ├── TXCore-Manager.html
│   ├── ProbeMonitoring.html
│   ├── wc2026_rota_management.html
│   ├── jira-formatter.html
│   ├── so-toolbox-admin.html
│   ├── sp-extensions.html
│   └── SO-Toolbox-API-Docs.html
│
├── Backend routes (Flask Blueprints)
│   ├── routes_auth.py          Auth, users, roles, sessions
│   ├── routes_env.py           .env manager (admin only)
│   ├── routes_gop.py           Video analysis & compliance
│   ├── routes_srt.py           SRT ingest control
│   ├── id3as_routes.py         DC monitoring
│   ├── rts_routes.py           PhenixRTS
│   ├── routes_txcore.py        TXCore provisioning
│   ├── routes_live_probe.py    Real-time IAT/MLR monitor
│   ├── routes_rota.py          Team rota
│   └── wc2026_routes.py        WC2026 backend
│
├── Data & storage
│   ├── mtr-results/            Saved MTR traces (JSON)
│   ├── store/gop-results/      Video compliance results (JSON + .ts)
│   ├── store/ingest-results/   Analysis reports (ZIP + HTML)
│   ├── store/recordings/       Video Analyser recordings (.ts)
│   └── sessions.json           Persisted user sessions (mode 0600)
│
├── Scripts & services
│   ├── generate-report.sh      HTML/text reports (perl >= 5.36)
│   ├── cleanup.sh              Maintenance cleanup
│   ├── srt-push.py             SRT push service daemon
│   └── srt-push-config.example.json
│
└── Documentation
    ├── README.md               This file
    ├── SERVER_REBUILD.md       Setup & deployment guide
    ├── DEPLOY_id3as.md         id3as deployment notes
    ├── CHANGELOG.md            Version history (Keep a Changelog)
    └── LICENSE                 MIT License
```

---

## Quick Start

### Server

```bash
# See SERVER_REBUILD.md for the full procedure
git clone https://github.com/marcusmarcal/SO-Toolbox.git /opt/web/so-toolbox
cd /opt/web/so-toolbox
cp .env.template .env
# Edit .env with your credentials and presets
systemctl start so-proxy
```

After updating any served file (HTML, Python, Markdown) restart the proxy so the new version is picked up:

```bash
systemctl restart so-proxy.service
```

### Local development

```bash
python3 proxy.py
# Access at http://localhost:5050
# For nginx setup, see nginx-debian.conf
```

---

## Requirements

### Server

- Python 3.8+ with Flask, requests and python-ldap (or local auth)
- nginx
- `ffprobe`, `mediainfo`, `perl >= 5.36`, `gnuplot`, `jq`, `bc` — video and ingest analysis
- `srt-live-transmit` (Haivision srt-tools) — Live Probe
- `mtr` — MTR Network Trace

### Browser

- Modern browser (Chrome, Firefox, Safari, Edge) with JavaScript enabled
- `EventSource` (Server-Sent Events) support for live streams

---

## Security Notes

- `.env`, `users.json` and `sessions.json` are git-ignored and blocked by nginx; they are never served to browsers.
- Credentials (PRFAUTH, API keys, passwords) stay server-side; every third-party API is reached through the proxy.
- Role-based access control: admin, engineer, specialist, analyst, user. Sensitive actions (git pull, restart, specs editing, user management) require admin or engineer.
- Passwords are stored as bcrypt hashes; sessions are `HttpOnly` cookies with an 8-hour TTL and are persisted with mode 0600.
- Large uploads need `client_max_body_size 2G` in the nginx configuration.
- The SRT passphrase is delivered to the browser for the SRT tools — serve the Toolbox over HTTPS only.

---

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Jobs not running | `systemctl status so-proxy` and the proxy log; `.env` credentials and network access; dependent binaries (`ffprobe`, `mtr`, `srt-live-transmit`) installed |
| A file update is not visible (old content or 404) | The proxy keeps serving the previous file until restarted: `systemctl restart so-proxy.service` |
| Large file uploads fail | Increase `client_max_body_size` in the nginx config |
| id3as data not loading | `ID3AS_HOST_IX` / `ID3AS_HOST_EQ` in `.env`; PRFAUTH token and DC network access |
| Video analysis stuck | Incomplete `.ts` uploads; `ffprobe` / `mediainfo` availability; `/var/log/so-proxy.log` |
| Users logged out after a restart | Sessions are persisted in `sessions.json`; check the file exists and is writable by the service user |

---

## Versioning

Releases follow [Semantic Versioning](https://semver.org) and are documented in [`CHANGELOG.md`](CHANGELOG.md) using the [Keep a Changelog](https://keepachangelog.com) format. The current version is always the first release entry in `CHANGELOG.md`; `index.html` reads it at runtime for the version badge and the changelog viewer, so nothing is hard-coded.

---

## License

MIT License — see the `LICENSE` file for details.

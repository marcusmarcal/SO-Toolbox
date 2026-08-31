# SP SO Web Toolbox

A comprehensive browser-based operations toolbox for the Streaming & Broadcast Operations team. Built as a single-page application served via nginx, with tools loaded dynamically through a Flask proxy backend.

> **For server setup and rebuild instructions, see [`SERVER_REBUILD.md`](SERVER_REBUILD.md).**
> **For deployment guidance on id3as monitoring, see [`DEPLOY_id3as.md`](DEPLOY_id3as.md).**

---

## Overview

The SO Toolbox is a unified platform providing real-time monitoring, stream analysis, and operational management for broadcast and streaming infrastructure. The architecture separates the frontend (static HTML/JS) from the backend (Flask proxy), enabling secure credential management and API aggregation.

### Key Components

- **index.html** — Main application shell with navigation, tool registry, and real-time server monitoring
- **proxy.py** — Flask CORS proxy handling API requests, credentials, and backend integrations
- **nginx.conf** / **nginx-debian.conf** — Web server configuration (CentOS/RHEL and Debian/Ubuntu)
- **so-proxy.service** — systemd service unit for the Flask proxy
- **.env** — Tool registry, server presets, and credentials (git-ignored, server-side only)

---

## Tools

### 📊 Video & Stream Analysis

#### **BTV Video Analyser** (`BTV-Video-Analyser.html`)
Professional video stream analysis for SRT and uploaded files.
- **Capabilities**: GOP analysis, frame type distribution, codec/profile detection, bitrate/resolution analysis
- **Compliance**: Configurable specs for IDR presence, GOP structure, B-frames, audio/video sync
- **Reports**: Visual compliance dashboard, text/Jira export, mediainfo report viewer
- **Tech**: `ffprobe` / `mediainfo` backend, batch result storage with filtering/pagination

#### **Ingest Analyser** (`Ingest-Analyzer.html`)
Stream quality validation for ingest sources (SRT, RTMP, UDP, file upload).
- **Capabilities**: Runs detailed analysis via `run-ingest-analysis.sh` (~2 min), generates reports with charts
- **Backend**: Streaming job monitor, background task support, ZIP + HTML report download
- **Tech**: Requires `ffprobe`, `perl >= 5.36`, `gnuplot`, `jq`, `bc`

#### **Live Probe** (integrated in BTV)
Real-time network telemetry for SRT sources.
- **Metrics**: PCR interval (IAT), TS continuity-counter loss (MLR), bitrate
- **Tech**: `srt-live-transmit` backend (Haivision SRT tools), no external probe dependency
- **Display**: Live area chart with avg/max readouts, configurable alarm thresholds

### 📡 Real-Time Monitoring

#### **RTS Monitor** (`RTS-Monitor.html`)
PhenixRTS channel and viewing statistics dashboard.
- **Tabs**:
  - **Channels** — Live channel table with publisher status, alias, channel ID, stream key, forked-from tracking
  - **Viewing Report** — Query session data by Event ID and time window (UTC), paginated results (100 rows/batch)
  - **Fork Origin** — Track fork events by date range; query source/destination channels
- **Features**: Search (name, alias, channel ID, stream key, status), supplier filter (RMG HA/EBC), export to Excel
- **Tech**: PhenixRTS API via proxy, no credentials in browser

#### **id3as DC Monitor** (`id3as-DC-Monitor.html`)
Distributed encoding infrastructure monitoring across multiple Data Centers.
- **Views**:
  - **Channels** — Live channel state (encoding/source status), search & filter
  - **Nodes** — Node list with health and alarm status, grace-period event-starting detection
  - **Events** — Running scheduled events with channel flag warnings
  - **Logs** — System event log (by date, today UTC by default)
- **Features**: Real-time flag warnings (per channel or event), event-starting grace period (suppress alarms 3 min), drill-down to channel/node status
- **Tech**: id3as API via proxy using PRFAUTH token, DC hostnames from `.env`

#### **Probe Monitoring** (`ProbeMonitoring.html`)
Control-room style monitoring for distributed probe channels.
- **Features**: Two independent channel slots, 40 configurable Id3as AWS + Probe URL pairs, fixed RMG MV reference feeds
- **Tech**: localStorage persistence, dark control-room UI with teal/amber accents

### 🚀 SRT & Ingest Control

#### **SRT URI Builder** (`SRT-URI-Builder.html`)
Form-based SRT connection URI generator.
- **Features**: Mode, passphrase, latency, pbkeylen, advanced SRT options; server/local presets from `.env`
- **Output**: Copy-ready SRT URIs for stream configuration

#### **SRT Tool** (`srt_tool.html`)
Advanced SRT stream ingestion and management.
- **Capabilities**: Single & multi-destination ingest, shared ffmpeg mode (passthrough to many targets), file-based or B&T (colour bars + 1kHz tone) source
- **Features**: Auto-retry on failure, per-job restart, error tracking, bitrate monitor
- **B&T Mode**: Burns live UTC clock overlay (HH:MM:SS.mmm) for latency measurement

#### **SRT Push Monitor** (`srt_push_monitor.html`)
Manages concurrent SRT push services (static image or HTML page capture).
- **Features**: Per-service configuration, preview/log viewing, enable toggle, source type switching
- **Tech**: Multi-service support via `srt-push.py` daemon, per-service systemd integration

### 📺 Broadcast Infrastructure

#### **RTS Player** (`RTS-Test-Player.html`)
Generate and launch RTS player URLs with automatic viewer token injection.

#### **TXCore Manager** (`TXCore-Manager.html`)
TXCore channel provisioning tool for AVE/LMK/YER sites.
- **Features**: Category creation, bulk channel form, request preview, async job monitoring with live logs
- **Config**: Site-specific IP prefixes, auto-fill from First CH#, live multicast address preview

#### **RTS BC ConfigTool** (`RTS-BC-ConfigTool.html`)
Broadcast configuration management for RTS services.

#### **RTS Stats Channel Publisher** (`RTS-StatsChannelPublisher.html`)
Real-time stats publishing for RTS channels.

### ⚙️ Admin & Utilities

#### **Jira Formatter** (`jira-formatter.html`)
Transform ServiceNow onboarding data into clean Jira ticket format (copy-ready with rich text).

#### **RMG Purge URL Generator** (`purge-url-generator.html`)
Build cache purge URLs from Event IDs and month/year.

#### **Chrome Extensions** (`sp-extensions.html`)
Browser extensions for operational workflows:
- **RITM Ticket Formatter** — Convert ServiceNow RITM pages to Jira tickets
- **TXEdge VLC Launcher** — Auto-detect & launch SRT streams in VLC (passphrase stored securely)
- **SO Video Analyser** — Trigger video analysis on TXEdge/TXCore pages, results inline

#### **Users Admin** (`users-admin.html`)
User management with role-based access control.
- **Roles**: Admin, Engineer, Specialist, Analyst, User
- **Fields**: rota_status (active/inactive/observer), team (SOE/SOS/NA), display_name, employee_id

#### **WC2026 Rota Management** (`wc2026_rota_management.html`)
World Cup 2026 duty scheduling and rotation management.
- **Integration**: openfootball sync for match schedules, team tracking, kickoff times
- **Features**: Four engineer slots (auto-assign, bulk edit, score tracking), CSV export, filter by date/team

#### **MTR Network Trace** (`MTR-Trace.html`)
Server-side network path tracing with streaming results.
- **Features**: Packet count or time duration mode, background daemon threads, tagged result storage, browsable history

---

## API & Configuration

### Flask Blueprints (Backend Routes)

| Blueprint | File | Purpose |
|-----------|------|---------|
| **auth** | `routes_auth.py` | User authentication, role validation, session management |
| **GOP** | `routes_gop.py` | Video compliance analysis, specs management, workflow control |
| **SRT** | `routes_srt.py` | SRT ingest, multi-destination fan-out, B&T source control |
| **id3as** | `id3as_routes.py` | DC monitoring, channel/node/event/log queries |
| **RTS** | `rts_routes.py` | PhenixRTS channel list, publisher count, fork history |
| **TXCore** | `routes_txcore.py` | Channel provisioning, category management |
| **Live Probe** | `routes_live_probe.py` | Real-time IAT/MLR monitor for SRT streams |
| **Rota** | `routes_rota.py` | WC2026 schedule, assignments, team management |

### Environment Configuration (`.env`)

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

# Shared Credentials (Server-side only, never sent to browser)
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

### Proxy Endpoints

**Server Info & Status**
- `GET /so-proxy/config` — Safe config from `.env` (tools, presets, passphrase)
- `GET /so-proxy/server-info` — Local IPs, gateway, public IP
- `GET /so-proxy/server-stats` — Live CPU, memory, disk usage (refreshed every 5s)
- `GET /so-proxy/me` — Current user profile (role, team, rota_status)

**PhenixRTS**
- `GET /so-proxy/channels` — Channel list
- `GET /so-proxy/publishers/count/<id>` — Publisher count for channel
- `GET /so-proxy/rts/fork-history` — Fork events by date range

**id3as DC Monitoring**
- `GET /so-proxy/id3as/config` — DC base URLs
- `GET /so-proxy/id3as/<dc>/channels/<variant>` — Channel list (default | racing_uk)
- `GET /so-proxy/id3as/<dc>/flags/channels` — Active channel warnings
- `GET /so-proxy/id3as/<dc>/running_events` — Running scheduled events
- `GET /so-proxy/id3as/<dc>/nodes` — Node list with status
- `GET /so-proxy/id3as/<dc>/logs[/<y>/<m>/<d>]` — System event log

**GOP Video Analysis**
- `POST /gop/run` — Start analysis job (SRT or file)
- `GET /gop/jobs/running` — In-progress jobs
- `GET /gop/results` — History with pagination/filtering
- `PATCH /gop/result/<file>/workflow` — Change workflow, re-evaluate
- `GET /gop/specs` — Compliance specs for workflow
- `POST /gop/specs` — Save/update specs (admin/engineer)
- `POST /gop/workflows/default` — Set API default workflow

**SRT Ingest**
- `POST /srt/ingest/single` — Single-destination ingest
- `POST /srt/ingest/multi` — Multi-destination fan-out
- `POST /srt/ingest/multi-shared` — Shared ffmpeg mode
- `GET /srt/status/<job_id>` — Job status & bitrate stats

**MTR Network Trace**
- `GET /so-proxy/mtr/stream` — SSE stream for live trace
- `GET /so-proxy/mtr/results` — Completed traces
- `POST /so-proxy/mtr/tag/<file>` — Tag result

**File Upload & Download**
- `POST /upload` — Accept .ts file uploads (requires `client_max_body_size 2G` in nginx)
- `GET /so-proxy/ingest/download/<file>` — Download analysis ZIP

**Administration**
- `POST /so-proxy/git-pull` — Update from git (admin/engineer)
- `POST /so-proxy/restart-proxy` — Restart Flask proxy (admin/engineer)

---

## Directory Structure

```
.
├── index.html                          # Main application shell
├── proxy.py                            # Flask proxy (main entry point)
├── so-proxy.service                    # systemd service unit
├── nginx.conf                          # RHEL/CentOS config
├── nginx-debian.conf                   # Debian/Ubuntu config
├── .env                                # Credentials & config (git-ignored)
├── users.json.template                 # User template for local auth
│
├── Tools (HTML Frontends)
├── BTV-Video-Analyser.html
├── Ingest-Analyzer.html
├── id3as-DC-Monitor.html
├── RTS-Monitor.html
├── RTS-Player.html
├── SRT-URI-Builder.html
├── srt_tool.html
├── srt_push_monitor.html
├── MTR-Trace.html
├── TXCore-Manager.html
├── ProbeMonitoring.html
├── WC2026.html
├── wc2026_rota_management.html
├── jira-formatter.html
├── users-admin.html
├── sp-extensions.html
└── SO-Toolbox-API-Docs.html
│
├── Backend Routes (Flask Blueprints)
├── routes_auth.py                      # Auth, users, roles
├── routes_gop.py                       # Video analysis & compliance
├── routes_srt.py                       # SRT ingest control
├── id3as_routes.py                     # DC monitoring
├── rts_routes.py                       # PhenixRTS
├── routes_txcore.py                    # TXCore provisioning
├── routes_live_probe.py                # Real-time IAT/MLR monitor
├── routes_rota.py                      # WC2026 scheduling
└── wc2026_routes.py                    # WC2026 backend
│
├── Data & Storage
├── mtr-results/                        # Saved MTR traces (JSON)
├── ingest-results/                     # Analysis reports (ZIP + HTML)
├── gop-results/                        # Video compliance results (JSON)
└── rota/                               # WC2026 schedule data
│
├── Build & Helper Scripts
├── generate-report.sh                  # Generate HTML/text reports (perl >= 5.36)
├── cleanup.sh                          # Maintenance cleanup
├── srt-push.py                         # SRT push service daemon
├── srt-push-config.example.json        # SRT push config template
│
├── Configuration
├── SERVER_REBUILD.md                   # Setup & deployment guide
├── DEPLOY_id3as.md                     # id3as deployment notes
├── CHANGELOG.md                        # Version history (Keep a Changelog)
├── README.md                           # This file
└── LICENSE                             # MIT License
```

---

## Version History

See [CHANGELOG.md](CHANGELOG.md) for full version history in [Keep a Changelog](https://keepachangelog.com) format.

**Current version** is always the first entry in `CHANGELOG.md`. The `index.html` reads the changelog at runtime to display the version badge and modal — no hardcoding required.

---

## Quick Start

### Server Setup

```bash
# See SERVER_REBUILD.md for full instructions
git clone https://github.com/marcusmarcal/SO-Toolbox.git /opt/web/so-toolbox
cd /opt/web/so-toolbox
cp .env.template .env
# Edit .env with your credentials and presets
systemctl start so-proxy
```

### Local Development

```bash
python3 proxy.py
# Access at http://localhost:5050
# For nginx setup, see nginx-debian.conf
```

---

## Requirements

### Backend
- Python 3.8+
- Flask, requests, python-ldap (or local auth)
- ffprobe, mediainfo, perl >= 5.36, gnuplot, jq, bc (for analysis)
- srt-live-transmit (for Live Probe)
- nginx (web server)

### Browser
- Modern browser (Chrome, Firefox, Safari, Edge)
- JavaScript enabled
- WebSocket support (for SSE streams)

---

## Security Notes

- ✅ `.env` is git-ignored and never served directly to browsers
- ✅ Credentials (PRFAUTH, API keys, passwords) are server-side only
- ✅ Role-based access control (admin, engineer, specialist, analyst, user)
- ✅ CORS proxy prevents cross-origin API access from untrusted sources
- ✅ Large file uploads require `client_max_body_size 2G` in nginx config
- ⚠️ SRT passphrase is exposed to browser (consider HTTPS only)

---

## Troubleshooting

**Jobs not running:**
- Check `systemctl status so-proxy` and proxy logs
- Verify `.env` credentials and network access
- Ensure dependent binaries (ffprobe, mtr, srt-live-transmit) are installed

**Large file uploads fail:**
- Increase `client_max_body_size` in nginx config

**id3as data not loading:**
- Verify `ID3AS_HOST_IX` and `ID3AS_HOST_EQ` in `.env`
- Check PRFAUTH token and DC network access

**Video analysis stuck:**
- Check for incomplete .ts file uploads
- Verify ffprobe/mediainfo availability
- Review `/var/log/so-proxy.log`

---

## License

MIT License - See LICENSE file for details

---

## Support

For issues, feature requests, or deployment questions, refer to [SERVER_REBUILD.md](SERVER_REBUILD.md) or check the [API documentation](SO-Toolbox-API-Docs.html).

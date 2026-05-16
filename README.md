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

### ⛨ id3as DC Monitor (`id3as-DC-Monitor.html`)
Web frontend for monitoring id3as data-centre infrastructure. Communicates with the id3as API via the SO proxy using `PRFAUTH` from `.env` — credentials never reach the browser. Supports two DCs (IX and EQ) and four views: **Channels** (all default channels with job state, warnings, and active events), **Nodes** (per-node channel allocation grid with colour-coded chips), **RMG** (Racing UK channels with per-channel event list), and **Logs** (system event log with level filter, date picker, and grep). Auto-refreshes every 30 seconds. Filter and warnings-only toggle apply across all views.

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
| `/so-proxy/id3as/<dc>/channels/<variant>` | GET | id3as channel list (default \| racing_uk) |
| `/so-proxy/id3as/<dc>/flags/channels` | GET | Active warnings per channel |
| `/so-proxy/id3as/<dc>/running_events` | GET | Currently running events |
| `/so-proxy/id3as/<dc>/nodes` | GET | Node list with status |
| `/so-proxy/id3as/<dc>/logs` | GET | System event log (today UTC) |
| `/so-proxy/id3as/<dc>/logs/<y>/<m>/<d>` | GET | System event log for specific date |
| `/so-proxy/id3as/<dc>/channel/<id>/status` | GET | Single channel enc/src state |
| `/so-proxy/server-stats` | GET | Live CPU, memory and disk usage (refreshed every 5 s in topbar) |

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

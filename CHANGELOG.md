# Changelog

All notable changes to SP SO Web Toolbox are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.14.5] - 2026-05-12

### Added
- id3as DC Monitor: URL parameter state sync for kiosk use — `?view=`, `?dc=`, `?inuse=`, `?warn=`, `?sort=`, `?dir=` are read on load and written on every state change via `history.replaceState`; allows bookmarking any view/filter/sort combination
- Node cards with active warnings now pulse with a gentle amber glow (`warn-animated`) for kiosk visibility

### Fixed
- Merged manual URL-param changes back into main codebase (previously lost in a commit)
- All comments translated to English

## [2.14.4] - 2026-05-11

### Fixed
- id3as DC Monitor: logs load crash — form field values (node, channel, grep, level, dates) now captured before `#content` is replaced by the loading spinner
- Changelog extracted to `CHANGELOG.md` (Keep a Changelog format); `index.html` reads and renders it dynamically; `APP_VERSION` now sourced from `CHANGELOG.md` instead of `.env`

## [2.14.3] - 2026-05-11

### Fixed
- id3as DC Monitor: scheduled channel join now uses `input_address` field on channel objects (not `primary_source_specifier`) for correct multicast IP → channel ID resolution

## [2.14.2] - 2026-05-10

### Fixed
- id3as DC Monitor: logs search form with dropdowns (node/channel/level/date range/grep); no auto-fetch on view open
- Logs: suggestion dropdowns populated via parallel API fetch on first open, cached per DC
- Logs: level + grep bar shown after load with "← New search" button to return to form
- Auto-refresh skips logs view; DC change resets suggestion cache

## [2.14.1] - 2026-05-10

### Fixed
- id3as DC Monitor: RMG channels now included in `fetchStatuses` batch (bitrate display working)
- Scheduled: channel resolved from `primary_source_specifier` → `input_address` join with channels list
- Logs: `<datalist>` elements moved to body level (fix browser resolution inside `display:none` parent)
- Logs: view button no longer triggers auto-load; auto-refresh skips logs

## [2.14.0] - 2026-05-10

### Added
- id3as DC Monitor: Logs date-range picker (from/to, default today, max 14 days, parallel fetch per day)
- Logs: empty state shown before fetch; content only shown after explicit Fetch button
- Logs: node/channel/event ID suggestion datalists populated on fetch

### Fixed
- Scheduled: channel field extracted from root and nested `scheduled_event`; horizon buttons functional

## [2.13.0] - 2026-05-08

### Added
- id3as DC Monitor: soft refresh — no blank screen on auto-refresh (overlay-only)
- Sort by dropdown on Nodes (ID/CPU/MEM/SCH/Warnings/Events/Jobs) and Events (Channel/Start/End), persisted in `localStorage`
- Logs sub-filters: node, channel, event id
- Scheduled: horizon filter buttons (3d default / 7d / 14d / all)
- External links to id3as web GUI on node IDs, channel IDs (`ruk_channels` for `RMG_*`), and event IDs; domain switches IX/EQ

### Changed
- RMG view removed; Channels now fetches `default` + `racing_uk` with RMG-only toggle
- "In use only" filter now uses events-only criterion (jobs running no longer counts)

### Removed
- Events view: packaging info placeholder removed

## [2.12.0] - 2026-05-08

### Added
- id3as DC Monitor: SCH (scheduler) resource bar with thresholds 30/70 warn/crit
- Event/no-signal warnings on channel chips (orange), node event list, and Events view
- index: collapsible sidebar categories with colour-coded badges per type (LIVE/RTS/SRT/MTR/JIRA/ID3AS/RMG/TOOL)

### Fixed
- id3as DC Monitor Nodes: now fetches both `channels/default` + `channels/racing_uk` — 86 nodes displayed (was 42)
- Nodes view merged with Health view into single Nodes view

### Changed
- "Active only" filter renamed to "In use only"
- index: version now reads from `APP_VERSION` in `.env` via `/so-proxy/config`

## [2.11.0] - 2026-05-05

### Added
- New tool: id3as DC Monitor — web frontend for id3as channel/node/RMG/logs monitoring
- Proxy routes `/id3as/<dc>/*` handle `PRFAUTH` server-side (credentials never reach browser)
- DC toggle (IX/EQ), four views (Channels, Nodes, RMG, Logs), warnings filter, 30s auto-refresh
- Logs: grep and date picker

## [2.10.2] - 2026-05-05

### Changed
- Ingest Analyzer: File `.ts` tab replaced server-side path input with browser file upload
- New `/ingest/upload` proxy endpoint accepts multipart `.ts`, saves to temp, runs script, cleans up

## [2.10.1] - 2026-05-04

### Fixed
- SO Video Analyser: misplaced `</div>` in history panel
- `.ts` upload: Flask 2 GB `MAX_CONTENT_LENGTH` + nginx `client_max_body_size` note in README
- AV sync offset: use `pkt_dts_time` fallback when `pts_time` is N/A in MPEG-TS

## [2.10.0] - 2026-05-04

### Added
- SO Video Analyser: AV sync + jitter checks in GOP analyser
- `.ts` upload endpoint
- Clear form/history buttons; checkbox persistence across refresh
- Schedule UTC+30min, European time format, bigger report modal
- GOP in compliance report; AV sync in compliance table

## [2.9.2] - 2026-04-24

### Fixed
- Compliance fully specs-driven: deep-merge `_load_specs`/`_save_specs`; `saveSpecs` JS preserves all fields; specs editor handles number preferred

## [2.9.0] - 2026-04-24

### Added
- Unique `test_id` per run (searchable)
- Dual report: visual screenshot + text copy for ServiceNow/Jira
- Print CSS for reports

## [2.8.4] - 2026-04-23

### Fixed
- Re-run saves new JSON and appears in history; `pollStatus` sets `_currentResultFile`; `loadHistory` called on all outcomes

## [2.8.3] - 2026-04-23

### Added
- SO Video Analyser: Re-run button on loaded result and each history item

## [2.8.0] - 2026-04-23

### Added
- Schedule fix; fps history badge (`v_fps_compliance`); specs editor (JSON, password-protected)
- GOP Stats left / Audio right layout; scheduled badge

## [2.7.0] - 2026-04-22

### Added
- SO Video Analyser rename; 50i FPS fix; override→JSON; `.ts` download
- Scheduled jobs; metadata header
- index: UTC + local clocks in topbar

## [2.6.3] - 2026-04-21

### Fixed
- GOP: `v_scan` reference before assignment; 50i→25fps; HLG SDR; AAC-LC; audio tracks via `-show_programs`
- Generate Report; Override support

## [2.6.0] - 2026-04-16

### Added
- GOP: compliance RAG table, graceful timeout, NAL/IDR detection, open/closed GOP, FPS flag
- MTR: hops fix, bulk delete

## [2.5.0] - 2026-04-14

### Added
- New tool: GOP Analyzer — SRT stream capture, IDR/GOP visualizer (I/P/B/S frames), full stream info, compliance checks

## [2.4.0] - 2026-04-14

### Added
- MTR: ICMP/UDP-53 toggle, Country/ASN geo, `-b` flag

## [2.3.0] - 2026-04-13

### Changed
- Proxy renamed to `so-proxy`; `ADMIN_PASSWORD` for sensitive actions
- MTR: host sanity check; README modal in index

## [2.2.0] - 2026-04-10

### Added
- MTR: kill/delete with confirmation, date picker filter, history/running panels

## [2.1.0] - 2026-04-10

### Added
- MTR: wider history panel, date/search/tag filters, multi-tag, Date End + HH:MM:SS, Time mode

## [2.0.0] - 2026-04-07

### Added
- MTR background jobs with disk persistence, tag filtering, running jobs panel
- Ingest Analyzer: tags support, bufsize fix, progress bar with countdown

## [1.9.0] - 2026-04-06

### Added
- Ingest Analyzer: background jobs, ZIP copy, HTML report served via proxy, `SRT_LOCAL` presets

## [1.8.0] - 2026-04-06

### Fixed
- MTR time mode: use cycles instead of timeout; SSE tick countdown; progress bar with colour shift

## [1.7.0] - 2026-04-01

### Added
- New tool: MTR Network Trace with SSE streaming, server info, history

## [1.6.0] - 2026-04-01

### Added
- RMG RTS Multiview Dashboard launcher
- Ingest Analyzer tool (initial)

## [1.5.2] - 2026-04-01

### Fixed
- nginx `proxy_buffering off` for SSE; per-distro nginx configs (CentOS/RHEL + Debian/Ubuntu)

## [1.5.1] - 2026-03-31

### Changed
- Config loaded via proxy instead of public `.env`; `SRT_PASSPHRASE` secured

## [1.5.0] - 2026-03-27

### Added
- Git Pull button in index with auto-reload; proxy as systemd service

## [1.4.0] - 2026-03-27

### Changed
- Lazy redraw — only update screen on state changes

## [1.3.0] - 2026-03-27

### Changed
- Channel Monitor UI switched from rich to curses

## [1.0.0] - 2026-03-25

### Added
- Initial release — PhenixRTS Channel Health Monitor, SRT URI Builder

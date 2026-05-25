# Changelog

All notable changes to SP SO Web Toolbox are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.27.0] - 2026-05-25

### Added

- `routes_gop.py` — all `/gop/*` routes extracted from `proxy.py` into an independent blueprint, following the pattern of `routes_auth.py`.
- Mandatory `username` field in all GOP test results (JSON saved to disk and `/gop/results` endpoint). The value is the active session’s `username` (e.g., `marcus.marcal@statsperform.com`); if there is no valid session, `"anonymous"` is recorded.

### Changed

- `proxy.py` imports and registers `routes_gop` via `routes_gop.register_routes(app)`.
- GOP Analyzer and Specs Editor section removed from `proxy.py`.

## [2.26.0] - 2026-05-21

### Added

- RTS Channel Publisher (`RTS-StatsChannelPublisher.html`) to load StatsChannelPublisher with given publishing token.

## [2.25.0] - 2026-05-21

### Changed

- Password hashing upgraded from SHA-256 to bcrypt (cost factor 12); existing
  hashes from legacy systems (`$2a$`, `$2b$`, `$2y$`) are accepted without migration

### Added

- SQL export query (`import_users.sql`) to extract users and bcrypt hashes
  from a legacy MariaDB database
- Python import script to convert the SQL export into `users.json` format,
  preserving the existing `admin` entry

## [2.24.0] - 2026-05-20

### Added

- **Authentication system** — login wall protecting the main application; session tokens are issued on successful login and stored as `HttpOnly` cookies (TTL 8 h); fallback via `sessionStorage` for environments that strip cookies
- **`login.html`** — standalone login page matching the SO-Toolbox visual identity; animated hex logo, grid background, shake-on-error UX, `?next=` redirect support after successful sign-in
- **`users-admin.html`** — new tool for managing the local user database; full CRUD (create, edit, delete) gated behind `ADMIN_PASSWORD`; role assignment (`user` / `admin`); password change without revealing current hash; toast notifications and confirm-before-delete modal
- **`proxy.py` — auth routes**:
  - `POST /so-proxy/login` — validates credentials against `users.json`, creates in-memory session, sets `sotb-session` cookie
  - `POST /so-proxy/logout` — invalidates session and clears cookie
  - `GET  /so-proxy/me` — returns current session username and role
  - `GET  /so-proxy/users` — lists all users (admin-only, via `X-Admin-Password`)
  - `POST /so-proxy/users` — creates a user (admin-only)
  - `PUT  /so-proxy/users/<username>` — updates role and/or password (admin-only)
  - `DELETE /so-proxy/users/<username>` — removes user and invalidates their active sessions (admin-only)
- **`users.json`** — local user database file (SHA-256 hashed passwords); excluded from Git via `.gitignore`; `users.json.template` committed as reference
- **`@require_auth` / `@require_admin` decorators** in `proxy.py` for protecting existing and future routes
- **Brute-force delay** — 400 ms constant-time penalty on failed login attempts

### Changed

- `SERVER_REBUILD.md` — added Section 10 documenting the auth setup, first-run user seeding, and `.gitignore` entries

### Security

- `users.json` written with mode `0o600`; never served by nginx (blocked by existing `.env` rule pattern — extend to include `users.json`)
- Password hashes use `hmac.compare_digest` for constant-time comparison
- Admin endpoints authenticated via `ADMIN_PASSWORD` from `.env` (never exposed to the browser)
- Sessions invalidated immediately on user deletion

## [2.23.2] - 2026-05-20

### Changed

- Visual redesign to align with SO-Toolbox index colour scheme: replaced IBM Plex fonts with **Space Mono** + **Syne**, adopted `#1a1a1a` dark surface palette, purple accent (`#a18bf5`), and matching green/orange/red status colours.
- Added animated hexagonal logo mark and noise texture overlay to match index aesthetic.
- Login card gradient and box-shadow updated to use the new accent colour.
- Login title now uses gradient text (white → purple).
- Server resource pills restyled with rounded pill shape and updated colour tokens.

## [2.23.1] - 2026-05-20

### Fixed

- Timestamps in the Viewing Report table (`StartTimestamp`, `EndTimestamp`) now always display in UTC (`YYYY-MM-DD HH:MM:SS UTC`) instead of the browser's local timezone.

## [2.23.0] - 2026-05-20

### Changed

- `POST /rts/viewing-report` timeout increased from 30s to 120s to accommodate large CSV responses from Phenix.
- Viewing Report table now paginates in batches of 100 rows — initial load renders the first 100 sessions, with a **Load more** button showing progress (`loaded / total — N remaining`) to avoid blocking the browser on large reports.

## [2.22.2] - 2026-05-20

### Fixed

- Channels with `acquiring_signal` + active event (no signal) now correctly appear
  in Warnings Only — `srcWarn` is included in `r.warnings` count, and `warnOnly`
  filter simplified to `r.warnings > 0`.
- `bfmEv` helper restored (was lost on manual edits); `evFm`, `evWm`, and `srcWarn`
  re-applied to `renderChannels` and `renderNodes`.
- `renderRunning` uses `bfmEv(flagsEvData)` keyed by event id with `fm[id]` lookup.
- `nW` in Nodes no longer double-counts `encWarn` (already included in `c.warnings`).

## [2.22.1] - 2026-05-20

### Changed

- `warnOnly` filter in Channels now triggers on any encoder/source state that is not
  healthy: `encWarn` (encoder not `running`), `srcWarn` (source not `streaming`;
  `acquiring_signal` only counts as warning when there is an active event, i.e. "no
  signal"), and `evWm` (flags/events warnings). All three contribute to `r.warnings`
  so the counter in the summary bar reflects them correctly.
- Nodes view follows the same logic: `chSrcWarn` and `encWarn` per channel are now
  included in `nW`, driving the node-level `warnOnly` filter.

## [2.22.0] - 2026-05-19

### Added

- New route `POST /rts/viewing-report` in `rts_routes.py` — proxies the Phenix `PUT /pcast/reporting/viewing` endpoint with `kind: RealTime`, accepting `{ channel_alias, start, end }` in the request body and returning the raw CSV response.
- **Viewing Report tab** in `monitor.html`: form with Event ID (channel alias), Start and End datetime inputs (treated as UTC), status bar with session count and period, scrollable table showing key CSV columns (ChannelAlias, timestamps, ViewedMinutesDuringPeriod, ViewedMinutesTotal, TotalBytes in MB, Region, Country, City, RemoteAddress, UserAgent, Tags), and a download button for the raw CSV.
- Tab bar in the dashboard to switch between **Channels** and **Viewing Report** views.

## [2.21.0] - 2026-05-14

### Fixed

- Flags/events banner and per-event warn-strips were lost after manual code edits;
  restored `bfmEv`, `evFm`, `evWm` across `renderChannels`, `renderNodes`, and
  `renderRunning`.
- `renderChannels` early `return` was again missing `renderFlagsBanner()` call.
- `renderRunning` was looking up flags by `channel_id` instead of event `id`;
  corrected to `fm[id]` with `fm[ch]` fallback using `bfmEv(flagsEvData)`.
- Flags/events warnings now correctly counted in `r.warnings` / `nW` so
  "Warnings only" filter works for both channels and nodes views.

## [2.20.0] - 2026-05-19

### Changed

- Extracted RTS/Phenix routes (`/channels`, `/publishers/count/<channel_id>`) from `proxy.py` into a dedicated `rts_routes.py` Blueprint, registered via `app.register_blueprint(rts_bp)`.
- The existing `requests.Session` is shared with the new Blueprint (`rts_bp.session = session`) to preserve connection-pool reuse.

## [2.19.1] - 2026-05-18

### Added

- `id3as_routes.py`: new `/id3as/config` endpoint on the Blueprint — returns DC GUI base URLs
  built from `ID3AS_HOST_IX` / `ID3AS_HOST_EQ` in `.env`; used by the browser to build
  external deep-links without any hostname hardcoded in source files
- `DEPLOY_id3as.md`: updated deployment instructions to reflect Blueprint architecture;
  added `ID3AS_HOST_IX` / `ID3AS_HOST_EQ` to required `.env` entries

### Changed

- **Channels view**: rows replaced by cards matching the Running Events visual style —
  bordered blocks with channel ID, node, enc/src/bitrate/stream meta row, and events/warnings
  inline below; in-place status cell updates preserved (`enc-X`, `src-X`, `bps-X`, `str-X`)
- **Scheduled view**: horizon selector (3d / 7d / 14d / All) now correctly appears in the
  sub-toolbar — `display:''` fixed to `display:'block'` so the CSS default no longer wins
- `id3as-DC-Monitor.html`: `DC_URLS` no longer hardcoded — fetched at startup via
  `await fetch('/so-proxy/id3as/config')` before first render; no hostnames in source
- `README.md`: added `/so-proxy/id3as/config` to proxy endpoint table; updated id3as DC
  Monitor description; added `ID3AS_HOST_IX` / `ID3AS_HOST_EQ` to `.env` format section
- `SERVER_REBUILD.md`: added `PRFAUTH`, `ID3AS_HOST_IX`, `ID3AS_HOST_EQ` to `.env` template

### Security

- Removed `proxy_id3as_patch.py` — all id3as routes consolidated into `id3as_routes.py`
  (Flask Blueprint), the correct integration point via `app.register_blueprint(id3as_bp)`
- DC hostnames moved out of all source files; stored exclusively in `.env` and never
  committed to Git; Git history rewritten with `git filter-repo` to remove prior occurrences

## [2.19.0] - 2026-05-17

### Changed

- Security: hiding Id3as URLs

## [2.18.0] - 2026-05-15

### Added

- Real-time server resources monitor in topbar displaying CPU, memory, and disk usage
- Color-coded status indicators (green/ok, yellow/warning, red/error) based on configurable thresholds
- `/server-stats` endpoint in proxy.py for fetching system metrics (top, free, df commands)
- Auto-refresh of resource stats every 5 seconds

### Changed

- Topbar layout expanded to include server metrics display on the right side
- Resource thresholds: CPU (warn: 70%, error: 85%), Memory (warn: 75%, error: 90%), Disk (warn: 80%, error: 90%)

## [2.18.0] - 2026-05-15

### Fixed

- Removed limit of JSONs on GOP lists

## [2.17.5] - 2026-05-15

### Fixed

- Channel Monitor chart: X axis labels now show real UTC timestamps (HH:MM:SS)
  derived from the actual sample timestamps in `bitrateHistory`, updating live
  on every poll instead of static relative offsets

## [2.17.4] - 2026-05-15

### Changed

- Channel Monitor bitrate chart: Y grid lines with Mbps scale (4 divisions,
  rounded to clean values), X grid lines with time offset labels (-Ns to now),
  area fill, live value label with dot at last point

## [2.17.3] - 2026-05-15

### Fixed

- Channels: encoder state other than `running` now counted as warning —
  `warnOnly` filter surfaces channels with e.g. `initializing`, `stopped`, etc.
- Nodes: channels with non-running enc state now contribute to node warning
  count (`nW`) and appear with amber chip; node visible in `warnOnly` filter

## [2.17.2] - 2026-05-15

### Fixed

- Channel Monitor: events not shown — `renderChannelMonitorEvents` now reads
  `raw.events` via `bev()` instead of non-existent `chData.events`
- Channel Monitor: warnings not shown — `renderChannelMonitorWarnings` now
  merges `raw.flags` + `flagsEvData` directly instead of relying on
  `chMonState.flagsData` which was always empty

## [2.17.1] - 2026-05-14

### Fixed

- Channel Monitor modal now triggered via dedicated ⧉ button beside channel ID,
  preserving the external link click to id3as GUI; previously `onclick` on the
  row captured all clicks including on the `<a>` ext-lnk.

## [2.17.0] - 2026-05-14

### Added

- **Channel Monitor modal** — click any channel in Channels view to open detailed monitor
- Modal displays live bitrate graph (SVG, up to 2min history @ 1Hz polling)
- Channel encoder/packager nodes as external links to id3as GUI
- Active events list with IDs, timestamps, and encoder node links
- Warnings list filtered by channel ID with module, message, repeat count
- Encoder/Source state status, current bitrate, stream codec/audio info
- Modal auto-closes on close button or backdrop click; polling cleanup on close

## [2.16.0] - 2026-05-14

### Added

- Channel node column now renders as an external link (`nodeLink`) to the id3as admin UI.
- Each event in the channels ev-strip shows its actual encoder node beside the event id;
  highlighted in amber when the event node differs from the channel's node.
- Each event row in the nodes nev-list shows its encoder node with `↗` prefix in amber
  when it differs from the node card it appears under.
- `bev()` now carries `encoder_node_id` from the running event payload into the event map.

## [2.15.3] - 2026-05-14

### Changed

- Log results now displayed in reverse chronological order (newest first).

## [2.15.2] - 2026-05-14

### Fixed

- `evFm is not defined` in Nodes view — `evFm` is now declared locally
  inside `renderChannels` and `renderNodes` (not as a global variable).
- Flags/events (keyed by `system_id` = event id) now count as warnings
  in Channels and Nodes: `evWm` added to `r.warnings` / `c.warnings`, so
  "Warnings only" filters them correctly and the WARN counter in the sumBar reflects them.
- `renderRunning`: `fm` now uses `bfmEv(flagsEvData)` indexed by event id
  instead of `bfm(raw.flagsEv)` indexed by channel id — lookup fixed to
  `fm[id]` (event id) with fallback to `fm[ch]`.
- Channels: flag/event warn-strips appear indented below each event
  in the ev-strip, with badge `⚠ N flag(s)`.
- Nodes: nwarn-list shows flags/events with `[ev]` label to distinguish
  them from channel flags.

## [2.15.1] - 2026-05-14

### Fixed

- Flags/events were not appearing in any view because the code indexed by
  `channel_id`, while `system_id` in flags/events corresponds to the **event ID**.
- `renderRunning`: `fm` is now indexed by event ID (`bfmEv(flagsEvData)`),
  lookup fixed to `fm[id]` (event ID) with fallback to `fm[ch]`.
- `renderChannels`: added `evFm` indexed by event ID; each event
  in the ev-strip shows a `⚠ N flag(s)` badge and warning-strip details below.
- `renderNodes`: nev-row displays an event-level flags badge via `evFm`.
- `renderRunning` ev-card: `hw` border activates when there are event flags,
  even if there are no channel flags.

## [2.15.0] - 2026-05-13

### Added

- **Flags/Events banner** — persistent alert bar immediately below the toolbar,
  visible across all views, populated from `/flags/events`; sorted by `repeated`
  in descending order; displays up to 6 flags with a `+N more` indicator; automatically
  hides when there are no entries.
- `flagsEvData` loaded across all views: `channels`, `nodes`, and `scheduled` perform
  an additional fetch to `/flags/events`; `running` reuses the `fev` already present in
  the `Promise.all` without making an extra request.

### Fixed

- Event name truncated using `text-overflow: ellipsis` in `.ev-name2` (cards in the
  Running Events view) and in `.ev-name` (inline event rows in channel/node cards),
  preventing overflow when the API returns long descriptions.

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

# Changelog

All notable changes to SP SO Web Toolbox are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.38.0] - 2026-07-24

### Added

- Added support for assigning ownership of anonymous GOP Analyzer test results.
- Added `POST /gop/assign-user/<filename>` endpoint for admins and engineers.
- Added searchable floating user selector for anonymous test ownership assignment.
- Added ownership assignment support from both the test detail view and history table.
- Added assignment audit metadata (`assigned.by`, `assigned.at`) to result files.

### Changed

- Improved history user badge behaviour for anonymous results.
- Improved specifications editor labels by clearly distinguishing:
  - **Accept range** (hard validation limits)
  - **Compliant within** (preferred compliance range)

### Fixed

- Fixed CODEC Level compliance evaluation incorrectly rejecting values that fell inside the configured preferred range but outside the hard range.
- Updated `comply_range()` and `_reeval_compliance()` to automatically expand hard limits when preferred bounds exceed configured acceptance limits.
- Prevented configuration mismatches where `pref_lo`/`pref_hi` could allow values that were still rejected by `lo`/`hi`.
- Validated fix using the reported scenario (`lo=3`, `hi=4.2`, `pref_lo=4.1`, `pref_hi=5.1`, `measured=5.1`), which now evaluates as **COMPLIANT**.

## [2.37.0] - 2026-07-22

### Fixed

- Chroma Subsampling falsely REJECTED high-bit-depth / non-8-bit
  formats (e.g. yuv422p10le showed as an unrecognised raw pix_fmt
  string instead of "4:2:2") because the chroma lookup table only
  covered plain 8-bit yuv420p/yuv422p/yuv444p variants. Now derived
  via regex, robust to bit-depth/endianness suffixes and NV/semi-planar
  layouts (nv12, nv16, p010le, etc).
- Colour Range showed the raw pix_fmt string as its measured value
  instead of "limited"/"full". Now reports the actual colour range,
  preferring ffprobe's color_range tag over the deprecated yuvj\*
  pix_fmt heuristic.

### Added

- New informational-only "Pixel Format" field (spec key
  pixel_format), showing the raw pix_fmt value (e.g. yuv422p10le) on
  its own row across the compliance table, HTML report, text/Jira
  report and specs editor — separate from Chroma Subsampling and
  Colour Range. Never affects overall_status.
- Result JSON: new v_full_range field, so reeval/workflow-change
  compliance reuses the accurate colour-range value instead of
  re-deriving it from pix_fmt alone.

## [2.36.0] - 2026-07-21

### Added

- Every GOP test now automatically runs the Ingest Analyser on the
  recorded .ts file (same script and same store/ingest-results output
  used by the standalone Ingest Analyser tool), right after the
  mediainfo delay check. Faster than a live re-capture and uses the
  same source file.
- GOP result JSON: new ingest_dir / ingest_zip fields, null if the
  Ingest Analyser is unavailable or fails (never blocks or fails the
  GOP result itself).
- Frontend: new "Ingest Analyser Report" button (left of Generate
  Report, right of Re-run), shown only for tests that have an Ingest
  Analyser report, opening it in a new tab.

## [2.35.4] - 2026-07-20

### Fixed

- Audio Bits per Sample falsely REJECTED aac_latm streams ("?" instead
  of "fltp") because the codec whitelist used to infer floating-point
  sample format didn't include aac_latm. Now derived primarily from
  ffprobe's sample_fmt field, with the codec whitelist (now including
  aac_latm) kept only as a fallback.

## [2.35.3] - 2026-07-20

### Fixed

- Audio Coding compliance falsely REJECTED AAC-LATM streams because
  ffprobe reports codec_name as "aac_latm" (underscore), which bypassed
  the AAC profile-detection branch and produced "AAC_LATM" instead of
  the spec's "AAC-LATM". \_audio_display_name now recognizes codec_name
  "aac_latm" directly and normalizes it to "AAC-LATM".

## [2.35.2] - 2026-07-20

### Changed

- mediainfo_delay spec now uses two adjustable thresholds instead of a
  single hard limit: |delay| <= warn (default 350ms) is COMPLIANT,
  <= hard (default 1000ms) is ACCEPTED, above hard is REJECTED. Both
  thresholds are adjustable per workflow in the specs editor.
- Specs editor, results panel, and HTML/text reports updated to reflect
  the three-tier compliant/accepted/rejected status.

### Removed

- Obsolete "Inform only (never reject)" toggle for mediainfo_delay
  (leftover from the old AV sync spec pattern; no longer applicable).

## [2.35.1] - 2026-07-20

### Added

- GOP Analyzer: AV sync now measured via mediainfo's "Delay relative to
  video" metric, read directly from the recorded .ts file after capture
  or upload, before compliance evaluation.
- New spec "mediainfo_delay" with an adjustable hard limit (default
  1000ms / 1s), applied to all workflows. Exceeding the limit rejects
  the result. Editable in the specs editor under the new "TIMING"
  section.

### Removed

- The unreliable ffprobe PTS-offset based AV sync analysis and its
  "AV SYNC & TIMING" spec block (av_sync_warn, av_sync_max,
  v_pts_jitter, a_pts_jitter), along with all related fields and UI
  sections.

### Changed

- Result JSON: av_sync_min_ms, av_sync_max_ms, av_sync_avg_ms,
  av_sync_median_ms, v_pts_jitter_ms and a_pts_jitter_ms replaced by a
  single mediainfo_delay_ms field.

### Requires

- mediainfo installed on the server (apt-get install mediainfo).
  Analysis falls back to UNKNOWN status if mediainfo is missing or the
  delay metric cannot be measured.

## [2.35.0] - 2026-07-20

### Added

- MediaInfo to SERVER_REBUILD

## [2.34.0] - 2026-07-17

### Fixed

- Extraction returned zero fields and empty raw text on some machines/
  sessions where the ServiceNow RITM ticket loads through the "Unified
  Navigation App" shell (now/nav/ui), because pageExtractor read the page
  before any async-mounted content existed.

### Changed

- pageExtractor now waits (up to 12s, polling every 400ms) inside the
  page for real content or form fields to appear before reading them.
- Extraction retries up to 3 times across all frames as a fallback for
  frames created after the initial call.
- Loading state shows attempt/progress feedback during longer waits.

## [2.33.0] - 2026-07-17

### Added

- B&T to SRT Ingest

## [2.32.0] - 2026-07-17

### Added

- New standalone API documentation page (SO-Toolbox-API-Docs.html) covering all Flask Blueprints: auth, GOP compliance, SRT ingest, SRT push monitor, TXCore manager, RTS monitor, id3as DC monitor, WC2026 rota, and proxy.py utility routes. Includes searchable sidebar, collapsible endpoint cards, auth requirements, request/response examples, and known-issue notes carried over from current backlog items.

## [2.30.1] - 2026-07-16

### Fixed

- Restart Proxy was still rejecting requests with "Invalid admin
  password" after the 2.30.0 frontend change, because proxy.py's
  /restart-proxy endpoint still validated the X-Admin-Password header
  that the frontend no longer sends. /git-pull and /restart-proxy now
  use the existing require_admin_role decorator from routes_auth.py
  (admin/engineer only) instead of the ADMIN_PASSWORD check. The
  password check is unchanged for /mtr/kill and /mtr/delete.

---

## [2.30.0] - 2026-07-16

### Fixed

- Changelog modal: bullet items that wrap onto multiple lines in
  CHANGELOG.md were silently truncated at the first line, since the
  parser only matched lines starting with "- " and dropped indented
  continuation lines. Wrapped continuation lines are now appended to
  the previous bullet instead of being discarded.

### Changed

- Update and Restart Proxy actions are now restricted to users with
  the admin or engineer role, read from /so-proxy/me. The buttons are
  hidden for other roles and the actions no-op client-side if called
  directly. The admin password prompt on Restart Proxy has been
  removed.

---

## [2.29.0] - 2026-07-16

### Fixed

- GOP analysis: incomplete leading GOP (frames captured before the
  first I frame) is now excluded from GOP statistics, matching the
  existing exclusion of the incomplete trailing GOP. GOP size,
  min/max/avg, and open/closed detection now only consider complete
  GOPs between the first and last I frame.

## [2.28.1] - 2026-07-15

### Added

- Multi-destination ingest can now run as a single shared ffmpeg process
  (passthrough / -c copy) instead of one process per destination, to avoid
  CPU spikes when fanning out to many SRT targets at once.
- New endpoint POST /srt/ingest/multi-shared for the shared-process mode.
- "Shared single process" option in the Multi Destination form.

### Notes

- Shared mode only supports passthrough (-c copy). A shared-encode option
  for CBR transcode fan-out (via ffmpeg's tee muxer) is not implemented yet.

## [2.28.0] - 2026-07-15

### Added

- SRT ingest jobs now automatically retry connecting until the user explicitly
  stops them, instead of ending on the first ffmpeg failure.
- Per-job restart endpoint and button, independent from other jobs.
- Last error message per job is captured and shown live in the Bitrate
  Monitor when a job is reconnecting or has failed.
- "Clear Finished Jobs" button to remove stopped/error jobs from the list.

### Changed

- Job status model extended: starting, running, reconnecting, stopping,
  stopped, error (finished status removed, replaced by stopped).
- SSE stream for job stats now also emits a status event with status,
  last_error and retry_count whenever the job state changes.

### Fixed

- Job dict now stores full launch configuration (input file, host, port,
  passphrase, bitrate, mode), required to support relaunching a job.

## [2.27.0] - 2026-07-15

### Added

- Server-side filtering and pagination for the GOP results history,
  so search/tag/user/date filters cover the entire history instead
  of only the 500 most recently created results
- In-memory results index with mtime-based cache invalidation to
  avoid re-parsing every JSON result file on each request
- Numbered pagination controls in the History panel

### Changed

- GET /gop/results now returns a paginated object
  (items, total, page, page_size, total_pages, tags) instead of a
  flat array; filtering moved from client-side to query parameters
  (search, date, tag, server, user, page, page_size)

## [2.26.5] - 2026-07-13

### Added

- GET /api/txcore/categories endpoint to list existing TXCore
  categories.
- Category picker dropdown in TXCore Manager, populated from the new
  endpoint, to select an existing category instead of typing its ID.

### Changed

- Failed channel creation attempts now show the HTTP status and the
  API's response body directly in the job log, instead of just a
  generic "error" status.

### Fixed

- Channel creation failures with a non-JSON error body no longer
  crash response handling; the raw response text is now captured.

## [2.26.4] - 2026-07-13

### Fixed

- Stream addresses could be generated incomplete (e.g. ".35:21216")
  due to a stale empty value persisted in localStorage from an earlier
  form version. Storage key bumped to invalidate old state.

### Changed

- Reworked IP octet configuration: first two octets are now fixed per
  site (display-only), third octet is a shared field applied to all
  sites (still editable per site), last octet continues to follow
  First CH#. Ports are now read-only per site.
- Added a live address preview per site (AVE/LMK/YER) so the final
  multicast address is visible before submitting.

## [2.26.3] - 2026-07-13

### Changed

- TXCore Manager: "Provider name" relabeled to "Provider Acronym".
- TXCore Manager: First CH# now defaults to 01, channel count to 10.
- TXCore Manager: Channel number start and the three last-octet-start
  fields now auto-fill from First CH#, remaining editable; manual
  edits stop further auto-sync for that field.
- TXCore Manager: AVE/LMK/YER 3-octet IP prefixes are now prefilled
  as real default values instead of placeholder text.

## [2.26.2] - 2026-07-13

### Fixed

- TXCore status endpoint reported all env vars as missing even when
  set in .env, due to import-order dependency on proxy.py's
  load_dotenv() call. routes_txcore.py now loads .env explicitly.

## [2.26.1] - 2026-07-13

### Fixed

- routes_txcore.py failed to import on startup due to a nonexistent
  auth module reference. Now uses routes_auth (\_get_session,
  \_token_from_request), consistent with the other blueprints.

## [2.26.0] - 2026-07-13

### Added

- TXCore Manager frontend (TXCore-Manager.html) for the TXCore
  provisioning blueprint: category creation, bulk channel form,
  request preview, and async job monitoring with live progress log.

## [3.25.0] - 2026-07-13

### Fixed

- Id34as logs on showing reverse sort, new on top.

## [3.24.1] - 2026-07-10

### Added

- Automatic log rotation for /var/log/srt-push.log: rotates via copytruncate
  once the file exceeds 100 MB, keeps rotated backups for 7 days.
- Strict transport-level CBR via ffmpeg -muxrate, padding the MPEG-TS with
  null PID (0x1FFF) packets.
- Default value placeholders on the SRT Push configuration form (dashboard
  URL, width, height, FPS, bitrate).

### Fixed

- Bitrate/fps/frame telemetry and sparkline bars no longer keep showing
  stale values after the service is stopped.

### Changed

- Tally light and sparkline bars now use green for the running/on-air state;
  red is reserved for error/failed states.

## [3.24.0] - 2026-07-01

### Added

- Supplier filter dropdown in Channels tab, auto-populated from channel names matching the "XXX_CHXX" pattern.
- Dedicated "RMG" filter option for channels containing "RMG" in their name.
- "Show Stream ID" checkbox to toggle visibility of the Stream Key column (hidden by default).
- Channel name now links to the corresponding Phenix portal stream page, built from App ID, Channel ID and Stream Key.

### Changed

- Filter logic in the Channels tab now combines search, active-only, and supplier filters.
- Disconnect flow resets supplier filter and Stream ID toggle.

## [3.23.0] - 2026-07-01

### Fixed

- WC2026 Rota: openfootball sync lookup used a single-entry map keyed by date+BST time; simultaneous kickoffs (all group-stage matchdays have two games at the same time) caused the second entry to overwrite the first, making every other match silently lose its sync result; lookup now stores arrays of candidates per key and disambiguates by fuzzy team-name match with diacritic normalization~

## [3.22.0] - 2026-06-30

### Added

- RTS Monitor: added StreamID and StreamKey to the table. Created removed.

## [3.21.0] - 2026-06-30

### Fixed

- WC2026 Rota: openfootball sync lookup used a date+1 alias that caused key collisions between unrelated matches sharing the same BST time, silently overwriting one match with another's data and making it disappear from the view; lookup now matches each fixture primarily on its own date and falls back to date-1 only for early-morning BST kickoffs, computed per fixture instead of pre-indexed
- WC2026 Rota: team name updates from openfootball sync were only applied in memory and never persisted, reverting to placeholder names on every reload; added teamNames state, a new POST /wc2026/teamnames endpoint, and restoration of saved overrides on load
- WC2026 Rota: Turkiye vs Paraguay and Brazil vs Haiti (both 19 June) had stale hardcoded kickoff times that no longer matched the openfootball feed, so results never matched during sync; corrected kickoff times for both fixtures

## [3.20.0] - 2026-06-29

### Fixed

- WC2026 Rota: scores were silently discarded by save_assignments endpoint (only saved via the separate scores endpoint on Sync); assignments POST now merges and persists scores alongside assignments, so results survive page reloads for all users
- WC2026 Rota: openfootball sync failed to match late-night kickoffs (e.g. Brazil vs Haiti, Turkiye vs Paraguay) due to two issues: UTC offset regex did not accept two-digit formats (UTC-04:00), and games with local date differing from BST date were not found in the date-keyed lookup; lookup now also indexes under date+1 to cover BST crossings

### Added

- WC2026 Rota: fourth engineer slot Marcus (code M, purple) added across frontend and backend; filter button, name input, legend badge, row colouring, EPG colour, summary card, auto-assign, CSV import resolver and server persistence all updated

## [3.19.1] - 2026-06-26

### Added

- User list now grouped by role with section separators (Admins → Engineers → Specialists → Analysts → Users)
- Engineers are blocked from editing or deleting admin accounts (UI buttons disabled + backend 403 guard)
- Engineers cannot assign the admin role when creating or editing users (option removed from dropdown)

## [3.19.0] - 2026-06-26

### Changed

- /opt/web/store created and gop-results and ingest-results moved there. The idea is to have a separate storage for these results

## [3.18.1] - 2026-06-25

### Added

- Specs Editor now has its own workflow dropdown, independent of the workflow selector on the test page; switching workflow inside the editor loads the corresponding specs without affecting the active test configuration
- "★ Set as API default" button in the Specs Editor footer (admin/engineer only); sets the workflow used by the API when no workflow is specified in the request
- `GET /gop/workflows` now returns `{ labels: {…}, default: "…" }` so the frontend always knows which workflow is the current API default
- `POST /gop/workflows/default` endpoint to persist the API default workflow to `workflow_default.json` (admin/engineer only)
- `_effective_default_workflow()` helper in backend; all routes that previously fell back to the hard-coded `DEFAULT_WORKFLOW` constant now read the persisted value instead
- "Accept any GOP size" checkbox in the Specs Editor for the GOP Size row; when enabled, any measured GOP size returns ACCEPTED regardless of configured values or tolerance
- Re-evaluate (⇄) button on each history entry: opens a modal to select a target workflow and shows a read-only re-evaluated report without modifying the stored result (`GET /gop/reeval/<file>?workflow=<wf>`)
- Change Workflow (🔀) button on each history entry (admin/engineer only): permanently re-assigns the workflow, re-runs compliance, and appends an entry to `workflow_change_log` in the result JSON (`PATCH /gop/result/<file>/workflow`)
- After a workflow change the updated compliance result is rendered immediately from the PATCH response, without a second round-trip
- Workflow badge (📋) added to the test meta-bar on the main page; shows the workflow used for the loaded result, or `⇄ <label>` in cyan when showing a re-evaluated report
- Re-evaluated report shows `⇄ <workflow> (re-evaluated)` in the Workflow field of both the visual and text report tabs
- Specs save and workflow rename now record `_meta.saved_by` / `_meta.saved_at` in the specs JSON; the Specs Editor footer displays who last saved and when
- Role-based authorisation for all write operations in the Specs Editor (save, rename, reset, set default): replaced admin password with `/so-proxy/me` role check; requires `admin` or `engineer` role
- `GET /gop/specs` includes `_meta` in the response when specs have been saved at least once
- `POST /gop/specs` stamps `_meta` server-side and returns HTTP 403 if the caller's role is not `admin` or `engineer`

### Changed

- Workflow selection is no longer persisted in `localStorage`; the page always starts on the current API default workflow
- GOP Type spec changed from a single `required` field to the standard `values` + `preferred` model: CLOSED returns COMPLIANT, OPEN returns ACCEPTED; the Specs Editor renders a dropdown for the Preferred column
- B-Frames spec changed to the same `values` + `preferred` model: absent returns COMPLIANT, present returns ACCEPTED; the Specs Editor renders a dropdown for the Preferred column
- Specs Editor preferred column now renders as a `&lt;select&gt;` for any spec whose allowed values are a short fixed list of strings (less than or equal to 4 items), instead of a free-text input
- Frame Rate compliance row appends `[accepted: 50p @ 720p]` to the measured value in both visual and text reports when 50p is accepted due to 720p resolution
- GOP Type and B-Frames spec descriptions updated in visual and text report tabs to reflect the preferred/accepted model
- `PATCH /gop/result/<file>/workflow` now returns the full updated result object in addition to `overall_status`, eliminating the need for a follow-up GET

### Fixed

- Specs Editor no longer reads or writes `localStorage`; re-opening the tool always reflects the API default instead of the last manually selected workflow
- `gop_type` and `b_frames` compliance now goes through the shared `comply_enum_multi` function, removing ~30 lines of duplicate custom logic
- Re-evaluate modal now populates the workflow list from `WORKFLOW_LABELS` at open time, ensuring new or renamed workflows appear correctly

## [3.18.1] - 2026-06-25

### Added

- Specs Editor now has its own workflow dropdown, independent of the workflow
  selector on the test page; switching workflow inside the editor loads the
  corresponding specs without affecting the active test configuration
- "Set as API default" button in the Specs Editor footer (admin/engineer only);
  sets the workflow used by the API when no workflow is specified in the request
- GET /gop/workflows now returns labels and default workflow key so the frontend
  always knows which workflow is the current API default
- POST /gop/workflows/default endpoint to persist the API default workflow to
  workflow_default.json (admin/engineer only)
- \_effective_default_workflow() helper in backend; all routes that previously
  fell back to the hard-coded DEFAULT_WORKFLOW constant now read the persisted
  value instead
- "Accept any GOP size" checkbox in the Specs Editor for the GOP Size row; when
  enabled, any measured GOP size returns ACCEPTED regardless of configured values
  or tolerance
- Re-evaluate button on each history entry: opens a modal to select a target
  workflow and shows a read-only re-evaluated report without modifying the stored
  result (GET /gop/reeval/file?workflow=wf)
- Change Workflow button on each history entry (admin/engineer only):
  permanently re-assigns the workflow, re-runs compliance, and appends an entry
  to workflow_change_log in the result JSON (PATCH /gop/result/file/workflow)
- After a workflow change the updated compliance result is rendered immediately
  from the PATCH response, without a second round-trip
- Workflow badge added to the test meta-bar on the main page; shows the workflow
  used for the loaded result, or the re-evaluated workflow label in cyan when
  showing a re-evaluated report
- Re-evaluated report shows the target workflow label in the Workflow field of
  both the visual and text report tabs, marked as re-evaluated
- Specs save and workflow rename now record saved_by and saved_at in the specs
  JSON; the Specs Editor footer displays who last saved and when
- Role-based authorisation for all write operations in the Specs Editor (save,
  rename, reset, set default): replaced admin password with /so-proxy/me role
  check; requires admin or engineer role
- GET /gop/specs includes \_meta in the response when specs have been saved at
  least once
- POST /gop/specs stamps \_meta server-side and returns HTTP 403 if the caller's
  role is not admin or engineer

### Changed

- Workflow selection is no longer persisted in localStorage; the page always
  starts on the current API default workflow
- GOP Type spec changed from a single required field to the standard
  values + preferred model: CLOSED returns COMPLIANT, OPEN returns ACCEPTED;
  the Specs Editor renders a dr0pdown for the Preferred column
- B-Frames spec changed to the same values + preferred model: absent returns
  COMPLIANT, present returns ACCEPTED; the Specs Editor renders a dropdown for
  the Preferred column
- Specs Editor preferred column now renders as a dr0pdown for any spec whose
  allowed values are a short fixed list of strings (4 items or fewer), instead
  of a free-text input
- Frame Rate compliance row appends a note when 50p is accepted due to 720p
  resolution, visible in both visual and text reports
- GOP Type and B-Frames spec descriptions updated in visual and text report tabs
  to reflect the preferred/accepted model
- PATCH /gop/result/file/workflow now returns the full updated result object in
  addition to overall_status, eliminating the need for a follow-up GET

### Fixed

- Specs Editor no longer reads or writes localStorage; re-opening the tool
  always reflects the API default instead of the last manually selected workflow
- gop_type and b_frames compliance now goes through the shared comply_enum_multi
  function, removing duplicate custom logic
- Re-evaluate modal now populates the workflow list from WORKFLOW_LABELS at open
  time, ensuring new or renamed workflows appear correctly

## [3.17.0] - 2026-06-23

### Fixed

- BTV: Bug single B frame affecting GOP fixed

## [3.16.1] - 2026-06-23

### Fixed

- Event-starting alarm suppression now detects startup by comparing `started_at`
  to current time (3-min window), instead of the absence of `encoder_job_started`
  which was never false once the event was running.

## [3.16.0] - 2026-06-23

### Added

- **Nodes view – Event starting grace period**: when a channel has an active event but the encoder job has not yet started, alarms for that specific channel are suppressed for 3 minutes. The node card blinks green and displays an "event starting, ignoring alarms" badge during this window. Only alarms tied to that channel/event are suppressed; other channels on the same node are unaffected.
- **Nodes view – Warnings-only filter**: channels in the event-starting grace period are excluded from the warning count and hidden when "Warnings only" is active.

### Fixed

- **Events tab**: channel-level flags (`/flags/channels`) are now fetched alongside event flags when loading the running events view, so flag warnings appear correctly in the Events tab alongside the existing "no signal" indicator.
- **Events tab**: event flag deduplication prevents duplicate warning entries when a flag appears under both event id and channel id keys.

## [3.15.1] - 2026-06-22

### Fixed

- Colour Range spec editor now shows a note that internal values are
  `limited` / `full` (not pixel format strings like `yuvj420p`).
  Existing corrupted specs files should be reset to defaults.
- AV Sync metrics in "Inform only" mode now show `INFO` status instead
  of COMPLIANT/ACCEPTED, and are excluded from the overall result.
  A new blue INFO pill was added to the compliance table and reports.

## [3.15.0] - 2026-06-22

### Added

- AV Sync & Timing thresholds are now configurable in the Specs editor
  (warn threshold, hard limit, and "Inform only" mode that prevents REJECTED).
  Default mode is inform-only for all four AV sync metrics.
- Workflow display name can be renamed directly in the Specs editor; names
  are persisted server-side in workflow_labels.json and loaded at page boot.
- Workflow name now appears in both the visual and text test reports.

### Fixed

- Colour Range: `yuvj420p` (full range) is now accepted (ACCEPTED) instead
  of being incorrectly rejected; `limited` remains COMPLIANT.
- B-Frames spec now renders correctly in the Specs editor with a dr0pdown
  (absent / present); previously no field was shown.

## [3.14.0] - 2026-06-19

### Added

- Initial release of Probe Monitoring (`ProbeMonitoring.html`), replacing `RTV MV Monitoring.html`.
- Two independent channel slots, each with a dr0pdown of 40 configurable channels (`Id3as AWS CH301 - PROBE CH01` through `Id3as AWS CH340 - PROBE CH40`) plus a fixed `RMG MV` entry.
- "Configure channels" modal to register the Id3as AWS and Probe URL pair for each of the 40 channels, persisted in `localStorage`.
- `RMG MV` entry reproducing the original four reference feeds (T21 enc → INX, T21 enc → EQP, INX → AVE, EQP → AVE) as a fixed, non-editable option.
- Slot selections persisted in `localStorage` so the last-viewed channels are restored on reload.
- Empty-state messaging for slots and channels without configured URLs.
- Redesigned dark control-room UI (monospace channel labels, teal/amber accents) replacing the original CodePen-based table layout.

## [3.13.1] - 2026-06-17

### Fixed

- The Specification column in the compliance table, and the matching
  column in the visual/text test reports, now show the actual specs
  saved for the workflow used in the test, instead of always showing
  the original hardcoded defaults.

## [3.13.0] - 2026-06-18

### Added

- Workflow selector dropdown before "Analyse Now", supporting independent
  compliance spec sets: "DC - Aminos and TP" (existing), "RTS", and "W&B".
- The Specs editor (⚙) now edits the compliance specs for the currently
  selected workflow independently, with each workflow's specs stored and
  saved separately on the server.
- GOP analysis results now record which workflow was used for the test.

### Changed

- Reduced the Host / IP field width to make room for the new Workflow
  dropdown in the analysis form.

## [3.12.0] - 2026-06-16

### Added

- New `GET /gop/jobs/running` endpoint listing all in-progress jobs from
  the in-memory job store, regardless of the calling client (HTML
  frontend, Chrome extension, or any other API consumer).
- The Scheduled panel now also displays jobs started outside the
  scheduler (e.g. by the Chrome extension calling `/gop/run` directly),
  marked with a "🔌 External" badge and without a Cancel action.

## [3.11.2] - 2026-06-16

### Changed

- Split the chroma compliance check into two independent rows: **Chroma
  Subsampling** (4:2:0/4:2:2/4:4:4, derived from pixel format) and a new
  **Colour Range** check (limited vs full). Previously both concepts were
  conflated into a single `chroma` row, which made full-range formats
  like `yuvj420p` either incorrectly pass (same subsampling as `yuv420p`)
  or, after the v2.28.0 fix, correctly reject but under a misleading
  "Chroma Subsampling" label.

### Added

- New `colour_range` spec (default: `limited`) in `DEFAULT_SPECS`,
  configurable via the Specs Editor. Pixel formats starting with `yuvj`
  (e.g. `yuvj420p`) are measured as `full` and rejected against the
  `limited` requirement; standard formats (`yuv420p`, etc.) measure as
  `limited` and pass.

## [3.11.1] - 2026-06-16

### Fixed

- GOP chroma compliance check (`routes_gop.py`) no longer conflates pixel
  format with chroma subsampling. `yuvj420p` (full-range) is now
  correctly rejected instead of being reported as `ACCEPTED`; `yuv420p`
  (limited-range) is accepted as before. The compliance report now shows
  the actual pixel format (e.g. `yuv420p`, `yuvj420p`) as the measured
  value, distinct from the `4:2:0` chroma subsampling notation used for
  spec matching.

## [3.11.0] - 2026-06-16

### Fixed

- BTV: Specs editor `saveSpecs()` no longer truncates `preferred` values containing
  `:` or `x` (e.g. `4:2:0`, `1920x1080`) when saving. `parseFloat` was
  silently parsing only the leading numeric portion; the fix now requires
  an exact round-trip match before treating a value as numeric.

## [3.10.0] - 2026-06-15

### Added

- WC2026 rota: Sync is persistent now

## [3.9.0] - 2026-06-12

### Added

- Id3as DC: Channel list cards now display the input multicast address and port below the channel ID

## [3.8.0] - 2026-06-09

### Added

- `wc2026_rota.html`: WC 2026 engineering rota planner — assign engineers to
  matches, auto-assign by round-robin, filter by engineer/venue/date, export CSV
- `wc2026_routes.py`: Flask blueprint exposing `GET /wc2026/assignments` and
  `POST /wc2026/assignments`; assignments persisted to `wc2026_assignments.json`
- Role-based access: only admin users can assign, auto-assign, clear, import CSV
  or rename engineers; non-admins see the rota in read-only mode
- CSV import restores assignments and engineer names from a previously exported file
- Save status indicator in header shows last saved by/when, pending and error states
- Session resolved via existing `/so-proxy/me` endpoint; no additional auth logic

## [3.7.0] - 2026-06-02

### Added

- SRT Tool
- `GET /srt/sources`: lists test.mp4 and all .ts files from /gop-results
- Source file dropdown populated on page load; manual input still available
- Passthrough mode (-c:v copy -c:a copy) for .ts sources, no re-encode
- Passthrough checkbox shown only when a .ts source is selected
- Mode badge (transcode/passthrough) shown per job in the jobs list

## [3.7.0] - 2026-06-02

### Added

- FIFA World Cup 2026 Tool

## [3.6.0] - 2026-05-29

### Added

- Size of buttons and order of tags adjusted on BTV Video Analyser

## [3.5.1] - 2026-05-29

### Added

- feat: PATCH /gop/result/:file to update tag field

## [3.5.0] - 2026-05-29

### Added

- Tag editor for past analyses: click the ✎ button on any history item to add, remove, or rename tags
- Chip-based tag input UI with keyboard shortcuts (Enter/comma to confirm, Backspace to remove last)
- Tag suggestions populated from existing tags in the current history view
- Saves via POST /gop/tag/:file with { tag } payload, same pattern as override endpoint

## [3.4.0] - 2026-05-28

### Changed

- Copy button in report modal now copies rich HTML when Visual tab is active, enabling formatted paste into Jira comments (table, badges, and layout preserved with white background)
- Copy button label dynamically updates to **Copy Visual** or **Copy Text** depending on the active tab
- Added `id="btn-copy-report"` to the copy button for tab-aware label sync
- Visual copy uses `ClipboardItem` (`text/html` MIME) with fallback to `execCommand` for older browsers

## [3.3.0] - 2026-05-27

### Changed

- Sidebar now starts collapsed by default; expands on hamburger click
- Persisted state still respected on subsequent visits

## [3.2.2] - 2026-05-27

### Fixed

- `router_srt.py`: replaced Python 3.10+ union type syntax (`dict | None`, `list[str]`)
  with `typing.Optional` and `list` for compatibility with Python 3.9

## [3.1.2] - 2026-05-27

### Changed

- ffmpeg: added `-stream_loop -1` to loop input file indefinitely until job is stopped

## [3.1.1] - 2026-05-27

### Added

- Server quick-select dropdown populated from `SRT_LOCAL_N` keys in `/so-proxy/config`
- Manual host input remains editable alongside the dropdown

### Changed

- Passphrase is now optional in both frontend and backend
- SRT URL omits `?passphrase=` entirely when passphrase is empty

## [3.1.0] - 2026-05-27

### Added

- ffmpeg strict CBR profile: `-b:v`, `-minrate`, `-maxrate`, `-bufsize` (2× target)
- `bitrate_mbps` parameter on ingest endpoints (default 8 Mbps)
- SSE endpoint `GET /srt/jobs/<id>/stats` streams live ffmpeg stats
- Real-time bitrate canvas chart with UTC time axis and CBR target line
- UTC wall-clock (HH:MM:SS.mmm) updated every 50 ms
- Colour-coded bitrate deviation indicator (green/yellow/red vs target)

## [3.0.0] - 2026-05-27

### Added

- `router_srt.py`: Flask Blueprint for SRT ingest routes
  - `POST /srt/ingest/single` — start a single ffmpeg SRT ingest job
  - `POST /srt/ingest/multi` — start ingest to a port range (up to 100 destinations simultaneously)
  - `GET /srt/jobs` — list all registered jobs with status
  - `GET /srt/jobs/<id>` — get status of a specific job
  - `POST /srt/jobs/<id>/stop` — send SIGTERM to a running job
  - `POST /srt/jobs/stop-all` — stop every running job
- `templates/srt_tool.html`: browser-based SRT ingest control panel
  - Single-destination form with live ffmpeg command preview
  - Multi-destination form with port-range selector and destinations preview
  - Active jobs monitor with auto-refresh (5 s), per-job stop button and Stop All
  - Real-time stats counters: total / running / finished / errors
  - Toast notifications for API feedback
- `app.py`: main Flask proxy entry point; registers `srt_bp` blueprint
- ffmpeg encoding profile: libx264, force-cfr, 25 fps, GOP 50, AAC 48 kHz stereo, 1920×1080, mpegts over SRT

## [2.30.1] - 2026-05-26

### Fixed

- Collapsed sidebar tooltip was hidden due to `overflow: hidden` on sidebar element
- Replaced CSS `::after` tooltip (clipped by sidebar bounds) with a JS-driven fixed-position label
- Tooltip now slides in from the right edge of the collapsed sidebar, matching the design reference

## [2.30.0] - 2026-05-26

### Added

- Collapsible sidebar with hamburger button in the topbar
- Collapsed state shows icons only; hovering a tool reveals its name in a tooltip
- Sidebar open/collapsed state persists across sessions via `localStorage`
- Hamburger animates into an X when the sidebar is collapsed

## [2.29.0] - 2026-05-26

### Changed

- id3as routes: disable SSL certificate verification to support local HTTPS endpoints with self-signed or hostname-mismatched certificates

## [2.28.1] - 2026-05-25

### Added

- History user filter: new input field to filter results by username.
- "My Checks" toggle button: one-click filter to show only the current user's tests; reads session from `/me`, falls back to `anonymous`.
- Username badge displayed on each history item (first part of email, before `@`).
- GOP Structure section in Generate Report (visual HTML): per-GOP frame grid (I/P/B colour-coded cells), frame counts and avg GOP size.
- `username` field added to both visual and text/monospace report formats (`Tested by`).
- Manual: showing username on GOP check

## [2.28.0] - 2026-05-25

### Changed

- Refactored the ingest results endpoint (`/ingest/results`) to scan and sort by analysis directories instead of `.zip` files, ensuring incomplete or uncompressed execution outputs are properly listed.

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

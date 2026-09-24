# Changelog — Rota App

# Rota Changelog

## [Unreleased]

### Added 24-09-2026
- **In-app notification system**: staff receive a popup notification when
  management confirms or rejects an Annual Leave request. Management receive
  notifications for new leave submissions and withdrawal requests. Cancelled
  requests do not trigger notifications. Draft-mode changes are excluded.
- Notifications persist in `rota/notifications.json` and survive logout —
  unread notifications appear on next login.
- Notifications shown sequentially via modal popup; dismissed permanently
  on OK click.
- Frontend polls for unread notifications every 30 seconds.
- `notifications.json` added to the Data Files admin panel for
  download/backup/restore.

## [Unreleased] — UI overhaul PR1: sidebar shell + light theme

### Changed
- Replaced top horizontal `#topbar` + `#tab-bar` with a fixed 220px left
  sidebar (`#sidebar`), macOS Finder-style: logo/app-name header, nested
  nav (parent items expand/collapse only, no content change on parent
  click), user identity + Feedback/Sign out moved to sidebar footer.
- Retokenized `:root` to a macOS-light palette, grouped and labeled by
  purpose (NEUTRALS / BRAND / STATUS / TEAM BADGES / SPRINKLE / TYPE) for
  future tweaking without re-reading the whole stylesheet. Accent is
  `#3F1568` (deep purple).
- Draft banner now sits at the top of `#content-pane`, full width of the
  content area (not sidebar-embedded) — unchanged behaviour, new position.
- Reduced body noise-grain overlay opacity 0.35 → 0.08 (was tuned for dark
  bg, overpowered the light theme).
- Recalibrated `#rota-wrap` max-height (`calc(100vh - 230px)` →
  `calc(100vh - 140px)`) now that ~120px of sticky top chrome no longer
  sits above the content pane.
- Logo is now an `<img>` slot at `/assets/logo-placeholder.png` — shows
  broken-image icon until a PNG is supplied; deliberate placeholder.

### Not yet done (PR2, scoped separately)
- Sub-panel content splitting: Leave Approvals (Pending/History), Night &
  PH Hours (Compute/POT Consultation), My Overview (AL Allowance/SOE
  Weekend Coverage), Admin (People/Annual Leave/Feedback) all currently
  still render as one combined panel regardless of which child nav item
  was clicked — child clicks load the parent's full existing content.
  Actual show/hide-per-sub-item logic is the next pass.
- A handful of hardcoded dark-theme rgba/hex values remain in
  `.data-table`, `.pot-table`, and history-row border colors (e.g.
  `#252525`, `rgba(255,255,255,0.15)`) — not yet swept to light-theme
  equivalents. Cosmetic only, doesn't affect function.
- Mobile static-screenshot view — explicitly deferred, separate spec.

### Verification performed
- Div open/close tag balance confirmed equal (379/379).
- Full inline `<script>` block confirmed to parse as valid JS syntax
  (`new Function()` on extracted source — syntax check only, not a
  runtime/click-through test; UAT in-browser still required).
- No stray references to removed `.tab-btn` / `#topbar` / `#tab-bar`
  selectors remain anywhere in CSS, HTML, or JS.

---

## [Unreleased] — UI overhaul PR1 (cont.): rota table retheme + fonts

### Changed
- Rota table chrome (headers, date column, weekend/PH/today shading, gap
  flags, draft-mode header tint, borders) retheme dark → light. Scope
  was deliberately narrow: SHIFT_COLORS (the JS map driving actual shift
  code colors, plus OFF/ABSENT/PARENTAL/MARITAL) was left untouched per
  explicit "shift colors remain untouched" instruction — flag if
  OFF/ABSENT/PARENTAL/MARITAL should also be relit for light theme, since
  those aren't work-shift legend colors and the instruction's scope on
  them was ambiguous.
- Type system unified: --mono / --display / --numfont all now resolve to
  Inter; hierarchy comes from weight/size only, not family. Variable
  names kept as-is (legacy — "mono" doesn't mean monospace) with an
  explanatory comment rather than renaming ~106 call sites for a
  cosmetic-only gain.
- New --font-title token + @font-face for Resolve Sans (title only),
  falling back to Inter until licensed .woff2 files are supplied at
  /assets/fonts/ResolveSans-{Regular,Bold}.woff2. Resolve Sans is
  Blackmagic Design's proprietary font — NOT on any public CDN. Requires
  license confirmation for web-embed use before those files are hosted.
  Flagged explicitly; not resolved by this change.
- Google Fonts import trimmed: dropped Syne and Space Mono (both fully
  unused after the Inter consolidation — Space Mono was already dead
  weight before this pass, Syne was --display's old value). Kept Aptos
  Narrow + Roboto — both used exclusively by the print-export CSS
  (.print-title / table.print-rota), which is intentionally out of scope
  for this theme pass.
- Sidebar header text "SP SO Rota" -> "Streaming Ops Rota" (the <title>
  tag already read correctly -- only the visible sidebar label was stale).

### Verification performed
- Div balance (379/379), inline <script> syntax parse -- both hold post-edit.
- Confirmed no remaining "SP SO" string anywhere in the file.
- Confirmed print-export font-family declarations (Roboto/Aptos Narrow)
  untouched and still bypass the --mono/--display vars as designed.

### Still open
- Resolve Sans font files not supplied -- title currently renders in
  Inter (fallback) until sourced + licensed.
- Sub-panel content splitting (PR2, unchanged from prior entry).
- data-table / pot-table hardcoded dark border colors (#252525 etc.)
  not yet swept -- cosmetic only, still pending.

---

## [Unreleased] — UI overhaul PR1 (cont.): OFF/ABSENT/PARENTAL/MARITAL relight + type hierarchy

### Changed
- SHIFT_COLORS: OFF -> bg #ececec / fg #949292 (matches weekend date-col
  styling exactly). ABSENT -> same base + diagonal texture recolored to
  an intermediate gray (rgba(148,148,148,0.55)) so it reads between the
  light bg and dark label on a light table. PARENTAL and MARITAL both
  unified to bg #71efde with a dotted overlay; dotted pattern itself
  reworked (sparser, dark dots on transparent, ~7px grid) to read closer
  to Excel's light dot-fill pattern instead of the old faint white dots.
- .shift-cell font-weight 700 -> 500, to sit visually lighter than the
  bold header row (700) and bold date column (700) — this is the single
  line to touch if hierarchy needs further adjustment (index.html, rule
  `.shift-cell { font-weight: ... }`).
- Legend swatches (Rota tab, bottom) synced to match the above so the
  legend doesn't contradict the table.
- Unrelated pre-existing bug fixed opportunistically: legend's
  "Confirmed AL" swatch was #9ee6a6 (green) but SHIFT_COLORS['AL_APPROVED']
  is #FFEB3B (yellow) — legend never matched the actual cell color. Now
  synced.

### Flag — real functional consequence, not just cosmetic
- PARENTAL and MARITAL cells render as text-blank in the grid (existing
  behaviour, unchanged) — with both now sharing the identical bg color
  and dotted pattern, **they are visually indistinguishable in the rota
  table itself**. The legend has a thin border added to Marital's swatch
  to tell them apart there, but that border isn't applied to actual grid
  cells. If distinguishing Parental from Marital at a glance in the live
  table matters, this needs a follow-up (e.g. a border, different dot
  density, or a tiny corner mark) — not resolved by this change, executed
  literally per instruction as given.

---

## [Unreleased] — UI overhaul PR1 (cont.): legend hidden, Marital recolor, draft-selection contrast

### Changed
- #legend block commented out (not deleted) in the Rota tab — reclaims
  vertical space; users already know the color scheme from the existing
  Excel-format rota. Swatches inside were kept in sync with SHIFT_COLORS
  before commenting out, so uncommenting later won't restore stale colors.
- SHIFT_COLORS['MARITAL'] bg #71efde -> #ffffff (kept dotted:true) so
  Parental and Marital are now visually distinct in the actual grid, not
  just in the (now-hidden) legend.
- Draft-mode selected-cell text: was forced white (#fff !important),
  illegible against light-theme shift colors. Changed to var(--warn)
  (#e6a850) — the exact color of the selection-box border, not an
  approximation — plus a font-weight bump to 700 (unrequested addition,
  pairs with the earlier 500-weight base so selected text doesn't go thin
  and hard to read under the amber overlay).

### Flagged, not resolved
- Amber selection text against the lightest cells (OFF #ececec, Marital
  white) may still be low-contrast since the selection overlay itself is
  amber-tinted — same hue family as the text. Needs an actual in-browser
  check; if still weak, drop to a darker amber (#8a5a00, already used for
  the today-row text) instead of the exact border-match color.

---

## [Unreleased] — UI overhaul PR1 (cont.): draft-mode header contrast, layout scroll fix, toast relocation

### Changed
- Draft-mode header background/text: was pale amber bg (#fff3d6/#ffe9b8)
  with var(--muted) text — low contrast. Now #f0c876/#e8b85c bg with an
  explicit dark brown text color (#5c3d00), independent of whatever the
  base header's text-color token resolves to.
- Layout: replaced the hardcoded #rota-wrap max-height (calc(100vh -
  140px)) with proper flex distribution. Root cause of the "minor extra
  scroll" in draft mode: that magic number only accounted for chrome
  height *without* the draft banner, so it went stale whenever the
  banner appeared. Now #content-pane is a fixed-height (100vh) flex
  column, #panel-rota (when active) is itself a flex column filling all
  remaining space, #rota-toolbar/#draft-banner are flex-shrink:0, and
  #rota-wrap is flex:1 + min-height:0 — it now always fills exactly
  whatever space is actually left, banner shown or not, with no
  recalculation needed if chrome height changes again in future.
  content-pane keeps its own overflow-y:auto as a safety net for other
  (non-Rota) tabs whose content might exceed one viewport — untouched,
  not something you flagged as a problem.
- Toast notifications: moved from a fixed, viewport-centered overlay
  (z-index 9000, sitting on top of content) into the sidebar itself —
  now the last child of #sidebar-nav, pinned to the bottom of the nav
  column via margin-top:auto (so it sits just above the footer divider
  regardless of exact nav-item count, no pixel-math needed). #sidebar-nav
  is now display:flex/flex-direction:column to make that possible.
  Switched white-space:nowrap -> normal since it's now width-constrained
  to the sidebar rather than free-floating over full page width.

### Verification performed
- Confirmed exactly one #toast element in the DOM (caught and fixed a
  duplicate-insertion mistake during editing — old fixed-position toast
  wasn't removed on first pass, corrected before shipping).
- Div/nav tag balance, JS syntax parse — both hold.

### Still outstanding, not part of this pass
- The three items from the previous message (header bg #BDC0BF +
  text color, header/week-separator line, today-highlight dark-gray-bold
  text for working shifts) were given as instructions only, not applied
  to this file yet. Confirm if you want those folded in now.

---

## [Unreleased] — Bugfix: weekend-swap alias-blindness (backend + frontend)

### Root cause
WEEKEND_SWAP_PATTERNS is hand-tuned with specific shift codes (e.g.
1000-2000). The shift-registry alias feature (Admin > Shifts > rename)
rewrites what _base_shift() returns for dates on/after the alias's
effective_from, but the pattern tables were never consulted through
_resolve_alias. A renamed shift produces a silent "no match" — the button
never appears, no error, no clue. The rename from 1000-2000 triggered this.

### Backend fix (routes_rota.py)
- _match_weekend_pattern() now takes fri_date as a new required argument.
  Each hardcoded pattern code is resolved through _resolve_alias() per its
  actual calendar date before comparing against the (already
  alias-resolved) live window. OFF is fast-pathed — never aliased.
- Write path (rota_draft_weekend_swap): target_seq is now built by
  resolving each raw_target_seq code through _resolve_alias() per date,
  so the actual published override always writes the currently-active code
  rather than a pre-rename literal that may no longer exist in live data.
- Call site updated: _match_weekend_pattern(window) ->
  _match_weekend_pattern(window, fri_date).

### Frontend fix (index.html)
- init(): management users now eagerly fetch /rota/shifts and build a
  client-side alias cache (_clientAliasCache) via _rebuildClientAliasCache().
- New helpers: _rebuildClientAliasCache(shifts) and _resolveAliasClient(code,
  dateStr) — mirror the backend's _ALIAS_CACHE / _resolve_alias() exactly.
- openShiftPopover(): pattern comparison now builds per-cell dates for the
  10-cell window and resolves each pattern's codes through _resolveAliasClient
  before comparing, via a _resolvePattern(seq) helper defined inline.

### Acknowledged design debt (not fixed here)
The fix still duplicates alias-resolution logic across three locations
(backend _match_weekend_pattern, backend write path, frontend popover).
The clean solution is for the backend's /rota/draft/weekend-swap route to
return a "is_match / direction" flag so the frontend never needs its own
pattern logic at all. Flagged for PR2/3; out of scope for this pass.

---

## [Unreleased] — UI overhaul PR2: sub-panel content splitting

### Changed
- Leave Approvals: split into two sub-panels (pending, history). The
  hr-rule divider between them is removed — nav item click is now the
  mode switch.
- Night & PH Hours: split into compute and pot sub-panels. The
  pot-consult-toolbar was previously stacked below the compute result
  on the same combined page.
- My Overview: split into allowance and soe sub-panels. The SOE
  section was previously shown/hidden with a JS display toggle
  (getElementById('soe-weekends-section').style.display) — that
  approach is removed; the router handles visibility now.
- Admin: split into 5 sub-panels (people, shifts, al, datafiles,
  feedback). Added Shifts and Data Files as new child nav items in
  the Admin sidebar group (were previously only reachable by scrolling
  the combined Admin page with no nav affordance).
- _activateTab(tabName, subKey): new subKey parameter; when provided,
  shows only the matching [data-subpanel] div inside the active panel
  and hides the rest. Leaf tabs (Rota, Request Leave) have no
  [data-subpanel] children — the router no-ops the show/hide step for
  them cleanly.
- setupTabs: child button click now clears all active marks then sets
  both the child and its parent as active — active highlight now
  persists correctly after click (was previously lost because
  _activateTab was clearing all marks including the ones just set).

### Verification performed
- JS syntax parse: OK.
- div open/close tag balance: 407/407.
- data-subpanel attribute count: 16 (8 sub-panels × 2 for opening +
  data-subpanel= on the div itself — each key appears exactly twice).
- No remaining references to removed soe-weekends-section ID.

## [Unreleased]

### Added 22-09-2026
- **Day EPG popup**: clicking any date cell in the rota grid opens a modal
  showing all scheduled team members as time-accurate horizontal bars on a
  24h+ timeline (06:00 to 08:00 next day). Overnight shifts display
  correctly across midnight. A "NOW" marker appears on the current day.
  Leave states (confirmed AL, pending AL, parental, marital) render as
  full-width labelled bars. Cell notes are flagged with a 📝 icon and
  visible on hover. No additional API requests — data sourced from the
  already-loaded schedule in memory.

### Changed
- Data Files card now covers all 13 rota-related JSON files, including
  draft_lock, print_footer, and directory_audit_log.
- Backup directory moved from ./rota/backups/ to ./rota_backup_json_files/
  (one level above ./rota/ so a rota/ wipe doesn't take backups with it).
  The legacy person_directory.backup.json self-heal file at _BASE_DIR is
  unchanged and continues to work alongside the new timestamped backups.
- directory_audit_log and draft_lock are marked download-only: upload and
  restore are blocked at both the backend (403) and hidden in the UI.
  Rationale: restoring the audit log rolls back history; restoring a draft
  lock can strand users in a phantom-locked state.
- print_footer (rota/assets/print-footer.json) added to managed files.

## [Unreleased] — Data File Management (Admin)

### What changed

Added a **Data Files** management card to the Admin tab, accessible to management users only. The feature covers three operations: download, upload-to-replace, and restore-from-backup, all gated behind the existing `_require_management()` check.

### Backend (`routes_rota.py`)

Four new routes added to `rota_bp`, placed just before `register_routes()`:

| Route | Method | Purpose |
|---|---|---|
| `/rota/admin/datafiles` | GET | List all managed files with metadata and their backup history |
| `/rota/admin/datafiles/<key>/download` | GET | Stream a live file as a download |
| `/rota/admin/datafiles/<key>/upload` | POST | Validate, backup, and replace a live file |
| `/rota/admin/datafiles/<key>/backup/<filename>/download` | GET | Download a specific backup |
| `/rota/admin/datafiles/<key>/backup/<filename>/restore` | POST | Restore a backup (snapshots the current live file first) |

**Managed file keys**: `leave_requests`, `published_overrides`, `draft_overrides`, `cell_notes`, `person_directory`, `al_allowance`, `hours_pot`, `shift_registry`, `config`, `feedback`.

**Backup storage**: `rota/backups/` subdirectory. Timestamped filenames, e.g. `person_directory_BU_2026-09-18T14-32-01Z.json`.

**Validation on upload and restore**: Each file key has a schema check (correct container type, required fields present) before any write occurs. An invalid file is rejected with a 422 and the live file is never touched.

**Side effects after write**:
- `person_directory` → calls `_rebuild_person_directory_caches()`
- `shift_registry` → calls `_rebuild_alias_cache(data)`

### Frontend (`index.html`)

**New HTML elements** (paste into `#panel-admin`, after the last existing `.admin-card`):
- `admin-card-datafiles` — the card div with a table of all managed files

**New modal** (paste near the other modals, before `</body>`):
- `restore-modal` — confirmation dialog with a data-loss warning before any restore

**New JS block** (paste at the end of the existing `<script>` block, before `</script>`):
- `_setupDatafilesCard()` — wires event listeners; called from `setupAdminTab()`
- `loadDatafiles()` — fetches and renders the file list
- `renderDatafiles(files)` — builds the table including per-file backup rows
- `_handleDatafileUpload(event, key)` — reads the picked file, confirms, POSTs to upload endpoint
- `_confirmRestore(key, backup_filename)` — opens the restore modal
- `_doRestore(key, backup_filename)` — POSTs to the restore endpoint

**One line to add to `setupAdminTab()`** — call `_setupDatafilesCard()` at the top of the function body (or anywhere inside it before `loadDatafiles()` is first triggered):

```js
_setupDatafilesCard();
```

And add this call inside the existing `if (!_adminTabReady)` block:

```js
loadDatafiles();
```

### Guardrails

- **Path traversal**: backup filenames are validated to start with the expected `<key>_BU_` prefix and contain no `/` or `\` characters.
- **Size cap**: upload body is capped at 20 MB server-side.
- **Double-snapshot on restore**: when restoring, the current live file is snapshotted first. Restoring a backup never results in data loss without a recovery path.
- **High-risk files flagged**: `leave_requests`, `published_overrides`, `person_directory`, `al_allowance`, `hours_pot` show a ⚠ marker and use a red confirm button to signal that data loss is possible if an old snapshot is restored.
- **Client-side JSON parse check**: the upload handler parses the file in the browser before sending. Invalid JSON is rejected immediately with no server round-trip.

## [Unreleased] — UI overhaul PR1: sidebar shell + light theme

### Changed 17-09-2026
- Replaced top horizontal `#topbar` + `#tab-bar` with a fixed 220px left
  sidebar (`#sidebar`), macOS Finder-style: logo/app-name header, nested
  nav (parent items expand/collapse only, no content change on parent
  click), user identity + Feedback/Sign out moved to sidebar footer.
- Retokenized `:root` to a macOS-light palette, grouped and labeled by
  purpose (NEUTRALS / BRAND / STATUS / TEAM BADGES / SPRINKLE / TYPE) for
  future tweaking without re-reading the whole stylesheet. Accent is
  `#3F1568` (deep purple).
- Draft banner now sits at the top of `#content-pane`, full width of the
  content area (not sidebar-embedded) — unchanged behaviour, new position.
- Reduced body noise-grain overlay opacity 0.35 → 0.08 (was tuned for dark
  bg, overpowered the light theme).
- Recalibrated `#rota-wrap` max-height (`calc(100vh - 230px)` →
  `calc(100vh - 140px)`) now that ~120px of sticky top chrome no longer
  sits above the content pane.
- Logo is now an `<img>` slot at `/assets/logo-placeholder.png` — shows
  broken-image icon until a PNG is supplied; deliberate placeholder.

### Not yet done (PR2, scoped separately)
- Sub-panel content splitting: Leave Approvals (Pending/History), Night &
  PH Hours (Compute/POT Consultation), My Overview (AL Allowance/SOE
  Weekend Coverage), Admin (People/Annual Leave/Feedback) all currently
  still render as one combined panel regardless of which child nav item
  was clicked — child clicks load the parent's full existing content.
  Actual show/hide-per-sub-item logic is the next pass.
- A handful of hardcoded dark-theme rgba/hex values remain in
  `.data-table`, `.pot-table`, and history-row border colors (e.g.
  `#252525`, `rgba(255,255,255,0.15)`) — not yet swept to light-theme
  equivalents. Cosmetic only, doesn't affect function.
- Mobile static-screenshot view — explicitly deferred, separate spec.

### Verification performed
- Div open/close tag balance confirmed equal (379/379).
- Full inline `<script>` block confirmed to parse as valid JS syntax
  (`new Function()` on extracted source — syntax check only, not a
  runtime/click-through test; UAT in-browser still required).
- No stray references to removed `.tab-btn` / `#topbar` / `#tab-bar`
  selectors remain anywhere in CSS, HTML, or JS.

---

## [Unreleased] — UI overhaul PR1 (cont.): rota table retheme + fonts

### Changed
- Rota table chrome (headers, date column, weekend/PH/today shading, gap
  flags, draft-mode header tint, borders) retheme dark → light. Scope
  was deliberately narrow: SHIFT_COLORS (the JS map driving actual shift
  code colors, plus OFF/ABSENT/PARENTAL/MARITAL) was left untouched per
  explicit "shift colors remain untouched" instruction — flag if
  OFF/ABSENT/PARENTAL/MARITAL should also be relit for light theme, since
  those aren't work-shift legend colors and the instruction's scope on
  them was ambiguous.
- Type system unified: --mono / --display / --numfont all now resolve to
  Inter; hierarchy comes from weight/size only, not family. Variable
  names kept as-is (legacy — "mono" doesn't mean monospace) with an
  explanatory comment rather than renaming ~106 call sites for a
  cosmetic-only gain.
- New --font-title token + @font-face for Resolve Sans (title only),
  falling back to Inter until licensed .woff2 files are supplied at
  /assets/fonts/ResolveSans-{Regular,Bold}.woff2. Resolve Sans is
  Blackmagic Design's proprietary font — NOT on any public CDN. Requires
  license confirmation for web-embed use before those files are hosted.
  Flagged explicitly; not resolved by this change.
- Google Fonts import trimmed: dropped Syne and Space Mono (both fully
  unused after the Inter consolidation — Space Mono was already dead
  weight before this pass, Syne was --display's old value). Kept Aptos
  Narrow + Roboto — both used exclusively by the print-export CSS
  (.print-title / table.print-rota), which is intentionally out of scope
  for this theme pass.
- Sidebar header text "SP SO Rota" -> "Streaming Ops Rota" (the <title>
  tag already read correctly -- only the visible sidebar label was stale).

### Verification performed
- Div balance (379/379), inline <script> syntax parse -- both hold post-edit.
- Confirmed no remaining "SP SO" string anywhere in the file.
- Confirmed print-export font-family declarations (Roboto/Aptos Narrow)
  untouched and still bypass the --mono/--display vars as designed.

### Still open
- Resolve Sans font files not supplied -- title currently renders in
  Inter (fallback) until sourced + licensed.
- Sub-panel content splitting (PR2, unchanged from prior entry).
- data-table / pot-table hardcoded dark border colors (#252525 etc.)
  not yet swept -- cosmetic only, still pending.

---

## [Unreleased] — UI overhaul PR1 (cont.): OFF/ABSENT/PARENTAL/MARITAL relight + type hierarchy

### Changed
- SHIFT_COLORS: OFF -> bg #ececec / fg #949292 (matches weekend date-col
  styling exactly). ABSENT -> same base + diagonal texture recolored to
  an intermediate gray (rgba(148,148,148,0.55)) so it reads between the
  light bg and dark label on a light table. PARENTAL and MARITAL both
  unified to bg #71efde with a dotted overlay; dotted pattern itself
  reworked (sparser, dark dots on transparent, ~7px grid) to read closer
  to Excel's light dot-fill pattern instead of the old faint white dots.
- .shift-cell font-weight 700 -> 500, to sit visually lighter than the
  bold header row (700) and bold date column (700) — this is the single
  line to touch if hierarchy needs further adjustment (index.html, rule
  `.shift-cell { font-weight: ... }`).
- Legend swatches (Rota tab, bottom) synced to match the above so the
  legend doesn't contradict the table.
- Unrelated pre-existing bug fixed opportunistically: legend's
  "Confirmed AL" swatch was #9ee6a6 (green) but SHIFT_COLORS['AL_APPROVED']
  is #FFEB3B (yellow) — legend never matched the actual cell color. Now
  synced.

### Flag — real functional consequence, not just cosmetic
- PARENTAL and MARITAL cells render as text-blank in the grid (existing
  behaviour, unchanged) — with both now sharing the identical bg color
  and dotted pattern, **they are visually indistinguishable in the rota
  table itself**. The legend has a thin border added to Marital's swatch
  to tell them apart there, but that border isn't applied to actual grid
  cells. If distinguishing Parental from Marital at a glance in the live
  table matters, this needs a follow-up (e.g. a border, different dot
  density, or a tiny corner mark) — not resolved by this change, executed
  literally per instruction as given.

[Unreleased] — UI overhaul PR1 (cont.): legend hidden, Marital recolor, draft-selection contrast
Changed
#legend block commented out (not deleted) in the Rota tab — reclaims vertical space; users already know the color scheme from the existing Excel-format rota. Swatches inside were kept in sync with SHIFT_COLORS before commenting out, so uncommenting later won't restore stale colors.
SHIFT_COLORS['MARITAL'] bg 
#71efde -> 
#ffffff (kept dotted:true) so Parental and Marital are now visually distinct in the actual grid, not just in the (now-hidden) legend.
Draft-mode selected-cell text: was forced white (#fff !important), illegible against light-theme shift colors. Changed to var(--warn) (
#e6a850) — the exact color of the selection-box border, not an approximation — plus a font-weight bump to 700 (unrequested addition, pairs with the earlier 500-weight base so selected text doesn't go thin and hard to read under the amber overlay).
Flagged, not resolved
Amber selection text against the lightest cells (OFF 
#ececec, Marital white) may still be low-contrast since the selection overlay itself is amber-tinted — same hue family as the text. Needs an actual in-browser check; if still weak, drop to a darker amber (
#8a5a00, already used for the today-row text) instead of the exact border-match color.

[Unreleased] — UI overhaul PR1 (cont.): draft-mode header contrast, layout scroll fix, toast relocation
Changed
Draft-mode header background/text: was pale amber bg (
#fff3d6/
#ffe9b8) with var(--muted) text — low contrast. Now 
#f0c876/
#e8b85c bg with an explicit dark brown text color (
#5c3d00), independent of whatever the base header's text-color token resolves to.
Layout: replaced the hardcoded #rota-wrap max-height (calc(100vh - 140px)) with proper flex distribution. Root cause of the "minor extra scroll" in draft mode: that magic number only accounted for chrome height without the draft banner, so it went stale whenever the banner appeared. Now #content-pane is a fixed-height (100vh) flex column, #panel-rota (when active) is itself a flex column filling all remaining space, #rota-toolbar/#draft-banner are flex-shrink:0, and #rota-wrap is flex:1 + min-height:0 — it now always fills exactly whatever space is actually left, banner shown or not, with no recalculation needed if chrome height changes again in future. content-pane keeps its own overflow-y:auto as a safety net for other (non-Rota) tabs whose content might exceed one viewport — untouched, not something you flagged as a problem.
Toast notifications: moved from a fixed, viewport-centered overlay (z-index 9000, sitting on top of content) into the sidebar itself — now the last child of #sidebar-nav, pinned to the bottom of the nav column via margin-top:auto (so it sits just above the footer divider regardless of exact nav-item count, no pixel-math needed). #sidebar-nav is now display:flex/flex-direction:column to make that possible. Switched white-space:nowrap -> normal since it's now width-constrained to the sidebar rather than free-floating over full page width.
Verification performed
Confirmed exactly one #toast element in the DOM (caught and fixed a duplicate-insertion mistake during editing — old fixed-position toast wasn't removed on first pass, corrected before shipping).
Div/nav tag balance, JS syntax parse — both hold.
Still outstanding, not part of this pass
The three items from the previous message (header bg 
#BDC0BF + text color, header/week-separator line, today-highlight dark-gray-bold text for working shifts) were given as instructions only, not applied to this file yet. Confirm if you want those folded in now.

## [Unreleased] — UI overhaul PR1: sidebar shell + light theme

### Changed 16-09-2026
- Replaced top horizontal `#topbar` + `#tab-bar` with a fixed 220px left
  sidebar (`#sidebar`), macOS Finder-style: logo/app-name header, nested
  nav (parent items expand/collapse only, no content change on parent
  click), user identity + Feedback/Sign out moved to sidebar footer.
- Retokenized `:root` to a macOS-light palette, grouped and labeled by
  purpose (NEUTRALS / BRAND / STATUS / TEAM BADGES / SPRINKLE / TYPE) for
  future tweaking without re-reading the whole stylesheet. Accent is
  `#3F1568` (deep purple).
- Draft banner now sits at the top of `#content-pane`, full width of the
  content area (not sidebar-embedded) — unchanged behaviour, new position.
- Reduced body noise-grain overlay opacity 0.35 → 0.08 (was tuned for dark
  bg, overpowered the light theme).
- Recalibrated `#rota-wrap` max-height (`calc(100vh - 230px)` →
  `calc(100vh - 140px)`) now that ~120px of sticky top chrome no longer
  sits above the content pane.
- Logo is now an `<img>` slot at `/assets/logo-placeholder.png` — shows
  broken-image icon until a PNG is supplied; deliberate placeholder.

### Not yet done (PR2, scoped separately)
- Sub-panel content splitting: Leave Approvals (Pending/History), Night &
  PH Hours (Compute/POT Consultation), My Overview (AL Allowance/SOE
  Weekend Coverage), Admin (People/Annual Leave/Feedback) all currently
  still render as one combined panel regardless of which child nav item
  was clicked — child clicks load the parent's full existing content.
  Actual show/hide-per-sub-item logic is the next pass.
- A handful of hardcoded dark-theme rgba/hex values remain in
  `.data-table`, `.pot-table`, and history-row border colors (e.g.
  `#252525`, `rgba(255,255,255,0.15)`) — not yet swept to light-theme
  equivalents. Cosmetic only, doesn't affect function.
- Mobile static-screenshot view — explicitly deferred, separate spec.

### Verification performed
- Div open/close tag balance confirmed equal (379/379).
- Full inline `<script>` block confirmed to parse as valid JS syntax
  (`new Function()` on extracted source — syntax check only, not a
  runtime/click-through test; UAT in-browser still required).
- No stray references to removed `.tab-btn` / `#topbar` / `#tab-bar`
  selectors remain anywhere in CSS, HTML, or JS.

### Fixed 10-09-2026
- Shift registry table: implicit shifts and entries saved with the grey
  placeholder color now display their correct default colors in the admin UI.
  Colors explicitly set by a user are never overwritten.

## [Unreleased] — Shift Registry

### Added 09-09-2026
- **`rota/shift_registry.json`** — new persistent file. Stores explicit shift definitions (code, color, fg_color, active state, aliases). Shifts present in rotation arrays but not explicitly registered are shown as "implicit" in the UI and auto-register on first edit.
- **Shift alias system** — time-gated renaming of shift codes. An alias maps `old_code → new_code` from a future `effective_from` date. `_base_shift()` now checks the alias cache before returning, so all rotation-derived cells transparently use the new code from that date without touching the rotation arrays or any historical data.
- **`_ALIAS_CACHE`** — in-memory sorted list rebuilt on every registry write and at import time. Zero overhead for days with no aliases.
- **`_resolve_alias(code, date)`** — returns the effective code for (code, date), picking the latest alias whose `effective_from ≤ date`.
- **`_alias_color_for(code, date)`** — returns (bg, fg) from the active alias, used by the frontend color map.
- **`_migrate_published_overrides_for_alias()`** — on alias creation, rewrites `published_overrides.json` entries whose `shift == old_code` and `date >= effective_from`, **only for non-manual types** (skips `shift_change`, `al_toggle`, `al_remove`). Manual overrides are left as-is.
- **Routes (all management-only)**:
  - `GET /rota/shifts` — full registry + implicit rotation shifts, annotated with rotation membership
  - `POST /rota/shifts` — add a new shift to the registry (registry only; does NOT modify rotation arrays)
  - `PUT /rota/shifts/<code>` — edit color and/or create a time alias. Color change is immediate and retroactive for rotation-derived cells. Alias is date-gated.
  - `PUT /rota/shifts/<code>/active` — toggle active/inactive (inactive = hidden from shift picker, no data deleted)
  - `DELETE /rota/shifts/<code>/alias/<alias_id>` — delete a future alias (refuses if `effective_from` is today or past)
- **Admin tab — Shifts card** (`🕐 Shifts`): registry table with color swatches, rotation membership tags, alias pills with delete, active/inactive toggle, Edit and Add flows.
- **Edit flow**: two-step modal — fields then diff summary. Diff explicitly lists what changed and what was left unchanged, with a note on scope (color = immediate all rotation cells; time = date-gated, past cells untouched, published override migration noted).
- **Add flow**: inline form with live color preview cell.

### Changed
- `_base_shift()` now runs alias resolution after computing the rotation index. `OFF` codes skip the lookup. All callers of `_base_shift()` (`_resolve_shift`, `_flanking_off_range`, `_effective_shift_for_hours`, weekend swap pattern matching) inherit alias resolution automatically.
- File paths block: added `SHIFT_REGISTRY_FILE = os.path.join(ROTA_DIR, 'shift_registry.json')`.

### Not changed (by design)
- Rotation arrays (`SPECIALIST_ROTATION`, `ENGINEERING_ROTATION`, `MANAGEMENT_SHIFTS`) are read-only from the app. Adding/removing shifts from the cycle remains a manual backend operation. The UI surfaces a clear label ("registry only") for shifts not in any rotation.
- Night hours tables (`SHIFT_NIGHT_MINUTES`, `SHIFT_TOTAL_MINUTES`, etc.) are unchanged. New/aliased codes that are not in those tables fall back to `_parse_raw_shift_minutes()` which computes all four values generically and correctly.
- Past published overrides with the old code that are typed as `shift_change` (manual human edits) are not migrated — they represent intentional overrides on specific cells.

## [Unreleased]
### Added 08-09-2026
- `/rota/next-shift` backend route — returns each person's next working shift
  (skipping OFF/AL/ABSENT/PARENTAL/MARITAL), bulk or single-person, capped at
  180 days lookahead. Staff self-only, management full roster or by `person=`.
- Overview tab redesigned: AL Allowance, Booked vs Allowance, Next Shift,
  and Next Leave now render as a card grid (`.ov-grid`/`.ov-card`) for both
  single-member and all-members views.
- All-members view now shows compact clickable tiles (`.ov-tile`) per person;
  clicking switches to that person's full single-member card view with no
  re-fetch.
- Single-member view: MHD, Base Allowance, Absence Reward, Misc Hours,
  Carry-over, and PH-on-AL Giveback are now individual cards instead of a
  bundled breakdown grid.

### Pending (next pass)
- AL monthly distribution chart (single-member view only).
- SOE Weekend Coverage card restyle to match new card language.

## [Unreleased]

### Fixed 04-09-2026
- person_directory.json missing no longer crashes the whole app process.
  Backup copy now stored at /opt/web/person_directory.backup.json
  (outside the rota/ subdirectory) so it survives a rota-scoped file
  wipe. Auto-restores from backup on boot if the primary is missing;
  falls back to an empty directory only if no backup exists either.

### Changed 03-09-2026
- **Admin tab** restructured into cards: People, Annual Leave, Feedback
- **People card**: directory table now primary view; Add Person form expands on demand; Recent Changes collapsible via button
- **Annual Leave card** (Admin, management only):
  - MHD default field with lock/unlock flow — null → integer on first entry, locked after save; unlock requires confirmation modal; past years read-only
  - Misc entries form with member dropdown; entry list shown contextually after member selection
- **Overview tab** is now fully read-only:
  - Staff: own balance card only
  - Management: individual member dropdown (default) with All Members toggle restoring team-grouped view
  - All edit controls (base allowance, MHD, misc entries) moved to Admin → Annual Leave card

### Fixed 03-09-2026
- SOE Weekend Coverage year dropdown now correctly pre-selects the current year on load.

## [Unreleased]
### Changed 03-09-2026
- SOE Weekend Coverage widget now defaults to Single Year view with the current year pre-selected, instead of Aggregate.
- Year dropdown is disabled (greyed out) when "All Years (Aggregate)" mode is selected.

### Fixed 02-09-2026
- `_flanking_off_range` caused an OverflowError when the person directory
  was empty (all shifts resolve to OFF), because the 14-day cap was measured
  from the moving boundary instead of the original date, so it never fired.
  Fixed cap calculation and added hard date bounds as a safety net.

## [Unreleased] 01-09-2026
### Fixed
- Admin tab: `setupAdminTab` was defined twice — second definition silently
  overwrote the first, breaking feedback tab wiring entirely.
- Admin tab: feedback filter controls (`admin-fb-type-filter`, etc.) were
  referenced in JS but absent from the HTML; added the missing DOM section.
- Leave history: year-filter `change` listener was only attached in the
  empty-state branch, so the dropdown did nothing when entries existed.
- routes_rota.py: removed dead `HR_CONFIG_FILE` constant left over from
  the hr_config.json → person_directory.json migration.

## [Unreleased] — Admin tab: manage the person directory from the UI 31-08-2026

### Added
- New "👥 Admin" tab (management only) — add, edit, hide/show, and delete
  people directly, instead of hand-editing `person_directory.json`.
- Backend: `GET/POST /rota/directory`, `PUT/DELETE /rota/directory/<id>`,
  `GET /rota/directory/audit`. All management-only.
- `rota/directory_audit_log.json` (new, gitignored) — every directory
  create/update/delete is logged with who, when, and before/after state.
  Not full undo, but this data drives payroll exports and shift
  computation directly, so unlike leave requests (which already have full
  history) an unreviewable edit here has a much larger blast radius.
- `active` field on directory entries. Hiding someone (`active: false`)
  drops them from `MANAGEMENT_SHIFTS`/`ENGINEERING_OFFSETS`/
  `SPECIALIST_OFFSETS` — and therefore off the rota grid and out of hour
  computation — without touching their historical records. Preferred over
  deleting.
- Directory writes take effect immediately, live, for every logged-in
  session's next request — no server restart required. The admin's own
  currently-open tabs also refresh their local roster copy right after a
  save so the change is visible without a page reload.

### Design constraint — `rota_label` is immutable after creation
Every transactional file (`leave_requests.json`, `draft_overrides.json`,
`cell_notes.json`, `published_overrides.json`) still keys on `rota_label`,
not `employee_id` (that re-key is the deferred, larger pass). If the label
could be renamed, every historical record under the old label would
silently stop resolving to a name. The edit endpoint accepts a
`rota_label` field for convenience but silently ignores changes to it —
to relabel someone, hide the old entry and create a new one under a new
label.

### Delete vs. hide
Delete is available (`DELETE /rota/directory/<id>`) but removes the
`rota_label` from every lookup — historical leave/override/note records
under that label will show no resolved name anywhere in the app afterward.
The UI warns about this before calling delete; the backend does not
cross-check usage across the other JSON files before allowing it.


### Added
- `rota/person_directory.json` (gitignored, new — sample at
  `person_directory.json.sample`, populate with real `employee_id` keys
  before deploying). Single source of truth for every person: rota label,
  full legal name, rotation group/offset/shift, HR team, SOE join date.
- `GET /rota/roster` — returns management/engineering/specialists rota
  labels in directory order. Replaces the hardcoded `MGMT_NAMES`/
  `ENG_NAMES`/specialist arrays that used to live in `index.html`.
- `full_name` field on `GET /rota/me` and `GET /rota/members` responses —
  used for the topbar and will be used for any future full-name display.

### Removed
- `EMAIL_TO_ROTA_NAME`, `SPECIALIST_OFFSETS`, `ENGINEERING_OFFSETS`,
  `MANAGEMENT_SHIFTS` as source literals — now derived at import time from
  `person_directory.json`.
- `_display_name_from_email()` — email-dot-parsing name guesser, no longer
  needed now that full names are stored explicitly.
- `_picaponto_infer_name()` and its `name_warnings`/`X-Name-Warnings`
  response header — same reason; PicaPonto export now reads `full_name`
  directly from the directory.
- `hr_config.json` and `_load_hr_config()` — fully absorbed into
  `person_directory.json` (`hr_team` field) plus a live users-API lookup.
  `_normalise_to_rota_name()` deleted — nothing left to reconcile once both
  name forms are stored explicitly.
- `MGMT_NAMES` / `ENG_NAMES` frontend literals — replaced by
  `state.roster`, fetched once at init from `/rota/roster`.
- A hardcoded `"Fernando"` spot-check name in the `/rota/hours/debug`
  endpoint — replaced with a dynamically-picked SOS specialist.

### Fixed
- `employeeID` was never actually written onto committed POT entries
  (`rota_hours_pot_commit`), so `/rota/hours/export` always read a blank
  Employee ID column regardless of what was configured. Now populated from
  the person directory at commit time.
- A pre-existing duplicate `const SHIFT_COLORS` declaration in
  `index.html` (with a stray orphaned array line left over from an earlier
  edit) — this was a hard `SyntaxError` breaking the entire page at load.
  Unrelated to the identity-data work; found and fixed while in the same
  region of the file.

### Changed
- `_check_hr_config_consistency()` no longer takes an `hr_cfg` parameter —
  reads directly from the person directory + live users API.
- `rota_hours_export` now reads member names/full names straight from the
  committed POT snapshot rather than re-deriving them, so exports can never
  drift from what was actually committed even if the directory changes
  later.

### Migration notes
- Create `rota/person_directory.json` on the server before deploying this
  version — the app now raises `RuntimeError` at import time if it's
  missing or empty (fail loud, not silent-empty-roster).
- Delete `rota/hr_config.json` — no longer read.
- `employee_id` is the canonical key. Rota-label collisions (e.g. two
  people who'd otherwise both be "Tiago") must still be manually
  disambiguated by choosing distinct `rota_label` values in the directory
  — this hasn't changed from before, it's just now config instead of code.
- Deferred, not done here: `leave_requests.json`, `draft_overrides.json`,
  `cell_notes.json`, `published_overrides.json` still key on `rota_label`
  (`person`/`name` fields), not `employee_id`. Planned as a separate,
  larger pass — see prior thread discussion.

### Added
- New **Admin** tab (management only) with a feedback reading UI.
- Feedback entries filterable by type (bug / feature / all) and status
  (unreviewed / resolved / dismissed / all). Sorted unreviewed-first.
- Resolve and Dismiss actions per entry, with confirmation modal.
- `PUT /rota/feedback/<id>` endpoint to transition feedback status.
- Feedback entries now stored with `status`, `actioned_by`, `actioned_at` fields.
  Existing entries without a status field will appear as unreviewed (frontend
  falls back gracefully via the sort/filter logic).

### Added
- My Leave History (staff view): year dropdown filter, defaulting to current year. "All years" option available. Year selection is preserved across tab re-visits within the same session.
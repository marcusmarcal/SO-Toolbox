# Changelog — Rota App

## [Unreleased]

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
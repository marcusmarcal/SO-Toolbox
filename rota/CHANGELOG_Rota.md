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
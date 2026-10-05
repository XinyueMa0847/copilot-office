# Copilot Office dashboard

This read-only dashboard combines an office's declared records with an
incremental index of Copilot CLI session logs.

## Views

- **Office** shows open and recent sessions, delegation counts, usage, and a
  floor plan built from stable employee IDs. The Lead and Guardian have private
  offices; Builder/Reviewer, Results Analyst/Code Reader, and
  Researcher/Docs Curator share pods; the configured front desk is at the
  entrance. Names and titles come from `staff.json` and `dashboard.json`.
- **Tasks** groups session activity by week, UTC credit month, and local day.
  Expanding a row shows delegation chains, prompts, status, model, timing, and
  available accounting fields. A session or delegation is activity, not proof
  of a completed deliverable.
- **Runs** summarizes user-launched runs declared in `run-logs/registry.json`
  and their optional notes and health logs. An empty registry displays setup
  guidance from `run-logs/README.md`; the dashboard never launches a run.

Delegation status is inferred from session events. A completion or
`agent_idle` notification ends a turn. A start, resume, or shutdown boundary
ends prior background-agent activity. “Running” also requires a live session
lock, so it remains an index signal rather than a process heartbeat.

Weeks start Monday and days use `dashboard.json`'s IANA timezone. Credit months
always start at 00:00 UTC on the first day of the month. Log usage is computed
with running-max semantics so a decreasing checkpoint does not subtract usage.

## Configuration

The dashboard consumes the files created by `office.py init`:

- `.agent-office/project.json`, `staff.json`, `tasks.json`, and
  `dashboard.json`
- optional state, decisions, starting brief, and generated-profile manifest
- `.github/agents/<staff-name>.agent.md`
- `run-logs/registry.json`, README, and optional run notes/health logs

Only the front desk named by `dashboard.json` is read from
`~/.copilot/agents/<name>.agent.md`; other user-level agents are not scanned.
Session logs default to `~/.copilot/session-state`. The incremental cache is
stored in `<home>/.agent-office/.dashboard-cache/`, never in this repository.

## Serve

From the repository root:

```sh
python3 dashboard/server.py --office HOME
python3 dashboard/server.py --office HOME --port 8765 --sessions SESSION_DIR
python3 dashboard/server.py --office HOME --allowed-host EXACT_HOST
```

`OFFICE_HOME` may replace `--office`. The server binds only to `127.0.0.1`.
Forward the chosen port privately.

For a records-only offline copy:

```sh
python3 dashboard/server.py --office HOME --embed-snapshot
python3 dashboard/server.py --office HOME --embed-snapshot --sessions SESSION_DIR
```

This rewrites only `index.html`'s embedded JSON block. The public repository
ships with that block set to `null`; do not commit a private snapshot.

Inspect the session index directly:

```sh
python3 dashboard/session_index.py --office HOME --summary
python3 dashboard/session_index.py --office HOME --sessions SESSION_DIR --summary
```

## Credit calibration

Before calibration, the page shows account-wide log units and all estimated
AIC fields are `null`. Read the current monthly used-AIC value from `/usage`,
then run:

```sh
python3 dashboard/session_index.py --office HOME --calibrate USED_AIC
python3 dashboard/session_index.py --office HOME --sessions SESSION_DIR --calibrate USED_AIC --plan PLAN_AIC
```

Calibration sums the current UTC month's log units across **all** sessions in
the selected session directory, not only sessions associated with this office.
It atomically stores the factor and measurement metadata in
`.agent-office/dashboard.json`. Calibration fails clearly when the month has no
log usage. `office.py calibrate --used N` wraps this command.

## Browser-local customization

Avatar uploads and session renames stay in browser `localStorage`:

```text
copilot-office:<project_id>:avatar:v1:<employee_id>
copilot-office:<project_id>:title:v1:<session-id>
```

They are never sent to the server and do not rename Copilot sessions. No
character artwork is included.

## Security boundary

- loopback-only HTTP listener with strict Host/Origin checks
- GET/HEAD only; no mutation or command endpoints
- no symlink traversal
- evidence served only from allowlisted office-relative roots and text types
- front-desk definition is parsed for display but never served
- bounded file sizes, source count, health-log tail, and displayed field sizes

The UI uses text nodes for record content and has no network dependencies.
Keep the server and any forwarded port private.

## Tests

```sh
cd dashboard
python3 -B -m unittest test_server test_session_index -q
NODE=$(ls /path/to/editor/servers/*/server/node | head -1)
"$NODE" validate.mjs
```

All tests and client validation create temporary office/session fixtures.
They do not require or copy real records from the user's home directory.

# Example office: Tiny Search

A demo office for a made-up project, so you can see the dashboard populated
and read real rendered agent files before setting up your own. Everything
here is fictional: the project, records, runs and session logs.

```sh
python3 example/generate.py     # refresh the demo so its dates end today
python3 office.py dashboard --home example/demo-office --sessions example/session-state
```

Then open http://127.0.0.1:8765. If that port is busy, add `--port 8766` to the
dashboard command and open that port instead.

What's here:

- `demo-office/`: an office made with
  `office.py init --theme pokemon`, plus invented records:
  - `.agent-office/`: project state, decisions, tasks, staff, dashboard settings.
  - `.github/agents/`: the eight named agents (Bidoof, Abra, …) rendered from
    `roles/`, with each one's binding to this project. Read these to see what
    each agent is told and how they hand work to each other.
  - `run-logs/`: four runs (one still running) with health logs and notes.
- `session-state/`: synthetic Copilot CLI session logs in the same format as
  `~/.copilot/session-state`, so the Office and Tasks tabs have activity.
- `generate.py`: rebuilds both from scratch.

The demo isn't installed into `~/.copilot/agents`; it's only for viewing.
To start your own office, follow the setup guides in the repository root.

`office.py doctor` reports the demo's agent files as stale because their
absolute paths were replaced with `~/offices/tiny-search` for publishing. To
try the demo agents for real, run `python3 office.py render --home
example/demo-office` first; that writes your own paths back in.

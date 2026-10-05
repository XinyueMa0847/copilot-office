# copilot-office

A small "office" of GitHub Copilot CLI custom agents that work on one project
together, with shared project records and a local dashboard to watch them.

- **You talk to one Manager.** It owns direction and delivery, keeps the project
  records, and hands focused work to specialists.
- **Specialists do focused jobs:** Builder (implement, test, run), Reviewer
  (independent code review), Results Analyst (compare runs, check evidence),
  Code Reader (trace and explain code, read-only), Researcher (cited external
  research), Docs Curator (guides and runbooks).
- **A Counselor gives second opinions.** When you ask the Manager to "double
  check", it brings plans, numbers and reports to the Counselor instead of
  re-reading its own work.
- **A Front Desk finds your sessions.** Describe the conversation you want
  ("yesterday's analysis") and it hands back the exact `copilot --resume`
  command.
- **A dashboard shows the office.** A floor plan of who is working on what,
  task chains (you → Manager → specialists) by day, week and month, estimated
  AI-credit use, and your experiment runs. It runs locally on 127.0.0.1.

The role definitions in [`roles/`](roles) are the core of the project: what
each role does, when the Manager delegates, how reviews and second opinions
work, and the safety rules they share. Read them and adapt them.

## Characters

Agents get character names from a theme. Three are included:

| Role | Pokémon | Star Trek: TNG | Animal Crossing |
|---|---|---|---|
| Manager | Bidoof | Picard | Nook |
| Counselor | Abra | Troi | Katrina |
| Builder | Dratini | Laforge | Cyrus |
| Reviewer | Ivysaur | Worf | Reese |
| Results Analyst | Furret | Crusher | Blathers |
| Code Reader | Eevee | Data | Wilbur |
| Researcher | Lapras | Guinan | Gulliver |
| Docs Curator | Joltik | Barclay | Pelly |
| Front Desk | Rotom | Obrien | Isabelle |

In commands and file names, agent names are lowercase (`copilot --agent
bidoof`). Only names are included, no artwork. The dashboard shows initials until you
upload your own avatar images; they stay in your browser. Character names
belong to their owners and are used here as fan labels. See
[`themes/README.md`](themes/README.md) to switch themes or make your own.

## Requirements

- GitHub Copilot CLI with custom agents (`copilot --agent NAME`).
- Python 3.10 or newer (standard library only).
- Linux or macOS. On a remote machine, VS Code port forwarding lets you open
  the dashboard in your local browser.

The role files name default models. If you don't have access to one, change
the `model:` line in the role file, or set per-agent models with `/subagents`.
Each office's agent names are global in `~/.copilot/agents`, so use a
different theme (or your own names) for each office.

## Set up

Pick one:

- **Let Copilot do it:** clone this repo, start `copilot` in it and say:
  "Read SETUP_WITH_COPILOT.md and set up my office." See
  [SETUP_WITH_COPILOT.md](SETUP_WITH_COPILOT.md).
- **Do it yourself:** follow [SETUP_MANUAL.md](SETUP_MANUAL.md) (about ten
  commands).

## Branches

- `main`: templates only. A new office starts empty: no tasks, no runs.
- `example`: a demo office with made-up project data and session logs, so you
  can see the dashboard populated before you set up your own.

## See the example

```sh
git clone --branch example https://github.com/XinyueMa0847/copilot-office.git
cd copilot-office
python3 example/generate.py      # moves the demo's dates up to today
python3 office.py dashboard --home example/demo-office --sessions example/session-state
```

Open http://127.0.0.1:8765. If that port is busy, add `--port 8766` and open
that port instead. On a remote machine, forward the port in VS Code (Ports tab).
See [example/README.md](https://github.com/XinyueMa0847/copilot-office/tree/example/example)
for what the demo contains.

## Privacy

Everything stays on your machine. The dashboard reads your local project
records and Copilot CLI session logs (`~/.copilot/session-state`), serves them
only on 127.0.0.1 and has no write endpoints. Avatars and renamed task titles
are stored in your browser. Do not expose the dashboard port publicly.

## Layout

```
office.py          setup and helper commands (init, install, render, doctor, calibrate, dashboard)
roles/             the role definitions (the office's operating manual)
themes/            character names per theme
templates/         starting project records
frontdesk/         session finder used by the Front Desk agent
dashboard/         local dashboard (server, session-log indexer, single-file page)
```

## License

MIT. See [LICENSE](LICENSE).

Not affiliated with or endorsed by GitHub or Microsoft. GitHub Copilot is a
trademark of GitHub, Inc. Character names belong to their owners: Pokémon
(Nintendo / Creatures / GAME FREAK), Star Trek (CBS Studios / Paramount) and
Animal Crossing (Nintendo).

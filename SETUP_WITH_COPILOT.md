# Setup guide for Copilot

The user wants a copilot-office set up. Follow these steps in order. This
repository is the current directory. Keep the user informed in short
messages and ask before anything that writes outside the new office folder.

## Rules

- Do not log in, authenticate, push or publish anything. If a step needs a
  login, stop and tell the user.
- Do not install packages. Everything here uses the Python standard library.
- Never overwrite an existing file in `~/.copilot/agents` without asking.
- If the office folder already exists, don't re-run `init --force`; use
  `python3 office.py render --home <folder>` to regenerate the agents.
- Agent names are global in `~/.copilot/agents`: if the user already has an
  office with the chosen theme, suggest a different theme.
  `office.py install` refuses by default; only pass `--force` if the user
  agrees after seeing which files differ.
- Run every command from this repository's root.

## 1. Check the environment

```sh
python3 office.py doctor
```

Report anything it flags. Python 3.10+ is required. If `copilot` is not on
PATH the office still installs, but tell the user.

## 2. Ask the user four questions

Ask them together and wait for the answers:

1. **Project name**, e.g. "Recipe App". Derive a project ID from it
   (lowercase, hyphens), e.g. `recipe-app`, and show it to the user.
2. **Office folder**, where the project records will live. Suggest
   `~/offices/<project-id>`.
3. **Theme**: `pokemon`, `startrek` or `animalcrossing`. Show the names from
   the table in README.md.
4. **Time zone** for days and weeks on the dashboard. Suggest the machine's
   zone (`office.py init` detects it if you omit `--timezone`).

## 3. Create the office

```sh
python3 office.py init --home <folder> --project-id <id> --name "<name>" --theme <theme> [--timezone <zone>]
```

## 4. Install the agents

Explain first: this copies the eight role templates (`office-*.agent.md`) and
the Front Desk agent into `~/.copilot/agents`, and links the eight named
agents there so `copilot --agent <name>` works from any folder. Wait for the
user's yes, then run:

```sh
python3 office.py install --home <folder>
```

If it refuses because a file already exists and differs, show the user the
file names and ask whether to keep theirs or replace them (`--force`).

## 5. Optional: calibrate AI credits

The dashboard estimates AI-credit use from session logs. To show real numbers,
ask the user to run `/usage` in a Copilot CLI session and tell you the credits
used this month and the plan size (e.g. 12,000 of 300,000). Then:

```sh
python3 office.py calibrate --home <folder> --used <used> --plan <plan>
```

Skip this if the user doesn't want it; the dashboard then shows raw log units.

## 6. Start the dashboard

```sh
python3 office.py dashboard --home <folder> --port 8765
```

It runs in the foreground. Ask the user before starting it as a background
process; otherwise give them the command to run in their own terminal. Tell the user to open
http://127.0.0.1:8765, or, on a remote machine, to forward port 8765 in VS
Code (Ports tab → Forward a Port → 8765, visibility Private).

## 7. Hand over

Tell the user:

- How to start work: `copilot --agent <manager name>` (from `init`'s output),
  then describe the project goal. The Manager records it and delegates.
- How to find old sessions: `copilot --agent <front desk name>`.
- Avatars: in the dashboard, click a person and use "Upload avatar"; images
  stay in the browser.
- To switch theme or rename someone later: see `themes/README.md`.

# Manual setup

About ten minutes. Run everything from the repository root.

## 1. Check your environment

```sh
python3 office.py doctor
```

You need Python 3.10+ and GitHub Copilot CLI (`copilot`).

## 2. Create your office

Choose a project ID (lowercase, hyphens), a display name, a folder for the
project records, and a theme (`pokemon`, `startrek` or `animalcrossing`):

```sh
python3 office.py init --home ~/offices/recipe-app --project-id recipe-app \
  --name "Recipe App" --theme pokemon
```

This creates:

```
~/offices/recipe-app/
  .agent-office/     project.json, staff.json, tasks.json, PROJECT_STATE.md,
                     DECISIONS.md, STARTING_BRIEF.md, dashboard.json
  .github/agents/    the eight named agents (bidoof.agent.md, abra.agent.md, …)
  run-logs/          registry.json (empty) and README.md
  reports/  meeting-notes/
```

Days and weeks use your machine's time zone; pass `--timezone
America/New_York` (any IANA zone) to choose another.

## 3. Install the agents

```sh
python3 office.py install --home ~/offices/recipe-app
```

This copies the role templates and the Front Desk agent into
`~/.copilot/agents` and links the named agents there. It never overwrites a
different existing file unless you add `--force`.

Check:

```sh
python3 office.py doctor --home ~/offices/recipe-app
```

## 4. Start working

```sh
copilot --agent bidoof        # your Manager (name depends on the theme)
```

Tell it the project goal. It records the goal in `.agent-office/` and
delegates to the others. To find and resume an older conversation:

```sh
copilot --agent rotom         # your Front Desk
```

## 5. Open the dashboard

```sh
python3 office.py dashboard --home ~/offices/recipe-app --port 8765
```

Open http://127.0.0.1:8765. On a remote machine, forward port 8765 in VS Code
(Ports tab → Forward a Port, visibility Private). To keep it running after you
close the terminal, start it with `nohup … &` or in `tmux`.

## 6. Optional: real AI-credit numbers

Run `/usage` in Copilot CLI and note the credits used this month and your plan
size, then:

```sh
python3 office.py calibrate --home ~/offices/recipe-app --used 12000 --plan 300000
```

Re-run it occasionally; the estimate drifts as your mix of models changes.

## 7. Make it yours

- **Avatars:** click a person in the dashboard → Upload avatar (PNG, JPEG or
  WebP). Images stay in your browser.
- **Theme or names:** see [themes/README.md](themes/README.md), then run
  `python3 office.py render --home …` and `python3 office.py install --home …`.
- **Roles:** edit `roles/*.agent.md` (models, tools, rules), then re-run
  `install`. It updates the copies it installed; if you edited a copy in
  `~/.copilot/agents` directly, it refuses and lists the file (`--force`
  replaces it).
- **Runs:** list your experiment runs in `run-logs/registry.json` to see them
  in the Runs tab; see `run-logs/README.md` in your office folder.

# Name themes

Themes provide short fan-label names for the eight project employees and the
office-wide front desk. They do not change any role's responsibilities,
permissions, tools, or model defaults. Names and trademarks belong to their
respective owners; these files contain names only and imply no affiliation or
endorsement. No images or artwork are included.

## Pick a built-in theme

Pass one of `pokemon`, `startrek`, or `animalcrossing` to `office.py init`:

```sh
python3 office.py init --home /path/to/project --project-id demo \
  --name "Demo" --theme startrek
```

The dashboard reads the selected names from `.agent-office/staff.json` and the
front-desk name from `.agent-office/dashboard.json`. Avatars are not bundled;
users upload their own in the dashboard.

## Make a theme

A theme is a JSON object with this shape:

```json
{
  "schema_version": 1,
  "id": "example",
  "label": "Example",
  "note": "Origin and ownership note.",
  "names": {
    "lead": "leader",
    "guardian": "guide",
    "builder": "builder",
    "reviewer": "check",
    "analyst": "analyst",
    "navigator": "mapper",
    "researcher": "scout",
    "curator": "docs",
    "frontdesk": "welcome"
  }
}
```

Every name must contain only lowercase `a`–`z`, be 2–10 characters long, be
unique, and have a unique first letter. A real custom theme should also avoid
command/package collisions and explain any third-party ownership in `note`.
Built-in theme IDs are currently fixed by the command-line choices; to add one,
add its JSON file and register its ID in `office.py`.

## Switch later

1. Back up or commit the current project records.
2. Edit `.agent-office/staff.json`: change `theme`, and update each employee's
   `name` and `profile` to the new mapping while preserving `employee_id`,
   `role_profile`, title, sessions, and assignments.
3. Edit `.agent-office/dashboard.json` and set `frontdesk.name`.
4. Run `python3 /path/to/copilot-office/office.py render --home /path/to/project`.
5. Remove the old `~/.copilot/agents/<old>.agent.md` links/files, then run
   `office.py install` again. If a new name conflicts with another reviewed
   installed target, use `--force` intentionally to replace that target.

`render` safely removes a renamed generated profile only when it still matches
the prior manifest. It refuses to erase a manually edited generated file.

Named agents are installed globally in `~/.copilot/agents`, not namespaced by
office. Two installed offices therefore cannot share employee or front-desk
names. Choose a different built-in theme, or use unique custom names for each
office, before installing both.

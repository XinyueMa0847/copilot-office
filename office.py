#!/usr/bin/env python3
"""Create, render, install, and check a Copilot Office."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT = Path(__file__).resolve().parent
ROLES_DIR = ROOT / "roles"
THEMES_DIR = ROOT / "themes"
TEMPLATES_DIR = ROOT / "templates"
INSTALL_MANIFEST_NAME = ".copilot-office-installed.json"

EMPLOYEES = (
    ("lead", "office-lead", "Manager", ["lead", "manager"]),
    ("guardian", "office-guardian", "Counselor", ["guardian", "counselor"]),
    ("builder", "office-builder", "Builder", ["builder"]),
    ("reviewer", "office-reviewer", "Reviewer", ["reviewer"]),
    ("analyst", "office-analyst", "Results Analyst", ["analyst", "results analyst"]),
    ("navigator", "office-navigator", "Code Reader", ["navigator", "code reader"]),
    ("researcher", "office-researcher", "Researcher", ["researcher"]),
    ("curator", "office-curator", "Docs Curator", ["curator", "docs curator"]),
)
EMPLOYEE_IDS = tuple(item[0] for item in EMPLOYEES)
THEME_IDS = ("pokemon", "startrek", "animalcrossing")
PROJECT_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
NAME_RE = re.compile(r"[a-z]{2,10}")


class OfficeError(ValueError):
    """A user-actionable office configuration error."""


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OfficeError(f"cannot read JSON from {path}: {error}") from error


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, value) -> None:
    write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def validate_theme(theme: dict, source: str = "theme") -> dict:
    if theme.get("schema_version") != 1:
        raise OfficeError(f"{source}: schema_version must be 1")
    theme_id = theme.get("id")
    if not isinstance(theme_id, str) or not re.fullmatch(r"[a-z]{2,32}", theme_id):
        raise OfficeError(f"{source}: id must contain lowercase ASCII letters")
    if not isinstance(theme.get("label"), str) or not theme["label"].strip():
        raise OfficeError(f"{source}: label is required")
    if not isinstance(theme.get("note"), str) or not theme["note"].strip():
        raise OfficeError(f"{source}: note is required")
    names = theme.get("names")
    if not isinstance(names, dict) or set(names) != set(EMPLOYEE_IDS) | {"frontdesk"}:
        raise OfficeError(
            f"{source}: names must contain exactly {', '.join(EMPLOYEE_IDS)}, frontdesk"
        )
    values = list(names.values())
    for employee_id, name in names.items():
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise OfficeError(
                f"{source}: name for {employee_id} must contain 2-10 lowercase ASCII letters"
            )
    if len(values) != len(set(values)):
        raise OfficeError(f"{source}: names must be unique")
    initials = [name[0] for name in values]
    if len(initials) != len(set(initials)):
        raise OfficeError(f"{source}: names must have unique initials")
    return theme


def load_theme(theme_id: str) -> dict:
    if theme_id not in THEME_IDS:
        raise OfficeError(f"unknown built-in theme: {theme_id}")
    theme = validate_theme(read_json(THEMES_DIR / f"{theme_id}.json"), theme_id)
    if theme["id"] != theme_id:
        raise OfficeError(f"{theme_id}: file id does not match its filename")
    return theme


def detect_timezone() -> str:
    candidates = []
    if os.environ.get("TZ"):
        candidates.append(os.environ["TZ"].lstrip(":"))
    try:
        target = Path("/etc/localtime").resolve()
        parts = target.parts
        if "zoneinfo" in parts:
            candidates.append("/".join(parts[parts.index("zoneinfo") + 1 :]))
    except OSError:
        pass
    try:
        candidates.append(Path("/etc/timezone").read_text(encoding="utf-8").strip())
    except OSError:
        pass
    tzinfo = datetime.now().astimezone().tzinfo
    if getattr(tzinfo, "key", None):
        candidates.append(tzinfo.key)
    for candidate in candidates:
        try:
            if candidate:
                ZoneInfo(candidate)
                return candidate
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return "UTC"


def validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise OfficeError(f"unknown IANA timezone: {value}") from error
    return value


def parse_frontmatter(text: str, source: str = "profile") -> tuple[dict, str]:
    """Parse the flat YAML subset used by the bundled agent profiles."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise OfficeError(f"{source}: missing frontmatter")
    closing = next(
        (index for index, line in enumerate(lines[1:], 1) if line.rstrip("\r\n") == "---"),
        None,
    )
    if closing is None:
        raise OfficeError(f"{source}: unterminated frontmatter")
    header = {}
    for raw in lines[1:closing]:
        line = raw.rstrip("\r\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1].isspace() or ":" not in line:
            raise OfficeError(f"{source}: unsupported frontmatter line: {line!r}")
        key, raw_value = line.split(":", 1)
        key, raw_value = key.strip(), raw_value.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", key) or not raw_value:
            raise OfficeError(f"{source}: invalid frontmatter line: {line!r}")
        if raw_value in ("true", "false"):
            value = raw_value == "true"
        elif raw_value.startswith(('"', "[", "{")):
            try:
                value = json.loads(raw_value)
            except json.JSONDecodeError as error:
                raise OfficeError(f"{source}: unsupported value for {key}") from error
        else:
            value = raw_value
        header[key] = value
    return header, "".join(lines[closing + 1 :]).lstrip("\r\n")


def dump_frontmatter(header: dict, body: str) -> str:
    rows = ["---"]
    for key, value in header.items():
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, str):
            rendered = json.dumps(value, ensure_ascii=False)
        else:
            rendered = json.dumps(value, ensure_ascii=False)
        rows.append(f"{key}: {rendered}")
    rows.extend(("---", "", body.rstrip() + "\n"))
    return "\n".join(rows)


def replace_roles(value, mapping: dict[str, str]):
    if isinstance(value, str):
        return re.sub(
            r"\boffice-[a-z]+\b",
            lambda match: mapping.get(match.group(0), match.group(0)),
            value,
        )
    if isinstance(value, list):
        return [replace_roles(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: replace_roles(item, mapping) for key, item in value.items()}
    return value


def project_paths(home: Path) -> tuple[Path, Path]:
    home = home.expanduser().resolve()
    return home, home / ".agent-office"


def load_office(home: Path) -> tuple[Path, Path, dict, dict]:
    home, records = project_paths(home)
    project = read_json(records / "project.json")
    staff_name = project.get("records", {}).get("staff")
    if not isinstance(staff_name, str):
        raise OfficeError("project.json does not declare records.staff")
    staff = read_json(records / staff_name)
    validate_staff(project, staff)
    return home, records, project, staff


def validate_staff(project: dict, staff: dict) -> dict[str, str]:
    project_id = project.get("project_id")
    if not isinstance(project_id, str) or staff.get("project_id") != project_id:
        raise OfficeError("project and staff project_id values must match")
    if not isinstance(project.get("display_name"), str) or not project["display_name"].strip():
        raise OfficeError("project display_name is required")
    theme_id = staff.get("theme")
    if not isinstance(theme_id, str):
        raise OfficeError("staff theme is required")
    load_theme(theme_id)
    employees = staff.get("employees")
    if not isinstance(employees, list) or len(employees) != len(EMPLOYEES):
        raise OfficeError("staff must contain the eight fixed employees")
    expected = {item[0]: item for item in EMPLOYEES}
    seen_ids, names, initials, aliases = set(), set(), set(), set()
    mapping = {}
    for employee in employees:
        if not isinstance(employee, dict):
            raise OfficeError("every staff employee must be an object")
        employee_id = employee.get("employee_id")
        if employee_id not in expected or employee_id in seen_ids:
            raise OfficeError(f"unknown or duplicate employee_id: {employee_id!r}")
        _, role, title, _ = expected[employee_id]
        if employee.get("role_profile") != role or employee.get("title") != title:
            raise OfficeError(f"fixed role_profile/title changed for {employee_id}")
        name = employee.get("name")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise OfficeError(f"invalid employee name for {employee_id}: {name!r}")
        if employee.get("profile") != name:
            raise OfficeError(f"profile must match name for {employee_id}")
        if name in names:
            raise OfficeError(f"duplicate employee name: {name}")
        if name[0] in initials:
            raise OfficeError(f"employee names must have unique initials: {name}")
        own_aliases = employee.get("aliases")
        if not isinstance(own_aliases, list) or any(
            not isinstance(alias, str) or not alias.strip() for alias in own_aliases
        ):
            raise OfficeError(f"aliases must be non-empty strings for {employee_id}")
        normalized = [name.casefold()] + [alias.casefold() for alias in own_aliases]
        if len(normalized) != len(set(normalized)) or aliases.intersection(normalized):
            raise OfficeError(f"ambiguous staff alias for {name}")
        for key in ("sessions", "assignments"):
            if not isinstance(employee.get(key), list):
                raise OfficeError(f"{key} must be a list for {employee_id}")
        seen_ids.add(employee_id)
        names.add(name)
        initials.add(name[0])
        aliases.update(normalized)
        mapping[role] = name
    if seen_ids != set(expected):
        raise OfficeError("staff employee set is incomplete")
    return mapping


def employee_binding(employee: dict, project: dict, binding: Path, mapping: dict) -> str:
    name = employee["name"]
    label = f"{name} ({project['display_name']} - {employee['title']})"
    lead = mapping["office-lead"]
    guardian = mapping["office-guardian"]
    return f"""# Employee binding

You are **{label}**, employee ID `{employee['employee_id']}`, project ID
`{project['project_id']}`. Your reusable role is `{employee['role_profile']}`.

At session start and before a new assignment, read the explicit binding:
`{binding}`.
Then load its staff directory, project state, decisions, relevant task records,
and starting brief when declared in the binding. Resolve record paths relative
to the binding. Do not infer a different project from the current directory or
automatic memory. Flag a conflicting binding before acting.

The durable project root is `{binding.parent.parent}`. Follow the binding's
current authorized workspace roots. A reference checkout is not automatically
authorized for edits, execution, or runs.

When `repository.status` is not `ready`, repository implementation and
experiments are blocked. Only office administration/research and an explicitly
started repository-preparation task may proceed within their approved scope. A
pending task in the records is not permission to start it when a session opens.
Do not create a fork, change remotes, or install tools merely to greet the user
or report staff status.

Use names and role aliases from the staff directory. "Ask", "connect", and
"status" are distinct requests, not proof of delivery, a live session, or
continuous monitoring. Names are labels, not role descriptions or personality
instructions. Keep the role boundaries below unchanged.

Starting rule: **next working milestone -> demonstrated blocker -> smallest
necessary action**. Get the selected minimal pipeline working first; defer
unused dependencies, speculative repairs, broad repeated checks, and
nonessential polish. Retain mandatory safety and owned cleanup.
{lead} is the user-facing owner of direction and delivery; {guardian} is an
optional second opinion, not a gate. Guardian advice is not execution approval;
completed budgets stay completed.

<!-- Generated by office.py from the staff directory and canonical role.
Edit the source records/profile, then run office.py render; do not edit this copy. -->

"""


def render_profile(
    source: str, employee: dict, project: dict, binding: Path, mapping: dict[str, str]
) -> str:
    header, body = parse_frontmatter(source, employee["role_profile"])
    if header.get("name") != employee["role_profile"]:
        raise OfficeError(f"source identity does not match {employee['role_profile']}")
    original_description = header.get("description")
    if not isinstance(original_description, str):
        raise OfficeError(f"description missing from {employee['role_profile']}")
    header = replace_roles(header, mapping)
    body = replace_roles(body, mapping)
    label = f"{employee['name']} ({project['display_name']} - {employee['title']})"
    header["name"] = employee["name"]
    header["description"] = f"{label}. {original_description}"
    header["disable-model-invocation"] = False
    return dump_frontmatter(
        header, employee_binding(employee, project, binding, mapping) + body
    )


def _safe_generated_path(home: Path, relative: str) -> Path:
    path = home / relative
    try:
        path.resolve().relative_to(home)
    except (OSError, ValueError) as error:
        raise OfficeError(f"generated path escapes the project home: {relative}") from error
    return path


def render_home(home: Path, *, check: bool = False, force: bool = False) -> list[str]:
    home, records, project, staff = load_office(home)
    binding = (records / "project.json").resolve()
    mapping = validate_staff(project, staff)
    generated_dir = home / ".github" / "agents"
    if generated_dir.is_symlink():
        raise OfficeError(f"generated directory may not be a symlink: {generated_dir}")
    manifest_path = records / "generated-profiles.json"
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        if manifest.get("schema_version") != 1 or not isinstance(
            manifest.get("outputs"), dict
        ):
            if not force:
                raise OfficeError("generated-profiles.json does not belong to this project")
            manifest = {
                "schema_version": 1,
                "project_id": project["project_id"],
                "outputs": {},
            }
        if manifest.get("project_id") != project["project_id"]:
            if not force:
                raise OfficeError("generated-profiles.json belongs to a different project")
            manifest["project_id"] = project["project_id"]
    else:
        manifest = {
            "schema_version": 1,
            "project_id": project["project_id"],
            "outputs": {},
        }
    previous = manifest["outputs"]
    planned = {}
    for employee in staff["employees"]:
        role, name = employee["role_profile"], employee["name"]
        source_path = ROLES_DIR / f"{role}.agent.md"
        try:
            source = source_path.read_text(encoding="utf-8")
        except OSError as error:
            raise OfficeError(f"missing canonical role: {source_path}") from error
        content = render_profile(source, employee, project, binding, mapping)
        relative = f".github/agents/{name}.agent.md"
        target = _safe_generated_path(home, relative)
        entry = {
            "relative_path": relative,
            "role_profile": role,
            "source_sha256": sha256_text(source),
            "sha256": sha256_text(content),
        }
        old = previous.get(name)
        if target.is_symlink():
            raise OfficeError(f"refusing to replace generated-profile symlink: {target}")
        if target.exists():
            actual = sha256_text(target.read_text(encoding="utf-8"))
            tracked = isinstance(old, dict) and actual == old.get("sha256")
            if not force and not tracked and actual != entry["sha256"]:
                raise OfficeError(f"refusing to overwrite untracked or edited profile: {target}")
        if check and (
            not target.is_file()
            or target.read_text(encoding="utf-8") != content
            or old != entry
        ):
            raise OfficeError(f"generated profile is missing or stale: {target}")
        planned[name] = (target, content, entry)

    stale_names = set(previous) - set(planned)
    for name in stale_names:
        old = previous[name]
        if not isinstance(old, dict) or not isinstance(old.get("relative_path"), str):
            raise OfficeError(f"invalid old manifest entry for {name}")
        old_path = _safe_generated_path(home, old["relative_path"])
        if old_path.exists() or old_path.is_symlink():
            if old_path.is_symlink():
                raise OfficeError(f"refusing to remove stale symlink: {old_path}")
            actual = sha256_text(old_path.read_text(encoding="utf-8"))
            if not force and actual != old.get("sha256"):
                raise OfficeError(f"refusing to remove edited stale profile: {old_path}")
            if check:
                raise OfficeError(f"stale generated profile remains: {old_path}")

    if check:
        return sorted(planned)
    generated_dir.mkdir(parents=True, exist_ok=True)
    for name in stale_names:
        old_path = _safe_generated_path(home, previous[name]["relative_path"])
        if old_path.exists():
            old_path.unlink()
    outputs = {}
    for name, (target, content, entry) in planned.items():
        if not target.exists() or target.read_text(encoding="utf-8") != content:
            write_text(target, content)
        outputs[name] = entry
    manifest["outputs"] = outputs
    write_json(manifest_path, manifest)
    return sorted(planned)


def project_record(project_id: str, display_name: str) -> dict:
    return {
        "schema_version": 1,
        "project_id": project_id,
        "display_name": display_name,
        "status": "active",
        "records": {
            "state": "PROJECT_STATE.md",
            "decisions": "DECISIONS.md",
            "staff": "staff.json",
            "tasks": "tasks.json",
        },
        "repository": {"status": "not_configured", "readiness_scope": ""},
    }


def staff_record(project_id: str, theme: dict) -> dict:
    names = theme["names"]
    employees = []
    for employee_id, role, title, aliases in EMPLOYEES:
        name = names[employee_id]
        employees.append(
            {
                "employee_id": employee_id,
                "role_profile": role,
                "profile": name,
                "name": name,
                "title": title,
                "aliases": aliases,
                "sessions": [],
                "assignments": [],
            }
        )
    return {
        "schema_version": 1,
        "project_id": project_id,
        "theme": theme["id"],
        "employees": employees,
    }


def render_template(filename: str, replacements: dict[str, str] | None = None) -> str:
    path = TEMPLATES_DIR / filename
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise OfficeError(f"missing markdown template: {path}") from error
    for key, value in (replacements or {}).items():
        content = content.replace("{{" + key + "}}", value)
    unresolved = sorted(set(re.findall(r"\{\{([A-Z0-9_]+)\}\}", content)))
    if unresolved:
        raise OfficeError(
            f"{filename}: unresolved template placeholders: {', '.join(unresolved)}"
        )
    return content


def dashboard_record(timezone: str, frontdesk_name: str) -> dict:
    return {
        "schema_version": 1,
        "timezone": timezone,
        "plan_aic": None,
        "aic_per_log_unit": None,
        "calibration": None,
        "frontdesk": {"name": frontdesk_name, "title": "Front Desk"},
    }


def ensure_gitignore_entry(home: Path, entry: str) -> None:
    path = home / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = existing.splitlines()
    if entry in lines:
        return
    if existing and not existing.endswith("\n"):
        existing += "\n"
    write_text(path, existing + entry + "\n")


def init_home(
    home: Path,
    project_id: str,
    display_name: str,
    theme_id: str,
    timezone: str | None = None,
    *,
    force: bool = False,
) -> list[str]:
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise OfficeError(
            "project ID must be a lowercase ASCII slug (letters, digits, hyphens)"
        )
    if not display_name.strip():
        raise OfficeError("display name may not be empty")
    theme = load_theme(theme_id)
    timezone = validate_timezone(timezone) if timezone else detect_timezone()
    home, records = project_paths(home)
    binding = records / "project.json"
    if binding.exists():
        if not force:
            raise OfficeError(
                f"office already exists at {home}; regenerate agents with "
                f"`{sys.executable} {ROOT / 'office.py'} render --home {home}`"
            )
        _, existing_records, _project, existing_staff = load_office(home)
        dashboard_path = existing_records / "dashboard.json"
        if not dashboard_path.exists():
            existing_theme = load_theme(existing_staff["theme"])
            write_json(
                dashboard_path,
                dashboard_record(timezone, existing_theme["names"]["frontdesk"]),
            )
        return render_home(home, force=True)

    records.mkdir(parents=True, exist_ok=True)
    project = project_record(project_id, display_name)
    staff = staff_record(project_id, theme)
    write_json(binding, project)
    write_json(records / "staff.json", staff)
    write_json(
        records / "tasks.json",
        {"schema_version": 1, "project_id": project_id, "tasks": []},
    )
    replacements = {
        "PROJECT_ID": project_id,
        "DISPLAY_NAME": display_name,
        "MANAGER_NAME": theme["names"]["lead"],
    }
    write_text(
        records / "PROJECT_STATE.md",
        render_template("PROJECT_STATE.md", replacements),
    )
    write_text(records / "DECISIONS.md", render_template("DECISIONS.md"))
    write_text(
        records / "STARTING_BRIEF.md",
        render_template("STARTING_BRIEF.md", replacements),
    )
    write_json(
        records / "dashboard.json",
        dashboard_record(timezone, theme["names"]["frontdesk"]),
    )
    write_text(
        home / "run-logs" / "README.md",
        render_template("RUN_LOGS_README.md"),
    )
    write_json(home / "run-logs" / "registry.json", {})
    write_text(home / "reports" / ".gitkeep", "")
    write_text(
        home / "reports" / "REPORT.md",
        render_template("REPORT.md", replacements),
    )
    write_text(home / "meeting-notes" / ".gitkeep", "")
    ensure_gitignore_entry(home, ".agent-office/.dashboard-cache/")
    return render_home(home, force=force)


def frontdesk_content(name: str) -> str:
    template = (ROLES_DIR / "frontdesk.agent.md").read_text(encoding="utf-8")
    sessions_path = str((ROOT / "frontdesk" / "sessions.py").resolve())
    required = ("{{FRONTDESK_NAME}}", "{{SESSIONS_PATH}}")
    if any(item not in template for item in required):
        raise OfficeError("front-desk template placeholders are missing")
    return template.replace("{{FRONTDESK_NAME}}", name).replace(
        "{{SESSIONS_PATH}}", sessions_path
    )


def _same_symlink(path: Path, target: Path) -> bool:
    if not path.is_symlink():
        return False
    try:
        return path.resolve() == target.resolve()
    except OSError:
        return False


def _load_install_manifest(path: Path, force: bool) -> tuple[dict[str, str], list[str]]:
    conflicts = []
    if not path.exists() and not path.is_symlink():
        return {}, conflicts
    if path.is_symlink() or not path.is_file():
        if not force or (path.is_dir() and not path.is_symlink()):
            conflicts.append(f"install manifest is not a regular file: {path}")
        return {}, conflicts
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        if not force:
            conflicts.append(f"install manifest is unreadable or invalid: {path}")
        return {}, conflicts
    if not isinstance(manifest, dict) or any(
        not isinstance(key, str)
        or "/" in key
        or not isinstance(value, str)
        or not re.fullmatch(r"[0-9a-f]{64}", value)
        for key, value in manifest.items()
    ):
        if not force:
            conflicts.append(f"install manifest has invalid path/hash entries: {path}")
        return {}, conflicts
    return manifest, conflicts


def _preflight_installed_file(
    target: Path,
    content: str,
    installed_hashes: dict[str, str],
    force: bool,
) -> tuple[dict, str | None]:
    desired_hash = sha256_text(content)
    plan = {
        "kind": "file",
        "target": target,
        "content": content,
        "sha256": desired_hash,
        "action": "copied",
    }
    if not target.exists() and not target.is_symlink():
        return plan, None
    if target.is_dir() and not target.is_symlink():
        return plan, f"refusing to replace directory: {target}"
    if target.is_symlink() or not target.is_file():
        if not force:
            return plan, f"refusing to overwrite different path: {target}"
        plan["action"] = "updated"
        return plan, None
    actual_hash = sha256_text(target.read_text(encoding="utf-8"))
    recorded_hash = installed_hashes.get(target.name)
    if recorded_hash is not None:
        if actual_hash != recorded_hash and not force:
            return plan, f"refusing to overwrite user-edited installed file: {target}"
        if actual_hash == desired_hash:
            plan["action"] = "skip identical"
        else:
            plan["action"] = "updated"
        return plan, None
    if force:
        plan["action"] = "updated"
        return plan, None
    return plan, f"refusing to overwrite foreign file: {target}"


def _looks_like_stale_office_symlink(path: Path) -> bool:
    if not path.is_symlink():
        return False
    try:
        linked = Path(os.readlink(path))
    except OSError:
        return False
    if not linked.is_absolute():
        linked = path.parent / linked
    return (
        linked.name == path.name
        and linked.parent.name == "agents"
        and linked.parent.parent.name == ".github"
    )


def _preflight_installed_symlink(
    target: Path, source: Path, force: bool
) -> tuple[dict, str | None]:
    plan = {
        "kind": "symlink",
        "target": target,
        "source": source,
        "action": "linked",
    }
    if _same_symlink(target, source):
        plan["action"] = "skip identical symlink"
        return plan, None
    if not target.exists() and not target.is_symlink():
        return plan, None
    if target.is_dir() and not target.is_symlink():
        return plan, f"refusing to replace directory: {target}"
    if _looks_like_stale_office_symlink(target):
        if not force:
            return (
                plan,
                f"refusing to replace stale office symlink without --force: {target}",
            )
        plan["action"] = "relinked stale office symlink"
        return plan, None
    if not force:
        return plan, f"refusing to replace different installed agent: {target}"
    plan["action"] = "replaced and linked"
    return plan, None


def install(home: Path, user_dir: Path, *, force: bool = False) -> tuple[list[str], str]:
    home, records, _project, staff = load_office(home)
    render_home(home, check=True)
    user_dir = user_dir.expanduser().resolve()
    agents_dir = user_dir / "agents"
    if agents_dir.is_symlink() or (agents_dir.exists() and not agents_dir.is_dir()):
        raise OfficeError(f"agents install path must be a real directory: {agents_dir}")
    manifest_path = agents_dir / INSTALL_MANIFEST_NAME
    installed_hashes, conflicts = _load_install_manifest(manifest_path, force)
    file_plans = []
    for source in sorted(ROLES_DIR.glob("office-*.agent.md")):
        target = agents_dir / source.name
        plan, conflict = _preflight_installed_file(
            target,
            source.read_text(encoding="utf-8"),
            installed_hashes,
            force,
        )
        file_plans.append(plan)
        if conflict:
            conflicts.append(conflict)
    dashboard = read_json(records / "dashboard.json")
    frontdesk_name = dashboard.get("frontdesk", {}).get("name")
    if not isinstance(frontdesk_name, str) or not NAME_RE.fullmatch(frontdesk_name):
        raise OfficeError("dashboard.json has an invalid frontdesk.name")
    frontdesk_target = agents_dir / f"{frontdesk_name}.agent.md"
    plan, conflict = _preflight_installed_file(
        frontdesk_target,
        frontdesk_content(frontdesk_name),
        installed_hashes,
        force,
    )
    file_plans.append(plan)
    if conflict:
        conflicts.append(conflict)

    by_id = {employee["employee_id"]: employee for employee in staff["employees"]}
    symlink_plans = []
    for employee in staff["employees"]:
        name = employee["name"]
        source = (home / ".github" / "agents" / f"{name}.agent.md").resolve()
        target = agents_dir / f"{name}.agent.md"
        plan, conflict = _preflight_installed_symlink(target, source, force)
        symlink_plans.append(plan)
        if conflict:
            conflicts.append(conflict)

    if conflicts:
        raise OfficeError("install preflight failed:\n  - " + "\n  - ".join(conflicts))

    agents_dir.mkdir(parents=True, exist_ok=True)
    messages = []
    new_hashes = dict(installed_hashes)
    for plan in file_plans:
        target = plan["target"]
        if plan["action"] != "skip identical":
            if target.exists() or target.is_symlink():
                target.unlink()
            write_text(target, plan["content"])
        new_hashes[target.name] = plan["sha256"]
        messages.append(f"{plan['action']}: {target}")
    for plan in symlink_plans:
        target, source = plan["target"], plan["source"]
        if plan["action"] != "skip identical symlink":
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(source)
        messages.append(f"{plan['action']}: {target} -> {source}")
    if manifest_path.is_symlink():
        manifest_path.unlink()
    write_json(manifest_path, new_hashes)
    manager = by_id["lead"]["name"]
    return messages, f"copilot --agent {manager}"


def validate_home_layout(home: Path) -> list[str]:
    errors = []
    try:
        home, records, project, staff = load_office(home)
    except OfficeError as error:
        return [str(error)]
    required_records = (
        "project.json",
        "staff.json",
        "tasks.json",
        "PROJECT_STATE.md",
        "DECISIONS.md",
        "STARTING_BRIEF.md",
        "generated-profiles.json",
        "dashboard.json",
    )
    for filename in required_records:
        if not (records / filename).is_file():
            errors.append(f"missing {records / filename}")
    for path in (
        home / "run-logs" / "README.md",
        home / "run-logs" / "registry.json",
        home / "reports" / "REPORT.md",
        home / "reports" / ".gitkeep",
        home / "meeting-notes" / ".gitkeep",
        home / ".gitignore",
    ):
        if not path.is_file():
            errors.append(f"missing {path}")
    gitignore = home / ".gitignore"
    if gitignore.is_file() and ".agent-office/.dashboard-cache/" not in gitignore.read_text(
        encoding="utf-8"
    ).splitlines():
        errors.append(f"missing dashboard-cache entry in {gitignore}")
    try:
        tasks = read_json(records / project["records"]["tasks"])
        if tasks.get("schema_version") != 1 or tasks.get("project_id") != project["project_id"]:
            errors.append("tasks.json identity/schema is invalid")
        if not isinstance(tasks.get("tasks"), list):
            errors.append("tasks.json tasks must be a list")
        dashboard = read_json(records / "dashboard.json")
        if dashboard.get("schema_version") != 1 or not isinstance(
            dashboard.get("frontdesk"), dict
        ):
            errors.append("dashboard.json schema/frontdesk is invalid")
        validate_timezone(dashboard.get("timezone", ""))
        render_home(home, check=True)
    except (OfficeError, KeyError, TypeError) as error:
        errors.append(str(error))
    for employee in staff["employees"]:
        path = home / ".github" / "agents" / f"{employee['name']}.agent.md"
        if not path.is_file():
            errors.append(f"missing {path}")
    return errors


def installed_errors(home: Path, user_dir: Path) -> list[str]:
    errors = []
    home, records, _project, staff = load_office(home)
    agents_dir = user_dir.expanduser().resolve() / "agents"
    manifest, manifest_conflicts = _load_install_manifest(
        agents_dir / INSTALL_MANIFEST_NAME, False
    )
    errors.extend(manifest_conflicts)
    for source in sorted(ROLES_DIR.glob("office-*.agent.md")):
        target = agents_dir / source.name
        if not target.is_file() or target.is_symlink():
            errors.append(f"install canonical role: {target}")
        elif target.read_text(encoding="utf-8") != source.read_text(encoding="utf-8"):
            errors.append(f"reinstall changed canonical role: {target}")
        elif manifest.get(target.name) != sha256_text(
            source.read_text(encoding="utf-8")
        ):
            errors.append(f"repair install manifest entry: {target}")
    dashboard = read_json(records / "dashboard.json")
    frontdesk_name = dashboard["frontdesk"]["name"]
    frontdesk_target = agents_dir / f"{frontdesk_name}.agent.md"
    if (
        not frontdesk_target.is_file()
        or frontdesk_target.is_symlink()
        or frontdesk_target.read_text(encoding="utf-8") != frontdesk_content(frontdesk_name)
    ):
        errors.append(f"install front-desk agent: {frontdesk_target}")
    elif manifest.get(frontdesk_target.name) != sha256_text(
        frontdesk_content(frontdesk_name)
    ):
        errors.append(f"repair install manifest entry: {frontdesk_target}")
    for employee in staff["employees"]:
        source = home / ".github" / "agents" / f"{employee['name']}.agent.md"
        target = agents_dir / source.name
        if not _same_symlink(target, source):
            errors.append(f"link named agent: {target}")
    return errors


def doctor(home: Path | None, user_dir: Path) -> tuple[bool, list[str]]:
    messages = []
    ok = True
    if sys.version_info >= (3, 10):
        messages.append(f"[OK] Python {sys.version_info.major}.{sys.version_info.minor}")
    else:
        ok = False
        messages.append("[FAIL] Python 3.10+ is required")
    copilot = shutil.which("copilot")
    if copilot:
        messages.append(f"[OK] copilot found at {copilot}")
    else:
        ok = False
        messages.append("[FAIL] copilot is not on PATH; install GitHub Copilot CLI")
    if home is None:
        messages.append("[INFO] no --home supplied; project layout and named agents not checked")
        return ok, messages
    errors = validate_home_layout(home)
    if errors:
        ok = False
        messages.append("[FAIL] office layout is invalid:")
        messages.extend(f"  - {error}" for error in errors)
        messages.append(
            f"  Fix: run {sys.executable} {ROOT / 'office.py'} render --home {home}"
        )
        return ok, messages
    messages.append(f"[OK] office layout valid at {Path(home).expanduser().resolve()}")
    errors = installed_errors(home, user_dir)
    if errors:
        ok = False
        messages.append("[FAIL] agents are not fully installed:")
        messages.extend(f"  - {error}" for error in errors)
        messages.append(
            f"  Fix: run {sys.executable} {ROOT / 'office.py'} install "
            f"--user-dir {Path(user_dir).expanduser()} --home {Path(home).expanduser()}"
        )
    else:
        messages.append(f"[OK] agents installed in {Path(user_dir).expanduser().resolve() / 'agents'}")
    return ok, messages


def run_dashboard_script(script: str, arguments: list[str]) -> int:
    path = ROOT / "dashboard" / script
    if not path.is_file():
        raise OfficeError(f"dashboard helper is missing: {path}")
    completed = subprocess.run([sys.executable, str(path), *arguments], check=False)
    return completed.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and manage a local, project-scoped Copilot Office."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create a new project office")
    init_parser.add_argument("--home", type=Path, required=True, help="project home directory")
    init_parser.add_argument("--project-id", required=True, help="lowercase project slug")
    init_parser.add_argument("--name", required=True, help="project display name")
    init_parser.add_argument("--theme", required=True, choices=THEME_IDS)
    init_parser.add_argument("--timezone", help="IANA timezone (auto-detected by default)")
    init_parser.add_argument(
        "--force", action="store_true", help="regenerate managed files in an existing office"
    )

    install_parser = subparsers.add_parser(
        "install", help="install reusable roles and project-agent links"
    )
    install_parser.add_argument("--home", type=Path, required=True, help="project home directory")
    install_parser.add_argument(
        "--user-dir",
        type=Path,
        default=Path.home() / ".copilot",
        help="Copilot user directory (default: ~/.copilot)",
    )
    install_parser.add_argument(
        "--force", action="store_true", help="replace conflicting installed agent files"
    )

    render_parser = subparsers.add_parser(
        "render", help="regenerate named project agents from staff.json"
    )
    render_parser.add_argument("--home", type=Path, required=True, help="project home directory")

    doctor_parser = subparsers.add_parser(
        "doctor", help="check Python, Copilot CLI, office layout, and installed agents"
    )
    doctor_parser.add_argument("--home", type=Path, help="project home directory")
    doctor_parser.add_argument(
        "--user-dir",
        type=Path,
        default=Path.home() / ".copilot",
        help="Copilot user directory (default: ~/.copilot)",
    )

    calibrate_parser = subparsers.add_parser(
        "calibrate", help="calibrate dashboard AIC accounting from session logs"
    )
    calibrate_parser.add_argument("--home", type=Path, required=True)
    calibrate_parser.add_argument("--used", required=True, type=float)
    calibrate_parser.add_argument("--plan", type=float)
    calibrate_parser.add_argument("--sessions", type=Path)

    dashboard_parser = subparsers.add_parser(
        "dashboard", help="run the local office dashboard in the foreground"
    )
    dashboard_parser.add_argument("--home", type=Path, required=True)
    dashboard_parser.add_argument("--port", type=int, default=8765)
    dashboard_parser.add_argument("--sessions", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            names = init_home(
                args.home,
                args.project_id,
                args.name,
                args.theme,
                args.timezone,
                force=args.force,
            )
            print(f"Initialized {args.project_id} at {args.home.expanduser().resolve()}")
            print("Rendered: " + ", ".join(names))
        elif args.command == "install":
            messages, command = install(args.home, args.user_dir, force=args.force)
            for message in messages:
                print(message)
            print("Start your office:")
            print(command)
        elif args.command == "render":
            names = render_home(args.home)
            print("Rendered: " + ", ".join(names))
        elif args.command == "doctor":
            ok, messages = doctor(args.home, args.user_dir)
            for message in messages:
                print(message)
            return 0 if ok else 1
        elif args.command == "calibrate":
            command = [
                "--office",
                str(args.home.expanduser().resolve()),
                "--calibrate",
                str(args.used),
            ]
            if args.plan is not None:
                command.extend(("--plan", str(args.plan)))
            if args.sessions is not None:
                command.extend(("--sessions", str(args.sessions.expanduser().resolve())))
            return run_dashboard_script("session_index.py", command)
        elif args.command == "dashboard":
            command = [
                "--office",
                str(args.home.expanduser().resolve()),
                "--port",
                str(args.port),
            ]
            if args.sessions is not None:
                command.extend(("--sessions", str(args.sessions.expanduser().resolve())))
            return run_dashboard_script("server.py", command)
    except (OfficeError, OSError) as error:
        print(f"office.py: error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

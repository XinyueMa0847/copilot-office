"""Read-only, loopback-only office record feed. Python standard library only.

Collects recorded office state (staff, agent definitions, tasks, decisions,
run registry and health logs). It is not worker or process telemetry.
"""

import argparse
import hashlib
import json
import logging
import math
import os
import re
import stat
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlsplit


DASHBOARD = Path(__file__).resolve().parent
# User-level agents (e.g. the office front desk); read-only, frontmatter only, never served.
USER_AGENTS = Path(os.path.expanduser("~/.copilot/agents"))
MAX_FILE_BYTES = 1024 * 1024
MAX_TAIL_BYTES = 64 * 1024
MAX_SOURCES = 250
MAX_FIELD_CHARS = 1500
STALE_HEALTH_SECONDS = 20 * 60
UNKNOWN = "Unknown"
RECORDS = {
    "binding": ".agent-office/project.json",
    "staff": ".agent-office/staff.json",
    "tasks": ".agent-office/tasks.json",
    "dashboard": ".agent-office/dashboard.json",
    "state": ".agent-office/PROJECT_STATE.md",
    "decisions": ".agent-office/DECISIONS.md",
    "brief": ".agent-office/STARTING_BRIEF.md",
    "profiles": ".agent-office/generated-profiles.json",
    "registry": "run-logs/registry.json",
    "runs-readme": "run-logs/README.md",
}
REQUIRED = ("binding", "staff", "tasks", "dashboard")
# Record-referenced evidence is served only under these roots and file types.
EVIDENCE_ROOTS = ("reports", "meeting-notes", "run-logs", "research")
EVIDENCE_SUFFIXES = (".md", ".json", ".log", ".txt")
REF_PATTERN = re.compile(r"(?:\.\./)*(?:reports|meeting-notes|run-logs|research|assets|\.agent-office)/[^\s;,()`\"'\]\[<>]+")
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
CLOSED_STATUS = re.compile(r"^(?:completed?|closed|superseded|cancell?ed|abandoned)(?:_|$)")
COLORS = {
    "guardian": "#efdfba", "lead": "#f6d3bd", "builder": "#dce7fb",
    "navigator": "#e8d9f4", "analyst": "#f8dfbc", "reviewer": "#d5eacb",
    "curator": "#f7e59f", "researcher": "#cfe8ed",
}
STATUS_FIELDS = (
    ("status", "Status"), ("delivery", "Delivery"), ("current_summary", "Current summary"),
    ("last_confirmed_status", "Last confirmed status"), ("review_result", "Review result"),
    ("lead_result", "Lead result"), ("result", "Result"),
)
BLOCKER_FIELDS = (
    ("blockers", "Blockers"), ("blocker", "Blocker"), ("open_gates", "Open gates"),
    ("remaining_gates", "Remaining gates"),
)
TIME_FIELDS = (
    "requested_at", "created_at", "started_at", "observed_at", "last_checked_at",
    "review_reconciled_at", "checked_at", "designation_at", "closure_reported_at",
)
AUTH_FIELDS = (
    ("request_id", "Request ID"), ("requested_at", "Requested at"), ("source", "Source"),
    ("basis", "Basis"), ("scope", "Scope"), ("execution_rule", "Execution rule"),
    ("ground_rule", "Ground rule"),
)


class RecordError(Exception):
    pass


def utc(timestamp=None):
    date = datetime.now(timezone.utc) if timestamp is None else datetime.fromtimestamp(timestamp, timezone.utc)
    return date.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _walk(root, relative):
    """Yield (parent_fd, final_name) opening directories without following symlinks."""
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or any(p in (".", "..", "") for p in parts):
        raise RecordError("Invalid source path")
    descriptors = []
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(directory)
        for part in parts[:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            descriptors.append(directory)
        return descriptors, directory, parts[-1]
    except OSError as error:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise RecordError("Source unavailable or unsafe to read") from error


def read_local(root, relative, tail=None):
    """Read a regular file with no symlink components. tail=N reads the last N bytes."""
    descriptors, directory, name = _walk(root, relative)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise RecordError("Source is not a regular file")
        if tail is None and before.st_size > MAX_FILE_BYTES:
            raise RecordError("Source exceeds the 1 MiB read limit")
        offset = max(0, before.st_size - tail) if tail else 0
        with os.fdopen(os.dup(fd), "rb") as stream:
            stream.seek(offset)
            raw = stream.read((tail or MAX_FILE_BYTES) + 1)
        after = os.fstat(fd)
        if tail is None and (len(raw) > MAX_FILE_BYTES or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
            raise RecordError("Source changed during collection; retry on next poll")
        if tail:
            text = raw.decode("utf-8", errors="replace")
            if offset:
                text = text.split("\n", 1)[1] if "\n" in text else ""
        else:
            text = raw.decode("utf-8")
        return text, utc(after.st_mtime), hashlib.sha256(raw).hexdigest(), after.st_mtime, after.st_size
    except UnicodeDecodeError as error:
        raise RecordError("Source is not valid UTF-8") from error
    except OSError as error:
        raise RecordError("Source unavailable or unsafe to read") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def probe(root, relative):
    """Classify a path without reading it: file, directory, missing or unsafe."""
    try:
        descriptors, directory, name = _walk(root, relative)
    except RecordError:
        return "missing_or_unsafe", None
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            return "symlink", None
        if stat.S_ISDIR(info.st_mode):
            return "directory", None
        if stat.S_ISREG(info.st_mode):
            return "file", utc(info.st_mtime)
        return "other", None
    except OSError:
        return "missing", None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def text(value, limit=MAX_FIELD_CHARS):
    if value is None or value == "" or value == [] or value == {}:
        return UNKNOWN
    if isinstance(value, str):
        result = value
    elif isinstance(value, list):
        result = "\n".join("• " + text(item, limit) for item in value)
    else:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(result) > limit:
        result = result[:limit].rstrip() + " … (truncated; open the source record)"
    return result


def labelled(record, fields):
    lines = [label + ": " + text(record[key]) for key, label in fields if record.get(key) not in (None, "", [], {})]
    return "\n".join(lines)


def times(record):
    found = [key + ": " + text(record[key]) for key in TIME_FIELDS if record.get(key)]
    return "; ".join(found) if found else "Source observation/update time unknown"


def explicit_stage(status):
    """Only structured status prefixes become stages; free prose stays Recorded."""
    if re.match(r"^returned(?:_|$)", status) or status == "idle_assessment_returned":
        return "Returned"
    if re.match(r"^reviewed(?:_|$)", status):
        return "Reviewed"
    return "Recorded"


def is_closed(task):
    status = task.get("status")
    if task.get("superseded_by"):
        return True
    return isinstance(status, str) and bool(CLOSED_STATUS.match(status))


def md_section(markdown, heading_regex):
    match = re.search(r"^## (" + heading_regex + r")[^\n]*\n([\s\S]*?)(?=^## |\Z)", markdown, re.MULTILINE)
    if not match:
        return None, ""
    heading_line = markdown[match.start():markdown.index("\n", match.start())].lstrip("# ").strip()
    body = re.sub(r"\n-{3,}\s*$", "", match.group(2).strip())
    return heading_line, body


def frontmatter(markdown):
    """Parse the small YAML subset used by agent definitions; anything else is an error."""
    if not markdown.startswith("---\n"):
        raise RecordError("Agent definition has no frontmatter")
    end = markdown.find("\n---", 4)
    if end < 0:
        raise RecordError("Agent definition frontmatter is unterminated")
    data, key = {}, None
    for line in markdown[4:end].splitlines():
        if not line.strip():
            continue
        match = re.match(r"^([A-Za-z][\w-]*):\s*(.*)$", line)
        if match:
            key, value = match.groups()
            if value.startswith("[") and value.endswith("]"):
                data[key] = [item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()]
            else:
                data[key] = [] if value == "" else value
        elif line.startswith("- ") and key and isinstance(data.get(key), list):
            data[key].append(line[2:].strip())
        elif line.startswith("  ") and key and isinstance(data.get(key), str):
            data[key] += " " + line.strip()
        else:
            raise RecordError("Unsupported agent frontmatter syntax")
    for key, value in data.items():
        if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            data[key] = value[1:-1].replace("''", "'") if value[0] == "'" else value[1:-1]
    return data


def resolve_ref(raw):
    """Lexically resolve a recorded path; ../ is relative to .agent-office, else the office root."""
    path = raw.split("#", 1)[0].rstrip(".:")
    if any(char in path for char in "{}*?$"):
        return None, "pattern or placeholder — not a single file"
    base = [".agent-office"] if path.startswith("../") else []
    parts = list(base)
    for part in PurePosixPath(path).parts:
        if part == "..":
            if not parts:
                return None, "outside office root — not served"
            parts.pop()
        elif part not in ("", "."):
            parts.append(part)
    if not parts:
        return None, "outside office root — not served"
    return "/".join(parts), None


def validate_records(records):
    binding = records.get("binding")
    if not isinstance(binding, dict):
        raise RecordError("binding: missing or invalid project binding")
    project_id = binding.get("project_id")
    if not isinstance(project_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project_id):
        raise RecordError("binding: invalid project_id")
    for key in REQUIRED:
        value = records.get(key)
        if not isinstance(value, dict):
            raise RecordError(key + ": missing, invalid, or conflicting project binding")
        if key in ("binding", "staff", "tasks") and value.get("project_id") != project_id:
            raise RecordError(key + ": missing, invalid, or conflicting project binding")
    expected = {"staff": "staff.json", "tasks": "tasks.json", "state": "PROJECT_STATE.md", "decisions": "DECISIONS.md"}
    if any(binding.get("records", {}).get(key) != value for key, value in expected.items()):
        raise RecordError("Binding record paths changed; feed allowlist requires explicit reconciliation")
    staff = records["staff"].get("employees")
    tasks = records["tasks"].get("tasks")
    dashboard = records["dashboard"]
    if not isinstance(staff, list) or not staff or not isinstance(tasks, list):
        raise RecordError("Staff or task collection is invalid")
    if not isinstance(dashboard.get("timezone"), str) or not dashboard["timezone"]:
        raise RecordError("Dashboard timezone is invalid")
    for key in ("plan_aic", "aic_per_log_unit"):
        value = dashboard.get(key)
        if value is not None and (
            not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or value <= 0
        ):
            raise RecordError("Dashboard " + key + " must be null or a positive number")
    if dashboard.get("calibration") is not None and not isinstance(dashboard["calibration"], dict):
        raise RecordError("Dashboard calibration is invalid")
    frontdesk = dashboard.get("frontdesk")
    if not isinstance(frontdesk, dict) or any(
        not isinstance(frontdesk.get(key), str) or not frontdesk[key]
        for key in ("name", "title")
    ):
        raise RecordError("Dashboard front desk is invalid")
    expected_roles = {
        "lead": ("office-lead", "Manager"),
        "guardian": ("office-guardian", "Counselor"),
        "builder": ("office-builder", "Builder"),
        "reviewer": ("office-reviewer", "Reviewer"),
        "analyst": ("office-analyst", "Results Analyst"),
        "navigator": ("office-navigator", "Code Reader"),
        "researcher": ("office-researcher", "Researcher"),
        "curator": ("office-curator", "Docs Curator"),
    }
    ids, names = set(), set()
    for employee in staff:
        if not isinstance(employee, dict) or any(not isinstance(employee.get(k), str) or not employee[k] for k in ("employee_id", "name", "title")):
            raise RecordError("Employee identity is invalid")
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", employee["employee_id"]) or not SAFE_NAME.fullmatch(employee["name"]):
            raise RecordError("Employee identity is invalid")
        if employee["employee_id"] in ids or employee["name"] in names:
            raise RecordError("Employee identities must be unique and stable")
        if not isinstance(employee.get("role_profile"), str):
            raise RecordError("Employee role profile is invalid")
        expected = expected_roles.get(employee["employee_id"])
        if expected is None or (employee["role_profile"], employee["title"]) != expected:
            raise RecordError("Employee role profile or title conflicts with the office contract")
        ids.add(employee["employee_id"])
        names.add(employee["name"])
        sessions = employee.get("sessions", [])
        if not isinstance(sessions, list) or any(not isinstance(s, dict) for s in sessions):
            raise RecordError("Employee session collection is invalid")
    if ids != set(expected_roles):
        raise RecordError("Staff directory must contain each fixed employee ID exactly once")
    task_ids = set()
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("task_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]+", task["task_id"]):
            raise RecordError("Task identity is invalid")
        if task["task_id"] in task_ids:
            raise RecordError("Duplicate task ID in task records")
        task_ids.add(task["task_id"])
        sessions = task.get("sessions", [])
        if not isinstance(sessions, list) or any(not isinstance(s, dict) for s in sessions):
            raise RecordError("Task worker session collection is invalid")


class Collector:
    def __init__(self, root):
        self.root = root
        self.sources, self.records, self.errors, self.contents = {}, {}, [], {}
        self.collected = datetime.now(timezone.utc)

    def add(self, key, relative, note, parse_json=False, tail=None, required=False):
        if len(self.sources) >= MAX_SOURCES:
            raise RecordError("Source count exceeds the approved feed limit")
        entry = {"path": relative, "time": "Source observation/update time unknown", "note": note,
                 "modifiedAt": None, "served": tail is None}
        try:
            content, modified, digest, mtime, size = read_local(self.root, relative, tail=tail)
            entry.update(modifiedAt=modified, sha256=digest, ageSeconds=max(0, int(self.collected.timestamp() - mtime)))
            if tail:
                entry["note"] += " Last {} KiB read of {} bytes.".format(tail // 1024, size) if size > tail else ""
                entry["served"] = size <= MAX_FILE_BYTES
            self.contents[key] = content
            if parse_json:
                try:
                    value = json.loads(content)
                except json.JSONDecodeError as error:
                    raise RecordError("Source JSON is incomplete or malformed") from error
                if not isinstance(value, dict):
                    raise RecordError("Source JSON must be an object")
                self.records[key] = value
        except RecordError as error:
            self.contents.pop(key, None)
            entry["error"] = str(error)
            entry["served"] = False
            self.errors.append({"path": relative, "error": str(error), "required": required})
            logging.warning("%s: %s", relative, error)
        self.sources[key] = entry
        return entry

    def reference(self, raw):
        """Register a record-referenced evidence path; return (source_key or None, description)."""
        relative, problem = resolve_ref(raw)
        if problem:
            return None, raw + " — " + problem
        known = next((key for key, source in self.sources.items() if source["path"] == relative), None)
        if known:
            return (known, raw) if self.sources[known].get("served") and "error" not in self.sources[known] else (None, raw + " — recorded source, not served as evidence")
        root_part = relative.split("/", 1)[0]
        if root_part not in EVIDENCE_ROOTS:
            kind, _ = probe(self.root, relative)
            state = {"file": "exists, but outside the served evidence roots", "directory": "directory — not served"}.get(kind, "not found at recorded location")
            return None, raw + " — " + state
        kind, modified = probe(self.root, relative)
        if kind == "directory":
            return None, raw + " — directory — not served"
        if kind != "file":
            return None, raw + " — not found at recorded location" + (" (symlink not followed)" if kind == "symlink" else "")
        if not relative.endswith(EVIDENCE_SUFFIXES):
            return None, raw + " — file type not served"
        if len(self.sources) >= MAX_SOURCES:
            return None, raw + " — source limit reached; not served"
        self.sources[relative] = {"path": relative, "time": "Evidence referenced by records; not read during collection",
                                  "note": "Record-referenced evidence. Opened on demand as plain text.",
                                  "modifiedAt": modified, "served": True}
        return relative, raw

    def references(self, value):
        keys, notes = [], []
        for raw in sorted(set(REF_PATTERN.findall(json.dumps(value, ensure_ascii=False)))):
            key, description = self.reference(raw)
            if key and key not in keys:
                keys.append(key)
            elif not key:
                notes.append(description)
        return keys, notes


def invocation(front):
    user = str(front.get("user-invocable", "")).lower()
    model = str(front.get("disable-model-invocation", "")).lower()
    if not user and not model:
        return UNKNOWN
    parts = ["you can invoke it" if user == "true" else "not user-invocable" if user == "false" else "user invocation unknown",
             "other agents cannot delegate to it" if model == "true" else "other agents may delegate to it" if model == "false" else "delegation unknown"]
    return "; ".join(parts)


def employee_profiles(collector, staff):
    manifest = collector.records.get("profiles", {}).get("outputs", {}) if isinstance(collector.records.get("profiles"), dict) else {}
    profiles = {}
    for employee in staff:
        name = employee["name"]
        relative = ".github/agents/" + name + ".agent.md"
        key = "agent:" + name
        entry = collector.add(key, relative, "Agent definition (declared configuration, not the state of any running session).")
        profile = {"path": relative, "description": UNKNOWN, "model": UNKNOWN, "tools": [], "integrity": "unknown",
                   "integrityDetail": "Agent definition could not be read.", "declaredEmployeeId": None, "source": key,
                   "invocation": UNKNOWN}
        if "error" not in entry:
            content = collector.contents[key]
            try:
                front = frontmatter(content)
                profile["description"] = text(front.get("description"))
                profile["model"] = text(front.get("model"))
                profile["invocation"] = invocation(front)
                profile["tools"] = [t for t in front.get("tools", []) if isinstance(t, str)] if isinstance(front.get("tools"), list) else []
                if front.get("name") != name:
                    profile["integrity"] = "conflict"
                    profile["integrityDetail"] = "Frontmatter name does not match staff name."
            except RecordError as error:
                profile["integrityDetail"] = str(error)
            declared = re.search(r"employee ID `([a-z][a-z0-9_-]*)`", content)
            profile["declaredEmployeeId"] = declared.group(1) if declared else None
            expected = manifest.get(name, {}).get("sha256") if isinstance(manifest.get(name), dict) else None
            if profile["integrity"] != "conflict":
                if declared and declared.group(1) != employee["employee_id"]:
                    profile["integrity"] = "conflict"
                    profile["integrityDetail"] = "Definition declares employee ID " + declared.group(1) + ", staff directory says " + employee["employee_id"] + "."
                elif not expected:
                    profile["integrityDetail"] = "No sha256 recorded for this profile in generated-profiles.json."
                elif expected == entry.get("sha256"):
                    profile["integrity"] = "matches"
                    profile["integrityDetail"] = "File bytes match generated-profiles.json (manifest consistency, not proof the canonical role is current)."
                else:
                    profile["integrity"] = "differs"
                    profile["integrityDetail"] = "File bytes differ from generated-profiles.json: edited after generation, or regenerated without a manifest update."
        profiles[employee["employee_id"]] = profile
    return profiles


def office_agents(collector, staff, agents_dir, dashboard_config):
    """Read only the front desk named in dashboard.json."""
    frontdesk = dashboard_config.get("frontdesk")
    if not isinstance(frontdesk, dict):
        return [], "Front desk configuration is unavailable."
    name, configured_title = frontdesk.get("name"), frontdesk.get("title")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
        return [], "Front desk name is invalid."
    if not isinstance(configured_title, str) or not configured_title:
        return [], "Front desk title is invalid."
    if name in {e["name"] for e in staff}:
        return [], "Front desk name conflicts with project staff."
    display = "~/.copilot/agents/" + name + ".agent.md"
    entry = {"path": display, "time": "Declared configuration; no observation time", "modifiedAt": None, "served": False,
             "note": "Configured user-level front desk definition; shown, not served."}
    profile = {"path": display, "description": UNKNOWN, "model": UNKNOWN, "tools": [], "integrity": "unknown",
               "integrityDetail": "Not managed by generated-profiles.json (user-level agent, not project staff).",
               "declaredEmployeeId": None, "invocation": UNKNOWN}
    problem = None
    try:
        content, modified, digest, _mtime, _size = read_local(agents_dir, name + ".agent.md")
        entry.update(modifiedAt=modified, sha256=digest)
        front = frontmatter(content)
        if front.get("name") != name:
            raise RecordError("Frontmatter name does not match configured front desk name")
        profile.update(description=text(front.get("description")), model=text(front.get("model")),
                       invocation=invocation(front),
                       tools=[t for t in front.get("tools", []) if isinstance(t, str)] if isinstance(front.get("tools"), list) else [])
    except RecordError as error:
        entry["error"] = str(error)
        profile["integrityDetail"] = str(error)
        problem = "Configured front desk definition could not be read safely."
        collector.errors.append({"path": display, "error": str(error), "required": False})
    collector.sources["office-agent:" + name] = entry
    agent = {"id": "frontdesk", "name": name, "role": configured_title,
             "color": "#f2d6e4", "scope": "office", "roleProfile": "frontdesk",
             "label": "Office-wide · not project staff",
             "note": "Configured user-level front desk. Not in staff.json, so it has no project tasks here.",
             "aliases": [], "profile": profile, "suggested": [], "openCount": 0, "closedCount": 0}
    return [agent], problem


def parse_readme_runs(markdown):
    rows = {}
    for line in markdown.splitlines():
        if not line.startswith("|") or re.match(r"^\|\s*-", line):
            continue
        cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 4 and cells[0] != "Name":
            rows[cells[0]] = {"host": cells[1], "runDir": cells[2], "status": cells[3]}
    return rows


HEALTH_HEADER = re.compile(r"^(\d{1,2}:\d{2}) (\S+) \[(.*?)\] up (\S+).*?state=(\S+)")


def last_health_block(content):
    """Return (block_lines, warning) for the last complete health-check block."""
    lines = content.split("\n")
    warning = None
    if lines and lines[-1] != "":
        warning = "Last line is incomplete (write in progress?); it was ignored."
        lines = lines[:-1]
    lines = [line for line in lines if line.strip()]
    starts = [i for i, line in enumerate(lines) if HEALTH_HEADER.match(line)]
    if not starts:
        return [], warning or "No recognised health-check header found."
    return lines[starts[-1]:starts[-1] + 14], warning


def build_runs(collector):
    registry = collector.records.get("registry")
    if registry is None:
        return [], "Run registry unavailable; run coverage unknown."
    readme = parse_readme_runs(collector.contents.get("runs-readme", ""))
    runs = []
    for run_dir, entry in registry.items():
        if not isinstance(entry, dict) or not SAFE_NAME.fullmatch(run_dir) or not SAFE_NAME.fullmatch(str(entry.get("name", ""))):
            collector.errors.append({"path": RECORDS["registry"], "error": "Skipped a registry entry with an invalid run or name", "required": False})
            continue
        name = entry["name"]
        md_key, health_key = "run:" + name + ":md", "run:" + name + ":health"
        md = collector.add(md_key, "run-logs/" + name + ".md", "Lead-maintained run setup and issues/events.")
        health = collector.add(health_key, "run-logs/" + name + ".health.log", "Append-only health-check log (health_check.py --log).", tail=MAX_TAIL_BYTES)
        block, warning = last_health_block(collector.contents.get(health_key, "")) if "error" not in health else ([], None)
        header = HEALTH_HEADER.match(block[0]) if block else None
        _, issues = md_section(collector.contents.get(md_key, ""), r"Issues / events")
        registry_status = entry.get("status")
        superseded = entry.get("superseded")
        readme_row = readme.get(name, {})
        terminal = bool(registry_status or superseded)
        runs.append({
            "id": run_dir, "name": name, "host": text(entry.get("host")), "rid": text(entry.get("rid")),
            "started": text(entry.get("started")), "attempt": text(entry.get("attempt")), "setup": text(entry.get("setup")),
            "registryStatus": text(registry_status) if registry_status else "No outcome recorded in registry.json",
            "superseded": text(superseded) if superseded else None,
            "readmeStatus": readme_row.get("status", "Not listed in run-logs/README.md"),
            "recordedActive": not terminal and readme_row.get("status", "").lower() == "running",
            "health": {
                "modifiedAt": health.get("modifiedAt"), "ageSeconds": health.get("ageSeconds"),
                "stale": health.get("ageSeconds") is not None and health["ageSeconds"] > STALE_HEALTH_SECONDS,
                "error": health.get("error"), "warning": warning,
                "blockTime": header.group(1) if header else None,
                "uptime": header.group(4) if header else None,
                "stateField": header.group(5) if header else None,
                "block": "\n".join(block) if block else None,
            },
            "issues": "\n".join(issues.splitlines()[-10:]) if issues else None,
            "sources": ["registry", "runs-readme", md_key, health_key],
        })
    active = [run for run in runs if run["recordedActive"]]
    others = sorted([run for run in runs if not run["recordedActive"]], key=lambda run: run["started"], reverse=True)
    return active + others, None


def build(collector, agents_dir=USER_AGENTS):
    records = collector.records
    validate_records(records)
    staff = records["staff"]["employees"]
    tasks = records["tasks"]["tasks"]
    by_id = {e["employee_id"]: e for e in staff}
    by_name = {e["name"]: e["employee_id"] for e in staff}
    lead_id = next((e["employee_id"] for e in staff if e.get("role_profile") == "office-lead"), None)
    profiles = employee_profiles(collector, staff)

    decisions_md = collector.contents.get("decisions", "")
    decision_titles = dict(re.findall(r"^## (D\d+):\s*(.*)$", decisions_md, re.MULTILINE))

    def decision_notes(value):
        ids = sorted(set(re.findall(r"\bD\d{3}\b", json.dumps(value, ensure_ascii=False))))
        return [d + " — " + (decision_titles[d] if d in decision_titles else "not found as a heading in DECISIONS.md") for d in ids]

    lead_sessions = []
    for employee in staff:
        for session in employee.get("sessions", []):
            if employee["employee_id"] == lead_id and isinstance(session.get("session_id"), str):
                lead_sessions.append({
                    "id": session["session_id"], "label": text(session.get("label") or session.get("task_id")),
                    "status": text(session.get("status")), "scope": text(session.get("scope")),
                    "designationAt": text(session.get("designation_at")),
                    "closedAt": session.get("closure_reported_at"), "current": session.get("status") == "current_lead",
                    "task": session.get("task_id"),
                })
    session_status = {s["id"]: s["status"] for s in lead_sessions}

    def session_line(session_id):
        if not session_id:
            return "Coordinating Lead CLI session: unknown (not recorded on this task)."
        status = session_status.get(session_id)
        return "Coordinating Lead CLI session: " + session_id + (" — staff.json status: " + status if status else " — not listed among the Lead's sessions in staff.json")

    # Worker identities: (worker_id, task_id) -> merged record; missing IDs are never merged.
    workers = {}

    def add_worker(employee_id, worker_id, id_kind, task_id, record, origin):
        if not worker_id or not task_id or employee_id is None:
            return
        slot = workers.setdefault((worker_id, task_id), {"employee": employee_id, "id": worker_id, "kind": id_kind, "task": task_id, "records": []})
        slot["records"].append({"origin": origin, "status": text(record.get("status")), "scope": text(record.get("scope")),
                                "time": times(record), "evidence": record.get("evidence")})

    for employee in staff:
        for session in employee.get("sessions", []):
            if employee["employee_id"] == lead_id and session.get("session_id"):
                continue
            if session.get("agent_id"):
                add_worker(employee["employee_id"], session["agent_id"], "Worker agent ID", session.get("task_id"), session, "staff.json")
            elif session.get("session_id"):
                add_worker(employee["employee_id"], session["session_id"], "CLI session ID (direct user conversation)", session.get("task_id"), session, "staff.json")
    for task in tasks:
        for session in task.get("sessions", []):
            add_worker(by_name.get(session.get("employee")), session.get("agent_id"), "Worker agent ID", task["task_id"], session, "tasks.json")

    task_index = {task["task_id"]: task for task in tasks}
    assignments = []
    suggestions = {}

    def row(identifier, employee, lane, title, task, status, sources, **extra):
        base = {"id": identifier, "employee": employee, "lane": lane, "title": title, "task": task, "stage": "Recorded",
                "status": status, "blocker": UNKNOWN, "next": UNKNOWN, "observed": "Source observation/update time unknown",
                "modified": collector.sources[sources[0]].get("modifiedAt"), "leadSession": None, "worker": None,
                "workerKind": None, "subtasks": [], "sources": sources, "steps": []}
        base.update(extra)
        return base

    for task in tasks:
        task_id = task["task_id"]
        lane = "closed" if is_closed(task) else "open"
        owner = task.get("owner_employee_id") if task.get("owner_employee_id") in by_id else None
        refs, unresolved = collector.references(task)
        sources = ["tasks"] + [key for key in ("decisions",) if decision_notes(task)] + refs
        status = labelled(task, STATUS_FIELDS) or "Status: Unknown (no status field recorded)"
        if lane == "closed" and task.get("superseded_by"):
            status += "\nSuperseded by: " + text(task["superseded_by"])
        blockers = labelled(task, BLOCKER_FIELDS)
        pending = [d for d in task.get("pending_user_decisions", []) or [] if isinstance(d, dict) and not str(d.get("status", "")).startswith("approved")]
        if pending:
            blockers = (blockers + "\n" if blockers else "") + "Pending user decisions: " + ", ".join(text(d.get("id")) for d in pending)
        subtasks = []
        for sub in task.get("subtasks", []) or []:
            if not isinstance(sub, dict):
                continue
            suggested = sub.get("suggested_owner")
            employee_name = by_id[suggested]["name"] if suggested in by_id else suggested
            subtasks.append({"id": text(sub.get("id")), "title": text(sub.get("title")), "status": text(sub.get("status")),
                             "suggestedOwner": text(employee_name), "dependsOn": [text(d) for d in sub.get("depends_on", []) or []]})
            if suggested in by_id and str(sub.get("status")) == "pending":
                suggestions.setdefault(suggested, []).append(task_id + " " + text(sub.get("id")) + ": " + text(sub.get("title")))
        next_action = text(task.get("next_action")) if task.get("next_action") else UNKNOWN
        if subtasks and next_action == UNKNOWN:
            next_action = "{} subtasks recorded ({} pending). Suggested owners are suggestions, not assignments.".format(
                len(subtasks), sum(1 for s in subtasks if s["status"] == "pending"))
        linked = [w for (wid, tid), w in workers.items() if tid == task_id]
        owner_session = task.get("owner_session") or task.get("lead_session")
        auth = labelled(task, AUTH_FIELDS)
        authorization = task.get("current_authorization")
        if isinstance(authorization, dict) and authorization.get("scope"):
            auth += ("\n" if auth else "") + "Current authorization: " + text(authorization["scope"])
        decisions = decision_notes(task)
        steps = [
            {"title": "User request / authorization", "stage": "Requested" if (task.get("requested_at") or task.get("request_id") or task.get("source")) else "Unknown",
             "text": (auth or "No request/authorization fields recorded on this task.") + ("\nDecision IDs cited: " + "; ".join(decisions) if decisions else "\nNo decision IDs cited in this task record."),
             "sources": sources[:2], "observed": text(task.get("requested_at")) if task.get("requested_at") else UNKNOWN},
            {"title": "Coordinating Lead / CLI session", "stage": "Recorded",
             "text": "Owner: " + text(task.get("owner_name")) + " (" + text(task.get("owner_employee_id")) + ")\n" + session_line(owner_session),
             "sources": ["tasks", "staff"], "observed": UNKNOWN},
            {"title": "Specialist assignment", "stage": "Recorded" if linked or subtasks or task.get("implementation_owner") or task.get("intended_implementer") else "Unknown",
             "text": "\n".join(
                 [w["kind"] + " " + w["id"] + " (" + by_id[w["employee"]]["name"] + "): " + " | ".join(r["origin"] + " status " + r["status"] for r in w["records"]) for w in linked]
                 + ([ "Implementation owner: " + text(task["implementation_owner"])] if task.get("implementation_owner") else [])
                 + ([ "Intended implementer: " + text(task["intended_implementer"])] if task.get("intended_implementer") else [])
                 + ([ "Subtasks: " + str(len(subtasks)) + " recorded; suggested owners are not assignments."] if subtasks else [])
             ) or "No worker session or implementer linked to this task in the records.",
             "sources": ["tasks", "staff"], "observed": UNKNOWN},
            {"title": "Report / result / review", "stage": explicit_stage(str(task.get("status", ""))),
             "text": (labelled(task, (("delivery", "Delivery"), ("review_result", "Review result"), ("lead_result", "Lead result"),
                                      ("result", "Result"), ("guardian", "Guardian (advisory)"), ("guardian_delivery", "Guardian delivery"))) or "No result/review fields recorded.")
             + ("\nEvidence references not served: " + "; ".join(unresolved) if unresolved else ""),
             "sources": refs or ["tasks"], "observed": times(task)},
        ]
        assignments.append(row("task-" + task_id, owner, lane, text(task.get("title")) if task.get("title") else task_id, task_id, status, sources,
                               blocker=blockers or UNKNOWN, next=next_action, observed=times(task), leadSession=owner_session,
                               titleRecorded=bool(task.get("title")), subtasks=subtasks, unresolved=unresolved, steps=steps,
                               stage=explicit_stage(str(task.get("status", "")))))
        for worker in linked:
            if worker["employee"] is None:
                continue
            statuses = sorted({r["status"] for r in worker["records"]})
            wrefs = []
            for record in worker["records"]:
                if record["evidence"]:
                    key, description = collector.reference(str(record["evidence"]))
                    if key and key not in wrefs:
                        wrefs.append(key)
            worker_status = "\n".join(r["origin"] + ": " + r["status"] for r in worker["records"])
            if len(statuses) > 1:
                worker_status += "\nRecords disagree; both statuses shown as recorded."
            assignments.append(row("worker-" + task_id + "-" + worker["id"], worker["employee"], lane,
                                   text(worker["records"][0]["scope"]), task_id, worker_status, ["staff", "tasks"] + wrefs,
                                   blocker="Task-level blockers:\n" + (blockers or UNKNOWN), next="Task-level next action (not a new instruction):\n" + next_action,
                                   observed="; ".join(r["origin"] + " " + r["time"] for r in worker["records"]),
                                   leadSession=owner_session, worker=worker["id"], workerKind=worker["kind"],
                                   stage=explicit_stage(statuses[0]) if len(statuses) == 1 else "Recorded",
                                   steps=[steps[0], steps[1],
                                          {"title": "Specialist assignment", "stage": "Recorded",
                                           "text": worker["kind"] + ": " + worker["id"] + "\n" + worker_status, "sources": ["staff", "tasks"], "observed": UNKNOWN},
                                          {"title": "Report / result", "stage": "Recorded",
                                           "text": "Worker evidence: " + (", ".join(wrefs) if wrefs else "none resolved") + "\nTask result:\n" + steps[3]["text"],
                                           "sources": wrefs or ["tasks"], "observed": "; ".join(r["time"] for r in worker["records"])}]))

    # Worker sessions whose task is not in tasks.json stay visible, never dropped.
    for (worker_id, task_id), worker in workers.items():
        if task_id in task_index or worker["employee"] is None:
            continue
        assignments.append(row("worker-" + task_id + "-" + worker_id, worker["employee"], "closed", text(worker["records"][0]["scope"]), task_id,
                               "\n".join(r["origin"] + ": " + r["status"] for r in worker["records"]) + "\nTask " + task_id + " not found in tasks.json; lane unknown, shown with closed records.",
                               ["staff"], worker=worker_id, workerKind=worker["kind"],
                               steps=[{"title": t, "stage": "Unknown", "text": "Task record not found; provenance unknown.", "sources": ["staff"], "observed": UNKNOWN}
                                      for t in ("User request / authorization", "Coordinating Lead / CLI session", "Specialist assignment", "Report / result")]))

    employees = []
    for employee in staff:
        identifier = employee["employee_id"]
        mine = [a for a in assignments if a["employee"] == identifier]
        open_count = sum(1 for a in mine if a["lane"] == "open")
        closed_count = len(mine) - open_count
        suggested = suggestions.get(identifier, [])
        profile = profiles[identifier]
        label = "{} open · {} closed in records".format(open_count, closed_count) if mine else "No recorded assignments"
        note = ("Suggested owner for {} pending subtask{} — suggested, not assigned.".format(len(suggested), "" if len(suggested) == 1 else "s")
                if suggested else "No pending subtask suggestions.")
        employees.append({"id": identifier, "name": employee["name"], "role": employee["title"],
                          "roleProfile": employee["role_profile"],
                          "color": COLORS.get(identifier, "#e1e8dd"), "label": label, "note": note,
                          "aliases": [a for a in employee.get("aliases", []) if isinstance(a, str)],
                          "profile": profile, "suggested": suggested, "openCount": open_count, "closedCount": closed_count,
                          "scope": "project"})
        if open_count == 0:
            assignments.append(row("idle-" + identifier, identifier, "idle", "No open assignment recorded", None,
                                   "Closed records: {}. Availability unknown; no open task or worker session in the records.".format(closed_count),
                                   ["staff", "tasks"], stage="Unknown",
                                   blocker="Unknown; no open assignment", next=("Pending subtasks suggested (not assigned):\n" + "\n".join("• " + s for s in suggested)) if suggested else "Unknown; no action inferred",
                                   steps=[{"title": t, "stage": "Unknown", "text": msg, "sources": ["staff", "tasks"], "observed": UNKNOWN} for t, msg in (
                                       ("User request / authorization", "No open task authorizes work for this employee."),
                                       ("Coordinating Lead / CLI session", "No coordinating session linked to an open assignment."),
                                       ("Specialist assignment", ("Suggested owner (not an assignment) for: " + "; ".join(suggested)) if suggested else "No dispatch or worker ID recorded."),
                                       ("Report / result", "No open result recorded."))]))

    dashboard_config = records["dashboard"]
    office, office_problem = office_agents(collector, staff, agents_dir, dashboard_config)
    runs, runs_problem = build_runs(collector)
    binding = records["binding"]
    repository = binding.get("repository", {}) if isinstance(binding.get("repository"), dict) else {}
    heading, snapshot_text = md_section(collector.contents.get("state", ""), r"Current snapshot")
    ground = re.search(r"\*\*(Execution ground rule[\s\S]*?)\*\*", collector.contents.get("brief", ""))
    return {
        "employees": employees + office, "officeAgentsProblem": office_problem,
        "projectStaffCount": len(employees), "officeAgentCount": len(office),
        "assignments": assignments, "leadSessions": lead_sessions,
        "currentLeadSessions": [s["id"] for s in lead_sessions if s["current"]],
        "runs": runs, "runsProblem": runs_problem,
        "sources": collector.sources,
        "project": {"id": binding["project_id"], "displayName": text(binding.get("display_name")),
                    "status": text(binding.get("status")), "repositoryStatus": text(repository.get("status")),
                    "readinessScope": text(repository.get("readiness_scope")),
                    "groundRule": " ".join(ground.group(1).split()) if ground else UNKNOWN,
                    "currentSnapshotHeading": heading or "Current snapshot section not found in PROJECT_STATE.md",
                    "currentSnapshot": text(snapshot_text, 9000) if snapshot_text else UNKNOWN},
        "taskCount": len(tasks), "openTaskCount": sum(1 for t in tasks if not is_closed(t)),
        "activeRunCount": sum(1 for r in runs if r["recordedActive"]),
        "dashboard": {
            "timezone": dashboard_config.get("timezone"),
            "planAic": dashboard_config.get("plan_aic"),
            "aicPerLogUnit": dashboard_config.get("aic_per_log_unit"),
            "calibration": dashboard_config.get("calibration"),
            "frontdesk": dashboard_config.get("frontdesk"),
        },
    }


def collect(root, agents_dir=USER_AGENTS):
    root = Path(root)
    collector = Collector(root)
    for key, relative in RECORDS.items():
        collector.add(key, relative, "Office record." if key not in ("registry", "runs-readme") else "Run registry (user-launched runs; the dashboard never controls runs).",
                      parse_json=relative.endswith(".json"), required=key in REQUIRED)
    snapshot = build(collector, agents_dir)
    # Contents are never exposed; only metadata travels in the feed.
    revision = hashlib.sha256(json.dumps(collector.sources, sort_keys=True).encode()).hexdigest()
    return {
        "schemaVersion": 2, "projectId": collector.records["binding"]["project_id"], "mode": "record-feed",
        "collectedAt": utc(collector.collected.timestamp()), "revision": revision, "pollSeconds": 5,
        "telemetry": "unavailable", "errors": collector.errors, "snapshot": snapshot,
    }


def served_paths(root, agents_dir=USER_AGENTS):
    result = collect(root, agents_dir)
    return {source["path"] for source in result["snapshot"]["sources"].values() if source.get("served") and not source.get("error")}


def embed_snapshot(root, target=DASHBOARD / "index.html", agents_dir=USER_AGENTS):
    payload = collect(root, agents_dir)
    payload["mode"] = "embedded-snapshot"
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/").replace("<!--", "<\\!--")
    html = target.read_text(encoding="utf-8")
    pattern = re.compile(r'(<script id="snapshot" type="application/json">)[\s\S]*?(</script>)')
    if len(pattern.findall(html)) != 1:
        raise RecordError("index.html must contain exactly one embedded snapshot block")
    updated = pattern.sub(lambda m: m.group(1) + "\n" + data + "\n  " + m.group(2), html)
    temporary = target.with_suffix(".html.tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.replace(temporary, target)
    return payload


class OfficeActivity:
    """Owns the session-log index; refreshes it on a background thread."""

    def __init__(self, index=None, interval=10.0, project_id=None, office=None, sessions=None):
        self.index = index
        self.interval = interval
        self.error = None
        self.revision = None
        self._payload = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.project_id = project_id
        self.office = office
        self.sessions = sessions

    def refresh_once(self):
        try:
            if self.index is None:
                from session_index import SessionIndex
                self.index = SessionIndex(office=self.office, root=self.sessions)
            self.index.refresh()
            snapshot = self.index.snapshot()
            stable = {k: v for k, v in snapshot.items() if k != "generatedAt"}
            revision = hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()
            with self._lock:
                self._payload = {"schemaVersion": 1, "projectId": self.project_id, "revision": revision,
                                 "collectedAt": utc(), "snapshot": snapshot}
                self.error = None
        except Exception as error:  # keep serving the last good index
            logging.exception("Session index refresh failed")
            with self._lock:
                self.error = type(error).__name__ + ": " + str(error)[:300]

    def start(self):
        def loop():
            while not self._stop.is_set():
                started = time.monotonic()
                self.refresh_once()
                self._stop.wait(max(1.0, self.interval - (time.monotonic() - started)))
        threading.Thread(target=loop, name="session-index", daemon=True).start()

    def stop(self):
        self._stop.set()

    def payload(self):
        with self._lock:
            if self._payload is None:
                return None, self.error or "Session index is still building"
            # Last good index, flagged when the latest refresh failed (kept outside the revision hash).
            return dict(self._payload, stale=self.error is not None, error=self.error), None


def handler_for(root, allowed_hosts=(), agents_dir=USER_AGENTS, activity=None):
    allowed = {"127.0.0.1", "localhost", *allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "OfficeRecords/2"

        def send(self, status, body, content_type="application/json; charset=utf-8"):
            if isinstance(body, dict):
                body = json.dumps(body, ensure_ascii=True).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; connect-src 'self'; form-action 'none'")
            self.end_headers()
            try:
                if self.command != "HEAD":
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self.log_message("Client disconnected before response completed")

        def trusted_request(self):
            try:
                host = urlsplit("http://" + self.headers.get("Host", ""))
                _ = host.port
                if host.hostname not in allowed or host.username or host.password or host.path or host.query or host.fragment:
                    return False
                origin = self.headers.get("Origin")
                if origin:
                    parsed = urlsplit(origin)
                    if parsed.scheme not in ("http", "https") or parsed.netloc != self.headers.get("Host") or parsed.path or parsed.query or parsed.fragment:
                        return False
                return self.headers.get("Sec-Fetch-Site") != "cross-site"
            except ValueError:
                return False

        def do_GET(self):
            if not self.trusted_request():
                self.send(403, {"error": "Host/origin not allowed. Use private localhost forwarding or explicitly configure the exact forwarded host."})
                return
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc:
                self.send(400, {"error": "Only local request paths are accepted"})
                return
            try:
                if parsed.path in ("/", "/index.html") and not parsed.query:
                    html = read_local(DASHBOARD, "index.html")[0]
                    self.send(200, html, "text/html; charset=utf-8")
                elif parsed.path == "/api/office" and not parsed.query:
                    payload, problem = activity.payload() if activity else (None, "Session activity is not enabled on this server")
                    if payload is None:
                        self.send(503, {"error": problem, "collectedAt": utc()})
                    else:
                        self.send(200, payload)
                elif parsed.path == "/api/status" and not parsed.query:
                    self.send(200, collect(root, agents_dir))
                elif parsed.path == "/evidence":
                    params = parse_qs(parsed.query)
                    relative = params.get("source", [])
                    if set(params) != {"source"} or len(relative) != 1 or relative[0] not in served_paths(root, agents_dir):
                        self.send(404, {"error": "Evidence source is not allowlisted"})
                        return
                    self.send(200, read_local(root, relative[0])[0], "text/plain; charset=utf-8")
                else:
                    self.send(404, {"error": "Not found"})
            except RecordError as error:
                logging.warning("Feed request failed: %s", error)
                self.send(503, {"error": str(error), "collectedAt": utc()})

        do_HEAD = do_GET

        def do_POST(self):
            self.send(405, {"error": "Read-only dashboard; no mutation or command endpoints"})

        do_PUT = do_POST
        do_DELETE = do_POST
        do_PATCH = do_POST

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--office", type=Path, default=os.environ.get("OFFICE_HOME"),
                        help="Office home (or set OFFICE_HOME)")
    parser.add_argument("--sessions", type=Path, default=Path("~/.copilot/session-state").expanduser())
    parser.add_argument("--allowed-host", action="append", default=[], help="Exact trusted forwarded hostname (no scheme, wildcard or port)")
    parser.add_argument("--embed-snapshot", action="store_true", help="Rewrite only index.html's embedded offline snapshot, then exit")
    args = parser.parse_args()
    if args.office is None:
        parser.error("--office is required unless OFFICE_HOME is set")
    office = args.office.expanduser().resolve()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.embed_snapshot:
        payload = embed_snapshot(office)
        print("Embedded snapshot collected at " + payload["collectedAt"] + " with " + str(len(payload["errors"])) + " source errors.")
        return
    if any(not re.fullmatch(r"[A-Za-z0-9.-]+", host) for host in args.allowed_host):
        parser.error("--allowed-host requires an exact hostname")
    binding = json.loads((office / ".agent-office/project.json").read_text(encoding="utf-8"))
    project_id = binding.get("project_id")
    activity = OfficeActivity(project_id=project_id, office=office, sessions=args.sessions)
    activity.start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(office, allowed_hosts=args.allowed_host, activity=activity))
    server.daemon_threads = True
    print(f"Office record feed: http://127.0.0.1:{args.port} (loopback, read-only; no worker telemetry). Root: {office}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Office record feed stopped.", flush=True)
    finally:
        activity.stop()
        server.server_close()


if __name__ == "__main__":
    main()

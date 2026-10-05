"""Incremental, read-only index of Copilot CLI session event logs.

The index deliberately keeps only the small subset of event data needed by the
office dashboard.  In particular, large model and message events are rejected
from their JSON prefix without deserializing them.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


SCHEMA_VERSION = 1
CACHE_VERSION = 6
DEFAULT_ROOT = Path("~/.copilot/session-state").expanduser()
CACHE_NAME = "session-index-v6.json"
MAX_EVENT_LINE_BYTES = 8 * 1024 * 1024
MAX_PROMPT_CHARS = 4000
MAX_TITLE_CHARS = 300

INTERESTING_TYPES = {
    "abort",
    "session.start",
    "session.resume",
    "subagent.selected",
    "tool.execution_start",
    "subagent.started",
    "subagent.configured",
    "subagent.completed",
    "system.notification",
    "session.usage_checkpoint",
    "session.shutdown",
    "session.compaction_start",
    "session.compaction_complete",
    "user.message",
    "assistant.turn_start",
    "assistant.turn_end",
}
TYPE_PREFIX = re.compile(br'^\s*\{\s*"type"\s*:\s*"([^"]+)"')
LOCK_NAME = re.compile(r"^inuse\.(\d+)\.lock$")
LOGGER = logging.getLogger(__name__)


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: Any) -> str | None:
    parsed = value if isinstance(value, datetime) else _parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _new_state() -> dict[str, Any]:
    return {
        "malformed": 0,
        "earliest": None,
        "latest": None,
        "last_resume": None,
        "first_user_content": None,
        "first_user_ts": None,
        "last_user_ts": None,
        "user_messages": [],
        "main_selections": [],
        "current_main": None,
        "main_open_turns": {},
        "agent_open_turns": {},
        "task_calls": {},
        "delegations": {},
        "usage_running_max": 0,
        "usage_deltas": [],
        "premium_max": 0,
        "context": {
            "currentTokens": None,
            "conversationTokens": None,
            "observedAt": None,
        },
        "reported": {},
    }


def _flat_yaml(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result
    for line in text.splitlines():
        if not line or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        result[key] = value
    return result


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, ValueError, OverflowError):
        return False


class SessionIndex:
    """Incrementally index session-state and produce dashboard snapshots."""

    def __init__(
        self,
        root: os.PathLike[str] | str = DEFAULT_ROOT,
        staff_path: os.PathLike[str] | str | None = None,
        cache_dir: os.PathLike[str] | str | None = None,
        office: os.PathLike[str] | str | None = None,
        config_path: os.PathLike[str] | str | None = None,
        now: Callable[[], datetime] = _default_now,
    ) -> None:
        self.office = Path(office).expanduser() if office is not None else None
        self.root = Path(root).expanduser()
        if staff_path is None:
            if self.office is None:
                raise ValueError("office or staff_path is required")
            staff_path = self.office / ".agent-office/staff.json"
        if config_path is None:
            if self.office is None:
                raise ValueError("office or config_path is required")
            config_path = self.office / ".agent-office/dashboard.json"
        if cache_dir is None:
            if self.office is None:
                raise ValueError("office or cache_dir is required")
            cache_dir = self.office / ".agent-office/.dashboard-cache"
        self.staff_path = Path(staff_path).expanduser()
        self.config_path = Path(config_path).expanduser()
        self.cache_dir = Path(cache_dir).expanduser()
        self.cache_path = self.cache_dir / CACHE_NAME
        self.now = now
        self._lock = threading.RLock()
        self._building = False
        self._cache_dirty = False
        self._config = self._load_config()
        self.timezone = ZoneInfo(self._config["timezone"])
        self.factor = self._config.get("aic_per_log_unit")
        self.calibrated = isinstance(self.factor, (int, float)) and not isinstance(self.factor, bool)
        self._staff, self._roles, self._agent_order = self._load_staff()
        self._sessions: dict[str, dict[str, Any]] = self._load_cache()

    def _load_config(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"Cannot load dashboard configuration: {self.config_path}") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("timezone"), str):
            raise ValueError("Dashboard configuration requires a timezone")
        try:
            ZoneInfo(payload["timezone"])
        except Exception as error:
            raise ValueError("Dashboard timezone is invalid") from error
        factor = payload.get("aic_per_log_unit")
        if factor is not None and (
            not isinstance(factor, (int, float)) or isinstance(factor, bool)
            or not math.isfinite(factor) or factor <= 0
        ):
            raise ValueError("aic_per_log_unit must be null or a positive number")
        frontdesk = payload.get("frontdesk")
        if not isinstance(frontdesk, dict) or not isinstance(frontdesk.get("name"), str):
            raise ValueError("Dashboard configuration requires frontdesk.name")
        return payload

    def _load_staff(
        self,
    ) -> tuple[dict[str, str], dict[str, str], list[dict[str, str]]]:
        try:
            payload = json.loads(self.staff_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"Cannot load staff directory: {self.staff_path}") from error
        staff: dict[str, str] = {}
        roles: dict[str, str] = {}
        order: list[dict[str, str]] = []
        for employee in payload.get("employees", []):
            if not isinstance(employee, dict):
                continue
            name = employee.get("name")
            title = employee.get("title")
            role = employee.get("role_profile")
            if not isinstance(name, str) or not isinstance(title, str):
                continue
            staff[name] = title
            if isinstance(role, str) and role:
                roles[role] = name
            order.append({"name": name, "title": title, "scope": "project"})
        frontdesk = self._config["frontdesk"]
        order.append({"name": frontdesk["name"], "title": frontdesk.get("title") or "Front Desk", "scope": "office"})
        return staff, roles, order

    def _canonical(self, name: Any) -> str | None:
        if not isinstance(name, str) or not name:
            return None
        return self._roles.get(name, name)

    def _is_office_name(self, name: Any) -> bool:
        canonical = self._canonical(name)
        return canonical in self._staff or canonical == self._config["frontdesk"]["name"]

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if payload.get("cacheVersion") != CACHE_VERSION:
                return {}
            sessions = payload.get("sessions")
            if not isinstance(sessions, dict):
                return {}
            for item in sessions.values():
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("state"), dict)
                    or not isinstance(item.get("offset"), int)
                    or not isinstance(item.get("inode"), int)
                    or not isinstance(item.get("size"), int)
                ):
                    return {}
            return sessions
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            return {}

    def _save_cache(self, sessions: dict[str, dict[str, Any]]) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_dir / (
            f".{CACHE_NAME}.tmp.{os.getpid()}.{threading.get_ident()}"
        )
        payload = {
            "cacheVersion": CACHE_VERSION,
            "sessions": sessions,
        }
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.cache_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def refresh(self) -> None:
        """Refresh changed logs, preserving the previous snapshot while building."""
        with self._lock:
            if self._building:
                return
            self._building = True
            previous = copy.deepcopy(self._sessions)
            cache_dirty = self._cache_dirty
        try:
            result: dict[str, dict[str, Any]] = {}
            try:
                directories = sorted(
                    item
                    for item in self.root.iterdir()
                    if item.is_dir()
                )
            except OSError as error:
                LOGGER.warning("Cannot scan session-state root %s: %s", self.root, error)
                return
            cache_changed = cache_dirty
            for directory in directories:
                session_id = directory.name
                events = directory / "events.jsonl"
                old = previous.get(session_id)
                try:
                    info = events.stat()
                except OSError as error:
                    if old:
                        LOGGER.warning(
                            "Cannot stat session log %s: %s", events, error
                        )
                        result[session_id] = old
                    continue
                unchanged = bool(
                    old
                    and old.get("inode") == info.st_ino
                    and old.get("size") == info.st_size
                )
                if unchanged:
                    item = old
                else:
                    can_append = bool(
                        old
                        and old.get("inode") == info.st_ino
                        and info.st_size >= old.get("size", -1)
                        and 0 <= old.get("offset", -1) <= info.st_size
                    )
                    state = (
                        copy.deepcopy(old["state"]) if can_append else _new_state()
                    )
                    offset = old["offset"] if can_append else 0
                    try:
                        offset = self._parse_file(events, offset, state)
                    except OSError as error:
                        LOGGER.warning("Cannot read session log %s: %s", events, error)
                        if old:
                            item = old
                        else:
                            continue
                    else:
                        item = {
                            "inode": info.st_ino,
                            "size": info.st_size,
                            "offset": offset,
                            "state": state,
                        }
                item["workspace"] = _flat_yaml(directory / "workspace.yaml")
                result[session_id] = item
                old_position = (
                    old.get("inode"),
                    old.get("size"),
                    old.get("offset"),
                ) if old else None
                new_position = (
                    item.get("inode"),
                    item.get("size"),
                    item.get("offset"),
                )
                if old_position != new_position:
                    cache_changed = True
            if set(result) != set(previous):
                cache_changed = True
            cache_failed = False
            if cache_changed:
                try:
                    self._save_cache(result)
                except Exception as error:
                    cache_failed = True
                    LOGGER.warning("Cannot save session index cache %s: %s", self.cache_path, error)
            with self._lock:
                self._sessions = result
                self._cache_dirty = cache_failed
        finally:
            with self._lock:
                self._building = False

    def _parse_file(
        self, path: Path, offset: int, state: dict[str, Any]
    ) -> int:
        with path.open("rb") as stream:
            stream.seek(offset)
            complete_offset = offset
            while True:
                line_start = stream.tell()
                raw = stream.readline(MAX_EVENT_LINE_BYTES + 1)
                if not raw:
                    break
                oversized = len(raw) > MAX_EVENT_LINE_BYTES and not raw.endswith(b"\n")
                if oversized:
                    prefix = raw[:256]
                    complete = False
                    while True:
                        tail = stream.readline(MAX_EVENT_LINE_BYTES + 1)
                        if not tail:
                            break
                        if tail.endswith(b"\n"):
                            complete = True
                            break
                    if not complete:
                        complete_offset = line_start
                        break
                    event_type = self._prefix_type(prefix)
                    if event_type is None or event_type in INTERESTING_TYPES:
                        state["malformed"] += 1
                    complete_offset = stream.tell()
                    continue
                if not raw.endswith(b"\n"):
                    complete_offset = line_start
                    break
                complete_offset = stream.tell()
                event_type = self._prefix_type(raw[:256])
                if event_type is not None and event_type not in INTERESTING_TYPES:
                    continue
                try:
                    event = json.loads(raw.decode("utf-8", errors="replace"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    state["malformed"] += 1
                    continue
                if not isinstance(event, dict):
                    state["malformed"] += 1
                    continue
                if event.get("type") not in INTERESTING_TYPES:
                    continue
                try:
                    self._consume(state, event)
                except Exception:
                    state["malformed"] += 1
            return complete_offset

    @staticmethod
    def _prefix_type(prefix: bytes) -> str | None:
        match = TYPE_PREFIX.match(prefix)
        if not match:
            return None
        return match.group(1).decode("ascii", errors="replace")

    @staticmethod
    def _touch_time(state: dict[str, Any], timestamp: Any) -> str | None:
        normalized = _iso(timestamp)
        if normalized is None:
            return None
        if state["earliest"] is None or normalized < state["earliest"]:
            state["earliest"] = normalized
        if state["latest"] is None or normalized > state["latest"]:
            state["latest"] = normalized
        return normalized

    def _consume(self, state: dict[str, Any], event: dict[str, Any]) -> None:
        kind = event.get("type")
        data = event.get("data")
        if not isinstance(data, dict):
            data = {}
        timestamp = self._touch_time(state, event.get("timestamp"))
        agent_id = event.get("agentId")
        top_level = "agentId" not in event

        if kind in {
            "abort",
            "session.start",
            "session.resume",
            "session.shutdown",
        }:
            state["main_open_turns"].clear()
            state["agent_open_turns"].clear()

        # Start, resume and shutdown all end background subagents; "last_resume" is the
        # latest such boundary (exposed as lastResume).
        if kind in {"session.start", "session.resume", "session.shutdown"} and top_level:
            if timestamp and (
                state["last_resume"] is None or timestamp > state["last_resume"]
            ):
                state["last_resume"] = timestamp
            if kind != "session.shutdown":
                return

        if kind == "subagent.selected" and top_level:
            name = self._canonical(data.get("agentName"))
            if name:
                state["current_main"] = name
                state["main_selections"].append([timestamp, name])
            return

        if kind == "user.message" and top_level:
            # A new top-level request supersedes any unterminated main turn.
            # Subagents may legitimately continue across user messages.
            state["main_open_turns"].clear()
            content = data.get("content")
            if state["first_user_content"] is None and isinstance(content, str):
                state["first_user_content"] = content[:MAX_TITLE_CHARS]
                state["first_user_ts"] = timestamp
            if timestamp:
                state["last_user_ts"] = timestamp
                state["user_messages"].append(timestamp)
            return

        if kind == "assistant.turn_start":
            turn_id = str(data.get("turnId") or "")
            if top_level:
                state["main_open_turns"][turn_id] = timestamp
            elif isinstance(agent_id, str):
                state["agent_open_turns"].setdefault(agent_id, {})[turn_id] = timestamp
            return

        if kind == "assistant.turn_end":
            turn_id = str(data.get("turnId") or "")
            if top_level:
                state["main_open_turns"].pop(turn_id, None)
            elif isinstance(agent_id, str):
                state["agent_open_turns"].setdefault(agent_id, {}).pop(turn_id, None)
            return

        if kind == "tool.execution_start":
            tool = data.get("toolName")
            arguments = data.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            if tool == "task":
                call_id = data.get("toolCallId")
                if isinstance(call_id, str):
                    parent = None
                    issuer = "unknown"
                    if isinstance(agent_id, str):
                        parent_record = state["delegations"].get(agent_id)
                        if parent_record:
                            parent = agent_id
                            issuer = parent_record.get("agent") or "unknown"
                    elif top_level and state.get("current_main"):
                        issuer = state["current_main"]
                    state["task_calls"][call_id] = {
                        "parent": parent,
                        "issuer": issuer,
                        "timestamp": timestamp,
                        "arguments": {
                            "name": arguments.get("name"),
                            "description": arguments.get("description"),
                            "prompt": (
                                arguments.get("prompt")[:MAX_PROMPT_CHARS]
                                if isinstance(arguments.get("prompt"), str)
                                else arguments.get("prompt")
                            ),
                            "agent_type": arguments.get("agent_type"),
                            "mode": arguments.get("mode"),
                        },
                    }
            elif tool == "write_agent":
                targets: list[str] = []
                if isinstance(arguments.get("agent_id"), str):
                    targets.append(arguments["agent_id"])
                if isinstance(arguments.get("agent_ids"), list):
                    targets.extend(
                        value
                        for value in arguments["agent_ids"]
                        if isinstance(value, str)
                    )
                for target in dict.fromkeys(targets):
                    delegation = state["delegations"].get(target)
                    if delegation:
                        delegation["followUps"] += 1
                        delegation["turns"] += 1
                        delegation["lastRequest"] = timestamp
                        delegation["lastActivity"] = timestamp
            return

        if kind == "subagent.started" and isinstance(agent_id, str):
            call_id = data.get("toolCallId")
            pending = (
                state["task_calls"].pop(call_id, {})
                if isinstance(call_id, str)
                else {}
            )
            arguments = pending.get("arguments", {})
            raw_name = data.get("agentName") or data.get("agentType")
            raw_type = data.get("agentType") or arguments.get("agent_type")
            canonical = self._canonical(raw_name) or self._canonical(raw_type) or "unknown"
            role = raw_name in self._roles or raw_type in self._roles
            builtin = not role and canonical not in self._staff and canonical != self._config["frontdesk"]["name"]
            existing = state["delegations"].get(agent_id)
            if existing:
                existing["lastRequest"] = timestamp
                existing["lastActivity"] = timestamp
                existing["model"] = data.get("model") or existing.get("model")
                existing["mode"] = (
                    data.get("executionMode") or existing.get("mode")
                )
                return
            state["delegations"][agent_id] = {
                "id": agent_id,
                "agent": canonical,
                "rawAgent": raw_name,
                "agentType": raw_type,
                "role": bool(role),
                "builtin": bool(builtin),
                "parent": pending.get("parent"),
                "issuer": pending.get("issuer", "unknown"),
                "task": {
                    "name": arguments.get("name"),
                    "description": arguments.get("description"),
                    "prompt": arguments.get("prompt"),
                },
                "model": data.get("model"),
                "effort": None,
                "mode": data.get("executionMode") or arguments.get("mode"),
                "started": timestamp,
                "lastRequest": timestamp,
                "lastActivity": timestamp,
                "completionTimes": [],
                "turns": 1,
                "followUps": 0,
                "durationMs": 0,
                "tokens": 0,
                "toolCalls": 0,
                "hasDuration": False,
                "hasTokens": False,
                "hasToolCalls": False,
            }
            return

        if kind == "subagent.configured" and isinstance(agent_id, str):
            delegation = state["delegations"].get(agent_id)
            if delegation:
                delegation["model"] = data.get("model") or delegation.get("model")
                delegation["effort"] = (
                    data.get("reasoningEffort") or delegation.get("effort")
                )
                delegation["lastActivity"] = timestamp
            return

        # Follow-up turns of background agents end with an "agent_idle" notification
        # to the main agent; only the first turn emits subagent.completed.
        if kind == "system.notification":
            note = data.get("kind")
            idle_id = note.get("agentId") if isinstance(note, dict) and note.get("type") == "agent_idle" else None
            delegation = state["delegations"].get(idle_id) if isinstance(idle_id, str) else None
            if delegation:
                delegation["completionTimes"].append(timestamp)
                delegation["lastActivity"] = timestamp
            return

        if kind == "subagent.completed" and isinstance(agent_id, str):
            delegation = state["delegations"].get(agent_id)
            if delegation:
                delegation["completionTimes"].append(timestamp)
                delegation["lastActivity"] = timestamp
                delegation["model"] = data.get("model") or delegation.get("model")
                for source, target, marker in (
                    ("durationMs", "durationMs", "hasDuration"),
                    ("totalTokens", "tokens", "hasTokens"),
                    ("totalToolCalls", "toolCalls", "hasToolCalls"),
                ):
                    value = data.get(source)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        delegation[target] += value
                        delegation[marker] = True
            return

        if kind == "session.usage_checkpoint":
            value = data.get("totalNanoAiu")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                old = state["usage_running_max"]
                delta = max(0, value - old)
                state["usage_running_max"] = max(old, value)
                if delta and timestamp:
                    state["usage_deltas"].append(
                        [timestamp, delta, state.get("current_main")]
                    )
            premium = data.get("totalPremiumRequests")
            if isinstance(premium, (int, float)) and not isinstance(premium, bool):
                state["premium_max"] = max(state["premium_max"], premium)
            return

        if kind == "session.shutdown":
            premium = data.get("totalPremiumRequests")
            if isinstance(premium, (int, float)) and not isinstance(premium, bool):
                state["premium_max"] = max(state["premium_max"], premium)
            metrics = data.get("agentMetrics")
            if isinstance(metrics, dict):
                for metric_id, metric in metrics.items():
                    if metric_id == "main" or not isinstance(metric, dict):
                        continue
                    value = metric.get("totalNanoAiu")
                    if (
                        isinstance(metric_id, str)
                        and isinstance(value, (int, float))
                        and not isinstance(value, bool)
                    ):
                        state["reported"][metric_id] = {
                            "timestamp": timestamp,
                            "nanoAiu": value,
                        }
            self._update_context(state, data, timestamp, complete=False)
            return

        if kind in {"session.compaction_start", "session.compaction_complete"}:
            self._update_context(
                state,
                data,
                timestamp,
                complete=kind == "session.compaction_complete",
            )

    @staticmethod
    def _update_context(
        state: dict[str, Any],
        data: dict[str, Any],
        timestamp: str | None,
        complete: bool,
    ) -> None:
        context = state["context"]
        changed = False
        for source, target in (
            ("currentTokens", "currentTokens"),
            ("conversationTokens", "conversationTokens"),
        ):
            value = data.get(source)
            if isinstance(value, int) and not isinstance(value, bool):
                context[target] = value
                changed = True
        if complete:
            post = data.get("postCompactionTokens")
            if isinstance(post, int) and not isinstance(post, bool):
                if "conversationTokens" not in data:
                    context["conversationTokens"] = post
                if "currentTokens" not in data:
                    context["currentTokens"] = post
                changed = True
        if changed:
            context["observedAt"] = timestamp

    def snapshot(self) -> dict[str, Any]:
        """Return an independent, now-sensitive dashboard snapshot."""
        with self._lock:
            sessions = copy.deepcopy(self._sessions)
            building = self._building
            staff = dict(self._staff)
            agent_order = copy.deepcopy(self._agent_order)
        current = self.now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        local_now = current.astimezone(self.timezone)
        week_start = (local_now - timedelta(days=local_now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        week_end = week_start + timedelta(days=7)
        # Task months follow the credit plan reset: 00:00 UTC on the 1st
        # Weeks and days use the configured display timezone.
        credits_month_start = current.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        month_start = credits_month_start
        utc_month = (current.year, current.month)

        rendered: list[dict[str, Any]] = []
        coverage_since: datetime | None = None
        malformed = 0
        bytes_indexed = 0
        other_sessions = 0
        task_records: list[dict[str, Any]] = []
        month_usage_by_agent: dict[str, float] = {}
        global_month_usage = 0.0

        for session_id, item in sessions.items():
            state = item["state"]
            workspace = item.get("workspace", {})
            malformed += int(state.get("malformed", 0))
            bytes_indexed += int(item.get("offset", 0))
            first_candidates = [
                value
                for value in (
                    _parse_datetime(state.get("earliest")),
                    _parse_datetime(workspace.get("created_at")),
                )
                if value is not None
            ]
            first_seen = min(first_candidates) if first_candidates else None
            if first_seen and (coverage_since is None or first_seen < coverage_since):
                coverage_since = first_seen

            # Credits are account-wide, including sessions outside office scope.
            # Detailed sessions, tasks, and per-agent attribution remain office-only.
            for timestamp, nano, _agent in state.get("usage_deltas", []):
                when = _parse_datetime(timestamp)
                if when and when <= current and (when.year, when.month) == utc_month:
                    global_month_usage += float(nano) / 1_000_000_000

            office = any(
                self._is_office_name(selection[1])
                for selection in state.get("main_selections", [])
                if isinstance(selection, list) and len(selection) == 2
            )
            if not office:
                other_sessions += 1
                continue

            attached = self._attached(self.root / session_id)
            main_in_progress = bool(state.get("main_open_turns"))
            status = (
                "working"
                if attached and main_in_progress
                else "open"
                if attached
                else "closed"
            )
            created = (
                _iso(workspace.get("created_at"))
                or state.get("earliest")
                or state.get("first_user_ts")
            )
            updated_candidates = [
                value
                for value in (
                    _parse_datetime(workspace.get("updated_at")),
                    _parse_datetime(state.get("latest")),
                    _parse_datetime(state.get("last_user_ts")),
                    _parse_datetime(created),
                )
                if value is not None
            ]
            updated = _iso(max(updated_candidates)) if updated_candidates else created
            direct_started = state.get("first_user_ts") or created
            direct_owner = self._main_at(state, direct_started)
            if direct_owner is None:
                direct_owner = next(
                    (
                        selection[1]
                        for selection in state.get("main_selections", [])
                        if len(selection) == 2 and self._is_office_name(selection[1])
                    ),
                    "unknown",
                )
            task_records.append(
                {
                    "kind": "direct",
                    "session": session_id,
                    "delegation": None,
                    "agent": direct_owner,
                    "issuer": "you",
                    "started": direct_started,
                    "open": attached,
                }
            )

            delegations = []
            day_delegations: dict[str, list[str]] = {}
            for delegation_id, raw in state.get("delegations", {}).items():
                delegation = self._render_delegation(
                    raw, state, staff, attached
                )
                delegations.append(delegation)
                day = self._local_day(delegation["started"])
                if day:
                    day_delegations.setdefault(day, []).append(delegation_id)
                task_records.append(
                    {
                        "kind": "delegation",
                        "session": session_id,
                        "delegation": delegation_id,
                        "agent": delegation["agent"],
                        "issuer": delegation["issuer"],
                        "started": delegation["started"],
                        "open": delegation["status"] == "running",
                        "tokens": delegation["tokens"] or 0,
                    }
                )
            delegations.sort(
                key=lambda value: _parse_datetime(value["started"])
                or datetime.min.replace(tzinfo=timezone.utc)
            )

            message_counts: dict[str, int] = {}
            for timestamp in state.get("user_messages", []):
                day = self._local_day(timestamp)
                if day:
                    message_counts[day] = message_counts.get(day, 0) + 1
            days = [
                {
                    "date": day,
                    "messages": message_counts.get(day, 0),
                    "delegations": day_delegations.get(day, []),
                }
                for day in sorted(set(message_counts) | set(day_delegations))
            ]

            usage_total = 0.0
            usage_month = 0.0
            usage_week = 0.0
            # Per (credit month, local day) so the UI can total any period; a
            # local day may be split at the 00:00 UTC credit reset.
            usage_by_day: dict[tuple[str, str], float] = {}
            for timestamp, nano, agent in state.get("usage_deltas", []):
                when = _parse_datetime(timestamp)
                if when is None:
                    continue
                units = float(nano) / 1_000_000_000
                usage_total += units
                day_key = (f"{when.year:04d}-{when.month:02d}", when.astimezone(self.timezone).date().isoformat())
                usage_by_day[day_key] = usage_by_day.get(day_key, 0.0) + units
                if when <= current and (when.year, when.month) == utc_month:
                    usage_month += units
                    if self._is_office_name(agent):
                        canonical = self._canonical(agent) or "unknown"
                        month_usage_by_agent[canonical] = (
                            month_usage_by_agent.get(canonical, 0.0) + units
                        )
                local_when = when.astimezone(self.timezone)
                if when <= current and week_start <= local_when < week_end:
                    usage_week += units

            name = workspace.get("name") or session_id
            fallback = state.get("first_user_content") or session_id
            title = workspace.get("name") or fallback
            agents: list[str] = []
            for _, name_at_time in state.get("main_selections", []):
                canonical = self._canonical(name_at_time)
                if canonical and canonical not in agents:
                    agents.append(canonical)
            rendered.append(
                {
                    "id": session_id,
                    "name": name,
                    "title": title,
                    "agents": agents,
                    "created": created,
                    "updated": updated,
                    "lastResume": state.get("last_resume"),
                    "status": status,
                    "attached": attached,
                    "usage": {
                        "totalLogUnits": usage_total,
                        "monthLogUnits": usage_month,
                        "weekLogUnits": usage_week,
                        "monthEstAic": usage_month * self.factor if self.calibrated else None,
                        "premiumRequests": int(state.get("premium_max", 0)),
                        "byDay": [
                            {"month": month, "date": date, "logUnits": units}
                            for (month, date), units in sorted(usage_by_day.items())
                        ],
                    },
                    "context": {
                        "currentTokens": state["context"].get("currentTokens"),
                        "conversationTokens": state["context"].get(
                            "conversationTokens"
                        ),
                        "observedAt": state["context"].get("observedAt"),
                    },
                    "days": days,
                    "delegations": delegations,
                    "_currentMain": state.get("current_main"),
                    "_mainWorking": main_in_progress,
                    "_agentWorking": {
                        key: bool(value)
                        for key, value in state.get("agent_open_turns", {}).items()
                    },
                }
            )

        rendered.sort(
            key=lambda value: _parse_datetime(value.get("updated"))
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        by_session = {session["id"]: session for session in rendered}

        def in_week(record: dict[str, Any]) -> bool:
            parsed = _parse_datetime(record.get("started"))
            if parsed is None:
                return False
            local = parsed.astimezone(self.timezone)
            return parsed <= current and week_start <= local < week_end

        def in_month(record: dict[str, Any]) -> bool:
            parsed = _parse_datetime(record.get("started"))
            if parsed is None:
                return False
            utc = parsed.astimezone(timezone.utc)
            return parsed <= current and (utc.year, utc.month) == utc_month

        stats = {
            "open": sum(1 for task in task_records if task["open"]),
            "week": sum(1 for task in task_records if in_week(task)),
            "month": sum(1 for task in task_records if in_month(task)),
            "total": len(task_records),
            "weekByIssuer": {"you": 0},
        }
        for task in task_records:
            if in_week(task):
                issuer = task.get("issuer") or "unknown"
                stats["weekByIssuer"][issuer] = (
                    stats["weekByIssuer"].get(issuer, 0) + 1
                )

        agents = []
        for identity in agent_order:
            name = identity["name"]
            direct = [
                task
                for task in task_records
                if task["kind"] == "direct" and task["agent"] == name
            ]
            delegated = [
                task
                for task in task_records
                if task["kind"] == "delegation" and task["agent"] == name
            ]
            current_tasks = [
                task for task in direct + delegated if task["open"]
            ]
            week_tasks = [task for task in direct + delegated if in_week(task)]
            working = False
            has_open = bool(current_tasks)
            for task in current_tasks:
                session = by_session.get(task["session"])
                if not session:
                    continue
                if task["kind"] == "direct":
                    if (
                        session["_currentMain"] == name
                        and session["_mainWorking"]
                    ):
                        working = True
                elif session["_agentWorking"].get(task["delegation"]):
                    working = True
            agent_month = month_usage_by_agent.get(name, 0.0)
            agents.append(
                {
                    "name": name,
                    "title": identity["title"],
                    "scope": identity["scope"],
                    "status": "working" if working else "open" if has_open else "idle",
                    "direct": self._counts(direct, in_week, in_month),
                    "delegated": self._counts(delegated, in_week, in_month),
                    "tokens": {
                        "week": sum(
                            task.get("tokens", 0)
                            for task in delegated
                            if in_week(task)
                        ),
                        "total": sum(
                            task.get("tokens", 0) for task in delegated
                        ),
                    },
                    "usage": {
                        "monthLogUnits": agent_month,
                        "monthEstAic": agent_month * self.factor if self.calibrated else None,
                        "monthShare": agent_month / global_month_usage
                        if global_month_usage
                        else 0.0,
                    },
                    "current": self._task_refs(current_tasks),
                    "week": self._task_refs(week_tasks),
                }
            )

        for session in rendered:
            session.pop("_currentMain", None)
            session.pop("_mainWorking", None)
            session.pop("_agentWorking", None)

        return {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAt": _iso(current),
            "timezone": self._config["timezone"],
            "periods": {
                "weekStart": _iso(week_start),
                "monthStart": _iso(month_start),
                "creditsMonthStart": _iso(credits_month_start),
            },
            "coverage": {
                "sessionsScanned": len(sessions),
                "officeSessions": len(rendered),
                "otherSessions": other_sessions,
                "bytesIndexed": bytes_indexed,
                "since": _iso(coverage_since),
                "malformedLines": malformed,
                "building": building,
            },
            "credits": {
                "unit": "AIC" if self.calibrated else "log units",
                "calibrated": self.calibrated,
                "factor": self.factor if self.calibrated else None,
                "calibration": self._config.get("calibration"),
                "planAic": self._config.get("plan_aic"),
                "monthUtc": f"{current.year:04d}-{current.month:02d}",
                "monthLogUnits": global_month_usage,
                "monthEstAic": global_month_usage * self.factor if self.calibrated else None,
            },
            "stats": stats,
            "agents": agents,
            "sessions": rendered,
        }

    @staticmethod
    def _counts(
        tasks: list[dict[str, Any]],
        in_week: Callable[[dict[str, Any]], bool],
        in_month: Callable[[dict[str, Any]], bool],
    ) -> dict[str, int]:
        return {
            "open": sum(1 for task in tasks if task["open"]),
            "week": sum(1 for task in tasks if in_week(task)),
            "month": sum(1 for task in tasks if in_month(task)),
            "total": len(tasks),
        }

    @staticmethod
    def _task_refs(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ordered = sorted(
            tasks,
            key=lambda task: _parse_datetime(task.get("started"))
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return [
            {
                "session": task["session"],
                "delegation": task["delegation"],
            }
            for task in ordered
        ]

    @staticmethod
    def _main_at(state: dict[str, Any], timestamp: Any) -> str | None:
        target = _parse_datetime(timestamp)
        selected = None
        for selection_time, name in state.get("main_selections", []):
            parsed = _parse_datetime(selection_time)
            if target is None or parsed is None or parsed <= target:
                selected = name
            elif target is not None and parsed > target:
                break
        return selected

    def _local_day(self, timestamp: Any) -> str | None:
        parsed = _parse_datetime(timestamp)
        return parsed.astimezone(self.timezone).date().isoformat() if parsed else None

    @staticmethod
    def _attached(directory: Path) -> bool:
        try:
            names = os.listdir(directory)
        except OSError:
            return False
        for name in names:
            match = LOCK_NAME.match(name)
            if match and _alive(int(match.group(1))):
                return True
        return False

    @staticmethod
    def _render_delegation(
        raw: dict[str, Any],
        state: dict[str, Any],
        staff: dict[str, str],
        attached: bool,
    ) -> dict[str, Any]:
        request = _parse_datetime(raw.get("lastRequest"))
        valid_completions = [
            timestamp
            for timestamp in raw.get("completionTimes", [])
            if _parse_datetime(timestamp)
            and (
                request is None
                or _parse_datetime(timestamp) > request
            )
        ]
        completed = valid_completions[-1] if valid_completions else None
        # An agent started before the last start/resume/shutdown no longer exists;
        # follow-ups sent to it afterwards fail, so they cannot make it running again.
        resume = _parse_datetime(state.get("last_resume"))
        started = _parse_datetime(raw.get("started"))
        alive = resume is None or (started is not None and started >= resume)
        status = (
            "completed"
            if completed
            else "running"
            if attached and alive
            else "no-completion"
        )
        reported = state.get("reported", {}).get(raw["id"])
        return {
            "id": raw["id"],
            "agent": raw.get("agent") or "unknown",
            "title": staff.get(raw.get("agent")),
            "role": bool(raw.get("role")),
            "builtin": bool(raw.get("builtin")),
            "parent": raw.get("parent"),
            "issuer": raw.get("issuer") or "unknown",
            "task": {
                "name": raw.get("task", {}).get("name"),
                "description": raw.get("task", {}).get("description"),
                "prompt": raw.get("task", {}).get("prompt"),
            },
            "model": raw.get("model"),
            "effort": raw.get("effort"),
            "mode": raw.get("mode"),
            "started": raw.get("started"),
            "lastActivity": raw.get("lastActivity"),
            "completed": completed,
            "status": status,
            "turns": int(raw.get("turns", 1)),
            "followUps": int(raw.get("followUps", 0)),
            "durationMs": raw.get("durationMs")
            if raw.get("hasDuration")
            else None,
            "tokens": raw.get("tokens") if raw.get("hasTokens") else None,
            "toolCalls": raw.get("toolCalls")
            if raw.get("hasToolCalls")
            else None,
            "reportedLogUnits": (
                float(reported["nanoAiu"]) / 1_000_000_000
                if reported
                else None
            ),
        }


def _summary(snapshot: dict[str, Any], first_seconds: float, warm_seconds: float) -> str:
    coverage = snapshot["coverage"]
    stats = snapshot["stats"]
    periods = snapshot["periods"]
    lines = [
        f"refresh: first {first_seconds:.3f}s, warm {warm_seconds:.3f}s",
        (
            "periods: "
            f"week {periods['weekStart']}, "
            f"month {periods['monthStart']}, "
            f"credits {periods['creditsMonthStart']}"
        ),
        (
            "coverage: "
            f"{coverage['sessionsScanned']} scanned, "
            f"{coverage['officeSessions']} office, "
            f"{coverage['otherSessions']} other, "
            f"{coverage['bytesIndexed']} bytes, "
            f"{coverage['malformedLines']} malformed, "
            f"since {coverage['since']}"
        ),
        (
            "tasks: "
            f"{stats['open']} open, {stats['week']} week, "
            f"{stats['month']} month, {stats['total']} total"
        ),
        "credits: "
        + f"{snapshot['credits']['monthLogUnits']:.1f} log units"
        + (
            f", {snapshot['credits']['monthEstAic']:.1f} est. AIC"
            if snapshot["credits"]["calibrated"]
            else ", uncalibrated"
        )
        + f" ({snapshot['credits']['monthUtc']} UTC)",
        "agents:",
    ]
    for agent in snapshot["agents"]:
        lines.append(
            "  "
            f"{agent['name']:<8} {agent['status']:<7} "
            f"direct {agent['direct']['open']}/{agent['direct']['week']}/{agent['direct']['total']} "
            f"delegated {agent['delegated']['open']}/{agent['delegated']['week']}/{agent['delegated']['total']} "
            f"tokens {agent['tokens']['total']}"
        )
    lines.append("top sessions:")
    top = sorted(
        snapshot["sessions"],
        key=lambda session: (
            len(session["delegations"]),
            session["usage"]["totalLogUnits"],
        ),
        reverse=True,
    )[:10]
    for session in top:
        counts: dict[str, int] = {}
        for delegation in session["delegations"]:
            counts[delegation["agent"]] = counts.get(delegation["agent"], 0) + 1
        breakdown = ", ".join(
            f"{name} {count}"
            for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        )
        lines.append(
            f"  {session['id'][:8]} {len(session['delegations'])} delegations "
            f"{session['usage']['totalLogUnits']:.1f} units"
            + (f" ({breakdown})" if breakdown else "")
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", action="store_true", help="print a compact validation summary")
    parser.add_argument("--office", type=Path, default=os.environ.get("OFFICE_HOME"),
                        help="Office home (or set OFFICE_HOME)")
    parser.add_argument("--sessions", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--calibrate", type=float, metavar="USED_AIC",
                        help="Calibrate against the current UTC month's /usage value")
    parser.add_argument("--plan", type=float, metavar="PLAN_AIC",
                        help="Store the monthly plan size while calibrating")
    arguments = parser.parse_args(argv)
    if arguments.office is None:
        parser.error("--office is required unless OFFICE_HOME is set")
    if arguments.plan is not None and arguments.calibrate is None:
        parser.error("--plan requires --calibrate")
    office = arguments.office.expanduser().resolve()
    index = SessionIndex(
        root=arguments.sessions,
        office=office,
    )
    started = time.monotonic()
    index.refresh()
    first_seconds = time.monotonic() - started
    started = time.monotonic()
    index.refresh()
    warm_seconds = time.monotonic() - started
    snapshot = index.snapshot()
    if arguments.calibrate is not None:
        if not math.isfinite(arguments.calibrate) or arguments.calibrate <= 0:
            parser.error("--calibrate USED_AIC must be positive")
        log_units = snapshot["credits"]["monthLogUnits"]
        if log_units <= 0:
            parser.error("cannot calibrate: current UTC month has zero log units")
        config_path = office / ".agent-office/dashboard.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        factor = arguments.calibrate / log_units
        measured = _default_now()
        measured_at = _iso(measured)
        month_start = _iso(measured.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
        config["aic_per_log_unit"] = factor
        config["calibration"] = {
            "used_aic": arguments.calibrate,
            "log_units": log_units,
            "measured_at": measured_at,
            "since": month_start,
        }
        if arguments.plan is not None:
            if not math.isfinite(arguments.plan) or arguments.plan <= 0:
                parser.error("--plan PLAN_AIC must be positive")
            config["plan_aic"] = arguments.plan
        temporary = config_path.with_name(f".{config_path.name}.tmp.{os.getpid()}")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(config, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, config_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        print(json.dumps({"used_aic": arguments.calibrate, "log_units": log_units,
                          "aic_per_log_unit": factor, "plan_aic": config.get("plan_aic"),
                          "calibration": config["calibration"]}, ensure_ascii=False))
        return 0
    if arguments.summary:
        print(_summary(snapshot, first_seconds, warm_seconds))
    else:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

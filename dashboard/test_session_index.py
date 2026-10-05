"""Fixture-only tests for the incremental Copilot session index."""

import json
import io
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout

import session_index


def event(kind, timestamp, data=None, agent_id=None):
    value = {
        "type": kind,
        "data": data or {},
        "id": f"id-{kind}-{timestamp}",
        "timestamp": timestamp,
        "parentId": None,
    }
    if agent_id is not None:
        value["agentId"] = agent_id
    return value


class SessionFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.office = self.base / "office"
        (self.office / ".agent-office").mkdir(parents=True)
        self.root = self.base / "sessions"
        self.root.mkdir()
        self.cache = self.office / ".agent-office/.dashboard-cache"
        self.staff = self.office / ".agent-office/staff.json"
        self.config = self.office / ".agent-office/dashboard.json"
        employees = [
            ("bidoof", "Lead", "office-lead"),
            ("dratini", "Builder / Experimenter", "office-builder"),
            ("ivysaur", "Reviewer", "office-reviewer"),
        ]
        self.staff.write_text(
            json.dumps(
                {
                    "employees": [
                        {"name": name, "title": title, "role_profile": role}
                        for name, title, role in employees
                    ]
                }
            )
        )
        self.config.write_text(json.dumps({
            "schema_version": 1,
            "timezone": "America/Vancouver",
            "plan_aic": None,
            "aic_per_log_unit": None,
            "calibration": None,
            "frontdesk": {"name": "rotom", "title": "Front Desk"},
        }))
        self.current = datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc)

    def make_session(self, session_id="s1", name="Fixture", created=None):
        directory = self.root / session_id
        directory.mkdir()
        created = created or "2026-09-30T16:00:00Z"
        (directory / "workspace.yaml").write_text(
            "\n".join(
                [
                    f"id: {session_id}",
                    f"name: {name}",
                    f"created_at: {created}",
                    f"updated_at: {created}",
                ]
            )
            + "\n"
        )
        (directory / "events.jsonl").write_bytes(b"")
        return directory

    def append(self, directory, *events, final_newline=True):
        payload = b"\n".join(
            json.dumps(value, separators=(",", ":")).encode() for value in events
        )
        if final_newline and payload:
            payload += b"\n"
        with (directory / "events.jsonl").open("ab") as stream:
            stream.write(payload)

    def index(self):
        return session_index.SessionIndex(
            root=self.root,
            staff_path=self.staff,
            cache_dir=self.cache,
            config_path=self.config,
            now=lambda: self.current,
        )

    @staticmethod
    def selection(name, timestamp="2026-09-30T16:00:01Z"):
        return event("subagent.selected", timestamp, {"agentName": name})

    @staticmethod
    def user(timestamp="2026-09-30T16:00:02Z", content="Do the thing"):
        return event("user.message", timestamp, {"content": content, "delivery": "idle"})

    @staticmethod
    def task_start(
        call_id,
        timestamp,
        agent_type="dratini",
        agent_id=None,
        name="Build",
    ):
        return event(
            "tool.execution_start",
            timestamp,
            {
                "toolName": "task",
                "toolCallId": call_id,
                "arguments": {
                    "name": name,
                    "description": f"{name} description",
                    "agent_type": agent_type,
                    "mode": "background",
                    "prompt": "p" * 5000,
                },
            },
            agent_id,
        )

    @staticmethod
    def started(
        call_id,
        agent_id,
        timestamp,
        name="dratini",
        agent_type="dratini",
    ):
        return event(
            "subagent.started",
            timestamp,
            {
                "toolCallId": call_id,
                "agentName": name,
                "agentType": agent_type,
                "executionMode": "background",
                "model": "model-a",
            },
            agent_id,
        )

    @staticmethod
    def completed(agent_id, timestamp, tokens=10, duration=20, calls=2):
        return event(
            "subagent.completed",
            timestamp,
            {
                "agentName": "dratini",
                "model": "model-a",
                "durationMs": duration,
                "totalTokens": tokens,
                "totalToolCalls": calls,
            },
            agent_id,
        )


class IncrementalTests(SessionFixture):
    def test_incremental_append_keeps_partial_line_until_newline(self):
        directory = self.make_session()
        self.append(directory, self.selection("bidoof"))
        partial = json.dumps(
            self.user(content="partial title"), separators=(",", ":")
        ).encode()
        with (directory / "events.jsonl").open("ab") as stream:
            stream.write(partial)
        index = self.index()
        index.refresh()
        first = index.snapshot()
        self.assertEqual(first["sessions"][0]["days"], [])
        self.assertLess(
            first["coverage"]["bytesIndexed"],
            (directory / "events.jsonl").stat().st_size,
        )

        with (directory / "events.jsonl").open("ab") as stream:
            stream.write(b"\n")
        index.refresh()
        second = index.snapshot()
        self.assertEqual(second["sessions"][0]["days"][0]["messages"], 1)
        self.assertEqual(
            second["coverage"]["bytesIndexed"],
            (directory / "events.jsonl").stat().st_size,
        )

    def test_truncation_and_inode_change_rebuild_instead_of_accumulating(self):
        directory = self.make_session()
        self.append(directory, self.selection("bidoof"), self.user())
        index = self.index()
        index.refresh()
        self.assertEqual(index.snapshot()["coverage"]["officeSessions"], 1)

        # Same inode, shorter content: rebuilding removes the old office selection.
        (directory / "events.jsonl").write_bytes(
            json.dumps(self.user(), separators=(",", ":")).encode() + b"\n"
        )
        index.refresh()
        self.assertEqual(index.snapshot()["coverage"]["officeSessions"], 0)

        # New inode, larger content: rebuilding discovers only the replacement.
        replacement = directory / "replacement"
        replacement.write_bytes(
            (
                json.dumps(self.selection("dratini"), separators=(",", ":"))
                + "\n"
                + json.dumps(self.user(content="replacement"), separators=(",", ":"))
                + "\n"
            ).encode()
        )
        os.replace(replacement, directory / "events.jsonl")
        index.refresh()
        snapshot = index.snapshot()
        self.assertEqual(snapshot["coverage"]["officeSessions"], 1)
        self.assertEqual(snapshot["sessions"][0]["agents"], ["dratini"])

    def test_malformed_invalid_utf8_and_oversized_lines(self):
        directory = self.make_session()
        invalid = (
            b'{"type":"user.message","data":{"content":"bad\xff"},'
            b'"timestamp":"2026-09-30T16:00:02Z"}\n'
        )
        oversized = json.dumps(
            self.user("2026-09-30T16:00:03Z", "x" * 1000),
            separators=(",", ":"),
        ).encode() + b"\n"
        with (directory / "events.jsonl").open("wb") as stream:
            stream.write(
                json.dumps(self.selection("bidoof"), separators=(",", ":")).encode()
                + b"\n"
            )
            stream.write(b"{not-json}\n")
            stream.write(invalid)
            stream.write(oversized)
        index = self.index()
        with mock.patch.object(session_index, "MAX_EVENT_LINE_BYTES", 300):
            index.refresh()
        snapshot = index.snapshot()
        self.assertEqual(snapshot["coverage"]["malformedLines"], 2)
        self.assertEqual(snapshot["sessions"][0]["days"][0]["messages"], 1)

    def test_cache_persistence_skips_unchanged_file_and_corruption_recovers(self):
        directory = self.make_session()
        self.append(directory, self.selection("bidoof"), self.user())
        first = self.index()
        first.refresh()
        expected = first.snapshot()["coverage"]["bytesIndexed"]

        loaded = self.index()
        with mock.patch.object(
            loaded, "_parse_file", wraps=loaded._parse_file
        ) as parse, mock.patch.object(
            loaded, "_save_cache", wraps=loaded._save_cache
        ) as save:
            loaded.refresh()
            parse.assert_not_called()
            save.assert_not_called()
        self.assertEqual(loaded.snapshot()["coverage"]["bytesIndexed"], expected)

        (self.cache / session_index.CACHE_NAME).write_text("{broken")
        recovered = self.index()
        recovered.refresh()
        self.assertEqual(recovered.snapshot()["coverage"]["officeSessions"], 1)
        json.loads((self.cache / session_index.CACHE_NAME).read_text())

    def test_cache_write_failure_still_publishes_new_state(self):
        directory = self.make_session()
        self.append(directory, self.selection("bidoof"), self.user())
        index = self.index()
        with mock.patch.object(
            index, "_save_cache", side_effect=OSError("read-only cache")
        ):
            index.refresh()
        snapshot = index.snapshot()
        self.assertEqual(snapshot["coverage"]["officeSessions"], 1)
        self.assertEqual(snapshot["sessions"][0]["days"][0]["messages"], 1)

    def test_event_exception_is_malformed_and_file_read_error_keeps_old_state(self):
        directory = self.make_session()
        self.append(
            directory,
            self.selection("bidoof"),
            event(
                "subagent.started",
                "2026-09-30T16:00:02Z",
                {"agentName": ["invalid"], "agentType": "dratini"},
                "bad-agent",
            ),
            self.user("2026-09-30T16:00:03Z"),
        )
        index = self.index()
        index.refresh()
        first = index.snapshot()
        self.assertEqual(first["coverage"]["malformedLines"], 1)
        self.assertEqual(first["sessions"][0]["days"][0]["messages"], 1)
        old_bytes = first["coverage"]["bytesIndexed"]

        self.append(directory, self.user("2026-09-30T16:00:04Z"))
        with mock.patch.object(
            index, "_parse_file", side_effect=OSError("vanished")
        ):
            index.refresh()
        retained = index.snapshot()
        self.assertEqual(retained["coverage"]["bytesIndexed"], old_bytes)
        self.assertEqual(retained["sessions"][0]["days"][0]["messages"], 1)


class DelegationTests(SessionFixture):
    def test_abort_clears_cancelled_main_and_subagent_turns(self):
        directory = self.make_session()
        self.append(
            directory,
            self.selection("bidoof"),
            self.user(),
            self.task_start("call", "2026-09-30T16:01:00Z"),
            self.started("call", "worker", "2026-09-30T16:01:01Z"),
            event(
                "assistant.turn_start",
                "2026-09-30T16:01:02Z",
                {"turnId": "0"},
            ),
            event(
                "assistant.turn_start",
                "2026-09-30T16:01:03Z",
                {"turnId": "0"},
                "worker",
            ),
            event("abort", "2026-09-30T16:01:04Z", {"reason": "user_abort"}),
        )
        (directory / f"inuse.{os.getpid()}.lock").touch()
        index = self.index()
        index.refresh()
        snapshot = index.snapshot()
        self.assertEqual(snapshot["sessions"][0]["status"], "open")
        statuses = {agent["name"]: agent["status"] for agent in snapshot["agents"]}
        self.assertEqual(statuses["bidoof"], "open")
        self.assertEqual(statuses["dratini"], "open")

    def test_turn_fallback_matches_and_repeated_start_does_not_double_count(self):
        directory = self.make_session()
        self.append(
            directory,
            self.selection("bidoof"),
            self.user(),
            event("assistant.turn_start", "2026-09-30T16:00:03Z"),
            event("assistant.turn_end", "2026-09-30T16:00:04Z"),
            self.task_start("call", "2026-09-30T16:01:00Z"),
            self.started("call", "worker", "2026-09-30T16:01:01Z"),
            event(
                "tool.execution_start",
                "2026-09-30T16:02:00Z",
                {
                    "toolName": "write_agent",
                    "arguments": {"agent_id": "worker"},
                },
            ),
            self.started("call", "worker", "2026-09-30T16:02:01Z"),
        )
        (directory / f"inuse.{os.getpid()}.lock").touch()
        index = self.index()
        index.refresh()
        session = index.snapshot()["sessions"][0]
        self.assertEqual(session["status"], "open")
        self.assertEqual(session["delegations"][0]["turns"], 2)
        self.assertEqual(
            index._sessions["s1"]["state"]["task_calls"],
            {},
        )

    def test_resume_boundary_controls_attached_delegations_and_open_counts(self):
        directory = self.make_session()
        self.append(
            directory,
            event("session.start", "2026-09-30T15:00:00Z"),
            self.selection("bidoof"),
            self.user(),
            self.task_start("stale", "2026-09-30T16:01:00Z", name="Stale"),
            self.started("stale", "stale-agent", "2026-09-30T16:01:01Z"),
            self.task_start("followed", "2026-09-30T16:02:00Z", name="Followed"),
            self.started("followed", "followed-agent", "2026-09-30T16:02:01Z"),
            event("session.resume", "2026-09-30T17:00:00Z"),
            self.task_start("fresh", "2026-09-30T17:01:00Z", name="Fresh"),
            self.started("fresh", "fresh-agent", "2026-09-30T17:01:01Z"),
            event(
                "tool.execution_start",
                "2026-09-30T17:02:00Z",
                {
                    "toolName": "write_agent",
                    "arguments": {"agent_id": "followed-agent"},
                },
            ),
        )
        (directory / f"inuse.{os.getpid()}.lock").touch()
        index = self.index()
        index.refresh()
        snapshot = index.snapshot()
        session = snapshot["sessions"][0]
        statuses = {
            delegation["id"]: delegation["status"]
            for delegation in session["delegations"]
        }
        self.assertEqual(session["lastResume"], "2026-09-30T17:00:00.000Z")
        # A follow-up to an agent started before the resume targets a dead agent.
        self.assertEqual(
            statuses,
            {
                "stale-agent": "no-completion",
                "followed-agent": "no-completion",
                "fresh-agent": "running",
            },
        )
        self.assertEqual(snapshot["stats"]["open"], 2)
        dratini = next(
            agent for agent in snapshot["agents"] if agent["name"] == "dratini"
        )
        self.assertEqual(dratini["delegated"]["open"], 1)
        self.assertEqual(
            {task["delegation"] for task in dratini["current"]},
            {"fresh-agent"},
        )
        self.assertEqual(dratini["status"], "open")

    def test_shutdown_ends_background_agents_even_with_later_follow_ups(self):
        directory = self.make_session()
        (directory / f"inuse.{os.getpid()}.lock").touch()
        self.append(
            directory,
            self.selection("bidoof"),
            self.user(),
            self.task_start("old", "2026-09-30T16:01:00Z", name="Old"),
            self.started("old", "old-agent", "2026-09-30T16:01:01Z"),
            event("session.shutdown", "2026-09-30T17:00:00Z", {"totalNanoAiu": 1}),
            event("tool.execution_start", "2026-09-30T17:30:00Z",
                  {"toolName": "write_agent", "arguments": {"agent_id": "old-agent"}}),
            self.task_start("new", "2026-09-30T17:31:00Z", name="New"),
            self.started("new", "new-agent", "2026-09-30T17:31:01Z"),
        )
        index = self.index()
        index.refresh()
        statuses = {d["id"]: d["status"] for d in index.snapshot()["sessions"][0]["delegations"]}
        self.assertEqual(statuses, {"old-agent": "no-completion", "new-agent": "running"})

    def test_multi_turn_follow_up_changes_status_and_sums_completions(self):
        directory = self.make_session()
        agent_id = "agent-a"
        self.append(
            directory,
            self.selection("bidoof"),
            self.user(),
            self.task_start("call-a", "2026-09-30T16:01:00Z"),
            self.started("call-a", agent_id, "2026-09-30T16:01:01Z"),
            event(
                "subagent.configured",
                "2026-09-30T16:01:02Z",
                {"model": "model-b", "reasoningEffort": "high"},
                agent_id,
            ),
            self.completed(agent_id, "2026-09-30T16:02:00Z", tokens=10),
        )
        index = self.index()
        index.refresh()
        delegation = index.snapshot()["sessions"][0]["delegations"][0]
        self.assertEqual(delegation["status"], "completed")
        self.assertEqual(delegation["effort"], "high")
        self.assertEqual(len(delegation["task"]["prompt"]), 4000)

        self.append(
            directory,
            event(
                "tool.execution_start",
                "2026-09-30T16:03:00Z",
                {
                    "toolName": "write_agent",
                    "arguments": {"agent_ids": [agent_id]},
                },
            ),
        )
        index.refresh()
        delegation = index.snapshot()["sessions"][0]["delegations"][0]
        self.assertEqual(delegation["status"], "no-completion")
        self.assertEqual((delegation["turns"], delegation["followUps"]), (2, 1))

        (directory / f"inuse.{os.getpid()}.lock").touch()
        self.assertEqual(
            index.snapshot()["sessions"][0]["delegations"][0]["status"], "running"
        )
        self.append(
            directory,
            self.completed(
                agent_id, "2026-09-30T16:04:00Z", tokens=15, duration=30, calls=3
            ),
        )
        index.refresh()
        delegation = index.snapshot()["sessions"][0]["delegations"][0]
        self.assertEqual(delegation["status"], "completed")
        self.assertEqual(
            (delegation["tokens"], delegation["durationMs"], delegation["toolCalls"]),
            (25, 50, 5),
        )

    def test_background_follow_up_completes_on_agent_idle_notification(self):
        directory = self.make_session()
        agent_id = "agent-bg"
        (directory / f"inuse.{os.getpid()}.lock").touch()
        self.append(
            directory,
            self.selection("bidoof"),
            self.user(),
            self.task_start("call-bg", "2026-09-30T16:01:00Z"),
            self.started("call-bg", agent_id, "2026-09-30T16:01:01Z"),
            self.completed(agent_id, "2026-09-30T16:02:00Z"),
            event("tool.execution_start", "2026-09-30T16:03:00Z",
                  {"toolName": "write_agent", "arguments": {"agent_id": agent_id}}),
        )
        index = self.index()
        index.refresh()
        self.assertEqual(index.snapshot()["sessions"][0]["delegations"][0]["status"], "running")
        self.append(
            directory,
            event("system.notification", "2026-09-30T16:05:00Z",
                  {"content": "idle", "kind": {"type": "agent_idle", "agentId": agent_id}}),
            event("system.notification", "2026-09-30T16:06:00Z",
                  {"content": "x", "kind": {"type": "shell_completed"}}),
        )
        index.refresh()
        snapshot = index.snapshot()
        delegation = snapshot["sessions"][0]["delegations"][0]
        self.assertEqual(delegation["status"], "completed")
        self.assertEqual(delegation["completed"], "2026-09-30T16:05:00.000Z")
        self.assertEqual(snapshot["stats"]["open"], 1, "only the attached session itself stays open")

    def test_nested_parent_and_unresolved_spawner(self):
        directory = self.make_session()
        self.append(
            directory,
            self.selection("bidoof"),
            self.user(),
            self.task_start("parent-call", "2026-09-30T16:01:00Z"),
            self.started(
                "parent-call", "parent", "2026-09-30T16:01:01Z", "dratini"
            ),
            self.task_start(
                "child-call",
                "2026-09-30T16:02:00Z",
                agent_type="task",
                agent_id="parent",
                name="Nested",
            ),
            self.started(
                "child-call",
                "child",
                "2026-09-30T16:02:01Z",
                name="task",
                agent_type="task",
            ),
            self.task_start(
                "lost-call",
                "2026-09-30T16:03:00Z",
                agent_type="rubber-duck",
                agent_id="missing",
                name="Lost",
            ),
            self.started(
                "lost-call",
                "lost",
                "2026-09-30T16:03:01Z",
                name="rubber-duck",
                agent_type="rubber-duck",
            ),
        )
        index = self.index()
        index.refresh()
        delegations = {
            value["id"]: value
            for value in index.snapshot()["sessions"][0]["delegations"]
        }
        self.assertEqual(delegations["child"]["parent"], "parent")
        self.assertEqual(delegations["child"]["issuer"], "dratini")
        self.assertTrue(delegations["child"]["builtin"])
        self.assertEqual(delegations["lost"]["parent"], None)
        self.assertEqual(delegations["lost"]["issuer"], "unknown")

    def test_role_and_builtin_classification(self):
        directory = self.make_session()
        self.append(
            directory,
            self.selection("office-lead"),
            self.user(),
            self.task_start(
                "role", "2026-09-30T16:01:00Z", agent_type="office-builder"
            ),
            self.started(
                "role",
                "role-agent",
                "2026-09-30T16:01:01Z",
                name="office-builder",
                agent_type="office-builder",
            ),
            self.task_start(
                "builtin", "2026-09-30T16:02:00Z", agent_type="task"
            ),
            self.started(
                "builtin",
                "builtin-agent",
                "2026-09-30T16:02:01Z",
                name="task",
                agent_type="task",
            ),
        )
        index = self.index()
        index.refresh()
        values = {
            item["id"]: item
            for item in index.snapshot()["sessions"][0]["delegations"]
        }
        self.assertEqual(index.snapshot()["sessions"][0]["agents"], ["bidoof"])
        self.assertEqual(values["role-agent"]["agent"], "dratini")
        self.assertTrue(values["role-agent"]["role"])
        self.assertFalse(values["role-agent"]["builtin"])
        self.assertIsNotNone(values["role-agent"]["title"])
        self.assertTrue(values["builtin-agent"]["builtin"])
        self.assertIsNone(values["builtin-agent"]["title"])


class AccountingTests(SessionFixture):
    def test_uncalibrated_usage_and_calibrate_command(self):
        self.current = datetime.now(timezone.utc)
        stamp = self.current.isoformat().replace("+00:00", "Z")
        directory = self.make_session()
        self.append(
            directory,
            self.selection("bidoof", stamp),
            event("session.usage_checkpoint", stamp,
                  {"totalNanoAiu": 20_000_000_000}),
        )
        other = self.make_session("outside-office")
        self.append(
            other,
            event("session.usage_checkpoint", stamp,
                  {"totalNanoAiu": 30_000_000_000}),
        )
        index = self.index()
        index.refresh()
        credits = index.snapshot()["credits"]
        self.assertFalse(credits["calibrated"])
        self.assertIsNone(credits["monthEstAic"])
        self.assertEqual(credits["monthLogUnits"], 50)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(session_index.main([
                "--office", str(self.office), "--sessions", str(self.root),
                "--calibrate", "50", "--plan", "1000",
            ]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["aic_per_log_unit"], 1)
        saved = json.loads(self.config.read_text())
        self.assertEqual(saved["plan_aic"], 1000)
        self.assertEqual(saved["calibration"]["log_units"], 50)
        empty = self.base / "empty-sessions"
        empty.mkdir()
        error = io.StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit):
            session_index.main([
                "--office", str(self.office), "--sessions", str(empty),
                "--calibrate", "50",
            ])
        self.assertIn("zero log units", error.getvalue())

    def test_main_agent_switch_controls_usage_and_spawn_issuer(self):
        directory = self.make_session()
        self.append(
            directory,
            self.selection("bidoof", "2026-09-30T16:00:01Z"),
            self.user(),
            event(
                "session.usage_checkpoint",
                "2026-09-30T16:01:00Z",
                {"totalNanoAiu": 100_000_000_000, "totalPremiumRequests": 1},
            ),
            self.selection("office-builder", "2026-09-30T16:02:00Z"),
            event(
                "session.usage_checkpoint",
                "2026-09-30T16:03:00Z",
                {"totalNanoAiu": 150_000_000_000, "totalPremiumRequests": 2},
            ),
            self.task_start("call", "2026-09-30T16:04:00Z"),
            self.started("call", "worker", "2026-09-30T16:04:01Z"),
        )
        index = self.index()
        index.refresh()
        snapshot = index.snapshot()
        session = snapshot["sessions"][0]
        self.assertEqual(session["agents"], ["bidoof", "dratini"])
        self.assertEqual(session["delegations"][0]["issuer"], "dratini")
        usage = {agent["name"]: agent["usage"] for agent in snapshot["agents"]}
        self.assertEqual(usage["bidoof"]["monthLogUnits"], 100)
        self.assertEqual(usage["dratini"]["monthLogUnits"], 50)

    def test_running_max_utc_month_and_la_week_boundaries(self):
        self.current = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)
        directory = self.make_session(
            created="2026-09-30T23:00:00Z"
        )
        self.append(
            directory,
            self.selection("bidoof", "2026-09-30T23:00:01Z"),
            self.user("2026-09-30T23:00:02Z"),
            event(
                "session.usage_checkpoint",
                "2026-09-30T23:30:00Z",
                {"totalNanoAiu": 100_000_000_000},
            ),
            event(
                "session.usage_checkpoint",
                "2026-10-01T00:10:00Z",
                {"totalNanoAiu": 150_000_000_000},
            ),
            event(
                "session.usage_checkpoint",
                "2026-10-01T00:20:00Z",
                {"totalNanoAiu": 120_000_000_000},
            ),
            event(
                "session.usage_checkpoint",
                "2026-10-05T06:59:00Z",
                {"totalNanoAiu": 180_000_000_000},
            ),
            event(
                "session.usage_checkpoint",
                "2026-10-05T07:01:00Z",
                {"totalNanoAiu": 200_000_000_000},
            ),
        )
        index = self.index()
        index.refresh()
        snapshot = index.snapshot()
        usage = snapshot["sessions"][0]["usage"]
        self.assertEqual(usage["totalLogUnits"], 200)
        self.assertEqual(usage["monthLogUnits"], 100)
        self.assertEqual(usage["weekLogUnits"], 20)
        # Sep 30 Pacific splits at the reset: 100 before (September), 50 after (October).
        self.assertEqual(
            usage["byDay"],
            [
                {"month": "2026-09", "date": "2026-09-30", "logUnits": 100.0},
                {"month": "2026-10", "date": "2026-09-30", "logUnits": 50.0},
                {"month": "2026-10", "date": "2026-10-04", "logUnits": 30.0},
                {"month": "2026-10", "date": "2026-10-05", "logUnits": 20.0},
            ],
        )
        self.assertEqual(snapshot["credits"]["monthUtc"], "2026-10")
        self.assertEqual(
            snapshot["periods"],
            {
                "weekStart": "2026-10-05T07:00:00.000Z",
                "monthStart": "2026-10-01T00:00:00.000Z",
                "creditsMonthStart": "2026-10-01T00:00:00.000Z",
            },
        )

    def test_task_month_follows_utc_credit_reset(self):
        self.current = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)
        before = self.make_session("s1", created="2026-09-30T23:30:00Z")
        self.append(before, self.selection("bidoof", "2026-09-30T23:30:01Z"), self.user("2026-09-30T23:30:02Z"))
        after = self.make_session("s2", created="2026-10-01T03:00:00Z")
        self.append(after, self.selection("bidoof", "2026-10-01T03:00:01Z"), self.user("2026-10-01T03:00:02Z"))
        index = self.index()
        index.refresh()
        snapshot = index.snapshot()
        # 2026-10-01T03:00Z is Sep 30 8 PM Pacific: after the 5 PM reset, so it is October.
        self.assertEqual(snapshot["stats"]["month"], 1)
        self.assertEqual(snapshot["stats"]["total"], 2)

    def test_lock_liveness_and_turn_progress_are_recomputed(self):
        directory = self.make_session()
        self.append(
            directory,
            self.selection("bidoof"),
            self.user(),
            event(
                "assistant.turn_start",
                "2026-09-30T16:01:00Z",
                {"turnId": "turn-1"},
            ),
        )
        alive = directory / f"inuse.{os.getpid()}.lock"
        alive.touch()
        index = self.index()
        index.refresh()
        snapshot = index.snapshot()
        self.assertEqual(snapshot["sessions"][0]["status"], "working")
        self.assertEqual(
            next(a for a in snapshot["agents"] if a["name"] == "bidoof")["status"],
            "working",
        )

        alive.unlink()
        (directory / "inuse.999999999.lock").touch()
        snapshot = index.snapshot()
        self.assertEqual(snapshot["sessions"][0]["status"], "closed")
        self.assertEqual(
            next(a for a in snapshot["agents"] if a["name"] == "bidoof")["status"],
            "idle",
        )

    def test_non_office_sessions_are_excluded_but_counted(self):
        office = self.make_session("office")
        other = self.make_session("other")
        self.append(office, self.selection("bidoof"), self.user())
        self.append(
            other,
            event(
                "session.usage_checkpoint",
                "2026-09-30T16:00:00Z",
                {"totalNanoAiu": 999_000_000_000},
            ),
            self.user(),
        )
        index = self.index()
        index.refresh()
        snapshot = index.snapshot()
        self.assertEqual(
            (
                snapshot["coverage"]["sessionsScanned"],
                snapshot["coverage"]["officeSessions"],
                snapshot["coverage"]["otherSessions"],
            ),
            (2, 1, 1),
        )
        self.assertEqual([value["id"] for value in snapshot["sessions"]], ["office"])
        self.assertEqual(snapshot["credits"]["monthLogUnits"], 999)

    def test_exact_output_shapes_and_reported_partial_usage(self):
        directory = self.make_session()
        self.append(
            directory,
            self.selection("bidoof"),
            self.user(),
            self.task_start("call", "2026-09-30T16:01:00Z"),
            self.started("call", "worker", "2026-09-30T16:01:01Z"),
            event(
                "session.shutdown",
                "2026-09-30T16:02:00Z",
                {
                    "totalPremiumRequests": 3,
                    "currentTokens": 12,
                    "conversationTokens": 10,
                    "agentMetrics": {
                        "worker": {
                            "agentName": "dratini",
                            "totalNanoAiu": 4_500_000_000,
                        }
                    },
                },
            ),
        )
        index = self.index()
        index.refresh()
        snapshot = index.snapshot()
        self.assertEqual(
            list(snapshot),
            [
                "schemaVersion",
                "generatedAt",
                "timezone",
                "periods",
                "coverage",
                "credits",
                "stats",
                "agents",
                "sessions",
            ],
        )
        delegation = snapshot["sessions"][0]["delegations"][0]
        self.assertEqual(
            set(delegation),
            {
                "id",
                "agent",
                "title",
                "role",
                "builtin",
                "parent",
                "issuer",
                "task",
                "model",
                "effort",
                "mode",
                "started",
                "lastActivity",
                "completed",
                "status",
                "turns",
                "followUps",
                "durationMs",
                "tokens",
                "toolCalls",
                "reportedLogUnits",
            },
        )
        self.assertEqual(delegation["reportedLogUnits"], 4.5)
        self.assertEqual(
            snapshot["sessions"][0]["context"]["currentTokens"], 12
        )


if __name__ == "__main__":
    unittest.main()

"""Focused backend tests on temporary fixtures; never touch real office records."""

import hashlib
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

import server

NAMES = ["abra", "bidoof", "dratini", "eevee", "furret", "ivysaur", "joltik", "lapras"]
ROLES = ["guardian", "lead", "builder", "navigator", "analyst", "reviewer", "curator", "researcher"]
TITLES = {
    "lead": "Manager", "guardian": "Counselor", "builder": "Builder",
    "reviewer": "Reviewer", "analyst": "Results Analyst", "navigator": "Code Reader",
    "researcher": "Researcher", "curator": "Docs Curator",
}
ROTOM = '---\nname: rotom\ndescription: rotom (Office Front Desk). Routes sessions.\nmodel: gpt-5.6-sol-fast\ntools: ["read", "grep", "execute"]\nuser-invocable: true\ndisable-model-invocation: true\n---\n\n# Front desk\n'
AGENT = "---\nname: {name}\ndescription: {name} (Fixture Project - Role). Long description\n  continues here.\nmodel: test-model\ntools:\n- read\n- edit\n---\n\nYou are **{name}**, employee ID `{role}`, project ID\n"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        manifest = {}
        for name, role in zip(NAMES, ROLES):
            content = AGENT.format(name=name, role=role)
            self.write(".github/agents/" + name + ".agent.md", content)
            manifest[name] = {"sha256": hashlib.sha256(content.encode()).hexdigest()}
        self.json(".agent-office/generated-profiles.json", {"project_id": "my-project", "outputs": manifest})
        self.json(".agent-office/project.json", {"schema_version": 1, "project_id": "my-project", "display_name": "My Project", "status": "active",
            "repository": {"status": "ready", "readiness_scope": "No workload validation."},
            "records": {"staff": "staff.json", "tasks": "tasks.json", "state": "PROJECT_STATE.md", "decisions": "DECISIONS.md"}})
        self.staff = {"schema_version": 1, "project_id": "my-project", "employees": [
            {"employee_id": r, "name": n, "title": TITLES[r], "role_profile": "office-" + r, "sessions": [], "assignments": []}
            for n, r in zip(NAMES, ROLES)]}
        self.staff["employees"][1]["sessions"] = [
            {"session_id": "11111111-1111-1111-1111-111111111111", "status": "closed_by_user", "scope": "old"},
            {"session_id": "22222222-2222-2222-2222-222222222222", "status": "current_lead", "task_id": "T-OPEN"}]
        self.staff["employees"][2]["sessions"] = [
            {"agent_id": "aaaaaaaa-0000-0000-0000-000000000000", "task_id": "T-OPEN", "scope": "Build", "status": "stopped_idle"}]
        self.json(".agent-office/staff.json", self.staff)
        self.tasks = {"schema_version": 1, "project_id": "my-project", "tasks": [
            {"task_id": "T-OPEN", "owner_employee_id": "lead", "owner_name": "bidoof", "status": "experiment_running",
             "current_summary": "Running arms", "open_gates": ["gate A"], "requested_at": "2026-09-25", "basis": "D001",
             "owner_session": "22222222-2222-2222-2222-222222222222",
             "evidence": ["reports/T/REPORT.md", "../../reports/outside.md", "reports/T", "reports/missing.md"],
             "sessions": [{"employee": "dratini", "agent_id": "aaaaaaaa-0000-0000-0000-000000000000", "scope": "Build", "status": "returned_ok"}]},
            {"task_id": "T-SYNC", "owner_employee_id": "lead", "owner_name": "bidoof", "status": "planned",
             "subtasks": [{"id": "S1", "title": "Explain", "status": "pending", "suggested_owner": "curator", "depends_on": []}]},
            {"task_id": "T-DONE", "owner_employee_id": "lead", "status": "completed_but_blocked"},
            {"task_id": "T-OLD", "owner_employee_id": "lead", "status": "partial", "superseded_by": "T-OPEN"}]}
        self.json(".agent-office/tasks.json", self.tasks)
        self.json(".agent-office/dashboard.json", {
            "schema_version": 1, "timezone": "UTC", "plan_aic": None,
            "aic_per_log_unit": None, "calibration": None,
            "frontdesk": {"name": "rotom", "title": "Front Desk"}})
        self.write(".agent-office/PROJECT_STATE.md", "# State\n\n## Current snapshot (2026-09-30) — START HERE\n\nGoal text.\n\n---\n\n## Historical\n\nOld text.\n")
        self.write(".agent-office/DECISIONS.md", "## D001: First decision\n")
        self.write(".agent-office/STARTING_BRIEF.md", "**Execution ground rule (user): the user launches live jobs.**\n")
        self.write("reports/T/REPORT.md", "report")
        self.json("run-logs/registry.json", {
            "run-a": {"name": "live-run", "host": "h1", "started": "2026-09-30 12:00 PDT"},
            "run-b": {"name": "old-run", "host": "h2", "started": "2026-09-29 12:00 PDT", "status": "40/40 done"}})
        self.write("run-logs/README.md", "| Name | Host | Run dir | Status |\n|---|---|---|---|\n| live-run | h1 | `runs/run-a` | running |\n| old-run | h2 | `runs/run-b` | 40/40 done |\n")
        self.write("run-logs/live-run.md", "# live-run\n\n## Issues / events\n- event one\n")
        self.write("run-logs/live-run.health.log", "15:00 live-run [x] up 1h (capture -) state=starting\n  updates=v1\n15:30 live-run [x] up 2h (capture -) state=starting\n  updates=v1 v2\n15:45 live-run [x] up 3h")
        self.write("run-logs/old-run.md", "# old-run\n")
        self.write("run-logs/old-run.health.log", "05:00 old-run [x] up 9h (capture -) state=failed\n")
        old = time.time() - 3 * 3600
        os.utime(self.root / "run-logs/old-run.health.log", (old, old))
        self.write("runs/run-a/secret.txt", "never read")
        self.agents = self.root / "user-agents"
        self.write("user-agents/rotom.agent.md", ROTOM)
        self.write("user-agents/office-lead.agent.md", "---\nname: office-lead\n---\n")
        self.write("user-agents/bidoof.agent.md", AGENT.format(name="bidoof", role="lead"))
        self.write("user-agents/Bad Name.agent.md", "---\nname: x\n---\n")
        (self.agents / "abra.agent.md").symlink_to(self.root / ".github/agents/abra.agent.md")

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def json(self, relative, value):
        self.write(relative, json.dumps(value))

    def collect(self):
        return server.collect(self.root, self.agents)


class CollectorTests(Fixture):
    def test_lanes_follow_records_and_titles_fall_back_to_task_id(self):
        s = self.collect()["snapshot"]
        lane = {a["task"]: a["lane"] for a in s["assignments"] if a["id"].startswith("task-")}
        self.assertEqual(lane, {"T-OPEN": "open", "T-SYNC": "open", "T-DONE": "closed", "T-OLD": "closed"})
        done = next(a for a in s["assignments"] if a["id"] == "task-T-DONE")
        self.assertIn("completed_but_blocked", done["status"])
        self.assertEqual(done["title"], "T-DONE")
        self.assertEqual(s["openTaskCount"], 2)

    def test_lead_sessions_and_worker_merge_keep_conflicts(self):
        s = self.collect()["snapshot"]
        self.assertEqual([x["current"] for x in s["leadSessions"]], [False, True])
        worker = next(a for a in s["assignments"] if a.get("worker") == "aaaaaaaa-0000-0000-0000-000000000000")
        self.assertIn("staff.json: stopped_idle", worker["status"])
        self.assertIn("tasks.json: returned_ok", worker["status"])
        self.assertIn("disagree", worker["status"])
        self.assertEqual(worker["workerKind"], "Worker agent ID")
        self.assertEqual(worker["stage"], "Recorded")
        owner = next(a for a in s["assignments"] if a["id"] == "task-T-OPEN")
        self.assertIn("current_lead", owner["steps"][1]["text"])

    def test_suggested_owner_is_not_an_assignment(self):
        s = self.collect()["snapshot"]
        joltik = next(e for e in s["employees"] if e["id"] == "curator")
        self.assertEqual(joltik["label"], "No recorded assignments")
        self.assertIn("suggested, not assigned", joltik["note"])
        idle = next(a for a in s["assignments"] if a["id"] == "idle-curator")
        self.assertEqual(idle["lane"], "idle")
        self.assertIn("not assigned", idle["next"])

    def test_evidence_references_are_classified_not_followed(self):
        s = self.collect()["snapshot"]
        owner = next(a for a in s["assignments"] if a["id"] == "task-T-OPEN")
        self.assertIn("reports/T/REPORT.md", owner["sources"])
        notes = " ".join(owner["unresolved"])
        self.assertIn("outside office root", notes)
        self.assertIn("directory — not served", notes)
        self.assertIn("not found at recorded location", notes)
        self.assertFalse(any(v["path"].startswith("runs/") for v in s["sources"].values()))

    def test_profiles_integrity_and_frontmatter(self):
        s = self.collect()["snapshot"]
        abra = next(e for e in s["employees"] if e["id"] == "guardian")["profile"]
        self.assertEqual(abra["integrity"], "matches")
        self.assertEqual(abra["tools"], ["read", "edit"])
        self.assertIn("continues here", abra["description"])
        self.write(".github/agents/abra.agent.md", AGENT.format(name="abra", role="guardian") + "edited\n")
        self.write(".github/agents/eevee.agent.md", AGENT.format(name="eevee", role="reviewer"))
        s = self.collect()["snapshot"]
        profiles = {e["id"]: e["profile"] for e in s["employees"]}
        self.assertEqual(profiles["guardian"]["integrity"], "differs")
        self.assertEqual(profiles["navigator"]["integrity"], "conflict")
        (self.root / ".github/agents/furret.agent.md").unlink()
        s = self.collect()
        self.assertEqual(next(e for e in s["snapshot"]["employees"] if e["id"] == "analyst")["profile"]["integrity"], "unknown")
        self.assertTrue(any("furret" in e["path"] for e in s["errors"]))

    def test_office_wide_agents_only_regular_non_staff_non_template(self):
        s = self.collect()["snapshot"]
        office = [e for e in s["employees"] if e["scope"] == "office"]
        self.assertEqual([e["name"] for e in office], ["rotom"])
        rotom = office[0]
        self.assertEqual(rotom["role"], "Front Desk")
        self.assertEqual(rotom["profile"]["tools"], ["read", "grep", "execute"])
        self.assertEqual(rotom["profile"]["invocation"], "you can invoke it; other agents cannot delegate to it")
        self.assertEqual((s["projectStaffCount"], s["officeAgentCount"]), (8, 1))
        self.assertFalse(any(a["employee"] == "rotom" for a in s["assignments"]))
        self.assertFalse(s["sources"]["office-agent:rotom"]["served"])
        project = next(e for e in s["employees"] if e["id"] == "lead")
        self.assertEqual(project["scope"], "project")

    def test_office_agent_problems_are_visible_not_fatal(self):
        self.write("user-agents/rotom.agent.md", ROTOM.replace("name: rotom", "name: other"))
        result = self.collect()
        rotom = next(e for e in result["snapshot"]["employees"] if e["name"] == "rotom")
        self.assertIn("does not match", rotom["profile"]["integrityDetail"])
        self.assertTrue(any("rotom" in e["path"] for e in result["errors"]))
        self.agents = self.root / "no-such-dir"
        snapshot = self.collect()["snapshot"]
        self.assertEqual(snapshot["officeAgentCount"], 1)
        self.assertIn("could not be read safely", snapshot["officeAgentsProblem"])

    def test_runs_use_last_complete_health_block_and_staleness(self):
        s = self.collect()["snapshot"]
        runs = {r["name"]: r for r in s["runs"]}
        live = runs["live-run"]
        self.assertTrue(live["recordedActive"])
        self.assertEqual(live["health"]["blockTime"], "15:30")
        self.assertIn("incomplete", live["health"]["warning"])
        self.assertIn("event one", live["issues"])
        old = runs["old-run"]
        self.assertFalse(old["recordedActive"])
        self.assertTrue(old["health"]["stale"])
        self.assertEqual(old["health"]["stateField"], "failed")
        self.assertEqual(s["activeRunCount"], 1)

    def test_current_snapshot_section_excludes_history(self):
        project = self.collect()["snapshot"]["project"]
        self.assertIn("Goal text", project["currentSnapshot"])
        self.assertNotIn("Old text", project["currentSnapshot"])
        self.assertIn("launches live jobs", project["groundRule"])

    def test_required_records_fail_and_optional_are_visible(self):
        (self.root / "run-logs/registry.json").unlink()
        result = self.collect()
        self.assertIn("unavailable", result["snapshot"]["runsProblem"])
        for content in ("{", "[]", '{"project_id":"other"}'):
            self.write(".agent-office/staff.json", content)
            with self.assertRaises(server.RecordError):
                self.collect()

    def test_unsafe_paths_rejected(self):
        (self.root / "link.md").symlink_to(self.root / "reports/T/REPORT.md")
        for bad in ("link.md", "../x", "/etc/passwd", "reports/../../x"):
            with self.assertRaises(server.RecordError):
                server.read_local(self.root, bad)
        self.assertEqual(server.resolve_ref("../../reports/x.md")[0], None)
        self.assertEqual(server.resolve_ref("../reports/x.md")[0], "reports/x.md")

    def test_embed_snapshot_rewrites_only_the_block(self):
        page = self.root / "page.html"
        page.write_text('<p>keep</p><script id="snapshot" type="application/json">\nnull\n  </script><script>code</script>')
        server.embed_snapshot(self.root, page)
        html = page.read_text()
        self.assertIn("<p>keep</p>", html)
        self.assertIn("<script>code</script>", html)
        self.assertIn('"embedded-snapshot"', html)


class HttpTests(Fixture):
    def test_routes_and_boundaries(self):
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.handler_for(self.root, agents_dir=self.agents))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        port = httpd.server_address[1]

        def request(url, method="GET", headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                connection.request(method, url, headers=headers or {})
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()

        status, _, body = request("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["schemaVersion"], 2)
        for ok in ("reports/T/REPORT.md", "run-logs/live-run.health.log", ".github/agents/abra.agent.md"):
            status, headers, _ = request("/evidence?source=" + ok)
            self.assertEqual(status, 200, ok)
            self.assertTrue(headers["Content-Type"].startswith("text/plain"))
        for bad in ("runs/run-a/secret.txt", "../../reports/outside.md", "reports/T", "server.py", "reports/missing.md"):
            self.assertEqual(request("/evidence?source=" + bad)[0], 404, bad)
        self.assertEqual(request("/evidence?source=~/.copilot/agents/rotom.agent.md")[0], 404)
        self.assertEqual(request("/evidence?source=user-agents/rotom.agent.md")[0], 404)
        self.assertEqual(request("/api/status", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(request("/api/status", headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(request("/api/status", method="POST")[0], 405)
        self.write(".agent-office/tasks.json", "{")
        status, _, body = request("/api/status")
        self.assertEqual(status, 503)
        self.assertNotIn(str(self.root).encode(), body)


class FakeIndex:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def refresh(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("broken log /secret/path")

    def snapshot(self):
        return {"schemaVersion": 1, "generatedAt": "now-%d" % self.calls, "stats": {"open": 1, "week": 2, "month": 3, "total": 4},
                "agents": [], "sessions": [], "coverage": {"building": False}}


class OfficeActivityTests(Fixture):
    def serve(self, activity):
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.handler_for(self.root, agents_dir=self.agents, activity=activity))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        port = httpd.server_address[1]

        def request(url):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                connection.request("GET", url)
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()
        return request

    def test_office_feed_serves_last_good_index_and_stable_revision(self):
        index = FakeIndex()
        activity = server.OfficeActivity(index)
        request = self.serve(activity)
        self.assertEqual(request("/api/office")[0], 503)
        activity.refresh_once()
        status, body = request("/api/office")
        first = json.loads(body)
        self.assertEqual((status, first["schemaVersion"], first["snapshot"]["stats"]["open"]), (200, 1, 1))
        activity.refresh_once()
        second = json.loads(request("/api/office")[1])
        self.assertEqual(first["revision"], second["revision"], "generatedAt alone must not change the revision")
        index.fail = True
        activity.refresh_once()
        status, body = request("/api/office")
        self.assertEqual(status, 200, "a failed refresh keeps serving the last good index")
        stale = json.loads(body)
        self.assertEqual(stale["revision"], first["revision"])
        self.assertTrue(stale["stale"])
        self.assertIn("RuntimeError", stale["error"])
        self.assertFalse(first["stale"])

    def test_office_feed_errors_without_index(self):
        activity = server.OfficeActivity(FakeIndex(fail=True))
        request = self.serve(activity)
        activity.refresh_once()
        status, body = request("/api/office")
        self.assertEqual(status, 503)
        self.assertIn(b"RuntimeError", body)
        self.assertEqual(self.serve(None)("/api/office")[0], 503)


if __name__ == "__main__":
    unittest.main()

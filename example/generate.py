#!/usr/bin/env python3
"""Generate the example office: a fictional project with made-up records, runs and
Copilot CLI session logs, dated relative to today so the dashboard looks current.

Usage (from the repository root):
    python3 example/generate.py
    python3 office.py dashboard --home example/demo-office --sessions example/session-state

Everything here is invented. Re-run it any time to move the dates up to today.
"""

import json
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parent
REPO = EXAMPLE.parent
HOME = EXAMPLE / "demo-office"
SESSIONS = EXAMPLE / "session-state"
NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)
NANO_PER_LOG_UNIT = 1_000_000_000


def iso(when):
    return when.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def at(days_ago, hour, minute=0):
    """A UTC time `days_ago` days before now at hour:minute, never in the future."""
    return min((NOW - timedelta(days=days_ago)).replace(hour=hour, minute=minute), NOW - timedelta(minutes=5))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


class Session:
    """One events.jsonl in the Copilot CLI session-log format the dashboard reads."""

    def __init__(self, slug, name, main_agent, start):
        self.id = str(uuid.uuid5(uuid.NAMESPACE_URL, "copilot-office-demo/" + slug))
        self.name = name
        self.events = []
        self.clock = start
        self.created = start
        self.usage = 0
        self.premium = 0
        self.add("session.start", {"sessionId": self.id, "selectedModel": "demo-model"})
        self.add("subagent.selected", {"agentName": main_agent})

    def tick(self, minutes):
        self.clock += timedelta(minutes=minutes)

    def add(self, kind, data, agent_id=None):
        event = {"type": kind, "data": data, "id": f"{self.id[:8]}-{len(self.events)}",
                 "timestamp": iso(self.clock), "parentId": None}
        if agent_id:
            event["agentId"] = agent_id
        self.events.append(event)

    def spend(self, log_units, requests=1):
        self.usage += int(log_units * NANO_PER_LOG_UNIT)
        self.premium += requests
        self.add("session.usage_checkpoint", {"totalNanoAiu": self.usage, "totalPremiumRequests": self.premium})

    def say(self, text, minutes=2, cost=40):
        self.tick(minutes)
        turn = str(len(self.events))
        self.add("user.message", {"content": text, "delivery": "idle"})
        self.add("assistant.turn_start", {"turnId": turn})
        self.tick(1)
        self.add("assistant.turn_end", {"turnId": turn})
        self.spend(cost)

    def delegate(self, agent, description, prompt, minutes, tokens, calls, mode="background", follow_up=None):
        call = f"call-{len(self.events)}"
        agent_id = str(uuid.uuid5(uuid.NAMESPACE_URL, self.id + call))
        self.tick(1)
        self.add("tool.execution_start", {"toolName": "task", "toolCallId": call, "arguments": {
            "name": description.lower().replace(" ", "-")[:30], "description": description,
            "agent_type": agent, "mode": mode, "prompt": prompt}})
        self.add("subagent.started", {"toolCallId": call, "agentName": agent, "agentDisplayName": agent,
                                      "agentType": agent, "executionMode": mode, "model": "demo-model"}, agent_id)
        self.add("subagent.configured", {"model": "demo-model", "reasoningEffort": "medium", "contextTier": "default"}, agent_id)
        self.tick(minutes)
        self.add("subagent.completed", {"toolCallId": call, "agentName": agent, "model": "demo-model",
                                        "durationMs": minutes * 60_000, "totalTokens": tokens, "totalToolCalls": calls}, agent_id)
        self.spend(tokens / 400, 3)
        if follow_up:
            self.tick(3)
            self.add("tool.execution_start", {"toolName": "write_agent", "toolCallId": call + "-f",
                                              "arguments": {"agent_id": agent_id, "message": follow_up}})
            self.tick(max(2, minutes // 2))
            self.add("system.notification", {"content": f'Agent "{agent}" has finished processing and is now idle.',
                                              "kind": {"type": "agent_idle", "agentId": agent_id, "agentType": agent}})
            self.spend(tokens / 800, 2)

    def close(self):
        self.tick(2)
        self.add("session.shutdown", {"totalNanoAiu": self.usage, "shutdownType": "routine"})
        directory = SESSIONS / self.id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in self.events))
        (directory / "workspace.yaml").write_text(
            f"id: {self.id}\ncwd: ~/offices/tiny-search\nname: {self.name}\nuser_named: true\n"
            f"created_at: {iso(self.created)}\nupdated_at: {iso(self.clock)}\n")


def build_sessions(n):
    s = Session("setup", "Office setup and goal", n["lead"], at(35, 17))
    s.say("Here is the goal: measure how well Tiny Search ranks results on our 2,000-query test set.")
    s.delegate(n["researcher"], "Find standard search metrics", "List common ranking metrics (recall@k, MRR, nDCG) with one-line definitions and sources.", 18, 21000, 14)
    s.say("Record the goal and metrics in the project state.", cost=25)
    s.close()

    s = Session("kickoff", "Kickoff: plan the evaluation", n["lead"], at(9, 16))
    s.say("Plan the first evaluation of Tiny Search: baseline vs the new dense ranker.")
    s.delegate(n["navigator"], "Map the indexing and ranking code", "Trace how documents are indexed and how a query is ranked. Cite files and functions.", 14, 32000, 25)
    s.delegate(n["researcher"], "Survey BM25 vs dense baselines", "Summarize what published comparisons report for BM25 vs dense retrieval on small corpora.", 22, 41000, 30)
    s.say("Double check the plan before we spend compute.", cost=30)
    s.delegate(n["guardian"], "Second opinion on the eval plan", "Check the evaluation plan: matched queries, same index, one variable changed.", 6, 9000, 5, mode="sync")
    s.close()

    s = Session("harness", "Build the benchmark harness", n["lead"], at(5, 18))
    s.say("Build a benchmark harness that runs the 2,000 queries and records recall@10 and latency.")
    s.delegate(n["builder"], "Add the benchmark harness", "Implement bench/run.py: load queries, run the ranker, write recall@10 and p50/p95 latency to JSON. Add tests.",
               35, 88000, 61, follow_up="Also record the index build time.")
    s.delegate(n["reviewer"], "Review the benchmark harness", "Review bench/run.py and its tests for correctness and timing bias.", 12, 26000, 18, mode="sync")
    s.say("Give me the launch commands for the three runs.", cost=20)
    s.close()

    s = Session("analysis", "Analyze recall results", n["lead"], at(2, 20))
    s.say("Runs are done. Compare them.")
    s.delegate(n["analyst"], "Compare recall@10 across runs", "Compare bm25-baseline, dense-v1 (failed) and dense-v2. Check the query sets match.", 20, 47000, 33)
    s.delegate(n["analyst"], "Plot latency vs recall", "Make one figure: p95 latency vs recall@10 per run, with the baseline marked.", 15, 30000, 21)
    s.say("Are these numbers right? Get a second opinion.", cost=25)
    s.delegate(n["guardian"], "Check the recall numbers", "Re-derive recall@10 for dense-v2 from the raw results and confirm the denominator.", 8, 12000, 7, mode="sync")
    s.close()

    s = Session("docs", "Runbook and cleanup", n["lead"], at(1, 17))
    s.say("Write down how to rerun the benchmark.")
    s.delegate(n["curator"], "Write the benchmark runbook", "Write docs/benchmark.md: setup, commands, outputs, known pitfalls.", 16, 22000, 15)
    s.delegate(n["reviewer"], "Check the runbook commands", "Check every command in docs/benchmark.md against bench/run.py options.", 7, 11000, 9, mode="sync")
    s.close()

    s = Session("hybrid", "Hybrid ranker run", n["lead"], NOW - timedelta(hours=2))
    s.say("Prepare the hybrid ranker run; I'll launch it myself.")
    s.delegate(n["builder"], "Prepare the hybrid run config", "Add configs/hybrid-v1.json mixing BM25 and dense scores 50/50.", 10, 18000, 12)
    s.close()

    s = Session("frontdesk", "Find the analysis session", n["frontdesk"], NOW - timedelta(minutes=50))
    s.say("Where was the session where we compared recall?", cost=5)
    s.close()

    s = Session("counsel", "Quick second opinion", n["guardian"], NOW - timedelta(minutes=30))
    s.say("Is 2,000 queries enough to see a 2-point recall difference?", cost=15)
    s.close()


def build_records(n):
    office = HOME / ".agent-office"
    (office / "PROJECT_STATE.md").write_text("""# Tiny Search

## Current snapshot

**Goal.** Measure how well Tiny Search ranks results on a fixed 2,000-query
test set, and compare the BM25 baseline with a dense ranker and a hybrid.

**Status.** Baseline and dense-v2 are done; dense-v1 failed (out of memory).
The hybrid run is in progress. Recall@10: baseline 0.61, dense-v2 0.68.

**Next.** Finish the hybrid run, then decide which ranker to ship.

This is a fictional demo project.
""")
    (office / "DECISIONS.md").write_text("""# Decisions

## D001: Use recall@10 as the main metric

Latency is reported alongside it; nDCG is a secondary check.

## D002: Fix the query set

All runs use the same 2,000 queries and the same index snapshot.

## D003: Rerun dense with a smaller batch

dense-v1 ran out of memory; dense-v2 uses batch size 64.
""")
    task = lambda tid, title, status, when, **extra: {"task_id": tid, "title": title, "owner_employee_id": "lead",
                                                     "owner_name": n["lead"], "status": status, "requested_at": iso(when),
                                                     "sessions": [], **extra}
    write_json(office / "tasks.json", {"schema_version": 1, "project_id": "tiny-search", "tasks": [
        task("TS-001", "Build the benchmark harness", "completed", at(5, 18), delivery="Harness merged with tests; reviewed."),
        task("TS-002", "Compare rankers", "in_progress", at(2, 20), next_action="Analyze the hybrid run when it finishes."),
        task("TS-003", "Benchmark runbook", "completed", at(1, 17)),
    ]})
    config_path = office / "dashboard.json"
    config = json.loads(config_path.read_text())
    config.update(plan_aic=300000, aic_per_log_unit=40,
                  calibration={"used_aic": None, "log_units": None, "measured_at": iso(NOW), "since": iso(NOW), "note": "demo values"})
    write_json(config_path, config)


def build_runs():
    runs = HOME / "run-logs"
    plan = [
        ("run-0001", "bm25-baseline", "node-a", at(5, 21), "completed", 8, 2000, 0),
        ("run-0002", "dense-v1", "node-b", at(5, 21, 5), "failed: out of memory while encoding batch 3", 2, 410, 0),
        ("run-0003", "dense-v2", "node-b", at(4, 2), "completed", 8, 1997, 3),
        ("run-0004", "hybrid-v1", "node-a", NOW - timedelta(hours=1, minutes=40), None, 5, 1240, 1),
    ]
    registry = {}
    for run_dir, name, host, started, status, steps, done, failed in plan:
        entry = {"name": name, "host": host, "rid": "demo-" + run_dir, "started": started.strftime("%Y-%m-%d %H:%M UTC"),
                 "attempt": 1, "setup": f"{name}: 2,000 queries, index snapshot S1, recall@10 and latency."}
        if status:
            entry["status"] = status
        registry[run_dir] = entry
        last = started + timedelta(minutes=10 * steps) if status else NOW - timedelta(minutes=4)
        updates = " ".join(f"v{i}@{(started + timedelta(minutes=10 * i)).strftime('%H:%M')}" for i in range(1, steps + 1))
        state = "running" if not status else ("failed" if "fail" in status else "finished")
        (runs / f"{name}.health.log").write_text(
            f"{last.strftime('%H:%M')} {name} [queries=2000] up {steps * 10}m (capture -) state={state}\n"
            f"  updates={updates}\n"
            f"  traj started={done + failed} ended={done} failed={failed}\n")
        events = "- Out of memory at batch 3; rerun as dense-v2 with batch 64.\n" if status and "fail" in status else "- Nothing unusual.\n"
        (runs / f"{name}.md").write_text(f"# {name}\n\nHost {host}. Fictional demo run.\n\n## Issues / events\n{events}")
    write_json(runs / "registry.json", registry)


def neutralize_paths():
    """Rendered agents embed the absolute office path; show a neutral one in the demo."""
    import hashlib
    manifest_path = HOME / ".agent-office/generated-profiles.json"
    manifest = json.loads(manifest_path.read_text())
    for name, entry in manifest["outputs"].items():
        agent = HOME / entry["relative_path"]
        text = agent.read_text().replace(str(HOME), "~/offices/tiny-search")
        agent.write_text(text)
        entry["sha256"] = hashlib.sha256(text.encode()).hexdigest()
    write_json(manifest_path, manifest)
    leaks = {str(HOME), str(REPO), str(Path.home())}
    for root in (HOME, SESSIONS):
        for path in root.rglob("*"):
            if path.is_file() and ".dashboard-cache" not in path.parts:
                text = path.read_text(errors="ignore")
                if any(leak in text for leak in leaks):
                    raise SystemExit(f"absolute path left in {path}")


def main():
    for path in (HOME, SESSIONS):
        if path.exists():
            shutil.rmtree(path)
    subprocess.run([sys.executable, str(REPO / "office.py"), "init", "--home", str(HOME), "--project-id", "tiny-search",
                    "--name", "Tiny Search", "--theme", "pokemon", "--timezone", "UTC"], check=True)
    staff = json.loads((HOME / ".agent-office/staff.json").read_text())
    names = {e["employee_id"]: e["name"] for e in staff["employees"]}
    names["frontdesk"] = json.loads((HOME / ".agent-office/dashboard.json").read_text())["frontdesk"]["name"]
    build_records(names)
    build_runs()
    build_sessions(names)
    neutralize_paths()
    print(f"Demo office: {HOME}\nDemo session logs: {SESSIONS}")
    print("Open it with:\n  python3 office.py dashboard --home example/demo-office --sessions example/session-state")


if __name__ == "__main__":
    main()

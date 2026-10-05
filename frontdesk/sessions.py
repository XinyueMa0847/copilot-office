#!/usr/bin/env python3
"""Front-desk session index: list Copilot CLI sessions and build exact resume commands."""

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

USER_DIR = Path(os.environ.get("COPILOT_USER_DIR", Path.home() / ".copilot")).expanduser()
STATE = Path(os.environ.get("COPILOT_SESSION_STATE", USER_DIR / "session-state")).expanduser()
SETTINGS = USER_DIR / "settings.json"
DESK = Path(
    os.environ.get("COPILOT_FRONTDESK_STATE", USER_DIR / "office" / "frontdesk")
).expanduser()
INDEX = DESK / "index.json"
NOTES = DESK / "notes.json"
FRONTDESK_NAME = os.environ.get("COPILOT_FRONTDESK_NAME", "frontdesk")
KEYS = ('"session.start"', '"session.resume"', '"session.model_change"',
        '"subagent.selected"', '"user.message"')


def load(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def workspace(d):
    out = {}
    try:
        for line in (d / "workspace.yaml").read_text().splitlines():
            if ":" in line and not line.startswith(" "):
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def clip(text, n=160):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def scan_events(path):
    info = {"agent": None, "model": None, "effort": None, "context": None,
            "first_message": None, "last_message": None, "user_messages": 0}
    try:
        f = path.open("r", errors="replace")
    except OSError:
        return info
    with f:
        for line in f:
            if not any(k in line[:60] for k in KEYS):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            t, d = e.get("type"), e.get("data") or {}
            if t in ("session.start", "session.resume"):
                info["model"] = d.get("selectedModel") or info["model"]
                info["effort"] = d.get("reasoningEffort") or info["effort"]
                info["context"] = d.get("contextTier") or info["context"]
            elif t == "session.model_change":
                info["model"] = d.get("newModel") or info["model"]
                info["effort"] = d.get("reasoningEffort") or info["effort"]
                if d.get("contextTier"):
                    info["context"] = d["contextTier"]
            elif t == "subagent.selected" and "agentId" not in e:
                info["agent"] = d.get("agentName")  # main-session agent switch
            elif t == "user.message":
                msg = d.get("content", "")
                if msg.startswith("<"):
                    continue
                info["user_messages"] += 1
                info["first_message"] = info["first_message"] or clip(msg)
                info["last_message"] = clip(msg)
    return info


def checkpoints(d):
    titles = []
    try:
        for line in (d / "checkpoints/index.md").read_text().splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) > 3 and parts[1].isdigit():
                titles.append(parts[2])
    except OSError:
        pass
    return titles


def in_use(d):
    for lock in d.glob("inuse.*.lock"):
        try:
            os.kill(int(lock.name.split(".")[1]), 0)
            return True
        except (ValueError, ProcessLookupError):
            continue
        except PermissionError:
            return True
    return False


def build_index():
    cache = load(INDEX, {})
    out = {}
    try:
        directories = STATE.iterdir()
    except OSError:
        directories = ()
    for d in directories:
        if not d.is_dir():
            continue
        ev = d / "events.jsonl"
        try:
            st = ev.stat()
            sig = [st.st_size, int(st.st_mtime)]
        except OSError:
            sig = None
        old = cache.get(d.name)
        if old and old.get("_sig") == sig:
            entry = old
        else:
            entry = scan_events(ev) if sig else scan_events(Path("/nonexistent"))
            entry["_sig"] = sig
        ws = workspace(d)
        entry.update(id=d.name, name=ws.get("name", ""), cwd=ws.get("cwd", str(Path.home())),
                     created=ws.get("created_at", ""), updated=ws.get("updated_at", ""),
                     checkpoints=checkpoints(d), in_use=in_use(d))
        out[d.name] = entry
    DESK.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(out, indent=1))
    notes = load(NOTES, {})
    for sid, entry in out.items():
        entry["note"] = notes.get(sid, "")
    return out


def sessions(include_frontdesk=False):
    rows = list(build_index().values())
    if not include_frontdesk:
        rows = [r for r in rows if r.get("name") != FRONTDESK_NAME
                and r.get("agent") != FRONTDESK_NAME
                and (r.get("user_messages") or r.get("checkpoints"))]
    return sorted(rows, key=lambda r: r.get("updated") or "", reverse=True)


def resolve(key):
    if not isinstance(key, str) or not key.strip():
        sys.exit("session key must not be empty")
    rows = sessions(include_frontdesk=True)
    exact = [r for r in rows if r["id"] == key or r["name"] == key]
    hits = exact or [r for r in rows if r["id"].startswith(key)]
    if len(hits) != 1:
        sys.exit(f"'{key}' matches {len(hits)} sessions; use a longer ID prefix")
    return hits[0]


def resume_command(r, agent=None, model=None, effort=None, context="long_context"):
    agent = agent or r.get("agent")
    prefs = load(SETTINGS, {}).get("subagents", {}).get("agents", {}).get(agent or "", {})
    model = model or prefs.get("model") or r.get("model")
    effort = effort or prefs.get("effortLevel") or r.get("effort")
    cmd = ["copilot", "--resume", r["id"]]
    if agent:
        cmd += ["--agent", agent]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--reasoning-effort", effort]
    cmd += ["--context", context]
    return f"cd {shlex.quote(r['cwd'])} && " + " ".join(shlex.quote(c) for c in cmd)


def show(r, verbose=False):
    flags = " [IN USE]" if r.get("in_use") else ""
    print(f"{r['id'][:8]}  {r['updated'][:16]}  {r['name'] or '(unnamed)'}{flags}")
    print(f"    agent={r.get('agent') or '-'} model={r.get('model')} effort={r.get('effort')} "
          f"context={r.get('context')} msgs={r.get('user_messages')} cwd={r['cwd']}")
    if r.get("note"):
        print(f"    note: {r['note']}")
    if r.get("checkpoints"):
        print(f"    checkpoints: {' / '.join(r['checkpoints'][-3:])}")
    print(f"    first: {r.get('first_message') or '-'}")
    if verbose:
        print(f"    last:  {r.get('last_message') or '-'}")


def main():
    global USER_DIR, STATE, SETTINGS, DESK, INDEX, NOTES, FRONTDESK_NAME
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--user-dir",
        type=Path,
        default=USER_DIR,
        help="Copilot user directory (default: $COPILOT_USER_DIR or ~/.copilot)",
    )
    ap.add_argument(
        "--sessions-dir",
        type=Path,
        default=None,
        help="session-state directory (default: $COPILOT_SESSION_STATE or USER/session-state)",
    )
    ap.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="front-desk cache/notes directory (default: $COPILOT_FRONTDESK_STATE or USER/office/frontdesk)",
    )
    ap.add_argument(
        "--frontdesk-name",
        default=FRONTDESK_NAME,
        help="agent name to omit from ordinary listings",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="recent sessions, newest first")
    ls.add_argument("--limit", type=int, default=20)
    ls.add_argument("--query", help="case-insensitive text filter over name/messages/checkpoints/notes")
    ls.add_argument("--all", action="store_true", help="include front-desk sessions")
    sh = sub.add_parser("show", help="full details for one session")
    sh.add_argument("session")
    rc = sub.add_parser("resume", help="print the exact resume command")
    rc.add_argument("session")
    rc.add_argument("--agent")
    rc.add_argument("--model")
    rc.add_argument("--effort")
    nt = sub.add_parser("note", help="attach a short description to a session")
    nt.add_argument("session")
    nt.add_argument("text")
    a = ap.parse_args()

    USER_DIR = a.user_dir.expanduser()
    STATE = (
        a.sessions_dir
        or (
            Path(os.environ["COPILOT_SESSION_STATE"])
            if os.environ.get("COPILOT_SESSION_STATE")
            else USER_DIR / "session-state"
        )
    ).expanduser()
    SETTINGS = USER_DIR / "settings.json"
    DESK = (
        a.state_dir
        or (
            Path(os.environ["COPILOT_FRONTDESK_STATE"])
            if os.environ.get("COPILOT_FRONTDESK_STATE")
            else USER_DIR / "office" / "frontdesk"
        )
    ).expanduser()
    INDEX = DESK / "index.json"
    NOTES = DESK / "notes.json"
    FRONTDESK_NAME = a.frontdesk_name

    if a.cmd == "list":
        rows = sessions(a.all)
        if a.query:
            q = a.query.lower()
            rows = [r for r in rows if q in json.dumps(
                [r.get(k) for k in ("name", "note", "first_message", "last_message", "checkpoints", "agent", "id")]).lower()]
        for r in rows[: a.limit]:
            show(r)
    elif a.cmd == "show":
        r = resolve(a.session)
        show(r, verbose=True)
        print("    resume: " + resume_command(r))
    elif a.cmd == "resume":
        print(resume_command(resolve(a.session), a.agent, a.model, a.effort))
    elif a.cmd == "note":
        r = resolve(a.session)
        notes = load(NOTES, {})
        notes[r["id"]] = a.text
        NOTES.write_text(json.dumps(notes, indent=1))
        print(f"noted {r['id'][:8]}: {a.text}")


if __name__ == "__main__":
    main()

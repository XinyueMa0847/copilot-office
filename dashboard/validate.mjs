// Client checks with a small DOM test double and temporary collector fixtures.
// Not a browser: layout, CSS, and native input/dialog behavior remain unverified.
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import vm from "node:vm";
import {execFileSync} from "node:child_process";
import {fileURLToPath} from "node:url";

const dir = path.dirname(fileURLToPath(import.meta.url));
const html = fs.readFileSync(path.join(dir, "index.html"), "utf8");
const embedded = JSON.parse(html.match(/<script id="snapshot" type="application\/json">([\s\S]*?)<\/script>/)[1]);
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)][0][1];
new vm.Script(script);
assert.equal(embedded, null, "the repository ships with no embedded office snapshot");
assert.doesNotMatch(script, /\b(?:innerHTML|outerHTML|insertAdjacentHTML|eval|XMLHttpRequest|WebSocket)\b/);
assert.doesNotMatch(html, /<(?:script|link|img)[^>]+(?:src|href)=["'](?:https?:)?\/\//i);

const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "copilot-office-dashboard-"));
try {
  const officeHome = path.join(temporary, "office");
  const sessionsDir = path.join(temporary, "sessions");
  const userAgents = path.join(temporary, "user-agents");
  for (const directory of [
    path.join(officeHome, ".agent-office"),
    path.join(officeHome, ".github", "agents"),
    path.join(officeHome, "run-logs"),
    sessionsDir,
    userAgents,
  ]) fs.mkdirSync(directory, {recursive: true});

  const write = (relative, value) => {
    const destination = path.join(officeHome, relative);
    fs.mkdirSync(path.dirname(destination), {recursive: true});
    fs.writeFileSync(destination, typeof value === "string" ? value : JSON.stringify(value));
  };
  const identities = [
    ["lead", "office-lead", "picard", "Manager"],
    ["guardian", "office-guardian", "troi", "Counselor"],
    ["builder", "office-builder", "laforge", "Builder"],
    ["reviewer", "office-reviewer", "worf", "Reviewer"],
    ["analyst", "office-analyst", "crusher", "Results Analyst"],
    ["navigator", "office-navigator", "data", "Code Reader"],
    ["researcher", "office-researcher", "guinan", "Researcher"],
    ["curator", "office-curator", "barclay", "Docs Curator"],
  ];
  const outputs = {};
  const employees = identities.map(([employee_id, role_profile, name, title]) => {
    const definition = `---\nname: ${name}\ndescription: Fixture ${title}\nmodel: fixture-model\ntools: [read]\n---\n\nYou are **${name}**, employee ID \`${employee_id}\`.\n`;
    write(`.github/agents/${name}.agent.md`, definition);
    outputs[name] = {sha256: crypto.createHash("sha256").update(definition).digest("hex")};
    return {employee_id, role_profile, profile: name, name, title, aliases: [], sessions: [], assignments: []};
  });
  write(".agent-office/project.json", {
    schema_version: 1, project_id: "my-project", display_name: "My Project", status: "active",
    records: {state: "PROJECT_STATE.md", decisions: "DECISIONS.md", staff: "staff.json", tasks: "tasks.json"},
    repository: {status: "not_configured", readiness_scope: ""},
  });
  write(".agent-office/staff.json", {schema_version: 1, project_id: "my-project", theme: "startrek", employees});
  write(".agent-office/tasks.json", {schema_version: 1, project_id: "my-project", tasks: []});
  write(".agent-office/dashboard.json", {
    schema_version: 1, timezone: "America/Vancouver", plan_aic: null,
    aic_per_log_unit: null, calibration: null,
    frontdesk: {name: "computer", title: "Front Desk"},
  });
  write(".agent-office/generated-profiles.json", {schema_version: 1, project_id: "my-project", outputs});
  write(".agent-office/PROJECT_STATE.md", "# Project state\n");
  write(".agent-office/DECISIONS.md", "# Decisions\n");
  write(".agent-office/STARTING_BRIEF.md", "# Starting brief\n");
  fs.writeFileSync(path.join(userAgents, "computer.agent.md"),
    "---\nname: computer\ndescription: Fixture Front Desk\nmodel: fixture-model\ntools: [read]\n---\n");

  const registry = {
    "done-dir": {name: "done-run", host: "host-a", started: "2026-09-28 12:00 UTC", status: "completed"},
    "failed-dir": {name: "failed-run", host: "host-b", started: "2026-09-21 12:00 UTC", status: "failed_terminated"},
    "stopped-dir": {name: "stopped-run", host: "host-c", started: "2026-09-14 12:00 UTC", status: "stopped_by_user"},
  };
  write("run-logs/registry.json", registry);
  write("run-logs/README.md",
    "| Name | Host | Run dir | Status |\n|---|---|---|---|\n" +
    "| done-run | host-a | `done-dir` | completed |\n" +
    "| failed-run | host-b | `failed-dir` | failed |\n" +
    "| stopped-run | host-c | `stopped-dir` | stopped |\n");
  for (const entry of Object.values(registry)) {
    write(`run-logs/${entry.name}.md`, `# ${entry.name}\n\n## Issues / events\n- fixture event\n`);
    write(`run-logs/${entry.name}.health.log`,
      `12:00 ${entry.name} [x] up 1h (capture -) state=finished\n  updates=v1@11:00\n  traj started=10 ended=10 failed=0 groups={}\n`);
  }

  const sessionPath = path.join(sessionsDir, "fixture-session");
  fs.mkdirSync(sessionPath);
  fs.writeFileSync(path.join(sessionPath, "workspace.yaml"),
    "id: fixture-session\nname: Fixture session\ncreated_at: 2026-10-05T12:00:00Z\nupdated_at: 2026-10-05T12:05:00Z\n");
  const events = [
    {type: "subagent.selected", timestamp: "2026-10-05T12:00:00Z", data: {agentName: "picard"}},
    {type: "user.message", timestamp: "2026-10-05T12:01:00Z", data: {content: "Fixture request"}},
    {type: "tool.execution_start", timestamp: "2026-10-05T12:02:00Z",
      data: {toolName: "task", toolCallId: "call-1", arguments: {
        name: "Fixture build", description: "Build fixture", prompt: "fixture prompt",
        agent_type: "office-builder", mode: "background",
      }}},
    {type: "subagent.started", timestamp: "2026-10-05T12:02:01Z", agentId: "worker-1",
      data: {toolCallId: "call-1", agentName: "office-builder", agentType: "office-builder", executionMode: "background"}},
    {type: "subagent.completed", timestamp: "2026-10-05T12:03:00Z", agentId: "worker-1",
      data: {agentName: "office-builder", durationMs: 59000, totalTokens: 100, totalToolCalls: 2}},
    {type: "session.usage_checkpoint", timestamp: "2026-10-05T12:04:00Z", data: {totalNanoAiu: 7000000000}},
  ];
  fs.writeFileSync(path.join(sessionPath, "events.jsonl"), events.map(value => JSON.stringify(value)).join("\n") + "\n");

  const python = (code, args) => JSON.parse(execFileSync("python3", ["-B", "-c", code, ...args], {
    cwd: dir, encoding: "utf8", maxBuffer: 32 << 20,
  }));
  const recordsFeed = python(
    "import json,sys,server; print(json.dumps(server.collect(sys.argv[1], sys.argv[2])))",
    [officeHome, userAgents],
  );
  const realOffice = {
    schemaVersion: 1, projectId: "my-project", revision: "real",
    collectedAt: new Date().toISOString(),
    snapshot: python(
      "import json,sys; from session_index import SessionIndex; i=SessionIndex(office=sys.argv[1],root=sys.argv[2]); i.refresh(); print(json.dumps(i.snapshot()))",
      [officeHome, sessionsDir],
    ),
  };
  assert.equal(recordsFeed.projectId, "my-project");
  assert.equal(recordsFeed.snapshot.project.displayName, "My Project");
  assert.equal(realOffice.snapshot.coverage.officeSessions, 1);
  assert.equal(realOffice.snapshot.credits.calibrated, false);
  const embeddedRecords = structuredClone(recordsFeed);
  embeddedRecords.mode = "embedded-snapshot";
  const emptyRecords = structuredClone(recordsFeed);
  emptyRecords.revision = "empty-runs";
  emptyRecords.snapshot.runs = [];
  emptyRecords.snapshot.activeRunCount = 0;

  const now = Date.now();
  const iso = milliseconds => new Date(now - milliseconds).toISOString();
  const localDay = milliseconds => new Date(now - milliseconds).toLocaleDateString("en-CA", {timeZone: "America/Vancouver"});
  const zero = {open: 0, week: 0, month: 0, total: 0};
  const projectPeople = identities.map(([, , name, title]) => ({name, title, scope: "project"}));
  projectPeople.push({name: "computer", title: "Front Desk", scope: "office"});
  function synthetic({credits = "plan", extra = {}} = {}) {
    const delegations = [
      {id: "d1", agent: "laforge", title: "Builder", role: false, builtin: false, parent: null, issuer: "picard",
       task: {name: "build", description: '<img src=x onerror="alert(1)"> Build thing', prompt: "Long prompt text"},
       model: "fixture-model", effort: "high", mode: "background", started: iso(3600e3), lastActivity: iso(600e3),
       completed: null, status: "running", turns: 2, followUps: 1, durationMs: null, tokens: 1200,
       toolCalls: 9, reportedLogUnits: null},
      {id: "d2", agent: "troi", title: "Counselor", role: false, builtin: false, parent: null, issuer: "picard",
       task: {name: "check", description: "Second opinion", prompt: "p"}, model: "fixture-model", effort: "medium",
       mode: "sync", started: iso(3000e3), lastActivity: iso(2900e3), completed: iso(2900e3), status: "completed",
       turns: 1, followUps: 0, durationMs: 90000, tokens: 5000, toolCalls: 3, reportedLogUnits: 2},
      {id: "d3", agent: "worf", title: "Reviewer", role: false, builtin: false, parent: "d1", issuer: "laforge",
       task: {name: "review", description: "Nested review", prompt: "q"}, model: "fixture-model", effort: "medium",
       mode: "sync", started: iso(2000e3), lastActivity: iso(1900e3), completed: iso(1900e3), status: "completed",
       turns: 1, followUps: 0, durationMs: 60000, tokens: 800, toolCalls: 2, reportedLogUnits: null},
    ];
    const session = {
      id: "aaaaaaaa-1111-2222-3333-444444444444", name: "main_monitor", title: "first message", agents: ["picard"],
      created: iso(7200e3), updated: iso(60e3), status: "working", attached: true, lastResume: iso(7200e3),
      usage: {totalLogUnits: 10, monthLogUnits: 10, weekLogUnits: 10,
        monthEstAic: credits === "uncalibrated" ? null : 3.6, premiumRequests: 5, byDay: []},
      context: {currentTokens: 120000, conversationTokens: 90000, observedAt: iso(60e3)},
      days: [{date: localDay(0), messages: 4, delegations: ["d1", "d2", "d3"]}], delegations,
    };
    const agent = ({name, title, scope}, overrides = {}) => ({
      name, title, scope, status: "idle", direct: {...zero}, delegated: {...zero},
      tokens: {week: 0, total: 0},
      usage: {monthLogUnits: 0, monthEstAic: credits === "uncalibrated" ? null : 0, monthShare: 0},
      current: [], week: [], ...overrides,
    });
    const ref = delegation => ({session: session.id, delegation});
    const creditData = credits === "uncalibrated"
      ? {unit: "log units", calibrated: false, factor: null, calibration: null, planAic: null,
         monthUtc: "2026-10", monthLogUnits: 10, monthEstAic: null}
      : {unit: "AIC", calibrated: true, factor: 0.36, calibration: {used_aic: 10, log_units: 27.8},
         planAic: credits === "plan" ? 1_000_000 : null, monthUtc: "2026-10", monthLogUnits: 10, monthEstAic: 3.6};
    const agents = projectPeople.map(person => agent(person));
    Object.assign(agents.find(a => a.name === "picard"), {
      status: "working", direct: {open: 1, week: 1, month: 1, total: 1},
      current: [ref(null)], week: [ref(null)],
      usage: {monthLogUnits: 10, monthEstAic: credits === "uncalibrated" ? null : 3.6, monthShare: 1},
    });
    Object.assign(agents.find(a => a.name === "laforge"), {
      status: "working", delegated: {open: 1, week: 1, month: 1, total: 1},
      current: [ref("d1")], week: [ref("d1")], tokens: {week: 1200, total: 1200},
    });
    Object.assign(agents.find(a => a.name === "troi"), {
      delegated: {open: 0, week: 1, month: 1, total: 1}, week: [ref("d2")],
    });
    Object.assign(agents.find(a => a.name === "worf"), {
      delegated: {open: 0, week: 1, month: 1, total: 1}, week: [ref("d3")],
    });
    return {schemaVersion: 1, projectId: "my-project", revision: "r1", collectedAt: iso(0), snapshot: {
      schemaVersion: 1, generatedAt: iso(0), timezone: "America/Vancouver",
      periods: {
        weekStart: new Date(now - 6 * 864e5).toISOString(),
        monthStart: new Date(Date.UTC(new Date(now).getUTCFullYear(), new Date(now).getUTCMonth(), 1)).toISOString(),
        creditsMonthStart: new Date(Date.UTC(new Date(now).getUTCFullYear(), new Date(now).getUTCMonth(), 1)).toISOString(),
      },
      coverage: {sessionsScanned: 3, officeSessions: 1, otherSessions: 2, bytesIndexed: 10,
        since: iso(864e5), malformedLines: 0, building: false},
      credits: creditData,
      stats: {open: 2, week: 4, month: 4, total: 4, weekByIssuer: {you: 1, picard: 2, laforge: 1}},
      agents, sessions: [session], ...extra,
    }};
  }

  class Element {
    constructor(tag, doc) {
      this.tagName = tag.toLowerCase(); this.document = doc; this.children = []; this.attributes = {}; this.events = {};
      this.className = ""; this._text = ""; this.id = ""; this.files = []; this.value = ""; this.hidden = false; this._open = false;
      this.style = {props: {}, setProperty(key, value) { this.props[key] = value; }};
    }
    set textContent(value) { this.children = []; this._text = String(value); }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
    get open() { return this._open; }
    set open(value) { this._open = Boolean(value); }
    get lastChild() { return this.children.at(-1) || null; }
    append(...nodes) {
      for (let node of nodes) {
        if (typeof node === "string") { const text = new Element("#text", this.document); text.textContent = node; node = text; }
        node.parent = this; this.children.push(node);
      }
    }
    replaceChildren(...nodes) { this.children = []; this._text = ""; this.append(...nodes); }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    getAttribute(key) { return this.attributes[key] ?? null; }
    addEventListener(name, handler) { (this.events[name] ||= []).push(handler); }
    async dispatch(name, event = {}) {
      await Promise.all((this.events[name] || []).map(handler => handler({target: this, key: event.key, ...event})));
    }
    click() { return this.dispatch("click"); }
    focus() { this.document.activeElement = this; }
    showModal() { this._open = true; }
    close() { this._open = false; void this.dispatch("close"); }
    getBoundingClientRect() { return {left: 0, top: 0, right: 500, bottom: 900}; }
    querySelector(selector) {
      return walk(this).find(node => selector.startsWith(".")
        ? node.className.split(" ").includes(selector.slice(1)) : node.tagName === selector) || null;
    }
    contains(node) { return node === this || walk(this).includes(node); }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); }
  }
  const walk = root => root.children.flatMap(child => [child, ...walk(child)]);
  const IDS = [
    "brand-name","tab-office","tab-tasks","tab-runs","view-office","view-tasks","view-runs","live","live-dot",
    "live-text","live-records","live-office","live-coverage","live-errors","live-toggle","office-banner","stats",
    "floor","tasks-banner","chains","tasks-count","runs-banner","runs-live","runs-history","drawer","drawer-close",
    "drawer-content","snapshot",
  ];
  function env({hosted = false, saved = new Map(), failSet = false, snapshot = embeddedRecords} = {}) {
    const doc = {activeElement: null, title: ""};
    const root = new Element("body", doc);
    doc.getElementById = id => walk(root).find(node => node.id === id);
    doc.createTextNode = value => { const node = new Element("#text", doc); node.textContent = value; return node; };
    doc.createElement = tag => {
      const node = new Element(tag, doc);
      if (tag === "canvas") {
        node.getContext = () => ({drawImage() {}});
        node.toDataURL = () => "data:image/png;base64," + Buffer.from("x" + Math.random()).toString("base64");
      }
      return node;
    };
    for (const id of IDS) {
      const node = doc.createElement(id === "drawer" ? "dialog" : id === "live" ? "details" : "div");
      node.id = id; root.append(node);
    }
    const segment = doc.createElement("div"); segment.className = "seg";
    for (const value of ["week","month","all"]) {
      const button = doc.createElement("button"); button.setAttribute("data-period", value); segment.append(button);
    }
    doc.getElementById("view-tasks").append(segment);
    doc.getElementById("snapshot").textContent = JSON.stringify(snapshot);
    const control = {requests: [], responses: [], timers: []};
    const storage = {
      getItem: key => saved.get(key) ?? null,
      setItem: (key, value) => {
        if (failSet) { const error = new Error("full"); error.name = "QuotaExceededError"; throw error; }
        saved.set(key, value);
      },
      removeItem: key => saved.delete(key),
    };
    class Image {
      set src(url) {
        if (url) queueMicrotask(() => {
          this.naturalWidth = 300; this.naturalHeight = 200; this.onload?.();
        });
      }
    }
    const context = vm.createContext({
      document: doc, localStorage: storage, Image, Uint8Array, AbortController, Date, Math, JSON,
      location: {protocol: hosted ? "http:" : "file:"},
      setTimeout: hosted ? (callback, delay) => {
        control.timers.push({callback, delay}); return control.timers.length;
      } : setTimeout,
      clearTimeout: () => {},
      URL: {createObjectURL: () => "blob:fixture", revokeObjectURL() {}},
      fetch: async url => {
        control.requests.push(url);
        const response = control.responses.shift();
        if (!response) throw new Error("server unavailable");
        return response;
      },
    });
    vm.runInContext(script, context);
    const $ = id => doc.getElementById(id);
    const find = (node, predicate) => walk(node).find(predicate);
    const byKey = key => find(root, node => node.getAttribute?.("data-key") === key);
    const openFold = async key => {
      const details = byKey(key); assert.ok(details, "fold " + key);
      details.open = true; await details.dispatch("toggle"); return details;
    };
    const feed = (name, data) => {
      context.__data = structuredClone(data);
      vm.runInContext(`receive(${JSON.stringify(name)}, __data); renderAll(); updateLive();`, context);
    };
    return {doc, root, $, control, saved, context, find, byKey, openFold, feed};
  }
  const png = () => ({
    type: "image/png", size: 100,
    slice: () => ({arrayBuffer: async () => new Uint8Array([137,80,78,71,13,10,26,10,0,0,0,0]).buffer}),
  });
  const settle = () => new Promise(resolve => setImmediate(resolve));
  const clickPeriod = (environment, value) =>
    environment.find(environment.$("view-tasks"), node => node.getAttribute?.("data-period") === value).click();
  const head = node => node.children[0].textContent;
  let count = 0;
  async function check(name, operation) {
    await operation();
    count += 1;
    console.log("PASS " + name);
  }

  await check("from disk: floor seats and run history render from synthetic embedded records; no fetch", async () => {
    const environment = env();
    const seats = walk(environment.$("floor")).filter(node => node.className === "seat");
    assert.deepEqual(seats.map(seat => seat.id).sort(),
      [...identities.map(([, , name]) => name), "computer"].map(name => "seat-" + name).sort());
    assert.match(environment.$("office-banner").textContent, /need the server/);
    assert.equal(environment.$("live-text").textContent, "Snapshot");
    assert.ok(environment.$("runs-history").children.length > 0);
    assert.equal(environment.control.requests.length, 0);
  });

  await check("office layout: lead and guardian offices, three employee-ID pods, front desk, named hover hints", async () => {
    const environment = env();
    const rooms = walk(environment.$("floor")).filter(node => node.className.startsWith("room"));
    const roomOf = name => rooms.find(room => room.children.some(child => child.id === "seat-" + name));
    assert.match(roomOf("picard").textContent, /Manager/);
    assert.match(roomOf("troi").textContent, /Counselor/);
    assert.equal(roomOf("laforge"), roomOf("worf"));
    assert.equal(roomOf("crusher"), roomOf("data"));
    assert.equal(roomOf("guinan"), roomOf("barclay"));
    assert.match(roomOf("computer").textContent, /Front desk/);
    const hints = rooms.map(room => room.children.find(child => child.className === "room-label")).filter(Boolean);
    for (const label of hints) assert.ok(label.attributes.title, "hover hint on " + label.textContent);
    assert.match(hints.find(label => label.textContent === "Build & Review").attributes.title, /Laforge.*Worf/);
    assert.match(hints.find(label => label.textContent === "Analysis & Code Reading").attributes.title, /Crusher.*Data/);
    assert.match(hints.find(label => label.textContent === "Research & Docs").attributes.title, /Guinan.*Barclay/);
    assert.match(hints.find(label => label.textContent === "Front desk").attributes.title, /Computer/);
  });

  await check("stats cards: titles, session/delegation counts, one disclosure, and all credit modes", async () => {
    const environment = env();
    environment.feed("office", synthetic());
    const labels = () => walk(environment.$("stats")).filter(node => node.className === "lbl").map(node => node.textContent);
    assert.deepEqual(labels(), ["Sessions open now", "Sessions this week", "Sessions this month", "All sessions", "AI credits this month"]);
    let numbers = walk(environment.$("stats")).filter(node => node.className === "num").map(node => node.textContent);
    assert.deepEqual(numbers.slice(0, 4), ["1","1","1","1"]);
    const sublines = walk(environment.$("stats")).filter(node => node.className === "sub").map(node => node.textContent);
    assert.equal(sublines[0], "1 delegation running");
    assert.equal(sublines[1], "2 delegations");
    assert.equal(walk(environment.$("stats")).filter(node => node.className === "caret").length, 0);
    const plan = synthetic(); plan.revision = "plan"; plan.snapshot.credits.monthEstAic = 48000;
    environment.feed("office", plan);
    numbers = walk(environment.$("stats")).filter(node => node.className === "num").map(node => node.textContent);
    assert.equal(numbers[4], "48K/1M (5%)");
    const noPlan = synthetic({credits: "no-plan"}); noPlan.revision = "no-plan"; noPlan.snapshot.credits.monthEstAic = 48000;
    environment.feed("office", noPlan);
    assert.equal(walk(environment.$("stats")).filter(node => node.className === "num")[4].textContent, "≈48K AIC");
    const raw = synthetic({credits: "uncalibrated"}); raw.revision = "raw"; raw.snapshot.credits.monthLogUnits = 1234;
    environment.feed("office", raw);
    assert.equal(labels()[4], "Session-log usage this month");
    assert.equal(walk(environment.$("stats")).filter(node => node.className === "num")[4].textContent, "1.2K log units");
    const creditsFold = await environment.openFold("stat:credits");
    assert.match(creditsFold.textContent, /office\.py calibrate --used <N from \/usage>/);
  });

  await check("drawer: seat to stats, Now, weekly day, task detail, and close focus", async () => {
    const environment = env();
    environment.feed("office", synthetic());
    await environment.$("seat-laforge").click();
    assert.equal(environment.$("drawer").open, true);
    const text = environment.$("drawer-content").textContent;
    assert.match(text, /Laforge/); assert.match(text, /Builder/);
    assert.match(text, /Now \(1\)/); assert.match(text, /This week \(1\)/);
    assert.ok(environment.byKey("drawer:laforge:day:" + localDay(3600e3)), "weekly drawer groups tasks by day");
    const task = await environment.openFold("task:aaaaaaaa-1111-2222-3333-444444444444:d1");
    assert.match(task.textContent, /Issued byPicard/);
    assert.match(task.textContent, /1,200/);
    assert.ok(task.textContent.includes('<img src=x onerror="alert(1)"> Build thing'));
    await environment.$("drawer-close").click();
    assert.equal(environment.doc.activeElement.id, "seat-laforge");
  });

  function resetFixture({calibrated = true} = {}) {
    const data = synthetic({credits: calibrated ? "no-plan" : "uncalibrated"});
    const base = data.snapshot.sessions[0];
    const make = (id, agent, title, started, parent = null) => ({
      ...structuredClone(base.delegations[0]), id, agent, title, parent,
      issuer: parent ? "laforge" : "picard", task: {name: id, description: "Task " + id, prompt: "p"},
      started, lastActivity: started, completed: started, status: "completed",
    });
    const recent = {
      ...structuredClone(base), id: "aaaaaaaa-1111-2222-3333-444444444444", name: "main_monitor",
      created: "2026-09-30T15:00:00Z", updated: "2026-10-01T03:00:00Z",
      days: [
        {date: "2026-09-30", messages: 2, delegations: ["d2"]},
        {date: "2026-10-01", messages: 1, delegations: ["d1","d3"]},
      ],
      delegations: [
        make("d1", "laforge", "Builder", "2026-10-01T02:00:00Z"),
        make("d2", "troi", "Counselor", "2026-09-30T20:00:00Z"),
        make("d3", "worf", "Reviewer", "2026-10-01T02:30:00Z", "d1"),
      ],
    };
    const older = {
      ...structuredClone(base), id: "bbbbbbbb-1111-2222-3333-444444444444", name: "older", status: "closed",
      created: "2026-09-10T17:00:00Z", updated: "2026-09-10T19:00:00Z",
      days: [{date: "2026-09-10", messages: 2, delegations: ["d4"]}],
      delegations: [make("d4", "guinan", "Researcher", "2026-09-10T18:00:00Z")],
    };
    recent.usage.byDay = [
      {month: "2026-09", date: "2026-09-30", logUnits: 1000},
      {month: "2026-10", date: "2026-09-30", logUnits: 4000},
    ];
    older.usage.byDay = [{month: "2026-09", date: "2026-09-10", logUnits: 500}];
    data.snapshot.sessions = [recent, older];
    if (calibrated) data.snapshot.credits.factor = 1;
    data.snapshot.periods = {
      weekStart: "2026-09-28T07:00:00.000Z",
      monthStart: "2026-10-01T00:00:00.000Z",
      creditsMonthStart: "2026-10-01T00:00:00.000Z",
    };
    return data;
  }

  await check("tasks: week, month, all, UTC reset split, counts, and calibrated-only AIC", async () => {
    const environment = env();
    environment.feed("office", resetFixture());
    const septemberDay = environment.byKey("tasks:week:d:2026-09-30");
    assert.ok(septemberDay);
    assert.ok(environment.byKey("tasks:week:d:2026-10-01")?.open);
    assert.match(head(septemberDay), /1 session · 1 delegation · ≈5K AIC/);
    assert.ok(environment.byKey("tasks:week:d:2026-10-01"));
    assert.ok(!environment.byKey("tasks:week:d:2026-09-10"));
    const row = await environment.openFold("tasks:week:d:2026-10-01:s:aaaaaaaa-1111-2222-3333-444444444444");
    assert.match(head(row), /Me→.*Picard→.*Laforge/);
    assert.match(head(row), /1 deeper/);
    await clickPeriod(environment, "month");
    const week = environment.byKey("tasks:month:w:2026-09-28");
    assert.ok(week?.open);
    assert.match(head(week), /Sep 28 – Oct 4.*1 session · 1 delegation · ≈4K AIC/);
    const boundary = environment.byKey("tasks:month:w:2026-09-28:d:2026-09-30");
    assert.match(head(boundary), /after reset/);
    assert.match(boundary.children[0].children.find(child => child.className === "period-title").attributes.title, /October 2026/);
    await clickPeriod(environment, "all");
    const october = environment.byKey("tasks:all:m:2026-10");
    const september = environment.byKey("tasks:all:m:2026-09");
    assert.ok(october.open && !september.open);
    assert.match(head(october), /October 2026.*≈4K AIC/);
    assert.match(head(september), /September 2026.*≈1.5K AIC/);

    const rawEnvironment = env();
    rawEnvironment.feed("office", resetFixture({calibrated: false}));
    assert.doesNotMatch(rawEnvironment.$("chains").textContent, /AIC/);
    await clickPeriod(rawEnvironment, "month");
    assert.doesNotMatch(rawEnvironment.$("chains").textContent, /AIC/);
  });

  await check("folds retain both user-opened and user-closed state across refresh", async () => {
    const environment = env();
    environment.feed("office", resetFixture());
    const rowKey = "tasks:week:d:2026-10-01:s:aaaaaaaa-1111-2222-3333-444444444444";
    await environment.openFold(rowKey);
    const next = resetFixture(); next.revision = "refresh-open";
    environment.feed("office", next);
    assert.ok(environment.byKey(rowKey).open);
    const day = environment.byKey("tasks:week:d:2026-09-30");
    day.open = false; await day.dispatch("toggle");
    const again = resetFixture(); again.revision = "refresh-closed";
    environment.feed("office", again);
    assert.equal(environment.byKey("tasks:week:d:2026-09-30").open, false);
  });

  await check("Show chain in Tasks opens the correct period, day, and session row", async () => {
    const environment = env();
    environment.feed("office", resetFixture());
    vm.runInContext('goToSession("bbbbbbbb-1111-2222-3333-444444444444")', environment.context);
    assert.equal(environment.$("view-tasks").hidden, false);
    const pressed = environment.find(environment.$("view-tasks"), node => node.getAttribute?.("aria-pressed") === "true");
    assert.equal(pressed.getAttribute("data-period"), "all");
    assert.ok(environment.byKey(
      "tasks:all:m:2026-09:w:2026-09-07:d:2026-09-10:s:bbbbbbbb-1111-2222-3333-444444444444"
    )?.open);
  });

  await check("session rename is project-scoped, persistent, drawer-visible, and resettable", async () => {
    const saved = new Map();
    const environment = env({saved});
    environment.feed("office", resetFixture());
    const key = "tasks:week:d:2026-10-01:s:aaaaaaaa-1111-2222-3333-444444444444";
    const parts = () => environment.byKey(key).children[0].children;
    assert.ok(parts()[0].className.startsWith("dot"));
    assert.equal(parts()[1].className, "row-title");
    assert.equal(parts()[1].textContent, "main_monitor");
    await environment.$("rename:" + key).click();
    const input = environment.find(environment.byKey(key), node => node.className === "rename-input");
    input.value = "  Experiment monitor  ";
    await input.dispatch("keydown", {key: "Enter"});
    const storageKey = "copilot-office:my-project:title:v1:aaaaaaaa-1111-2222-3333-444444444444";
    assert.equal(saved.get(storageKey), "Experiment monitor");
    assert.equal(parts()[1].textContent, "Experiment monitor");
    assert.equal(environment.doc.activeElement.id, "rename:" + key);

    const reloaded = env({saved});
    reloaded.feed("office", resetFixture());
    assert.equal(reloaded.byKey(key).children[0].children[1].textContent, "Experiment monitor");
    await reloaded.$("seat-picard").click();
    assert.match(reloaded.$("drawer-content").textContent, /Experiment monitor/);
    await reloaded.$("drawer-close").click();
    await reloaded.$("rename:" + key).click();
    await reloaded.find(reloaded.byKey(key), node => node.tagName === "button" && node.textContent === "Use original").click();
    assert.equal(saved.has(storageKey), false);
    await reloaded.$("rename:" + key).click();
    const original = reloaded.find(reloaded.byKey(key), node => node.className === "rename-input");
    original.value = "main_monitor";
    await original.dispatch("keydown", {key: "Enter"});
    assert.equal(saved.has(storageKey), false);
  });

  await check("real temporary collector output renders in every period and every session is reachable under All", async () => {
    const environment = env();
    environment.feed("office", realOffice);
    for (const period of ["week","month","all"]) {
      await clickPeriod(environment, period);
      for (let depth = 0; depth < 5; depth++) {
        for (const details of walk(environment.$("chains")).filter(node =>
          node.tagName === "details" && !node.open && /^tasks:/.test(node.getAttribute("data-key") || ""))) {
          await environment.openFold(details.getAttribute("data-key"));
        }
      }
      const periodKeys = walk(environment.$("chains")).map(node => node.getAttribute?.("data-key")).filter(Boolean);
      assert.ok(periodKeys.some(key => key.endsWith(":s:fixture-session")), "collector session renders in " + period);
    }
    const keys = walk(environment.$("chains")).map(node => node.getAttribute?.("data-key")).filter(Boolean);
    for (const session of realOffice.snapshot.sessions.filter(item => item.days.length || item.created || item.updated)) {
      assert.ok(keys.some(key => key.endsWith(":s:" + session.id)), "session row for " + session.id);
    }
    assert.equal(walk(environment.$("floor")).filter(node => node.className === "seat").length, 9);
  });

  await check("avatars upload, persist by project and employee, hide initials, and reset", async () => {
    const saved = new Map();
    const environment = env({saved});
    environment.feed("office", synthetic());
    await environment.$("seat-picard").click();
    const input = environment.find(environment.$("drawer-content"), node => node.tagName === "input");
    input.files = [png()];
    await input.dispatch("change"); await settle(); await settle();
    const storageKey = "copilot-office:my-project:avatar:v1:lead";
    assert.ok(saved.get(storageKey));
    const avatar = environment.$("seat-picard").querySelector(".avatar");
    assert.ok(avatar.querySelector("img"));
    assert.equal(avatar.querySelector("span").hidden, true);
    assert.match(environment.$("avatar-status-lead").textContent, /Saved in this browser/);
    const reset = environment.find(environment.$("drawer-content"),
      node => node.tagName === "button" && node.textContent === "Reset");
    await reset.click();
    assert.equal(saved.has(storageKey), false);
    assert.equal(environment.$("seat-picard").querySelector(".avatar").querySelector("img"), null);
  });

  await check("avatar wrong-type and full-storage errors are visible", async () => {
    const environment = env({failSet: true});
    await environment.$("seat-troi").click();
    const input = () => environment.find(environment.$("drawer-content"), node => node.tagName === "input");
    input().files = [{...png(), type: "text/html"}];
    await input().dispatch("change");
    assert.match(environment.$("avatar-status-guardian").textContent, /file types/);
    input().files = [png()];
    await input().dispatch("change"); await settle(); await settle();
    assert.match(environment.$("avatar-status-guardian").textContent, /Preview only — not saved/);
  });

  await check("live feed failure marks Stale, preserves data, and pause stops polling", async () => {
    const environment = env({hosted: true});
    await settle();
    environment.control.responses.push(
      {ok: true, status: 200, json: async () => recordsFeed},
      {ok: true, status: 200, json: async () => synthetic()},
    );
    await vm.runInContext('Promise.all([pollFeed("records"), pollFeed("office")])', environment.context);
    assert.equal(environment.$("live-text").textContent, "Live");
    environment.control.responses.push({ok: false, status: 503, json: async () => ({error: "index broke"})});
    await vm.runInContext('pollFeed("office")', environment.context);
    assert.equal(environment.$("live-text").textContent, "Stale");
    assert.match(environment.$("live-errors").textContent, /503: index broke/);
    assert.equal(walk(environment.$("stats")).find(node => node.className === "num").textContent, "1");
    await environment.$("live-toggle").click();
    assert.equal(environment.$("live-text").textContent, "Paused");
    const before = environment.control.requests.length;
    await vm.runInContext('pollFeed("office")', environment.context);
    assert.equal(environment.control.requests.length, before);
  });

  await check("server-side stale flag keeps the last office data and reports Stale", async () => {
    const environment = env({hosted: true});
    await settle();
    const stale = {...synthetic(), stale: true, error: "OSError: fixture failure"};
    environment.control.responses.push(
      {ok: true, status: 200, json: async () => recordsFeed},
      {ok: true, status: 200, json: async () => stale},
    );
    await vm.runInContext('Promise.all([pollFeed("records"), pollFeed("office")])', environment.context);
    assert.equal(environment.$("live-text").textContent, "Stale");
    assert.match(environment.$("live-errors").textContent, /fixture failure/);
    assert.match(environment.$("live-office").textContent, /last good index/);
    assert.equal(walk(environment.$("stats")).find(node => node.className === "num").textContent, "1");
  });

  await check("Runs tab: running progress, weekly history, outcomes, and empty registry message", async () => {
    const environment = env();
    const data = structuredClone(recordsFeed);
    const running = structuredClone(data.snapshot.runs[0]);
    Object.assign(running, {
      id: "live-dir", name: "live-run", registryStatus: "No outcome recorded in registry.json",
      superseded: null, started: "2026-10-05 12:00 UTC",
    });
    running.health = {
      ...running.health, stale: false, error: null, modifiedAt: new Date().toISOString(),
      block: "15:36 live-run [x] up 3h (capture -) state=starting\n" +
        "  eval_before=n/a updates=v1@12:29 v2@13:32 v6@15:17\n" +
        "  traj started=3424 ended=3012 failed=2 groups={}",
    };
    data.revision = "runs";
    data.snapshot.runs.unshift(running);
    environment.feed("records", data);
    await environment.$("tab-runs").click();
    const card = environment.$("run-live-dir");
    assert.match(card.textContent, /live-run/);
    assert.match(card.textContent, /v6 · 3,012 traj · 2 failed/);
    const weeks = environment.$("runs-history").children.filter(child =>
      /^runs:w:/.test(child.getAttribute?.("data-key") || ""));
    assert.ok(weeks[0].open && weeks.slice(1).every(week => !week.open));
    for (const week of weeks) await environment.openFold(week.getAttribute("data-key"));
    const outcome = run => environment.byKey("run:" + run.id).children[0].textContent;
    assert.match(outcome(data.snapshot.runs.find(run => run.name === "done-run")), /Done/);
    assert.match(outcome(data.snapshot.runs.find(run => run.name === "failed-run")), /Failed/);
    assert.match(outcome(data.snapshot.runs.find(run => run.name === "stopped-run")), /Stopped/);

    const empty = env({snapshot: null});
    empty.feed("records", emptyRecords);
    assert.match(empty.$("runs-live").textContent, /No runs yet/);
    assert.match(empty.$("runs-live").textContent, /run-logs\/README\.md/);
  });

  await check("staff.json role titles appear in Tasks, drawer, and seat labels", async () => {
    const environment = env();
    environment.feed("office", resetFixture());
    await environment.openFold("tasks:week:d:2026-09-30");
    await environment.openFold("tasks:week:d:2026-09-30:s:aaaaaaaa-1111-2222-3333-444444444444");
    assert.match(head(environment.byKey(
      "tasks:week:d:2026-09-30:s:aaaaaaaa-1111-2222-3333-444444444444:troi"
    )), /Troi · Counselor/);
    await environment.$("seat-picard").click();
    assert.match(environment.$("drawer-content").textContent, /Manager/);
    assert.match(environment.$("seat-data").attributes["aria-label"], /Code Reader/);
  });

  console.log(`\n${count} client check groups passed.`);
  console.log("NOT VERIFIED: real browser layout/CSS, hover rendering, native details/dialog behavior, image decoding, localStorage.");
} finally {
  fs.rmSync(temporary, {recursive: true, force: true});
}

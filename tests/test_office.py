import ast
import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("office", ROOT / "office.py")
office = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(office)


class OfficeTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="copilot-office-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "project"
        self.user_dir = self.root / "user" / ".copilot"

    def init(self, theme="pokemon"):
        return office.init_home(
            self.home,
            "my-project",
            "My Project",
            theme,
            "UTC",
        )

    def load(self, relative):
        return json.loads((self.home / relative).read_text(encoding="utf-8"))


class InitTests(OfficeTestCase):
    def test_init_contract_for_all_themes(self):
        expected_names = {
            theme_id: office.load_theme(theme_id)["names"]
            for theme_id in office.THEME_IDS
        }
        for theme_id in office.THEME_IDS:
            with self.subTest(theme=theme_id):
                home = self.root / theme_id
                names = office.init_home(
                    home, "my-project", "My Project", theme_id, "UTC"
                )
                project = json.loads(
                    (home / ".agent-office/project.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    project,
                    {
                        "schema_version": 1,
                        "project_id": "my-project",
                        "display_name": "My Project",
                        "status": "active",
                        "records": {
                            "state": "PROJECT_STATE.md",
                            "decisions": "DECISIONS.md",
                            "staff": "staff.json",
                            "tasks": "tasks.json",
                        },
                        "repository": {
                            "status": "not_configured",
                            "readiness_scope": "",
                        },
                    },
                )
                staff = json.loads(
                    (home / ".agent-office/staff.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    [item["employee_id"] for item in staff["employees"]],
                    list(office.EMPLOYEE_IDS),
                )
                self.assertEqual(staff["theme"], theme_id)
                for employee in staff["employees"]:
                    expected = expected_names[theme_id][employee["employee_id"]]
                    self.assertEqual(employee["name"], expected)
                    self.assertEqual(employee["profile"], expected)
                    self.assertEqual(
                        set(employee),
                        {
                            "employee_id",
                            "role_profile",
                            "profile",
                            "name",
                            "title",
                            "aliases",
                            "sessions",
                            "assignments",
                        },
                    )
                self.assertEqual(
                    json.loads(
                        (home / ".agent-office/tasks.json").read_text(encoding="utf-8")
                    ),
                    {
                        "schema_version": 1,
                        "project_id": "my-project",
                        "tasks": [],
                    },
                )
                dashboard = json.loads(
                    (home / ".agent-office/dashboard.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    dashboard,
                    {
                        "schema_version": 1,
                        "timezone": "UTC",
                        "plan_aic": None,
                        "aic_per_log_unit": None,
                        "calibration": None,
                        "frontdesk": {
                            "name": expected_names[theme_id]["frontdesk"],
                            "title": "Front Desk",
                        },
                    },
                )
                self.assertEqual(
                    set(names),
                    {expected_names[theme_id][item] for item in office.EMPLOYEE_IDS},
                )
                for name in names:
                    profile = home / ".github" / "agents" / f"{name}.agent.md"
                    self.assertTrue(profile.is_file())
                    self.assertIn(
                        str((home / ".agent-office/project.json").resolve()),
                        profile.read_text(encoding="utf-8"),
                    )
                for path in (
                    home / ".agent-office/PROJECT_STATE.md",
                    home / ".agent-office/DECISIONS.md",
                    home / ".agent-office/STARTING_BRIEF.md",
                    home / ".agent-office/generated-profiles.json",
                    home / "run-logs/README.md",
                    home / "run-logs/registry.json",
                    home / "reports/REPORT.md",
                    home / "reports/.gitkeep",
                    home / "meeting-notes/.gitkeep",
                    home / ".gitignore",
                ):
                    self.assertTrue(path.is_file(), path)
                self.assertEqual(
                    json.loads((home / "run-logs/registry.json").read_text()), {}
                )
                self.assertIn(
                    ".agent-office/.dashboard-cache/",
                    (home / ".gitignore").read_text().splitlines(),
                )
                replacements = {
                    "PROJECT_ID": "my-project",
                    "DISPLAY_NAME": "My Project",
                    "MANAGER_NAME": expected_names[theme_id]["lead"],
                }
                self.assertEqual(
                    (home / ".agent-office/PROJECT_STATE.md").read_text(),
                    office.render_template("PROJECT_STATE.md", replacements),
                )
                self.assertEqual(
                    (home / "reports/REPORT.md").read_text(),
                    office.render_template("REPORT.md", replacements),
                )

    def test_existing_office_points_to_render_and_force_keeps_records(self):
        self.init()
        records = self.home / ".agent-office"
        tasks_path = records / "tasks.json"
        state_path = records / "PROJECT_STATE.md"
        decisions_path = records / "DECISIONS.md"
        staff_path = records / "staff.json"
        dashboard_path = records / "dashboard.json"
        tasks_path.write_text('{"user":"tasks"}\n')
        state_path.write_text("user state\n")
        decisions_path.write_text("user decisions\n")
        staff = json.loads(staff_path.read_text())
        lead = next(item for item in staff["employees"] if item["employee_id"] == "lead")
        lead["name"] = lead["profile"] = "quagsire"
        staff_path.write_text(json.dumps(staff))
        dashboard_path.write_text('{"user":"dashboard"}\n')

        with self.assertRaisesRegex(office.OfficeError, "render --home"):
            self.init()
        names = office.init_home(
            self.home, "ignored-project", "Ignored Name", "startrek", "UTC", force=True
        )
        self.assertEqual(len(names), 8)
        self.assertIn("quagsire", names)
        self.assertEqual(tasks_path.read_text(), '{"user":"tasks"}\n')
        self.assertEqual(state_path.read_text(), "user state\n")
        self.assertEqual(decisions_path.read_text(), "user decisions\n")
        self.assertEqual(json.loads(staff_path.read_text()), staff)
        self.assertEqual(dashboard_path.read_text(), '{"user":"dashboard"}\n')
        self.assertTrue((self.home / ".github/agents/quagsire.agent.md").is_file())
        self.assertFalse((self.home / ".github/agents/bidoof.agent.md").exists())

    def test_force_recreates_dashboard_only_when_missing(self):
        self.init()
        dashboard = self.home / ".agent-office/dashboard.json"
        dashboard.unlink()
        office.init_home(
            self.home, "ignored-project", "Ignored Name", "startrek", "UTC", force=True
        )
        recreated = json.loads(dashboard.read_text())
        self.assertEqual(recreated["frontdesk"]["name"], "rotom")
        self.assertEqual(recreated["timezone"], "UTC")

    def test_theme_validation(self):
        valid = office.load_theme("pokemon")
        office.validate_theme(valid)
        cases = []
        malformed = copy.deepcopy(valid)
        malformed["names"]["lead"] = "Bad"
        cases.append(malformed)
        duplicate = copy.deepcopy(valid)
        duplicate["names"]["lead"] = duplicate["names"]["guardian"]
        cases.append(duplicate)
        initial = copy.deepcopy(valid)
        initial["names"]["lead"] = "azelf"
        cases.append(initial)
        missing = copy.deepcopy(valid)
        del missing["names"]["frontdesk"]
        cases.append(missing)
        for theme in cases:
            with self.subTest(theme=theme):
                with self.assertRaises(office.OfficeError):
                    office.validate_theme(theme)

    def test_invalid_timezone_and_project_id(self):
        with self.assertRaisesRegex(office.OfficeError, "timezone"):
            office.init_home(
                self.home, "my-project", "My Project", "pokemon", "Not/A_Zone"
            )
        with self.assertRaisesRegex(office.OfficeError, "project ID"):
            office.init_home(
                self.home, "Not Valid", "My Project", "pokemon", "UTC"
            )


class RenderTests(OfficeTestCase):
    def test_render_is_idempotent_and_manifest_matches(self):
        names = self.init("startrek")
        before = {
            name: (self.home / f".github/agents/{name}.agent.md").read_bytes()
            for name in names
        }
        self.assertEqual(office.render_home(self.home), names)
        self.assertEqual(office.render_home(self.home, check=True), names)
        manifest = self.load(".agent-office/generated-profiles.json")
        self.assertEqual(set(manifest["outputs"]), set(names))
        for name, content in before.items():
            path = self.home / f".github/agents/{name}.agent.md"
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(
                manifest["outputs"][name]["sha256"],
                office.sha256_text(content.decode()),
            )
            header, body = office.parse_frontmatter(path.read_text())
            self.assertEqual(header["name"], name)
            self.assertFalse(header["disable-model-invocation"])
            self.assertIn("Employee binding", body)
            self.assertIn("office.py render", body)

    def test_rename_removes_only_unchanged_stale_profile(self):
        self.init()
        staff_path = self.home / ".agent-office/staff.json"
        staff = json.loads(staff_path.read_text())
        lead = next(item for item in staff["employees"] if item["employee_id"] == "lead")
        lead["name"] = lead["profile"] = "quagsire"
        staff_path.write_text(json.dumps(staff), encoding="utf-8")
        office.render_home(self.home)
        self.assertFalse((self.home / ".github/agents/bidoof.agent.md").exists())
        self.assertTrue((self.home / ".github/agents/quagsire.agent.md").exists())

    def test_render_refuses_manual_edit(self):
        self.init()
        path = self.home / ".github/agents/bidoof.agent.md"
        path.write_text(path.read_text() + "\nmanual edit\n", encoding="utf-8")
        with self.assertRaisesRegex(office.OfficeError, "edited"):
            office.render_home(self.home)

    def test_flat_frontmatter_reader_has_no_yaml_dependency(self):
        tree = ast.parse((ROOT / "office.py").read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in (
                node.names
                if isinstance(node, ast.Import)
                else [ast.alias(name=node.module or "")]
            )
        }
        self.assertNotIn("yaml", imported)
        header, body = office.parse_frontmatter(
            '---\nname: sample\ntools: ["read", "grep"]\n'
            'user-invocable: true\ndescription: "Has: punctuation"\n---\nBody\n'
        )
        self.assertEqual(header["tools"], ["read", "grep"])
        self.assertTrue(header["user-invocable"])
        self.assertEqual(header["description"], "Has: punctuation")
        self.assertEqual(body, "Body\n")


class InstallTests(OfficeTestCase):
    def test_install_copy_skip_refuse_symlinks_and_frontdesk(self):
        self.init("animalcrossing")
        messages, command = office.install(self.home, self.user_dir)
        self.assertIn("copilot --agent nook", command)
        self.assertTrue(any(message.startswith("copied:") for message in messages))
        agents = self.user_dir / "agents"
        for source in (ROOT / "roles").glob("office-*.agent.md"):
            self.assertEqual(
                (agents / source.name).read_text(encoding="utf-8"),
                source.read_text(encoding="utf-8"),
            )
            self.assertFalse((agents / source.name).is_symlink())
        staff = self.load(".agent-office/staff.json")
        for employee in staff["employees"]:
            installed = agents / f"{employee['name']}.agent.md"
            self.assertTrue(installed.is_symlink())
            self.assertEqual(
                installed.resolve(),
                (self.home / ".github/agents" / installed.name).resolve(),
            )
        frontdesk = agents / "isabelle.agent.md"
        self.assertTrue(frontdesk.is_file())
        self.assertFalse(frontdesk.is_symlink())
        content = frontdesk.read_text(encoding="utf-8")
        self.assertNotIn("{{FRONTDESK_NAME}}", content)
        self.assertNotIn("{{SESSIONS_PATH}}", content)
        self.assertIn(str((ROOT / "frontdesk/sessions.py").resolve()), content)
        self.assertIn("name: isabelle", content)
        installed_manifest = json.loads(
            (agents / office.INSTALL_MANIFEST_NAME).read_text()
        )
        self.assertEqual(
            installed_manifest["office-builder.agent.md"],
            office.sha256_text(
                (ROOT / "roles/office-builder.agent.md").read_text(encoding="utf-8")
            ),
        )
        self.assertEqual(
            installed_manifest["isabelle.agent.md"],
            office.sha256_text(content),
        )

        messages, _ = office.install(self.home, self.user_dir)
        self.assertTrue(any(message.startswith("skip identical:") for message in messages))
        self.assertTrue(
            any(message.startswith("skip identical symlink:") for message in messages)
        )

        role = agents / "office-builder.agent.md"
        role.write_text("different\n", encoding="utf-8")
        with self.assertRaisesRegex(office.OfficeError, "refusing"):
            office.install(self.home, self.user_dir)
        office.install(self.home, self.user_dir, force=True)
        self.assertEqual(
            role.read_text(encoding="utf-8"),
            (ROOT / "roles/office-builder.agent.md").read_text(encoding="utf-8"),
        )

    def test_reinstall_updates_owned_role_but_refuses_user_edit(self):
        self.init()
        office.install(self.home, self.user_dir)
        copied_roles = self.root / "roles"
        shutil.copytree(ROOT / "roles", copied_roles)
        source = copied_roles / "office-builder.agent.md"
        source.write_text(source.read_text() + "\n<!-- role revision -->\n")
        installed = self.user_dir / "agents/office-builder.agent.md"
        with patch.object(office, "ROLES_DIR", copied_roles):
            office.render_home(self.home)
            messages, _ = office.install(self.home, self.user_dir)
            self.assertIn(source.read_text(), installed.read_text())
            self.assertTrue(
                any(
                    message.startswith("updated:")
                    and "office-builder.agent.md" in message
                    for message in messages
                )
            )
            installed.write_text(installed.read_text() + "\nuser edit\n")
            with self.assertRaisesRegex(office.OfficeError, "user-edited"):
                office.install(self.home, self.user_dir)
            office.install(self.home, self.user_dir, force=True)
            self.assertEqual(installed.read_text(), source.read_text())

    def test_install_preflight_prevents_partial_writes(self):
        self.init()
        agents = self.user_dir / "agents"
        agents.mkdir(parents=True)
        conflict = agents / "bidoof.agent.md"
        conflict.write_text("foreign\n")
        with self.assertRaisesRegex(office.OfficeError, "preflight"):
            office.install(self.home, self.user_dir)
        self.assertEqual(conflict.read_text(), "foreign\n")
        self.assertFalse((agents / "office-builder.agent.md").exists())
        self.assertFalse((agents / "rotom.agent.md").exists())
        self.assertFalse((agents / office.INSTALL_MANIFEST_NAME).exists())

    def test_stale_office_symlink_requires_force_and_is_reported(self):
        first_home = self.home
        office.init_home(first_home, "first", "First", "pokemon", "UTC")
        office.install(first_home, self.user_dir)
        second_home = self.root / "second"
        office.init_home(second_home, "second", "Second", "pokemon", "UTC")
        with self.assertRaisesRegex(office.OfficeError, "stale office symlink"):
            office.install(second_home, self.user_dir)
        messages, _ = office.install(second_home, self.user_dir, force=True)
        self.assertTrue(
            any(message.startswith("relinked stale office symlink:") for message in messages)
        )
        self.assertEqual(
            (self.user_dir / "agents/bidoof.agent.md").resolve(),
            (second_home / ".github/agents/bidoof.agent.md").resolve(),
        )

    def test_install_refuses_different_named_agent(self):
        self.init()
        target = self.user_dir / "agents/bidoof.agent.md"
        target.parent.mkdir(parents=True)
        target.write_text("unrelated\n", encoding="utf-8")
        with self.assertRaisesRegex(office.OfficeError, "different installed"):
            office.install(self.home, self.user_dir)
        self.assertEqual(target.read_text(), "unrelated\n")


class DoctorAndCliTests(OfficeTestCase):
    def test_doctor_on_fresh_installed_setup(self):
        self.init()
        office.install(self.home, self.user_dir)
        with patch.object(office.shutil, "which", return_value="/usr/bin/copilot"):
            ok, messages = office.doctor(self.home, self.user_dir)
        self.assertTrue(ok, "\n".join(messages))
        output = "\n".join(messages)
        self.assertIn("[OK] Python", output)
        self.assertIn("[OK] copilot found", output)
        self.assertIn("[OK] office layout valid", output)
        self.assertIn("[OK] agents installed", output)

    def test_doctor_actionable_when_not_installed(self):
        self.init()
        with patch.object(office.shutil, "which", return_value=None):
            ok, messages = office.doctor(self.home, self.user_dir)
        self.assertFalse(ok)
        output = "\n".join(messages)
        self.assertIn("install GitHub Copilot CLI", output)
        self.assertIn("Fix:", output)
        self.assertIn("office.py install", output)

    def test_cli_help_and_init_from_another_cwd(self):
        help_result = subprocess.run(
            [sys.executable, str(ROOT / "office.py"), "--help"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        for command in ("init", "install", "render", "doctor", "calibrate", "dashboard"):
            self.assertIn(command, help_result.stdout)
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "office.py"),
                "init",
                "--home",
                str(self.home),
                "--project-id",
                "demo",
                "--name",
                "Demo",
                "--theme",
                "startrek",
                "--timezone",
                "UTC",
            ],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / ".github/agents/picard.agent.md").is_file())

    def test_dashboard_helpers_use_current_python(self):
        completed = subprocess.CompletedProcess([], 0)
        with patch.object(office.subprocess, "run", return_value=completed) as run:
            self.assertEqual(office.run_dashboard_script("server.py", ["--port", "1"]), 0)
        self.assertEqual(run.call_args.args[0][0], sys.executable)


class FrontdeskTests(OfficeTestCase):
    def test_frontdesk_uses_overridable_temp_paths(self):
        sessions = self.user_dir / "session-state"
        sessions.mkdir(parents=True)
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "frontdesk/sessions.py"),
                "--user-dir",
                str(self.user_dir),
                "--frontdesk-name",
                "welcome",
                "list",
            ],
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "HOME": str(self.root / "unused-home")},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.user_dir / "office/frontdesk/index.json").is_file())
        self.assertFalse((self.root / "unused-home/.copilot").exists())

    def test_empty_session_key_is_rejected_without_state_write(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "frontdesk/sessions.py"),
                "--user-dir",
                str(self.user_dir),
                "show",
                "",
            ],
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "HOME": str(self.root / "unused-home")},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("session key must not be empty", result.stderr)
        self.assertFalse((self.user_dir / "office/frontdesk").exists())


if __name__ == "__main__":
    unittest.main()

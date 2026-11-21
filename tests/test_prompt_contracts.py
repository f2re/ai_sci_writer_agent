"""Check executable routing and skill packaging, rather than prose word-count quotas."""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from sci_writer.contracts import ROOT
from sci_writer.engine import init_run, next_task, workflow


class PromptContracts(unittest.TestCase):
    def test_workflow_routes_to_existing_scoped_modules(self):
        config = workflow()
        self.assertEqual(config["contract_version"], "2.1")
        self.assertTrue((ROOT / config["coordinator"]).is_file())
        stages = config["stages"]
        self.assertEqual(len(stages), 7)
        self.assertEqual(stages[4]["id"], "05_quality")
        for stage in stages:
            self.assertTrue(stage["agents"], stage["id"])
            self.assertEqual(len(stage["agents"]), len(set(stage["agents"])))
            for relative in stage["agents"]:
                path = (ROOT / relative).resolve()
                self.assertTrue(path.is_relative_to(ROOT.resolve()))
                self.assertTrue(path.is_file(), relative)
                self.assertNotIn("research-archive", path.parts)
            self.assertTrue(stage["outputs"], stage["id"])
        self.assertEqual(stages[3]["agents"], ["skills/manuscript-writer/SKILL.md"])
        self.assertEqual(stages[4]["agents"], ["skills/editor-quality/SKILL.md"])
        peer = stages[5]["agents"]
        final = stages[6]["agents"]
        self.assertIn("skills/independent-review/references/domain.md", peer)
        self.assertIn("skills/independent-review/references/methods.md", peer)
        self.assertNotIn("skills/independent-review/references/chief.md", peer)
        self.assertIn("skills/independent-review/references/chief.md", final)
        self.assertNotIn("skills/manuscript-writer/SKILL.md", peer + final)

    def test_skills_have_valid_names_descriptions_and_resolved_links(self):
        skills = sorted((ROOT / "skills").glob("*/SKILL.md"))
        self.assertEqual(len(skills), 8)
        for path in skills:
            text = path.read_text(encoding="utf-8")
            match = re.match(r"\A---\nname: ([a-z0-9]+(?:-[a-z0-9]+)*)\ndescription: (.+)\n---\n", text)
            self.assertIsNotNone(match, str(path))
            name, description = match.groups()
            self.assertEqual(name, path.parent.name)
            self.assertLessEqual(len(name), 64)
            self.assertTrue(1 <= len(description) <= 1024)
            self.assertNotIn("[TODO", text)
            self._check_links(path, text)

    def test_active_documentation_does_not_link_deleted_v01_prompts(self):
        paths = [ROOT / "README.md", ROOT / "AGENTS.md"]
        paths += list((ROOT / "docs").glob("*.md"))
        paths += list((ROOT / "skills").rglob("*.md"))
        paths += list((ROOT / ".agents/skills").rglob("*.md"))
        paths += list((ROOT / "agents").glob("*.md"))
        for path in paths:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("agents/00-coordinator.md", text, str(path))
            self._check_links(path, text)

    def test_platform_adapters_point_to_single_policy(self):
        adapters = (
            "CLAUDE.md", "GEMINI.md", ".github/copilot-instructions.md",
            ".cursor/rules/sci-writer.mdc",
        )
        for relative in adapters:
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("AGENTS.md", text)
            self.assertIn("00-orchestrator.md", text)
        cursor = (ROOT / ".cursor/rules/sci-writer.mdc").read_text(encoding="utf-8")
        self.assertRegex(cursor, r"(?m)^alwaysApply: true$")
        self.assertIn("research-archive/", (ROOT / ".cursorignore").read_text(encoding="utf-8"))

    def test_repository_skill_is_a_discoverable_thin_entrypoint(self):
        path = ROOT / ".agents/skills/sci-writer/SKILL.md"
        text = path.read_text(encoding="utf-8")
        match = re.match(r"\A---\nname: ([a-z0-9]+(?:-[a-z0-9]+)*)\ndescription: (.+)\n---\n", text)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), path.parent.name)
        self.assertTrue(1 <= len(match.group(2)) <= 1024)
        self._check_links(path, text)
        for required in ("../../../AGENTS.md", "../../../agents/00-orchestrator.md", "../../../config/workflow.json"):
            self.assertIn(required, text)
        self.assertNotIn("00-coordinator.md", text)
        self.assertNotIn("[TODO", text)
        self.assertIn("sci-writer next", text)
        self.assertNotIn("../../../research-archive", text)
        self.assertNotIn("skills/russian-school/SKILL.md", text)
        routed_paths = [relative for stage in workflow()["stages"] for relative in stage["agents"]]
        self.assertNotIn(".agents/skills/sci-writer/SKILL.md", routed_paths)
        self.assertEqual(len(list((ROOT / "skills").glob("*/SKILL.md"))), 8)

    def test_claude_excludes_only_archive_with_supported_permissions(self):
        text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^@AGENTS\.md$")
        settings = json.loads((ROOT / ".claude/settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings, {"permissions": {"deny": ["Read(/research-archive/**)"]}})
        self.assertFalse((ROOT / ".claudeignore").exists())
        ignored = (ROOT / ".cursorignore").read_text(encoding="utf-8")
        patterns = [line.strip() for line in ignored.splitlines() if line.strip() and not line.startswith("#")]
        self.assertEqual(patterns, ["research-archive/"])

    def test_next_task_loads_only_current_stage_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "article"
            init_run(run, "Проверка выборочного контекста; это не исследование")
            result = next_task(run)
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertIn("brief-venue-style", serialized)
            self.assertNotIn("russian-school/SKILL.md", serialized)
            self.assertNotIn("research-archive", serialized)
            self.assertNotIn("00-coordinator", serialized)

    def _check_links(self, path: Path, text: str):
        for target in re.findall(r"\]\(([^)\s]+)(?:\s+[^)]*)?\)", text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = target.split("#", 1)[0]
            if not target:
                continue
            resolved = (path.parent / target).resolve()
            self.assertTrue(resolved.is_relative_to(ROOT.resolve()), f"{path}: {target}")
            self.assertTrue(resolved.exists(), f"Broken link {path}: {target}")


if __name__ == "__main__":
    unittest.main()

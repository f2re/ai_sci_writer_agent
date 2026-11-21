"""Real stdio transport smoke test plus workspace-bound mutation regressions."""
import importlib.util
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from sci_writer.contracts import ContractError
from sci_writer.mcp_server import WorkflowService


class MCPWorkspace(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = WorkflowService(Path(self.temp.name) / "runs")
        self.service.init_run("fixture", "Программная проверка интерфейса")

    def tearDown(self):
        self.temp.cleanup()

    def test_workspace_escape_and_internal_state_write_are_rejected(self):
        for run_id in ("../escape", "/tmp/escape", "..", "a/b"):
            with self.assertRaises(ContractError):
                self.service.init_run(run_id, "Invalid path fixture")
        with self.assertRaises(ContractError):
            self.service.read_artifact("fixture", "../../outside.txt")
        for path in (".workflow/state.json", "release/article.md", "../brief.json", "analysis/.secret.json"):
            with self.assertRaises(ContractError):
                self.service.write_artifact("fixture", path, "{}")
        run = self.service.run_path("fixture")
        (run / "alias").symlink_to(run / ".workflow", target_is_directory=True)
        observed = self.service.read_artifact("fixture", "alias/state.json")
        with self.assertRaises(ContractError):
            self.service.write_artifact("fixture", "alias/state.json", "{}", observed["sha256"])

    def test_existing_artifacts_require_observed_hash_and_do_not_advance_state(self):
        artifact = self.service.read_artifact("fixture", "brief.json")
        content = json.loads(artifact["content"])
        content["research_questions"] = ["Как проверяется контракт артефактов?"]
        with self.assertRaises(ContractError):
            self.service.write_artifact("fixture", "brief.json", json.dumps(content))
        self.service.write_artifact("fixture", "brief.json", json.dumps(content), artifact["sha256"])
        with self.assertRaises(ContractError):
            self.service.write_artifact("fixture", "brief.json", "{}", artifact["sha256"])
        self.assertEqual(self.service.status("fixture")["stages"][0]["status"], "pending")
        task = self.service.next_task("fixture")
        self.assertIn("AGENTS.md", task["instructions"])
        self.assertFalse(any("research-archive" in path for path in task["instructions"]))


@unittest.skipUnless(importlib.util.find_spec("mcp"), "Optional MCP SDK not installed; CI installs the extra")
class MCPStdio(unittest.TestCase):
    def test_real_stdio_discovery_init_and_validation(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryFile(mode="w+t") as log:
            process = subprocess.Popen([sys.executable, "-m", "sci_writer.mcp_server", "--workspace", folder],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8", bufsize=1,
                env={**os.environ, "PYTHONUNBUFFERED": "1"})
            messages = queue.Queue()
            def receive():
                for line in process.stdout:
                    try:
                        messages.put(json.loads(line))
                    except ValueError:
                        messages.put({"invalid_protocol_line": line})
            reader = threading.Thread(target=receive, daemon=True)
            reader.start()
            def call(identity, method, params):
                process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": identity, "method": method, "params": params}) + "\n")
                process.stdin.flush()
                while True:
                    try:
                        result = messages.get(timeout=20)
                    except queue.Empty:
                        log.seek(0)
                        self.fail("MCP response timed out: " + log.read()[-3000:])
                    self.assertNotIn("invalid_protocol_line", result)
                    if result.get("id") == identity:
                        self.assertNotIn("error", result)
                        return result["result"]
            try:
                initialized = call(1, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "contract-fixture", "version": "1"}})
                self.assertIn("tools", initialized["capabilities"])
                process.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                process.stdin.flush()
                listed = call(2, "tools/list", {})
                names = {item["name"] for item in listed["tools"]}
                self.assertTrue({"init_run", "next_task", "validate_stage", "complete_stage", "run_quality", "hashes", "finalize", "read_artifact", "write_artifact"}.issubset(names))
                created = call(3, "tools/call", {"name": "init_run", "arguments": {"run_id": "protocol-fixture", "topic": "Проверка реального stdio транспорта", "execution_profile": "offline_sandbox", "review_mode": "single_session_self_audit"}})
                self.assertFalse(created.get("isError", created.get("is_error", False)))
                next_result = call(4, "tools/call", {"name": "next_task", "arguments": {"run_id": "protocol-fixture"}})
                serialized = json.dumps(next_result, ensure_ascii=False)
                self.assertIn("01_intake", serialized)
                self.assertIn("AGENTS.md", serialized)
                validated = call(5, "tools/call", {"name": "validate_stage", "arguments": {"run_id": "protocol-fixture", "stage": "01_intake"}})
                self.assertIn("false", json.dumps(validated).lower())
                rejected = call(6, "tools/call", {"name": "finalize", "arguments": {"run_id": "protocol-fixture"}})
                self.assertTrue(rejected.get("isError", rejected.get("is_error", False)))
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                process.stdout.close()
                reader.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

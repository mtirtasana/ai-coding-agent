import base64
import asyncio
from datetime import timedelta
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

from agent.adapters.artifacts import ArtifactStore, TTL
from agent.adapters.editing import FileTools
from agent.adapters.execution import LocalCommandRunner, LintFixRunner
from agent.adapters.handoff import seal_task, restore_task
from agent.adapters.tools import workspace_tools
from agent.core.context import RuleBasedContextBuilder, ContextLimitError, context_chars
from agent.core.types import ConversationState, Message, ToolCall, Continuation
from agent.files import Workspace
from agent.server import EditorTools, Server, restore_state
from agent.core.cancellation import CancellationToken
from agent.terminal import InteractiveShell


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.workspace = Workspace(self.project, True)
        (self.project / "a.py").write_text(
            "first\nsecond 😀\nthird\n", encoding="utf-8"
        )
        self.archive = ArtifactStore(self.workspace)

    def disk(self, **kwargs):
        if not importlib.util.find_spec("cryptography"):
            self.skipTest("Optional .[archives] dependency is not installed.")
        return ArtifactStore(
            self.workspace,
            persistent=True,
            directory=self.root / "archives",
            key=b"x" * 32,
            **kwargs,
        )

    def state(self, size=5000):
        payload = json.dumps(
            {
                "ok": True,
                "content": json.dumps(
                    {"path": "a.py", "sha256": "a" * 64, "content": "data" * size}
                ),
            }
        )
        return ConversationState(
            [
                Message("user", "Fix a.py and retain the public API"),
                Message(
                    "assistant",
                    tool_calls=(ToolCall("read", "file_snapshot", '{"path":"a.py"}'),),
                ),
                Message("tool", payload, call_id="read"),
                Message(
                    "assistant", "Changed a.py. Follow-up: inspect dependency behavior."
                ),
            ],
            latest_plan="old plan",
            accepted_plan="old accepted plan",
        )


class BatchTests(Fixture):
    def test_editor_bridge_never_receives_denied_batch_paths(self):
        def bridge(method, params):
            self.assertEqual(method, "editor/tool")
            self.assertEqual(
                [item["path"] for item in params["arguments"]["requests"]], ["a.py"]
            )
            return {
                "ok": True,
                "content": json.dumps(
                    {
                        "results": [
                            {
                                "ok": True,
                                "path": "a.py",
                                "sha256": "a" * 64,
                                "content": "from dirty buffer",
                            }
                        ],
                        "stable": True,
                        "atomic": False,
                    }
                ),
            }

        tools = EditorTools(
            self.workspace, bridge, {}, CancellationToken(), self.archive
        )
        result = tools.execute(
            ToolCall(
                "b",
                "batch_inspect",
                json.dumps(
                    {
                        "requests": [
                            {"path": ".env", "start_line": 1, "end_line": 1},
                            {"path": "a.py", "start_line": 1, "end_line": 1},
                        ]
                    }
                ),
            )
        )
        self.assertTrue(result.ok)
        self.assertEqual(
            [item["ok"] for item in json.loads(result.content)["results"]],
            [False, True],
        )

    def test_partial_results_hashes_registry_validation_and_receipt(self):
        (self.project / ".env").write_text("secret-code")
        tools = workspace_tools(self.workspace, archive=self.archive)
        requests = [
            {"path": name, "start_line": 1, "end_line": 2}
            for name in ("a.py", ".env", "../outside", "missing")
        ]
        result = tools.execute(
            ToolCall("batch", "batch_inspect", json.dumps({"requests": requests}))
        )
        self.assertTrue(result.ok)
        data = json.loads(result.content)
        self.assertTrue(data["stable"])
        self.assertFalse(data["atomic"])
        self.assertEqual(
            [item["ok"] for item in data["results"]], [True, False, False, False]
        )
        self.assertEqual(data["results"][0]["content"], "first\nsecond 😀\n")
        self.assertEqual(
            data["results"][0]["sha256"],
            json.loads(FileTools(self.workspace).snapshot("a.py"))["sha256"],
        )
        self.assertNotIn("secret-code", result.content)
        _, raw = self.archive.get(data["receipt"]["handle"])
        self.assertEqual(json.loads(raw)["results"], data["results"])
        for invalid in (
            [],
            requests * 3,
            [{"path": "a.py", "start_line": True, "end_line": 2}],
            [{"path": "a.py", "start_line": 1, "end_line": 201}],
            [{"path": "a.py", "start_line": 1, "end_line": 2, "extra": 1}],
        ):
            with self.subTest(invalid=invalid):
                self.assertFalse(
                    tools.execute(
                        ToolCall(
                            "bad", "batch_inspect", json.dumps({"requests": invalid})
                        )
                    ).ok
                )

    def test_output_budget_reserves_space_for_later_errors(self):
        (self.project / "a.py").write_text("x" * 11000)
        requests = [{"path": "a.py", "start_line": 1, "end_line": 1}] * 2
        requests += [
            {"path": "missing" + str(i) + "z" * 1000, "start_line": 1, "end_line": 1}
            for i in range(8)
        ]
        result = workspace_tools(self.workspace, archive=self.archive).execute(
            ToolCall("b", "batch_inspect", json.dumps({"requests": requests}))
        )
        self.assertLessEqual(len(result.content), 24000)
        self.assertTrue(json.loads(result.content)["results"][0]["ok"])
        self.assertFalse(json.loads(result.content)["results"][1]["ok"])

    def test_file_changes_during_batch_are_marked_unstable(self):
        files = FileTools(self.workspace)
        original = files._read
        count = 0

        def read(name):
            nonlocal count
            count += 1
            if count == 2:
                (self.project / "a.py").write_text("changed\n")
            return original(name)

        with patch.object(files, "_read", side_effect=read):
            data = json.loads(
                files.batch_inspect([{"path": "a.py", "start_line": 1, "end_line": 1}])
            )
        self.assertFalse(data["stable"])
        self.assertEqual(data["results"][0]["content"], "first\n")


class ReceiptTests(Fixture):
    def test_fixer_archives_each_original_capture_and_preserves_rerun_requirement(self):
        fixer = LocalCommandRunner(
            self.project, [sys.executable, "-c", "print('fixed')"], check="lint_fix"
        )
        lint = LocalCommandRunner(
            self.project, [sys.executable, "-c", "print('linted')"], check="lint"
        )
        tools = workspace_tools(
            self.workspace,
            archive=self.archive,
            lint_fix_runner=LintFixRunner(fixer, lint),
        )
        report = json.loads(
            tools.execute(ToolCall("fix", "run_lint_fix", "{}")).content
        )
        self.assertTrue(report["requires_test_rerun"])
        self.assertEqual(
            self.archive.get(report["fix"]["receipt"]["handle"])[1].strip(), b"fixed"
        )
        self.assertEqual(
            self.archive.get(report["lint"]["receipt"]["handle"])[1].strip(), b"linted"
        )

    def test_source_path_access_is_rechecked_before_archive_creation(self):
        (self.project / ".agentignore").write_text("a.py\n")
        with self.assertRaises(ValueError):
            self.archive.put("inspection", b"old source", {"paths": ["a.py"]})

    def test_persistent_key_is_scoped_to_vault_and_never_written_to_disk(self):
        if not importlib.util.find_spec("cryptography"):
            self.skipTest("Requires optional .[archives].")
        backend = Mock()
        backend.get_password.return_value = None
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("agent.adapters.connections.OSVault.backend", return_value=backend),
        ):
            previous = os.environ.pop("AGENT_ARCHIVE_KEY", None)
            try:
                store = ArtifactStore(
                    self.workspace,
                    persistent=True,
                    directory=self.root / "vault-archives",
                )
            finally:
                if previous is not None:
                    os.environ["AGENT_ARCHIVE_KEY"] = previous
        arguments = backend.set_password.call_args.args
        self.assertEqual(arguments[0], "ai-coding-agent-artifacts")
        self.assertEqual(arguments[1], store.scope)
        self.assertEqual(len(base64.b64decode(arguments[2])), 32)
        self.assertEqual(list(store.directory.iterdir()), [])

    def test_exact_binary_roundtrip_and_authenticated_handles(self):
        original = b"\xff\x00Traceback\r\n"
        receipt = self.archive.put("check", original, {"exit_code": 1})
        read = json.loads(self.archive.read(receipt["handle"], 0, 4096))
        self.assertEqual(base64.b64decode(read["base64"]), original)
        self.assertTrue(read["eof"])
        receipt["metadata"]["exit_code"] = 0
        self.assertEqual(
            self.archive.get(receipt["handle"])[0]["metadata"]["exit_code"], 1
        )
        for bad in (
            "../secret",
            receipt["handle"][:-1] + ("1" if receipt["handle"][-1] != "1" else "0"),
        ):
            with self.assertRaises(ValueError):
                self.archive.get(bad)
        for offset, length in ((-1, 1), (0, 4097), (True, 1), (0, 0)):
            with self.assertRaises(ValueError):
                self.archive.read(receipt["handle"], offset, length)

    def test_access_policy_change_revokes_historical_reads(self):
        receipt = self.archive.put("inspection", b"old source")
        (self.project / ".agentignore").write_text("a.py\n")
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.archive.get(receipt["handle"])

    def test_disk_encryption_restart_and_wrong_key(self):
        store = self.disk()
        receipt = store.put("check", b"PRIVATE-source-contents")
        path = store.directory / (receipt["id"] + ".json")
        self.assertNotIn(b"PRIVATE-source-contents", path.read_bytes())
        restarted = self.disk()
        self.assertEqual(
            restarted.get(receipt["handle"])[1], b"PRIVATE-source-contents"
        )
        wrong = ArtifactStore(
            self.workspace, persistent=True, directory=store.directory, key=b"y" * 32
        )
        with self.assertRaises(ValueError):
            wrong.get(receipt["handle"])
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_disk_tampering_and_workspace_binding(self):
        store = self.disk()
        receipt = store.put("check", b"original", {"exit_code": 1})
        path = store.directory / (receipt["id"] + ".json")
        data = json.loads(path.read_text())
        self.assertNotIn("metadata", data["receipt"])
        data["receipt"]["bytes"] = 0
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            store.get(receipt["handle"])
        other = self.root / "other"
        other.mkdir()
        other_store = ArtifactStore(
            Workspace(other), persistent=True, directory=store.directory, key=b"x" * 32
        )
        with self.assertRaises(ValueError):
            other_store.get(receipt["handle"])

    def test_ciphertext_authentication_and_expiry(self):
        current = [1000]
        store = self.disk(clock=lambda: current[0])
        receipt = store.put("check", b"original")
        path = store.directory / (receipt["id"] + ".json")
        data = json.loads(path.read_text())
        cipher = bytearray(base64.b64decode(data["data"]))
        cipher[-1] ^= 1
        data["data"] = base64.b64encode(cipher).decode()
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            store.get(receipt["handle"])
        fresh = store.put("task", b"other")
        current[0] += TTL
        with self.assertRaises(ValueError):
            store.get(fresh["handle"])
        store.prune()
        self.assertFalse((store.directory / (fresh["id"] + ".json")).exists())

    def test_quota_lock_and_workspace_storage_guards(self):
        store = self.disk()
        with patch("agent.adapters.artifacts.MAX_STORAGE", 100):
            with self.assertRaisesRegex(ValueError, "quota"):
                store.put("check", b"data")
        self.assertFalse((store.directory / ".write.lock").exists())
        (store.directory / ".write.lock").write_text("busy")
        with self.assertRaises(FileExistsError):
            store.put("check", b"data")
        with self.assertRaises(ValueError):
            ArtifactStore(
                self.workspace,
                persistent=True,
                directory=self.project / "archives",
                key=b"x" * 32,
            )

    def test_receipt_proves_bounded_raw_capture_not_success_or_full_stdout(self):
        command = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'\\xff' + b'x'*5000); sys.exit(7)",
        ]
        runner = LocalCommandRunner(self.workspace.root, command, output_limit=1024)
        tools = workspace_tools(
            self.workspace, test_runner=runner, archive=self.archive
        )
        result = tools.execute(ToolCall("run", "run_tests", "{}"))
        report = json.loads(result.content)
        self.assertFalse(result.ok)
        self.assertEqual(report["exit_code"], 7)
        self.assertTrue(report["output_truncated"])
        receipt, raw = self.archive.get(report["receipt"]["handle"])
        self.assertEqual(raw, b"\xff" + b"x" * 1023)
        self.assertEqual(receipt["metadata"]["exit_code"], 7)
        self.assertFalse(receipt["metadata"]["passed"])


class HandoffTests(Fixture):
    def test_oversized_restore_response_cannot_commit_backend_state(self):
        _, receipt = seal_task(
            self.workspace, self.state(), self.archive, "Open dependency work."
        )
        server = Server(io.StringIO())
        server.initialize(
            {"protocolVersion": 2, "workspace": str(self.project), "provider": "demo"}
        )
        server.artifacts = self.archive
        before = asdict(server.state)
        with patch("agent.server.MAX_FRAME", 1024):
            server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "session/restoreTask",
                    "params": {"handle": receipt["handle"]},
                }
            )
        self.assertEqual(asdict(server.state), before)
        self.assertIn("error", json.loads(server.output.getvalue().splitlines()[-1]))

    def test_sealing_refuses_newly_denied_observations_and_keeps_original_history(self):
        state = self.state()
        before = asdict(state)
        (self.project / ".agentignore").write_text("a.py\n")
        with self.assertRaises(ValueError):
            seal_task(
                self.workspace, state, self.archive, "Inspect dependencies later."
            )
        self.assertEqual(asdict(state), before)

    def test_seal_and_restore_reduce_context_without_mutating_source_or_replaying(self):
        state = self.state()
        original = asdict(state)
        sealed, receipt = seal_task(
            self.workspace,
            state,
            self.archive,
            "Decision: preserve API. Open: inspect dependencies.",
        )
        self.assertEqual(asdict(state), original)
        self.assertLess(
            context_chars(sealed.messages, []), context_chars(state.messages, []) * 0.4
        )
        self.assertIn("inspect dependencies", sealed.messages[0].content)
        self.assertEqual(restore_state(json.loads(json.dumps(asdict(sealed)))), sealed)
        restored = restore_task(self.archive, receipt["handle"])
        self.assertEqual(restored.messages, state.messages)
        self.assertIsNone(restored.accepted_plan)
        self.assertIsNone(restored.latest_plan)

    def test_incomplete_tasks_and_archive_failure_leave_history_intact(self):
        state = self.state()
        before = asdict(state)
        broken = Mock()
        broken.put.side_effect = OSError("full")
        with self.assertRaises(OSError):
            seal_task(self.workspace, state, broken, "Open work")
        self.assertEqual(asdict(state), before)
        for invalid in (ConversationState(), ConversationState(state.messages[:-1])):
            with self.assertRaises(ValueError):
                seal_task(self.workspace, invalid, self.archive, "Open work")

    def test_cli_commands_reset_mode_and_evidence(self):
        output, reset = [], Mock()
        shell = InteractiveShell(
            self.workspace,
            Mock(),
            mode="implement",
            output=output.append,
            artifacts=self.archive,
            reset_verification=reset,
        )
        shell.state = self.state()
        shell.handle("/seal Preserve API; investigate dependencies.")
        self.assertEqual(shell.mode, "plan")
        self.assertEqual(len(shell.state.messages), 2)
        reset.assert_called_once()
        handoff = json.loads(shell.state.messages[0].content.split("\n", 1)[1])
        shell.handle("/raw " + handoff["receipt"]["handle"] + " 0:10")
        self.assertIn("base64", output[-1])
        shell.handle("/restore-task " + handoff["receipt"]["handle"])
        self.assertEqual(len(shell.state.messages), 4)
        self.assertEqual(shell.mode, "plan")

    def test_rpc_seal_restore_and_busy_guard(self):
        server = Server(io.StringIO())
        server.initialize(
            {"protocolVersion": 2, "workspace": str(self.project), "provider": "demo"}
        )
        server.state = self.state()
        server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session/seal",
                "params": {"summary": "Open dependency work."},
            }
        )
        message = json.loads(server.output.getvalue().splitlines()[-1])
        handle = message["result"]["receipt"]["handle"]
        self.assertEqual(len(server.state.messages), 2)
        server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/restoreTask",
                "params": {"handle": handle},
            }
        )
        self.assertEqual(len(server.state.messages), 4)
        server.worker = Mock(is_alive=lambda: True)
        before = asdict(server.state)
        server.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/seal",
                "params": {"summary": "Must not seal busy task"},
            }
        )
        self.assertEqual(asdict(server.state), before)
        self.assertEqual(
            json.loads(server.output.getvalue().splitlines()[-1])["error"]["code"],
            -32001,
        )


class FrontierTests(Fixture):
    def test_pinned_completed_provider_turn_remains_whole(self):
        history = self.state(size=1500).messages
        history[1] = replace(
            history[1], continuation=Continuation("fixture", "opaque-state")
        )
        history = history + [Message("user", "Next task")]
        selected = RuleBasedContextBuilder("compact", pinned_paths=["a.py"]).build(
            history, [], 100000
        )
        self.assertEqual(selected.messages, tuple(history))
        with self.assertRaises(ContextLimitError):
            RuleBasedContextBuilder("compact", pinned_paths=["a.py"]).build(
                history, [], context_chars(history, []) - 1
            )

    def history(self):
        messages = self.state(size=1500).messages[:-1]
        messages += [
            Message(
                "assistant",
                tool_calls=(
                    ToolCall(
                        "next",
                        "read_file_range",
                        '{"path":"b.py","start_line":1,"end_line":1}',
                    ),
                ),
            ),
            Message("tool", "small result", call_id="next"),
        ]
        messages += [
            Message(
                "assistant",
                tool_calls=(
                    ToolCall(
                        "last",
                        "read_file_range",
                        '{"path":"c.py","start_line":1,"end_line":1}',
                    ),
                ),
            ),
            Message("tool", "latest result", call_id="last"),
        ]
        return messages

    def test_pinned_observation_is_not_aged_or_dropped(self):
        history = self.history()
        regular = RuleBasedContextBuilder("compact").build(history, [], 100000)
        pinned = RuleBasedContextBuilder("compact", pinned_paths=["./a.py"]).build(
            history, [], 100000
        )
        self.assertLess(regular.stats.selected_chars, pinned.stats.selected_chars)
        self.assertEqual(pinned.messages[2], history[2])
        with self.assertRaises(ContextLimitError):
            RuleBasedContextBuilder("compact", pinned_paths=["a.py"]).build(
                history, [], 1000
            )

    def test_invalid_pin_configuration(self):
        for pins in (
            "a.py",
            ["../secret"],
            ["/absolute"],
            ["C:\\secret"],
            ["a.py"] * 17,
        ):
            with self.assertRaises(ValueError):
                RuleBasedContextBuilder("compact", pinned_paths=pins)


class MCPTests(Fixture):
    def test_real_stdio_protocol_has_only_read_tools_and_exact_recovery(self):
        if not importlib.util.find_spec("mcp"):
            self.skipTest("Requires optional .[mcp].")
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def exercise():
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-I", "-m", "agent.mcp_server", "--workspace", str(self.project)],
            )
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(
                    read, write, read_timeout_seconds=timedelta(seconds=8)
                ) as client:
                    await client.initialize()
                    listed = await client.list_tools()
                    self.assertEqual(
                        {tool.name for tool in listed.tools},
                        {"batch_inspect", "archive_read"},
                    )
                    result = await client.call_tool(
                        "batch_inspect",
                        {
                            "requests": [
                                {"path": "a.py", "start_line": 1, "end_line": 2}
                            ]
                        },
                    )
                    self.assertFalse(result.isError)
                    data = result.structuredContent or json.loads(
                        result.content[0].text
                    )
                    self.assertTrue(data["ok"])
                    handle = data["result"]["receipt"]["handle"]
                    recovered = await client.call_tool(
                        "archive_read", {"handle": handle, "offset": 0, "length": 4096}
                    )
                    self.assertFalse(recovered.isError)
                    raw = recovered.structuredContent or json.loads(
                        recovered.content[0].text
                    )
                    self.assertEqual(
                        json.loads(base64.b64decode(raw["base64"]))["results"][0][
                            "content"
                        ],
                        "first\nsecond 😀\n",
                    )

        asyncio.run(asyncio.wait_for(exercise(), timeout=20))

    def test_optional_adapter_exposes_only_read_tools(self):
        from agent.mcp_server import build_server

        class FakeMCP:
            def __init__(self, name):
                self.tools = {}

            def tool(self):
                def decorate(function):
                    self.tools[function.__name__] = function
                    return function

                return decorate

        server = build_server(self.workspace, self.archive, factory=FakeMCP)
        self.assertEqual(set(server.tools), {"batch_inspect", "archive_read"})
        result = server.tools["batch_inspect"](
            [{"path": "a.py", "start_line": 1, "end_line": 1}]
        )
        self.assertTrue(result["ok"])
        read = server.tools["archive_read"](
            result["result"]["receipt"]["handle"], 0, 4096
        )
        self.assertTrue(read["eof"])

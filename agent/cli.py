import argparse
import os
import shlex
import sys
from pathlib import Path

from agent.adapters.events import JsonlEventSink
from agent.adapters.instructions import load_project_instructions
from agent.adapters.models import DemoProvider, OllamaProvider, OpenAIProvider, OpenAICompatibleProvider
from agent.adapters.tools import workspace_tools
from agent.core.context import RuleBasedContextBuilder
from agent.core.engine import Agent
from agent.files import Workspace


def main():
    parser = argparse.ArgumentParser(description="Anbieterunabhängiger Coding-Agent")
    parser.add_argument("task", nargs="?")
    parser.add_argument("--mode", choices=["plan", "implement"], default=None)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--provider", choices=["openai", "ollama", "openai-compatible", "demo"],
                        default=os.getenv("AGENT_PROVIDER", "openai"))
    parser.add_argument("--model", default=os.getenv("AGENT_MODEL"))
    parser.add_argument("--base-url", default=None, help="Ollama or compatible API base URL")
    parser.add_argument("--api-key-env", default="OPENAI_COMPATIBLE_API_KEY",
                        help="Environment variable holding the compatible endpoint API key")
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument("--max-context-chars", type=int, default=200_000)
    parser.add_argument("--context-mode", choices=["recent", "full"], default="recent",
                        help="recent: neueste vollständige Runden; full: gesamte Historie")
    parser.add_argument("--allow-write", action="store_true")
    parser.add_argument("--allow-exec", action="store_true", help="Enable the configured LOCAL test command")
    parser.add_argument("--test-command", help="Explicit command, split into arguments; no shell evaluation")
    parser.add_argument("--test-timeout", type=float, default=60)
    parser.add_argument("--test-output-bytes", type=int, default=65536)
    parser.add_argument("--verbose", action="store_true", help="JSONL-Ereignisse auf stderr")
    args = parser.parse_args()
    if args.max_steps < 1 or args.max_context_chars < 1:
        parser.error("Schritt- und Kontextlimit müssen positiv sein.")
    if args.provider == "openai" and not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY setzen oder --provider ollama/demo verwenden.")
    if args.provider == "ollama" and not args.model:
        parser.error("Für Ollama --model mit einem installierten Modell angeben.")
    if args.provider == "openai-compatible" and (
            not args.model or not args.base_url or not os.getenv(args.api_key_env)):
        parser.error("Compatible provider requires --model, --base-url and the selected API-key environment variable.")
    if args.allow_exec != bool(args.test_command):
        parser.error("Local test execution requires both --allow-exec and --test-command.")
    try:
        workspace = Workspace(args.workspace, args.allow_write)
        runner = None
        if args.allow_exec:
            from agent.adapters.execution import LocalTestRunner
            runner = LocalTestRunner(workspace.root, shlex.split(args.test_command),
                                     args.test_timeout, args.test_output_bytes)
        mode = args.mode or ("plan" if args.task is None else "implement")
        # Check the terminal dependency before creating SDK clients.
        session = None
        if args.task is None:
            from agent.terminal import InteractiveShell, create_prompt_session
            session = create_prompt_session()
        instructions = load_project_instructions(workspace) if session is None else None
        if args.provider == "openai":
            model = OpenAIProvider(args.model or os.getenv("OPENAI_MODEL", "gpt-4.1"))
        elif args.provider == "ollama":
            model = OllamaProvider(args.model, args.base_url or "http://localhost:11434")
        elif args.provider == "openai-compatible":
            model = OpenAICompatibleProvider(args.model, args.base_url, os.environ[args.api_key_env])
        else:
            model = DemoProvider()

        def agent_factory(current_mode, rules):
            return Agent(model, workspace_tools(workspace, test_runner=runner),
                         JsonlEventSink(sys.stderr) if args.verbose else None,
                         max_steps=args.max_steps, max_context_chars=args.max_context_chars,
                         context_builder=RuleBasedContextBuilder(args.context_mode),
                         mode=current_mode, project_instructions=rules)

        if session is not None:
            InteractiveShell(workspace, agent_factory, mode=mode, execution_enabled=runner is not None).run(session)
        else:
            print(agent_factory(mode, instructions.content).run(args.task).text)
    except KeyboardInterrupt:
        parser.exit(130, "Interrupted; file changes may remain.\n")
    except Exception as exc:
        parser.exit(1, f"Fehler: {exc}\n")


if __name__ == "__main__":
    main()

import argparse
import os
import sys
from pathlib import Path

from agent.adapters.events import JsonlEventSink
from agent.adapters.instructions import load_project_instructions
from agent.adapters.models import DemoProvider, OllamaProvider, OpenAIProvider
from agent.adapters.tools import workspace_tools
from agent.core.context import RuleBasedContextBuilder
from agent.core.engine import Agent
from agent.files import Workspace


def main():
    parser = argparse.ArgumentParser(description="Anbieterunabhängiger Coding-Agent")
    parser.add_argument("task", nargs="?")
    parser.add_argument("--mode", choices=["plan", "implement"], default=None)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--provider", choices=["openai", "ollama", "demo"],
                        default=os.getenv("AGENT_PROVIDER", "openai"))
    parser.add_argument("--model", default=os.getenv("AGENT_MODEL"))
    parser.add_argument("--base-url", default="http://localhost:11434",
                        help="Ollama-Serveradresse")
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument("--max-context-chars", type=int, default=200_000)
    parser.add_argument("--context-mode", choices=["recent", "full"], default="recent",
                        help="recent: neueste vollständige Runden; full: gesamte Historie")
    parser.add_argument("--allow-write", action="store_true")
    parser.add_argument("--verbose", action="store_true", help="JSONL-Ereignisse auf stderr")
    args = parser.parse_args()
    if args.max_steps < 1 or args.max_context_chars < 1:
        parser.error("Schritt- und Kontextlimit müssen positiv sein.")
    if args.provider == "openai" and not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY setzen oder --provider ollama/demo verwenden.")
    if args.provider == "ollama" and not args.model:
        parser.error("Für Ollama --model mit einem installierten Modell angeben.")
    try:
        workspace = Workspace(args.workspace, args.allow_write)
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
            model = OllamaProvider(args.model, args.base_url)
        else:
            model = DemoProvider()

        def agent_factory(current_mode, rules):
            return Agent(model, workspace_tools(workspace),
                         JsonlEventSink(sys.stderr) if args.verbose else None,
                         max_steps=args.max_steps, max_context_chars=args.max_context_chars,
                         context_builder=RuleBasedContextBuilder(args.context_mode),
                         mode=current_mode, project_instructions=rules)

        if session is not None:
            InteractiveShell(workspace, agent_factory, mode=mode).run(session)
        else:
            print(agent_factory(mode, instructions.content).run(args.task).text)
    except KeyboardInterrupt:
        parser.exit(130, "Interrupted; file changes may remain.\n")
    except Exception as exc:
        parser.exit(1, f"Fehler: {exc}\n")


if __name__ == "__main__":
    main()

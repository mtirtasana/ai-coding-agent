import argparse
import os
import sys
from pathlib import Path

from agent.adapters.events import JsonlEventSink
from agent.adapters.models import DemoProvider, OllamaProvider, OpenAIProvider
from agent.adapters.tools import workspace_tools
from agent.core.engine import Agent
from agent.files import Workspace


def main():
    parser = argparse.ArgumentParser(description="Anbieterunabhängiger Coding-Agent")
    parser.add_argument("task")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--provider", choices=["openai", "ollama", "demo"],
                        default=os.getenv("AGENT_PROVIDER", "openai"))
    parser.add_argument("--model", default=os.getenv("AGENT_MODEL"))
    parser.add_argument("--base-url", default="http://localhost:11434",
                        help="Ollama-Serveradresse")
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument("--max-context-chars", type=int, default=200_000)
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
        if args.provider == "openai":
            model = OpenAIProvider(args.model or os.getenv("OPENAI_MODEL", "gpt-4.1"))
        elif args.provider == "ollama":
            model = OllamaProvider(args.model, args.base_url)
        else:
            model = DemoProvider()
        workspace = Workspace(args.workspace, args.allow_write)
        agent = Agent(model, workspace_tools(workspace),
                      JsonlEventSink(sys.stderr) if args.verbose else None,
                      max_steps=args.max_steps, max_context_chars=args.max_context_chars)
        print(agent.run(args.task).text)
    except Exception as exc:
        parser.exit(1, f"Fehler: {exc}\n")


if __name__ == "__main__":
    main()

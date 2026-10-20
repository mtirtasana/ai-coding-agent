import argparse
import os
import shlex
import sys
from pathlib import Path

from agent.adapters.events import JsonlEventSink
from agent.adapters.console import EventFanout, RunObserver, friendly_error, safe_text
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
    parser.add_argument('--review-edits', action='store_true', help='Opt in to per-edit terminal diff approval; requires --allow-write and a real terminal')
    parser.add_argument('--no-progress', action='store_true', help='Hide interactive progress lines (run summaries remain)')
    parser.add_argument("--allow-exec", action="store_true", help="Enable configured LOCAL verification commands")
    parser.add_argument("--test-command", help="Explicit command, split into arguments; no shell evaluation")
    parser.add_argument("--test-timeout", type=float, default=60)
    parser.add_argument("--test-output-bytes", type=int, default=65536)
    parser.add_argument("--lint-command", help="Separate explicitly configured lint check (no shell evaluation)")
    parser.add_argument("--lint-fix-command", help="Optional fixer; requires --lint-command and --allow-write")
    parser.add_argument("--lint-timeout", type=float, default=60)
    parser.add_argument("--lint-output-bytes", type=int, default=65536)
    for profile, timeout in (("security", 60), ("dependency-audit", 120)):
        parser.add_argument(f"--{profile}-command", help="Explicit scanner command; no shell evaluation or automatic fixes")
        parser.add_argument(f"--{profile}-timeout", type=float, default=timeout)
        parser.add_argument(f"--{profile}-output-bytes", type=int, default=65536)
    parser.add_argument("--verbose", action="store_true", help="JSONL-Ereignisse auf stderr")
    args = parser.parse_args()
    if args.max_steps < 1 or args.max_context_chars < 1:
        parser.error("Schritt- und Kontextlimit müssen positiv sein.")
    if args.review_edits:
        if not args.allow_write:
            parser.error('--review-edits requires --allow-write.')
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            parser.error('--review-edits requires a real input/output terminal.')
        if args.lint_fix_command:
            parser.error('--review-edits cannot be combined with --lint-fix-command.')
    if args.provider == "openai" and not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY setzen oder --provider ollama/demo verwenden.")
    if args.provider == "ollama" and not args.model:
        parser.error("Für Ollama --model mit einem installierten Modell angeben.")
    if args.provider == "openai-compatible" and (
            not args.model or not args.base_url or not os.getenv(args.api_key_env)):
        parser.error("Compatible provider requires --model, --base-url and the selected API-key environment variable.")
    if args.allow_exec != any((args.test_command, args.lint_command, args.lint_fix_command,
                                   args.security_command, args.dependency_audit_command)):
        parser.error("Local verification requires --allow-exec and at least one configured verification command.")
    if args.lint_fix_command and (not args.lint_command or not args.allow_write):
        parser.error("Lint fixes require --lint-command and --allow-write.")
    try:
        workspace = Workspace(args.workspace, args.allow_write)
        runner = lint_runner = lint_fix_runner = None
        security_runners = {}
        if args.allow_exec:
            from agent.adapters.execution import LocalCommandRunner, LintFixRunner
            if args.test_command:
                runner = LocalCommandRunner(workspace.root, shlex.split(args.test_command),
                                            args.test_timeout, args.test_output_bytes, check='test')
            if args.lint_command:
                lint_runner = LocalCommandRunner(workspace.root, shlex.split(args.lint_command),
                                                 args.lint_timeout, args.lint_output_bytes, check='lint')
            if args.lint_fix_command:
                fixer = LocalCommandRunner(workspace.root, shlex.split(args.lint_fix_command),
                                           args.lint_timeout, args.lint_output_bytes, check='lint_fix')
                lint_fix_runner = LintFixRunner(fixer, lint_runner)
            for profile in ('security', 'dependency_audit'):
                command = getattr(args, f'{profile}_command')
                if command:
                    security_runners[f'{profile}_runner'] = LocalCommandRunner(
                        workspace.root, shlex.split(command), getattr(args, f'{profile}_timeout'),
                        getattr(args, f'{profile}_output_bytes'), check=profile)
        mode = args.mode or ("plan" if args.task is None else "implement")
        # Check the terminal dependency before creating SDK clients.
        session = None
        review_session = None
        if args.task is None:
            from agent.terminal import InteractiveShell, create_prompt_session
            session = create_prompt_session(workspace=workspace)
        if args.review_edits:
            from agent.terminal import create_review_prompt
            review_session = create_review_prompt()
        observer = RunObserver(progress=not args.no_progress and sys.stdout.isatty()) if session is not None else None
        if review_session is not None and observer is None:
            observer = RunObserver(progress=False)
        instructions = load_project_instructions(workspace) if session is None else None
        model_name = ('demo' if args.provider == 'demo' else args.model or
                      (os.getenv('OPENAI_MODEL', 'gpt-4.1') if args.provider == 'openai' else ''))
        if args.provider == "openai":
            model = OpenAIProvider(model_name)
        elif args.provider == "ollama":
            model = OllamaProvider(args.model, args.base_url or "http://localhost:11434")
        elif args.provider == "openai-compatible":
            model = OpenAICompatibleProvider(args.model, args.base_url, os.environ[args.api_key_env])
        else:
            model = DemoProvider()

        def agent_factory(current_mode, rules):
            tools = workspace_tools(workspace, test_runner=runner,
                                               lint_runner=lint_runner, lint_fix_runner=lint_fix_runner,
                                               **security_runners)
            if observer is not None or review_session is not None:
                from agent.adapters.review import ConsoleTools, ReviewedWrites
                from agent.terminal import confirm_change
                reviewer = ReviewedWrites(workspace, lambda *args: confirm_change(review_session, print, *args)) if review_session is not None else None
                tools = ConsoleTools(tools, observer, reviewer)
            return Agent(model, tools,
                         EventFanout(observer, JsonlEventSink(sys.stderr) if args.verbose else None),
                         max_steps=args.max_steps, max_context_chars=args.max_context_chars,
                         context_builder=RuleBasedContextBuilder(args.context_mode),
                         mode=current_mode, project_instructions=rules)

        if session is not None:
            InteractiveShell(workspace, agent_factory, mode=mode, execution_enabled=runner is not None,
                             lint_enabled=lint_runner is not None,
                             lint_fix_enabled=lint_fix_runner is not None,
                             security_enabled='security_runner' in security_runners,
                             audit_enabled='dependency_audit_runner' in security_runners,
                             observer=observer, provider=args.provider,
                             model=model_name,
                             max_context_chars=args.max_context_chars, review_edits=args.review_edits).run(session)
        else:
            try:
                result = agent_factory(mode, instructions.content).run(args.task)
            except BaseException:
                if observer is not None:
                    observer.summary('interrupted' if sys.exc_info()[0] is KeyboardInterrupt else 'failed')
                raise
            print(safe_text(result.text))
            if observer is not None:
                observer.summary()
    except KeyboardInterrupt:
        if args.verbose:
            JsonlEventSink(sys.stderr).emit('cli_interrupted', {'message': 'Interrupted; file changes may remain.'})
            parser.exit(130)
        parser.exit(130, "Interrupted; file changes may remain.\n")
    except Exception as exc:
        if args.verbose:
            JsonlEventSink(sys.stderr).emit('cli_error', {'error_type': type(exc).__name__, 'message': friendly_error(exc)})
            parser.exit(1)
        parser.exit(1, safe_text(f"Fehler: {friendly_error(exc)}\n"))


if __name__ == "__main__":
    main()

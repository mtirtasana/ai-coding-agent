"""Interactive frontend. Commands and session management never call a model implicitly."""
from difflib import get_close_matches

from agent.adapters.instructions import load_project_instructions
from agent.core.types import ConversationState


COMMANDS = {
    "/plan": "Switch to read-only planning; /plan <task> also submits a task.",
    "/implement": "Accept the latest plan, switch mode, and wait (writes need --allow-write).",
    "/instructions": "Show the loaded workspace-root AGENTS.md snapshot.",
    "/help": "Show commands and effective permissions.",
    "/new": "Clear conversation/plans, reload instructions, and return to planning.",
    "/exit": "Exit the terminal.",
}


def command_candidates(text):
    return tuple(name for name in COMMANDS if text.startswith("/")
                 and not any(c.isspace() for c in text) and name.startswith(text))


def create_prompt_session(**kwargs):
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.history import InMemoryHistory
        from prompt_toolkit.key_binding import KeyBindings
    except ImportError as exc:
        raise RuntimeError("Interactive terminal requires: pip install -e '.[terminal]'") from exc

    class SlashCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            for name in command_candidates(text):
                yield Completion(name, start_position=-len(text), display_meta=COMMANDS[name])

    bindings = KeyBindings()

    @bindings.add("tab")
    def complete_command(event):
        buffer = event.app.current_buffer
        text = buffer.document.text_before_cursor
        candidates = command_candidates(text)
        if buffer.complete_state:
            buffer.complete_next()
        elif len(candidates) == 1:
            # Apply unique matches synchronously, even if Enter follows immediately.
            buffer.apply_completion(Completion(candidates[0], start_position=-len(text)))
        elif candidates:
            buffer.start_completion(select_first=False)

    return PromptSession(completer=SlashCompleter(), history=InMemoryHistory(),
                         complete_while_typing=False, key_bindings=bindings, **kwargs)


class InteractiveShell:
    def __init__(self, workspace, agent_factory, mode="plan", output=print, execution_enabled=False,
                 lint_enabled=False, lint_fix_enabled=False, security_enabled=False, audit_enabled=False):
        if mode not in {"plan", "implement"}:
            raise ValueError("Mode must be plan or implement.")
        self.workspace, self.agent_factory, self.mode = workspace, agent_factory, mode
        self.output = output
        self.execution_enabled = execution_enabled
        self.lint_enabled, self.lint_fix_enabled = lint_enabled, lint_fix_enabled
        self.security_enabled, self.audit_enabled = security_enabled, audit_enabled
        self.state = ConversationState()
        self.instructions = load_project_instructions(workspace)

    @property
    def writable(self):
        return self.mode == "implement" and self.workspace.allow_write

    @property
    def prompt(self):
        tests = 'on' if self.execution_enabled and self.mode == 'implement' else 'off'
        lint = 'on' if self.lint_enabled and self.mode == 'implement' else 'off'
        fixes = 'on' if self.lint_fix_enabled and self.writable else 'off'
        security = 'on' if self.security_enabled and self.mode == 'implement' else 'off'
        audit = 'on' if self.audit_enabled and self.mode == 'implement' else 'off'
        return (f"[{self.mode} | {'write' if self.writable else 'read-only'} | tests:{tests} "
                f"| lint:{lint} | fixes:{fixes} | security:{security} | audit:{audit}] > ")

    def submit(self, task):
        try:
            agent = self.agent_factory(self.mode, self.instructions.content)
            result = agent.run(task, self.state)
        except KeyboardInterrupt:
            self.output("Run interrupted. Conversation unchanged; file changes may remain. No automatic retry.")
        except Exception as exc:
            self.output(f"Run failed: {exc}. Conversation unchanged; file changes may remain.")
        else:
            self.output(result.text)

    def handle(self, text):
        text = text.strip()
        if not text:
            return True
        if not text.startswith("/"):
            self.submit(text)
            return True
        parts = text.split(maxsplit=1)
        command, argument = parts[0], parts[1] if len(parts) == 2 else ""
        if command not in COMMANDS:
            suggestions = get_close_matches(command, COMMANDS, n=3, cutoff=0.4)
            self.output("Unknown command. " + ("Try: " + ", ".join(suggestions) if suggestions else "Use /help."))
            return True
        if argument and command != "/plan":
            self.output(f"{command} does not accept arguments. Use /help.")
            return True
        if command == "/exit":
            return False
        if command == "/help":
            self.output("\n".join(f"{name}: {description}" for name, description in COMMANDS.items()))
            self.output(self.prompt.strip())
        elif command == "/instructions":
            self.output(f"Loaded snapshot: {self.instructions.path}\n" +
                        (self.instructions.content or "(No project instructions loaded.)"))
        elif command == "/new":
            # Load first: a broken new file must not destroy a working conversation.
            try:
                instructions = load_project_instructions(self.workspace)
            except (OSError, ValueError) as exc:
                self.output(f"Could not reload instructions: {exc}. Session unchanged.")
                return True
            self.instructions = instructions
            self.state = ConversationState()
            self.mode = "plan"
            self.output("New conversation. Instructions reloaded; mode is plan.")
        elif command == "/plan":
            self.mode = "plan"
            self.output("Planning mode: read-only.")
            if argument:
                self.submit(argument)
        elif command == "/implement":
            if self.state.latest_plan is not None:
                self.state.accepted_plan = self.state.latest_plan
            self.mode = "implement"
            self.output("Implementation mode. Waiting for your next task.")
            if not self.writable:
                self.output("Write access is disabled. Restart with --allow-write to permit edits.")
        return True

    def run(self, session):
        self.output("Interactive coding agent. Tab completes /commands; /help lists them.")
        while True:
            try:
                text = session.prompt(self.prompt)
            except KeyboardInterrupt:
                continue
            except EOFError:
                break
            if not self.handle(text):
                break

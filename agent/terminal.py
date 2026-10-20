"""Interactive frontend. Commands and session management never call a model implicitly."""
from difflib import get_close_matches
import re
import shlex

from agent.adapters.attachments import Attachments
from agent.adapters.console import RunObserver, TerminalUnavailable, friendly_error, safe_text
from agent.adapters.instructions import load_project_instructions
from agent.adapters.review import full_diff
from agent.core.types import ConversationState


COMMANDS = {
    "/plan": "Switch to read-only planning; /plan <task> also submits a task.",
    "/implement": "Accept the latest plan, switch mode, and wait (writes need --allow-write).",
    "/instructions": "Show the loaded workspace-root AGENTS.md snapshot.",
    "/status": "Show provider, workspace, permissions, budget and pending attachments.",
    "/attach": 'Queue a file or range: /attach "path" [start:end].',
    "/attachments": "List pending attachments without sending them to a model.",
    "/detach": "Remove an attachment ID, or /detach all.",
    "/help": "Show commands and effective permissions.",
    "/new": "Clear conversation/plans, reload instructions, and return to planning.",
    "/exit": "Exit the terminal.",
}


def command_candidates(text):
    return tuple(name for name in COMMANDS if text.startswith("/")
                 and not any(c.isspace() for c in text) and name.startswith(text))


def attachment_candidates(text, workspace):
    """Complete one path token only; never enumerate outside guarded directories."""
    match = re.match(r'^/attach[ \t]+(.*)$', text)
    if workspace is None or match is None:
        return ()
    token = match[1]
    fragment = token
    if fragment[:1] in {'"', "'"}:
        quote, fragment = fragment[0], fragment[1:]
        if fragment.endswith(quote):
            fragment = fragment[:-1]
        if quote in fragment:
            return ()
    elif any(c.isspace() for c in fragment):
        return ()
    # User-facing paths use / on every platform; quoted backslashes are accepted by /attach.
    parent, _, prefix = fragment.rpartition('/')
    try:
        directory = workspace.path(parent or '.')
        matches = []
        for child in sorted(directory.iterdir(), key=lambda p: p.name):
            if not child.name.startswith(prefix) or safe_text(child.name) != child.name:
                continue
            candidate = (parent + '/' if parent else '') + child.name
            try:
                workspace.path(candidate)
            except (OSError, ValueError):
                continue
            candidate += '/' if child.is_dir() else ''
            matches.append((shlex.quote(candidate), len(token), child.name))
            if len(matches) >= 100:
                break
        return tuple(matches)
    except (OSError, ValueError):
        return ()


def create_prompt_session(workspace=None, **kwargs):
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.history import InMemoryHistory
        from prompt_toolkit.key_binding import KeyBindings
    except ImportError as exc:
        raise TerminalUnavailable("Interactive terminal requires: pip install -e '.[terminal]'") from exc

    class SlashCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            for name in command_candidates(text):
                yield Completion(name, start_position=-len(text), display_meta=COMMANDS[name])
            for value, length, label in attachment_candidates(text, workspace):
                yield Completion(value, start_position=-length, display=label, display_meta='Workspace path')

    bindings = KeyBindings()

    @bindings.add('enter')
    def submit(event):
        event.app.current_buffer.validate_and_handle()

    @bindings.add('escape', 'enter')
    def newline(event):
        event.app.current_buffer.insert_text('\n')

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
        else:
            paths = attachment_candidates(text, workspace)
            if len(paths) == 1:
                value, length, label = paths[0]
                buffer.apply_completion(Completion(value, start_position=-length, display=label))
            elif paths:
                buffer.start_completion(select_first=False)

    return PromptSession(completer=SlashCompleter(), history=InMemoryHistory(),
                         complete_while_typing=False, multiline=True, key_bindings=bindings, **kwargs)


def create_review_prompt(**kwargs):
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import InMemoryHistory
    except ImportError as exc:
        raise TerminalUnavailable("Edit review requires: pip install -e '.[terminal]'") from exc
    return PromptSession(history=InMemoryHistory(), **kwargs)


def confirm_change(session, output, name, before, after):
    output(safe_text(f'Change to review: {name}\n' + (full_diff(name, before, after) or '(No content difference)')))
    output('Awaiting edit review. Apply or Reject? Default: Reject.')
    try:
        return session.prompt('[a]pply / [r]eject > ').strip().lower() in {'a', 'apply'}
    except EOFError:
        return False


class InteractiveShell:
    def __init__(self, workspace, agent_factory, mode="plan", output=print, execution_enabled=False,
                 lint_enabled=False, lint_fix_enabled=False, security_enabled=False, audit_enabled=False,
                 observer=None, provider='unknown', model='unknown', max_context_chars=200000,
                 review_edits=False):
        if mode not in {"plan", "implement"}:
            raise ValueError("Mode must be plan or implement.")
        self.workspace, self.agent_factory, self.mode = workspace, agent_factory, mode
        self.output = lambda text: output(safe_text(text))
        self.observer = observer or RunObserver(self.output)
        self.provider, self.model, self.max_context_chars = provider, model, max_context_chars
        self.review_edits = review_edits
        self.attachments = Attachments(workspace)
        self.draft = ''
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
        self.observer.reset()
        status = 'failed'
        try:
            enriched = self.attachments.task(task)
        except (OSError, ValueError) as exc:
            self.output(f'Attachment validation failed: {safe_text(exc)}. Correct attachments before retrying.')
            self.draft = task
            self.observer.summary('failed')
            return False
        try:
            agent = self.agent_factory(self.mode, self.instructions.content)
            # Direct shell integrations can keep their two-argument factory.
            if agent.events is None:
                agent.events = self.observer
            result = agent.run(enriched, self.state)
        except KeyboardInterrupt:
            status = 'interrupted'
            self.output("Run interrupted. Conversation unchanged; file changes may remain. No automatic retry.")
        except Exception as exc:
            self.output(f"Run failed: {friendly_error(exc)} Conversation unchanged; file changes may remain.")
        else:
            status = 'completed'
            self.attachments.clear()
            self.output(result.text)
        self.draft = '' if status == 'completed' else task
        self.observer.summary(status)
        return status == 'completed'

    def handle(self, text):
        if not text.strip():
            return True
        if not text.lstrip().startswith("/"):
            self.submit(text)
            return True
        text = text.strip()
        parts = text.split(maxsplit=1)
        command, argument = parts[0], parts[1] if len(parts) == 2 else ""
        if command not in COMMANDS:
            suggestions = get_close_matches(command, COMMANDS, n=3, cutoff=0.4)
            self.output("Unknown command. " + ("Try: " + ", ".join(suggestions) if suggestions else "Use /help."))
            return True
        if argument and command not in {"/plan", "/attach", "/detach"}:
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
        elif command == '/status':
            self.output(f'Workspace: {self.workspace.root}\nProvider: {self.provider}\nModel: {self.model}\n'
                        f'Context budget: {self.max_context_chars} characters\nReview edits: {self.review_edits}\n'
                        f'Pending attachments: {len(self.attachments.items)}\n' + self.prompt.strip())
        elif command == '/attachments':
            self.output('\n'.join(f'{item.id}: {item.path} ' + (
                f'[{item.start}:{item.end}]' if item.start is not None else '[whole file]')
                for item in self.attachments.items) or '(No pending attachments)')
        elif command in {'/attach', '/detach'}:
            try:
                parts = shlex.split(argument)
                if command == '/detach':
                    if len(parts) != 1:
                        raise ValueError('Usage: /detach <id|all>')
                    self.attachments.detach(parts[0])
                    self.output('Attachment removed.')
                else:
                    if len(parts) not in {1, 2}:
                        raise ValueError('Usage: /attach "path" [start:end]')
                    bounds = None if len(parts) == 1 else re.fullmatch(r'([0-9]+):([0-9]+)', parts[1])
                    if len(parts) == 2 and bounds is None:
                        raise ValueError('Expected start:end with positive integer line numbers.')
                    item = self.attachments.add(parts[0], *([int(n) for n in bounds.groups()] if bounds else []))
                    self.output(f'Attachment {item.id} queued. A fresh snapshot will be read before submission.')
            except (OSError, ValueError) as exc:
                self.output(f'Attachment error: {safe_text(exc)}')
        elif command == "/new":
            # Load first: a broken new file must not destroy a working conversation.
            try:
                instructions = load_project_instructions(self.workspace)
            except (OSError, ValueError) as exc:
                self.output(f"Could not reload instructions: {exc}. Session unchanged.")
                return True
            self.instructions = instructions
            self.state = ConversationState()
            self.attachments.clear()
            self.draft = ''
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
        self.output("Interactive coding agent. Enter sends; Alt+Enter adds a line. Tab completes /commands; /help lists them.")
        while True:
            try:
                text = session.prompt(self.prompt, **({'default': self.draft} if self.draft else {}))
            except KeyboardInterrupt:
                self.draft = ''
                continue
            except EOFError:
                break
            if not self.handle(text):
                break

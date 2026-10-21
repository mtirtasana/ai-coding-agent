"""Plain terminal presentation and CLI-only observation; no model content in events."""
import json
import time
import unicodedata


class TerminalUnavailable(RuntimeError):
    pass


def safe_text(value):
    text = str(value)
    return ''.join(c if c in '\n\t' or not unicodedata.category(c).startswith('C')
                   else (f'\\x{ord(c):02x}' if ord(c) < 256 else f'\\u{ord(c):04x}') for c in text)


def friendly_error(exc):
    name = type(exc).__name__
    if name == 'ConnectionSetupError':
        return str(exc)
    if isinstance(exc, TerminalUnavailable):
        return "Interactive terminal and edit review require: pip install -e '.[terminal]'"
    if isinstance(exc, ImportError):
        return "Provider dependencies are missing. Install the optional .[openai] extra for OpenAI/compatible APIs."
    if name == 'ContextLimitError':
        return 'Context budget exceeded. Narrow attachments, start /new, or increase --max-context-chars.'
    if name == 'StepLimitError':
        return 'Step limit reached. Inspect changes before submitting another task or increasing --max-steps.'
    if name in {'AuthenticationError', 'PermissionDeniedError'}:
        return 'Provider access denied. Check the configured API-key environment variable and account permissions.'
    if name == 'RateLimitError':
        return 'Provider rate or quota limit reached. Check your quota and retry explicitly later.'
    if isinstance(exc, (TimeoutError, ConnectionError)) or name in {'APITimeoutError', 'APIConnectionError', 'URLError'}:
        return 'Provider connection failed or timed out. Check the endpoint, network and proxy configuration.'
    return f'Operation failed ({name}). Check provider configuration, workspace files and installed dependencies.'


class RunObserver:
    def __init__(self, output=print, progress=True):
        self.output = lambda text: output(safe_text(text))
        self.progress = progress
        self.reset()

    def reset(self):
        self.started = time.monotonic()
        self.calls = self.completed = self.tools = self.applied = self.rejected = 0
        self.usage = []
        self.checks = []
        self.selected_chars = 0

    def emit(self, kind, data):
        if kind == 'run_started':
            self.reset()
        elif kind == 'context_selected':
            self.calls += 1
            self.selected_chars += data['selected_chars']
            self.show(f"Waiting for model (request {self.calls})...")
        elif kind == 'model_completed':
            self.completed += 1
            self.usage.append(data.get('usage'))
        elif kind == 'tool_started':
            self.show(f"Starting {data['name']}...")
        elif kind == 'tool_completed':
            self.tools += 1
            self.show(f"{data['name']}: {'completed' if data['ok'] else 'failed'}")

    def show(self, text):
        if self.progress:
            self.output(text)

    def tool_result(self, name, result):
        try:
            data = json.loads(result.content)
        except (ValueError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if name in {'write_file', 'apply_edit'}:
            self.applied += bool(result.ok)
            self.rejected += data.get('review_status') == 'rejected'
        profiles = {'run_tests': 'test', 'run_lint': 'lint', 'run_lint_fix': 'lint_fix',
                    'run_security': 'security', 'run_dependency_audit': 'dependency_audit'}
        if name in profiles:
            status = 'not run' if data.get('not_run') is True else (
                'passed' if result.ok and data.get('passed') is True else 'failed')
            self.checks.append((profiles[name], status))
            self.show(f'{profiles[name]}: {status}')

    def summary(self, status='completed'):
        self.output(f'Run {status} | {time.monotonic() - self.started:.2f}s | '
                    f'model calls: {self.calls} ({self.completed} responses) | tools: {self.tools} | '
                    f'edits: {self.applied} applied, {self.rejected} rejected')
        checks = [*self.checks, *[(name, 'not run') for name in ('test', 'lint', 'security', 'dependency_audit')
                                 if not any(seen == name for seen, _ in self.checks)]]
        self.output('Checks: ' + '; '.join(f'{name}: {status}' for name, status in checks))
        fields = []
        for key, label in [('input_tokens', 'input'), ('output_tokens', 'output'), ('cached_input_tokens', 'cached input')]:
            values = [u[key] for u in self.usage if u is not None and type(u.get(key)) is int and u[key] >= 0]
            fields.append(f'{label}: {sum(values)} reported ({len(values)}/{self.completed} responses)'
                          if values else f'{label}: unknown')
        self.output('Tokens: ' + '; '.join(fields))
        self.output(f'Context: {self.selected_chars} cumulative selected characters (not tokens)')


class EventFanout:
    def __init__(self, *sinks):
        self.sinks = tuple(sink for sink in sinks if sink is not None)

    def emit(self, kind, data):
        for sink in self.sinks:
            sink.emit(kind, data)

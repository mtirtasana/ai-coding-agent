"""Editor JSON-RPC over UTF-8 JSON lines. stdout is exclusively protocol traffic."""
import json
import math
import sys
import threading
from concurrent.futures import Future
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

from agent.adapters.execution import LocalCommandRunner
from agent.adapters.models import DemoProvider, OllamaProvider, OpenAICompatibleProvider, OpenAIProvider, responses_items
from agent.adapters.tools import ToolRegistry, spec, workspace_tools
from agent.core.cancellation import CancellationToken, RunCancelled
from agent.core.context import ContextLimitError, split_turns
from agent.core.engine import Agent, TokenBudgetError
from agent.core.types import Continuation, ConversationState, Message, ToolCall, ToolResult
from agent.files import Workspace
from agent.adapters.usage import UsageJournal, MeteredProvider
from agent.adapters.verification import VerifiedTools, workspace_fingerprint
from agent.adapters.recovery import RecoveryStore

PROTOCOL_VERSION = 2
MAX_FRAME = 16 * 1024 * 1024
PROFILES = {'test': 'run_tests', 'lint': 'run_lint', 'security': 'run_security',
            'dependency_audit': 'run_dependency_audit'}


def restore_state(value):
    if not isinstance(value, dict) or set(value) != {'messages', 'latest_plan', 'accepted_plan'}:
        raise ValueError('Invalid checkpoint.')
    messages = []
    if not isinstance(value['messages'], list):
        raise ValueError('Invalid messages.')
    for item in value['messages']:
        if not isinstance(item, dict) or set(item) not in (
                {'role', 'content', 'tool_calls', 'call_id'},
                {'role', 'content', 'tool_calls', 'call_id', 'continuation'}):
            raise ValueError('Invalid message.')
        if not isinstance(item['role'], str) or not isinstance(item['content'], str):
            raise ValueError('Invalid message text.')
        calls = []
        if not isinstance(item['tool_calls'], list):
            raise ValueError('Invalid calls.')
        for call in item['tool_calls']:
            if not isinstance(call, dict) or set(call) != {'id', 'name', 'arguments'} or any(
                    not isinstance(v, str) for v in call.values()):
                raise ValueError('Invalid call.')
            calls.append(ToolCall(**call))
        if item['call_id'] is not None and not isinstance(item['call_id'], str):
            raise ValueError('Invalid call reference.')
        raw = item.get('continuation')
        continuation = None
        if raw is not None:
            if (not isinstance(raw, dict) or set(raw) != {'format', 'payload'}
                    or any(not isinstance(v, str) for v in raw.values())):
                raise ValueError('Invalid continuation.')
            continuation = Continuation(**raw)
        message = Message(item['role'], item['content'], tuple(calls), item['call_id'], continuation)
        if continuation is not None:
            responses_items(message)
        messages.append(message)
    if messages:
        if any(m.role == 'system' for m in messages) or messages[-1].role != 'assistant' or messages[-1].tool_calls:
            raise ValueError('Only completed turns can be restored.')
        split_turns(messages)
    for name in ('latest_plan', 'accepted_plan'):
        if value[name] is not None and not isinstance(value[name], str):
            raise ValueError('Invalid plan.')
    return ConversationState(messages, value['latest_plan'], value['accepted_plan'])


def make_provider(config, *, completion=False):
    name = config['provider']
    model = config.get('model') or ('gpt-4.1' if name == 'openai' else '')
    options = {'timeout': 5, 'max_output_tokens': 256} if completion else {
        'timeout': config.get('requestTimeout', 120), 'max_output_tokens': config.get('maxOutputTokens', 4096)}
    if name == 'demo':
        return DemoProvider()
    if name == 'ollama' and model:
        return OllamaProvider(model, config.get('baseUrl') or 'http://localhost:11434', **options)
    key = config.get('apiKey')
    if not isinstance(key, str) or not key:
        raise ValueError('API key is required.')
    if name == 'openai':
        from openai import OpenAI
        return OpenAIProvider(model, client=OpenAI(api_key=key, timeout=options['timeout'],
                                                   max_retries=0), **options)
    if name == 'openai-compatible' and model:
        return OpenAICompatibleProvider(model, config.get('baseUrl') or '', key, **options)
    raise ValueError('Unsupported provider or missing model.')


class EditorTools(ToolRegistry):
    def __init__(self, workspace, bridge, checks, token):
        super().__init__()
        self.workspace, self.bridge, self.token = workspace, bridge, token
        from agent.adapters.check_output import CheckOutputStore, register_output_reader
        outputs = CheckOutputStore()
        for tool in workspace_tools(workspace).specifications():
            def handler(_name=tool.name, **args):
                # Apply the same Python path guard before the independently validated editor callback.
                self.workspace.path(args.get('path', '.'))
                self.token.check()
                result = self.bridge('editor/tool', {'name': _name, 'arguments': args})
                self.token.check()
                if not isinstance(result, dict) or type(result.get('ok')) is not bool or not isinstance(result.get('content'), str):
                    raise ValueError('Invalid editor tool result.')
                return ToolResult(result['ok'], result['content'])
            self.register(tool, handler)
        for profile, config in checks.items():
            def run(_profile=profile, _config=config):
                permission = self.bridge('editor/beforeCheck', {'profile': _profile})
                self.token.check()
                if permission is not True:
                    report = {'check': _profile, 'passed': False, 'not_run': True,
                              'reason': 'permission_or_save_declined'}
                    self.bridge('editor/checkResult', {'profile': _profile, 'result': report})
                    return ToolResult(False, json.dumps(report))
                result = LocalCommandRunner(workspace.root, _config['command'],
                                          _config.get('timeout', 120 if _profile == 'dependency_audit' else 60),
                                          _config.get('outputLimit', 65536), check=_profile,
                                          cancellation=self.token).as_tool()
                self.bridge('editor/checkResult', {'profile': _profile, 'result': json.loads(result.content)})
                return outputs.present(result)
            self.register(spec(PROFILES[profile], 'Run the configured check after editor permission and save checks.', {}), run)
        if checks:
            register_output_reader(self, outputs)


class Server:
    def __init__(self, output, provider_factory=make_provider):
        self.output, self.provider_factory = output, provider_factory
        self._write_lock = threading.Lock()
        self._lock = threading.RLock()
        self.pending = {}
        self.serial = 0
        self.worker = None
        self.token = CancellationToken()
        self.config = None
        self.state = ConversationState()
        self.closed = False
        self.session_id = str(uuid4())
        self.usage = UsageJournal(warn=lambda message: self.emit('usage_warning', {'message': message}))
        self.recovery = RecoveryStore(warn=lambda message: self.emit('recovery_warning', {'message': message}))
        self.verification_records = {}

    def send(self, value):
        frame = json.dumps({'jsonrpc': '2.0', **value}, ensure_ascii=True)
        if len(frame.encode('utf-8')) > MAX_FRAME:
            raise ValueError('Protocol frame limit exceeded.')
        with self._write_lock:
            if not self.closed:
                self.output.write(frame + '\n')
                self.output.flush()

    def emit(self, kind, data):
        self.send({'method': 'agent/event', 'params': {'kind': kind, 'data': data}})

    def editor(self, method, params):
        self.token.check()
        with self._lock:
            self.token.check()
            self.serial += 1
            request_id = f'editor-{self.serial}'
            future = Future()
            self.pending[request_id] = future
        try:
            self.send({'id': request_id, 'method': method, 'params': params})
            # Human diff/check reviews may take time, but cannot wait forever.
            return future.result(timeout=600)
        finally:
            with self._lock:
                self.pending.pop(request_id, None)

    def cancel(self):
        self.token.cancel()
        with self._lock:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RunCancelled('Editor request cancelled.'))
        return {'cleanupComplete': self.token.wait_for_cleanup(8), 'restartRequired': True}

    def initialize(self, params):
        allowed = {'protocolVersion', 'workspace', 'provider', 'model', 'baseUrl', 'apiKey', 'checks',
                   'maxSteps', 'maxContextChars', 'checkpoint', 'sessionId', 'persistSession', 'requestTimeout', 'maxOutputTokens',
                   'contextMode', 'maxContextTokens', 'maxRunTokens'}
        if set(params) - allowed or type(params.get('protocolVersion')) is not int or params.get('protocolVersion') != PROTOCOL_VERSION:
            raise ValueError('Incompatible protocol or configuration.')
        if params.get('provider') not in {'demo', 'openai', 'ollama', 'openai-compatible'}:
            raise ValueError('Unknown provider.')
        for key in ('workspace', 'provider', 'model', 'baseUrl', 'apiKey'):
            if key in params and not isinstance(params[key], str):
                raise ValueError('Expected string setting.')
        workspace = Workspace(Path(params['workspace']), allow_write=True)
        checks = params.get('checks', {})
        if not isinstance(checks, dict) or set(checks) - set(PROFILES):
            raise ValueError('Unknown check profile.')
        for profile, config in checks.items():
            if not isinstance(config, dict) or set(config) - {'command', 'timeout', 'outputLimit'}:
                raise ValueError('Invalid check configuration.')
            LocalCommandRunner(workspace.root, config['command'], config.get('timeout', 60),
                               config.get('outputLimit', 65536), check=profile)
        for key, default in (('maxSteps', 15), ('maxContextChars', 200000)):
            if type(params.get(key, default)) is not int or params.get(key, default) < 1:
                raise ValueError('Invalid budget.')
        for key in ('maxContextTokens', 'maxRunTokens'):
            if key in params and (type(params[key]) is not int or params[key] < 1):
                raise ValueError('Invalid token budget.')
        if params.get('contextMode', 'recent') not in {'recent', 'compact', 'full'}:
            raise ValueError('Invalid context mode.')
        state = restore_state(params['checkpoint']) if 'checkpoint' in params else ConversationState()
        timeout = params.get('requestTimeout', 120)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
            raise ValueError('Invalid provider timeout.')
        if type(params.get('maxOutputTokens', 4096)) is not int or not 1 <= params.get('maxOutputTokens', 4096) <= 100000:
            raise ValueError('Invalid output limit.')
        if type(params.get('persistSession', False)) is not bool:
            raise ValueError('Invalid recovery setting.')
        if 'sessionId' in params:
            if not isinstance(params['sessionId'], str) or str(UUID(params['sessionId'])) != params['sessionId']:
                raise ValueError('Invalid usage session identifier.')
            self.session_id = params['sessionId']
        self.config, self.workspace, self.state = params, workspace, state
        self.verification_records.clear()
        return {'protocolVersion': PROTOCOL_VERSION, 'backendVersion': '0.12.0'}

    def run(self, params):
        if set(params) != {'task', 'mode'} or not isinstance(params['task'], str) or not params['task'].strip():
            raise ValueError('Expected task and mode.')
        if params['mode'] not in {'plan', 'implement'} or len(params['task']) > 100000:
            raise ValueError('Invalid mode or task size.')
        instructions = self.editor('editor/instructions', {})
        if not isinstance(instructions, str) or len(instructions) > 100000:
            raise ValueError('Invalid project instructions.')
        model = self.meter(self.provider_factory(self.config), 'agent')
        from agent.adapters.tokens import ModelTokenEstimator
        from agent.core.context import RuleBasedContextBuilder
        tools = EditorTools(self.workspace, self.editor, self.config.get('checks', {}), self.token)
        verified = VerifiedTools(tools, lambda: workspace_fingerprint(self.workspace),
                                 configuration=self.config.get('checks', {}), records=self.verification_records)
        result = Agent(model, verified, self, mode=params['mode'], project_instructions=instructions,
                       max_steps=self.config.get('maxSteps', 15),
                       max_context_chars=self.config.get('maxContextChars', 200000), cancellation=self.token,
                       context_builder=RuleBasedContextBuilder(self.config.get('contextMode', 'recent')),
                       max_context_tokens=self.config.get('maxContextTokens'), max_run_tokens=self.config.get('maxRunTokens'),
                       output_token_reserve=self.config.get('maxOutputTokens', 4096),
                       token_estimator=ModelTokenEstimator(self.config['provider'], self.config.get('model', '')),
                       verification=verified).run(
                           params['task'], self.state)
        self.token.check()
        if self.config.get('persistSession'):
            try:
                self.recovery.save(self.session_id, self.workspace, self.state,
                                   {'provider': self.config['provider'], 'profile': 'editor', 'model': self.config.get('model', '')})
            except (OSError, ValueError):
                self.emit('recovery_warning', {'message': 'Snapshot could not be saved; conversation remains in memory.'})
        return {'text': result.text, 'steps': result.steps, 'checkpoint': asdict(self.state),
                'requests': [asdict(request) for request in result.requests],
                'verification': [asdict(item) for item in result.verification]}

    def complete(self, params):
        if set(params) != {'prefix', 'suffix', 'language'} or any(not isinstance(v, str) for v in params.values()):
            raise ValueError('Invalid completion request.')
        if len(params['prefix']) > 6000 or len(params['suffix']) > 2000 or len(params['language']) > 100:
            raise ValueError('Completion context limit exceeded.')
        if self.config['provider'] == 'demo':
            return {'text': '', 'usage': None}
        model = self.meter(self.provider_factory(self.config, completion=True), 'completion')
        response = model.generate([Message('system', 'Return only the code to insert at <CURSOR>. No explanation, tools or markdown.'),
                                   Message('user', f"Language: {params['language']}\n{params['prefix']}<CURSOR>{params['suffix']}")], [])
        self.token.check()
        text = response.message.content
        if response.truncated or response.message.tool_calls or '```' in text:
            text = ''
        return {'text': text[:2000], 'usage': None if response.usage is None else asdict(response.usage)}

    def meter(self, provider, kind):
        model = self.config.get('model') or ('gpt-4.1' if self.config['provider'] == 'openai' else self.config['provider'])
        return MeteredProvider(provider, self.usage, self.session_id, provider_name=self.config['provider'],
                               model=model, profile='editor', interface='vscode', kind=kind)

    def handle(self, message):
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
            self.send({'id': None, 'error': {'code': -32600, 'message': 'Invalid JSON-RPC message.'}})
            return
        request_id = message.get('id')
        if 'method' not in message:
            with self._lock:
                future = self.pending.get(request_id)
                if future is not None and not future.done():
                    if 'error' in message:
                        future.set_exception(ValueError('Editor operation failed.'))
                    else:
                        future.set_result(message.get('result'))
            return
        if type(request_id) not in (str, int) or not isinstance(message.get('params', {}), dict):
            self.send({'id': None, 'error': {'code': -32600, 'message': 'Invalid request.'}})
            return
        method, params = message['method'], message.get('params', {})
        try:
            if method in {'diagnostics/query', 'session/listSaved', 'session/resumeSaved', 'session/deleteSaved'}:
                if method == 'diagnostics/query':
                    if set(params) - {'checks'}:
                        raise ValueError('Diagnostics accepts no provider data.')
                    checks = params.get('checks', {})
                    if not isinstance(checks, dict) or set(checks) - set(PROFILES) or any(
                            not isinstance(command, list) or not command or len(command) > 128 or
                            any(not isinstance(item, str) or len(item) > 4096 for item in command) for command in checks.values()):
                        raise ValueError('Invalid diagnostic check definitions.')
                    from agent.adapters.diagnostics import diagnose
                    result = diagnose(checks)
                else:
                    expected = {'workspace'} if method == 'session/listSaved' else {'workspace', 'sessionId'}
                    if set(params) != expected or not isinstance(params['workspace'], str):
                        raise ValueError('Invalid recovery request.')
                    workspace = Workspace(Path(params['workspace']))
                    if method == 'session/listSaved':
                        result = self.recovery.list(workspace)
                    elif method == 'session/deleteSaved':
                        self.recovery.delete(params['sessionId'], workspace)
                        result = {'deleted': True}
                    else:
                        state, connection, changed = self.recovery.resume(params['sessionId'], workspace)
                        result = {'checkpoint': asdict(state), 'connection': connection, 'workspaceChanged': changed}
                self.send({'id': request_id, 'result': result})
                return
            if method == 'usage/pending':
                if params:
                    raise ValueError('Unexpected pending-usage parameters.')
                self.send({'id': request_id, 'result': self.usage.volatile_calls()})
                return
            if method == 'usage/query':
                options = dict(params)
                volatile = options.pop('volatileCalls', [])
                if not isinstance(volatile, list) or len(volatile) > 10000:
                    raise ValueError('Invalid volatile usage metadata.')
                self.send({'id': request_id, 'result': self.usage.query(options, self.session_id, volatile)})
                return
            if method == 'cancel':
                self.send({'id': request_id, 'result': self.cancel()})
                return
            if self.worker is not None and self.worker.is_alive():
                self.send({'id': request_id, 'error': {'code': -32001, 'message': 'Worker is busy.'}})
                return
            if method == 'initialize':
                result = self.initialize(params)
            elif self.config is None:
                raise ValueError('Initialize first.')
            elif method == 'session/acceptPlan':
                self.state.accepted_plan = self.state.latest_plan
                result = {'checkpoint': asdict(self.state)}
            elif method == 'session/reset':
                self.state = ConversationState()
                self.session_id = str(uuid4())
                self.verification_records.clear()
                result = {'checkpoint': asdict(self.state)}
            elif method in {'session/run', 'completion/generate'}:
                self.token = CancellationToken()
                def work():
                    try:
                        result = self.run(params) if method == 'session/run' else self.complete(params)
                        self.send({'id': request_id, 'result': result})
                    except RunCancelled:
                        self.send({'id': request_id, 'error': {'code': -32800, 'message': 'Run cancelled; accepted edits may remain.'}})
                    except Exception as exc:
                        # Never serialize raw provider exceptions: they may contain credentials or request bodies.
                        message = ('Run token budget reached before another request. Inspect existing changes or increase maxRunTokens.'
                                   if isinstance(exc, TokenBudgetError) else
                                   'Context budget exceeded. Reduce input or review context limits and the output reserve.'
                                   if isinstance(exc, ContextLimitError) else
                                   f'Agent operation failed ({type(exc).__name__}).')
                        self.send({'id': request_id, 'error': {'code': -32000, 'message': message}})
                self.worker = threading.Thread(target=work, daemon=True)
                self.worker.start()
                return
            else:
                self.send({'id': request_id, 'error': {'code': -32601, 'message': 'Unknown method.'}})
                return
            self.send({'id': request_id, 'result': result})
        except (ValueError, KeyError, TypeError, OSError):
            self.send({'id': request_id, 'error': {'code': -32602, 'message': 'Invalid request or backend configuration.'}})

    def close(self):
        self.cancel()
        self.closed = True


def main():
    # Strict UTF-8 independent of the Windows console encoding.
    if hasattr(sys.stdin, 'reconfigure'):
        sys.stdin.reconfigure(encoding='utf-8')
        sys.stdout.reconfigure(encoding='utf-8')
    server = Server(sys.stdout)
    try:
        while True:
            line = sys.stdin.readline(MAX_FRAME + 1)
            if not line:
                break
            if len(line.encode('utf-8')) > MAX_FRAME or not line.endswith('\n'):
                server.send({'id': None, 'error': {'code': -32600, 'message': 'Frame too large or incomplete.'}})
                break
            try:
                def reject_constant(_):
                    raise ValueError('Non-standard JSON constant.')
                message = json.loads(line, parse_constant=reject_constant)
                server.handle(message)
            except (ValueError, TypeError):
                server.send({'id': None, 'error': {'code': -32700, 'message': 'Invalid JSON.'}})
    finally:
        server.close()


if __name__ == '__main__':
    main()

"""Opt-in live evaluations. Reports contain metrics, never raw conversations/errors."""
import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from agent.adapters.execution import LocalTestRunner
from agent.adapters.instructions import load_project_instructions
from agent.adapters.models import OpenAIProvider, OllamaProvider, OpenAICompatibleProvider
from agent.adapters.tools import workspace_tools
from agent.core.engine import Agent
from agent.files import Workspace


class Unavailable(RuntimeError):
    pass


class Metrics:
    def __init__(self):
        self.events = []

    def emit(self, kind, data):
        self.events.append((kind, data))

    def report(self):
        contexts = [data for kind, data in self.events if kind == 'context_selected']
        usages = [data['usage'] for kind, data in self.events if kind == 'model_completed']
        counts = {}
        for field in ['input_tokens', 'output_tokens', 'cached_input_tokens']:
            known = [usage[field] for usage in usages if usage is not None and usage[field] is not None]
            counts[field] = {'reported_sum': sum(known) if known else None,
                             'responses_with_value': len(known)}
        return {'model_calls': len(contexts),
                'completed_responses': len(usages),
                'full_context_chars': sum(c['full_chars'] for c in contexts),
                'selected_context_chars': sum(c['selected_chars'] for c in contexts),
                'tool_calls': sum(kind == 'tool_completed' for kind, _ in self.events),
                'usage': counts}


def make_provider(profile):
    provider, model = profile['provider'], profile.get('model')
    try:
        if provider == 'openai':
            if not os.getenv('OPENAI_API_KEY'):
                raise Unavailable('missing_credentials')
            return OpenAIProvider(model or 'gpt-4.1')
        if provider == 'ollama':
            if not model:
                raise Unavailable('missing_model')
            return OllamaProvider(model, profile.get('base_url') or 'http://localhost:11434')
        if provider == 'openai-compatible':
            key = os.getenv(profile.get('api_key_env') or 'OPENAI_COMPATIBLE_API_KEY')
            if not model or not profile.get('base_url') or not key:
                raise Unavailable('missing_compatible_configuration')
            return OpenAICompatibleProvider(model, profile['base_url'], key)
    except ImportError:
        raise Unavailable('missing_optional_sdk') from None
    raise ValueError('Unsupported evaluation provider.')


TASKS = {
    'explain': 'Explain this project. Name calc.py, the add function, and the tests directory. Do not edit files.',
    'change_function': 'Fix add(a, b) in calc.py to return the sum. Preserve multiply and tests. Preview targeted edits if available.',
    'repair_test': 'Find and fix the failing multiply test by correcting calc.py, not the tests. Run the configured tests when available.',
}


def create_fixture(parent, task):
    root = parent / 'workspace'
    root.mkdir()
    (root / 'tests').mkdir()
    add = 'a - b' if task == 'change_function' else 'a + b'
    multiply = 'a + b' if task == 'repair_test' else 'a * b'
    (root / 'calc.py').write_text(f'def add(a, b):\n    return {add}\n\ndef multiply(a, b):\n    return {multiply}\n', encoding='utf-8')
    tests = ("import unittest\nfrom calc import add, multiply\n\nclass CalcTests(unittest.TestCase):\n"
             "    def test_add(self):\n        self.assertEqual(add(3, 2), 5)\n"
             "    def test_multiply(self):\n        self.assertEqual(multiply(3, 2), 6)\n")
    (root / 'tests/test_calc.py').write_text(tests, encoding='utf-8')
    (root / 'AGENTS.md').write_text('Keep tests unchanged. Prefer targeted edits. Verify changes when execution is available.\n', encoding='utf-8')
    # This checker is outside the agent's workspace and is never exposed as a tool command.
    checker = parent / 'verify.py'
    checker.write_text(
        "import runpy, sys\nfrom pathlib import Path\n"
        "root = Path(sys.argv[1])\nmodule = runpy.run_path(str(root / 'calc.py'))\n"
        "for a, b in [(3, 2), (0, 5), (-2, 4), (7, -3)]:\n"
        "    assert module['add'](a, b) == a + b\n"
        "    assert module['multiply'](a, b) == a * b\n"
        "print('independent checks passed')\n", encoding='utf-8')
    originals = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in root.rglob('*') if path.is_file()}
    return root, checker, originals


def unchanged(root, originals, names=None):
    for name in originals if names is None else names:
        path = root / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != originals[name]:
            return False
    if names is None:
        actual = {str(path.relative_to(root)) for path in root.rglob('*') if path.is_file()}
        return actual == set(originals)
    return True


def evaluate_profile(profile, *, live=False, allow_exec=False, toolset='baseline',
                     max_steps=15, max_context_chars=200000, model_factory=None):
    if toolset != 'baseline':
        raise ValueError('Invalid evaluation toolset.')
    factory = make_provider if model_factory is None else model_factory
    results = []
    for task, prompt in TASKS.items():
        metrics = Metrics()
        row = {'provider': profile['provider'], 'name': profile.get('name', profile['provider']),
               'model': profile.get('model') or ('gpt-4.1' if profile['provider'] == 'openai' else None),
               'toolset': toolset, 'task': task, 'status': 'not_run', 'reason': None}
        started = time.monotonic()
        try:
            if not live:
                raise Unavailable('live_not_enabled')
            if task != 'explain' and not allow_exec:
                raise Unavailable('local_verification_not_enabled')
            model = factory(profile)
            with tempfile.TemporaryDirectory(prefix='coding-agent-eval-') as directory:
                root, checker, originals = create_fixture(Path(directory), task)
                workspace = Workspace(root, allow_write=task != 'explain')
                agent = Agent(model, workspace_tools(workspace),
                              metrics, max_steps=max_steps, max_context_chars=max_context_chars,
                              mode='plan' if task == 'explain' else 'implement',
                              project_instructions=load_project_instructions(workspace).content)
                result = agent.run(prompt)
                if task == 'explain':
                    passed = all(word in result.text.lower() for word in ['calc.py', 'add', 'tests'])
                    passed = passed and unchanged(root, originals)
                else:
                    verification = LocalTestRunner(root, [sys.executable, str(checker), str(root)],
                                                   timeout=10, output_limit=8192).run()
                    passed = verification['passed'] and unchanged(root, originals, ['tests/test_calc.py', 'AGENTS.md'])
                row.update(status='passed' if passed else 'failed',
                           reason=None if passed else 'independent_verification_failed')
        except Unavailable as exc:
            row['reason'] = str(exc)  # Only controlled constant reasons enter this exception.
        except Exception as exc:
            unavailable = isinstance(exc, (ConnectionError, TimeoutError)) or type(exc).__name__ in {
                'URLError', 'APIConnectionError', 'APITimeoutError'}
            no_response = not any(kind == 'model_completed' for kind, _ in metrics.events)
            row.update(status='not_run' if unavailable and no_response else 'failed',
                       reason='endpoint_unavailable' if unavailable and no_response else 'execution_error',
                       error_type=type(exc).__name__)
        row.update(elapsed_seconds=round(time.monotonic() - started, 6), **metrics.report())
        results.append(row)
    return results


def main():
    parser = argparse.ArgumentParser(description='Opt-in coding-agent evaluation; local tests are NOT sandboxed.')
    parser.add_argument('--profiles', type=Path, help='JSON list: provider, model, base_url, api_key_env, name')
    parser.add_argument('--live', action='store_true', help='Permit live model requests (may incur API costs)')
    parser.add_argument('--allow-exec', action='store_true', help='Permit fixture and independent checks on the HOST')
    parser.add_argument('--toolset', choices=['baseline'], default='baseline')
    parser.add_argument('--max-steps', type=int, default=15)
    parser.add_argument('--max-context-chars', type=int, default=200000)
    parser.add_argument('--output', type=Path, help='Write a metadata-only JSON report instead of stdout')
    args = parser.parse_args()
    if args.max_steps < 1 or args.max_context_chars < 1:
        parser.error('Limits must be positive.')
    try:
        profiles = (json.loads(args.profiles.read_text(encoding='utf-8')) if args.profiles else
                    [{'provider': 'openai'}, {'provider': 'ollama'}, {'provider': 'openai-compatible'}])
        allowed = {'provider', 'model', 'base_url', 'api_key_env', 'name'}
        if (not isinstance(profiles, list) or not profiles or any(
                not isinstance(p, dict) or set(p) - allowed or
                p.get('provider') not in {'openai', 'ollama', 'openai-compatible'} or
                any(not isinstance(v, str) or not v for v in p.values()) for p in profiles)):
            parser.error('Profiles must be a nonempty list of provider configurations with string values; use key environment names, never keys.')
        results = []
        for profile in profiles:
            for toolset in (['baseline', 'workflow'] if args.toolset == 'both' else [args.toolset]):
                results.extend(evaluate_profile(profile, live=args.live, allow_exec=args.allow_exec,
                                                toolset=toolset, max_steps=args.max_steps,
                                                max_context_chars=args.max_context_chars))
        report = json.dumps({'report_version': 1, 'results': results}, indent=2)
        if args.output:
            args.output.write_text(report + '\n', encoding='utf-8')
        else:
            print(report)
        if any(row['status'] == 'failed' for row in results):
            parser.exit(1)
    except KeyboardInterrupt:
        parser.exit(130, 'Evaluation interrupted; no automatic retry.\n')
    except (OSError, ValueError) as exc:
        parser.exit(2, f'Evaluation configuration/report error: {type(exc).__name__}\n')


if __name__ == '__main__':
    main()

"""Opt-in live evaluations. Reports contain metrics, never raw conversations/errors."""
import argparse
import ast
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
from agent.core.context import RuleBasedContextBuilder
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

READ_TASKS = {
    'large_file': 'Fix add(a, b) in calc.py to return the sum. Preserve all unrelated functions and tests. Run the configured tests.',
    'multi_file': 'Fix subtotal in pricing.py and total in invoice.py: subtotal is price times quantity, and total adds shipping. Preserve tests. Run them.',
    'surrounding_context': 'Fix net_price in discounts.py to return the remaining price after applying the existing clamp_discount policy. Inspect the policy and its limits; preserve them and the tests. Run the tests.',
}


def read_fixture_structure(root, task):
    targets = {'large_file': {'calc.py': 'add'},
               'multi_file': {'pricing.py': 'subtotal', 'invoice.py': 'total'},
               'surrounding_context': {'discounts.py': 'net_price'}}[task]
    result = {}
    for name, function in targets.items():
        tree = ast.parse((root / name).read_text(encoding='utf-8'))
        tree.body = [node for node in tree.body if not (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function)]
        result[name] = ast.dump(tree, include_attributes=False)
    return result


def create_read_fixture(parent, task):
    """Identical source/checks for full and range strategies; no live quality claim."""
    root = parent / 'workspace'
    (root / 'tests').mkdir(parents=True)
    padding = ''.join(f'def unrelated_{i}(x):\n    return x + {i}\n\n' for i in range(180))
    if task == 'large_file':
        sources = {'calc.py': padding + 'def add(a, b):\n    return a - b\n'}
        imports = 'from calc import add, unrelated_0, unrelated_179\n'
        basic = 'assert add(3, 2) == 5\n'
        extra = ('for a, b in [(0, 5), (-2, 4), (7, -3)]:\n    assert add(a, b) == a + b\n'
                 'assert unrelated_0(4) == 4\nassert unrelated_179(4) == 183\n')
    elif task == 'multi_file':
        sources = {'pricing.py': 'def subtotal(price, quantity):\n    return price + quantity\n',
                   'invoice.py': 'from pricing import subtotal\n\ndef total(price, quantity, shipping):\n    return subtotal(price, quantity) - shipping\n'}
        imports = 'from pricing import subtotal\nfrom invoice import total\n'
        basic = 'assert subtotal(4, 3) == 12\nassert total(4, 3, 2) == 14\n'
        extra = ('for p, q, s in [(0, 5, 2), (2.5, 4, 1), (7, 0, 3)]:\n'
                 '    assert subtotal(p, q) == p * q\n    assert total(p, q, s) == p * q + s\n')
    elif task == 'surrounding_context':
        sources = {'discounts.py': 'MAX_DISCOUNT = 25\n\ndef clamp_discount(percentage):\n    return min(MAX_DISCOUNT, max(0, percentage))\n\n'
                   + padding + 'def net_price(price, percentage):\n    return price * percentage / 100\n'}
        imports = 'from discounts import net_price, clamp_discount, MAX_DISCOUNT, unrelated_179\n'
        basic = 'assert net_price(100, 10) == 90\n'
        extra = ('for p, d in [(80, -5), (80, 0), (80, 25), (80, 90), (0, 10)]:\n'
                 '    assert net_price(p, d) == p * (100 - min(25, max(0, d))) / 100\n'
                 'assert MAX_DISCOUNT == 25\nassert clamp_discount(-5) == 0\n'
                 'assert clamp_discount(90) == 25\nassert unrelated_179(4) == 183\n')
    else:
        raise ValueError('Unknown read-comparison task.')
    for name, source in sources.items():
        (root / name).write_text(source, encoding='utf-8')
    (root / 'tests/test_project.py').write_text(
        'import unittest\n' + imports + '\nclass ProjectTests(unittest.TestCase):\n    def test_behavior(self):\n'
        + ''.join('        ' + line + '\n' for line in basic.splitlines()), encoding='utf-8')
    (root / 'AGENTS.md').write_text('Preserve tests and unrelated code. Inspect dependencies before editing. Verify changes.\n', encoding='utf-8')
    # Run from the workspace, but keep the independent checker outside tool access.
    checker = parent / 'verify.py'
    checker.write_text('import sys\nsys.path.insert(0, sys.argv[1])\n' + imports + basic + extra,
                       encoding='utf-8')
    originals = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in root.rglob('*') if p.is_file()}
    return root, checker, originals


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
    originals = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in root.rglob('*') if path.is_file()}
    return root, checker, originals


def unchanged(root, originals, names=None):
    for name in originals if names is None else names:
        path = root / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != originals[name]:
            return False
    if names is None:
        actual = {path.relative_to(root).as_posix() for path in root.rglob('*') if path.is_file()}
        return actual == set(originals)
    return True


def evaluate_profile(profile, *, live=False, allow_exec=False, toolset='workflow',
                     max_steps=15, max_context_chars=200000, model_factory=None,
                     read_strategy='ranges', suite='basic', context_mode='recent'):
    builder = RuleBasedContextBuilder(context_mode)
    if toolset not in {'baseline', 'workflow'}:
        raise ValueError('Invalid evaluation toolset.')
    if read_strategy not in {'full', 'ranges'} or suite not in {'basic', 'read_comparison'}:
        raise ValueError('Invalid read strategy or suite.')
    if suite == 'read_comparison' and toolset != 'workflow':
        raise ValueError('Read comparison requires the workflow toolset.')
    factory = make_provider if model_factory is None else model_factory
    results = []
    for task, prompt in (READ_TASKS if suite == 'read_comparison' else TASKS).items():
        metrics = Metrics()
        row = {'provider': profile['provider'], 'name': profile.get('name', profile['provider']),
               'model': profile.get('model') or ('gpt-4.1' if profile['provider'] == 'openai' else None),
               'toolset': toolset, 'read_strategy': read_strategy, 'suite': suite, 'context_mode': context_mode,
               'task': task, 'status': 'not_run', 'reason': None}
        started = time.monotonic()
        try:
            if not live:
                raise Unavailable('live_not_enabled')
            if task != 'explain' and not allow_exec:
                raise Unavailable('local_verification_not_enabled')
            model = factory(profile)
            with tempfile.TemporaryDirectory(prefix='coding-agent-eval-') as directory:
                fixture = create_read_fixture if suite == 'read_comparison' else create_fixture
                root, checker, originals = fixture(Path(directory), task)
                structure = read_fixture_structure(root, task) if suite == 'read_comparison' else None
                workspace = Workspace(root, allow_write=task != 'explain')
                runner = (LocalTestRunner(root, [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-v'])
                          if toolset == 'workflow' and allow_exec else None)
                agent = Agent(model, workspace_tools(workspace, enhanced=toolset == 'workflow', test_runner=runner,
                                                    range_reads=read_strategy == 'ranges'),
                              metrics, max_steps=max_steps, max_context_chars=max_context_chars,
                              mode='plan' if task == 'explain' else 'implement', context_builder=builder,
                              project_instructions=load_project_instructions(workspace).content)
                result = agent.run(prompt)
                if task == 'explain':
                    passed = all(word in result.text.lower() for word in ['calc.py', 'add', 'tests'])
                    passed = passed and unchanged(root, originals)
                else:
                    verification = LocalTestRunner(root, [sys.executable, '-B', str(checker), str(root)],
                                                   timeout=10, output_limit=8192).run()
                    protected = [name for name in originals if name.startswith('tests/') or name == 'AGENTS.md']
                    passed = verification['passed'] and unchanged(root, originals, protected)
                    if structure is not None:
                        passed = passed and read_fixture_structure(root, task) == structure
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


def compare_read_results(full, ranges, offset=0):
    """Paired evidence, not a price estimate. Null/partial token coverage stays explicit."""
    comparisons = []
    for index, (before, after) in enumerate(zip(full, ranges, strict=True)):
        if before['task'] != after['task']:
            raise ValueError('Mismatched comparison tasks.')
        passed = before['status'] == after['status'] == 'passed'
        chars = before['selected_context_chars']
        comparisons.append({'task': before['task'], 'full_result': offset + index,
                            'range_result': offset + len(full) + index, 'both_passed': passed,
                            'selected_context_char_reduction': (
                                round(1 - after['selected_context_chars'] / chars, 6) if passed and chars else None)})
    return comparisons


def main():
    parser = argparse.ArgumentParser(description='Opt-in coding-agent evaluation; local tests are NOT sandboxed.')
    parser.add_argument('--profiles', type=Path, help='JSON list: provider, model, base_url, api_key_env, name')
    parser.add_argument('--live', action='store_true', help='Permit live model requests (may incur API costs)')
    parser.add_argument('--allow-exec', action='store_true', help='Permit fixture and independent checks on the HOST')
    parser.add_argument('--toolset', choices=['baseline', 'workflow', 'both'], default='workflow')
    parser.add_argument('--compare-reads', action='store_true',
                        help='Pair full/range workflow reads on large-file, multi-file and dependency-context fixtures')
    parser.add_argument('--max-steps', type=int, default=15)
    parser.add_argument('--max-context-chars', type=int, default=200000)
    parser.add_argument('--context-mode', choices=['recent', 'compact', 'full'], default='recent')
    parser.add_argument('--output', type=Path, help='Write a metadata-only JSON report instead of stdout')
    args = parser.parse_args()
    if args.max_steps < 1 or args.max_context_chars < 1:
        parser.error('Limits must be positive.')
    if args.compare_reads and args.toolset != 'workflow':
        parser.error('--compare-reads requires --toolset workflow.')
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
        comparisons = []
        for profile in profiles:
            if args.compare_reads:
                paired = [evaluate_profile(profile, live=args.live, allow_exec=args.allow_exec,
                                           read_strategy=strategy, suite='read_comparison',
                                           max_steps=args.max_steps, max_context_chars=args.max_context_chars,
                                           context_mode=args.context_mode)
                          for strategy in ('full', 'ranges')]
                comparisons.extend(compare_read_results(*paired, offset=len(results)))
                results.extend(paired[0] + paired[1])
                continue
            for toolset in (['baseline', 'workflow'] if args.toolset == 'both' else [args.toolset]):
                results.extend(evaluate_profile(profile, live=args.live, allow_exec=args.allow_exec,
                                                toolset=toolset, max_steps=args.max_steps,
                                                max_context_chars=args.max_context_chars, context_mode=args.context_mode))
        report = json.dumps({'report_version': 1, 'results': results, 'read_comparisons': comparisons}, indent=2)
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

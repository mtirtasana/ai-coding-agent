import json
import os
import tempfile
import unittest
import io
from pathlib import Path
from unittest.mock import Mock, patch

from agent.core.types import Message, ModelResponse, ToolCall
from agent.evaluate import compare_read_results, create_read_fixture, evaluate_profile, main, read_fixture_structure


class FixtureModel:
    """A deterministic test double; never advertised as a live model result."""
    def __init__(self):
        self.step = 0
        self.edit = None
        self.test_outcomes = []

    def generate(self, messages, tools):
        task = next(message.content for message in messages if message.role == 'user')
        if 'Explain this project' in task:
            return ModelResponse(Message('assistant', 'calc.py defines add; tests contains verification.'))
        repair = 'multiply test' in task
        function = 'multiply' if repair else 'add'
        old = 'def multiply(a, b):\n    return a + b' if repair else 'return a - b'
        new = 'def multiply(a, b):\n    return a * b' if repair else 'return a + b'
        workflow = any(tool.name == 'file_snapshot' for tool in tools)
        step, self.step = self.step, self.step + 1
        if workflow:
            if step in [1, 6]:
                outer = json.loads(messages[-1].content)
                self.test_outcomes.append(json.loads(outer['content'])['passed'])
            if step in [0, 5]:
                name, args = 'run_tests', {}
            elif step == 1:
                name, args = 'search_text', {'path': '.', 'query': 'def ' + function}
            elif step == 2:
                name, args = 'file_snapshot', {'path': 'calc.py'}
            elif step == 3:
                snapshot = json.loads(json.loads(messages[-1].content)['content'])
                self.edit = {'path': 'calc.py', 'expected_sha256': snapshot['sha256'],
                             'old_text': old, 'new_text': new}
                name, args = 'preview_edit', self.edit
            elif step == 4:
                name, args = 'apply_edit', self.edit
            else:
                return ModelResponse(Message('assistant', 'Fixed and verified.'))
        else:
            if step == 0:
                name, args = 'read_file', {'path': 'calc.py'}
            elif step == 1:
                text = json.loads(messages[-1].content)['content']
                name, args = 'write_file', {'path': 'calc.py', 'content': text.replace(old, new)}
            else:
                return ModelResponse(Message('assistant', 'Fixed; external validation required.'))
        return ModelResponse(Message('assistant', tool_calls=(ToolCall(str(step), name, json.dumps(args)),)))


class ReadFixtureModel:
    """Scripted tool flow measures transport/context mechanics, not model intelligence."""
    def __init__(self):
        self.actions = None
        self.step = 0
        self.snapshot = None
        self.edit = None
        self.policy_seen = False

    def generate(self, messages, tools):
        prompt = next(m.content for m in messages if m.role == 'user')
        if self.actions is None:
            if 'subtotal in pricing.py' in prompt:
                targets = [('pricing.py', 'subtotal', 'return price + quantity', 'return price * quantity'),
                           ('invoice.py', 'total', '- shipping', '+ shipping')]
            elif 'net_price' in prompt:
                targets = [('discounts.py', 'net_price', 'return price * percentage / 100',
                            'return price * (100 - clamp_discount(percentage)) / 100')]
            else:
                targets = [('calc.py', 'add', 'return a - b', 'return a + b')]
            self.actions = [('test', None)]
            if 'net_price' in prompt:
                self.actions += [('search', ('discounts.py', 'clamp_discount', '', '')),
                                 ('policy', None)]
            for target in targets:
                self.actions += [(action, target) for action in ['search', 'read', 'preview', 'apply']]
            self.actions += [('test', None)]
        if self.step >= len(self.actions):
            self.assert_last_passed(messages)
            return ModelResponse(Message('assistant', 'Fixed and independently verifiable.'))
        action, target = self.actions[self.step]
        if action == 'test':
            name, args = 'run_tests', {}
        elif action == 'search':
            name, args = 'search_text', {'path': target[0], 'query': 'def ' + target[1] + '('}
        elif action in {'read', 'policy'}:
            found = json.loads(json.loads(messages[-1].content)['content'])['matches'][0]
            if any(t.name == 'read_file_range' for t in tools):
                name, args = 'read_file_range', {'path': found['path'], 'start_line': max(1, found['line'] - 4),
                                                'end_line': found['line'] + 8}
            else:
                name, args = 'file_snapshot', {'path': found['path']}
        elif action == 'preview':
            self.snapshot = json.loads(json.loads(messages[-1].content)['content'])
            assert target[2] in self.snapshot['content']
            if 'net_price' in prompt:
                assert self.policy_seen
            self.edit = {'path': target[0], 'expected_sha256': self.snapshot['sha256'],
                         'old_text': target[2], 'new_text': target[3]}
            name, args = 'preview_edit', self.edit
        else:
            name, args = 'apply_edit', self.edit
        if self.step and self.actions[self.step - 1][0] == 'policy':
            policy = json.loads(json.loads(messages[-1].content)['content'])['content']
            self.policy_seen = 'MAX_DISCOUNT = 25' in policy and 'def clamp_discount' in policy
        self.step += 1
        return ModelResponse(Message('assistant', tool_calls=(ToolCall(str(self.step), name, json.dumps(args)),)))

    @staticmethod
    def assert_last_passed(messages):
        assert json.loads(json.loads(messages[-1].content)['content'])['passed']


class EvaluationTests(unittest.TestCase):
    profile = {'provider': 'openai', 'model': 'simulated', 'name': 'unit-test-double'}

    def test_compact_context_preserves_scripted_repairs_and_independent_verification(self):
        rows = {}
        for mode in ('recent', 'compact'):
            rows[mode] = evaluate_profile(self.profile, live=True, allow_exec=True,
                                         model_factory=lambda profile: ReadFixtureModel(),
                                         suite='read_comparison', read_strategy='full', context_mode=mode)
            self.assertTrue(all(row['status'] == 'passed' for row in rows[mode]), rows[mode])
            self.assertTrue(all(row['usage']['input_tokens']['reported_sum'] is None for row in rows[mode]))
        before = sum(row['selected_context_chars'] for row in rows['recent'])
        after = sum(row['selected_context_chars'] for row in rows['compact'])
        self.assertLess(after, before)
        print(f'\nScripted compact comparison (characters, NOT live tokens): recent={before}, compact={after}')

    def test_baseline_and_complete_edit_verify_workflow(self):
        for toolset in ['baseline', 'workflow']:
            models = []
            def factory(profile):
                model = FixtureModel()
                models.append(model)
                return model
            results = evaluate_profile(self.profile, live=True, allow_exec=True,
                                       toolset=toolset, model_factory=factory)
            self.assertTrue(all(row['status'] == 'passed' for row in results), results)
            self.assertTrue(all(row['usage']['input_tokens']['reported_sum'] is None for row in results))
            self.assertGreater(results[-1]['selected_context_chars'], 0)
            if toolset == 'workflow':
                self.assertEqual(models[1].test_outcomes, [False, True])
                self.assertEqual(models[2].test_outcomes, [False, True])

    def test_no_live_opt_in_means_no_provider_creation(self):
        factory = Mock(side_effect=AssertionError('must not create client'))
        results = evaluate_profile(self.profile, model_factory=factory)
        self.assertTrue(all(row['status'] == 'not_run' for row in results))
        self.assertTrue(all(row['model_calls'] == 0 for row in results))
        factory.assert_not_called()

    def test_host_verification_requires_opt_in(self):
        factory = Mock(side_effect=lambda profile: FixtureModel())
        results = evaluate_profile(self.profile, live=True, allow_exec=False, model_factory=factory)
        self.assertEqual([row['status'] for row in results], ['passed', 'not_run', 'not_run'])
        self.assertEqual(factory.call_count, 1)

    def test_missing_credentials_and_endpoint_are_not_run(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': ''}):
            results = evaluate_profile(self.profile, live=True, allow_exec=True)
        self.assertTrue(all(row['status'] == 'not_run' for row in results))
        results = evaluate_profile(self.profile, live=True, allow_exec=True,
                                   model_factory=Mock(side_effect=ConnectionError('private endpoint')))
        self.assertTrue(all(row['reason'] == 'endpoint_unavailable' for row in results))
        self.assertNotIn('private endpoint', json.dumps(results))

    def test_reports_do_not_store_raw_errors_or_prompts(self):
        secret = 'DO-NOT-REPORT-THIS-API-KEY-OR-CONVERSATION'
        model = Mock()
        model.generate.side_effect = RuntimeError(secret)
        results = evaluate_profile(self.profile, live=True, allow_exec=True, model_factory=lambda p: model)
        serialized = json.dumps(results)
        self.assertNotIn(secret, serialized)
        self.assertNotIn('Explain this project', serialized)
        self.assertTrue(all(row['status'] == 'failed' for row in results))

    def test_final_claim_is_not_proof_of_correctness(self):
        model = Mock()
        model.generate.return_value = ModelResponse(Message('assistant', 'All fixed. Tests pass.'))
        results = evaluate_profile(self.profile, live=True, allow_exec=True, model_factory=lambda p: model)
        self.assertTrue(all(row['status'] == 'failed' for row in results))

    def test_paired_reads_preserve_correctness_and_reduce_large_file_context(self):
        results = [evaluate_profile(self.profile, live=True, allow_exec=True, suite='read_comparison',
                                    read_strategy=strategy, model_factory=lambda p: ReadFixtureModel())
                   for strategy in ['full', 'ranges']]
        for rows in results:
            self.assertTrue(all(row['status'] == 'passed' for row in rows), rows)
            self.assertTrue(all(row['usage']['input_tokens']['reported_sum'] is None for row in rows))
        pairs = compare_read_results(*results)
        self.assertTrue(all(pair['both_passed'] for pair in pairs))
        self.assertGreater(pairs[0]['selected_context_char_reduction'], 0)
        self.assertEqual([r['model_calls'] for r in results[0]], [r['model_calls'] for r in results[1]])
        self.assertEqual(compare_read_results(*results, offset=6)[0]['range_result'], 9)
        print('\nDeterministic read comparison (characters, NOT live tokens): ' + json.dumps([
            {'task': pair['task'], 'full_chars': before['selected_context_chars'],
             'range_chars': after['selected_context_chars'], 'both_passed': pair['both_passed'],
             'reduction': pair['selected_context_char_reduction']}
            for pair, before, after in zip(pairs, *results, strict=True)]))

    def test_read_comparison_opt_ins_and_unavailable_measurements(self):
        factory = Mock(side_effect=AssertionError('No provider calls authorized'))
        full = evaluate_profile(self.profile, suite='read_comparison', read_strategy='full', model_factory=factory)
        ranges = evaluate_profile(self.profile, suite='read_comparison', model_factory=factory)
        self.assertTrue(all(r['status'] == 'not_run' for r in full + ranges))
        self.assertTrue(all(p['selected_context_char_reduction'] is None for p in compare_read_results(full, ranges)))
        blocked = evaluate_profile(self.profile, live=True, suite='read_comparison', model_factory=factory)
        self.assertTrue(all(r['reason'] == 'local_verification_not_enabled' for r in blocked))
        factory.assert_not_called()
        for kwargs in [{'suite': 'unknown'}, {'read_strategy': 'unknown'},
                       {'suite': 'read_comparison', 'toolset': 'baseline'}]:
            with self.assertRaises(ValueError):
                evaluate_profile(self.profile, **kwargs)

    def test_comparison_checks_fixture_identity_and_unrelated_declarations(self):
        for task in ['large_file', 'multi_file', 'surrounding_context']:
            with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
                root, _, before = create_read_fixture(Path(a), task)
                self.assertEqual(before, create_read_fixture(Path(b), task)[2])
                original = read_fixture_structure(root, task)
                source = root / next(iter(original))
                source.write_text(source.read_text() + '\nUNRELATED_CHANGE = True\n')
                self.assertNotEqual(original, read_fixture_structure(root, task))

    def test_cli_comparison_preserves_explicit_opt_ins_and_pair_indices(self):
        output = io.StringIO()
        with patch('sys.argv', ['coding-agent-eval', '--compare-reads']), patch('sys.stdout', output):
            main()
        report = json.loads(output.getvalue())
        self.assertEqual(len(report['results']), 18)
        self.assertEqual(len(report['read_comparisons']), 9)
        self.assertTrue(all(row['status'] == 'not_run' for row in report['results']))
        for pair in report['read_comparisons']:
            self.assertEqual(report['results'][pair['full_result']]['read_strategy'], 'full')
            self.assertEqual(report['results'][pair['range_result']]['read_strategy'], 'ranges')
            self.assertIsNone(pair['selected_context_char_reduction'])
        with patch('sys.argv', ['coding-agent-eval', '--compare-reads', '--toolset', 'both']), \
                patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)

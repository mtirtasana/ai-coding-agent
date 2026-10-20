import json
import os
import unittest
from unittest.mock import Mock, patch

from agent.core.types import Message, ModelResponse, ToolCall
from agent.evaluate import evaluate_profile


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


class EvaluationTests(unittest.TestCase):
    profile = {'provider': 'openai', 'model': 'simulated', 'name': 'unit-test-double'}

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

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from agent.adapters.editing import FileTools
from agent.adapters.ranges import line_range
from agent.adapters.tools import workspace_tools
from agent.core.cancellation import CancellationToken
from agent.core.context import split_turns
from agent.core.engine import Agent, RANGE_READ_INSTRUCTIONS
from agent.core.policy import ModeToolExecutor
from agent.core.types import Message, ModelResponse, ToolCall
from agent.files import Workspace
from agent.server import EditorTools


class RangeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = Workspace(self.root, allow_write=True)
        self.path = self.root / 'sample.py'
        self.files = FileTools(self.workspace)

    def test_line_boundaries_unicode_and_original_endings(self):
        for text in ['alpha\r\n😀 café\r\nlast', 'alpha\n😀 café\nlast', 'alpha\r😀 café\rlast']:
            with self.subTest(text=text):
                self.path.write_bytes(text.encode('utf-8'))
                result = json.loads(self.files.read_range('./sample.py', 2, 200))
                self.assertEqual(result['path'], 'sample.py')
                self.assertEqual(result['content'], text[len(text.splitlines(keepends=True)[0]):])
                self.assertEqual((result['start_line'], result['end_line'], result['total_lines']), (2, 3, 3))
                self.assertEqual(result['sha256'], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(line_range('one\n', 1, 1)['total_lines'], 1)
        self.assertEqual(line_range('\n\n', 2, 2)['content'], '\n')
        self.assertEqual(line_range('a\u2028b', 1, 1)['total_lines'], 1)

    def test_empty_file_and_eof(self):
        self.path.write_bytes(b'')
        result = json.loads(self.files.read_range('sample.py', 1, 1))
        self.assertEqual((result['start_line'], result['end_line'], result['total_lines']), (0, 0, 0))
        self.assertEqual(result['content'], '')
        for text, start in [('', 2), ('a\n', 2)]:
            with self.assertRaisesRegex(ValueError, 'beyond'):
                line_range(text, start, start)

    def test_invalid_ranges_and_hard_output_bounds(self):
        for start, end in [(0, 1), (-1, 1), (2, 1), (True, 1), (1, False), (1.0, 2),
                           ('1', 2), (1, 2**53), (1, 201)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                line_range('a\n' * 300, start, end)
        self.assertEqual(len(line_range('😀' * 12000, 1, 1)['content']), 12000)
        with self.assertRaisesRegex(ValueError, 'narrower'):
            line_range('x' * 12001, 1, 1)
        self.assertEqual(line_range('a\n' * 200, 1, 200)['end_line'], 200)

    def test_range_hash_supports_edit_and_becomes_stale(self):
        self.path.write_bytes(b'first\r\nvalue = 1\r\nlast\r\n')
        snap = json.loads(self.files.read_range('sample.py', 2, 2))
        args = {'path': 'sample.py', 'expected_sha256': snap['sha256'],
                'old_text': 'value = 1', 'new_text': 'value = 2'}
        original = self.path.read_bytes()
        self.path.write_bytes(original.replace(b'first', b'outside the excerpt'))
        with self.assertRaisesRegex(ValueError, 'Stale'):
            self.files.apply(**args)
        self.path.write_bytes(original)
        self.assertTrue(json.loads(self.files.apply(**args))['applied'])
        self.assertEqual(self.path.read_bytes(), b'first\r\nvalue = 2\r\nlast\r\n')
        with self.assertRaisesRegex(ValueError, 'Stale'):
            self.files.apply(**args)

    def test_path_and_file_eligibility_guards_apply_to_ranges(self):
        self.path.write_bytes(b'value = 1')
        for name in ['../outside', '.git/config', '.env.local']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.files.read_range(name, 1, 1)
        for raw in [b'bad\xff', b'bad\0', b'x' * 100001, b'x' * 400001]:
            self.path.write_bytes(raw)
            with self.assertRaises(ValueError):
                self.files.read_range('sample.py', 1, 1)

    def test_registry_accepts_only_bounded_integers_and_existing_string_contracts(self):
        self.path.write_text('value = 1\n')
        planning = ModeToolExecutor(workspace_tools(self.workspace), 'plan')
        self.assertIn('read_file_range', {s.name for s in planning.specifications()})
        args = {'path': 'sample.py', 'start_line': 1, 'end_line': 1}
        self.assertTrue(planning.execute(ToolCall('x', 'read_file_range', json.dumps(args))).ok)
        for patch in [{'start_line': '1'}, {'start_line': True}, {'end_line': 1.0},
                      {'end_line': 2**53}, {'extra': 1}, {'path': 1}]:
            with self.subTest(patch=patch):
                self.assertFalse(planning.execute(ToolCall('x', 'read_file_range', json.dumps({**args, **patch}))).ok)
        self.assertFalse(planning.execute(ToolCall('x', 'read_file', '{"path":1}')).ok)
        self.assertNotIn('read_file_range', {s.name for s in workspace_tools(self.workspace, range_reads=False).specifications()})

    def test_bridge_forwards_numeric_arguments_and_blocks_invalid_calls(self):
        bridge = Mock(return_value={'ok': True, 'content': '{}'})
        registry = EditorTools(self.workspace, bridge, {}, CancellationToken())
        args = {'path': 'sample.py', 'start_line': 1, 'end_line': 2}
        self.assertTrue(registry.execute(ToolCall('x', 'read_file_range', json.dumps(args))).ok)
        bridge.assert_called_once_with('editor/tool', {'name': 'read_file_range', 'arguments': args})
        bridge.reset_mock()
        for patch in [{'start_line': True}, {'path': '../outside'}]:
            self.assertFalse(registry.execute(ToolCall('x', 'read_file_range', json.dumps({**args, **patch}))).ok)
        bridge.assert_not_called()

    def test_guidance_is_capability_dependent_without_history_changes(self):
        for enabled in [False, True]:
            model = Mock()
            model.generate.return_value = ModelResponse(Message('assistant', 'Done'))
            result = Agent(model, workspace_tools(self.workspace, range_reads=enabled)).run('Inspect')
            messages = model.generate.call_args.args[0]
            self.assertEqual(any(m.content == RANGE_READ_INSTRUCTIONS for m in messages), enabled)
            split_turns(result.messages)
            self.assertEqual(result.messages[-1].content, 'Done')

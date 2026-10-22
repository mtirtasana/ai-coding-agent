import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.adapters.editing import FileTools
from agent.adapters.tools import workspace_tools
from agent.core.policy import ModeToolExecutor
from agent.core.types import ToolCall
from agent.files import Workspace


class EditingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = Workspace(self.root, allow_write=True)
        self.files = FileTools(self.workspace)
        self.path = self.root / 'sample.py'
        self.path.write_bytes(b'def add(a, b):\r\n    return a - b\r\n')

    def args(self, **changes):
        snapshot = json.loads(self.files.snapshot('sample.py'))
        return {'path': 'sample.py', 'expected_sha256': snapshot['sha256'],
                'old_text': 'return a - b', 'new_text': 'return a + b', **changes}

    def test_preview_then_atomic_apply_preserves_newlines_and_permissions(self):
        self.path.chmod(0o640)
        args = self.args()
        original = self.path.read_bytes()
        preview = json.loads(self.files.preview(**args))
        self.assertFalse(preview['applied'])
        self.assertIn('-    return a - b', preview['diff'])
        self.assertEqual(self.path.read_bytes(), original)
        applied = json.loads(self.files.apply(**args))
        self.assertTrue(applied['applied'])
        self.assertIn(b'return a + b\r\n', self.path.read_bytes())
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), applied['sha256'])
        if __import__('os').name != 'nt':
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(list(self.root.glob('.agent-edit-*')), [])

    def test_stale_missing_and_overlapping_matches(self):
        args = self.args()
        self.path.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'Stale'):
            self.files.apply(**args)
        self.path.write_text('abc')
        preview = json.loads(self.files.preview(**self.args(old_text='abc', new_text='xyz')))
        self.assertIn('\\ No newline at end of file', preview['diff'])
        self.path.write_text('aaa')
        for text in ['', 'missing', 'aa', 'a']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.files.apply(**self.args(old_text=text))
        with self.assertRaises(ValueError):
            self.files.apply(**self.args(old_text='aaa', new_text='x' * 100001))

    def test_failed_replace_and_concurrent_change_leave_no_temporary_file(self):
        original = self.path.read_bytes()
        with patch('agent.adapters.editing.os.replace', side_effect=OSError('blocked')):
            with self.assertRaises(OSError):
                self.files.apply(**self.args())
        self.assertEqual(self.path.read_bytes(), original)
        prepare = self.files._prepare
        def changed(*args, **kwargs):
            result = prepare(*args, **kwargs)
            self.path.write_text('External update')
            return result
        args = self.args()
        with patch.object(self.files, '_prepare', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.files.apply(**args)
        self.assertEqual(self.path.read_text(), 'External update')
        self.assertEqual(list(self.root.glob('.agent-edit-*')), [])

    def test_permissions_and_protected_paths(self):
        with self.assertRaises(ValueError):
            FileTools(Workspace(self.root)).apply(**self.args())
        for path in ['../outside', '.git/config', '.env.local']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.files.snapshot(path)
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'file'
            target.write_text('outside')
            (self.root / 'link').symlink_to(target)
            with self.assertRaises(ValueError):
                self.files.snapshot('link')

    def test_search_skips_protected_binary_large_and_linked_files(self):
        self.path.write_text('needle\nsecond needle\n')
        for directory in ['.git', '.venv', 'node_modules']:
            (self.root / directory).mkdir()
            (self.root / directory / 'hidden').write_text('needle')
        (self.root / '.env').write_text('needle')
        (self.root / 'binary').write_bytes(b'needle\x00')
        (self.root / 'large').write_text('needle' * 20000)
        (self.root / 'linked').symlink_to(self.path)
        result = json.loads(self.files.search('.', 'needle'))
        self.assertEqual([m['line'] for m in result['matches']], [1, 2])
        self.assertTrue(all(m['path'] == 'sample.py' for m in result['matches']))
        self.assertFalse(result['truncated'])
        self.path.write_text('needle\n' * 300)
        result = json.loads(self.files.search('sample.py', 'needle'))
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['matches']), 100)
        with self.assertRaises(ValueError):
            self.files.search('.', '')

    def test_registry_and_planning_permissions(self):
        registry = workspace_tools(self.workspace)
        planning = ModeToolExecutor(registry, 'plan')
        names = {tool.name for tool in planning.specifications()}
        self.assertIn('preview_edit', names)
        self.assertNotIn('apply_edit', names)
        self.assertFalse(planning.execute(ToolCall('a', 'apply_edit', json.dumps(self.args()))).ok)
        self.assertTrue(planning.execute(ToolCall('b', 'preview_edit', json.dumps(self.args()))).ok)

import tempfile
import unittest
from pathlib import Path

from agent.files import Workspace


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root, allow_write=True)

    def test_write_and_read(self):
        self.workspace.execute("write_file", {"path": "src/demo.py", "content": "x = 1\n"})
        self.assertEqual(self.workspace.execute("read_file", {"path": "src/demo.py"}), "x = 1\n")

    def test_read_only(self):
        with self.assertRaises(ValueError):
            Workspace(self.root).execute("write_file", {"path": "x", "content": "x"})

    def test_rejects_escape(self):
        for path in ["../outside", "/etc/passwd"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.workspace.path(path)

    def test_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as outside:
            (self.root / "link").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                self.workspace.path("link/file.txt")

    def test_protected_files(self):
        for path in [".git/config", ".env", ".env.local", "config/.aws/credentials"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.workspace.path(path)

    def test_oversized_file(self):
        (self.root / "large.txt").write_text("a" * 100_001)
        with self.assertRaises(ValueError):
            self.workspace.execute("read_file", {"path": "large.txt"})


if __name__ == "__main__":
    unittest.main()

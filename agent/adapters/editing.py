"""Bounded workspace search and hash-checked, atomic text replacements."""
import difflib
import hashlib
import json
import os
import stat
import tempfile
from collections import deque

from .ranges import line_range


SKIP_DIRS = {'.git', '.ssh', '.aws', '.venv', 'venv', 'node_modules', '__pycache__',
             '.tox', '.ruff_cache', 'build', 'dist'}


class FileTools:
    def __init__(self, workspace):
        self.workspace = workspace

    def _read(self, name):
        path = self.workspace.path(name)
        with path.open('rb') as stream:
            raw = stream.read(400001)
        text = raw.decode('utf-8')
        if len(raw) > 400000 or len(text) > 100000 or '\x00' in text:
            raise ValueError('File is binary or exceeds the UTF-8 text limit.')
        return path, raw, text

    def snapshot(self, path):
        resolved, raw, text = self._read(path)
        return json.dumps({'path': str(resolved.relative_to(self.workspace.root)),
                           'sha256': hashlib.sha256(raw).hexdigest(), 'content': text}, ensure_ascii=False)

    def read_range(self, path, start_line, end_line):
        resolved, raw, text = self._read(path)
        return json.dumps({'path': resolved.relative_to(self.workspace.root).as_posix(),
                           'sha256': hashlib.sha256(raw).hexdigest(),
                           **line_range(text, start_line, end_line)}, ensure_ascii=False)

    def search(self, path, query):
        if not query or len(query) > 512 or '\n' in query or '\r' in query:
            raise ValueError('Search requires a nonempty single-line literal query, at most 512 characters.')
        root = self.workspace.path(path)
        pending = deque([root])
        matches, visited, scanned = [], 0, 0
        truncated = False
        output_chars = 0
        while pending and not truncated:
            current = pending.popleft()
            if current.is_symlink():
                continue
            if current.is_dir():
                if current.name in SKIP_DIRS or current.name.startswith('.env'):
                    continue
                with os.scandir(current) as entries:
                    for entry in entries:
                        visited += 1
                        if visited > 10000:
                            truncated = True
                            break
                        if (entry.name in SKIP_DIRS or entry.name.startswith('.env')
                                or entry.is_symlink()):
                            continue
                        if self.workspace.policy.ignored(current.relative_to(self.workspace.root) / entry.name, entry.is_dir()):
                            continue
                        candidate = self.workspace.path(str(current.relative_to(self.workspace.root) / entry.name))
                        pending.append(candidate)
                continue
            if not current.is_file():
                continue
            scanned += 1
            if scanned > 1000:
                truncated = True
                break
            try:
                resolved, _, text = self._read(str(current.relative_to(self.workspace.root)))
            except (OSError, ValueError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                position = line.find(query)
                if position < 0:
                    continue
                snippet = line[max(0, position - 60):position + 180]
                match = {'path': str(resolved.relative_to(self.workspace.root)), 'line': number,
                         'snippet': snippet}
                size = len(json.dumps(match, ensure_ascii=False))
                if len(matches) >= 100 or output_chars + size > 20000:
                    truncated = True
                    break
                matches.append(match)
                output_chars += size
        return json.dumps({'matches': matches, 'files_scanned': min(scanned, 1000),
                           'truncated': truncated}, ensure_ascii=False)

    def _prepare(self, path, expected_sha256, old_text, new_text):
        resolved, raw, text = self._read(path)
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValueError('Stale file snapshot: read the current snapshot before editing.')
        start = text.find(old_text)
        if not old_text or start < 0 or text.find(old_text, start + 1) >= 0:
            raise ValueError('Original text must occur exactly once, including overlapping matches.')
        updated = text[:start] + new_text + text[start + len(old_text):]
        if len(updated) > 100000 or '\x00' in updated:
            raise ValueError('Replacement exceeds the text limit or contains NUL.')
        encoded = updated.encode('utf-8')
        name = str(resolved.relative_to(self.workspace.root))
        lines = difflib.unified_diff(text.splitlines(keepends=True), updated.splitlines(keepends=True),
                                    fromfile=name, tofile=name)
        diff = ''.join(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n'
                       for line in lines)
        result = {'path': name, 'sha256': hashlib.sha256(encoded).hexdigest(),
                  'diff': diff[:20000], 'diff_truncated': len(diff) > 20000}
        return resolved, raw, encoded, result

    def preview(self, path, expected_sha256, old_text, new_text):
        _, _, _, result = self._prepare(path, expected_sha256, old_text, new_text)
        return json.dumps({'applied': False, **result}, ensure_ascii=False)

    def apply(self, path, expected_sha256, old_text, new_text):
        if not self.workspace.allow_write:
            raise ValueError('Editing requires --allow-write.')
        resolved, original, encoded, result = self._prepare(path, expected_sha256, old_text, new_text)
        mode = stat.S_IMODE(resolved.stat().st_mode)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=resolved.parent, prefix='.agent-edit-', delete=False) as stream:
                temporary = stream.name
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            current, current_raw, _ = self._read(path)
            if current != resolved or current_raw != original:
                raise ValueError('File changed while preparing the edit; nothing was replaced.')
            os.replace(temporary, resolved)
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        return json.dumps({'applied': True, **result}, ensure_ascii=False)

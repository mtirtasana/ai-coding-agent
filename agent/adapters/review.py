"""Opt-in CLI file-tool reviews. Ordinary command processes remain outside this gate."""
import difflib
import hashlib
import json
import os
import stat
import tempfile

from agent.core.types import ToolCall, ToolResult
from .editing import FileTools
from .tools import ToolRegistry


def full_diff(name, before, after):
    lines = difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                 fromfile=name, tofile=name)
    return ''.join(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n' for line in lines)


class ReviewedWrites:
    def __init__(self, workspace, confirm):
        self.workspace, self.confirm = workspace, confirm
        self.files = FileTools(workspace)

    def change(self, name, args):
        if not self.workspace.allow_write:
            raise ValueError('Editing requires --allow-write.')
        path = args['path']
        resolved = self.workspace.path(path)
        original = None
        mode = None
        if name == 'apply_edit':
            resolved, original, encoded, _ = self.files._prepare(**args)
        else:
            text = args['content']
            if len(text) > 100000 or '\x00' in text:
                raise ValueError('Replacement exceeds the text limit or contains NUL.')
            encoded = text.encode('utf-8')
            if resolved.exists():
                resolved, original, _ = self.files._read(path)
        if original is not None:
            mode = stat.S_IMODE(resolved.stat().st_mode)
        canonical = resolved.relative_to(self.workspace.root).as_posix()
        before = original.decode('utf-8') if original is not None else ''
        if not self.confirm(canonical, before, encoded.decode('utf-8')):
            return ToolResult(False, json.dumps({'applied': False, 'review_status': 'rejected', 'path': canonical}))
        if self.workspace.path(path) != resolved:
            raise ValueError('Resolved path changed during review; nothing was written.')
        if original is None:
            if os.path.lexists(resolved):
                raise ValueError('File appeared during review; nothing was written.')
        else:
            current, raw, _ = self.files._read(path)
            if current != resolved or raw != original:
                raise ValueError('File changed during review; nothing was written.')
        resolved.parent.mkdir(parents=True, exist_ok=True)
        if self.workspace.path(path) != resolved:
            raise ValueError('Resolved path changed; nothing was written.')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=resolved.parent, prefix='.agent-review-', delete=False) as stream:
                temporary = stream.name
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            if mode is not None:
                os.chmod(temporary, mode)
                current, raw, _ = self.files._read(path)
                if current != resolved or raw != original:
                    raise ValueError('File changed before commit; nothing was replaced.')
                os.replace(temporary, resolved)
                temporary = None
            else:
                if self.workspace.path(path) != resolved:
                    raise ValueError('Resolved path changed; nothing was written.')
                # Hard-link creation is atomic and fails if any target already exists.
                os.link(temporary, resolved)
        finally:
            if temporary is not None:
                os.unlink(temporary)
        return ToolResult(True, json.dumps({'applied': True, 'path': canonical,
                                          'sha256': hashlib.sha256(encoded).hexdigest()}))


class ConsoleTools(ToolRegistry):
    """Retain registry validation before effects; observe actual handler results."""
    def __init__(self, source, observer=None, reviewer=None):
        super().__init__()
        for specification in source.specifications():
            def handler(_name=specification.name, **args):
                if reviewer is not None and _name in {'write_file', 'apply_edit'}:
                    try:
                        result = reviewer.change(_name, args)
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        result = ToolResult(False, str(exc))
                else:
                    result = source.execute(ToolCall('console', _name, json.dumps(args, ensure_ascii=False)))
                if observer is not None:
                    observer.tool_result(_name, result)
                return result
            self.register(specification, handler)

"""Opt-in local snapshots. Completed conversation content can contain sensitive source."""
import json
import os
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

from .user_paths import user_directory
from .verification import workspace_fingerprint


NOTICE = 'Local recovery stores completed prompts and source excerpts. No API-key configuration is stored. Snapshots expire after 30 inactive days.'
MAX_SNAPSHOT = 8 * 1024 * 1024


class RecoveryStore:
    def __init__(self, directory=None, clock=time.time, warn=lambda message: None):
        self.directory = Path(directory) if directory is not None else user_directory('state') / 'sessions'
        self.clock, self.warn = clock, warn

    def path(self, identifier):
        if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
            raise ValueError('Invalid saved-session identifier.')
        return self.directory / (identifier + '.json')

    def read(self, path):
        if path.is_symlink():
            raise ValueError('Session snapshot must not be a symlink.')
        with path.open(encoding='utf-8') as stream:
            text = stream.read(MAX_SNAPSHOT + 1)
        if len(text) > MAX_SNAPSHOT:
            raise ValueError('Session snapshot is too large.')
        value = json.loads(text)
        fields = {'version', 'id', 'workspace', 'updated', 'fingerprint', 'connection', 'state'}
        if (not isinstance(value, dict) or set(value) != fields or type(value['version']) is not int or value['version'] != 1
                or self.path(value['id']) != path or not isinstance(value['workspace'], str)
                or not Path(value['workspace']).is_absolute() or type(value['updated']) not in (int, float)
                or not 0 <= value['updated'] <= self.clock() + 300
                or not isinstance(value['connection'], dict) or set(value['connection']) != {'provider', 'profile', 'model'}
                or any(not isinstance(v, str) or len(v) > 256 for v in value['connection'].values())
                or value['fingerprint'] is not None and (not isinstance(value['fingerprint'], str) or len(value['fingerprint']) != 64)):
            raise ValueError('Invalid session snapshot.')
        # Reuse the strict completed-turn validator, without importing it into the core.
        from agent.server import restore_state
        restore_state(value['state'])
        return value

    def list(self, workspace):
        if not self.directory.exists():
            return []
        result = []
        for path in sorted(self.directory.glob('*.json')):
            try:
                item = self.read(path)
                if self.clock() - item['updated'] >= 30 * 86400:
                    path.unlink()
                elif item['workspace'] == str(workspace.root):
                    result.append({key: item[key] for key in ('id', 'updated', 'connection')})
            except (OSError, ValueError, TypeError, KeyError):
                self.warn('An unreadable, corrupt or incompatible saved session was skipped and preserved.')
        return sorted(result, key=lambda item: item['updated'], reverse=True)

    def save(self, identifier, workspace, state, connection):
        target = self.path(identifier)
        value = {'version': 1, 'id': identifier, 'workspace': str(workspace.root), 'updated': self.clock(),
                 'fingerprint': workspace_fingerprint(workspace), 'connection': connection, 'state': asdict(state)}
        text = json.dumps(value, ensure_ascii=True)
        if len(text) > MAX_SNAPSHOT:
            raise ValueError('Recovery snapshot exceeds 8 MiB; start a new conversation.')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=self.directory, delete=False) as stream:
                temporary = stream.name
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            temporary = None
        finally:
            if temporary is not None:
                os.unlink(temporary)

    def resume(self, identifier, workspace):
        path = self.path(identifier)
        value = self.read(path)
        if value['workspace'] != str(workspace.root):
            raise ValueError('Saved session belongs to another workspace.')
        if self.clock() - value['updated'] >= 30 * 86400:
            path.unlink()
            raise ValueError('Saved session expired after 30 inactive days.')
        from agent.server import restore_state
        state = restore_state(value['state'])
        state.accepted_plan = None
        current = workspace_fingerprint(workspace)
        changed = current is None or value['fingerprint'] is None or current != value['fingerprint']
        return state, value['connection'], changed

    def delete(self, identifier, workspace):
        path = self.path(identifier)
        if self.read(path)['workspace'] != str(workspace.root):
            raise ValueError('Saved session belongs to another workspace.')
        path.unlink()

"""Verification evidence owned by the host, independent of model assertions."""
import hashlib
import json
from dataclasses import replace

from agent.core.types import CheckEvidence
from .editing import SKIP_DIRS


CHECKS = {'run_tests': 'test', 'run_lint': 'lint', 'run_security': 'security',
          'run_dependency_audit': 'dependency_audit'}


def workspace_fingerprint(workspace):
    """Bounded content fingerprint. Uncertain observations return None, never a pass."""
    digest = hashlib.sha256()
    count = size = 0
    pending = [workspace.root]
    try:
        # Policy changes invalidate evidence even when the policy denies itself.
        policy = workspace.root / '.agentignore'
        if policy.is_symlink():
            return None
        if policy.exists():
            if policy.stat().st_size > 65536:
                return None
            digest.update(policy.read_bytes())
        while pending:
            directory = pending.pop()
            gitignore = directory / '.gitignore'
            if gitignore.is_symlink():
                return None
            if gitignore.exists():
                if gitignore.stat().st_size > 65536:
                    return None
                digest.update(gitignore.relative_to(workspace.root).as_posix().encode() + b'\0' + gitignore.read_bytes())
            for path in sorted(directory.iterdir()):
                relative = path.relative_to(workspace.root)
                if path.name in SKIP_DIRS or workspace.policy.ignored(relative, path.is_dir()):
                    continue
                count += 1
                if count > 10000 or path.is_symlink():
                    return None
                if path.is_dir():
                    pending.append(path)
                elif path.is_file():
                    stat = path.stat()
                    size += stat.st_size
                    if size > 64 * 1024 * 1024:
                        return None
                    digest.update(relative.as_posix().encode() + b'\0' + str(stat.st_mode).encode() + b'\0')
                    with path.open('rb') as stream:
                        while chunk := stream.read(65536):
                            digest.update(chunk)
                    after = path.stat()
                    if (stat.st_mtime_ns, stat.st_size, stat.st_ino) != (after.st_mtime_ns, after.st_size, after.st_ino):
                        return None
                else:
                    return None
        return digest.hexdigest()
    except (OSError, ValueError):
        return None


class VerifiedTools:
    def __init__(self, tools, snapshot, configuration=None, records=None):
        self.tools, self.snapshot = tools, snapshot
        self.configuration = hashlib.sha256(json.dumps(configuration or {}, sort_keys=True).encode()).hexdigest()
        self.records = records if records is not None else {}

    def specifications(self):
        return self.tools.specifications()

    def execute(self, call):
        profile = CHECKS.get(call.name)
        before = self.snapshot() if profile else None
        result = self.tools.execute(call)
        if profile:
            after = self.snapshot()
            try:
                data = json.loads(result.content)
            except (TypeError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            status = ('not_run' if data.get('not_run') else 'failed' if not result.ok else
                      'unknown' if before is None or after is None else 'stale' if before != after else 'passed')
            self.records[profile] = CheckEvidence(profile, status, after, self.configuration)
        elif call.name in {'write_file', 'apply_edit', 'run_lint_fix'} and result.ok:
            self.invalidate()
        return result

    def invalidate(self):
        for profile, record in self.records.items():
            if record.status == 'passed':
                self.records[profile] = replace(record, status='stale')

    def evidence(self):
        current = self.snapshot() if self.records else None
        result = []
        for profile in CHECKS.values():
            record = self.records.get(profile, CheckEvidence(profile, 'not_run'))
            if record.status == 'passed':
                if current is None:
                    record = replace(record, status='unknown')
                elif current != record.fingerprint or self.configuration != record.configuration:
                    record = replace(record, status='stale')
            result.append(record)
        return result

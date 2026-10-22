"""Allowlisted diagnostic metadata; no prompts, source, credentials or raw errors."""
import importlib.metadata
import json
import os
import platform
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .connections import OSVault, ProfileStore
from .user_paths import user_directory


def storage_ready(directory):
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryFile(dir=directory) as stream:
            stream.write(b'probe')
        return True
    except OSError:
        return False


def diagnose(checks=None):
    try:
        backend = importlib.metadata.version('ai-coding-agent-starter')
    except importlib.metadata.PackageNotFoundError:
        backend = 'not-installed'
    report = {'schema': 1, 'python': platform.python_version(), 'platform': platform.system(),
              'backend': backend, 'protocol': 2, 'production_status': 'release-candidate',
              'supported_python': (3, 11) <= sys.version_info[:2] <= (3, 14),
              'model_requests_made': False}
    try:
        profiles = ProfileStore().load()
        report['profiles'] = {'valid': True, 'count': len(profiles['profiles'])}
    except (OSError, ValueError):
        report['profiles'] = {'valid': False}
    try:
        OSVault().backend()
        report['keychain'] = 'available'
    except ValueError:
        report['keychain'] = 'unavailable-or-locked'
    try:
        report['storage'] = {kind: storage_ready(user_directory(kind)) for kind in ('config', 'state')}
    except ValueError:
        report['storage'] = {'configuration': 'invalid-directory-setting'}
    # Only executable availability, not argv/environment, enters the report.
    report['checks'] = {name: bool(command and shutil.which(command[0])) for name, command in (checks or {}).items()}
    return report


def connection_test(name):
    """Explicit authenticated GET of model metadata, never inference or automatic retries."""
    store = ProfileStore()
    active = store.activate(store.get(name))
    url = ('https://api.openai.com/v1' if active.provider == 'openai' else active.base_url)
    url += '/api/tags' if active.provider == 'ollama' else '/models'
    headers = {'Accept': 'application/json'}
    if active.api_key:
        headers['Authorization'] = 'Bearer ' + active.api_key

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, response_headers, newurl):
            raise ValueError('Connection test redirects are disabled.')

    try:
        with build_opener(NoRedirect()).open(Request(url, headers=headers), timeout=10) as response:
            return {'reachable': 200 <= response.status < 300, 'inference': False}
    except Exception as exc:
        return {'reachable': False, 'inference': False, 'error_type': type(exc).__name__}


def export_report(path, report):
    """Caller-specified destination; refuse overwriting an existing file."""
    descriptor = os.open(Path(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')

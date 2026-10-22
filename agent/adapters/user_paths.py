"""Shared per-user paths for CLI and workspace-host workers, never project-relative."""
import os
import sys
from pathlib import Path


def user_directory(kind):
    override = os.getenv('AGENT_CONFIG_DIR' if kind == 'config' else 'AGENT_STATE_DIR')
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ValueError('Agent user-directory overrides must be absolute.')
        return path
    if sys.platform == 'win32':
        variable = 'APPDATA' if kind == 'config' else 'LOCALAPPDATA'
        root = Path(os.getenv(variable) or Path.home() / 'AppData' / ('Roaming' if kind == 'config' else 'Local'))
    elif sys.platform == 'darwin':
        root = Path.home() / 'Library' / 'Application Support'
    else:
        variable = 'XDG_CONFIG_HOME' if kind == 'config' else 'XDG_STATE_HOME'
        root = Path(os.getenv(variable) or Path.home() / ('.config' if kind == 'config' else '.local/state'))
        if not root.is_absolute():
            raise ValueError('XDG user-directory settings must be absolute.')
    return root / 'ai-coding-agent'

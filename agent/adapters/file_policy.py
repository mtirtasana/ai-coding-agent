"""Git-compatible discovery exclusions and explicit, non-overridable file denial."""
from pathlib import Path

from pathspec import GitIgnoreSpec


PRIVATE_NAMES = {'.git', '.ssh', '.aws', '.azure', '.config', '.agentignore',
                 '.netrc', '.npmrc', '.pypirc', 'credentials', 'credentials.json',
                 'id_rsa', 'id_ed25519', 'id_ecdsa', 'id_dsa'}
PRIVATE_SUFFIXES = {'.pem', '.key', '.p12', '.pfx'}


def protected_part(part):
    return part.lower() in PRIVATE_NAMES or part.lower().startswith('.env') or Path(part).suffix.lower() in PRIVATE_SUFFIXES


class FilePolicy:
    def __init__(self, root):
        self.root = Path(root)

    def rules(self, directory, filename):
        path = directory / filename
        if path.is_symlink():
            raise ValueError('Ignore policy files must not be symlinks.')
        try:
            with path.open(encoding='utf-8') as stream:
                text = stream.read(65537)
            if len(text) > 65536:
                raise ValueError('Ignore policy is too large.')
            return GitIgnoreSpec.from_lines(text.splitlines())
        except FileNotFoundError:
            return GitIgnoreSpec.from_lines([])

    def denied(self, relative, directory=False):
        parts = Path(relative).parts
        if any(protected_part(part) for part in parts):
            return True
        name = Path(relative).as_posix().rstrip('/')
        if name in {'', '.'}:
            return False
        spec = self.rules(self.root, '.agentignore')
        # A denied parent cannot be reopened by naming a child directly.
        return any(spec.match_file('/'.join(parts[:index]) + ('/' if index < len(parts) or directory else ''))
                   for index in range(1, len(parts) + 1))

    def ignored(self, relative, directory=False):
        if self.denied(relative, directory):
            return True
        parts = Path(relative).parts
        if not parts or parts == ('.',):
            return False
        # Parent directory exclusion wins, matching Git's traversal semantics.
        for end in range(1, len(parts) + 1):
            excluded = False
            for depth in range(end):
                parent = self.root.joinpath(*parts[:depth])
                name = '/'.join(parts[depth:end]) + ('/' if end < len(parts) or directory else '')
                match = self.rules(parent, '.gitignore').check_file(name)
                if match.include is not None:
                    excluded = match.include
            if excluded:
                return True
        return False

"""Explicit maintainer tool: refresh reviewed runtime pins/hashes from PyPI metadata.

Run in a resolved Python environment containing openai/pathspec. Review/audit the
result and exercise installation in the supported OS/Python CI matrix before release.
"""
import json
import urllib.request
from importlib.metadata import distribution
from pathlib import Path

from packaging.requirements import Requirement
from packaging.markers import default_environment


def lock():
    targets = [dict(default_environment(), sys_platform=system, python_version=version,
                    python_full_version=version + '.0', extra='',
                    platform_system={'linux': 'Linux', 'darwin': 'Darwin', 'win32': 'Windows'}[system],
                    os_name='nt' if system == 'win32' else 'posix')
               for system in ('linux', 'darwin', 'win32') for version in ('3.11', '3.14')]
    def closure(roots):
        names, pending = set(), list(roots)
        while pending:
            name = pending.pop().lower().replace('_', '-')
            if name in names:
                continue
            names.add(name)
            package = distribution(name)
            for raw in package.requires or []:
                requirement = Requirement(raw)
                if requirement.marker is None or any(requirement.marker.evaluate(env) for env in targets):
                    pending.append(requirement.name)
        return names
    sdk_names = closure(['openai', 'pathspec', 'pip', 'setuptools'])
    names = closure(['openai', 'pathspec', 'pip', 'setuptools', 'keyring', 'prompt-toolkit'])
    base_names = {'pathspec', 'pip', 'setuptools'}
    inventory, lines = [], {}
    for name in sorted(names):
        package = distribution(name)
        with urllib.request.urlopen(f'https://pypi.org/pypi/{name}/{package.version}/json', timeout=30) as response:
            data = json.load(response)
        hashes = sorted({item['digests']['sha256'] for item in data['urls'] if item['filename'].endswith('.whl')})
        if not hashes:
            raise ValueError('No wheel hashes for ' + name)
        lines[name] = f'{name}=={package.version} ' + ' '.join('--hash=sha256:' + item for item in hashes)
        inventory.append({'name': name, 'version': package.version,
                          'license': data['info'].get('license_expression') or data['info'].get('license'),
                          'source': f'https://pypi.org/project/{name}/{package.version}/', 'wheel_sha256': hashes})
    output = Path(__file__).resolve().parents[1] / 'vscode' / 'backend'
    output.mkdir(exist_ok=True)
    for profile, selected in [('base', base_names), ('openai', sdk_names), ('cli', names)]:
        (output / f'runtime-{profile}.txt').write_text('# Reviewed runtime dependency lock; refresh with tools/lock_runtime.py\n' +
                                                    '\n'.join(lines[name] for name in sorted(selected)) + '\n')
    (output / 'dependencies.json').write_text(json.dumps({'schema': 1, 'packages': inventory}, indent=2) + '\n')


if __name__ == '__main__':
    lock()

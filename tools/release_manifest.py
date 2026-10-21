"""Assemble reviewable internal artifacts; never publish or claim manual checks passed."""
import hashlib
import json
import shutil
from pathlib import Path


def assemble():
    root = Path(__file__).resolve().parents[1]
    output = root / 'release' / 'artifacts'
    output.mkdir(parents=True, exist_ok=True)
    package = json.loads((root / 'vscode/package.json').read_text())
    backend = json.loads((root / 'vscode/backend/manifest.json').read_text())
    checklist = json.loads((root / 'release/checklist.json').read_text())
    source = [root / 'vscode' / f"generic-coding-agent-{package['version']}.vsix",
              root / 'vscode/backend' / backend['file'], root / 'vscode/backend/dependencies.json',
              *sorted((root / 'vscode/backend').glob('runtime-*.txt')), root / 'release/checklist.json',
              root / 'CHANGELOG.md', root / 'docs/production-readiness.md']
    artifacts = []
    for path in source:
        data = path.read_bytes()
        destination = output / path.name
        shutil.copyfile(path, destination)
        artifacts.append({'file': path.name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    manifest = {'schema': 1, 'extension': package['version'], 'backend': backend['backendVersion'],
                'protocol': backend['protocolVersion'], 'classification': checklist['classification'],
                'manual_checks': checklist['manual_checks'], 'artifacts': artifacts}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (output / 'SHA256SUMS').write_text(''.join(f"{item['sha256']}  {item['file']}\n" for item in artifacts))
    print(f"Assembled {len(artifacts)} internal artifacts; classification: {checklist['classification']}.")


if __name__ == '__main__':
    assemble()

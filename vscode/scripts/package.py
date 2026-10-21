"""Build a local VSIX from an explicit asset allowlist using the standard library."""
import hashlib
import json
import mimetypes
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
package = json.loads((root / 'package.json').read_text())
manifest = json.loads((root / 'backend/manifest.json').read_text())
wheel = root / 'backend' / manifest['file']
assert wheel.parent == root / 'backend' and wheel.suffix == '.whl'
assert hashlib.sha256(wheel.read_bytes()).hexdigest() == manifest['sha256']
ns = 'http://schemas.microsoft.com/developer/vsx-schema/2011'
ET.register_namespace('', ns)
xml = ET.Element(f'{{{ns}}}PackageManifest', Version='2.0.0')
metadata = ET.SubElement(xml, 'Metadata')
ET.SubElement(metadata, 'Identity', Language='en-US', Id=package['name'], Version=package['version'], Publisher=package['publisher'])
ET.SubElement(metadata, 'DisplayName').text = package['displayName']
ET.SubElement(metadata, 'Description').text = package['description']
ET.SubElement(metadata, 'Categories').text = ','.join(package['categories'])
properties = ET.SubElement(metadata, 'Properties')
for key, value in {'Engine': package['engines']['vscode'], 'ExtensionKind': 'workspace', 'ExecutesCode': 'true',
                   'ExtensionDependencies': '', 'ExtensionPack': ''}.items():
    ET.SubElement(properties, 'Property', Id='Microsoft.VisualStudio.Code.' + key, Value=value)
installation = ET.SubElement(xml, 'Installation')
ET.SubElement(installation, 'InstallationTarget', Id='Microsoft.VisualStudio.Code')
ET.SubElement(xml, 'Dependencies')
assets = ET.SubElement(xml, 'Assets')
ET.SubElement(assets, 'Asset', Type='Microsoft.VisualStudio.Code.Manifest', Path='extension/package.json', Addressable='true')
ET.SubElement(assets, 'Asset', Type='Microsoft.VisualStudio.Services.Content.Details', Path='extension/README.md', Addressable='true')
files = [root / name for name in ('package.json', 'README.md', 'backend/manifest.json',
                                 'backend/runtime-base.txt', 'backend/runtime-openai.txt', 'backend/dependencies.json')]
files += [root / 'dist/extension.js', root / 'dist/webview.js', *sorted((root / 'media').glob('*')), wheel]
assert all(p.is_file() and not p.is_symlink() for p in files)
types_ns = 'http://schemas.openxmlformats.org/package/2006/content-types'
types = ET.Element('Types', xmlns=types_ns)
for extension in sorted({p.suffix[1:] for p in files} | {'vsixmanifest'}):
    mime = 'text/xml' if extension == 'vsixmanifest' else mimetypes.guess_type('file.' + extension)[0] or 'application/octet-stream'
    ET.SubElement(types, 'Default', Extension=extension, ContentType=mime)
output = root / f"{package['name']}-{package['version']}.vsix"
with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
    archive.writestr('extension.vsixmanifest', ET.tostring(xml, encoding='utf-8', xml_declaration=True))
    archive.writestr('[Content_Types].xml', ET.tostring(types, encoding='utf-8', xml_declaration=True))
    for file in files:
        archive.write(file, 'extension/' + file.relative_to(root).as_posix())
print(f'Packaged {output.name}: {output.stat().st_size:,} bytes; {len(files)} allowed assets.')

import json
import hashlib

from agent.core.types import ToolResult, ToolSpec
from .ranges import MAX_LINE_NUMBER


def spec(name, description, properties, *, read_only=False):
    return ToolSpec(name, description, {"type": "object", "properties": properties,
                    "required": list(properties), "additionalProperties": False}, read_only)


PATH = {"type": "string", "description": "Pfad relativ zum Workspace"}
FILE_TOOLS = (
    spec("list_files", "Listet ein Verzeichnis.", {"path": PATH}, read_only=True),
    spec("read_file", "Liest eine UTF-8-Datei.", {"path": PATH}, read_only=True),
    spec("write_file", "Erstellt oder ersetzt eine UTF-8-Datei.",
         {"path": PATH, "content": {"type": "string"}}),
)


class ToolRegistry:
    def __init__(self):
        self._tools = {}

    def register(self, specification, handler):
        if specification.name in self._tools:
            raise ValueError("Werkzeug bereits registriert: " + specification.name)
        self._tools[specification.name] = (specification, handler)

    def specifications(self):
        return tuple(spec for spec, _ in self._tools.values())

    def validate(self, value, schema):
        kind = schema['type']
        if kind == 'string':
            if not isinstance(value, str) or len(value) > schema.get('maxLength', 1000000):
                raise ValueError('Expected a bounded string argument.')
        elif kind == 'integer':
            if type(value) is not int or not schema.get('minimum', value) <= value <= schema.get('maximum', value):
                raise ValueError('Invalid integer argument.')
        elif kind == 'array':
            if not isinstance(value, list) or not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 10):
                raise ValueError('Invalid bounded array argument.')
            for item in value:
                self.validate(item, schema['items'])
        elif kind == 'object':
            if not isinstance(value, dict) or set(value) != set(schema['properties']):
                raise ValueError('Missing or unknown object fields.')
            for key, item in value.items():
                self.validate(item, schema['properties'][key])
        else:
            raise ValueError('Unsupported argument type.')

    def execute(self, call):
        try:
            if call.name not in self._tools:
                raise ValueError("Werkzeug nicht verfügbar: " + call.name)
            specification, handler = self._tools[call.name]
            args = json.loads(call.arguments)
            properties = specification.parameters["properties"]
            if not isinstance(args, dict) or set(args) != set(properties):
                raise ValueError("Fehlende oder unbekannte Werkzeugargumente.")
            for name, value in args.items():
                self.validate(value, properties[name])
            result = handler(**args)
            return result if isinstance(result, ToolResult) else ToolResult(True, result)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return ToolResult(False, str(exc))


def workspace_tools(workspace, *, enhanced=True, test_runner=None, lint_runner=None, lint_fix_runner=None,
                    security_runner=None, dependency_audit_runner=None, range_reads=True, archive=None):
    registry = ToolRegistry()
    from .check_output import CheckOutputStore, register_output_reader
    outputs = CheckOutputStore(archive=archive)
    def check_handler(runner):
        def run():
            result = runner.as_tool()
            return outputs.present(result, raw=getattr(runner, 'last_raw', None),
                                   metadata={'command_sha256': hashlib.sha256(
                                       json.dumps(getattr(runner, 'command', [])).encode()).hexdigest()})
        return run
    if archive is not None:
        from .artifacts import register_artifact_reader
        register_artifact_reader(registry, archive)
    for specification in FILE_TOOLS:
        if specification.name == "write_file" and not workspace.allow_write:
            continue
        def handler(_name=specification.name, **args):
            return workspace.execute(_name, args)
        registry.register(specification, handler)
    if enhanced:
        from .editing import FileTools
        files = FileTools(workspace)
        registry.register(spec("search_text", "Search a single-line literal within project files.",
                               {"path": PATH, "query": {"type": "string"}}, read_only=True), files.search)
        registry.register(spec("file_snapshot", "Read UTF-8 content and its SHA-256 for targeted editing.",
                               {"path": PATH}, read_only=True), lambda path: archive_inspection(files.snapshot(path), archive))
        if range_reads:
            line = {'type': 'integer', 'minimum': 1, 'maximum': MAX_LINE_NUMBER}
            registry.register(spec('read_file_range',
                                   'Read inclusive 1-based lines (at most 200 lines/12000 characters) and a full-file edit hash.',
                                   {'path': PATH, 'start_line': line, 'end_line': line},
                                   read_only=True), lambda path, start_line, end_line: archive_inspection(
                                       files.read_range(path, start_line, end_line), archive))
            entry = spec('range', '', {'path': {**PATH, 'maxLength': 1024}, 'start_line': line, 'end_line': line}).parameters
            registry.register(spec('batch_inspect',
                                   'Inspect 1–10 related file ranges in one request (24000 total characters). Results include full-file hashes and stability; not an atomic snapshot.',
                                   {'requests': {'type': 'array', 'minItems': 1, 'maxItems': 10, 'items': entry}},
                                   read_only=True), lambda requests: archive_inspection(files.batch_inspect(requests), archive))
        edit_args = {"path": PATH, "expected_sha256": {"type": "string"},
                     "old_text": {"type": "string"}, "new_text": {"type": "string"}}
        registry.register(spec("preview_edit", "Preview one exact replacement without changing the file.",
                               edit_args, read_only=True), files.preview)
        if workspace.allow_write:
            registry.register(spec("apply_edit", "Apply one exact replacement only if the snapshot is current.",
                                   edit_args), files.apply)
    if test_runner is not None:
        registry.register(spec("run_tests", "Run the user-configured test command. Check passed and exit_code.",
                               {}), check_handler(test_runner))
    if lint_runner is not None:
        registry.register(spec("run_lint", "Run the configured lint check. Check passed and exit_code.",
                               {}), check_handler(lint_runner))
    if lint_fix_runner is not None and workspace.allow_write:
        registry.register(spec("run_lint_fix", "Run the configured fixer, then lint again. May edit files; rerun tests afterward.",
                               {}), check_handler(lint_fix_runner))
    for name, runner, description in (
        ('run_security', security_runner, 'Run the configured static security check.'),
        ('run_dependency_audit', dependency_audit_runner, 'Run the configured dependency vulnerability audit; may access the network.'),
    ):
        if runner is not None:
            registry.register(spec(name, description + ' Check passed, exit_code and output; a pass is not a safety guarantee.',
                                   {}), check_handler(runner))
    if any(runner is not None for runner in (test_runner, lint_runner, lint_fix_runner,
                                            security_runner, dependency_audit_runner)):
        register_output_reader(registry, outputs)
    return registry


def archive_inspection(content, archive):
    if archive is None:
        return content
    data = json.loads(content)
    try:
        from .artifacts import receipt_view
        items = data.get('results', [data])
        paths = [item['path'] for item in items if isinstance(item, dict) and item.get('ok', True) is True and isinstance(item.get('path'), str)]
        data['receipt'] = receipt_view(archive.put('inspection', content.encode('utf-8'), {'stable': data.get('stable'), 'paths': paths}))
    except (OSError, ValueError):
        data['archive_error'] = 'Inspection could not be archived.'
    return json.dumps(data, ensure_ascii=False)

import json

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
                schema = properties[name]
                if schema['type'] == 'string':
                    if not isinstance(value, str):
                        raise ValueError('Werkzeugargument muss ein String sein: ' + name)
                elif schema['type'] == 'integer':
                    if (type(value) is not int or value < schema.get('minimum', value)
                            or value > schema.get('maximum', value)):
                        raise ValueError('Ungültiges ganzzahliges Werkzeugargument: ' + name)
                else:
                    raise ValueError('Nicht unterstützter Argumenttyp.')
            result = handler(**args)
            return result if isinstance(result, ToolResult) else ToolResult(True, result)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return ToolResult(False, str(exc))


def workspace_tools(workspace, *, enhanced=True, test_runner=None, lint_runner=None, lint_fix_runner=None,
                    security_runner=None, dependency_audit_runner=None, range_reads=True):
    registry = ToolRegistry()
    from .check_output import CheckOutputStore, register_output_reader
    outputs = CheckOutputStore()
    def check_handler(runner):
        return lambda: outputs.present(runner.as_tool())
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
                               {"path": PATH}, read_only=True), files.snapshot)
        if range_reads:
            line = {'type': 'integer', 'minimum': 1, 'maximum': MAX_LINE_NUMBER}
            registry.register(spec('read_file_range',
                                   'Read inclusive 1-based lines (at most 200 lines/12000 characters) and a full-file edit hash.',
                                   {'path': PATH, 'start_line': line, 'end_line': line},
                                   read_only=True), files.read_range)
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

import json

from agent.core.types import ToolResult, ToolSpec


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
            # The initial tools accept strings only. Extend validation with new types.
            if any(not isinstance(value, str) for value in args.values()):
                raise ValueError("Werkzeugargumente müssen Strings sein.")
            return ToolResult(True, handler(**args))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return ToolResult(False, str(exc))


def workspace_tools(workspace):
    registry = ToolRegistry()
    for specification in FILE_TOOLS:
        if specification.name == "write_file" and not workspace.allow_write:
            continue
        def handler(_name=specification.name, **args):
            return workspace.execute(_name, args)
        registry.register(specification, handler)
    return registry

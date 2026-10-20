import json


class JsonlEventSink:
    """Schreibt Metadaten; keine Dateiinhalte, Prompts oder Zugangsdaten."""
    def __init__(self, stream):
        self.stream = stream

    def emit(self, kind, data):
        self.stream.write(json.dumps({"event": kind, **data}, ensure_ascii=False) + "\n")
        self.stream.flush()

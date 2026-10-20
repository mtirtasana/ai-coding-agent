import json


class JsonlEventSink:
    """Schreibt Metadaten; keine Dateiinhalte, Prompts oder Zugangsdaten."""
    def __init__(self, stream):
        self.stream = stream

    def emit(self, kind, data):
        # ASCII escaping also keeps C1/bidi controls from model-supplied tool names inert.
        self.stream.write(json.dumps({"event": kind, **data}, ensure_ascii=True) + "\n")
        self.stream.flush()

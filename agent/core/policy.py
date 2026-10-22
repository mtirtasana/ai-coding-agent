from .types import ToolResult


MODE_INSTRUCTIONS = {
    "plan": (
        "Modus PLAN: Untersuche die Aufgabe nur lesend. Liefere einen überprüfbaren "
        "Plan mit Annahmen, betroffenen Komponenten und Validierung. Stelle bei "
        "wesentlichen Unklarheiten Fragen. Ändere keine Dateien."
    ),
    "implement": (
        "Modus IMPLEMENT: Setze die Nutzeraufgabe unter Beachtung des akzeptierten "
        "Plans um, soweit die verfügbaren Werkzeuge dies erlauben. Neuere explizite "
        "Nutzeranweisungen können den Plan ändern. Berichte tatsächliche Änderungen "
        "und offene Prüfungen. Testresultate dürfen nur auf tatsächlicher Ausführung beruhen."
    ),
}


class ModeToolExecutor:
    """Enforce capabilities, including calls the model invents despite the schema."""
    def __init__(self, tools, mode):
        if mode not in MODE_INSTRUCTIONS:
            raise ValueError("Modus muss plan oder implement sein.")
        self.tools = tools
        self.specs = tuple(s for s in tools.specifications()
                           if mode == "implement" or s.read_only)
        self.allowed = {s.name for s in self.specs}

    def specifications(self):
        return self.specs

    def execute(self, call):
        if call.name not in self.allowed:
            return ToolResult(False, "Werkzeug im aktuellen Modus nicht verfügbar: " + call.name)
        return self.tools.execute(call)

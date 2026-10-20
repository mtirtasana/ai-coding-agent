# Generischer KI-Coding-Agent

Ein Python-Grundgerüst mit einem anbieterunabhängigen Agent-Kern. Modelle und
Werkzeuge werden über Schnittstellen eingebunden. Keine ChatGPT-Mitgliedschaft
nötig. Python 3.11 oder neuer ist erforderlich.

## Schnellstart ohne API oder Modell

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
coding-agent "Projekt untersuchen" --provider demo --workspace . --verbose
```

Die Demo führt deterministisch einen Verzeichnisaufruf aus. Sie ist kein KI-Modell.
Unter Windows die Umgebung mit `.venv\Scripts\Activate.ps1` aktivieren.

## Lokales Modell mit Ollama

Ollama separat installieren, den Server starten und ein Modell mit Unterstützung
für Tool Calling herunterladen. Den tatsächlichen installierten Modellnamen angeben:

```bash
coding-agent "Erkläre den Projektaufbau" --provider ollama --model MODELLNAME --workspace .
```

Mit `--base-url http://localhost:11434` lässt sich die Serveradresse ändern.
Modellqualität und Werkzeugunterstützung hängen vom ausgewählten Modell ab.

## OpenAI (optional)

```bash
pip install -e '.[openai]'
read -rsp 'OpenAI API key: ' OPENAI_API_KEY
export OPENAI_API_KEY
coding-agent "Erkläre den Projektaufbau" --provider openai --model gpt-4.1
```

API-Nutzung wird separat abgerechnet. Der Kern benötigt weder das OpenAI-SDK noch
Zugangsdaten. Standardanbieter ist für bestehende Aufrufe weiterhin `openai`.
`AGENT_PROVIDER` und `AGENT_MODEL` setzen Defaults; `OPENAI_MODEL` bleibt als
Fallback für den OpenAI-Adapter unterstützt.

## Architektur: Ports and Adapters

Die [ausführliche Architekturdokumentation](docs/architecture.md) erläutert die
Komponenten, Design Patterns, Datenflüsse und Erweiterungspunkte mit Diagrammen.

Ein separater [Architekturvorschlag für Kontextverwaltung und SQLite](docs/context-and-persistence.md)
beschreibt Datenmodell, Cache-Invalidierung und weitere Ausbaustufen. Regelbasierte
Kontextauswahl und Messung sind implementiert; SQLite, Cache und Zusammenfassungen noch nicht.

```mermaid
flowchart TD
    CLI[CLI: Composition Root] --> Agent[Agent-Kern]
    Agent --> Context[ContextBuilder]
    Agent --> Model[ModelProvider-Port]
    Model --> OpenAI[OpenAI-Adapter]
    Model --> Ollama[Ollama-Adapter]
    Model --> Demo[Offline-Demo]
    Agent --> Tools[ToolExecutor-Port]
    Tools --> Registry[ToolRegistry]
    Registry --> Workspace[Lokaler Workspace]
    Agent --> Events[EventSink-Port]
    Events --> JSONL[JSONL-Ereignisse]
```

- `agent/core/types.py`: eigene Nachrichten, Aufrufe, Ergebnisse und Werkzeugschemas.
- `agent/core/ports.py`: Python-Protokolle für Modelle, Werkzeuge und Ereignisse.
- `agent/core/engine.py`: Beobachten → Kontextauswahl → Modellentscheidung → Werkzeug → Ergebnis.
- `agent/core/context.py`: vollständige Runden auswählen und Zeichenbudget prüfen.
- `agent/adapters/models.py`: Übersetzung zwischen internen Typen und Anbieterformaten.
- `agent/adapters/tools.py`: Werkzeugregistrierung, String-Argumentvalidierung und Fehlerergebnisse.
- `agent/files.py`: lokaler Workspace mit bestehenden Pfadbeschränkungen.
- `agent/adapters/events.py`: JSONL-Metadaten ohne Prompts und Dateiinhalte.
- `agent/cli.py`: wählt Adapter aus und verbindet sie mit dem Kern.

Abhängigkeiten zeigen zum Kern. Der Kern kennt weder SDKs noch CLI, HTTP oder
Dateisystem. Für einen neuen Anbieter `ModelProvider.generate(messages, tools)`
implementieren und in der CLI auswählen lassen. Rückgabe ist `ModelResponse`
mit einer `Message` und optionalen tatsächlichen `ModelUsage`-Tokenwerten. Ein Provider muss interne
Assistant-Nachrichten liefern und seine anbieterspezifischen Tool-Formate selbst
übersetzen. Zusätzliche Werkzeuge werden mit `ToolSpec` und Handler registriert;
bei anderen Argumenttypen als Strings muss die Validierung erweitert werden.

## Grenzen des Grundgerüsts

Standardmäßig sind nur `list_files` und `read_file` verfügbar. `--allow-write`
schaltet `write_file` hinzu; dabei können Dateien vollständig ersetzt werden.
Patch-Anwendung, Testprozesse, Git-Werkzeuge, Container-Isolation, Anthropic-Adapter
und automatische Auswahl relevanter Dateiabschnitte sind noch nicht implementiert.

`--max-steps 15` begrenzt Modellaufrufe. `--max-context-chars 200000` begrenzt den
ungefähren Zeichenumfang vor dem nächsten Aufruf, keine Tokens oder Kosten.
Standard ist `--context-mode recent`: Systemanweisungen und Nutzeraufgabe bleiben
immer erhalten, danach werden die neuesten vollständigen Assistant-/Werkzeugrunden
bis zum Budget ausgewählt. Ältere Runden entfallen zuerst aus dem Modellkontext.
Die vollständige Historie bleibt unverändert erhalten. Informationen aus älteren
Runden können dem Modell deshalb fehlen; es gibt keine automatische Zusammenfassung.
Passt bereits der Pflichtkontext oder die neueste Runde nicht hinein, wird abgebrochen.
`--context-mode full` überträgt wie bisher die gesamte Historie oder bricht am Limit ab.
Einzelne Nachrichten werden nie abgeschnitten.
Bereits geschriebene Änderungen bleiben bei Abbruch oder API-Fehler bestehen.
Jeder neue Lauf beginnt mit eigenem Kontext. `--verbose` schreibt Ereignisse
auf stderr; diese können mit `2>events.jsonl` gespeichert werden.
`context_selected` zeigt ursprünglichen/ausgewählten Zeichenumfang, Nachrichtenzahl
und entfernte Runden. `model_completed.usage` enthält verfügbare tatsächliche
Tokenwerte; fehlende Werte sind `null`, keine Schätzung und keine Nullmessung.
Zeichenwerte sind keine Tokens und schließen nicht die gesamte Anbieterformatierung ein.

```bash
coding-agent "Projekt untersuchen" --provider demo --context-mode recent --verbose
coding-agent "Projekt untersuchen" --provider demo --context-mode full --verbose
```

Bei direkter Python-Nutzung enthält `RunResult.requests` die Auswahlstatistiken
und optionalen Nutzungswerte pro Modellaufruf. `RunResult.messages` bleibt vollständig.

Aufgelöste Pfade müssen im Workspace liegen. `.git`, `.ssh`, `.aws` und Namen
beginnend mit `.env` sind gesperrt. Das ist keine Betriebssystem-Sandbox und schützt
nicht gegen gleichzeitig manipulierte Symlinks. Eine isolierte Arbeitskopie ohne
Geheimnisse verwenden. Gelesene Inhalte werden an den gewählten Modellserver
übermittelt. Änderungen mit `git diff` prüfen. Der Agent kann keine Tests ausführen
und darf deshalb keine erfolgreiche Testausführung behaupten.

## Entwicklung und Prüfungen

```bash
python -m unittest discover -s tests -v
```

Tests benötigen weder SDKs noch API-Schlüssel. Sie prüfen Pfadgrenzen,
Werkzeugvalidierung, den Agent-Zyklus, Limits, Fehlerweitergabe und Übersetzungen
beider Modelladapter mit simulierten Antworten. Live-Tests gegen Modellserver
müssen separat erfolgen.

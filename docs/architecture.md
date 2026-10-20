# Architektur des generischen Coding-Agenten

Diese Dokumentation beschreibt die implementierte Architektur der Version 0.2.
Sie richtet sich an Entwicklerinnen und Entwickler, die den Agenten verstehen,
einen Modellanbieter ergänzen oder neue Werkzeuge anbinden möchten.

## Inhaltsverzeichnis

1. [Ziele und Umfang](#ziele-und-umfang)
2. [Design Pattern: Ports and Adapters](#design-pattern-ports-and-adapters)
3. [Komponenten und Abhängigkeiten](#komponenten-und-abhängigkeiten)
4. [Datenmodell und Verträge](#datenmodell-und-verträge)
5. [Agent-Kern und Arbeitszyklus](#agent-kern-und-arbeitszyklus)
6. [Modelladapter](#modelladapter)
7. [Werkzeugverwaltung und Workspace](#werkzeugverwaltung-und-workspace)
8. [Kontext, Limits und Fehler](#kontext-limits-und-fehler)
9. [Ereignisse und Protokollierung](#ereignisse-und-protokollierung)
10. [CLI und Konfiguration](#cli-und-konfiguration)
11. [Erweiterungen implementieren](#erweiterungen-implementieren)
12. [Tests und Qualitätsgrenzen](#tests-und-qualitätsgrenzen)
13. [Architekturentscheidungen und nächste Schritte](#architekturentscheidungen-und-nächste-schritte)

## Ziele und Umfang

Der Agent soll Coding-Aufgaben bearbeiten können, ohne seinen Arbeitszyklus an
ChatGPT, ein bestimmtes Modell oder ein SDK zu binden. Ein Nutzer übergibt eine
Aufgabe und einen Workspace. Das Modell entscheidet, welche angebotenen Werkzeuge
es benötigt. Der Agent führt die Aufrufe aus und gibt deren Ergebnisse an das
Modell zurück, bis eine Abschlussantwort oder ein Limit erreicht ist.

Aktuell gibt es drei Dateiwerkzeuge, Adapter für die OpenAI Responses API und
Ollamas Chat API sowie eine deterministische Offline-Demo. Der Kern arbeitet
synchron und führt Werkzeugaufrufe nacheinander aus. Es gibt einen Agenten pro
Lauf, keine verteilten Dienste und keine Datenbank.

Die Bezeichnung „generisch“ bezieht sich auf austauschbare Modellanbieter und
bearbeitbare Projekte. Sie bedeutet nicht, dass jedes Modell dieselben Fähigkeiten
hat: Für die Modelladapter ist Unterstützung für strukturierte Werkzeugaufrufe
nötig. Das Grundgerüst führt noch keine Tests, Builds oder Git-Befehle selbst aus.

## Design Pattern: Ports and Adapters

Das zentrale Muster ist **Ports and Adapters**, auch hexagonale Architektur.
Der Kern beschreibt die Arbeit und die benötigten Fähigkeiten. Ports definieren
Verträge für diese Fähigkeiten. Adapter erfüllen die Verträge mit konkreten
Bibliotheken, APIs oder Dateisystemzugriffen.

Beispiel: Der Kern fragt `ModelProvider.generate(messages, tools)` auf. Er kennt
dabei eigene `Message`- und `ToolSpec`-Objekte. Der OpenAI-Adapter übersetzt diese
in die Responses API; der Ollama-Adapter übersetzt sie in die Chat API. Der Kern
bleibt bei einem Anbieterwechsel unverändert.

```mermaid
flowchart LR
    CLI["CLI: Einstieg und Verdrahtung"] --> Core["Agent-Kern"]
    Core --> MP["Port: ModelProvider"]
    Core --> TP["Port: ToolExecutor"]
    Core --> EP["Port: EventSink"]
    OA["OpenAIProvider"] -. "implementiert" .-> MP
    OL["OllamaProvider"] -. "implementiert" .-> MP
    DM["DemoProvider"] -. "implementiert" .-> MP
    TR["ToolRegistry"] -. "implementiert" .-> TP
    JL["JsonlEventSink"] -. "implementiert" .-> EP
    TR --> WS["Workspace: lokale Dateien"]
```

Die durchgezogenen Pfeile zeigen Nutzung, die gestrichelten Implementierung.
Python-`Protocol` ermöglicht strukturelle Typisierung: Ein Adapter muss nicht von
einer gemeinsamen Basisklasse erben. Passende Methodensignaturen genügen für den
Vertrag; zur Laufzeit werden diese Signaturen nicht automatisch geprüft.

Drei ergänzende Muster helfen bei der Umsetzung:

- **Dependency Injection:** `Agent` erhält Modell, Werkzeuge und optionalen
  Ereignisempfänger im Konstruktor. Er erzeugt sie nicht selbst.
- **Strategy:** Der Modellanbieter ist eine austauschbare Strategie für die
  nächste Entscheidung. Die Auswahl findet vor dem Lauf statt.
- **Registry:** Werkzeugspezifikationen und Handler werden unter einem eindeutigen
  Namen registriert. Neue Werkzeuge benötigen keine Fallunterscheidung im Kern.

## Komponenten und Abhängigkeiten

```mermaid
flowchart TB
    subgraph Entry["Einstieg"]
        CLI["agent/cli.py"]
    end
    subgraph Core["Anbieterunabhängiger Kern"]
        Engine["core/engine.py: Agent"]
        Ports["core/ports.py: Protokolle"]
        Types["core/types.py: Datentypen"]
        Engine --> Ports
        Engine --> Types
        Ports --> Types
    end
    subgraph Adapters["Konkrete Adapter"]
        Models["adapters/models.py"]
        Tools["adapters/tools.py"]
        Events["adapters/events.py"]
        Files["files.py: Workspace"]
        Tools --> Files
    end
    CLI --> Engine
    CLI --> Models
    CLI --> Tools
    CLI --> Events
    CLI --> Files
    Models --> Types
    Tools --> Types
```

Dieses Diagramm zeigt Codeabhängigkeiten. Der Kern importiert keine Adapter,
kein SDK, kein HTTP-Modul und keine CLI. Die CLI ist die **Composition Root**:
Hier werden konkrete Objekte ausgewählt, erzeugt und miteinander verbunden.

| Datei | Verantwortung | Bewusst außerhalb ihrer Verantwortung |
| --- | --- | --- |
| `agent/core/engine.py` | Arbeitszyklus, Historie, Limits und Abschluss | HTTP, Modellformate, Dateizugriffe |
| `agent/core/ports.py` | Verträge für Modell, Werkzeuge und Ereignisse | Konkrete Implementierungen |
| `agent/core/types.py` | Gemeinsame anbieterunabhängige Datentypen | SDK-Typen und Persistenz |
| `agent/adapters/models.py` | Übersetzung und Modellkommunikation | Werkzeugausführung |
| `agent/adapters/tools.py` | Registrierung, Argumentvalidierung, Fehlerergebnisse | Entscheidung über nächste Aktion |
| `agent/files.py` | Lokale Dateizugriffe und Pfadbeschränkungen | Modellkommunikation |
| `agent/adapters/events.py` | Ereignisse als JSONL schreiben | Speicherung vollständiger Gespräche |
| `agent/cli.py` | Konfiguration, Adapterauswahl, Ausgabe und Exit-Fehler | Fachlicher Agent-Zyklus |

Der Workspace ist konzeptionell ein Infrastrukturadapter. Er bleibt für die
Kompatibilität mit dem ersten Projektstand unter `agent/files.py`. Einen eigenen
Workspace-Port gibt es derzeit nicht: Der Kern ist über `ToolExecutor` bereits
von der konkreten Dateisystemimplementierung getrennt.

## Datenmodell und Verträge

```mermaid
classDiagram
    class Message {
        role: str
        content: str
        tool_calls: tuple
        call_id: str or None
    }
    class ToolCall {
        id: str
        name: str
        arguments: str
    }
    class ToolSpec {
        name: str
        description: str
        parameters: dict
    }
    class ToolResult {
        ok: bool
        content: str
    }
    class RunResult {
        text: str
        steps: int
        messages: list
    }
    Message "1" o-- "0..*" ToolCall
    RunResult "1" o-- "0..*" Message
```

### Nachrichten und Aufrufe

`Message` repräsentiert die Rollen `system`, `user`, `assistant` oder `tool`.
Die Rollen sind aktuell Strings statt eines Enums. Der Kern prüft beim
Modellergebnis ausdrücklich, dass die Rolle `assistant` ist.

Eine Assistant-Nachricht kann Text und mehrere `ToolCall`-Objekte enthalten.
`ToolCall.id` verbindet einen Aufruf mit seiner Ergebnisnachricht;
`Message.call_id` ist in einer Tool-Nachricht die Referenz darauf. `arguments`
ist ein JSON-String. Dadurch bleibt auch ungültiges JSON bis zur zentralen
Werkzeugvalidierung darstellbar und kann als verständlicher Fehler zurückgehen.
Der Kern verwirft doppelte Aufruf-IDs innerhalb einer einzelnen Modellantwort.

`ToolSpec` enthält Name, Beschreibung und ein JSON-Schema für die Argumente.
Diese Informationen beschreiben das Werkzeug für das Modell. Sie ersetzen nicht
die Validierung vor der tatsächlichen Ausführung.

### Ergebnisse

`ToolResult` transportiert entweder Erfolg oder einen erwarteten Werkzeugfehler.
Der Kern serialisiert das Ergebnis als JSON in eine Tool-Nachricht:

```json
{"ok": false, "content": "Werkzeug nicht verfügbar: unknown"}
```

Das Modell kann diesen Fehler sehen und eine andere Aktion wählen. `RunResult`
entsteht dagegen nur bei regulärem Abschluss und enthält den Abschlusstext,
die Zahl der Modellaufrufe und die gesamte interne Nachrichtenhistorie.
Die Historie wird nicht automatisch auf die Festplatte geschrieben.

`Message`, `ToolCall`, `ToolSpec` und `ToolResult` sind eingefrorene Dataclasses.
Das verhindert das Neuzuweisen ihrer Felder; verschachtelte Dictionaries wie
`ToolSpec.parameters` werden dadurch nicht tief unveränderlich.

### Ports

| Port | Methoden | Erwartung |
| --- | --- | --- |
| `ModelProvider` | `generate(messages, tools) -> Message` | Anbieterformate intern halten und eine Assistant-Nachricht liefern |
| `ToolExecutor` | `specifications()` und `execute(call)` | Angebotene Werkzeuge beschreiben und strukturierte Ergebnisse liefern |
| `EventSink` | `emit(kind, data)` | Ereignisse empfangen; optional für den gesamten Lauf |

## Agent-Kern und Arbeitszyklus

`Agent.run(task)` startet bei jedem Aufruf mit einer frischen Historie aus den
festen Systemanweisungen und der Nutzeraufgabe. Die verfügbaren Werkzeuge werden
zu Beginn als Tuple übernommen. Ein dynamischer Werkzeugwechsel während eines
Laufs ist derzeit nicht vorgesehen.

```mermaid
sequenceDiagram
    actor User as Nutzer
    participant CLI
    participant Agent
    participant Model as ModelProvider
    participant Tools as ToolExecutor
    participant WS as Workspace
    User->>CLI: Aufgabe und Optionen
    CLI->>Agent: run(task)
    Agent->>Tools: specifications()
    loop Bis Abschluss oder Limit
        Agent->>Agent: Kontextumfang prüfen
        Agent->>Model: generate(messages, tools)
        Model-->>Agent: Assistant-Nachricht
        alt Werkzeugaufrufe vorhanden
            loop Jeden Aufruf nacheinander
                Agent->>Tools: execute(call)
                Tools->>WS: Dateifunktion, falls Argumente gültig
                WS-->>Tools: Inhalt oder Fehler
                Tools-->>Agent: ToolResult
                Agent->>Agent: Ergebnis zur Historie hinzufügen
            end
        else Keine Werkzeugaufrufe
            Agent-->>CLI: RunResult
        end
    end
    CLI-->>User: Abschlusstext oder Fehlermeldung
```

Die Schleife entspricht dem praktischen **ReAct-Ablauf**: beobachten, nächste
Aktion entscheiden, handeln und das Ergebnis wieder beobachten. Es wird kein
separates internes Gedankentranskript gespeichert. Die sichtbare Steuerung
besteht aus Nachrichten und expliziten Werkzeugaufrufen.

Eine Antwort ohne Werkzeugaufrufe gilt als Abschluss, auch wenn ihr Text leer
ist. Das ist eine Steuerungsentscheidung, kein Nachweis, dass die Nutzeraufgabe
inhaltlich erfüllt wurde. Die Implementierung hat noch keinen unabhängigen
Ergebnisprüfer und keine automatische Erfolgsmessung.

`AGENTS.md` wird nicht automatisch eingelesen oder vorab injiziert. Die festen
Systemanweisungen fordern das Modell auf, die Datei zu beachten, falls vorhanden;
das Modell muss sie mit `read_file` selbst lesen. Ebenso gibt es noch keinen
separaten Planer oder eine automatische Suche nach relevanten Dateien.

## Modelladapter

### OpenAIProvider

Der Adapter übersetzt interne Nachrichten in Eingabeelemente der Responses API.
Textnachrichten erhalten Rolle und Inhalt, Aufrufe werden `function_call` und
Ergebnisse werden `function_call_output`. Die Aufruf-ID bleibt erhalten.
`ToolSpec` wird zu einer Funktionsdefinition mit `strict=True`.

Der Request enthält die gesamte bisherige Historie und `store=False`. Die
Implementierung setzt keinen serverseitigen Gesprächsfortsetzungsbezeichner ein.
Die Antwort wird auf Ausgabetext und Funktionsaufrufe abgebildet.

Das OpenAI-SDK wird erst beim Erstellen eines Adapters ohne übergebenen Client
importiert. Deshalb laufen Kern, Demo und Ollama ohne SDK-Installation. Tests
können einen simulierten Client übergeben.

Die Abbildung umfasst aktuell Text und Werkzeugaufrufe. Bilder, Streaming,
anbieterspezifische Reasoning-Elemente, Nutzungsstatistiken und andere spezialisierte
Ausgabearten werden nicht als eigene interne Datentypen erhalten. Unterstützung
für beliebige Modellfamilien ist damit nicht durch die Architektur allein garantiert.

### OllamaProvider

Dieser Adapter sendet einen synchronen JSON-POST an `/api/chat` der konfigurierten
Basisadresse. Er verwendet nur die Python-Standardbibliothek. `stream=False`
fordert eine vollständige Antwort statt eines Streams an; der Netzwerk-Timeout
beträgt standardmäßig 120 Sekunden und ist im Adapter-Konstruktor änderbar.

Interne Funktionsargumente werden in JSON-Objekte übersetzt. Für Tool-Ergebnisse
ordnet der Adapter die interne Aufruf-ID wieder dem Werkzeugnamen zu und trägt
`tool_name` in die Ollama-Nachricht ein. IDs für zurückgelieferte Aufrufe werden
lokal aus Historienlänge und Aufrufindex erzeugt.

Ein laufender Server und ein installiertes Modell mit Werkzeugunterstützung sind
Voraussetzungen. Der Adapter installiert oder startet Ollama nicht. Eine explizite
Authentifizierung für einen entfernten Ollama-Server ist noch nicht implementiert.

### DemoProvider

Die Demo ist kein KI-Modell. Bei der ersten Nutzeraufgabe fordert sie fest
`list_files` mit `{"path":"."}` an. Danach schließt sie mit dem empfangenen
Werkzeugergebnis ab. Damit lässt sich der komplette Zyklus ohne Netzwerk,
API-Schlüssel und Modellinstallation ausprobieren.

## Werkzeugverwaltung und Workspace

### ToolRegistry

Die Registry verbindet `ToolSpec.name` mit einem Handler. Ein Name darf nur
einmal registriert werden. `specifications()` liefert die Beschreibungen für das
Modell. `execute()` nimmt einen konkreten Aufruf entgegen.

```mermaid
flowchart TD
    Call["ToolCall"] --> Name{"Werkzeug registriert?"}
    Name -->|Ja| JSON["JSON dekodieren"]
    JSON --> Args{"Objekt, exakte Schlüsselliste und String-Werte?"}
    Args -->|Ja| Handler["Handler aufrufen"]
    Handler --> OK["ToolResult: ok=true"]
    Name -->|Nein| Error["ToolResult: ok=false"]
    Args -->|Nein| Error
    JSON -->|Dekodierungsfehler| Error
    Handler -->|Erwarteter Fehler| Error
```

Die Validierung verlangt aktuell exakt die im Schema genannten Schlüssel und
String-Werte für alle Argumente. Sie ist auf die drei vorhandenen Dateiwerkzeuge
zugeschnitten; sie ist kein vollständiger JSON-Schema-Validator. Optionale Felder,
Zahlen, Arrays oder verschachtelte Objekte erfordern eine Erweiterung.

`OSError`, `ValueError`, `KeyError` und `TypeError` werden in Fehlerergebnisse
umgewandelt. Andere Exceptions werden weitergereicht und beenden den Lauf, wenn
sie nicht außerhalb der Registry behandelt werden.

### workspace_tools und Fähigkeiten

`workspace_tools(workspace)` registriert `list_files` und `read_file`.
Nur bei `workspace.allow_write=True` kommt `write_file` hinzu. Im Lesemodus sieht
das Modell dieses Werkzeug also nicht. Ein trotzdem erzeugter Schreibaufruf wird
von der Registry abgelehnt. Zusätzlich prüft der Workspace Schreibberechtigung
noch einmal selbst.

| Werkzeug | Argumente | Verhalten |
| --- | --- | --- |
| `list_files` | `path` | Listet direkte Verzeichniseinträge; keine rekursive Suche |
| `read_file` | `path` | Liest UTF-8-Text bis maximal 100.000 Zeichen |
| `write_file` | `path`, `content` | Erstellt Elternverzeichnisse und ersetzt vollständigen Dateiinhalt; maximal 100.000 Zeichen |

Die Verzeichnisliste betrachtet die ersten 500 sortierten Einträge und filtert
dann geschützte Namen. Sie ist daher nicht zwingend vollständig und kann weniger
als 500 sichtbare Einträge enthalten. Symlink-Namen können in einer Liste
auftauchen; beim tatsächlichen Zugriff wird der aufgelöste Zielpfad geprüft.

### Workspace und Vertrauensgrenzen

```mermaid
flowchart LR
    Model["Modell: schlägt Aktionen vor"] --> Registry["Registry: Werkzeug und Argumente prüfen"]
    Registry --> Workspace["Workspace: Pfad und Schreibmodus prüfen"]
    Workspace --> Files["Lokales Dateisystem"]
    Files -. "gelesene Inhalte über Historie" .-> Model
```

Der Workspace muss existieren und ein Verzeichnis sein. Pfade werden aufgelöst;
das Ziel muss innerhalb des aufgelösten Workspace liegen. Dadurch werden auch
Symlinks nach außerhalb abgewiesen. `.git`, `.ssh`, `.aws` und Pfadbestandteile,
die mit `.env` beginnen, sind gesperrt. Absolute Pfade werden nicht grundsätzlich
verboten, solange ihr aufgelöstes Ziel innerhalb des Workspace liegt.

Diese Prüfungen sind keine Betriebssystem-Sandbox. Gleichzeitig veränderte
Symlinks sind nicht abgesichert. Andere vertrauliche Dateien innerhalb des
Workspace können gelesen und an den Modellserver übermittelt werden. Auch ein
lokaler Server ist nur dann lokal, wenn seine konfigurierte Adresse es ist.
Eine dedizierte Arbeitskopie ohne Geheimnisse ist deshalb das vorgesehene
Arbeitsverzeichnis.

Schreiboperationen sind weder transaktional noch mit automatischem Rollback
versehen. Nach einem späteren Fehler bleiben Änderungen bestehen; ein Fehler
während des Schreibens kann ebenfalls eine teilweise geänderte Datei hinterlassen.
Dateien sind nach dem Lauf über Git-Diffs und passende Tests zu prüfen.

## Kontext, Limits und Fehler

Die Historie lebt ausschließlich in `Agent.run()`. Sie enthält Systemanweisungen,
Aufgabe, Assistant-Antworten und Werkzeugergebnisse. Jeder Modellaufruf erhält
den bisherigen Verlauf. Es gibt keine automatische Auswahl, Zusammenfassung,
Vektordatenbank, Persistenz oder Wiederaufnahme eines abgebrochenen Laufs.

```mermaid
stateDiagram-v2
    [*] --> Initialisieren
    Initialisieren --> Kontextpruefung
    Kontextpruefung --> Modellaufruf: Umfang im Limit
    Kontextpruefung --> Abbruch: Kontextlimit erreicht
    Modellaufruf --> Abschluss: Keine Werkzeugaufrufe
    Modellaufruf --> Werkzeuge: Werkzeugaufrufe vorhanden
    Werkzeuge --> Kontextpruefung: Weitere Schritte erlaubt
    Werkzeuge --> Abbruch: Schrittlimit erreicht
    Modellaufruf --> Abbruch: API-Fehler oder ungueltige Antwort
    Werkzeuge --> Abbruch: Nicht behandelter Fehler
    Abschluss --> [*]
    Abbruch --> [*]
```

`max_steps` begrenzt die Zahl der Modellaufrufe, nicht die Anzahl der einzelnen
Werkzeugaufrufe. Eine Modellantwort kann mehrere Werkzeuge anfordern. Auch im
letzten zulässigen Schritt werden diese noch ausgeführt; anschließend wird bei
fehlendem Abschluss `StepLimitError` ausgelöst.

`max_context_chars` prüft vor jedem Modellaufruf einen ungefähren Zeichenumfang:
Nachrichtentexte, Aufrufnamen und -argumente sowie Werkzeugbeschreibungen und
serialisierte Parameterschemas. Rollen, IDs und Anbieterformatierung werden
nicht vollständig mitgerechnet. Die Grenze ist weder ein Tokenbudget noch eine
Kostenobergrenze und begrenzt nicht die Größe der nächsten Modellantwort.

| Situation | Reaktion |
| --- | --- |
| Erwarteter Datei- oder Argumentfehler | `ToolResult(ok=False)` geht an das Modell |
| Zeichenumfang vor nächstem Modellaufruf zu groß | `ContextLimitError` |
| Kein Abschluss innerhalb der erlaubten Modellaufrufe | `StepLimitError` |
| Andere Rolle als `assistant` oder doppelte IDs innerhalb einer Antwort | `ValueError` |
| API-, Netzwerk- oder nicht behandelter Adapterfehler | Exception erreicht die CLI |
| CLI fängt eine Exception im Ausführungsblock | Fehlermeldung auf stderr, Exit-Code 1 |

Es gibt keine einheitliche Retry-Strategie im Kern. Einzelne SDKs können eigene
Wiederholungen und Timeouts besitzen. Der Agent setzt auch keine globale
Laufzeitgrenze. Ein allgemeines Abbruchereignis für jede denkbare Exception ist
noch nicht implementiert.

## Ereignisse und Protokollierung

`EventSink` ist optional. Ohne Sink läuft der Agent ohne Ereignisausgabe.
`JsonlEventSink` schreibt pro Ereignis ein JSON-Objekt in eine Zeile und leert
den Stream-Puffer sofort. Die CLI aktiviert ihn mit `--verbose` auf stderr.

| Ereignis | Daten |
| --- | --- |
| `run_started` | `max_steps` |
| `model_completed` | `step`, `tool_calls` |
| `tool_completed` | `name`, `ok` |
| `run_completed` | `steps` |
| `run_stopped` | `reason`: `context_limit` oder `step_limit` |

```bash
python -m agent.cli "Projekt untersuchen" --provider demo --verbose 2>events.jsonl
```

Die aktuellen Ereignisse enthalten keine Aufgaben, Dateiinhalte oder
Werkzeugargumente. Sie enthalten auch keine Zeitstempel, Lauf-IDs oder
Token-/Kostenwerte. Ein Ereignisprotokoll ist somit eine Ausführungsübersicht,
kein vollständiges Audit oder wiederabspielbares Gespräch. Fehler beim Schreiben
des Sinks werden nicht abgefangen und können den Lauf beenden.

## CLI und Konfiguration

Die CLI parst Argumente, wählt Adapter und erzeugt den Workspace. Danach verbindet
sie `Agent(model, tools, events, ...)`, ruft `run()` auf und gibt den Abschlusstext
aus. Damit bleiben Konfigurationsdetails außerhalb des Kerns.

| Option | Standard und Bedeutung |
| --- | --- |
| `task` | Erforderliche Aufgabe als Positionsargument |
| `--workspace` | Aktuelles Arbeitsverzeichnis |
| `--provider` | `AGENT_PROVIDER`, sonst `openai`; Auswahl `openai`, `ollama`, `demo` |
| `--model` | `AGENT_MODEL`; bei OpenAI danach `OPENAI_MODEL`, sonst `gpt-4.1` |
| `--base-url` | `http://localhost:11434`, nur für Ollama |
| `--max-steps` | 15 Modellaufrufe, muss positiv sein |
| `--max-context-chars` | 200.000 ungefähre Zeichen, muss positiv sein |
| `--allow-write` | Standardmäßig aus |
| `--verbose` | Standardmäßig aus; JSONL-Metadaten auf stderr |

Ollama benötigt einen explizit oder über `AGENT_MODEL` gewählten Modellnamen.
OpenAI benötigt `OPENAI_API_KEY` und die optionale Installation
`pip install -e '.[openai]'`. `pip install -e .` installiert nur das Grundgerüst.
Die Demo benötigt keine Schlüssel und ignoriert die fachliche Aufgabe.

## Erweiterungen implementieren

### Einen Modellanbieter ergänzen

1. Einen Adapter außerhalb von `agent/core` erstellen.
2. `generate(messages, tools)` implementieren und interne Typen in das
   Anbieterformat übersetzen.
3. Textantworten und Werkzeugaufrufe einschließlich ihrer Zuordnung wieder als
   `Message(role="assistant", ...)` zurückgeben.
4. Den Adapter in der CLI beziehungsweise einem späteren Konfigurationsmodul
   auswählbar machen und Bibliotheken bei Bedarf optional deklarieren.
5. Die Übersetzung und mehrere aufeinanderfolgende Werkzeugrunden testen.

Minimaler Testadapter ohne externe API:

```python
from agent.core.types import Message

class StaticProvider:
    def generate(self, messages, tools):
        return Message("assistant", "Keine Änderungen erforderlich.")
```

Ein zukünftiger Anthropic-Adapter folgt demselben Vertrag; er ist derzeit noch
nicht implementiert. Bei erweiterten Formaten wie multimodalen Inhalten muss
gegebenenfalls zuerst das gemeinsame Datenmodell erweitert werden.

### Ein Werkzeug ergänzen

Ein Werkzeug benötigt eine Spezifikation und eine aufrufbare Funktion.
Dieses Beispiel bleibt absichtlich bei den aktuell unterstützten String-Argumenten:

```python
from agent.adapters.tools import ToolRegistry, spec
from agent.core.types import ToolCall

registry = ToolRegistry()
registry.register(
    spec("echo", "Gibt den Text zurück.", {"text": {"type": "string"}}),
    lambda text: text,
)
result = registry.execute(ToolCall("example-1", "echo", '{"text":"Hallo"}'))
assert result.ok and result.content == "Hallo"
```

Für ein Dateiewerkzeug weiterhin `Workspace.path()` und den Schreibmodus prüfen.
Für neue Typen oder optionale Felder zuerst die Registry-Validierung erweitern.
Ein JSON-Schema allein macht einen Handler nicht sicher oder korrekt.

### Andere Ein- und Ausgaben anbinden

Ein späteres Web-API oder eine IDE-Integration kann dieselben Ports und denselben
Kern verwenden. Es muss Modell, Werkzeuge und Ereignissink selbst zusammenstellen.
Ein neuer `EventSink` kann die Metadaten beispielsweise in eine Datenbank schreiben.
Diese Anpassungen benötigen keine Änderung des normalen Agent-Zyklus.

## Tests und Qualitätsgrenzen

```bash
python -m unittest discover -s tests -v
```

`tests/test_files.py` prüft Schreib-/Lesezugriffe, den Lesemodus, Pfadausbrüche,
Symlinks nach außerhalb, geschützte Pfade und Dateigrößen.

`tests/test_architecture.py` prüft den vollständigen Offline-Zyklus,
Werkzeugvalidierung, Fehlerrückgabe an das Modell, Schritt- und Kontextlimits,
getrennte Historien, Registry-Routing sowie die OpenAI- und Ollama-Übersetzungen
mit simulierten Antworten. Die Tests benötigen kein SDK und keinen API-Schlüssel.

Der dokumentierte Implementierungsstand besteht aus 16 erfolgreichen lokalen
Tests und einem ausgeführten Offline-CLI-Durchlauf. Das belegt die getesteten
Verträge und Pfade, keine erfolgreiche Kommunikation mit echten Modellservern.
Live-Tests, Kompatibilitätsprüfungen mehrerer Modelle und eine Überprüfung der
inhaltlichen Coding-Ergebnisse bleiben separate Aufgaben.

## Architekturentscheidungen und nächste Schritte

| Entscheidung | Begründung | Konsequenz |
| --- | --- | --- |
| Ein synchroner Agent | Einfacher, nachvollziehbarer Start | Keine parallelen Modell- oder Werkzeugaufrufe |
| Eigene Datentypen | Anbieterwechsel ohne SDK-Typen im Kern | Adapter müssen Formate und Besonderheiten abbilden |
| Protocol statt Pflicht-Basisklasse | Leichte Testdoubles und geringe Kopplung | Keine automatische Laufzeitprüfung der Verträge |
| OpenAI als optionale Abhängigkeit | Lokaler Betrieb ohne Cloud-SDK | OpenAI muss separat installiert werden |
| Expliziter Abbruch am Kontextlimit | Keine still verlorenen Anweisungen oder Tool-Ergebnisse | Große Aufgaben müssen aufgeteilt werden |
| Vollständige Dateien statt Patches | Kleiner erster Werkzeugsatz | Unbeabsichtigte Überschreibungen sind möglich |
| Metadaten statt vollständiger Gesprächslogs | Kleine, einfache Ereignisausgabe | Keine automatische Wiederaufnahme oder vollständige Rekonstruktion |

Sinnvolle nächste Ausbaustufen sind eine gezielte Patch-Anwendung, kontrollierte
Testprozesse in isolierten Workspaces und automatische Auswahl relevanter
Projektdateien. Bei Testausführung müssen zugleich die Systemanweisungen, neue
Werkzeugverträge, Prozesslimits und Fehlerbehandlung angepasst werden.

Git-Funktionen, Container-Workspaces und ein Anthropic-Adapter können danach an
den vorhandenen Grenzen ergänzt werden. Multi-Agent-Orchestrierung ist kein
Bestandteil des Grundgerüsts und sollte erst bei konkretem Bedarf hinzukommen.

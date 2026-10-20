# KI Coding Agent

Ein Python-Starter für einen eigenen KI-Coding-Agenten mit der OpenAI Responses API.
Er untersucht Projektdateien, plant Änderungen im Modell und kann UTF-8-Dateien
erstellen oder ersetzen. Standardmäßig arbeitet er nur lesend.

## Start

Voraussetzungen: Python 3.11+, ein OpenAI-API-Schlüssel und Zugriff auf das gewählte
Modell. Die API wird separat von einem ChatGPT-Abonnement abgerechnet.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
# Schlüssel verdeckt einlesen (Bash):
read -rsp 'OpenAI API key: ' OPENAI_API_KEY
export OPENAI_API_KEY
echo
coding-agent "Erkläre den Projektaufbau" --workspace ./
coding-agent "Ergänze eine Funktion zum Addieren" --workspace ./ --allow-write
```

Unter Windows die virtuelle Umgebung mit `.venv\Scripts\Activate.ps1` aktivieren
und `OPENAI_API_KEY` in der PowerShell-Umgebung setzen.

Mit `--model` oder `OPENAI_MODEL` lässt sich das Modell wechseln; Standard ist
`gpt-4.1`. `--max-steps 15` begrenzt die Anzahl der Modellaufrufe. Das ist keine
feste Kostenobergrenze. Gelesene Dateien werden an die OpenAI-API gesendet.

## Funktionsumfang und Grenzen

- Werkzeuge: `list_files`, `read_file`, `write_file`.
- Schreibzugriff erfordert `--allow-write` und kann bestehende Dateien ersetzen.
- Aufgelöste Dateipfade müssen innerhalb des gewählten Workspace liegen.
- `.git`, `.ssh`, `.aws` und Namen beginnend mit `.env` sind gesperrt.
- Der Agent führt keine Shell-Befehle oder Tests aus und erstellt keine Commits.
- Pfadprüfungen sind keine Betriebssystem-Sandbox. Nutze eine isolierte
  Arbeitskopie ohne Geheimnisse; beliebige andere Dateien im Workspace können
  gelesen werden. Gleichzeitige Änderungen an Symlinks sind nicht abgesichert.
- Bei einem API-Fehler oder Schrittlimit bleiben bereits geschriebene Änderungen
  bestehen. Änderungen anschließend mit `git diff` prüfen.

## Tests

Die lokalen Werkzeugtests benötigen keinen API-Schlüssel und keine Zusatzpakete:

```bash
python -m unittest discover -s tests -v
```

GitHub Actions führt dieselben Tests für Pushes und Pull Requests aus.
Die Tests decken die Dateizugriffe ab; sie ersetzen keinen Live-Test mit dem Modell.

## Repository verwenden

```bash
git clone https://github.com/mtirtasana/ai-coding-agent.git
cd ai-coding-agent
```

Anschließend die Installation unter „Start“ ausführen. Für Codex das Repository
in der Repository-Auswahl auswählen und eine Umgebung erstellen.

## Aufbau

`agent/cli.py` enthält die Kommandozeile und den Modell-Werkzeug-Zyklus.
`agent/files.py` implementiert Dateizugriffe. `tests/` prüft die Grenzen dieser
Werkzeuge. `AGENTS.md` enthält Hinweise für Codex und andere Coding-Agenten.

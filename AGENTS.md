# Hinweise für Coding-Agenten

- Python 3.11 oder neuer verwenden.
- Änderungen klein und auf die Aufgabe begrenzt halten.
- Keine Zugangsdaten in Dateien oder Commits schreiben.
- Die Pfadbeschränkungen in `agent/files.py` erhalten.
- Prüfen mit `python -m unittest discover -s tests -v`.
- Linting mit `python -m ruff check .`; Ruff ist im optionalen Extra `.[dev]` enthalten.
- Security mit `python -m bandit -r agent`; Abhängigkeiten mit `python -m pip_audit --skip-editable` prüfen (`.[security]`).
- Bandit-Ausnahmen nur einzeln mit Begründung in `docs/security-checks.md`; keine pauschalen Regel- oder Advisory-Ausnahmen.
- Ohne tatsächliche Ausführung keine erfolgreichen Tests behaupten.

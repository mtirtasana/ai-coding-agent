from pathlib import Path


class Workspace:
    def __init__(self, root: Path, allow_write: bool = False):
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Der Workspace muss ein Verzeichnis sein.")
        self.allow_write = allow_write

    def path(self, name: str) -> Path:
        candidate = (self.root / name).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("Pfad liegt außerhalb des Workspace.")
        parts = candidate.relative_to(self.root).parts
        original_parts = Path(name).parts
        if any(p in {".git", ".ssh", ".aws"} or p.startswith(".env")
               for p in (*parts, *original_parts)):
            raise ValueError("Geschützter Pfad.")
        return candidate

    def execute(self, name: str, args: dict) -> str:
        path = self.path(args.get("path", "."))
        if name == "list_files":
            entries = sorted(path.iterdir())
            return "\n".join(p.name + ("/" if p.is_dir() else "")
                             for p in entries[:500]
                             if p.name not in {".git", ".ssh", ".aws"}
                             and not p.name.startswith(".env"))
        if name == "read_file":
            with path.open("r", encoding="utf-8") as stream:
                content = stream.read(100_001)
            if len(content) > 100_000:
                raise ValueError("Datei zu groß (maximal 100.000 Zeichen).")
            return content
        if name == "write_file":
            if not self.allow_write:
                raise ValueError("Schreiben erfordert --allow-write.")
            content = args["content"]
            if not isinstance(content, str) or len(content) > 100_000:
                raise ValueError("Ungültiger oder zu großer Dateiinhalt.")
            # Reject unpaired Unicode surrogates before mkdir or opening/truncating a file.
            content.encode("utf-8")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return f"Geschrieben: {path.relative_to(self.root)}"
        raise ValueError(f"Unbekanntes Werkzeug: {name}")

from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectInstructions:
    path: str
    content: str


def load_project_instructions(workspace) -> ProjectInstructions:
    """Read only the workspace root, through the same path/size checks as tools."""
    path = workspace.path("AGENTS.md")
    original = workspace.root / "AGENTS.md"
    if not path.exists() and not original.is_symlink():
        return ProjectInstructions(str(original), "")
    return ProjectInstructions(str(original), workspace.execute("read_file", {"path": "AGENTS.md"}))

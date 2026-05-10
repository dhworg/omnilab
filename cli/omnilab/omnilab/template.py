"""Template management for `omnilab template list/show/install`.

Per project-spec-v1.md (rev 3) § "Templates" + "v1 must-do" #15.

v0 strategy:
  * `LocalRegistry` looks up templates from this repo's `templates/`
    tree (the in-repo source). Lets us ship the three foundational
    templates immediately and run the full `template install` flow
    in CI without an OCI registry round-trip.
  * `OCIRegistry` stub is sketched but its `fetch()` is a Phase
    B.future: it will pull `ghcr.io/dhworg/templates/<name>:<version>`
    via `podman pull` + extract. Same install pipeline; only the
    fetch step changes.

Template layout on disk:

    templates/<name>/
      template.yaml          # name, description, version, variables
      files/                 # everything under here is copied into
        omnilab.yaml         # the new project, with {{var}} substituted
        ...

Variable substitution is intentionally simple: `{{name}}` ↔ `name` from
the variables map. No control flow, no expressions; that's Jinja's job
and we don't need it. Catches `{{undefined}}` by raising during render.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import yaml


@dataclass
class TemplateInfo:
    """The metadata in `templates/<name>/template.yaml`."""

    name: str
    description: str
    version: str = "1"
    variables: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, text: str) -> TemplateInfo:
        data = yaml.safe_load(text) or {}
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            version=data.get("version", "1"),
            variables=list(data.get("variables", [])),
            files=list(data.get("files", [])),
        )


class TemplateRegistry(Protocol):
    """Where templates come from. v0 has LocalRegistry; OCIRegistry is
    a Phase B.future replacement with the same surface."""

    def list_names(self) -> list[str]: ...

    def fetch(self, name: str) -> Path:
        """Return a local path to the template's root directory.

        For LocalRegistry this is just the repo path; for OCIRegistry it
        will be a temp dir containing the unpacked layer.
        """
        ...


class LocalRegistry:
    """Reads templates straight from the repo's `templates/` tree."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def list_names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self.root.iterdir()
            if entry.is_dir() and (entry / "template.yaml").exists()
        )

    def fetch(self, name: str) -> Path:
        path = self.root / name
        if not (path / "template.yaml").exists():
            raise TemplateNotFound(name)
        return path


class TemplateNotFound(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"template {name!r} not found in registry")
        self.name = name


# ---- pure substitution + render -----------------------------------------


_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def render(text: str, variables: dict[str, str]) -> str:
    """Replace every `{{var}}` in `text` with `variables[var]`. Unknown
    variables raise KeyError. No fallback / no expression evaluation.
    """

    def _sub(m: re.Match[str]) -> str:
        var = m.group(1)
        if var not in variables:
            raise KeyError(var)
        return variables[var]

    return _VAR_RE.sub(_sub, text)


# ---- install pipeline ---------------------------------------------------


def install_template(
    *,
    info: TemplateInfo,
    template_root: Path,
    target: Path,
    variables: dict[str, str],
) -> list[Path]:
    """Render every file in `template_root/files/` into `target/` with
    variables substituted. Returns the list of written paths.

    Refuses to overwrite an existing file (caller can clear target first
    if they really mean it).
    """
    src_dir = template_root / "files"
    if not src_dir.is_dir():
        raise FileNotFoundError(f"template missing files/ dir at {src_dir}")

    target.mkdir(parents=True, exist_ok=True)
    declared = set(info.files)
    written: list[Path] = []

    for src in src_dir.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(src_dir).as_posix()

        # If template.yaml declared an explicit file list, treat it as
        # a strict allow-list. Empty list = take everything.
        if declared and rel not in declared:
            continue

        dst = target / rel
        if dst.exists():
            raise FileExistsError(f"{dst} already exists")
        dst.parent.mkdir(parents=True, exist_ok=True)

        # Render text-ish files; copy bytes for everything else (urdf
        # blobs, meshes, images, etc.).
        if _is_textual(src):
            dst.write_text(render(src.read_text(), variables))
        else:
            shutil.copyfile(src, dst)
        written.append(dst)
    return written


_TEXTUAL_SUFFIXES = {
    ".yaml", ".yml", ".toml", ".md", ".txt", ".py", ".sh", ".json",
    ".launch", ".xml", ".sdf", ".urdf", ".xacro", ".cfg", ".ini",
    ".cpp", ".c", ".h", ".hpp", ".rs", ".ts", ".js",
}


def _is_textual(path: Path) -> bool:
    return path.suffix.lower() in _TEXTUAL_SUFFIXES


# ---- repo-root resolver -------------------------------------------------


def find_repo_templates_dir(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default cwd) looking for a `templates/` dir
    next to a `CLAUDE.md` or `project-spec-v1.md` (markers of the repo
    root). Returns None if nothing matches — callers fall back to OCI."""
    cur = (start or Path.cwd()).resolve()
    for parent in [cur, *cur.parents]:
        candidate = parent / "templates"
        if candidate.is_dir() and any(
            (parent / m).exists() for m in ("CLAUDE.md", "project-spec-v1.md")
        ):
            return candidate
    return None

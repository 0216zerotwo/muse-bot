"""`SkillRegistry` scanning `<HOME>/skills/**/SKILL.md` with `.bundled_manifest` folding."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from muse.paths import get_muse_home
from muse.skills.scanner import scan_skill_content, scan_skill_directory

MAX_SKILL_CHARS = 100_000
MAX_SUPPORT_FILE_CHARS = 100_000
ALLOWED_SUBDIRS = frozenset({"references", "templates", "scripts", "assets"})
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_VALID_CAT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


def get_skills_dir(home: Optional[Path] = None) -> Path:
    return (Path(home) if home is not None else get_muse_home()) / "skills"


def current_platform_key() -> str:
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt":
        return "windows"
    return "linux"


def parse_skill_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter without external crashes; returns `(frontmatter_dict, body)`."""
    normalized = (text or "").replace("\r\n", "\n")
    m = _FRONTMATTER_RE.match(normalized)
    if not m:
        return {}, normalized
    raw_yaml, body = m.group(1), m.group(2)
    meta: dict[str, Any] = {}
    for line in raw_yaml.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" in line and not line.startswith((" ", "\t", "-")):
            k, v = line.split(":", 1)
            key = k.strip()
            val = v.strip()
            if val.startswith("[") and val.endswith("]"):
                items = [
                    x.strip().strip("\"'")
                    for x in val[1:-1].split(",")
                    if x.strip().strip("\"'")
                ]
                meta[key] = items
            else:
                meta[key] = val.strip("\"'")
    return meta, body


def validate_skill_name(name: str) -> Optional[str]:
    clean = (name or "").strip()
    if not clean:
        return "Skill name is required."
    if not _VALID_NAME_RE.match(clean) or ".." in clean:
        return (
            f"Invalid skill name '{clean}'. Must be lowercase letters, digits, "
            "hyphens, dots, or underscores (1-64 chars)."
        )
    return None


def validate_skill_markdown(content: str) -> Optional[str]:
    if not content or not content.strip():
        return "SKILL.md content cannot be empty."
    if len(content) > MAX_SKILL_CHARS:
        return f"SKILL.md exceeds {MAX_SKILL_CHARS:,} characters ({len(content):,} chars)."
    meta, body = parse_skill_frontmatter(content)
    if not meta:
        return "SKILL.md must start with YAML frontmatter delimited by '---' lines."
    if not str(meta.get("name") or "").strip():
        return "SKILL.md frontmatter must include 'name'."
    if not str(meta.get("description") or "").strip():
        return "SKILL.md frontmatter must include 'description'."
    if not body.strip():
        return "SKILL.md must contain step-by-step instructions after frontmatter."
    return None


def load_bundled_manifest(skills_root: Path) -> set[str]:
    manifest = skills_root / ".bundled_manifest"
    if not manifest.is_file():
        return set()
    try:
        lines = manifest.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return set()
    return {line.split(":", 1)[0].strip() for line in lines if line.strip()}


@dataclass(slots=True)
class SkillRecord:
    name: str
    description: str
    category: str
    skill_dir: Path
    skill_file: Path
    bundled: bool
    updated_at_ms: int
    platforms: tuple[str, ...] = ()

    def matches_platform(self) -> bool:
        if not self.platforms:
            return True
        cur = current_platform_key()
        lowered = {p.lower() for p in self.platforms}
        if cur in lowered:
            return True
        if cur == "macos" and "darwin" in lowered:
            return True
        if cur == "windows" and ("win32" in lowered or "nt" in lowered):
            return True
        return False

    def to_page_row(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description[:200],
            "category": self.category,
            "updated_at": self.updated_at_ms,
        }


class SkillRegistry:
    """Discovers, validates, reads, writes, and deletes `<HOME>/skills` entries."""

    def __init__(self, *, home: Optional[Path] = None, skills_dir: Optional[Path] = None) -> None:
        self._explicit_home = Path(home) if home is not None else None
        self._explicit_dir = Path(skills_dir) if skills_dir is not None else None

    @property
    def root(self) -> Path:
        if self._explicit_dir is not None:
            return self._explicit_dir
        return get_skills_dir(self._explicit_home)

    def bundled_names(self) -> set[str]:
        return load_bundled_manifest(self.root)

    def scan_all(self, *, filter_platform: bool = False) -> list[SkillRecord]:
        base = self.root
        if not base.is_dir():
            return []
        bundled = self.bundled_names()
        records: list[SkillRecord] = []

        for md_path in sorted(base.glob("**/SKILL.md")):
            rel_parts = md_path.relative_to(base).parts
            if any(part.startswith(".") for part in rel_parts):
                continue
            # Ignore SKILL.md accidentally placed inside asset subdirectories.
            if any(part in ALLOWED_SUBDIRS for part in rel_parts[:-1]):
                continue
            try:
                raw = md_path.read_text(encoding="utf-8-sig")
                mtime_ms = int(md_path.stat().st_mtime * 1000)
            except OSError:
                continue
            meta, _ = parse_skill_frontmatter(raw)
            skill_dir = md_path.parent
            name = str(meta.get("name") or skill_dir.name).strip()
            if not name:
                continue
            desc = " ".join(str(meta.get("description") or "").split())
            category = skill_dir.parent.name if skill_dir.parent != base else ""
            raw_plats = meta.get("platforms")
            plats = (
                tuple(str(x) for x in raw_plats)
                if isinstance(raw_plats, list)
                else ((str(raw_plats),) if raw_plats else ())
            )
            is_bundled = (name in bundled) or (skill_dir.name in bundled)
            rec = SkillRecord(
                name=name,
                description=desc,
                category=category,
                skill_dir=skill_dir,
                skill_file=md_path,
                bundled=is_bundled,
                updated_at_ms=mtime_ms,
                platforms=plats,
            )
            if filter_platform and not rec.matches_platform():
                continue
            records.append(rec)
        return records

    def list_for_page(self) -> dict[str, list[dict[str, Any]]]:
        """Return `{"learned": [...], "bundled": [...]}` matching `companion/modules/skills_page.py`."""
        learned: list[dict[str, Any]] = []
        bundled: list[dict[str, Any]] = []
        for rec in self.scan_all(filter_platform=False):
            (bundled if rec.bundled else learned).append(rec.to_page_row())
        learned.sort(key=lambda r: r["updated_at"], reverse=True)
        bundled.sort(key=lambda r: r["name"])
        return {"learned": learned, "bundled": bundled}

    def find_skill(self, name: str) -> Optional[SkillRecord]:
        target = (name or "").strip()
        if not target:
            return None
        target_low = target.lower()
        for rec in self.scan_all(filter_platform=False):
            if rec.name == target or rec.skill_dir.name == target:
                return rec
        for rec in self.scan_all(filter_platform=False):
            if rec.name.lower() == target_low or rec.skill_dir.name.lower() == target_low:
                return rec
        return None

    def read_skill(self, name: str, *, file_path: Optional[str] = None) -> dict[str, Any]:
        rec = self.find_skill(name)
        if rec is None:
            raise ValueError(f"Skill '{name}' not found")
        if file_path:
            sub = self._resolve_supporting_path(rec.skill_dir, file_path, require_allowed_subdir=False)
            if not sub.is_file():
                raise FileNotFoundError(f"Supporting file '{file_path}' not found in skill '{rec.name}'")
            return {
                "name": rec.name,
                "file_path": sub.relative_to(rec.skill_dir).as_posix(),
                "content": sub.read_text(encoding="utf-8-sig"),
                "bundled": rec.bundled,
            }

        linked: dict[str, list[str]] = {}
        for sub_name in sorted(ALLOWED_SUBDIRS):
            sdir = rec.skill_dir / sub_name
            if sdir.is_dir():
                rels = sorted(
                    p.relative_to(rec.skill_dir).as_posix()
                    for p in sdir.rglob("*")
                    if p.is_file()
                )
                if rels:
                    linked[sub_name] = rels
        return {
            "name": rec.name,
            "description": rec.description,
            "category": rec.category,
            "bundled": rec.bundled,
            "content": rec.skill_file.read_text(encoding="utf-8-sig"),
            "linked_files": linked,
        }

    @staticmethod
    def _resolve_supporting_path(
        skill_dir: Path,
        rel_path: str,
        *,
        require_allowed_subdir: bool = True,
    ) -> Path:
        clean = (rel_path or "").strip().replace("\\", "/").lstrip("/")
        if not clean or ".." in clean.split("/"):
            raise ValueError(f"Invalid relative file_path '{rel_path}'")
        parts = [p for p in clean.split("/") if p]
        if require_allowed_subdir and (len(parts) < 2 or parts[0] not in ALLOWED_SUBDIRS):
            allowed_str = ", ".join(sorted(ALLOWED_SUBDIRS))
            raise ValueError(f"Supporting files must live under one of: {allowed_str}")
        resolved = (skill_dir / Path(*parts)).resolve()
        root_resolved = skill_dir.resolve()
        if resolved == root_resolved or root_resolved not in resolved.parents:
            raise ValueError(f"Path traversal blocked for '{rel_path}'")
        return resolved

    def save_skill(
        self,
        name: str,
        content: str,
        *,
        category: Optional[str] = None,
        create: bool = False,
        trusted_user: bool = False,
    ) -> SkillRecord:
        """Create or update a skill with validation & security scan (explicit `trusted_user` API)."""
        name_err = validate_skill_name(name)
        if name_err:
            raise ValueError(name_err)
        md_err = validate_skill_markdown(content)
        if md_err:
            raise ValueError(md_err)

        if not trusted_user:
            report = scan_skill_content(content, filename="SKILL.md")
            if not report.ok:
                raise ValueError(report.summary_error())

        existing = self.find_skill(name)
        if create and existing is not None:
            raise ValueError(f"Skill '{name}' already exists; use edit or patch to update it.")
        if not create and existing is None:
            # Allow seamless save when creating via UI if not found yet only when create=True;
            # otherwise mirror Hermes `edit` semantics requiring existing skill.
            raise ValueError(f"Skill '{name}' not found.")

        if existing is not None:
            target_dir = existing.skill_dir
        else:
            base = self.root
            base.mkdir(parents=True, exist_ok=True)
            cat = (category or "").strip()
            if cat:
                if not _VALID_CAT_RE.match(cat) or ".." in cat:
                    raise ValueError(f"Invalid category name '{cat}'.")
                target_dir = base / cat / name.strip()
            else:
                target_dir = base / name.strip()

        target_dir.mkdir(parents=True, exist_ok=True)
        md_file = target_dir / "SKILL.md"
        normalized = content.replace("\r\n", "\n")
        if not normalized.endswith("\n"):
            normalized += "\n"
        _atomic_write_bytes(md_file, normalized.encode("utf-8"))

        if not trusted_user:
            dir_report = scan_skill_directory(target_dir)
            if not dir_report.ok:
                if existing is None:
                    shutil.rmtree(target_dir, ignore_errors=True)
                raise ValueError(dir_report.summary_error())

        saved = self.find_skill(name)
        if saved is None:
            raise RuntimeError(f"Failed to reload saved skill '{name}'")
        return saved

    def write_supporting_file(
        self,
        name: str,
        file_path: str,
        file_content: str,
        *,
        trusted_user: bool = False,
    ) -> Path:
        rec = self.find_skill(name)
        if rec is None:
            raise ValueError(f"Skill '{name}' not found")
        if len(file_content) > MAX_SUPPORT_FILE_CHARS:
            raise ValueError(f"Supporting file exceeds {MAX_SUPPORT_FILE_CHARS:,} characters.")
        dest = self._resolve_supporting_path(rec.skill_dir, file_path, require_allowed_subdir=True)
        if not trusted_user:
            report = scan_skill_content(file_content, filename=file_path)
            if not report.ok:
                raise ValueError(report.summary_error())
        _atomic_write_bytes(dest, file_content.replace("\r\n", "\n").encode("utf-8"))
        return dest

    def remove_supporting_file(self, name: str, file_path: str) -> None:
        rec = self.find_skill(name)
        if rec is None:
            raise ValueError(f"Skill '{name}' not found")
        dest = self._resolve_supporting_path(rec.skill_dir, file_path, require_allowed_subdir=True)
        if not dest.is_file():
            raise FileNotFoundError(f"Supporting file '{file_path}' not found")
        dest.unlink()

    def delete_skill(self, name: str, *, forbid_bundled: bool = True) -> None:
        rec = self.find_skill(name)
        if rec is None:
            raise ValueError(f"Skill '{name}' not found")
        if forbid_bundled and rec.bundled:
            raise ValueError("自带的技能不能删")
        parent = rec.skill_dir.parent
        shutil.rmtree(rec.skill_dir)
        if parent != self.root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()


__all__ = [
    "ALLOWED_SUBDIRS",
    "MAX_SKILL_CHARS",
    "SkillRecord",
    "SkillRegistry",
    "current_platform_key",
    "get_skills_dir",
    "load_bundled_manifest",
    "parse_skill_frontmatter",
    "validate_skill_markdown",
    "validate_skill_name",
]

"""Built-in LLM tools for progressive-disclosure skills: `skills_list`, `skill_view`, `skill_manage`."""

from __future__ import annotations

import json
from typing import Any, Optional

from muse.skills.registry import SkillRegistry

TOOLSET = "skills"


def skills_list_handler(
    args: dict[str, Any],
    *,
    skill_registry: Optional[SkillRegistry] = None,
    **_: Any,
) -> str:
    """List learned skills (and optionally bundled skills without flooding default output)."""
    reg = skill_registry or SkillRegistry()
    category_filter = str(args.get("category") or "").strip().lower()
    include_bundled = bool(args.get("include_bundled", False))

    records = reg.scan_all(filter_platform=True)
    learned_rows: list[dict[str, Any]] = []
    bundled_rows: list[dict[str, Any]] = []

    for rec in records:
        if category_filter and rec.category.lower() != category_filter:
            continue
        item = {
            "name": rec.name,
            "description": rec.description[:200],
            "category": rec.category,
            "source": "bundled" if rec.bundled else "learned",
        }
        if rec.bundled:
            bundled_rows.append(item)
        else:
            learned_rows.append(item)

    learned_rows.sort(key=lambda r: r["name"])
    bundled_rows.sort(key=lambda r: r["name"])
    skills_out = learned_rows + (bundled_rows if include_bundled else [])
    return json.dumps(
        {
            "success": True,
            "count": len(skills_out),
            "learned_count": len(learned_rows),
            "bundled_count": len(bundled_rows),
            "skills": skills_out,
        },
        ensure_ascii=False,
    )


def skill_view_handler(
    args: dict[str, Any],
    *,
    skill_registry: Optional[SkillRegistry] = None,
    **_: Any,
) -> str:
    """Load full `SKILL.md` instructions or a supporting reference/template/script file."""
    name = str(args.get("name") or "").strip()
    if not name:
        return json.dumps(
            {"success": False, "error": "Parameter 'name' is required."},
            ensure_ascii=False,
        )
    file_path = str(args["file_path"]).strip() if args.get("file_path") else None
    reg = skill_registry or SkillRegistry()
    try:
        data = reg.read_skill(name, file_path=file_path)
        return json.dumps({"success": True, **data}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


def skill_manage_handler(
    args: dict[str, Any],
    *,
    skill_registry: Optional[SkillRegistry] = None,
    **_: Any,
) -> str:
    """Create, edit, patch, delete, or manage supporting files in agent-authored skills."""
    action = str(args.get("action") or "").strip().lower()
    name = str(args.get("name") or "").strip()
    if not name:
        return json.dumps(
            {"success": False, "error": "Parameter 'name' is required."},
            ensure_ascii=False,
        )
    reg = skill_registry or SkillRegistry()

    try:
        if action == "create":
            content = str(args.get("content") or "")
            category = str(args["category"]).strip() if args.get("category") else None
            saved = reg.save_skill(name, content, category=category, create=True, trusted_user=False)
            return json.dumps(
                {
                    "success": True,
                    "action": "create",
                    "name": saved.name,
                    "path": str(saved.skill_dir),
                    "message": f"Skill '{saved.name}' created.",
                },
                ensure_ascii=False,
            )

        if action == "edit":
            content = str(args.get("content") or "")
            saved = reg.save_skill(name, content, create=False, trusted_user=False)
            return json.dumps(
                {
                    "success": True,
                    "action": "edit",
                    "name": saved.name,
                    "message": f"Skill '{saved.name}' updated.",
                },
                ensure_ascii=False,
            )

        if action == "patch":
            file_path = str(args["file_path"]).strip() if args.get("file_path") else None
            old_s = str(args.get("old_string") or "")
            if not old_s:
                return json.dumps(
                    {"success": False, "error": "'old_string' is required for action='patch'."},
                    ensure_ascii=False,
                )
            if "new_string" not in args:
                return json.dumps(
                    {"success": False, "error": "'new_string' is required for action='patch'."},
                    ensure_ascii=False,
                )
            new_s = str(args.get("new_string") or "")
            replace_all = bool(args.get("replace_all", False))
            loaded = reg.read_skill(name, file_path=file_path)
            cur_text = str(loaded.get("content") or "")
            count = cur_text.count(old_s)
            if count == 0:
                return json.dumps(
                    {"success": False, "error": f"'old_string' not found in {file_path or 'SKILL.md'}."},
                    ensure_ascii=False,
                )
            if count > 1 and not replace_all:
                return json.dumps(
                    {
                        "success": False,
                        "error": (
                            f"'old_string' matched {count} times; "
                            "provide more context or replace_all=True."
                        ),
                    },
                    ensure_ascii=False,
                )
            patched = cur_text.replace(old_s, new_s) if replace_all else cur_text.replace(old_s, new_s, 1)
            if file_path:
                reg.write_supporting_file(name, file_path, patched, trusted_user=False)
            else:
                reg.save_skill(name, patched, create=False, trusted_user=False)
            return json.dumps(
                {
                    "success": True,
                    "action": "patch",
                    "name": name,
                    "replacements": count if replace_all else 1,
                },
                ensure_ascii=False,
            )

        if action == "delete":
            reg.delete_skill(name, forbid_bundled=True)
            return json.dumps(
                {"success": True, "action": "delete", "name": name, "message": f"Skill '{name}' deleted."},
                ensure_ascii=False,
            )

        if action == "write_file":
            fpath = str(args.get("file_path") or "").strip()
            raw_fc = args.get("file_content") if "file_content" in args else args.get("content")
            fcontent = str(raw_fc or "")
            dest = reg.write_supporting_file(name, fpath, fcontent, trusted_user=False)
            return json.dumps(
                {"success": True, "action": "write_file", "name": name, "path": str(dest)},
                ensure_ascii=False,
            )

        if action == "remove_file":
            fpath = str(args.get("file_path") or "").strip()
            reg.remove_supporting_file(name, fpath)
            return json.dumps(
                {"success": True, "action": "remove_file", "name": name, "file_path": fpath},
                ensure_ascii=False,
            )

        return json.dumps(
            {
                "success": False,
                "error": (
                    f"Unknown action '{action}'. Use 'create', 'edit', 'patch', "
                    "'delete', 'write_file', or 'remove_file'."
                ),
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


SKILLS_LIST_SCHEMA: dict[str, Any] = {
    "name": "skills_list",
    "description": "List available procedural skills (`name` and one-line `description`).",
    "parameters": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "Optional category filter."},
            "include_bundled": {
                "type": "boolean",
                "description": "Include legacy bundled developer skills in addition to learned skills.",
                "default": False,
            },
        },
    },
}

SKILL_VIEW_SCHEMA: dict[str, Any] = {
    "name": "skill_view",
    "description": "Load full instructions of a skill (`SKILL.md`) or one of its supporting files.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill name from skills_list."},
            "file_path": {
                "type": "string",
                "description": "Optional relative supporting path, e.g. 'references/api.md'.",
            },
        },
        "required": ["name"],
    },
}

SKILL_MANAGE_SCHEMA: dict[str, Any] = {
    "name": "skill_manage",
    "description": (
        "Create, edit, patch, or delete reusable procedural skills (`SKILL.md` and "
        "supporting files under references/, templates/, scripts/, assets/)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "edit", "patch", "delete", "write_file", "remove_file"],
                "description": "Management action to perform.",
            },
            "name": {"type": "string", "description": "Lowercase hyphenated skill name."},
            "content": {"type": "string", "description": "Full SKILL.md text for create/edit."},
            "category": {"type": "string", "description": "Optional folder category on create."},
            "old_string": {"type": "string", "description": "Target text for action='patch'."},
            "new_string": {"type": "string", "description": "Replacement text for action='patch'."},
            "replace_all": {"type": "boolean", "default": False},
            "file_path": {
                "type": "string",
                "description": "Relative path under references/, templates/, scripts/, or assets/.",
            },
            "file_content": {"type": "string", "description": "Content for action='write_file'."},
        },
        "required": ["action", "name"],
    },
}


def register_skill_tools(registry: Any) -> None:
    registry.register("skills_list", toolset=TOOLSET, schema=SKILLS_LIST_SCHEMA, handler=skills_list_handler)
    registry.register("skill_view", toolset=TOOLSET, schema=SKILL_VIEW_SCHEMA, handler=skill_view_handler)
    registry.register(
        "skill_manage",
        toolset=TOOLSET,
        schema=SKILL_MANAGE_SCHEMA,
        handler=skill_manage_handler,
    )


__all__ = [
    "SKILLS_LIST_SCHEMA",
    "SKILL_MANAGE_SCHEMA",
    "SKILL_VIEW_SCHEMA",
    "TOOLSET",
    "register_skill_tools",
    "skill_manage_handler",
    "skill_view_handler",
    "skills_list_handler",
]

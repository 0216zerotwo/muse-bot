"""Security scanner for agent-authored skills (`SKILL.md` and supporting files)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_INVISIBLE_CHARS = frozenset({
    "\u200b", "\u200c", "\u200d", "\u2060", "\ufeff",
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
})

_SKILL_THREAT_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"ignore\s+(?:all\s+)?(?:previous|above|prior)\s+instructions", re.I),
        "high",
        "prompt_injection_override",
    ),
    (
        re.compile(r"disregard\s+(?:your|all|any)\s+(?:instructions|rules|guidelines)", re.I),
        "high",
        "disregard_safety_rules",
    ),
    (
        re.compile(r"do\s+not\s+(?:tell|inform|mention\s+to)\s+the\s+user", re.I),
        "high",
        "deceptive_concealment",
    ),
    (
        re.compile(
            r"(?:curl|wget|Invoke-WebRequest|iwr)\b[^\r\n]*\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|AUTH)",
            re.I,
        ),
        "critical",
        "secret_exfiltration_command",
    ),
    (
        re.compile(r"(?:/dev/tcp/|nc\s+-e\s+/bin/(?:ba)?sh|bash\s+-i\s+>&)", re.I),
        "critical",
        "reverse_shell_payload",
    ),
    (
        re.compile(r"(?:^|[\s;|&])rm\s+-rf\s+/(?:\s|$|\*)", re.I),
        "critical",
        "destructive_root_wipe",
    ),
    (
        re.compile(r"<!--[\s\S]*?(?:ignore|system|override|curl|wget)[\s\S]*?-->", re.I),
        "high",
        "hidden_html_comment_directive",
    ),
)


@dataclass(slots=True)
class SkillScanFinding:
    severity: str
    rule_id: str
    file: str
    detail: str


@dataclass(slots=True)
class SkillScanReport:
    verdict: str = "safe"
    findings: list[SkillScanFinding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verdict == "safe"

    def summary_error(self) -> str:
        if not self.findings:
            return ""
        first = self.findings[0]
        return f"Skill rejected by security scan ({first.rule_id} in {first.file}): {first.detail}"


def scan_skill_text(text: str, *, filename: str = "SKILL.md") -> list[SkillScanFinding]:
    findings: list[SkillScanFinding] = []
    for ch in text:
        if ch in _INVISIBLE_CHARS:
            findings.append(
                SkillScanFinding(
                    severity="high",
                    rule_id="invisible_unicode",
                    file=filename,
                    detail=f"Contains invisible/bidi Unicode character U+{ord(ch):04X}",
                )
            )
            break
    for pattern, severity, rule_id in _SKILL_THREAT_PATTERNS:
        m = pattern.search(text)
        if m:
            snippet = " ".join(m.group(0).split())[:80]
            findings.append(
                SkillScanFinding(
                    severity=severity,
                    rule_id=rule_id,
                    file=filename,
                    detail=f"Matched suspicious pattern: {snippet!r}",
                )
            )
    return findings


def scan_skill_content(content: str, *, filename: str = "SKILL.md") -> SkillScanReport:
    findings = scan_skill_text(content, filename=filename)
    return SkillScanReport(verdict="dangerous" if findings else "safe", findings=findings)


def scan_skill_directory(skill_dir: Path) -> SkillScanReport:
    all_findings: list[SkillScanFinding] = []
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.stat().st_size > 1_000_000:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            continue
        rel = path.relative_to(skill_dir).as_posix()
        text = raw.decode("utf-8", errors="replace")
        all_findings.extend(scan_skill_text(text, filename=rel))
    return SkillScanReport(
        verdict="dangerous" if all_findings else "safe",
        findings=all_findings,
    )


__all__ = [
    "SkillScanFinding",
    "SkillScanReport",
    "scan_skill_content",
    "scan_skill_directory",
    "scan_skill_text",
]

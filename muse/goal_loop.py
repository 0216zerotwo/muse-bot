"""Autonomous multi-turn ``/goal`` controller backed by ``muse.db`` (`session_goals`)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .config_store import load_config
from .db import MuseDB
from .interceptors import sanitize_subprocess_env
from .paths import get_muse_db_path
from .time_utils import now as muse_now

logger = logging.getLogger(__name__)

DEFAULT_MAX_TURNS = 20
DEFAULT_JUDGE_TIMEOUT = 30.0
DEFAULT_JUDGE_MAX_TOKENS = 4096
DEFAULT_GATE_TIMEOUT_SECONDS = 300
DEFAULT_GATE_MAX_RETRIES = 3
MAX_CONSECUTIVE_PARSE_FAILURES = 3
MAX_CONSECUTIVE_TRANSPORT_FAILURES = 5
_MAX_BARRIER_WAIT_S = 30 * 60.0
_GATE_OUTPUT_TAIL_CHARS = 3000
_JUDGE_SNIPPET_CHARS = 4000
_JSON_OBJ_RE = re.compile(r"\{.*?\}", re.DOTALL)

NOTICE_GOAL_DONE = "✓ 这件事办完了。"
NOTICE_GOAL_PAUSED = "⏸ 这件事先停下了：做了不少步还没办完，或者遇到了需要你看一下的情况。想继续的话跟我说一声。"
NOTICE_GOAL_PARKED = "⏳ 在等一件事完成，好了我接着做。"
GOAL_ALREADY_SEEN_KICK = "[Goal set] Continue with the goal you were just given; there is no need to re-read it."

_CONTRACT_FIELDS = ("outcome", "verification", "constraints", "boundaries", "stop_when")
_CONTRACT_LABELS = {
    "outcome": "Outcome", "verification": "Verification", "constraints": "Constraints",
    "boundaries": "Boundaries", "stop_when": "Stop when blocked",
}
_CONTRACT_ALIASES = {
    "outcome": "outcome", "goal": "outcome", "done": "outcome", "done when": "outcome",
    "verification": "verification", "verify": "verification", "verified by": "verification",
    "evidence": "verification", "proof": "verification", "constraints": "constraints",
    "constraint": "constraints", "preserve": "constraints", "must not": "constraints",
    "do not change": "constraints", "boundaries": "boundaries", "boundary": "boundaries",
    "scope": "boundaries", "allowed": "boundaries", "files": "boundaries",
    "stop when": "stop_when", "stop_when": "stop_when", "blocked": "stop_when", "stop if blocked": "stop_when",
}

JUDGE_SYSTEM_PROMPT = (
    "You are a strict judge evaluating whether an autonomous agent has achieved a user's stated goal.\n"
    "Decide one of four verdicts:\n"
    "- done: deliverable exists and goal (plus all extra criteria/contract verification) is satisfied.\n"
    "- blocked: genuinely unachievable or requires user input/decision to proceed.\n"
    "- wait: progress is gated on a running background process (`wait_on_session` or `wait_on_pid`) "
    "or explicit rate-limit/delegation timer (`wait_for_seconds`).\n"
    "- continue: concrete next step can be taken right now (default when in doubt).\n"
    "Reply ONLY with a single-line JSON object:\n"
    '{"verdict": "done"|"blocked"|"continue"|"wait", "reason": "<one sentence>", '
    '"wait_on_session": "<id>", "wait_on_pid": <int>, "wait_for_seconds": <int>}'
)


@dataclass
class GoalContract:
    outcome: str = ""
    verification: str = ""
    constraints: str = ""
    boundaries: str = ""
    stop_when: str = ""

    def is_empty(self) -> bool:
        return not any(getattr(self, f).strip() for f in _CONTRACT_FIELDS)

    def render_block(self) -> str:
        return "\n".join(f"- {_CONTRACT_LABELS[f]}: {getattr(self, f).strip()}" for f in _CONTRACT_FIELDS if getattr(self, f).strip())

    @classmethod
    def from_dict(cls, data: Any) -> GoalContract:
        return cls(**{f: str(data.get(f) or "").strip() for f in _CONTRACT_FIELDS}) if isinstance(data, dict) else cls()


@dataclass
class GoalGate:
    command: str
    timeout_seconds: int = DEFAULT_GATE_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_GATE_MAX_RETRIES
    attempts: int = 0
    last_exit_code: Optional[int] = None
    last_output_tail: str = ""

    @classmethod
    def from_dict(cls, data: Any) -> GoalGate:
        if not isinstance(data, dict):
            return cls(command="")
        return cls(
            command=str(data.get("command") or ""),
            timeout_seconds=int(data.get("timeout_seconds") or DEFAULT_GATE_TIMEOUT_SECONDS),
            max_retries=int(data.get("max_retries") or DEFAULT_GATE_MAX_RETRIES),
            attempts=int(data.get("attempts") or 0),
            last_exit_code=(int(data["last_exit_code"]) if data.get("last_exit_code") is not None else None),
            last_output_tail=str(data.get("last_output_tail") or ""),
        )


@dataclass
class GoalState:
    goal: str
    status: str = "active"
    turns_used: int = 0
    max_turns: int = DEFAULT_MAX_TURNS
    created_at: float = 0.0
    last_turn_at: float = 0.0
    last_verdict: Optional[str] = None
    last_reason: Optional[str] = None
    paused_reason: Optional[str] = None
    consecutive_parse_failures: int = 0
    consecutive_transport_failures: int = 0
    subgoals: list[str] = field(default_factory=list)
    waiting_on_pid: Optional[int] = None
    waiting_on_session: Optional[str] = None
    waiting_until: float = 0.0
    waiting_reason: Optional[str] = None
    waiting_since: float = 0.0
    contract: GoalContract = field(default_factory=GoalContract)
    gates: list[GoalGate] = field(default_factory=list)

    def clear_wait(self) -> None:
        self.waiting_on_pid = None
        self.waiting_on_session = None
        self.waiting_until = 0.0
        self.waiting_reason = None
        self.waiting_since = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> GoalState:
        if not isinstance(data, dict):
            return cls(goal="")
        raw_sub = data.get("subgoals") or []
        return cls(
            goal=str(data.get("goal") or ""),
            status=str(data.get("status") or "active"),
            turns_used=int(data.get("turns_used") or 0),
            max_turns=int(data.get("max_turns") or DEFAULT_MAX_TURNS),
            created_at=float(data.get("created_at") or 0.0),
            last_turn_at=float(data.get("last_turn_at") or 0.0),
            last_verdict=data.get("last_verdict"),
            last_reason=data.get("last_reason"),
            paused_reason=data.get("paused_reason"),
            consecutive_parse_failures=int(data.get("consecutive_parse_failures") or 0),
            consecutive_transport_failures=int(data.get("consecutive_transport_failures") or 0),
            subgoals=[str(s).strip() for s in raw_sub if str(s).strip()] if isinstance(raw_sub, list) else [],
            waiting_on_pid=(int(data["waiting_on_pid"]) if data.get("waiting_on_pid") else None),
            waiting_on_session=(str(data["waiting_on_session"]) if data.get("waiting_on_session") else None),
            waiting_until=float(data.get("waiting_until") or 0.0),
            waiting_reason=data.get("waiting_reason"),
            waiting_since=float(data.get("waiting_since") or 0.0),
            contract=GoalContract.from_dict(data.get("contract")),
            gates=[GoalGate.from_dict(g) for g in (data.get("gates") or []) if isinstance(g, dict) and str(g.get("command") or "").strip()],
        )


@dataclass
class GoalDecision:
    status: str
    should_continue: bool
    continuation_prompt: Optional[str]
    verdict: str
    reason: str
    notice: Optional[str] = None
    wait_directive: Optional[dict[str, Any]] = None


def parse_contract(text: str) -> tuple[str, GoalContract]:
    if not text:
        return "", GoalContract()
    headlines: list[str] = []
    fields: dict[str, list[str]] = {f: [] for f in _CONTRACT_FIELDS}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ":" in line:
            prefix, _, val = line.partition(":")
            key = _CONTRACT_ALIASES.get(prefix.strip().lower())
            if key is not None and val.strip():
                fields[key].append(val.strip())
                continue
        headlines.append(line)
    return " ".join(headlines).strip(), GoalContract(**{f: " ".join(v).strip() for f, v in fields.items()})


def goal_kick_prompt(goal: str, last_user_message: str = "") -> str:
    gnorm, lnorm = " ".join((goal or "").split()), " ".join((last_user_message or "").split())
    if len(gnorm) >= 400 and gnorm in lnorm and len(gnorm) >= 0.8 * len(lnorm):
        return GOAL_ALREADY_SEEN_KICK
    return goal


def pid_alive(pid: int) -> bool:
    """Cross-platform PID liveness check via `psutil` (never calls `os.kill(pid, 0)` on Windows)."""
    if not pid or pid <= 0:
        return False
    with contextlib.suppress(Exception):
        import psutil  # type: ignore[import-untyped]

        return bool(psutil.pid_exists(int(pid)))
    return False


async def run_gate_async(gate: GoalGate, *, cwd: Optional[str] = None) -> tuple[bool, int, str]:
    """Run a quality gate command asynchronously with sanitized env and UTF-8 decoding."""
    proc: Optional[asyncio.subprocess.Process] = None
    try:
        proc = await asyncio.create_subprocess_shell(
            gate.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or None,
            env=sanitize_subprocess_env(),
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=max(1, int(gate.timeout_seconds)))
        out_s = (out_b or b"").decode("utf-8", "replace")
        err_s = (err_b or b"").decode("utf-8", "replace")
        combined = out_s + (f"\n{err_s}" if err_s else "")
        code = int(proc.returncode or 0)
        return code == 0, code, combined[-_GATE_OUTPUT_TAIL_CHARS:]
    except asyncio.TimeoutError:
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.kill()
                await proc.wait()
        return False, -1, f"[gate timed out after {gate.timeout_seconds}s]"
    except Exception as exc:
        return False, -1, f"[gate could not run: {type(exc).__name__}: {exc}]"


def parse_judge_response(raw: str) -> tuple[str, str, bool, Optional[dict[str, Any]]]:
    if not raw or not raw.strip():
        return "continue", "judge returned empty response", True, None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")
        text = text[nl + 1 :] if nl != -1 else text
    data: Any = None
    with contextlib.suppress(Exception):
        data = json.loads(text)
    if not isinstance(data, dict):
        match = _JSON_OBJ_RE.search(text)
        with contextlib.suppress(Exception):
            data = json.loads(match.group(0)) if match else None
    if not isinstance(data, dict):
        return "continue", "judge reply was not JSON", True, None

    reason = str(data.get("reason") or "").strip() or "no reason provided"
    verdict_raw = data.get("verdict")
    if isinstance(verdict_raw, str):
        verdict = verdict_raw.strip().lower()
    else:
        done_val = data.get("done")
        done = done_val.strip().lower() in {"true", "yes", "1", "done"} if isinstance(done_val, str) else bool(done_val)
        verdict = "done" if done else "continue"
    if verdict not in {"done", "blocked", "continue", "wait"}:
        verdict = "continue"
    if verdict != "wait":
        return verdict, reason, False, None

    sess = data.get("wait_on_session") or data.get("session_id") or data.get("wait_session")
    if isinstance(sess, str) and sess.strip():
        return "wait", reason, False, {"session_id": sess.strip()}
    for keys, field_name in ((("wait_on_pid", "pid", "wait_pid"), "pid"), (("wait_for_seconds", "seconds", "wait_seconds"), "seconds")):
        for key in keys:
            with contextlib.suppress(TypeError, ValueError):
                iv = int(data[key]) if data.get(key) is not None else 0
                if iv > 0:
                    return "wait", reason, False, {field_name: iv}
    return "continue", f"{reason} (wait verdict had no target)", False, None


JudgeLLMFn = Callable[[str, str, float, int], Awaitable[str]]


class GoalLoopController:
    """Manages `/goal` lifecycle, gate checks, judge verdicts, async wait barriers, and notices."""

    def __init__(
        self,
        *,
        db: Optional[MuseDB] = None,
        db_path: Optional[Path] = None,
        adapter: Any = None,
        judge_llm_fn: Optional[JudgeLLMFn] = None,
        post_notice_fn: Optional[Callable[[str, str], Awaitable[None]]] = None,
        push_goal_state_fn: Optional[Callable[[str], Awaitable[None]]] = None,
        session_waiting_fn: Optional[Callable[[str], bool]] = None,
        pid_alive_fn: Callable[[int], bool] = pid_alive,
        list_bg_processes_fn: Optional[Callable[[str], list[dict[str, Any]]]] = None,
        gate_cwd: Optional[str] = None,
        default_max_turns: int = DEFAULT_MAX_TURNS,
        poll_interval_s: float = 0.5,
    ) -> None:
        self._external_db = db
        self._db_path = db_path
        self._owned_db: Optional[MuseDB] = None
        self._judge_llm_fn = judge_llm_fn
        self._post_notice_fn = post_notice_fn if post_notice_fn is not None else getattr(adapter, "post_notice", None)
        self._push_goal_state_fn = push_goal_state_fn if push_goal_state_fn is not None else getattr(adapter, "push_goal_state", None)
        self._session_waiting_fn = session_waiting_fn or (lambda _sid: False)
        self._pid_alive_fn = pid_alive_fn
        self._list_bg_processes_fn = list_bg_processes_fn or (lambda _cid: [])
        self._gate_cwd = gate_cwd
        self.default_max_turns = max(1, int(default_max_turns))
        self.poll_interval_s = max(0.02, float(poll_interval_s))
        self._wake_events: dict[str, asyncio.Event] = {}

    def _db(self) -> MuseDB:
        if self._external_db is not None:
            return self._external_db
        target = self._db_path or get_muse_db_path()
        if self._owned_db is None or self._owned_db.db_path != target:
            if self._owned_db is not None:
                self._owned_db.close()
            self._owned_db = MuseDB(target)
        return self._owned_db

    def close(self) -> None:
        if self._owned_db is not None:
            self._owned_db.close()
            self._owned_db = None

    def load_state(self, chat_id: str) -> Optional[GoalState]:
        row = self._db().get_goal(chat_id)
        if row is None:
            return None
        raw_state = row.get("state") if isinstance(row.get("state"), dict) else {}
        merged = {
            **raw_state,
            "goal": row.get("objective") or raw_state.get("goal") or "",
            "status": row.get("status") or raw_state.get("status") or "active",
            "turns_used": int(row.get("iteration") if row.get("iteration") is not None else raw_state.get("turns_used") or 0),
            "max_turns": int(row.get("max_iterations") if row.get("max_iterations") is not None else raw_state.get("max_turns") or self.default_max_turns),
        }
        state = GoalState.from_dict(merged)
        if row.get("gate") and not state.gates:
            state.gates = [GoalGate(command=str(row["gate"]))]
        return state

    def save_state(self, chat_id: str, state: GoalState, *, session_key: Optional[str] = None) -> GoalState:
        primary_gate = state.gates[0].command if state.gates else None
        self._db().upsert_goal(
            chat_id,
            status=state.status,
            objective=state.goal,
            session_key=session_key or f"companion:{chat_id}",
            gate=primary_gate,
            iteration=state.turns_used,
            max_iterations=state.max_turns,
            state=state.to_dict(),
        )
        return state

    def banner_state(self, chat_id: str) -> dict[str, Any]:
        return self._db().get_goal_banner_state(chat_id)

    async def _sync_ui(self, chat_id: str, notice: Optional[str] = None) -> None:
        if self._push_goal_state_fn is not None:
            with contextlib.suppress(Exception):
                await self._push_goal_state_fn(chat_id)
        if notice and self._post_notice_fn is not None:
            with contextlib.suppress(Exception):
                await self._post_notice_fn(chat_id, notice)

    def _wake(self, chat_id: str) -> None:
        if (ev := self._wake_events.get(chat_id)) is not None:
            ev.set()

    # ── User & UI Controls (`/goal ...`) ──────────────────────────────────────

    async def set_goal(
        self, chat_id: str, raw_text: str, *, max_turns: Optional[int] = None, gate: Optional[str] = None
    ) -> GoalState:
        headline, contract = parse_contract(raw_text)
        objective = headline or raw_text.strip()
        if not objective:
            raise ValueError("目标内容不能为空")
        gates = [GoalGate(command=gate.strip())] if gate and gate.strip() else []
        state = GoalState(
            goal=objective,
            status="active",
            turns_used=0,
            max_turns=int(max_turns) if max_turns else self.default_max_turns,
            created_at=time.time(),
            contract=contract,
            gates=gates,
        )
        self.save_state(chat_id, state)
        self._wake(chat_id)
        await self._sync_ui(chat_id)
        return state

    async def handle_control(self, chat_id: str, command: str) -> Optional[GoalState]:
        """Handle `/goal stop|clear|pause|resume|gate ...` commands and sync the app banner."""
        cmd = (command or "").strip()
        low = cmd.lower()
        if low in ("/goal stop", "/goal clear"):
            self._db().clear_goal(chat_id)
            self._wake(chat_id)
            await self._sync_ui(chat_id)
            return None
        state = self.load_state(chat_id)
        if low == "/goal pause":
            if state is None or state.status not in ("active", "paused"):
                return None
            state.status = "paused"
            state.paused_reason = "已暂停"
            state.clear_wait()
            self.save_state(chat_id, state)
            self._wake(chat_id)
            await self._sync_ui(chat_id)
            return state
        if low == "/goal resume":
            if state is None or state.status == "cleared":
                return None
            state.status = "active"
            state.paused_reason = None
            state.last_verdict = None
            state.turns_used = 0
            state.clear_wait()
            self.save_state(chat_id, state)
            self._wake(chat_id)
            await self._sync_ui(chat_id)
            return state
        if low.startswith("/goal gate add "):
            if state is None or state.status not in ("active", "paused"):
                raise RuntimeError("当前没有进行中的目标")
            gate_cmd = cmd[len("/goal gate add ") :].strip()
            if gate_cmd:
                state.gates.append(GoalGate(command=gate_cmd))
                self.save_state(chat_id, state)
            return state
        if low == "/goal gate clear" and state is not None:
            state.gates.clear()
            self.save_state(chat_id, state)
            return state
        if low.startswith("/subgoal ") and state is not None:
            sub = cmd[len("/subgoal ") :].strip()
            if sub:
                state.subgoals.append(sub)
                self.save_state(chat_id, state)
            return state
        if low.startswith("/goal "):
            body = cmd[len("/goal ") :].strip()
            if body:
                return await self.set_goal(chat_id, body)
        return state

    handle_command = handle_control

    # ── Judge & Multi-Turn Loop ───────────────────────────────────────────────

    def _build_continuation_prompt(self, state: GoalState) -> str:
        parts = [f"[Continuing toward your standing goal]\nGoal: {state.goal}"]
        if not state.contract.is_empty():
            parts.append(f"Completion contract:\n{state.contract.render_block()}")
        if state.subgoals:
            sub_block = "\n".join(f"- {i}. {s}" for i, s in enumerate(state.subgoals, start=1))
            parts.append(f"Additional criteria:\n{sub_block}")
        parts.append(
            "Continue working toward this goal. Take the next concrete step. "
            "If the goal is complete with concrete evidence, state so clearly and stop. "
            "If you are blocked and need user input, say so clearly and stop."
        )
        return "\n\n".join(parts)

    async def _call_judge(self, state: GoalState, last_response: str, chat_id: str) -> tuple[str, str, bool, Optional[dict[str, Any]], bool]:
        cfg = (load_config().get("auxiliary") or {}).get("goal_judge") or {}
        timeout = float(cfg.get("timeout") or DEFAULT_JUDGE_TIMEOUT)
        max_tokens = int(cfg.get("max_tokens") or DEFAULT_JUDGE_MAX_TOKENS)
        bg_rows = [
            f"- pid {p['pid']}" + (f" / session {p['session_id']}" if p.get("session_id") else "") + f": {str(p.get('command') or '')[:120]}"
            for p in self._list_bg_processes_fn(chat_id)
            if isinstance(p, dict) and p.get("status") != "exited" and p.get("pid")
        ]
        bg_block = ("Background processes currently running:\n" + "\n".join(bg_rows) + "\n\n") if bg_rows else ""
        extra = ""
        if not state.contract.is_empty():
            extra += f"\nCompletion contract:\n{state.contract.render_block()}\n"
        if state.subgoals:
            extra += "\nAdditional criteria (all must be met):\n" + "\n".join(f"- {i}. {s}" for i, s in enumerate(state.subgoals, start=1)) + "\n"
        user_prompt = (
            f"Goal:\n{state.goal[:2000]}\n{extra}\n"
            f"Agent's most recent response:\n{(last_response or '')[:_JUDGE_SNIPPET_CHARS]}\n\n"
            f"{bg_block}Current time: {muse_now().strftime('%Y-%m-%d %H:%M:%S %Z')}\n\n"
            "Is the goal satisfied — done, blocked, continue, or wait?"
        )
        try:
            if self._judge_llm_fn is not None:
                raw = await self._judge_llm_fn(JUDGE_SYSTEM_PROMPT, user_prompt, timeout, max_tokens)
            else:
                from muse.llm.auxiliary import async_call_llm  # type: ignore[import-not-found]

                resp = await async_call_llm(
                    task="goal_judge",
                    messages=[{"role": "system", "content": JUDGE_SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}],
                    temperature=0,
                    max_tokens=max_tokens,
                    timeout=timeout,
                    session_id=f"goal_judge:{chat_id}",
                )
                raw = (resp.choices[0].message.content or "") if resp.choices else ""
        except Exception as exc:
            logger.info("muse.goal_loop: judge call failed (%s)", type(exc).__name__)
            return "continue", f"judge error: {type(exc).__name__}", False, None, True

        verdict, reason, parse_failed, wait_dir = parse_judge_response(raw)
        logger.info("muse.goal_loop: chat=%s verdict=%s reason_chars=%d", chat_id, verdict, len(reason))
        return verdict, reason, parse_failed, wait_dir, False

    async def evaluate_after_turn(self, chat_id: str, last_response: str) -> GoalDecision:
        state = self.load_state(chat_id)
        if state is None or state.status != "active":
            return GoalDecision("inactive", False, None, "skipped", "no active goal")

        state.turns_used += 1
        state.last_turn_at = time.time()

        # 1. Deterministic quality gates run BEFORE LLM judge
        for gate in state.gates:
            passed, exit_code, tail = await run_gate_async(gate, cwd=self._gate_cwd)
            gate.last_exit_code = exit_code
            gate.last_output_tail = tail
            if passed:
                gate.attempts = 0
                continue
            gate.attempts += 1
            if gate.attempts > gate.max_retries or state.turns_used >= state.max_turns:
                state.status = "paused"
                state.last_verdict = "gate_failed"
                state.paused_reason = f"门禁检查连续未通过（退出码 {exit_code}）"
                self.save_state(chat_id, state)
                await self._sync_ui(chat_id, NOTICE_GOAL_PAUSED)
                return GoalDecision("paused", False, None, "gate_failed", state.paused_reason, notice=NOTICE_GOAL_PAUSED)
            self.save_state(chat_id, state)
            prompt = (
                f"[Continuing toward your standing goal — a quality gate failed]\nGoal: {state.goal}\n\n"
                f"Quality gate failed (attempt {gate.attempts}/{gate.max_retries}, exit {exit_code}):\n"
                f"  $ {gate.command}\n```\n{tail or '(no output)'}\n```\n\n"
                "Fix the underlying issue so this gate passes before declaring the goal complete."
            )
            return GoalDecision("active", True, prompt, "gate_failed", f"exit {exit_code}")

        # 2. Fast-model judge verdict (`done | blocked | wait | continue`)
        if not (last_response or "").strip():
            verdict, reason, parse_failed, wait_dir, transport_failed = "continue", "empty response", False, None, False
        else:
            verdict, reason, parse_failed, wait_dir, transport_failed = await self._call_judge(state, last_response, chat_id)

        state.last_verdict = verdict
        state.last_reason = reason
        state.consecutive_parse_failures = (state.consecutive_parse_failures + 1) if parse_failed else 0
        state.consecutive_transport_failures = (state.consecutive_transport_failures + 1) if transport_failed else 0

        if verdict == "done":
            state.status = "done"
            state.clear_wait()
            self.save_state(chat_id, state)
            await self._sync_ui(chat_id, NOTICE_GOAL_DONE)
            return GoalDecision("done", False, None, "done", reason, notice=NOTICE_GOAL_DONE)

        if verdict == "blocked":
            state.status = "paused"
            state.paused_reason = reason
            state.clear_wait()
            self.save_state(chat_id, state)
            await self._sync_ui(chat_id, NOTICE_GOAL_PAUSED)
            return GoalDecision("paused", False, None, "blocked", reason, notice=NOTICE_GOAL_PAUSED)

        if (
            state.turns_used >= state.max_turns
            or state.consecutive_parse_failures >= MAX_CONSECUTIVE_PARSE_FAILURES
            or state.consecutive_transport_failures >= MAX_CONSECUTIVE_TRANSPORT_FAILURES
        ):
            state.status = "paused"
            state.paused_reason = reason if state.turns_used < state.max_turns else f"已推进 {state.turns_used} 步，暂停等你确认"
            state.clear_wait()
            self.save_state(chat_id, state)
            await self._sync_ui(chat_id, NOTICE_GOAL_PAUSED)
            return GoalDecision("paused", False, None, verdict, state.paused_reason, notice=NOTICE_GOAL_PAUSED)

        if verdict == "wait" and wait_dir:
            state.clear_wait()
            state.waiting_since = time.time()
            state.waiting_reason = reason
            if "session_id" in wait_dir:
                state.waiting_on_session = str(wait_dir["session_id"])
            elif "pid" in wait_dir:
                state.waiting_on_pid = int(wait_dir["pid"])
            elif "seconds" in wait_dir:
                state.waiting_until = time.time() + min(float(wait_dir["seconds"]), _MAX_BARRIER_WAIT_S)
            self.save_state(chat_id, state)
            await self._sync_ui(chat_id, NOTICE_GOAL_PARKED)
            return GoalDecision(
                "waiting", True, self._build_continuation_prompt(state), "wait", reason,
                notice=NOTICE_GOAL_PARKED, wait_directive=wait_dir,
            )

        state.clear_wait()
        self.save_state(chat_id, state)
        await self._sync_ui(chat_id)
        return GoalDecision("active", True, self._build_continuation_prompt(state), "continue", reason)

    async def wait_for_barrier(self, chat_id: str, directive: dict[str, Any]) -> bool:
        """Async wait on background process PID/session or timer; returns `True` if goal remains active."""
        ev = self._wake_events.setdefault(chat_id, asyncio.Event())
        ev.clear()
        loop = asyncio.get_running_loop()
        max_wait = min(float(directive["seconds"]), _MAX_BARRIER_WAIT_S) if "seconds" in directive else _MAX_BARRIER_WAIT_S
        deadline = loop.time() + max(0.05, max_wait)
        while loop.time() < deadline:
            state = self.load_state(chat_id)
            if state is None or state.status != "active":
                return False
            if "session_id" in directive and not self._session_waiting_fn(str(directive["session_id"])):
                break
            if "pid" in directive and not self._pid_alive_fn(int(directive["pid"])):
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ev.wait(), timeout=min(self.poll_interval_s, remaining))
            if ev.is_set():
                state = self.load_state(chat_id)
                return bool(state and state.status == "active")
        state = self.load_state(chat_id)
        if state is None or state.status != "active":
            return False
        state.clear_wait()
        self.save_state(chat_id, state)
        return True

    async def run_goal_loop(
        self, chat_id: str, initial_response: str, run_turn_fn: Callable[[str, str], Awaitable[str]]
    ) -> GoalDecision:
        """Drive autonomous multi-turn continuation until `done`, `blocked`, `paused`, or stopped."""
        response = initial_response
        while True:
            decision = await self.evaluate_after_turn(chat_id, response)
            if not decision.should_continue or not decision.continuation_prompt:
                return decision
            if decision.verdict == "wait" and decision.wait_directive:
                still_active = await self.wait_for_barrier(chat_id, decision.wait_directive)
                if not still_active:
                    return GoalDecision("inactive", False, None, "stopped", "goal stopped while waiting")
            state = self.load_state(chat_id)
            if state is None or state.status != "active":
                return GoalDecision("inactive", False, None, "stopped", "goal no longer active")
            response = await run_turn_fn(chat_id, decision.continuation_prompt)

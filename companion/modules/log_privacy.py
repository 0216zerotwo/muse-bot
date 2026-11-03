"""Keeps what the user writes out of the server's logs.

Hermes logs the start of every message it receives ("inbound message: … msg='…'"), a preview of a
tool's failed output and the command a failed terminal run had — on a rented server, log files are
one more copy of the user's words. These filters, on Hermes's own loggers, leave the log line and put
the size in place of the words ("<12 字>"). Nothing in Hermes is changed: a filter only rewrites the
record before any handler (console, file) writes it.
"""

from __future__ import annotations

import logging
from typing import Any

# (logger, how the line starts, which args are the user's words)
_LINES = (
    ("gateway.run", "inbound message:", (3, 5)),
    ("agent.tool_executor", "Tool %s returned error", (2,)),
    ("tools.terminal_tool", "Execution error, retrying", (3,)),
    ("tools.terminal_tool", "Execution failed after", (1,)),
)


def _size(value: Any) -> str:
    text = "" if value is None else str(value)
    return f"<{len(text)} 字>" if text else ""


class _WordsOut(logging.Filter):
    def __init__(self, rules: list[tuple[str, tuple[int, ...]]]):
        super().__init__()
        self.rules = rules

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and isinstance(record.args, tuple):
            for start, positions in self.rules:
                if record.msg.startswith(start):
                    args = list(record.args)
                    for at in positions:
                        if at < len(args):
                            args[at] = _size(args[at])
                    record.args = tuple(args)
                    break
        return True  # the line itself is kept


def install() -> None:
    """Once per process (a second call leaves the filters as they are)."""
    by_logger: dict[str, list[tuple[str, tuple[int, ...]]]] = {}
    for name, start, positions in _LINES:
        by_logger.setdefault(name, []).append((start, positions))
    for name, rules in by_logger.items():
        logger = logging.getLogger(name)
        # By name: the plugin can be loaded again in one process, and one filter is enough.
        if not any(type(f).__name__ == "_WordsOut" for f in logger.filters):
            logger.addFilter(_WordsOut(rules))

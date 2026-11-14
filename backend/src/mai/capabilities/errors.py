"""The pre-effect refusal marker for capability executors.

Deliberately dependency-free (no dispatch/capability imports — the
capability modules already import this, so any back edge would be a
cycle): one exception marker the dispatcher's executor seam keys on.
"""

from __future__ import annotations

__all__ = ["PreEffectError"]


class PreEffectError(ValueError):
    """Structured refusal proven to occur BEFORE any host effect/transport.

    The dispatcher treats this marker as a deterministic denial
    (``effect="not_attempted"``, stable ``error_code`` token on the
    receipt) — never a reconciliation park.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code  # stable machine token (StrEnum .value or str)
        self.detail = detail  # operator-facing; NEVER persisted verbatim

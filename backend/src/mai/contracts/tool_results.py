"""Neutral execution/tool-result projection vocabulary (frozen).

Campaign ``machina-session-first-chat`` (D01 extraction): the tool-result
bounds and the execution/domain/effect-certainty enums previously frozen
inside the task contract move here so the retained
:class:`~mai.agents.tools.CapabilityToolBridge` and the new session-first
domain vocabulary depend on a server-agnostic module instead of the deleted
task contracts. ``contracts/tasks.py`` keeps re-exporting these names until
D09 deletes that module; the re-exports are the same objects, so every
existing caller keeps working unchanged.

The vocabulary separates the three layers a tool answer must not conflate
(card ``19-tool-results``; act_CONTRACTS §9):

- **Wrapper execution** (:class:`ExecutionStatus`) — did the guarded wrapper
  run to a terminal observation, and is that observation trustworthy?
  ``outcome_unknown`` is honest after a crash/timeout; a fabricated
  ``succeeded`` is the cardinal defect the runtime never commits.
- **Domain outcome** (:class:`DomainOutcome`) — did the *task* the caller
  asked about succeed? ``unknown`` is honest for arbitrary shell work where
  no deterministic oracle exists.
- **Effect certainty** (:class:`EffectCertainty`) — do we know what the call
  actually changed? ``uncertain``/``unknown`` park attempts for
  reconciliation; they are never silently replayed as successful.

Also carried: the evidence-reference kinds a bounded projection may cite
(:class:`EvidenceKind`), the ``result_data`` map bounds (act_CONTRACTS §9
"safe bounded result data") and the credential-shaped key names that may
never appear as ``result_data`` keys (defense-in-depth exact key-name
matching, the same discipline as the jobs envelope guard).

The module is pure: no process environment, filesystem, network or clock
access — validation is SSR/worker/replay safe and never leaks ambient secret
material.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

__all__ = [
    "MAX_EVIDENCE_REFS",
    "RESULT_DATA_MAX_ENTRIES",
    "RESULT_DATA_VALUE_MAX_CHARS",
    "SECRET_DATA_KEYS",
    "DomainOutcome",
    "EffectCertainty",
    "EvidenceKind",
    "ExecutionStatus",
]

# --- frozen bounds ------------------------------------------------------------

#: Maximum evidence references carried by one step / verdict / turn section,
#: and the subcheck/entry bound the tool-result projection reuses (the
#: projection caps its bounded subchecks at the evidence-ref bound).
MAX_EVIDENCE_REFS: Final[int] = 16

#: Maximum entries in a verdict's bounded ``result_data`` map (act_CONTRACTS
#: §9 "safe bounded result data").
RESULT_DATA_MAX_ENTRIES: Final[int] = 8

#: Value bound per ``result_data`` entry (act_CONTRACTS §9).
RESULT_DATA_VALUE_MAX_CHARS: Final[int] = 200

#: Credential-shaped key names that may never appear as ``result_data`` keys.
#: Exact key-name matching (case-insensitive), the same discipline as the
#: jobs envelope guard — defense-in-depth only, never proof.
SECRET_DATA_KEYS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "private_key",
        "passphrase",
        "api_key",
        "access_key",
        "credential",
    }
)

# --- frozen vocabularies --------------------------------------------------------


class EvidenceKind(StrEnum):
    """What kind of sourced record an evidence reference points at (§5 facts;
    §9 evidence validator checks provenance, task/target and observation type)."""

    MESSAGE = "message"
    OBSERVATION = "observation"
    RUN_EVENT = "run_event"
    ARTIFACT = "artifact"
    DOCUMENT = "document"


class ExecutionStatus(StrEnum):
    """Compact technical execution status of the closing attempt (§9 receipt;
    values subset the frozen C04 ``CommandOutcome`` vocabulary)."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    CANCELLED = "cancelled"


class DomainOutcome(StrEnum):
    """Domain-level outcome; ``unknown`` is honest for arbitrary shell work
    (§9: no regex oracle claims semantic success)."""

    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"


class EffectCertainty(StrEnum):
    """How certain the effect evidence is (§9 ``effect_certainty``)."""

    CERTAIN = "certain"
    UNCERTAIN = "uncertain"
    UNKNOWN = "unknown"

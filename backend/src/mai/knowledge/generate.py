"""Evidence-backed incident and maintenance document generation.

Card ``07-doc-generation`` (service ``knowledge``) against contracts.md
**C07 rule 5** (generated docs separate outcomes from hypotheses; only a
successful verified operation may update an authoritative success
instruction; a failed job creates an incident record, never a fabricated
runbook success), **C07 rule 1** (the generated doc is a source record with
provenance), **C07 rule 2** (the durable reindex intent makes the update
visible to the indexer) and **C02 rule 6** (generated operational docs use
a separate provenance space from admin-controlled server prompt additions —
here: ``source_kind=generated`` in the canonical docs/runbooks layout).

Inputs are the reviewed producers integrated before this card:

- ``05-job-schema`` / ``06-agent-run`` — the frozen run vocabulary
  (:class:`~mai.contracts.jobs.RunState`, :class:`~mai.contracts.jobs.StepState`).
  A run's terminal engine truth plus its settled step rows are the
  *evidence*; this module consumes a validated, value-free evidence payload
  (``parse_job_evidence``), never run/database state directly — the caller
  (a deterministic collector step or an operator) assembles the payload
  from the persisted run record, so this module stays engine-agnostic and
  replay-safe.
- ``04-shell-capability`` / ``04-log-capability`` — their ExecutorResult
  documents carry the bounded, redacted outcome vocabulary and value-free
  evidence references (target receipt directories, controller intent
  paths, evidence artifacts). Those references are exactly what the
  generated doc cites; this module validates them structurally (below).
- ``07-doc-store`` — :class:`~mai.knowledge.documents.DocumentStore` owns
  the canonical write: revision-checked, atomic, provenance-carrying, and
  every write already emits the durable ``upsert`` reindex intent (C07
  rule 2 / C02 rule 5 outbox).
- ``07-chunk-index`` / ``07-retrieval`` — the optional reconcile seam
  consumes the intent through the reviewed
  :class:`~mai.knowledge.index.ScopeIndexer`; the generated doc is then
  retrievable with full citations (the feature-gate "references must
  resolve" half).

Classification (C07 rule 5), decided by run truth, never by prose:

- run ``succeeded`` → a **runbook** in the ``runbooks`` collection. Its
  ``verification`` is ``verified`` only when the run outcome is
  host-verified AND every settled step is host-verified; any unverified
  step downgrades the whole document to ``hypothesis`` — an authoritative
  success instruction requires a fully verified success run.
- run ``failed`` → an **incident** record in the ``docs`` collection,
  ``verified`` when the failure itself is host-verified, else
  ``hypothesis``. A failed run can never produce a runbook or a
  ``verified`` success instruction, no matter what the summary text claims
  (the payload prose is evidence *content*, not decision input).
- any other run state (including ``cancelled`` and the application-level
  states) is rejected: only settled completed/failed evidence may become a
  canonical doc. Unsettled step states (``pending``/``running``/
  ``outcome_unknown``/``cancel_requested``) are rejected the same way —
  reconciliation owners settle them first; this generator never snapshots
  an in-flight fact.

Secret boundary (C03 rule 6 posture, mirroring the reviewed
``07-doc-store`` guard): content-based secret detection is prohibited, so
the enforcement is structural. (a) The CLI evidence-file read goes through
the same guarded outside-read vocabulary as the doc-store import
(credential-material names, symlink refusal, regular-file only, size/UTF-8
bounds). (b) Every evidence reference must be a bounded opaque reference:
no NUL, no ``..`` segment, no backslash, and the basename must not be a
credential-material name (``.env`` family, ``id_*`` key stems,
pem/key/p12/pfx suffixes) — the deny-list authority is
:func:`mai.knowledge.documents._is_secret_name`, imported (not re-invented)
so the two modules can never drift; a consumer test pins the identity.
Producers (the host capabilities) already redact output; these gates keep
credential *material and names* out of generated canonical docs, never
scanning host content.

Determinism: the renderer is a pure function of the evidence payload
(classification, verification included) — no clock, no environment, no
network. The same evidence yields byte-identical Markdown, so a retried
collector recomputes the same content and the revision check on
``DocumentStore.write`` is the concurrency guard (single-writer per
document remains a caller invariant, engine locks, C05 rule 6). The
document id is derived deterministically from the run id, so duplicate
deliveries of the same run evidence revise one canonical document instead
of minting siblings. Wall-clock facts travel inside the payload
(``captured_at``) as evidence content, never from this module.

Production entrypoint (exit contract 0 success / 1 structured rejection /
2 usage or IO error)::

    uv run python -m mai.knowledge.generate preview EVIDENCE.json
    uv run python -m mai.knowledge.generate commit ROOT EVIDENCE.json \
        [--base-url URL [--credential-ref REF] [--env-file PATH]
         [--timeout-seconds N] [--header NAME=VALUE]]

``preview`` validates and renders without writing (prints classification,
verification, namespace, deterministic doc id and the content hash).
``commit`` writes the canonical document through the doc store (durable
reindex intent emitted) and, when ``--base-url`` is given, reconciles the
scope's intent outbox through the real chunk indexer; the printed JSON is
value-free (revision/hash/provenance plus per-intent reconcile statuses).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import stat
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from mai.contracts.jobs import RunState, StepState
from mai.knowledge import documents as _documents
from mai.knowledge.documents import (
    ABSENT_REVISION,
    DocumentNamespace,
    DocumentStore,
    DocumentStoreError,
    DocumentStoreErrorCode,
    SourceKind,
    StoredDocument,
    VerificationStatus,
)

__all__ = [
    "EVIDENCE_PAYLOAD_VERSION",
    "GENERATOR_LABEL",
    "MAX_ERROR_CHARS",
    "MAX_EVIDENCE_REF_CHARS",
    "MAX_EVIDENCE_REFS",
    "MAX_STEP_DISPLAY_CHARS",
    "MAX_STEPS",
    "MAX_SUMMARY_CHARS",
    "MAX_TEXT_FIELD_CHARS",
    "DocClassification",
    "DocGenerationError",
    "DocGenerationErrorCode",
    "DocGenerator",
    "GenerateResult",
    "JobEvidence",
    "ModelPins",
    "StepEvidence",
    "classify",
    "doc_id_for_run",
    "parse_job_evidence",
    "render_document",
]

#: The payload document version this freeze validates.
EVIDENCE_PAYLOAD_VERSION: int = 1

#: Provenance origin marker recorded on every generated document.
GENERATOR_LABEL: str = "mai.knowledge.generate"

#: Deterministic namespace for the run→document id derivation (C01 rule 1:
#: opaque stable identity; one canonical generated document per run).
_DOC_ID_NAMESPACE = uuid.UUID("3b6dd8d2-6c0e-4b0a-9f6d-1e2a3b4c5d6e")

#: Bounds for the evidence text fields (operator-scale, never corpus-scale).
MAX_SUMMARY_CHARS: int = 4096
MAX_ERROR_CHARS: int = 2048
MAX_STEP_DISPLAY_CHARS: int = 256
MAX_TEXT_FIELD_CHARS: int = 256

#: Bounds for the step list and per-step evidence references.
MAX_STEPS: int = 128
MAX_EVIDENCE_REFS: int = 16
MAX_EVIDENCE_REF_CHARS: int = 256

#: The only run states that may become a canonical document. ``cancelled``
#: is deliberately excluded: it records an interruption, not an outcome —
#: no verified procedure and no incident failure to document.
_DOC_RUN_STATES = frozenset({RunState.SUCCEEDED, RunState.FAILED})

#: The only step states accepted into evidence: settled facts. Anything
#: open (``outcome_unknown``, ``cancel_requested``, ``running``,
#: ``pending``) belongs to the reconciliation owners and is rejected here.
_SETTLED_STEP_STATES = frozenset(
    {StepState.SUCCEEDED, StepState.FAILED, StepState.CANCELLED}
)

#: The doc-store layout/vocabulary authority for the secret name deny-list
#: (C03 rule 6 posture). Imported, not re-invented: a consumer test pins
#: this identity so the two modules cannot drift apart.
_is_secret_name = _documents._is_secret_name


class DocClassification(StrEnum):
    """The kind of canonical document one run's evidence produces."""

    RUNBOOK = "runbook"
    INCIDENT = "incident"


class DocGenerationErrorCode(StrEnum):
    """Stable machine-readable rejection codes (C01 envelope vocabulary)."""

    INVALID_PAYLOAD = "invalid_payload"
    NON_TERMINAL_RUN = "non_terminal_run"
    UNSETTLED_STEP = "unsettled_step"
    SECRET_REFERENCE = "secret_reference"
    INVALID_EVIDENCE_REF = "invalid_evidence_ref"


class DocGenerationError(ValueError):
    """One structured, value-free evidence rejection."""

    def __init__(self, code: DocGenerationErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


def _reject(code: DocGenerationErrorCode, detail: str) -> DocGenerationError:
    return DocGenerationError(code, detail)


def _canonical_uuid(value: object, field: str) -> uuid.UUID:
    if not isinstance(value, str):
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD, f"{field} must be a UUID string"
        )
    try:
        return uuid.UUID(value)
    except ValueError:
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            f"{field} must be a canonical UUID string",
        ) from None


def _bounded_text(
    value: object, field: str, maximum: int, *, allow_empty: bool = False
) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            f"{field} must be a string without NUL bytes",
        )
    if not allow_empty and not value.strip():
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD, f"{field} must be non-empty"
        )
    if len(value) > maximum:
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            f"{field} is {len(value)} characters, limit is {maximum}",
        )
    return value


def _validated_evidence_ref(value: object) -> str:
    """Structural validation of one evidence reference (C03 rule 6 posture).

    The reference is an opaque citation (a receipt directory, a controller
    intent path, an evidence-artifact handle); it is never opened here.
    Credential-material *names* are refused; content is never scanned.
    """
    if not isinstance(value, str) or not value.strip():
        raise _reject(
            DocGenerationErrorCode.INVALID_EVIDENCE_REF,
            "evidence reference must be a non-empty string",
        )
    if len(value) > MAX_EVIDENCE_REF_CHARS:
        raise _reject(
            DocGenerationErrorCode.INVALID_EVIDENCE_REF,
            f"evidence reference exceeds {MAX_EVIDENCE_REF_CHARS} characters",
        )
    if "\x00" in value:
        raise _reject(
            DocGenerationErrorCode.INVALID_EVIDENCE_REF,
            "evidence reference contains NUL bytes",
        )
    if "\\" in value:
        raise _reject(
            DocGenerationErrorCode.INVALID_EVIDENCE_REF,
            "evidence reference must use POSIX separators",
        )
    # A single leading empty segment is the POSIX absolute root (real
    # host-capability receipt references are absolute); every other segment
    # must be a plain name — no traversal, no dot segments, no empties.
    segments = value.split("/")
    absolute = segments[0] == ""
    checked = segments[1:] if absolute else segments
    if any(segment in ("", ".", "..") for segment in checked):
        raise _reject(
            DocGenerationErrorCode.INVALID_EVIDENCE_REF,
            "evidence reference must not contain empty, dot or dot-dot segments",
        )
    basename = segments[-1]
    if _is_secret_name(basename):
        raise _reject(
            DocGenerationErrorCode.SECRET_REFERENCE,
            f"evidence reference names credential material {basename!r}",
        )
    return value


@dataclass(frozen=True, slots=True)
class ModelPins:
    """The pinned model selection recorded by the run (C05 rule 3/C06 rule 1)."""

    connection: str
    profile: str
    connection_revision: int
    profile_revision: int


@dataclass(frozen=True, slots=True)
class StepEvidence:
    """One settled step of the run: frozen vocabulary + host verification."""

    seq: int
    capability_id: str
    status: StepState
    verified: bool
    display: str
    exit_code: int | None
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class JobEvidence:
    """Validated, value-free terminal-run evidence (the generation input)."""

    run_id: uuid.UUID
    server_id: uuid.UUID
    app_id: uuid.UUID | None
    status: RunState
    host_verified: bool
    summary: str
    error: str | None
    captured_at: str | None
    model: ModelPins | None
    steps: tuple[StepEvidence, ...]


def _parse_step(index: int, value: object) -> StepEvidence:
    if not isinstance(value, Mapping):
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD, f"steps[{index}] must be an object"
        )
    allowed = {
        "seq",
        "capability_id",
        "status",
        "verified",
        "display",
        "exit_code",
        "evidence_refs",
    }
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            f"steps[{index}] has unknown field {unknown[0]!r}",
        )
    seq = value.get("seq")
    if (
        not isinstance(seq, int)
        or isinstance(seq, bool)
        or not 1 <= seq <= 2**31 - 1
        or (index > 0 and seq <= index)
    ):
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            f"steps[{index}].seq must be a positive increasing integer",
        )
    capability_id = _bounded_text(
        value.get("capability_id"), f"steps[{index}].capability_id", MAX_TEXT_FIELD_CHARS
    )
    status_raw = value.get("status")
    try:
        status = StepState(status_raw)
    except ValueError:
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            f"steps[{index}].status must be one of "
            f"{sorted(state.value for state in StepState)}",
        ) from None
    if status not in _SETTLED_STEP_STATES:
        raise _reject(
            DocGenerationErrorCode.UNSETTLED_STEP,
            f"steps[{index}].status {status.value!r} is not settled; "
            "reconciliation must settle the step before it becomes evidence",
        )
    verified = value.get("verified")
    if not isinstance(verified, bool):
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            f"steps[{index}].verified must be a boolean",
        )
    display = _bounded_text(
        value.get("display"), f"steps[{index}].display", MAX_STEP_DISPLAY_CHARS
    )
    exit_code = value.get("exit_code")
    if exit_code is not None and (
        not isinstance(exit_code, int) or isinstance(exit_code, bool)
    ):
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            f"steps[{index}].exit_code must be an integer or null",
        )
    refs_value = value.get("evidence_refs", [])
    if not isinstance(refs_value, list):
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            f"steps[{index}].evidence_refs must be a list",
        )
    if len(refs_value) > MAX_EVIDENCE_REFS:
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            f"steps[{index}].evidence_refs exceeds {MAX_EVIDENCE_REFS} entries",
        )
    refs = tuple(_validated_evidence_ref(ref) for ref in refs_value)
    return StepEvidence(
        seq=seq,
        capability_id=capability_id,
        status=status,
        verified=verified,
        display=display,
        exit_code=exit_code,
        evidence_refs=refs,
    )


def _parse_model_pins(value: object) -> ModelPins:
    if not isinstance(value, Mapping):
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD, "model must be an object"
        )
    allowed = {"connection", "profile", "connection_revision", "profile_revision"}
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            f"model has unknown field {unknown[0]!r}",
        )
    connection = _bounded_text(value.get("connection"), "model.connection", 200)
    profile = _bounded_text(value.get("profile"), "model.profile", 200)
    connection_revision = value.get("connection_revision")
    profile_revision = value.get("profile_revision")
    for field, revision in (
        ("connection_revision", connection_revision),
        ("profile_revision", profile_revision),
    ):
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
        ):
            raise _reject(
                DocGenerationErrorCode.INVALID_PAYLOAD,
                f"model.{field} must be a positive integer",
            )
    return ModelPins(
        connection=connection,
        profile=profile,
        connection_revision=connection_revision,  # type: ignore[arg-type]
        profile_revision=profile_revision,  # type: ignore[arg-type]
    )


def parse_job_evidence(payload: Mapping[str, object]) -> JobEvidence:
    """Validate one evidence payload; fail closed before any store touch.

    Strict shape (the frozen field set, ``v`` = this freeze's version);
    run identity and scope are canonical UUIDs (C01 rule 1); the run status
    must be a settled ``succeeded``/``failed`` and every step a settled
    terminal state — unsettled facts are rejected for the reconciliation
    owners, never snapshotted into a canonical document.
    """
    if not isinstance(payload, Mapping):
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            "evidence payload must be a JSON object",
        )
    allowed = {
        "v",
        "run_id",
        "server_id",
        "app_id",
        "status",
        "host_verified",
        "summary",
        "error",
        "captured_at",
        "model",
        "steps",
    }
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            f"unknown evidence field {unknown[0]!r}",
        )
    missing = {"v", "run_id", "server_id", "status", "host_verified", "summary", "steps"} - set(
        payload
    )
    if missing:
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            f"missing evidence field {sorted(missing)[0]!r}",
        )
    if payload["v"] != EVIDENCE_PAYLOAD_VERSION:
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            f"v must be {EVIDENCE_PAYLOAD_VERSION}",
        )
    run_id = _canonical_uuid(payload["run_id"], "run_id")
    server_id = _canonical_uuid(payload["server_id"], "server_id")
    app_id = _canonical_uuid(payload["app_id"], "app_id") if "app_id" in payload else None
    status_raw = payload["status"]
    try:
        status = RunState(status_raw)
    except ValueError:
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            f"status must be one of {sorted(state.value for state in RunState)}",
        ) from None
    if status not in _DOC_RUN_STATES:
        raise _reject(
            DocGenerationErrorCode.NON_TERMINAL_RUN,
            f"run status {status.value!r} is not completed or failed evidence; "
            "only settled terminal runs may become a canonical document",
        )
    host_verified = payload["host_verified"]
    if not isinstance(host_verified, bool):
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            "host_verified must be a boolean",
        )
    summary = _bounded_text(payload["summary"], "summary", MAX_SUMMARY_CHARS)
    error = None
    if payload.get("error") is not None:
        error = _bounded_text(payload["error"], "error", MAX_ERROR_CHARS)
    captured_at = None
    if payload.get("captured_at") is not None:
        captured_at = _bounded_text(
            payload["captured_at"], "captured_at", MAX_TEXT_FIELD_CHARS
        )
    model = _parse_model_pins(payload["model"]) if "model" in payload else None
    steps_value = payload["steps"]
    if not isinstance(steps_value, list):
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD, "steps must be a list"
        )
    if len(steps_value) > MAX_STEPS:
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            f"steps exceeds {MAX_STEPS} entries",
        )
    steps = tuple(_parse_step(index, step) for index, step in enumerate(steps_value))
    return JobEvidence(
        run_id=run_id,
        server_id=server_id,
        app_id=app_id,
        status=status,
        host_verified=host_verified,
        summary=summary,
        error=error,
        captured_at=captured_at,
        model=model,
        steps=steps,
    )


def doc_id_for_run(run_id: uuid.UUID) -> uuid.UUID:
    """The one canonical generated document for one run (idempotent retries)."""
    return uuid.uuid5(_DOC_ID_NAMESPACE, f"mai:knowledge:generate:{run_id}")


def classify(
    evidence: JobEvidence,
) -> tuple[DocClassification, DocumentNamespace, VerificationStatus]:
    """The C07 rule 5 decision: document kind, collection and verification.

    Pure function of the settled run truth — never of the payload prose.
    A failed run is an incident record in ``docs``; a succeeded run is a
    runbook in ``runbooks``; a runbook is ``verified`` only when the run
    outcome and every step are host-verified, otherwise ``hypothesis``.
    """
    if evidence.status is RunState.FAILED:
        verification = (
            VerificationStatus.VERIFIED
            if evidence.host_verified
            else VerificationStatus.HYPOTHESIS
        )
        return DocClassification.INCIDENT, DocumentNamespace.DOCS, verification
    all_steps_verified = all(step.verified for step in evidence.steps)
    if evidence.host_verified and all_steps_verified:
        verification = VerificationStatus.VERIFIED
    else:
        verification = VerificationStatus.HYPOTHESIS
    return DocClassification.RUNBOOK, DocumentNamespace.RUNBOOKS, verification


def _verification_word(verification: VerificationStatus) -> str:
    return "host-verified" if verification is VerificationStatus.VERIFIED else "hypothesis"


def render_document(
    evidence: JobEvidence,
    classification: DocClassification,
    verification: VerificationStatus,
) -> str:
    """Render the canonical Markdown document (pure, deterministic).

    No clock/env/network: identical evidence yields byte-identical output,
    so a retried collector converges on the same revision and the store's
    revision check is the only concurrency guard needed.
    """
    lines: list[str] = []
    title = "Runbook" if classification is DocClassification.RUNBOOK else "Incident"
    lines.append(f"# {title}: run {evidence.run_id}")
    lines.append("")
    lines.append(f"- classification: {classification.value}")
    lines.append(f"- run status: {evidence.status.value}")
    lines.append(f"- verification: {verification.value}")
    lines.append(f"- server_id: `{evidence.server_id}`")
    if evidence.app_id is not None:
        lines.append(f"- app_id: `{evidence.app_id}`")
    if evidence.model is not None:
        lines.append(
            f"- model: {evidence.model.connection}"
            f"@{evidence.model.connection_revision} /"
            f" {evidence.model.profile}@{evidence.model.profile_revision}"
        )
    if evidence.captured_at is not None:
        lines.append(f"- captured_at: {evidence.captured_at}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(evidence.summary)
    lines.append("")
    lines.append("## Steps")
    lines.append("")
    if not evidence.steps:
        lines.append("No settled steps were recorded for this run.")
        lines.append("")
    for step in evidence.steps:
        step_verification = "host-verified" if step.verified else "hypothesis"
        lines.append(
            f"### step {step.seq} — {step.capability_id} — "
            f"{step.status.value} ({step_verification})"
        )
        lines.append("")
        lines.append(f"- display: {step.display}")
        if step.exit_code is not None:
            lines.append(f"- exit_code: {step.exit_code}")
        for ref in step.evidence_refs:
            lines.append(f"- evidence: `{ref}`")
        lines.append("")
    if evidence.status is RunState.FAILED:
        lines.append("## Failure")
        lines.append("")
        lines.append(evidence.error or "The run failed; no bounded error text was recorded.")
        lines.append("")
    lines.append("---")
    lines.append(
        f"generated_by: {GENERATOR_LABEL} v{EVIDENCE_PAYLOAD_VERSION}"
        " (deterministic evidence-backed render; update only from new"
        " settled evidence for the same run)"
    )
    return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class GenerateResult:
    """The outcome of one generation: the stored doc + reindex truth."""

    doc: StoredDocument
    classification: DocClassification
    verification: VerificationStatus
    reindex: tuple[object, ...] | None


#: The optional reconcile seam: consumes the scope's durable intent outbox
#: (the reviewed chunk indexer) and returns per-intent reconcile outcomes.
ReconcileFn = Callable[[], Awaitable[Sequence[object]]]


class DocGenerator:
    """Revision-checked generated-doc writer over the canonical doc store.

    Composition is constructor-explicit (no ambient engine/database): the
    store owns the canonical write and its durable reindex intent; the
    optional reconcile hook runs the reviewed indexer after the intent is
    durable. A missing hook leaves the intent pending — honest, the next
    reconcile converges — never a fabricated "reindexed" claim.
    """

    def __init__(
        self,
        store: DocumentStore,
        reconcile: ReconcileFn | None = None,
    ) -> None:
        self._store = store
        self._reconcile = reconcile

    async def generate(self, payload: Mapping[str, object]) -> GenerateResult:
        """Validate, classify, write and (optionally) reindex one run's doc."""
        evidence = parse_job_evidence(payload)
        classification, namespace, verification = classify(evidence)
        doc_id = doc_id_for_run(evidence.run_id)
        text = render_document(evidence, classification, verification)
        try:
            current = self._store.read(
                doc_id,
                server_id=evidence.server_id,
                app_id=evidence.app_id,
                namespace=namespace,
            )
            expected_revision = current.revision
        except DocumentStoreError as exc:
            if exc.code is DocumentStoreErrorCode.NOT_FOUND:
                expected_revision = ABSENT_REVISION
            else:
                # TOMBSTONE_CONFLICT and friends surface unchanged (C02 W4).
                raise
        stored = self._store.write(
            doc_id,
            text,
            server_id=evidence.server_id,
            app_id=evidence.app_id,
            namespace=namespace,
            expected_revision=expected_revision,
            source_kind=SourceKind.GENERATED,
            origin=f"run:{evidence.run_id}",
            verification=verification,
        )
        reindex: tuple[object, ...] | None = None
        if self._reconcile is not None:
            reindex = tuple(await self._reconcile())
        return GenerateResult(
            doc=stored,
            classification=classification,
            verification=verification,
            reindex=reindex,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read_evidence_file(path: str) -> Mapping[str, object]:
    """Guarded read of one caller-supplied evidence file (outside seam).

    Mirrors the doc-store outside-read vocabulary (its vocabulary
    authority): credential-material names refused, symlink refused,
    regular-file only, size/UTF-8 bounded. Content itself is never scanned
    for secrets (C03 rule 6).
    """
    source = Path(path)
    if _is_secret_name(source.name):
        raise _reject(
            DocGenerationErrorCode.SECRET_REFERENCE,
            f"refusing to read credential-material file name {source.name!r}",
        )
    try:
        info = source.lstat()
    except FileNotFoundError:
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            f"evidence file {source} does not exist",
        ) from None
    if stat.S_ISLNK(info.st_mode):
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            f"evidence file {source} is a symbolic link; refuse rather than "
            "follow a swap-able alias",
        ) from None
    if not stat.S_ISREG(info.st_mode):
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            f"evidence file {source} is not a regular file",
        ) from None
    try:
        data = source.read_bytes()
    except PermissionError:
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            f"evidence file {source} is not readable",
        ) from None
    if len(data) > 1024 * 1024:
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            "evidence file exceeds the 1 MiB fixture bound",
        ) from None
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            "evidence file is not valid UTF-8 JSON",
        ) from None
    if not isinstance(document, Mapping):
        raise _reject(
            DocGenerationErrorCode.INVALID_PAYLOAD,
            "evidence file must hold one JSON object",
        ) from None
    return document


def _preview_document(payload: Mapping[str, object]) -> dict[str, object]:
    evidence = parse_job_evidence(payload)
    classification, namespace, verification = classify(evidence)
    text = render_document(evidence, classification, verification)
    return {
        "kind": "preview",
        "classification": classification.value,
        "namespace": namespace.value,
        "verification": verification.value,
        "doc_id": str(doc_id_for_run(evidence.run_id)),
        "server_id": str(evidence.server_id),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "bytes": len(text.encode("utf-8")),
    }


def _result_document(result: GenerateResult) -> dict[str, object]:
    document: dict[str, object] = {
        "kind": "committed",
        "classification": result.classification.value,
        "namespace": result.doc.namespace.value,
        "verification": result.verification.value,
        "doc_id": str(result.doc.doc_id),
        "server_id": str(result.doc.server_id) if result.doc.server_id else None,
        "app_id": str(result.doc.app_id) if result.doc.app_id else None,
        "revision": result.doc.revision,
        "sha256": result.doc.sha256,
        "source_kind": result.doc.source_kind.value,
        "origin": result.doc.origin,
    }
    if result.reindex is None:
        document["reindex"] = None
    else:
        document["reindex"] = [
            {
                "doc_id": str(getattr(outcome, "doc_id", "")),
                "namespace": getattr(outcome, "namespace", None),
                "op": getattr(outcome, "op", None),
                "status": getattr(
                    getattr(outcome, "status", None), "value", getattr(outcome, "status", None)
                ),
                "revision": getattr(outcome, "revision", None),
                "chunks": getattr(outcome, "chunks", None),
            }
            for outcome in result.reindex
        ]
    return document


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True, ensure_ascii=True))


async def _commit_async(args: argparse.Namespace) -> int:
    from mai.credentials.env import EnvResolver
    from mai.database import create_app_engine, resolve_database_url
    from mai.knowledge.index import GatewayEmbedder, ScopeIndexer
    from mai.agents.models import (
        SCHEMA_VERSION as CONNECTION_SCHEMA_VERSION,
        parse_connection,
    )

    payload = _read_evidence_file(args.evidence)
    evidence = parse_job_evidence(payload)
    store = DocumentStore(args.root)
    reconcile: ReconcileFn | None = None
    engine = None
    if args.base_url is not None:
        try:
            engine, sessions = create_app_engine(resolve_database_url())
        except Exception as exc:  # noqa: BLE001 - usage/IO boundary
            _emit(
                {
                    "kind": "error",
                    "code": "database_unavailable",
                    "detail": type(exc).__name__,
                }
            )
            return 2

        async def _reconcile() -> Sequence[object]:
            assert engine is not None
            connection = parse_connection(
                {
                    "schema_version": CONNECTION_SCHEMA_VERSION,
                    "name": "doc-generation-reindex",
                    "base_url": args.base_url,
                    "credential_ref": args.credential_ref,
                    "timeout_seconds": args.timeout_seconds,
                    "headers": {
                        name: value
                        for name, _, value in (
                            header.partition("=") for header in (args.header or ())
                        )
                        if name
                    },
                }
            )
            resolver = EnvResolver(args.env_file) if args.env_file else None
            indexer = ScopeIndexer(sessions, store, GatewayEmbedder(connection, resolver))
            return await indexer.reconcile_scope(
                server_id=evidence.server_id, app_id=evidence.app_id
            )

        reconcile = _reconcile
    try:
        result = await DocGenerator(store, reconcile).generate(payload)
    finally:
        if engine is not None:
            await engine.dispose()
    _emit(_result_document(result))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mai.knowledge.generate",
        description="Generate evidence-backed runbook/incident documents "
        "from settled run evidence, revision-checked through the canonical "
        "doc store with durable reindex integration.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    preview = sub.add_parser(
        "preview", help="validate and render without writing anything"
    )
    preview.add_argument("evidence", metavar="EVIDENCE.json")
    commit = sub.add_parser(
        "commit", help="write the canonical document and optionally reindex"
    )
    commit.add_argument("root", metavar="ROOT", help="explicit doc-store data root")
    commit.add_argument("evidence", metavar="EVIDENCE.json")
    commit.add_argument(
        "--base-url",
        default=None,
        help="OpenAI-compatible endpoint root; when given, the scope's "
        "reindex intent outbox is reconciled through the real chunk indexer",
    )
    commit.add_argument(
        "--credential-ref",
        default=None,
        help="credential reference resolved through --env-file at request time",
    )
    commit.add_argument(
        "--env-file",
        default=None,
        help="explicit .env credential source (C03 rule 6: no discovery)",
    )
    commit.add_argument("--timeout-seconds", type=float, default=None)
    commit.add_argument(
        "--header",
        action="append",
        metavar="NAME=VALUE",
        help="extra endpoint header (repeatable)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: exit 0 committed / 1 structured rejection / 2 usage."""
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "preview":
            _emit(_preview_document(_read_evidence_file(args.evidence)))
            return 0
        if args.command == "commit":
            return asyncio.run(_commit_async(args))
    except DocGenerationError as exc:
        _emit({"kind": "error", "code": exc.code.value, "detail": exc.detail})
        return 1
    except (DocumentStoreError, ValueError) as exc:
        detail = str(exc)
        code = (
            exc.code.value
            if isinstance(exc, DocumentStoreError)
            else detail.split(":", 1)[0]
        )
        _emit({"kind": "error", "code": code, "detail": detail})
        return 1
    return 2  # pragma: no cover - argparse `required=True` guards this


if __name__ == "__main__":  # pragma: no cover - argparse glue
    sys.exit(main())

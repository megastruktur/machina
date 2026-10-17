"""Declarative portable skill package loader — bounded, read-only, inert.

Cards ``09-skills`` (P09, service ``skills``) against contracts.md
**C03 rule 7** ("No installation-time auto-grant exists"), **C06 rules
2-3** (context assembly order; size/token budgets with references and
provenance) and IDEA.md §13: "Skills provide procedural instructions and
knowledge for using enabled capabilities. Installing a skill must not
silently grant tools or privileges."

The selected file format (AC1)
------------------------------

P00 left the portable skill format open (IDEA.md §13); this card freezes
v1 as one directory per skill below the canonical shared namespace of
the data root (SERVICES/IDEA §"Canonical data ownership": ``shared/``):

.. code-block:: text

    <data_root>/shared/skills/<skill>/skill.yaml        # manifest
    <data_root>/shared/skills/<skill>/instructions.md   # bounded instructions
    <data_root>/shared/skills/<skill>/<reference>       # optional declared files

The manifest is the only configuration surface and its vocabulary is
frozen and strict (unknown fields are rejected, never ignored — the
profile/plugin-config convention):

.. code-block:: yaml

    schema_version: 1                # required, exactly 1
    skill: deploy-webapp             # required, == package directory name
    title: Deploy the web app        # optional, <= 200 chars
    description: Step-by-step ...    # optional, <= 512 chars
    requires_capabilities:           # optional declared prerequisites
      - deploy.app                   # (capability slug charset, C03 rule 1)
    references:                      # optional declared reference files
      - runbook.md                   # relative paths inside the package

Provenance: every read file yields ``(name, bytes, sha256)``; the
package carries a deterministic ``package_sha256`` over the manifest +
document digest set, suitable as a context receipt / audit label.

What this loader does — and deliberately does not
------------------------------------------------

- **Read-only by construction.** The module opens files read-only
  (``O_NOFOLLOW`` per canonical-read discipline, C02 rule 3) and never
  creates, writes, renames or deletes anything: loading is not
  installing, and there is no import API (negative controls pin the
  import surface). Skill content is inert validated data.
- **Only declared files are read.** Manifest vocabulary has no tool,
  grant, install or script fields; undeclared files (including scripts)
  in the package directory are never opened and never appear in any
  digest. Script-based capabilities are explicitly later work (IDEA
  §13) — a ``.sh``/``.py`` file is packaging inert weight here.
- **Skills never grant anything (AC2).** ``requires_capabilities`` is a
  *declaration*: :func:`missing_prerequisites` reports it against a
  caller-supplied allowed set (the run's
  ``ExecutionContext.allowed_capabilities``, C03 rule 2) — it never
  mutates grants, the registry, policy or the dispatcher. File content
  cannot create a tool grant: the tool surface remains exactly
  ``allowed_capabilities ∧ registry`` (``mai.agents.tools`` bridge);
  a loaded skill only ever adds *context*.
- **Bounded context injection (C06 rules 2-3).**
  :func:`skill_evidence` converts instructions + declared references
  into shared-namespace :class:`~mai.agents.context.ContextEvidence`
  documents (kind ``doc``, per-file sha256 revision, provenance in the
  receipt). Skill prose is untrusted source data behind the assembly's
  random boundary — context, never system authority (GLOBAL_INSTRUCTIONS
  binding rule 1); budget/truncation/overflow semantics stay owned by
  the reviewed assembly.
- **Approved directory only.** The package directory is derived from the
  validated skill name alone; every path component is checked for
  symbolic links and final opens use ``O_NOFOLLOW``; declared reference
  paths must be relative, may not contain ``..``/empty/backslash
  components and are resolved — confined — inside the package directory.
  Escape attempts are structured rejections, never partial reads.

Value-free discipline: rejection details carry skill names, paths,
field names and bound numbers — never manifest or instruction *content*.
One rejected package yields one structured rejection; nothing partial is
returned. The module performs no logging, reads no process environment,
touches no clock or network state and executes nothing.

Thread safety: all functions are pure filesystem reads of immutable
package content; the returned dataclasses are frozen.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import stat as stat_module
from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from collections.abc import Sequence

from mai.agents.context import ContextEvidence, EvidenceKind

__all__ = [
    "CAPABILITY_ID_RE",
    "INSTRUCTIONS_FILENAME",
    "MANIFEST_FILENAME",
    "MAX_DESCRIPTION_CHARS",
    "MAX_INSTRUCTIONS_BYTES",
    "MAX_MANIFEST_BYTES",
    "MAX_PACKAGE_BYTES",
    "MAX_REFERENCE_BYTES",
    "MAX_REFERENCE_PATH_CHARS",
    "MAX_REFERENCES",
    "MAX_REQUIRES",
    "MAX_TITLE_CHARS",
    "SCHEMA_VERSION",
    "SKILLS_SUBPATH",
    "SKILL_NAME_RE",
    "SkillDocument",
    "SkillLoadError",
    "SkillLoadErrorCode",
    "SkillManifest",
    "SkillPackage",
    "load_skill",
    "main",
    "missing_prerequisites",
    "skill_evidence",
]

# ---------------------------------------------------------------------------
# Frozen format constants (the selected v1 file format)
# ---------------------------------------------------------------------------

#: Canonical location of portable skills under the data root: the shared
#: namespace (IDEA §"Canonical data ownership" ``shared/skills/``). Skills
#: are server-independent knowledge, so the loader takes the data root and
#: derives the package path from the validated name alone.
SKILLS_SUBPATH = PurePosixPath("shared", "skills")

MANIFEST_FILENAME = "skill.yaml"
INSTRUCTIONS_FILENAME = "instructions.md"

#: Frozen manifest schema revision. A future revision is an explicit
#: reconciliation (new loader capability), never a silent accept.
SCHEMA_VERSION = 1

#: Canonical byte caps (same order as the canonical prompt/document caps
#: of the profile/plugin stores, 64 KiB): a skill is bounded knowledge,
#: not a data dump. The package total caps the declared set as a whole.
MAX_MANIFEST_BYTES = 64 * 1024
MAX_INSTRUCTIONS_BYTES = 64 * 1024
MAX_REFERENCE_BYTES = 64 * 1024
MAX_PACKAGE_BYTES = 256 * 1024

#: Declared-set bounds. The reference-path length keeps the assembly
#: evidence ``source`` label (``skill/<name>/<path>``) inside the frozen
#: 256-char ``ContextEvidence`` cap even at maximum skill-name length.
MAX_REFERENCES = 16
MAX_REQUIRES = 32
MAX_TITLE_CHARS = 200
MAX_DESCRIPTION_CHARS = 512
MAX_REFERENCE_PATH_CHARS = 96

#: Skill (package) name shape: the capability slug charset (C03 rule 1
#: identifiers are lowercase slugs) — also a single path component, which
#: the anchored regex structurally guarantees (no ``/``, no ``..``).
#: Local copy of the frozen registry/config pattern, same review note:
#: the shape owners are outside this card's allowlist.
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

#: Declared prerequisite shape: capability identifier slug (C03 rule 1).
#: Same frozen charset as ``mai.plugins.registry``/``mai.plugins.config``
#: (anchored local copy with the same review note).
CAPABILITY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

#: Hex-digit set for sha256 labels (local copy, plugin-config convention).
_HEX_DIGITS = frozenset("0123456789abcdef")

# ---------------------------------------------------------------------------
# Structured rejections (C01 envelope vocabulary)
# ---------------------------------------------------------------------------


class SkillLoadErrorCode(StrEnum):
    """Stable machine-readable skill-load rejection codes."""

    SKILL_INVALID = "skill_invalid"
    SKILL_NOT_FOUND = "skill_not_found"
    PATH_ESCAPE = "path_escape"
    MANIFEST_MISSING = "manifest_missing"
    MANIFEST_UNREADABLE = "manifest_unreadable"
    MANIFEST_TOO_LARGE = "manifest_too_large"
    MANIFEST_INVALID = "manifest_invalid"
    MANIFEST_ALIAS_FORBIDDEN = "manifest_alias_forbidden"
    MANIFEST_FIELD_UNKNOWN = "manifest_field_unknown"
    SCHEMA_VERSION_UNSUPPORTED = "schema_version_unsupported"
    INSTRUCTIONS_MISSING = "instructions_missing"
    INSTRUCTIONS_UNREADABLE = "instructions_unreadable"
    INSTRUCTIONS_TOO_LARGE = "instructions_too_large"
    REFERENCE_INVALID = "reference_invalid"
    REFERENCE_MISSING = "reference_missing"
    REFERENCE_UNREADABLE = "reference_unreadable"
    REFERENCE_TOO_LARGE = "reference_too_large"
    PACKAGE_TOO_LARGE = "package_too_large"
    ALLOWED_INVALID = "allowed_invalid"


class SkillLoadError(ValueError):
    """One structured, value-free skill-load rejection."""

    def __init__(self, code: SkillLoadErrorCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


def _no_control_chars(value: str) -> bool:
    return not any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


# ---------------------------------------------------------------------------
# Loaded shapes (frozen, inert data — no code references, no execution)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkillDocument:
    """One provenance-stamped text document of a loaded skill package."""

    name: str  # ``instructions.md`` or the declared relative path
    text: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class SkillManifest:
    """Validated manifest (frozen vocabulary, strict fields)."""

    skill: str
    title: str | None
    description: str | None
    requires_capabilities: tuple[str, ...]
    references: tuple[str, ...]
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class SkillPackage:
    """One loaded skill: validated manifest + bounded documents.

    Inert by construction: text plus digests only. There is no handler,
    no tool, no code reference and no write surface — a package cannot
    ``do`` anything; it can only be (a) reported and (b) injected as
    bounded context evidence (C06 rules 2-3).
    """

    manifest: SkillManifest
    instructions: SkillDocument
    references: tuple[SkillDocument, ...]
    package_sha256: str
    total_bytes: int

    @property
    def name(self) -> str:
        """The validated skill name (== manifest ``skill``)."""
        return self.manifest.skill

    @property
    def requires_capabilities(self) -> tuple[str, ...]:
        """The declared prerequisite capability ids, manifest order."""
        return self.manifest.requires_capabilities


# ---------------------------------------------------------------------------
# Confinement + bounded reads (canonical-read discipline, C02 rule 3)
# ---------------------------------------------------------------------------


def _validated_skill_name(name: object) -> str:
    if not isinstance(name, str) or not SKILL_NAME_RE.fullmatch(name):
        raise SkillLoadError(
            SkillLoadErrorCode.SKILL_INVALID,
            f"skill name must match {SKILL_NAME_RE.pattern} (a single "
            "lowercase slug path component)",
        )
    return name


def _package_dir(root: Path, name: str) -> Path:
    """Resolve ``<root>/shared/skills/<name>`` with component confinement.

    Same discipline as the canonical profile/prompt reads: the path is
    derived from the validated name alone and every component below the
    caller-supplied root is checked for symbolic links, so a planted
    symlink cannot move the package outside the approved directory.
    """
    current = root
    for part in (*SKILLS_SUBPATH.parts, name):
        current /= part
        if current.is_symlink():
            raise SkillLoadError(
                SkillLoadErrorCode.PATH_ESCAPE,
                f"path component {part!r} is a symbolic link; the approved "
                "skills directory is confined",
            )
    if not current.is_dir():
        raise SkillLoadError(
            SkillLoadErrorCode.SKILL_NOT_FOUND,
            f"no skill package at the canonical shared/skills/{name} path",
        )
    return current


def _read_bounded(
    path: Path,
    *,
    cap: int,
    missing: SkillLoadErrorCode,
    unreadable: SkillLoadErrorCode,
    too_large: SkillLoadErrorCode,
    label: str,
) -> bytes:
    """One confined, size-capped, regular-file-only read (O_NOFOLLOW)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        raise SkillLoadError(missing, f"{label} is not present at its "
                             "canonical path") from None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise SkillLoadError(
                SkillLoadErrorCode.PATH_ESCAPE,
                f"{label} is a symbolic link; the approved package "
                "directory is confined",
            ) from None
        raise SkillLoadError(unreadable, f"{label} could not be opened") from None
    with os.fdopen(fd, "rb") as handle:
        if not stat_module.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise SkillLoadError(
                unreadable, f"{label} is not a regular file"
            )
        data = handle.read(cap + 1)
    if len(data) > cap:
        raise SkillLoadError(
            too_large, f"{label} exceeds the {cap}-byte cap"
        )
    return data


def _decode_utf8(data: bytes, *, unreadable: SkillLoadErrorCode, label: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise SkillLoadError(
            unreadable, f"{label} is not valid UTF-8"
        ) from None


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Manifest parsing (strict frozen vocabulary; plugin-config hardening)
# ---------------------------------------------------------------------------


class _StrictSafeLoader(yaml.SafeLoader):
    """``SafeLoader`` rejecting duplicate and unhashable mapping keys.

    PyYAML would otherwise silently keep the last of duplicate keys —
    unacceptable for a provenance-bearing manifest (plugin-config store
    loader, local copy).
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False):  # type: ignore[override]
        seen: list[object] = []
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
            except TypeError:
                raise yaml.YAMLError("unhashable mapping key") from None
            if duplicate:
                raise yaml.YAMLError("duplicate mapping key")
            seen.append(key)
        return super().construct_mapping(node, deep=deep)


def _contains_alias(root: yaml.Node) -> bool:
    """Detect YAML alias references before any construction (local copy).

    Composed aliases reuse the anchor's node object, so node-identity
    reuse in the composed tree proves an alias reference. Iterative
    walk — no recursion on hostile documents.
    """
    seen: set[int] = set()
    stack: list[yaml.Node] = [root]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            return True
        seen.add(id(node))
        if isinstance(node, yaml.MappingNode):
            for key_node, value_node in node.value:
                stack.append(key_node)
                stack.append(value_node)
        elif isinstance(node, yaml.SequenceNode):
            stack.extend(node.value)
    return False


_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "skill",
        "title",
        "description",
        "requires_capabilities",
        "references",
    }
)


def _parse_manifest(raw: bytes, *, name: str, sha256: str) -> SkillManifest:
    """Validate one manifest document against the frozen vocabulary."""
    text = _decode_utf8(
        raw, unreadable=SkillLoadErrorCode.MANIFEST_UNREADABLE, label="manifest"
    )
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as error:
        raise SkillLoadError(
            SkillLoadErrorCode.MANIFEST_INVALID,
            f"manifest is not valid YAML ({type(error).__name__})",
        ) from None
    except RecursionError:
        raise SkillLoadError(
            SkillLoadErrorCode.MANIFEST_INVALID,
            "manifest nesting exceeds the supported depth",
        ) from None
    if node is not None and _contains_alias(node):
        raise SkillLoadError(
            SkillLoadErrorCode.MANIFEST_ALIAS_FORBIDDEN,
            "manifest contains YAML alias nodes; anchor/alias expansion is "
            "not permitted in canonical files",
        )
    try:
        document = yaml.load(text, Loader=_StrictSafeLoader)
    except yaml.YAMLError as error:
        detail = str(error)
        if "duplicate mapping key" in detail or "unhashable" in detail:
            raise SkillLoadError(
                SkillLoadErrorCode.MANIFEST_INVALID, detail
            ) from None
        raise SkillLoadError(
            SkillLoadErrorCode.MANIFEST_INVALID,
            f"manifest is not valid YAML ({type(error).__name__})",
        ) from None
    if not isinstance(document, dict):
        raise SkillLoadError(
            SkillLoadErrorCode.MANIFEST_INVALID,
            "manifest root must be a mapping",
        )
    for key in sorted(document):
        if key not in _MANIFEST_FIELDS:
            raise SkillLoadError(
                SkillLoadErrorCode.MANIFEST_FIELD_UNKNOWN,
                f"manifest field {key!r}: unknown field (unknown fields are "
                "rejected, never ignored); the skill vocabulary carries no "
                "tool, grant or install surface",
            )
    version = document.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise SkillLoadError(
            SkillLoadErrorCode.MANIFEST_INVALID,
            "schema_version must be the integer 1",
        )
    if version != SCHEMA_VERSION:
        raise SkillLoadError(
            SkillLoadErrorCode.SCHEMA_VERSION_UNSUPPORTED,
            f"manifest schema_version {version} is not supported "
            f"(this loader implements {SCHEMA_VERSION})",
        )
    skill = document.get("skill")
    if not isinstance(skill, str) or not SKILL_NAME_RE.fullmatch(skill):
        raise SkillLoadError(
            SkillLoadErrorCode.MANIFEST_INVALID,
            f"manifest field 'skill' must match {SKILL_NAME_RE.pattern}",
        )
    if skill != name:
        raise SkillLoadError(
            SkillLoadErrorCode.MANIFEST_INVALID,
            f"manifest field 'skill' ({skill!r}) does not match the package "
            f"directory name ({name!r})",
        )
    title = _optional_bounded_text(
        document, "title", MAX_TITLE_CHARS, "manifest field 'title'"
    )
    description = _optional_bounded_text(
        document, "description", MAX_DESCRIPTION_CHARS, "manifest field "
        "'description'"
    )
    requires = _parse_requires(document.get("requires_capabilities"))
    references = _parse_reference_paths(document.get("references"))
    return SkillManifest(
        skill=skill,
        title=title,
        description=description,
        requires_capabilities=requires,
        references=references,
        sha256=sha256,
        size_bytes=len(raw),
    )


def _optional_bounded_text(
    document: dict[str, object], field: str, cap: int, label: str
) -> str | None:
    value = document.get(field)
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > cap
        or not _no_control_chars(value)
    ):
        raise SkillLoadError(
            SkillLoadErrorCode.MANIFEST_INVALID,
            f"{label} must be a printable non-empty string of "
            f"1..{cap} characters",
        )
    return value


def _parse_requires(raw: object) -> tuple[str, ...]:
    """Validate ``requires_capabilities``: declared prerequisite slugs.

    A declaration of facts about what the skill *needs* — checked by
    :func:`missing_prerequisites` against the caller's allowed set; the
    loader grants nothing for it (C03 rule 7).
    """
    if raw is None:
        return ()
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise SkillLoadError(
            SkillLoadErrorCode.MANIFEST_INVALID,
            "requires_capabilities must be a list of capability id strings",
        )
    if len(raw) > MAX_REQUIRES:
        raise SkillLoadError(
            SkillLoadErrorCode.MANIFEST_INVALID,
            f"requires_capabilities exceeds the {MAX_REQUIRES}-entry bound",
        )
    for item in raw:
        if not CAPABILITY_ID_RE.fullmatch(item):
            raise SkillLoadError(
                SkillLoadErrorCode.MANIFEST_INVALID,
                f"requires_capabilities entry {item!r} must match "
                f"{CAPABILITY_ID_RE.pattern} (C03 rule 1 capability slug)",
            )
    if len(set(raw)) != len(raw):
        raise SkillLoadError(
            SkillLoadErrorCode.MANIFEST_INVALID,
            "requires_capabilities contains duplicates",
        )
    return tuple(raw)


def _parse_reference_paths(raw: object) -> tuple[str, ...]:
    """Validate declared ``references``: confined relative paths.

    Structural rejection before any open: absolute paths, ``..``/empty
    components and backslashes cannot name anything inside the package
    directory; component-level symlink checks at read time close the
    planted-link escape.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise SkillLoadError(
            SkillLoadErrorCode.MANIFEST_INVALID,
            "references must be a list of relative path strings",
        )
    if len(raw) > MAX_REFERENCES:
        raise SkillLoadError(
            SkillLoadErrorCode.MANIFEST_INVALID,
            f"references exceeds the {MAX_REFERENCES}-entry bound",
        )
    for item in raw:
        _validated_reference_path(item)
    if len(set(raw)) != len(raw):
        raise SkillLoadError(
            SkillLoadErrorCode.MANIFEST_INVALID,
            "references contains duplicates",
        )
    return tuple(raw)


def _validated_reference_path(item: str) -> PurePosixPath:
    if (
        not 1 <= len(item) <= MAX_REFERENCE_PATH_CHARS
        or not _no_control_chars(item)
        or "\\" in item
    ):
        raise SkillLoadError(
            SkillLoadErrorCode.REFERENCE_INVALID,
            f"reference path must be a printable relative path of "
            f"1..{MAX_REFERENCE_PATH_CHARS} characters without backslashes",
        )
    pure = PurePosixPath(item)
    if pure.is_absolute() or item.startswith("/"):
        raise SkillLoadError(
            SkillLoadErrorCode.REFERENCE_INVALID,
            "reference path must be relative to the package directory",
        )
    for part in pure.parts:
        if part in ("", ".", ".."):
            raise SkillLoadError(
                SkillLoadErrorCode.REFERENCE_INVALID,
                f"reference path component {part!r} is not allowed "
                "(references stay inside the package directory)",
            )
    return pure


def _reference_path(package_dir: Path, relative: PurePosixPath) -> Path:
    """Resolve one validated reference under the package, no symlink hops."""
    current = package_dir
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise SkillLoadError(
                SkillLoadErrorCode.PATH_ESCAPE,
                f"reference path component {part!r} is a symbolic link; "
                "references stay inside the package directory",
            )
    return current


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_skill(root: str | os.PathLike[str], name: str) -> SkillPackage:
    """Load one skill package: manifest + instructions + declared references.

    Read-only and all-or-nothing: any structural rejection leaves the
    filesystem untouched and returns nothing partial.
    """
    name = _validated_skill_name(name)
    package_dir = _package_dir(Path(root), name)

    manifest_raw = _read_bounded(
        package_dir / MANIFEST_FILENAME,
        cap=MAX_MANIFEST_BYTES,
        missing=SkillLoadErrorCode.MANIFEST_MISSING,
        unreadable=SkillLoadErrorCode.MANIFEST_UNREADABLE,
        too_large=SkillLoadErrorCode.MANIFEST_TOO_LARGE,
        label="manifest",
    )
    manifest = _parse_manifest(
        manifest_raw, name=name, sha256=_digest(manifest_raw)
    )

    instructions_raw = _read_bounded(
        package_dir / INSTRUCTIONS_FILENAME,
        cap=MAX_INSTRUCTIONS_BYTES,
        missing=SkillLoadErrorCode.INSTRUCTIONS_MISSING,
        unreadable=SkillLoadErrorCode.INSTRUCTIONS_UNREADABLE,
        too_large=SkillLoadErrorCode.INSTRUCTIONS_TOO_LARGE,
        label="instructions",
    )
    instructions = SkillDocument(
        name=INSTRUCTIONS_FILENAME,
        text=_decode_utf8(
            instructions_raw,
            unreadable=SkillLoadErrorCode.INSTRUCTIONS_UNREADABLE,
            label="instructions",
        ),
        sha256=_digest(instructions_raw),
        size_bytes=len(instructions_raw),
    )

    documents: list[SkillDocument] = []
    for relative in manifest.references:
        pure = _validated_reference_path(relative)
        target = _reference_path(package_dir, pure)
        raw = _read_bounded(
            target,
            cap=MAX_REFERENCE_BYTES,
            missing=SkillLoadErrorCode.REFERENCE_MISSING,
            unreadable=SkillLoadErrorCode.REFERENCE_UNREADABLE,
            too_large=SkillLoadErrorCode.REFERENCE_TOO_LARGE,
            label=f"reference {relative!r}",
        )
        documents.append(
            SkillDocument(
                name=relative,
                text=_decode_utf8(
                    raw,
                    unreadable=SkillLoadErrorCode.REFERENCE_UNREADABLE,
                    label=f"reference {relative!r}",
                ),
                sha256=_digest(raw),
                size_bytes=len(raw),
            )
        )

    total = manifest.size_bytes + instructions.size_bytes + sum(
        document.size_bytes for document in documents
    )
    if total > MAX_PACKAGE_BYTES:
        raise SkillLoadError(
            SkillLoadErrorCode.PACKAGE_TOO_LARGE,
            f"declared package content is {total} bytes, limit is "
            f"{MAX_PACKAGE_BYTES}",
        )
    package_sha256 = _package_digest(manifest, instructions, documents)
    return SkillPackage(
        manifest=manifest,
        instructions=instructions,
        references=tuple(documents),
        package_sha256=package_sha256,
        total_bytes=total,
    )


def _package_digest(
    manifest: SkillManifest, instructions: SkillDocument, documents: list[SkillDocument]
) -> str:
    """Deterministic digest over the declared content set (audit label)."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "skill": manifest.skill,
        "manifest_sha256": manifest.sha256,
        "instructions": {
            "name": instructions.name,
            "sha256": instructions.sha256,
        },
        "references": [
            {"name": document.name, "sha256": document.sha256}
            for document in documents
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Bounded context injection (C06 rules 2-3) + prerequisite report
# ---------------------------------------------------------------------------


def skill_evidence(package: SkillPackage) -> tuple[ContextEvidence, ...]:
    """Convert a loaded package into shared-namespace context evidence.

    Kind ``doc`` with ``server_id=None`` (the explicitly shared namespace,
    C07 rule 1 — skills are server-independent knowledge under
    ``shared/``); ``source`` = ``skill/<name>[/<reference path>]``,
    ``source_revision`` = the file's sha256. Injection bounds, budget
    arithmetic, truncation notices and receipts stay owned by the
    reviewed assembly the caller feeds these into.
    """
    items = [
        ContextEvidence(
            kind=EvidenceKind.DOC,
            source=f"skill/{package.name}",
            source_revision=package.instructions.sha256,
            text=package.instructions.text,
        )
    ]
    items.extend(
        ContextEvidence(
            kind=EvidenceKind.DOC,
            source=f"skill/{package.name}/{document.name}",
            source_revision=document.sha256,
            text=document.text,
        )
        for document in package.references
    )
    return tuple(items)


def missing_prerequisites(
    package: SkillPackage, allowed: Collection[str]
) -> tuple[str, ...]:
    """Report declared prerequisites absent from the caller's allowed set.

    ``allowed`` is the run's effective capability surface
    (``ExecutionContext.allowed_capabilities``, C03 rule 2 — grants ∧
    registry, composed by the caller). Pure set arithmetic: this never
    mutates grants, policy, registry or dispatcher state; the return is
    sorted and duplicate-free for stable receipts. A skill with missing
    prerequisites is still *loaded* — the declared missing dependency is
    reported, never papered over and never self-served.
    """
    allowed_set: set[str]
    if isinstance(allowed, (str, bytes)) or not isinstance(allowed, Collection):
        raise SkillLoadError(
            SkillLoadErrorCode.ALLOWED_INVALID,
            "allowed must be a collection of capability id strings "
            "(not a single string)",
        )
    for item in allowed:
        if not isinstance(item, str):
            raise SkillLoadError(
                SkillLoadErrorCode.ALLOWED_INVALID,
                "allowed must be a collection of capability id strings",
            )
    allowed_set = set(allowed)
    return tuple(
        sorted(set(package.requires_capabilities).difference(allowed_set))
    )


# ---------------------------------------------------------------------------
# Declarative CLI (no execution surface)
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mai.skills.load",
        description=(
            "Declarative skill package inspector: loads one skill package "
            "from <data-root>/shared/skills/<skill> and prints its "
            "provenance and declared-prerequisite report as one JSON line. "
            "Read-only; no execution surface."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect = subparsers.add_parser(
        "inspect", help="load one skill package and print its JSON report"
    )
    inspect.add_argument("data_root", help="MAI data root path")
    inspect.add_argument("skill", help="skill package name (lowercase slug)")
    inspect.add_argument(
        "--allowed",
        default="",
        help="comma-separated allowed capability ids to diff the declared "
        "prerequisites against (default: empty — everything declared is "
        "reported missing)",
    )
    return parser


def _rejection_view(error: SkillLoadError) -> dict[str, object]:
    return {"error": {"code": error.code.value, "detail": error.detail}}


def _package_view(
    package: SkillPackage, allowed: Collection[str]
) -> dict[str, object]:
    missing = missing_prerequisites(package, allowed)
    return {
        "skill": package.name,
        "title": package.manifest.title,
        "description": package.manifest.description,
        "package_sha256": package.package_sha256,
        "total_bytes": package.total_bytes,
        "manifest_sha256": package.manifest.sha256,
        "requires_capabilities": list(package.requires_capabilities),
        "allowed": sorted(set(allowed)),
        "missing_capabilities": list(missing),
        "instructions": {
            "name": package.instructions.name,
            "sha256": package.instructions.sha256,
            "size_bytes": package.instructions.size_bytes,
        },
        "references": [
            {
                "name": document.name,
                "sha256": document.sha256,
                "size_bytes": document.size_bytes,
            }
            for document in package.references
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: 0 success, 1 structured rejection, 2 usage/IO."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        package = load_skill(args.data_root, args.skill)
    except SkillLoadError as error:
        print(json.dumps(_rejection_view(error)))
        return 1
    except OSError:
        print(json.dumps({"error": {"code": "io_error", "detail": "data root "
                          "could not be accessed"}}))
        return 2
    allowed = [
        item.strip() for item in args.allowed.split(",") if item.strip()
    ]
    print(json.dumps(_package_view(package, allowed)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

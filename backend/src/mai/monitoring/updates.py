"""Bounded update checks over SSH + declared upstream sources (card 10-update-check, C09 + C10).

Deterministic ``update`` collector: detect available application updates
WITHOUT applying anything. One fixed, read-only, self-bounding probe command
gathers, from a pinned-identity SSH target (the ``04-ssh-connect`` transport:
frozen option surface, host-key pinning, no credential in argv):

- **OS package update candidates** — read-only queries only: ``pacman -Quq``
  (query the installed set against the synced databases) and
  ``apt-get -s dist-upgrade`` (the simulate mode; it plans, it never
  installs). Neither mutates package state; a source that is absent or fails
  is UNKNOWN, never zero pending updates.
- **Managed application image pins** — the frozen planner scheme
  (``recipes.TARGET_APPS_ROOT``): ``/var/lib/mai/apps/<application_id>/
  compose.yaml`` exists only for MAI-managed (adopted + installed)
  applications, so the pinned ``image: <repo>@sha256:<digest>`` line read
  there IS the installed pin (the same ground truth ``08-update`` verifies
  its baseline recipes against).

For every parsed pin the collector then makes BOUNDED network calls against
the upstream source the pin itself declares (the registry host + repository
of the image reference — OCI Distribution API v2): at most
:data:`MAX_REGISTRY_CALLS_PER_APP` GET requests, byte-capped responses, a
hard timeout, no credentials, no redirect following. The newest
version-parseable tag's manifest digest is compared to the installed pin
digest.

- **Acceptance 1** — the comparison carries provenance: every measured app
  row records which registry/repository/tag/digest answered, bounded to the
  schema's text columns (``0007_observations``).
- **Acceptance 2** — the check is a NOTICE: the only write paths are the
  0007 observation tables. There is no install/update/deployment call in
  this module (a structural property the tests audit against the frozen
  forbidden-verb list). An ambiguous or unavailable upstream (no
  version-parseable tags, conflicting answers, HTTP error, timeout,
  oversized body) is reported as UNKNOWN — a metric-free
  ``quality='unknown'`` observation row with the finding deliberately left
  untouched — never as "no updates".

Findings are collector-owned (kinds are collector references, not a frozen
vocabulary — 10-observation-schema): ``update.available`` per application
(the contract's named example kind) and ``update.os_packages`` for the OS
package set, deduped on ``(server_id, kind, dedup_key)`` so repeated polls
collapse onto one finding per condition. Severity stays ``info``: an
available update is a notice for a human decision, never an automatic
deployment.

The probe runs synchronously (P04 precedent: system ``ssh`` client via
:func:`subprocess.run`), upstream checks and persistence are async on the
single application engine (:func:`mai.database.create_app_engine`). A
collection attempt never raises for target unavailability — it returns the
unknown path — but controller misconfiguration (unresolvable key reference,
missing enrollment) still raises, failing closed.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.applications.recipes import TARGET_APPS_ROOT
from mai.contracts.observations import (
    SUMMARY_MAX_CHARS,
    ObservationMethod,
    ObservationQuality,
)
from mai.contracts.server import SshIdentity
from mai.execution.ssh import (
    SshConnectError,
    SshErrorCode,
    ssh_arguments,
)

__all__ = [
    "FRESH_HORIZON",
    "MAX_APPS_LISTED",
    "MAX_MANIFEST_BODY_BYTES",
    "MAX_OS_UPDATES_LISTED",
    "MAX_PROBE_OUTPUT_BYTES",
    "MAX_REGISTRY_CALLS_PER_APP",
    "MAX_TAGS_BODY_BYTES",
    "PROBE_COMMAND",
    "PROBE_TIMEOUT_SECONDS",
    "RETENTION_HORIZON",
    "SOURCE",
    "UPSTREAM_TIMEOUT_SECONDS",
    "UPSTREAM_USER_AGENT",
    "AppCheck",
    "AppPin",
    "CollectionResult",
    "ImagePin",
    "OsUpdates",
    "UpdateSample",
    "check_application_pin",
    "collect",
    "findings_for_sample",
    "parse_image_pin",
    "parse_probe_output",
    "probe_updates",
    "record_sample",
    "record_unavailable",
    "registry_endpoint",
]

#: Collector identity persisted on every row of this collector.
SOURCE = "update"

#: Wall-clock budget for one probe round trip (connect timeout is the frozen
#: P04 :data:`CONNECT_TIMEOUT_SECONDS`).
PROBE_TIMEOUT_SECONDS = 30.0

#: Hard cap on captured probe output. The probe emits at most a few hundred
#: bytes by construction; anything larger is a hostile/broken target.
MAX_PROBE_OUTPUT_BYTES = 32 * 1024

#: Collector-declared freshness horizon (C10 rule 4: consumers derive
#: staleness against their own clock via ``derive_freshness``).
FRESH_HORIZON = timedelta(minutes=15)

#: Retention bound stamped on every row this collector writes.
RETENTION_HORIZON = timedelta(days=7)

#: Bounded listing of managed applications per probe (enforced target-side
#: by the probe loop's counter; the controller re-checks).
MAX_APPS_LISTED = 16

#: Bounded listing of OS package names per source (``head`` target-side).
MAX_OS_UPDATES_LISTED = 50

#: Hard structural cap on upstream registry calls per application check:
#: exactly one ``tags/list`` plus at most one ``manifests/<tag>``.
MAX_REGISTRY_CALLS_PER_APP = 2

#: Upstream response body budgets (the transfer itself is bounded by an
#: early abort at cap+1 bytes, not just the retained bytes).
MAX_TAGS_BODY_BYTES = 64 * 1024
MAX_MANIFEST_BODY_BYTES = 64 * 1024

#: Bounded upstream round trip; a hung registry is UNKNOWN, never a blocker.
UPSTREAM_TIMEOUT_SECONDS = 10.0

#: Descriptive non-identifying UA: no caller headers, no credentials, no
#: host identity beyond the collector label.
UPSTREAM_USER_AGENT = "mai-update-check/1.0 (monitoring collector)"

#: Accept set for the manifest GET (OCI + Docker v2 manifest/index types).
_MANIFEST_ACCEPT = (
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json, "
    "application/vnd.docker.distribution.manifest.v2+json"
)

_TS_FORMAT = "%Y-%m-%dT%H:%M:%S"

# The frozen recipe pin shape (recipes._IMAGE_RE) mirrored locally with a
# comment instead of a private cross-import (the packages convention): a tag
# is not a pin, so `name:tag` references cannot declare an upstream source
# and are dropped, never fetched.
_PIN_RE = re.compile(
    r"^(?P<ref>[a-z0-9][a-z0-9.:_/-]*)@sha256:(?P<digest>[0-9a-f]{64})$"
)

# Version-parseable release tags only: optional `v` prefix + 1–4 numeric
# dot-separated components (v2, 1.9, v2.31.2). Anything else (latest,
# main, sha-abc, 2024.01.17-beta) is not orderable and cannot claim to be
# the newest release — that way lies the ambiguity honesty rule.
_VERSION_TAG_RE = re.compile(r"^v?([0-9]{1,5}(?:\.[0-9]{1,5}){0,3})$")

#: Canonical lowercase UUID (the frozen application identity shape); a
#: directory under TARGET_APPS_ROOT that is not one is not MAI-managed.
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Loopback literals may carry plain-HTTP fixture registries (the C10
#: disposable-target rule is loopback-only); every other declared registry
#: host is https.
_LOOPBACK_HOST_RE = re.compile(r"^(127\.[0-9.]{1,15}|\[::1\]|::1|localhost)(:|$)")

#: Docker Hub's declared API endpoint (the image reference namespace is
#: docker.io; the Distribution API lives on registry-1.docker.io).
_DOCKER_HUB_API = "https://registry-1.docker.io"


# ---------------------------------------------------------------------------
# The one remote command: fixed, read-only, self-bounding, POSIX-sh.
# ---------------------------------------------------------------------------

#: Every line is failure-tolerant so one absent source degrades rather than
#: aborts the sample. Read-only audit trail (tests pin this): the only
#: package invocations are ``pacman -Quq`` (query) and ``apt-get -s
#: dist-upgrade`` (simulate); there is no install/upgrade/pull/compose path,
#: no redirection into target files, no state mutation of any kind. The
#: status/count marker protocol lets the controller distinguish "source ran
#: and found zero" from "source absent or failed" — absent/failed is UNKNOWN.
PROBE_COMMAND = f"""
ts=$(date -u +{_TS_FORMAT!r})
echo "ts=$ts"
if command -v pacman >/dev/null 2>&1; then
  pacman_out=$(pacman -Quq 2>/dev/null)
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "os_pacman_status=ok"
    echo "os_pacman_count=$(printf '%s\\n' "$pacman_out" | sed '/^$/d' | wc -l | tr -d ' ')"
    printf '%s\\n' "$pacman_out" | sed '/^$/d' | head -n {MAX_OS_UPDATES_LISTED} | sed 's/^/os_pacman=/'
  else
    echo "os_pacman_status=error_rc_$rc"
  fi
else
  echo "os_pacman_status=absent"
fi
if command -v apt-get >/dev/null 2>&1; then
  apt_out=$(apt-get -s dist-upgrade 2>/dev/null)
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "os_apt_status=ok"
    echo "os_apt_count=$(printf '%s\\n' "$apt_out" | grep -c '^Inst ' || true)"
    printf '%s\\n' "$apt_out" | grep '^Inst ' | head -n {MAX_OS_UPDATES_LISTED} \
      | sed 's/^Inst \\([^ ]\\{{1,64\\}}\\) .*/os_apt=\\1/'
  else
    echo "os_apt_status=error_rc_$rc"
  fi
else
  echo "os_apt_status=absent"
fi
count=0
for d in {TARGET_APPS_ROOT}/*/; do
  [ "$count" -lt {MAX_APPS_LISTED} ] || break
  f="${{d}}compose.yaml"
  [ -f "$f" ] || continue
  img=$(sed -n 's/^[[:space:]]*image:[[:space:]]*\\([^[:space:]]\\{{1,512\\}}\\).*$/\\1/p' "$f" | head -n 1)
  [ -n "$img" ] || continue
  app=${{d#{TARGET_APPS_ROOT}/}}
  app=${{app%/}}
  echo "app=$app image=$img"
  count=$((count + 1))
done
"""


# ---------------------------------------------------------------------------
# Sample shape (pure; what one probe round + upstream checks observed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OsUpdates:
    """One authoritative OS package source's answer (never both at once)."""

    source: Literal["pacman", "apt"]
    count: int
    names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AppPin:
    """One managed application's installed pin, read from the target."""

    application_id: uuid.UUID
    image_ref: str


@dataclass(frozen=True, slots=True)
class ImagePin:
    """One validated image reference split into its declared upstream."""

    registry_host: str
    repository: str
    digest: str


@dataclass(frozen=True, slots=True)
class AppCheck:
    """One upstream comparison for one managed pin (bounded, provenance-carrying).

    ``status``: ``up_to_date`` | ``update_available`` | ``unavailable`` |
    ``ambiguous`` | ``invalid_pin``. Only the first two are measured facts;
    the rest are honest unknowns. ``detail`` is a stable value-free code.
    ``status`` is always set by :func:`check_application_pin` — the empty
    default exists only for the shared base record it derives from.
    """

    application_id: uuid.UUID
    status: str = ""
    repository: str | None = None
    registry_host: str | None = None
    endpoint: str | None = None
    upstream_tag: str | None = None
    upstream_digest: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class UpdateSample:
    """One bounded update-check round."""

    observed_at: datetime
    observed_at_source: str
    quality: ObservationQuality
    os_updates: OsUpdates | None
    pins: tuple[AppPin, ...]
    checks: tuple[AppCheck, ...] = ()


def parse_image_pin(image_ref: str) -> ImagePin | None:
    """Split one pinned image reference into its declared upstream source.

    Returns ``None`` for anything that is not a digest-pinned reference with
    an EXPLICIT registry host (the Docker reference rule: the pre-slash
    segment counts as a host only when it carries a dot, a port, or is
    ``localhost`` — `library/nginx` is a host-less docker.io path, not a
    source). A tag or a host-less reference declares no checkable source.
    """
    match = _PIN_RE.fullmatch(image_ref.strip())
    if match is None:
        return None
    host, separator, repository = match.group("ref").partition("/")
    if not separator:
        return None
    if "." not in host and ":" not in host and host != "localhost":
        return None
    return ImagePin(
        registry_host=host,
        repository=repository,
        digest=f"sha256:{match.group('digest')}",
    )


def registry_endpoint(registry_host: str) -> str:
    """The declared Distribution API base URL for one registry host.

    ``docker.io`` namespaces the registry-1.docker.io API; loopback literals
    speak plain HTTP (fixture registries, the C10 disposable-target rule);
    everything else is https. The caller controls what hosts appear here:
    they come from adopted pins, never from probe output of unenrolled
    targets.
    """
    host = registry_host.strip()
    if host == "docker.io":
        return _DOCKER_HUB_API
    if _LOOPBACK_HOST_RE.match(host):
        return f"http://{host}"
    return f"https://{host}"


def _version_key(tag: str) -> tuple[int, ...] | None:
    match = _VERSION_TAG_RE.fullmatch(tag.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _pad(key: tuple[int, ...], width: int) -> tuple[int, ...]:
    return key + (0,) * (width - len(key))


def _newest_version_tag(tags: list[str]) -> str | None:
    """Deterministic newest release tag, or None when the answer is ambiguous.

    Pure numeric-component ordering (padded); a version TIE between two
    distinct tags (``v1.0`` vs ``1.0``) has no single "newest release"
    answer and is reported ambiguous rather than guessed.
    """
    keyed = [(tag, _version_key(tag)) for tag in tags]
    versions = [(tag, key) for tag, key in keyed if key is not None]
    if not versions:
        return None
    width = max(len(key) for _, key in versions)
    padded = [(tag, _pad(key, width)) for tag, key in versions]
    top = max(padded, key=lambda item: item[1])[1]
    winners = [tag for tag, key in padded if key == top]
    if len(winners) != 1:
        return None
    return winners[0]


# ---------------------------------------------------------------------------
# Probe output parsing (pure; bounded target output → honest sample)
# ---------------------------------------------------------------------------


def _int_field(raw: str | None) -> int | None:
    if raw is None or not raw.isdigit():
        return None
    return int(raw)


def _source_counts(
    values: dict[str, str],
    names: dict[str, list[str]],
    prefix: str,
    name: str,
) -> OsUpdates | None:
    """One package source's answer: ok marker + exact count + bounded names."""
    if values.get(f"{prefix}_status") != "ok":
        return None
    count = _int_field(values.get(f"{prefix}_count"))
    if count is None or count < 0:
        return None
    listed = tuple(names.get(prefix, ()))[:MAX_OS_UPDATES_LISTED]
    return OsUpdates(source=name, count=count, names=listed)


def parse_probe_output(output: str, *, controller_now: datetime) -> UpdateSample:
    """Parse fixed probe output into an :class:`UpdateSample` (pure).

    Absent/failed sources are UNKNOWN, never zero: the ``status=ok`` marker
    (printed only after a zero-exit source query) is what makes a zero count
    a fact. Package names arrive as repeated ``os_<source>=<name>`` lines
    and collect in first-seen order. Application pins are validated against
    the frozen UUID + pin shapes; anything else under the managed root is
    not MAI-managed and is dropped.
    """
    values: dict[str, str] = {}
    names: dict[str, list[str]] = {"os_pacman": [], "os_apt": []}
    pins: list[AppPin] = []
    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        if line.startswith("app=") and " image=" in line:
            app_raw, image_ref = line[4:].split(" image=", 1)
            if _UUID_RE.fullmatch(app_raw) and len(pins) < MAX_APPS_LISTED:
                application_id = uuid.UUID(app_raw)
                if all(pin.application_id != application_id for pin in pins):
                    pins.append(AppPin(application_id, image_ref))
            continue
        key, sep, value = line.partition("=")
        if not sep or not key:
            continue
        if key in names:
            if len(names[key]) < MAX_OS_UPDATES_LISTED:
                names[key].append(value[:64])
        elif key not in values:
            values[key] = value[:512]

    marker = values.get("ts")
    observed_at: datetime | None = None
    if marker:
        try:
            observed_at = datetime.strptime(marker, _TS_FORMAT).replace(tzinfo=UTC)
        except ValueError:
            observed_at = None

    os_updates = _source_counts(values, names, "os_pacman", "pacman")
    if os_updates is None:
        os_updates = _source_counts(values, names, "os_apt", "apt")

    has_os = os_updates is not None
    if has_os or pins:
        quality = ObservationQuality.MEASURED if has_os else ObservationQuality.DEGRADED
    else:
        quality = ObservationQuality.UNKNOWN

    return UpdateSample(
        observed_at=observed_at or controller_now,
        observed_at_source="target" if observed_at is not None else "controller",
        quality=quality,
        os_updates=os_updates,
        pins=tuple(pins),
    )


# ---------------------------------------------------------------------------
# Probe execution (P04 transport: frozen option surface, pinned host key)
# ---------------------------------------------------------------------------


def _sanitize_stderr(stderr: str) -> str:
    """Bounded single-line stderr tail for error details (value-free)."""
    flat = " ".join(stderr.split())
    return flat[-200:] if flat else "<no stderr>"


def _map_probe_failure(rc: int, stderr: str, endpoint: str) -> SshConnectError:
    """Map a non-zero ssh exit to the stable P04 rejection vocabulary.

    Local mirror of the measured-probe classification (the transport owns
    the canonical table): only the coarse class matters here — the collector
    maps any of these to the unknown path, the codes exist so evidence and
    logs name the same rejection reason the transport suite pins.
    """
    flat = " ".join(stderr.split())
    if (
        "Host key verification failed" in flat
        or "HOST IDENTIFICATION HAS CHANGED" in flat
    ):
        return SshConnectError(
            SshErrorCode.HOST_KEY_MISMATCH,
            f"{endpoint} refused by host key pinning (stderr: {_sanitize_stderr(stderr)})",
        )
    if "Permission denied" in flat or "bad permissions" in flat.lower():
        return SshConnectError(
            SshErrorCode.AUTH_FAILED,
            f"{endpoint} rejected the pinned identity key (stderr: {_sanitize_stderr(stderr)})",
        )
    if any(
        marker in flat
        for marker in (
            "Could not resolve hostname",
            "Connection refused",
            "Connection timed out",
            "Connection reset",
            "Network is unreachable",
        )
    ):
        return SshConnectError(
            SshErrorCode.UNREACHABLE,
            f"{endpoint} unreachable (stderr: {_sanitize_stderr(stderr)})",
        )
    return SshConnectError(
        SshErrorCode.SSH_FAILED,
        f"{endpoint} ssh exited rc={rc} (stderr: {_sanitize_stderr(stderr)})",
    )


def probe_updates(
    identity: SshIdentity,
    *,
    key_path: Path,
    known_hosts_path: Path,
    controller_now: datetime,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> UpdateSample:
    """Run the fixed update probe over the pinned-identity transport.

    Preconditions mirror :func:`mai.execution.ssh.probe_environment`: the key
    path comes from ``resolve_identity_key`` and the managed known-hosts file
    from ``pin_host_key`` (enrollment). Raises :class:`SshConnectError`
    (stable codes) for any transport failure; a target that answers is
    parsed into an :class:`UpdateSample` under the hard output budget.
    """
    if not known_hosts_path.is_file():
        raise SshConnectError(
            SshErrorCode.INVALID_CONFIGURATION,
            f"managed known-hosts file {known_hosts_path} does not exist; "
            "enroll the pinned host key explicitly before collecting",
        )
    command = ssh_arguments(
        identity,
        key_path=key_path,
        known_hosts_path=known_hosts_path,
        remote_command=PROBE_COMMAND,
    )
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise SshConnectError(
            SshErrorCode.TIMEOUT, f"update probe timed out after {timeout}s"
        ) from None
    if len(proc.stdout.encode("utf-8")) > MAX_PROBE_OUTPUT_BYTES:
        raise SshConnectError(
            SshErrorCode.BOUNDED_OUTPUT_EXCEEDED,
            f"update probe output exceeded the {MAX_PROBE_OUTPUT_BYTES}-byte budget",
        )
    if proc.returncode != 0:
        raise _map_probe_failure(
            proc.returncode, proc.stderr, f"{identity.host}:{identity.port}"
        )
    return parse_probe_output(proc.stdout, controller_now=controller_now)


# ---------------------------------------------------------------------------
# Upstream checks (bounded OCI Distribution API v2 GETs, no credentials)
# ---------------------------------------------------------------------------


async def _bounded_read(response: object, cap: int) -> bytes:
    """Stream the body under an early abort at cap+1 bytes.

    The transfer itself is bounded (like the page reader): a hostile or
    broken registry cannot stream unbounded bytes into the controller.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():  # type: ignore[attr-defined]
        chunks.append(chunk)
        total += len(chunk)
        if total > cap:
            raise _UpstreamTooLarge(cap)
    return b"".join(chunks)


class _UpstreamTooLarge(Exception):
    """The upstream response body exceeded its declared budget."""


async def _get_bounded(
    client: object,
    url: str,
    *,
    cap: int,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, object]:
    """One bounded GET: (status, body, response-headers-view)."""

    async with client.stream("GET", url, headers=headers or {}) as response:
        status = response.status_code
        if status != 200:
            return status, b"", response.headers
        body = await _bounded_read(response, cap)
        return status, body, response.headers


def _classify_upstream_exception(exc: BaseException) -> str:
    """Map one transport exception to a stable coarse status code."""
    import httpx2

    if isinstance(exc, httpx2.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx2.TransportError):
        return "unreachable"
    return "error"


def _resolve_upstream_digest(body: bytes, headers: object) -> str | None:
    """The manifest digest of one release tag (header first, hash fallback).

    Registries compute ``Docker-Content-Digest`` for the returned manifest
    document; when the header is absent (minimal/fake registries) the
    response body's sha256 IS the content digest by definition.
    """
    value = getattr(headers, "get", lambda _k, _d=None: None)("Docker-Content-Digest")
    if isinstance(value, str) and _DIGEST_RE.fullmatch(value.strip()):
        return value.strip()
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


async def check_application_pin(
    client: object,
    pin: AppPin,
) -> AppCheck:
    """Compare one installed pin against its declared upstream source.

    At most :data:`MAX_REGISTRY_CALLS_PER_APP` GET requests, byte-capped,
    timeout-bounded, no credentials, no redirects. Any ambiguity (no
    version-parseable tags, a version tie, an unexpected answer shape) or
    unavailability (HTTP error, transport failure, oversized body) yields an
    honest unknown status — NEVER an "up to date" claim.
    """
    import httpx2

    parsed = parse_image_pin(pin.image_ref)
    if parsed is None:
        return AppCheck(
            application_id=pin.application_id,
            status="invalid_pin",
            detail="not a digest-pinned reference with an explicit registry host",
        )
    endpoint = registry_endpoint(parsed.registry_host)
    base = AppCheck(
        application_id=pin.application_id,
        repository=parsed.repository,
        registry_host=parsed.registry_host,
        endpoint=endpoint,
    )

    tags_url = f"{endpoint}/v2/{parsed.repository}/tags/list"
    try:
        status, body, _headers = await _get_bounded(
            client, tags_url, cap=MAX_TAGS_BODY_BYTES
        )
    except _UpstreamTooLarge:
        return replace(base, status="unavailable", detail="tags body over budget")
    except httpx2.HTTPError as exc:
        return replace(
            base, status="unavailable", detail=_classify_upstream_exception(exc)
        )
    if status != 200:
        return replace(base, status="unavailable", detail=f"tags http_{status}")
    try:
        document = json.loads(body)
        tags = document["tags"]
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ValueError("tags must be a list of strings")
    except ValueError, KeyError, TypeError:
        return replace(base, status="unavailable", detail="tags body unparsable")

    tag = _newest_version_tag(tags)
    if tag is None:
        return replace(base, status="ambiguous", detail="no single newest version tag")

    manifest_url = f"{endpoint}/v2/{parsed.repository}/manifests/{tag}"
    try:
        status, body, headers = await _get_bounded(
            client,
            manifest_url,
            cap=MAX_MANIFEST_BODY_BYTES,
            headers={"Accept": _MANIFEST_ACCEPT},
        )
    except _UpstreamTooLarge:
        return replace(base, status="unavailable", detail="manifest body over budget")
    except httpx2.HTTPError as exc:
        return replace(
            base, status="unavailable", detail=_classify_upstream_exception(exc)
        )
    if status != 200:
        return replace(base, status="unavailable", detail=f"manifest http_{status}")
    upstream_digest = _resolve_upstream_digest(body, headers)
    if upstream_digest is None:  # pragma: no cover - sha256 always matches
        return replace(
            base, status="unavailable", detail="manifest digest unresolvable"
        )

    if upstream_digest == parsed.digest:
        return replace(
            base,
            status="up_to_date",
            upstream_tag=tag,
            upstream_digest=upstream_digest,
        )
    return replace(
        base,
        status="update_available",
        upstream_tag=tag,
        upstream_digest=upstream_digest,
    )


# ---------------------------------------------------------------------------
# Findings (collector-owned kinds; dedup by (server_id, kind, dedup_key))
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FindingDraft:
    """One collector-owned finding evaluation for a sample."""

    kind: str
    dedup_key: str
    severity: str
    summary: str
    state: Literal["active", "clear", "no_data"]


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _short_digest(digest: str) -> str:
    return digest[:19] + "…" if len(digest) > 22 else digest


def findings_for_sample(sample: UpdateSample) -> tuple[FindingDraft, ...]:
    """Evaluate the collector's notice policy against one sample (pure).

    Unknown/unavailable checks are ``no_data``: they never create, update,
    close or reopen a finding — "upstream unavailable" is never reported as
    "no updates", and stale data is never re-evaluated.
    """
    drafts: list[FindingDraft] = []

    os_updates = sample.os_updates
    if os_updates is None:
        drafts.append(
            FindingDraft(
                kind="update.os_packages",
                dedup_key="os",
                severity="info",
                summary="",
                state="no_data",
            )
        )
    else:
        active = os_updates.count > 0
        names = ", ".join(os_updates.names[:5]) + (
            "…" if len(os_updates.names) > 5 else ""
        )
        drafts.append(
            FindingDraft(
                kind="update.os_packages",
                dedup_key="os",
                severity="info",
                summary=_truncate(
                    (
                        f"{os_updates.count} OS package updates pending"
                        if active
                        else "no OS package updates pending"
                    )
                    + f" (source: {os_updates.source}"
                    + (f": {names}" if active and names else "")
                    + ")",
                    SUMMARY_MAX_CHARS,
                ),
                state="active" if active else "clear",
            )
        )

    checks_by_id = {check.application_id: check for check in sample.checks}
    for pin in sample.pins:
        check = checks_by_id.get(pin.application_id)
        if check is None or check.status in ("unavailable", "ambiguous", "invalid_pin"):
            drafts.append(
                FindingDraft(
                    kind="update.available",
                    dedup_key=str(pin.application_id),
                    severity="info",
                    summary="",
                    state="no_data",
                )
            )
            continue
        active = check.status == "update_available"
        if active:
            parsed = parse_image_pin(pin.image_ref)
            installed = parsed.digest if parsed is not None else "unknown"
            summary = (
                f"update available for {check.repository}: installed "
                f"{_short_digest(installed)}, upstream {check.upstream_tag} "
                f"{_short_digest(check.upstream_digest or 'unknown')}"
            )
        else:
            summary = (
                f"{check.repository} matches upstream release {check.upstream_tag}"
            )
        drafts.append(
            FindingDraft(
                kind="update.available",
                dedup_key=str(pin.application_id),
                severity="info",
                summary=_truncate(summary, SUMMARY_MAX_CHARS),
                state="active" if active else "clear",
            )
        )
    return tuple(drafts)


# ---------------------------------------------------------------------------
# Persistence into the 0007 observation schema
# ---------------------------------------------------------------------------

_INSERT_OBSERVATION = sa.text(
    """
    INSERT INTO server_observations
        (observation_id, server_id, source, method, quality, metric, value,
         observed_at, collected_at, fresh_until, retain_until)
    VALUES
        (:observation_id, :server_id, :source, :method, :quality, :metric,
         :value, :observed_at, :collected_at, :fresh_until, :retain_until)
    """
)

_UPSERT_FINDING = sa.text(
    """
    INSERT INTO observation_findings
        (finding_id, server_id, kind, dedup_key, severity, summary, detail,
         observation_id, opened_at, updated_at, resolved_at, retain_until)
    VALUES
        (:finding_id, :server_id, :kind, :dedup_key, :severity, :summary,
         :detail, :observation_id, :collected_at, :collected_at, NULL,
         :retain_until)
    ON CONFLICT (server_id, kind, dedup_key) DO UPDATE SET
        severity = EXCLUDED.severity,
        summary = EXCLUDED.summary,
        detail = EXCLUDED.detail,
        observation_id = EXCLUDED.observation_id,
        updated_at = EXCLUDED.updated_at,
        resolved_at = NULL
    """
)

_CLOSE_FINDING = sa.text(
    """
    UPDATE observation_findings
       SET resolved_at = :resolved_at, updated_at = :resolved_at
     WHERE server_id = :server_id AND kind = :kind
       AND dedup_key = :dedup_key AND resolved_at IS NULL
    """
)

_UNAVAILABLE_FINDING_KINDS = ("unavailable", "ambiguous", "invalid_pin")


def _metric_rows(
    server_id: uuid.UUID,
    sample: UpdateSample,
    collected_at: datetime,
    fresh_until: datetime,
    retain_until: datetime,
) -> list[dict[str, object]]:
    """Bounded per-fact rows; every value is a short normalized rendering."""

    def row(metric: str, value: str, method: ObservationMethod) -> dict[str, object]:
        return {
            "observation_id": uuid.uuid4(),
            "server_id": server_id,
            "source": SOURCE,
            "method": method.value,
            "quality": sample.quality.value,
            "metric": metric,
            "value": value,
            "observed_at": sample.observed_at,
            "collected_at": collected_at,
            "fresh_until": fresh_until,
            "retain_until": retain_until,
        }

    rows: list[dict[str, object]] = []
    os_updates = sample.os_updates
    if os_updates is not None:
        rows.append(
            row("update.os.count", str(os_updates.count), ObservationMethod.COMMAND)
        )
        rows.append(
            row(
                "update.os.source",
                os_updates.source,
                ObservationMethod.COMMAND,
            )
        )
    checks_by_id = {check.application_id: check for check in sample.checks}
    for pin in sample.pins:
        check = checks_by_id.get(pin.application_id)
        if check is None or check.status in _UNAVAILABLE_FINDING_KINDS:
            continue  # honest unknown: the metric-free row below carries it
        parsed = parse_image_pin(pin.image_ref)
        installed = parsed.digest if parsed is not None else "unknown"
        provenance = (
            f"app={pin.application_id} repo={check.repository} "
            f"registry={check.registry_host} upstream_tag={check.upstream_tag} "
            f"upstream_digest={check.upstream_digest} "
            f"installed_digest={installed} status={check.status}"
        )
        rows.append(
            row(
                "update.app",
                _truncate(provenance, 512),
                ObservationMethod.DERIVED,
            )
        )
    return rows


async def record_sample(
    sessions: async_sessionmaker[AsyncSession],
    server_id: uuid.UUID,
    sample: UpdateSample,
    *,
    controller_now: datetime,
) -> int:
    """Persist one sample as bounded observation rows + notice findings.

    One row per measured fact (source/method/quality/timestamps per C10 rule
    4; the app rows carry the upstream provenance), one metric-free unknown
    row per UNKNOWN upstream check, then the notice set: active drafts
    upsert onto ``(server_id, kind, dedup_key)``, cleared drafts close open
    rows, ``no_data`` drafts do nothing. Returns the number of observation
    rows written (findings excluded).
    """
    if controller_now.tzinfo is None:
        raise ValueError("controller_now must be timezone-aware")
    fresh_until = sample.observed_at + FRESH_HORIZON
    retain_until = controller_now + RETENTION_HORIZON
    rows = _metric_rows(server_id, sample, controller_now, fresh_until, retain_until)
    unknown_rows = [
        {
            "observation_id": uuid.uuid4(),
            "server_id": server_id,
            "source": SOURCE,
            "method": ObservationMethod.DERIVED.value,
            "quality": ObservationQuality.UNKNOWN.value,
            "metric": None,
            "value": None,
            "observed_at": None,
            "collected_at": controller_now,
            "fresh_until": None,
            "retain_until": retain_until,
        }
        for check in sample.checks
        if check.status in _UNAVAILABLE_FINDING_KINDS
    ]
    if not rows and not unknown_rows and sample.quality is ObservationQuality.UNKNOWN:
        # The round ran but measured nothing (no package source answered,
        # no managed pin found): persist the honest nothing-measured row.
        unknown_rows.append(
            {
                "observation_id": uuid.uuid4(),
                "server_id": server_id,
                "source": SOURCE,
                "method": ObservationMethod.COMMAND.value,
                "quality": ObservationQuality.UNKNOWN.value,
                "metric": None,
                "value": None,
                "observed_at": None,
                "collected_at": controller_now,
                "fresh_until": None,
                "retain_until": retain_until,
            }
        )
    # Active findings anchor to a REAL inserted sample row (FK to
    # server_observations); with no metric rows no finding can be active.
    observation_id = rows[0]["observation_id"] if rows else uuid.uuid4()
    async with sessions() as session:
        for params in [*rows, *unknown_rows]:
            await session.execute(_INSERT_OBSERVATION, params)
        for draft in findings_for_sample(sample):
            if draft.state == "active":
                await session.execute(
                    _UPSERT_FINDING,
                    {
                        "finding_id": uuid.uuid4(),
                        "server_id": server_id,
                        "kind": draft.kind,
                        "dedup_key": draft.dedup_key,
                        "severity": draft.severity,
                        "summary": draft.summary,
                        "detail": None,  # bounded text column stays unused here
                        "observation_id": observation_id,
                        "collected_at": controller_now,
                        "retain_until": retain_until,
                    },
                )
            elif draft.state == "clear":
                await session.execute(
                    _CLOSE_FINDING,
                    {
                        "resolved_at": controller_now,
                        "server_id": server_id,
                        "kind": draft.kind,
                        "dedup_key": draft.dedup_key,
                    },
                )
        await session.commit()
    return len(rows) + len(unknown_rows)


async def record_unavailable(
    sessions: async_sessionmaker[AsyncSession],
    server_id: uuid.UUID,
    *,
    controller_now: datetime,
) -> int:
    """Persist the unknown path for an unreachable/refused target.

    Exactly ONE ``quality='unknown'`` row with NO metric/value/observed_at
    (the schema's ``ck_server_observations_unknown_has_no_measurement``
    forbids anything else) — never a zero-updates reading. Existing findings
    are deliberately left untouched: missing data neither re-evaluates nor
    closes a condition. Returns 1.
    """
    if controller_now.tzinfo is None:
        raise ValueError("controller_now must be timezone-aware")
    async with sessions() as session:
        await session.execute(
            _INSERT_OBSERVATION,
            {
                "observation_id": uuid.uuid4(),
                "server_id": server_id,
                "source": SOURCE,
                "method": ObservationMethod.COMMAND.value,
                "quality": ObservationQuality.UNKNOWN.value,
                "metric": None,
                "value": None,
                "observed_at": None,
                "collected_at": controller_now,
                "fresh_until": None,
                "retain_until": controller_now + RETENTION_HORIZON,
            },
        )
        await session.commit()
    return 1


@dataclass(frozen=True, slots=True)
class CollectionResult:
    """Outcome of one bounded collection attempt.

    ``quality`` is the sample's quality, or ``unknown`` when the target was
    unavailable. ``error`` carries only the stable rejection code; the
    per-application statuses (including every honest unknown) travel in
    ``apps`` for callers and logs — never an "up to date" claim for an
    unchecked application.
    """

    quality: ObservationQuality
    observations: int
    server_id: uuid.UUID
    apps: tuple[AppCheck, ...]
    error: str | None = None


async def collect(
    sessions: async_sessionmaker[AsyncSession],
    identity: SshIdentity,
    *,
    key_path: Path,
    known_hosts_path: Path,
    server_id: uuid.UUID,
    controller_now: datetime,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    transport: object | None = None,
) -> CollectionResult:
    """Probe a target, check declared upstreams, persist the round.

    Never raises for target-side failures (any :class:`SshConnectError`
    becomes a persisted unknown observation) nor for upstream failures
    (each becomes an honest unknown for that application); controller
    misconfiguration (unresolvable key reference, missing enrollment) still
    raises — failing closed is the configuration boundary's job, not a
    collection sample. ``transport`` is the httpx2 test seam; production
    uses the real network stack.
    """
    import httpx2

    try:
        sample = probe_updates(
            identity,
            key_path=key_path,
            known_hosts_path=known_hosts_path,
            controller_now=controller_now,
            timeout=timeout,
        )
    except SshConnectError as error:
        written = await record_unavailable(
            sessions, server_id, controller_now=controller_now
        )
        return CollectionResult(
            quality=ObservationQuality.UNKNOWN,
            observations=written,
            server_id=server_id,
            apps=(),
            error=error.code.value,
        )

    checks: list[AppCheck] = []
    if sample.pins:
        client = httpx2.AsyncClient(
            timeout=UPSTREAM_TIMEOUT_SECONDS,
            transport=transport,  # type: ignore[arg-type]
            follow_redirects=False,
            headers={"User-Agent": UPSTREAM_USER_AGENT},
        )
        try:
            for pin in sample.pins:
                checks.append(await check_application_pin(client, pin))
        finally:
            await client.aclose()
    downgraded = any(check.status in _UNAVAILABLE_FINDING_KINDS for check in checks)
    sample = replace(
        sample,
        checks=tuple(checks),
        quality=(
            ObservationQuality.DEGRADED
            if downgraded and sample.quality is ObservationQuality.MEASURED
            else sample.quality
        ),
    )
    written = await record_sample(
        sessions, server_id, sample, controller_now=controller_now
    )
    return CollectionResult(
        quality=sample.quality,
        observations=written,
        server_id=server_id,
        apps=tuple(checks),
    )

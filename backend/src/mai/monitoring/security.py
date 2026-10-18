"""Bounded security posture observations over SSH (card 10-security, C04 + C10).

Frozen-shape producer for the deterministic ``security`` collector, sibling of
:mod:`mai.monitoring.system` / :mod:`mai.monitoring.network`: one fixed,
read-only, self-bounding probe command gathers a small DECLARED baseline of
security-posture facts from a pinned-identity SSH target (the
``04-ssh-connect`` transport: exactly the frozen probe option surface, host-key
pinning, no credential in argv) and the results persist as bounded rows of the
``0007_observations`` schema:

- **Acceptance 1 ("baseline checks produce evidence/severity/remediation
  suggestions without automatic hardening")** — the probe only READS
  configuration and kernel state: the sshd effective configuration
  (``sshd -T``, two directives) and the sshd_config file fallback, the
  firewall ruleset size (``nft list ruleset`` / ``iptables -S``, line counts
  only, never rule content), the TCP LISTEN tables (``/proc/net/tcp{,6}``,
  same parse as the network collector), the sudoers NOPASSWD grant COUNT
  (never sudoers content) and whether automatic-update machinery is
  configured (file/timer/unit markers). Every finding carries a remediation
  SUGGESTION in the bounded ``detail`` column (the schema's stated purpose
  for that column); nothing is ever changed on the target — the probe has no
  write, reload or rule path at all.
- **Acceptance 2 ("no compliance/all-safe claims; unavailable facts explicit
  and secret content excluded")** — a check that cannot be answered is
  ``no_data`` / an absent metric, never a pass: an unreadable sudoers is a
  gap (not "no weak grants"), a firewall tool that refuses its listing is
  ``no_data`` (not "no rules"), sshd defaults resolved only by the
  config-file fallback stay explicit unknowns. There is no aggregate
  "compliant" or "all-safe" verdict anywhere in the output vocabulary —
  findings are per-check and three-valued (active/clear/no_data), and clear
  summaries restate the observed value, never a safety judgment. Secret
  content is excluded structurally: no metric can hold sudoers lines, rule
  texts or key material — only counts, enum words and ``address:port``
  renderings (all short and normalized, bounded by the schema CHECKs).

Threshold policy is collector-owned (finding ``kind`` values are collector
references, not a frozen vocabulary — 10-observation-schema): six
``security.*`` kinds with deterministic dedup keys, upserted with
``ON CONFLICT (server_id, kind, dedup_key)`` so repeated polls collapse onto
one finding per condition. Exposed-listener findings are keyed per listener
(``proto:address:port``); a COMPLETE listener table reconciles the open rows
against the observed set (removed listeners close, survivors refresh) while
an incomplete table leaves every row untouched — missing data never closes a
condition.

Exposed-listener judgment is this card's declared ownership (the network
collector deliberately persists the raw bindings without judging them). The
probe runs synchronously (same shape as the P04 ``probe_environment``:
system ``ssh`` client via :func:`subprocess.run`), persistence is async on
the single application engine (:func:`mai.database.create_app_engine`). A
collection attempt never raises for target unavailability — it returns the
unknown path — but controller misconfiguration (unresolvable key reference,
missing enrollment) still raises, failing closed.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mai.contracts.observations import (
    DETAIL_MAX_CHARS,
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
    "MAX_EXPOSED_FINDINGS",
    "MAX_PROBE_OUTPUT_BYTES",
    "MAX_RULE_LINES",
    "MAX_TCP_LISTENERS",
    "PROBE_COMMAND",
    "PROBE_TIMEOUT_SECONDS",
    "RETENTION_HORIZON",
    "SOURCE",
    "CollectionResult",
    "FindingDraft",
    "Listener",
    "SecuritySample",
    "collect",
    "findings_for_sample",
    "parse_probe_output",
    "probe_security",
    "record_sample",
    "record_unavailable",
]

#: Collector identity persisted on every row of this collector.
SOURCE = "security"

#: Wall-clock budget for one probe round trip (connect timeout is the frozen
#: P04 :data:`CONNECT_TIMEOUT_SECONDS`).
PROBE_TIMEOUT_SECONDS = 30.0

#: Hard cap on captured probe output. The probe emits at most a few hundred
#: lines by construction; anything larger is a hostile/broken target.
MAX_PROBE_OUTPUT_BYTES = 32 * 1024

#: Collector-declared freshness horizon (C10 rule 4: consumers derive
#: staleness against their own clock via ``derive_freshness``). Posture drifts
#: slowly; the horizon matches the sibling collectors so one consumer clock
#: policy covers the family.
FRESH_HORIZON = timedelta(minutes=15)

#: Retention bound stamped on every row this collector writes.
RETENTION_HORIZON = timedelta(days=7)

# Target-side bounds (enforced by ``head`` inside the probe; the parser
# re-applies them so a lying echo cannot inflate storage).
MAX_TCP_LISTENERS = 32
MAX_RULE_LINES = 4000
MAX_DIRECTIVE_LINES = 8
MAX_EXPOSED_FINDINGS = 32

#: The single remote command this module executes: fixed, read-only,
#: self-bounding, POSIX-sh (busybox targets parse the same; no ``awk``). It
#: reads declared posture facts only — sshd presence + effective directives
#: (``sshd -T``) + sshd_config fallback (first-match-per-directive, single
#: file, ``Include`` not expanded), firewall tooling presence and ruleset LINE
#: COUNTS (never rule content), the TCP LISTEN tables (state ``0A``), the
#: sudoers NOPASSWD grant COUNT (comment lines skipped, content never
#: emitted), and automatic-update markers (apt ``20auto-upgrades`` /
#: ``unattended-upgrade``, dnf ``dnf-automatic.timer``). Every listing is
#: failure-tolerant: its rc/readability marker distinguishes "source read"
#: (rc=0/ok) from "source absent/refused" (rc≠0/refused) — a gap is never
#: reported as a clean or safe value.
PROBE_COMMAND = (
    "printf 'ts=%s\\n' \"$(date -u '+%Y-%m-%dT%H:%M:%S' 2>/dev/null)\"; "
    'SSHD_BIN="$(command -v sshd 2>/dev/null)"; '
    'if [ -z "$SSHD_BIN" ] && [ -x /usr/sbin/sshd ]; then '
    "SSHD_BIN=/usr/sbin/sshd; fi; "
    'if [ -z "$SSHD_BIN" ] && [ -x /usr/local/sbin/sshd ]; then '
    "SSHD_BIN=/usr/local/sbin/sshd; fi; "
    'if [ -n "$SSHD_BIN" ]; then '
    "printf 'sshd_present=yes\\n'; "
    '"$SSHD_BIN" -T >/dev/null 2>&1; printf \'sshd_t_rc=%s\\n\' "$?"; '
    '"$SSHD_BIN" -T 2>/dev/null | grep -iE '
    "'^(permitrootlogin|passwordauthentication)([[:space:]]|$)' "
    "| sed 's/[[:space:]][[:space:]]*/ /g; s/^/eff=/' | head -n 8; "
    "else printf 'sshd_present=no\\n'; fi; "
    "if [ -r /etc/ssh/sshd_config ]; then "
    "printf 'cfg_rc=0\\n'; "
    "grep -iE '^[[:space:]]*(permitrootlogin|passwordauthentication)"
    "([[:space:]]|$)' /etc/ssh/sshd_config 2>/dev/null "
    "| sed 's/^[[:space:]][[:space:]]*//; "
    "s/[[:space:]][[:space:]]*/ /g; s/^/cfg=/' | head -n 8; "
    "else printf 'cfg_rc=1\\n'; fi; "
    "if command -v nft >/dev/null 2>&1; then "
    "printf 'nft_present=yes\\n'; "
    "nft list ruleset >/dev/null 2>&1; nft_rc=$?; "
    "printf 'nft_rc=%s\\n' \"$nft_rc\"; "
    'if [ "$nft_rc" -eq 0 ]; then '
    "nft list ruleset 2>/dev/null | head -n 4001 | grep -c . "
    "| sed 's/.*/nft_rules=&/'; fi; "
    "else printf 'nft_present=no\\n'; fi; "
    "if command -v iptables >/dev/null 2>&1; then "
    "printf 'ipt_present=yes\\n'; "
    "iptables -S >/dev/null 2>&1; ipt_rc=$?; "
    "printf 'ipt_rc=%s\\n' \"$ipt_rc\"; "
    'if [ "$ipt_rc" -eq 0 ]; then '
    "iptables -S 2>/dev/null | head -n 4001 | grep -c . "
    "| sed 's/.*/ipt_rules=&/'; fi; "
    "else printf 'ipt_present=no\\n'; fi; "
    "sed -n 's/^ *[0-9]\\{1,\\}: \\([0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\}\\) "
    "[0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\} 0A .*/tcp_l=\\1/p' /proc/net/tcp "
    ">/dev/null 2>&1; printf 'tcp_rc=%s\\n' \"$?\"; "
    "sed -n 's/^ *[0-9]\\{1,\\}: \\([0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\}\\) "
    "[0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\} 0A .*/tcp_l=\\1/p' /proc/net/tcp "
    "2>/dev/null | head -n 32; "
    "sed -n 's/^ *[0-9]\\{1,\\}: \\([0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\}\\) "
    "[0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\} 0A .*/tcp6_l=\\1/p' /proc/net/tcp6 "
    ">/dev/null 2>&1; printf 'tcp6_rc=%s\\n' \"$?\"; "
    "sed -n 's/^ *[0-9]\\{1,\\}: \\([0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\}\\) "
    "[0-9A-F]\\{1,32\\}:[0-9A-F]\\{4\\} 0A .*/tcp6_l=\\1/p' /proc/net/tcp6 "
    "2>/dev/null | head -n 32; "
    "if command -v sudo >/dev/null 2>&1; then "
    "printf 'sudo_present=yes\\n'; "
    "if [ -r /etc/sudoers ]; then "
    "printf 'sudo_read=ok\\n'; "
    "grep -hsE '^[[:space:]]*[^#[:space:]].*NOPASSWD' /etc/sudoers "
    "/etc/sudoers.d/* 2>/dev/null | head -n 4001 | grep -c . "
    "| sed 's/.*/sudo_nopasswd=&/'; "
    "else printf 'sudo_read=refused\\n'; fi; "
    "else printf 'sudo_present=no\\n'; fi; "
    "if command -v apt-get >/dev/null 2>&1; then "
    "printf 'pkg_apt=yes\\n'; "
    "if [ -e /etc/apt/apt.conf.d/20auto-upgrades ] "
    "|| command -v unattended-upgrade >/dev/null 2>&1; then "
    "printf 'apt_auto=yes\\n'; else printf 'apt_auto=no\\n'; fi; "
    "else printf 'pkg_apt=no\\n'; fi; "
    "if command -v dnf >/dev/null 2>&1; then "
    "printf 'pkg_dnf=yes\\n'; "
    "if systemctl is-enabled dnf-automatic.timer >/dev/null 2>&1 "
    "|| [ -e /etc/systemd/system/dnf-automatic.timer ]; then "
    "printf 'dnf_auto=yes\\n'; else printf 'dnf_auto=no\\n'; fi; "
    "else printf 'pkg_dnf=no\\n'; fi"
)

_TS_FORMAT = "%Y-%m-%dT%H:%M:%S"

# Allowed directive values (the effective-config vocabulary). Anything else
# is a data gap, not a renamed policy.
_PERMIT_ROOT_VOCAB = frozenset(
    {"yes", "no", "prohibit-password", "forced-commands-only"}
)
_PASSWORD_AUTH_VOCAB = frozenset({"yes", "no"})

_HEX32_RE = re.compile(r"^[0-9A-F]{32}$")
_HEXDIGITS = frozenset("0123456789ABCDEF")

# Remediation SUGGESTIONS (acceptance 1: bounded advice, never an action).
# Keyed by finding kind; only active rows carry one. This monitor reports —
# it never changes the target.
_REMEDIATION: dict[str, str] = {
    "security.sshd_root_login": (
        "Set 'PermitRootLogin prohibit-password' (or 'no') in sshd_config and "
        "reload sshd after confirming who still needs direct root login. This "
        "monitor only reports; it never changes the target."
    ),
    "security.sshd_password_auth": (
        "Prefer key-based authentication: set 'PasswordAuthentication no' for "
        "accounts that do not need passwords and reload sshd. This monitor "
        "only reports; it never changes the target."
    ),
    "security.exposed_listener": (
        "Verify this listener is meant to be reachable; if not, bind it to "
        "loopback or restrict it with a firewall rule. This monitor only "
        "reports; it never changes the target."
    ),
    "security.sudo_nopasswd": (
        "Review each NOPASSWD grant; restrict them to the minimum commands "
        "and accounts that need them. This monitor only reports; it never "
        "changes the target."
    ),
    "security.firewall_rules": (
        "Confirm firewall enforcement at another layer (cloud security "
        "group, host) or create a reviewed baseline ruleset before enabling "
        "rules on a remote host. This monitor only reports; it never changes "
        "the target."
    ),
    "security.updates_auto": (
        "Enable automatic security updates (unattended-upgrades for apt, "
        "dnf-automatic for dnf) or add the host to a regular patching "
        "routine. This monitor only reports; it never changes the target."
    ),
}


# ---------------------------------------------------------------------------
# Pure decode helpers (/proc hex forms, shared shape with the network module)
# ---------------------------------------------------------------------------


def _ipv4_hex(raw: str) -> str | None:
    """Decode a ``/proc`` big-endian-word hex dword into dotted quad."""
    if len(raw) != 8 or not _HEXDIGITS.issuperset(raw):
        return None
    return str(ipaddress.IPv4Address(bytes.fromhex(raw)[::-1]))


def _ipv6_hex(raw: str) -> str | None:
    """Decode a ``/proc/net/tcp6`` 32-hex address (four LE words)."""
    if not _HEX32_RE.fullmatch(raw):
        return None
    words = [bytes.fromhex(raw[i : i + 8])[::-1] for i in range(0, 32, 8)]
    return str(ipaddress.IPv6Address(b"".join(words)))


def _is_loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Sample shape (pure; what one probe round trip observed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Listener:
    """One TCP LISTEN socket as the target reported it.

    ``address`` is a dotted quad (v4) or the compressed IPv6 form
    (``0.0.0.0``/``::`` = any interface); it states the binding, nothing
    more. UDP is deliberately out of scope for the exposure baseline
    (connectionless sockets are always "listening").
    """

    proto: Literal["tcp", "tcp6"]
    address: str
    port: int


@dataclass(frozen=True, slots=True)
class SecuritySample:
    """One bounded security-posture snapshot.

    ``None`` fields are genuinely unobserved sources — the collector never
    substitutes a clean/safe value (acceptance 2). ``sshd_source`` records
    which parse answered the directives: ``effective`` (``sshd -T``, the
    authoritative shape, defaults resolved) or ``config`` (readable
    sshd_config file; directives the file does not mention stay ``None``
    because single-file first-match semantics plus unexpanded ``Include``
    lines cannot resolve defaults honestly).
    """

    observed_at: datetime
    observed_at_source: Literal["target", "controller"]
    quality: ObservationQuality
    sshd_present: bool | None = None
    sshd_effective_ok: bool | None = None
    sshd_config_readable: bool | None = None
    sshd_source: Literal["effective", "config"] | None = None
    permit_root_login: str | None = None
    password_authentication: str | None = None
    nft_present: bool | None = None
    nft_rules: int | None = None
    iptables_present: bool | None = None
    iptables_rules: int | None = None
    tcp_complete: bool = False
    tcp6_complete: bool = False
    listeners: tuple[Listener, ...] = ()
    sudo_present: bool | None = None
    sudo_nopasswd: int | None = None
    apt_present: bool | None = None
    apt_auto: bool | None = None
    dnf_present: bool | None = None
    dnf_auto: bool | None = None

    @property
    def listeners_complete(self) -> bool:
        """Both TCP LISTEN tables were actually read (rc=0)."""
        return self.tcp_complete and self.tcp6_complete

    @property
    def sshd_posture_resolved(self) -> bool:
        """One declared sshd source answered (or sshd is absent)."""
        if self.sshd_present is None:
            return False
        if self.sshd_present is False:
            return True
        return bool(self.sshd_effective_ok) or bool(self.sshd_config_readable)

    @property
    def firewall_resolved(self) -> bool:
        """Either no firewall tooling exists, or a ruleset listing was read."""
        presents = (self.nft_present, self.iptables_present)
        if all(p is False for p in presents):
            return True
        return any(
            present and rules is not None
            for present, rules in (
                (self.nft_present, self.nft_rules),
                (self.iptables_present, self.iptables_rules),
            )
        )

    @property
    def sudo_resolved(self) -> bool:
        """sudo presence is known and, when present, sudoers readability."""
        if self.sudo_present is None:
            return False
        if self.sudo_present is False:
            return True
        return self.sudo_nopasswd is not None

    @property
    def updates_resolved(self) -> bool:
        """Package-manager detection answered for both supported families."""
        return self.apt_present is not None and self.dnf_present is not None

    @property
    def managed_package_managers(self) -> tuple[str, ...]:
        """Supported managers detected on the target (``apt`` before ``dnf``)."""
        names = []
        if self.apt_present:
            names.append("apt")
        if self.dnf_present:
            names.append("dnf")
        return tuple(names)

    @property
    def updates_auto_configured(self) -> bool | None:
        """Any detected manager has automatic-update machinery configured.

        ``None`` when no supported manager is detected (the check does not
        apply) or a detected manager's marker could not be read — never
        ``False`` by inference.
        """
        managed = self.managed_package_managers
        if not managed:
            return None
        markers = {
            "apt": self.apt_auto,
            "dnf": self.dnf_auto,
        }
        known = [markers[name] for name in managed if markers[name] is not None]
        if len(known) < len(managed):
            return None
        return any(known)


def _listener(proto: Literal["tcp", "tcp6"], raw: str) -> Listener | None:
    address_hex, sep, port_hex = raw.rpartition(":")
    # Valid shapes: v4 "8-hex:4-hex" (13 chars) … v6 "32-hex:4-hex" (37).
    if not sep or len(port_hex) > 4 or not 6 <= len(raw) <= 37:
        return None
    try:
        port = int(port_hex, 16)
    except ValueError:
        return None
    if not 0 < port <= 65535:
        return None
    if proto == "tcp":
        address = _ipv4_hex(address_hex)
    else:
        address = _ipv6_hex(address_hex)
    if address is None:
        return None
    return Listener(proto=proto, address=address, port=port)


def _bool_field(raw: str | None) -> bool | None:
    if raw == "yes":
        return True
    if raw == "no":
        return False
    return None


def _int_field(raw: str | None) -> int | None:
    if raw is None or not raw.isdigit():
        return None
    return int(raw)


def _directive_values(
    lines: list[str], *, first_match_wins: bool
) -> tuple[str | None, str | None]:
    """Parse ``directive value`` lines into (permit_root, password_auth).

    Effective output lists each directive exactly once; the config-file
    fallback applies sshd's own FIRST-match-wins semantics for a single
    file (unexpanded ``Include`` lines are a declared limitation of that
    fallback, never silently resolved to a default).
    """
    root: str | None = None
    auth: str | None = None
    ordered = lines if first_match_wins else list(reversed(lines))
    for line in ordered:
        parts = line.split(" ")
        directive = parts[0].strip().lower()
        value = parts[1].strip().lower() if len(parts) > 1 else ""
        if directive == "permitrootlogin" and root is None:
            root = value if value in _PERMIT_ROOT_VOCAB else None
        elif directive == "passwordauthentication" and auth is None:
            auth = value if value in _PASSWORD_AUTH_VOCAB else None
    return root, auth


def parse_probe_output(output: str, *, controller_now: datetime) -> SecuritySample:
    """Parse fixed probe output into a :class:`SecuritySample` (pure).

    Order-sensitive like the P04 identity parser: recognized keys are
    captured on their first appearance, unknown keys are ignored, malformed
    values leave the field ``None``. Sample quality is the shared observation
    vocabulary applied to the five declared checks (sshd posture, firewall
    state, TCP listener tables, sudoers posture, automatic-update posture):
    all resolved ⇒ ``measured``, some ⇒ ``degraded``, none ⇒ ``unknown``
    (the schema forbids any metric on an unknown row). The timestamp is the
    target's own ``date -u`` when parsable, otherwise the controller clock —
    a measured sample is always timestamped.
    """
    if controller_now.tzinfo is None:
        raise ValueError("controller_now must be timezone-aware")
    values: dict[str, str] = {}
    eff_lines: list[str] = []
    cfg_lines: list[str] = []
    listener_lines: dict[str, list[str]] = {"tcp_l": [], "tcp6_l": []}
    for raw_line in output.splitlines():
        line = raw_line.rstrip("\r")
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key in listener_lines:
            listener_lines[key].append(value)
        elif key in ("eff", "cfg"):
            if value:
                (eff_lines if key == "eff" else cfg_lines).append(value)
        elif key not in values:
            values[key] = value

    observed_at = controller_now
    observed_at_source: Literal["target", "controller"] = "controller"
    raw_ts = values.get("ts")
    if raw_ts:
        try:
            parsed = datetime.strptime(raw_ts, _TS_FORMAT).replace(tzinfo=UTC)
        except ValueError:
            parsed = None
        if parsed is not None:
            observed_at = parsed
            observed_at_source = "target"

    sshd_present = _bool_field(values.get("sshd_present"))
    sshd_effective_ok = _int_field(values.get("sshd_t_rc")) == 0
    if values.get("sshd_t_rc") is None:
        sshd_effective_ok = False
    cfg_readable = _int_field(values.get("cfg_rc")) == 0
    if values.get("cfg_rc") is None:
        cfg_readable = False

    sshd_source: Literal["effective", "config"] | None = None
    permit_root_login: str | None = None
    password_authentication: str | None = None
    if sshd_effective_ok:
        sshd_source = "effective"
        permit_root_login, password_authentication = _directive_values(
            eff_lines, first_match_wins=False
        )
    elif cfg_readable:
        sshd_source = "config"
        permit_root_login, password_authentication = _directive_values(
            cfg_lines, first_match_wins=True
        )

    nft_present = _bool_field(values.get("nft_present"))
    nft_rules: int | None = None
    if nft_present:
        nft_rules = _int_field(values.get("nft_rules"))
    iptables_present = _bool_field(values.get("ipt_present"))
    iptables_rules: int | None = None
    if iptables_present:
        iptables_rules = _int_field(values.get("ipt_rules"))

    tcp_complete = _int_field(values.get("tcp_rc")) == 0
    tcp6_complete = _int_field(values.get("tcp6_rc")) == 0
    listeners: list[Listener] = []
    for proto, key in (
        ("tcp", "tcp_l"),
        ("tcp6", "tcp6_l"),
    ):
        for raw in listener_lines[key][:MAX_TCP_LISTENERS]:
            listener = _listener(proto, raw)
            if listener is not None:
                listeners.append(listener)

    sudo_present = _bool_field(values.get("sudo_present"))
    sudo_nopasswd: int | None = None
    if sudo_present and values.get("sudo_read") == "ok":
        sudo_nopasswd = _int_field(values.get("sudo_nopasswd"))

    apt_present = _bool_field(values.get("pkg_apt"))
    apt_auto = _bool_field(values.get("apt_auto")) if apt_present else None
    dnf_present = _bool_field(values.get("pkg_dnf"))
    dnf_auto = _bool_field(values.get("dnf_auto")) if dnf_present else None

    core_resolved = (
        bool(sshd_present is not None and (not sshd_present or sshd_source)),
        all(p is False for p in (nft_present, iptables_present))
        or any(
            present and rules is not None
            for present, rules in (
                (nft_present, nft_rules),
                (iptables_present, iptables_rules),
            )
        ),
        tcp_complete and tcp6_complete,
        sudo_present is not None and (not sudo_present or sudo_nopasswd is not None),
        apt_present is not None and dnf_present is not None,
    )
    if all(core_resolved):
        quality = ObservationQuality.MEASURED
    elif any(core_resolved):
        quality = ObservationQuality.DEGRADED
    else:
        quality = ObservationQuality.UNKNOWN

    return SecuritySample(
        observed_at=observed_at,
        observed_at_source=observed_at_source,
        quality=quality,
        sshd_present=sshd_present,
        sshd_effective_ok=sshd_effective_ok or None,
        sshd_config_readable=cfg_readable or None,
        sshd_source=sshd_source,
        permit_root_login=permit_root_login,
        password_authentication=password_authentication,
        nft_present=nft_present,
        nft_rules=nft_rules,
        iptables_present=iptables_present,
        iptables_rules=iptables_rules,
        tcp_complete=tcp_complete,
        tcp6_complete=tcp6_complete,
        listeners=tuple(listeners),
        sudo_present=sudo_present,
        sudo_nopasswd=sudo_nopasswd,
        apt_present=apt_present,
        apt_auto=apt_auto,
        dnf_present=dnf_present,
        dnf_auto=dnf_auto,
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

    Local mirror of the measured-probe classification (the transport owns the
    canonical table): only the coarse class matters here — the collector maps
    any of these to the unknown path, the codes exist so evidence and logs
    name the same rejection reason the transport suite pins.
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


def probe_security(
    identity: SshIdentity,
    *,
    key_path: Path,
    known_hosts_path: Path,
    controller_now: datetime,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> SecuritySample:
    """Run the fixed security probe over the pinned-identity transport.

    Preconditions mirror :func:`mai.execution.ssh.probe_environment`: the key
    path comes from ``resolve_identity_key`` and the managed known-hosts file
    from ``pin_host_key`` (enrollment). Raises :class:`SshConnectError`
    (stable codes) for any transport failure; a target that answers is
    parsed into a :class:`SecuritySample` under the hard output budget.
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
            SshErrorCode.TIMEOUT, f"security probe timed out after {timeout}s"
        ) from None
    if len(proc.stdout.encode("utf-8")) > MAX_PROBE_OUTPUT_BYTES:
        raise SshConnectError(
            SshErrorCode.BOUNDED_OUTPUT_EXCEEDED,
            f"security probe output exceeded the {MAX_PROBE_OUTPUT_BYTES}-byte budget",
        )
    if proc.returncode != 0:
        raise _map_probe_failure(
            proc.returncode, proc.stderr, f"{identity.host}:{identity.port}"
        )
    return parse_probe_output(proc.stdout, controller_now=controller_now)


# ---------------------------------------------------------------------------
# Findings (collector-owned checks; dedup by (server_id, kind, dedup_key))
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FindingDraft:
    """One collector-owned check evaluation for a sample.

    ``state`` is three-valued: ``active`` upserts the row, ``clear`` closes
    the matching open row, ``no_data`` leaves rows untouched (missing data is
    never evidence that a condition ended — and never a pass). ``detail``
    carries the bounded remediation SUGGESTION for active rows (acceptance
    1: advice, never automatic hardening).
    """

    kind: str
    dedup_key: str
    severity: Literal["info", "warning", "critical"]
    summary: str
    state: Literal["active", "clear", "no_data"]
    detail: str | None = None


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _sshd_check(
    sample: SecuritySample,
    *,
    kind: str,
    directive: str,
    value: str | None,
    allowed_active: str,
    weak_wording: str,
) -> FindingDraft:
    """One sshd directive check: active on the weak value, clear otherwise."""
    if not sample.sshd_posture_resolved or value is None:
        return FindingDraft(
            kind=kind,
            dedup_key="config",
            severity="warning",
            summary="",
            state="no_data",
        )
    active = value == allowed_active
    source = sample.sshd_source or "unknown"
    summary = f"sshd {directive}={value} ({source} config)"
    if active:
        summary += f" ({weak_wording})"
    return FindingDraft(
        kind=kind,
        dedup_key="config",
        severity="warning" if active else "info",
        summary=_truncate(summary, SUMMARY_MAX_CHARS),
        state="active" if active else "clear",
        detail=_REMEDIATION[kind] if active else None,
    )


def exposed_listener_keys(sample: SecuritySample) -> tuple[str, ...]:
    """Deterministic dedup keys of the listeners this sample rates exposed.

    Exposed = bound to a non-loopback address (wildcard or specific).
    Loopback-only bindings are not reachable from outside the host and are
    never findings.
    """
    if not sample.listeners_complete:
        return ()
    return tuple(
        f"{listener.proto}:{listener.address}:{listener.port}"
        for listener in sample.listeners[:MAX_EXPOSED_FINDINGS]
        if not _is_loopback(listener.address)
    )


def findings_for_sample(sample: SecuritySample) -> tuple[FindingDraft, ...]:
    """Evaluate the collector's declared baseline checks against one sample.

    Pure; per-check three-valued outputs only. There is deliberately NO
    aggregate verdict: nothing in the output claims "compliant", "secure"
    or "all safe" — a clear row restates the observed value and nothing
    more (acceptance 2).
    """
    drafts: list[FindingDraft] = []

    drafts.append(
        _sshd_check(
            sample,
            kind="security.sshd_root_login",
            directive="permitrootlogin",
            value=sample.permit_root_login,
            allowed_active="yes",
            weak_wording="direct root SSH login permitted",
        )
    )
    drafts.append(
        _sshd_check(
            sample,
            kind="security.sshd_password_auth",
            directive="passwordauthentication",
            value=sample.password_authentication,
            allowed_active="yes",
            weak_wording="password SSH authentication permitted",
        )
    )

    keys = exposed_listener_keys(sample)
    if not sample.listeners_complete:
        drafts.append(
            FindingDraft(
                kind="security.exposed_listener",
                dedup_key="table",
                severity="warning",
                summary="",
                state="no_data",
            )
        )
    else:
        for key in keys:
            proto, rest = key.split(":", 1)
            address, _, port = rest.rpartition(":")
            drafts.append(
                FindingDraft(
                    kind="security.exposed_listener",
                    dedup_key=key,
                    severity="warning",
                    summary=_truncate(
                        f"{proto.upper()} listener {address}:{port} "
                        "bound on a non-loopback address",
                        SUMMARY_MAX_CHARS,
                    ),
                    state="active",
                    detail=_REMEDIATION["security.exposed_listener"],
                )
            )

    if sample.sudo_present is None:
        drafts.append(
            FindingDraft(
                kind="security.sudo_nopasswd",
                dedup_key="grants",
                severity="warning",
                summary="",
                state="no_data",
            )
        )
    elif sample.sudo_present is False:
        drafts.append(
            FindingDraft(
                kind="security.sudo_nopasswd",
                dedup_key="grants",
                severity="info",
                summary="sudo not installed; no sudoers grants to review",
                state="clear",
            )
        )
    elif sample.sudo_nopasswd is None:
        # sudoers unreadable: a gap, never "no weak grants" (acceptance 2).
        drafts.append(
            FindingDraft(
                kind="security.sudo_nopasswd",
                dedup_key="grants",
                severity="warning",
                summary="",
                state="no_data",
            )
        )
    else:
        count = sample.sudo_nopasswd
        drafts.append(
            FindingDraft(
                kind="security.sudo_nopasswd",
                dedup_key="grants",
                severity="warning" if count else "info",
                summary=_truncate(
                    f"{count} NOPASSWD sudo grant line(s) readable in sudoers",
                    SUMMARY_MAX_CHARS,
                ),
                state="active" if count else "clear",
                detail=_REMEDIATION["security.sudo_nopasswd"] if count else None,
            )
        )

    rule_sources = (
        ("nft", sample.nft_present, sample.nft_rules),
        ("iptables", sample.iptables_present, sample.iptables_rules),
    )
    read = [
        (name, rules)
        for name, present, rules in rule_sources
        if present and rules is not None
    ]
    if all(present is False for _, present, _ in rule_sources):
        drafts.append(
            FindingDraft(
                kind="security.firewall_rules",
                dedup_key="ruleset",
                severity="warning",
                summary="",
                state="no_data",
            )
        )
    elif read:
        # nft first when both answered: on hybrid hosts iptables -S and
        # nft list ruleset describe the SAME rules — never sum them.
        name, rules = read[0]
        active = rules == 0
        drafts.append(
            FindingDraft(
                kind="security.firewall_rules",
                dedup_key="ruleset",
                severity="warning" if active else "info",
                summary=_truncate(
                    f"{rules} rule line(s) in the {name} ruleset"
                    + (" (empty ruleset)" if active else ""),
                    SUMMARY_MAX_CHARS,
                ),
                state="active" if active else "clear",
                detail=_REMEDIATION["security.firewall_rules"] if active else None,
            )
        )
    else:
        # A firewall tool exists but refused/failed its listing: the state is
        # UNKNOWN, never an empty ruleset (acceptance 2).
        drafts.append(
            FindingDraft(
                kind="security.firewall_rules",
                dedup_key="ruleset",
                severity="warning",
                summary="",
                state="no_data",
            )
        )

    managers = sample.managed_package_managers
    auto = sample.updates_auto_configured
    if not managers:
        # No supported manager: the check does not apply — explicit no_data,
        # never a pass (acceptance 2).
        drafts.append(
            FindingDraft(
                kind="security.updates_auto",
                dedup_key="security",
                severity="warning",
                summary="",
                state="no_data",
            )
        )
    elif auto is None:
        drafts.append(
            FindingDraft(
                kind="security.updates_auto",
                dedup_key="security",
                severity="warning",
                summary="",
                state="no_data",
            )
        )
    else:
        active = not auto
        drafts.append(
            FindingDraft(
                kind="security.updates_auto",
                dedup_key="security",
                severity="warning" if active else "info",
                summary=_truncate(
                    (
                        "no automatic security-update configuration detected "
                        f"({', '.join(managers)})"
                        if active
                        else f"automatic security-update configuration detected "
                        f"({', '.join(managers)})"
                    ),
                    SUMMARY_MAX_CHARS,
                ),
                state="active" if active else "clear",
                detail=_REMEDIATION["security.updates_auto"] if active else None,
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
     WHERE server_id = :server_id AND kind = :kind AND dedup_key = :dedup_key
       AND resolved_at IS NULL
    """
)

# Per-listener dedup keys need reconciliation, not kind-wide closing: a
# complete listener table closes exactly the open exposed-listener rows whose
# key vanished; survivors were refreshed by the upserts above.
_CLOSE_STALE_LISTENERS = sa.text(
    """
    UPDATE observation_findings
       SET resolved_at = :resolved_at, updated_at = :resolved_at
     WHERE server_id = :server_id AND kind = :kind AND resolved_at IS NULL
       AND dedup_key NOT IN :keys
    """
).bindparams(sa.bindparam("keys", expanding=True))

_CLOSE_ALL_LISTENERS = sa.text(
    """
    UPDATE observation_findings
       SET resolved_at = :resolved_at, updated_at = :resolved_at
     WHERE server_id = :server_id AND kind = :kind AND resolved_at IS NULL
    """
)


def _metric_rows(
    server_id: uuid.UUID,
    sample: SecuritySample,
    collected_at: datetime,
    fresh_until: datetime,
    retain_until: datetime,
) -> list[dict[str, object]]:
    """Bounded per-metric rows; the rendering stays a short normalized text.

    No row can ever carry configuration or sudoers CONTENT — only presence
    flags, enum words and counts (acceptance 2, structural half).
    """

    def row(metric: str, value: str) -> dict[str, object]:
        return {
            "observation_id": uuid.uuid4(),
            "server_id": server_id,
            "source": SOURCE,
            "method": ObservationMethod.COMMAND.value,
            "quality": sample.quality.value,
            "metric": metric,
            "value": value,
            "observed_at": sample.observed_at,
            "collected_at": collected_at,
            "fresh_until": fresh_until,
            "retain_until": retain_until,
        }

    rows: list[dict[str, object]] = []
    if sample.sshd_present is not None:
        rows.append(row("sec.sshd.present", "yes" if sample.sshd_present else "no"))
    if sample.sshd_source is not None:
        rows.append(row("sec.sshd.source", sample.sshd_source))
    if sample.permit_root_login is not None:
        rows.append(row("sec.sshd.permit_root_login", sample.permit_root_login))
    if sample.password_authentication is not None:
        rows.append(
            row("sec.sshd.password_authentication", sample.password_authentication)
        )
    if sample.nft_present is not None:
        rows.append(row("sec.firewall.nft", "yes" if sample.nft_present else "no"))
    if sample.nft_rules is not None:
        rows.append(
            row("sec.firewall.nft_rules", str(min(sample.nft_rules, MAX_RULE_LINES)))
        )
    if sample.iptables_present is not None:
        rows.append(
            row("sec.firewall.iptables", "yes" if sample.iptables_present else "no")
        )
    if sample.iptables_rules is not None:
        rows.append(
            row(
                "sec.firewall.iptables_rules",
                str(min(sample.iptables_rules, MAX_RULE_LINES)),
            )
        )
    if sample.listeners_complete:
        for listener in sample.listeners:
            rows.append(
                row(
                    f"sec.listen.{listener.proto}",
                    f"{listener.address}:{listener.port}",
                )
            )
        rows.append(row("sec.listen_count", str(len(sample.listeners))))
    if sample.sudo_present is not None:
        rows.append(row("sec.sudo.present", "yes" if sample.sudo_present else "no"))
    if sample.sudo_nopasswd is not None:
        rows.append(row("sec.sudo.nopasswd_count", str(sample.sudo_nopasswd)))
    if sample.updates_resolved:
        if sample.apt_present or sample.dnf_present:
            names = sample.managed_package_managers
            rows.append(row("sec.updates.pkg_mgr", ",".join(names)))
            auto = sample.updates_auto_configured
            if auto is not None:
                rows.append(row("sec.updates.auto_configured", "yes" if auto else "no"))
        else:
            rows.append(row("sec.updates.pkg_mgr", "none"))
    return rows


async def record_sample(
    sessions: async_sessionmaker[AsyncSession],
    server_id: uuid.UUID,
    sample: SecuritySample,
    *,
    controller_now: datetime,
) -> int:
    """Persist one sample as bounded observation rows + baseline findings.

    One row per observed metric (source/method/quality/timestamps per C10
    rule 4), then the collector's check set: active drafts upsert onto
    ``(server_id, kind, dedup_key)``, cleared drafts close the matching open
    row, ``no_data`` drafts do nothing. When the listener tables were
    completely read, open exposed-listener rows whose key is absent from the
    observed set are closed (removed listeners end; survivors were refreshed
    by their upserts). An unknown-quality sample parses to zero metric rows
    (the schema forbids a measurement on an unknown row). Returns the number
    of observation rows written (findings excluded).
    """
    if controller_now.tzinfo is None:
        raise ValueError("controller_now must be timezone-aware")
    fresh_until = sample.observed_at + FRESH_HORIZON
    retain_until = controller_now + RETENTION_HORIZON
    rows = _metric_rows(server_id, sample, controller_now, fresh_until, retain_until)
    # Active findings anchor to a REAL inserted sample row (FK to
    # server_observations); with no metric rows no finding can be active.
    observation_id = rows[0]["observation_id"] if rows else uuid.uuid4()
    async with sessions() as session:
        for params in rows:
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
                        "detail": _truncate(draft.detail or "", DETAIL_MAX_CHARS)
                        or None,
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
        if sample.listeners_complete:
            keys = list(exposed_listener_keys(sample))
            if keys:
                await session.execute(
                    _CLOSE_STALE_LISTENERS,
                    {
                        "resolved_at": controller_now,
                        "server_id": server_id,
                        "kind": "security.exposed_listener",
                        "keys": keys,
                    },
                )
            else:
                await session.execute(
                    _CLOSE_ALL_LISTENERS,
                    {
                        "resolved_at": controller_now,
                        "server_id": server_id,
                        "kind": "security.exposed_listener",
                    },
                )
        await session.commit()
    return len(rows)


async def record_unavailable(
    sessions: async_sessionmaker[AsyncSession],
    server_id: uuid.UUID,
    *,
    controller_now: datetime,
) -> int:
    """Persist the unknown path for a permission-refused/unreachable target.

    Exactly ONE ``quality='unknown'`` row with NO metric/value/observed_at
    (the schema's ``ck_server_observations_unknown_has_no_measurement``
    forbids anything else) — never a zero reading and never a " posture
    re-evaluation". Existing findings are deliberately left untouched:
    missing data neither closes nor re-affirms a condition. Returns 1.
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
    unavailable — the caller-facing shape of acceptance 2 (a gap is a gap,
    never a clean bill). ``error`` carries only the stable rejection code.
    """

    quality: ObservationQuality
    observations: int
    server_id: uuid.UUID
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
) -> CollectionResult:
    """Probe a target and persist the sample, or persist the unknown path.

    Never raises for target-side failures (any :class:`SshConnectError`
    becomes a persisted unknown observation); controller misconfiguration
    (unresolvable key reference, missing enrollment) still raises — failing
    closed is the configuration boundary's job, not a collection sample.
    """
    try:
        sample = probe_security(
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
            error=error.code.value,
        )
    written = await record_sample(
        sessions, server_id, sample, controller_now=controller_now
    )
    return CollectionResult(
        quality=sample.quality, observations=written, server_id=server_id
    )

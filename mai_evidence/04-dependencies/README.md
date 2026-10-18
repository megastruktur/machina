# mai-04-dependencies — evidence (fix round, 2026-10-14)

## Target
Card `mai-04-dependencies`: pin selected SSH/host-execution dependency set.
This is the fix round after reviewer FAIL: the first round (2ad8a7c) added
`asyncssh>=2.24,<3`, contradicting the P00 ssh-spike decision
(`docs/architecture/host-supervision.md`, probe
`experiments/ssh_supervision_probe.py`) which selected the **system OpenSSH
client driven via subprocess** — no third-party SSH library, nothing to pin.

## Implementation (fix diff vs 2ad8a7c, purely subtractive in deps)
- `backend/pyproject.toml` — asyncssh requirement removed; provenance comment
  records the P00 system-ssh selection and points at the runtime check.
- `backend/uv.lock` — asyncssh 2.24.1 and its sole new transitive
  cryptography 50.0.2 dropped (78 deletions, no other changes; 36 packages).
- `backend/tests/test_execution_dependencies.py` — rewritten:
  - negative control: no unselected SSH distribution (asyncssh, paramiko,
    fabric, plumbum, pexpect, libssh) in manifest, uv.lock, or importable in
    the locked environment;
  - `uv lock --check` manifest↔lock consistency;
  - runtime assertion of the un-pinnable host dependency: `ssh`, `ssh-keygen`,
    `ssh-keyscan` on PATH; `ssh -V` ≥ OpenSSH 9.2 (documented baseline,
    `docs/architecture/runtime-environment.md` capability table);
  - frozen probe option surface (`ssh_opts()`: IdentitiesOnly, BatchMode,
    StrictHostKeyChecking=yes, UserKnownHostsFile, ConnectTimeout=5) verified
    offline via `ssh -G` (renders config, never dials — D5 rule).

## Acceptance mapping
1. *Only P00-selected supported SSH/host execution libraries added* — zero
   libraries added; P00 selected no third-party SSH library; negative controls
   across manifest/lock/installed env (02-tests §pytest, 01-commands).
2. *Clean locked install imports required production adapters; no invented
   third-party API* — `uv sync --frozen` from lock only; unchanged P01 import
   guard (`tests/test_import.py`: dbos/fastapi/mai) passes in the same run;
   the API surface the downstream P04 adapters will call (system client
   options) is proven to exist on the installed client, not invented
   (02-tests §ssh -G).

## File index

| File | Content |
|---|---|
| `01-commands.txt` | Fix-round commands and results (`uv lock` removals, `--check`, `ssh -V`) |
| `02-tests-and-runtime-checks.txt` | Focused suite receipt: `test_execution_dependencies.py` + `test_import.py`, 8 passed |
| `03-lock-diff.txt` | `uv.lock` diff: asyncssh + cryptography dropped, 36 packages retained |

Preserved first-round history (matrix copy in `plans/mai-v1/mai_evidence/`
only; not part of the committed fix-round set): `01-toolchain-and-tree.txt`,
`02-resolve-lock.txt`, `03-install-offline.txt`, `04-tests-entrypoint.txt`,
`05-cleanup-proof.txt`, `06-commit-receipt.txt` — the asyncssh round kept
for the failure record per test policy.

## Commands (verbatim outputs in the numbered files)
- `cd backend && uv lock` → "Removed asyncssh v2.24.1 / Removed cryptography v50.0.2"
- `cd backend && uv lock --check` → rc=0
- `cd backend && uv sync --frozen` → rc=0 (34 packages checked)
- `cd backend && uv run pytest tests/test_execution_dependencies.py tests/test_import.py -q` → 8 passed
- `ssh -V` → OpenSSH_9.2p1 Debian-2+deb12u10
- `ssh -G -o <frozen options> mai-p04-option-probe.invalid` → rc=0, values echoed

## Limitations
- Version floor 9.2 is the documented coordinator baseline, not a probe-measured
  requirement; the probe's flags are available in far older clients.
- `ssh -G` proves option acceptance, not a live session; live transport was
  P00's probe evidence (fixture container), and D5 forbids testing SSH
  transports against the coordinator host.
- No clean-room rebuild (fresh container) was run; `uv sync --frozen` proves
  lock-only installation in the existing environment.
- asyncssh test file from the failed round tested an API surface the P04
  adapters were claimed to freeze against; with system ssh the freeze is the
  probe option surface, asserted at runtime here.

# mai_evidence/00-environment — P00 probe evidence

Card: mai-00-environment (P00, service `platform`, documentation-only).
Captured: 2026-10-12, on the assigned feature worktree `megastruktur/mai-00-environment`.
Capture method: live one-shot shell probes on the runtime host; outputs pasted verbatim from the
tool transcripts into `0*.txt`. No probe scripts exist in the repository (documentation-only scope);
every command is reproduced inline in the evidence files so the coordinator can re-run it.

| File | Content |
|---|---|
| `01-host-toolchain.txt` | Host identity, resources, toolchain inventory/versions, shim-layer anomaly |
| `02-disposable-arch-docker.txt` | Docker daemon facts + verified disposable Arch container target |
| `03-provider-access.txt` | Provider/credential env-var NAMES only, local endpoint reachability, package indexes |
| `04-ssh-identity-probe.txt` | Disposable SSH identity probe: host identity, unprovisioned-key connect attempt, first failed attempt preserved |
| `05-browser-probe.txt` | omp managed headless Chromium probe |
| `06-cleanup-proof.txt` | Final fixture/resource cleanup state |

## Limitations

- P00 draft inputs `mai_CONTRACTS.md` and `mai_UI_WORKSPACE.md` were not present anywhere under
  `~/orca` at execution time; this card ran on `BRIEF.md` + `IDEA.md` alone. The contracts this
  document produces are inputs to, not derivations from, those files.
- Secret values were never read or recorded; only credential env-var NAMES are listed.
- Host userland is Debian 12 in a container (no systemd); the Arch target is therefore containerized.
  See `docs/architecture/runtime-environment.md` decision D1.

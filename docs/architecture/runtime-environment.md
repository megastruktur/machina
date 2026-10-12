# Runtime Environment — Disposable Arch Target and Toolchain (ADR)

- **Card:** mai-00-environment (P00 · service `platform` · risk `decision` · documentation-only)
- **Status:** Accepted for P00 (author decision under user-authorized START, 2026-10-12)
- **Exclusive resource:** `architecture-decision:00-environment`
- **Scope:** this file only; evidence in `mai_evidence/00-environment/`
- **Inputs:** `BRIEF.md`, `IDEA.md`. The named draft inputs `mai_CONTRACTS.md` and
  `mai_UI_WORKSPACE.md` were absent at execution time (see Limitations); nothing here invents
  APIs from their proposed filenames.

## Context

Later MAI phases must execute real commands, package installs, service checks and SSH probes
against something disposable before anything touches a production target. IDEA.md §1 names Arch
Linux as the initial installation host, but the actual coordinator/dev runtime is a **Debian 12
container** (hostname `megaserver`, CachyOS 7.2.8 kernel, overlay root, no systemd). The Arch
environment therefore cannot be "the machine we are on"; it must be materialized per-run.

Resolved capabilities, all verified live (evidence files 01–06):

| Capability | Actual state |
|---|---|
| Orca / OMP | orca 1.4.219, omp 18.6.0 |
| Python | 3.14.6 via `~/.local/bin` rtk shims; `venv` ok; **global `pytest` shim broken**; uv 0.11.26; pip3 absent |
| JS | node v22.22.3, npm 10.9.8, pnpm present; bun MISSING, deno MISSING |
| Docker | client 29.8.1 / daemon 29.8.2, Compose v5.5.0, overlayfs, cgroup v2, daemon reachable without systemd |
| Browser | no system browser binaries; **omp managed headless Chromium (engine 150) verified** open/evaluate/close |
| SSH | client OpenSSH 9.2p1 (Debian); no sshd on host — endpoints must come from fixtures |
| Provider access | gateway `127.0.0.1:4000` alive, HTTP 401 unauthenticated; credential env NAME `PI_LITELLM_KEY` present (value never read); no local ollama/LM Studio; pypi + npm registry reachable (200) |
| Misc | `jq` resolves to jaq 2.3.0; `rsync` and `ss` MISSING; git 2.39.5 |

## Decisions

**D1 — Disposable Arch target = ephemeral Docker containers.**
`docker run --rm archlinux:latest …` is the disposable Arch environment (verified: Arch Linux
rolling `20261011.0.609700`, container self-removed). Distro-specific phases (pacman, systemd
layouts) run inside such containers. Production targets, persistent VMs, and the coordinator host
userland are excluded as targets. Reproducibility-critical runs should pin the image digest; the
rolling tag is acceptable for exploratory phases. When a phase needs an SSH endpoint, it starts a
throwaway sshd fixture container (producer-verified form:
`docker run --rm -d --name mai-fxt-sshd -p 127.0.0.1:2222:22 -e SSH_USERS='user:uid:gid' panubo/sshd`).

**D2 — Resource budget for disposable work.**
Per disposable target: ≤ 2 vCPU, ≤ 2 GiB RAM, wall-clock bounded by the card attempt budget
(2100 s). One disposable target at a time per card. Fixture containers are `--rm`; aggregate
pulled-image cache stays within ordinary disk headroom (569 G free); no long-lived volumes.

**D3 — Fixture namespace.**
Docker resources: `mai-fxt-<card>-<purpose>` (e.g. `mai-fxt-sshd`). Temp files:
`/tmp/mai-fxt-<card>-*`. Evidence: `mai_evidence/<NN>-<card>/`. Anything named outside this
namespace is not a MAI fixture and must not be touched.

**D4 — Cleanup ownership.**
The run that creates a fixture deletes it and records proof in its evidence directory before
completion (06-cleanup-proof.txt). The coordinator audits that proof at the artifact check.
Pulled images are cache, not fixtures; their removal is a later, explicit decision. The probes
here touched no persistent host state (no sshd/authorized_keys/systemd/global packages).

**D5 — Toolchain usage rules.**
Python work uses uv-managed project venvs (`uv run …`); the broken global `pytest` shim and the
absent `pip3` are never worked around globally, and P00 installed nothing. SSH transports are
tested against fixture containers, never the coordinator host. Provider tests authenticate
against `127.0.0.1:4000` using credentials supplied at runtime through configuration; secret
values never enter repo, transcripts, or evidence (this card recorded env NAMES only).

## Acceptance mapping

- **AC1 (approved disposable Arch target, budget, fixture namespace, cleanup ownership):**
  D1–D4, verified by `02` and `06`.
- **AC2 (actual Orca/OMP, Python, JS, Docker, browser, provider-test capabilities, no secrets):**
  capability table + D5, verified by `01`, `02`, `03`, `05`; the disposable SSH identity probe
  (host identity, unprovisioned-key denial, cleanup) is in `04`.
  Gaps that BLOCK later execution if unaddressed: no bun/deno, no sshd on host, broken global
  pytest shim (mitigated by uv venvs), provider gateway requires credentials that must arrive
  via configuration before any live model test.

## Limitations

- `mai_CONTRACTS.md` / `mai_UI_WORKSPACE.md` absent everywhere under `~/orca` at execution time;
  this ADR is written standalone and hands its content forward as P00's contract contribution
  (environment sections) rather than editing files outside the allowlist.
- The runtime host being Debian-12-in-a-container is an environment fact, not an IDEA.md conflict:
  IDEA's Arch host is the future installation target, not this dev runtime.
- Image `panubo/sshd` was chosen as a small generic fixture; a future card may substitute an
  Arch-based sshd fixture without changing D1–D4.

## Provenance

Recorded 2026-10-12 on branch `megastruktur/mai-00-environment`; commands and outputs are quoted
verbatim in `mai_evidence/00-environment/01…06`. Every future-facing command in this document was
executed by its producer during this card except `docker run` variants for other images and
`uv run` workflows, which are marked as rules, not verified commands.

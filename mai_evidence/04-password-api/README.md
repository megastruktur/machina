# mai-04-password-api — evidence (fix round, 2026-10-15)

## Target
Card `mai-04-password-api`, fix round after independent review FAIL with a real
security finding: the challenge submission body carried client-controlled
`key_path` / `known_hosts_path` which flowed verbatim into the ssh boundary
(`execute_passworded_command(..., key_path=payload.key_path,
known_hosts_path=payload.known_hosts_path)`). A client could (a) aim `ssh -i`
at an arbitrary controller file and (b) point `UserKnownHostsFile` at a file it
can plant, defeating the C04 rule 1 pinned host key (MITM window with a spent
decision) — path injection into the security boundary.

## Implementation (fix delta vs the first-round commit, allowlisted files only)
- `backend/src/mai/api/password_challenges.py`
  - `ChallengeBody`: `key_path` / `known_hosts_path` REMOVED;
    `model_config = ConfigDict(extra="forbid")` — unknown fields (a
    client-supplied path included) are REJECTED with a value-free 422 before
    anything is touched, never silently honored or silently ignored.
  - New `_credentials` lane dependency: the lifespan-built
    `mai.credentials.env.EnvResolver`; absent source ⇒ 503 `unavailable`
    (fail closed, no ambient fallback).
  - New `_transport_paths`: resolves BOTH controller-local paths SERVER-SIDE
    only, before the one-use gate (failures consume nothing):
    - identity key (C03 rule 6): `identity.credential_ref` →
      `mai.execution.ssh.resolve_identity_key` over the configured `.env`
      source; `EnvFileError` AND `SshConnectError(INVALID_CONFIGURATION)`
      (stale reference: resolved path not an existing regular file) both map
      to the same value-free 422 `invalid_configuration`;
    - known-hosts store (C04 rule 1): canonical enrolled file
      `<data_root>/servers/<server_id>/known_hosts` — written only by the
      explicit admin enrollment flow (`mai.execution.ssh.pin_host_key`);
      absence refuses pre-consume in the channel (`invalid_configuration`).
    Local `SERVERS_DIR`/filename constants: `mai.profiles.files` is outside
    this card's allowlist (same keep-local discipline as
    `mai.credentials.env`'s credential-reference regex).
  - Module/class docstrings updated to state the refusal honestly.
- `backend/src/mai/api/app.py` — lifespan builds `app.state.credentials` ONCE
  from `MAI_ENV_FILE` (the documented composition-boundary variable,
  `mai/credentials/env.py` module docstring); absent or unusable source ⇒
  `None` ⇒ the challenge surface answers 503 and touches no state.
- `backend/tests/test_password_challenge_http.py` — fixtures reworked
  (server-side fixture `.env` at 0600 — the resolver refuses group/other
  readable sources —, key file, canonical per-target known-hosts store);
  submission bodies no longer carry any path; 5 new negative controls (below).
- `frontend/src/lib/api/schema.generated.ts` — regenerated mechanically with
  the central exporter (the one documented command, `mai/api/client.ts`
  header); fields removed + ChallengeBody docstring carried over, nothing
  hand-edited.

## Acceptance mapping (unchanged criteria, fix delta)
1. *Authenticated CSRF-protected bound endpoint, no persistence/logging* —
   unchanged; the negative controls now also prove path fields cannot ride a
   valid presentation (02-tests, 03-negative-controls).
2. *Wrong principal/target/expired/replayed denied* — plus the new
   path-injection refusal: a body carrying `key_path`/`known_hosts_path` is a
   422 with ZERO transport spawns and the decision stays `pending` and
   spendable by the honest presentation (03-negative-controls).
3. *Real app registration, current OpenAPI contract, mechanical regeneration*
   — exported `ChallengeBody` has exactly 9 properties, no path fields,
   `additionalProperties: false`; route types re-exported (04-schema-export).

## Commands (verbatim outputs in numbered files)
- Focused: `cd backend && uv run pytest tests/test_password_challenge_http.py -q`
  → 21 passed (16 first-round + 5 new).
- App-consuming seams: `uv run pytest tests/test_password_challenge_http.py
  tests/test_auth_http.py tests/test_servers_http.py tests/test_grants_http.py
  tests/test_approval_http.py tests/test_health.py -q` → 120 passed.
- Producer seams (unchanged modules): `tests/test_env_resolver.py
  tests/test_ssh.py tests/test_password_input.py` → 106 passed, 9 skipped
  (the skipped ones are the REAL Arch sshd live-transport boundary claims,
  which need the fixture container; `password_input.py`/`ssh.py` are untouched
  by this fix, so their live claims are re-referenced, not re-measured).
- `uv run ruff check src/mai/api tests/test_password_challenge_http.py`
  → All checks passed!
- Central exporter rerun → no drift (`git status` on the generated file
  unchanged by a second run).

## Red/green proof of the fix (03-negative-controls)
With ONLY `model_config = ConfigDict(extra="forbid")` reverted (the guard the
finding is about), the suite fails exactly where it must:
`test_client_supplied_transport_paths_are_rejected` — the planted
`known_hosts_path` and `key_path` are silently accepted (first-round
behavior) and the request proceeds to the transport seam (spy raises:
unexpected spawn) ⇒ 500 ≠ 422; plus the `additionalProperties` contract pin.
Restoring the guard: all green. The pre-fix data flow itself
(`key_path=payload.key_path`) is visible in 01-fix-diff.txt as the removed
lines.

## Limitations
- The red run was done by reverting the `extra="forbid"` guard on the FIXED
  module, not by checking out the first-round commit: the reworked fixtures
  (server-side `.env`, canonical known-hosts store) are part of the fix and
  the old fixtures exercised the vulnerable contract by construction.
- mypy is not part of this programme's backend toolchain (dev group = pytest
  only); type discipline per ruff, as in the first round.
- The known-hosts ENROLLMENT writer (admin flow calling `pin_host_key` to
  create `servers/<id>/known_hosts`) is not this card's scope; the store's
  location and its read-side contract (refuse unenrolled pre-consume) are
  pinned here. Until the enrollment card lands, an operator enrolls by
  writing the pinned file at the canonical path (tests use a touch() stand-in
  for exactly that file's EXISTENCE — the channel's isfile check is the
  contract under test, never the pin content).
- Postgres fixture: disposable `postgres:16-alpine` container on loopback
  (35432), migrated from zero through the real Alembic chain per test
  session; stopped and removed after the runs (05-cleanup-proof).

# ADR 0001 — Windowed workspace: reusable OS-style desktop UI

- **Status:** FROZEN v1.0 (P00 decision output)
- **Phase:** P00 `00-ui-contract` (documentation-only slice)
- **Supersedes:** the page-oriented UI assumptions in the first campaign draft (per `mai_UI_WORKSPACE.md` status note). Committed `IDEA.md` §15 is compatible; folding the windowed-workspace requirement into `IDEA.md` is a separately authorized spec update, not part of this card.
- **Authority:** From acceptance of this document at the P00 gate, this file is the authoritative contract for the desktop windowed workspace. `mai_CONTRACTS.md` (C08, C11), `mai_UI_WORKSPACE.md` and `IDEA.md` §15/§17 remain draft/planning inputs; where they conflict with this frozen document, this document governs until a consented refreeze (see Change control).
- **Consumers:** `01-web`, `01-ui-tokens`, `01-ui-controls`, `01-panel-registry`, `01-window-types`, `01-window-state`, `01-window-frame`, `01-window-drag`, `01-window-resize`, `01-window-presentation`, `01-window-layout`, `01-window-responsive`, `01-workspace-runtime`, and every later feature panel card (P03–P11). Consumers read the sections relevant to them; they do not re-derive this contract.
- **Naming rule:** All type, module and constant names below are design targets for producer cards. No name in this document is an existing repo symbol, file or command.

---

## 1. Decision summary

1. **D1 — Mandatory architecture.** The first-class MAI user interface is a SvelteKit desktop workspace with independent, OS-like draggable windows composed entirely from reusable components (primitives → window state/controller → window chrome → panel registry/host → feature panels). This is an agreed product requirement, not an optional enhancement. **No Odysseus code or assets are reused**; Odysseus is inspiration for user experience only.
2. **D2 — UI library approach.** The window system is MAI-owned Svelte 5 code. **No third-party windowing/MDI library is adopted at this freeze.** Existing headless accessible primitives are **not** adopted for window chrome at this freeze; a criteria-gated path for adopting them later is defined in §4.4.
3. **D3 — Window identity.** `window_id` is view identity only, strictly separated from `server_id`/`run_id`/`application_id`. Every window carries a validated explicit scope; activation, movement, hiding or closing a window never changes the target of any window, pending operation or backend job.
4. **D4 — SSR isolation.** The workspace layer is client-only. SSR renders the route shell (launcher/overview) with an empty workspace; layout restore happens after hydration. No module-level writable workspace store may be shared across SSR requests. No secrets or domain payloads are serialized in geometry/layout state.
5. **D5 — Accessibility.** Windows are labelled, non-modal dialogs; there are no per-window focus traps; keyboard alternatives for every pointer interaction are mandatory; reduced-motion and browser-zoom behavior are contracted. Real-browser verification is required downstream; documentation review is not runtime UI validation.
6. **D6 — Registration and lifecycle.** A typed, centrally frozen `PanelRegistry` maps validated panel kinds to a fixed, trusted component map. Registration is not an authorization grant. Window/panel lifecycle is a frozen state machine; mounting never submits backend work; unmounting/closing releases subscriptions and never cancels or retargets backend jobs.
7. **D7 — Selected window controls (Q8, user-approved).** Drag (title bar only), resize, minimize/restore, maximize/restore, focus/stacking, close, a taskbar/window switcher, and same-browser safe layout restore are selected requirements. **Desktop-first.** Phone/touch parity and cross-device layout sync are out of initial scope (§9.2).
8. **D8 — Verification handoff.** §10 freezes positive/negative interaction timelines and §11 freezes good/bad window descriptors as the contract definitions that `01-window-*` producers implement and `01-workspace-runtime`/phase gates exercise in a real browser. This document freezes definitions; it does not claim any runtime behavior exists.

## 2. Mandatory architecture (D1)

The desktop workspace consists of independent windows inside a single SvelteKit page. The dashboard becomes a reusable Overview panel, chat a Chat panel, and every other feature its own panel. Layer boundaries (top→bottom):

| Layer | Owns | Must not own |
|---|---|---|
| SvelteKit routes | Thin entry, auth and deep-link adapters; mount the workspace | Feature business logic in `+page.svelte` |
| Shared UI primitives | Button, Field, StatusBadge, loading/empty/error displays, accessible control patterns, backed by design tokens | Window geometry, drag, server scoping |
| Workspace state/controller | Typed window descriptors, instance-scoped state, pure transitions, focus/stacking, persistence | Domain APIs, panel content |
| Window chrome | `WindowFrame`: title bar, server/context badge, control slots, body slot, status/error boundary; separate pointer drag adapter | SSH, chat, indexing, app lifecycle, permission decisions |
| Panel registry/host | `PanelRegistry` (kind → component/scope schema/metadata), `PanelHost` (mount/unmount, error boundary, typed context) | Domain data fetching on its own behalf |
| Feature panels | One reusable panel per feature with its own focused tests and registry entry | Duplicated drag/geometry/global-current-server logic |

Prohibited: any feature implementing its own dragging, z-order management, window geometry store, or reading an unqualified global "current server" to decide a mutation target.

## 3. Window identity and scope (D3)

### 3.1 Identity

- `window_id` is a client-generated opaque UUID (`crypto.randomUUID()` producer-side), namespace-distinct from `server_id`, `run_id`, `application_id`, `conversation_id` (C01). It identifies a view, never a target or a job.
- `window_id` is session/local scope: it is never used as a cross-device or cross-user identity.

### 3.2 Explicit scope

Every window descriptor carries one of:

- `scope.kind = "server"`: a validated `server_id` plus an optional typed object reference (`application`, `run`, `document`, `conversation`) with its own validated id.
- `scope.kind = "global"`: a declared global settings view. Its title/badge must clearly say "Global", never the last-selected server's name.

Rules (restating C11 with contractual force):

- **Pinned target.** Each submitted operation uses the immutable/pinned target context captured at submit time. Changing the launcher/global server selection MUST NOT rewrite the scope of existing windows, queued/running jobs, pending password challenges or child jobs. Retargeting is always explicit: open a new window/scope or navigate deliberately.
- The active (focused) window is not the execution target.
- Two windows bound to different servers must not exchange prompts, logs, credential challenges, application selection or retrieval cache.

## 4. UI library approach (D2)

### 4.1 Decision

- Stack: Svelte 5 (runes) + SvelteKit, already selected in `IDEA.md` §2. Exact minor/patch versions are pinned by `01-web` at its first producer commit; P00 freezes the major-line compatibility requirement only (per `IDEA.md` §19: versions are selected and verified during implementation, never copied from research snapshots).
- Window chrome, drag, resize, focus/stacking, taskbar, persistence: MAI-owned implementation using browser-native Pointer Events (with pointer capture/cancel) and standard DOM APIs. No windowing/MDI dependency.

### 4.2 Library survey recorded at freeze (documentation-level inspection, 2026-10-12)

| Candidate | License (verified) | Verdict | Reason |
|---|---|---|---|
| WinBox.js (`nextapps-de/winbox`) | Apache-2.0 | Rejected | License outside the project's MIT preference; vanilla-JS own-DOM window manager conflicts with Svelte-5-owned component model and per-panel reuse/typing. |
| Golden Layout (`golden-layout/golden-layout`) | MIT | Rejected | Docked-tab layout manager model (tabbed/docked panels), not free OS-like windows with taskbar/minimize semantics; large foreign interaction model for a capability we must own and contract-test. |
| Bits UI (`huntabyte/bits-ui` v2.x) | MIT, Svelte 5-native | **Deferred, criteria-gated** | Headless primitives target modal/overlay widgets; window chrome here is a custom non-modal widget. Eligible later for in-panel widgets (menus, popovers, dialogs) under §4.4. |
| Melt UI (`melt-ui/melt-ui`) | MIT | Deferred | Same rationale as Bits UI; at most one of the two is ever adopted, via §4.4. |

### 4.3 Dependency policy (binding on all UI producers)

A UI dependency may be added only when the introducing producer records: MIT-compatible license, Svelte 5 runes compatibility, maintenance status, pointer-cleanup/accessibility behavior, and a focused validation receipt. Feature workers never add unreviewed packages (per `mai_UI_WORKSPACE.md`). The default remains: build MAI-owned, extract a dependency only when duplication is real.

### 4.4 Headless primitives decision (explicitly required by the draft input)

Not adopted at this freeze. Adoption trigger: a feature needs a modal/overlay pattern (menu, popover, dialog inside a panel) whose accessibility is hard to own; then the producer evaluates Bits UI v2 first, records the §4.3 evidence, and the adoption goes through the normal review gate. Window chrome itself remains MAI-owned regardless.

## 5. SSR isolation and state ownership (D4)

1. The workspace route server-renders only the shell: launcher/overview and an accessible fallback content (no window DOM). The client hydrates the shell and restores the persisted layout **after hydration** (restore runs post-mount), so the first client render matches SSR (empty workspace) by construction — no hydration-mismatch class of bugs.
2. Workspace/window state lives in client-only Svelte 5 state created per browser session. **No module-level writable store at SSR-shared scope; no workspace singleton shared between users/requests.**
3. Workspace modules MUST NOT be imported from server-only modules (`+page.server.ts`, `+layout.server.ts`, server hooks). Violation is a review-blocking defect; `01-web` SHOULD add a lint/import boundary enforcing this.
4. Shared modules must not touch `window`, `document` or `localStorage` at module top level; browser APIs only inside `browser`-guarded scopes (`browser` from `$app/environment`).
5. Backend domain state and frontend window geometry are different stores. Persisted jobs continue independently of any window lifecycle.
6. Multiple tabs in the same browser each run their own workspace; persistence is last-writer-wins. Accepted single-admin consequence, recorded here, not a defect.

## 6. Accessibility contract (D5)

Semantics:

- Window root: `role="dialog"`, `aria-modal="false"`, `aria-labelledby` pointing at the title element (non-modal dialog per WAI-ARIA APG Dialog pattern). Title bar contains a heading; the server/context badge is part of the window's accessible name ("«panel title» — «server label»" or "«panel title» — Global").
- Taskbar: `role="toolbar"`, `aria-label="Open windows"`; entries are buttons whose accessible name identifies panel + server/object; minimized state is conveyed with `aria-pressed`.
- Maximize/minimize are presentation changes only; roles do not change.

Keyboard (minimal contract; no broad shortcut suite — not approved):

- Title bar is focusable; `Shift+F10` (or `Menu`) opens the title-bar context menu with Move / Resize / Minimize or Restore / Maximize or Restore / Close.
- Keyboard Move mode: arrow keys move in 10 px steps, `Shift`+arrow in 1 px steps, `Enter` commits, `Escape` cancels (reverts to pre-move geometry). Keyboard Resize mode: arrow keys resize from the chosen handle with the same commit/cancel and min-size clamp.
- All chrome controls (minimize, maximize, close, taskbar) are reachable and operable by keyboard with visible focus indicators.
- No per-window focus traps; tab order follows DOM order of the focused window, then the taskbar. Activation (click or keyboard) brings a window to front and moves focus to its title bar. No additional global shortcuts in initial scope.

Motion, zoom, bounds:

- `prefers-reduced-motion: reduce` disables drag/resize ghosting and transition animations (direct geometry follow is not an animation and stays).
- Controls must remain recoverable under viewport resize and browser zoom (see clamp rule, §10.2 NT3).
- Token values (contrast, focus ring, target sizes) are owned by `01-ui-tokens`; this contract requires their presence, not their values.

Honest limit: meeting this section is verified in a real browser by `01-workspace-runtime` and the P12 final verifier. Component-snapshot or mock-only evidence does not satisfy it (per `mai_UI_WORKSPACE.md`).

## 7. Panel registration and lifecycle (D6)

### 7.1 Registry entry (frozen fields; design-target names)

| Field | Meaning |
|---|---|
| `panel_kind` | Stable slug (`"chat"`, `"server-profile"`, …); validated on every open/restore |
| `title` | Display title key |
| `scope_types` | `"server"` and/or `"global"`, plus allowed object reference kinds |
| `component` | Compile-time reference in a fixed trusted map; never a dynamic import path from persisted state or URL |
| `default_geometry`, `min_geometry` | Geometry defaults and clamps (global defaults in §10.2 NT3 if entry omits) |
| `singleton` | At most one live instance |
| `keep_mounted_when_hidden` | Default `false`: hidden/minimized windows unmount their content and suspend subscriptions |
| `capabilities_note` | Descriptive metadata only — **registration is not a tool/backed-permission grant** (C11) |

Discovery/registration API is centrally frozen and tested by `01-panel-registry`. Shell code must not import not-yet-existing feature modules; registration is feature-owned explicit entries. Unknown or disabled `panel_kind` produces an understandable error view; it never imports from a path found in URL/layout state. Custom third-party frontend plugin windows remain out of scope (later, separately approved).

### 7.2 Lifecycle state machine

`registered → opening → mounted ⇄ hidden (minimized) / maximized(presentation overlay) → closed`

- `opening`: descriptor validated (kind known, scope objects exist and are authorized). Invalid → error view or dropped (restore), never a partial window.
- `mounted`: panel content rendered; subscriptions active per panel policy.
- `hidden` (minimized or closed-to-taskbar): content unmounted by default; subscriptions released; live data suspended.
- `closed` (final): all subscriptions/resources released explicitly.
- **Backend independence:** closing/moving/focusing/minimizing a window never starts, cancels, resubmits or retargets backend work. Job cancellation is a separate explicit domain action through the policy/job API (C08/C11).
- **Reconnect:** reopening a job/chat panel reconnects to the persisted `run_id`/event stream (SSE replay cursor, C08); mounting the same run in two windows performs no duplicate submission — panels never auto-submit on mount; resume is always an explicit user action.
- **Error containment:** panel errors are caught by a panel-scoped error boundary inside the frame; a broken panel cannot blank the desktop or stop jobs. The frame offers close/retry.
- **State display:** panels show loading/empty/stale/unknown/offline/error states, never all-green on unavailable data (C08).

## 8. Window descriptor schema (D3, D8)

Persisted/serializable descriptor, schema version 1 (field names are design targets; §11 defines what may appear in it):

```json
{
  "schema_version": 1,
  "window_id": "0f0c2b1e-…-…",
  "panel_kind": "chat",
  "scope": {
    "kind": "server",
    "server_id": "8c1d…",
    "object": { "kind": "run", "id": "44b0…" }
  },
  "geometry": { "x": 120, "y": 80, "width": 720, "height": 480 },
  "presentation": "normal",
  "z": 3,
  "title_override": null
}
```

Serialization rules:

1. Safe reference inputs only. **Forbidden anywhere in workspace/layout state:** credentials, tokens, passwords, password-input values, log bodies, prompts, conversation text, and any other domain payload content (C11). Losing a view must not leak any of these; reopening a password-protected view requires a fresh valid challenge.
2. Integers only in geometry; booleans/enum values as declared; unknown fields within the same major version are ignored on load (forward-compatible); unknown **major** version invalidates the stored layout (§10.2 NT4).
3. Layout preferences are not canonical MAI server documentation and never feed the documentation/memory systems (`mai_UI_WORKSPACE.md`; C02 authority boundaries).

## 9. Selected controls and scope (D7)

### 9.1 Selected (Q8, user-approved — requirements, not proposals)

Drag (title-bar initiation only), resize, minimize/restore, maximize/restore, focus/stacking, close, taskbar/window switcher, same-browser safe layout restore, desktop-first.

### 9.2 Explicitly out of initial scope

Phone/touch parity and dedicated mobile layout; cross-device layout sync; snapping/tiling; virtual desktops; desktop icons/file manager; a broad keyboard-shortcut suite; a third-party windowing library; custom third-party plugin windows. Desktop browser resize/zoom must keep controls recoverable and preserve pinned scope; there is no implicit single-maximized-panel mobile mode. The same reusable panels remain suitable for a future mobile adaptation.

## 10. Interaction timelines (frozen definitions for downstream runtime verification)

Each timeline lists inputs and the observable outcomes `01-window-*` producers must implement and that `01-workspace-runtime`/phase gates exercise in a real browser. None has been executed at P00; this document defines them.

### 10.1 Positive timelines

- **PT1 Drag.** `pointerdown` on title bar (excluding interactive descendants) → `setPointerCapture` on the frame → `pointermove` updates geometry, clamped to viewport per NT3 clamp → `pointerup` releases capture and persists geometry (persist on gesture end, not per frame; `pagehide` best-effort flush). Dragging also raises/focuses the window.
- **PT2 Focus/stacking.** Activating an inactive window (click anywhere in it or taskbar) brings it to front, assigns the next z-index, and moves focus to its title bar; other windows' state and targets are unchanged.
- **PT3 Resize.** `pointerdown` on an edge/corner resize handle → capture → resize with per-entry `min_geometry` clamp (global default minimum 320×200 CSS px when unset) → commit+persist on `pointerup`.
- **PT4 Minimize/restore via taskbar.** Minimize hides the window (content unmounted by default), the taskbar entry remains and reflects state (`aria-pressed`); activating the entry restores and focuses the window.
- **PT5 Close.** Close unmounts the panel, releases its subscriptions, removes it from persisted layout; any backend job it displayed continues; the taskbar entry disappears.
- **PT6 Same-browser restore.** Reload → SSR shell → after hydration the stored layout is validated (§10.2 NT4 rules), survivors are reopened with clamped geometry and pinned scopes; no mutations, approvals or submissions are replayed.

### 10.2 Negative timelines

- **NT1 Interrupted drag/resize.** `pointercancel` or lost capture mid-gesture → capture released, geometry reverted to the last committed state (drag) or clamped (resize); no stuck capture, no stuck pointer styles.
- **NT2 Wrong drag origin.** `pointerdown` on buttons, inputs, selects, scrollbars, links or during text selection in the body never initiates a drag.
- **NT3 Bounds invariant.** After any drag, resize, viewport resize or zoom, every window retains a visible, hit-testable title-bar area of at least **64×32 CSS px** inside the viewport (frozen constant; producers may expose it as a CSS variable, the value is contractual). Offscreen stored coordinates are clamped at restore. Resizes below `min_geometry` are clamped, never break layout.
- **NT4 Hostile/corrupt restore.** Unknown `schema_version` major, unparsable JSON, unknown `panel_kind`, deleted/unauthorized scope objects → those entries are dropped with a visible, understandable notice; the desktop remains functional; no dynamic import from data; no action replay; secrets can never enter this path (§8 rule 1).
- **NT5 Global selector vs pinned scope.** With server-scoped windows open and an operation pending: changing the launcher/global server selection changes neither the open windows' scopes, nor the pending operation's target, nor child jobs; only subsequently opened windows use the new selection.
- **NT6 Close/minimize during run.** Closing or minimizing a window showing a running job does not affect the job; reopening reconnects to the same `run_id`; the same run opened in two windows submits nothing twice.

## 11. Good/bad descriptor examples (frozen fixtures for consumer tests)

**Good — server-scoped chat bound to a run** (§8 example above) passes schema v1 validation.

**Good — global settings view:**

```json
{ "schema_version": 1, "window_id": "5b2e…", "panel_kind": "model-settings",
  "scope": { "kind": "global" },
  "geometry": { "x": 40, "y": 40, "width": 640, "height": 400 },
  "presentation": "normal", "z": 1, "title_override": null }
```

Bad examples, each MUST be rejected as specified:

| Payload defect | Required behavior |
|---|---|
| `"panel_kind": "log-viewer"` not in registry | Drop/error view "unavailable panel"; no import attempt |
| `"scope": {"kind": "server"}` without `server_id` | Validation failure; window not opened |
| `"geometry": {"x": NaN, …}` / non-integers | Validation failure; entry dropped at restore |
| `"schema_version": 2` (unknown major) | Whole stored layout discarded; default workspace + notice |
| `"title_override": "token=sk-…"` or any secret/payload content | Must be impossible by construction: serialization layer rejects forbidden content classes (§8 rule 1); a violation is a severity-1 defect |
| duplicate `window_id` in one layout | Restore deduplicates deterministically (keep first, drop rest) |

## 12. Consequences and downstream obligations

- P01 producer cards implement this contract; their focused tests validate descriptor/state transitions, pointer behavior and registry validation against §§3–11. `01-workspace-runtime` assembles and supplies the real-browser feature scenario; P12 re-exercises the critical journeys.
- Feature cards (P03–P11) own one reusable panel + focused test + registry entry each, and must not duplicate drag/geometry/global-server logic (§2).
- If any executable code is added to this documentation-only card's scope, the card must be reclassified and gain a paired code review before dependency release (test policy). This card adds none.
- Doc-level acceptance of this ADR is checked by the coordinator artifact review and independently at the P00 gate; runtime UI validation is explicitly **not** claimed by this document.

## 13. Rejected alternatives

1. **Third-party windowing library now** — license/model/maintenance risks (§4.2) and a loss of ownership over the contract-tested pointer/focus behavior; revisit only via §4.4/§4.3 for in-panel widgets.
2. **SSR-rendered window layer** — hydration mismatch and cross-request state hazards; rejected in favor of §5.
3. **Odysseus code reuse** — prohibited by the agreed specification (inspiration only).
4. **Per-feature window implementations / route-owned features** — duplication and inconsistent interaction behavior; prohibited by §2.
5. **Cross-device sync / mobile parity now** — deferred by Q8; not silently re-scoped by implementation convenience.

## 14. Explicitly open after this freeze (with owners)

- Exact dependency versions and build tooling pins — `01-web`.
- Design token values (contrast, focus ring, target sizes) — `01-ui-tokens`.
- Accessible shared control primitives inventory — `01-ui-controls` (may trigger §4.4 evaluation).
- Registry/`PanelHost` producer implementation and its tests — `01-panel-registry`.
- Real-browser demonstration of coexistence, scope isolation, lifecycle, component reuse — `01-workspace-runtime`, verified at the P01 gate.
- Mobile/touch adaptation, cross-device sync, plugin windows — future authorized scope decisions.

## 15. Sources consulted at freeze (2026-10-12, documentation-level)

| Source | Verified content |
|---|---|
| svelte.dev/docs/svelte (incl. runes docs) | Svelte 5 runes API is the documented current major line |
| svelte.dev/docs/kit/$app-environment | `browser` build-time client check exported by `$app/environment` (alias `$app/env`) |
| github.com/sveltejs/svelte | MIT license (repo listing) |
| raw.githubusercontent.com/sveltejs/kit/main/LICENSE | SvelteKit MIT license text (read verbatim) |
| w3.org/WAI/ARIA/apg/patterns/dialog-modal/ | APG dialog pattern; non-modal dialog semantics (`role="dialog"`, `aria-modal="false"`) |
| github.com/nextapps-de/winbox (+ LICENSE) | Apache-2.0; vanilla JS window manager (rejection evidence) |
| github.com/golden-layout/golden-layout (+ LICENSE) | MIT; docked/tab layout manager (rejection evidence) |
| github.com/huntabyte/bits-ui + npm `bits-ui` (package.json `"license": "MIT"`, v2.x, Svelte export condition) | MIT, Svelte 5-native, actively published (deferral evidence) |
| github.com/melt-ui/melt-ui | MIT headless Svelte builders (deferral evidence) |

Limitation: all library evidence is documentation-level; no dependency was installed or executed on the coordinator host (not permitted for this card). Version pinning happens at `01-web`.

## Change control

This document is frozen at P00. Post-freeze changes require explicit consent reconciliation per `mai_EXECUTION.md` §3 (pause affected dispatch, record the proposed change, obtain approval, produce a new manifest); silent dispatch-time edits are prohibited. Minor/patch bumps of this document follow the same rule.

## Provenance

Inputs read in full by the author for this freeze: `BRIEF.md` (this card), `IDEA.md`, `mai_CONTRACTS.md`, `mai_UI_WORKSPACE.md`, `mai_EXECUTION.md`, `mai_TEST_POLICY.md`, `mai_COORDINATOR.md`. Author check receipts and limitations: `mai_evidence/00-ui-contract/` (programme evidence root, outside the worktree).

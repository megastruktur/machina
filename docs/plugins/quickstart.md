# Plugins and skills — operator/contributor quickstart (P12)

How capabilities are added to a running MAI instance: native Python plugins,
external MCP servers, and portable skill packages. Everything here is
verified against the real surface by `tests/docs/test_docs_consistency.py`.

Anchors: `docs/architecture/contracts.md` **C03 rules 1/2/6/7** (frozen
descriptor vocabulary, credential references, no auto-grant, trusted-code
boundary). Producers: `mai.plugins.native`, `mai.plugins.mcp`,
`mai.plugins.config` (`backend/src/mai/plugins/`), skills loader
`mai.skills.load`.

## 0. The model (read first)

- **Registration ≠ authorization.** A registered capability is declarative
  metadata; it runs only through the capability dispatcher after policy,
  grants and (where required) an explicit approval. Nothing registers,
  enables or grants implicitly.
- **No in-app installer in v1.** Native plugins are discovered from
  *already-installed* distributions (`importlib.metadata` entry points);
  there is no install/uninstall endpoint and no runtime pip install.
  Adding one is an operator installation (§2), then a composition reload.
- **Validation is not sandboxing.** Loading a native plugin executes
  trusted code by design (C03 rule 7). Only install distributions you
  trust as you would any other root-level tooling.
- **Credentials are references, never values.** Stored configuration holds
  credential *names*; values resolve at effect time from the fail-closed
  `.env` source (C03 rule 6). Config keys that look like credentials are
  rejected outright (`inline_credential_forbidden`).

## 1. What the web UI gives you

The workspace plugins panel (`frontend/src/lib/components/PluginConfigForm.svelte`,
registered by `frontend/src/lib/workspace/entries/09-web-plugin-forms.ts`)
lists the plugin catalog (`GET /api/v1/plugins`), edits one plugin's
connection config and its per-server grant bindings, and triggers bounded
MCP discovery — session, exact-origin and CSRF handled by the app. The
`curl` equivalents below show the raw surface (login first, then
`GET /api/v1/auth/csrf`, then send double-submit header `x-csrf-token`
carrying the `mai_csrf` cookie value on every mutation).

## 2. Native Python capability plugins

One installed distribution exposes plugins through the entry-point group
**`mai.capabilities`**; one entry point is one plugin:

- entry point **name** = plugin name (the configuration/disable key);
- entry point **value** = a dotted module path; the module declares:
  - `CAPABILITY` — the frozen C03 rule 1 descriptor vocabulary
    (`identifier`, `version`, `config_schema`, `input_schema`,
    `output_schema`, `execution_environments`, `effect_class`,
    `resource_scope`, `required_grants`); unknown fields are rejected, so
    a descriptor cannot smuggle a handler path or a credential value;
  - `execute(call)` — the trusted handler (sync or `async`), receiving a
    `CapabilityCall` and returning an `ExecutorResult`;
  - optionally `CREDENTIAL_REFERENCES` — credential *names* the handler
    needs; resolution happens at effect time, never at load.

Minimal module (the shape exercised by `backend/tests/test_native_plugins.py`):

```python
from mai.capabilities.dispatch import ExecutorResult

CAPABILITY = {
    "identifier": "example.greet",
    "version": "1.0.0",
    "config_schema": None,
    "input_schema": {"type": "object",
                     "properties": {"name": {"type": "string"}},
                     "required": ["name"], "additionalProperties": False},
    "output_schema": {"type": "object"},
    "execution_environments": ["controller"],
    "effect_class": "read",
    "resource_scope": [],
    "required_grants": ["example.greet"],
}

async def execute(call):
    return ExecutorResult(ok=True,
                          output="hello %s" % call.payload["name"])
```

Ship it with a distribution metadata file declaring the entry point:

```text
# <dist>.dist-info/entry_points.txt
[mai.capabilities]
greet = my_package.greet
```

Install into the backend environment the composition uses — on a compose
deployment, the `mai-backend` image build (`backend/Dockerfile`); for local
development, `backend/.venv` (`uv pip install …` into it is an operator act,
outside the locked `uv sync --frozen` lane). Then reload the composition
(restart the worker/API process); discovery runs once per composition.
Installing never grants: add the capability to a server's grant set through
the policy/grants API or UI — or the tool simply never dispatches.

Disable a plugin without uninstalling: `enabled: false` in its plugin
config (§3). A disabled or invalid plugin is skipped at load; one rejected
plugin never prevents the others. A second plugin claiming an
already-bound capability identifier is rejected
(`capability_identifier_conflict`) — no silent shadowing.

## 3. Plugin connection config (both kinds)

Canonical store layout under the data root (card `09-plugin-store`, C02
discipline: atomic temp+replace, revision triple, stale-revision refusal):

```text
<data-root>/plugins/<plugin>/config.yaml            # {schema_version, plugin, enabled, credential_refs, config}
<data-root>/plugins/<plugin>/bindings/<server_id>.yaml  # {schema_version, plugin, server_id, capabilities}
```

API surface (all under `/api/v1/plugins`; mutations need session + origin +
CSRF):

| Call | Purpose |
|---|---|
| `GET /api/v1/plugins` | List installed, configured and granted plugins |
| `GET /api/v1/plugins/{plugin}/config` | Read one stored connection document |
| `PUT /api/v1/plugins/{plugin}/config` | Validate against the declared schema, then atomically commit (body carries `expected_revision`; stale ⇒ `409`) |
| `DELETE /api/v1/plugins/{plugin}/config` | Revision-checked deletion |
| `GET /api/v1/plugins/{plugin}/schema` | Read the declared config schema |
| `GET /api/v1/plugins/{plugin}/bindings` | List the plugin's server grant bindings |
| `PUT /api/v1/plugins/{plugin}/bindings/{server_id}` | Grant listed capabilities to ONE server explicitly |
| `DELETE /api/v1/plugins/{plugin}/bindings/{server_id}` | Revoke that binding (revision-checked) |
| `POST /api/v1/plugins/{plugin}/discovery` | One bounded MCP discovery run (§4) |

A binding write requires an existing connection config (otherwise `409
conflict`, `connection_config_missing`); installing or configuring a plugin
never creates a binding — grants are separate, explicit, revision-checked
acts (no installation-time auto-grant, C03 rule 7).

## 4. External MCP servers

An MCP server is a plugin connection config (`config` mapping validated
against the frozen schema; `credential_refs` names only). Frozen v1
transports: `stdio` (fields `command`, `args`, `cwd`) and
`streamable_http` (field `url`); optional `connect_timeout_s`,
`call_timeout_s`, `max_tools`. Unknown fields are rejected
(`additionalProperties: false`).

```json
PUT /api/v1/plugins/buildtools/config
{"expected_revision": 0,
 "enabled": true,
 "credential_refs": [],
 "config": {"transport": "stdio", "command": "/usr/local/bin/buildtools-mcp",
            "args": ["--safe"], "max_tools": 32}}
```

Then bind explicit tools to one server
(`PUT /api/v1/plugins/buildtools/bindings/<server_id>` with the capability
list) and run bounded discovery
(`POST /api/v1/plugins/buildtools/discovery`): initialize + `tools/list`,
page- and deadline-limited; each tool maps through the frozen descriptor
vocabulary as `mcp.<plugin>.<tool>`; malformed tools become value-free
rejection records, never registrations. **Connecting ≠ enabling**: only
bound tools are registered; ungranted tools are reported by name and stored
nowhere. A reconnect never re-lists tools, so a server cannot inject new
capabilities mid-flight. Prompts, resources, sampling and elicitation are
never declared to the server; tool *output* is untrusted data that reaches
the model only inside the dispatcher's redact-then-bound output boundary.

## 5. Portable skill packages

Skills are read-only instruction packages below the shared namespace of the
data root (card `09-skills`; installing one never grants tools):

```text
<data-root>/shared/skills/<skill>/skill.yaml        # manifest (frozen, strict)
<data-root>/shared/skills/<skill>/instructions.md   # bounded instructions
<data-root>/shared/skills/<skill>/<reference>       # optional declared files
```

Manifest vocabulary (`skill.yaml`): `schema_version: 1` (exactly 1),
`skill` (must equal the directory name), optional `title` (≤200 chars),
`description` (≤512 chars), `requires_capabilities` (capability slugs),
`references` (relative paths inside the package). Unknown fields rejected.
Every read yields `(name, bytes, sha256)` and a deterministic
`package_sha256` over manifest + documents — the audit label carried into
context assembly.

Read them back through `GET /api/v1/skills` (catalog) and
`GET /api/v1/skills/{skill}` (package + prerequisite binding report).
Skills are inert data: declaring `requires_capabilities: [deploy.app]`
documents a prerequisite; it does not create a grant.

## 6. Limits (disclosed)

- No CLI and no HTTP route executes a capability ("run this tool for me"
  backdoors are deliberately absent); the only invocation path is the
  dispatcher behind policy/grants/approval.
- No hot reload: composition-time discovery only; changing the plugin set
  or the disabled list means recomposing (restart the process).
- Import failures report the exception *type name* only — never its
  message (value-free discipline).
- MCP tool descriptions are dropped at mapping (the descriptor vocabulary
  has no free-text description field); the model-facing summary is
  generated from declarative fields only.
- There is no plugin marketplace, signing or sandbox in v1; trusted-code
  installation is the operator's responsibility (C03 rule 7).

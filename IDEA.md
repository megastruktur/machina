# MAI — AI Administration Control Panel

## 1. Product idea

MAI is a self-hosted, AI-powered administration control panel that understands the servers it manages, installs and maintains applications, investigates problems, and keeps authoritative operational documentation.

It combines a dashboard and chat with a trusted, delegated administrator capable of performing real operations. It is not merely a monitoring dashboard with a chatbot, nor an application catalogue limited to predefined installation templates.

The core workflow is:

**Observe → Understand → Plan → Execute under configured policy → Verify → Document and remember.**

An approval step is inserted where policy requires one. Autonomous execution is a first-class mode, not an exception.

### Audience and licensing

- Initial user: the project's owner, testing on their own server and potentially replacing their existing Hermes devops agent workflow.
- Intended audience afterward: other self-hosting administrators.
- Intended project license: **MIT**. Dependency and bundled-service licenses remain their own and must be respected.
- Initial deployment and validation target: **Arch Linux**, managing Docker applications and the actual host.
- Future direction: additional Linux distributions, remote servers, and Windows targets.

### Status of this document

This is the agreed product specification and architecture direction, not a claim that the software exists or has passed integration tests.

**Agreed** requirements and selected technologies are distinguished from **proposed implementation details** and **open decisions**. Exact file formats, SDK contracts, service topology, release milestones, and dependency versions still require implementation design and validation.

## 2. Selected technology and delivery model

| Area | Decision |
|---|---|
| Backend | Python with FastAPI |
| Web frontend | SvelteKit; dashboard and chat |
| Agent runtime | PydanticAI |
| Database | PostgreSQL in the default stack |
| Model access | Configurable OpenAI-compatible endpoints |
| Deployment | Dockerized application, delivered through Docker Compose |
| Managed-host execution | SSH, including host privilege escalation when authorized |
| Search | SearXNG bundled as a Compose service |
| Operational memory | Hindsight integration |
| Native extensions | Python plugins |
| External tool integration | MCP servers |
| Initial credentials source | `.env` files |
| Additional credentials | Pluggable providers; 1Password is an optional plugin |
| Canonical management data | MAI-owned records and documentation, grouped by server ID |
| Future interfaces | Messenger adapters without coupling jobs to the web UI |

PydanticAI is the agent runtime, **not the complete durable job system**. PostgreSQL does not select a workflow engine or vector-index implementation. Those decisions remain open.

## 3. Central controller, explicit managed servers

MAI runs as a central Dockerized application. A managed server is an explicit profile, not implicitly the container or machine where the controller happens to run.

The installation host is one managed server and uses the same SSH execution interface intended for future remote targets.

Example future topology:

```text
MAI controller — Docker Compose
├── megaserver — SSH → Arch Linux
│   ├── Server instructions and capabilities
│   ├── Applications, documentation and memory
│   └── Monitoring, jobs and execution history
└── solarburn — SSH → Windows
    ├── Server instructions and capabilities
    ├── Applications, documentation and memory
    └── Monitoring, jobs and execution history
```

These names illustrate the intended model. They do not hard-code connection details or assert that Windows or remote enrollment is implemented.

### Release boundary

- **V1:** validate the installation host on Arch Linux, with server-profile-based data and execution from the beginning.
- **V2+:** remote enrollment, multiple managed hosts, Windows support, and broader operating-system coverage.
- Fleet-wide orchestration, coordinated rollouts, bastion support, and cross-server workflows are not required for v1.

Preparing the data model for multiple servers does not mean building the entire fleet-management product immediately.

### Server identity and profile

Every managed server has a stable `server_id`. Names and addresses can change without changing its identity.

A server profile needs to describe:

- Display name and connection endpoint.
- SSH identity and credential references.
- Operating system and actual execution environment.
- Shell, path conventions, and privilege-escalation strategy.
- Enabled capability sets and applicable policy.
- Adopted applications and relevant external deployment/documentation locations.
- **An administrator-editable system prompt addition describing that server's specifics.**

Exact schema and filenames are not yet fixed.

### Per-server system prompt addition

This is mandatory, not an optional note field. It may explain:

- What the server is used for.
- Local conventions, unusual service arrangements, and operational constraints.
- Where to consult existing documentation when needed.
- Preferred maintenance procedures and known caveats.

Server instructions accompany its chat sessions, scheduled jobs, investigations, and delegated tasks. A subagent working on Solarburn must not inherit Megaserver's operating-system assumptions.

Administrator-controlled prompt additions are distinct from retrieved documents, logs, and web pages. Retrieved content is evidence, not privileged instructions. Prompts describe context; structured policy controls permissions.

### Server scoping

Server-specific applications, observations, documentation chunks, incidents, jobs, execution history, and operational memories must retain their server identity.

Retrieval must be scoped by the application, not merely by asking the model to remember which server is selected. Shared knowledge is explicitly shared and distinguishable from a server's installation details.

A job binds its target when created. Changing the selected server in the dashboard must not redirect queued or running work. Delegated tasks retain an explicit target and authorized scope.

### SSH does not make operating systems interchangeable

SSH is the common transport. Platform capabilities still differ:

- Linux: Linux shells, service managers such as systemd, package managers, and sudo.
- Native Windows: PowerShell, Windows services, and Windows-specific privilege handling.
- WSL: a separate execution environment, not a synonym for native Windows.

Enrollment must identify the actual environment. MAI must not assume that any SSH endpoint supports Linux commands, sudo, or unattended Windows elevation.

## 4. Host installation and execution

### Simple installation

The intended installation experience is one automated, host-authorized setup flow that provisions the necessary SSH connection and starts the Docker stack. Administrators should not have to manually assemble SSH accounts, keys, permissions, and container-to-host connectivity.

The installer must make requested authority explicit. The exact bootstrap implementation and account lifecycle remain design work.

### Real host operations

Host commands run through SSH on the selected target. Running sudo inside MAI's container is not a substitute for host administration.

Supported administration must accommodate:

- Broad passwordless sudo for a trusted-admin configuration.
- Narrower grants where the administrator chooses them.
- Password-required host access where configured, with a secure credential-entry path when necessary.

A password-required flow must identify whose credential is required; a password-locked service account cannot authenticate merely because a UI asks for a password. Passwords must not be exposed in command arguments, transcripts, or agent prompts.

### Lifecycle limitations

SSH transport alone does not provide durable execution or reliable process-tree cancellation. Losing a connection does not establish whether a command stopped, failed, or completed.

The command-supervision design must account for controller restarts, Docker restarts, SSH loss, target reboot, and operations that disrupt networking. A successful request submission is not proof that the target reached the desired state.

Privileged-container and custom-native-executor approaches were researched but are not selected v1 execution transports.

## 5. Trusted administration and policy

The agent is a **trusted delegated administrator**. When granted authority, it may install software, restart services, change host configuration and networking, reboot a server, perform upgrades, and remove applications or data.

These actions are not permanently prohibited or universally gated by mandatory human confirmation. Their availability and approval requirements are controlled through configurable policy.

### Capability grants and execution modes

- Tool sets are explicitly enabled or disabled.
- **Ask mode:** require approval according to the configured policy.
- **YOLO / delegated mode:** execute within the granted scope without repetitive confirmation.
- Dangerous or sensitive actions may receive stricter policies if the administrator wants them.
- Changing execution mode must not silently grant additional tools or credentials.

Policy must be enforced at MAI's mediated tool/execution boundary, not only expressed in a system prompt. Subagents cannot bypass it by delegating again.

### Important trust boundary

Unrestricted shell and root access are broad authority. Once granted, MAI cannot promise OS-enforced confinement to selected applications merely by hiding other tools or writing restrictive prompts.

The distinction is deliberate:

- MAI enforces its configured grants and approval decisions.
- Administrator-granted root remains capable of broad system changes.
- Auditability and verification remain necessary even when approval is not.

Policy language, grant granularity, and exact approval UI remain open design decisions.

## 6. Application management and catalogue

### Inventory and adoption

Maintain a structured inventory/database of discovered and managed software, including its installation method, version, location, and relevant services or containers.

Discovery must not silently imply permission to maintain an application:

1. Discover existing installations.
2. Present them to the administrator.
3. Explicitly select which applications MAI may maintain.

Existing deployment files may stay where they are. Adoption must not require moving a Compose project into MAI's directory.

### Lifecycle operations

The initial management focus is Docker applications:

- Install and configure.
- Inspect status, health, and logs.
- Update and verify the result.
- Remove with explicit handling of persistent data according to policy.
- Maintain associated records and operational documentation automatically.

Host shell access remains available when granted, including for prerequisites and applications without a prebuilt integration. Native package-manager coverage can expand after the Docker-first foundation.

### Catalogue philosophy

Prefer a small **maintained catalogue** over a large library of unverified launch templates.

A maintained integration should describe installation, configuration, data locations, health checks, update considerations, removal semantics, and applicable backup/recovery procedures. Do not promise rollback where the underlying operation cannot support it.

For software outside the catalogue, the agent can research and execute an installation using its enabled capabilities. Reusable recipes and skill improvements are a possible extension; automatic self-publication is not yet specified.

## 7. Monitoring and notifications

The full product includes the following capabilities. Their individual depth and delivery order beyond the initial workflows remain to be planned.

| Capability | Purpose |
|---|---|
| System monitor | Explain resource usage, capacity, health, and changes over time |
| Network monitor | Inspect connectivity, interfaces, ports, traffic-related evidence, and faults |
| Security monitor | Surface security-relevant configuration, exposure, findings, and events |
| Scheduled-task monitor | Inspect cron jobs, systemd timers, and later other OS scheduling mechanisms |
| Running-process monitor | Inspect processes and resource use; connect them to applications and incidents |
| Application/service monitoring | Correlate deployment health and logs with host observations |
| Scheduled notifications | Report updates, maintenance needs, job outcomes, and other configured events |

Monitoring data should be available both in the dashboard and as explicit chat/agent context.

Scheduled-task monitoring is distinct from MAI's own agent-job scheduler. Security monitoring is not a claim of complete protection, compliance certification, or an autonomous penetration-testing platform.

Routine collection and notifications should not depend on an LLM being available. The agent interprets and acts on observations; it is not required to generate every observation.

The first interface is the web application. Messenger-based notifications and control are future adapters.

## 8. Agent runtime, models, and delegation

### Runtime

Use **PydanticAI** for agent interactions. Keep MAI's capability, policy, server-profile, and job contracts independent of framework-specific internals where practical.

### OpenAI-compatible connections

Administrators configure a base URL, token, and additional applicable settings. Local and remote endpoints are both valid; an external cloud provider is not mandatory.

A proposed configuration model separates:

- **Connection:** base URL, credential reference, timeouts, TLS and optional header settings.
- **Model profile:** model identifier, context/output limits, sampling or reasoning settings where supported, and bounded provider-specific overrides.

Exact settings and schema remain to define. OpenAI compatibility does not imply universal support for tool calls, streaming, structured outputs, Responses API features, or reasoning parameters. Configuration and validation must reflect actual endpoint support.

Token values and secret headers belong to credential handling, not ordinary shareable profile data. Intentionally unauthenticated local endpoints should not require storing a fake real credential.

### Mandatory delegation

A v1 agent must be able to delegate work to subagents. This is separate from running several unrelated background jobs.

Examples include delegating log analysis and web research during one investigation, then combining their evidence.

The intended delegation contract includes:

- Explicit parent/child relationships and task objectives.
- Server context and relevant server prompt additions.
- Bounded context transfer rather than indiscriminately copying every transcript.
- Child capability grants no broader than the parent's authorized scope.
- Visible progress, findings, failures, and final handoff.
- Defined cancellation and failure propagation.
- Limits on concurrency, nesting, and aggregate resource/model usage.

Exact limits, profile design, and child-model selection are open. Permanent named specialist personas are not required by the specification.

Concurrent investigations may read independently. Conflicting mutations to the same server resource need coordination across all jobs and subagents, not separate locks per conversation.

### Easy local model setup

The product should help administrators install and use local models, taking inspiration from Odysseus's user experience without reusing its code.

Specific inference backends, hardware selection, GPU support, model download handling, and completely offline operation remain design work. MAI must not bundle a particular large model as an unstated prerequisite.

## 9. Persistent jobs and scheduling

MAI is an always-on daemon with agent jobs that run asynchronously and independently of an open chat, browser connection, or interactive request.

### Required foundation

- Launch a job from chat or the dashboard.
- Schedule agentic work.
- Persist its objective, target server, progress, execution evidence, and outcome.
- Support delegated child work.
- Expose status and results after the original client disconnects.
- Define cancellation, failure reporting, restart recovery, and continuation semantics.
- Provide notification hooks independent of the web transport.

Persistent job state, command supervision, and the agent conversation are related but different responsibilities.

### Recovery semantics

Do not claim exactly-once host side effects. A database receipt cannot atomically cover arbitrary shell commands or remote changes.

When an outcome is uncertain:

1. Inspect actual target state.
2. Determine whether work already completed, partially completed, or failed.
3. Continue or compensate where supported.
4. Escalate when safe recovery cannot be determined.

A missing completion receipt is not permission to blindly repeat a destructive command. Prefer convergent/idempotent operations where possible.

### Later conditional jobs

Leave an architectural interface for **conditional jobs defined through YAML configuration and scripting**. They are a later capability, not a requirement to build an unrestricted event automation engine in v1.

The durable engine, scheduler library, host supervisor, checkpoint design, and safe pause/steering UX remain open. PostgreSQL is accepted; overlapping engines and competing run-state authorities should be avoided.

## 10. Canonical data and documentation

### Source of truth

MAI is authoritative for management records and operational documentation. The controller's persistent data root defaults to **`~/.mai/`**, and server-specific data is grouped by server ID.

Existing deployment files may remain elsewhere and be referenced by MAI. Existing READMEs can be consulted or imported when instructed, but are not a competing authority that must be bidirectionally synchronized.

On a fresh system, the agent documents the installation directly inside MAI.

Canonical desired configuration and documentation do not overrule observed runtime facts. If the system differs from its records, MAI must expose and reconcile the discrepancy.

### Proposed filesystem organization

This is an illustrative layout, not a finalized file-format contract:

```text
~/.mai/
├── servers/
│   └── <server-id>/
│       ├── profile.yaml
│       ├── prompt.md
│       ├── docs/
│       ├── runbooks/
│       └── applications/
│           └── <application-id>/
│               ├── manifest.yaml
│               └── docs/
└── shared/
    ├── skills/
    └── catalogue/
```

The data root is on the controller's persistent storage and mounted into MAI. Remote machines, including Windows targets, do not need a POSIX `~/.mai/` directory merely to be managed.

### Documentation requirements

- Retrieve relevant upstream software documentation and generate installation-specific documentation.
- Automatically update operational documentation after work, recording verification results and uncertainty honestly.
- Keep source documents inside MAI's data root.
- Chunk and vector-index documents.
- Expose retrieval tools to the agent and delegated workers.
- Preserve server/application scope, source provenance, and relevant version information.
- Make documentation usable interactively through the UI and chat.
- Keep the index rebuildable from source documents.

A report should distinguish confirmed findings, hypotheses, attempted actions, and verified outcomes. Failed work is documented as failed, not silently rewritten into a successful runbook.

### Database versus files

PostgreSQL stores runtime records and indexed metadata. The implementation must explicitly define which records are file-canonical, database-canonical, or derived views.

There must not be two independently writable sources of truth for the same configuration. Backup and recovery need to account for both the database and persistent documents/artifacts.

### Optional Git and GitOps

Administrators may place MAI-managed files under Git, but Git is not required to operate the product.

- Supply appropriate ignores for `.env`, credentials, and runtime data.
- Preserve referenced external deployment repositories.
- Prefer recorded, reviewable changes when using GitOps.
- Permit incident intervention under policy, followed by reconciliation/documentation rather than silent configuration drift.

Exact commit and deployment workflows remain configurable design work. A Git ignore rule is not a substitute for secret handling or inspection before publication.

## 11. Memory

Use **Hindsight** for operational memory: previous incidents, decisions, preferences, and lessons that can help future work.

Maintain clear distinctions:

| Information | Role |
|---|---|
| Live observations | What is true on the managed server now |
| Structured inventory and job records | Known installations, work state, and execution history |
| Documentation retrieval | Sourced software knowledge and authoritative operational documents |
| Operational memory | Past decisions, incidents, and lessons |

A remembered fact is not proof of current configuration. Server-specific memories remain scoped; reusable knowledge may be shared explicitly.

Hindsight packaging, memory lifecycle, embedding models, and the exact documentation vector store are not yet selected. Its own state must not become an accidental replacement for MAI's canonical documents or job database.

## 12. Search and Deep Research

Bundle **SearXNG as a Docker Compose service** and expose it through a pluggable search capability available to authorized agents and subagents.

The proposed default is an internal service without a published host port. An external SearXNG endpoint may be supported as a configuration alternative; it must not replace the agreed bundled experience.

Separate search from page fetching/extraction:

- Search discovers sources and returns structured results.
- Retrieval reads selected sources and retains citations/provenance.
- Deep Research combines those sources with the selected server's documentation, inventory, history, and relevant live evidence.

Self-hosted metasearch still forwards queries to configured upstream engines. Minimize and sanitize outbound context; credentials, private logs, and sensitive host identifiers must not be indiscriminately copied into search queries.

Web content is untrusted evidence. It cannot authorize commands, enable capabilities, or override server policy.

SearXNG deployment settings, page-extraction tooling, research budgets, and source-quality rules remain implementation decisions.

## 13. Pluggable capabilities and skills

All agent-facing tools should fit a pluggable capability architecture, including built-in functions. Plugins must not require extending the core agent loop for each integration.

### Agreed extension entry points

- **Python plugins:** native tools, integrations, and credential providers.
- **External MCP servers:** existing or separately hosted tools.
- **Schema-generated configuration forms:** configure extensions without bespoke frontend work.
- **Custom dashboard pages/widgets:** later, not required for the initial plugin system.

The public SDK, manifest, compatibility rules, discovery process, and isolation model remain open.

### Capability responsibilities

A capability needs an explicit interface for its configuration, inputs/outputs, and permission requirements. Host-facing tools must understand their target and supported OS environment.

MCP is an integration protocol, not an authorization boundary. Connecting a server does not imply enabling all its tools.

### Skills

Skills provide procedural instructions and knowledge for using enabled capabilities. Installing a skill must not silently grant tools or privileges.

Portable file-based skills are the preferred direction, but the exact format, Hermes compatibility, repository installation, and agent-authored skill update policy remain undecided. Script-based capabilities can be added later if selected.

## 14. Credentials and sensitive data

`.env` files are the primary credential source. Administrators must not need a 1Password account to use MAI.

**1Password is an optional plugin**, behind a provider interface that allows other secret stores later.

The preferred architecture resolves credentials outside model context and injects them into an authorized execution/request. Store references in profiles rather than copying values into prompts, documents, or job specifications.

Protect credentials from accidental inclusion in:

- Chat transcripts and model context.
- Logs, traces, tool previews, and execution records.
- Documentation, vector indexes, and long-term memory.
- Web research queries and third-party requests unrelated to their purpose.
- Git history and exported artifacts.

The exact policy for exceptional raw-secret access remains open. Broad shell/root authority is not a guarantee that a model can never encounter a secret; the product must state that boundary honestly.

Do not index credential files as documentation. Generated credentials, password entry, retention, and provider-specific storage workflows require a deliberate implementation design.

## 15. Dashboard, chat, and future interfaces

The first user interface is a SvelteKit web application combining **dashboard and chat**.

The dashboard should expose servers, managed applications, observations, documentation, ongoing jobs, findings, and actions requiring attention. Chat should make it possible to attach any relevant capability's data as context, including:

- A selected server or application.
- Monitoring observations and process information.
- Logs and job execution evidence.
- Documents, incidents, and operational memories.
- Research findings and selected sources.

Target identity and delegated activity must remain visible. Long operations should show progress rather than requiring the user to infer that an agent is still working.

The architecture must allow later messenger-based interaction and notifications. Job execution must not depend on a particular frontend session or transport.

Exact navigation, real-time event protocol, interactive steering, and custom plugin UI contracts are not yet fixed.

## 16. Reference workflows and outcome criteria

These scenarios define the intended usefulness of the product. They are acceptance targets, **not executed tests**.

### A. Install and maintain an application

Request: **Install Odysseus on the selected server.**

Expected workflow:

1. Load the server's profile, instructions, adopted scope, and relevant documentation.
2. Inspect prerequisites and existing deployments.
3. Research installation details where the catalogue does not suffice.
4. Prepare and execute installation under the configured policy, using host SSH where needed.
5. Resolve credentials through the configured provider.
6. Verify application health and relevant access paths.
7. Record deployment location, version, data locations, and maintenance information inside the server's MAI records.
8. Report verified results, outstanding issues, and any follow-up work.

It is not complete merely because a command exited successfully or a container was created.

### B. Investigate model failures through LiteLLM

Request: **Investigate why a deployed model is failing through LiteLLM.**

Expected workflow:

1. Identify the server, deployment, endpoint configuration, and relevant history.
2. Resolve needed API credentials from `.env` or the optional 1Password provider.
3. Consult correct API documentation and inspect appropriate endpoints.
4. Collect bounded, relevant logs and observations.
5. Delegate evidence collection or research when useful.
6. Search for explanations without leaking sensitive server context.
7. Distinguish confirmed causes from hypotheses; do not infer the cause solely from a similar online report.
8. If authorized, apply a fix and verify its effect.
9. Save a server-scoped incident report and update relevant operational documentation and memory.

### C. Explain an existing server

Request: **Explain what runs here and how it fits together.**

Expected outcome: a server-specific, evidence-backed overview of applications, services, dependencies, deployment locations, and relevant operational caveats, distinguishing discovered facts from unknowns. Discovery alone must not adopt applications for maintenance.

### D. Scheduled delegated work

A scheduled maintenance or investigation job continues without an open browser, exposes child work and progress, retains its outcome, and handles interruption without blindly repeating uncertain mutations.

## 17. Scope and sequencing

### Firm initial foundation

- Dockerized FastAPI/SvelteKit controller with PostgreSQL.
- PydanticAI and configurable OpenAI-compatible models.
- SSH-based administration of the initial Arch target.
- Server-scoped records and mandatory per-server prompt additions.
- Docker-first discovery, explicit adoption, and lifecycle workflows.
- Opt-in capabilities and configurable ask/delegated execution.
- Native plugin and MCP integration approach.
- `.env` credentials and extensible credential providers.
- Independent asynchronous and scheduled agent jobs, with mandatory delegation.
- Canonical MAI documentation with chunking, vector retrieval, and automatic updates.
- SearXNG bundled in Compose.
- Dashboard and chat supporting install, investigate, and explain workflows.

Hindsight, the monitoring capabilities, local-model installation, catalogue breadth, and the optional 1Password plugin are agreed parts of the product. Their precise delivery milestones and depth still need planning; they are not removed from scope by not assigning them all to the first development slice.

### Explicitly later

- Remote/multihost enrollment and fleet UX.
- Windows and broader OS support.
- Conditional jobs through YAML and scripting.
- Messenger integrations.
- Custom frontend plugin pages/widgets.

### Not implied

- Mandatory Git or mandatory 1Password.
- Mandatory cloud models or a requirement to download a large model for setup.
- Unconditional approval prompts for every privileged action.
- Universal rollback or exactly-once external execution.
- Every host exposing the same shell or package/service APIs.
- A fully offline product guarantee.
- An enterprise multi-tenant/RBAC product or a complete security platform.
- Implementing all researched host transports or agent frameworks.

## 18. Open design decisions

The following remain intentionally unresolved:

1. Durable job engine, scheduler, host supervision, and restart/cancellation semantics.
2. Concrete server/application schemas, file/database authority boundaries, and migrations.
3. Plugin SDK, manifests, version compatibility, execution isolation, and installation lifecycle.
4. Policy language, approval granularity, live steering, and emergency intervention UX.
5. Subagent profiles, model selection, budgets, nesting, context transfer, and failure propagation.
6. Exact OpenAI-compatible request features and configuration validation.
7. Hindsight packaging, embedding providers, documentation vector index, and retrieval/reranking strategy.
8. Local model serving backends, hardware support, downloads, and offline expectations.
9. SSH bootstrap, key rotation, account lifecycle, host-password flow, and uninstall/recovery.
10. Backup/restore procedures across database, files, memory, and credential references.
11. Monitoring retention, notification policy, catalogue entries, and detailed feature sequencing.
12. Native Windows/WSL enrollment and privilege semantics for the later multi-OS phase.
13. Portable skill format, import of existing Hermes knowledge, and autonomous skill updates.
14. Web authentication/session security and access exposure suitable for a privileged administration panel.

These are implementation questions, not invitations to replace the selected stack or weaken the agreed product requirements.

## 19. References and research status

- [PydanticAI](https://ai.pydantic.dev/) — selected agent runtime.
- [Hindsight](https://github.com/vectorize-io/hindsight) — operational memory integration.
- [SearXNG](https://docs.searxng.org/) — bundled metasearch service.
- [Odysseus](https://github.com/odysseus-dev/odysseus) — inspiration for local model setup and integrated search **only; no code reuse**.
- [Hermes documentation](https://hermes-agent.nousresearch.com/docs) — reference for the existing workflow and possible future interoperability, not a selected backend dependency.

Agent-runtime and host-execution options were studied and independently reviewed before selecting PydanticAI and SSH. Reviews identified material caveats around privilege boundaries, replay safety, and job durability; those caveats are reflected here.

No MAI prototype, host-execution integration test, recovery test, or local-model reliability test has been completed as part of this idea-stage specification. Dependency versions must be selected and verified during implementation rather than copied from a research snapshot.

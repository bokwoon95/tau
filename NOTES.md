~/Documents/tau/ I plan to port this over to C++. Give me the high level architecture of the codebase and where I should start reading. I am looking to read and understand the codebase this way: "Files are written in such a way that each successive file in the list below only depends on files that come before it. This makes it easy to rewrite the codebase from scratch file-by-file, complete with working tests at every step of the way. Please adhere to this file order when submitting pull requests.". I think Python codebases are usually written like that. I need a file reading order, what each files does in the grand scheme of things and what it contributes to the overall codebase each step of the way

## 1. The architectural picture

Tau is a coding agent with three major parts:

| Package | Responsibility |
|---|---|
| `tau_agent` | The reusable agent: conversation types, tools, model interface, execution loop, harness, session history. |
| `tau_ai` | Implementations of the model interface: HTTP requests, streaming responses, provider-specific serialization. |
| `tau_coding` | The application: filesystem tools, configuration, project instructions, extensions, persistent sessions, CLI and TUI. |

**An important correction:** the documentation describes `tau_coding → tau_agent → tau_ai`, but the current source does not literally follow that import direction.

The actual foundational relationship is:

```text
                     tau_agent types and interfaces
                         ↑                 ↑
                tau_agent loop       tau_ai providers
                         ↑                 ↑
                         └── tau_coding ───┘
                                  ↑
                          CLI / RPC / TUI
```

For example, `tau_ai/events.py` re-exports events from `tau_agent/provider_events.py`, and `tau_ai/provider.py` re-exports interfaces from `tau_agent/provider.py`.

**Start with `tau_agent`, not `tau_ai`.**

### What happens when you submit a prompt?

```text
Frontend receives input
    ↓
CodingSession applies coding-application policy
    • expand skills/templates
    • assemble project context and system prompt
    • manage provider/model selection
    • manage persistence, retries and compaction
    ↓
AgentHarness manages conversation and execution state
    ↓
run_agent_loop()
    • request an assistant response
    • receive streamed model events
    • execute requested tools
    • append tool results
    • request another assistant response if needed
    ↓
ModelProvider implementation talks to the model API
```

Events travel outward to subscribers and frontends. Persistence also subscribes to harness events; it is not solely dependent on the frontend consuming them.

---

## 2. Can you get a strictly dependency-ordered file list?

**Mostly, but not literally for every existing Python file.** Python does not automatically imply an acyclic file dependency graph.

This repository has several exceptions:

1. **The provider catalog contains actual circular dependencies.**
   - `provider_catalog.py` defines types, then calls into `catalog_loader.py`.
   - `catalog_loader.py` imports those types.
   - Catalog loading also uses `models_dev.py` and `models_dev_store.py`; the latter imports the loader inside its refresh function.

2. **Some dependencies are deferred until a function executes.**
   - Built-in extension declarations lazily import the llama.cpp implementation.
   - Extension API theme access lazily imports TUI configuration.

3. **Some references are type-checking-only.**
   - `extensions/api.py` refers to `ExtensionRuntime` without importing it at runtime.
   - These are usually forward declarations in C++ rather than implementation dependencies.

4. **Package `__init__.py` files eagerly re-export higher-level modules.**
   - They are public API facades, not sensible starting points for an incremental port.

Therefore, the order below is a **dependency-first implementation/reading order**, with circular components explicitly identified. For a strict C++ rewrite, split those components into types, implementation, and composition/wiring.

All paths below are relative to:

```text
~/Documents/tau/src/
```

Within each section, read top to bottom; sections build on previous sections.

---

## 3. Reading order

### Stage A — Define the language of the agent

These files establish the contracts everything else uses.

| Order | File | Contribution |
|---|---|---|
| A1 | `tau_agent/types.py` | JSON value types used for tool arguments, metadata and arbitrary structured data. |
| A2 | `tau_agent/messages.py` | Conversation representation: user, assistant, tool-result and special messages; ordered content blocks; usage and timing; wire serialization. |
| A3 | `tau_agent/tools.py` | Tool definition, executor contract, cancellation interface and structured tool results. Defines what a tool *is*, not filesystem tools themselves. |
| A4 | `tau_agent/provider_events.py` | Canonical assistant stream: text/thinking/tool-call block boundaries and deltas, completion and failure. |
| A5 | `tau_agent/provider.py` | `ModelProvider` and cancellation contracts. Defines what the agent needs from a model implementation. |
| A6 | `tau_agent/events.py` | Higher-level execution events: agent, turn, message and tool-execution lifecycle. |

**What you now have:** a provider-independent vocabulary for conversations and execution.

For C++, this is the natural place for structs, tagged unions/`std::variant`, JSON codecs, and abstract interfaces.

**Read alongside:** `tests/test_agent_types.py`, `tests/test_pi_event_protocol.py`.

---

### Stage B — Build the reusable agent brain

| Order | File | Contribution |
|---|---|---|
| B1 | `tau_agent/tool_history.py` | Repairs incomplete or inconsistent tool-call/result history so a conversation can safely be replayed. |
| B2 | `tau_agent/loop.py` | The central model → tools → model state machine. Emits events, executes tools, handles terminal responses and drains steering/follow-up messages. |
| B3 | `tau_agent/harness.py` | Stateful wrapper around the loop: conversation ownership, subscriptions, cancellation, run exclusivity and message queues. |

**What you now have:** an agent that can run against any implementation of `ModelProvider`, using arbitrary tools, without knowing about coding projects or terminals.

The most important distinction:

- **`loop.py`** executes a run.
- **`harness.py`** owns state between runs.

**Read alongside:** `tests/test_tool_history.py`, `tests/test_agent_loop.py`, `tests/test_agent_harness.py`.

You can implement a tiny fake provider directly from the Stage A contracts. Tau’s existing implementation is `tau_ai/fake.py`, listed below.

---

### Stage C — Represent durable, branching conversations

This branch depends on message types, not on real provider implementations.

| Order | File | Contribution |
|---|---|---|
| C1 | `tau_agent/session/entries.py` | Durable history records: messages, model/thinking changes, compactions, branch summaries, labels and session metadata. |
| C2 | `tau_agent/session/tree.py` | Resolves parent relationships and the path to a selected history entry; validates tree structure. |
| C3 | `tau_agent/session/jsonl.py` | Serialization and parsing of history records, including legacy-format migration. |
| C4 | `tau_agent/session/storage.py` | Storage interface and JSONL/in-memory implementations; append/read operations and filesystem durability concerns. |
| C5 | `tau_agent/session/memory.py` | Replays history into `SessionState`: active conversation, selected model, labels, compaction and branch-summary effects. |

**What you now have:** persistent history that can branch and produce an active model context.

Important: **stored history is not identical to the messages currently sent to the model.** Replay, compaction and branch selection determine the latter.

**Read alongside:** `tests/test_session.py`.

---

### Stage D — Implement model streaming

First understand the common machinery, then individual providers.

| Order | File | Contribution |
|---|---|---|
| D1 | `tau_ai/events.py` | Compatibility/public facade for canonical provider events from Stage A. |
| D2 | `tau_ai/provider.py` | Compatibility/public facade for the model-provider contracts. |
| D3 | `tau_ai/env.py` | Provider transport configuration, authentication values and environment parsing. |
| D4 | `tau_ai/model_limits.py` | Runtime context/output limits and the interface for discovering them. |
| D5 | `tau_ai/model_catalog.py` | Runtime model descriptions and model-catalog discovery interface. |
| D6 | `tau_ai/http.py` | HTTP-client creation and proxy handling. |
| D7 | `tau_ai/http_errors.py` | Extracts useful provider errors from HTTP response bodies. |
| D8 | `tau_ai/tool_call_ids.py` | Makes tool-call identifiers portable across provider formats. |
| D9 | `tau_ai/openai_cache.py` | OpenAI cache-key and endpoint-related helpers. |
| D10 | `tau_ai/content.py` | Shared inspection/extraction of text and image content. |
| D11 | `tau_ai/_provider_events.py` | Internal, coarser events emitted by provider parsers before canonicalization. |
| D12 | `tau_ai/retry.py` | Retry timing, retry metadata and cancellable waiting. |
| D13 | `tau_ai/stream.py` | Converts internal provider events into the canonical block-oriented assistant stream. |
| D14 | `tau_ai/fake.py` | Replays scripted canonical events for deterministic execution tests. |
| D15 | `tau_ai/openai_compatible.py` | OpenAI-compatible transport, including Chat Completions and Responses payload/stream formats. |
| D16 | `tau_ai/anthropic.py` | Anthropic message/tool encoding, caching, SSE parsing and usage accounting. |
| D17 | `tau_ai/google.py` | Google request/response translation, thinking and multimodal details. |
| D18 | `tau_ai/mistral.py` | Mistral Conversations API adapter. |
| D19 | `tau_ai/openai_codex.py` | Codex-specific transport, credentials, response parsing and runtime model discovery. |

The real provider implementations are largely siblings. Reading one thoroughly is more valuable initially than reading all five.

**Start with `fake.py`, then `stream.py`, then `openai_compatible.py`.**

**What you now have:** an agent that can talk to a real model.

**Read alongside:** `tests/test_tau_ai.py`, `tests/test_http.py`, `tests/test_chat_channels.py`, then provider-specific tests such as `test_anthropic_sse.py`, `test_multimodal_provider_payloads.py` and `test_cross_provider_history.py`.

---

### Stage E — Establish the coding environment

These are application foundations, still without the large `CodingSession` orchestrator.

| Order | File | Contribution |
|---|---|---|
| E1 | `tau_coding/paths.py` | Canonical user/project paths, profiles and session/configuration locations. |
| E2 | `tau_coding/thinking.py` | Application-level thinking choices and mappings to provider reasoning parameters. |
| E3 | `tau_coding/reload.py` | Data structures describing what changed during resource reload. |
| E4 | `tau_coding/version.py` | Installed application version lookup. |
| E5 | `tau_coding/self_docs.py` | Locations of packaged documentation and examples. |
| E6 | `tau_coding/resources.py` | Resource search paths, Markdown frontmatter, diagnostics and system-prompt file discovery. |
| E7 | `tau_coding/skills.py` | Loads skills, builds their prompt index and expands explicit skill invocations. |
| E8 | `tau_coding/prompt_templates.py` | Loads named prompts and substitutes invocation arguments. |
| E9 | `tau_coding/system_prompt.py` | Assembles and explains the system prompt from tools, guidelines, skills and project context. |
| E10 | `tau_coding/context.py` | Discovers project instruction/context files and reports loading problems. |
| E11 | `tau_coding/project_trust.py` | Detects protected project inputs, persists trust decisions and coordinates approval. |
| E12 | `tau_coding/shell_config.py` | Durable shell-execution settings. |
| E13 | `tau_coding/image_processing.py` | Validates and normalizes image attachments within size/dimension limits. |
| E14 | `tau_coding/tools.py` | Actual read/write/edit/bash tools, input validation, output truncation, edits/diffs and subprocess cancellation. |
| E15 | `tau_coding/context_window.py` | Context-size estimates, compaction thresholds and summary-prompt construction. |
| E16 | `tau_coding/branch_summary.py` | Uses a model to summarize conversation history when changing branches. |
| E17 | `tau_coding/diagnostics.py` | Structured diagnostic logging for agent calls and failures. |
| E18 | `tau_coding/events.py` | Application events added around agent execution: compaction, queues, retries, session metadata and settling. |
| E19 | `tau_coding/session_manager.py` | Discovers/indexes/manages saved sessions at application-defined locations. |
| E20 | `tau_coding/session_stats.py` | Calculates activity and usage totals for session history. |

**What you now have:** the components that turn a generic tool-using agent into a coding agent.

Most files have directly corresponding `tests/test_<name>.py`; the tool tests are `tests/test_coding_tools.py`.

---

### Stage F — Credentials, login and model selection

#### F1. Authentication contracts and implementations

| Order | File | Contribution |
|---|---|---|
| F1.1 | `tau_coding/credentials.py` | API-key/OAuth credential types and credential storage. |
| F1.2 | `tau_coding/oauth_types.py` | Provider-neutral login callbacks, prompts, runtime authentication and OAuth interface. |
| F1.3 | `tau_coding/oauth.py` | OAuth/PKCE helpers and OpenAI Codex login, callback-server and token-refresh implementation. |
| F1.4 | `tau_coding/oauth_device.py` | Device-code polling and cancellation. |
| F1.5 | `tau_coding/oauth_anthropic.py` | Anthropic subscription login and refresh. |
| F1.6 | `tau_coding/oauth_github_copilot.py` | Copilot device login and runtime authentication. |
| F1.7 | `tau_coding/oauth_registry.py` | Registers and selects OAuth implementations. |

#### F2. The catalog cycle — read as one component

This is the first place where **a literal file-by-file topological order does not exist**.

Use this internal reading order:

| Order | File/portion | Contribution |
|---|---|---|
| F2.1 | `tau_coding/provider_catalog.py` — types first | Provider/model descriptions, capabilities and pricing metadata. Stop before `_load_builtin_catalog()` initially. |
| F2.2 | `tau_coding/models_dev.py` | Transforms models.dev data into Tau model metadata and catalog overlays. |
| F2.3 | `tau_coding/models_dev_store.py` | Downloads, validates and caches refreshed model metadata. |
| F2.4 | `tau_coding/catalog_loader.py` | Loads packaged TOML and merges generated/refreshed/user catalog data. |
| F2.5 | `tau_coding/provider_catalog.py` — initialization remainder | Initializes the built-in catalog and provides lookup functions. |

Also inspect:

```text
tau_coding/data/catalog.toml
tau_coding/data/models-dev-catalog.json
```

**For the C++ port:** separate catalog types from catalog loading. Pass baseline catalog inputs into refresh/generation code rather than letting it import the loader. Initialize the built-in catalog at the application composition layer.

#### F3. Configuration above those foundations

| Order | File | Contribution |
|---|---|---|
| F3.1 | `tau_coding/provider_config.py` | Durable provider settings, validation, model selection, per-model overrides and translation to transport configuration. |
| F3.2 | `tau_coding/codex_version.py` | Determines the Codex client-version value used by the transport. |
| F3.3 | `tau_coding/codex_model_store.py` | Persists discovered Codex model metadata and limits. |

**What you now have:** a distinction between model *descriptions*, durable user *settings*, stored *credentials*, and transport *configuration*.

That separation is worth preserving in C++.

**Read alongside:** provider catalog/configuration, models.dev, credentials and OAuth tests.

---

### Stage G — Presentation contracts needed by the application

Some presentation-related files are dependencies of commands, exports or extension contracts, so they appear before the full TUI.

| Order | File | Contribution |
|---|---|---|
| G1 | `tau_coding/tui/themes/__init__.py` | Theme types, JSON theme loading and theme discovery. This `__init__.py` contains real implementation. |
| G2 | `tau_coding/tui/config.py` | Durable theme, keybinding and TUI settings. |
| G3 | `tau_coding/extensions/api.py` | Extension-facing contracts: hooks, commands, custom rendering, UI bridge and extension context. |
| G4 | `tau_coding/session_usage.py` | Collects usage analytics and renders the HTML usage dashboard. |
| G5 | `tau_coding/session_export.py` | JSONL/HTML export of session history, tree, messages and usage. |
| G6 | `tau_coding/commands.py` | Slash-command registry, parsing and handlers returning structured action requests. |

`extensions/api.py` has forward/type-only references to later runtime classes. Treat these as interface declarations, not a reason to implement the runtime first.

Two architectural details to notice:

- Commands use a `CommandSession` protocol rather than importing concrete `CodingSession`.
- HTML exports and usage analytics depend on theme definitions under `tui/`.

For C++, you could place shared theme/rendering data outside the interactive-TUI namespace.

---

### Stage H — Extensions and runtime provider composition

| Order | File | Contribution |
|---|---|---|
| H1 | `tau_coding/extensions/providers.py` | Dynamic-provider contracts: authentication, model snapshots, transport options and runtime factories. |
| H2 | `tau_coding/extensions/provider_registry.py` | Composes dynamic provider layers by source/generation; manages refresh, shadowing, retirement and cancellation. |
| H3 | `tau_coding/local_backends.py` | Provider-neutral local-inference management contracts and supervised operation registry. |
| H4 | `tau_coding/extensions/loader.py` | Discovers extension sources/manifests and imports extension implementations. |
| H5 | `tau_coding/built_in_extensions.py` | Declares trusted bundled extensions and dependencies supplied by the host. |
| H6 | `tau_coding/extensions/builtins/llama_cpp/state.py` | Durable llama.cpp integration/model state. |
| H7 | `tau_coding/extensions/builtins/llama_cpp/router.py` | llama.cpp router protocol: capabilities, models, actions and download progress. |
| H8 | `tau_coding/extensions/builtins/llama_cpp/huggingface.py` | Searches Hugging Face for GGUF repositories and variants. |
| H9 | `tau_coding/extensions/builtins/llama_cpp/service.py` | Concrete local-backend service joining configuration, authentication, discovery, router operations and saved state. |
| H10 | `tau_coding/extensions/builtins/llama_cpp/__init__.py` | Registers the llama.cpp provider/backend through the extension API. |
| H11 | `tau_coding/extensions/runtime.py` | Runs extension setup/lifecycle/hooks, wraps tools, binds sessions and owns registries. |
| H12 | `tau_coding/provider_runtime.py` | Constructs actual static/dynamic model providers and resolves/refreshes runtime credentials. |
| H13 | `tau_coding/extensions/__init__.py` | Public facade over the completed extension subsystem. |
| H14 | `tau_coding/extension_installer.py` | Installs/manages extension sources. |

**Dependency-order exception:** H5 lazily calls H10. In C++, separate trusted-extension declaration types from the final built-in registration/composition function.

**What you now have:** a runtime capable of loading plugins and building selected providers, including local inference.

Do not start your port here. Source identity, generation ownership, cancellation and retirement are advanced lifecycle concerns.

**Read alongside:** `test_extensions.py`, `test_extension_providers.py`, `test_provider_runtime.py`, `test_local_backends.py`, `test_llama_cpp_extension.py`.

---

### Stage I — Assemble the coding application

| Order | File | Contribution |
|---|---|---|
| I1 | `tau_coding/session.py` | `CodingSession`: joins harness, resources, tools, providers, trust, persistence, branching, model switching, compaction and extension lifecycle. |
| I2 | `tau_coding/session_preparation.py` | Shared trust-aware staging/startup boundary for constructing a candidate coding session. |

**This is the application’s main integration layer.** It is approximately 5,200 lines; reading it first would obscure the smaller abstractions beneath it.

Within `session.py`, use this order:

1. `CodingSessionConfig` and supporting data structures.
2. `CodingSession.__init__()` and `load()`.
3. `expand_prompt_text()`, `prompt()` and `continue_()`.
4. `_attach_persistence_listener()`, `_persist_on_message_end()` and append/replay methods.
5. Provider/model/thinking selection.
6. Compaction and overflow recovery.
7. Branching, resume and new-session operations.
8. Reload, replacement adoption and resource cleanup.

**What you now have:** the complete frontend-independent coding application.

**Read alongside:** `test_coding_session.py`, `test_codex_resume.py`, `test_context_window.py`, `test_project_trust.py`.

---

### Stage J — Frontends and application entry point

#### J1. Noninteractive rendering

| Order | File | Contribution |
|---|---|---|
| J1.1 | `tau_coding/tui/state.py` | Display/transcript state and shared tool/message formatting. Used by human-readable print rendering too. |
| J1.2 | `tau_coding/rendering/base.py` | Output-mode and renderer contracts. |
| J1.3 | `tau_coding/rendering/plain.py` | Prints final assistant text. |
| J1.4 | `tau_coding/rendering/json.py` | Prints structured event streams. |
| J1.5 | `tau_coding/rendering/transcript.py` | Human-readable streaming transcript. |
| J1.6 | `tau_coding/rendering/__init__.py` | Selects the renderer for the requested output mode. |
| J1.7 | `tau_coding/rpc.py` | JSONL command/event frontend for external clients. |

#### J2. Interactive terminal frontend

| Order | File | Contribution |
|---|---|---|
| J2.1 | `tau_coding/tui/terminal_title.py` | Terminal title formatting/control. |
| J2.2 | `tau_coding/tui/terminal_notification.py` | Completion/attention notifications. |
| J2.3 | `tau_coding/tui/file_drop.py` | Recognizes and normalizes terminal-dropped file paths. |
| J2.4 | `tau_coding/tui/autocomplete.py` | Completions for commands, skills, prompts and paths. |
| J2.5 | `tau_coding/tui/adapter.py` | Translates coding-session events into display-state changes. |
| J2.6 | `tau_coding/tui/project_trust.py` | Interactive trust approval screens. |
| J2.7 | `tau_coding/tui/local_backends.py` | Generic local-backend configuration/status/action screens. |
| J2.8 | `tau_coding/tui/widgets.py` | Transcript, sidebar, Markdown, session information and completion widgets. |
| J2.9 | `tau_coding/tui/app.py` | Full interactive app: input, screens, keybindings, event consumption and session coordination. |
| J2.10 | `tau_coding/tui/__init__.py` | Public TUI facade. |

`app.py` is approximately 9,000 lines. **Leave it until last among the substantive application modules.** Much of it is Textual-specific and should be redesigned for your chosen C++ terminal toolkit rather than translated mechanically.

#### J3. Product entry point and maintenance

| Order | File | Contribution |
|---|---|---|
| J3.1 | `tau_coding/update_check.py` | Update checks, release notes and their cached state. |
| J3.2 | `tau_coding/updater.py` | Updates the installed application using its owning package manager. |
| J3.3 | `tau_coding/cli.py` | Typer entry point: flags, configuration, startup and dispatch to print/RPC/TUI modes. |

**What you now have:** the complete executable product.

Read `tests/test_rendering.py`, `test_rpc.py`, `test_tui_adapter.py`, the relevant TUI tests, and finally `test_cli.py`.

---

## 4. Files not to treat as implementation layers

Read these after the implementations they expose:

```text
tau_agent/session/__init__.py
tau_agent/__init__.py
tau_ai/__init__.py
tau_coding/__init__.py
```

They mainly aggregate public APIs.

Also:

- `py.typed`: Python typing marker; no C++ counterpart.
- `data/examples/` and `examples/extensions/`: extension usage examples.
- `scripts/generate_models.py`: catalog-generation tooling; read after Stage F.
- `website/`, `docs/`, `dev-notes/`: documentation/site infrastructure, not agent runtime.
- Theme JSON files: inputs to Stage G, not executable modules.

---

## 5. How I would stage your C++ port

Your desired “working tests at every step” is achievable, but **port test cases by responsibility, not entire Python test files wholesale**. Some test files cover several layers, and eager Python package imports pull in modules beyond the feature being tested.

I would use these milestones:

| Milestone | Working capability |
|---|---|
| 1 | Message/tool/event types serialize and deserialize correctly. |
| 2 | Scripted fake provider drives the loop, including tool calls and cancellation. |
| 3 | Harness supports successive prompts, subscriptions and queued messages. |
| 4 | History persists and replays, including branches and compaction records. |
| 5 | One real provider works through HTTP/SSE. |
| 6 | Read/write/edit/bash tools and project prompt assembly work. |
| 7 | Minimal persistent coding session works in print mode. |
| 8 | Add configuration, model switching, compaction and recovery. |
| 9 | Add extensions/local backends and RPC. |
| 10 | Add the interactive TUI and product-maintenance features. |

**The best initial reading sequence is:**

```text
tau_agent/types.py
tau_agent/messages.py
tau_agent/tools.py
tau_agent/provider_events.py
tau_agent/provider.py
tau_agent/events.py
tau_agent/tool_history.py
tau_agent/loop.py
tau_agent/harness.py
```

That sequence gives you the central design without dragging you into authentication, catalog cycles, plugin lifecycle or terminal widgets. Preserve that core boundary in the port; treat the existing giant application files as integration code, not as models for your C++ file organization.

# BlueAgent Next — Public Demo

**Language**: English | [日本語](README.ja.md) | [中文](README.zh.md)

An entity-first, multi-character AI runtime demo. This project demonstrates how to build a recoverable, testable end-to-end pipeline from event ingestion, routing, memory disclosure, and persona assembly to Web and App client delivery.

> This repository is a sanitized public portfolio version. It runs on an offline Mock brain by default and contains no real user data, access tokens, internal service endpoints, developer signing keys, or third-party character assets.

## Core Capabilities

- **Implicit Session Management**: Derives and slices threads automatically from `(scope, entity, scene, purpose)` without explicit session creation.
- **Multi-Entity & Multi-Identity**: Memories, relationships, stances, voice, and attention are scoped to `CharacterEntity`.
- **Data-Boundary Memory Disclosure**: Memories are recalled only when all currently present participants belong to the memory's `audience` (`present ⊆ fact.audience`).
- **Pluggable Model Layer**: Runs offline via Mock mode (`BLUEAGENT_BRAIN=mock`) by default, or connects to any OpenAI-compatible endpoint.
- **Persistence & Recovery**: Threads, facts, episodes, activities, leases, and outbox frames are persisted to disk with cursor-based SSE reconnection recovery.
- **Multi-Client Delivery**: Includes an Owner Web debug console, standalone App Server, Unity mobile client, and Quest 3 MR project.
- **Engineering Guardrails**: Includes automated tests for package layering, API contracts, permissions, memory isolation, streaming delivery, and persistence recovery.

## Quick Start

Requires Python 3.11+. The core Mock demo uses only the Python standard library.

```bash
cd blueagent-next-public
BLUEAGENT_BRAIN=mock python3 agent/run.py
```

On first launch, an Owner token is generated locally. Open the Owner URL printed in your terminal (the token is stored only in your local runtime directory and should never be committed):

```text
http://127.0.0.1:8787/
```

## Offline Tests

```bash
python3 agent/tests/run_all.py
```

Some voice tests require optional dependencies; the core demo has no external model or network dependencies.

## Connecting Your Own Model

Copy the example environment file outside the repository or keep `.env` local only, then specify it explicitly:

```bash
cp .env.example .env
# Edit .env with your OpenAI-compatible endpoint and API key
BLUEAGENT_ENV_FILE=.env python3 agent/run.py
```

Never commit `.env`, real tokens, or runtime data to the repository.

## Docker Compose

The App Server does not bypass account initialization. First start the local Agent to generate `data/accounts.json`, then provision fictional local accounts and entity tokens for the App Server:

```bash
# Press Ctrl+C after first startup; local data/accounts.json is now generated
BLUEAGENT_BRAIN=mock python3 agent/run.py

python3 agent/tools/provision_app_server.py \
  --agent-accounts data/accounts.json \
  --app-accounts app-data/accounts.json \
  --secret-file app-secrets/agent.env

BLUEAGENT_BRAIN=mock docker compose up --build
```

`data/`, `app-data/`, and `app-secrets/` are all git-ignored. Compose starts the Agent and App Server by default; the gateway starts only when the `gateway` profile is explicitly enabled and does not connect to any private infrastructure.

## Directory Structure

```text
agent/
  brain/        Domain models, routing, memory, persona, and runtime
  adapter/      External adapters (Discord, TRPG, App, voice, etc.)
  server/       Agent HTTP / SSE and management endpoints
  client/       Owner Web debug console
  persona/      Original fictional characters and shared persona rules
  tests/        Agent offline acceptance tests
app/
  server/       Standalone account, session, public area, and media service
  client/       Unity client, shared C# logic, and procedural geometric assets
  tests/        App Server and client contract tests
mr/             Quest 3 MR project
docs/           Architecture, API contracts, glossary, and generated docs
```

## Public Edition Boundaries

- The three default characters **Mira, Aster, Nori** and additional sample character **Lumi** are original fictional characters.
- Portraits, avatars, backgrounds, and stickers are procedurally generated abstract geometric graphics.
- No third-party character models are included. 3D loading hooks are preserved and fall back to 2D when no model is provided.
- No prebuilt native libraries are included; mobile builds require preparing the corresponding dependencies locally.
- Android builds use a developer-local signing key; no keystore is included in this repository.
- No project-wide open-source license is granted; public readability does not grant redistribution rights. Third-party components are governed by their respective notices in `THIRD_PARTY_NOTICES.md`.

## Design Documentation

- `DESIGN.md`: Architecture and security boundary summary.
- `docs/APP-API-SSE.md`: App / Agent API & SSE contract.
- `docs/accounts-and-clients.md`: Account and client identity model.
- `docs/FILE-TREE.md`: Generated file tree.
- `docs/ENVIRONMENT.md`: Generated environment variables reference.

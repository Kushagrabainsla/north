# ADR 0002: Composition Root Is the Only Concrete Wiring Layer

- **Status:** Accepted
- **Date:** 2026-09-09

## Context

`orchestrator/app.py` currently performs both application lifecycle work and broad system assembly. `config/dependencies.py` also constructs production services. This makes orchestration and configuration high-coupling hubs, complicates testing, and permits lower-level modules to know too much about concrete infrastructure.

## Decision

Create a dedicated application composition root during Stage 3. It alone will construct concrete databases, providers, tool implementations, gateways, registries, and background services, then inject narrow interfaces into application workflows.

`config` will retain settings parsing and configuration value types. Orchestration will retain task workflow behavior. Neither layer will own broad production dependency construction after migration.

## Consequences

- Dependency direction becomes inspectable and testable.
- Orchestration and configuration modules become easier to unit test with fakes.
- Migration must preserve existing CLI and server entry points through temporary compatibility adapters.
- Wiring changes must receive startup/shutdown, CLI, API-health, and persistence regression coverage.

## Compatibility and Validation

Existing public commands, HTTP endpoints, environment variables, settings files, and persisted user data remain stable. No entry point is removed until a separately documented compatibility window expires.

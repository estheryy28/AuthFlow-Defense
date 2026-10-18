# Policy-to-Proof implementation plan

## Module boundaries and contracts

| Module | Responsibility | Inputs and outputs | Current state |
|---|---|---|---|
| 0. Local runtime | Docker Compose or Windows host-run fallback, PostgreSQL, Ollama, browser UI | Local services; target binds to loopback in host mode and to an internal lab network in Compose | Both paths are implemented; Compose engine still needs Windows restart, host path is live |
| 1. Vulnerable workflow target | Own payment API with vulnerable and fixed behaviors | HTTP workflow requests; persisted payment and ledger rows | Implemented |
| 2. Evidence bundle | Provide policy, API description, and target source to analyze | Read-only files under `fixtures/` and `target/app/` | Fixture-based MVP implemented; uploads deferred |
| 3. Specialist agents and gates | Extract an invariant, corroborate section evidence, map routes/state, plan a bounded test, critique it | Ollama structured JSON plus source-bound and oracle-specific deterministic validation | Implemented and exercised; critic/auditor output is advisory where the small model contradicts deterministic evidence |
| 4. Safe execution and oracle | Restrict plan actions; run against owned target; query state through read-only database role | HTTP trace plus payment/ledger snapshot and deterministic verdict | Implemented and verified in vulnerable and fixed modes |
| 5. Run API and persistence | Expose health/run endpoints and store stage/evidence data | FastAPI and PostgreSQL `platform.runs` | Implemented |
| 6. Judge UI | Show service health, stages, actual HTTP trace, agent evidence, and oracle result | React/Vite browser UI | Implemented |
| 7. Integration and demo polish | Build services, run vulnerable/fixed paths, inspect the visible result, resolve setup issues | Local host-run fallback verified; Compose path remains available | Host-run integration verified; Compose startup awaits Windows restart |

## Host setup checkpoint

- Docker Desktop and Ollama are installed on the Windows host. The Docker CLI exists at the per-user install path but is not currently in this shell's PATH; `start-demo.ps1` adds known install locations.
- Ollama 0.40.5 is installed; `qwen2.5:3b-instruct` is downloaded, listed by Ollama, and returned valid JSON through its local chat API.
- Windows still reports that WSL2 virtualization cannot start. The WSL and Virtual Machine Platform features were enabled, but Windows must restart to apply them. Docker's Linux engine cannot start before that restart.
- A native PostgreSQL 16.15 Windows binary distribution is installed under `.tools/`; its downloaded ZIP was checked against the publisher's SHA-256. A loopback-only local cluster at `.local/pgdata` hosts the real target database. `start-local-demo.ps1` starts missing host services and checks for port conflicts.
- `docker compose config --quiet` passes when the per-user Docker executable is invoked directly. Python module compilation, dependency checks, and the frontend production build pass. The Docker engine and container image builds remain unverified because WSL2 is not active in this session.
- The full host-run pipeline completed live on 2026-10-18. Vulnerable mode sent create, approve, amend, and execute requests to the real FastAPI target; execution returned HTTP 200 and PostgreSQL showed $200 settled while the approval snapshot remained $100 (`PROVEN`). Fixed mode returned HTTP 409 on execute and its per-run database snapshot contained zero ledger entries (`NOT_REPRODUCED`).
- The API is configured to call Ollama through `host.docker.internal:11434`; container-to-host reachability still needs a check after Docker starts. Ollama's documented Windows default is loopback, so do not widen its bind address unless that check shows it is necessary.

## Execution sequence

1. The API loads the policy, OpenAPI file, and target source as read-only evidence.
2. The Policy Analyst and Application Mapper run independently and concurrently. Their outputs must satisfy typed contracts; every cited quote must exist verbatim in the supplied artifact.
3. A separate Policy Evidence Auditor locates one exact quote from each policy section. Every quote must match the original file and its declared section. The model's ambiguity verdict is retained as an advisory; a narrow deterministic premise gate checks the payment oracle's required facts: the review event records approved terms, an amendment can preserve status without changing that event, and settlement uses current terms for approved payments. This gate is specific to the included payment fixture, not a general policy engine.
4. The Test Planner receives the accepted invariant and a compact mapped API/state view, then proposes a bounded plan. It is not given the full exploit sequence. Deterministic checks reject actions outside the mapped workflow, plans that cannot exercise the invariant, unsupported fields, and demo amounts above USD 500. One repair attempt may use precise validation feedback; a second invalid proposal stops before target execution.
5. The language-model critic reviews the plan and application map, but is advisory because the installed 3B model has produced contradictory critiques (for example, demanding a database-read action that the harness performs after the API sequence). Its raw output is retained. Execution eligibility comes from exact-source checks, payment-premise checks, state-transition validation, plan validation, and the read-only database oracle.
6. A fixed dispatcher executes the permitted plan against the local target. The model cannot choose arbitrary URLs, shell commands, or external systems.
7. The independent read-only oracle compares the approved terms with the actual ledger entry. The verdict comes from persisted state, not from the model's claim.
8. The API stores the agent artifacts, request trace, state snapshot, and verdict. The UI presents these records for the live demo.

## Trust boundaries

- Ollama roles use the same local model in this MVP. Role separation gives different tasks, not statistical independence; correlated errors remain possible.
- LLM output is an untrusted proposal. Pydantic validation, exact-source quote checks, the fixture-specific policy-premise gate, AST-backed transition checks, action allowlisting, amount limits, bounded plan repair, and the database oracle are separate controls. The free-form model critic cannot veto or authorize execution.
- The target is reachable only on Docker's internal lab network. Only the UI and API ports bind to loopback on the host.
- The fixture bundle demonstrates one payment approval invariant. It is not evidence that the prototype can discover arbitrary business-logic flaws; uploaded projects, reusable policy semantics, multiple oracle types, and production integrations are outside the MVP.
- The target and state oracle are deliberately specific. This demo can show a policy candidate mapped through source evidence into a live differential test; it does not establish that an LLM outperforms a skilled tester or a purpose-written script on this one known workflow.

## Module previews

- **Target preview:** the UI chooses `vulnerable` or `fixed`, then drives create → approve → edit → execute requests against the real local FastAPI target. The target writes payment and ledger rows to PostgreSQL.
- **Agent preview:** the run record exposes the cited invariant, policy auditor's evidence and verdict, mapped routes/state transitions, proposed test plan, and advisory critic output. A separate validation summary shows which deterministic gates allowed execution.
- **Oracle preview:** vulnerable mode should show a ledger term that differs from the terms captured at approval; fixed mode should reject execution after the edit and show no unauthorized ledger row.
- **UI preview:** `http://127.0.0.1:5173`, with actual health checks, run stages, request evidence, and the database-backed result. It does not use canned result data.

## Completion criteria for the host-run prototype

- PostgreSQL, Ollama, target, backend, and frontend start locally; the browser-facing ports bind only to loopback.
- Ollama reports the configured model as available to the backend.
- Vulnerable mode produces a live request trace and a mismatch verified from PostgreSQL state.
- Fixed mode produces a live rejection and no mismatched ledger entry.
- A policy ambiguity or malformed/out-of-scope agent plan stops without touching the target workflow.

The first four runtime criteria have been verified on this machine. The last gate still needs an explicit negative-path run. Docker Compose clean-checkout startup remains a separate pending verification because Windows needs a restart before its WSL2 engine can run.

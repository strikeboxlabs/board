# Dispatcher

---

## Core purpose

The Dispatcher is Strikebox's client-side executor. It:

1. Coordinates project container lifecycles.
2. Assigns explicit tasks to agents.
3. Calls the Strikebox API on agents' behalf to write to the graph.

Agents do not claim Intents, send heartbeats, or call the Strikebox API directly. They receive tasks from the Dispatcher and return structured results; the Dispatcher decides how to call the Server.

---

## Design principles

1. Agent tasks fall into three categories: `bootstrap`, `reason`, and `explore`. Bootstrap runs only in the project's initial state and attempts to solve the whole problem directly. Its main phase returns only when solved, with both a key Fact and `complete`; if it times out, `bootstrap_conclude` produces a summary Fact. Reason reads the graph to determine whether the goal is met or a new intent is needed. Explore executes one claimed intent and produces one Fact conclusion.
2. The Dispatcher is the sole protocol writer and control plane. Agents do not claim, heartbeat, or call the Strikebox API directly.
3. Timeouts are defined per task type. Bootstrap and explore both support two phases: initial execution, followed by conclude in the same session after a timeout or parse failure.
4. Prompts ship with the code as Markdown files and can be selected by prompt group. Drivers such as `claudecode`, `codex`, and `mock` implement worker behavior. `dispatch.yaml` contains runtime parameters only.
5. Initial-state scheduling chooses bootstrap or reason based on `project.bootstrap_enabled` and worker capabilities. In later states, new Facts or Hints prioritize reason; otherwise, claimable explore intents take priority. The server's project-level reason lease limits reason to one task per project. Bootstrap is limited to one reserved bootstrap intent and one bootstrap task per project. Different projects may run concurrently. `runtime.interval` intentionally serves as both the main-loop interval and the heartbeat period for claimed tasks.
6. A Worker models an independent LLM concurrency quota. Do not split the same API key across multiple Workers; `workers[].max_running` is sufficient to control concurrency.
7. Logging emphasizes state changes. Routine polling, successful heartbeats, and repeated skips should remain quiet. Container creation, dispatch, new processes inside containers, health checks, timeouts, conclusion, intent release, and temporary worker unavailability must be visible.
8. Project container cleanup must not block the main scheduling loop. Multiple completed projects may be cleaned up concurrently.
9. A project leaving `active` is a hard stop: stop dispatching, immediately request cancellation of local bootstrap/explore/reason tasks, do not enter conclude fallback for cancelled tasks, and stop the project container on a subsequent poll to terminate remaining agent processes.
10. This implementation is designed and tested for one Dispatcher instance. Multiple Dispatchers scheduling against the same server are unsupported.
11. A completed project explicitly reopened as active needs no special scheduling branch. Treat it as an ordinary active project. If its container still has queued cleanup, wait for cleanup to finish before scheduling to avoid racing the old completed/stopped cleanup.
12. Known limitation: the protocol retains only the current claim holder, not an Intent's worker history. When stopping clears open-intent workers, the Dispatcher, UI, and API can no longer show the last worker advancing each Intent. A future `worker_history` field would be more appropriate than changing current claim semantics.
13. Evidence and reporting contract: each project owns a durable directory under `evidence_root`. Local mode defaults to `~/.local/share/board/projects`, overridable through `BOARD_PROJECTS_DIR`. Container mode requires this root and bind-mounts it at `/board/project`. The layout is `evidence/<intent_id>/`, `workspace/`, `reports/`, and `scratch/`. Workers receive `BOARD_PROJECT_DIR` and `BOARD_EVIDENCE_DIR`; prompts require all artifacts under `$BOARD_EVIDENCE_DIR`, never arbitrary `/tmp` paths. When an intent finishes, the Dispatcher hashes its evidence files with SHA-256, registers evidence rows through the API, and registers any structured findings returned alongside the fact in the optional findings array. It also refreshes `<project>/reports/timeline.txt` and `graph.yaml`, using the Server as the single rendering source and only writing the files locally. Completion writes the full report set. Regenerable graph snapshots belong in project scratch, not a hardcoded `/tmp/board-prompts`. Pi's agent directory deliberately defaults to ephemeral `/tmp/board-pi`; set worker environment variable `PI_AGENT_DIR` for a persistent location.
14. Terminal states are `completed` (goal achieved) and `aborted` (deliberately abandoned; the abort endpoint records reason, time, and actor). Both cancel local tasks as non-active projects and stop containers using stopped semantics, never deleting project directories or bind-mounted evidence. Reports differ: completion writes the full set in the task path; abort writes it from the polling loop, once per project and idempotently again after a process restart. Abandoning a run preserves its record.

Additional note:

- A removed project is treated as `deleted`. Like stopped, it cancels local tasks first, but cleanup deletes its orphan container instead of merely stopping and retaining it.

---

## Architecture overview

The architecture has four components:

1. Strikebox Server
2. Dispatcher
3. Project containers
4. Worker / Agent CLI

### 1. Strikebox Server

The Server is the protocol's source of truth.

It:

- Stores Projects, Facts, Intents, and Hints.
- Provides protocol endpoints.
- Maintains Intent claims, heartbeats, and conclusions.
- Maintains project-level reason lease claims, heartbeats, and releases.

### 2. Dispatcher

The Dispatcher is the core component implemented by this design.

It:

- Fetches project graph state.
- Decides which task type to dispatch.
- Selects a Worker.
- Manages project containers and worker processes.
- Manages sessions, timeouts, health checks, and conclusion.
- Writes results to the Strikebox Server.

### 3. Project containers

Each project has one runtime container.

The container is the project's execution environment and typically:

- Provides the toolchain.
- Provides the network environment.
- Hosts the project's worker processes.

### 4. Worker / Agent CLI

A Worker is an execution unit managed by the Dispatcher, not a direct protocol participant.

Examples:

- Claude Code CLI
- Codex CLI

They:

- Receive prompts rendered by the Dispatcher.
- Execute tasks inside the current container.
- Return structured JSON.

### Component relationships

```text
                         +----------------------+
                         |   Strikebox Server   |
                         |----------------------|
                         | Projects / Facts     |
                         | Intents / Hints      |
                         | Protocol API         |
                         +----------^-----------+
                                    |
                           read / write API
                                    |
+-----------------------------------------------------------+
|                         Dispatcher                        |
|-----------------------------------------------------------|
| Scheduling / Task Dispatch / Session / Timeout / Health   |
| Container Lifecycle / Protocol Writeback                  |
+----------------------+----------------------+-------------+
                       |                      |
             manage container        manage container
                       |                      |
          +------------v-----------+  +------v-------------+
          |   Project Container A  |  | Project Container B|
          |------------------------|  |--------------------|
          | Worker / Agent CLI     |  | Worker / Agent CLI |
          | - Claude Code          |  | - Codex            |
          | - Codex                |  | - ...              |
          +------------------------+  +--------------------+
```

### Main execution flow

The Dispatcher reads two forms of data:

- Structured API responses for scheduling, state assessment, intent selection, and protocol writes.
- `GET /projects/{project_id}/export?format=yaml` solely for graph snapshots used in prompts.

1. Read the project graph from the Server.
2. Select a task type and Worker according to scheduling rules.
3. Render the prompt and command placeholders.
4. For explore, claim the Intent with `POST /projects/{project_id}/intents/{intent_id}/heartbeat`; for reason, claim the project lease with `POST /projects/{project_id}/reason/claim`.
5. Start the worker process in the project container.
6. The Worker returns structured JSON.
7. Parse the result and call `POST /projects/{project_id}/complete`, `POST /projects/{project_id}/intents`, `POST /projects/{project_id}/intents/{intent_id}/conclude`, `POST /projects/{project_id}/intents/{intent_id}/release`, or `POST /projects/{project_id}/reason/release` as appropriate.

If a project becomes stopped, this flow short-circuits on the next iteration: exclude it from active scheduling, cancel local running tasks, then clean up and stop its container. Cancelled bootstrap/explore tasks do not enter conclude fallback and produce no additional Facts. After resumption, reread graph state and choose explore, reason, or, in the initial state, bootstrap according to `project.bootstrap_enabled` and worker capabilities. A completed project reopened by the Server is likewise an active project with a new Fact and resumes normal scheduling on the next iteration.

Worker selection rules:

1. Filter by task type.
2. Exclude Workers at their max_running limit.
3. Exclude unhealthy Workers in a temporary unavailability window.
4. Prefer the lowest priority value among those remaining.
5. Break equal priorities by the fewest currently running tasks.
6. Break any remaining ties randomly.
7. For explore, successfully claim via `POST /projects/{project_id}/intents/{intent_id}/heartbeat` before starting the task.
8. For reason, maintain the claimed lease through `POST /projects/{project_id}/reason/heartbeat`. With `runtime.worker_healthcheck=startup_and_task`, check the selected Worker again before launch. Failure discards this task and temporarily excludes the Worker until a later scheduling round.

---

## Configuration model

The Dispatcher uses one runtime configuration file:

- `dispatch.yaml`

This means:

- Users need only supply `dispatch.yaml`.
- Task prompts ship as Markdown with the code; `runtime.prompt_group` selects their directory.
- Each driver implements worker health checks, command templates, session handling, and two-phase conclusion support.
- `runtime.execution` selects `container` (default, one container per project) or `local` (worker subprocesses on the dispatcher host using already configured CLIs, with no Docker or API keys required).

The code may be organized as follows:

```text
dispatcher/
  models.py
  prompting.py
  output_parser.py
  contracts.py
  prompts/
    default/
      bootstrap.md
      bootstrap_conclude.md
      reason.md
      explore.md
      explore_conclude.md
    mock/
      bootstrap.md
      bootstrap_conclude.md
      reason.md
      explore.md
      explore_conclude.md
  workers/
    base.py
    registry.py
    adapters/
      claudecode.py
      codex.py
      mock.py
```

The appendices contain:

- Example contents of `dispatch.example.yaml`.
- Example Markdown prompts listed above.

---

## Task model

### Overview of the three task types

| Task | Trigger | Input | Output | Timeout policy |
| --- | --- | --- | --- | --- |
| `bootstrap` | Active initial-state project; bootstrap enabled and a capable Worker or existing reserved bootstrap intent; only origin/goal facts, no ordinary intents, and at most the reserved open bootstrap intent | `{origin}`, `{goal}`, `{hints}` | Main-phase success returns `fact + complete`; conclusion phase returns only `fact` | Two phases: timeout followed by conclude_timeout; if both fail, release the reserved intent and retry as a new project next round |
| `reason` | Active project; no unclaimed intents or other reason task; initial trigger or a qualifying new situation | `{graph_yaml}`, `{fact_ids}`, `{open_intents}` | A complete object, an intent object, or empty data | Timeout only; timeout or invalid output is discarded without graph writes |
| `explore` | Active project with a claimable unconcluded intent | `{graph_yaml}`, `{intent_id}`, `{intent_description}` | One Fact conclusion description | Single phase: discard on timeout; two phases: conclude fallback after timeout or output parse failure |

### `bootstrap`

#### Trigger conditions

- The project is still active.
- `project.bootstrap_enabled = true`, and a configured Worker supports bootstrap or a reserved bootstrap intent already exists.
- Facts contain exactly origin and goal.
- There are no intents, or only the reserved open bootstrap intent.
- The reserved bootstrap intent uses this fixed convention:
  - `description = "bootstrap"`
  - `creator = "dispatcher.bootstrap"`
  - `from = ["origin"]`

#### Input

- `{origin}`
- `{goal}`
- `{hints}`

Where:

- `{hints}` is JSON array text so the Worker can quickly absorb strategic guidance at startup.
- Bootstrap does not read graph YAML or depend on the ordinary intent graph.

#### Output contract

Bootstrap has its own output contract:

- The main phase returns only after solving the problem.
- It must return both fact and complete.
- If it cannot solve the problem before timeout, it will not return a valid result; the Dispatcher terminates it and enters bootstrap_conclude.
- Bootstrap_conclude only summarizes and therefore returns fact alone.

```json
{
  "accepted": true,
  "data": {
    "fact": {
      "description": "Obtained flags flag{...} and flag{...}, plus an administrator shell; privilege proof is in $BOARD_EVIDENCE_DIR/root.txt"
    },
    "complete": {
      "description": "Obtained and verified all key results required by the goal; the goal is achieved"
    }
  }
}
```

Constraints:

- The main phase must return both `data.fact.description` and `data.complete.description`.
- Returning fact alone is not allowed in the main phase.
- Bootstrap_conclude may return only fact, never complete.

#### API mapping

Bootstrap uses the ordinary intent protocol with a reserved intent:

| Endpoint | Purpose | When used |
| --- | --- | --- |
| `POST /projects/{project_id}/intents` | Create the reserved bootstrap intent | First entry into an initial-state project, if absent |
| `POST /projects/{project_id}/intents/{intent_id}/heartbeat` | Claim and maintain bootstrap | Before dispatch, then every interval during execution |
| `POST /projects/{project_id}/intents/{intent_id}/conclude` | Record key bootstrap results as a Fact | After bootstrap or bootstrap_conclude returns a valid fact |
| `POST /projects/{project_id}/complete` | Complete directly from the new bootstrap Fact | Only after main-phase success returns fact + complete |
| `POST /projects/{project_id}/intents/{intent_id}/release` | Abandon the current bootstrap attempt | Both phases fail, or the command fails directly |

#### Timeouts and failures

- The first bootstrap phase uses timeout.
- The bootstrap_conclude phase uses conclude_timeout.
- Solving within the main timeout returns fact + complete.
- A timeout, parse failure, or contract violation enters bootstrap_conclude if the Worker supports sessions and conclusion.
- The bootstrap_conclude prompt must require: no further work, no waiting for unfinished tasks, summarize only the most important current facts. It therefore returns fact alone.
- Valid main-phase JSON is first concluded to write the Fact, then immediately completed.
- If bootstrap_conclude returns valid JSON and conclusion succeeds, the reserved intent becomes concluded and the project is no longer initial-state.
- If the main phase's complete write fails, the recorded Fact remains; a later reason task may complete the project.
- If both phases fail, or the conclude write fails, release the current bootstrap intent without writing a Fact. Treat the project as new on the next round.

### `reason`

#### Trigger conditions

- The project is still active.
- It has no unclaimed intents.
- Its project.reason is empty: no other reason lease is active.
- The first trigger occurs only when there are no open intents.
- Subsequent triggers require a new situation, defined as:
  - An increased Fact count.
  - An increased Hint count.
  - A transition from having open intents to having none.
- An explore failure, missed heartbeat, or release that leaves its intent open is not a new situation and must not trigger reason.

#### Input

- `{graph_yaml}`
- `{fact_ids}`
- `{open_intents}`

Where:

- `{fact_ids}` is JSON array text explicitly listing all currently valid Fact IDs.
- `{open_intents}` is JSON array text listing all unconcluded intents. A new Fact or Hint can therefore trigger reason even while another intent is being explored.
- These placeholders assist prompting; they do not replace final server validation.

#### Output contract

Completed:

```json
{
  "accepted": true,
  "data": {
    "complete": {
      "from": ["f008"],
      "description": "flag{abc} satisfies the goal"
    }
  }
}
```

Not completed; propose a new intent:

```json
{
  "accepted": true,
  "data": {
    "intent": {
      "from": ["f003"],
      "description": "Try SQL injection"
    }
  }
}
```

Not completed; no new intent:

```json
{
  "accepted": true,
  "data": {}
}
```

Constraints:

- If present, `data.complete` must be an object containing both `complete.from` and `complete.description`.
- A result containing `data.complete` must not also contain intent.
- If intent is present, both `intent.from` and `intent.description` are required.
- If `{open_intents}` is empty, no exploration is in progress. Without `data.complete`, the result must contain intent.
- If `{open_intents}` is nonempty and there is no `data.complete`, intent may be omitted.

#### API mapping

Before starting reason, the Dispatcher must claim the project-level reason lease and maintain heartbeats during execution. This state appears directly in `project.reason` in `GET /projects` and `GET /projects/{project_id}`, making it visible to the UI and other consumers.

| Reason output | Dispatcher action | Notes |
| --- | --- | --- |
| data.complete present | Call `POST /projects/{project_id}/complete` | Use the executing worker's workers[].name as worker |
| No data.complete, but intent present | Call `POST /projects/{project_id}/intents` | Creator is the current Worker name; worker is always null |
| Empty data object | No graph writes | Do not write Fact, Intent, or Complete |

Logging semantics for failed writes:

- A 403 from `POST /projects/{project_id}/complete` or `POST /projects/{project_id}/intents` usually means the project is no longer active. Discard the task and log at info.
- Other write failures also discard the task without immediate retry; log them.
- Whether or not the graph was written, release reason at cleanup if the project is still active and the Dispatcher still owns the lease. If completed or stopped, the Server clears the lease directly.

#### Timeouts and failures

- Reason uses timeout only.
- Discard immediately on timeout.
- Discard `accepted: false` and log at warn.
- Discard other execution errors as well, including:
  - Nonzero command exit status.
  - Invalid JSON output.
  - Missing required JSON fields.
- In all these cases, log without writing Fact, Intent, or Complete.

### `explore`

#### Trigger conditions

- The project is still active.
- A claimable unconcluded intent exists.

#### Input

- `{graph_yaml}`
- `{intent_id}`
- `{intent_description}`

#### Output contract

Normal result:

```json
{
  "accepted": true,
  "data": {
    "description": "Found error-based injection in a /search parameter"
  }
}
```

Even when no vulnerability is found, return an objective exploration conclusion rather than an empty response. For example:

```json
{
  "accepted": true,
  "data": {
    "description": "Tested common SQL injection payloads against /search parameters; found no evidence of exploitable injection"
  }
}
```

Constraints:

- `data.description` is required and must be an objective factual conclusion.
- Do not use text such as "I refuse to help with penetration testing" or "This is unsafe" as the description.

#### API mapping

Explore uses three protocol endpoints:

| Endpoint | Purpose | When used |
| --- | --- | --- |
| `POST /projects/{project_id}/intents/{intent_id}/heartbeat` | Claim and retain ownership | Before dispatch, then every interval during execution |
| `POST /projects/{project_id}/intents/{intent_id}/conclude` | Produce a Fact and conclude the Intent | After execute or conclude returns a valid conclusion |
| `POST /projects/{project_id}/intents/{intent_id}/release` | Abandon the current attempt | Failure paths |

Dispatch must follow this order:

1. Select a claimable intent.
2. Send one heartbeat request as the claim.
3. Start the explore Worker only after that heartbeat succeeds.

#### Timeouts and failures

Explore supports two modes:

1. Single phase: execute only.
2. Two phases: execute + session + conclude.

Conclude is an optional finishing phase, not a mandatory stage of the normal flow.

The first phase uses timeout.
In two-phase mode, the second phase uses conclude_timeout.

Normal completion:

- Execute returns valid JSON with accepted: true before timeout.
- The Dispatcher calls `POST /projects/{project_id}/intents/{intent_id}/conclude` directly.
- A successful conclude completes the Intent; no extra release is needed.
- A failed conclude write discards the task, releases the Intent, and logs the failure without immediate retry.

Failures eligible for second-phase conclusion:

- Only Workers supporting two phases enter conclude for these failures.
- Only two classes of failure qualify:
  - Execution timeout.
  - The Dispatcher cannot extract and parse the first-phase result, for example:
    - Output is not valid JSON.
    - JSON lacks required fields.
    - accepted is true but the data structure violates the task contract.
- Single-phase Workers do not enter conclude for these failures.

The two-phase conclusion flow is fixed:

1. Kill the current process.
2. Retain this task's session ID.
3. Continue heartbeats and enter conclude directly in the same session.
4. The conclude prompt must explicitly require: do not continue exploring; summarize only exploration and conclusions already completed.
5. If conclude returns valid JSON with accepted: true within conclude_timeout:
   - Call `POST /projects/{project_id}/intents/{intent_id}/conclude`.
   - Finish on success.
6. If conclude times out, returns invalid output or accepted: false, or the conclude API write fails:
   - Discard the entire exploration attempt.
   - Write no graph data.
   - Release the current intent.
   - Log only.

Single-phase failure handling:

- A Worker without session or conclude support uses single-phase mode.
- A first-phase timeout or output parsing/validation failure is then a direct failure.
- Kill the process, discard the exploration, write no graph data, release the intent, and log at warn.

Direct failures that do not enter conclude:

- The first phase returns accepted: false.
- The command exits with a nonzero status.
- The worker process produces no readable result.
- The Dispatcher determines execution failed before result parsing begins.

In each case:

- Do not enter conclude.
- Write no graph data.
- Release the current intent.
- Clean up local task state.
- Log only.

---

## Scheduling policy

### Global scheduling

Core rules:

1. Prioritize projects already running, but only tasks that can be dispatched immediately.
2. Continue bootstrap first when a running project is initial-state and eligible.
3. Otherwise, continue exploration if a running project has a dispatchable explore task.
4. If no running project has a dispatchable task and runtime.max_running_projects has capacity, start a new project.

The runtime.interval design contract:

- It is more than an ordinary polling interval.
- It deliberately provides one shared cadence for:
  - The Dispatcher's main loop.
  - Heartbeats for claimed tasks (bootstrap and explore).
- This reduces timing parameters and keeps the initial implementation simple.
- It is an explicit design choice, not accidental coupling.

Scheduling pseudocode can remain at this level:

```text
for project in running_projects_round_robin:
  if has_dispatchable_bootstrap(project):
    dispatch_bootstrap(project)
    continue
  if has_dispatchable_reason(project):
    dispatch_reason(project)
    continue
  if has_dispatchable_explore(project):
    dispatch_explore(project)
    continue

if running_project_count < runtime.max_running_projects:
  maybe_start_one_new_project()
```

### Scheduling within a project

After reading the full project state, schedule in this order:

1. In the initial state, choose by project.bootstrap_enabled and worker capabilities: use reason if bootstrap is disabled or unsupported, otherwise bootstrap. Continue bootstrap if its reserved intent already exists.
2. Prioritize reason when a new-situation retrigger condition is met.
3. Otherwise dispatch explore if an unclaimed intent exists.
4. Deduplicate reason by situation, not every graph change: trigger initially only with no open intents; retrigger only when Fact/Hint counts increase or the project transitions from open intents to none.
5. If reason returns data.complete, call `POST /projects/{project_id}/complete`.
6. If it returns intent without data.complete, call `POST /projects/{project_id}/intents`.
7. If it returns neither, do not write the graph this round.

Additionally:

- If the initial-state bootstrap path is selected and its intent is claimed, do not dispatch reason or ordinary explore in that round.
- A project with an explore task already running may also receive a reason task.
- This requires new Facts, Hints, or another qualifying situation; a newly created intent alone must not immediately trigger reason again.
- There must still be no unclaimed intent, no other reason task in the project, and capacity under runtime.max_project_workers.

Keep reason deduplication simple:

- Record the Fact count, Hint count, and presence of open intents at the last successful reason completion.
- Establish a baseline checkpoint for observed active projects with open intents and no checkpoint. This applies both at startup and to projects arriving later, so the first new Fact/Hint during execution is not swallowed.
- With no history and no open intents, trigger immediately.
- Thereafter, trigger only when Fact/Hint counts increase or open intents drop to none.
- An increased total intent count is not a trigger: it usually means the previous reason task just created an intent, not that the situation changed.
- Explore failures, missed heartbeats, and temporary releases return intents to the exploration queue without separately triggering reason.

### Concurrency limits

- Only one Dispatcher instance may schedule against a server in the current design.
- Multiple Dispatchers do not coordinate local admission, worker health, concurrency counters, container cleanup, or bootstrap deduplication across processes, and are unsupported.
- At most one bootstrap task may run per project at a time.
- The Dispatcher makes a best effort to retain only one open bootstrap intent per initial-state project.
- At most one reason task may run per project at a time.
- Reason tasks may run concurrently across projects.
- Reason counts toward the project's runtime.max_project_workers.
- runtime.max_workers: maximum number of tasks running across the Dispatcher.
- runtime.max_running_projects: admission limit for active projects accepted by this Dispatcher process. An active project keeps its slot even when it temporarily has no dispatchable task, until it leaves active.
- runtime.max_project_workers: per-project concurrent task limit, counting bootstrap, reason, and explore alike.
- workers[].max_running: each Worker's concurrency limit; reaching it temporarily removes that Worker from dispatch selection.

---

## Worker configuration

### Field definitions

The Dispatcher takes the complete stdout text as the model's response body.

Worker-related runtime fields in dispatch.yaml are:

| Field | Meaning | Notes |
| --- | --- | --- |
| `name` | Static Worker identifier | Used as creator or worker in protocol writes |
| `type` | Worker driver name | Supports claudecode, codex, mock |
| `task_types` | Supported tasks | bootstrap, reason, explore |
| `max_running` | Worker concurrency limit | No dispatch when the limit is reached |
| `priority` | Selection priority | Lower numbers take precedence |
| `env` | Runtime environment variables | Used by the driver; mock phase delays and outcome probabilities are configured here too |

The system provides three Worker drivers:

- `claudecode`
- `codex`
- `mock`

In other words:

- dispatch.yaml declares the driver, supported tasks, concurrency, and environment. For mock, each phase's simulated distribution also lives in env.
- Driver code implements health checks, command startup, session extraction, and conclude resumption.

### Driver interface

Each agent/CLI tool has its own driver file implementing a shared interface.

The interface should cover at least:

- `check_health(worker, *, timeout)`: issue an in-process HTTP health probe and return HealthResult.
- `prepare_session()`: pregenerate a session ID when required.
- `build_execute(worker, prompt, session)`: build the first-phase command.
- `extract_session(session, stderr)`: extract the session ID from stderr when needed, or keep the pregenerated session.
- `build_conclude(worker, prompt, session)`: resume the same session for two-phase explore conclusion.
- `supports_conclude()`: declare support for two-phase explore.

Driver capability contracts:

- claudecode supports two-phase explore.
- codex supports two-phase explore.
- mock supports two-phase explore.

Concurrency model:

- One Worker represents one independent LLM concurrency quota.
- Sharing one key across multiple Workers is not considered.
- Use workers[].max_running to control concurrency.

### Health checks

Health checks use the driver's check_health implementation. The Dispatcher calls HTTP directly in its own process using requests, rather than running curl inside the container.

The goal is to validate the Worker's actual LLM configuration, not merely that its container is alive:

- The base URL is reachable.
- The API key is valid.
- The model can be called.

Each driver follows its provider's protocol: claudecode calls `{base}/v1/messages`, codex calls `{base}/responses`, and pi selects `/chat/completions`, `/responses`, or `/v1/messages` according to PI_PROVIDER_API. Requests use the Worker's http(s)_proxy environment so the probe follows the same outbound path as the Worker.

Execution rules:

- In per-task health-check mode, check before starting each worker process.
- Do not repeat health checks when explore enters explore_conclude after timeout or parse failure. Attempt conclusion directly and rely on JSON/schema validation before writing results.
- HTTP 2xx is healthy.
- Non-2xx responses, connection failures, and timeouts are unhealthy; runtime.healthcheck_timeout controls the requests timeout.
- Failure discards this task and puts the Worker into a brief unavailability window.
- After that window, check again when a later round selects the Worker.
- The Dispatcher records a local retry_after for recently failed Workers and does not dispatch to them before it expires.

The Dispatcher checks only HTTP status, not the response body. Local mode makes no API calls because the CLI manages its own authentication; instead, startup probes each CLI with --help.

### CLI integration contracts

#### `claudecode` driver

Required environment variables:

- `ANTHROPIC_MODEL`
- `ANTHROPIC_BASE_URL`
- `ANTHROPIC_AUTH_TOKEN`

The health check is equivalent to this request, implemented as HTTP inside the Dispatcher process rather than curl in the container:

```bash
curl -sS --fail -o /dev/null \
  "$ANTHROPIC_BASE_URL/v1/messages" \
  -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" \
  -H "content-type: application/json" \
  -d "{\"model\":\"$ANTHROPIC_MODEL\",\"max_tokens\":10,\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}"
```

Known behavior:

- The driver pregenerates a session ID.
- The first invocation can specify that ID.
- If it already exists, the command errors instead of reusing the old session.
- The Dispatcher reads all stdout as the response body.

First-phase execution:

```bash
claude --session-id "{session}" --dangerously-skip-permissions -p -- "{prompt}"
```

Second-phase conclusion:

```bash
claude -r "{session}" --dangerously-skip-permissions -p -- "{prompt}"
```

#### `codex` driver

Required environment variables:

- `CODEX_MODEL`
- `CODEX_BASE_URL`
- `OPENAI_API_KEY`

The health check is equivalent to this request, implemented as HTTP inside the Dispatcher process rather than curl in the container:

```bash
curl -sS --fail -o /dev/null \
  "$CODEX_BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "content-type: application/json" \
  -d "{\"model\":\"$CODEX_MODEL\",\"max_tokens\":10,\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}"
```

Known behavior:

- The first invocation prints its session ID to stderr.
- Extract it with the regex `session id:\s*([0-9a-fA-F-]+)`.
- The Dispatcher reads all stdout as the response body.

First-phase execution:

```bash
codex exec --dangerously-bypass-approvals-and-sandbox --model "{env.CODEX_MODEL}" \
  -c 'model_provider="board"' \
  -c 'model_providers.board.name="board"' \
  -c 'model_providers.board.wire_api="responses"' \
  -c 'model_reasoning_effort="high"' \
  -c 'model_providers.board.base_url="{env.CODEX_BASE_URL}"' \
  -c 'model_providers.board.env_key="OPENAI_API_KEY"' \
  -- "{prompt}"
```

Second-phase conclusion:

```bash
codex exec resume "{session}" --dangerously-bypass-approvals-and-sandbox --model "{env.CODEX_MODEL}" \
  -c 'model_provider="board"' \
  -c 'model_providers.board.name="board"' \
  -c 'model_providers.board.wire_api="responses"' \
  -c 'model_reasoning_effort="high"' \
  -c 'model_providers.board.base_url="{env.CODEX_BASE_URL}"' \
  -c 'model_providers.board.env_key="OPENAI_API_KEY"' \
  -- "{prompt}"
```

#### `mock` driver

The mock driver supports local observation of dispatcher success, failure, and timeout paths.

Behavior contract:

- With runtime.prompt_group: mock, prompts are structured JSON rather than natural-language instructions.
- Reason prompts include at least phase, fact_ids, and open_intents.
- Explore/conclude prompts include at least phase and intent_id.
- Bootstrap/bootstrap_conclude prompts include at least phase, origin, goal, and hints.
- The driver reads phase from the prompt, then selects a simulated outcome using the corresponding MOCK_<PHASE> JSON environment variable.
- Each phase's JSON configures delay: [min, max] in seconds, including fractional values. A sampled delay exceeding the Dispatcher's outer timeout naturally produces a timeout.
- Reason.noop participates in sampling only when open_intents is nonempty; mock avoids outcomes invalid in the current context.

Mock supports six phases:

- `healthcheck`
- `bootstrap`
- `bootstrap_conclude`
- `reason`
- `explore_execute`
- `explore_conclude`

Supported outcomes:

- `healthcheck.outcomes`: `ok`, `fail`
- `bootstrap.outcomes`: `fact`, `rejected`, `invalid_json`, `invalid_payload`, `command_fail`
- `bootstrap_conclude.outcomes`: `fact`, `rejected`, `invalid_json`, `invalid_payload`, `command_fail`
- `reason.outcomes`: `complete`, `intent`, `noop`, `rejected`, `invalid_json`, `invalid_payload`, `command_fail`
- `explore_execute.outcomes`: `fact`, `rejected`, `invalid_json`, `invalid_payload`, `command_fail`
- `explore_conclude.outcomes`: `fact`, `rejected`, `invalid_json`, `invalid_payload`, `command_fail`

Naming conventions:

- One variable per phase: MOCK_<PHASE>, for example MOCK_REASON.
- Its value must be a JSON object shaped as `{"delay":[min,max],"outcomes":{...}}`.
- Delay contains two nonnegative numbers in seconds.
- Outcome probabilities for each phase are decimals from 0 to 1 and must sum exactly to 1.0.

### Configuration validation

#### Load-time validation

On Dispatcher startup, perform these static checks:

- dispatch.yaml exists and is readable.
- runtime.max_workers is present.
- runtime.max_running_projects is present.
- runtime.max_project_workers is present.
- runtime.interval is present.
- runtime.healthcheck_timeout is present.
- If present, runtime.worker_healthcheck is startup_and_task, startup_only, or disabled.
- runtime.prompt_group is present.
- tasks.bootstrap.timeout is present.
- tasks.bootstrap.conclude_timeout is present.
- tasks.reason.timeout is present.
- tasks.explore.timeout is present.
- tasks.explore.conclude_timeout is present.
- Every Worker has a type.
- Every Worker has max_running.
- Task_types contains only bootstrap, reason, and explore.
- Type is a supported driver, such as claudecode, codex, or mock.
- Max_running is a positive integer.
- Claudecode, codex, and mock support two-phase explore.
- The prompt directory selected by runtime.prompt_group exists.
- Prompt resources are present in the code package.
- In the default group, reason.md includes at least `{graph_yaml}`, `{fact_ids}`, and `{open_intents}`.
- In the default group, explore.md includes at least `{graph_yaml}`, `{intent_id}`, and `{intent_description}`.
- In the default group, bootstrap.md and bootstrap_conclude.md include at least `{origin}`, `{goal}`, and `{hints}`.
- In the mock group, reason.md includes at least `{fact_ids}` and `{open_intents}`.
- In the mock group, explore.md and explore_conclude.md include at least `{intent_id}`.
- In the mock group, bootstrap.md and bootstrap_conclude.md include at least `{origin}`, `{goal}`, and `{hints}`.
- Mock Worker MOCK_* variable names use only supported phases.
- Each mock phase's probabilities lie between 0 and 1 and sum exactly to 1.0.

#### Runtime validation

When actually dispatching a task, also validate:

- The driver exists and supports the task type.
- With runtime.worker_healthcheck=startup_and_task, the driver's health check succeeds before launch.
- Explore_conclude fallback after timeout/parse failure does not repeat health checks; it proceeds directly to conclusion and result validation.
- Only Workers supporting the current task type are selectable.
- Only Workers below max_running are selectable.
- Unhealthy Workers inside their local retry_after window are not selectable.
- Bootstrap and explore must claim successfully before their task thread starts.
- Two-phase explore needs a session ID from the first phase before entering conclude.
- Two-phase bootstrap needs a session ID from the first phase before entering bootstrap_conclude.
- Parse the entire stdout as JSON and validate its task-specific structure.
- Log accepted: false, invalid JSON, missing fields, and failed API writes without immediate retries.

---

## Configuration field reference

### `dispatch.yaml`

| Field | Required | Meaning |
| --- | --- | --- |
| `server` | Yes | Strikebox Server base URL |

### `runtime.*`

| Field | Required | Meaning |
| --- | --- | --- |
| `runtime.max_workers` | Yes | Maximum total running Dispatcher tasks |
| `runtime.max_running_projects` | Yes | Active projects admitted during this Dispatcher process, unaffected by historical containers |
| `runtime.max_project_workers` | Yes | Per-project concurrency limit across bootstrap, reason, and explore |
| `runtime.interval` | Yes | Shared main-loop interval and heartbeat period for claimed tasks |
| `runtime.healthcheck_timeout` | Yes | Common outer watchdog timeout for worker health checks |
| `runtime.worker_healthcheck` | No | startup_and_task, startup_only, or disabled; defaults to startup_only |
| `runtime.execution` | No | container (default) or local; local runs host subprocesses using native CLIs and checks their availability at startup |
| `runtime.prompt_group` | Yes | Selected prompt-group directory name |

### `container.*`

Required only with runtime.execution: container (the default).

| Field | Required | Meaning |
| --- | --- | --- |
| `container.image` | Yes | Project container image |
| `container.network_mode` | Yes | Project container network mode |
| `container.completed_action` | Yes | Container action after project completion |

Allowed container.completed_action values:

- remove: delete the container after completion.
- stop: stop the container but retain its state.

Implementation contract:

- Completed-project container cleanup may run asynchronously and concurrently without blocking the main loop.
- If the project has been deleted from the Server, the Dispatcher treats its board-dispatch-* container as orphaned and removes it during cleanup.

### `local.*`

Effective only with runtime.execution: local; ignored in container mode. Container settings and LLM environment variables are unnecessary. At startup, the Dispatcher probes each configured worker CLI with --help and exits with an error if all are missing.

| Field | Required | Meaning |
| --- | --- | --- |
| `local.workspace_root` | No | Legacy alias for evidence_root; each project uses an isolated `<root>/<project_id>/workspace/` as its worker cwd. Defaults to `~/.local/share/board/projects` when neither root is set |
| `local.completed_action` | No | Action on project completion: keep (default, retain state) or remove (delete the project directory) |

### `tasks.*`

| Field | Required | Meaning |
| --- | --- | --- |
| `tasks.bootstrap.timeout` | Yes | Bootstrap first-phase timeout |
| `tasks.bootstrap.conclude_timeout` | Yes | Bootstrap second-phase conclusion timeout |
| `tasks.reason.timeout` | Yes | Reason timeout |
| `tasks.explore.timeout` | Yes | Explore first-phase timeout |
| `tasks.explore.conclude_timeout` | Yes | Explore second-phase conclusion timeout |

### `workers.*`

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | Yes | Static Worker ID, used as creator or worker in protocol writes |
| `type` | Yes | Worker driver name, such as claudecode, codex, or mock |
| `task_types` | Yes | Tasks supported by this Worker |
| `max_running` | Yes | This Worker's concurrency limit |
| `priority` | Yes | Lower values take precedence among candidates for the current task type |
| `env` | Yes | Worker's environment; each driver defines required keys and validates them at startup |

Additional notes:

- Filter Workers by task type, max_running, and retry_after first. Then choose by lowest priority, fewest running tasks, and finally randomly. Bootstrap and explore claim before launch. With startup_and_task health checks, probe before launch and temporarily exclude failed Workers. Do not repeat checks on bootstrap_conclude/explore_conclude fallback.
- Each driver implements health checks, command execution, session extraction, and second-phase conclusion.
- Load prompt content from Markdown resources in the code package.

---

## Appendix: example configuration and prompts

### `dispatch.example.yaml`

```yaml
server: "http://127.0.0.1:8000"

runtime:
  max_workers: 5  # total running tasks
  max_running_projects: 3  # total active projects admitted by this dispatcher runtime
  max_project_workers: 2  # per-project running tasks, including bootstrap + reason + explore
  interval: 3  # intentional shared cadence: scheduler loop interval + claim-task heartbeat interval, in seconds
  healthcheck_timeout: 15  # shared watchdog for all worker healthchecks, in seconds
  worker_healthcheck: "startup_only"  # startup_and_task | startup_only | disabled
  prompt_group: "default"  # selects prompts/<group>/

tasks:
  bootstrap:
    timeout: 120
    conclude_timeout: 30
  reason:
    timeout: 45
  explore:
    timeout: 600
    conclude_timeout: 120

container:
  image: "tmp:latest"
  network_mode: "host"
  completed_action: "stop"  # options: "remove" | "stop"

common_env:
  TSEC_BASE_URL: "http://<SERVER_HOST>/api"
  TSEC_AGENT_TOKEN: "..."

workers:
  # Split the same model into multiple Workers only when they use different API keys
  # and therefore have independent concurrency quotas.
  - name: "claude-sonnet-thinker"
    type: "claudecode"
    task_types: [bootstrap, reason]
    max_running: 1
    priority: 0  # lower number wins; ties prefer fewer running tasks, then choose randomly
    env:
      ANTHROPIC_MODEL: "claude-sonnet-4-6"
      ANTHROPIC_BASE_URL: "https://api.example.com"
      ANTHROPIC_AUTH_TOKEN: "sk-ant-worker-a"

  - name: "claude-sonnet-doer"
    type: "claudecode"
    task_types: [bootstrap, explore]
    max_running: 1
    priority: 1
    env:
      ANTHROPIC_MODEL: "claude-sonnet-4-6"
      ANTHROPIC_BASE_URL: "https://api.example.com"
      ANTHROPIC_AUTH_TOKEN: "sk-ant-worker-b"

  - name: "codex-gpt54"
    type: "codex"
    task_types: [bootstrap, reason, explore]
    max_running: 1
    priority: 3
    env:
      CODEX_MODEL: "gpt-5.4"
      CODEX_BASE_URL: "https://api.example.com/v1"
      OPENAI_API_KEY: "sk-worker-c"

  - name: "codex-gpt54-alt"
    type: "codex"
    task_types: [bootstrap, explore]
    max_running: 1
    priority: 4
    env:
      CODEX_MODEL: "gpt-5.4"
      CODEX_BASE_URL: "https://api.example.com/v1"
      OPENAI_API_KEY: "sk-worker-d"

  - name: "mock-observer"
    type: "mock"
    task_types: [bootstrap, reason, explore]
    max_running: 1
    priority: 9
    env:
      MOCK_HEALTHCHECK: '{"delay":[0.05,0.15],"outcomes":{"ok":0.9,"fail":0.1}}'
      MOCK_BOOTSTRAP: '{"delay":[0.1,12.0],"outcomes":{"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
      MOCK_BOOTSTRAP_CONCLUDE: '{"delay":[0.1,2.2],"outcomes":{"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
      MOCK_REASON: '{"delay":[0.1,2.2],"outcomes":{"complete":0.1,"intent":0.3,"noop":0.1,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.2}}'
      MOCK_EXPLORE_EXECUTE: '{"delay":[0.1,12.0],"outcomes":{"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
      MOCK_EXPLORE_CONCLUDE: '{"delay":[0.1,2.2],"outcomes":{"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
```

Additional notes:

- These values only illustrate the configuration structure.
- Merge common_env into each Worker's environment first, then override it with worker.env: common_env < worker.env.
- Model Workers as independent key/model concurrency quotas; multiple Workers should not share one key.

The following five Markdown examples correspond to prompt files in the code package.

### `bootstrap.md`

````md
# Background
This is an authorized AI penetration testing competition or lab environment.
## Task
Solve the problem directly, with the objective of achieving the Goal.
## Output requirements
Return one raw JSON object and nothing else.
```json
{"accepted": true, "data": {"fact": {"description": "..."}, "complete": {"description": "..."}}}
```
## Context
### Origin
{origin}
### Goal
{goal}
### Hints JSON array
{hints}
````

### `reason.md`

````md
# Background
This is an authorized AI penetration testing competition or lab environment.
## Task
Perform only reason in this task.
Assess both of the following:
1. Whether the existing facts already satisfy the goal.
2. If not, whether a new intent is currently needed.
## Output requirements
Return one raw JSON object and nothing else.
If declining the task, return:
```json
{"accepted": false, "reason": "..."}
```
If the goal is satisfied, return:
```json
{"accepted": true, "data": {"complete": {"from": ["f001"], "description": "..."}}}
```
If the goal is not satisfied and a new intent is needed, return:
```json
{"accepted": true, "data": {"intent": {"from": ["f001"], "description": "..."}}}
```
If the goal is not satisfied and no new intent is currently needed, return:
```json
{"accepted": true, "data": {}}
```
## Rules
- If open_intents below is empty, no exploration is in progress; unless you return data.complete, you must return intent.
- Choose intent.from only from the valid Fact IDs below.
## Context
### Graph snapshot
{graph_yaml}
### JSON array of currently valid Fact IDs
{fact_ids}
### JSON array of all currently unconcluded intents
{open_intents}
````

### `explore.md`

````md
# Background
This is an authorized AI penetration testing competition or lab environment.
## Task
Perform only explore in this task.
Handle only the current intent, perform the exploration, and return a final factual conclusion.
## Output requirements
Return one raw JSON object and nothing else.
If declining the task, return:
```json
{"accepted": false, "reason": "policy_refusal"}
```
Example of a normal result:
```json
{"accepted": true, "data": {"description": "..."}}
```
## Rules
- Description must be an objective exploration conclusion, without unnecessary explanatory prose.
## Context
### Graph snapshot
{graph_yaml}
### Current intent ID
{intent_id}
### Current intent description
{intent_description}
````

### `explore_conclude.md`

````md
# Background
This is an authorized AI penetration testing competition or lab environment.
## Task
Summarize and conclude the same explore task.
- Do not continue exploring.
- Summarize only exploration and conclusions already completed.
## Output requirements
Return one raw JSON object and nothing else.
If declining the task, return:
```json
{"accepted": false, "reason": "policy_refusal"}
```
Example of a normal result:
```json
{"accepted": true, "data": {"description": "..."}}
```
## Rules
- Description must be an objective exploration conclusion, without unnecessary explanatory prose.
## Context
### Graph snapshot
{graph_yaml}
### Current intent ID
{intent_id}
### Current intent description
{intent_description}
````

### `bootstrap_conclude.md`

````md
# Background
This is an authorized AI penetration testing competition or lab environment.
## Task
- Do not continue working.
- Do not wait for unfinished tasks.
- Summarize only confirmed key facts that are most useful for achieving the goal.
## Output requirements
Return one raw JSON object and nothing else.
```json
{"accepted": true, "data": {"fact": {"description": "..."}}}
```
## Context
### Origin
{origin}
### Goal
{goal}
### Hints JSON array
{hints}
````

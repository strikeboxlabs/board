# Strikebox Collaborative Exploration Protocol

---

## Core idea

Strikebox models goal-directed exploration as a directed acyclic graph: starting from known facts, exploration continually produces new facts until a set of facts jointly satisfies the goal. Each Fact is a stepping stone: agents continue from their predecessors' discoveries and leave new discoveries for those who follow.

This API manages the graph's growth. Multiple consumers (humans or agents) read the complete graph concurrently, declare exploration intents, and produce new facts, until a consumer judges that the goal has been reached. For simple problems, a consumer may make a quick attempt directly from the origin and goal at project startup, without first splitting the work into multiple planning rounds. This immediate attempt is still recorded through the normal Intent/Fact graph.

The graph remains the sole representation of knowledge and causality. The protocol also allows a small amount of **project-level coordination state** for mutual exclusion and visibility between consumers. This state is not a fact, is not part of the graph, and does not participate in causal reasoning. The only built-in project-level coordination state is currently `Project.reason`, which indicates that a consumer is performing a project-wide `reason` assessment.

The system performs no reasoning or decision-making; it only maintains graph consistency.

### Database contention and heartbeat failures

SQLite BUSY/LOCKED errors, including their extended result codes, return HTTP 503
with `Retry-After: 1` and the JSON body
`{"detail":"Board database is busy; retry shortly.","code":"database_busy"}`.
Database lock waits are bounded to one second. A failed transaction rolls back;
the server does not replay the request. Unrelated SQL errors remain errors, and
ownership conflicts remain HTTP 409.

Project list/detail and graph/report GET handlers commit lease-expiry maintenance
before opening a consistent, query-only read snapshot. Hashing and rendering do
not retain the maintenance write lock. Claim and renewal checks still run in
atomic writer transactions, with expiry clocks sampled after lock acquisition.

The dispatcher bounds heartbeat renewal requests and any transient retries within
its existing client confirmation window; it does not extend that window. HTTP
403/409 stops renewal immediately, while a separate watchdog initiates stopping
if the client window expires during a blocked request. Late responses cannot
revive a failed or stopped lease. See [heartbeat policy and limits](../planner-efficiency.md).
Intent heartbeat failures preserve a sanitized HTTP/transport diagnostic in
`dispatch_error`, the timeline and the durable error history. HTTP response bodies
are excluded from that diagnostic, and the existing intent fail-stop remains in
effect.

### Design origins

This protocol is a modern reconstruction of **Blackboard Architecture**. CMU's Hearsay-II system introduced the same paradigm in the 1970s: multiple experts work around a shared blackboard, each reading the current state and contributing knowledge, without central scheduling. Facts correspond to blackboard content, Intents to experts' action records, and Hints to annotations. Unlike a classic blackboard that holds only the current state, this protocol preserves the complete causal chain through Intents, making the graph both a knowledge base and an audit log of the reasoning path.

Its coordination mechanism resembles an ant colony's **pheromone mechanism (stigmergy)**: individuals coordinate indirectly by changing a shared environment instead of communicating directly. Agents reading the graph and writing Facts leave signals that other agents use to make decisions.

Each consumer's work cycle (read graph → assess → declare intent → execute → produce facts) forms an independent **OODA loop** (Observe-Orient-Decide-Act). Multiple loops synchronize indirectly through the shared graph. This resembles **Mission Command**: a superior specifies only the goal, units decide independently based on the situation, and Hints represent the commander's intent. The main advantage is that decision speed is not constrained by a central scheduling bottleneck.

### Representing changes in state

Facts are append-only. State changes are expressed by adding new Facts. For example, in penetration testing across multiple network layers:

- `f003`: "Obtained a root shell on host-A"
- `f007`: "Discovered internal network 10.0.2.0/24 through host-A as a pivot"
- `f025`: "The host-A shell has disconnected"

The disconnected shell is itself a new objective fact. An agent reading f003 and f025 assesses current reachability and declares a new exploration intent from f025 (reestablish the shell or use a different pivot). Historical Facts need no modification or invalidation marker: their chronological order already conveys the change in state.

---

## Core concepts

### Project

A problem instance with an explicit origin and goal, containing the complete graph.

Four states:

- `active`: in progress; accepts exploration writes.
- `stopped`: a hard stop; rejects exploration writes; can resume as `active`.
- `completed`: currently complete; rejects exploration writes. If external verification finds that completion was incorrect, explicitly call `reopen` to reverse completion and continue.
- `aborted`: a terminal state for a deliberately abandoned project whose goal was not reached. Ordinary status changes are blocked, as with `completed`, but `aborted` cannot be reopened. It records why the run was abandoned: the reason (`abort_reason`), time (`aborted_at`), and actor (`aborted_by`) are stored on the project and included in its timeline and reports.

### Fact

A graph node representing a confirmed objective fact. It contains descriptive text, with no state marker. Facts are append-only and retained permanently.

Descriptions should distill exploration conclusions instead of reproducing raw data. For large output, such as a scan, include key insights and a file reference to the raw data: "A full-port nmap scan found ports 22/80/443/8080 open; 80 and 8080 serve HTTP. See $BOARD_EVIDENCE_DIR/nmap_192.168.1.10.xml for the full results." This keeps the graph lightweight while preserving traceability to the raw data.

Every Project has two special Facts, written when the Project is created:

- `origin`: the starting point.
- `goal`: the desired endpoint.

Ordinary Facts receive system-generated IDs, such as `f001`.

### Intent

A graph edge representing exploration from one or more Facts. It has three states:

- **In progress**: a consumer has claimed the Intent; `worker` is not null.
- **Unclaimed**: the Intent has no conclusion and `worker` is null, awaiting a consumer.
- **Concluded**: `to` is not null; exploration is complete and has produced a conclusion Fact.

An unconcluded Intent (`to=null`) is a declared exploration direction without a conclusion. It may be declared without a claim or claimed at creation. Concluded Intents cannot be modified.

The protocol does not distinguish Intent types; the server treats all Intents equally. Consumers may agree on **reserved semantics**. For example, an open Intent with:

- `from = ["origin"]`
- `description = "bootstrap"`

can represent a direct attempt to make progress at project startup. Such an Intent follows the same heartbeat / release / conclude lifecycle and needs no additional endpoints. Consumers interpret it as: analyze and execute directly, record the key supporting fact, then declare complete if that fact satisfies the goal. It is not an ordinary narrowly scoped exploration direction.

This convention suits problems that may finish quickly, or yield an early breakthrough. The UI and audit record still show the initial `bootstrap` attempt and its key facts. Useful results such as flags, shells, and proof of privileges remain in the fact graph even when the project completes directly.

**Hyperedge semantics:** `from` accepts multiple Fact IDs. With multiple IDs, an Intent is equivalent to a hyperedge: several source nodes jointly point to one destination, sharing the same description, creator, and worker:

```
f002 ──┐
       ├──(intent)──→ f006
f004 ──┘
```

This preserves the causal relationship in which multiple known facts support one exploration, without selecting a single primary Fact and losing the remaining context.

### Hint

Input outside the graph: strategic advice or supplementary notes. Hints are not part of the fact graph and do not affect causality; consumers consult them when reading the graph.

Hints provide external, higher-level input rather than exploration execution. They can therefore be added while a project is `stopped` or `completed`. After a stopped project resumes as `active`, consumers see the new Hints on their next read. Hints added to completed projects are retained; continuing the project still requires `reopen` to return it to `active`.

Besides human strategic guidance, Hints can convey **situational assessments** between consumers. As the graph grows, ruled-out directions can dilute useful information. After exploring, a consumer can add an assessment such as "SSH and SQL injection have been ruled out; the remaining attack surface is concentrated in file upload." This helps subsequent consumers focus quickly and reduces the cognitive cost of each graph read.

---

## Data structures

### Project

```
id
title           # Project name
status          # "active" | "stopped" | "completed"
bootstrap_enabled  # Allow consumers to bootstrap the initial state; defaults to true
created_at
reason          # Current project-level reason lease; null means no reason task is running
planner_wait    # Last valid wait {reason, since, worker, input_signature}; null otherwise
completion_outcome # null while open; goal_met or current_results when completed
```

### Project.reason

```
worker             # Consumer currently running reason
trigger            # Consumer-defined trigger for this reason task
started_at         # When this reason task started
last_heartbeat_at  # Time of the latest reason heartbeat
lease_id           # UUID fencing decisions to this lease
```

`POST /projects/{id}/reason/wait` accepts `worker`, `lease_id`, `reason` and
`input_signature` (SHA-256 of the stable planner inputs). It requires an active
project and the current lease. A changed lease or graph returns `409`; inactive
projects return `403`. The persisted reason survives release and server restarts.
New evidence, work, job status or execution policy clears it; lease heartbeats do
not, and no-op edits preserve it. A fresh planning claim also clears it. The UI treats this as a planner wait,
not a suspended project or proof that other workers have stopped.

Reason heartbeat and release accept optional `lease_id` for compatibility with
older clients. New clients send the current UUID; stale UUIDs return `409` even
when the worker name is unchanged.

`POST /projects/{id}/complete` accepts optional `outcome` (`goal_met`, the default,
or `current_results`). The latter is an explicit operator conclusion without a
claim that the original goal was met, and returns `409` while unfinished,
non-excluded intents, running jobs, unresolved background stop acknowledgements
or an active planner remain. Both outcomes use
the completed lifecycle, report generation and existing reopen endpoint. Reports
and graph snapshots carry the outcome so downstream consumers must preserve it.

### Fact

```
id              # "origin" | "goal" | system-generated ID (for example, f001)
description     # Description of the objective fact
```

### Intent

```
id
from            # Array of source Fact IDs, with at least one entry
to              # Conclusion Fact ID; null means not yet concluded
description     # Intent description
creator         # Consumer declaring the intent; immutable after creation
worker          # Executor identifier; meaning depends on state (see below)
last_heartbeat_at  # Last heartbeat; relevant only before conclusion; retained permanently
created_at
concluded_at    # Conclusion time; null means not yet concluded
```

`creator` is set at creation, identifies the consumer that proposed this exploration direction, and is immutable. `worker` may be null. If the Intent is claimed at creation, `worker` must equal `creator`, and the initial `last_heartbeat_at` is recorded.

The server trims leading and trailing whitespace from request text fields (including `title`, `content`, `description`, `creator`, `worker`, and `trigger`) and Fact IDs in `from`. A value that is empty after trimming returns `422`.

Consumers using the `bootstrap` convention commonly use a fixed `creator`, such as `dispatcher.bootstrap`, to distinguish it from ordinary exploration intents in the graph. This is a consumer convention, not a server constraint.

**Meaning of the worker field:**

| Intent state | Meaning of worker |
| --------------------- | -------------------------------- |
| Unconcluded, worker=null | No consumer is processing it; awaiting a claim |
| Unconcluded, worker set | A consumer is processing it |
| Unconcluded, worker timed out | Worker is cleared to null; awaiting a new claim |
| Concluded | Consumer that produced the conclusion; retained permanently |

Timeouts clear workers only on unconcluded Intents. Concluded Intents do not participate in timeout handling; their worker permanently identifies the consumer that produced the conclusion.

### Hint

```
id
content
creator         # Hint author
created_at
```

### Settings

```
intent_timeout  # Seconds without a heartbeat before worker is cleared to null
reason_timeout  # Seconds without a heartbeat before Project.reason is cleared to null
```

---

## API endpoints

### Global settings

#### GET /settings

Return the global configuration.

Response:

```json
{
  "intent_timeout": 5,
  "reason_timeout": 5
}
```

---

#### PUT /settings

Update the global configuration.

Body:

```json
{
  "intent_timeout": 5,
  "reason_timeout": 5
}
```

---

### Projects

#### GET /projects

Return all projects with metadata and summary statistics, without graph data.

Response:

```json
[
  {
    "id": "proj_001",
    "title": "Example penetration test",
    "status": "active",
    "bootstrap_enabled": true,
    "created_at": "2026-03-21T10:00:00Z",
    "reason": {
      "worker": "dispatcher-worker-A",
      "trigger": "new_facts",
      "started_at": "2026-03-21T10:06:00Z",
      "last_heartbeat_at": "2026-03-21T10:06:03Z"
    },
    "fact_count": 8,
    "intent_count": 5,
    "working_intent_count": 2,
    "unclaimed_intent_count": 1,
    "hint_count": 3
  }
]
```

---

#### POST /projects

Create a project. `origin` and `goal` become special Facts in `facts`. `hints` is optional. `bootstrap_enabled` is optional and defaults to `true`; when false, consumers skip bootstrap. Even when true, a consumer without bootstrap capability may proceed directly to reason.

Body:

```json
{
  "title": "Example penetration test",
  "origin": "Target http://192.168.1.10",
  "goal": "Capture the flag",
  "bootstrap_enabled": true,
  "hints": [
    { "content": "Prioritize web services", "creator": "human" },
    { "content": "Pay attention to port 80", "creator": "human" }
  ]
}
```

---

#### GET /projects/{project_id}

Return the complete project, including facts, intents, and hints.

Response:

```json
{
  "project": {
    "id": "proj_001",
    "title": "Example penetration test",
    "status": "active",
    "bootstrap_enabled": true,
    "created_at": "2026-03-21T10:00:00Z",
    "reason": {
      "worker": "dispatcher-worker-A",
      "trigger": "new_facts",
      "started_at": "2026-03-21T10:06:00Z",
      "last_heartbeat_at": "2026-03-21T10:06:03Z"
    }
  },
  "facts": [
    { "id": "origin", "description": "Target http://192.168.1.10" },
    { "id": "goal",   "description": "Capture the flag" },
    { "id": "f001",   "description": "Discovered open ports 22/80/443" }
  ],
  "intents": [
    {
      "id": "i001",
      "from": ["origin"],
      "to": "f001",
      "description": "Full-port nmap scan",
      "creator": "agent-A",
      "worker": "agent-A",
      "last_heartbeat_at": "2026-03-21T10:02:00Z",
      "created_at": "2026-03-21T10:01:00Z",
      "concluded_at": "2026-03-21T10:02:00Z"
    },
    {
      "id": "i002",
      "from": ["f001"],
      "to": null,
      "description": "HTTP service probing and directory scanning",
      "creator": "agent-A",
      "worker": "agent-B",
      "last_heartbeat_at": "2026-03-21T10:04:00Z",
      "created_at": "2026-03-21T10:03:00Z",
      "concluded_at": null
    }
  ],
  "hints": [
    { "id": "h001", "content": "Prioritize web services", "creator": "human", "created_at": "2026-03-21T10:00:00Z" },
    { "id": "h002", "content": "Pay attention to port 80", "creator": "human", "created_at": "2026-03-21T10:00:00Z" }
  ]
}
```

---

#### DELETE /projects/{project_id}

Delete the project and all its data.

If a Dispatcher still holds a running container for the project, the Server does not manage it directly. On a subsequent poll, the Dispatcher detects that the project no longer exists, treats it as `deleted`, cancels any local running tasks, and removes the orphan container.

---

#### PUT /projects/{project_id}/title

Change the project title. This does not change the graph, the `reason` lease, open intent claims, or project status. Titles can be changed while the project is `active`, `stopped`, or `completed`.

Body:

```json
{
  "title": "Example penetration test (review)"
}
```

Response:

```json
{
  "id": "proj_001",
  "title": "Example penetration test (review)",
  "status": "completed",
  "created_at": "2026-03-21T10:00:00Z",
  "reason": null
}
```

---

#### PUT /projects/{project_id}/status

Update project status. Only transitions between `active` and `stopped` are allowed. This endpoint cannot change a `completed` or `aborted` project; reversing completion requires the dedicated `reopen` endpoint, which applies only to `completed`. This is a project administration operation, not an exploration write.

When a project becomes `stopped`, the Server immediately clears `worker` to `null` on all unconcluded Intents and clears `project.reason`, invalidating their claims. On resumption, they can be reclaimed immediately without waiting for a timeout. `stopped` means a hard stop: the Server rejects subsequent exploration writes and clears open intent claims and the reason lease. Consumers receiving this signal should immediately cancel local tasks and stop the project container.

Known limitation: each Intent currently stores only its current claim holder in `intent.worker`. Stopping a project immediately clears that field on open Intents, so the project details no longer show which worker was last advancing each Intent. An Intent-level field such as `worker_history` could preserve that visibility in the future, but is not implemented in this version.

Body:

```json
{
  "status": "stopped"
}
```

Response:

---

#### POST /projects/{project_id}/abort

Mark the project `aborted`: a terminal state for a run that cannot or should not continue but must remain on record. `reason` is required; `actor` is optional and defaults to `human`. Allowed from `active` or `stopped`; `completed` and `aborted` return 409.

Claim cleanup matches `stopped`: immediately clear `worker` on all unconcluded Intents and clear `project.reason`; consumers then cancel local tasks. Unlike stopped, `aborted` is terminal: it cannot transition to `stopped` or `active`, and cannot be reopened. To continue, create a new project seeded from exports/evidence, or use a fork.

Project metadata gains three optional fields: `abort_reason`, `aborted_at`, and `aborted_by`. The timeline ends with `PROJECT ABORTED`. Human and agent report headers show the aborted status and reason. The human executive summary replaces "Overall posture" with "Outcome", states that the goal was not reached, and lists unexecuted intents.

When the Dispatcher detects `aborted` on its next poll, it automatically writes the complete report set (`report-agent.md`, `report-agent.json`, `report-human.md`, `timeline.txt`, `graph.yaml`) to the project's `reports/` directory. Abandoning a run preserves its record. The equivalent CLI command is `board abort <project_id> --reason "..." --actor F`, which aborts and immediately writes the reports.

Body:

```json
{
  "reason": "foreign-tenant account unobtainable",
  "actor": "F"
}
```

Response: the updated ProjectMeta, including `abort_reason`, `aborted_at`, and `aborted_by`.

```json
{
  "id": "proj_001",
  "title": "Example penetration test",
  "status": "stopped",
  "created_at": "2026-03-21T10:00:00Z",
  "reason": null
}
```

---

#### POST /projects/{project_id}/reason/claim

Claim the project-level `reason` lease. It is outside the graph and creates no Intent or Fact; it only indicates that a consumer is performing a project-wide reason assessment. Each project may have at most one reason lease at a time.

If `project.reason` is empty, the claim succeeds and records `worker`, `trigger`, `started_at`, and `last_heartbeat_at`. If another consumer holds an unexpired lease, return 409. If the same worker already holds it, return the current state idempotently. This is an exploration write, allowed only for `active` projects; `stopped` or `completed` returns 403.

Body:

```json
{
  "worker": "dispatcher-worker-A",
  "trigger": "new_facts"
}
```

Response:

```json
{
  "id": "proj_001",
  "title": "Example penetration test",
  "status": "active",
  "created_at": "2026-03-21T10:00:00Z",
  "reason": {
    "worker": "dispatcher-worker-A",
    "trigger": "new_facts",
    "started_at": "2026-03-21T10:06:00Z",
    "last_heartbeat_at": "2026-03-21T10:06:00Z"
  }
}
```

---

#### POST /projects/{project_id}/reason/heartbeat

The current worker renews the project-level `reason` lease, updating `last_heartbeat_at`. Only its current holder may renew. Return 409 if there is no holder, or if another consumer holds an unexpired lease. This is an exploration write, allowed only for `active` projects; `stopped` or `completed` returns 403.

Body:

```json
{
  "worker": "dispatcher-worker-A"
}
```

---

#### POST /projects/{project_id}/reason/release

The current worker voluntarily releases the project-level reason lease, immediately making it available. Only the holder may release it. If already empty, return the current state idempotently; if another consumer holds an unexpired lease, return 409. This is an exploration write, allowed only for `active` projects; `stopped` or `completed` returns 403.

Body:

```json
{
  "worker": "dispatcher-worker-A"
}
```

---

### Hints

#### POST /projects/{project_id}/hints

Add a strategic Hint to the project. This is an external-input write, permitted for `active`, `stopped`, and `completed` projects.

Body:

```json
{
  "content": "Try bypassing file upload restrictions",
  "creator": "agent-A"
}
```

---

### Intents

#### POST /projects/{project_id}/intents

Declare an exploration direction from a Fact, creating an unconcluded Intent. It may be declared alone or claimed at creation. This is an exploration write, allowed only for `active` projects; `stopped` or `completed` returns 403.

Body:

```json
{
  "from": ["f001"],
  "description": "HTTP service probing and directory scanning",
  "creator": "agent-A",
  "worker": null
}
```

`from` is an array containing at least one Fact ID. `worker` may be `null` (declare without claiming) or equal to `creator` (declare and claim immediately). When multiple Facts support one exploration, include all of them in `from` to preserve causality. For example:

`from` must not contain `goal`.

The server does not validate the business meaning of `description`; a reserved intent such as `description = "bootstrap"` is still an ordinary Intent. Consumers decide whether to interpret it as a direct attempt at project startup.

```json
{
  "from": ["f002", "f004"],
  "description": "Use credentials from f004 to log into the admin interface discovered in f002",
  "creator": "agent-A",
  "worker": "agent-A"
}
```

Response: the newly created Intent.

---

#### POST /projects/{project_id}/intents/{intent_id}/heartbeat

A consumer reports liveness, updating `worker` and `last_heartbeat_at`. Valid only for unconcluded Intents. Use it to renew a claim or take over an Intent that has timed out and become unclaimed. If `worker` is null, any consumer may claim it by heartbeat. If another consumer holds an unexpired claim, return 409. Heartbeat is an exploration write, allowed only for `active` projects; `stopped` or `completed` returns 403.

Consumers normally send periodic heartbeats or poll the project. Once the project is `stopped` or `completed`, heartbeats return 403 and polling reveals the changed status. Because the Server immediately clears open-intent workers when stopped, consumers should treat that as a hard-stop signal and terminate local work promptly instead of relying on stale claims.

Body:

```json
{
  "worker": "agent-B"
}
```

---

#### POST /projects/{project_id}/intents/{intent_id}/release

The current worker voluntarily releases an unconcluded Intent, making it immediately available for another consumer. Only the current holder may release it; another consumer's unexpired claim returns 409. If already unclaimed, return the current state idempotently. Retain `last_heartbeat_at`. Release is an exploration write, allowed only for `active` projects; `stopped` or `completed` returns 403.

Body:

```json
{
  "worker": "agent-B"
}
```

Response: the updated Intent.

---

#### POST /projects/{project_id}/intents/{intent_id}/conclude

Finish exploration, create a new Fact, and conclude the Intent atomically. Valid only for unconcluded Intents. Claim rules match heartbeat: an unclaimed Intent can be concluded directly, the current worker may conclude, and another consumer's unexpired claim returns 409. Conclude is an exploration write, allowed only for `active` projects; `stopped` or `completed` returns 403.

The recorded worker is retained permanently to identify who produced the conclusion.

Body:

```json
{
  "worker": "agent-B",
  "description": "Port 80 runs nginx 1.18 and is vulnerable to directory traversal"
}
```

Response:

```json
{
  "fact": {
    "id": "f002",
    "description": "Port 80 runs nginx 1.18 and is vulnerable to directory traversal"
  },
  "intent": {
    "id": "i002",
    "from": ["f001"],
    "to": "f002",
    "description": "HTTP service probing and directory scanning",
    "creator": "agent-A",
    "worker": "agent-B",
    "last_heartbeat_at": "2026-03-21T10:05:00Z",
    "created_at": "2026-03-21T10:03:00Z",
    "concluded_at": "2026-03-21T10:05:00Z"
  }
}
```

---

#### POST /projects/{project_id}/complete

Declare completion when a consumer judges that a set of Facts jointly satisfies the goal. This is atomic and is an exploration write, allowed only for `active` projects; `stopped` or `completed` returns 403.

Create a concluded Intent whose `from` lists the supporting Facts and whose `to` is `goal`. Because declaring completion involves no exploration, the server sets both `creator` and `worker` to the request's worker. Set project status to `completed` and immediately clear `project.reason`.

If later external verification disproves completion, call `POST /projects/{project_id}/reopen` to reverse the completion edge and record corrective information as a new Fact before continuing exploration.

`from` must not contain `goal`.

Body:

```json
{
  "from": ["f007", "f012", "f015"],
  "description": "All three shells have been obtained, satisfying the goal",
  "worker": "agent-A"
}
```

---

#### POST /projects/{project_id}/reopen

Explicitly reverse completion when external verification shows that the project is unfinished. This is a project administration operation, not an exploration write, and is allowed only for `completed` projects.

The server:

- Finds the unique completion edge with `to=goal`.
- Reads its original `from`.
- Deletes that edge, without retaining an in-graph record of the previous completion.
- Creates an ordinary Fact containing the caller's objective corrective explanation.
- Creates a concluded Intent with the fixed description `external_feedback`, inheriting the original completion edge's `from` and pointing `to` the new Fact.
- Sets both `creator` and `worker` to the request's creator.
- Returns the project to `active`.
- Clears `project.reason`.

This supports external feedback such as "The submitted flag is incorrect; continue searching for the correct flag." The feedback becomes a new Fact in the graph; `reopen` is simply the control operation that triggers this rewrite.

Body:

```json
{
  "description": "FLAG{fake} is incorrect; continue searching for the correct flag",
  "creator": "judge"
}
```

Response:

```json
{
  "project": {
    "id": "proj_001",
    "title": "Example penetration test",
    "status": "active",
    "created_at": "2026-03-21T10:00:00Z",
    "reason": null
  },
  "fact": {
    "id": "f016",
    "description": "FLAG{fake} is incorrect; continue searching for the correct flag"
  },
  "intent": {
    "id": "i021",
    "from": ["f008"],
    "to": "f016",
    "description": "external_feedback",
    "creator": "judge",
    "worker": "judge",
    "last_heartbeat_at": "2026-03-21T10:10:00Z",
    "created_at": "2026-03-21T10:10:00Z",
    "concluded_at": "2026-03-21T10:10:00Z"
  }
}
```

---

### Export

#### GET /projects/{project_id}/export?format=yaml

Return a structured YAML snapshot of the project graph for consumers. It omits `last_heartbeat_at`. Unconcluded Intents (`to=null`) are included, with worker indicating whether someone is currently processing them. `project.reason` is omitted because it is coordination state outside the fact graph. The server still performs timeout cleanup on reads, so the export reflects the resulting current state. `created_at` and `concluded_at` use the server's current timezone and the format `YYYY-MM-DD HH:mm:ss`.

```yaml
project:
  title: "Example penetration test"
  origin: "Target http://192.168.1.10"
  goal: "Capture the flag"
  bootstrap_enabled: true

hints:
  - content: "Prioritize web services"
    creator: "human"
    created_at: "2026-03-21 18:00:00"
  - content: "Pay attention to port 80"
    creator: "human"
    created_at: "2026-03-21 18:00:00"

facts:
  - id: origin
    description: "Target http://192.168.1.10"
  - id: goal
    description: "Capture the flag"
  - id: f001
    description: "Discovered open ports 22/80/443"
  - id: f002
    description: "Port 80 runs nginx 1.18 and is vulnerable to directory traversal"
  - id: f003
    description: "Directory traversal revealed /backup/db.sql, containing the users table"
  - id: f004
    description: "Obtained credentials admin/passwd123"
  - id: f005
    description: "SSH login failed; the credentials are not shared"
  - id: f006
    description: "Logged into the web admin interface and discovered file upload functionality"
  - id: f007
    description: "Uploaded a webshell and obtained a www-data shell"
  - id: f008
    description: "Found flag{abc123} in /var/www/html/flag.txt"

intents:
  - from: [origin]
    to: f001
    description: "Full-port nmap scan"
    creator: "agent-A"
    worker: "agent-A"
    created_at: "2026-03-21 18:01:00"
    concluded_at: "2026-03-21 18:02:00"

  - from: [f001]
    to: f002
    description: "HTTP service probing and directory scanning"
    creator: "agent-A"
    worker: "agent-B"
    created_at: "2026-03-21 18:02:00"
    concluded_at: "2026-03-21 18:04:00"

  - from: [f002]
    to: f003
    description: "Download /backup/db.sql using directory traversal"
    creator: "agent-B"
    worker: "agent-B"
    created_at: "2026-03-21 18:04:00"
    concluded_at: "2026-03-21 18:05:00"

  - from: [f003]
    to: f004
    description: "Parse db.sql to extract user credentials"
    creator: "agent-A"
    worker: "agent-A"
    created_at: "2026-03-21 18:05:00"
    concluded_at: "2026-03-21 18:05:30"

  - from: [f004]
    to: f005
    description: "Attempt SSH login"
    creator: "agent-A"
    worker: "agent-C"
    created_at: "2026-03-21 18:05:30"
    concluded_at: "2026-03-21 18:06:00"

  - from: [f002, f004]
    to: f006
    description: "Use credentials from f004 to log into the admin interface discovered in f002"
    creator: "agent-A"
    worker: "agent-B"
    created_at: "2026-03-21 18:05:30"
    concluded_at: "2026-03-21 18:06:00"

  - from: [f006]
    to: f007
    description: "Upload a PHP webshell by bypassing extension checks"
    creator: "agent-B"
    worker: "agent-B"
    created_at: "2026-03-21 18:06:00"
    concluded_at: "2026-03-21 18:07:00"

  - from: [f007]
    to: f008
    description: "Search for the flag file from the shell"
    creator: "agent-B"
    worker: "agent-A"
    created_at: "2026-03-21 18:07:00"
    concluded_at: "2026-03-21 18:08:00"

  - from: [f008]
    to: goal
    description: "flag{abc123} satisfies the goal"
    creator: "agent-A"
    worker: "agent-A"
    created_at: "2026-03-21 18:08:00"
    concluded_at: "2026-03-21 18:08:01"
```

Example of an unconcluded Intent (a non-null worker means a consumer is processing it):

```yaml
  - from: [f003]
    to: null
    description: "Try reading sensitive files through directory traversal"
    creator: "agent-A"
    worker: "agent-C"
    created_at: "2026-03-21 18:09:00"
    concluded_at: null
```

---

#### GET /projects/{project_id}/export?format=timeline

Return the project timeline as plain text, sorted by event time. YAML shows the graph's topology; the timeline complements it with chronological events and causal chains. Event types include `PROJECT CREATED`, `HINT`, `INTENT DECLARED`, `INTENT CONCLUDED`, and `PROJECT COMPLETED`. If a completed project is reopened, its old goal edge is removed, so the previous `PROJECT COMPLETED` event is absent from the timeline.

---

### Evidence and reports

#### Evidence store

Each project has a durable directory. The root is set by the Dispatcher's `evidence_root`, defaults to `~/.local/share/board/projects`, and can be overridden by `BOARD_PROJECTS_DIR`:

```
<projects_root>/<project_id>/
  evidence/<intent_id>/   # All evidence artifacts produced by workers
  workspace/              # Worker working directory (cwd)
  reports/                # Automatically generated timeline and reports
  scratch/                # Regenerable internal files, such as graph snapshots
```

Worker processes receive these paths through `BOARD_PROJECT_DIR` and `BOARD_EVIDENCE_DIR`. Prompts explicitly require all artifacts (scripts, captures, screenshots, notes) to be written under `$BOARD_EVIDENCE_DIR`, never under a self-selected `/tmp` path. At intent completion, the Dispatcher walks the intent's evidence directory, computes SHA-256 hashes, and registers each file.

#### GET /projects/{project_id}/evidence

List registered project evidence in registration order.

```json
[
  {
    "id": "ev001",
    "intent_id": "i002",
    "fact_id": "f002",
    "path": "proj_001/evidence/i002/capture.txt",
    "kind": "notes",
    "sha256": "9f86d0818…",
    "size_bytes": 18,
    "created_at": "2026-08-25T12:00:00Z",
    "description": null
  }
]
```

`path` is relative to the projects root. `kind` is one of `screenshot | capture | script | data | notes | lost`, inferred from the extension if omitted. Use `lost` to register destroyed historical evidence, such as temporary files cleared by a reboot; its sha256 is empty.

#### POST /projects/{project_id}/evidence

Register evidence. The body must contain at least `path`. If sha256 is omitted and the file can be resolved under the projects root, the server calculates its hash and size. If neither is available, return 400, except for `kind=lost`. Repeated registration with the same `(path, sha256)` idempotently returns the existing row.

```json
{
  "path": "proj_001/evidence/i002/capture.txt",
  "kind": "capture",
  "intent_id": "i002",
  "fact_id": "f002",
  "sha256": "9f86d0818…",
  "size_bytes": 18,
  "description": "Login flow traffic capture"
}
```

#### GET /projects/{project_id}/findings

List structured findings sorted by severity (critical → high → medium → low → info → negative), with optional `?severity=` and `?status=` filters. `negative` means a weakness was verified absent, such as a patched CVE or hardened configuration, and is a first-class result.

#### POST /projects/{project_id}/findings

Create a finding. `fact_id` must exist. `severity` is one of `critical|high|medium|low|info|negative` (default `info`); `status` is one of `open|confirmed|mitigated|false_positive|needs_retest|blocked` (default `open`). `evidence_ids` is an array of evidence IDs forming the evidence chain.

The human report excludes `false_positive` and `mitigated` records from current severity totals and displays them in finding disposition history. Unresolved `needs_retest` and `blocked` records remain current. The findings API and agent report retain all records. Worker findings preserve an explicit supported status; a new finding does not retract or replace an earlier record.

#### PUT /projects/{project_id}/findings/{finding_id}

Partially update a finding (`status`, `severity`, `recommendation`, `evidence_ids`, and other fields), automatically refreshing `updated_at`.

#### GET /projects/{project_id}/report?type=human|agent|agent-json

Render reports deterministically from the blackboard database and evidence directory:

- `human`: a deliverable Markdown report with an executive summary, findings by severity, methodology timeline, negative results and coverage, open questions/blocked paths, and an evidence manifest appendix.
- `agent`: a machine-first Markdown report for AI agents, embedding complete JSON (all finding fields, evidence manifest with absolute paths and SHA-256 hashes, intent→fact provenance, negative results, unexecuted intents, and timeline text).
- `agent-json`: the exact JSON embedded in the agent report, returned as plain JSON.

The report header includes project metadata, time window, generation time, board version, `evidence_root`, and the database file's SHA-256 at generation time as a tamper-evidence record.

#### GET /projects/{project_id}/asset-map?format=html|json|md

Attack-surface asset map: extract a typed asset graph from fact and finding prose (domains, IPs, endpoints, services as host:port, technologies and versions, cloud/SaaS, identity providers, CVEs, and test probes), then infer relationships (`resolves_to`, `served_by`, `hosted_on`, `runs`, `exposes`, `belongs_to`, `affects`, `associated`). Hostname patterns identify cloud providers, such as `*.execute-api.*.amazonaws.com` → AWS API Gateway, `*.azure-api.net` → Azure APIM, and `*.force.com` → Salesforce. Finding `affected_target` values anchor severity to assets; parent domains aggregate subdomain findings, while negative findings count as verified absence of weakness. Every node and edge carries the supporting fact/finding IDs. The map is an auditable rendering of the blackboard, not a parallel data path.

- `html`: a self-contained interactive map with inline Cytoscape, search, type/severity filters, layout switching, and a provenance panel.
- `json`: a machine-readable graph (nodes, edges, facts, and evidence).
- `md`: an asset inventory table grouped by category.

Available for projects in any state, including running ones. On completion or abort, automatically write `asset-map.html`, `asset-map.json`, and `asset-inventory.md` alongside the report set. CLI: `board assets <project_id>`.

#### Automatic file output

After every intent concludes, the Dispatcher refreshes `<project>/reports/timeline.txt` and `graph.yaml`. On project completion, it automatically writes the complete report set (`report-agent.md`, `report-agent.json`, `report-human.md`, `timeline.txt`, `graph.yaml`). The CLI exposes the same renderers on demand through `board export`, `board report`, and `board findings list|extract`.

---

## Typical consumer workflow

```
1. Read the structured project data
   for scheduling, state assessment, intent selection, and protocol writes

2. GET /projects/{id}/export?format=yaml
   Read the graph snapshot (only for the Dispatcher to render into agent prompts)

3. If the project is still in its initial state:
   Typically, facts contain only origin and goal, with no ordinary intents;
   consumers may accept either no intents or only the reserved bootstrap intent as initial
   If project.bootstrap_enabled=false or the consumer cannot bootstrap, proceed to reason;
   a. If the reserved bootstrap intent does not exist:
      POST /projects/{id}/intents
        { from: ["origin"], description: "bootstrap", creator: "dispatcher.bootstrap", worker: null }
   b. POST /projects/{id}/intents/{intent_id}/heartbeat
        { worker: "dispatcher-worker-A" }
      After claiming successfully, perform one bootstrap attempt directly
   c. Continue heartbeats while working
   d. If a key supporting fact is obtained:
      POST /projects/{id}/intents/{intent_id}/conclude
        { worker: "dispatcher-worker-A", description: "The login page accepts default credentials admin:admin" }
      If this new fact already satisfies the goal, also:
      POST /projects/{id}/complete
        { from: ["new fact id"], description: "Why this fact satisfies the goal", worker: "dispatcher-worker-A" }
   e. If bootstrap fails and must be abandoned:
      POST /projects/{id}/intents/{intent_id}/release
        { worker: "dispatcher-worker-A" }
   f. Return to step 1

4. If an ordinary unclaimed Intent exists (worker=null):
   a. Select a claimable Intent
   b. POST /projects/{id}/intents/{intent_id}/heartbeat
        { worker: "dispatcher-worker-A" }
      After claiming successfully, dispatch an explore task
   c. Continue heartbeats during exploration
   d. If exploration succeeds:
      POST /projects/{id}/intents/{intent_id}/conclude
        { worker: "dispatcher-worker-A", description: "Found error-based injection in a /search parameter" }
   e. If exploration fails and must be abandoned:
      POST /projects/{id}/intents/{intent_id}/release
        { worker: "dispatcher-worker-A" }
   f. Return to step 1

5. If there are no unclaimed Intents:
   a. POST /projects/{id}/reason/claim
        { worker: "dispatcher-worker-A", trigger: "new_facts" }
      After claiming successfully, dispatch a reason task
   b. Continue POST /projects/{id}/reason/heartbeat while working
        { worker: "dispatcher-worker-A" }
   c. If reason returns a completion conclusion:
      POST /projects/{id}/complete
        { from: ["f008"], description: "flag{abc} satisfies the goal", worker: "dispatcher-worker-A" }
      End the workflow
   d. If reason returns new intents:
      POST /projects/{id}/intents
        { from: ["f003"], description: "Try SQL injection", creator: "dispatcher-worker-A", worker: null }
      Then POST /projects/{id}/reason/release
        { worker: "dispatcher-worker-A" }
   e. If reason returns neither completion nor new intents:
      POST /projects/{id}/reason/release
        { worker: "dispatcher-worker-A" }
      Do not write to the graph this round
   f. If reason fails, times out, or must be abandoned:
      POST /projects/{id}/reason/release
        { worker: "dispatcher-worker-A" }
   g. Return to step 1

6. If the project is completed, but external verification disproves completion:
   a. POST /projects/{id}/reopen
        { description: "FLAG{fake} is incorrect; continue searching for the correct flag", creator: "judge" }
   b. The project returns to active, with a new external_feedback conclusion edge and corrective Fact
   c. Return to step 1
```

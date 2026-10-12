# Intent worker routing and lifecycle controls

When creating an intent, select **Automatic**, **Pi**, **Codex**, or **Claude Code**.
The selected type is persisted as `assigned_worker_type` (`pi`, `codex`, or
`claudecode`). Automatic uses the normal worker selection policy. A selected type
restricts dispatch to matching registered execution workers. An optional named
worker pin also applies; both constraints must match.

The type can also be edited in the intent detail panel. Busy matching workers
cause the intent to wait. Missing registrations or incompatible routing constraints
produce a persistent error. Board does not fall back to another worker type.

## Clean installations

The Stage 5 installer clones this repository and supplies the local dispatcher
configuration. Its default configuration registers `local-pi`; publishing these
controls does not automatically register Codex or Claude Code. The local dispatch
example includes all three adapter types.

Register additional workers in `dispatch.yaml` only when their CLIs are configured
for the dispatcher service account. CLI installation and worker registration are
separate from successful model access. Local startup checks verify CLI availability;
an invocation can still fail at runtime. Runtime configuration, credentials, project
databases, transcripts, and backups are not versioned in this repository.

## Worker errors

An unsuccessful attempt releases its slot and stops automatic retries of that
intent. The graph shows **Error**, and the detail panel displays the failure and
timestamp. Errors persist in the database and a project-local
`reports/intent-errors.json` latch, so a dispatcher restart does not restart the same
failed attempt. Changing a worker assignment does not clear an existing error.

Failed or cancelled work is explicitly excluded from automatic retry or rerouting
by the planner. Worker processes that exit unsuccessfully have their remaining
local process-group children terminated.

## Cancel and delete

Select an unfinished intent in the graph, then choose **Cancel intent** or
**Delete intent**. Both actions require confirmation.

Cancellation prevents a new claim and rejects late worker results. A running
attempt shows **Stopping** until its process ends; then it shows **Cancelled**.
The attempt occupies its slot until it actually stops. Queued intents can be
cancelled immediately.

Deletion confirms **Cancel and delete**, waits for cancellation acknowledgement,
and removes the unfinished intent and its graph links. Existing facts and evidence
files are preserved. Concluded intents are retained as project history, and an
associated background job must be stopped before deletion. Intent IDs are not
reused after deletion.

For manually claimed work, stop any external commands and use **Release** before
cancelling or deleting. Board can terminate the worker processes it manages;
external manual commands require their owner's cleanup.

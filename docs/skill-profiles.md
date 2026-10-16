# Native Pi skill profiles

Board local workers use the daemon account's Pi configuration. Pi 1.3 is the pinned
integration target on Debian-derived Linux hosts. Library installation and skill selection do not install scanners,
SDKs, rules, databases, or grant execution permissions.

## Configure installed libraries

The optional stages installers register these catalogs. Configure only paths that exist:

```yaml
skills:
  enabled: true
  catalogs:
    - /opt/research-library/catalog.json
    - /opt/sast-tools/catalog.json
  default_profiles: [general]
  max_metadata_tokens: 6000
  profiles:
    python-review:
      description: Review supplied Python source and its static reports.
      extends: [source-review, python]
local:
  pi_session_storage: project
```

This is a configuration fragment; retain the server, runtime, tasks and workers in
`dispatch.local.example.yaml`. Relative catalog paths resolve from dispatch.yaml.
Catalog symlinks resolve before relative package paths, so a stable SAST catalog link
can refer to the selected checkout. Each catalog uses schema_version 1, library_id,
and skills records with id, path, tags and requires. Paths are exactly skills/<id>;
names and descriptions come from SKILL.md. Package resources must stay inside the
package; symlinked resources are rejected. Standalone skill installation still works.

Run as the same account and HOME used by board-dispatcher.service:

```bash
board skills doctor --config /opt/board/dispatch.yaml --json
board skills profiles --config /opt/board/dispatch.yaml --json
board skills search --config /opt/board/dispatch.yaml 'Python source' --json
```

These commands make no model calls. Doctor validates catalogs, readable complete
packages, collisions, profile inheritance and default metadata budget. Executable
requirements are separately reported when declared; an empty requires list means
unspecified readiness. It does not establish scanner behavior, SDK availability,
network isolation or model effectiveness.

## Select per intent

Intent creation accepts `skill_profiles: [source-review, python]`. The dashboard can
create or edit the IDs for an unclaimed intent. Profile edits do not reassign workers;
explicit worker routing and profiles remain separate. Profile-bearing intents require
a compatible Pi worker. Unknown, disabled or incompatible selections fail visibly.
Existing clients and migrated intent rows default to an empty list.

The planner receives short profile summaries and can request applicable IDs. The
execution dispatcher resolves each selection before starting Pi. Empty intent IDs
use worker.skill_profiles when configured, otherwise skills.default_profiles. Profiles
select explicit library/name IDs through skills, OR-match catalog tags through tags,
and compose parents through extends. Composition is additive, not a language filter
on another profile. For an exact subset, define explicit skills or narrower task tags.

Built-ins: general, source-review, config-review, offline-artifacts, data-processing,
python, go, ruby, javascript, dotnet, c-cpp, java and php. Custom definitions override
these IDs. Only curated classifications enter tag-based profiles. All catalog entries,
including classification:unreviewed, remain searchable and manually selectable by
an operator's explicit profile skills list. Unreviewed metadata is not reviewed task
coverage. The initial catalogs deliberately classify a bounded subset of the large
research library; see that library's catalog documentation for current coverage.

A selection exceeding max_metadata_tokens is rejected with a request to split the
intent or adjust the explicit budget. No required package is silently truncated. The
estimate mirrors Pi's advertised name, description and path entries at characters/4;
it is not measured model tokens and there is no universal 150-skill limit.

## Persistent attempts and diverse tasks

Each execution attempt copies the selected complete packages into project scratch,
checks package hashes before/after copying, and uses those exact paths for execution
and conclusion. A skill-selection JSON evidence file records profiles, paths, package
and catalog digests, estimated metadata cost and missing declared executables.
Parallel workers never change shared skill symlinks or singleton driver configuration.
Library updates cannot alter an already copied selection. Scratch snapshots must be
retained while sessions are active; evidence manifests refer to them.

The local launcher keeps --no-skills and passes repeated --skill paths explicitly.
It supplies Board's neutral contract through --append-system-prompt while disabling
automatic project context and resource trust. Worker pi_extensions and pi_tools are
explicit optional operator settings; planners retain their tool-free integration.
A filtered skill catalog is a context optimization, not a filesystem/permission boundary.

Native project storage places Pi session JSONL under <project>/sessions/pi with umask
077. Existing transcripts remain untouched. local.pi_session_storage: ephemeral is an
explicit legacy option and retains a configured PI_AGENT_DIR override. The same frozen
worker selection is passed through conclusion
fallback; a new intent gets a new session and can request different profiles. Removing
an instruction from a resumed invocation cannot erase conversation history.

Execution prompts read a dependency-focused graph view first; full graph snapshots
remain available. Board's shared contract is technology-neutral and carries evidence,
scope and handoff requirements. Static review and permitted isolated regression checks
can share a project through separate intents and evidence-backed candidate records.
Keep confirmed, rejected and unresolved candidates distinct. Skills do not authorize
external actions or enforce a read-only source/egress policy; provision the required
filesystem, execution-account and network limits for the actual task.

## Provisioning and rollback

Use current Board with the stages native configuration helpers. Old hash-patched
application code is not rewritten by the new helpers: preserve its work, install a clean
supported Board revision, then migrate the config. Keep dispatch.yaml, existing project
sessions and data. Stop active work before changing the provisioned runtime/libraries.
The server migrates existing databases idempotently; skill_profiles defaults to [].

For rollback, finish active work and stop the dispatcher, revert the relevant repository
commit and restore its pre-migration dispatch.yaml with the original owner and mode.
Stages keeps protected configuration backups under /var/lib/stages/board-config-backups.
An older Board build may reject the new configuration fields. Preserve project data,
evidence and session directories; a code rollback does not require deleting the database.

The integration tests use harmless owned packages. Linux CI runs full pytest and an
actual Pi 1.3 CLI against a localhost dummy model, never a real provider or scanner.
This validates loading/session mechanics, not skill usefulness or vulnerability discovery.
Matched no-skills/full-catalog/selected-profile worker evaluations remain a later on-host
step using a controlled task and model budget.

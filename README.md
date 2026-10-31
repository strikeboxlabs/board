"# board" 

The dispatcher supports live file settings and web UI overrides for routing,
concurrency, and execution timeouts. See [live settings](docs/specs/dispatcher-design.md#live-settings).


## Native skill profiles

Local Pi workers support trusted catalogs, per-intent skill profiles, frozen package selections, and persistent project sessions. See [configuration and validation](docs/skill-profiles.md). The profile selector keeps task and target technology separate and preserves independently installable skill packages.

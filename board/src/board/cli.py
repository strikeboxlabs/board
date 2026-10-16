from pathlib import Path

import click
import uvicorn

from board.dispatcher.logging import configure_logging
from board.dispatcher.scheduler.loop import DispatcherLoop
from board.server import db


@click.group()
def main():
    """board - Strikebox's fact-graph based collaborative exploration service."""


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host")
@click.option("--port", default="8000", show_default=True, help="Bind port")
@click.option(
    "--db-path",
    type=click.Path(),
    default=str(db.DEFAULT_DB),
    show_default=True,
    help="SQLite database path",
)
@click.option("--log-level", default="info", show_default=True, help="Uvicorn log level")
@click.option("--access-log/--no-access-log", default=True, show_default=True, help="Enable Uvicorn access log")
def serve(host: str, port: int, db_path: str, log_level: str, access_log: bool):
    """Start the board API server for Strikebox."""
    db.configure(Path(db_path))
    db.backup_existing_db()
    from board.server.app import app

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=access_log,
    )


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path",
)
@click.option("--once", is_flag=True, help="Run one scheduling iteration and exit")
@click.option(
    "--startup-healthcheck-only",
    is_flag=True,
    help="Run startup worker healthchecks and exit",
)
@click.option("--log-level", default="INFO", show_default=True, help="Log level")
def dispatch(config_path: Path, once: bool, startup_healthcheck_only: bool, log_level: str):
    """Run the board dispatcher for Strikebox."""
    configure_logging(log_level, bare=startup_healthcheck_only)
    loop = DispatcherLoop(config_path)
    try:
        if startup_healthcheck_only:
            loop.run_startup_healthchecks_only()
            return
        loop.run(once=once)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


# --------------------------------------------------------------------------- reporting


def _configure_db(db_path: str | None) -> None:
    db.configure(Path(db_path) if db_path else db.DEFAULT_DB)


def _emit(text: str, out: Path | None) -> None:
    if out is not None and str(out) != "-":
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        click.echo(str(out))
    else:
        click.echo(text, nl=False)


@main.command()
@click.argument("project_id")
@click.option(
    "--format",
    "export_format",
    type=click.Choice(["timeline", "yaml", "report", "json"]),
    default="timeline",
    show_default=True,
    help="timeline: methodology timeline · yaml: graph snapshot · report: human report (md) · json: agent report (json)",
)
@click.option("--out", type=click.Path(path_type=Path), default=None, help="Output file ('-' for stdout). Default: the project's reports/ directory.")
@click.option("--db-path", type=click.Path(), default=None, help="SQLite database path")
def export(project_id: str, export_format: str, out: Path | None, db_path: str | None):
    """Export a project's blackboard using the shared renderers (same output as the API)."""
    from board.reporting.agent_report import build_agent_report, render_agent_report_json
    from board.reporting.human_report import render_human_report
    from board.reporting.paths import default_projects_root, reports_dir
    from board.reporting.timeline import render_timeline
    from board.reporting.yaml_export import render_graph_yaml

    _configure_db(db_path)
    try:
        with db.get_conn() as conn:
            if export_format == "timeline":
                text, default_name = render_timeline(conn, project_id), "timeline.txt"
            elif export_format == "yaml":
                text, default_name = render_graph_yaml(conn, project_id), "graph.yaml"
            elif export_format == "report":
                text, default_name = (
                    render_human_report(conn, project_id, evidence_root=default_projects_root(), db_sha256=db.hash_db_file()),
                    "report-human.md",
                )
            else:
                report = build_agent_report(
                    conn, project_id, evidence_root=default_projects_root(), db_sha256=db.hash_db_file()
                )
                text, default_name = render_agent_report_json(report), "report-agent.json"
    except LookupError as exc:
        raise click.ClickException(str(exc)) from exc

    if out is None:
        out = reports_dir(default_projects_root(), project_id) / default_name
    _emit(text, out)


def _render_full_report_set(conn, project_id: str, evidence_root) -> list[tuple[str, str]]:
    """The full report set: reports, timeline, graph, and the attack-surface map."""
    from board.reporting.agent_report import build_agent_report, render_agent_report_json, render_agent_report_markdown
    from board.reporting.asset_map_html import render_asset_map_html
    from board.reporting.assets import build_asset_map, render_asset_inventory_md, render_asset_map_json
    from board.reporting.human_report import render_human_report
    from board.reporting.timeline import render_timeline
    from board.reporting.yaml_export import render_graph_yaml

    db_sha256 = db.hash_db_file()
    built = build_agent_report(conn, project_id, evidence_root=evidence_root, db_sha256=db_sha256)
    asset_map = build_asset_map(conn, project_id)
    return [
        (render_agent_report_markdown(built), "report-agent.md"),
        (render_agent_report_json(built), "report-agent.json"),
        (render_human_report(conn, project_id, evidence_root=evidence_root, db_sha256=db_sha256), "report-human.md"),
        (render_timeline(conn, project_id), "timeline.txt"),
        (render_graph_yaml(conn, project_id), "graph.yaml"),
        (render_asset_map_json(asset_map), "asset-map.json"),
        (render_asset_inventory_md(asset_map), "asset-inventory.md"),
        (render_asset_map_html(asset_map), "asset-map.html"),
    ]


@main.command()
@click.argument("project_id")
@click.option("--type", "report_type", type=click.Choice(["agent", "human", "both"]), default="both", show_default=True)
@click.option("--format", "report_format", type=click.Choice(["md", "json"]), default="md", show_default=True, help="Used with --type agent")
@click.option("--out", type=click.Path(path_type=Path), default=None, help="Output file ('-' for stdout). Default: the project's reports/ directory.")
@click.option("--db-path", type=click.Path(), default=None, help="SQLite database path")
def report(project_id: str, report_type: str, report_format: str, out: Path | None, db_path: str | None):
    """Render agent and/or human reports for a project from the blackboard DB."""
    from board.reporting.agent_report import build_agent_report, render_agent_report_json, render_agent_report_markdown
    from board.reporting.human_report import render_human_report
    from board.reporting.paths import default_projects_root, reports_dir

    _configure_db(db_path)
    evidence_root = default_projects_root()
    single_out = out

    try:
        with db.get_conn() as conn:
            outputs: list[tuple[str, str]] = []
            if report_type == "agent":
                built = build_agent_report(conn, project_id, evidence_root=evidence_root, db_sha256=db.hash_db_file())
                if report_format == "json":
                    outputs.append((render_agent_report_json(built), "report-agent.json"))
                else:
                    outputs.append((render_agent_report_markdown(built), "report-agent.md"))
            elif report_type == "human":
                outputs.append(
                    (
                        render_human_report(conn, project_id, evidence_root=evidence_root, db_sha256=db.hash_db_file()),
                        "report-human.md",
                    )
                )
            else:
                outputs.extend(_render_full_report_set(conn, project_id, evidence_root))
    except LookupError as exc:
        raise click.ClickException(str(exc)) from exc

    for i, (text, default_name) in enumerate(outputs):
        if single_out is not None and str(single_out) == "-":
            if i > 0:
                click.echo("")
            _emit(text, None)
        elif single_out is not None and len(outputs) == 1:
            _emit(text, single_out)
        else:
            _emit(text, reports_dir(evidence_root, project_id) / default_name)


@main.command()
@click.argument("project_id")
@click.option("--reason", required=True, help="Why the project is being abandoned (recorded in the project, timeline and reports).")
@click.option("--actor", default="cli", show_default=True, help="Who is aborting (recorded as aborted_by).")
@click.option("--db-path", type=click.Path(), default=None, help="SQLite database path")
def abort(project_id: str, reason: str, actor: str, db_path: str | None):
    """Abort a project (terminal) and write the full report set.

    Abandoning a run keeps its record: status becomes 'aborted' with the reason,
    running workers are released, and timeline + both report flavours are written
    to the project's reports/ directory.
    """
    from fastapi import HTTPException

    from board.reporting.paths import default_projects_root, reports_dir
    from board.server.services import abort_project

    _configure_db(db_path)
    with db.get_conn() as conn:
        try:
            updated = abort_project(conn, project_id, reason, actor)
        except HTTPException as exc:
            raise click.ClickException(str(exc.detail)) from exc
    click.echo(
        f"aborted {updated['id']} at {updated['aborted_at']} by {updated['aborted_by']} — {updated['abort_reason']}"
    )

    evidence_root = default_projects_root()
    try:
        with db.get_conn() as conn:
            outputs = _render_full_report_set(conn, project_id, evidence_root)
    except LookupError as exc:
        raise click.ClickException(str(exc)) from exc
    for text, default_name in outputs:
        _emit(text, reports_dir(evidence_root, project_id) / default_name)


@main.command()
@click.argument("project_id")
@click.option(
    "--format",
    "asset_format",
    type=click.Choice(["html", "json", "md", "all"]),
    default="all",
    show_default=True,
    help="html: interactive map · json: machine graph · md: inventory tables",
)
@click.option("--out", type=click.Path(path_type=Path), default=None, help="Output file ('-' for stdout). Default: the project's reports/ directory.")
@click.option("--db-path", type=click.Path(), default=None, help="SQLite database path")
def assets(project_id: str, asset_format: str, out: Path | None, db_path: str | None):
    """Render the attack-surface asset map (works on running projects too)."""
    from board.reporting.asset_map_html import render_asset_map_html
    from board.reporting.assets import build_asset_map, render_asset_inventory_md, render_asset_map_json
    from board.reporting.paths import default_projects_root, reports_dir

    _configure_db(db_path)
    try:
        with db.get_conn() as conn:
            asset_map = build_asset_map(conn, project_id)
    except LookupError as exc:
        raise click.ClickException(str(exc)) from exc

    renderers = {
        "json": (render_asset_map_json, "asset-map.json"),
        "md": (render_asset_inventory_md, "asset-inventory.md"),
        "html": (render_asset_map_html, "asset-map.html"),
    }
    selected = renderers if asset_format == "all" else {asset_format: renderers[asset_format]}
    summary = asset_map["summary"]
    click.echo(
        f"{summary['node_count']} assets ({', '.join(f'{v} {k}' for k, v in summary['counts'].items())}) · "
        f"{summary['edge_count']} relations",
        err=True,
    )
    for i, (render, default_name) in enumerate(selected.values()):
        text = render(asset_map)
        if out is not None and str(out) == "-":
            if i > 0:
                click.echo("")
            _emit(text, None)
        elif out is not None and len(selected) == 1:
            _emit(text, out)
        else:
            _emit(text, reports_dir(default_projects_root(), project_id) / default_name)


@main.group()
def findings():
    """List and extract structured findings for a project."""


@findings.command("list")
@click.argument("project_id")
@click.option("--severity", type=click.Choice(["critical", "high", "medium", "low", "info", "negative"]), default=None)
@click.option("--db-path", type=click.Path(), default=None, help="SQLite database path")
def findings_list(project_id: str, severity: str | None, db_path: str | None):
    """List findings sorted by severity."""
    from board.reporting.data import load_findings
    from board.server.models import FINDING_SEVERITY_ORDER

    _configure_db(db_path)
    with db.get_conn() as conn:
        rows = load_findings(conn, project_id)
    if not rows:
        click.echo("No findings recorded.")
        return
    rows = [row for row in rows if severity is None or row["severity"] == severity]
    rows.sort(
        key=lambda row: (
            FINDING_SEVERITY_ORDER.get(row["severity"] or "info", 99),
            row["created_at"],
            row["id"],
        )
    )
    click.echo(f"{'ID':<8} {'SEVERITY':<9} {'STATUS':<13} {'FACT':<6} TITLE")
    for row in rows:
        title = (row["title"] or "").replace("\n", " ")
        click.echo(f"{row['id']:<8} {(row['severity'] or '-'):<9} {(row['status'] or '-'):<13} {row['fact_id']:<6} {title}")


@findings.command("extract")
@click.argument("project_id")
@click.option("--apply", "apply_changes", is_flag=True, help="Write the extracted findings to the DB (default: dry-run print).")
@click.option("--db-path", type=click.Path(), default=None, help="SQLite database path")
def findings_extract(project_id: str, apply_changes: bool, db_path: str | None):
    """Extract structured findings from fact prose (rule-based, auditable).

    Dry-run by default; review the output, then pass --apply. Facts that already
    have findings are skipped, so re-running is safe. Referenced-but-missing
    artefact paths are registered as kind='lost' evidence rows on --apply.
    """
    from board.reporting.extraction import apply_extraction, extract_findings
    from board.server.services import utcnow

    _configure_db(db_path)
    with db.get_conn() as conn:
        result = extract_findings(conn, project_id)
        if not apply_changes:
            for finding in result.findings:
                click.echo(
                    f"[{finding.severity.upper():<8}] {finding.fact_id} {finding.title}"
                    + (f"  (target: {finding.affected_target})" if finding.affected_target else "")
                )
            for lost in result.lost_evidence:
                click.echo(f"[LOST     ] {lost['fact_id']} {lost['path']}")
            click.echo(
                f"\nscanned_facts={result.scanned_facts} findings={len(result.findings)} "
                f"lost_evidence={len(result.lost_evidence)} skipped_facts={len(result.skipped_facts)}"
            )
            click.echo("dry run — pass --apply to insert")
            return
        inserted, lost = apply_extraction(conn, project_id, result, utcnow=utcnow())
        click.echo(f"inserted findings={inserted} lost_evidence_rows={lost} skipped_facts={len(result.skipped_facts)}")


@main.command()
@click.argument("project_id")
@click.option("--root", "reports_root", type=click.Path(path_type=Path), default=None,
              help="Projects root (defaults to BOARD_PROJECTS_DIR).")
def efficiency(project_id: str, reports_root: Path | None):
    """Show task outcomes, token use, and elapsed time for a measured run."""
    import json
    from board.reporting.paths import default_projects_root, sanitize_project_id
    from board.reporting.efficiency import summarize
    path = (reports_root or default_projects_root()) / sanitize_project_id(project_id) / "reports" / "efficiency.jsonl"
    if not path.exists():
        raise click.ClickException(f"No efficiency measurements yet: {path}")
    click.echo(json.dumps(summarize(path), indent=2))



def _skill_config_failure(exc: Exception) -> str:
    """Never serialize configuration input values (provider credentials may be present)."""
    import json
    from pydantic import ValidationError
    if isinstance(exc, ValidationError):
        details = [{"location": list(error["loc"]), "message": error["msg"], "type": error["type"]}
                   for error in exc.errors(include_input=False, include_context=False)]
        return "Invalid dispatcher configuration: " + json.dumps(details)
    return "Cannot validate dispatcher configuration (" + type(exc).__name__ + ")"

@main.group("skills")
def skills_commands():
    """Inspect trusted skill catalogs and per-intent profiles without a model call."""


@skills_commands.command("doctor")
@click.option("--config", "config_path", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--json", "json_output", is_flag=True)
def skills_doctor(config_path: Path, json_output: bool):
    """Validate local catalog/profile/resource access as the current daemon user."""
    import json
    from board.dispatcher.config import DispatchConfig
    from board.dispatcher.skills import doctor
    try:
        config = DispatchConfig.load(config_path)
        result = doctor(config.skills)
        if config.skills.enabled and config.runtime.execution != "local":
            result["errors"].append("Catalog skill profiles currently require local execution")
    except Exception as exc:
        result = {"errors": [_skill_config_failure(exc)], "warnings": []}
    if json_output:
        click.echo(json.dumps(result, indent=2))
    else:
        click.echo("Skill configuration: " + ("FAILED" if result["errors"] else "OK"))
        for item in result.get("profiles", []):
            click.echo(f"{item['id']}: {item['skills']} skills, ~{item['estimated_metadata_tokens']} metadata tokens")
        for item in result.get("warnings", []) + result["errors"]:
            click.echo(item)
    if result["errors"]:
        raise click.exceptions.Exit(1)


@skills_commands.command("profiles")
@click.option("--config", "config_path", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--json", "json_output", is_flag=True)
def skills_profiles(config_path: Path, json_output: bool):
    """List available profile summaries and discovery cost estimates."""
    import json
    from board.dispatcher.config import DispatchConfig
    from board.dispatcher.skills import doctor, profile_summaries
    try:
        config = DispatchConfig.load(config_path)
        result = doctor(config.skills)
        if result["errors"]:
            raise ValueError("Invalid skill catalog or profile configuration; run skills doctor")
        text = json.dumps(result["profiles"], indent=2) if json_output else profile_summaries(config.skills)
    except Exception as exc:
        raise click.ClickException(_skill_config_failure(exc)) from None
    click.echo(text)


@skills_commands.command("search")
@click.argument("query")
@click.option("--config", "config_path", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--limit", default=20, type=click.IntRange(1, 100), show_default=True)
@click.option("--json", "json_output", is_flag=True)
def skills_search(query: str, config_path: Path, limit: int, json_output: bool):
    """Search all catalog descriptions, including explicitly unreviewed classifications."""
    import json
    import re
    from board.dispatcher.config import DispatchConfig
    from board.dispatcher.skills import load_catalogs
    try:
        config = DispatchConfig.load(config_path)
        catalog = load_catalogs(config.skills)
        terms = set(re.findall(r"[a-z0-9]+", query.lower()))
        matches = []
        for identity, skill in catalog.items():
            text = (identity + " " + skill.description + " " + " ".join(skill.tags)).lower()
            score = sum(term in text for term in terms) + (4 if query.lower() == skill.name else 0)
            if score:
                matches.append((score, identity, {"id": identity, "description": skill.description,
                    "tags": list(skill.tags), "requires": list(skill.requires)}))
        result = [item[2] for item in sorted(matches, key=lambda item: (-item[0], item[1]))[:limit]]
    except Exception as exc:
        raise click.ClickException(_skill_config_failure(exc)) from None
    if json_output:
        click.echo(json.dumps(result, indent=2))
    else:
        for item in result:
            click.echo(item["id"] + ": " + item["description"])

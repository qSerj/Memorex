from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer

from memorex.config import ConfigurationError, LLMConfig, WorkspaceConfig
from memorex.extraction import ExtractionError, extract_source
from memorex.ingest import SourceValidationError, parse_source
from memorex.llm import OpenAICompatibleProvider
from memorex.query import QueryError, answer_question
from memorex.storage import RecordNotFound, Storage, WorkspaceNotInitialized

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
source_app = typer.Typer(no_args_is_help=True)
claim_app = typer.Typer(no_args_is_help=True)
app.add_typer(source_app, name="source")
app.add_typer(claim_app, name="claim")


@dataclass
class CLIState:
    workspace: WorkspaceConfig


@app.callback()
def main(
    ctx: typer.Context,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            envvar="MEMOREX_DATA_DIR",
            help="Workspace directory (default: .memorex).",
        ),
    ] = None,
) -> None:
    """Compile local source files into reusable, provenance-backed knowledge."""
    ctx.obj = CLIState(WorkspaceConfig.resolve(data_dir))


@app.command("init")
def init_command(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Initialize a Memorex workspace and its SQLite schema."""
    _run(lambda: Storage(_state(ctx).workspace).initialize(), json_output, _format_init)


@app.command()
def add(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="UTF-8 .txt or Markdown file.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Register, snapshot, normalize, and segment one source file."""

    def action() -> dict[str, Any]:
        parsed = parse_source(path)
        return Storage(_state(ctx).workspace).ingest_source(parsed)

    _run(action, json_output, _format_add)


@source_app.command("list")
def source_list(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List registered sources and their current revisions."""
    _run(lambda: Storage(_state(ctx).workspace).list_sources(), json_output, _format_sources)


@source_app.command("show")
def source_show(
    ctx: typer.Context,
    source_id: Annotated[int, typer.Argument(min=1)],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show a source and all retained revisions."""
    _run(
        lambda: Storage(_state(ctx).workspace).get_source(source_id),
        json_output,
        _format_source,
    )


@app.command()
def extract(
    ctx: typer.Context,
    source_id: Annotated[int, typer.Argument(min=1)],
    base_url: Annotated[str | None, typer.Option("--base-url")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    force: Annotated[bool, typer.Option("--force", help="Run a fresh extraction job.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Extract validated, evidence-backed claims from the current source revision."""

    def action() -> dict[str, Any]:
        config = LLMConfig.resolve(base_url, model)
        storage = Storage(_state(ctx).workspace)
        provider = OpenAICompatibleProvider(config)
        return extract_source(storage, source_id, provider, config, force=force)

    _run(action, json_output, _format_extract)


@claim_app.command("list")
def claim_list(
    ctx: typer.Context,
    source_id: Annotated[int | None, typer.Option("--source", min=1)] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List active claims, optionally filtered by source."""
    _run(
        lambda: Storage(_state(ctx).workspace).list_claims(source_id),
        json_output,
        _format_claims,
    )


@claim_app.command("show")
def claim_show(
    ctx: typer.Context,
    claim_id: Annotated[int, typer.Argument(min=1)],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show one claim and its exact evidence span."""
    _run(
        lambda: Storage(_state(ctx).workspace).get_claim(claim_id),
        json_output,
        _format_claim,
    )


@app.command()
def ask(
    ctx: typer.Context,
    question: Annotated[str, typer.Argument(help="Question to answer from active claims.")],
    limit: Annotated[int, typer.Option("--limit", min=1, max=50)] = 8,
    base_url: Annotated[str | None, typer.Option("--base-url")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Answer a question from FTS-ranked claims and their evidence."""

    def action() -> dict[str, Any]:
        config = LLMConfig.resolve(base_url, model)
        storage = Storage(_state(ctx).workspace)
        provider = OpenAICompatibleProvider(config)
        return answer_question(storage, question, provider, config, limit=limit)

    _run(action, json_output, _format_answer)


def _state(ctx: typer.Context) -> CLIState:
    state = ctx.find_root().obj
    if not isinstance(state, CLIState):
        raise RuntimeError("CLI state was not initialized")
    return state


def _run(action: Any, json_output: bool, formatter: Any) -> None:
    try:
        result = action()
    except (
        ConfigurationError,
        ExtractionError,
        QueryError,
        RecordNotFound,
        SourceValidationError,
        WorkspaceNotInitialized,
    ) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        typer.echo(formatter(result))


def _format_init(result: dict[str, Any]) -> str:
    return f"Initialized {result['data_dir']} (SQLite FTS5: available)"


def _format_add(result: dict[str, Any]) -> str:
    return (
        f"Source {result['source_id']} {result['status']}: revision {result['revision']}, "
        f"{result['segments']} segments, sha256 {result['sha256']}"
    )


def _format_sources(sources: list[dict[str, Any]]) -> str:
    if not sources:
        return "No sources."
    return "\n".join(
        f"{source['id']}\tr{source['revision']}\t{source['segment_count']} segments\t"
        f"{source['canonical_path']}"
        for source in sources
    )


def _format_source(source: dict[str, Any]) -> str:
    lines = [f"Source {source['id']}: {source['canonical_path']}"]
    lines.extend(
        f"  revision {version['revision']} (id {version['id']}): {version['sha256']}, "
        f"{version['segment_count']} segments, {version['status']}"
        for version in source["versions"]
    )
    return "\n".join(lines)


def _format_extract(result: dict[str, Any]) -> str:
    return (
        f"Extraction {result['status']} for source {result['source_id']}: "
        f"job {result['job_id']}, {result['claims']} claims"
    )


def _format_claims(claims: list[dict[str, Any]]) -> str:
    if not claims:
        return "No active claims."
    return "\n".join(
        f"{claim['id']}\t{claim['confidence']:.2f}\t{claim['statement']}" for claim in claims
    )


def _format_claim(claim: dict[str, Any]) -> str:
    return (
        f"Claim {claim['id']}: {claim['statement']}\n"
        f"Confidence: {claim['confidence']:.2f}\n"
        f"Evidence: {claim['quote']}\n"
        f"Source: {claim['canonical_path']} (revision {claim['revision']}, "
        f"chars {claim['char_start']}:{claim['char_end']})"
    )


def _format_answer(result: dict[str, Any]) -> str:
    lines = [result["answer"]]
    if result["citations"]:
        lines.append("\nEvidence:")
        lines.extend(
            f"[C{citation['id']}] {citation['canonical_path']} "
            f"(revision {citation['revision']}, "
            f"chars {citation['char_start']}:{citation['char_end']}): {citation['quote']}"
            for citation in result["citations"]
        )
    return "\n".join(lines)

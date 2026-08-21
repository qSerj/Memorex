from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer

from memorex.compiler import compile_source
from memorex.config import ConfigurationError, LLMConfig, WorkspaceConfig, WorkspaceSettings
from memorex.evaluation import evaluate_models
from memorex.extraction import ExtractionError, extract_source
from memorex.inbox import compile_inbox_entry, scan_inbox
from memorex.ingest import SourceValidationError, parse_source
from memorex.llm import OpenAICompatibleProvider
from memorex.query import QueryError, answer_question
from memorex.storage import RecordNotFound, Storage, WorkspaceNotInitialized

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
source_app = typer.Typer(no_args_is_help=True)
claim_app = typer.Typer(no_args_is_help=True)
workspace_app = typer.Typer(no_args_is_help=True)
inbox_app = typer.Typer(no_args_is_help=True)
evaluation_app = typer.Typer(no_args_is_help=True)
app.add_typer(source_app, name="source")
app.add_typer(claim_app, name="claim")
app.add_typer(workspace_app, name="workspace")
app.add_typer(inbox_app, name="inbox")
app.add_typer(evaluation_app, name="eval")


@dataclass
class CLIState:
    workspace: WorkspaceConfig
    settings: WorkspaceSettings | None = None


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
    workspace_root: Annotated[
        Path | None,
        typer.Option("--workspace", help="Memorex workspace root containing memorex.toml."),
    ] = None,
) -> None:
    """Compile local source files into reusable, provenance-backed knowledge."""
    if workspace_root is not None:
        settings = WorkspaceSettings.load(workspace_root)
        ctx.obj = CLIState(settings.data, settings)
    else:
        ctx.obj = CLIState(WorkspaceConfig.resolve(data_dir))


@workspace_app.command("init")
def workspace_init(
    path: Annotated[Path, typer.Argument(help="Directory for the isolated workspace.")],
    name: Annotated[str | None, typer.Option("--name")] = None,
    language: Annotated[str, typer.Option("--language")] = "ru",
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Create an isolated Memorex workspace with inbox and SQLite state."""

    def action() -> dict[str, Any]:
        settings = WorkspaceSettings.create(path, name or path.name, language=language)
        initialized = Storage(settings.data).initialize()
        return {
            **initialized,
            "workspace_root": str(settings.root),
            "name": settings.name,
            "inbox": str(settings.inbox_dir),
        }

    _run(action, json_output, _format_workspace_init)


@workspace_app.command("models")
def workspace_models(
    ctx: typer.Context,
    fast: Annotated[str, typer.Option("--fast")],
    strong: Annotated[str, typer.Option("--strong")],
    answer: Annotated[str, typer.Option("--answer")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Assign model IDs to the fast, strong, and answer roles."""

    def action() -> dict[str, Any]:
        state = _state(ctx)
        if state.settings is None:
            raise ConfigurationError("Model roles require --workspace PATH")
        updated = state.settings.set_models(fast=fast, strong=strong, answer=answer)
        return {
            "fast": updated.fast_model,
            "strong": updated.strong_model,
            "answer": updated.answer_model,
        }

    _run(
        action,
        json_output,
        lambda result: (
            f"Models: fast={result['fast']}, strong={result['strong']}, answer={result['answer']}"
        ),
    )


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
        state = _state(ctx)
        selected_model = model or (state.settings.answer_model if state.settings else None)
        config = LLMConfig.resolve(base_url, selected_model)
        storage = Storage(state.workspace)
        provider = OpenAICompatibleProvider(config)
        return extract_source(storage, source_id, provider, config, force=force)

    _run(action, json_output, _format_extract)


@app.command("compile")
def compile_command(
    ctx: typer.Context,
    source_id: Annotated[int, typer.Argument(min=1)],
    force: Annotated[bool, typer.Option("--force")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Compile a source into decision-aware claims, entities, relations, and summary."""

    def action() -> dict[str, Any]:
        state = _state(ctx)
        if state.settings is None:
            raise ConfigurationError("The compile command requires --workspace PATH")
        fast = LLMConfig.resolve_role("fast", state.settings)
        strong = LLMConfig.resolve_role("strong", state.settings)
        storage = Storage(state.workspace)
        return compile_source(
            storage,
            source_id,
            OpenAICompatibleProvider(fast),
            fast,
            OpenAICompatibleProvider(strong),
            strong,
            force=force,
        )

    _run(action, json_output, _format_extract)


@inbox_app.command("scan")
def inbox_scan(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Discover supported files in the workspace inbox without invoking an LLM."""

    def action() -> list[dict[str, Any]]:
        state = _state(ctx)
        if state.settings is None:
            raise ConfigurationError("Inbox commands require --workspace PATH")
        return scan_inbox(state.settings, Storage(state.workspace))

    _run(action, json_output, _format_inbox)


@inbox_app.command("metadata")
def inbox_metadata(
    ctx: typer.Context,
    entry_id: Annotated[int, typer.Argument(min=1)],
    title: Annotated[str, typer.Option("--title")],
    source_kind: Annotated[
        str, typer.Option("--kind", help="conversation, user_note, external_reference, or other")
    ] = "other",
    authority: Annotated[
        str, typer.Option("--authority", help="primary, user_analysis, external, or unknown")
    ] = "unknown",
    author: Annotated[str | None, typer.Option("--author")] = None,
    occurred_from: Annotated[str | None, typer.Option("--from")] = None,
    occurred_to: Annotated[str | None, typer.Option("--to")] = None,
    tag: Annotated[list[str] | None, typer.Option("--tag")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Attach trusted user metadata to a staged inbox file."""
    _run(
        lambda: Storage(_state(ctx).workspace).set_inbox_metadata(
            entry_id,
            title=title,
            source_kind=source_kind,
            author=author,
            authority=authority,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            tags=tag or [],
        ),
        json_output,
        lambda result: f"Inbox entry {result['id']} is ready.",
    )


@inbox_app.command("compile")
def inbox_compile(
    ctx: typer.Context,
    entry_id: Annotated[int, typer.Argument(min=1)],
    force: Annotated[bool, typer.Option("--force")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Compile one metadata-ready inbox entry."""

    def action() -> dict[str, Any]:
        state = _state(ctx)
        if state.settings is None:
            raise ConfigurationError("Inbox commands require --workspace PATH")
        return compile_inbox_entry(state.settings, Storage(state.workspace), entry_id, force=force)

    _run(action, json_output, lambda result: _format_extract(result["compilation"]))


@app.command("dossier")
def dossier(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show the current evidence-backed decision dossier."""
    _run(lambda: Storage(_state(ctx).workspace).get_dossier(), json_output, _format_dossier)


@evaluation_app.command("run")
def evaluation_run(
    ctx: typer.Context,
    source_id: Annotated[int, typer.Argument(min=1)],
    models: Annotated[list[str], typer.Option("--model", help="Repeat for each candidate model.")],
    segment_limit: Annotated[int, typer.Option("--segments", min=1, max=100)] = 12,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Compare model candidates without activating their extracted knowledge."""

    def action() -> dict[str, Any]:
        state = _state(ctx)
        if state.settings is None:
            raise ConfigurationError("Evaluation requires --workspace PATH")
        candidates = []
        for model in models:
            config = LLMConfig.resolve(model=model)
            candidates.append((config, OpenAICompatibleProvider(config)))
        return evaluate_models(
            Storage(state.workspace), source_id, candidates, segment_limit=segment_limit
        )

    _run(action, json_output, _format_evaluation)


@evaluation_app.command("list")
def evaluation_list(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List isolated model evaluation results."""
    _run(
        lambda: Storage(_state(ctx).workspace).list_evaluations(),
        json_output,
        lambda result: json.dumps(result, ensure_ascii=False, indent=2),
    )


@app.command("serve")
def serve(
    path: Annotated[Path, typer.Argument(help="Workspace root.")],
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8765,
) -> None:
    """Run the local Memorex web workspace."""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise typer.BadParameter("Memorex binds to loopback only in v0.2")
    from memorex.web import run_server

    run_server(path, host=host, port=port)


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
        ValueError,
    ) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        typer.echo(formatter(result))


def _format_init(result: dict[str, Any]) -> str:
    return f"Initialized {result['data_dir']} (SQLite FTS5: available)"


def _format_workspace_init(result: dict[str, Any]) -> str:
    return (
        f"Workspace {result['name']} initialized at {result['workspace_root']}\n"
        f"Inbox: {result['inbox']}"
    )


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


def _format_inbox(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "Inbox is empty."
    return "\n".join(
        f"{entry['id']}\t{entry['status']}\t{entry['canonical_path']}" for entry in entries
    )


def _format_dossier(result: dict[str, Any]) -> str:
    labels = {
        "problems": "Problems",
        "goals": "Goals",
        "ideas": "Ideas",
        "active_decisions": "Active decisions",
        "rejected_decisions": "Rejected decisions",
        "action_items": "Action items",
        "observations": "Observations",
    }
    lines = []
    for key, label in labels.items():
        items = result["sections"][key]
        if not items:
            continue
        lines.append(f"\n{label}:")
        lines.extend(f"- [C{item['id']}] {item['statement']}" for item in items)
    if not lines:
        return "Dossier is empty."
    return "\n".join(lines).lstrip()


def _format_evaluation(result: dict[str, Any]) -> str:
    lines = [f"Evaluation {result['id']}: {result['status']}"]
    lines.extend(
        f"{model['model']}: schema {model['schema_passes']}/{model['segment_count']}, "
        f"evidence {model['evidence_passes']}/{model['segment_count']}, "
        f"{model['claim_count']} claims, {model['duration_ms']} ms"
        for model in result["models"]
    )
    return "\n".join(lines)


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

from __future__ import annotations

import asyncio
import importlib.resources
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx2
from pydantic import BaseModel
from typer.testing import CliRunner

from memorex.cli import app
from memorex.compiler import compile_source
from memorex.config import LLMConfig, WorkspaceConfig, WorkspaceSettings
from memorex.domain import ModelCallResult
from memorex.evaluation import evaluate_models
from memorex.inbox import scan_inbox
from memorex.ingest import parse_source
from memorex.storage import Storage
from memorex.web import create_app


class FakeProvider:
    def __init__(self, outputs: list[str | Exception]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        messages: list[dict[str, str]],
        response_model: type[BaseModel],
        schema_name: str,
    ) -> ModelCallResult:
        self.calls.append(
            {"messages": messages, "response_model": response_model, "schema_name": schema_name}
        )
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return ModelCallResult(raw_output=output, input_tokens=100, output_tokens=30)


def test_existing_v1_database_migrates_to_workspace_schema(tmp_path: Path) -> None:
    data_dir = tmp_path / ".memorex"
    data_dir.mkdir()
    database = data_dir / "memorex.db"
    migration = importlib.resources.files("memorex.migrations").joinpath("001_initial.sql")
    with sqlite3.connect(database) as connection:
        connection.executescript(migration.read_text(encoding="utf-8"))
        connection.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, '2026-08-20')"
        )

    storage = Storage(WorkspaceConfig(data_dir))
    storage.initialize()

    with sqlite3.connect(database) as connection:
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations")]
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert versions == [1, 2, 3]
    assert {"inbox_entries", "entities", "claim_links", "user_overrides_fts"} <= tables


def compiled_output() -> str:
    return json.dumps(
        {
            "claims": [
                {
                    "statement": "Иван предложил внедрить CRM.",
                    "kind": "idea",
                    "lifecycle": "proposed",
                    "polarity": "positive",
                    "actor": "Иван",
                    "valid_from": None,
                    "valid_to": None,
                    "confidence": 0.95,
                    "evidence_quote": "Иван предложил внедрить CRM.",
                    "entities": [
                        {"name": "Иван", "entity_type": "person", "role": "actor"},
                        {"name": "CRM", "entity_type": "product", "role": "object"},
                    ],
                },
                {
                    "statement": "CRM решили не использовать из-за цены.",
                    "kind": "decision",
                    "lifecycle": "rejected",
                    "polarity": "negative",
                    "actor": None,
                    "valid_from": "2026-08-20",
                    "valid_to": None,
                    "confidence": 0.98,
                    "evidence_quote": "CRM решили не использовать из-за цены.",
                    "entities": [{"name": "CRM", "entity_type": "product", "role": "subject"}],
                },
            ],
            "relations": [
                {
                    "source_name": "Иван",
                    "source_type": "person",
                    "predicate": "proposes",
                    "target_name": "CRM",
                    "target_type": "product",
                    "evidence_quote": "Иван предложил внедрить CRM.",
                    "confidence": 0.95,
                }
            ],
        },
        ensure_ascii=False,
    )


def prepare_source(tmp_path: Path) -> tuple[Storage, int]:
    storage = Storage(WorkspaceConfig(tmp_path / ".memorex"))
    storage.initialize()
    source = tmp_path / "dialogue.md"
    source.write_text(
        "Иван предложил внедрить CRM.\n\nCRM решили не использовать из-за цены.",
        encoding="utf-8",
    )
    ingested = storage.ingest_source(parse_source(source))
    storage.set_source_metadata(
        ingested["version_id"],
        {
            "title": "Обсуждение CRM",
            "source_kind": "conversation",
            "author": "Иван",
            "authority": "primary",
            "occurred_from": "2026-08-20",
            "occurred_to": None,
            "tags_json": "[]",
        },
    )
    return storage, ingested["source_id"]


def config(model: str = "test-model") -> LLMConfig:
    return LLMConfig(base_url="http://model.test/v1", model=model, api_key=None)


def test_workspace_scan_requires_metadata_before_paid_processing(tmp_path: Path) -> None:
    settings = WorkspaceSettings.create(tmp_path / "business", "Бизнес")
    storage = Storage(settings.data)
    storage.initialize()
    (settings.inbox_dir / "notes.txt").write_text("Важная заметка.", encoding="utf-8")

    entries = scan_inbox(settings, storage)

    assert len(entries) == 1
    assert entries[0]["status"] == "metadata_required"
    ready = storage.set_inbox_metadata(
        entries[0]["id"],
        title="Заметка",
        source_kind="user_note",
        author="qSerj",
        authority="user_analysis",
        occurred_from=None,
        occurred_to=None,
        tags=["business"],
    )
    assert ready["status"] == "ready"


def test_inbox_recognizes_unambiguous_file_move_without_new_source(tmp_path: Path) -> None:
    settings = WorkspaceSettings.create(tmp_path / "business", "Бизнес")
    storage = Storage(settings.data)
    storage.initialize()
    original = settings.inbox_dir / "old.txt"
    original.write_text("Один и тот же материал.", encoding="utf-8")
    entry = scan_inbox(settings, storage)[0]
    storage.set_inbox_metadata(
        entry["id"],
        title="Материал",
        source_kind="other",
        author=None,
        authority="unknown",
        occurred_from=None,
        occurred_to=None,
        tags=[],
    )
    ingested = storage.ingest_source(parse_source(original))
    storage.mark_inbox_status(entry["id"], "succeeded", source_id=ingested["source_id"])
    moved = settings.inbox_dir / "new.txt"
    original.rename(moved)

    entries = scan_inbox(settings, storage)

    assert len(entries) == 1
    assert entries[0]["id"] == entry["id"]
    assert entries[0]["canonical_path"] == str(moved.resolve())
    assert storage.get_source(ingested["source_id"])["canonical_path"] == str(moved.resolve())


def test_decision_compiler_builds_dossier_relations_and_exact_evidence(tmp_path: Path) -> None:
    storage, source_id = prepare_source(tmp_path)
    fast = FakeProvider([compiled_output()])
    strong = FakeProvider(
        [json.dumps({"title": "CRM", "summary": "CRM предложили, но затем отвергли."})]
    )

    result = compile_source(storage, source_id, fast, config(), strong, config("strong-model"))

    assert result["status"] == "compiled"
    dossier = storage.get_dossier()
    assert [item["statement"] for item in dossier["sections"]["rejected_decisions"]] == [
        "CRM решили не использовать из-за цены."
    ]
    assert dossier["relations"][0]["predicate"] == "proposes"
    claim_id = dossier["sections"]["rejected_decisions"][0]["id"]
    evidence = storage.get_evidence_context(claim_id)
    assert evidence["quote"] == "CRM решили не использовать из-за цены."
    assert (
        evidence["quote"]
        == (
            storage.get_current_version(source_id)["normalized_text"][
                evidence["char_start"] : evidence["char_end"]
            ]
        )
    )


def test_authoritative_override_survives_derived_dossier_render(tmp_path: Path) -> None:
    storage, source_id = prepare_source(tmp_path)
    compile_source(
        storage,
        source_id,
        FakeProvider([compiled_output()]),
        config(),
        FakeProvider(
            [json.dumps({"title": "CRM", "summary": "CRM предложили, но затем отвергли."})]
        ),
        config("strong-model"),
    )
    claim = storage.get_dossier()["sections"]["rejected_decisions"][0]

    storage.review_claim(
        claim["id"],
        "override",
        statement="CRM отложили до следующего этапа.",
        kind="decision",
        lifecycle="proposed",
        reason="Уточнение автора заметки",
    )

    dossier = storage.get_dossier()
    assert dossier["sections"]["rejected_decisions"] == []
    assert dossier["sections"]["proposed_decisions"][0]["statement"] == (
        "CRM отложили до следующего этапа."
    )
    assert dossier["sections"]["proposed_decisions"][0]["review_status"] == "overridden"
    assert dossier["sections"]["proposed_decisions"][0]["override_reason"] == (
        "Уточнение автора заметки"
    )
    matches = storage.search_claims('"CRM"', 8)
    statements = [item["statement"] for item in matches]
    assert "CRM отложили до следующего этапа." in statements
    assert "CRM решили не использовать из-за цены." not in statements
    override = next(item for item in matches if item["is_override"])
    assert override["statement"] == "CRM отложили до следующего этапа."


def test_accepted_supersedes_proposal_becomes_auditable_claim_link(tmp_path: Path) -> None:
    storage, first_source_id = prepare_source(tmp_path)
    compile_source(
        storage,
        first_source_id,
        FakeProvider([compiled_output()]),
        config(),
        FakeProvider(
            [json.dumps({"title": "CRM", "summary": "CRM предложили, но затем отвергли."})]
        ),
        config("strong-model"),
    )
    existing_claim_id = storage.get_dossier()["sections"]["rejected_decisions"][0]["id"]
    follow_up = tmp_path / "follow-up.md"
    follow_up.write_text("CRM решили внедрить на следующем этапе.", encoding="utf-8")
    ingested = storage.ingest_source(parse_source(follow_up))
    storage.set_source_metadata(
        ingested["version_id"],
        {
            "title": "Продолжение обсуждения",
            "source_kind": "conversation",
            "author": "Иван",
            "authority": "primary",
            "occurred_from": "2026-08-21",
            "occurred_to": None,
            "tags_json": "[]",
        },
    )
    next_output = json.dumps(
        {
            "claims": [
                {
                    "statement": "CRM решили внедрить на следующем этапе.",
                    "kind": "decision",
                    "lifecycle": "active",
                    "polarity": "positive",
                    "actor": None,
                    "valid_from": "2026-08-21",
                    "valid_to": None,
                    "confidence": 0.99,
                    "evidence_quote": "CRM решили внедрить на следующем этапе.",
                    "entities": [{"name": "CRM", "entity_type": "product", "role": "subject"}],
                }
            ],
            "relations": [],
        },
        ensure_ascii=False,
    )
    resolution = json.dumps(
        {
            "proposals": [
                {
                    "new_claim_index": 0,
                    "existing_claim_id": existing_claim_id,
                    "proposal_type": "supersedes",
                    "rationale": "Более новое решение заменяет старое.",
                    "confidence": 0.98,
                }
            ]
        },
        ensure_ascii=False,
    )

    compile_source(
        storage,
        ingested["source_id"],
        FakeProvider([next_output]),
        config(),
        FakeProvider(
            [
                json.dumps({"title": "CRM", "summary": "CRM решили внедрить позже."}),
                resolution,
            ]
        ),
        config("strong-model"),
    )
    proposal = storage.list_review_proposals()[0]

    storage.review_proposal(proposal["id"], True)

    link = storage.get_dossier()["claim_links"][0]
    assert link["relation_type"] == "supersedes"
    assert link["target_claim_id"] == existing_claim_id


def test_model_evaluation_does_not_activate_candidate_output(tmp_path: Path) -> None:
    storage, source_id = prepare_source(tmp_path)
    before = storage.list_claims(source_id)

    result = evaluate_models(
        storage,
        source_id,
        [(config("candidate"), FakeProvider([compiled_output()]))],
    )

    assert result["status"] == "succeeded"
    assert result["models"][0]["schema_passes"] == 1
    assert result["models"][0]["evidence_passes"] == 1
    assert storage.list_claims(source_id) == before


def test_local_web_workspace_exposes_staging_and_model_profiles(tmp_path: Path) -> None:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Предприниматель")
    (settings.inbox_dir / "notes.md").write_text("# Идея\n\nПроверить спрос.", encoding="utf-8")

    async def exercise_app() -> None:
        transport = httpx2.ASGITransport(app=create_app(settings.root))
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            inbox = await client.get("/api/inbox")
            assert inbox.status_code == 200
            entry_id = inbox.json()[0]["id"]
            updated = await client.post(
                f"/api/inbox/{entry_id}/metadata",
                json={
                    "title": "Идея",
                    "source_kind": "user_note",
                    "author": "qSerj",
                    "authority": "user_analysis",
                    "tags": ["business"],
                },
            )
            assert updated.status_code == 200
            assert updated.json()["status"] == "ready"
            profile = await client.post(
                "/api/models/profile",
                json={"fast": "qwen/test", "strong": "kimi/test", "answer": ""},
            )
            assert profile.status_code == 200
            assert profile.json()["strong"] == "kimi/test"
            assert (await client.get("/dossier")).status_code == 200
            assert (await client.get("/ask")).status_code == 200

    asyncio.run(exercise_app())


def test_cli_creates_isolated_workspace(tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / "new-workspace"

    result = runner.invoke(app, ["workspace", "init", str(root), "--name", "Новая база", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["name"] == "Новая база"
    assert (root / "memorex.toml").is_file()
    assert (root / ".memorex" / "memorex.db").is_file()

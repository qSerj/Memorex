from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from memorex.config import LLMConfig, WorkspaceConfig
from memorex.domain import ModelCallResult
from memorex.extraction import ExtractionError, extract_source
from memorex.ingest import parse_source
from memorex.query import QueryError, answer_question, build_fts_query
from memorex.storage import Storage


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
        return ModelCallResult(raw_output=output, input_tokens=10, output_tokens=5)


def prepared_source(tmp_path: Path) -> tuple[Storage, int]:
    storage = Storage(WorkspaceConfig(tmp_path / ".memorex"))
    storage.initialize()
    source = tmp_path / "source.md"
    source.write_text("# Storage\n\nMemorex uses SQLite for local storage.", encoding="utf-8")
    result = storage.ingest_source(parse_source(source))
    return storage, result["source_id"]


def llm_config() -> LLMConfig:
    return LLMConfig(base_url="http://model.test/v1", model="test-model", api_key=None)


def claim_output(statement: str = "Memorex uses SQLite for local storage.") -> str:
    return json.dumps(
        {
            "claims": [
                {
                    "statement": statement,
                    "confidence": 0.98,
                    "evidence_quote": "Memorex uses SQLite for local storage.",
                }
            ]
        }
    )


def test_extraction_persists_exact_evidence_and_is_idempotent(tmp_path: Path) -> None:
    storage, source_id = prepared_source(tmp_path)
    provider = FakeProvider([claim_output()])

    first = extract_source(storage, source_id, provider, llm_config())
    second = extract_source(storage, source_id, provider, llm_config())

    assert first["status"] == "extracted"
    assert first["claims"] == 1
    assert second["status"] == "unchanged"
    assert len(provider.calls) == 1
    claims = storage.list_claims(source_id)
    assert len(claims) == 1
    claim = storage.get_claim(claims[0]["id"])
    version = storage.get_current_version(source_id)
    assert version["normalized_text"][claim["char_start"] : claim["char_end"]] == claim["quote"]


def test_invalid_evidence_fails_job_without_active_claims(tmp_path: Path) -> None:
    storage, source_id = prepared_source(tmp_path)
    invalid = json.dumps(
        {
            "claims": [
                {
                    "statement": "Unsupported claim.",
                    "confidence": 0.5,
                    "evidence_quote": "This text is absent.",
                }
            ]
        }
    )
    provider = FakeProvider([invalid, invalid, invalid])

    with pytest.raises(ExtractionError, match="failed after 3 attempts"):
        extract_source(storage, source_id, provider, llm_config())

    assert storage.list_claims(source_id) == []
    assert len(provider.calls) == 3


def test_force_extraction_replaces_active_claim_set(tmp_path: Path) -> None:
    storage, source_id = prepared_source(tmp_path)
    first_provider = FakeProvider([claim_output()])
    extract_source(storage, source_id, first_provider, llm_config())
    old_claim_id = storage.list_claims(source_id)[0]["id"]

    second_provider = FakeProvider([claim_output("SQLite is Memorex's local store.")])
    result = extract_source(storage, source_id, second_provider, llm_config(), force=True)

    assert result["status"] == "extracted"
    active = storage.list_claims(source_id)
    assert [claim["statement"] for claim in active] == ["SQLite is Memorex's local store."]
    assert storage.get_claim(old_claim_id)["statement"] == "Memorex uses SQLite for local storage."


def test_claims_first_answer_validates_and_returns_evidence(tmp_path: Path) -> None:
    storage, source_id = prepared_source(tmp_path)
    extract_source(storage, source_id, FakeProvider([claim_output()]), llm_config())
    claim_id = storage.list_claims(source_id)[0]["id"]
    answer_json = json.dumps(
        {
            "answer": f"Memorex uses SQLite for local storage [C{claim_id}].",
            "citations": [claim_id],
        }
    )
    provider = FakeProvider([answer_json])

    result = answer_question(storage, "What uses SQLite?", provider, llm_config())

    assert result["status"] == "answered"
    assert result["citations"][0]["id"] == claim_id
    assert result["citations"][0]["quote"] == "Memorex uses SQLite for local storage."


def test_answer_rejects_unknown_citations_after_retries(tmp_path: Path) -> None:
    storage, source_id = prepared_source(tmp_path)
    extract_source(storage, source_id, FakeProvider([claim_output()]), llm_config())
    invalid = json.dumps({"answer": "Unsupported [C999].", "citations": [999]})
    provider = FakeProvider([invalid, invalid, invalid])

    with pytest.raises(QueryError, match="failed after 3 attempts"):
        answer_question(storage, "What uses SQLite?", provider, llm_config())


def test_no_matches_skips_model_call(tmp_path: Path) -> None:
    storage, source_id = prepared_source(tmp_path)
    extract_source(storage, source_id, FakeProvider([claim_output()]), llm_config())
    provider = FakeProvider([])

    result = answer_question(storage, "PostgreSQL?", provider, llm_config())

    assert result["status"] == "no_matches"
    assert result["citations"] == []
    assert provider.calls == []


def test_fts_query_is_safe_and_unicode_aware() -> None:
    assert build_fts_query('SQLite: "локальное" хранилище?') == (
        '"sqlite" OR "локальное" OR "хранилище"'
    )

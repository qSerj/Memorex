from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from memorex.compiler import (
    COMPILATION_PROMPT_VERSION,
    COMPILATION_SYSTEM_PROMPT,
    _anchor_compilation,
    _metadata_context,
)
from memorex.config import LLMConfig
from memorex.domain import CompilationBatch
from memorex.llm import LLMProvider
from memorex.storage import Storage


def evaluate_models(
    storage: Storage,
    source_id: int,
    candidates: Sequence[tuple[LLMConfig, LLMProvider]],
    *,
    segment_limit: int = 12,
) -> dict[str, Any]:
    if not candidates:
        raise ValueError("At least one model candidate is required")
    version = storage.get_current_version(source_id)
    segments = storage.get_segments(version["id"])[:segment_limit]
    metadata = storage.get_source_metadata(version["id"])
    run_id = storage.start_evaluation(version["id"], COMPILATION_PROMPT_VERSION)
    try:
        for config, provider in candidates:
            for segment in segments:
                started = time.monotonic()
                raw_output: str | None = None
                input_tokens: int | None = None
                output_tokens: int | None = None
                schema_valid = False
                evidence_valid = False
                claim_count = 0
                error: str | None = None
                try:
                    content = (
                        f"{_metadata_context(metadata)}\n\n<segment>\n{segment['text']}\n</segment>"
                    )
                    response = provider.complete(
                        messages=[
                            {"role": "system", "content": COMPILATION_SYSTEM_PROMPT},
                            {"role": "user", "content": content},
                        ],
                        response_model=CompilationBatch,
                        schema_name="knowledge_compilation_batch",
                    )
                    raw_output = response.raw_output
                    input_tokens = response.input_tokens
                    output_tokens = response.output_tokens
                    batch = CompilationBatch.model_validate_json(raw_output)
                    schema_valid = True
                    anchored = _anchor_compilation(batch, segment)
                    evidence_valid = True
                    claim_count = len(anchored)
                except Exception as exc:
                    error = str(exc)
                storage.record_evaluation_result(
                    run_id,
                    {
                        "model": config.model,
                        "segment_id": segment["id"],
                        "schema_valid": schema_valid,
                        "evidence_valid": evidence_valid,
                        "claim_count": claim_count,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "duration_ms": int((time.monotonic() - started) * 1_000),
                        "error": error,
                        "raw_output": raw_output,
                    },
                )
    except Exception as exc:
        storage.finish_evaluation(run_id, str(exc))
        raise
    storage.finish_evaluation(run_id)
    return next(item for item in storage.list_evaluations() if item["id"] == run_id)

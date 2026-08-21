from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from memorex.config import LLMConfig, WorkspaceSettings
from memorex.evaluation import evaluate_models
from memorex.inbox import compile_inbox_entry, scan_inbox
from memorex.llm import OpenAICompatibleProvider
from memorex.query import QueryError, answer_question
from memorex.storage import RecordNotFound, Storage

TEMPLATE_DIR = Path(__file__).with_name("templates")


class MetadataRequest(BaseModel):
    title: str = Field(min_length=1)
    source_kind: str
    author: str | None = None
    authority: str
    occurred_from: str | None = None
    occurred_to: str | None = None
    tags: list[str] = []


class ClaimReviewRequest(BaseModel):
    action: str
    statement: str | None = None
    kind: str | None = None
    lifecycle: str | None = None
    reason: str | None = None


class ProposalReviewRequest(BaseModel):
    accept: bool


class ModelProfileRequest(BaseModel):
    fast: str
    strong: str
    answer: str


class AskRequest(BaseModel):
    question: str = Field(min_length=1)
    limit: int = Field(default=8, ge=1, le=50)


def create_app(root: Path) -> FastAPI:
    settings = WorkspaceSettings.load(root)
    storage = Storage(settings.data)
    templates = Jinja2Templates(directory=TEMPLATE_DIR)
    storage.initialize()
    storage.recover_inbox_jobs()
    scan_inbox(settings, storage)
    app = FastAPI(title=f"Memorex — {settings.name}")

    def page_context(request: Request, page: str) -> dict[str, Any]:
        return {
            "request": request,
            "page": page,
            "workspace": WorkspaceSettings.load(root),
            "inbox": storage.list_inbox_entries(),
            "jobs": storage.list_jobs(),
            "dossier": storage.get_dossier(),
            "proposals": storage.list_review_proposals(),
            "evaluations": storage.list_evaluations(),
        }

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="workspace.html", context=page_context(request, "dashboard")
        )

    @app.get("/inbox", response_class=HTMLResponse)
    async def inbox_page(request: Request) -> HTMLResponse:
        scan_inbox(settings, storage)
        return templates.TemplateResponse(
            request=request, name="workspace.html", context=page_context(request, "inbox")
        )

    @app.get("/dossier", response_class=HTMLResponse)
    async def dossier_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="workspace.html", context=page_context(request, "dossier")
        )

    @app.get("/ask", response_class=HTMLResponse)
    async def ask_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="workspace.html", context=page_context(request, "ask")
        )

    @app.post("/ask", response_class=HTMLResponse)
    async def ask_form(
        request: Request,
        question: Annotated[str, Form()],
        limit: Annotated[int, Form()] = 8,
    ) -> HTMLResponse:
        current = WorkspaceSettings.load(root)
        config = LLMConfig.resolve_role("answer", current)
        result = await asyncio.to_thread(
            answer_question,
            storage,
            question,
            OpenAICompatibleProvider(config),
            config,
            limit=limit,
        )
        context = page_context(request, "ask")
        context["answer"] = result
        context["question"] = question
        return templates.TemplateResponse(request=request, name="workspace.html", context=context)

    @app.get("/review", response_class=HTMLResponse)
    async def review_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="workspace.html", context=page_context(request, "review")
        )

    @app.get("/models", response_class=HTMLResponse)
    async def models_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="workspace.html", context=page_context(request, "models")
        )

    @app.get("/evidence/{claim_id}", response_class=HTMLResponse)
    async def evidence_page(request: Request, claim_id: int) -> HTMLResponse:
        try:
            evidence = storage.get_evidence_context(claim_id)
        except RecordNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        context = page_context(request, "evidence")
        context["evidence"] = evidence
        return templates.TemplateResponse(request=request, name="workspace.html", context=context)

    @app.post("/scan")
    async def scan() -> RedirectResponse:
        scan_inbox(settings, storage)
        return RedirectResponse("/inbox", status_code=303)

    @app.post("/inbox/{entry_id}/metadata")
    async def set_metadata_form(
        entry_id: int,
        title: Annotated[str, Form()],
        source_kind: Annotated[str, Form()],
        authority: Annotated[str, Form()],
        author: Annotated[str | None, Form()] = None,
        occurred_from: Annotated[str | None, Form()] = None,
        occurred_to: Annotated[str | None, Form()] = None,
        tags: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        storage.set_inbox_metadata(
            entry_id,
            title=title,
            source_kind=source_kind,
            author=author,
            authority=authority,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            tags=[tag.strip() for tag in tags.split(",") if tag.strip()],
        )
        return RedirectResponse("/inbox", status_code=303)

    async def compile_safely(entry_id: int) -> None:
        try:
            await asyncio.to_thread(
                compile_inbox_entry,
                WorkspaceSettings.load(root),
                storage,
                entry_id,
            )
        except Exception:
            return

    @app.post("/inbox/{entry_id}/compile")
    async def compile_form(entry_id: int, background_tasks: BackgroundTasks) -> RedirectResponse:
        entry = storage.get_inbox_entry(entry_id)
        if entry["status"] not in {"ready", "failed"}:
            raise HTTPException(status_code=409, detail="Entry is not ready")
        storage.mark_inbox_status(entry_id, "queued")
        background_tasks.add_task(compile_safely, entry_id)
        return RedirectResponse("/inbox", status_code=303)

    @app.post("/claims/{claim_id}/review")
    async def review_claim_form(
        claim_id: int,
        action: Annotated[str, Form()],
        statement: Annotated[str | None, Form()] = None,
        kind: Annotated[str | None, Form()] = None,
        lifecycle: Annotated[str | None, Form()] = None,
        reason: Annotated[str | None, Form()] = None,
    ) -> RedirectResponse:
        storage.review_claim(
            claim_id,
            action,
            statement=statement,
            kind=kind,
            lifecycle=lifecycle,
            reason=reason,
        )
        return RedirectResponse("/dossier", status_code=303)

    @app.post("/proposals/{proposal_id}/review")
    async def review_proposal_form(
        proposal_id: int, decision: Annotated[str, Form()]
    ) -> RedirectResponse:
        storage.review_proposal(proposal_id, decision == "accept")
        return RedirectResponse("/review", status_code=303)

    async def evaluate_safely(source_id: int, model_ids: list[str]) -> None:
        candidates = []
        for model_id in model_ids:
            config = LLMConfig.resolve(model=model_id)
            candidates.append((config, OpenAICompatibleProvider(config)))
        await asyncio.to_thread(evaluate_models, storage, source_id, candidates)

    @app.post("/models/evaluate")
    async def evaluate_form(
        background_tasks: BackgroundTasks,
        source_id: Annotated[int, Form()],
        models: Annotated[str, Form()],
    ) -> RedirectResponse:
        model_ids = [model.strip() for model in models.split(",") if model.strip()]
        if not model_ids:
            raise HTTPException(status_code=422, detail="At least one model is required")
        background_tasks.add_task(evaluate_safely, source_id, model_ids)
        return RedirectResponse("/models", status_code=303)

    @app.post("/models/profile")
    async def model_profile_form(
        fast: Annotated[str, Form()],
        strong: Annotated[str, Form()],
        answer: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        WorkspaceSettings.load(root).set_models(fast=fast, strong=strong, answer=answer)
        return RedirectResponse("/models", status_code=303)

    @app.get("/api/inbox")
    async def api_inbox() -> list[dict[str, Any]]:
        return scan_inbox(settings, storage)

    @app.post("/api/inbox/{entry_id}/metadata")
    async def api_metadata(entry_id: int, payload: MetadataRequest) -> dict[str, Any]:
        return storage.set_inbox_metadata(entry_id, **payload.model_dump())

    @app.post("/api/inbox/{entry_id}/compile", status_code=202)
    async def api_compile(entry_id: int, background_tasks: BackgroundTasks) -> dict[str, Any]:
        entry = storage.get_inbox_entry(entry_id)
        if entry["status"] not in {"ready", "failed"}:
            raise HTTPException(status_code=409, detail="Entry is not ready")
        storage.mark_inbox_status(entry_id, "queued")
        background_tasks.add_task(compile_safely, entry_id)
        return {"id": entry_id, "status": "queued"}

    @app.get("/api/jobs")
    async def api_jobs() -> list[dict[str, Any]]:
        return storage.list_jobs()

    @app.get("/api/dossier")
    async def api_dossier() -> dict[str, Any]:
        return storage.get_dossier()

    @app.post("/api/ask")
    async def api_ask(payload: AskRequest) -> dict[str, Any]:
        current = WorkspaceSettings.load(root)
        config = LLMConfig.resolve_role("answer", current)
        try:
            return await asyncio.to_thread(
                answer_question,
                storage,
                payload.question,
                OpenAICompatibleProvider(config),
                config,
                limit=payload.limit,
            )
        except QueryError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/evidence/{claim_id}")
    async def api_evidence(claim_id: int) -> dict[str, Any]:
        try:
            return storage.get_evidence_context(claim_id)
        except RecordNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/claims/{claim_id}/review")
    async def api_review_claim(claim_id: int, payload: ClaimReviewRequest) -> dict[str, Any]:
        return storage.review_claim(claim_id, **payload.model_dump())

    @app.get("/api/reviews")
    async def api_reviews() -> list[dict[str, Any]]:
        return storage.list_review_proposals()

    @app.post("/api/reviews/{proposal_id}")
    async def api_review_proposal(
        proposal_id: int, payload: ProposalReviewRequest
    ) -> dict[str, Any]:
        return storage.review_proposal(proposal_id, payload.accept)

    @app.get("/api/evaluations")
    async def api_evaluations() -> list[dict[str, Any]]:
        return storage.list_evaluations()

    @app.post("/api/models/profile")
    async def api_model_profile(payload: ModelProfileRequest) -> dict[str, Any]:
        updated = WorkspaceSettings.load(root).set_models(**payload.model_dump())
        return {
            "fast": updated.fast_model,
            "strong": updated.strong_model,
            "answer": updated.answer_model,
        }

    return app


def run_server(root: Path, *, host: str, port: int) -> None:
    app = create_app(root)
    uvicorn.run(app, host=host, port=port)

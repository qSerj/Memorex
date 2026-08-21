from __future__ import annotations

import html
import json
import os
import re
import threading
import time
import uuid
import webbrowser
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from memorex.config import WorkspaceSettings
from memorex.wiki_first.service import RunnerResolver, WikiFirstError, WikiFirstService

TEMPLATES = Path(__file__).with_name("wiki_templates")
STATIC = Path(__file__).with_name("wiki_static")
MAX_UPLOAD = 10 * 1024 * 1024
WIKI_LINK = re.compile(r"\[\[([a-z0-9][a-z0-9-]*)\]\]")
MD_LINK = re.compile(r"\[([^]]+)\]\(([^)]+)\)")


class UserSettings:
    def __init__(self, path: Path | None = None):
        self.path = (
            path
            or Path(os.getenv("MEMOREX_APP_SETTINGS", "~/.config/memorex/app.json")).expanduser()
        )

    def load(self) -> Path | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8")).get("last_workspace")
            return Path(value).expanduser().resolve() if value else None
        except (OSError, json.JSONDecodeError, AttributeError):
            return None

    def save(self, root: Path) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"last_workspace": str(root.resolve())}), encoding="utf-8")


class WorkspaceTasks:
    def __init__(self, settings: WorkspaceSettings, resolver: RunnerResolver | None):
        self.settings, self.resolver = settings, resolver
        self.mutations = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memorex-mutation")
        self.queries = ThreadPoolExecutor(max_workers=2, thread_name_prefix="memorex-query")
        self.cancel: dict[str, threading.Event] = {}
        self.futures: dict[str, Future[object]] = {}

    def submit(self, kind: str, action: str, *args: object) -> str:
        task_id = uuid.uuid4().hex[:12]
        event = threading.Event()
        self.cancel[task_id] = event

        def work() -> object:
            service = WikiFirstService(
                self.settings, runner_resolver=self.resolver, cancel_event=event, task_id=task_id
            )
            try:
                if action == "ingest":
                    return service.ingest()
                if action == "revise":
                    return service.revise(str(args[0]), str(args[1]))
                if action == "retry":
                    return service.retry(str(args[0]))
                if action == "ask":
                    return service.ask(str(args[0]), session_id=str(args[1]))
                raise ValueError(f"Unknown task action: {action}")
            except Exception as exc:
                service.storage.add_task_event(
                    task_id, "error", {"phase": "error", "message": str(exc), "status": "failed"}
                )
                raise

        pool = self.queries if kind == "query" else self.mutations
        self.futures[task_id] = pool.submit(work)
        return task_id

    def stop(self, task_id: str) -> bool:
        event = self.cancel.get(task_id)
        if event is None:
            return False
        event.set()
        return True


def create_app(
    root: Path | None = None,
    *,
    runner_resolver: RunnerResolver | None = None,
    user_settings_path: Path | None = None,
) -> FastAPI:
    preferences = UserSettings(user_settings_path)
    chosen = root.expanduser().resolve() if root else preferences.load()
    state: dict[str, Any] = {"root": None, "settings": None, "service": None, "tasks": None}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        tasks: WorkspaceTasks | None = state.get("tasks")
        if tasks:
            tasks.mutations.shutdown(wait=False, cancel_futures=True)
            tasks.queries.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="Memorex Wiki", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    templates = Jinja2Templates(directory=TEMPLATES)

    def select(path: Path, create: bool = False, name: str | None = None) -> None:
        resolved = path.expanduser().resolve()
        settings = (
            WorkspaceSettings.create(resolved, name or resolved.name)
            if create
            else WorkspaceSettings.load(resolved)
        )
        service = WikiFirstService(settings, runner_resolver=runner_resolver)
        service.initialize()
        service.storage.recover_interrupted_jobs()
        preferences.save(resolved)
        state.update(
            root=resolved,
            settings=settings,
            service=service,
            tasks=WorkspaceTasks(settings, runner_resolver),
        )

    if chosen and (chosen / "memorex.toml").is_file():
        select(chosen)

    def service() -> WikiFirstService:
        if state["service"] is None:
            raise HTTPException(status_code=409, detail="Choose a workspace first")
        return state["service"]

    def context(request: Request, page: str, **extra: object) -> dict[str, object]:
        data: dict[str, object] = {
            "request": request,
            "page": page,
            "workspace": state["settings"],
            "setup": state["service"] is None,
        }
        if state["service"]:
            svc = service()
            snapshot = svc.storage.active_snapshot()
            data.update(
                status=svc.status(),
                pages=svc.storage.list_pages(snapshot),
                jobs=svc.storage.jobs(),
                history=svc.history(),
                chats=svc.storage.chat_sessions(),
                proposal=svc.storage.latest_proposal(),
            )
        data.update(extra)
        return data

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="app.html", context=context(request, "wiki")
        )

    @app.post("/workspace")
    async def workspace(
        path: Annotated[str, Form()],
        action: Annotated[str, Form()] = "open",
        name: Annotated[str | None, Form()] = None,
    ) -> RedirectResponse:
        try:
            select(Path(path), create=action == "create", name=name)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RedirectResponse("/", status_code=303)

    @app.get("/wiki/{slug}", response_class=HTMLResponse)
    async def wiki_page(request: Request, slug: str) -> HTMLResponse:
        if not re.fullmatch(r"(?:README|[a-z0-9]+(?:-[a-z0-9]+)*)", slug):
            raise HTTPException(404)
        svc = service()
        pages = {p["slug"]: p for p in svc.storage.list_pages()}
        page = pages.get(slug)
        if page is None:
            raise HTTPException(404)
        backlinks = [p for p in pages.values() if slug in WIKI_LINK.findall(str(p["text"]))]
        return templates.TemplateResponse(
            request=request,
            name="app.html",
            context=context(
                request,
                "wiki-page",
                article={**page, "html": render_markdown(str(page["text"]))},
                backlinks=backlinks,
            ),
        )

    @app.get("/inbox", response_class=HTMLResponse)
    async def inbox(request: Request) -> HTMLResponse:
        scanned = service().scan()
        return templates.TemplateResponse(
            request=request, name="app.html", context=context(request, "inbox", scanned=scanned)
        )

    @app.post("/upload")
    async def upload(files: Annotated[list[UploadFile], File()]) -> RedirectResponse:
        settings: WorkspaceSettings = state["settings"]
        for upload in files:
            name = Path(upload.filename or "").name
            if name != upload.filename or Path(name).suffix.lower() not in {".md", ".txt"}:
                raise HTTPException(415, "Only .md and .txt are accepted")
            data = await upload.read(MAX_UPLOAD + 1)
            if len(data) > MAX_UPLOAD:
                raise HTTPException(413, "File is larger than 10 MiB")
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HTTPException(415, "File must be UTF-8") from exc
            (settings.inbox_dir / name).write_bytes(data)
        service().scan()
        return RedirectResponse("/inbox", status_code=303)

    @app.get("/review", response_class=HTMLResponse)
    async def review(request: Request) -> HTMLResponse:
        proposal = service().storage.latest_proposal()
        details = service().review(str(proposal["job_id"])) if proposal else None
        if details:
            details["report_html"] = render_markdown(str(details["report"]))
            details["rendered"] = _render_changed(service(), details)
        return templates.TemplateResponse(
            request=request, name="app.html", context=context(request, "review", review=details)
        )

    @app.post("/review/apply")
    async def apply() -> RedirectResponse:
        proposal = service().storage.latest_proposal()
        if not proposal:
            raise HTTPException(409, "No proposal awaiting review")
        service().apply(str(proposal["job_id"]))
        return RedirectResponse("/", status_code=303)

    @app.post("/review/reject")
    async def reject(reason: Annotated[str, Form()] = "rejected in Web UI") -> RedirectResponse:
        proposal = service().storage.latest_proposal()
        if not proposal:
            raise HTTPException(409, "No proposal awaiting review")
        service().reject(str(proposal["job_id"]), reason)
        return RedirectResponse("/review", status_code=303)

    @app.post("/review/revise")
    async def revise(feedback: Annotated[str, Form()]) -> RedirectResponse:
        proposal = service().storage.latest_proposal()
        if not proposal:
            raise HTTPException(409, "No proposal awaiting review")
        task = state["tasks"].submit("mutation", "revise", str(proposal["job_id"]), feedback)
        return RedirectResponse(f"/tasks/{task}", status_code=303)

    @app.post("/tasks/ingest")
    async def ingest() -> RedirectResponse:
        task = state["tasks"].submit("mutation", "ingest")
        return RedirectResponse(f"/tasks/{task}", status_code=303)

    @app.post("/tasks/retry/{job_id}")
    async def retry(job_id: str) -> RedirectResponse:
        task = state["tasks"].submit("mutation", "retry", job_id)
        return RedirectResponse(f"/tasks/{task}", status_code=303)

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    async def task_page(request: Request, task_id: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="app.html", context=context(request, "task", task_id=task_id)
        )

    @app.get("/tasks/{task_id}/events")
    async def events(task_id: str) -> StreamingResponse:
        def stream():
            after = 0
            started = time.monotonic()
            while time.monotonic() - started < 1800:
                rows = service().storage.task_events(task_id, after)
                for row in rows:
                    after = int(row["id"])
                    yield f"id: {after}\ndata: {json.dumps(row['payload'], ensure_ascii=False)}\n\n"
                    if row["phase"] in {"review-ready", "error", "cancelled"}:
                        return
                future = state["tasks"].futures.get(task_id)
                if future and future.done() and not rows:
                    yield 'data: {"phase":"done"}\n\n'
                    return
                time.sleep(0.25)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/tasks/{task_id}/stop")
    async def stop(task_id: str) -> JSONResponse:
        if not state["tasks"].stop(task_id):
            raise HTTPException(404)
        return JSONResponse({"status": "stopping"})

    @app.get("/chat", response_class=HTMLResponse)
    async def chat(request: Request, session: str | None = None) -> HTMLResponse:
        messages = service().storage.chat_messages(session) if session else []
        for message in messages:
            if message["role"] == "assistant":
                message["html"] = render_markdown(str(message["content"]))
        return templates.TemplateResponse(
            request=request,
            name="app.html",
            context=context(request, "chat", session_id=session, messages=messages),
        )

    @app.post("/chat")
    async def chat_post(
        question: Annotated[str, Form()], session: Annotated[str | None, Form()] = None
    ) -> RedirectResponse:
        session = session or service().storage.create_chat(question[:80])
        task = state["tasks"].submit("query", "ask", question, session)
        return RedirectResponse(f"/tasks/{task}?next=/chat?session={session}", status_code=303)

    @app.get("/history", response_class=HTMLResponse)
    async def history(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="app.html", context=context(request, "history")
        )

    @app.post("/history/{snapshot}/rollback")
    async def rollback(snapshot: str) -> RedirectResponse:
        service().rollback(snapshot)
        return RedirectResponse("/history", status_code=303)

    @app.post("/vault/refresh")
    async def refresh_vault() -> RedirectResponse:
        service().storage.sync_vault()
        return RedirectResponse("/", status_code=303)

    @app.get("/source/{name}", response_class=HTMLResponse)
    async def source_view(
        request: Request, name: str, start: int = 1, end: int | None = None
    ) -> HTMLResponse:
        if name != Path(name).name:
            raise HTTPException(404)
        root_path = service().storage.snapshot_path(service().storage.active_snapshot()) / "sources"
        target = (root_path / name).resolve()
        try:
            target.relative_to(root_path.resolve())
        except ValueError as exc:
            raise HTTPException(404) from exc
        if not target.is_file():
            raise HTTPException(404)
        lines = target.read_text(encoding="utf-8").splitlines()
        start = max(1, start)
        end = min(len(lines), end or start + 40)
        return templates.TemplateResponse(
            request=request,
            name="app.html",
            context=context(
                request,
                "source",
                source_name=name,
                source_lines=list(enumerate(lines[start - 1 : end], start)),
            ),
        )

    @app.exception_handler(WikiFirstError)
    async def wiki_error(_request: Request, exc: WikiFirstError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    return app


def render_markdown(text: str) -> str:
    escaped = html.escape(text)
    blocks = []
    paragraph = []

    def flush() -> None:
        if paragraph:
            blocks.append("<p>" + _inline(" ".join(paragraph)) + "</p>")
            paragraph.clear()

    for line in escaped.splitlines():
        if line.startswith("### "):
            flush()
            blocks.append(f"<h3>{_inline(line[4:])}</h3>")
        elif line.startswith("## "):
            flush()
            blocks.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.startswith("# "):
            flush()
            blocks.append(f"<h1>{_inline(line[2:])}</h1>")
        elif line.startswith("- "):
            flush()
            blocks.append(f"<p class=bullet>• {_inline(line[2:])}</p>")
        elif not line.strip():
            flush()
        else:
            paragraph.append(line)
    flush()
    return "\n".join(blocks)


def _inline(text: str) -> str:
    text = WIKI_LINK.sub(lambda m: f'<a href="/wiki/{m.group(1)}">{m.group(1)}</a>', text)

    def link(match: re.Match[str]) -> str:
        label, target = match.groups()
        if target.startswith("../sources/"):
            raw = target.removeprefix("../sources/")
            name, _, fragment = raw.partition("#")
            query = ""
            found = re.fullmatch(r"L(\d+)(?:-L?(\d+))?", fragment)
            if found:
                query = f"?start={found.group(1)}&end={found.group(2) or found.group(1)}"
            return f'<a href="/source/{html.escape(Path(name).name)}{query}">{label}</a>'
        return label

    return MD_LINK.sub(link, text)


def _render_changed(service: WikiFirstService, review: dict[str, object]) -> list[dict[str, str]]:
    proposal = service.storage.proposal(str(review["job_id"]))
    root = service.storage.root / str(proposal["relative_path"]) / "wiki"
    base = (
        service.storage.snapshot_path(service._snapshot(str(proposal["base_snapshot_id"]))) / "wiki"
    )
    return [
        {
            "name": name,
            "before_html": render_markdown((base / name).read_text(encoding="utf-8"))
            if (base / name).is_file()
            else "<p>Новая страница</p>",
            "html": render_markdown((root / name).read_text(encoding="utf-8")),
        }
        for name in review["changed_pages"]
        if (root / name).is_file()
    ]


def run_server(
    path: Path | None, *, host: str = "127.0.0.1", port: int = 8766, open_browser: bool = True
) -> None:
    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    uvicorn.run(create_app(path), host=host, port=port)

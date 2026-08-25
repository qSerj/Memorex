from __future__ import annotations

import asyncio
import html
import json
import mimetypes
import os
import re
import sqlite3
import tempfile
import threading
import uuid
import webbrowser
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlencode

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from memorex.config import WorkspaceSettings
from memorex.wiki_first.models import PacketUpload
from memorex.wiki_first.service import (
    RunnerResolver,
    WikiFirstError,
    WikiFirstService,
    split_note_markdown,
)
from memorex.workspace_archive import (
    WorkspaceArchiveError,
    create_workspace_archive,
    restore_workspace_archive,
)

TEMPLATES = Path(__file__).with_name("wiki_templates")
STATIC = Path(__file__).with_name("wiki_static")
MAX_UPLOAD = 10 * 1024 * 1024
WIKI_LINK = re.compile(r"\[\[([a-z0-9][a-z0-9-]*)\]\]")
MD_LINK = re.compile(r"\[([^]]+)\]\(([^)]+)\)")
MD_IMAGE = re.compile(r"!\[([^]]*)\]\(([^)]+)\)")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
PACKET_STATE_LABELS = {
    "queued": "Сохранён · ожидает анализа",
    "processing": "Анализируется",
    "retry_wait": "Связь прервалась · повторим автоматически",
    "review": "Готов к проверке",
    "remembered": "Добавлен в память",
    "processed": "Обработан · Wiki без изменений",
    "failed": "Анализ не завершён",
    "ready": "Сохранён · можно разобрать",
    "waiting_importer": "Сохранён · ожидает импортера",
    "stored": "Сохранён без анализа",
}


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
        self.kinds: dict[str, str] = {}
        self.queue_wake = threading.Event()
        self.queue_paused = threading.Event()
        self.shutdown_event = threading.Event()
        self.queue_thread: threading.Thread | None = None
        self.task_lock = threading.RLock()

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
                if action == "packet":
                    return service.ingest_packet(str(args[0]))
                if action == "packet_claimed":
                    return service.ingest_packet(str(args[0]), queue_claimed=True)
                if action == "revise":
                    return service.revise(str(args[0]), str(args[1]))
                if action == "retry":
                    return service.retry(str(args[0]))
                if action == "ask":
                    result = service.ask(str(args[0]), session_id=str(args[1]))
                    service.storage.add_task_event(
                        task_id,
                        "done",
                        {"phase": "done", "next": f"/chat?session={args[1]}"},
                    )
                    return result
                if action == "discuss":
                    turn_id = str(args[0])
                    service.storage.start_discussion_turn(turn_id, task_id)
                    result = service.answer_discussion_turn(turn_id)
                    service.storage.add_task_event(
                        task_id,
                        "done",
                        {
                            "phase": "done",
                            "next": f"/discussions/{result['session_id']}",
                        },
                    )
                    return result
                raise ValueError(f"Unknown task action: {action}")
            except Exception as exc:
                if action == "discuss":
                    with suppress(ValueError):
                        service.storage.fail_discussion_turn(
                            str(args[0]), str(exc), "cancelled" if event.is_set() else "failed"
                        )
                service.storage.add_task_event(
                    task_id, "error", {"phase": "error", "message": str(exc), "status": "failed"}
                )
                raise

        pool = self.queries if kind == "query" else self.mutations
        with self.task_lock:
            self.kinds[task_id] = kind
            self.futures[task_id] = pool.submit(work)
            self.futures[task_id].add_done_callback(lambda _future: self.wake_queue())
        return task_id

    def start(self) -> None:
        if self.queue_thread is not None:
            return
        self.queue_thread = threading.Thread(
            target=self._queue_loop,
            name="memorex-packet-queue",
            daemon=True,
        )
        self.queue_thread.start()
        self.wake_queue()

    def shutdown(self) -> None:
        self.shutdown_event.set()
        self.queue_wake.set()
        with self.task_lock:
            for event in self.cancel.values():
                event.set()
        if self.queue_thread is not None:
            self.queue_thread.join(timeout=2)
        self.mutations.shutdown(wait=False, cancel_futures=True)
        self.queries.shutdown(wait=False, cancel_futures=True)

    def wake_queue(self) -> None:
        self.queue_wake.set()

    def _queue_loop(self) -> None:
        storage = WikiFirstService(self.settings, runner_resolver=self.resolver).storage
        while not self.shutdown_event.is_set():
            self.queue_wake.wait(timeout=0.5)
            self.queue_wake.clear()
            if self.shutdown_event.is_set():
                continue
            try:
                with self.task_lock:
                    if self.queue_paused.is_set() or self.mutation_busy():
                        continue
                    queued = storage.claim_next_packet()
                    if queued is not None:
                        self.submit("mutation", "packet_claimed", str(queued["packet_id"]))
            except (OSError, sqlite3.Error, ValueError):
                continue

    def mutation_busy(self) -> bool:
        with self.task_lock:
            return any(
                self.kinds.get(task_id) != "query" and not future.done()
                for task_id, future in self.futures.items()
            )

    def pause_for_maintenance(self) -> bool:
        with self.task_lock:
            self.queue_paused.set()
            if any(not future.done() for future in self.futures.values()):
                self.queue_paused.clear()
                return False
            return True

    def resume_after_maintenance(self) -> None:
        self.queue_paused.clear()
        self.wake_queue()

    def stop(self, task_id: str) -> bool:
        with self.task_lock:
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
    state: dict[str, Any] = {
        "root": None,
        "settings": None,
        "service": None,
        "tasks": None,
        "lifespan_active": False,
    }

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        state["lifespan_active"] = True
        tasks: WorkspaceTasks | None = state.get("tasks")
        if tasks:
            tasks.start()
        try:
            yield
        finally:
            state["lifespan_active"] = False
            tasks = state.get("tasks")
            if tasks:
                tasks.shutdown()

    app = FastAPI(title="Memorex Wiki", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    templates = Jinja2Templates(directory=TEMPLATES)

    def select(path: Path, create: bool = False, name: str | None = None) -> None:
        previous: WorkspaceTasks | None = state.get("tasks")
        if previous:
            previous.shutdown()
        resolved = path.expanduser().resolve()
        settings = (
            WorkspaceSettings.create(resolved, name or resolved.name)
            if create
            else WorkspaceSettings.load(resolved)
        )
        service = WikiFirstService(settings, runner_resolver=runner_resolver)
        service.initialize()
        service.storage.recover_interrupted_jobs()
        service.storage.recover_interrupted_discussions()
        preferences.save(resolved)
        tasks = WorkspaceTasks(settings, runner_resolver)
        state.update(
            root=resolved,
            settings=settings,
            service=service,
            tasks=tasks,
        )
        if state["lifespan_active"]:
            tasks.start()

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
                notes=svc.storage.notes(),
                notebooks=svc.storage.notebooks(),
                jobs=svc.storage.jobs(include_packets=False),
                history=svc.history(),
                chats=svc.storage.chat_sessions(),
                proposal=svc.storage.latest_proposal(),
                proposals=svc.storage.proposals(),
            )
        data.update(extra)
        return data

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> HTMLResponse:
        if state["service"] is None:
            return templates.TemplateResponse(
                request=request, name="app.html", context=context(request, "wiki")
            )
        notebook_id = request.query_params.get("notebook")
        query = request.query_params.get("q", "").strip()
        notes = (
            service().search_notes(query) if query else service().storage.notes(notebook_id or None)
        )
        return templates.TemplateResponse(
            request=request,
            name="app.html",
            context=context(
                request,
                "wiki",
                notes=notes,
                selected_notebook=notebook_id,
                search_query=query,
            ),
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

    @app.get("/transfer", response_class=HTMLResponse)
    async def transfer(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="app.html",
            context=context(
                request,
                "transfer",
                restored=request.query_params.get("restored"),
                safety_backup=request.query_params.get("safety_backup"),
            ),
        )

    @app.post("/workspace/backup")
    async def backup_workspace() -> Response:
        settings: WorkspaceSettings = state["settings"]
        if settings is None:
            raise HTTPException(409, "Choose a workspace first")
        tasks: WorkspaceTasks = state["tasks"]
        if not tasks.pause_for_maintenance():
            raise HTTPException(409, "Wait for the current analysis or query to finish")
        handle, temporary_name = tempfile.mkstemp(prefix="memorex-backup-", suffix=".zip")
        os.close(handle)
        temporary = Path(temporary_name)
        temporary.unlink()
        try:
            create_workspace_archive(settings.root, temporary)
        except (OSError, sqlite3.Error, WorkspaceArchiveError) as exc:
            temporary.unlink(missing_ok=True)
            raise HTTPException(422, str(exc)) from exc
        finally:
            tasks.resume_after_maintenance()
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", settings.root.name).strip("-")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"{safe_name or 'memorex'}-{timestamp}.memorex.zip"
        try:
            content = temporary.read_bytes()
        finally:
            temporary.unlink(missing_ok=True)
        return Response(
            content=content,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/workspace/restore")
    async def restore_workspace(
        path: Annotated[str, Form()], archive: Annotated[UploadFile, File()]
    ) -> RedirectResponse:
        handle, temporary_name = tempfile.mkstemp(prefix="memorex-restore-", suffix=".zip")
        old_tasks: WorkspaceTasks | None = state.get("tasks")
        paused = False
        switched = False
        try:
            with os.fdopen(handle, "wb") as output:
                while chunk := await archive.read(1024 * 1024):
                    output.write(chunk)
            if old_tasks is not None:
                paused = old_tasks.pause_for_maintenance()
                if not paused:
                    raise HTTPException(409, "Wait for the current analysis or query to finish")
            result = restore_workspace_archive(Path(temporary_name), Path(path))
            select(result.root)
            switched = True
        except HTTPException:
            raise
        except (OSError, sqlite3.Error, WorkspaceArchiveError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            await archive.close()
            Path(temporary_name).unlink(missing_ok=True)
            if paused and not switched and old_tasks is not None:
                old_tasks.resume_after_maintenance()
        parameters = {"restored": "1"}
        if result.safety_backup is not None:
            parameters["safety_backup"] = str(result.safety_backup)
        return RedirectResponse(f"/transfer?{urlencode(parameters)}", status_code=303)

    @app.get("/wiki/{slug}", response_class=HTMLResponse)
    async def wiki_page(request: Request, slug: str) -> Response:
        if not re.fullmatch(r"(?:README|[a-z0-9]+(?:-[a-z0-9]+)*)", slug):
            raise HTTPException(404)
        svc = service()
        pages = {p["slug"]: p for p in svc.storage.list_pages()}
        page = pages.get(slug)
        if page is None:
            raise HTTPException(404)
        if slug != "README":
            try:
                note = svc.storage.note_for_slug(slug)
            except ValueError:
                note = None
            if note:
                return RedirectResponse(f"/notes/{note['id']}", status_code=302)
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

    @app.get("/notes/new", response_class=HTMLResponse)
    async def new_note(request: Request, message: int | None = None) -> HTMLResponse:
        title, body = "", ""
        if message is not None:
            try:
                saved_message = service().storage.chat_message(message)
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from exc
            if saved_message["role"] != "assistant":
                raise HTTPException(422, "Only an AI answer can be saved as a note")
            discussion = service().storage.discussion(str(saved_message["session_id"]))
            title, body = str(discussion["title"]), str(saved_message["content"])
        inbox = next(
            notebook
            for notebook in service().storage.notebooks()
            if notebook["system_key"] == "inbox"
        )
        return templates.TemplateResponse(
            request=request,
            name="app.html",
            context=context(
                request,
                "note-edit",
                note=None,
                note_title=title,
                note_body=body,
                note_sources="",
                notebook_id=inbox["id"],
                expected_snapshot_id=service().storage.active_snapshot()["id"],
            ),
        )

    @app.post("/notes")
    async def create_note(
        title: Annotated[str, Form()],
        body: Annotated[str, Form()] = "",
        notebook_id: Annotated[str, Form()] = "",
        attachments: Annotated[list[UploadFile] | None, File()] = None,
    ) -> RedirectResponse:
        uploads = await _read_note_uploads(attachments or [])
        try:
            note = service().create_note(title, body, notebook_id)
            for name, mime_type, data in uploads:
                service().storage.add_note_attachment(
                    str(note["id"]), name=name, mime_type=mime_type, data=data
                )
        except (OSError, ValueError, WikiFirstError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return RedirectResponse(f"/notes/{note['id']}", status_code=303)

    @app.get("/notes/{note_id}", response_class=HTMLResponse)
    async def note_page(request: Request, note_id: str) -> HTMLResponse:
        try:
            note = service().storage.note(note_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        title, body, sources = split_note_markdown(str(note["text"]))
        pages = {p["slug"]: p for p in service().storage.list_pages()}
        backlinks = [
            p for p in pages.values() if str(note["slug"]) in WIKI_LINK.findall(str(p["text"]))
        ]
        return templates.TemplateResponse(
            request=request,
            name="app.html",
            context=context(
                request,
                "note",
                note={
                    **note,
                    "title": title,
                    "body_html": render_markdown(body),
                    "sources_html": render_markdown(sources) if sources else "",
                },
                backlinks=backlinks,
            ),
        )

    @app.get("/notes/{note_id}/edit", response_class=HTMLResponse)
    async def edit_note_page(request: Request, note_id: str) -> HTMLResponse:
        try:
            note = service().storage.note(note_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        title, body, sources = split_note_markdown(str(note["text"]))
        return templates.TemplateResponse(
            request=request,
            name="app.html",
            context=context(
                request,
                "note-edit",
                note=note,
                note_title=title,
                note_body=body,
                note_sources=sources,
                notebook_id=note["notebook_id"],
                expected_snapshot_id=service().storage.active_snapshot()["id"],
            ),
        )

    @app.post("/notes/{note_id}")
    async def save_note(
        note_id: str,
        title: Annotated[str, Form()],
        body: Annotated[str, Form()] = "",
        notebook_id: Annotated[str, Form()] = "",
        expected_snapshot_id: Annotated[str, Form()] = "",
        attachments: Annotated[list[UploadFile] | None, File()] = None,
    ) -> RedirectResponse:
        uploads = await _read_note_uploads(attachments or [])
        try:
            service().edit_note(
                note_id,
                title=title,
                body=body,
                notebook_id=notebook_id,
                expected_snapshot_id=expected_snapshot_id,
            )
            for name, mime_type, data in uploads:
                service().storage.add_note_attachment(
                    note_id, name=name, mime_type=mime_type, data=data
                )
        except (OSError, ValueError, WikiFirstError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return RedirectResponse(f"/notes/{note_id}", status_code=303)

    @app.post("/notes/{note_id}/attachments/{attachment_id}/remove")
    async def remove_note_attachment(note_id: str, attachment_id: str) -> RedirectResponse:
        try:
            service().storage.remove_note_attachment(note_id, attachment_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return RedirectResponse(f"/notes/{note_id}/edit", status_code=303)

    @app.get("/notes/{note_id}/attachments/{attachment_id}")
    async def note_attachment(note_id: str, attachment_id: str) -> Response:
        try:
            attachment = service().storage.note_attachment(note_id, attachment_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        if attachment["removed_at"]:
            raise HTTPException(404)
        target = service().storage.root / str(attachment["object_path"])
        disposition = (
            "inline" if str(attachment["mime_type"]).startswith("image/") else "attachment"
        )
        encoded_name = quote(str(attachment["display_name"]))
        return Response(
            target.read_bytes(),
            media_type=str(attachment["mime_type"]),
            headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{encoded_name}"},
        )

    @app.post("/notebooks")
    async def create_notebook(name: Annotated[str, Form()]) -> RedirectResponse:
        try:
            notebook = service().storage.create_notebook(name)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return RedirectResponse(f"/?notebook={notebook['id']}", status_code=303)

    @app.post("/notebooks/{notebook_id}/rename")
    async def rename_notebook(notebook_id: str, name: Annotated[str, Form()]) -> RedirectResponse:
        try:
            service().storage.rename_notebook(notebook_id, name)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return RedirectResponse(f"/?notebook={notebook_id}", status_code=303)

    @app.post("/notebooks/{notebook_id}/delete")
    async def delete_notebook(notebook_id: str) -> RedirectResponse:
        try:
            service().storage.delete_notebook(notebook_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return RedirectResponse("/", status_code=303)

    @app.post("/markdown/preview")
    async def markdown_preview(body: Annotated[str, Form()] = "") -> HTMLResponse:
        return HTMLResponse(render_markdown(body))

    @app.get("/inbox", response_class=HTMLResponse)
    async def inbox(request: Request) -> HTMLResponse:
        svc = service()
        scanned = svc.scan()
        packets = [_decorate_packet(packet) for packet in svc.storage.packets()]
        return templates.TemplateResponse(
            request=request,
            name="app.html",
            context=context(
                request,
                "inbox",
                scanned=scanned,
                packets=packets,
                saved=request.query_params.get("saved"),
            ),
        )

    @app.post("/packets")
    async def create_packet(
        user_note: Annotated[str, Form()] = "",
        urls: Annotated[str, Form()] = "",
        file_options: Annotated[str, Form()] = "",
        files: Annotated[list[UploadFile] | None, File()] = None,
    ) -> RedirectResponse:
        try:
            options = json.loads(file_options) if file_options else []
        except json.JSONDecodeError as exc:
            raise HTTPException(422, "Invalid attachment options") from exc
        if not isinstance(options, list):
            raise HTTPException(422, "Invalid attachment options")
        uploads: list[PacketUpload] = []
        for index, upload in enumerate(files or []):
            name = upload.filename or ""
            if not name:
                continue
            suffix = Path(name).suffix.lower()
            if (
                name != Path(name).name
                or "\\" in name
                or suffix not in {".md", ".txt"} | IMAGE_SUFFIXES
            ):
                raise HTTPException(
                    415, "Only safe TXT, Markdown, PNG, JPEG, and WebP filenames are accepted"
                )
            data = await upload.read(MAX_UPLOAD + 1)
            if len(data) > MAX_UPLOAD:
                raise HTTPException(413, f"{name} is larger than 10 MiB")
            if suffix in {".md", ".txt"}:
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise HTTPException(415, f"{name} must be UTF-8") from exc
            raw_option = options[index] if index < len(options) else {}
            if not isinstance(raw_option, dict):
                raise HTTPException(422, "Invalid attachment options")
            mode = str(raw_option.get("mode") or "analyze")
            instruction = str(raw_option.get("instruction") or "")
            if suffix in {".md", ".txt"}:
                mode, instruction = "analyze", ""
            if mode not in {"analyze", "store"}:
                raise HTTPException(422, f"Invalid processing mode for {name}")
            uploads.append(PacketUpload(name, upload.content_type, data, mode, instruction))
        packet = service().create_packet(
            user_note=user_note,
            files=uploads,
            urls=[line for line in urls.splitlines() if line.strip()],
        )
        state["tasks"].wake_queue()
        return RedirectResponse(f"/inbox?saved={packet['id']}", status_code=303)

    @app.post("/packets/{packet_id}/process")
    async def process_packet(packet_id: str) -> RedirectResponse:
        try:
            packet = service().storage.packet(packet_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        if not packet["processable_count"]:
            raise HTTPException(409, "Packet has no text sources awaiting processing")
        service().queue_packet(packet_id)
        state["tasks"].wake_queue()
        return RedirectResponse("/inbox", status_code=303)

    @app.post("/upload")
    async def upload(files: Annotated[list[UploadFile], File()]) -> RedirectResponse:
        settings: WorkspaceSettings = state["settings"]
        for upload in files:
            name = Path(upload.filename or "").name
            if (
                name != upload.filename
                or "\\" in name
                or Path(name).suffix.lower() not in {".md", ".txt"}
            ):
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
    async def review(request: Request, job: str | None = None) -> HTMLResponse:
        proposals = service().storage.proposals()
        proposal = next((item for item in proposals if str(item["id"]) == job), None)
        if job and proposal is None:
            raise HTTPException(404, "Proposal is not awaiting review")
        proposal = proposal or (proposals[0] if proposals else None)
        details = service().review(str(proposal["job_id"])) if proposal else None
        if details:
            details["report_html"] = render_markdown(
                str(details["report"]), source_job=str(details["job_id"])
            )
            details["rendered"] = _render_changed(service(), details)
        return templates.TemplateResponse(
            request=request,
            name="app.html",
            context=context(request, "review", review=details, proposals=proposals),
        )

    @app.post("/review/apply")
    async def apply() -> RedirectResponse:
        proposal = service().storage.latest_proposal()
        if not proposal:
            raise HTTPException(409, "No proposal awaiting review")
        return _apply_review(str(proposal["job_id"]))

    @app.post("/review/{job_id}/apply")
    async def apply_job(job_id: str) -> RedirectResponse:
        return _apply_review(job_id)

    def _apply_review(job_id: str) -> RedirectResponse:
        service().apply(job_id)
        state["tasks"].wake_queue()
        return RedirectResponse("/review", status_code=303)

    @app.post("/review/reject")
    async def reject(reason: Annotated[str, Form()] = "rejected in Web UI") -> RedirectResponse:
        proposal = service().storage.latest_proposal()
        if not proposal:
            raise HTTPException(409, "No proposal awaiting review")
        return _reject_review(str(proposal["job_id"]), reason)

    @app.post("/review/{job_id}/reject")
    async def reject_job(
        job_id: str, reason: Annotated[str, Form()] = "rejected in Web UI"
    ) -> RedirectResponse:
        return _reject_review(job_id, reason)

    def _reject_review(job_id: str, reason: str) -> RedirectResponse:
        service().reject(job_id, reason)
        state["tasks"].wake_queue()
        return RedirectResponse("/review", status_code=303)

    @app.post("/review/revise")
    async def revise(feedback: Annotated[str, Form()]) -> RedirectResponse:
        proposal = service().storage.latest_proposal()
        if not proposal:
            raise HTTPException(409, "No proposal awaiting review")
        return _revise_review(str(proposal["job_id"]), feedback)

    @app.post("/review/{job_id}/revise")
    async def revise_job(job_id: str, feedback: Annotated[str, Form()]) -> RedirectResponse:
        return _revise_review(job_id, feedback)

    @app.post("/review/{job_id}/pages/{page_name}/edit")
    async def edit_proposal_page(
        job_id: str, page_name: str, content: Annotated[str, Form()]
    ) -> RedirectResponse:
        try:
            service().edit_proposal_page(job_id, page_name, content)
        except WikiFirstError as exc:
            raise HTTPException(422, str(exc)) from exc
        return RedirectResponse(f"/review?job={job_id}", status_code=303)

    def _revise_review(job_id: str, feedback: str) -> RedirectResponse:
        task = state["tasks"].submit("mutation", "revise", job_id, feedback)
        return RedirectResponse(f"/tasks/{task}", status_code=303)

    @app.post("/tasks/ingest")
    async def ingest() -> RedirectResponse:
        task = state["tasks"].submit("mutation", "ingest")
        return RedirectResponse(f"/tasks/{task}", status_code=303)

    @app.post("/tasks/retry/{job_id}")
    async def retry(job_id: str) -> RedirectResponse:
        try:
            job = service().storage.get_job(job_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        if job.get("packet_id"):
            service().queue_packet(str(job["packet_id"]))
            state["tasks"].wake_queue()
            return RedirectResponse("/inbox", status_code=303)
        task = state["tasks"].submit("mutation", "retry", job_id)
        return RedirectResponse(f"/tasks/{task}", status_code=303)

    @app.get("/api/packets")
    async def packet_statuses() -> JSONResponse:
        packets = [_decorate_packet(packet) for packet in service().storage.packets()]
        return JSONResponse(
            [
                {
                    "id": packet["id"],
                    "state": packet["state"],
                    "state_label": packet["state_label"],
                    "progress": packet["progress"],
                    "last_error": packet["error_summary"],
                    "attempt_count": len(packet["attempts"]),
                }
                for packet in packets
            ]
        )

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    async def task_page(request: Request, task_id: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="app.html",
            context=context(
                request, "task", task_id=task_id, next_url=request.query_params.get("next") or "/"
            ),
        )

    @app.get("/tasks/{task_id}/events")
    async def events(task_id: str) -> StreamingResponse:
        async def stream():
            after = 0
            loop = asyncio.get_running_loop()
            started = loop.time()
            while loop.time() - started < 1800:
                rows = service().storage.task_events(task_id, after)
                for row in rows:
                    after = int(row["id"])
                    yield f"id: {after}\ndata: {json.dumps(row['payload'], ensure_ascii=False)}\n\n"
                    if row["phase"] in {"review-ready", "done", "error", "cancelled"}:
                        return
                future = state["tasks"].futures.get(task_id)
                if future and future.done() and not rows:
                    yield 'data: {"phase":"done"}\n\n'
                    return
                await asyncio.sleep(0.25)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/tasks/{task_id}/stop")
    async def stop(task_id: str) -> JSONResponse:
        if not state["tasks"].stop(task_id):
            raise HTTPException(404)
        return JSONResponse({"status": "stopping"})

    @app.get("/discussions", response_class=HTMLResponse)
    async def discussions(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="app.html",
            context=context(request, "discussions"),
        )

    @app.get("/discussions/new", response_class=HTMLResponse)
    async def new_discussion(
        request: Request, note: str | None = None, q: str = ""
    ) -> HTMLResponse:
        selected: list[str] = []
        title = q.strip()
        if note:
            try:
                anchored = service().storage.note(note)
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from exc
            selected = [str(anchored["id"])]
            title = title or f"Обсуждение: {anchored['title']}"
        candidates = service().search_notes(q) if q.strip() else service().storage.notes()
        if note and all(str(item["id"]) != note for item in candidates):
            candidates.insert(0, anchored)
        return templates.TemplateResponse(
            request=request,
            name="app.html",
            context=context(
                request,
                "discussion-new",
                candidates=candidates,
                selected_note_ids=selected,
                discussion_title=title,
                discussion_query=q,
            ),
        )

    @app.post("/discussions")
    async def create_discussion(
        title: Annotated[str, Form()] = "",
        question: Annotated[str, Form()] = "",
        note_ids: Annotated[list[str] | None, Form()] = None,
    ) -> RedirectResponse:
        try:
            discussion = service().storage.create_discussion(
                title or question[:120], note_ids or []
            )
            if question.strip():
                turn = service().storage.create_discussion_turn(
                    str(discussion["id"]), question.strip()
                )
                task = state["tasks"].submit("query", "discuss", str(turn["id"]))
                return RedirectResponse(
                    f"/tasks/{task}?next=/discussions/{discussion['id']}", status_code=303
                )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return RedirectResponse(f"/discussions/{discussion['id']}", status_code=303)

    @app.get("/discussions/{session_id}", response_class=HTMLResponse)
    async def discussion_page(request: Request, session_id: str) -> HTMLResponse:
        try:
            discussion = service().storage.discussion(session_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        messages = service().storage.chat_messages(session_id)
        for message in messages:
            if message["role"] == "assistant":
                message["html"] = render_markdown(str(message["content"]))
        selected = {str(note["id"]) for note in discussion["notes"]}
        candidates = [note for note in service().storage.notes() if str(note["id"]) not in selected]
        return templates.TemplateResponse(
            request=request,
            name="app.html",
            context=context(
                request,
                "discussion",
                discussion=discussion,
                messages=messages,
                candidates=candidates,
            ),
        )

    @app.post("/discussions/{session_id}/context")
    async def update_discussion_context(
        session_id: str, note_ids: Annotated[list[str] | None, Form()] = None
    ) -> RedirectResponse:
        try:
            service().storage.set_discussion_notes(session_id, note_ids or [])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return RedirectResponse(f"/discussions/{session_id}", status_code=303)

    @app.post("/discussions/{session_id}/messages")
    async def discuss(session_id: str, question: Annotated[str, Form()]) -> RedirectResponse:
        try:
            turn = service().storage.create_discussion_turn(session_id, question.strip())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        task = state["tasks"].submit("query", "discuss", str(turn["id"]))
        return RedirectResponse(f"/tasks/{task}?next=/discussions/{session_id}", status_code=303)

    @app.post("/discussions/{session_id}/turns/{turn_id}/retry")
    async def retry_discussion_turn(session_id: str, turn_id: str) -> RedirectResponse:
        try:
            turn = service().storage.discussion_turn(turn_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        if str(turn["session_id"]) != session_id or turn["status"] != "failed":
            raise HTTPException(409, "Discussion turn is not retryable")
        task = state["tasks"].submit("query", "discuss", turn_id)
        return RedirectResponse(f"/tasks/{task}?next=/discussions/{session_id}", status_code=303)

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
        request: Request,
        name: str,
        start: int = 1,
        end: int | None = None,
        job: str | None = None,
    ) -> Response:
        if name != Path(name).name:
            raise HTTPException(404)
        if job:
            try:
                proposal = service().storage.proposal(job)
            except ValueError as exc:
                raise HTTPException(404) from exc
            if proposal["status"] != "proposed":
                raise HTTPException(404)
            root_path = service().storage.root / str(proposal["relative_path"]) / "sources"
        else:
            root_path = (
                service().storage.snapshot_path(service().storage.active_snapshot()) / "sources"
            )
        target = (root_path / name).resolve()
        try:
            target.relative_to(root_path.resolve())
        except ValueError as exc:
            raise HTTPException(404) from exc
        if not target.is_file():
            raise HTTPException(404)
        if target.suffix.lower() in IMAGE_SUFFIXES:
            return Response(
                content=target.read_bytes(),
                media_type=_image_media_type(target.suffix),
            )
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

    @app.get("/packets/{packet_id}/items/{item_id}")
    async def packet_item(packet_id: str, item_id: str) -> Response:
        try:
            item = service().storage.packet_item(packet_id, item_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        if item["source_kind"] != "image" or not item["object_path"]:
            raise HTTPException(404)
        target = service().storage.root / str(item["object_path"])
        if not target.is_file():
            raise HTTPException(404)
        return Response(
            content=target.read_bytes(),
            media_type=str(item["mime_type"]),
        )

    @app.exception_handler(WikiFirstError)
    async def wiki_error(_request: Request, exc: WikiFirstError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    return app


async def _read_note_uploads(
    uploads: list[UploadFile],
) -> list[tuple[str, str, bytes]]:
    result = []
    for upload in uploads:
        name = upload.filename or ""
        if not name:
            continue
        if name != Path(name).name or "\\" in name or "\0" in name:
            raise HTTPException(415, f"Unsafe attachment filename: {name}")
        data = await upload.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, f"{name} is larger than 10 MiB")
        mime_type = (
            upload.content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        )
        result.append((name, mime_type, data))
    return result


def _decorate_packet(packet: dict[str, Any]) -> dict[str, Any]:
    result = {**packet}
    result["items"] = [
        {
            **item,
            "is_image": str(item.get("mime_type") or "").startswith("image/"),
            "content_url": f"/packets/{packet['id']}/items/{item['id']}",
            "mode_label": (
                "ожидает импортера"
                if item.get("kind") == "url"
                else ("только хранение" if item.get("processing_mode") == "store" else "анализ")
            ),
        }
        for item in packet.get("items", [])
    ]
    queue = packet.get("queue") or {}
    latest = packet.get("latest_job") or {}
    raw_error = str(queue.get("last_error") or latest.get("rejection_reason") or "")
    result["state_label"] = PACKET_STATE_LABELS.get(str(packet["state"]), str(packet["state"]))
    result["progress"] = _packet_progress(packet)
    result["error_summary"] = _friendly_packet_error(raw_error)
    result["raw_error"] = raw_error
    result["attempts"] = [
        {
            **attempt,
            "error_summary": _friendly_packet_error(str(attempt.get("rejection_reason") or "")),
        }
        for attempt in packet.get("attempts", [])
    ]
    return result


def _packet_progress(packet: dict[str, Any]) -> str:
    state = str(packet["state"])
    queue = packet.get("queue") or {}
    if state == "queued":
        return "Ожидает свободного места в очереди"
    if state == "retry_wait":
        remaining = _seconds_until(queue.get("available_at"))
        return (
            f"Автоматическая повторная попытка через {_format_duration(remaining)}"
            if remaining > 0
            else "Готовим автоматическую повторную попытку"
        )
    if state != "processing":
        return ""

    latest = packet.get("latest_job") or {}
    event = packet.get("latest_event") or {}
    payload = event.get("payload") or {}
    phase = str(event.get("phase") or "")
    runner = str(payload.get("runner") or latest.get("runner") or "").capitalize()
    if phase == "retrieval":
        message = "Подбираем связанные страницы Wiki"
    elif phase == "runner":
        message = f"{runner} запускается" if runner else "Запускаем модель"
    elif phase == "model-started":
        message = f"{runner} начал анализ" if runner else "Модель начала анализ"
    elif phase == "model-working":
        message = f"{runner} анализирует материалы" if runner else "Модель анализирует материалы"
    elif phase == "model-completed":
        message = (
            f"{runner} закончил анализ; проверяем результат"
            if runner
            else "Модель закончила анализ; проверяем результат"
        )
    elif phase == "fallback":
        fallback = str(payload.get("fallback") or "запасную модель").capitalize()
        message = f"Первый анализ не завершён; пробуем {fallback}"
    else:
        message = "Готовим материалы к анализу"
    started_at = latest.get("created_at") or queue.get("updated_at")
    elapsed = _seconds_since(started_at)
    return f"{message} · прошло {_format_duration(elapsed)}"


def _seconds_since(value: object) -> int:
    parsed = _parse_datetime(value)
    return max(0, int((datetime.now(UTC) - parsed).total_seconds())) if parsed else 0


def _seconds_until(value: object) -> int:
    parsed = _parse_datetime(value)
    return max(0, int((parsed - datetime.now(UTC)).total_seconds())) if parsed else 0


def _parse_datetime(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} с"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} мин {remainder:02d} с"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes:02d} мин"


def _friendly_packet_error(error: str) -> str:
    if not error:
        return ""
    lowered = error.lower()
    if "previous proposal preserved" in lowered:
        return "Корректировка прервалась. Предыдущий результат сохранён и доступен в Review."
    if "revised proposal is invalid" in lowered:
        return "Корректировка не прошла проверку. Предыдущий результат сохранён в Review."
    if "file exists" in lowered and "-merged" in lowered:
        return "Внутренняя ошибка сборки результата. Packet сохранён и его можно повторить."
    if "interrupted" in lowered:
        return "Анализ был прерван. Packet сохранён и вернётся в очередь."
    if "timed out" in lowered or "timeout" in lowered:
        return "Модель не ответила вовремя. Memorex повторит анализ автоматически."
    if any(marker in lowered for marker in ("connection", "network", "transport", "http/request")):
        return "Не удалось связаться с моделью. Memorex повторит анализ автоматически."
    return error


def render_markdown(text: str, *, source_job: str | None = None) -> str:
    escaped = html.escape(text)
    blocks = []
    paragraph = []

    def flush() -> None:
        if paragraph:
            blocks.append("<p>" + _inline(" ".join(paragraph), source_job=source_job) + "</p>")
            paragraph.clear()

    lines = escaped.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if index + 1 < len(lines) and _is_table_separator(lines[index + 1], line):
            flush()
            headers = _table_cells(line)
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(_table_cells(lines[index]))
                index += 1
            head = "".join(f"<th>{_inline(cell, source_job=source_job)}</th>" for cell in headers)
            body = "".join(
                "<tr>"
                + "".join(f"<td>{_inline(cell, source_job=source_job)}</td>" for cell in row)
                + "</tr>"
                for row in rows
            )
            blocks.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
            continue
        if line.startswith("### "):
            flush()
            blocks.append(f"<h3>{_inline(line[4:], source_job=source_job)}</h3>")
        elif line.startswith("## "):
            flush()
            blocks.append(f"<h2>{_inline(line[3:], source_job=source_job)}</h2>")
        elif line.startswith("# "):
            flush()
            blocks.append(f"<h1>{_inline(line[2:], source_job=source_job)}</h1>")
        elif line.startswith("- "):
            flush()
            blocks.append(f"<p class=bullet>• {_inline(line[2:], source_job=source_job)}</p>")
        elif not line.strip():
            flush()
        else:
            paragraph.append(line)
        index += 1
    flush()
    return "\n".join(blocks)


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_table_separator(separator: str, header: str) -> bool:
    if "|" not in header or "|" not in separator:
        return False
    cells = _table_cells(separator)
    return (
        bool(cells)
        and len(cells) == len(_table_cells(header))
        and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)
    )


def _inline(text: str, *, source_job: str | None = None) -> str:
    text = WIKI_LINK.sub(lambda m: f'<a href="/wiki/{m.group(1)}">{m.group(1)}</a>', text)

    def image(match: re.Match[str]) -> str:
        alt, target = match.groups()
        href = _source_href(target, source_job=source_job)
        if href is None:
            return alt
        return f'<img class="wiki-image" src="{href}" alt="{alt}" loading="lazy">'

    text = MD_IMAGE.sub(image, text)

    def link(match: re.Match[str]) -> str:
        label, target = match.groups()
        if (href := _source_href(target, source_job=source_job)) is not None:
            return f'<a href="{href}">{label}</a>'
        return label

    return MD_LINK.sub(link, text)


def _source_href(target: str, *, source_job: str | None) -> str | None:
    if not target.startswith("../sources/"):
        return None
    raw = target.removeprefix("../sources/")
    name, _, fragment = raw.partition("#")
    parameters: dict[str, str] = {}
    found = re.fullmatch(r"L(\d+)(?:-L?(\d+))?", fragment)
    if found:
        parameters.update(start=found.group(1), end=found.group(2) or found.group(1))
    if source_job:
        parameters["job"] = source_job
    query = f"?{html.escape(urlencode(parameters))}" if parameters else ""
    return f"/source/{html.escape(Path(name).name)}{query}"


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
            "html": render_markdown(
                (root / name).read_text(encoding="utf-8"), source_job=str(review["job_id"])
            ),
            "text": (root / name).read_text(encoding="utf-8"),
        }
        for name in review["changed_pages"]
        if (root / name).is_file()
    ]


def _image_media_type(suffix: str) -> str:
    return "image/jpeg" if suffix.lower() in {".jpg", ".jpeg"} else f"image/{suffix[1:].lower()}"


def run_server(
    path: Path | None, *, host: str = "127.0.0.1", port: int = 8766, open_browser: bool = True
) -> None:
    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    uvicorn.run(create_app(path), host=host, port=port)

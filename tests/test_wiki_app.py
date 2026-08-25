from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2

from memorex.config import WorkspaceSettings
from memorex.wiki_app import create_app, render_markdown
from memorex.wiki_first.models import AgentRunner, RunnerResult
from memorex.wiki_first.service import WikiFirstService
from memorex.wiki_first.storage import WikiStorage

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8cfc000000301010018dd8db10000000049454e44ae426082"
)


class RetrievalRunner(AgentRunner):
    name = "fake"
    model = "fake-model"

    def __init__(self) -> None:
        self.visible_pages: list[set[str]] = []

    def run(self, workdir: Path, prompt: str, *, writable: bool) -> RunnerResult:
        pages = {path.name for path in (workdir / "wiki").glob("*.md")}
        self.visible_pages.append(pages)
        if "Write the complete answer" in prompt:
            (workdir / "answer.md").write_text(
                "Ответ из выбранных заметок.\n\n## Путь\n\n- [[alpha-topic]]\n",
                encoding="utf-8",
            )
            return RunnerResult(self.name, self.model, "1", 1, "", "")
        sources = sorted((workdir / "sources").glob("*"))
        newest = sources[-1]
        if len(self.visible_pages) == 1:
            (workdir / "wiki" / "alpha-topic.md").write_text(
                "# Alpha topic\n\nAlpha datum. [S1]\n\n## Источники\n\n"
                f"- [S1] [{newest.name}](../sources/{newest.name}), строка 1.\n",
                encoding="utf-8",
            )
            (workdir / "wiki" / "unrelated-topic.md").write_text(
                "# Unrelated topic\n\nUnrelated datum. [S1]\n\n## Источники\n\n"
                f"- [S1] [{newest.name}](../sources/{newest.name}), строка 1.\n",
                encoding="utf-8",
            )
            (workdir / "wiki" / "README.md").write_text(
                "# Wiki\n\n[[alpha-topic]].\n", encoding="utf-8"
            )
        else:
            page = workdir / "wiki" / "alpha-topic.md"
            page.write_text(
                "# Alpha topic\n\nAlpha updated. [S1]\n\n## Источники\n\n"
                f"- [S1] [{newest.name}](../sources/{newest.name}), строка 1.\n",
                encoding="utf-8",
            )
        (workdir / "proposal-report.md").write_text("# Report\n\nDone.\n", encoding="utf-8")
        return RunnerResult(self.name, self.model, "1", 1, "", "")


def test_web_setup_upload_last_workspace_and_safe_markdown(tmp_path: Path) -> None:
    preferences = tmp_path / "preferences.json"

    async def exercise() -> None:
        transport = httpx2.ASGITransport(app=create_app(None, user_settings_path=preferences))
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            assert "Создайте новый workspace" in (await client.get("/")).text
            created = await client.post(
                "/workspace",
                data={"path": str(tmp_path / "knowledge"), "action": "create", "name": "Test"},
            )
            assert created.status_code == 303
            uploaded = await client.post(
                "/upload", files={"files": ("note.md", b"# Note\n", "text/markdown")}
            )
            assert uploaded.status_code == 303
            packet = await client.post("/packets", data={"urls": "https://example.com/article"})
            assert packet.status_code == 303
            inbox = (await client.get("/inbox")).text
            assert "https://example.com/article" in inbox
            assert "waiting_importer" in inbox
            invalid = await client.post("/packets", data={"urls": "file:///etc/passwd"})
            assert invalid.status_code == 422
            empty = await client.post("/packets", data={"user_note": "", "urls": ""})
            assert empty.status_code == 422
        remembered = create_app(None, user_settings_path=preferences)
        second = httpx2.ASGITransport(app=remembered)
        async with httpx2.AsyncClient(transport=second, base_url="http://test") as client:
            assert "Новая заметка" in (await client.get("/")).text

    asyncio.run(exercise())
    rendered = render_markdown("# Safe\n\n<script>alert(1)</script> [[topic]]")
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert 'href="/wiki/topic"' in rendered
    table = render_markdown("| Дата | Показание |\n| --- | ---: |\n| Сегодня | 123 [S1] |")
    assert "<table>" in table
    assert "<th>Дата</th>" in table
    assert "<td>123 [S1]</td>" in table


def test_web_saves_previews_and_serves_stored_image_without_analysis(tmp_path: Path) -> None:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Image Web")
    app = create_app(settings.root, user_settings_path=tmp_path / "preferences.json")

    async def exercise() -> None:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/packets",
                data={
                    "file_options": '[{"mode":"store","instruction":""}]',
                },
                files={"files": ("photo.png", PNG, "image/png")},
            )
            assert response.status_code == 303
            packet_id = response.headers["location"].split("saved=", 1)[1]
            inbox = await client.get("/inbox")
            assert "Сохранён без анализа" in inbox.text
            assert "Анализировать все" in inbox.text
            assert 'id="attachment-gallery"' in inbox.text
            assert f"/packets/{packet_id}/items/" in inbox.text

            packet = WikiStorage(settings).packet(packet_id)
            item_id = str(packet["items"][0]["id"])
            image = await client.get(f"/packets/{packet_id}/items/{item_id}")
            assert image.status_code == 200
            assert image.headers["content-type"] == "image/png"
            assert image.content == PNG

            invalid = await client.post(
                "/packets",
                files={"files": ("broken.png", b"not an image", "image/png")},
            )
            assert invalid.status_code == 422
            assert len(WikiStorage(settings).packets()) == 1

    asyncio.run(exercise())
    rendered = render_markdown("![Скан](../sources/r1-photo.png)")
    assert '<img class="wiki-image"' in rendered
    assert 'src="/source/r1-photo.png"' in rendered


def test_web_downloads_and_restores_full_workspace(tmp_path: Path) -> None:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Portable Web")
    service = WikiFirstService(settings, runner_resolver=lambda _name: RetrievalRunner())
    original = service.create_packet(
        user_note="Запись из скачанной копии.", files=[], urls=["https://example.com/original"]
    )
    app = create_app(
        settings.root,
        runner_resolver=lambda _name: RetrievalRunner(),
        user_settings_path=tmp_path / "preferences.json",
    )

    async def exercise() -> None:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            transfer = await client.get("/transfer")
            assert "Скачать полную копию" in transfer.text
            downloaded = await client.post("/workspace/backup")
            assert downloaded.status_code == 200
            assert downloaded.headers["content-type"] == "application/zip"
            assert ".memorex.zip" in downloaded.headers["content-disposition"]

            added = await client.post(
                "/packets",
                data={"urls": "https://example.com/added-after-backup"},
            )
            assert added.status_code == 303
            assert len(WikiStorage(settings).packets()) == 2

            restored = await client.post(
                "/workspace/restore",
                data={"path": str(settings.root)},
                files={"archive": ("portable.memorex.zip", downloaded.content, "application/zip")},
            )
            assert restored.status_code == 303
            receipt = await client.get(restored.headers["location"])
            assert "Полная копия восстановлена" in receipt.text
            assert ".memorex-backups" in receipt.text

    asyncio.run(exercise())
    packets = WikiStorage(WorkspaceSettings.load(settings.root)).packets()
    assert [packet["id"] for packet in packets] == [original["id"]]
    assert list((tmp_path / ".memorex-backups").glob("*.memorex.zip"))


def test_web_exposes_memory_question_and_literal_review_editor(tmp_path: Path) -> None:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Editable Web")
    runner = RetrievalRunner()
    service = WikiFirstService(settings, runner_resolver=lambda _name: runner)
    packet = service.create_packet(user_note="Посмотреть фильм.", files=[], urls=[])
    proposed = service.ingest_packet(str(packet["id"]))
    job_id = str(proposed["job_id"])
    proposal = service.storage.proposal(job_id)
    stage = service.storage.root / str(proposal["relative_path"])
    page = stage / "wiki" / "alpha-topic.md"
    edited = page.read_text(encoding="utf-8").replace("Alpha datum.", "- Один фильм.")
    app = create_app(
        settings.root,
        runner_resolver=lambda _name: runner,
        user_settings_path=tmp_path / "preferences.json",
    )

    async def exercise() -> None:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            home = (await client.get("/")).text
            assert "Найти в заголовках и тексте" in home
            review = (await client.get(f"/review?job={job_id}")).text
            assert "Редактировать Markdown вручную" in review
            assert "Переделать моделью" in review
            saved = await client.post(
                f"/review/{job_id}/pages/alpha-topic.md/edit", data={"content": edited}
            )
            assert saved.status_code == 303

    asyncio.run(exercise())
    assert WikiStorage(settings).proposal(job_id)["revision_no"] == 2
    assert len(runner.visible_pages) == 1


def test_web_creates_edits_searches_and_discusses_notes(tmp_path: Path) -> None:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Notes Web")
    runner = RetrievalRunner()
    app = create_app(
        settings.root,
        runner_resolver=lambda _name: runner,
        user_settings_path=tmp_path / "preferences.json",
    )

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
                storage = WikiStorage(settings)
                inbox = next(x for x in storage.notebooks() if x["system_key"] == "inbox")
                created = await client.post(
                    "/notes",
                    data={
                        "title": "Локальная заметка",
                        "body": "Текст без модели.",
                        "notebook_id": inbox["id"],
                    },
                    files={"attachments": ("image.png", PNG, "image/png")},
                )
                assert created.status_code == 303
                note_url = created.headers["location"]
                note_id = note_url.rsplit("/", 1)[1]
                page = (await client.get(note_url)).text
                assert "Локальная заметка" in page
                assert "image.png" in page
                editor = (await client.get(f"{note_url}/edit")).text
                assert "Текст Markdown" in editor
                snapshot = storage.active_snapshot()["id"]
                saved = await client.post(
                    note_url,
                    data={
                        "title": "Исправленная заметка",
                        "body": "Точное содержимое.",
                        "notebook_id": inbox["id"],
                        "expected_snapshot_id": snapshot,
                    },
                )
                assert saved.status_code == 303
                found = (await client.get("/", params={"q": "Точное содержимое"})).text
                assert "Исправленная заметка" in found

                discussion = await client.post(
                    "/discussions",
                    data={"title": "Проверка", "note_ids": note_id},
                )
                session_url = discussion.headers["location"]
                submitted = await client.post(
                    f"{session_url}/messages", data={"question": "Что здесь записано?"}
                )
                assert submitted.status_code == 303
                task_url = submitted.headers["location"]
                task_id = task_url.split("/tasks/", 1)[1].split("?", 1)[0]
                events = await client.get(f"/tasks/{task_id}/events")
                assert '"phase": "done"' in events.text
                assert session_url in events.text
                result = (await client.get(session_url)).text
                assert "Ответ из выбранных заметок" in result
                assert "Сохранить ответ как заметку" in result

    asyncio.run(exercise())


def test_retrieval_hides_irrelevant_pages_and_merge_preserves_them(tmp_path: Path) -> None:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Test")
    runner = RetrievalRunner()
    service = WikiFirstService(settings, runner_resolver=lambda _name: runner)
    first_source = settings.inbox_dir / "seed.txt"
    first_source.write_text("Alpha knowledge.\n", encoding="utf-8")
    first = service.ingest()
    service.apply(str(first["job_id"]))

    second_source = settings.inbox_dir / "alpha-update.txt"
    second_source.write_text("Alpha updated.\n", encoding="utf-8")
    second = service.ingest()

    assert "alpha-topic.md" in runner.visible_pages[-1]
    assert "unrelated-topic.md" not in runner.visible_pages[-1]
    applied = service.apply(str(second["job_id"]))
    wiki = Path(str(applied["wiki_path"]))
    assert (wiki / "unrelated-topic.md").is_file()
    vault = settings.root / "vault"
    assert (vault / "wiki" / "unrelated-topic.md").is_file()
    assert not (vault / "wiki" / "unrelated-topic.md").stat().st_mode & 0o200


def test_web_packet_is_saved_then_automatically_processed(tmp_path: Path) -> None:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Test")
    runner = RetrievalRunner()
    app = create_app(
        settings.root,
        runner_resolver=lambda _name: runner,
        user_settings_path=tmp_path / "preferences.json",
    )

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/packets", data={"user_note": "Запомни этот связанный фрагмент."}
                )
                assert response.status_code == 303
                assert response.headers["location"].startswith("/inbox?saved=")
                receipt = await client.get(response.headers["location"])
                assert "Сохранено локально" in receipt.text
                storage = WikiStorage(settings)
                proposal = None
                for _attempt in range(100):
                    proposal = storage.latest_proposal()
                    if proposal is not None:
                        break
                    await asyncio.sleep(0.01)
                assert proposal is not None
                assert proposal["packet_id"] is not None
                review = await client.get("/review")
                assert f"Packet {proposal['packet_id']}" in review.text
                waiting = await client.post(
                    "/packets", data={"user_note": "Этот Packet должен спокойно подождать."}
                )
                assert waiting.headers["location"].startswith("/inbox?saved=")
                proposals = []
                for _attempt in range(100):
                    proposals = storage.proposals()
                    if len(proposals) == 2:
                        break
                    await asyncio.sleep(0.01)
                assert len(proposals) == 2
                inbox = await client.get("/inbox")
                assert "Этот Packet должен спокойно подождать" in inbox.text
                assert inbox.text.count("Готов к проверке") == 2
                statuses = (await client.get("/api/packets")).json()
                assert sum(item["state"] == "review" for item in statuses) == 2
                reviews = await client.get("/review")
                assert all(str(item["id"]) in reviews.text for item in proposals)

    asyncio.run(exercise())


def test_web_groups_packet_attempts_and_requeue_is_idempotent(tmp_path: Path) -> None:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Test")
    service = WikiFirstService(settings, runner_resolver=lambda _name: RetrievalRunner())
    packet = service.create_packet(user_note="Сохранённый материал.", files=[], urls=[])
    revisions = [
        int(item["id"]) for item in service.storage.packet_source_revisions(str(packet["id"]))
    ]
    service.storage.begin_packet_attempt(str(packet["id"]))
    for number in range(3):
        job_id = f"failed-{number}"
        service.storage.create_job(
            job_id,
            kind="packet",
            runner="claude",
            source_revision_ids=revisions,
            packet_id=str(packet["id"]),
        )
        service.storage.fail_job(job_id, "connection unavailable")
    service.storage.fail_packet_attempt(
        str(packet["id"]),
        job_id="failed-2",
        error="connection unavailable",
        retryable=False,
    )
    app = create_app(
        settings.root,
        runner_resolver=lambda _name: RetrievalRunner(),
        user_settings_path=tmp_path / "preferences.json",
    )

    async def exercise() -> None:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            inbox = (await client.get("/inbox")).text
            assert inbox.count("Повторить анализ") == 1
            assert "Retry job" not in inbox
            assert "Попытки анализа:" in inbox
            assert "Не удалось связаться с моделью" in inbox

            first = await client.post(f"/packets/{packet['id']}/process")
            second = await client.post(f"/packets/{packet['id']}/process")
            assert first.status_code == second.status_code == 303

    asyncio.run(exercise())
    queued = service.storage.packet_queue(str(packet["id"]))
    assert queued is not None
    assert queued["status"] == "queued"
    assert queued["attempt_count"] == 0
    assert len(service.storage.packet(str(packet["id"]))["attempts"]) == 3


def test_web_shows_live_packet_phase_from_persisted_events(tmp_path: Path) -> None:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Test")
    service = WikiFirstService(settings, runner_resolver=lambda _name: RetrievalRunner())
    packet = service.create_packet(user_note="Материал в процессе анализа.", files=[], urls=[])
    app = create_app(
        settings.root,
        runner_resolver=lambda _name: RetrievalRunner(),
        user_settings_path=tmp_path / "preferences.json",
    )
    storage = WikiStorage(settings)
    claimed = storage.claim_next_packet()
    assert claimed is not None
    revisions = [int(item["id"]) for item in storage.packet_source_revisions(str(packet["id"]))]
    storage.create_job(
        "active-job",
        kind="packet",
        runner="codex",
        source_revision_ids=revisions,
        packet_id=str(packet["id"]),
    )
    storage.add_task_event(
        "active-task",
        "runner",
        {"phase": "runner", "runner": "codex", "model": "test-model"},
        "active-job",
    )
    storage.add_task_event(
        "active-task",
        "model-working",
        {"phase": "model-working", "elapsed_ms": 5000},
        "active-job",
    )

    async def exercise() -> None:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            inbox = (await client.get("/inbox")).text
            assert "Codex анализирует материалы" in inbox
            statuses = (await client.get("/api/packets")).json()
            status = next(item for item in statuses if item["id"] == packet["id"])
            assert status["state"] == "processing"
            assert "Codex анализирует материалы" in status["progress"]
            assert "прошло" in status["progress"]

    asyncio.run(exercise())


def test_web_restart_resumes_interrupted_packet_from_persistent_queue(tmp_path: Path) -> None:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Test")
    service = WikiFirstService(settings, runner_resolver=lambda _name: RetrievalRunner())
    packet = service.create_packet(user_note="Продолжить после рестарта.", files=[], urls=[])
    claimed = service.storage.claim_next_packet()
    assert claimed is not None
    revisions = [
        int(item["id"]) for item in service.storage.packet_source_revisions(str(packet["id"]))
    ]
    service.storage.create_job(
        "interrupted-job",
        kind="packet",
        runner="claude",
        source_revision_ids=revisions,
        packet_id=str(packet["id"]),
    )

    app = create_app(
        settings.root,
        runner_resolver=lambda _name: RetrievalRunner(),
        user_settings_path=tmp_path / "preferences.json",
    )

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            storage = WikiStorage(settings)
            proposal = None
            for _attempt in range(100):
                proposal = storage.latest_proposal()
                if proposal is not None:
                    break
                await asyncio.sleep(0.01)
            assert proposal is not None

    asyncio.run(exercise())
    recovered = service.storage.packet(str(packet["id"]))
    assert recovered["state"] == "review"
    assert len(recovered["attempts"]) == 2
    interrupted = service.storage.get_job("interrupted-job")
    assert interrupted["status"] == "failed"
    assert "interrupted" in interrupted["rejection_reason"]

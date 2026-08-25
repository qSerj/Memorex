from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorex.config import WorkspaceSettings
from memorex.wiki_first.models import AgentRunner, PacketUpload, RunnerResult
from memorex.wiki_first.runners import CLIAgentRunner, RunnerError, _parse_usage
from memorex.wiki_first.service import (
    WikiFirstError,
    WikiFirstProcessingError,
    WikiFirstService,
)
from memorex.wiki_first.storage import SCHEMA, WikiStorage
from memorex.wiki_first.validation import validate_wiki

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8cfc000000301010018dd8db10000000049454e44ae426082"
)


class FakeWikiRunner(AgentRunner):
    def __init__(self, name: str = "fake"):
        self.name = name
        self.model = "strong-fake"
        self.calls: list[str] = []
        self.visible_sources: list[list[str]] = []

    def run(self, workdir: Path, prompt: str, *, writable: bool) -> RunnerResult:
        self.calls.append(prompt)
        if "Write the complete answer" in prompt:
            (workdir / "answer.md").write_text(
                "Решение описано на странице [[project-direction]].\n\n"
                "## Путь\n\n- [[project-direction]]\n",
                encoding="utf-8",
            )
        else:
            sources = sorted((workdir / "sources").glob("*"))
            self.visible_sources.append([source.read_text(encoding="utf-8") for source in sources])
            source = sources[-1]
            page = workdir / "wiki" / "project-direction.md"
            previous = page.read_text(encoding="utf-8") if page.exists() else ""
            revised = "Revision feedback" in prompt
            extra = "\nТекст уточнён администратором. [S1]\n" if revised else ""
            page.write_text(
                "# Направление проекта\n\n"
                "Команда выбрала Wiki-first эксперимент. [S1]\n"
                f"{extra}\n"
                "Связанная тема: [[project-direction]].\n\n"
                "## Источники\n\n"
                f"- [S1] [{source.name}](../sources/{source.name}), строки 1-2.\n",
                encoding="utf-8",
            )
            (workdir / "wiki" / "README.md").write_text(
                "# Wiki\n\n- [[project-direction]] — выбранное направление.\n",
                encoding="utf-8",
            )
            (workdir / "proposal-report.md").write_text(
                f"# Report\n\nUpdated project direction. Previous bytes: {len(previous)}.\n",
                encoding="utf-8",
            )
        return RunnerResult(
            runner=self.name,
            model=self.model,
            command_version="fake 1",
            duration_ms=7,
            stdout="ok",
            stderr="",
        )


class NoChangeRunner(AgentRunner):
    name = "fake"
    model = "no-change-fake"

    def run(self, workdir: Path, prompt: str, *, writable: bool) -> RunnerResult:
        (workdir / "proposal-report.md").write_text(
            "# Report\n\nPacket сохранён, но долговременных изменений Wiki не требуется.\n",
            encoding="utf-8",
        )
        return RunnerResult(self.name, self.model, "fake 1", 3, "ok", "")


class ContractBreakingRunner(AgentRunner):
    name = "claude"
    model = "contract-breaking-fake"

    def run(self, workdir: Path, prompt: str, *, writable: bool) -> RunnerResult:
        return RunnerResult(self.name, self.model, "fake 1", 3, "ok", "")


class FailingRunner(AgentRunner):
    model = "offline-fake"

    def __init__(self, name: str):
        self.name = name

    def run(self, workdir: Path, prompt: str, *, writable: bool) -> RunnerResult:
        raise RunnerError(f"{self.name} connection unavailable")


class ImageRunner(AgentRunner):
    name = "codex"
    model = "vision-fake"

    def __init__(self) -> None:
        self.prompt = ""
        self.images: list[Path] = []

    def run(self, workdir: Path, prompt: str, *, writable: bool) -> RunnerResult:
        raise AssertionError("Image ingest must use the attachment-aware runner contract")

    def run_with_progress(
        self,
        workdir: Path,
        prompt: str,
        *,
        writable: bool,
        images: list[Path] | None = None,
        progress=None,
        cancel_event=None,
    ) -> RunnerResult:
        self.prompt = prompt
        self.images = images or []
        image = self.images[0]
        (workdir / "wiki" / "visual-note.md").write_text(
            "# Визуальная заметка\n\nНа изображении сохранён важный фрагмент. [S1]\n\n"
            f"![Исходное изображение](../sources/{image.name})\n\n"
            "## Источники\n\n"
            f"- [S1] [{image.name}](../sources/{image.name}), изображение.\n",
            encoding="utf-8",
        )
        (workdir / "wiki" / "README.md").write_text(
            "# Wiki\n\n- [[visual-note]]\n", encoding="utf-8"
        )
        (workdir / "proposal-report.md").write_text("# Report\n\nImage inspected.\n")
        return RunnerResult(self.name, self.model, "fake 1", 2, "ok", "")


class InvalidRevisionRunner(FakeWikiRunner):
    def run(self, workdir: Path, prompt: str, *, writable: bool) -> RunnerResult:
        result = super().run(workdir, prompt, writable=writable)
        if "Revision feedback" in prompt:
            (workdir / "wiki" / "project-direction.md").write_text(
                "# Направление проекта\n\nИспорченная корректировка без источников.\n",
                encoding="utf-8",
            )
        return result


class IndependentTopicRunner(AgentRunner):
    name = "codex"
    model = "independent-fake"

    def run(self, workdir: Path, prompt: str, *, writable: bool) -> RunnerResult:
        sources = sorted((workdir / "sources").glob("r*"))
        source = sources[-1]
        text = source.read_text(encoding="utf-8")
        slug = "alpha-memory" if "Alpha" in text else "beta-memory"
        title = "Alpha" if slug.startswith("alpha") else "Beta"
        (workdir / "wiki" / f"{slug}.md").write_text(
            f"# {title}\n\n{title} knowledge. [S1]\n\n[[{slug}]]\n\n"
            f"## Источники\n\n- [S1] [{source.name}](../sources/{source.name}), строка 1.\n",
            encoding="utf-8",
        )
        (workdir / "wiki" / "README.md").write_text(f"# Wiki\n\n- [[{slug}]]\n", encoding="utf-8")
        (workdir / "proposal-report.md").write_text("# Report\n\nDone.\n")
        return RunnerResult(self.name, self.model, "fake 1", 1, "ok", "")


def make_service(tmp_path: Path) -> tuple[WikiFirstService, WorkspaceSettings, FakeWikiRunner]:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Test Wiki")
    runner = FakeWikiRunner()
    service = WikiFirstService(settings, runner_resolver=lambda _name: runner)
    return service, settings, runner


def test_ingest_review_revise_apply_query_and_idempotence(tmp_path: Path) -> None:
    service, settings, runner = make_service(tmp_path)
    source = settings.inbox_dir / "discussion.txt"
    source.write_text("Решили проверить Wiki-first.\nЭто важный эксперимент.\n", encoding="utf-8")

    proposed = service.ingest()

    assert proposed["status"] == "proposed"
    job_id = str(proposed["job_id"])
    review = service.review(job_id)
    assert "project-direction.md" in review["diff"]
    assert review["validation"]["valid"] is True

    revised = service.revise(job_id, "Revision feedback: make the decision explicit")
    assert revised["revision"] == 2
    applied = service.apply(job_id)

    assert applied["status"] == "applied"
    wiki_path = Path(str(applied["wiki_path"]))
    assert "уточнён" in (wiki_path / "project-direction.md").read_text(encoding="utf-8")
    assert service.ingest()["status"] == "unchanged"
    assert service.status()["integrity"] == "ok"

    answer = service.ask("Какое направление выбрала команда?")
    assert "[[project-direction]]" in answer["answer"]
    assert len(runner.calls) == 3


def test_manual_proposal_edit_creates_revision_without_model(tmp_path: Path) -> None:
    service, _settings, runner = make_service(tmp_path)
    packet = service.create_packet(user_note="Добавить фильм в список.", files=[], urls=[])
    proposed = service.ingest_packet(str(packet["id"]))
    job_id = str(proposed["job_id"])
    before_calls = len(runner.calls)
    current = service.storage.proposal(job_id)
    stage = service.storage.root / str(current["relative_path"])
    page = stage / "wiki" / "project-direction.md"
    edited = page.read_text(encoding="utf-8").replace(
        "Команда выбрала Wiki-first эксперимент.", "- Посмотреть один фильм."
    )

    review = service.edit_proposal_page(job_id, "project-direction.md", edited)

    assert review["revision"] == 2
    assert len(runner.calls) == before_calls
    latest = service.storage.proposal(job_id)
    saved = service.storage.root / str(latest["relative_path"]) / "wiki" / page.name
    assert "- Посмотреть один фильм." in saved.read_text(encoding="utf-8")


def test_ingest_prompt_requests_compact_lists_and_logs(tmp_path: Path) -> None:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Prompt contract")
    runner = FakeWikiRunner()
    service = WikiFirstService(settings, runner_resolver=lambda _name: runner)
    packet = service.create_packet(user_note="Посмотреть фильм", files=[], urls=[])

    service.ingest_packet(str(packet["id"]))

    assert "watchlists" in runner.calls[-1]
    assert "stable chronological" in runner.calls[-1]
    assert "Do not add boilerplate open questions" in runner.calls[-1]
    assert "existing note defines its own local schema" in runner.calls[-1].lower()
    assert "smallest change" in runner.calls[-1]


def test_workspace_model_profiles_have_portable_defaults(tmp_path: Path) -> None:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Profiles")

    assert settings.wiki.simple_profile.model == "gpt-5.6-luna"
    assert settings.wiki.simple_profile.effort == "medium"
    assert settings.wiki.standard_profile.model == "gpt-5.6-terra"
    assert settings.wiki.fallback_profile.runner == "claude"
    assert settings.wiki.fallback_profile.model == "sonnet"


def test_small_ingest_uses_simple_profile_and_semantic_fallback_uses_standard(
    tmp_path: Path,
) -> None:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Routing")
    good = FakeWikiRunner("codex")
    resolved = 0

    def resolver(_name: str) -> AgentRunner:
        nonlocal resolved
        resolved += 1
        return ContractBreakingRunner() if resolved == 1 else good

    service = WikiFirstService(settings, runner_resolver=resolver)
    packet = service.create_packet(user_note="Добавить один фильм.", files=[], urls=[])
    result = service.ingest_packet(str(packet["id"]))

    with sqlite3.connect(service.storage.database_path) as connection:
        calls = connection.execute(
            "SELECT profile, effort FROM runner_calls WHERE job_id = ? ORDER BY id",
            (result["job_id"],),
        ).fetchall()
    assert calls == [("simple", "medium"), ("standard", "medium")]


def test_multiple_sources_use_standard_profile_and_codex_failure_uses_claude(
    tmp_path: Path,
) -> None:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Routing")
    good = FakeWikiRunner("claude")

    def resolver(name: str) -> AgentRunner:
        return FailingRunner("codex") if name == "codex" else good

    service = WikiFirstService(settings, runner_resolver=resolver)
    packet = service.create_packet(
        user_note="Эти два файла связаны.",
        files=[
            ("one.txt", "text/plain", b"One.\nMore.\n"),
            ("two.txt", "text/plain", b"Two.\nMore.\n"),
        ],
        urls=[],
    )
    result = service.ingest_packet(str(packet["id"]))

    with sqlite3.connect(service.storage.database_path) as connection:
        calls = connection.execute(
            "SELECT profile, runner, effort FROM runner_calls WHERE job_id = ? ORDER BY id",
            (result["job_id"],),
        ).fetchall()
    assert calls == [
        ("standard", "codex", "medium"),
        ("fallback", "claude", "medium"),
    ]


def test_local_notes_edit_search_notebooks_and_attachments_without_ai(tmp_path: Path) -> None:
    service, _settings, runner = make_service(tmp_path)
    service.initialize()
    inbox = next(item for item in service.storage.notebooks() if item["system_key"] == "inbox")
    calls = len(runner.calls)

    note = service.create_note(
        "Фильмы, которые я хочу посмотреть",
        "- Good Luck Have Fun Don't Die",
        str(inbox["id"]),
    )
    original_snapshot = str(service.storage.active_snapshot()["id"])
    service.storage.add_note_attachment(
        str(note["id"]), name="poster.jpg", mime_type="image/jpeg", data=b"poster bytes"
    )
    personal = service.storage.create_notebook("Личное")
    edited = service.edit_note(
        str(note["id"]),
        title="Фильмы и сериалы",
        body="- Good Luck Have Fun Don't Die\n- Severance",
        notebook_id=str(personal["id"]),
        expected_snapshot_id=original_snapshot,
    )

    assert len(runner.calls) == calls
    assert edited["title"] == "Фильмы и сериалы"
    assert edited["notebook_name"] == "Личное"
    assert service.search_notes("Severance")[0]["id"] == note["id"]
    attachment = edited["attachments"][0]
    assert (service.storage.root / attachment["object_path"]).read_bytes() == b"poster bytes"
    assert service.history()[0]["reason"] == f"manual-edit:{note['id']}"
    with pytest.raises(WikiFirstError, match="memory changed"):
        service.edit_note(
            str(note["id"]),
            title="Устаревшая правка",
            body="lost",
            notebook_id=str(personal["id"]),
            expected_snapshot_id=original_snapshot,
        )

    service.storage.delete_notebook(str(personal["id"]))
    assert service.storage.note(str(note["id"]))["notebook_name"] == "Входящие"


def test_discussion_uses_only_explicit_note_context_and_persists_answer(tmp_path: Path) -> None:
    service, _settings, runner = make_service(tmp_path)
    source = service.settings.inbox_dir / "discussion.txt"
    source.write_text("Wiki-first context.\nSecond line.\n", encoding="utf-8")
    proposal = service.ingest()
    service.apply(str(proposal["job_id"]))
    first = service.storage.note_for_slug("project-direction")
    assert first["notebook_name"] == "Входящие"
    inbox = next(item for item in service.storage.notebooks() if item["system_key"] == "inbox")
    unrelated = service.create_note(
        "Посторонняя заметка", "Не передавать модели.", str(inbox["id"])
    )
    discussion = service.storage.create_discussion("Архитектура", [str(first["id"])])
    turn = service.storage.create_discussion_turn(str(discussion["id"]), "Что мы решили?")

    result = service.answer_discussion_turn(str(turn["id"]))

    assert result["selected_pages"] == ["project-direction"]
    completed = service.storage.discussion_turn(str(turn["id"]))
    assert completed["status"] == "succeeded"
    assert completed["answer_message_id"] is not None
    answer_stage = sorted(service.storage.answers_dir.iterdir())[-1]
    visible = {path.stem for path in (answer_stage / "wiki").glob("*.md")}
    assert visible == {"project-direction"}
    assert str(unrelated["slug"]) not in visible


def test_interrupted_discussion_keeps_question_for_retry(tmp_path: Path) -> None:
    service, _settings, _runner = make_service(tmp_path)
    service.initialize()
    inbox = next(item for item in service.storage.notebooks() if item["system_key"] == "inbox")
    note = service.create_note("Надёжный диалог", "Контекст.", str(inbox["id"]))
    discussion = service.storage.create_discussion("Диалог", [str(note["id"])])
    turn = service.storage.create_discussion_turn(str(discussion["id"]), "Не потеряй вопрос")
    service.storage.start_discussion_turn(str(turn["id"]), "abandoned-task")

    assert service.storage.recover_interrupted_discussions() == 1

    recovered = service.storage.discussion_turn(str(turn["id"]))
    assert recovered["status"] == "failed"
    assert recovered["question"] == "Не потеряй вопрос"
    assert "retry" in recovered["error"]


def test_changed_source_creates_new_snapshot_and_rollback(tmp_path: Path) -> None:
    service, settings, _runner = make_service(tmp_path)
    source = settings.inbox_dir / "notes.md"
    source.write_text("Первая версия.\nВторая строка.\n", encoding="utf-8")
    first = service.ingest()
    first_snapshot = str(service.apply(str(first["job_id"]))["snapshot"])

    source.write_text("Изменённая версия.\nВторая строка.\n", encoding="utf-8")
    second = service.ingest()
    second_snapshot = str(service.apply(str(second["job_id"]))["snapshot"])

    assert first_snapshot != second_snapshot
    assert len(service.history()) == 3
    rolled_back = service.rollback(first_snapshot)
    assert rolled_back["snapshot"] == first_snapshot


def test_tell_is_a_reviewable_source_and_rejection_does_not_activate(tmp_path: Path) -> None:
    service, _settings, _runner = make_service(tmp_path)

    proposed = service.tell("Я считаю эту идею приоритетной.")
    before = service.status()["active_snapshot"]
    rejected = service.reject(str(proposed["job_id"]), "too broad")

    assert rejected["status"] == "rejected"
    assert service.status()["active_snapshot"] == before
    assert service.status()["pending_sources"] == 1


def test_packet_groups_note_files_and_urls_into_one_reviewable_ingest(tmp_path: Path) -> None:
    service, _settings, runner = make_service(tmp_path)
    packet = service.create_packet(
        user_note="Эти материалы нужно понимать вместе.",
        files=[
            ("same.txt", "text/plain", b"Shared material.\nSecond line.\n"),
            ("same.txt", "text/plain", b"Shared material.\nSecond line.\n"),
        ],
        urls=["https://example.com/reference"],
    )
    other = service.create_packet(user_note="Unrelated private fragment.", files=[], urls=[])

    assert [item["kind"] for item in packet["items"]] == ["file", "file", "url"]
    assert [item["ordinal"] for item in packet["items"]] == [0, 1, 2]
    assert packet["waiting_importer_count"] == 1
    assert service.storage.pending_revisions() == []
    assert service.ingest()["status"] == "unchanged"
    first_revision = service.storage.source_revision(packet["items"][0]["source_revision_id"])
    second_revision = service.storage.source_revision(packet["items"][1]["source_revision_id"])
    assert first_revision["object_path"] == second_revision["object_path"]

    proposed = service.ingest_packet(str(packet["id"]))
    review = service.review(str(proposed["job_id"]))

    assert proposed["status"] == "proposed"
    assert review["packet"]["id"] == packet["id"]
    assert "one user Packet" in runner.calls[-1]
    seen = "\n".join(runner.visible_sources[-1])
    assert "Эти материалы нужно понимать вместе" in seen
    assert "Shared material" in seen
    assert "Unrelated private fragment" not in seen

    service.reject(str(proposed["job_id"]), "not yet")
    assert service.storage.packet(str(packet["id"]))["state"] == "ready"
    assert service.storage.packet(str(other["id"]))["state"] == "queued"

    retried = service.ingest_packet(str(packet["id"]))
    service.apply(str(retried["job_id"]))
    remembered = service.storage.packet(str(packet["id"]))
    assert remembered["state"] == "remembered"
    assert remembered["processable_count"] == 0
    assert remembered["waiting_importer_count"] == 1


def test_packet_analyzes_selected_images_and_keeps_other_images_as_artifacts(
    tmp_path: Path,
) -> None:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Image memory")
    runner = ImageRunner()
    service = WikiFirstService(settings, runner_resolver=lambda _name: runner)
    packet = service.create_packet(
        user_note="Разбери снимок вместе с моим комментарием.",
        files=[
            PacketUpload("scan.png", "image/png", PNG, "analyze", "Распознай таблицу"),
            PacketUpload("keepsake.png", "image/png", PNG, "store", ""),
        ],
        urls=[],
    )

    assert packet["state"] == "queued"
    assert packet["stored_only_count"] == 1
    assert len(service.storage.packet_source_revisions(str(packet["id"]))) == 2

    proposed = service.ingest_packet(str(packet["id"]))

    assert proposed["status"] == "proposed"
    assert [path.read_bytes() for path in runner.images] == [PNG]
    assert "scan.png (image; user instruction: Распознай таблицу)" in runner.prompt
    assert "keepsake.png" not in runner.prompt
    assert service.review(str(proposed["job_id"]))["validation"]["valid"] is True

    applied = service.apply(str(proposed["job_id"]))
    snapshot_sources = Path(str(applied["wiki_path"])).parent / "sources"
    assert len(list(snapshot_sources.glob("*.png"))) == 1
    assert service.storage.packet(str(packet["id"]))["state"] == "remembered"
    stored = service.storage.packet_item(str(packet["id"]), str(packet["items"][1]["id"]))
    assert (service.storage.root / str(stored["object_path"])).read_bytes() == PNG


def test_packet_with_only_stored_image_does_not_enter_analysis_queue(tmp_path: Path) -> None:
    service, _settings, _runner = make_service(tmp_path)

    packet = service.create_packet(
        user_note="",
        files=[PacketUpload("photo.jpg", "image/jpeg", b"\xff\xd8\xffphoto", "store")],
        urls=[],
    )

    assert packet["state"] == "stored"
    assert packet["processable_count"] == 0
    assert packet["queue"] is None


def test_codex_runner_attaches_images_explicitly() -> None:
    runner = CLIAgentRunner("codex", "test-model", "high")
    image = Path("/tmp/test image.png")

    command = runner._command("/usr/bin/codex", "inspect", writable=True, images=[image])

    assert command[-4:] == ["--image", str(image), "--", "inspect"]


def test_claude_runner_disables_global_plugins_and_mcp_customizations() -> None:
    runner = CLIAgentRunner("claude", "opus", "max")

    command = runner._command("/usr/bin/claude", "inspect", writable=True, images=[])

    assert command[1] == "--safe-mode"


def test_invalid_revision_restores_previous_proposal_and_review_queue(tmp_path: Path) -> None:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Revision recovery")
    runner = InvalidRevisionRunner()
    service = WikiFirstService(settings, runner_resolver=lambda _name: runner)
    packet = service.create_packet(user_note="Исходный материал.", files=[], urls=[])
    proposed = service.ingest_packet(str(packet["id"]))

    with pytest.raises(WikiFirstError, match="Revised proposal is invalid"):
        service.revise(str(proposed["job_id"]), "Revision feedback: simplify")

    restored = service.storage.get_job(str(proposed["job_id"]))
    assert restored["status"] == "proposed"
    assert restored["current_revision"] == 1
    assert service.storage.latest_proposal()["id"] == proposed["job_id"]
    assert service.storage.packet(str(packet["id"]))["state"] == "review"


def test_restart_preserves_proposal_when_revision_was_interrupted(tmp_path: Path) -> None:
    service, _settings, _runner = make_service(tmp_path)
    packet = service.create_packet(user_note="Исходный материал.", files=[], urls=[])
    proposed = service.ingest_packet(str(packet["id"]))
    service.storage.set_job_status(str(proposed["job_id"]), "running")

    assert service.storage.recover_interrupted_jobs() == 1

    restored = service.storage.get_job(str(proposed["job_id"]))
    assert restored["status"] == "proposed"
    assert service.storage.packet(str(packet["id"]))["state"] == "review"


def test_independent_packets_reach_review_and_rebase_without_blocking(tmp_path: Path) -> None:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Parallel review")
    runner = IndependentTopicRunner()
    service = WikiFirstService(settings, runner_resolver=lambda _name: runner)
    alpha = service.create_packet(user_note="Alpha", files=[], urls=[])
    beta = service.create_packet(user_note="Beta", files=[], urls=[])

    first = service.ingest_packet(str(alpha["id"]))
    second = service.ingest_packet(str(beta["id"]))

    assert [item["id"] for item in service.storage.proposals()] == [
        first["job_id"],
        second["job_id"],
    ]
    assert service.storage.packet(str(alpha["id"]))["state"] == "review"
    assert service.storage.packet(str(beta["id"]))["state"] == "review"

    service.apply(str(first["job_id"]))
    rebased = service.apply(str(second["job_id"]))

    assert rebased["status"] == "applied"
    pages = {item["slug"] for item in service.storage.list_pages()}
    assert {"alpha-memory", "beta-memory"} <= pages
    readme = next(item for item in service.storage.list_pages() if item["slug"] == "README")
    assert "[[alpha-memory]]" in readme["text"]
    assert "[[beta-memory]]" in readme["text"]


def test_conflicting_stale_packet_is_requeued_instead_of_overwriting(tmp_path: Path) -> None:
    service, _settings, _runner = make_service(tmp_path)
    first_packet = service.create_packet(user_note="First direction.", files=[], urls=[])
    second_packet = service.create_packet(user_note="Second direction.", files=[], urls=[])
    first = service.ingest_packet(str(first_packet["id"]))
    second = service.ingest_packet(str(second_packet["id"]))
    service.apply(str(first["job_id"]))

    result = service.apply(str(second["job_id"]))

    assert result["status"] == "requeued"
    assert service.storage.get_job(str(second["job_id"]))["status"] == "stale"
    assert service.storage.packet(str(second_packet["id"]))["state"] == "queued"


def test_packet_no_change_is_processed_without_identical_snapshot(tmp_path: Path) -> None:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Test Wiki")
    runner = NoChangeRunner()
    service = WikiFirstService(settings, runner_resolver=lambda _name: runner)
    packet = service.create_packet(user_note="Слабый сигнал на будущее.", files=[], urls=[])
    before = service.storage.active_snapshot()["id"]

    result = service.ingest_packet(str(packet["id"]))

    assert result["status"] == "no_change"
    assert service.storage.active_snapshot()["id"] == before
    assert service.storage.latest_proposal() is None
    stored = service.storage.packet(str(packet["id"]))
    assert stored["state"] == "processed"
    assert stored["processable_count"] == 0
    assert "долговременных изменений" in result["report"]


def test_packet_rejects_non_http_urls_without_saving(tmp_path: Path) -> None:
    service, _settings, _runner = make_service(tmp_path)

    with pytest.raises(WikiFirstError, match=r"HTTP\(S\)"):
        service.create_packet(user_note="", files=[], urls=["file:///etc/passwd"])
    with pytest.raises(WikiFirstError, match="Unsafe"):
        service.create_packet(
            user_note="",
            files=[("..\\private.txt", "text/plain", b"secret")],
            urls=[],
        )

    assert service.storage.packets() == []


def test_existing_wiki_database_receives_numbered_packet_migration(tmp_path: Path) -> None:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Test Wiki")
    storage = WikiStorage(settings)
    storage.root.mkdir(parents=True)
    with sqlite3.connect(storage.database_path) as connection:
        connection.executescript(SCHEMA)

    storage.initialize()

    with storage.connection() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        migrations = {
            int(row["version"]): str(row["name"])
            for row in connection.execute(
                "SELECT version, name FROM schema_migrations WHERE version IN (4, 5, 6)"
            ).fetchall()
        }
        packet_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(packet_items)").fetchall()
        }
    assert {"packets", "packet_items", "job_packets", "packet_queue"} <= tables
    assert migrations == {
        4: "wiki_first_packets",
        5: "wiki_first_packet_queue",
        6: "wiki_first_packet_analysis",
    }
    assert {"processing_mode", "analysis_instruction"} <= packet_columns


def test_invalid_primary_runner_falls_back_without_staging_collision(tmp_path: Path) -> None:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Test Wiki")
    primary = ContractBreakingRunner()
    fallback = FakeWikiRunner("codex")
    service = WikiFirstService(
        settings,
        runner_resolver=lambda name: primary if name == "claude" else fallback,
    )
    (settings.inbox_dir / "notes.txt").write_text(
        "Материал для проверки fallback.\nЕщё строка.\n", encoding="utf-8"
    )

    result = service.ingest()

    assert result["status"] == "proposed"
    assert result["runner"] == "codex"
    assert service.storage.get_job(str(result["job_id"]))["status"] == "proposed"


def test_packet_queue_survives_interruption_and_claim_is_exclusive(tmp_path: Path) -> None:
    service, _settings, _runner = make_service(tmp_path)
    packet = service.create_packet(user_note="Не потерять при перезапуске.", files=[], urls=[])

    assert packet["state"] == "queued"
    claimed = service.storage.claim_next_packet()
    assert claimed is not None
    assert claimed["packet_id"] == packet["id"]
    assert claimed["attempt_count"] == 1
    assert service.storage.claim_next_packet() is None

    service.storage.recover_interrupted_jobs()

    recovered = service.storage.packet(str(packet["id"]))
    assert recovered["state"] == "queued"
    assert recovered["queue"]["attempt_count"] == 1


def test_retryable_packet_failure_uses_bounded_persistent_backoff(tmp_path: Path) -> None:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Test Wiki")
    service = WikiFirstService(settings, runner_resolver=lambda name: FailingRunner(name))
    packet = service.create_packet(user_note="Связь может оборваться.", files=[], urls=[])

    with pytest.raises(WikiFirstProcessingError) as raised:
        service.ingest_packet(str(packet["id"]))

    assert raised.value.retryable is True
    first = service.storage.packet(str(packet["id"]))
    assert first["state"] == "retry_wait"
    assert first["queue"]["attempt_count"] == 1
    assert service.storage.claim_next_packet(now="9999-12-31T23:59:59+00:00") is not None
    second = service.storage.fail_packet_attempt(
        str(packet["id"]), job_id=None, error="still offline", retryable=True
    )
    assert second["status"] == "retry_wait"
    assert service.storage.claim_next_packet(now="9999-12-31T23:59:59+00:00") is not None
    final = service.storage.fail_packet_attempt(
        str(packet["id"]), job_id=None, error="still offline", retryable=True
    )
    assert final["status"] == "failed"


def test_failed_packet_reuses_completed_stage_without_another_model_call(tmp_path: Path) -> None:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Test Wiki")

    def unexpected_runner(_name: str) -> AgentRunner:
        raise AssertionError("A completed recoverable stage must not call a model again")

    service = WikiFirstService(settings, runner_resolver=unexpected_runner)
    packet = service.create_packet(user_note="Восстановить готовый результат.", files=[], urls=[])
    pending = service.storage.packet_source_revisions(str(packet["id"]))
    service.storage.begin_packet_attempt(str(packet["id"]))
    job = service.storage.create_job(
        "recoverable",
        kind="packet",
        runner="claude",
        source_revision_ids=[int(item["id"]) for item in pending],
        packet_id=str(packet["id"]),
    )
    base = service.storage.snapshot_path(service._snapshot(str(job["base_snapshot_id"])))
    stage = service.storage.jobs_dir / "recoverable" / "rev-1"
    service._prepare_subset(stage, base, ["README"], pending)
    authored = FakeWikiRunner("codex")
    authored.run(stage, "", writable=True)
    (stage / "proposal-actions.json").write_text(
        '{"actions": [{"action": "upsert", "path": "wiki/README.md"}, '
        '{"action": "upsert", "path": "wiki/project-direction.md"}]}',
        encoding="utf-8",
    )
    service.storage.record_call(
        {
            "job_id": "recoverable",
            "purpose": "ingest",
            "runner": "codex",
            "model": "strong-fake",
            "command_version": "fake 1",
            "prompt_version": "wiki-first-ingest-v3",
            "duration_ms": 7,
            "status": "succeeded",
        }
    )
    service.storage.fail_job("recoverable", "Unsafe Wiki page path: wiki/README.md")
    service.storage.fail_packet_attempt(
        str(packet["id"]),
        job_id="recoverable",
        error="Unsafe Wiki page path: wiki/README.md",
        retryable=False,
    )
    service.queue_packet(str(packet["id"]))

    recovered = service.ingest_packet(str(packet["id"]))

    assert recovered["status"] == "proposed"
    assert recovered["job_id"] == "recoverable"
    assert recovered["runner"] == "codex"
    assert recovered["recovered"] is True
    assert service.storage.get_job("recoverable")["status"] == "proposed"
    assert len(authored.calls) == 1


def test_validator_rejects_unresolved_links_and_missing_citations(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    sources = tmp_path / "sources"
    wiki.mkdir()
    sources.mkdir()
    (wiki / "README.md").write_text("# Wiki\n", encoding="utf-8")
    (wiki / "broken.md").write_text(
        "# Broken\n\nUnsupported [S1] and [[missing]].\n\n## Источники\n",
        encoding="utf-8",
    )

    result = validate_wiki(wiki, sources)

    assert result.valid is False
    assert any("unresolved Wiki link" in error for error in result.errors)
    assert any("undefined citations" in error for error in result.errors)


def test_validator_checks_reference_style_source_links_and_line_fragments(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    sources = tmp_path / "sources"
    wiki.mkdir()
    sources.mkdir()
    (sources / "note.txt").write_text("one\ntwo\n", encoding="utf-8")
    (wiki / "README.md").write_text("# Wiki\n", encoding="utf-8")
    (wiki / "topic.md").write_text(
        '# Topic\n\nKnown. [S1]\n\n## Источники\n\n[S1]: ../sources/note.txt#L1-L2 "Support"\n',
        encoding="utf-8",
    )

    assert validate_wiki(wiki, sources).valid is True


def test_validator_accepts_image_citation_but_rejects_fake_line_locator(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    sources = tmp_path / "sources"
    wiki.mkdir()
    sources.mkdir()
    (sources / "scan.png").write_bytes(PNG)
    (wiki / "README.md").write_text("# Wiki\n", encoding="utf-8")
    page = wiki / "scan-note.md"
    page.write_text(
        "# Scan\n\nVisible fact. [S1]\n\n![Scan](../sources/scan.png)\n\n"
        "## Источники\n\n- [S1] [scan.png](../sources/scan.png), изображение.\n",
        encoding="utf-8",
    )

    assert validate_wiki(wiki, sources).valid is True

    page.write_text(
        "# Scan\n\nVisible fact. [S1]\n\n## Источники\n\n"
        "- [S1] [scan.png](../sources/scan.png), строка 1.\n",
        encoding="utf-8",
    )
    result = validate_wiki(wiki, sources)
    assert any("image source cannot use a line locator" in error for error in result.errors)


def test_active_snapshot_detects_external_modification(tmp_path: Path) -> None:
    service, settings, _runner = make_service(tmp_path)
    (settings.inbox_dir / "notes.txt").write_text("Материал.\nЕщё строка.\n", encoding="utf-8")
    proposal = service.ingest()
    applied = service.apply(str(proposal["job_id"]))
    page = Path(str(applied["wiki_path"])) / "README.md"
    page.chmod(0o644)
    page.write_text("tampered", encoding="utf-8")

    assert service.status()["integrity"] == "modified"
    with pytest.raises(ValueError, match="modified outside Memorex"):
        service.ask("Что известно?")


def test_next_ingest_recovers_job_interrupted_in_a_previous_process(tmp_path: Path) -> None:
    service, settings, _runner = make_service(tmp_path)
    source = settings.inbox_dir / "notes.txt"
    source.write_text("Незавершённый запуск.\nЕщё строка.\n", encoding="utf-8")
    service.storage.initialize()
    revision = service.storage.register_source(source)
    abandoned = service.storage.create_job(
        "abandoned",
        kind="ingest",
        runner="claude",
        source_revision_ids=[int(revision["id"])],
    )

    assert abandoned["status"] == "running"
    proposed = service.ingest()

    assert proposed["status"] == "proposed"
    assert service.storage.get_job("abandoned")["status"] == "failed"


def test_unknown_runtime_runner_is_rejected_before_creating_a_job(tmp_path: Path) -> None:
    service, settings, _runner = make_service(tmp_path)
    (settings.inbox_dir / "notes.txt").write_text("Текст.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="choose claude or codex"):
        service.ingest(runner_name="small-model")

    assert service.status()["proposal"] is None


def test_cli_telemetry_is_schema_validated_before_token_logging() -> None:
    claude = _parse_usage(
        "claude",
        '{"result":"done","usage":{"input_tokens":120,"output_tokens":40,'
        '"cache_read_input_tokens":80}}',
    )
    codex = _parse_usage(
        "codex",
        '{"type":"item.completed"}\n'
        '{"type":"turn.completed","usage":{"input_tokens":200,'
        '"cached_input_tokens":50,"output_tokens":60}}',
    )

    assert claude == {"input_tokens": 120, "output_tokens": 40, "cached_input_tokens": 80}
    assert codex == {"input_tokens": 200, "output_tokens": 60, "cached_input_tokens": 50}

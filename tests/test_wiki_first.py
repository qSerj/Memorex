from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memorex.config import WorkspaceSettings
from memorex.wiki_first.models import AgentRunner, RunnerResult
from memorex.wiki_first.runners import RunnerError, _parse_usage
from memorex.wiki_first.service import (
    WikiFirstError,
    WikiFirstProcessingError,
    WikiFirstService,
)
from memorex.wiki_first.storage import SCHEMA, WikiStorage
from memorex.wiki_first.validation import validate_wiki


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
                "SELECT version, name FROM schema_migrations WHERE version IN (4, 5)"
            ).fetchall()
        }
    assert {"packets", "packet_items", "job_packets", "packet_queue"} <= tables
    assert migrations == {4: "wiki_first_packets", 5: "wiki_first_packet_queue"}


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

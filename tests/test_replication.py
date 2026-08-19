from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ocean_bot.application.analytics import AnalyticsEvent, AnalyticsRecord, AnalyticsService
from ocean_bot.application.assessments import AssessmentService, StorageUnavailable
from ocean_bot.application.data_rights import (
    DataRightsUnavailable,
    StoredUserData,
    UserReferences,
)
from ocean_bot.domain.assessment import Assessment, AssessmentStatus, UserRecord
from ocean_bot.domain.questionnaire import Item, Questionnaire, Trait
from ocean_bot.infrastructure.database import (
    Base,
    SqlAnalyticsRepository,
    SqlAssessmentRepository,
    SqlUserDataRightsRepository,
    session_factory,
)
from ocean_bot.infrastructure.replication import (
    AuthoritativeAnalyticsRepository,
    LocalAssessmentEventWriter,
    LocalFirstAssessmentRepository,
    LocalFirstUserDataRightsRepository,
    ReplicationMode,
    RetryingAssessmentRepository,
    SyncHealth,
    SyncOutboxRepository,
    SyncService,
)


async def _database(
    path: Path,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, session_factory(engine)


async def test_local_answer_survives_restart_before_remote_sync(tmp_path: Path) -> None:
    local_engine, local_sessions = await _database(tmp_path / "local.db")
    remote_engine, remote_sessions = await _database(tmp_path / "remote.db")
    questionnaire = Questionnaire(
        "quick",
        "1",
        "Quick",
        (
            Item("e1", "First", Trait.EXTRAVERSION, False),
            Item("e2", "Second", Trait.EXTRAVERSION, False),
        ),
    )
    outbox = SyncOutboxRepository(local_sessions)
    local = SqlAssessmentRepository(local_sessions, outbox=outbox)
    remote = SqlAssessmentRepository(remote_sessions)
    repository = LocalFirstAssessmentRepository(
        local,
        remote,
        outbox,
        ReplicationMode.AUTHORITATIVE,
    )
    service = AssessmentService(repository, {"quick": questionnaire})
    now = datetime(2026, 1, 1, tzinfo=UTC)

    await service.accept_terms(42, "en", now)
    started = await service.start(42, "quick", now)
    await service.answer(42, started.id, "e1", 5, now)
    assert [job.kind for job in await outbox.pending(now)] == ["user"]
    assert {job.kind for job in await outbox.pending(now + timedelta(seconds=5))} == {
        "user",
        "assessment",
    }

    restarted = AssessmentService(
        LocalFirstAssessmentRepository(
            SqlAssessmentRepository(local_sessions, outbox=outbox),
            remote,
            outbox,
            ReplicationMode.AUTHORITATIVE,
        ),
        {"quick": questionnaire},
    )
    resumed = await restarted.resume(42, now)
    assert resumed is not None and resumed.answered_count == 1
    assert await AssessmentService(remote, {"quick": questionnaire}).resume(42, now) is None

    references = UserReferences("long-test-pepper")
    sync = SyncService(
        outbox,
        SqlAssessmentRepository(local_sessions),
        SqlAnalyticsRepository(local_sessions),
        remote,
        SqlAnalyticsRepository(remote_sessions),
        references,
    )
    result = await sync.run_once(now + timedelta(seconds=30))

    assert result.synced_users == 1
    assert result.synced_assessments == 1
    remote_resumed = await AssessmentService(remote, {"quick": questionnaire}).resume(42, now)
    assert remote_resumed is not None and remote_resumed.answered_count == 1

    await local_engine.dispose()
    await remote_engine.dispose()


async def test_analytics_retry_does_not_increment_remote_counter_twice(
    tmp_path: Path,
) -> None:
    local_engine, local_sessions = await _database(tmp_path / "local.db")
    remote_engine, remote_sessions = await _database(tmp_path / "remote.db")
    outbox = SyncOutboxRepository(local_sessions)
    local_analytics = SqlAnalyticsRepository(local_sessions, outbox=outbox)
    remote_analytics = SqlAnalyticsRepository(remote_sessions)
    service = AnalyticsService(local_analytics, UserReferences("long-test-pepper"))
    now = datetime(2026, 1, 1, tzinfo=UTC)

    await service.record(42, "answer_recorded", now)
    event = await local_analytics.get_event((await outbox.pending(now))[0].entity_id)
    assert event is not None

    await remote_analytics.add_event(event)
    await remote_analytics.add_event(event)

    assert await remote_analytics.counts() == {"answer_recorded": 1}

    await local_engine.dispose()
    await remote_engine.dispose()


async def test_failed_outbox_job_observes_backoff_after_hard_deadline(
    tmp_path: Path,
) -> None:
    engine, sessions = await _database(tmp_path / "local.db")
    outbox = SyncOutboxRepository(sessions)
    analytics = AnalyticsService(
        SqlAnalyticsRepository(sessions, outbox=outbox),
        UserReferences("long-test-pepper"),
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await analytics.record(42, "answer_recorded", now)
    job = (await outbox.pending(now))[0]

    await outbox.fail(job, now)

    assert await outbox.pending(now + timedelta(seconds=1)) == []
    assert await outbox.pending(now + timedelta(seconds=2)) == [job]
    await engine.dispose()


async def test_outbox_acknowledges_jobs_in_one_batch(tmp_path: Path) -> None:
    engine, sessions = await _database(tmp_path / "local.db")
    outbox = SyncOutboxRepository(sessions)
    analytics = AnalyticsService(
        SqlAnalyticsRepository(sessions, outbox=outbox),
        UserReferences("long-test-pepper"),
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)

    await analytics.record(42, "answer_recorded", now)
    await analytics.record(43, "answer_recorded", now)
    jobs = await outbox.pending(now)
    assert len(jobs) == 2

    await outbox.acknowledge_many(jobs)

    assert await outbox.pending_count() == 0
    await engine.dispose()


async def test_authoritative_analytics_honors_local_volume_write_guard(
    tmp_path: Path,
) -> None:
    class FullVolumeGuard:
        async def ensure_writable(self, *, starting_new_assessment: bool = False) -> None:
            raise StorageUnavailable("Local assessment storage is full")

    engine, sessions = await _database(tmp_path / "local.db")
    local = SqlAnalyticsRepository(sessions)
    repository = AuthoritativeAnalyticsRepository(
        local,
        local,
        FullVolumeGuard(),  # type: ignore[arg-type]
    )

    with pytest.raises(StorageUnavailable):
        await repository.add_event(
            AnalyticsRecord(
                "event-1",
                "user-ref",
                "answer_recorded",
                datetime(2026, 1, 1, tzinfo=UTC),
                {},
            )
        )

    assert await local.counts() == {}
    await engine.dispose()


async def test_answer_commit_persists_analytics_in_the_same_local_store(
    tmp_path: Path,
) -> None:
    engine, sessions = await _database(tmp_path / "local.db")
    outbox = SyncOutboxRepository(sessions)
    references = UserReferences("long-test-pepper")
    analytics_repository = SqlAnalyticsRepository(sessions, outbox=outbox)
    assessment_repository = SqlAssessmentRepository(
        sessions,
        outbox=outbox,
        transition_events=LocalAssessmentEventWriter(
            analytics_repository,
            references,
        ),
    )
    questionnaire = Questionnaire(
        "quick",
        "1",
        "Quick",
        (
            Item("e1", "First", Trait.EXTRAVERSION, False),
            Item("e2", "Second", Trait.EXTRAVERSION, False),
        ),
    )
    assessments = AssessmentService(assessment_repository, {"quick": questionnaire})
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await assessments.accept_terms(42, "en", now)
    started = await assessments.start(42, "quick", now)

    await assessments.answer(
        42,
        started.id,
        "e1",
        5,
        now,
        lambda assessment: AnalyticsEvent(
            "answer_recorded",
            now,
            {"questionnaire_id": assessment.questionnaire_id},
        ),
    )

    assert await analytics_repository.counts() == {"answer_recorded": 1}
    assert {job.kind for job in await outbox.pending(now + timedelta(seconds=5))} == {
        "user",
        "assessment",
        "analytics",
    }
    await engine.dispose()


async def test_export_includes_pending_local_assessment(tmp_path: Path) -> None:
    local_engine, local_sessions = await _database(tmp_path / "local.db")
    remote_engine, remote_sessions = await _database(tmp_path / "remote.db")
    questionnaire = Questionnaire(
        "quick",
        "1",
        "Quick",
        (Item("e1", "First", Trait.EXTRAVERSION, False),),
    )
    outbox = SyncOutboxRepository(local_sessions)
    local_assessments = SqlAssessmentRepository(local_sessions, outbox=outbox)
    remote_assessments = SqlAssessmentRepository(remote_sessions)
    assessments = AssessmentService(
        LocalFirstAssessmentRepository(
            local_assessments,
            remote_assessments,
            outbox,
            ReplicationMode.AUTHORITATIVE,
        ),
        {"quick": questionnaire},
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await assessments.accept_terms(42, "en", now)
    await assessments.start(42, "quick", now)

    data_rights = LocalFirstUserDataRightsRepository(
        SqlUserDataRightsRepository(local_sessions),
        SqlUserDataRightsRepository(remote_sessions),
    )
    stored = await data_rights.load_user_data(42, UserReferences("long-test-pepper")(42))

    assert stored.user is not None
    assert len(stored.assessments) == 1
    assert stored.assessments[0].status is AssessmentStatus.ACTIVE

    await local_engine.dispose()
    await remote_engine.dispose()


async def test_failed_remote_deletion_leaves_a_local_tombstone(
    tmp_path: Path,
) -> None:
    class UnavailableRemoteRights:
        async def load_user_data(self, telegram_user_id: int, user_ref: str) -> StoredUserData:
            return StoredUserData(None, (), ())

        async def delete_user_data(self, telegram_user_id: int, user_ref: str) -> None:
            raise ConnectionError("remote unavailable")

    engine, sessions = await _database(tmp_path / "local.db")
    outbox = SyncOutboxRepository(sessions)
    assessments = SqlAssessmentRepository(sessions, outbox=outbox)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await assessments.save_user(UserRecord(42, "en", now))
    local_rights = SqlUserDataRightsRepository(sessions, outbox=outbox)
    repository = LocalFirstUserDataRightsRepository(
        local_rights,
        UnavailableRemoteRights(),
    )

    with pytest.raises(DataRightsUnavailable):
        await repository.delete_user_data(42, "user-ref")

    assert await repository.load_user_data(42, "user-ref") == StoredUserData(
        None,
        (),
        (),
    )
    assert "delete_user" in {job.kind for job in await outbox.pending(datetime.now(UTC))}
    await engine.dispose()


async def test_user_facing_remote_read_retries_one_invalidated_connection() -> None:
    class FlakyRemote:
        def __init__(self, user: UserRecord) -> None:
            self.user = user
            self.attempts = 0

        async def get_user(self, telegram_user_id: int) -> UserRecord:
            self.attempts += 1
            if self.attempts == 1:
                raise DBAPIError(
                    "SELECT user",
                    {},
                    ConnectionError("stale connection"),
                    connection_invalidated=True,
                )
            return self.user

    expected = UserRecord(42, "en", datetime(2026, 1, 1, tzinfo=UTC))
    remote = FlakyRemote(expected)
    repository = RetryingAssessmentRepository(remote)  # type: ignore[arg-type]

    assert await repository.get_user(42) == expected
    assert remote.attempts == 2


async def test_shadow_mode_reports_revision_mismatch_without_failing_remote_write(
    tmp_path: Path,
) -> None:
    local_engine, local_sessions = await _database(tmp_path / "local.db")
    remote_engine, remote_sessions = await _database(tmp_path / "remote.db")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    user = UserRecord(42, "en", now)
    questionnaire = Questionnaire(
        "quick",
        "1",
        "Quick",
        (
            Item("e1", "First", Trait.EXTRAVERSION, False),
            Item("e2", "Second", Trait.EXTRAVERSION, False),
        ),
    )
    assessment = Assessment.start("assessment-1", 42, questionnaire, now)
    local = SqlAssessmentRepository(local_sessions)
    remote = SqlAssessmentRepository(remote_sessions)
    await local.save_user(user)
    await remote.save_user(user)
    local_stored = await local.create_assessment(assessment)
    remote_stored = await remote.create_assessment(assessment)
    await local.save_assessment(
        local_stored,
        assessment.answer(questionnaire, "e1", 1, now),
    )
    health = SyncHealth()
    repository = LocalFirstAssessmentRepository(
        local,
        remote,
        SyncOutboxRepository(local_sessions),
        ReplicationMode.SHADOW,
        replication_health=health,
    )

    saved = await repository.save_assessment(
        remote_stored,
        assessment.answer(questionnaire, "e1", 5, now),
    )

    assert saved.token == 1
    assert saved.assessment.answer_map == {"e1": 5}
    assert health.revision_mismatches == 1
    await local_engine.dispose()
    await remote_engine.dispose()


async def test_newer_revision_wins_results_and_export_merge(tmp_path: Path) -> None:
    local_engine, local_sessions = await _database(tmp_path / "local.db")
    remote_engine, remote_sessions = await _database(tmp_path / "remote.db")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    user = UserRecord(42, "en", now)
    questionnaire = Questionnaire(
        "quick",
        "1",
        "Quick",
        (Item("e1", "First", Trait.EXTRAVERSION, False),),
    )
    assessment = Assessment.start("assessment-1", 42, questionnaire, now)
    local = SqlAssessmentRepository(local_sessions)
    remote = SqlAssessmentRepository(remote_sessions)
    await local.save_user(user)
    await remote.save_user(user)
    local_stored = await local.create_assessment(assessment)
    remote_stored = await remote.create_assessment(assessment)
    local_completed = await local.save_assessment(
        local_stored,
        assessment.answer(questionnaire, "e1", 1, now),
    )
    remote_completed = await remote.save_assessment(
        remote_stored,
        assessment.answer(questionnaire, "e1", 5, now),
    )
    await remote.save_assessment(remote_completed, remote_completed.assessment)
    outbox = SyncOutboxRepository(local_sessions)

    results = await LocalFirstAssessmentRepository(
        local,
        remote,
        outbox,
        ReplicationMode.AUTHORITATIVE,
    ).list_completed(42)
    exported = await LocalFirstUserDataRightsRepository(
        SqlUserDataRightsRepository(local_sessions),
        SqlUserDataRightsRepository(remote_sessions),
    ).load_user_data(42, UserReferences("long-test-pepper")(42))

    assert local_completed.token == 1
    assert results[0].token == 2
    assert results[0].assessment.answer_map == {"e1": 5}
    assert exported.assessments[0].answer_map == {"e1": 5}
    await local_engine.dispose()
    await remote_engine.dispose()

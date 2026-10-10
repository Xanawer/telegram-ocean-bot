from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import partial
from uuid import uuid4

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ocean_bot.application.analytics import (
    AnalyticsEvent,
    AnalyticsRecord,
    AnalyticsRepository,
)
from ocean_bot.application.assessments import (
    AssessmentRepository,
    StorageUnavailable,
    StoredAssessment,
)
from ocean_bot.application.data_rights import (
    DataRightsUnavailable,
    LocalUserDataRightsRepository,
    RevisionedUserDataRightsRepository,
    StoredUserData,
    UserDataRightsRepository,
    UserReferences,
)
from ocean_bot.domain.assessment import Assessment, AssessmentStatus, UserRecord
from ocean_bot.infrastructure.database import (
    SqlAnalyticsRepository,
    SqlAssessmentRepository,
    SqlUserDataRightsRepository,
    SyncJobKind,
    SyncOutboxModel,
)
from ocean_bot.infrastructure.local import LocalStorageGuard

_logger = logging.getLogger(__name__)


async def _retry_disconnect[T](operation: Callable[[], Awaitable[T]]) -> T:
    try:
        return await operation()
    except DBAPIError as error:
        if not error.connection_invalidated:
            raise
        return await operation()


_READ_THROUGH_ATTEMPTS = 3


class ReplicationCoordinator:
    """Order deletions against sync batches and read-through imports.

    Local-first storage runs as exactly one replica, so in-process coordination suffices.
    A batch or read-through that read remote state before a deletion must not write it
    back afterwards: sync batches hold the lock throughout, and read-throughs compare the
    deletion count from before their remote read while holding the lock to store.
    """

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.deletions = 0

    @asynccontextmanager
    async def deleting(self) -> AsyncIterator[None]:
        async with self.lock:
            self.deletions += 1
            yield


class ReplicationMode(StrEnum):
    SHADOW = "shadow"
    AUTHORITATIVE = "authoritative"


@dataclass(frozen=True, slots=True)
class SyncJob:
    kind: SyncJobKind
    entity_id: str
    revision: int


@dataclass(frozen=True, slots=True)
class SyncResult:
    synced_users: int = 0
    synced_assessments: int = 0
    synced_analytics: int = 0
    failed: int = 0


@dataclass(frozen=True, slots=True)
class SyncDrainResult:
    synced_users: int
    synced_assessments: int
    synced_analytics: int
    failed: int
    remaining: int


@dataclass(slots=True)
class SyncHealth:
    healthy: bool = True
    last_success: datetime | None = None
    oldest_pending: datetime | None = None
    revision_mismatches: int = 0


class RetryingAssessmentRepository(AssessmentRepository):
    def __init__(self, repository: AssessmentRepository) -> None:
        self._repository = repository

    async def save_user(self, user: UserRecord) -> None:
        await _retry_disconnect(partial(self._repository.save_user, user))

    async def get_user(self, telegram_user_id: int) -> UserRecord | None:
        return await _retry_disconnect(partial(self._repository.get_user, telegram_user_id))

    async def create_assessment(self, assessment: Assessment) -> StoredAssessment:
        return await _retry_disconnect(partial(self._repository.create_assessment, assessment))

    async def save_assessment(
        self,
        stored: StoredAssessment,
        assessment: Assessment,
        event: AnalyticsEvent | None = None,
    ) -> StoredAssessment:
        return await _retry_disconnect(
            partial(self._repository.save_assessment, stored, assessment, event)
        )

    async def get_assessment(self, assessment_id: str) -> StoredAssessment | None:
        return await _retry_disconnect(partial(self._repository.get_assessment, assessment_id))

    async def get_active(self, telegram_user_id: int) -> StoredAssessment | None:
        return await _retry_disconnect(partial(self._repository.get_active, telegram_user_id))

    async def list_completed(self, telegram_user_id: int) -> list[StoredAssessment]:
        return await _retry_disconnect(partial(self._repository.list_completed, telegram_user_id))

    async def list_active_before(self, cutoff: datetime) -> list[StoredAssessment]:
        return await _retry_disconnect(partial(self._repository.list_active_before, cutoff))

    async def delete_assessment(self, assessment_id: str, telegram_user_id: int) -> None:
        await _retry_disconnect(
            partial(
                self._repository.delete_assessment,
                assessment_id,
                telegram_user_id,
            )
        )


class RetryingAnalyticsRepository(AnalyticsRepository):
    def __init__(self, repository: AnalyticsRepository) -> None:
        self._repository = repository

    async def add_event(self, event: AnalyticsRecord) -> None:
        await _retry_disconnect(partial(self._repository.add_event, event))

    async def counts(self) -> dict[str, int]:
        return await _retry_disconnect(self._repository.counts)

    async def delete_before(self, cutoff: datetime) -> int:
        return await _retry_disconnect(partial(self._repository.delete_before, cutoff))


class RetryingUserDataRightsRepository(UserDataRightsRepository):
    def __init__(self, repository: UserDataRightsRepository) -> None:
        self._repository = repository

    async def load_user_data(self, telegram_user_id: int, user_ref: str) -> StoredUserData:
        return await _retry_disconnect(
            partial(self._repository.load_user_data, telegram_user_id, user_ref)
        )

    async def delete_user_data(self, telegram_user_id: int, user_ref: str) -> None:
        await _retry_disconnect(
            partial(self._repository.delete_user_data, telegram_user_id, user_ref)
        )


class LocalBootstrapService:
    def __init__(
        self,
        local: SqlAssessmentRepository,
        remote: SqlAssessmentRepository,
        local_rights: LocalUserDataRightsRepository | None = None,
    ) -> None:
        self._local = local
        self._remote = remote
        self._local_rights = local_rights

    async def run(self) -> int:
        active = [
            stored
            for stored in await _retry_disconnect(self._remote.list_all_active)
            if self._local_rights is None
            or await self._local_rights.deletion_user_ref(stored.assessment.telegram_user_id)
            is None
        ]
        users = await _retry_disconnect(
            lambda: self._remote.users_by_id(
                list({stored.assessment.telegram_user_id for stored in active})
            )
        )
        await self._local.import_users(users)
        await self._local.import_assessments(active)
        return len(active)


class SyncOutboxRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def enqueue_user(self, session: AsyncSession, user: UserRecord) -> None:
        revision = int(user.accepted_at.timestamp() * 1_000_000)
        await self._upsert(
            session,
            SyncJobKind.USER,
            str(user.telegram_user_id),
            revision,
            user.accepted_at,
            user.accepted_at,
        )

    async def enqueue_assessment(
        self,
        session: AsyncSession,
        assessment: Assessment,
        revision: int,
    ) -> None:
        completed = assessment.status is AssessmentStatus.COMPLETED
        await self._upsert(
            session,
            SyncJobKind.ASSESSMENT,
            assessment.id,
            revision,
            assessment.updated_at if completed else assessment.updated_at + timedelta(seconds=5),
            assessment.updated_at if completed else assessment.updated_at + timedelta(seconds=30),
        )

    async def enqueue_analytics(
        self,
        session: AsyncSession,
        event: AnalyticsRecord,
    ) -> None:
        await self._upsert(
            session,
            SyncJobKind.ANALYTICS,
            event.event_id,
            0,
            event.occurred_at,
            event.occurred_at,
        )

    async def enqueue_delete_user(
        self,
        session: AsyncSession,
        telegram_user_id: int,
        created_at: datetime,
    ) -> None:
        await self._upsert(
            session,
            SyncJobKind.DELETE_USER,
            str(telegram_user_id),
            0,
            created_at,
            created_at,
        )

    async def pending(
        self,
        now: datetime,
        limit: int = 100,
        *,
        force: bool = False,
    ) -> list[SyncJob]:
        statement = select(SyncOutboxModel)
        if not force:
            statement = statement.where(
                or_(
                    SyncOutboxModel.available_at <= now,
                    SyncOutboxModel.force_at <= now,
                )
            )
        statement = statement.order_by(SyncOutboxModel.created_at, SyncOutboxModel.kind).limit(
            limit
        )
        async with self._sessions() as session:
            models = (await session.scalars(statement)).all()
        return [
            SyncJob(SyncJobKind(model.kind), model.entity_id, model.revision) for model in models
        ]

    async def acknowledge(self, job: SyncJob) -> None:
        await self.acknowledge_many([job])

    async def acknowledge_many(self, jobs: list[SyncJob]) -> None:
        if not jobs:
            return
        conditions = [
            and_(
                SyncOutboxModel.kind == job.kind,
                SyncOutboxModel.entity_id == job.entity_id,
                SyncOutboxModel.revision == job.revision,
            )
            for job in jobs
        ]
        async with self._sessions() as session, session.begin():
            await session.execute(delete(SyncOutboxModel).where(or_(*conditions)))

    async def fail(self, job: SyncJob, now: datetime) -> None:
        async with self._sessions() as session, session.begin():
            model = await session.get(SyncOutboxModel, (job.kind, job.entity_id))
            if model is None or model.revision != job.revision:
                return
            model.attempts += 1
            delay = min(300, 2 ** min(model.attempts, 8))
            retry_at = now + timedelta(seconds=delay)
            model.available_at = retry_at
            model.force_at = retry_at

    async def oldest_pending(self) -> datetime | None:
        async with self._sessions() as session:
            value = await session.scalar(
                select(SyncOutboxModel.created_at).order_by(SyncOutboxModel.created_at)
            )
        return value

    async def pending_count(self) -> int:
        async with self._sessions() as session:
            value = await session.scalar(select(func.count()).select_from(SyncOutboxModel))
        return value or 0

    @staticmethod
    async def _upsert(
        session: AsyncSession,
        kind: SyncJobKind,
        entity_id: str,
        revision: int,
        available_at: datetime,
        force_at: datetime,
    ) -> None:
        key = (kind, entity_id)
        model = await session.get(SyncOutboxModel, key)
        if model is None:
            session.add(
                SyncOutboxModel(
                    kind=kind,
                    entity_id=entity_id,
                    revision=revision,
                    created_at=available_at,
                    available_at=available_at,
                    force_at=force_at,
                    attempts=0,
                )
            )
            return
        model.revision = revision
        model.available_at = available_at
        stored_force_at = model.force_at
        if stored_force_at.tzinfo is None:
            stored_force_at = stored_force_at.replace(tzinfo=UTC)
        model.force_at = min(stored_force_at, force_at)
        model.attempts = 0


class LocalAssessmentEventWriter:
    def __init__(
        self,
        analytics: SqlAnalyticsRepository,
        user_references: UserReferences,
    ) -> None:
        self._analytics = analytics
        self._user_references = user_references

    async def record_event(
        self,
        session: AsyncSession,
        assessment: Assessment,
        event: AnalyticsEvent,
    ) -> None:
        await self._analytics.add_event_in_session(
            session,
            AnalyticsRecord(
                event_id=str(uuid4()),
                user_ref=self._user_references(assessment.telegram_user_id),
                name=event.name,
                occurred_at=event.occurred_at,
                properties=dict(event.properties),
            ),
        )


class LocalFirstAssessmentRepository(AssessmentRepository):
    def __init__(
        self,
        local: SqlAssessmentRepository,
        remote: AssessmentRepository,
        outbox: SyncOutboxRepository,
        mode: ReplicationMode,
        storage_guard: LocalStorageGuard | None = None,
        replication_health: SyncHealth | None = None,
        local_rights: LocalUserDataRightsRepository | None = None,
        coordinator: ReplicationCoordinator | None = None,
    ) -> None:
        self._local = local
        self._remote = remote
        self._outbox = outbox
        self._mode = mode
        self._storage_guard = storage_guard
        self._replication_health = replication_health
        self._local_rights = local_rights
        self._coordinator = coordinator or ReplicationCoordinator()

    async def save_user(self, user: UserRecord) -> None:
        if await self._deletion_pending(user.telegram_user_id):
            raise StorageUnavailable(
                "Your previous deletion request is still being completed; please try again later"
            )
        if self._storage_guard is not None:
            await self._storage_guard.ensure_writable()
        if self._mode is ReplicationMode.SHADOW:
            await _retry_disconnect(partial(self._remote.save_user, user))
            try:
                await self._local.save_user(user)
            except (OSError, SQLAlchemyError):
                _logger.exception("Shadow user mirror failed")
            return
        try:
            await self._local.save_user(user)
        except (OSError, SQLAlchemyError) as error:
            raise StorageUnavailable(
                "Local assessment storage is unavailable; please try again"
            ) from error

    async def get_user(self, telegram_user_id: int) -> UserRecord | None:
        if await self._deletion_pending(telegram_user_id):
            return None
        if self._mode is ReplicationMode.AUTHORITATIVE:
            user = await self._local.get_user(telegram_user_id)
            if user is not None:
                return user
        return await self._read_through(
            partial(self._remote.get_user, telegram_user_id),
            lambda user: (user.telegram_user_id,),
            self._local.import_user,
        )

    async def create_assessment(self, assessment: Assessment) -> StoredAssessment:
        if self._storage_guard is not None:
            await self._storage_guard.ensure_writable(starting_new_assessment=True)
        if self._mode is ReplicationMode.SHADOW:
            remote = await _retry_disconnect(partial(self._remote.create_assessment, assessment))
            try:
                local = await self._local.create_assessment(assessment)
                self._compare_shadow(local, remote)
            except Exception:
                self._record_shadow_mismatch()
                _logger.exception("Shadow assessment mirror failed")
            return remote
        try:
            return await self._local.create_assessment(assessment)
        except (OSError, SQLAlchemyError) as error:
            raise StorageUnavailable(
                "Local assessment storage is unavailable; please try again"
            ) from error

    async def save_assessment(
        self,
        stored: StoredAssessment,
        assessment: Assessment,
        event: AnalyticsEvent | None = None,
    ) -> StoredAssessment:
        if self._storage_guard is not None:
            await self._storage_guard.ensure_writable()
        if self._mode is ReplicationMode.SHADOW:
            remote = await _retry_disconnect(
                partial(self._remote.save_assessment, stored, assessment, event)
            )
            try:
                local = await self._local.save_assessment(stored, assessment, event)
                self._compare_shadow(local, remote)
            except Exception:
                self._record_shadow_mismatch()
                _logger.exception("Shadow assessment mirror failed")
            return remote
        try:
            return await self._local.save_assessment(stored, assessment, event)
        except (OSError, SQLAlchemyError) as error:
            raise StorageUnavailable("Your answer could not be saved; please try again") from error

    async def get_assessment(self, assessment_id: str) -> StoredAssessment | None:
        store: Callable[[list[StoredAssessment]], Awaitable[None]]
        if self._mode is ReplicationMode.AUTHORITATIVE:
            stored = await self._local.get_assessment(assessment_id)
            if stored is not None:
                if await self._deletion_pending(stored.assessment.telegram_user_id):
                    return None
                return stored
            store = self._import_with_owner
        else:
            store = self._mirror_shadow
        return await self._read_through(
            partial(self._remote.get_assessment, assessment_id),
            lambda stored: (stored.assessment.telegram_user_id,),
            lambda stored: store([stored]),
        )

    async def get_active(self, telegram_user_id: int) -> StoredAssessment | None:
        if await self._deletion_pending(telegram_user_id):
            return None
        if self._mode is ReplicationMode.AUTHORITATIVE:
            return await self._local.get_active(telegram_user_id)
        return await self._read_through(
            partial(self._remote.get_active, telegram_user_id),
            lambda stored: (stored.assessment.telegram_user_id,),
            lambda stored: self._mirror_shadow([stored]),
        )

    async def list_completed(self, telegram_user_id: int) -> list[StoredAssessment]:
        if await self._deletion_pending(telegram_user_id):
            return []
        if self._mode is ReplicationMode.SHADOW:
            mirrored = await self._read_through(
                partial(self._remote.list_completed, telegram_user_id),
                lambda _: (telegram_user_id,),
                self._mirror_shadow,
            )
            return mirrored or []
        remote = await _retry_disconnect(partial(self._remote.list_completed, telegram_user_id))
        local = await self._local.list_completed(telegram_user_id)
        merged = {stored.assessment.id: stored for stored in remote}
        for stored in local:
            current = merged.get(stored.assessment.id)
            if current is None or self._revision(stored) >= self._revision(current):
                merged[stored.assessment.id] = stored
        return sorted(
            merged.values(),
            key=lambda stored: stored.assessment.completed_at or stored.assessment.updated_at,
            reverse=True,
        )

    async def list_active_before(self, cutoff: datetime) -> list[StoredAssessment]:
        primary = self._remote if self._mode is ReplicationMode.SHADOW else self._local
        if primary is self._remote:
            return await _retry_disconnect(partial(primary.list_active_before, cutoff))
        return await primary.list_active_before(cutoff)

    async def delete_assessment(self, assessment_id: str, telegram_user_id: int) -> None:
        async with self._coordinator.deleting():
            await _retry_disconnect(
                partial(self._remote.delete_assessment, assessment_id, telegram_user_id)
            )
            await self._local.delete_assessment(assessment_id, telegram_user_id)

    async def _deletion_pending(self, telegram_user_id: int) -> bool:
        return (
            self._local_rights is not None
            and await self._local_rights.deletion_user_ref(telegram_user_id) is not None
        )

    async def _read_through[T](
        self,
        fetch: Callable[[], Awaitable[T | None]],
        owners: Callable[[T], Iterable[int]],
        store: Callable[[T], Awaitable[None]],
    ) -> T | None:
        """Read remote state and cache it locally unless a deletion intervened."""
        for _ in range(_READ_THROUGH_ATTEMPTS):
            deletions = self._coordinator.deletions
            value = await _retry_disconnect(fetch)
            if value is None:
                return None
            async with self._coordinator.lock:
                if self._coordinator.deletions != deletions:
                    continue
                for owner in owners(value):
                    if await self._deletion_pending(owner):
                        return None
                await store(value)
                return value
        raise StorageUnavailable("Storage is busy; please try again")

    async def _import_with_owner(
        self,
        stored_assessments: list[StoredAssessment],
        *,
        enqueue: bool = False,
    ) -> None:
        if not stored_assessments:
            return
        owners: list[UserRecord] = []
        for user_id in {stored.assessment.telegram_user_id for stored in stored_assessments}:
            if await self._local.get_user(user_id) is not None:
                continue
            owner = await _retry_disconnect(partial(self._remote.get_user, user_id))
            if owner is not None:
                owners.append(owner)
        await self._local.import_users(owners)
        await self._local.import_assessments(stored_assessments, enqueue=enqueue)

    async def _mirror_shadow(self, stored_assessments: list[StoredAssessment]) -> None:
        try:
            await self._import_with_owner(stored_assessments, enqueue=True)
        except (OSError, SQLAlchemyError) as error:
            self._record_shadow_mismatch()
            _logger.warning("shadow_mirror_failed error=%s", type(error).__name__)

    @staticmethod
    def _revision(stored: StoredAssessment) -> int:
        return stored.token if isinstance(stored.token, int) else -1

    def _compare_shadow(self, local: StoredAssessment, remote: StoredAssessment) -> None:
        if local != remote:
            self._record_shadow_mismatch()

    def _record_shadow_mismatch(self) -> None:
        if self._replication_health is not None:
            self._replication_health.revision_mismatches += 1
            count = self._replication_health.revision_mismatches
        else:
            count = 1
        _logger.warning("shadow_revision_mismatch count=%d", count)


class LocalFirstUserDataRightsRepository(UserDataRightsRepository):
    def __init__(
        self,
        local: LocalUserDataRightsRepository,
        remote: RevisionedUserDataRightsRepository,
        mode: ReplicationMode = ReplicationMode.AUTHORITATIVE,
        coordinator: ReplicationCoordinator | None = None,
    ) -> None:
        self._local = local
        self._remote = remote
        self._mode = mode
        self._coordinator = coordinator or ReplicationCoordinator()

    async def load_user_data(self, telegram_user_id: int, user_ref: str) -> StoredUserData:
        if await self._local.deletion_user_ref(telegram_user_id) is not None:
            return StoredUserData(None, (), ())
        local = await self._local.load_user_data_with_revisions(telegram_user_id, user_ref)
        remote = await _retry_disconnect(
            partial(
                self._remote.load_user_data_with_revisions,
                telegram_user_id,
                user_ref,
            )
        )
        if self._mode is ReplicationMode.SHADOW:
            return remote.data
        assessments = {assessment.id: assessment for assessment in remote.data.assessments}
        revisions = dict(remote.assessment_revisions)
        for assessment in local.data.assessments:
            local_revision = local.assessment_revisions.get(assessment.id, -1)
            if local_revision >= revisions.get(assessment.id, -1):
                assessments[assessment.id] = assessment
                revisions[assessment.id] = local_revision
        analytics = {record.event_id: record for record in remote.data.analytics}
        analytics.update({record.event_id: record for record in local.data.analytics})
        return StoredUserData(
            local.data.user or remote.data.user,
            tuple(assessments.values()),
            tuple(analytics.values()),
        )

    async def delete_user_data(self, telegram_user_id: int, user_ref: str) -> None:
        async with self._coordinator.deleting():
            await self._local.mark_deletion(telegram_user_id, user_ref, datetime.now(UTC))
            try:
                await _retry_disconnect(
                    partial(self._remote.delete_user_data, telegram_user_id, user_ref)
                )
            except Exception as error:
                await self._local.purge_user_data(telegram_user_id, user_ref)
                raise DataRightsUnavailable(
                    "Deletion could not be confirmed remotely; use /delete_me to retry"
                ) from error
            await self._local.purge_user_data(telegram_user_id, user_ref)
            await self._local.finalize_deletion(telegram_user_id)


class MirroringAnalyticsRepository(AnalyticsRepository):
    def __init__(
        self,
        local: SqlAnalyticsRepository,
        remote: AnalyticsRepository,
    ) -> None:
        self._local = local
        self._remote = remote

    async def add_event(self, event: AnalyticsRecord) -> None:
        await _retry_disconnect(partial(self._remote.add_event, event))
        await self._local.add_event(event)

    async def counts(self) -> dict[str, int]:
        return await _retry_disconnect(self._remote.counts)

    async def delete_before(self, cutoff: datetime) -> int:
        remote, _ = await _delete_in_both(self._remote, self._local, cutoff)
        return remote


class AuthoritativeAnalyticsRepository(AnalyticsRepository):
    def __init__(
        self,
        local: SqlAnalyticsRepository,
        remote: AnalyticsRepository,
        storage_guard: LocalStorageGuard | None = None,
    ) -> None:
        self._local = local
        self._remote = remote
        self._storage_guard = storage_guard

    async def add_event(self, event: AnalyticsRecord) -> None:
        if self._storage_guard is not None:
            await self._storage_guard.ensure_writable()
        await self._local.add_event(event)

    async def counts(self) -> dict[str, int]:
        return await _retry_disconnect(self._remote.counts)

    async def delete_before(self, cutoff: datetime) -> int:
        remote, local = await _delete_in_both(self._remote, self._local, cutoff)
        return remote + local


async def _delete_in_both(
    remote: AnalyticsRepository,
    local: SqlAnalyticsRepository,
    cutoff: datetime,
) -> tuple[int, int]:
    """Purge expired events from both stores, so a remote outage cannot block local retention."""
    try:
        remote_deleted = await _retry_disconnect(partial(remote.delete_before, cutoff))
    except Exception:
        await local.delete_before(cutoff)
        raise
    return remote_deleted, await local.delete_before(cutoff)


class SyncService:
    def __init__(
        self,
        outbox: SyncOutboxRepository,
        local_assessments: SqlAssessmentRepository,
        local_analytics: SqlAnalyticsRepository,
        remote_assessments: SqlAssessmentRepository,
        remote_analytics: SqlAnalyticsRepository,
        user_references: UserReferences,
        local_rights: SqlUserDataRightsRepository | None = None,
        remote_rights: SqlUserDataRightsRepository | None = None,
        coordinator: ReplicationCoordinator | None = None,
    ) -> None:
        self._outbox = outbox
        self._local_assessments = local_assessments
        self._local_analytics = local_analytics
        self._remote_assessments = remote_assessments
        self._remote_analytics = remote_analytics
        self._user_references = user_references
        self._local_rights = local_rights
        self._remote_rights = remote_rights
        self._coordinator = coordinator or ReplicationCoordinator()
        self._last_backlog_warning: datetime | None = None

    async def run_once(
        self,
        now: datetime | None = None,
        *,
        force: bool = False,
    ) -> SyncResult:
        async with self._coordinator.lock:
            return await self._run_batch(now or datetime.now(UTC), force=force)

    async def _run_batch(self, effective_now: datetime, *, force: bool) -> SyncResult:
        jobs = await self._outbox.pending(effective_now, force=force)
        users = 0
        assessments = 0
        analytics = 0
        failed = 0
        local_rights = self._local_rights
        remote_rights = self._remote_rights
        delete_jobs = [job for job in jobs if job.kind is SyncJobKind.DELETE_USER]
        delete_acknowledged: list[SyncJob] = []
        for job in delete_jobs:
            if local_rights is None or remote_rights is None:
                delete_acknowledged.append(job)
                continue
            user_ref = await local_rights.deletion_user_ref(int(job.entity_id))
            if user_ref is None:
                delete_acknowledged.append(job)
                continue
            try:
                await _retry_disconnect(
                    partial(
                        remote_rights.delete_user_data,
                        int(job.entity_id),
                        user_ref,
                    )
                )
                await local_rights.purge_user_data(int(job.entity_id), user_ref)
                await local_rights.finalize_deletion(int(job.entity_id))
                delete_acknowledged.append(job)
            except Exception as error:
                failed += 1
                await self._outbox.fail(job, effective_now)
                _logger.warning(
                    "sync_batch_failed kind=delete_user error=%s",
                    type(error).__name__,
                )
        await self._outbox.acknowledge_many(delete_acknowledged)
        user_jobs = [job for job in jobs if job.kind is SyncJobKind.USER]
        user_records: list[UserRecord] = []
        for job in user_jobs:
            user = await self._local_assessments.get_user(int(job.entity_id))
            if user is not None:
                user_records.append(user)
        try:
            await _retry_disconnect(lambda: self._remote_assessments.import_users(user_records))
            await self._outbox.acknowledge_many(user_jobs)
            users = len(user_records)
        except Exception as error:
            failed += len(user_jobs)
            for job in user_jobs:
                await self._outbox.fail(job, effective_now)
            _logger.warning(
                "sync_batch_failed kind=user count=%d error=%s",
                len(user_jobs),
                type(error).__name__,
            )

        assessment_jobs = [job for job in jobs if job.kind is SyncJobKind.ASSESSMENT]
        assessment_records: list[StoredAssessment] = []
        for job in assessment_jobs:
            stored = await self._local_assessments.get_assessment(job.entity_id)
            if stored is not None:
                assessment_records.append(stored)
        try:
            await _retry_disconnect(
                lambda: self._remote_assessments.import_assessments(assessment_records)
            )
            records_by_id = {stored.assessment.id: stored for stored in assessment_records}
            for job in assessment_jobs:
                stored = records_by_id.get(job.entity_id)
                if (
                    stored is not None
                    and stored.token == job.revision
                    and stored.assessment.status is not AssessmentStatus.ACTIVE
                ):
                    await self._local_assessments.delete_assessment(
                        stored.assessment.id,
                        stored.assessment.telegram_user_id,
                    )
            await self._outbox.acknowledge_many(assessment_jobs)
            assessments = len(assessment_records)
        except Exception as error:
            failed += len(assessment_jobs)
            for job in assessment_jobs:
                await self._outbox.fail(job, effective_now)
            _logger.warning(
                "sync_batch_failed kind=assessment count=%d error=%s",
                len(assessment_jobs),
                type(error).__name__,
            )

        analytics_jobs = [job for job in jobs if job.kind is SyncJobKind.ANALYTICS]
        analytics_records: list[AnalyticsRecord] = []
        for job in analytics_jobs:
            event = await self._local_analytics.get_event(job.entity_id)
            if event is not None:
                analytics_records.append(event)
        try:
            analytics = await _retry_disconnect(
                lambda: self._remote_analytics.add_events(analytics_records)
            )
            await self._local_analytics.delete_events(
                [record.event_id for record in analytics_records]
            )
            await self._outbox.acknowledge_many(analytics_jobs)
        except Exception as error:
            failed += len(analytics_jobs)
            for job in analytics_jobs:
                await self._outbox.fail(job, effective_now)
            _logger.warning(
                "sync_batch_failed kind=analytics count=%d error=%s",
                len(analytics_jobs),
                type(error).__name__,
            )
        return SyncResult(users, assessments, analytics, failed)

    async def run_forever(
        self,
        health: SyncHealth,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        while True:
            try:
                now = datetime.now(UTC)
                result = await self.run_once(now)
                health.healthy = result.failed == 0
                health.last_success = now
                health.oldest_pending = await self._outbox.oldest_pending()
                oldest_age = 0.0
                if health.oldest_pending is not None:
                    oldest = health.oldest_pending
                    if oldest.tzinfo is None:
                        oldest = oldest.replace(tzinfo=UTC)
                    oldest_age = max(0.0, (now - oldest).total_seconds())
                if any(
                    (
                        result.synced_users,
                        result.synced_assessments,
                        result.synced_analytics,
                        result.failed,
                    )
                ):
                    _logger.info(
                        "sync_cycle users=%d assessments=%d analytics=%d "
                        "failed=%d oldest_age_s=%.1f",
                        result.synced_users,
                        result.synced_assessments,
                        result.synced_analytics,
                        result.failed,
                        oldest_age,
                    )
                warning_due = (
                    self._last_backlog_warning is None
                    or now - self._last_backlog_warning >= timedelta(minutes=1)
                )
                if oldest_age >= 15 * 60 and warning_due:
                    _logger.warning(
                        "sync_backlog oldest_age_s=%.1f failed=%d",
                        oldest_age,
                        result.failed,
                    )
                    self._last_backlog_warning = now
            except asyncio.CancelledError:
                raise
            except Exception:
                health.healthy = False
                _logger.exception("Local synchronization cycle failed")
            await sleep(1)

    async def drain(self, max_cycles: int = 100) -> SyncDrainResult:
        totals = SyncResult()
        for _ in range(max_cycles):
            remaining = await self._outbox.pending_count()
            if remaining == 0 or totals.failed:
                break
            result = await self.run_once(datetime.now(UTC), force=True)
            totals = SyncResult(
                totals.synced_users + result.synced_users,
                totals.synced_assessments + result.synced_assessments,
                totals.synced_analytics + result.synced_analytics,
                totals.failed + result.failed,
            )
        return SyncDrainResult(
            totals.synced_users,
            totals.synced_assessments,
            totals.synced_analytics,
            totals.failed,
            await self._outbox.pending_count(),
        )

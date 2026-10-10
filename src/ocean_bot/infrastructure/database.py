from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum
from time import perf_counter
from typing import Any, Protocol, cast

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    delete,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import Select

from ocean_bot.application.analytics import AnalyticsEvent, AnalyticsRecord
from ocean_bot.application.assessments import ActiveAssessmentExists, StaleInput, StoredAssessment
from ocean_bot.application.data_rights import RevisionedUserData, StoredUserData
from ocean_bot.domain.assessment import Assessment, AssessmentStatus, UserRecord
from ocean_bot.domain.questionnaire import Trait

_logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class UserModel(Base):
    __tablename__ = "users"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    language_code: Mapped[str | None] = mapped_column(String(16))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AssessmentModel(Base):
    __tablename__ = "assessments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_user_id", ondelete="CASCADE"),
        index=True,
    )
    questionnaire_id: Mapped[str] = mapped_column(String(64))
    questionnaire_version: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), index=True)
    answers: Mapped[dict[str, int]] = mapped_column(JSON)
    scores: Mapped[dict[str, float]] = mapped_column(JSON)
    facet_scores: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


Index(
    "uq_assessments_one_active_per_user",
    AssessmentModel.telegram_user_id,
    unique=True,
    postgresql_where=AssessmentModel.status == AssessmentStatus.ACTIVE.value,
    sqlite_where=AssessmentModel.status == AssessmentStatus.ACTIVE.value,
)


class AnalyticsEventModel(Base):
    __tablename__ = "analytics_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36))
    user_ref: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    properties: Mapped[dict[str, str]] = mapped_column(JSON)


class AnalyticsCounterModel(Base):
    __tablename__ = "analytics_counters"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False)


class SyncJobKind(StrEnum):
    USER = "user"
    ASSESSMENT = "assessment"
    ANALYTICS = "analytics"
    DELETE_USER = "delete_user"


Index(
    "uq_analytics_events_event_id",
    AnalyticsEventModel.event_id,
    unique=True,
)


class SyncOutboxModel(Base):
    __tablename__ = "sync_outbox"

    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    force_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class DeletionTombstoneModel(Base):
    __tablename__ = "deletion_tombstones"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


Index("ix_sync_outbox_available_at", SyncOutboxModel.available_at)


class SyncOutboxWriter(Protocol):
    async def enqueue_user(self, session: AsyncSession, user: UserRecord) -> None: ...

    async def enqueue_assessment(
        self,
        session: AsyncSession,
        assessment: Assessment,
        revision: int,
    ) -> None: ...

    async def enqueue_analytics(
        self,
        session: AsyncSession,
        event: AnalyticsRecord,
    ) -> None: ...

    async def enqueue_delete_user(
        self,
        session: AsyncSession,
        telegram_user_id: int,
        created_at: datetime,
    ) -> None: ...


class AssessmentEventWriter(Protocol):
    async def record_event(
        self,
        session: AsyncSession,
        assessment: Assessment,
        event: AnalyticsEvent,
    ) -> None: ...


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


class SqlAssessmentRepository:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        outbox: SyncOutboxWriter | None = None,
        transition_events: AssessmentEventWriter | None = None,
        storage_label: str = "remote",
    ) -> None:
        self._sessions = sessions
        self._outbox = outbox
        self._transition_events = transition_events
        self._storage_label = storage_label

    async def save_user(self, user: UserRecord) -> None:
        async with self._sessions() as session, session.begin():
            model = await session.get(UserModel, user.telegram_user_id)
            if model is None:
                session.add(
                    UserModel(
                        telegram_user_id=user.telegram_user_id,
                        language_code=user.language_code,
                        accepted_at=user.accepted_at,
                    )
                )
            else:
                model.language_code = user.language_code
                model.accepted_at = user.accepted_at
            if self._outbox is not None:
                await self._outbox.enqueue_user(session, user)

    async def get_user(self, telegram_user_id: int) -> UserRecord | None:
        async with self._sessions() as session:
            model = await session.get(UserModel, telegram_user_id)
            if model is None:
                return None
            return UserRecord(
                model.telegram_user_id,
                model.language_code,
                _as_utc(model.accepted_at),
            )

    async def create_assessment(self, assessment: Assessment) -> StoredAssessment:
        async with self._sessions() as session, session.begin():
            values = self._model_values(assessment)
            session.add(AssessmentModel(id=assessment.id, revision=0, **values))
            try:
                await session.flush()
            except IntegrityError as error:
                raise ActiveAssessmentExists("An active assessment already exists") from error
            if self._outbox is not None:
                await self._outbox.enqueue_assessment(session, assessment, 0)
            return StoredAssessment(assessment, token=0)

    async def save_assessment(
        self,
        stored: StoredAssessment,
        assessment: Assessment,
        event: AnalyticsEvent | None = None,
    ) -> StoredAssessment:
        if not isinstance(stored.token, int):
            raise TypeError("Invalid SQL persistence token")
        expected_revision = stored.token
        started = perf_counter()
        async with self._sessions() as session, session.begin():
            next_revision = expected_revision + 1
            statement = (
                update(AssessmentModel)
                .where(
                    AssessmentModel.id == assessment.id,
                    AssessmentModel.revision == expected_revision,
                )
                .values(revision=next_revision, **self._model_values(assessment))
                .returning(AssessmentModel.revision)
            )
            saved_revision = await session.scalar(statement)
            if saved_revision is None:
                raise StaleInput("This assessment changed; use the latest message")
            if self._outbox is not None:
                await self._outbox.enqueue_assessment(
                    session,
                    assessment,
                    saved_revision,
                )
            if self._transition_events is not None and event is not None:
                await self._transition_events.record_event(
                    session,
                    assessment,
                    event,
                )
            saved = StoredAssessment(assessment, token=saved_revision)
        _logger.info(
            "assessment_commit storage=%s duration_ms=%.1f",
            self._storage_label,
            (perf_counter() - started) * 1000,
        )
        return saved

    async def import_user(self, user: UserRecord) -> None:
        async with self._sessions() as session, session.begin():
            model = await session.get(UserModel, user.telegram_user_id)
            if model is None:
                session.add(
                    UserModel(
                        telegram_user_id=user.telegram_user_id,
                        language_code=user.language_code,
                        accepted_at=user.accepted_at,
                    )
                )
            else:
                model.language_code = user.language_code
                model.accepted_at = user.accepted_at

    async def import_assessment(
        self,
        stored: StoredAssessment,
        *,
        enqueue: bool = False,
    ) -> None:
        await self.import_assessments([stored], enqueue=enqueue)

    async def import_assessments(
        self,
        stored_assessments: list[StoredAssessment],
        *,
        enqueue: bool = False,
    ) -> None:
        if not stored_assessments:
            return
        values_list: list[dict[str, object]] = []
        for stored in stored_assessments:
            if not isinstance(stored.token, int):
                raise TypeError("Invalid SQL persistence token")
            values = {"id": stored.assessment.id, "revision": stored.token}
            values.update(self._model_values(stored.assessment))
            values_list.append(values)
        async with self._sessions() as session, session.begin():
            dialect_name = session.get_bind().dialect.name
            statement: Any
            if dialect_name == "postgresql":
                statement = postgresql_insert(AssessmentModel).values(values_list)
            elif dialect_name == "sqlite":
                statement = sqlite_insert(AssessmentModel).values(values_list)
            else:
                raise RuntimeError(f"Unsupported assessment database dialect: {dialect_name}")
            model_values = self._model_values(stored_assessments[0].assessment)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[AssessmentModel.id],
                    set_={key: getattr(statement.excluded, key) for key in model_values}
                    | {"revision": statement.excluded.revision},
                    where=AssessmentModel.revision < statement.excluded.revision,
                )
            )
            if enqueue and self._outbox is not None:
                for stored in stored_assessments:
                    await self._outbox.enqueue_assessment(
                        session,
                        stored.assessment,
                        cast(int, stored.token),
                    )

    async def import_users(self, users: list[UserRecord]) -> None:
        if not users:
            return
        async with self._sessions() as session, session.begin():
            dialect_name = session.get_bind().dialect.name
            statement: Any
            values = [
                {
                    "telegram_user_id": user.telegram_user_id,
                    "language_code": user.language_code,
                    "accepted_at": user.accepted_at,
                }
                for user in users
            ]
            if dialect_name == "postgresql":
                statement = postgresql_insert(UserModel).values(values)
            elif dialect_name == "sqlite":
                statement = sqlite_insert(UserModel).values(values)
            else:
                raise RuntimeError(f"Unsupported user database dialect: {dialect_name}")
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[UserModel.telegram_user_id],
                    set_={
                        "language_code": statement.excluded.language_code,
                        "accepted_at": statement.excluded.accepted_at,
                    },
                    where=UserModel.accepted_at <= statement.excluded.accepted_at,
                )
            )

    async def get_assessment(self, assessment_id: str) -> StoredAssessment | None:
        async with self._sessions() as session:
            model = await session.get(AssessmentModel, assessment_id)
            return self._to_stored(model) if model is not None else None

    async def get_active(self, telegram_user_id: int) -> StoredAssessment | None:
        statement = select(AssessmentModel).where(
            AssessmentModel.telegram_user_id == telegram_user_id,
            AssessmentModel.status == AssessmentStatus.ACTIVE.value,
        )
        async with self._sessions() as session:
            model = await session.scalar(statement)
            return self._to_stored(model) if model is not None else None

    async def list_completed(self, telegram_user_id: int) -> list[StoredAssessment]:
        statement = (
            select(AssessmentModel)
            .where(
                AssessmentModel.telegram_user_id == telegram_user_id,
                AssessmentModel.status == AssessmentStatus.COMPLETED.value,
            )
            .order_by(AssessmentModel.completed_at.desc())
        )
        return await self._list(statement)

    async def list_active_before(self, cutoff: datetime) -> list[StoredAssessment]:
        statement = select(AssessmentModel).where(
            AssessmentModel.status == AssessmentStatus.ACTIVE.value,
            AssessmentModel.updated_at < cutoff,
        )
        return await self._list(statement)

    async def list_all_active(self) -> list[StoredAssessment]:
        statement = select(AssessmentModel).where(
            AssessmentModel.status == AssessmentStatus.ACTIVE.value
        )
        return await self._list(statement)

    async def users_by_id(self, telegram_user_ids: list[int]) -> list[UserRecord]:
        if not telegram_user_ids:
            return []
        statement = select(UserModel).where(UserModel.telegram_user_id.in_(telegram_user_ids))
        async with self._sessions() as session:
            models = (await session.scalars(statement)).all()
        return [
            UserRecord(
                model.telegram_user_id,
                model.language_code,
                _as_utc(model.accepted_at),
            )
            for model in models
        ]

    async def delete_assessment(self, assessment_id: str, telegram_user_id: int) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                delete(AssessmentModel).where(
                    AssessmentModel.id == assessment_id,
                    AssessmentModel.telegram_user_id == telegram_user_id,
                )
            )

    async def _list(self, statement: Select[tuple[AssessmentModel]]) -> list[StoredAssessment]:
        async with self._sessions() as session:
            models = (await session.scalars(statement)).all()
            return [self._to_stored(model) for model in models]

    @staticmethod
    def _model_values(assessment: Assessment) -> dict[str, object]:
        return {
            "telegram_user_id": assessment.telegram_user_id,
            "questionnaire_id": assessment.questionnaire_id,
            "questionnaire_version": assessment.questionnaire_version,
            "status": assessment.status.value,
            "answers": dict(assessment.answers),
            "scores": {trait.value: value for trait, value in assessment.scores},
            "facet_scores": dict(assessment.facet_scores),
            "started_at": assessment.started_at,
            "updated_at": assessment.updated_at,
            "completed_at": assessment.completed_at,
        }

    @staticmethod
    def _to_domain(model: AssessmentModel) -> Assessment:
        return Assessment(
            id=model.id,
            telegram_user_id=model.telegram_user_id,
            questionnaire_id=model.questionnaire_id,
            questionnaire_version=model.questionnaire_version,
            status=AssessmentStatus(model.status),
            answers=tuple(model.answers.items()),
            scores=tuple((Trait(name), value) for name, value in model.scores.items()),
            facet_scores=tuple(model.facet_scores.items()),
            started_at=_as_utc(model.started_at),
            updated_at=_as_utc(model.updated_at),
            completed_at=_as_utc(model.completed_at) if model.completed_at else None,
        )

    @classmethod
    def _to_stored(cls, model: AssessmentModel) -> StoredAssessment:
        return StoredAssessment(cls._to_domain(model), token=model.revision)


class SqlAnalyticsRepository:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        outbox: SyncOutboxWriter | None = None,
    ) -> None:
        self._sessions = sessions
        self._outbox = outbox

    async def add_event(self, event: AnalyticsRecord) -> None:
        async with self._sessions() as session, session.begin():
            await self.add_event_in_session(session, event)

    async def add_event_in_session(
        self,
        session: AsyncSession,
        event: AnalyticsRecord,
    ) -> None:
        dialect_name = session.get_bind().dialect.name
        event_statement: Any
        values = {
            "event_id": event.event_id,
            "user_ref": event.user_ref,
            "name": event.name,
            "occurred_at": event.occurred_at,
            "properties": dict(event.properties),
        }
        if dialect_name == "postgresql":
            event_statement = postgresql_insert(AnalyticsEventModel).values(**values)
        elif dialect_name == "sqlite":
            event_statement = sqlite_insert(AnalyticsEventModel).values(**values)
        else:
            raise RuntimeError(f"Unsupported analytics database dialect: {dialect_name}")
        inserted = await session.scalar(
            event_statement.on_conflict_do_nothing(
                index_elements=[AnalyticsEventModel.event_id]
            ).returning(AnalyticsEventModel.event_id)
        )
        if inserted is None:
            return
        values = {"name": event.name, "count": 1}
        counter_statement: Any
        if dialect_name == "postgresql":
            counter_statement = postgresql_insert(AnalyticsCounterModel).values(**values)
        else:
            counter_statement = sqlite_insert(AnalyticsCounterModel).values(**values)
        await session.execute(
            counter_statement.on_conflict_do_update(
                index_elements=[AnalyticsCounterModel.name],
                set_={"count": AnalyticsCounterModel.count + 1},
            )
        )
        if self._outbox is not None:
            await self._outbox.enqueue_analytics(session, event)

    async def get_event(self, event_id: str) -> AnalyticsRecord | None:
        statement = select(AnalyticsEventModel).where(AnalyticsEventModel.event_id == event_id)
        async with self._sessions() as session:
            model = await session.scalar(statement)
        if model is None:
            return None
        return AnalyticsRecord(
            event_id=model.event_id,
            user_ref=model.user_ref,
            name=model.name,
            occurred_at=_as_utc(model.occurred_at),
            properties=dict(model.properties),
        )

    async def add_events(self, events: list[AnalyticsRecord]) -> int:
        if not events:
            return 0
        async with self._sessions() as session, session.begin():
            dialect_name = session.get_bind().dialect.name
            statement: Any
            counter_statement: Any
            values = [
                {
                    "event_id": event.event_id,
                    "user_ref": event.user_ref,
                    "name": event.name,
                    "occurred_at": event.occurred_at,
                    "properties": dict(event.properties),
                }
                for event in events
            ]
            if dialect_name == "postgresql":
                statement = postgresql_insert(AnalyticsEventModel).values(values)
            elif dialect_name == "sqlite":
                statement = sqlite_insert(AnalyticsEventModel).values(values)
            else:
                raise RuntimeError(f"Unsupported analytics database dialect: {dialect_name}")
            inserted = (
                await session.execute(
                    statement.on_conflict_do_nothing(
                        index_elements=[AnalyticsEventModel.event_id]
                    ).returning(AnalyticsEventModel.event_id, AnalyticsEventModel.name)
                )
            ).all()
            if not inserted:
                return 0
            counts: dict[str, int] = {}
            for _, name in inserted:
                counts[name] = counts.get(name, 0) + 1
            counter_values = [{"name": name, "count": count} for name, count in counts.items()]
            if dialect_name == "postgresql":
                counter_statement = postgresql_insert(AnalyticsCounterModel).values(counter_values)
            else:
                counter_statement = sqlite_insert(AnalyticsCounterModel).values(counter_values)
            await session.execute(
                counter_statement.on_conflict_do_update(
                    index_elements=[AnalyticsCounterModel.name],
                    set_={"count": AnalyticsCounterModel.count + counter_statement.excluded.count},
                )
            )
            return len(inserted)

    async def delete_events(self, event_ids: list[str]) -> None:
        if not event_ids:
            return
        async with self._sessions() as session, session.begin():
            await session.execute(
                delete(AnalyticsEventModel).where(AnalyticsEventModel.event_id.in_(event_ids))
            )

    async def counts(self) -> dict[str, int]:
        statement = select(AnalyticsCounterModel).order_by(AnalyticsCounterModel.name)
        async with self._sessions() as session:
            counters = (await session.scalars(statement)).all()
            return {counter.name: counter.count for counter in counters}

    async def delete_before(self, cutoff: datetime) -> int:
        condition = AnalyticsEventModel.occurred_at < cutoff
        async with self._sessions() as session, session.begin():
            count = await session.scalar(
                select(func.count(AnalyticsEventModel.id)).where(condition)
            )
            await session.execute(delete(AnalyticsEventModel).where(condition))
            return count or 0


class SqlUserDataRightsRepository:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        outbox: SyncOutboxWriter | None = None,
    ) -> None:
        self._sessions = sessions
        self._outbox = outbox

    async def load_user_data(self, telegram_user_id: int, user_ref: str) -> StoredUserData:
        return (await self.load_user_data_with_revisions(telegram_user_id, user_ref)).data

    async def load_user_data_with_revisions(
        self, telegram_user_id: int, user_ref: str
    ) -> RevisionedUserData:
        assessments_statement = (
            select(AssessmentModel)
            .where(AssessmentModel.telegram_user_id == telegram_user_id)
            .order_by(AssessmentModel.started_at.desc())
        )
        analytics_statement = (
            select(AnalyticsEventModel)
            .where(AnalyticsEventModel.user_ref == user_ref)
            .order_by(AnalyticsEventModel.occurred_at)
        )
        async with self._sessions() as session:
            tombstone = await session.get(DeletionTombstoneModel, telegram_user_id)
            if tombstone is not None:
                return RevisionedUserData(StoredUserData(None, (), ()), {})
            user_model = await session.get(UserModel, telegram_user_id)
            assessment_models = (await session.scalars(assessments_statement)).all()
            analytics_models = (await session.scalars(analytics_statement)).all()

        user = (
            UserRecord(
                user_model.telegram_user_id,
                user_model.language_code,
                _as_utc(user_model.accepted_at),
            )
            if user_model is not None
            else None
        )
        assessments = tuple(
            SqlAssessmentRepository._to_domain(model) for model in assessment_models
        )
        analytics = tuple(
            AnalyticsRecord(
                event_id=model.event_id,
                user_ref=model.user_ref,
                name=model.name,
                occurred_at=_as_utc(model.occurred_at),
                properties=dict(model.properties),
            )
            for model in analytics_models
        )
        return RevisionedUserData(
            StoredUserData(user, assessments, analytics),
            {model.id: model.revision for model in assessment_models},
        )

    async def delete_user_data(self, telegram_user_id: int, user_ref: str) -> None:
        await self.purge_user_data(telegram_user_id, user_ref)

    async def mark_deletion(self, telegram_user_id: int, user_ref: str, now: datetime) -> None:
        async with self._sessions() as session, session.begin():
            tombstone = await session.get(DeletionTombstoneModel, telegram_user_id)
            if tombstone is None:
                session.add(
                    DeletionTombstoneModel(
                        telegram_user_id=telegram_user_id,
                        user_ref=user_ref,
                        created_at=now,
                    )
                )
            if self._outbox is not None:
                await self._outbox.enqueue_delete_user(session, telegram_user_id, now)

    async def finalize_deletion(self, telegram_user_id: int) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                delete(DeletionTombstoneModel).where(
                    DeletionTombstoneModel.telegram_user_id == telegram_user_id
                )
            )
            await session.execute(
                delete(SyncOutboxModel).where(
                    SyncOutboxModel.kind == SyncJobKind.DELETE_USER,
                    SyncOutboxModel.entity_id == str(telegram_user_id),
                )
            )

    async def deletion_user_ref(self, telegram_user_id: int) -> str | None:
        async with self._sessions() as session:
            tombstone = await session.get(DeletionTombstoneModel, telegram_user_id)
        return tombstone.user_ref if tombstone is not None else None

    async def purge_user_data(self, telegram_user_id: int, user_ref: str) -> None:
        async with self._sessions() as session, session.begin():
            assessment_ids = list(
                await session.scalars(
                    select(AssessmentModel.id).where(
                        AssessmentModel.telegram_user_id == telegram_user_id
                    )
                )
            )
            event_ids = list(
                await session.scalars(
                    select(AnalyticsEventModel.event_id).where(
                        AnalyticsEventModel.user_ref == user_ref
                    )
                )
            )
            conditions = [
                (SyncOutboxModel.kind == SyncJobKind.USER)
                & (SyncOutboxModel.entity_id == str(telegram_user_id))
            ]
            if assessment_ids:
                conditions.append(
                    (SyncOutboxModel.kind == SyncJobKind.ASSESSMENT)
                    & SyncOutboxModel.entity_id.in_(assessment_ids)
                )
            if event_ids:
                conditions.append(
                    (SyncOutboxModel.kind == SyncJobKind.ANALYTICS)
                    & SyncOutboxModel.entity_id.in_(event_ids)
                )
            await session.execute(delete(SyncOutboxModel).where(or_(*conditions)))
            await session.execute(
                delete(AssessmentModel).where(AssessmentModel.telegram_user_id == telegram_user_id)
            )
            await session.execute(
                delete(AnalyticsEventModel).where(AnalyticsEventModel.user_ref == user_ref)
            )
            await session.execute(
                delete(UserModel).where(UserModel.telegram_user_id == telegram_user_id)
            )

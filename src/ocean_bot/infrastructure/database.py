from __future__ import annotations

from datetime import UTC, datetime

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
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import Select

from ocean_bot.application.analytics import AnalyticsRecord
from ocean_bot.application.assessments import ActiveAssessmentExists, StaleInput
from ocean_bot.domain.assessment import Assessment, AssessmentStatus, UserRecord
from ocean_bot.domain.questionnaire import Trait


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
    user_ref: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    properties: Mapped[dict[str, str]] = mapped_column(JSON)


class AnalyticsCounterModel(Base):
    __tablename__ = "analytics_counters"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False)


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


class SqlAssessmentRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

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

    async def save_assessment(self, assessment: Assessment) -> None:
        async with self._sessions() as session, session.begin():
            model = await session.get(AssessmentModel, assessment.id)
            values = self._model_values(assessment)
            if model is None:
                session.add(
                    AssessmentModel(id=assessment.id, revision=assessment.revision, **values)
                )
                try:
                    await session.flush()
                except IntegrityError as error:
                    raise ActiveAssessmentExists("An active assessment already exists") from error
                return
            next_revision = assessment.revision + 1
            statement = (
                update(AssessmentModel)
                .where(
                    AssessmentModel.id == assessment.id,
                    AssessmentModel.revision == assessment.revision,
                )
                .values(revision=next_revision, **values)
                .returning(AssessmentModel.revision)
            )
            saved_revision = await session.scalar(statement)
            if saved_revision is None:
                raise StaleInput("This assessment changed; use the latest message")
            assessment.revision = saved_revision

    async def get_assessment(self, assessment_id: str) -> Assessment | None:
        async with self._sessions() as session:
            model = await session.get(AssessmentModel, assessment_id)
            return self._to_domain(model) if model is not None else None

    async def get_active(self, telegram_user_id: int) -> Assessment | None:
        statement = select(AssessmentModel).where(
            AssessmentModel.telegram_user_id == telegram_user_id,
            AssessmentModel.status == AssessmentStatus.ACTIVE.value,
        )
        async with self._sessions() as session:
            model = await session.scalar(statement)
            return self._to_domain(model) if model is not None else None

    async def list_completed(self, telegram_user_id: int) -> list[Assessment]:
        statement = (
            select(AssessmentModel)
            .where(
                AssessmentModel.telegram_user_id == telegram_user_id,
                AssessmentModel.status == AssessmentStatus.COMPLETED.value,
            )
            .order_by(AssessmentModel.completed_at.desc())
        )
        return await self._list(statement)

    async def list_all(self, telegram_user_id: int) -> list[Assessment]:
        statement = (
            select(AssessmentModel)
            .where(AssessmentModel.telegram_user_id == telegram_user_id)
            .order_by(AssessmentModel.started_at.desc())
        )
        return await self._list(statement)

    async def list_active_before(self, cutoff: datetime) -> list[Assessment]:
        statement = select(AssessmentModel).where(
            AssessmentModel.status == AssessmentStatus.ACTIVE.value,
            AssessmentModel.updated_at < cutoff,
        )
        return await self._list(statement)

    async def delete_assessment(self, assessment_id: str, telegram_user_id: int) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                delete(AssessmentModel).where(
                    AssessmentModel.id == assessment_id,
                    AssessmentModel.telegram_user_id == telegram_user_id,
                )
            )

    async def delete_user(self, telegram_user_id: int) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                delete(AssessmentModel).where(AssessmentModel.telegram_user_id == telegram_user_id)
            )
            await session.execute(
                delete(UserModel).where(UserModel.telegram_user_id == telegram_user_id)
            )

    async def _list(self, statement: Select[tuple[AssessmentModel]]) -> list[Assessment]:
        async with self._sessions() as session:
            models = (await session.scalars(statement)).all()
            return [self._to_domain(model) for model in models]

    @staticmethod
    def _model_values(assessment: Assessment) -> dict[str, object]:
        return {
            "telegram_user_id": assessment.telegram_user_id,
            "questionnaire_id": assessment.questionnaire_id,
            "questionnaire_version": assessment.questionnaire_version,
            "status": assessment.status.value,
            "answers": dict(assessment.answers),
            "scores": {trait.value: value for trait, value in assessment.scores.items()},
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
            answers=dict(model.answers),
            scores={Trait(name): value for name, value in model.scores.items()},
            started_at=_as_utc(model.started_at),
            updated_at=_as_utc(model.updated_at),
            completed_at=_as_utc(model.completed_at) if model.completed_at else None,
            revision=model.revision,
        )


class SqlAnalyticsRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def add_event(self, event: AnalyticsRecord) -> None:
        async with self._sessions() as session, session.begin():
            session.add(
                AnalyticsEventModel(
                    user_ref=event.user_ref,
                    name=event.name,
                    occurred_at=event.occurred_at,
                    properties=dict(event.properties),
                )
            )
            dialect_name = session.get_bind().dialect.name
            values = {"name": event.name, "count": 1}
            if dialect_name == "postgresql":
                postgresql_statement = postgresql_insert(AnalyticsCounterModel).values(**values)
                await session.execute(
                    postgresql_statement.on_conflict_do_update(
                        index_elements=[AnalyticsCounterModel.name],
                        set_={"count": AnalyticsCounterModel.count + 1},
                    )
                )
            elif dialect_name == "sqlite":
                sqlite_statement = sqlite_insert(AnalyticsCounterModel).values(**values)
                await session.execute(
                    sqlite_statement.on_conflict_do_update(
                        index_elements=[AnalyticsCounterModel.name],
                        set_={"count": AnalyticsCounterModel.count + 1},
                    )
                )
            else:
                raise RuntimeError(f"Unsupported analytics database dialect: {dialect_name}")

    async def list_for_user(self, user_ref: str) -> list[AnalyticsRecord]:
        statement = (
            select(AnalyticsEventModel)
            .where(AnalyticsEventModel.user_ref == user_ref)
            .order_by(AnalyticsEventModel.occurred_at)
        )
        async with self._sessions() as session:
            models = (await session.scalars(statement)).all()
            return [
                AnalyticsRecord(
                    user_ref=model.user_ref,
                    name=model.name,
                    occurred_at=_as_utc(model.occurred_at),
                    properties=dict(model.properties),
                )
                for model in models
            ]

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

    async def delete_for_user(self, user_ref: str) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                delete(AnalyticsEventModel).where(AnalyticsEventModel.user_ref == user_ref)
            )


class SqlPrivacyRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def delete_user_data(self, telegram_user_id: int, user_ref: str) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                delete(AssessmentModel).where(AssessmentModel.telegram_user_id == telegram_user_id)
            )
            await session.execute(
                delete(AnalyticsEventModel).where(AnalyticsEventModel.user_ref == user_ref)
            )
            await session.execute(
                delete(UserModel).where(UserModel.telegram_user_id == telegram_user_id)
            )

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import EditMessageText
from sqlalchemy.exc import SQLAlchemyError

from ocean_bot.application.analytics import AnalyticsEvent
from ocean_bot.application.assessments import StoredAssessment
from ocean_bot.application.data_rights import UserReferences
from ocean_bot.domain.assessment import Assessment, UserRecord
from ocean_bot.domain.catalog import load_bundled_questionnaires
from ocean_bot.infrastructure.database import (
    Base,
    SqlAnalyticsRepository,
    SqlAssessmentRepository,
    session_factory,
)
from ocean_bot.infrastructure.local import create_local_engine
from ocean_bot.infrastructure.replication import (
    LocalAssessmentEventWriter,
    SyncOutboxRepository,
    SyncService,
)
from ocean_bot.telegram.delivery import TelegramDelivery

_logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RunMetrics:
    callback_latencies_ms: list[float]
    errors: Counter[str]
    telegram_throttles: int = 0
    scheduled: int = 0
    completed: int = 0
    max_backlog: int = 0


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * percentile))
    return ordered[index]


async def _run(args: argparse.Namespace) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="ocean-load-test-") as directory:
        root = Path(directory)
        local_engine = create_local_engine(f"sqlite+aiosqlite:///{root / 'local.db'}")
        remote_engine = create_local_engine(f"sqlite+aiosqlite:///{root / 'remote.db'}")
        async with local_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with remote_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        local_sessions = session_factory(local_engine)
        remote_sessions = session_factory(remote_engine)
        outbox = SyncOutboxRepository(local_sessions)
        references = UserReferences("load-test-pepper-12345")
        local_analytics = SqlAnalyticsRepository(local_sessions, outbox=outbox)
        local_assessments = SqlAssessmentRepository(
            local_sessions,
            outbox=outbox,
            transition_events=LocalAssessmentEventWriter(local_analytics, references),
        )
        remote_assessments = SqlAssessmentRepository(remote_sessions)
        remote_analytics = SqlAnalyticsRepository(remote_sessions)
        sync = SyncService(
            outbox,
            local_assessments,
            local_analytics,
            remote_assessments,
            remote_analytics,
            references,
        )
        questionnaire = load_bundled_questionnaires()["mini-ipip-20"]
        now = datetime.now(UTC)
        users = [UserRecord(10_000_000 + i, "en", now) for i in range(args.users)]
        assessments = [
            Assessment.start(str(uuid4()), user.telegram_user_id, questionnaire, now)
            for user in users
        ]
        stored = [StoredAssessment(assessment, 0) for assessment in assessments]
        await local_assessments.import_users(users)
        await local_assessments.import_assessments(stored)
        await remote_assessments.import_users(users)

        metrics = RunMetrics([], Counter())
        user_locks = [asyncio.Lock() for _ in stored]
        telegram_window_started = perf_counter()
        telegram_window_calls = 0

        async def fake_telegram_edit() -> None:
            nonlocal telegram_window_started, telegram_window_calls
            now_value = perf_counter()
            if now_value - telegram_window_started >= 1:
                telegram_window_started = now_value
                telegram_window_calls = 0
            if telegram_window_calls >= args.telegram_rate:
                metrics.telegram_throttles += 1
                raise TelegramRetryAfter(
                    method=EditMessageText(chat_id=1, message_id=1, text="load test"),
                    message="synthetic flood control",
                    retry_after=0.05,
                )
            telegram_window_calls += 1

        delivery = TelegramDelivery(
            max_in_flight=args.delivery_concurrency,
            max_per_second=args.delivery_rate,
        )

        async def answer_one(index: int) -> None:
            slot = index % len(stored)
            started = perf_counter()
            try:
                async with user_locks[slot]:
                    current = await local_assessments.get_assessment(stored[slot].assessment.id)
                    if current is None:
                        raise RuntimeError("assessment disappeared")
                    item = questionnaire.items[len(current.assessment.answers)]
                    updated = current.assessment.answer(
                        questionnaire,
                        item.id,
                        (index % 5) + 1,
                        datetime.now(UTC),
                    )
                    event = AnalyticsEvent(
                        "answer_recorded",
                        datetime.now(UTC),
                        {"questionnaire_id": questionnaire.id},
                    )
                    stored[slot] = await local_assessments.save_assessment(
                        current,
                        updated,
                        event,
                    )
                    await delivery.call(fake_telegram_edit, "message.edit_text")
                    metrics.completed += 1
            except (SQLAlchemyError, OSError, RuntimeError) as error:
                metrics.errors[type(error).__name__] += 1
            finally:
                metrics.callback_latencies_ms.append((perf_counter() - started) * 1000)

        stop_sync = asyncio.Event()

        async def sync_loop() -> None:
            while not stop_sync.is_set():
                try:
                    await sync.run_once(datetime.now(UTC))
                    metrics.max_backlog = max(metrics.max_backlog, await outbox.pending_count())
                except Exception:
                    _logger.exception("Load-test synchronization cycle failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_sync.wait(), timeout=1)

        sync_task = asyncio.create_task(sync_loop())
        tasks: list[asyncio.Task[None]] = []
        started = perf_counter()
        next_due = started
        while perf_counter() - started < args.duration:
            tasks.append(asyncio.create_task(answer_one(metrics.scheduled)))
            metrics.scheduled += 1
            next_due += 1 / args.rate
            await asyncio.sleep(max(0, next_due - perf_counter()))
        await asyncio.gather(*tasks)
        stop_sync.set()
        await sync_task
        metrics.max_backlog = max(metrics.max_backlog, await outbox.pending_count())
        final_backlog = await outbox.pending_count()
        await local_engine.dispose()
        await remote_engine.dispose()

        return {
            "rate_answers_per_s": args.rate,
            "duration_s": args.duration,
            "active_users": args.users,
            "scheduled": metrics.scheduled,
            "completed": metrics.completed,
            "errors": dict(metrics.errors),
            "callback_p50_ms": round(_percentile(metrics.callback_latencies_ms, 0.50), 1),
            "callback_p95_ms": round(_percentile(metrics.callback_latencies_ms, 0.95), 1),
            "callback_p99_ms": round(_percentile(metrics.callback_latencies_ms, 0.99), 1),
            "telegram_throttles": metrics.telegram_throttles,
            "max_sync_backlog": metrics.max_backlog,
            "final_sync_backlog": final_backlog,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Synthetic local-first concurrency test")
    parser.add_argument("--rate", type=float, required=True, help="Answer callbacks per second")
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--users", type=int, default=200)
    parser.add_argument("--telegram-rate", type=int, default=30)
    parser.add_argument(
        "--delivery-rate",
        type=float,
        default=25.0,
        help="Proactive Telegram delivery pace; keep below the synthetic limit",
    )
    parser.add_argument("--delivery-concurrency", type=int, default=20)
    args = parser.parse_args()
    if (
        args.rate <= 0
        or args.duration <= 0
        or args.users <= 0
        or args.telegram_rate <= 0
        or args.delivery_rate <= 0
    ):
        parser.error("rates, duration, and users must be positive")
    logging.basicConfig(level=logging.ERROR)
    print(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update

from ocean_bot.application.assessments import AssessmentService
from ocean_bot.application.interactions import (
    AssessmentInteraction,
    CallbackIntent,
    InteractionOutcome,
)
from ocean_bot.domain.questionnaire import Item, Questionnaire, Trait
from ocean_bot.infrastructure.memory import InMemoryAssessmentRepository
from ocean_bot.telegram import handlers
from ocean_bot.telegram.callbacks import decode_callback, encode_callback
from ocean_bot.telegram.handlers import admin_stats_command, handle_callback
from ocean_bot.telegram.middleware import UserActionLocks


def _interaction(
    questionnaires: dict[str, Questionnaire],
) -> tuple[AssessmentInteraction, SimpleNamespace]:
    analytics = SimpleNamespace(record=AsyncMock())
    interaction = AssessmentInteraction(
        AssessmentService(InMemoryAssessmentRepository(), questionnaires),
        analytics,  # type: ignore[arg-type]
        questionnaires,
    )
    return interaction, analytics


async def test_start_interaction_requests_age_and_privacy_consent() -> None:
    interaction, _ = _interaction({})

    outcome = await interaction.start(42)

    assert "13 or older" in outcome.replies[0].text
    assert "analytics" in outcome.replies[0].text
    assert encode_callback(outcome.replies[0].buttons[0][0].intent) == "consent:accept"
    assert encode_callback(outcome.replies[0].buttons[0][1].intent) == "consent:decline"


async def test_stale_answer_intent_gets_an_inactive_alert() -> None:
    questionnaire = Questionnaire(
        "quick",
        "1",
        "Quick",
        (
            Item("e1", "First", Trait.EXTRAVERSION, False),
            Item("e2", "Second", Trait.EXTRAVERSION, False),
        ),
    )
    interaction, analytics = _interaction({"quick": questionnaire})
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await interaction.accept_consent(42, "en", now)
    started = await interaction.choose_mode(42, "quick", now)
    assessment_id = started.replies[0].buttons[0][0].intent.values[0]
    await interaction.answer(42, assessment_id, "e1", 5, now)
    analytics.record.reset_mock()

    outcome = await interaction.answer(42, assessment_id, "e1", 4, now)

    assert outcome.callback_alert is True
    analytics.record.assert_not_awaited()


async def test_telegram_adapter_decodes_stale_answer_and_acknowledges_alert() -> None:
    questionnaire = Questionnaire(
        "quick",
        "1",
        "Quick",
        (
            Item("e1", "First", Trait.EXTRAVERSION, False),
            Item("e2", "Second", Trait.EXTRAVERSION, False),
        ),
    )
    interaction, analytics = _interaction({"quick": questionnaire})
    now = datetime.now(UTC)
    await interaction.accept_consent(42, "en", now)
    started = await interaction.choose_mode(42, "quick", now)
    assessment_id = started.replies[0].buttons[0][0].intent.values[0]
    await interaction.answer(42, assessment_id, "e1", 5, now)
    analytics.record.reset_mock()
    callback = SimpleNamespace(
        data=encode_callback(CallbackIntent("answer", (assessment_id, "e1", "4"))),
        from_user=SimpleNamespace(id=42, language_code="en"),
        message=None,
        answer=AsyncMock(),
    )

    await handle_callback(
        callback,
        interaction,
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    callback.answer.assert_awaited_once_with()
    analytics.record.assert_not_awaited()


async def test_admin_stats_are_allowlisted() -> None:
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=99, language_code="en"),
        answer=AsyncMock(),
    )
    analytics = SimpleNamespace(stats=AsyncMock(return_value={"assessment_completed": 2}))

    await admin_stats_command(message, admin_user_id=42, analytics=analytics)

    message.answer.assert_awaited_once_with("Command not available.")
    analytics.stats.assert_not_awaited()


async def test_results_outcome_exposes_delete_for_older_results() -> None:
    questionnaire = Questionnaire(
        "quick",
        "1",
        "Quick",
        (Item("e1", "First", Trait.EXTRAVERSION, False),),
    )
    interaction, _ = _interaction({"quick": questionnaire})
    first_at = datetime(2026, 1, 1, tzinfo=UTC)
    second_at = datetime(2026, 1, 2, tzinfo=UTC)
    await interaction.accept_consent(42, "en", first_at)
    first = await interaction.choose_mode(42, "quick", first_at)
    first_answer = first.replies[0].buttons[0][0].intent
    await interaction.answer(42, *first_answer.values[:2], 1, first_at)
    second = await interaction.choose_mode(42, "quick", second_at)
    second_answer = second.replies[0].buttons[0][0].intent
    await interaction.answer(42, *second_answer.values[:2], 1, second_at)

    outcome = await interaction.results(42)

    assert len(outcome.replies) == 2
    older_delete = outcome.replies[1].buttons[0][1].intent
    assert older_delete == CallbackIntent("result.delete", (first_answer.values[0],))


def test_callback_codec_round_trips_transport_formats() -> None:
    cases = {
        CallbackIntent("mode", ("quick",)): "mode:quick",
        CallbackIntent("answer", ("assessment", "e1", "5")): "a:assessment:e1:5",
        CallbackIntent("back", ("assessment",)): "b:assessment",
        CallbackIntent("cancel", ("assessment",)): "c:assessment",
        CallbackIntent("history.page", ("2",)): "history:page:2",
        CallbackIntent("result.view", ("assessment",)): "result:view:assessment",
        CallbackIntent("result.delete", ("assessment",)): "result:delete:assessment",
        CallbackIntent("result.confirm", ("assessment",)): "result:confirm:assessment",
        CallbackIntent("result.keep"): "result:keep",
        CallbackIntent("delete.confirm"): "delete:confirm",
        CallbackIntent("delete.cancel"): "delete:cancel",
    }

    assert {intent: encode_callback(intent) for intent in cases} == cases
    assert [decode_callback(value) for value in cases.values()] == list(cases)


def _update(chat_type: str, text: str) -> Update:
    return Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 0,
                "chat": {"id": -100 if chat_type != "private" else 42, "type": chat_type},
                "from": {"id": 42, "is_bot": False, "first_name": "Test"},
                "text": text,
            },
        }
    )


@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
async def test_personal_data_commands_are_refused_outside_private_chats(
    chat_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    delivery = SimpleNamespace(call=AsyncMock())
    monkeypatch.setattr(handlers, "_message_delivery", delivery)
    data_rights = SimpleNamespace(export_user=AsyncMock())
    dispatcher = Dispatcher(data_rights=data_rights)
    dispatcher.include_router(handlers.create_router())

    await dispatcher.feed_update(Bot("123456:test-token"), _update(chat_type, "/export"))

    data_rights.export_user.assert_not_awaited()
    operation = delivery.call.await_args.args[0]
    assert operation.func.__name__ == "answer"
    assert "private chat" in operation.args[0]


async def test_private_export_still_delivers_document(monkeypatch: pytest.MonkeyPatch) -> None:
    delivery = SimpleNamespace(call=AsyncMock())
    monkeypatch.setattr(handlers, "_message_delivery", delivery)
    export = SimpleNamespace(json_bytes=lambda: b"{}")
    data_rights = SimpleNamespace(export_user=AsyncMock(return_value=export))
    dispatcher = Dispatcher(data_rights=data_rights)
    dispatcher.include_router(handlers.create_router())

    await dispatcher.feed_update(Bot("123456:test-token"), _update("private", "/export"))

    data_rights.export_user.assert_awaited_once_with(42)
    assert delivery.call.await_args.args[1] == "message.answer_document"


async def test_repeated_back_callback_removes_only_one_answer() -> None:
    questionnaire = Questionnaire(
        "quick",
        "1",
        "Quick",
        tuple(Item(f"e{index}", "Item", Trait.EXTRAVERSION, False) for index in range(1, 5)),
    )
    service = AssessmentService(InMemoryAssessmentRepository(), {"quick": questionnaire})
    interaction = AssessmentInteraction(
        service,
        SimpleNamespace(record=AsyncMock()),  # type: ignore[arg-type]
        {"quick": questionnaire},
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await interaction.accept_consent(42, "en", now)
    outcome = await interaction.choose_mode(42, "quick", now)
    assessment_id = outcome.replies[0].buttons[0][0].intent.values[0]
    for item_id in ("e1", "e2", "e3"):
        outcome = await interaction.answer(42, assessment_id, item_id, 3, now)
    back = outcome.replies[0].buttons[1][0].intent
    assert back == CallbackIntent("back", (assessment_id, "3"))

    first = await interaction.back(42, assessment_id, now, int(back.values[1]))
    repeated = await interaction.back(42, assessment_id, now, int(back.values[1]))

    assert first.callback_alert is False
    assert repeated.callback_alert is True
    resumed = await service.resume(42, now)
    assert resumed is not None and resumed.answered_count == 2


async def test_callback_delivery_finishes_before_the_next_callback_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    first_delivery_started = asyncio.Event()
    release_first_delivery = asyncio.Event()

    async def dispatch(callback: SimpleNamespace, *_: object) -> InteractionOutcome:
        events.append(f"dispatch:{callback.data}")
        return InteractionOutcome(())

    async def deliver(callback: SimpleNamespace, _: InteractionOutcome) -> None:
        if callback.data == "result:keep" and not first_delivery_started.is_set():
            first_delivery_started.set()
            await release_first_delivery.wait()
        events.append(f"deliver:{callback.data}")

    monkeypatch.setattr(handlers, "_dispatch_callback", dispatch)
    monkeypatch.setattr(handlers, "_deliver_callback", deliver)
    monkeypatch.setattr(handlers, "_callback_delivery", SimpleNamespace(call=AsyncMock()))
    locks = UserActionLocks()

    def callback(data: str) -> SimpleNamespace:
        return SimpleNamespace(data=data, from_user=SimpleNamespace(id=42), answer=AsyncMock())

    first = asyncio.create_task(
        handle_callback(callback("result:keep"), None, None, locks)  # type: ignore[arg-type]
    )
    await asyncio.wait_for(first_delivery_started.wait(), 5)
    second = asyncio.create_task(
        handle_callback(callback("delete:cancel"), None, None, locks)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.05)
    release_first_delivery.set()
    await asyncio.wait_for(asyncio.gather(first, second), 5)

    assert events == [
        "dispatch:result:keep",
        "deliver:result:keep",
        "dispatch:delete:cancel",
        "deliver:delete:cancel",
    ]


async def test_callback_without_a_message_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    alerts = SimpleNamespace(call=AsyncMock())
    monkeypatch.setattr(handlers, "_callback_delivery", alerts)
    data_rights = SimpleNamespace(delete_user=AsyncMock())
    dispatcher = Dispatcher(data_rights=data_rights, interaction=SimpleNamespace())
    dispatcher.include_router(handlers.create_router())
    update = Update.model_validate(
        {
            "update_id": 1,
            "callback_query": {
                "id": "1",
                "from": {"id": 42, "is_bot": False, "first_name": "Test"},
                "chat_instance": "1",
                "inline_message_id": "inline",
                "data": "delete:confirm",
            },
        }
    )

    await dispatcher.feed_update(Bot("123456:test-token"), update)

    data_rights.delete_user.assert_not_awaited()
    assert "private chat" in alerts.call.await_args.args[0].args[0]


async def test_commands_hold_the_users_action_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    delivery = SimpleNamespace(call=AsyncMock())
    monkeypatch.setattr(handlers, "_message_delivery", delivery)
    locks = UserActionLocks()
    held: list[bool] = []

    async def export_user(user_id: int) -> SimpleNamespace:
        held.append(locks._locks[user_id].locked())
        return SimpleNamespace(json_bytes=lambda: b"{}")

    dispatcher = Dispatcher(
        data_rights=SimpleNamespace(export_user=export_user), action_locks=locks
    )
    dispatcher.include_router(handlers.create_router())

    await dispatcher.feed_update(Bot("123456:test-token"), _update("private", "/export"))

    assert held == [True]

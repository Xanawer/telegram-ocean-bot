from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ocean_bot.application.analytics import AnalyticsService
from ocean_bot.application.assessments import (
    ActiveAssessmentExists,
    AssessmentNotFound,
    AssessmentService,
    ConsentRequired,
    StaleInput,
)
from ocean_bot.application.privacy import PrivacyService
from ocean_bot.domain.assessment import AssessmentStatus, AssessmentView
from ocean_bot.domain.questionnaire import Questionnaire
from ocean_bot.telegram.ui import render_question, render_results

router = Router(name="ocean-bot")

_PRIVACY_SUMMARY = (
    "Big Five self-reflection for people aged 13 or older.\n\n"
    "The bot stores your Telegram ID, answers, results, timestamps, navigation events, "
    "and sanitized errors. Required analytics support operation and product improvement. "
    "Use /privacy for retention and provider details. Results are not clinical advice."
)


def _consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="I am 13+ and accept",
                    callback_data="consent:accept",
                ),
                InlineKeyboardButton(text="Decline", callback_data="consent:decline"),
            ]
        ]
    )


def _mode_keyboard(questionnaires: Mapping[str, Questionnaire]) -> InlineKeyboardMarkup:
    rows = []
    for questionnaire_id in ("mini-ipip-20", "ipip-50"):
        questionnaire = questionnaires.get(questionnaire_id)
        if questionnaire is None:
            continue
        minutes = "3-5 min" if len(questionnaire.items) == 20 else "8-12 min"
        recommended = " (recommended)" if len(questionnaire.items) == 20 else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{questionnaire.title} - {len(questionnaire.items)} questions, "
                    f"{minutes}{recommended}",
                    callback_data=f"mode:{questionnaire.id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _history_page(
    history: list[AssessmentView],
    questionnaires: Mapping[str, Questionnaire],
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    page_size = 5
    page_count = max(1, (len(history) + page_size - 1) // page_size)
    page = min(max(page, 0), page_count - 1)
    visible = history[page * page_size : (page + 1) * page_size]
    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"View {view.started_at.date().isoformat()} "
                    f"{questionnaires[view.questionnaire_id].title}"
                ),
                callback_data=f"result:view:{view.id}",
            ),
            InlineKeyboardButton(
                text="Delete",
                callback_data=f"result:delete:{view.id}",
            ),
        ]
        for view in visible
    ]
    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(text="Previous", callback_data=f"history:page:{page - 1}")
        )
    if page + 1 < page_count:
        navigation.append(
            InlineKeyboardButton(text="Next", callback_data=f"history:page:{page + 1}")
        )
    if navigation:
        rows.append(navigation)
    return f"Earlier results - page {page + 1} of {page_count}", InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def _require_user(message: Message) -> tuple[int, str | None]:
    if message.from_user is None:
        raise ValueError("Telegram user is required")
    return message.from_user.id, message.from_user.language_code


@router.message(CommandStart())
async def start_command(message: Message, service: AssessmentService) -> None:
    user_id, _ = _require_user(message)
    if await service.has_consent(user_id):
        await message.answer("Welcome back. Use /test to start or resume an assessment.")
        return
    await message.answer(_PRIVACY_SUMMARY, reply_markup=_consent_keyboard())


@router.callback_query(F.data == "consent:accept")
async def accept_consent(
    callback: CallbackQuery,
    service: AssessmentService,
    analytics: AnalyticsService,
    questionnaires: Mapping[str, Questionnaire],
) -> None:
    now = datetime.now(UTC)
    await service.accept_terms(
        callback.from_user.id,
        callback.from_user.language_code,
        now,
    )
    await analytics.record(callback.from_user.id, "consent_accepted", now)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "Choose an assessment:",
            reply_markup=_mode_keyboard(questionnaires),
        )
    await callback.answer("Privacy notice accepted")


@router.callback_query(F.data == "consent:decline")
async def decline_consent(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "No data was stored. Send /start if you decide to use the bot later."
        )
    await callback.answer()


@router.message(Command("test"))
async def test_command(
    message: Message,
    service: AssessmentService,
    analytics: AnalyticsService,
    questionnaires: Mapping[str, Questionnaire],
) -> None:
    user_id, _ = _require_user(message)
    if not await service.has_consent(user_id):
        await message.answer(_PRIVACY_SUMMARY, reply_markup=_consent_keyboard())
        return
    active = await service.resume(user_id, datetime.now(UTC))
    if active is not None:
        await analytics.record(user_id, "assessment_resumed", datetime.now(UTC))
        questionnaire = questionnaires[active.questionnaire_id]
        rendered = render_question(questionnaire, active)
        await message.answer(rendered.text, reply_markup=rendered.reply_markup)
        return
    await message.answer("Choose an assessment:", reply_markup=_mode_keyboard(questionnaires))


@router.callback_query(F.data.startswith("mode:"))
async def choose_mode(
    callback: CallbackQuery,
    service: AssessmentService,
    analytics: AnalyticsService,
    questionnaires: Mapping[str, Questionnaire],
) -> None:
    questionnaire_id = (callback.data or "").removeprefix("mode:")
    try:
        assessment = await service.start(
            callback.from_user.id,
            questionnaire_id,
            datetime.now(UTC),
        )
    except ActiveAssessmentExists:
        resumed = await service.resume(callback.from_user.id, datetime.now(UTC))
        if resumed is None:
            await callback.answer("Please try again", show_alert=True)
            return
        assessment = resumed
    except (ConsentRequired, AssessmentNotFound) as error:
        await callback.answer(str(error), show_alert=True)
        return
    await analytics.record(
        callback.from_user.id,
        "assessment_started",
        datetime.now(UTC),
        {"questionnaire_id": assessment.questionnaire_id},
    )
    questionnaire = questionnaires[assessment.questionnaire_id]
    rendered = render_question(questionnaire, assessment)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(rendered.text, reply_markup=rendered.reply_markup)
    await callback.answer()


@router.callback_query(F.data.startswith("a:"))
async def answer_question(
    callback: CallbackQuery,
    service: AssessmentService,
    analytics: AnalyticsService,
    questionnaires: Mapping[str, Questionnaire],
) -> None:
    try:
        _, assessment_id, item_id, raw_value = (callback.data or "").split(":", 3)
        assessment = await service.answer(
            callback.from_user.id,
            assessment_id,
            item_id,
            int(raw_value),
            datetime.now(UTC),
        )
    except (ValueError, AssessmentNotFound, StaleInput) as error:
        await callback.answer(str(error), show_alert=True)
        return
    event_name = (
        "assessment_completed"
        if assessment.status is AssessmentStatus.COMPLETED
        else "answer_recorded"
    )
    await analytics.record(
        callback.from_user.id,
        event_name,
        datetime.now(UTC),
        {"questionnaire_id": assessment.questionnaire_id},
    )
    questionnaire = questionnaires[assessment.questionnaire_id]
    if isinstance(callback.message, Message):
        if assessment.status is AssessmentStatus.COMPLETED:
            await callback.message.edit_text(render_results(questionnaire, assessment))
        else:
            rendered = render_question(questionnaire, assessment)
            await callback.message.edit_text(rendered.text, reply_markup=rendered.reply_markup)
    await callback.answer()


@router.callback_query(F.data.startswith("b:"))
async def go_back(
    callback: CallbackQuery,
    service: AssessmentService,
    analytics: AnalyticsService,
    questionnaires: Mapping[str, Questionnaire],
) -> None:
    try:
        assessment = await service.back(
            callback.from_user.id,
            (callback.data or "").removeprefix("b:"),
            datetime.now(UTC),
        )
    except (AssessmentNotFound, StaleInput) as error:
        await callback.answer(str(error), show_alert=True)
        return
    await analytics.record(callback.from_user.id, "back_used", datetime.now(UTC))
    rendered = render_question(questionnaires[assessment.questionnaire_id], assessment)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(rendered.text, reply_markup=rendered.reply_markup)
    await callback.answer()


@router.callback_query(F.data.startswith("c:"))
async def cancel_assessment(
    callback: CallbackQuery,
    service: AssessmentService,
    analytics: AnalyticsService,
) -> None:
    try:
        await service.cancel(
            callback.from_user.id,
            (callback.data or "").removeprefix("c:"),
            datetime.now(UTC),
        )
    except (AssessmentNotFound, StaleInput) as error:
        await callback.answer(str(error), show_alert=True)
        return
    await analytics.record(callback.from_user.id, "assessment_cancelled", datetime.now(UTC))
    if isinstance(callback.message, Message):
        await callback.message.edit_text("Assessment cancelled. Use /test to start again.")
    await callback.answer()


@router.message(Command("results"))
async def results_command(
    message: Message,
    service: AssessmentService,
    questionnaires: Mapping[str, Questionnaire],
) -> None:
    user_id, _ = _require_user(message)
    history = await service.history(user_id)
    if not history:
        await message.answer("No completed assessments yet. Use /test to begin.")
        return
    latest = history[0]
    await message.answer(
        render_results(questionnaires[latest.questionnaire_id], latest),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Delete this result",
                        callback_data=f"result:delete:{latest.id}",
                    )
                ]
            ]
        ),
    )
    if len(history) > 1:
        text, keyboard = _history_page(history[1:], questionnaires, 0)
        await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("history:page:"))
async def history_page(
    callback: CallbackQuery,
    service: AssessmentService,
    questionnaires: Mapping[str, Questionnaire],
) -> None:
    try:
        page = int((callback.data or "").removeprefix("history:page:"))
    except ValueError:
        await callback.answer("Invalid history page", show_alert=True)
        return
    history = (await service.history(callback.from_user.id))[1:]
    if not history:
        await callback.answer("No earlier results", show_alert=True)
        return
    text, keyboard = _history_page(history, questionnaires, page)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("result:view:"))
async def view_result(
    callback: CallbackQuery,
    service: AssessmentService,
    questionnaires: Mapping[str, Questionnaire],
) -> None:
    assessment_id = (callback.data or "").removeprefix("result:view:")
    try:
        result = await service.get(callback.from_user.id, assessment_id)
    except AssessmentNotFound as error:
        await callback.answer(str(error), show_alert=True)
        return
    if result.status is not AssessmentStatus.COMPLETED:
        await callback.answer("Result is unavailable", show_alert=True)
        return
    if isinstance(callback.message, Message):
        await callback.message.reply(
            render_results(questionnaires[result.questionnaire_id], result),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Delete this result",
                            callback_data=f"result:delete:{result.id}",
                        )
                    ]
                ]
            ),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("result:delete:"))
async def request_result_delete(callback: CallbackQuery) -> None:
    assessment_id = (callback.data or "").removeprefix("result:delete:")
    if isinstance(callback.message, Message):
        await callback.message.reply(
            "Delete this completed result permanently?",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Delete result",
                            callback_data=f"result:confirm:{assessment_id}",
                        ),
                        InlineKeyboardButton(text="Keep", callback_data="result:keep"),
                    ]
                ]
            ),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("result:confirm:"))
async def confirm_result_delete(
    callback: CallbackQuery,
    service: AssessmentService,
) -> None:
    assessment_id = (callback.data or "").removeprefix("result:confirm:")
    try:
        await service.delete_assessment(callback.from_user.id, assessment_id)
    except (AssessmentNotFound, StaleInput) as error:
        await callback.answer(str(error), show_alert=True)
        return
    if isinstance(callback.message, Message):
        await callback.message.edit_text("Result deleted.")
    await callback.answer()


@router.callback_query(F.data == "result:keep")
async def keep_result(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text("Result kept.")
    await callback.answer()


@router.message(Command("export"))
async def export_command(
    message: Message,
    service: AssessmentService,
    analytics: AnalyticsService,
) -> None:
    user_id, _ = _require_user(message)
    payload_data = await service.export_user(user_id)
    payload_data["analytics"] = await analytics.export_user(user_id)
    payload = json.dumps(payload_data, indent=2).encode()
    await message.answer_document(
        BufferedInputFile(payload, filename="ocean-bot-export.json"),
        caption="Your stored assessment data.",
    )


@router.message(Command("delete_me"))
async def delete_command(message: Message) -> None:
    await message.answer(
        "Delete your account, answers, and results permanently?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Delete", callback_data="delete:confirm"),
                    InlineKeyboardButton(text="Keep", callback_data="delete:cancel"),
                ]
            ]
        ),
    )


@router.callback_query(F.data == "delete:confirm")
async def confirm_delete(
    callback: CallbackQuery,
    privacy: PrivacyService,
) -> None:
    await privacy.delete_user(callback.from_user.id)
    if isinstance(callback.message, Message):
        await callback.message.edit_text("Your stored account, answers, and results were deleted.")
    await callback.answer()


@router.callback_query(F.data == "delete:cancel")
async def cancel_delete(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text("Deletion cancelled.")
    await callback.answer()


@router.message(Command("privacy"))
async def privacy_command(message: Message, privacy_contact: str) -> None:
    await message.answer(
        _PRIVACY_SUMMARY
        + "\n\nAssessment history remains until /delete_me. Raw analytics are kept for 90 "
        "days and sanitized error logs for 30 days. Anonymous counters may remain. "
        "Use /export to access your data or /delete_me to erase it. Contact: " + privacy_contact
    )


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(
        "/test - start or resume\n/results - latest result\n/export - download your data\n"
        "/delete_me - erase your data\n/privacy - privacy details\n/help - this message"
    )


@router.message(Command("admin_stats"))
async def admin_stats_command(
    message: Message,
    admin_user_id: int,
    analytics: AnalyticsService,
) -> None:
    user_id, _ = _require_user(message)
    if user_id != admin_user_id:
        await message.answer("Command not available.")
        return
    stats = await analytics.stats()
    text = "Aggregate events\n" + "\n".join(
        f"{name}: {count}" for name, count in sorted(stats.items())
    )
    await message.answer(text)

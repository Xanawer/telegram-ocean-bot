from __future__ import annotations

import logging
from datetime import UTC, datetime
from functools import partial
from time import perf_counter

from aiogram import F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ocean_bot.application.analytics import AnalyticsService
from ocean_bot.application.data_rights import DataRightsUnavailable, UserDataRights
from ocean_bot.application.interactions import (
    PRIVACY_SUMMARY,
    AssessmentInteraction,
    CallbackIntent,
    Delivery,
    InteractionButton,
    InteractionOutcome,
    InteractionReply,
    TextFormat,
)
from ocean_bot.telegram.callbacks import decode_callback, encode_callback
from ocean_bot.telegram.delivery import TelegramDelivery
from ocean_bot.telegram.middleware import UserActionLockMiddleware, UserActionLocks

_logger = logging.getLogger(__name__)
_callback_delivery = TelegramDelivery(max_per_second=50)
_message_delivery = TelegramDelivery(max_per_second=25)


_COMMANDS = ("test", "results", "export", "delete_me", "privacy", "help", "admin_stats")


def create_router() -> Router:
    router = Router(name="ocean-bot")
    router.message.middleware(UserActionLockMiddleware())
    # Replies carry personal answers and exports, so only private chats are served.
    router.message.register(
        private_chat_required,
        F.chat.type != ChatType.PRIVATE,
        Command("start", *_COMMANDS),
    )
    router.message.register(start_command, CommandStart())
    router.message.register(test_command, Command("test"))
    router.message.register(results_command, Command("results"))
    router.message.register(export_command, Command("export"))
    router.message.register(delete_command, Command("delete_me"))
    router.message.register(privacy_command, Command("privacy"))
    router.message.register(help_command, Command("help"))
    router.message.register(admin_stats_command, Command("admin_stats"))
    router.callback_query.register(handle_callback, F.message.chat.type == ChatType.PRIVATE)
    # Anything else, including inline-mode callbacks that carry no message, is refused.
    router.callback_query.register(private_callback_required)
    return router


async def private_chat_required(message: Message) -> None:
    await _message_delivery.call(
        partial(message.answer, "For your privacy, please message me in a private chat."),
        "message.answer",
    )


async def private_callback_required(callback: CallbackQuery) -> None:
    await _callback_delivery.call(
        partial(
            callback.answer,
            "For your privacy, please use this bot in a private chat.",
            show_alert=True,
        ),
        "callback.answer.alert",
    )


def _require_user(message: Message) -> tuple[int, str | None]:
    if message.from_user is None:
        raise ValueError("Telegram user is required")
    return message.from_user.id, message.from_user.language_code


async def start_command(
    message: Message,
    interaction: AssessmentInteraction,
) -> None:
    user_id, _ = _require_user(message)
    await _deliver_message(message, await interaction.start(user_id))


async def test_command(
    message: Message,
    interaction: AssessmentInteraction,
) -> None:
    user_id, _ = _require_user(message)
    await _deliver_message(
        message,
        await interaction.begin_or_resume(user_id, datetime.now(UTC)),
    )


async def results_command(
    message: Message,
    interaction: AssessmentInteraction,
) -> None:
    user_id, _ = _require_user(message)
    await _deliver_message(message, await interaction.results(user_id))


async def handle_callback(
    callback: CallbackQuery,
    interaction: AssessmentInteraction,
    data_rights: UserDataRights,
    action_locks: UserActionLocks | None = None,
) -> None:
    started = perf_counter()
    try:
        intent = decode_callback(callback.data)
        await _callback_delivery.call(partial(callback.answer), "callback.answer")
        acknowledged = perf_counter()
        locks = action_locks or UserActionLocks()
        # Deliver while still holding the user's lock, so a delayed or retried edit
        # cannot land after, and overwrite, the response to a later callback.
        async with locks.hold(callback.from_user.id):
            outcome = await _dispatch_callback(
                callback,
                intent,
                interaction,
                data_rights,
                datetime.now(UTC),
            )
            dispatched = perf_counter()
            await _deliver_callback(callback, outcome)
    except (TypeError, ValueError) as error:
        await _callback_delivery.call(
            partial(callback.answer, str(error), show_alert=True),
            "callback.answer.alert",
        )
        return
    delivered = perf_counter()
    _logger.info(
        "callback_latency action=%s ack_ms=%.1f dispatch_ms=%.1f delivery_ms=%.1f total_ms=%.1f",
        intent.action,
        (acknowledged - started) * 1000,
        (dispatched - acknowledged) * 1000,
        (delivered - dispatched) * 1000,
        (delivered - started) * 1000,
    )


async def _dispatch_callback(
    callback: CallbackQuery,
    intent: CallbackIntent,
    interaction: AssessmentInteraction,
    data_rights: UserDataRights,
    now: datetime,
) -> InteractionOutcome:
    user_id = callback.from_user.id
    if intent.action == "consent.accept":
        return await interaction.accept_consent(user_id, callback.from_user.language_code, now)
    if intent.action == "consent.decline":
        return interaction.decline_consent()
    if intent.action == "mode":
        return await interaction.choose_mode(user_id, _single_value(intent), now)
    if intent.action == "answer":
        assessment_id, item_id, raw_value = _values(intent, 3)
        return await interaction.answer(user_id, assessment_id, item_id, int(raw_value), now)
    if intent.action == "back":
        # Keyboards sent before the answer count was added carry only the assessment ID.
        if len(intent.values) == 1:
            return await interaction.back(user_id, intent.values[0], now)
        assessment_id, answered_count = _values(intent, 2)
        return await interaction.back(user_id, assessment_id, now, int(answered_count))
    if intent.action == "cancel":
        return await interaction.cancel(user_id, _single_value(intent), now)
    if intent.action == "history.page":
        return await interaction.history_page(user_id, int(_single_value(intent)))
    if intent.action == "result.view":
        return await interaction.view_result(user_id, _single_value(intent))
    if intent.action == "result.delete":
        return interaction.request_result_delete(_single_value(intent))
    if intent.action == "result.confirm":
        return await interaction.confirm_result_delete(user_id, _single_value(intent))
    if intent.action == "result.keep":
        return interaction.keep_result()
    if intent.action == "delete.confirm":
        try:
            await data_rights.delete_user(user_id)
        except DataRightsUnavailable as error:
            return InteractionOutcome((InteractionReply(str(error), delivery=Delivery.EDIT),))
        return InteractionOutcome(
            (
                InteractionReply(
                    "Your stored account, answers, and results were deleted.",
                    delivery=Delivery.EDIT,
                ),
            )
        )
    if intent.action == "delete.cancel":
        return InteractionOutcome(
            (InteractionReply("Deletion cancelled.", delivery=Delivery.EDIT),)
        )
    raise ValueError("Unsupported callback action")


async def export_command(message: Message, data_rights: UserDataRights) -> None:
    user_id, _ = _require_user(message)
    export = await data_rights.export_user(user_id)
    await _message_delivery.call(
        partial(
            message.answer_document,
            BufferedInputFile(export.json_bytes(), filename="ocean-bot-export.json"),
            caption="Your stored assessment data.",
        ),
        "message.answer_document",
    )


async def delete_command(message: Message) -> None:
    buttons = (
        (
            InteractionButton("Delete", CallbackIntent("delete.confirm")),
            InteractionButton("Keep", CallbackIntent("delete.cancel")),
        ),
    )
    await _deliver_message(
        message,
        InteractionOutcome(
            (
                InteractionReply(
                    "Delete your account, answers, and results permanently?",
                    buttons,
                ),
            )
        ),
    )


async def privacy_command(message: Message, privacy_contact: str) -> None:
    await _message_delivery.call(
        partial(
            message.answer,
            PRIVACY_SUMMARY
            + "\n\nAssessment history remains until /delete_me. Raw analytics are kept for 90 "
            "days and sanitized error logs for 30 days. Anonymous counters may remain. "
            "Use /export to access your data or /delete_me to erase it. Contact: "
            + privacy_contact,
        ),
        "message.answer",
    )


async def help_command(message: Message) -> None:
    await _message_delivery.call(
        partial(
            message.answer,
            "/test - start or resume\n/results - latest result\n/export - download your data\n"
            "/delete_me - erase your data\n/privacy - privacy details\n/help - this message",
        ),
        "message.answer",
    )


async def admin_stats_command(
    message: Message,
    admin_user_id: int,
    analytics: AnalyticsService,
) -> None:
    user_id, _ = _require_user(message)
    if user_id != admin_user_id:
        await _message_delivery.call(
            partial(message.answer, "Command not available."),
            "message.answer",
        )
        return
    stats = await analytics.stats()
    text = "Aggregate events\n" + "\n".join(
        f"{name}: {count}" for name, count in sorted(stats.items())
    )
    await _message_delivery.call(partial(message.answer, text), "message.answer")


async def _deliver_message(message: Message, outcome: InteractionOutcome) -> None:
    for reply in outcome.replies:
        await _message_delivery.call(
            partial(
                message.answer,
                reply.text,
                reply_markup=_markup(reply),
                parse_mode=_parse_mode(reply),
            ),
            "message.answer",
        )


async def _deliver_callback(
    callback: CallbackQuery,
    outcome: InteractionOutcome,
) -> None:
    if isinstance(callback.message, Message):
        for reply in outcome.replies:
            markup = _markup(reply)
            if reply.delivery is Delivery.EDIT:
                await _message_delivery.call(
                    partial(
                        callback.message.edit_text,
                        reply.text,
                        reply_markup=markup,
                        parse_mode=_parse_mode(reply),
                    ),
                    "message.edit_text",
                )
            elif reply.delivery is Delivery.REPLY:
                await _message_delivery.call(
                    partial(
                        callback.message.reply,
                        reply.text,
                        reply_markup=markup,
                        parse_mode=_parse_mode(reply),
                    ),
                    "message.reply",
                )
            else:
                await _message_delivery.call(
                    partial(
                        callback.message.answer,
                        reply.text,
                        reply_markup=markup,
                        parse_mode=_parse_mode(reply),
                    ),
                    "message.answer",
                )
        if not outcome.replies and outcome.callback_notice is not None:
            await _message_delivery.call(
                partial(callback.message.reply, outcome.callback_notice),
                "message.reply",
            )


def _markup(reply: InteractionReply) -> InlineKeyboardMarkup | None:
    if not reply.buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=button.text,
                    callback_data=encode_callback(button.intent),
                )
                for button in row
            ]
            for row in reply.buttons
        ]
    )


def _parse_mode(reply: InteractionReply) -> ParseMode | None:
    return ParseMode.HTML if reply.text_format is TextFormat.HTML else None


def _single_value(intent: CallbackIntent) -> str:
    return _values(intent, 1)[0]


def _values(intent: CallbackIntent, count: int) -> tuple[str, ...]:
    if len(intent.values) != count:
        raise ValueError(f"{intent.action} requires {count} callback values")
    return intent.values

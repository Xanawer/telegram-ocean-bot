from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from html import escape

from ocean_bot.application.analytics import AnalyticsEvent, AnalyticsService
from ocean_bot.application.assessments import (
    ActiveAssessmentExists,
    AssessmentEventFactory,
    AssessmentNotFound,
    AssessmentService,
    ConsentRequired,
    StaleInput,
    StorageUnavailable,
    SyncBacklogExceeded,
)
from ocean_bot.domain.assessment import Assessment, AssessmentStatus, AssessmentView
from ocean_bot.domain.questionnaire import Questionnaire, Trait

_logger = logging.getLogger(__name__)


class Delivery(StrEnum):
    SEND = "send"
    EDIT = "edit"
    REPLY = "reply"


class TextFormat(StrEnum):
    PLAIN = "plain"
    HTML = "html"


@dataclass(frozen=True, slots=True)
class CallbackIntent:
    action: str
    values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InteractionButton:
    text: str
    intent: CallbackIntent


@dataclass(frozen=True, slots=True)
class InteractionReply:
    text: str
    buttons: tuple[tuple[InteractionButton, ...], ...] = ()
    delivery: Delivery = Delivery.SEND
    text_format: TextFormat = TextFormat.PLAIN


@dataclass(frozen=True, slots=True)
class InteractionOutcome:
    replies: tuple[InteractionReply, ...] = ()
    callback_notice: str | None = None
    callback_alert: bool = False


PRIVACY_SUMMARY = (
    "Big Five self-reflection for people aged 13 or older.\n\n"
    "The bot temporarily stores active responses on Railway and synchronizes your Telegram "
    "ID, answers, results, timestamps, and navigation events to Supabase. Required analytics "
    "support operation and product improvement. "
    "Use /privacy for retention and provider details. Results are not clinical advice."
)

_TRAIT_LABELS = {
    Trait.OPENNESS: "Openness",
    Trait.CONSCIENTIOUSNESS: "Conscientiousness",
    Trait.EXTRAVERSION: "Extraversion",
    Trait.AGREEABLENESS: "Agreeableness",
    Trait.NEUROTICISM: "Neuroticism",
}
_QUESTIONNAIRE_ORDER = ("mini-ipip-20", "ipip-50", "ipip-120")
_ESTIMATED_MINUTES = {
    "mini-ipip-20": "3-5 min",
    "ipip-50": "8-12 min",
    "ipip-120": "18-25 min",
}

_REFLECTIONS = {
    Trait.OPENNESS: "How much do you seek new ideas and experiences?",
    Trait.CONSCIENTIOUSNESS: "How do structure and follow-through show up in your routines?",
    Trait.EXTRAVERSION: "Which social settings tend to give you energy?",
    Trait.AGREEABLENESS: "How do you balance cooperation with your own boundaries?",
    Trait.NEUROTICISM: "What situations tend to intensify or settle your emotions?",
}


def _score_bar(score: float, width: int = 10) -> str:
    filled = min(width, max(0, int(score / 5 * width + 0.5)))
    return "█" * filled + "░" * (width - filled)


def _progress_bar(current: int, total: int, width: int = 10) -> str:
    partial_blocks = ("", "▏", "▎", "▍", "▌", "▋", "▊", "▉")
    units = min(width * 8, max(0, round(current / total * width * 8)))
    full_blocks, partial_units = divmod(units, 8)
    partial = partial_blocks[partial_units]
    empty_blocks = width - full_blocks - bool(partial)
    return "█" * full_blocks + partial + "░" * empty_blocks


class AssessmentInteraction:
    def __init__(
        self,
        assessments: AssessmentService,
        analytics: AnalyticsService,
        questionnaires: Mapping[str, Questionnaire],
        transition_analytics_persisted: bool = False,
    ) -> None:
        self._assessments = assessments
        self._analytics = analytics
        self._questionnaires = dict(questionnaires)
        self._transition_analytics_persisted = transition_analytics_persisted

    async def start(self, telegram_user_id: int) -> InteractionOutcome:
        if await self._assessments.has_consent(telegram_user_id):
            return self._send("Welcome back. Use /test to start or resume an assessment.")
        return InteractionOutcome((self._privacy_reply(),))

    async def accept_consent(
        self,
        telegram_user_id: int,
        language_code: str | None,
        now: datetime,
    ) -> InteractionOutcome:
        try:
            await self._assessments.accept_terms(telegram_user_id, language_code, now)
        except StorageUnavailable as error:
            return self._alert(str(error))
        await self._record(telegram_user_id, "consent_accepted", now)
        return InteractionOutcome(
            (self._mode_reply(Delivery.EDIT),),
            callback_notice="Privacy notice accepted",
        )

    def decline_consent(self) -> InteractionOutcome:
        return self._edit("No data was stored. Send /start if you decide to use the bot later.")

    async def begin_or_resume(self, telegram_user_id: int, now: datetime) -> InteractionOutcome:
        if not await self._assessments.has_consent(telegram_user_id):
            return InteractionOutcome((self._privacy_reply(),))
        active = await self._assessments.resume(telegram_user_id, now)
        if active is None:
            return InteractionOutcome((self._mode_reply(),))
        await self._record(telegram_user_id, "assessment_resumed", now)
        return InteractionOutcome((self._question_reply(active),))

    async def choose_mode(
        self,
        telegram_user_id: int,
        questionnaire_id: str,
        now: datetime,
    ) -> InteractionOutcome:
        event_name = "assessment_started"
        try:
            assessment = await self._assessments.start(telegram_user_id, questionnaire_id, now)
        except ActiveAssessmentExists:
            resumed = await self._assessments.resume(telegram_user_id, now)
            if resumed is None:
                return self._alert("Please try again")
            assessment = resumed
            event_name = "assessment_resumed"
        except (
            ConsentRequired,
            AssessmentNotFound,
            StorageUnavailable,
            SyncBacklogExceeded,
        ) as error:
            return self._alert(str(error))
        await self._record(
            telegram_user_id,
            event_name,
            now,
            {"questionnaire_id": assessment.questionnaire_id},
        )
        return InteractionOutcome((self._question_reply(assessment, Delivery.EDIT),))

    async def answer(
        self,
        telegram_user_id: int,
        assessment_id: str,
        item_id: str,
        value: int,
        now: datetime,
    ) -> InteractionOutcome:
        event_factory: AssessmentEventFactory | None = None
        if self._transition_analytics_persisted:
            event_factory = self._transition_event_factory(
                now,
                "answer_recorded",
                "assessment_completed",
            )
        try:
            transition = await self._assessments.answer(
                telegram_user_id,
                assessment_id,
                item_id,
                value,
                now,
                event_factory,
            )
        except (
            ValueError,
            AssessmentNotFound,
            StaleInput,
            StorageUnavailable,
        ) as error:
            return self._alert(str(error))
        assessment = transition.view
        if transition.changed and not self._transition_analytics_persisted:
            event_name = (
                "assessment_completed"
                if assessment.status is AssessmentStatus.COMPLETED
                else "answer_recorded"
            )
            await self._record(
                telegram_user_id,
                event_name,
                now,
                {"questionnaire_id": assessment.questionnaire_id},
            )
        reply = (
            self._result_reply(assessment, Delivery.EDIT, include_delete=False)
            if assessment.status is AssessmentStatus.COMPLETED
            else self._question_reply(assessment, Delivery.EDIT)
        )
        return InteractionOutcome((reply,))

    async def back(
        self,
        telegram_user_id: int,
        assessment_id: str,
        now: datetime,
        expected_answered: int | None = None,
    ) -> InteractionOutcome:
        event_factory: AssessmentEventFactory | None = None
        if self._transition_analytics_persisted:
            event_factory = self._transition_event_factory(now, "back_used")
        try:
            transition = await self._assessments.back(
                telegram_user_id,
                assessment_id,
                now,
                event_factory,
                expected_answered,
            )
        except (AssessmentNotFound, StaleInput, StorageUnavailable) as error:
            return self._alert(str(error))
        if transition.changed and not self._transition_analytics_persisted:
            await self._record(
                telegram_user_id,
                "back_used",
                now,
                {"questionnaire_id": transition.view.questionnaire_id},
            )
        return InteractionOutcome((self._question_reply(transition.view, Delivery.EDIT),))

    async def cancel(
        self, telegram_user_id: int, assessment_id: str, now: datetime
    ) -> InteractionOutcome:
        event_factory: AssessmentEventFactory | None = None
        if self._transition_analytics_persisted:
            event_factory = self._transition_event_factory(
                now,
                "assessment_cancelled",
            )
        try:
            transition = await self._assessments.cancel(
                telegram_user_id,
                assessment_id,
                now,
                event_factory,
            )
        except (AssessmentNotFound, StaleInput, StorageUnavailable) as error:
            return self._alert(str(error))
        if not self._transition_analytics_persisted:
            await self._record(
                telegram_user_id,
                "assessment_cancelled",
                now,
                {"questionnaire_id": transition.view.questionnaire_id},
            )
        return self._edit("Assessment cancelled. Use /test to start again.")

    async def results(self, telegram_user_id: int) -> InteractionOutcome:
        history = await self._assessments.history(telegram_user_id)
        if not history:
            return self._send("No completed assessments yet. Use /test to begin.")
        replies = [self._result_reply(history[0], include_delete=True)]
        if len(history) > 1:
            replies.append(self._history_reply(history[1:], 0))
        return InteractionOutcome(tuple(replies))

    async def history_page(self, telegram_user_id: int, page: int) -> InteractionOutcome:
        history = (await self._assessments.history(telegram_user_id))[1:]
        if not history:
            return self._alert("No earlier results")
        return InteractionOutcome((self._history_reply(history, page, Delivery.EDIT),))

    async def view_result(self, telegram_user_id: int, assessment_id: str) -> InteractionOutcome:
        try:
            result = await self._assessments.get(telegram_user_id, assessment_id)
        except AssessmentNotFound as error:
            return self._alert(str(error))
        if result.status is not AssessmentStatus.COMPLETED:
            return self._alert("Result is unavailable")
        return InteractionOutcome((self._result_reply(result, Delivery.REPLY, True),))

    def request_result_delete(self, assessment_id: str) -> InteractionOutcome:
        buttons = (
            (
                InteractionButton(
                    "Delete result", CallbackIntent("result.confirm", (assessment_id,))
                ),
                InteractionButton("Keep", CallbackIntent("result.keep")),
            ),
        )
        return InteractionOutcome(
            (
                InteractionReply(
                    "Delete this completed result permanently?",
                    buttons,
                    Delivery.REPLY,
                ),
            )
        )

    async def confirm_result_delete(
        self, telegram_user_id: int, assessment_id: str
    ) -> InteractionOutcome:
        try:
            await self._assessments.delete_assessment(telegram_user_id, assessment_id)
        except (AssessmentNotFound, StaleInput, StorageUnavailable) as error:
            return self._alert(str(error))
        return self._edit("Result deleted.")

    def keep_result(self) -> InteractionOutcome:
        return self._edit("Result kept.")

    def _privacy_reply(self) -> InteractionReply:
        buttons = (
            (
                InteractionButton("I am 13+ and accept", CallbackIntent("consent.accept")),
                InteractionButton("Decline", CallbackIntent("consent.decline")),
            ),
        )
        return InteractionReply(PRIVACY_SUMMARY, buttons)

    def _mode_reply(self, delivery: Delivery = Delivery.SEND) -> InteractionReply:
        rows: list[tuple[InteractionButton, ...]] = []
        for questionnaire_id in _QUESTIONNAIRE_ORDER:
            questionnaire = self._questionnaires.get(questionnaire_id)
            if questionnaire is None:
                continue
            minutes = _ESTIMATED_MINUTES.get(questionnaire_id, "")
            recommended = " (recommended)" if len(questionnaire.items) == 20 else ""
            rows.append(
                (
                    InteractionButton(
                        f"{questionnaire.title} - {len(questionnaire.items)} questions, "
                        f"{minutes}{recommended}",
                        CallbackIntent("mode", (questionnaire.id,)),
                    ),
                )
            )
        return InteractionReply("Choose an assessment:", tuple(rows), delivery)

    def _question_reply(
        self,
        assessment: AssessmentView,
        delivery: Delivery = Delivery.SEND,
    ) -> InteractionReply:
        questionnaire = self._questionnaires[assessment.questionnaire_id]
        if assessment.status is not AssessmentStatus.ACTIVE or assessment.current_item_id is None:
            raise ValueError("Only an active assessment can render a question")
        item = next(item for item in questionnaire.items if item.id == assessment.current_item_id)
        progress = assessment.answered_count + 1
        scale_labels = (
            (
                "Very inaccurate",
                "Moderately inaccurate",
                "Neither accurate nor inaccurate",
                "Moderately accurate",
                "Very accurate",
            )
            if questionnaire.id == "ipip-120"
            else (
                "Strongly disagree",
                "Disagree",
                "Neutral",
                "Agree",
                "Strongly agree",
            )
        )
        scale = "\n".join(
            f"<code>{value}</code> {label}" for value, label in enumerate(scale_labels, start=1)
        )
        text = (
            f"<b>Question {progress} of {assessment.total_items}</b>\n"
            f"<code>{_progress_bar(progress, assessment.total_items)}</code>\n\n"
            f"<b>{escape(item.text)}</b>\n\n"
            f"{scale}"
        )
        answers = tuple(
            InteractionButton(
                str(value),
                CallbackIntent("answer", (assessment.id, item.id, str(value))),
            )
            for value in range(1, 6)
        )
        navigation: list[InteractionButton] = []
        if assessment.answered_count:
            navigation.append(
                InteractionButton(
                    "← Back",
                    CallbackIntent("back", (assessment.id, str(assessment.answered_count))),
                )
            )
        navigation.append(InteractionButton("Cancel", CallbackIntent("cancel", (assessment.id,))))
        return InteractionReply(
            text,
            (answers, tuple(navigation)),
            delivery,
            TextFormat.HTML,
        )

    def _result_reply(
        self,
        assessment: AssessmentView,
        delivery: Delivery = Delivery.SEND,
        include_delete: bool = False,
    ) -> InteractionReply:
        questionnaire = self._questionnaires[assessment.questionnaire_id]
        if assessment.status is not AssessmentStatus.COMPLETED:
            raise ValueError("Only completed assessments have results")
        score_lines: list[str] = []
        for trait in Trait:
            if trait not in assessment.scores:
                continue
            score = assessment.scores[trait]
            score_lines.extend(
                (
                    f"<b>{_TRAIT_LABELS[trait]}</b>  {score:.2f} / 5",
                    f"<code>{_score_bar(score)}</code>",
                )
            )
        if assessment.facet_scores:
            score_lines.extend(("", "<b>Facet details</b>"))
            for trait in Trait:
                facets = dict.fromkeys(
                    item.facet
                    for item in questionnaire.items
                    if item.trait is trait and item.facet is not None
                )
                if facets:
                    score_lines.append(f"<i>{_TRAIT_LABELS[trait]}</i>")
                    score_lines.extend(
                        f"• {escape(facet)}: {assessment.facet_scores[facet]:.2f} / 5"
                        for facet in facets
                    )
        prompts = [_REFLECTIONS[trait] for trait in Trait if trait in assessment.scores][:2]
        completed = ""
        if assessment.completed_at:
            completed_date = assessment.completed_at.date()
            completed = f"{completed_date.day} {completed_date.strftime('%b %Y')}"
        text = "\n".join(
            [
                f"<b>{escape(questionnaire.title)} results</b>",
                f"<i>{completed}</i>",
                "",
                "<b>Trait scores</b>",
                *score_lines,
                "",
                (
                    "<i>Population percentiles are not shown because this assessment "
                    "does not yet include a compatible, documented benchmark.</i>"
                ),
                "",
                "<b>Questions for reflection</b>",
                *(f"{index}. {prompt}" for index, prompt in enumerate(prompts, start=1)),
                "",
                (
                    "<i>For self-reflection and entertainment only. "
                    "This is not a clinical assessment.</i>"
                ),
            ]
        )
        buttons = (
            (
                (
                    InteractionButton(
                        "Delete this result",
                        CallbackIntent("result.delete", (assessment.id,)),
                    ),
                ),
            )
            if include_delete
            else ()
        )
        return InteractionReply(text, buttons, delivery, TextFormat.HTML)

    def _history_reply(
        self,
        history: list[AssessmentView],
        page: int,
        delivery: Delivery = Delivery.SEND,
    ) -> InteractionReply:
        page_size = 5
        page_count = max(1, (len(history) + page_size - 1) // page_size)
        page = min(max(page, 0), page_count - 1)
        visible = history[page * page_size : (page + 1) * page_size]
        rows: list[tuple[InteractionButton, ...]] = [
            (
                InteractionButton(
                    f"View {view.started_at.date().isoformat()} "
                    f"{self._questionnaires[view.questionnaire_id].title}",
                    CallbackIntent("result.view", (view.id,)),
                ),
                InteractionButton("Delete", CallbackIntent("result.delete", (view.id,))),
            )
            for view in visible
        ]
        navigation: list[InteractionButton] = []
        if page > 0:
            navigation.append(
                InteractionButton("Previous", CallbackIntent("history.page", (str(page - 1),)))
            )
        if page + 1 < page_count:
            navigation.append(
                InteractionButton("Next", CallbackIntent("history.page", (str(page + 1),)))
            )
        if navigation:
            rows.append(tuple(navigation))
        return InteractionReply(
            f"Earlier results - page {page + 1} of {page_count}",
            tuple(rows),
            delivery,
        )

    async def _record(
        self,
        telegram_user_id: int,
        name: str,
        occurred_at: datetime,
        properties: Mapping[str, str] | None = None,
    ) -> None:
        try:
            await self._analytics.record(telegram_user_id, name, occurred_at, properties)
        except Exception:
            _logger.exception("Analytics recording failed", extra={"event_name": name})

    @staticmethod
    def _transition_event_factory(
        occurred_at: datetime,
        name: str,
        completed_name: str | None = None,
    ) -> AssessmentEventFactory:
        def event_for(assessment: Assessment) -> AnalyticsEvent:
            event_name = (
                completed_name
                if completed_name is not None and assessment.status is AssessmentStatus.COMPLETED
                else name
            )
            return AnalyticsEvent(
                event_name,
                occurred_at,
                {"questionnaire_id": assessment.questionnaire_id},
            )

        return event_for

    @staticmethod
    def _send(text: str) -> InteractionOutcome:
        return InteractionOutcome((InteractionReply(text),))

    @staticmethod
    def _edit(text: str) -> InteractionOutcome:
        return InteractionOutcome((InteractionReply(text, delivery=Delivery.EDIT),))

    @staticmethod
    def _alert(text: str) -> InteractionOutcome:
        return InteractionOutcome(callback_notice=text, callback_alert=True)

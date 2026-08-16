from __future__ import annotations

from ocean_bot.application.interactions import CallbackIntent

_PREFIXES = {
    "mode": "mode",
    "answer": "a",
    "back": "b",
    "cancel": "c",
    "history.page": "history:page",
    "result.view": "result:view",
    "result.delete": "result:delete",
    "result.confirm": "result:confirm",
}

_LITERALS = {
    "consent.accept": "consent:accept",
    "consent.decline": "consent:decline",
    "result.keep": "result:keep",
    "delete.confirm": "delete:confirm",
    "delete.cancel": "delete:cancel",
}


def encode_callback(intent: CallbackIntent) -> str:
    literal = _LITERALS.get(intent.action)
    if literal is not None:
        if intent.values:
            raise ValueError(f"{intent.action} does not accept callback values")
        return literal
    prefix = _PREFIXES.get(intent.action)
    if prefix is None:
        raise ValueError(f"Unknown callback action: {intent.action}")
    if not intent.values:
        raise ValueError(f"{intent.action} requires callback values")
    return ":".join((prefix, *intent.values))


def decode_callback(value: str | None) -> CallbackIntent:
    if not value:
        raise ValueError("Callback data is required")
    for action, literal in _LITERALS.items():
        if value == literal:
            return CallbackIntent(action)
    for action, prefix in sorted(
        _PREFIXES.items(), key=lambda item: len(item[1]), reverse=True
    ):
        marker = prefix + ":"
        if value.startswith(marker):
            values = tuple(value.removeprefix(marker).split(":"))
            if not all(values):
                break
            return CallbackIntent(action, values)
    raise ValueError("Invalid callback data")

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import asdict

from ocean_bot.config import Settings
from ocean_bot.runtime import Runtime


def _parse_command(argv: Sequence[str] | None) -> str:
    parser = argparse.ArgumentParser(description="Run the Telegram OCEAN bot")
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=("serve", "retention", "set-webhook", "sync"),
    )
    return str(parser.parse_args(argv).command)


def run(argv: Sequence[str] | None = None) -> None:
    command = _parse_command(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    runtime = Runtime(Settings())  # type: ignore[call-arg]
    if command == "retention":
        result = asyncio.run(runtime.run_retention())
        print(json.dumps(asdict(result), sort_keys=True))
        return
    if command == "set-webhook":
        asyncio.run(runtime.set_webhook())
        print("Webhook configured")
        return
    if command == "sync":
        sync_result = asyncio.run(runtime.run_sync())
        print(json.dumps(asdict(sync_result), sort_keys=True))
        return
    runtime.serve()

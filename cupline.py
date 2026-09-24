#!/usr/bin/env python3
"""cupline — universal terminal-attention monitor for AI coding agents.

Pipeline:

    iTerm sessions -> screen change events -> per-session debounce
                   -> classifier -> tab colour

Run with the shared venv:

    ~/.venvs/iterm2/bin/python cupline.py [--demo | --list | --reset | ...]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import logging.handlers
import os
import signal
import sys

import iterm2

from commands import cmd_capture, cmd_demo, cmd_list, cmd_reset, cmd_set
from config import DEBOUNCE_SECONDS, LOG_DIR, LOG_FILE
from monitor import Cupline

log = logging.getLogger("cupline")


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

def setup_logging(debug: bool) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    root = logging.getLogger("cupline")
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.handlers.clear()

    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, LOG_FILE), maxBytes=2_000_000, backupCount=3
    )
    file_handler.setLevel(logging.DEBUG if debug else logging.INFO)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if debug else logging.INFO)
    console.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
    root.addHandler(console)


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="cupline terminal-attention monitor")
    parser.add_argument("--debug", action="store_true", help="DEBUG-level file logging")
    parser.add_argument("--tail", action="store_true",
                        help="print the terminal tail on every classified change")
    parser.add_argument("--debounce", type=float, default=DEBOUNCE_SECONDS,
                        help=f"seconds of quiet before classifying (default {DEBOUNCE_SECONDS})")
    parser.add_argument("--list", action="store_true", help="list sessions and exit")
    parser.add_argument("--reset", action="store_true",
                        help="clear tab colour on every session and exit")
    parser.add_argument("--set", metavar="ID=STATE",
                        help="paint one session's tab and exit, e.g. --set 5D23=action")
    parser.add_argument("--demo", action="store_true",
                        help="cycle a tab through WAITING/ACTION/WORKING and exit")
    parser.add_argument("--demo-session", metavar="ID",
                        help="session id prefix for --demo")
    parser.add_argument("--demo-pause", type=float, default=5.0,
                        help="seconds to hold each demo state (default 5)")
    parser.add_argument("--capture", metavar="ID", help="save a session's tail as a fixture")
    parser.add_argument("--label", default="unknown",
                        help="expected label for --capture (working/waiting/action/unknown)")
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    setup_logging(args.debug)

    async def entry(connection):
        app = await iterm2.async_get_app(connection)

        if args.list:
            return await cmd_list(app)
        if args.reset:
            return await cmd_reset(app)
        if args.set:
            return await cmd_set(app, args.set)
        if args.demo:
            return await cmd_demo(app, args.demo_session, args.demo_pause)
        if args.capture:
            return await cmd_capture(app, args.capture, args.label)

        monitor = Cupline(connection, app, show_tail=args.tail, debounce=args.debounce)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, monitor.stop)
        log.info("watching. ctrl-c to stop; tab colour is restored on exit.")
        await monitor.run()

    try:
        iterm2.Connection().run_until_complete(entry, retry=False)
    except ConnectionRefusedError:
        # iTerm2 being closed is an expected launchd lifecycle state. Give the
        # exit guard a code it can acknowledge without masking real failures.
        raise SystemExit(os.EX_TEMPFAIL) from None


if __name__ == "__main__":
    main()

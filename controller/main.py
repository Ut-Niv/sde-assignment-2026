"""Entry point: python -m controller.main --url http://127.0.0.1:8700"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from controller.loop import Controller


def main() -> None:
    ap = argparse.ArgumentParser(description="Outcome-first agent loop controller")
    ap.add_argument("--url", default=os.environ.get("SIM_URL", "http://127.0.0.1:8700"))
    ap.add_argument("--state", default=os.environ.get("CONTROLLER_STATE", "controller_state.db"))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    log = logging.getLogger("controller")
    log.info("starting controller against %s (state=%s)", args.url, args.state)

    ctl = Controller(args.url, state_path=args.state)
    try:
        ctl.run()
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        ctl.api.close()
        log.info("controller exit")


if __name__ == "__main__":
    main()

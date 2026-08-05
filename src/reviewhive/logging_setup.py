"""Getting the lines that explain a decision out of the process.

Nothing configured logging before this module existed, so the root logger had no
handlers and sat at WARNING. That is what made the gap easy to miss: `warning`
and `exception` still reached stderr through Python's fallback handler, so
failures were visible and only the *explanations* were not. Every `logger.info`
was discarded — "delivery already recorded", "already reviewed", "ignoring a
mention from ..." — and those are the lines that say why a webhook returned 200
and did nothing. A real redelivery logged a bare `200 OK` with no reason, which
is the service's most common non-obvious behaviour and was undebuggable.

Two levels, not one. The handler goes on the *root* logger so any library's
records can reach it, but only `reviewhive` is lowered; the root stays at
WARNING. Lowering the root instead would also turn on an httpx line per request
and whatever else the dependencies say at INFO, burying the eleven lines this
exists to surface.
"""

from __future__ import annotations

import logging
import sys
from typing import TextIO

# A time a person reads while narrating a run, not a date they already know.
_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"

# Names the handler as ours so a second call replaces it rather than stacking a
# duplicate onto the root logger, which would print every line twice.
_HANDLER_NAME = "reviewhive"

# The one logger whose level this module lowers. Every logger in the package is
# `logging.getLogger(__name__)`, so they all descend from it.
PACKAGE_LOGGER = "reviewhive"


def configure_logging(level: str | int = "INFO", *, stream: TextIO | None = None) -> None:
    """Install one stderr handler on the root logger and open up `reviewhive`.

    Called from lifespan rather than at import — because reconfiguring the root
    logger is a process-wide side effect that merely importing a module should
    not have, not because uvicorn forces the order. That was checked rather than
    assumed: uvicorn runs `dictConfig` on its own config when the server starts,
    but that config has no `root` key, so the root logger keeps its level and its
    handlers, and it sets `disable_existing_loggers: False`, so the `reviewhive`
    logger survives. Both orders emit. The one thing that would break is a
    handler whose `close()` also closes its stream — `dictConfig` closes existing
    handlers, and `StreamHandler` pointedly does not close the stream it was
    given, which is why this stays a `StreamHandler` and does not become a
    `FileHandler` without revisiting the order.

    Idempotent, because the entry points are not mutually exclusive: a test, a
    script and a lifespan can each reasonably call it in one process.
    """
    root = logging.getLogger()
    for previous in [h for h in root.handlers if h.name == _HANDLER_NAME]:
        root.removeHandler(previous)
        previous.close()

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.name = _HANDLER_NAME
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    # Left at NOTSET deliberately. The level decision belongs to the loggers, so
    # a third-party WARNING and a reviewhive INFO each pass here on their own
    # merits; a level on the handler would override both at once.
    root.addHandler(handler)

    root.setLevel(logging.WARNING)
    logging.getLogger(PACKAGE_LOGGER).setLevel(level)

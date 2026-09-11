"""Per-request timing, so "the page feels slow" becomes a number.

Adds `Server-Timing` headers, which browsers show in the network panel, and
logs anything slower than the threshold. Cheap enough to leave on for a local
single-user tool, and it is the difference between measuring and guessing.
"""

from __future__ import annotations

import logging
import time

from django.db import connection

logger = logging.getLogger("fxmacro.timing")

#: Above this, the request is worth a line in the log with its slowest
#: statements. Set below the slowest page so a regression announces itself.
SLOW_REQUEST_MS = 900


class TimingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = time.perf_counter()
        queries_before = len(connection.queries_log)

        response = self.get_response(request)

        elapsed_ms = (time.perf_counter() - started) * 1000
        queries = len(connection.queries_log) - queries_before

        response["Server-Timing"] = f'total;dur={elapsed_ms:.0f}, queries;desc="{queries}"'
        if elapsed_ms >= SLOW_REQUEST_MS:
            logger.warning(
                "%s %s took %.0fms (%d queries logged)",
                request.method, request.get_full_path(), elapsed_ms, queries,
            )
            # Naming the slow statement is the whole point; a total alone sends
            # you guessing, which costs far more than the logging does.
            recent = list(connection.queries_log)[queries_before:]
            for entry in sorted(recent, key=lambda q: -float(q["time"]))[:3]:
                logger.warning("    %6.0fms  %s", float(entry["time"]) * 1000, entry["sql"][:220])
        return response

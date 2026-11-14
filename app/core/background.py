"""
Background-task helpers.

The agents are `async def` but make blocking calls (LLM SDK, DB, PDF
rendering). Run directly on the event loop they freeze every other request
for the 30-60s a pipeline takes. `in_thread` turns such a coroutine function
into a plain function, which Starlette's BackgroundTasks executes in its
thread pool, with a private event loop.
"""
import asyncio
import functools
import logging

logger = logging.getLogger(__name__)


def in_thread(coro_fn):
    @functools.wraps(coro_fn)
    def runner(*args, **kwargs):
        try:
            asyncio.run(coro_fn(*args, **kwargs))
        except Exception:
            logger.exception(f"Background task failed | task={coro_fn.__name__}")
    return runner

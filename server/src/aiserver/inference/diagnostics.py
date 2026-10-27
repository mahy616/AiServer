"""Request-scoped, content-free inference diagnostics in the existing service log."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging

_context = ContextVar("inference_diagnostic_context", default={})


@contextmanager
def inference_context(**fields):
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def record(stage, **facts):
    context = dict(_context.get())
    logger = context.pop("logger_name", "aiserver.inference")
    # Callers supply only fixed reason codes, counts and model settings. Never
    # pass exceptions, prompts, argument values, headers or thinking text here.
    logging.getLogger(logger).info("inference %s", json.dumps(
        {**context, "stage": stage, **facts}, ensure_ascii=False, separators=(",", ":"), allow_nan=False))

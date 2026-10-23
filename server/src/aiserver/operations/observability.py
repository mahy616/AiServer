"""Bounded rate accounting and metadata-only rotating service logs."""
from collections import OrderedDict, deque
import logging
from logging.handlers import RotatingFileHandler
import time


class RateLimits:
    def __init__(self):
        self.buckets = OrderedDict()

    def allow(self, key, limit):
        now = time.monotonic()
        bucket = self.buckets.setdefault(key, deque())
        self.buckets.move_to_end(key)
        while bucket and bucket[0] <= now - 60:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        # Identity keys come from authenticated users or fixed public categories.
        while len(self.buckets) > 10000:
            self.buckets.popitem(last=False)
        return True


def service_logger(settings):
    directory = settings.data_directory / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("aiserver." + str(settings.data_directory))
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RotatingFileHandler(directory / "service.log", maxBytes=settings.log_max_bytes,
                                  backupCount=settings.log_backup_count, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    return logger, handler

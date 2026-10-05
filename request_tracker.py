import time
from datetime import datetime


class RequestTracker:
    def __init__(self, max_retries, max_time, *, clock=None, log=None):
        self.retries = 0
        self.max_retries = max_retries
        self.max_time = max_time
        self.clock = clock if clock is not None else time.time
        self.log = log
        self.start_time = self.clock()

    def _log(self, message):
        if self.log is not None:
            self.log(message)
        else:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}")

    def retry(self):
        self.retries += 1

    def should_retry(self):
        if self.retries > self.max_retries:
            self._log("Max retries reached")
            return False
        elapsed_time = self.clock() - self.start_time
        if elapsed_time > self.max_time:
            self._log("Max time reached")
            return False
        return True

    def log_retry(self):
        self._log(f"Session retry: {self.retries}")

    def forgive_last_retry(self, poll_duration: float) -> None:
        """Exempt the last poll from the retry budget.

        Used for healthy-but-unusable responses (e.g. dates exist but
        none fall in the acceptable range). Those should poll forever
        on the same login, while real failures (network/empty/booking)
        still count toward max_retries/max_time.
        """
        self.retries = max(0, self.retries - 1)
        self.start_time += max(0.0, poll_duration)

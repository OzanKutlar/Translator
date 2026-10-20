"""Cancellation token shared by a batch's worker and its API calls.

The console's 'cancel' command sets the token. The API client checks it before every attempt,
during backoff and while waiting for a response, and then raises ApiCancelled.
"""

import threading

MAX_WAIT_SECONDS = 24 * 3600  # bounds a single wait; callers poll in much shorter steps


class ApiCancelled(Exception):
    """Raised when an API call stops because its batch was cancelled."""


class CancelToken:
    """Thread-safe one-way flag: once cancelled it stays cancelled."""

    def __init__(self):
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, seconds) -> bool:
        """Sleep up to seconds. Returns True as soon as the token is cancelled."""
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
            raise ValueError("seconds must be a non-negative number")
        return self._event.wait(min(float(seconds), MAX_WAIT_SECONDS))

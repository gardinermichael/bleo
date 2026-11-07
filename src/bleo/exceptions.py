from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math


class BleoError(Exception):
    """Base exception for bleo."""


class AuthError(BleoError):
    """The service rejected the supplied credentials or signing key."""


class RatelimitError(BleoError):
    def __init__(self, message='', *, retry_after=None):
        super().__init__(message)
        self.retry_after = None
        if retry_after is None:
            return
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            try:
                date = parsedate_to_datetime(retry_after)
            except (TypeError, ValueError):
                try:
                    date = datetime.fromisoformat(str(retry_after).replace('Z', '+00:00'))
                except ValueError:
                    return
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = (date - datetime.now(timezone.utc)).total_seconds()
        if math.isfinite(delay):
            self.retry_after = max(0.0, delay)


class APIError(BleoError):
    def __init__(self, message, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class RelayExhaustedError(BleoError):
    """Every relay failed and the direct request to the upstream failed too."""

    def __init__(self, message='', *, last_status=None):
        super().__init__(message)
        self.last_status = last_status


GatewayExhaustedError = RelayExhaustedError

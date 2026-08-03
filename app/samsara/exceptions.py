class SamsaraError(Exception):
    """Base Samsara integration error."""


class SamsaraAuthenticationError(SamsaraError):
    pass


class SamsaraPermissionError(SamsaraError):
    pass


class SamsaraRateLimitError(SamsaraError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class SamsaraNotFoundError(SamsaraError):
    pass


class SamsaraRequestError(SamsaraError):
    pass


class SamsaraResponseError(SamsaraError):
    pass


class SamsaraCursorError(SamsaraResponseError):
    pass


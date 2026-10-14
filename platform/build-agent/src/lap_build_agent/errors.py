class BuildError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str | None = None,
        status_code: int = 422,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.status_code = status_code
        self.retryable = retryable

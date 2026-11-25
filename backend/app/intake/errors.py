"""Intake failures that are the uploader's to fix, each with a stable code and a plain message."""


class IntakeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

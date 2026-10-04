"""Controlled diagnostics for the manually answered call experiment."""


class CallError(Exception):
    def __init__(self, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain

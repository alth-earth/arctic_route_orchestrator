"""Stable orchestration error codes for automation and run reports."""

from __future__ import annotations


class OrchestrationError(RuntimeError):
    """An expected fail-closed orchestration error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class ArtifactIntakeError(OrchestrationError):
    """A supplied A artifact cannot enter the formal chain."""


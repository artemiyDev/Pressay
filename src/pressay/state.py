"""Pure session state transitions for recording and transcription."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class SessionStatus(str, Enum):
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"


class InvalidTransition(RuntimeError):
    """Raised for an event that is invalid for the current session status."""


@dataclass(frozen=True, slots=True)
class SessionState:
    """Immutable state for one logical stream of dictation sessions.

    Every new recording receives a monotonically increasing ``session_id``.
    Methods ignore events carrying an older ID by returning ``self``.  That
    makes a late transcription result harmless after cancel, reset, or a newer
    recording.
    """

    session_id: int | None = None
    status: SessionStatus = SessionStatus.IDLE
    result: str | None = None
    error_message: str | None = None

    @property
    def active(self) -> bool:
        return self.status in {SessionStatus.RECORDING, SessionStatus.TRANSCRIBING}

    def start(self) -> "SessionState":
        """Start a new recording and supersede any older session."""

        next_id = 1 if self.session_id is None else self.session_id + 1
        return SessionState(session_id=next_id, status=SessionStatus.RECORDING)

    def begin_transcription(self, session_id: int) -> "SessionState":
        """Mark recording complete and allow a result for this session."""

        if session_id != self.session_id:
            return self
        self._require(SessionStatus.RECORDING, "begin transcription")
        return replace(self, status=SessionStatus.TRANSCRIBING)

    def accept_result(self, session_id: int, text: str) -> "SessionState":
        """Accept a current result; reject stale or post-cancel results."""

        if session_id != self.session_id or self.status in {
            SessionStatus.CANCELLED,
            SessionStatus.ERROR,
            SessionStatus.IDLE,
            SessionStatus.COMPLETED,
        }:
            return self
        self._require(SessionStatus.TRANSCRIBING, "accept result")
        if not isinstance(text, str):
            raise TypeError("result text must be a string")
        return replace(
            self,
            status=SessionStatus.COMPLETED,
            result=text,
            error_message=None,
        )

    def cancel(self, session_id: int) -> "SessionState":
        """Cancel the current active session, ignoring stale cancellation."""

        if session_id != self.session_id:
            return self
        if not self.active:
            return self
        return replace(
            self,
            status=SessionStatus.CANCELLED,
            result=None,
            error_message=None,
        )

    def fail(self, session_id: int, message: str) -> "SessionState":
        """Move the current active session to the error state."""

        if session_id != self.session_id:
            return self
        if not self.active:
            return self
        if not isinstance(message, str) or not message.strip():
            raise ValueError("error message must be a non-empty string")
        return replace(
            self,
            status=SessionStatus.ERROR,
            result=None,
            error_message=message.strip(),
        )

    def reset(self) -> "SessionState":
        """Return to idle while retaining the ID needed to reject old events."""

        return replace(
            self,
            status=SessionStatus.IDLE,
            result=None,
            error_message=None,
        )

    def _require(self, expected: SessionStatus, action: str) -> None:
        if self.status is not expected:
            raise InvalidTransition(
                f"cannot {action} while session is {self.status.value}; "
                f"expected {expected.value}"
            )

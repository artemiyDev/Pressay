from __future__ import annotations

import pytest

from pressay.state import InvalidTransition, SessionState, SessionStatus


def test_complete_session_lifecycle_is_immutable():
    idle = SessionState()
    recording = idle.start()
    transcribing = recording.begin_transcription(recording.session_id)
    completed = transcribing.accept_result(transcribing.session_id, "готово")

    assert idle.status is SessionStatus.IDLE
    assert recording.status is SessionStatus.RECORDING
    assert transcribing.status is SessionStatus.TRANSCRIBING
    assert completed.status is SessionStatus.COMPLETED
    assert completed.result == "готово"
    assert completed.session_id == 1


def test_new_session_rejects_late_result_from_superseded_session():
    first = SessionState().start()
    first_id = first.session_id
    first = first.begin_transcription(first_id)
    second = first.start()

    after_late_result = second.accept_result(first_id, "устаревший текст")

    assert after_late_result is second
    assert second.session_id == 2
    assert second.status is SessionStatus.RECORDING
    assert second.result is None


def test_cancel_rejects_result_even_for_same_session_id():
    state = SessionState().start()
    state = state.begin_transcription(state.session_id)
    cancelled = state.cancel(state.session_id)

    after_late_result = cancelled.accept_result(cancelled.session_id, "late")

    assert cancelled.status is SessionStatus.CANCELLED
    assert after_late_result is cancelled


def test_error_records_message_and_rejects_later_result():
    recording = SessionState().start()
    failed = recording.fail(recording.session_id, "  microphone disconnected  ")

    assert failed.status is SessionStatus.ERROR
    assert failed.error_message == "microphone disconnected"
    assert failed.accept_result(failed.session_id, "late") is failed


def test_invalid_current_transition_raises_but_stale_event_is_ignored():
    recording = SessionState().start()

    with pytest.raises(InvalidTransition):
        recording.accept_result(recording.session_id, "too early")

    assert recording.begin_transcription(999) is recording


def test_reset_retains_id_so_next_session_remains_monotonic():
    cancelled = SessionState().start().cancel(1)
    reset = cancelled.reset()
    next_recording = reset.start()

    assert reset.session_id == 1
    assert reset.status is SessionStatus.IDLE
    assert next_recording.session_id == 2

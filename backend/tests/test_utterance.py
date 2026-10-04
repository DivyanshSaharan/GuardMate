"""Pure energy endpointing tests; no audio runtime is imported or opened."""

import struct
from dataclasses import asdict

import pytest
from guardmate.cellular.call_errors import CallError
from guardmate.cellular.utterance import (
    FRAME_BYTES,
    NoSpeech,
    UtteranceConfig,
    UtteranceEndpointer,
    UtteranceTooLong,
    config_value,
)

QUIET = bytes(FRAME_BYTES)
VOICE = struct.pack("<hh", 1200, -1200) * (FRAME_BYTES // 4)


def feed(endpoint, frames):
    for frame in frames:
        result = endpoint.feed(frame)
        if result is not None:
            return result
    return endpoint.status


def test_defaults_and_worker_deadline_are_bounded():
    config = UtteranceConfig()
    assert config.idle_timeout_seconds == 15
    assert config.max_utterance_seconds == 10
    assert config.pre_roll_ms == 250 and config.end_silence_ms == 700
    assert config.worker_timeout == 30
    assert UtteranceConfig(idle_timeout_seconds=30).worker_timeout == 45
    assert config_value(asdict(config)) == config


@pytest.mark.parametrize("name", list(asdict(UtteranceConfig())))
@pytest.mark.parametrize("value", [True, False, 1.0, "1", None, -1, 100000])
def test_config_requires_bounded_integers_not_boolean(name, value):
    with pytest.raises(CallError):
        UtteranceConfig(**{name: value})


@pytest.mark.parametrize("value", [None, {}, {**asdict(UtteranceConfig()), "extra": 1}])
def test_complete_config_shape_required(value):
    with pytest.raises(CallError):
        config_value(value)


def test_incoherent_thresholds_and_insufficient_full_budget_refused():
    with pytest.raises(CallError):
        UtteranceConfig(start_rms=100, end_rms=200)
    with pytest.raises(CallError):
        UtteranceConfig(onset_ms=200, min_speech_ms=120)
    with pytest.raises(CallError):
        UtteranceConfig(max_utterance_seconds=1)
    with pytest.raises(CallError):
        UtteranceConfig(
            max_utterance_seconds=1,
            pre_roll_ms=0,
            onset_ms=20,
            min_speech_ms=1000,
            end_silence_ms=100,
        )


def test_pre_roll_retains_onset_and_ending_silence_without_clipping():
    endpoint = UtteranceEndpointer()
    assert feed(endpoint, [QUIET] * 20 + [VOICE] * 12 + [QUIET] * 35) == "utterance"
    pcm = endpoint.pcm
    assert pcm.endswith(QUIET * 35)
    assert VOICE * 12 in pcm
    assert len(pcm) <= 10 * 32000
    assert len(pcm) >= (12 + 35) * FRAME_BYTES


@pytest.mark.parametrize("pre_roll", [0, 20, 50, 250])
def test_zero_or_short_pre_roll_still_retains_all_sustained_onset(pre_roll):
    endpoint = UtteranceEndpointer(UtteranceConfig(pre_roll_ms=pre_roll))
    assert feed(endpoint, [VOICE] * 8 + [QUIET] * 35) == "utterance"
    assert endpoint.pcm.startswith(VOICE * 8)


def test_odd_onset_milliseconds_round_up_to_whole_analysis_frames():
    endpoint = UtteranceEndpointer(UtteranceConfig(onset_ms=81, pre_roll_ms=0))
    assert feed(endpoint, [VOICE] * 8 + [QUIET] * 35) == "utterance"
    assert endpoint.pcm.startswith(VOICE * 8)


def test_single_noise_spikes_and_short_burst_do_not_become_an_utterance():
    endpoint = UtteranceEndpointer()
    # Four onset frames are shorter than the 120ms minimum; the stream stays open.
    assert feed(endpoint, [QUIET] * 10 + [VOICE] * 4 + [QUIET] * 35) is None
    assert feed(endpoint, [QUIET] * 100 + [VOICE] * 12 + [QUIET] * 35) == "utterance"
    assert VOICE * 12 in endpoint.pcm


def test_silence_and_constant_dc_noise_are_known_no_speech():
    for frame in (QUIET, struct.pack("<h", 1200) * 320):
        endpoint = UtteranceEndpointer(UtteranceConfig(idle_timeout_seconds=1))
        assert feed(endpoint, [frame] * 50) == "no_speech"
        with pytest.raises(CallError):
            _ = endpoint.pcm


def test_pause_shorter_than_end_silence_does_not_end_utterance():
    endpoint = UtteranceEndpointer()
    assert feed(endpoint, [VOICE] * 8 + [QUIET] * 30) is None
    assert feed(endpoint, [VOICE] * 8 + [QUIET] * 35) == "utterance"
    assert VOICE * 8 + QUIET * 30 + VOICE * 8 in endpoint.pcm


def test_ongoing_speech_at_bound_is_rejected_and_partial_audio_discarded():
    endpoint = UtteranceEndpointer()
    assert feed(endpoint, [VOICE] * 500) == "too_long"
    with pytest.raises(CallError):
        _ = endpoint.pcm
    assert not endpoint._clip


def test_capture_budget_includes_waiting_pre_roll_and_ending_silence():
    endpoint = UtteranceEndpointer(
        UtteranceConfig(max_utterance_seconds=1, pre_roll_ms=100, end_silence_ms=200)
    )
    # Speech has not ended before the full WAV limit; no clipped WAV is returned.
    assert feed(endpoint, [QUIET] * 10 + [VOICE] * 48 + [QUIET] * 10) == "too_long"


def test_partial_callback_frames_join_without_audio_gaps():
    endpoint = UtteranceEndpointer()
    source = QUIET * 10 + VOICE * 12 + QUIET * 35
    for offset in range(0, len(source), 160):
        if endpoint.feed(source[offset : offset + 160]) is not None:
            break
    assert endpoint.status == "utterance"
    assert VOICE * 12 in endpoint.pcm


@pytest.mark.parametrize(
    "pcm",
    [b"", b"x", b"xxx", bytes(32002), None, "pcm"],
    ids=["empty", "odd-one", "odd-three", "oversized", "none", "text"],
)
def test_malformed_or_huge_callback_pcm_refused(pcm):
    with pytest.raises(CallError):
        UtteranceEndpointer().feed(pcm)


def test_endpoint_cannot_accept_more_audio_after_known_outcome():
    endpoint = UtteranceEndpointer(UtteranceConfig(idle_timeout_seconds=1))
    assert feed(endpoint, [QUIET] * 50) == "no_speech"
    with pytest.raises(CallError):
        endpoint.feed(QUIET)


def test_typed_outcomes_are_known_not_uncertain():
    assert NoSpeech("No speech detected").uncertain is False
    assert UtteranceTooLong("Utterance too long").uncertain is False

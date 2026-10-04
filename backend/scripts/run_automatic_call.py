"""Bounded automatic turns on a manually answered, consenting fictional call.

Default: read-only readiness/device inspection. --run needs separate consent for
unreviewed automatic transcript uploads, a TTY and AUTO CALL READY. Never answers
or hangs up a phone, retries a turn, or starts another call automatically.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from guardmate.cellular.call_errors import CallError  # noqa: E402
from guardmate.cellular.call_runner import (  # noqa: E402
    ManualCallRunner,
    phone_device,
    require_ready,
)


def event(value: dict) -> None:
    # ASR/model text is untrusted terminal content, not an encrypted log.
    print(json.dumps(value, ensure_ascii=True), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--backend", default="http://127.0.0.1:8765")
    parser.add_argument("--input-id", type=int)
    parser.add_argument("--output-id", type=int)
    parser.add_argument("--max-turns", type=int, default=5)
    parser.add_argument("--time-limit", type=int, default=180)
    parser.add_argument("--label", default="Automatic fictional cellular test")
    parser.add_argument("--ack-consenting-test-call", action="store_true")
    parser.add_argument("--ack-exclusive-audio-risk", action="store_true")
    parser.add_argument("--ack-hosted-transcripts", action="store_true")
    parser.add_argument(
        "--ack-automatic-transcripts",
        action="store_true",
        help="Unreviewed ASR text is uploaded automatically; supervised fictional testing only.",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.max_turns <= 5:
        parser.error("Automatic fictional tests allow 1 to 5 attempted model turns.")
    if not 30 <= args.time_limit <= 600:
        parser.error("Choose an admission deadline from 30 to 600 seconds.")
    if not args.label.strip() or len(args.label) > 80:
        parser.error("Use a fictional label of 1 to 80 characters, not a phone number.")
    if args.run:
        if not all(
            (
                args.ack_consenting_test_call,
                args.ack_exclusive_audio_risk,
                args.ack_hosted_transcripts,
                args.ack_automatic_transcripts,
            )
        ):
            parser.error(
                "--run needs all four acknowledgments: --ack-consenting-test-call, "
                "--ack-exclusive-audio-risk, --ack-hosted-transcripts and "
                "--ack-automatic-transcripts. Unlike manual mode, unreviewed ASR text, "
                "history and resident context are automatically sent to Tinker. "
                "Supervised fictional calls only; ASR is not reliable enough for real couriers."
            )
        if (
            args.input_id is None
            or args.output_id is None
            or min(args.input_id, args.output_id) < 0
        ):
            parser.error("Select fresh explicit phone --input-id and --output-id values.")
        if not sys.stdin.isatty():
            parser.error(
                "Automatic testing requires an interactive terminal; do not pipe a script."
            )

    runner = None
    try:
        # Missing acknowledgments/TTY are rejected before imports/native discovery/HTTP.
        from guardmate.cellular.backend_client import BackendClient
        from guardmate.cellular.call_audio import reply_audio
        from guardmate.cellular.utterance_audio import UtteranceCallAudio

        backend = BackendClient(args.backend)
        audio = UtteranceCallAudio(
            ROOT,
            consent=args.ack_consenting_test_call,
            exclusive_risk=args.ack_exclusive_audio_risk,
        )
        status = backend.inspect()
        devices = audio.devices()
        event(
            {
                "event": "inspection",
                "readiness": status,
                "phone_devices": [
                    {"id": item.id, "name": item.name, "direction": item.direction}
                    for item in devices
                    if "vivo T2x 5G".casefold() in item.name.casefold()
                ],
                "audio_stream_started": False,
                "model_requests_sent": 0,
                "physical_call_state_verified": False,
            }
        )
        if not args.run:
            return 0
        require_ready(status)
        selected_input = phone_device(devices, args.input_id, "input")
        selected_output = phone_device(devices, args.output_id, "output")
        event(
            {
                "event": "automatic_test_boundaries",
                "input_id": selected_input.id,
                "output_id": selected_output.id,
                "max_model_attempts": args.max_turns,
                "admission_deadline_seconds": args.time_limit,
                "operator_transcript_review": False,
                "hosted_data": "Unreviewed ASR text, conversation history and resident context.",
                "raw_audio_uploaded_to_tinker": False,
                "asr_confidence_available": False,
                "answer_and_hangup": "Manual Phone Link controls only.",
            }
        )
        print(
            "Supervised FICTIONAL calls only. Both people must agree BEFORE starting: "
            "local call capture and AUTOMATIC upload of unreviewed ASR text, history and "
            "saved resident context to hosted Qwen/Tinker. No per-turn review/send.\n"
            "Whisper can mishear or invent words; energy detection is not ASR confidence. "
            "Keep this terminal visible. Ctrl+C stops native work and attempts to end only "
            "the known saved conversation, NOT the physical call.\n"
            "Answer the agreed call on the PC. Keep the phone off speakerphone; mute only "
            "the laptop microphone, not the call. WDM-KS may interrupt human audio.\n"
            "Wait for the local utterance_armed event before speaking. There is no "
            "caller-audible ready beep or barge-in. Stay quiet during replies.\n"
            "Type AUTO CALL READY only for this same active, agreed fictional call. "
            "Anything else exits without a session or audio.",
            flush=True,
        )
        if input("Confirm> ").strip() != "AUTO CALL READY":
            event({"event": "cancelled", "session_created": False, "model_requests_sent": 0})
            return 0

        from guardmate.cellular.automatic_call import AutomaticCallCoordinator

        runner = ManualCallRunner(
            backend,
            audio,
            selected_input,
            selected_output,
            convert_reply=reply_audio,
            seconds=10,
            max_turns=args.max_turns,
            hosted_consent=args.ack_hosted_transcripts,
            emit=event,
        )
        coordinator = AutomaticCallCoordinator(
            runner,
            audio=audio,
            automatic_submission_consent=args.ack_automatic_transcripts,
            max_seconds=args.time_limit,
            emit=event,
        )
        result = coordinator.run(args.label.strip())
        event(
            {
                "event": "automatic_run_stopped",
                "status": result.status,
                "reason": result.reason,
                "session_id": result.session_id,
                "model_attempts": result.model_attempts,
                "physical_call_ended": False,
            }
        )
        print(
            "Automatic listening has stopped. Pending resident decisions stay saved; "
            "do not leave the caller waiting. Handle/end the physical call in Phone Link "
            "and inspect/end the saved session in the app as appropriate.",
            flush=True,
        )
        return 0
    except (KeyboardInterrupt, EOFError):
        if runner is not None:
            try:
                runner.stop()
            except CallError:
                event(
                    {
                        "event": "session_end_unconfirmed",
                        "session_id": runner.session.id if runner.session else None,
                    }
                )
        print(
            "Stopped. An in-flight backend job may still finish; no retry or replay. "
            "Physical hangup is manual.",
            flush=True,
        )
        return 130
    except CallError as error:
        event({"event": "error", "message": str(error), "uncertain": error.uncertain})
        event(
            {
                "event": "automatic_run_needs_manual_review",
                "session_id": runner.session.id if runner and runner.session else None,
                "automatic_retry": False,
            }
        )
        # Do not end/decline an awaiting approval or retry an uncertain POST.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Explicit operator-controlled turns on an already answered Phone Link test call.

Default: metadata/readiness only. --run requires three acknowledgments and a
terminal confirmation. Never answers, hangs up, listens continuously or retries.
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
    # Terminal-escape/control characters in untrusted caller/assistant text are
    # escaped. A visible local terminal is not an encrypted/private history store.
    print(json.dumps(value, ensure_ascii=True), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--backend", default="http://127.0.0.1:8765")
    parser.add_argument("--input-id", type=int)
    parser.add_argument("--output-id", type=int)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--label", default="Manual cellular test")
    parser.add_argument("--ack-consenting-test-call", action="store_true")
    parser.add_argument("--ack-exclusive-audio-risk", action="store_true")
    parser.add_argument("--ack-hosted-transcripts", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= 10:
        parser.error("Choose 1 to 10 seconds per captured turn.")
    if not 1 <= args.max_turns <= 10:
        parser.error("Choose a limit from 1 to 10 model turns.")
    if not args.label.strip() or len(args.label) > 80:
        parser.error("Use a fictional test label of 1 to 80 characters, not a phone number.")
    if args.run:
        if not all(
            (
                args.ack_consenting_test_call,
                args.ack_exclusive_audio_risk,
                args.ack_hosted_transcripts,
            )
        ):
            parser.error(
                "--run requires --ack-consenting-test-call, --ack-exclusive-audio-risk and "
                "--ack-hosted-transcripts. Reviewed text, history and saved resident "
                "context go to Tinker."
            )
        if (
            args.input_id is None
            or args.output_id is None
            or min(args.input_id, args.output_id) < 0
        ):
            parser.error(
                "Select explicit phone --input-id and --output-id from a fresh inspection."
            )
        if not sys.stdin.isatty():
            parser.error(
                "Live mode requires an interactive terminal; do not pipe commands into it."
            )
    runner = None
    try:
        # Lazy imports: parsing missing acknowledgments never starts native audio
        # discovery, loads model credentials or contacts the backend.
        from guardmate.cellular.backend_client import BackendClient
        from guardmate.cellular.call_audio import LocalCallAudio, reply_audio

        backend = BackendClient(args.backend)
        audio = LocalCallAudio(
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
                "event": "live_test_boundaries",
                "input_id": selected_input.id,
                "output_id": selected_output.id,
                "max_turns": args.max_turns,
                "capture_seconds": args.seconds,
                "raw_audio_upload_to_tinker": False,
                "hosted_data": "Reviewed text, conversation history and resident delivery context.",
                "text_retention": "Local conversation history; terminal text may remain visible.",
                "answer_and_hangup": "Manual Phone Link controls only.",
            }
        )
        print(
            "Type CALL READY only when both people agree and this same call is active. "
            "Anything else exits.",
            flush=True,
        )
        if input("Confirm> ").strip() != "CALL READY":
            event({"event": "cancelled", "session_created": False, "model_requests_sent": 0})
            return 0
        runner = ManualCallRunner(
            backend,
            audio,
            selected_input,
            selected_output,
            convert_reply=reply_audio,
            seconds=args.seconds,
            max_turns=args.max_turns,
            hosted_consent=args.ack_hosted_transcripts,
            emit=event,
        )
        runner.start(args.label.strip())
        print(
            "Commands: listen (record/transcribe locally), edit (correct draft), send "
            "(send reviewed text to Qwen and speak checked reply), discard, speak "
            "(an unplayed saved reply), repeat (explicitly replay), "
            "refresh (discard draft), stop.\n"
            "Keep the caller quiet while GuardMate speaks. "
            "This is turn-taking, not continuous listening.",
            flush=True,
        )
        while not runner.stopped:
            command = input("GuardMate> ").strip().casefold()
            try:
                if command == "listen":
                    runner.listen()
                elif command == "send":
                    runner.send()
                elif command == "edit":
                    runner.edit(input("Reviewed courier text> "))
                elif command == "discard":
                    runner.discard()
                elif command in ("speak", "repeat"):
                    runner.speak(repeat=command == "repeat")
                elif command == "refresh":
                    runner.refresh()
                elif command == "stop":
                    runner.stop()
                else:
                    event({"event": "unknown_command", "model_requests_sent": 0})
            except CallError as error:
                event({"event": "error", "message": str(error), "uncertain": error.uncertain})
                if error.uncertain or runner.uncertain:
                    print(
                        "Outcome uncertain. No resend/retry or further audio is allowed. "
                        "Handle the call manually and inspect/end the known session in the app.",
                        flush=True,
                    )
                    return 1
        print(
            "Runner stopped. End the physical call yourself in Phone Link when appropriate.",
            flush=True,
        )
        return 0
    except (KeyboardInterrupt, EOFError):
        # Native supervisor stops its own child before propagating Ctrl+C. End
        # only the known session; never guess an ID after an uncertain start.
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
            "Stopped. Playback may have been partial; no automatic retry. "
            "Physical hangup is manual."
        )
        return 130
    except CallError as error:
        event({"event": "error", "message": str(error), "uncertain": error.uncertain})
        if runner is not None and runner.session is not None:
            event({"event": "saved_session_needs_manual_review", "session_id": runner.session.id})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

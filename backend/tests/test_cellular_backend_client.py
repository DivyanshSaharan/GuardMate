import hashlib
import io
import json
import wave
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from guardmate.agent.models import Conversation, Message, ModelStatus
from guardmate.cellular.backend_client import (
    MAX_JSON_BYTES,
    MAX_RESPONSE_BYTES,
    BackendClient,
    _fingerprint,
)
from guardmate.cellular.call_errors import CallError
from guardmate.context import build_context
from guardmate.models import Dashboard, DeliveryMode, ResidentProfile

NOW = datetime(2026, 10, 5, 5, tzinfo=UTC)
PRIVATE = "PRIVATE_NATIVE_ERROR_AND_SECRET"


def dashboard(*, enabled=True, location="Guard room"):
    profile = ResidentProfile(resident_name="Resident", pg_name="PG", guard_location=location)
    mode = DeliveryMode(enabled=enabled, expires_at=NOW + timedelta(hours=1))
    return Dashboard(
        profile=profile,
        delivery_mode=mode,
        context=build_context(profile, mode, None, NOW),
        server_time=NOW,
    )


def session(*, plan=None, revision=0):
    plan = plan or dashboard()
    return Conversation(
        id=str(uuid4()),
        courier_label="Courier",
        created_at=NOW,
        revision=revision,
        dialogue_version=1,
        messages=[Message(role="assistant", content="Is this prepaid?", at=NOW)],
        reply_context_fingerprint=_fingerprint(plan),
    )


def json_bytes(value):
    if hasattr(value, "model_dump_json"):
        return value.model_dump_json().encode()
    return json.dumps(value).encode()


class Response:
    def __init__(self, value, *, status=200, content_type="application/json", length=True):
        self.data = value if isinstance(value, bytes) else json_bytes(value)
        self.status = status
        self.headers = {"content-type": content_type}
        if length is True:
            self.headers["content-length"] = str(len(self.data))
        elif length is not False:
            self.headers["content-length"] = length
        self.read_sizes = []

    def getheader(self, key, default=None):
        return self.headers.get(key.lower(), default)

    def read(self, size):
        self.read_sizes.append(size)
        return self.data[:size]


class Factory:
    def __init__(self, *responses, failure=None):
        self.responses = list(responses)
        self.failure = failure
        self.created = []
        self.requests = []
        self.closed = 0

    def __call__(self, host, port, *, timeout):
        self.created.append((host, port, timeout))
        factory = self

        class Connection:
            def request(self, method, path, *, body, headers):
                factory.requests.append((method, path, body, headers))
                if factory.failure:
                    raise factory.failure

            def getresponse(self):
                assert factory.responses, "Unexpected HTTP operation"
                return factory.responses.pop(0)

            def close(self):
                factory.closed += 1

        return Connection()


def client(*responses, failure=None):
    factory = Factory(*responses, failure=failure)
    return BackendClient(connection_factory=factory), factory


def wav(*, rate=16000, seconds=0.1):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"\x00\x01\x00\xff" * int(rate * seconds / 2))
    return buffer.getvalue()


@pytest.mark.parametrize(
    "url,host,port",
    [
        ("http://127.0.0.1:8765", "127.0.0.1", 8765),
        ("http://127.0.0.1/", "127.0.0.1", 80),
        ("http://[::1]:8765/", "::1", 8765),
    ],
)
def test_only_literal_loopback_connections(url, host, port, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No actual HTTP connections allowed in tests")

    monkeypatch.setattr("http.client.HTTPConnection", forbidden)
    factory = Factory(Response(session()))
    backend = BackendClient(url, connection_factory=factory)
    item = json.loads(factory.responses[0].data)
    backend.read(item["id"])
    assert factory.created == [(host, port, 60)]
    assert factory.closed == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8765",
        "http://localhost:8765",
        "http://example.com",
        "http://127.0.0.2",
        "http://127.1",
        "http://2130706433",
        "http://[::ffff:127.0.0.1]",
        "http://127.0.0.1/api",
        "http://127.0.0.1//",
        "http://127.0.0.1?x=1",
        "http://127.0.0.1#x",
        "http://user:password@127.0.0.1",
        " http://127.0.0.1",
        "http://127.0.0.1\n",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:",
        "http://[::1%25zone]",
    ],
)
def test_rejects_urls_before_connection(url):
    factory = Factory()
    with pytest.raises(CallError) as caught:
        BackendClient(url, connection_factory=factory)
    assert "password" not in str(caught.value)
    assert not factory.created


@pytest.mark.parametrize("timeout", [True, 0, -1, float("inf"), float("nan"), "60"])
def test_rejects_invalid_timeout(timeout):
    with pytest.raises(CallError):
        BackendClient(timeout=timeout)


def test_inspect_returns_only_safe_typed_status_fields():
    model = ModelStatus(
        configured=True,
        model="Qwen/test",
        provider="test",
        message=PRIVATE,
        reserved_usd=0.02,
        budget_usd=0.25,
    )
    backend, factory = client(
        Response(dashboard()),
        Response(model),
        Response({"stt_ready": True, "tts_ready": True, "private": PRIVATE}),
    )
    assert backend.inspect() == {
        "setup_complete": True,
        "delivery_mode_active": True,
        "model_configured": True,
        "stt_ready": True,
        "tts_ready": True,
        "model": "Qwen/test",
        "reserved_usd": 0.02,
        "budget_usd": 0.25,
    }
    assert [item[:2] for item in factory.requests] == [
        ("GET", "/api/dashboard"),
        ("GET", "/api/agent/status"),
        ("GET", "/api/speech/status"),
    ]


@pytest.mark.parametrize("field,value", [("setup_complete", 1), ("delivery_mode_active", "true")])
def test_inspect_dashboard_bools_are_strict(field, value):
    data = dashboard().model_dump(mode="json")
    data["context"][field] = value
    backend, _ = client(Response(data))
    with pytest.raises(CallError):
        backend.inspect()


@pytest.mark.parametrize("value", [1, "true", None])
def test_inspect_speech_bools_are_strict(value):
    model = ModelStatus(
        configured=True, model="test", provider="test", message="", reserved_usd=0, budget_usd=0.25
    )
    backend, _ = client(
        Response(dashboard()), Response(model), Response({"stt_ready": value, "tts_ready": True})
    )
    with pytest.raises(CallError):
        backend.inspect()


def test_start_is_new_trimmed_label_and_body_has_no_id():
    item = session()
    backend, factory = client(Response(item, status=201), Response(item, status=201))
    assert backend.start(" Courier ").id == item.id
    assert json.loads(factory.requests[0][2]) == {"courier_label": "Courier"}
    with pytest.raises(CallError) as caught:
        backend.start("Courier")
    assert caught.value.uncertain


@pytest.mark.parametrize("label", [None, 123, "x" * 81])
def test_invalid_label_does_not_send(label):
    backend, factory = client()
    with pytest.raises(CallError):
        backend.start(label)
    assert not factory.requests


@pytest.mark.parametrize("revision", [True, "0", 0.0, -1])
def test_response_revision_is_strict(revision):
    item = session()
    data = item.model_dump(mode="json")
    data["revision"] = revision
    backend, _ = client(Response(data))
    with pytest.raises(CallError):
        backend.read(item.id)


def test_read_rejects_wrong_id_and_input_injection():
    item = session()
    backend, factory = client(Response(session()))
    with pytest.raises(CallError, match="different conversation"):
        backend.read(item.id)
    with pytest.raises(CallError):
        backend.read(item.id + "/turns")
    assert len(factory.requests) == 1


def next_turn(item, text="Yes"):
    updated = item.model_copy(deep=True)
    updated.revision += 1
    updated.turn_count += 1
    updated.messages.extend(
        [
            Message(role="courier", content=text, at=NOW),
            Message(role="assistant", content="Is security present?", at=NOW),
        ]
    )
    return updated


def test_send_verifies_saved_turn_and_revision():
    item = session()
    updated = next_turn(item)
    backend, factory = client(Response(updated))
    assert backend.send(item, " Yes ") == updated
    assert json.loads(factory.requests[0][2]) == {"revision": 0, "text": " Yes "}


def test_send_passes_exact_captured_context_without_fetching_a_new_one():
    item = session()
    token = "a" * 64
    backend, factory = client(Response(next_turn(item)))
    backend.send(item, "Yes", expected_context=token)
    assert len(factory.requests) == 1
    assert json.loads(factory.requests[0][2]) == {
        "revision": 0,
        "text": "Yes",
        "expected_context": token,
    }


@pytest.mark.parametrize("token", ["", "a" * 63, "a" * 65, "g" * 64, 12, True, b"a" * 64])
def test_send_refuses_invalid_captured_context_before_http(token):
    backend, factory = client()
    with pytest.raises(CallError) as caught:
        backend.send(session(), "Yes", expected_context=token)
    assert not caught.value.uncertain
    assert not factory.requests


def test_send_never_silently_ignores_context_field(monkeypatch):
    from pydantic import BaseModel

    class IgnoringRequest(BaseModel):
        revision: int
        text: str

    monkeypatch.setattr("guardmate.cellular.backend_client.TurnRequest", IgnoringRequest)
    backend, factory = client()
    with pytest.raises(CallError, match="binding could not be validated"):
        backend.send(session(), "Yes", expected_context="a" * 64)
    assert not factory.requests


@pytest.mark.parametrize("change", ["id", "revision", "text", "history", "last_role"])
def test_send_rejects_unverifiable_mutation_without_retry(change):
    item = session()
    updated = next_turn(item)
    if change == "id":
        updated.id = str(uuid4())
    elif change == "revision":
        updated.revision = item.revision
    elif change == "text":
        updated.messages[-2].content = "Different text"
    elif change == "history":
        updated.messages[0].content = "Changed question"
    else:
        updated.messages[-1].role = "resident"
    backend, factory = client(Response(updated))
    with pytest.raises(CallError) as caught:
        backend.send(item, "Yes")
    assert caught.value.uncertain
    assert len(factory.requests) == 1


@pytest.mark.parametrize("text", ["", " \n ", "x" * 601, 12])
def test_invalid_turn_is_not_sent(text):
    backend, factory = client()
    with pytest.raises(CallError) as caught:
        backend.send(session(), text)
    assert not caught.value.uncertain
    assert not factory.requests


def test_fingerprint_matches_documented_engine_algorithm():
    plan = dashboard()
    expected = hashlib.sha256(
        (plan.profile.model_dump_json() + plan.context.availability.value).encode()
    ).hexdigest()
    assert _fingerprint(plan) == expected


@pytest.mark.parametrize(
    "change", ["revision", "text", "index", "fingerprint", "disabled", "profile"]
)
def test_freshness_fails_on_changed_reply_or_context(change):
    item = session()
    current = item.model_copy(deep=True)
    plan = dashboard()
    if change == "revision":
        current.revision += 1
    elif change == "text":
        current.messages[-1].content = "Changed"
    elif change == "index":
        current.messages.append(current.messages[-1].model_copy())
    elif change == "fingerprint":
        current.reply_context_fingerprint = "changed"
    elif change == "disabled":
        plan = dashboard(enabled=False)
    else:
        plan = dashboard(location="Different room")
    backend, _ = client(Response(current), Response(plan))
    with pytest.raises(CallError):
        backend.ensure_fresh(item)


def test_synthesize_checks_freshness_before_and_after_and_latest_index():
    item = next_turn(session())
    audio = wav()
    backend, factory = client(
        Response(item),
        Response(dashboard()),
        Response(audio, content_type="audio/wav"),
        Response(item),
        Response(dashboard()),
    )
    assert backend.synthesize(item) == audio
    assert json.loads(factory.requests[2][2]) == {"revision": 1, "message_index": 2}
    assert len(factory.requests) == factory.closed == 5


def test_synthesize_discards_audio_if_reply_changes_during_generation():
    item = session()
    changed = item.model_copy(deep=True)
    changed.messages[-1].content = "Changed"
    backend, _ = client(
        Response(item),
        Response(dashboard()),
        Response(wav(), content_type="audio/wav"),
        Response(changed),
        Response(dashboard()),
    )
    with pytest.raises(CallError):
        backend.synthesize(item)


def test_synthesize_resident_latest_message_never_posts():
    item = session()
    item.messages.append(Message(role="resident", content="Approved", at=NOW))
    backend, factory = client()
    with pytest.raises(CallError):
        backend.synthesize(item)
    assert not factory.requests


def test_context_token_allows_resident_latest_and_updated_profile():
    item = session()
    item.messages.append(Message(role="resident", content="Approved", at=NOW))
    changed_plan = dashboard(location="New guard room")
    backend, _ = client(Response(item), Response(changed_plan))
    assert backend.context_token(item) == _fingerprint(changed_plan)
    assert item.reply_context_fingerprint != _fingerprint(changed_plan)


@pytest.mark.parametrize("change", ["revision", "text", "role", "mode", "setup"])
def test_context_token_rejects_draft_question_or_plan_changes(change):
    item = session()
    current = item.model_copy(deep=True)
    plan = dashboard()
    if change == "revision":
        current.revision += 1
    elif change == "text":
        current.messages[-1].content = "New question"
    elif change == "role":
        current.messages[-1].role = "resident"
    elif change == "mode":
        plan = dashboard(enabled=False)
    else:
        plan.context.setup_complete = False
    backend, _ = client(Response(current), Response(plan))
    with pytest.raises(CallError):
        backend.context_token(item)


def test_transcribe_is_local_audio_route_and_filters_metadata():
    audio = wav()
    backend, factory = client(
        Response(
            {
                "text": " Yes ",
                "duration_ms": 100,
                "processing_ms": 20,
                "private": PRIVATE,
            }
        )
    )
    assert backend.transcribe(audio) == {"text": " Yes ", "duration_ms": 100, "processing_ms": 20}
    method, path, body, headers = factory.requests[0]
    assert (method, path, body) == ("POST", "/api/speech/transcribe", audio)
    assert headers["Content-Type"] == "audio/wav"


@pytest.mark.parametrize(
    "audio",
    [b"bad", wav(rate=8000), wav(seconds=10.1), bytearray(wav())],
    ids=["malformed", "wrong-rate", "too-long", "wrong-type"],
)
def test_transcribe_rejects_audio_before_http(audio):
    backend, factory = client()
    with pytest.raises(CallError):
        backend.transcribe(audio)
    assert not factory.requests


@pytest.mark.parametrize(
    "field,value",
    [
        ("text", " \n "),
        ("text", "x" * 601),
        ("text", 123),
        ("duration_ms", True),
        ("duration_ms", "100"),
        ("duration_ms", 0),
        ("duration_ms", 10001),
        ("processing_ms", True),
        ("processing_ms", -1),
    ],
)
def test_transcribe_validates_text_and_integer_timing(field, value):
    result = {"text": "Yes", "duration_ms": 100, "processing_ms": 0}
    result[field] = value
    backend, _ = client(Response(result))
    with pytest.raises(CallError):
        backend.transcribe(wav())


def ended(item):
    updated = item.model_copy(deep=True)
    updated.revision += 1
    updated.status = "ended"
    updated.authorized_location = None
    updated.messages.append(Message(role="resident", content="Ended", at=NOW))
    return updated


def test_end_is_revision_bound_resident_end_not_approval():
    item = session()
    backend, factory = client(Response(ended(item)))
    assert backend.end(item).status == "ended"
    assert json.loads(factory.requests[0][2]) == {"decision": "end", "revision": 0}
    assert factory.requests[0][:2] == ("PUT", f"/api/conversations/{item.id}/resident")


def test_end_rejects_stale_success():
    item = session()
    updated = ended(item)
    updated.revision = item.revision
    backend, _ = client(Response(updated))
    with pytest.raises(CallError) as caught:
        backend.end(item)
    assert caught.value.uncertain


@pytest.mark.parametrize("failure", [TimeoutError(PRIVATE), OSError(PRIVATE)])
@pytest.mark.parametrize("mutation", [False, True])
def test_network_failure_is_controlled_and_uncertain_only_for_mutation(failure, mutation):
    backend, factory = client(failure=failure)
    with pytest.raises(CallError) as caught:
        backend.start("Courier") if mutation else backend.read(str(uuid4()))
    assert caught.value.uncertain is mutation
    assert PRIVATE not in str(caught.value)
    assert len(factory.requests) == factory.closed == 1


@pytest.mark.parametrize("status", [302, 401, 404, 409, 500])
def test_http_errors_do_not_read_private_bodies_or_follow_redirects(status):
    response = Response({"detail": PRIVATE}, status=status)
    backend, factory = client(response)
    with pytest.raises(CallError) as caught:
        backend.start("Courier")
    assert PRIVATE not in str(caught.value)
    assert not response.read_sizes
    assert len(factory.requests) == 1


@pytest.mark.parametrize(
    "response",
    [
        Response(b"x" * (MAX_JSON_BYTES + 1), length=False),
        Response(b"{}", length=str(MAX_JSON_BYTES + 1)),
        Response(b"{}", length="invalid"),
        Response(b"{}", length="100"),
        Response(b"", length=False),
        Response(b"not-json"),
        Response(b"{}", content_type="text/plain"),
    ],
)
def test_response_bounds_and_invalid_content_are_controlled(response):
    backend, _ = client(response)
    with pytest.raises(CallError):
        backend.read(str(uuid4()))
    assert all(size <= MAX_JSON_BYTES + 1 for size in response.read_sizes)


def test_speech_response_bound_is_two_mb():
    item = session()
    response = Response(b"x" * (MAX_RESPONSE_BYTES + 1), content_type="audio/wav", length=False)
    backend, _ = client(Response(item), Response(dashboard()), response)
    with pytest.raises(CallError):
        backend.synthesize(item)
    assert response.read_sizes == [MAX_RESPONSE_BYTES + 1]

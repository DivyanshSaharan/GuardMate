import pytest
from guardmate.agent.dialogue import confirmation


@pytest.mark.parametrize(
    ("field", "text", "question", "expected"),
    [
        ("guard_available", "Security is not available", "guard_available", False),
        ("guard_available", "The guard is not present", "guard_available", False),
        ("guard_available", "There isn't a guard", "guard_available", False),
        ("guard_available", "Nobody is there", "guard_available", False),
        ("guard_available", "The guard will be there", "guard_available", None),
        ("guard_available", "Maybe security is here", "guard_available", None),
        ("guard_available", "Is the guard here?", "guard_available", None),
        ("prepaid", "Is this prepaid?", "prepaid", None),
        ("prepaid", "It will be paid for", "prepaid", None),
        ("prepaid", "yes", "prepaid", True),
        ("guard_available", "yes", "guard_available", True),
        ("guard_available", "yes he is there", "guard_available", True),
        ("guard_available", "yes", None, None),
        ("prepaid", "yes", "guard_available", None),
    ],
)
def test_only_grounded_current_answers_update_memory(field, text, question, expected):
    assert confirmation(field, text, question) is expected

"""A two-segment link token is redacted the same wherever it starts.

The two-segment alternative in the credential pattern has a left boundary that
keeps `eyJ` from matching inside a longer word (`keyJson.get`). That boundary
covers letters, digits and base64url's `-`/`_`, not `.`: a token right after a
`.` is redacted exactly as it is at the start of a text or after a space. A
dotted identifier is kept by the pattern's per-segment lengths instead.
"""

from __future__ import annotations

import pytest

from kiro_crew.security import redact_credentials

#: A neutral token of the two-segment shape: `eyJ` plus a 100-char payload in the
#: base64url alphabet, a `.`, and a 43-char signature. Built from repeated filler,
#: so it decodes to nothing meaningful.
_TOKEN = "eyJ" + "a-b_" * 25 + "." + "c" * 21 + "-" + "d" * 21
_PLACEHOLDER = "[REDACTED: credential]"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (_TOKEN, _PLACEHOLDER),
        ("prefix " + _TOKEN, "prefix " + _PLACEHOLDER),
        ("prefix." + _TOKEN, "prefix." + _PLACEHOLDER),
        ("see ." + _TOKEN, "see ." + _PLACEHOLDER),
    ],
    ids=["start", "after-space", "after-word-dot", "after-lone-dot"],
)
def test_link_token_is_redacted_wherever_it_starts(text, expected):
    result, warnings = redact_credentials(text)

    assert result == expected
    assert len(warnings) == 1


def test_link_token_after_a_sentence_dot_is_redacted():
    """A token that follows the end of a sentence with no space is removed whole."""
    result, _ = redact_credentials(f"Open the link.{_TOKEN} to continue")

    assert result == f"Open the link.{_PLACEHOLDER} to continue"


@pytest.mark.parametrize(
    "text",
    [
        "obj.eyJsonReader.readValueFromInputStream(x)",
        "config.eyJsonSerializerConfigurationFactoryBuilderRegistryProviderDelegating"
        "InterceptorFactoryAdapterHandler.deserializeFromStringValueUsing"
        "ConfiguredObjectMapperInstance(x)",
        "cache.eyJ2IjoxfQ.json",
    ],
    ids=["short-member", "long-member", "dotted-filename"],
)
def test_dotted_identifiers_after_a_dot_are_kept(text):
    """With `.` out of the boundary, the segment lengths still keep code intact."""
    result, warnings = redact_credentials(text)

    assert result == text
    assert warnings == []

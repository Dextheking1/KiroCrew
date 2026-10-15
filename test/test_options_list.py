"""List-form trailer agreement across dashboard and channel readers."""

import json
from pathlib import Path

import pytest

from kiro_crew.dashboard.state import _parse_options, _strip_options_text
from kiro_crew.messaging.renderer import split_options_trailer
from kiro_crew.preview_text import strip_markdown_preview
from kiro_crew.slack.format import extract_options
from kiro_crew.whatsapp.turn_renderer import _strip_options

CASES = json.loads(
    (Path(__file__).parent / "fixtures/options_list.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_list_options_agree_across_surfaces(case):
    text = case["input"]
    body = case.get("body", text)
    options = case["options"]
    assert split_options_trailer(text) == (body, options)
    assert split_options_trailer(text, hide_partial=True) == (body, options)
    assert extract_options(text) == (body, options)
    assert _parse_options(text) == options


def test_dashboard_prefers_trailing_list_over_earlier_marker():
    assert _parse_options("[OPTION: Old]\n[OPTIONS]\n- New") == ["New"]


@pytest.mark.parametrize("case", [c for c in CASES if c["options"]], ids=lambda c: c["name"])
def test_the_dashboard_strip_matches_what_it_parsed(case):
    assert _strip_options_text(case["input"]) == case.get("body", case["input"]).strip()


@pytest.mark.parametrize("case", [c for c in CASES if c["options"]], ids=lambda case: case["name"])
def test_the_session_preview_never_shows_a_list_trailer(case):
    preview = strip_markdown_preview(case["input"])
    assert "[OPTIONS]" not in preview.upper()
    assert preview == strip_markdown_preview(case.get("body", case["input"]))


def test_whatsapp_keeps_a_list_trailer_as_readable_text():
    # WhatsApp renders no buttons, so the list is the only way its reader
    # sees the choices.
    text = "Choose.\n[OPTIONS]\n- Alpha\n- Beta"
    assert "- Alpha" in _strip_options(text)
    assert "- Beta" in _strip_options(text)


def test_subagent_reading_keeps_a_list_trailer_for_the_parent():
    text = "Done.\n[OPTIONS]\n- Merge it now\n- Show me the diff"
    assert extract_options(text, bulleted=False) == (text, [])


def test_a_bare_header_stays_prose_even_on_a_streaming_surface():
    # Streaming renderers also seal the final message through hide_partial=True,
    # so a bare header must never be cut there: a one-frame flash is the cheap
    # failure, deleted text is not.
    text = "Literal marker:\n[OPTIONS]"
    assert split_options_trailer(text, hide_partial=True) == (text, [])


def test_a_long_digit_run_is_not_an_item():
    text = "Pick one.\n[OPTIONS]\n" + "0" * 10_000 + ". Alpha"
    assert split_options_trailer(text) == (text, [])

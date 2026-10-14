"""The ``project-city`` built-in: the project report drawn as a 3D city.

What a regex can pin is pinned here; the page's behaviour on a real field bag is driven
in ``website/src/test/projectCityPage.test.ts``. The page binds project-report's own
seven fields from the same places, keeps its zh-CN words as ASCII escapes, makes no
request, fits the frame's size ceiling, and carries one third-party library, three.js, whose license is
recorded beside the others and whose inlined block is the one
``scripts/build-project-city-three.mjs`` rebuilds.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import dashboard_frame
from kiro_crew.dashboard_templates import catalog
from kiro_crew.dashboard_templates.manifest import load_template

ROOT = Path(__file__).resolve().parents[1]
BUILTIN = ROOT / "src/kiro_crew/dashboard_templates/builtin"
DIRECTORY = BUILTIN / "project-city"
REPORT = BUILTIN / "project-report/manifest.json"
NOTICES = ROOT / "THIRD-PARTY-NOTICES"
BUILD_SCRIPT = ROOT / "scripts/build-project-city-three.mjs"

#: The seven fields project-report binds besides its published panel block.
SEVEN = ("items", "omitted", "series", "last_entry_at", "for_you", "verdict", "ci")

#: The name of the style the page was asked to never mention, spelled as a pattern so
#: this file does not carry the word either.
_STYLE_WORD = re.compile(r"cyber[\s_-]*punk", re.IGNORECASE)
_STYLE_WORD_ZH = "\u8d5b\u535a\u670b\u514b"
_CJK = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")
_REDUCED = re.compile(r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\}\s*\}", re.DOTALL)


def _source() -> str:
    return (DIRECTORY / "template.html").read_text(encoding="utf-8")


def _scripts() -> list[str]:
    return re.findall(r"<script>\n(.*?)</script>", _source(), re.DOTALL)


def _library() -> str:
    return _scripts()[0]


def _page_script() -> str:
    return _scripts()[1]


def _style() -> str:
    return _source().split("<style>")[1].split("</style>")[0]


def _fields() -> dict[str, Any]:
    raw = json.loads((DIRECTORY / "manifest.json").read_text(encoding="utf-8"))
    return dict(raw["fields"])


def _report_fields() -> dict[str, Any]:
    return dict(json.loads(REPORT.read_text(encoding="utf-8"))["fields"])


def _without(spec: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: v for k, v in spec.items() if k not in keys}


def test_the_registry_serves_it_by_its_own_id() -> None:
    entry = catalog.load_one("project-city")
    manifest, _ = load_template(DIRECTORY)
    assert entry.id == manifest.id == "project-city"
    assert manifest.title == "Project city"


def test_it_binds_project_reports_seven_fields_from_the_same_places() -> None:
    fields, report = _fields(), _report_fields()
    assert sorted(fields) == sorted(SEVEN)
    for name in SEVEN:
        assert fields[name]["type"] == report[name]["type"], name
        assert fields[name]["source"] == report[name]["source"], name


def test_its_three_written_fields_take_the_values_project_report_takes() -> None:
    """A crewmate moving between the two pages writes for_you, verdict (its desk
    `words` included) and ci unchanged: only the upkeep text is this page's own."""
    fields, report = _fields(), _report_fields()
    agentic = sorted(n for n, f in fields.items() if f["source"] == {"agentic": True})
    assert agentic == ["ci", "for_you", "verdict"]
    for name in agentic:
        assert _without(fields[name], "upkeep") == _without(report[name], "upkeep"), name
    assert "words" in fields["verdict"]["properties"]


def test_every_written_field_says_how_to_keep_it_current_and_names_no_helper() -> None:
    for name in ("for_you", "verdict", "ci"):
        upkeep = _fields()[name]["upkeep"]
        assert isinstance(upkeep, dict)
        assert set(upkeep) == {"means", "when", "how"}, name
    assert not (DIRECTORY / "helpers").exists()


def _decoded(text: str) -> str:
    """*text* with every ``\\uXXXX`` escape decoded, so its zh-CN table can be read."""
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)


def test_no_file_names_the_style_it_was_drawn_in() -> None:
    """In neither language: the English name, nor the Chinese one in its zh-CN table."""
    for path in (DIRECTORY / "manifest.json", DIRECTORY / "template.html"):
        text = _decoded(path.read_text(encoding="utf-8"))
        assert not _STYLE_WORD.search(text), path.name
        assert _STYLE_WORD_ZH not in text, path.name


def test_its_source_is_ascii_with_its_chinese_escaped() -> None:
    """Both files are ASCII: the page's zh-CN words are ``\\u`` escapes in its I18N table."""
    for path in (DIRECTORY / "manifest.json", DIRECTORY / "template.html"):
        text = path.read_text(encoding="utf-8")
        assert not _CJK.search(text), path.name
        assert text.isascii(), path.name
    script = _page_script()
    assert "var I18N = {" in script and "'zh-CN': {" in script
    assert _CJK.search(_decoded(script))


def test_it_reaches_no_network() -> None:
    lowered = _source().lower()
    for forbidden in (
        "<script src",
        "<link",
        "<img",
        "<iframe",
        "@import",
        "url(http",
        "fetch(",
        "xmlhttprequest",
        "importscripts",
        "websocket",
        "eventsource",
        "sendbeacon",
        "window.open",
        "//cdn.",
    ):
        assert forbidden not in lowered, forbidden


def test_it_fits_the_frames_ceiling_and_passes_its_check() -> None:
    html = _source()
    size = len(html.encode("utf-8"))
    assert size <= dashboard_frame.MAX_PAGE_BYTES
    assert dashboard_frame.validate_page(html) == size


def test_its_one_host_message_only_fills_the_chat_box() -> None:
    """Each Needs-you option posts ``act``, which the host turns into composer text and
    never sends. The page has no other way out, and the library has none at all."""
    script = _page_script()
    assert re.findall(r"type:\s*'([\w:-]+)'", script) == ["kirocrew-dashboard:act"]
    assert script.count("postMessage(") == 1
    assert "clipboard" not in script
    assert "postMessage" not in _library()


def test_the_library_is_three_r134_under_its_mit_banner() -> None:
    library = _library()
    banner = library.splitlines()[0]
    assert "three.js r134" in banner and "SPDX-License-Identifier: MIT" in banner
    assert "globalThis.THREE=" in library.replace(" ", "")
    assert '"134"' in library  # three.js's own REVISION constant


def test_the_library_is_the_block_its_build_script_pins() -> None:
    """The inlined three.js block is byte for byte the one the build script makes.

    ``node scripts/build-project-city-three.mjs`` rebuilds the block from the pinned
    three@0.134.0 tarball and compares it with the page. The path-filtered
    ``project-city-three`` CI job runs the same script on every push that changes the
    page, the build script, or THIRD-PARTY-NOTICES. This case holds the committed block
    to the sha256 the script pins:
    a hand edit to the library fails here, and a rebuild that changes the block has to
    move the pin in the same diff.
    """
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    pin = re.search(r"^const BLOCK_SHA256 = '([0-9a-f]{64})'$", script, re.MULTILINE)
    assert pin, "the build script pins no BLOCK_SHA256"
    assert hashlib.sha256(_library().encode("utf-8")).hexdigest() == pin.group(1)


def test_ci_rebuilds_the_library_whenever_one_of_its_inputs_changes() -> None:
    """The rebuild check needs the npm registry, so ci.yml runs it as its own job behind
    a path filter. The filter names every file the block is made from or recorded in,
    and the job runs the script without ``--write``, so a change to any of them is
    checked against a fresh rebuild."""
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    changes = workflow["jobs"]["changes"]
    step = next(s for s in changes["steps"] if "paths-filter" in str(s.get("uses", "")))
    patterns = yaml.safe_load(step["with"]["filters"])["project_city_three"]
    assert set(patterns) == {
        "src/kiro_crew/dashboard_templates/builtin/project-city/template.html",
        "scripts/build-project-city-three.mjs",
        "THIRD-PARTY-NOTICES",
        "website/package-lock.json",
    }
    assert "project_city_three" in changes["outputs"]
    job = workflow["jobs"]["project-city-three"]
    assert "needs.changes.outputs.project_city_three == 'true'" in job["if"]
    runs = [s.get("run", "") for s in job["steps"]]
    assert "node scripts/build-project-city-three.mjs" in runs
    assert not [r for r in runs if "--write" in r]


def test_no_object_uuid_comes_from_math_random() -> None:
    """three.js's generateUUID falls back, where ``crypto.randomUUID`` is missing, to four
    32-bit numbers; the build script draws them from ``crypto.getRandomValues``, so code
    scanning finds no Math.random() behind any object's ``uuid``."""
    library = _library()
    assert "crypto.getRandomValues(new Uint32Array(4))" in library
    assert "Math.random()*4294967295" not in library.replace(" ", "")
    section = (
        NOTICES.read_text(encoding="utf-8").split("## three.js r134", 1)[1].split("\n## ", 1)[0]
    )
    assert "crypto.getRandomValues" in section


def test_the_library_license_is_recorded_with_the_others() -> None:
    notices = NOTICES.read_text(encoding="utf-8")
    assert "## three.js r134" in notices
    section = notices.split("## three.js r134", 1)[1].split("\n## ", 1)[0]
    assert "MIT License" in section
    assert "Copyright" in section and "three.js authors" in section
    assert "project-city/template.html" in section


def test_its_fills_are_solid() -> None:
    """The page draws its frame, canvas box and cards in solid palette colours. A CSS
    gradient there would only decorate, so none is allowed."""
    assert "gradient(" not in _style()


def test_reduced_motion_stops_every_animation_and_transition() -> None:
    m = _REDUCED.search(_style())
    assert m, "no prefers-reduced-motion block"
    rule = m.group(1)
    for selector in (".pc *", ".pc *::before", ".pc *::after"):
        assert selector in rule, selector
    assert "animation: none !important" in rule
    assert "transition: none !important" in rule
    # The city's own loop reads the same query and stops on it.
    assert "matchMedia('(prefers-reduced-motion: reduce)')" in _page_script()


def test_every_animation_names_a_keyframes_this_page_defines() -> None:
    style = _style()
    used = set(re.findall(r"animation:\s*(pc-[\w-]+)", style))
    defined = set(re.findall(r"@keyframes\s+(pc-[\w-]+)", style))
    assert used and used == defined

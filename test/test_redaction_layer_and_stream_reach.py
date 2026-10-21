"""Redaction reads every decoded layer, holds a catalog match through its filler,
keeps numeric passwords behind a label, and leaves ports, paths and quoted PEM
markers alone."""

from __future__ import annotations

import json
import random
import re
import string

import pytest

from kiro_crew.apps.builtins.design_tweak.backend import preview_files, server
from kiro_crew.log_redaction import SecretRedactionFilter
from kiro_crew.security.redaction import redact_credentials
from kiro_crew.security.text_redaction import StreamRedactor


def _redact(text: str) -> str:
    return redact_credentials(text)[0]


def _stream(text: str, size: int) -> str:
    redactor = StreamRedactor()
    out = "".join(redactor.feed(text[i : i + size]) for i in range(0, len(text), size))
    return out + redactor.flush()


# ── A run of digits behind a secret label is the secret ──


@pytest.mark.parametrize("quoted", [True, False])
def test_a_numeric_password_behind_its_label_is_redacted(quoted: bool) -> None:
    password = "834729105623"  # 12 digits: the shape of an account ID
    text = json.dumps({"password": password}) if quoted else f"password={password}"

    assert password not in _redact(text)
    assert password not in SecretRedactionFilter([]).redact(text)


def test_an_account_id_behind_its_own_label_stays() -> None:
    text = '{"account_id": "123456789012"}'

    assert _redact(text) == text


# ── Every decoded layer is read ──


def test_a_double_encoded_password_with_a_quote_is_redacted_whole() -> None:
    password = 'Xq7"m2pzR9tvB8nc1kd6wf'
    text = json.dumps(json.dumps({"password": password}))

    out = _redact(text)

    assert "Xq7" not in out
    assert "m2pzR9tvB8nc1kd6wf" not in out
    # One value, one tag, and the stream agrees with the batch.
    assert out.count("[REDACTED") == 1
    assert _stream(text, 1) == out
    assert _stream(text, 16) == out


def test_a_triple_encoded_password_with_a_quote_is_redacted_whole() -> None:
    password = 'Xq7"m2pzR9tvB8nc1kd6wf'
    out = _redact(json.dumps(json.dumps(json.dumps({"password": password}))))

    assert "Xq7" not in out
    assert "m2pzR9tvB8nc1kd6wf" not in out


# ── The stream holds a catalog match through the rule's filler ──

#: A made-up UUID, kept apart from its keyword so no literal reads as a key.
_UUID = "12345678-1234-1234-1234-" "123456789abc"

_CATALOG_LINES = [
    f"Your heroku key = {_UUID} ok\n",
    "adafruit key = xq7lm2pz9rt4vb8nc1kd6wf3hj5gs0ty ok\n",
    # The rules' filler is up to 20 characters: " production api key ".
    f"heroku production api key = {_UUID} ok\n",
]


@pytest.mark.parametrize("text", _CATALOG_LINES)
@pytest.mark.parametrize("size", [1, 16])
def test_the_stream_redacts_a_catalog_secret_behind_filler_like_the_batch(
    text: str, size: int
) -> None:
    batch = _redact(text)
    assert "[REDACTED" in batch

    assert _stream(text, size) == batch


# ── A port and a path are not a password ──


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:5173/@vite/client",
        "https://registry.npmjs.org:443/@types/node",
    ],
)
def test_a_url_with_a_port_and_an_at_in_its_path_is_left_intact(url: str) -> None:
    assert _redact(url) == url


@pytest.mark.parametrize(
    ("url", "password", "host"),
    [
        ("postgres://app:ab/cdEF12xy@db.internal:5432/x", "ab/cdEF12xy", "@db.internal:5432/x"),
        # Digit-led passwords: the host after ``@`` is dotted or has a port.
        (
            "postgres://app:2024/SecretPw99xy@db.internal:5432/x",
            "2024/SecretPw99xy",
            "@db.internal",
        ),
        ("redis://default:007/xK9mQ2pL7vR4@cache.example.com:6379", "007/xK9mQ2pL7vR4", "@cache."),
        ("mysql://root:443/pa55wordQQ@db:3306/x", "443/pa55wordQQ", "@db:3306/x"),
    ],
)
def test_a_uri_password_holding_a_slash_is_still_redacted(
    url: str, password: str, host: str
) -> None:
    out = _redact(url)

    assert password not in out
    assert host in out
    assert _stream(url, 1) == out


# ── The preview's PEM marker exemption holds in a decoded view too ──

#: PEM armour lines, split so no literal marker sits in this file.
_BEGIN = "-----BEGIN RSA " "PRIVATE KEY-----"
_END = "-----END RSA " "PRIVATE KEY-----"


def test_an_escaped_pem_marker_without_a_body_is_served() -> None:
    data = f'const k = "{_BEGIN}\\n...\\n{_END}\\n";'.encode()

    assert preview_files.contains_credential(server, data) is False


def test_a_double_escaped_pem_marker_without_a_body_is_served() -> None:
    marker = f"{_BEGIN}\n...\n{_END}\n"
    data = json.dumps(json.dumps(marker)).encode()

    assert preview_files.contains_credential(server, data) is False


def test_an_escaped_pem_block_with_a_body_is_refused() -> None:
    body = "MIIEowIBAAKCAQEA" + "q7Lm2Pz9Rt4Vb8Nc1Kd6Wf3Hj5Gs0Ty" * 3
    data = (f'const k = "{_BEGIN}\\n{body}\\n' f'{_END}\\n";').encode()

    assert preview_files.contains_credential(server, data) is True


# ── The vendor catalog scans in linear time and holds long values whole ──


def _alnum(length: int, seed: int) -> str:
    rng = random.Random(seed)
    return "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(length))


@pytest.mark.parametrize(
    "token",
    [
        # A service-account token and a base64-encoded JWT run long: the
        # catalog reads the secret's own repeat as upstream wrote it, unbounded.
        "ops_" "eyJ" + _alnum(900, 1),
        "ZXlKaGJHY2lPaU" + _alnum(400, 2),
    ],
)
def test_a_long_catalog_token_is_redacted_whole(token: str) -> None:
    out = _redact(f"x {token} y")

    assert token[-40:] not in out
    assert token[:12] not in out


@pytest.mark.parametrize("length", [260, 700])
@pytest.mark.parametrize("size", [1, 64])
@pytest.mark.parametrize(
    "head",
    # A token after its keyword and one blank, and tokens that are the whole
    # match (the keyword is the token's own prefix).
    ["Authorization: FlyV1 fm2_", "x ATATT3", "x EAAC", "x ZXlKaGJHY2lPaU"],
)
def test_the_stream_holds_a_long_whole_token_like_the_batch(
    head: str, length: int, size: int
) -> None:
    text = f"{head}{_alnum(length, length + size)} end"
    batch = _redact(text)
    assert "[REDACTED" in batch

    assert _stream(text, size) == batch


def _catalog_patterns() -> dict[str, str]:
    from kiro_crew.security import secret_catalog

    return {rule.rule_id: rule.regex.pattern for rule in secret_catalog._load().rules}


def test_an_overlapping_catalog_prefix_repeat_is_bounded() -> None:
    # ``incoming+`` then ``0-`` repeated backtracked quadratically while the
    # prefix repeats were unbounded; the generator bounds every repeat outside
    # the secret's own group, and a bounded repeat costs constant work per start.
    pattern = _catalog_patterns()["catalog:gitlab-incoming-mail-address-token"]

    assert r"(?:[A-Za-z0-9._-]{1,256}-)?\d{1,256}-" in pattern
    assert "([A-Za-z0-9_-]+)" in pattern  # the secret is read whole


@pytest.mark.parametrize("length", [120, 210, 300, 450])
@pytest.mark.parametrize("size", [2, 8, 4096])
@pytest.mark.parametrize("after", [" done", "\nend", "\nAuthorization: Bearer x"])
def test_the_stream_holds_a_long_catalog_value_whole(length: int, size: int, after: str) -> None:
    value = _alnum(length, length)
    text = f"here snowflake token: {value}{after}"
    batch = _redact(text)
    assert value not in batch

    assert _stream(text, size) == batch


# ── The bundle gate refuses a bare secret key in both modes ──


@pytest.mark.parametrize("canonical", [True, False])
def test_the_bundle_gate_refuses_a_bare_secret_key(
    canonical: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.apps.builtins.aws_control.crew.packaging.pipeline import scan

    if not canonical:
        monkeypatch.setattr(scan, "_CANONICAL_REDACTOR", None)
    key = "k3Jz9Qp2Lm8Xv7Rt4Ws1" "Yb6Nc0Hd5Fg2Ae9Ui3Op"

    kinds = [leak.kind for leak in scan.scan_text(f"note {key} end", "bundle")]

    assert "bare-secret" in kinds


# ── Offsets survive a lower-case that changes a string's length ──


@pytest.mark.parametrize("size", [1, 4, 64])
def test_a_dotted_capital_i_before_a_catalog_keyword_streams_intact(size: int) -> None:
    text = "İZMİR'DEKİ SUNUCU İÇİN key"

    assert _stream(text, size) == text


# ── An STS session token is a secret wherever it appears ──


def _b64(length: int, seed: int) -> str:
    rng = random.Random(seed)
    return "".join(rng.choice(string.ascii_letters + string.digits + "+/") for _ in range(length))


@pytest.mark.parametrize("prefix", ["IQoJb3Jp" "Z2lu", "FwoGZXIv" "YXdz"])
def test_an_sts_session_token_is_redacted_beside_its_key_pair(prefix: str) -> None:
    token = prefix + _b64(300, 1)
    secret = _b64(40, 2)
    text = (
        f"ReadOnlyCredentials(access_key='AKIA{'Z' * 16}', "
        f"secret_key='{secret}', token='{token}')"
    )

    out = _redact(text)

    assert token[20:80] not in out
    assert secret not in out
    assert _stream(text, 16) == out


# ── A name/value pair is held from its name until the value closes ──


@pytest.mark.parametrize("length", [200, 450])
@pytest.mark.parametrize("size", [1, 16])
def test_a_long_value_beside_its_secret_name_streams_redacted(length: int, size: int) -> None:
    value = _alnum(length, length)
    text = json.dumps({"name": "AWS_SESSION_TOKEN", "value": value}) + " ok\n"
    batch = _redact(text)
    assert value[100:140] not in batch

    assert _stream(text, size) == batch


# ── A catalog cut keeps the keyword that activates the rule ──


@pytest.mark.parametrize("line", ["airtable: {pat}\nnext\n", "my airtable token is {pat} ok\n"])
@pytest.mark.parametrize("size", [1, 16])
def test_a_keyword_gated_token_streams_with_its_keyword(line: str, size: int) -> None:
    rng = random.Random(4)
    body = "".join(rng.choice("abcdefghijKLMNOP0123456789") for _ in range(14))
    pat = "pat" + body + "." + "".join(rng.choice("0123456789abcdef") for _ in range(64))
    text = line.format(pat=pat)
    batch = _redact(text)
    assert pat not in batch

    assert _stream(text, size) == batch


# ── The log filter reads a signed link nested percent-encoded in another ──


@pytest.mark.parametrize("name", ["X-Amz-Security-Token", "X-Amz-Signature", "S%69gnature"])
def test_the_log_filter_drops_a_signing_value_behind_an_encoded_separator(name: str) -> None:
    token = _alnum(60, 9)
    text = f"error: https://b/o%3F{name}%3D{token}%26next%3D1"

    assert token not in SecretRedactionFilter([]).redact(text)


# ── A key ID's secret is redacted whole behind a label glued to it ──

_KEY = "Xq8m1n/Tz4Kp0w9RbV2y" "LcH7sDfG5jU3eA6oQiN+"  # ends with a separator
_KID = "AKIA" "2E0A8F3B244C9986"  # a made-up key ID, split so no literal sits here


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        (f"{_KID} x={_KEY} end", "x="),
        (f"{_KID} cred={_KEY} end", "cred="),
        (f"{_KID},{_KEY}/path/x", "/path/x"),
        (f"https://e.com/{_KID}/{_KEY}/f.txt", "/f.txt"),
    ],
)
def test_a_paired_key_ending_in_a_separator_is_redacted_whole(text: str, kept: str) -> None:
    out = _redact(text)

    assert _KEY[-8:] not in out and _KEY[:8] not in out
    assert kept in out


# ── Labels in table cells, opposite quotes, signed links of other clouds ──


def test_a_secret_in_the_next_markdown_cell_is_redacted() -> None:
    secret = "wJalrXUtnFEMI/K7MDENG/" "bPxRfiCYEXAMPLEKEY"

    assert secret not in _redact(f"| AWS_SECRET_ACCESS_KEY | {secret} |")
    assert _redact("| SecretAccessKey | Description |") == "| SecretAccessKey | Description |"


def test_a_quoted_value_starting_with_the_other_quote_is_redacted() -> None:
    text = json.dumps({"password": "'Xq7m2pzR9tvB8nc"})

    assert "Xq7m2pzR9tvB8nc" not in _redact(text)
    assert "Xq7m2pzR9tvB8nc" not in SecretRedactionFilter([]).redact(text)


@pytest.mark.parametrize(
    "url",
    [
        "https://a.blob.core.windows.net/c/f?sv=2021&sig={v}",
        "https://storage.googleapis.com/b/o?X-Goog-Algorithm=x&X-Goog-Signature={v}",
    ],
)
def test_another_clouds_link_signature_is_redacted(url: str) -> None:
    value = _alnum(43, 7)
    text = url.format(v=value)

    assert value not in _redact(text)
    assert value not in SecretRedactionFilter([]).redact(text)
    assert _redact("def f(sig=None): pass") == "def f(sig=None): pass"


# ── A short labelled secret is redacted when base64-encoded ──


@pytest.mark.parametrize("password", ["abcdefgh", "Xq7m2pzR"])
def test_a_short_encoded_password_is_redacted(password: str) -> None:
    import base64

    blob = base64.b64encode(
        json.dumps({"password": password}, separators=(",", ":")).encode()
    ).decode()

    assert blob not in _redact(f"x {blob} y")


# ── A vendor rule with two side-by-side repeats stays fast ──


def test_no_catalog_rule_repeats_one_class_twice_side_by_side() -> None:
    # ``X{20,1000}X{0,24}`` is a split point at every length to a backtracking
    # engine (``caiskey=`` repeated stalled the gateway); the generator merges it.
    adjacent = re.compile(r"(\[(?:\\.|[^\]\\])+\])\{\d+,\d+\}\1\{\d+,\d+\}")

    assert [rid for rid, p in _catalog_patterns().items() if adjacent.search(p)] == []
    alibaba = _catalog_patterns()["catalog:alibaba-sts-security-token"]
    assert "(CAIS[A-Za-z0-9+/_=-]{20,1024})" in alibaba


# ── An inline bitmap with a key pair glued into its body is not exempted ──


def test_a_bitmap_body_carrying_a_key_pair_is_screened() -> None:
    import base64
    import struct

    from kiro_crew.apps.builtins.pptx_maker.backend import routes

    head = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
    raw = head + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0) + bytes(range(256)) * 4
    body = base64.b64encode(raw).decode().rstrip("=")
    glued = body + "AKIA" "ZX9QW3ER7TY1UI5O/" + "Xq8m1nTz4Kp0w9RbV2yL" "cH7sDfG5jU3eA6oQiNab"
    glued += "A" * (-len(glued) % 4)  # a whole base64 body, so it decodes

    assert routes._scanned_bitmap_bytes(glued)[1] is True
    assert routes._scanned_bitmap_bytes(base64.b64encode(raw).decode())[1] is False


# ── The stream holds a label the batch reads only through codes or escapes ──

_AWS_DOC_KEY = "wJalrXUtnFEMI/K7MDENG/" "bPxRfiCYEXAMPLEKEY"


@pytest.mark.parametrize(
    "text",
    [
        f"\x1b[33maws_secret_access_key\x1b[0m = {_AWS_DOC_KEY} and more\n",
        f"Some prose here. | AWS_SECRET_ACCESS_KEY | {_AWS_DOC_KEY} |\n",
    ],
)
@pytest.mark.parametrize("size", [1, 7])
def test_a_coloured_or_tabled_aws_label_streams_like_the_batch(text: str, size: int) -> None:
    batch = _redact(text)
    assert _AWS_DOC_KEY not in batch

    assert _stream(text, size) == batch


@pytest.mark.parametrize("length", [700, 1200])
def test_an_escaped_session_token_longer_than_the_floor_streams_redacted(length: int) -> None:
    value = _alnum(length, length)
    text = json.dumps(json.dumps({"SessionToken": value})) + " ok\n"
    batch = _redact(text)
    assert value[-30:] not in batch

    assert _stream(text, 16) == batch


# ── A vendor's hex key is its key, not a digest ──


def test_a_hex_key_beside_its_vendor_keyword_is_redacted() -> None:
    hexkey = "9f8e7d6c5b4a3928" "1706f5e4d3c2b1a098765432"

    assert hexkey not in _redact(f"datadog_app_key = {hexkey}")
    assert _redact(f"commit key = {hexkey}") == f"commit key = {hexkey}"


# ── An encoded ``&`` ends a value, so the next signing value is read ──


def test_a_signing_value_after_an_ordinary_encoded_parameter_is_dropped() -> None:
    token = _alnum(60, 11)
    text = f"error: https://b/o%3Ffoo%3Dbar%26X-Amz-Security-Token%3D{token}%26n%3D1"

    assert token not in SecretRedactionFilter([]).redact(text)


# ── The bundle gate leaves an integrity digest alone ──


def test_the_bundle_gate_keeps_a_subresource_integrity_digest() -> None:
    import base64
    import hashlib

    from kiro_crew.apps.builtins.aws_control.crew.packaging.pipeline import scan

    digest = base64.b64encode(hashlib.sha256(b"hello world").digest()).decode()
    text = f'<script integrity="sha256-{digest}"></script>'

    assert [leak.kind for leak in scan.scan_text(text, "skill")] == []


# ── Short PINs go; code names and escaped labels in templates stay ──


@pytest.mark.parametrize("text", ["PASSWORD=482913", "pin_password: 7391"])
def test_a_pin_behind_a_password_label_is_redacted(text: str) -> None:
    digits = text.rsplit("=", 1)[-1].rsplit(" ", 1)[-1]

    assert digits not in _redact(text)
    assert digits not in SecretRedactionFilter([]).redact(text)


@pytest.mark.parametrize(
    "text",
    [
        "password = new_password",
        "self.password = password_hash",
        "var api_key = userApiKey || ''",
        "lane_pass: '\\u7eff\\u706f'",
        "password: no",
    ],
)
def test_code_that_names_a_secret_is_left_alone(text: str) -> None:
    assert _redact(text) == text
    assert SecretRedactionFilter([]).redact(text) == text


def test_the_shipped_pr_watch_template_renders_its_labels() -> None:
    from pathlib import Path

    from kiro_crew.security.redaction import redact_credentials

    root = Path(__file__).resolve().parents[1]
    template = root / "src/kiro_crew/dashboard_templates/builtin/pr-watch/template.html"
    text = template.read_text(encoding="utf-8")

    assert "[REDACTED" not in redact_credentials(text)[0]


# ── A long quoted value is removed whole, commas and all ──


@pytest.mark.parametrize("length", [600, 1200])
def test_a_long_quoted_password_with_commas_is_redacted_whole(length: int) -> None:
    value = ",".join(_alnum(9, i) for i in range(length // 10))
    text = json.dumps({"password": value}) + " ok\n"
    batch = _redact(text)

    assert value[-40:] not in batch
    assert value[-40:] not in SecretRedactionFilter([]).redact(text)
    assert _stream(text, 16) == batch


# ── A factory-default Basic pair blocks a publish ──


def test_a_default_basic_pair_is_a_credential_finding() -> None:
    from kiro_crew.deploy.scan import is_credential_finding, scan_content

    assert [
        f for f in scan_content("Authorization: Basic YWRtaW46YWRtaW4=") if is_credential_finding(f)
    ]


@pytest.mark.parametrize("length", [1400, 3000])
def test_a_long_vendor_token_streams_whole_in_large_deltas(length: int) -> None:
    text = f"Authorization: FlyV1 fm2_{_alnum(length, length)} end\n"
    batch = _redact(text)
    assert "[REDACTED" in batch

    assert _stream(text, 100) == batch

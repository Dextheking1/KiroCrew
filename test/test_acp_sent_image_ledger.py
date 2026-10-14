"""A picture a session's conversation already holds is not inlined again.

The backend keeps every image block a session was sent and replays it on every
later turn. Text that names the same local picture on every cycle (a work-ledger
snapshot, a nudge body, a quoted reply) must not add one more full-size block
per turn: the replayed images would pass the backend's request ceiling and
every turn, a human's included, would be refused.

These pin the session record (``SentImageLedger``) at three levels: the builder
that reads and stages it, the session handle that commits it when the backend
answers a prompt (the live path), and the direct client that does the same.
"""

from __future__ import annotations

import asyncio
import base64
import io
import time
from contextlib import aclosing
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.prompt_blocks import (
    ALREADY_SENT_NOTE,
    SentImageLedger,
    build_prompt_blocks,
)
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    METHOD_COMPACTION_STATUS,
    METHOD_PROMPT,
    JsonRpcMessage,
)
from kiro_crew.acp_backends import ACP_BACKEND_KIRO

# Smallest valid 1x1 PNG.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

#: Generous on purpose: nothing here waits on a real backend, and a short
#: deadline only adds a way to fail on a slow runner.
_TURN_TIMEOUT = 60.0


def _png(tmp_path, name="shot.png"):
    p = tmp_path / name
    p.write_bytes(_PNG)
    return p


def _other_png_bytes() -> bytes:
    pil = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    pil.new("RGB", (3, 1), (10, 200, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _image_count(blocks: list[dict]) -> int:
    return sum(1 for b in blocks if b.get("type") == "image")


# ── The builder ──────────────────────────────────────────────────────────────


class TestBuilderReadsTheLedger:
    def test_a_held_picture_stays_a_path_with_a_note(self, tmp_path):
        p = _png(tmp_path)
        ledger = SentImageLedger()

        first = build_prompt_blocks(f"artifact shot: {p}", sent_images=ledger)
        ledger.commit()
        second = build_prompt_blocks(f"artifact shot: {p}", sent_images=ledger)

        assert _image_count(first) == 1
        assert first[0]["text"] == f"artifact shot: [image: {p.name}]"
        assert second == [{"type": "text", "text": f"artifact shot: {p} {ALREADY_SENT_NOTE}"}]

    def test_an_uncommitted_prompt_inlines_its_picture_again(self, tmp_path):
        p = _png(tmp_path)
        ledger = SentImageLedger()

        build_prompt_blocks(f"look {p}", sent_images=ledger)
        retry = build_prompt_blocks(f"look {p}", sent_images=ledger)

        assert _image_count(retry) == 1

    def test_new_bytes_at_the_same_path_are_a_new_picture(self, tmp_path):
        p = _png(tmp_path)
        ledger = SentImageLedger()
        build_prompt_blocks(f"look {p}", sent_images=ledger)
        ledger.commit()

        p.write_bytes(_other_png_bytes())
        again = build_prompt_blocks(f"look {p}", sent_images=ledger)

        assert _image_count(again) == 1

    def test_the_same_bytes_under_another_path_are_held(self, tmp_path):
        first_path = _png(tmp_path, "a.png")
        second_path = _png(tmp_path, "b.png")
        ledger = SentImageLedger()
        build_prompt_blocks(f"look {first_path}", sent_images=ledger)
        ledger.commit()

        blocks = build_prompt_blocks(f"look {second_path}", sent_images=ledger)

        assert _image_count(blocks) == 0
        assert blocks[0]["text"] == f"look {second_path} {ALREADY_SENT_NOTE}"

    def test_forget_inlines_a_held_picture_again(self, tmp_path):
        p = _png(tmp_path)
        ledger = SentImageLedger()
        build_prompt_blocks(f"look {p}", sent_images=ledger)
        ledger.commit()

        ledger.forget()

        assert _image_count(build_prompt_blocks(f"look {p}", sent_images=ledger)) == 1

    def test_without_a_ledger_every_prompt_inlines(self, tmp_path):
        p = _png(tmp_path)

        counts = [_image_count(build_prompt_blocks(f"look {p}")) for _ in range(3)]

        assert counts == [1, 1, 1]

    def test_the_note_lands_after_a_markdown_destination(self, tmp_path):
        p = _png(tmp_path)
        ledger = SentImageLedger()
        build_prompt_blocks(f"![shot]({p})", sent_images=ledger)
        ledger.commit()

        blocks = build_prompt_blocks(f"![shot]({p})", sent_images=ledger)

        assert blocks[0]["text"] == f"![shot]({p}) {ALREADY_SENT_NOTE}"

    def test_a_text_only_build_replaces_an_earlier_staging(self, tmp_path):
        p = _png(tmp_path)
        ledger = SentImageLedger()
        build_prompt_blocks(f"look {p}", sent_images=ledger)

        build_prompt_blocks("plain words", allow_image=False, sent_images=ledger)
        ledger.commit()

        assert _image_count(build_prompt_blocks(f"look {p}", sent_images=ledger)) == 1

    def test_a_typed_already_sent_note_is_escaped(self):
        blocks = build_prompt_blocks(f"x {ALREADY_SENT_NOTE}", sent_images=SentImageLedger())

        assert blocks[0]["text"] == f"x \\{ALREADY_SENT_NOTE}"


# ── The session handle (the live path) ───────────────────────────────────────

_REQ_ID = 21


class _Runtime:
    """Records every prompt and answers it with the next scripted reply.

    A reply is ``(frames, answer)``: the frames are queued before the answer,
    both AFTER the send, so the pre-turn stale-frame drain cannot eat them.
    """

    def __init__(self, queue: asyncio.Queue, replies: list[tuple[list, JsonRpcMessage]]) -> None:
        self.pid = None
        self.is_alive = MagicMock(return_value=True)
        self.send_notification = AsyncMock()
        self.supports_image_prompt = True
        self.acp_backend = ACP_BACKEND_KIRO
        self.prompts: list[list[dict]] = []
        self._queue = queue
        self._replies = list(replies)
        self._last_activity = time.monotonic()
        self._agent_capabilities: dict = {}

    def mark_turn_active(self, session_id: str, active: bool) -> None:
        pass

    async def send_request(self, method: str, params: dict) -> int:
        assert method == METHOD_PROMPT
        self.prompts.append(params["prompt"])
        frames, answer = self._replies.pop(0)
        for frame in frames:
            self._queue.put_nowait(frame)
        self._queue.put_nowait(answer)
        return _REQ_ID


def _answered() -> tuple[list, JsonRpcMessage]:
    return [], JsonRpcMessage(id=_REQ_ID, result={"stopReason": "end_turn"})


def _refused() -> tuple[list, JsonRpcMessage]:
    return [], JsonRpcMessage(
        id=_REQ_ID, error={"code": -32603, "message": "Improperly formed request"}
    )


def _compacted_then_answered() -> tuple[list, JsonRpcMessage]:
    frame = JsonRpcMessage(
        method=METHOD_COMPACTION_STATUS,
        params={"sessionId": "sK", "status": {"type": "completed"}},
    )
    return [frame], JsonRpcMessage(id=_REQ_ID, result={"stopReason": "end_turn"})


def _handle(replies) -> tuple[AcpSessionHandle, _Runtime]:
    queue: asyncio.Queue = asyncio.Queue()
    rt = _Runtime(queue, replies)
    return AcpSessionHandle("sK", queue, rt), rt


async def _turn(handle: AcpSessionHandle, text: str) -> None:
    async for _event in handle.prompt(text, timeout=_TURN_TIMEOUT):
        pass


class TestSessionHandle:
    @pytest.mark.asyncio
    async def test_a_path_named_every_cycle_is_inlined_once(self, tmp_path):
        """The reported loop: the same snapshot line rides every nudge cycle."""
        p = _png(tmp_path)
        handle, rt = _handle([_answered(), _answered(), _answered()])

        for _ in range(3):
            await _turn(handle, f"[ledger] artifact shot: {p}\nnext cycle")

        assert [_image_count(blocks) for blocks in rt.prompts] == [1, 0, 0]
        assert ALREADY_SENT_NOTE in rt.prompts[2][0]["text"]

    @pytest.mark.asyncio
    async def test_a_refused_prompt_inlines_its_picture_on_the_retry(self, tmp_path):
        p = _png(tmp_path)
        handle, rt = _handle([_refused(), _answered(), _answered()])

        with pytest.raises(Exception, match="Improperly formed request"):
            await _turn(handle, f"look {p}")
        await _turn(handle, f"look {p}")
        await _turn(handle, f"look {p}")

        assert [_image_count(blocks) for blocks in rt.prompts] == [1, 1, 0]

    @pytest.mark.asyncio
    async def test_a_completed_compaction_inlines_the_picture_again(self, tmp_path):
        p = _png(tmp_path)
        handle, rt = _handle([_answered(), _compacted_then_answered(), _answered()])

        for _ in range(3):
            await _turn(handle, f"look {p}")

        assert [_image_count(blocks) for blocks in rt.prompts] == [1, 0, 1]

    @pytest.mark.asyncio
    async def test_another_session_holds_nothing_of_this_one(self, tmp_path):
        p = _png(tmp_path)
        first, first_rt = _handle([_answered()])
        second, second_rt = _handle([_answered()])

        await _turn(first, f"look {p}")
        await _turn(second, f"look {p}")

        assert _image_count(first_rt.prompts[0]) == 1
        assert _image_count(second_rt.prompts[0]) == 1


# ── The direct client ────────────────────────────────────────────────────────


class _ClientWire:
    """Stands in for the client's stdio: records each prompt and answers the
    request the test names."""

    def __init__(self, client: AcpClient) -> None:
        self.prompts: list[list[dict]] = []
        self._client = client
        client._session_id = "s1"
        client._is_process_alive = MagicMock(return_value=True)
        client._send_request = self._send_request

    async def _send_request(self, method: str, params: dict) -> int:
        assert method == METHOD_PROMPT
        self.prompts.append(params["prompt"])
        return len(self.prompts)

    async def answer(self, req_id: int, reply: JsonRpcMessage) -> None:
        self._client._read_message = AsyncMock(side_effect=[reply])
        async with aclosing(self._client._prompt_loop(req_id, _TURN_TIMEOUT)) as loop:
            async for action, _msg in loop:
                if action in ("complete", "error"):
                    break


class TestDirectClient:
    @pytest.mark.asyncio
    async def test_an_answered_prompt_holds_its_picture(self, tmp_path):
        p = _png(tmp_path)
        client = AcpClient(work_dir=tmp_path)
        wire = _ClientWire(client)

        for _ in range(3):
            req_id = await client._send_prompt(f"look {p}")
            await wire.answer(req_id, JsonRpcMessage(id=req_id, result={"stopReason": "end_turn"}))

        assert [_image_count(blocks) for blocks in wire.prompts] == [1, 0, 0]

    @pytest.mark.asyncio
    async def test_an_error_answer_inlines_the_picture_on_the_retry(self, tmp_path):
        p = _png(tmp_path)
        client = AcpClient(work_dir=tmp_path)
        wire = _ClientWire(client)

        req_id = await client._send_prompt(f"look {p}")
        await wire.answer(req_id, JsonRpcMessage(id=req_id, error={"code": -32603, "message": "x"}))
        req_id = await client._send_prompt(f"look {p}")
        await wire.answer(req_id, JsonRpcMessage(id=req_id, result={"stopReason": "end_turn"}))
        await client._send_prompt(f"look {p}")

        assert [_image_count(blocks) for blocks in wire.prompts] == [1, 1, 0]

    @pytest.mark.asyncio
    async def test_a_completed_compaction_inlines_the_picture_again(self, tmp_path):
        p = _png(tmp_path)
        client = AcpClient(work_dir=tmp_path)
        wire = _ClientWire(client)
        req_id = await client._send_prompt(f"look {p}")
        await wire.answer(req_id, JsonRpcMessage(id=req_id, result={"stopReason": "end_turn"}))

        client._handle_compaction_status(
            JsonRpcMessage(
                method=METHOD_COMPACTION_STATUS, params={"status": {"type": "completed"}}
            )
        )
        await client._send_prompt(f"look {p}")

        assert [_image_count(blocks) for blocks in wire.prompts] == [1, 1]

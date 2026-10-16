"""Slack agent-session status and title: ``agents.sessions.setStatus`` / ``rename``.

With ``slack.agent_sessions`` on, a Slack thread the bot answers in is an agent
session: Slack shows "Working..." and a native Stop button while it is
``processing``, a paused state while it is ``suspended`` (a tool approval or a
review draft waiting on a person), and the ready state when it is ``active``.

The existing call sites speak the older ``assistant.threads.setStatus`` dialect:
free status text, where an empty string clears the indicator. This driver keeps
them unchanged and translates (:func:`lifecycle_for_text`), so the transport
renderer, the native handler and the review path all drive the session without
learning a second vocabulary. Slack's loading UX takes no custom text, so the
text only chooses the lifecycle state.

What it adds on top of a plain translation:

* **No stray sessions.** ``setStatus`` creates a session when there is none, so
  ``active`` is only ever sent for a thread this process put into ``processing``
  or ``suspended``; an error path clearing a status it never set creates nothing.
* **Fewer calls.** A tool event re-announces "working" many times a turn; a repeat
  ``processing`` inside the refresh window is not re-sent (``setStatus`` is Tier 3).
* **Long turns stay "Working...".** Slack times ``processing`` out to ``active``
  after an hour, so a keepalive re-sends it every :data:`PROCESSING_REFRESH_SECS`
  while the turn is still running.
* **Degrades to today's behaviour.** A workspace without the agent feature, or an
  install whose grant predates it, answers ``feature_disabled`` / ``missing_scope``
  (:data:`DISABLING_ERRORS`). The first such answer turns the driver off for the
  rest of the process, pushes ONE dashboard notification, and every call from
  then on -- that one included -- takes the ``assistant.threads.*`` path again.
  Nothing is ever shown in Slack.

The driver is attached to the :class:`RealSlackClient` at boot
(:func:`configure`) and only when the feature is on, so with it off the client
is byte-for-byte the old one. Its state lives on the instance, never in module
globals, so a second boot in one process starts clean.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kiro_crew.slack.agent_sessions import fence

if TYPE_CHECKING:
    from kiro_crew.slack.client import RealSlackClient

logger = logging.getLogger(__name__)

SESSION_STATUS_ACTIVE = "active"
SESSION_STATUS_PROCESSING = "processing"
SESSION_STATUS_SUSPENDED = "suspended"
#: The lifecycle states this runtime sends. ``closed`` is Slack's fourth state and is
#: deliberately absent: nothing here ends a session, so nothing sends it.
SESSION_STATUSES = frozenset(
    {SESSION_STATUS_ACTIVE, SESSION_STATUS_PROCESSING, SESSION_STATUS_SUSPENDED}
)

#: Status texts the existing call sites set while the turn waits on a PERSON: the
#: native handler's tool-approval prompt and the review path's draft. Pinned against
#: those call sites by ``test_slack_agent_session_status.py``.
SUSPENDED_STATUS_TEXTS = frozenset({"Waiting for approval…", "Awaiting review…"})

#: Slack moves a ``processing`` session to ``active`` after an hour; re-sent well
#: inside that so a long build never loses its "Working..." and its Stop button.
PROCESSING_REFRESH_SECS = 50 * 60

#: Answers that mean this workspace or install cannot use agent sessions at all, so
#: retrying per call would only spam a failing endpoint. ``not_authorized`` (the bot
#: is not in that channel) and argument errors are per-call and are NOT here.
DISABLING_ERRORS = frozenset(
    {
        "feature_disabled",
        "missing_scope",
        "not_allowed",
        "not_allowed_token_type",
        "unknown_method",
        "method_deprecated",
    }
)

#: ``setStatus`` warns with this while the app is not subscribed to
#: ``agent_session_stopped``: Slack then shows a loader with no Stop button.
MISSING_STOP_SUBSCRIPTION_WARNING = "missing_agent_session_stopped_event_subscription"

#: Bound on the per-thread records, so a long-lived gateway cannot grow them forever.
_MAX_TRACKED_SESSIONS = 2048

#: Slack's limit on a session title.
_TITLE_MAX = 200

_DISABLED_TITLE = "Slack agent sessions are off for this run"
_DISABLED_BODY = (
    "Slack answered `{code}` to an agent-session call, so Kiro Crew is using the "
    "older thread status for the rest of this run and Slack shows no native Stop "
    "button. Turn on the agent feature for the Slack app (update the app from the "
    "manifest in Settings → Slack, then reinstall it to the workspace), and restart "
    "the gateway."
)
_NO_STOP_TITLE = "Slack shows no Stop button"
_NO_STOP_BODY = (
    "The Slack app is not subscribed to the `agent_session_stopped` event, so threads "
    "show “Working…” without a Stop button. Update the app from the manifest in "
    "Settings → Slack and reinstall it to the workspace."
)


def lifecycle_for_text(text: str) -> str:
    """The agent-session state an ``assistant.threads``-style status text means."""
    if not text:
        return SESSION_STATUS_ACTIVE
    if text in SUSPENDED_STATUS_TEXTS:
        return SESSION_STATUS_SUSPENDED
    return SESSION_STATUS_PROCESSING


def thread_ts_of(key_or_ts: str) -> str | None:
    """The bare Slack thread ts in a session key or ts, or None when it holds none.

    The title call sites pass a session key (``slack:<ts>``); a single-session DM's
    key names a channel (``slack:D…``) and a dashboard-owned thread's key is not a
    Slack key at all, and neither is a thread Slack could address.
    """
    ts = key_or_ts.removeprefix("slack:")
    head, dot, tail = ts.partition(".")
    if dot and head.isdigit() and tail.isdigit():
        return ts
    return None


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    try:
        return str((response or {}).get("error", "") or "")
    except Exception:
        return ""


@dataclass
class _Session:
    """What this process last told Slack about one session."""

    status: str = ""
    sent_at: float = 0.0
    title: str = ""
    #: The turn that last announced ``processing``; the keepalive runs while it does.
    turn: fence.TurnScope | None = field(default=None, repr=False)
    keepalive: asyncio.Task[None] | None = field(default=None, repr=False)


class AgentSessionStatus:
    """Drives ``agents.sessions.*`` for one Slack client; see the module docstring."""

    def __init__(
        self,
        client: RealSlackClient,
        *,
        notify: Callable[[], Callable[..., Any] | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        #: Resolves the dashboard's ``notify`` when it is needed: the dashboard state
        #: may be wired after the client is.
        self._notify = notify
        self._clock = clock
        self._sessions: OrderedDict[tuple[str, str | None], _Session] = OrderedDict()
        self.disabled_reason = ""
        self._warned_no_stop = False

    @property
    def active(self) -> bool:
        return not self.disabled_reason

    # ── the two call-site dialects ──

    async def set_text(self, channel: str, thread_ts: str, text: str) -> bool:
        """``assistant.threads.setStatus``-style entry. False means "use the old call"."""
        if not self.active or not channel or not thread_ts:
            return False
        return await self.set_status(channel, thread_ts, lifecycle_for_text(text))

    async def set_status(self, channel: str, thread_ts: str | None, status: str) -> bool:
        """Move one session to *status*; False when the old path should run instead.

        *thread_ts* ``None`` addresses a whole-channel session (a code channel),
        where Slack refuses a ``thread_ts``. Never raises.
        """
        if not self.active or not channel or status not in SESSION_STATUSES:
            return False
        key = (channel, thread_ts or None)
        record = self._sessions.get(key)
        now = self._clock()
        if status == SESSION_STATUS_ACTIVE and (record is None or record.status in ("", status)):
            # Never set processing here (no stray session), or already ready.
            return True
        if (
            status == SESSION_STATUS_PROCESSING
            and record is not None
            and record.status == status
            and now - record.sent_at < PROCESSING_REFRESH_SECS
        ):
            # The keepalive follows whichever turn announced "working" last.
            record.turn = fence.current_scope() or record.turn
            self._arm_keepalive(key, record)
            return True
        ok = await self._send_status(channel, thread_ts, status)
        if not self.active:
            return False
        if ok:
            record = self._track(key)
            record.status = status
            record.sent_at = now
            if status == SESSION_STATUS_PROCESSING:
                record.turn = fence.current_scope()
                self._arm_keepalive(key, record)
            else:
                record.turn = None
                self._disarm(record)
        return True

    async def rename(self, channel: str, thread_ts: str | None, title: str) -> bool:
        """``agents.sessions.rename``; False means "use the old setTitle call"."""
        title = title.strip()[:_TITLE_MAX]
        if not self.active or not channel or not title:
            return False
        params: dict[str, Any] = {"channel_id": channel, "title": title}
        if thread_ts:
            params["thread_ts"] = thread_ts
        if not await self._call("agents.sessions.rename", params):
            return self.active
        self._track((channel, thread_ts or None)).title = title
        return True

    def owns(self, channel: str, thread_ts: str | None) -> bool:
        """Whether this process put that session into a lifecycle state."""
        record = self._sessions.get((channel, thread_ts or None))
        return record is not None and bool(record.status)

    def last_title(self, channel: str, thread_ts: str | None) -> str:
        record = self._sessions.get((channel, thread_ts or None))
        return record.title if record is not None else ""

    async def settle_after_stop(self, channel: str, thread_ts: str | None) -> None:
        """Move a stopped session off ``processing``, as Slack requires of the app.

        Slack does not change the status itself when Stop is pressed. Sent whatever
        was last recorded -- the click came from a loader, so Slack believes the
        session is still working, and after a restart nothing is recorded at all.
        The caller decides the session is this bot's before calling.
        """
        key = (channel, thread_ts or None)
        record = self._sessions.get(key)
        if record is not None:
            self._disarm(record)
        if not self.active or not channel:
            return
        if await self._send_status(channel, thread_ts, SESSION_STATUS_ACTIVE):
            record = self._track(key)
            record.status = SESSION_STATUS_ACTIVE
            record.sent_at = self._clock()

    async def close(self) -> None:
        for record in self._sessions.values():
            self._disarm(record)
        self._sessions.clear()

    # ── internals ──

    def _track(self, key: tuple[str, str | None]) -> _Session:
        record = self._sessions.get(key)
        if record is None:
            record = self._sessions[key] = _Session()
            while len(self._sessions) > _MAX_TRACKED_SESSIONS:
                _, evicted = self._sessions.popitem(last=False)
                self._disarm(evicted)
        else:
            self._sessions.move_to_end(key)
        return record

    def _disarm(self, record: _Session) -> None:
        task, record.keepalive = record.keepalive, None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _arm_keepalive(self, key: tuple[str, str | None], record: _Session) -> None:
        if record.turn is None:
            # Not inside a Slack turn (a code-channel status, a mirror): nothing tells
            # the keepalive when the work ends, so Slack's own timeout bounds it.
            return
        if record.keepalive is not None and not record.keepalive.done():
            return
        # A fresh context: the keepalive must not inherit the turn's write fence, and
        # it outlives the call that armed it.
        record.keepalive = asyncio.get_running_loop().create_task(
            self._keepalive(key, record), context=contextvars.Context()
        )

    async def _keepalive(self, key: tuple[str, str | None], record: _Session) -> None:
        """Re-send ``processing`` while the turn that set it is still running.

        Bound to the TURN, not to the status: a turn that ends without clearing
        its status (cancelled by another Stop path, or killed) would otherwise be
        kept "Working..." forever. Such a session is moved to ``active`` instead.
        """
        try:
            while self.active and record.status == SESSION_STATUS_PROCESSING:
                wait = record.sent_at + PROCESSING_REFRESH_SECS - self._clock()
                if wait > 0:
                    await asyncio.sleep(wait)
                    continue
                if self._sessions.get(key) is not record:
                    return
                if not fence.is_running(record.turn):
                    record.turn = None
                    if await self._send_status(key[0], key[1], SESSION_STATUS_ACTIVE):
                        record.status = SESSION_STATUS_ACTIVE
                        record.sent_at = self._clock()
                    return
                if await self._send_status(key[0], key[1], SESSION_STATUS_PROCESSING):
                    record.sent_at = self._clock()
                else:
                    return
        finally:
            if record.keepalive is asyncio.current_task():
                record.keepalive = None

    async def _send_status(self, channel: str, thread_ts: str | None, status: str) -> bool:
        params: dict[str, Any] = {"channel_id": channel, "status": status}
        if thread_ts:
            params["thread_ts"] = thread_ts
        return await self._call("agents.sessions.setStatus", params)

    async def _call(self, method: str, params: dict[str, Any]) -> bool:
        client = self._client
        client._inject_team(params["channel_id"], params)
        try:
            resp = await client._web.api_call(method, params=params)
        except Exception as exc:
            code = _error_code(exc)
            if code in DISABLING_ERRORS:
                self._disable(code)
            else:
                logger.debug("%s failed (%s)", method, code or type(exc).__name__, exc_info=True)
            return False
        warning = ""
        try:
            warning = str(resp.get("warning", "") or "")
        except Exception:
            warning = ""
        if MISSING_STOP_SUBSCRIPTION_WARNING in warning and not self._warned_no_stop:
            self._warned_no_stop = True
            logger.warning("Slack: %s — threads show no Stop button", warning)
            self._push(_NO_STOP_TITLE, _NO_STOP_BODY)
        return True

    def _disable(self, code: str) -> None:
        if self.disabled_reason:
            return
        self.disabled_reason = code
        logger.warning(
            "Slack agent sessions disabled for this process (%s); "
            "using assistant.threads status instead",
            code,
        )
        for record in self._sessions.values():
            self._disarm(record)
        self._sessions.clear()
        self._push(_DISABLED_TITLE, _DISABLED_BODY.format(code=code))

    def _push(self, title: str, body: str) -> None:
        notify = self._notify() if self._notify is not None else None
        if notify is None:
            return
        try:
            notify("agent", title, body, meta={"source": "slack.agent_sessions"})
        except Exception:
            logger.debug("agent-session notification failed", exc_info=True)


def configure(orch: Any) -> AgentSessionStatus | None:
    """Attach the driver to the orchestrator's Slack client when the feature is on.

    Called once at boot: ``slack.agent_sessions`` takes a restart to change. Returns
    the driver, or None (and detaches any old one) when the feature is off.
    """
    from kiro_crew.slack.features import agent_sessions_enabled

    client = getattr(orch, "slack", None)
    if client is None or not hasattr(client, "_web"):
        return None
    if not agent_sessions_enabled(getattr(orch, "_cfg", None)):
        client.agent_sessions = None
        return None

    def _notify() -> Callable[..., Any] | None:
        state = getattr(orch, "dashboard_state", None)
        return getattr(state, "notify", None) if state is not None else None

    driver = AgentSessionStatus(client, notify=_notify)
    client.agent_sessions = driver
    return driver


def driver_of(slack: Any) -> AgentSessionStatus | None:
    """The live driver on a Slack client, or None (feature off, disabled, or a double)."""
    driver = getattr(slack, "agent_sessions", None)
    if isinstance(driver, AgentSessionStatus) and driver.active:
        return driver
    return None


async def set_session_status(slack: Any, channel: str, thread_ts: str | None, status: str) -> bool:
    """Move a session to *status* when agent sessions are live; never raises.

    The helper the code-channel runtime uses for a whole-channel session
    (``thread_ts=None``), and the renderer for the approval pause. False when the
    feature is off, disabled for the process, or Slack refused the call.
    """
    driver = driver_of(slack)
    if driver is None or fence.turn_muted():
        return False
    try:
        return await driver.set_status(channel, thread_ts, status)
    except Exception:
        logger.debug("agent session status update failed", exc_info=True)
        return False

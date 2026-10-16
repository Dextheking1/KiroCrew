"""The write fence that keeps a natively stopped Slack turn silent.

When a user presses Slack's native Stop, the turn they stopped must post
nothing further: no partial answer sealed into the thread, no timing footer, no
"Working..." status flipped back on by a tool event that was already in flight.
The turn does not know it was stopped by Slack -- it only sees a cooperative
cancel, the same one the dashboard Stop sends -- so the fence sits where every
Slack write already goes through, the client.

Each Slack turn runs inside a :class:`TurnScope` (:func:`fenced_turn` wraps the
transport entry point; the native one calls :func:`enter_turn`). The scope rides a context variable, so every task the
turn spawns inherits it, and :meth:`RealSlackClient` asks :func:`turn_muted`
before each write. A Stop marks the live scopes of the thread it addresses as
stopped (:func:`mute_turns`); from then on those turns' writes are dropped
quietly. A NEW turn opens a new scope, so the next message in the thread is
answered normally -- nothing has to "resume" the thread.

Nothing here ever mutes a write unless a native Stop marked a scope, and only
the agent-sessions runtime does that, so with the feature off the fence is
inert.
"""

from __future__ import annotations

import contextvars
import functools
import weakref
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from typing import Any, ParamSpec, TypeAlias, TypeVar

_P = ParamSpec("_P")
_R = TypeVar("_R")


class TurnScope:
    """One Slack turn: where it replies, and whether a native Stop silenced it."""

    __slots__ = ("channel", "thread_ts", "stopped", "__weakref__")

    def __init__(self, channel: str, thread_ts: str) -> None:
        self.channel = channel
        #: The thread the turn replies in (``thread_ts or msg_ts``), the one Slack's
        #: agent session -- and so its Stop button -- is scoped to.
        self.thread_ts = thread_ts
        self.stopped = False


_CURRENT: ContextVar[TurnScope | None] = ContextVar("slack_turn_scope", default=None)

#: The scopes of turns still running. Weak, so a scope lives exactly as long as the
#: turn (and any task it spawned) still holds it; nothing here has to be pruned.
_LIVE: weakref.WeakSet[TurnScope] = weakref.WeakSet()

#: What :func:`enter_turn` hands back for :func:`exit_turn` to restore.
_Reset: TypeAlias = "contextvars.Token[TurnScope | None]"


def turn_muted() -> bool:
    """Whether the Slack write being made belongs to a turn a native Stop silenced."""
    scope = _CURRENT.get()
    return scope is not None and scope.stopped


def current_scope() -> TurnScope | None:
    """The scope of the Slack turn the caller runs in, if any."""
    return _CURRENT.get()


def is_running(scope: TurnScope | None) -> bool:
    """Whether *scope*'s turn is still running and was not stopped."""
    return scope is not None and not scope.stopped and scope in _LIVE


def _arg(args: tuple[Any, ...], kwargs: dict[str, Any], index: int, name: str) -> Any:
    if name in kwargs:
        return kwargs[name]
    return args[index] if len(args) > index else None


def enter_turn(channel: str, thread_ts: str) -> _Reset:
    """Open a :class:`TurnScope` for the rest of the caller's task.

    Pair it with :func:`exit_turn` in a ``finally``. For an entry point that cannot
    be wrapped: the native ``handle_message`` must keep its own code object in the
    facade file (the handler composition contract).
    """
    scope = TurnScope(channel, thread_ts)
    _LIVE.add(scope)
    return _CURRENT.set(scope)


def exit_turn(reset: _Reset) -> None:
    scope = _CURRENT.get()
    if scope is not None:
        _LIVE.discard(scope)
    _CURRENT.reset(reset)


def fenced_turn(
    fn: Callable[_P, Coroutine[Any, Any, _R]],
) -> Callable[_P, Coroutine[Any, Any, _R]]:
    """Run a Slack turn entry point inside its own :class:`TurnScope`.

    For ``handle_message_transport``, whose leading positional shape is
    ``(slack, sessions, channel, text, thread_ts, msg_ts)``.
    """

    @functools.wraps(fn)
    async def _wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        channel = str(_arg(args, kwargs, 2, "channel") or "")
        thread = _arg(args, kwargs, 4, "thread_ts") or _arg(args, kwargs, 5, "msg_ts") or ""
        reset = enter_turn(channel, str(thread))
        try:
            return await fn(*args, **kwargs)
        finally:
            exit_turn(reset)

    return _wrapper


def mute_turns(channel: str, thread_ts: str | None) -> list[TurnScope]:
    """Silence the running turns a Stop addresses; returns the scopes it marked.

    *thread_ts* ``None`` addresses every running turn in *channel*: a single-session
    DM is one conversation whichever thread a turn replies in, and a code channel is
    one session for the whole channel.
    """
    marked = [
        scope
        for scope in list(_LIVE)
        if scope.channel == channel and (thread_ts is None or scope.thread_ts == thread_ts)
    ]
    for scope in marked:
        scope.stopped = True
    return marked


def unmute(scopes: list[TurnScope]) -> None:
    """Undo :func:`mute_turns` for a Stop that turned out to stop nothing."""
    for scope in scopes:
        scope.stopped = False

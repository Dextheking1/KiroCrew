"""A restart keeps the app's slot reserved while it drains the dead root's tree.

``_restart_exited_backend`` drains the exited root's process tree before it spawns
the replacement, and that drain takes seconds (a SIGTERM grace, then SIGKILL). If
the dead record were simply removed first, the app would read as free to start for
that whole span: a concurrent start (enable, update, the boot pass, or any internal
starter) would begin its own spawn, and on a fixed port it could adopt the forked
server the drain is about to end, leaving a dead adopted record nothing restarts.

The restart therefore swaps the dead record for a STARTING reservation and holds
the app's lifecycle flock across the forget + drain. These tests hold the drain
open at the ``_drain_exited_root_tree`` seam and run a second start against it;
nothing here spawns a process or binds a port.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from apps_backend_coverage_helpers import _isolated_module_state  # noqa: F401
from apps_backend_coverage_helpers import _fake_proc, _manifest

import kiro_crew.apps.backend as bmod
from kiro_crew.apps.backend import AppProcess

_APP = "app"
_WAIT = 5.0


class _HeldDrain:
    """Stands in for ``_drain_exited_root_tree``; the first call blocks until released."""

    def __init__(self, during: Callable[[], None] | None = None) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self._during = during

    def __call__(self, *_args: Any) -> bool:
        self.calls += 1
        if self.calls > 1:
            return True
        self.entered.set()
        if self._during is not None:
            self._during()
        assert self.release.wait(_WAIT), "the test never released the drain"
        return True


class _Body:
    """Stands in for the spawn body: adopts while the drain is open, spawns after."""

    def __init__(self, drain: _HeldDrain) -> None:
        self._drain = drain
        self.adopted = threading.Event()
        self.spawned: list[AppProcess] = []

    def __call__(self, name: str, _manifest: Any) -> AppProcess:
        if self._drain.entered.is_set() and not self._drain.release.is_set():
            # The shape the issue reports: a fixed-port probe finds the dead
            # launcher's forked server still answering and adopts it.
            record = AppProcess(app_name=name, port=9136, adopted_pids=[9999])
            self.adopted.set()
        else:
            record = AppProcess(app_name=name, port=9136, pid=618, proc=_fake_proc(pid=618))
            self.spawned.append(record)
        with bmod._lock:
            bmod._processes[name] = record
            bmod._allocated_ports[name] = record.port
        return record


@pytest.fixture()
def restart_env(monkeypatch: pytest.MonkeyPatch) -> AppProcess:
    """An exited, tracked backend whose restart is permitted and runs at once."""
    monkeypatch.setattr(
        bmod,
        "shutdown_event",
        SimpleNamespace(is_set=lambda: False, wait=lambda _delay: False),
    )
    monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
    monkeypatch.setattr(bmod, "_forget_app_pid_if", lambda *_args: None)
    monkeypatch.setattr(bmod, "_forget_app_pid", lambda *_args: None)
    monkeypatch.setattr(bmod, "_activation_denied", lambda _n, _a: bmod.ActivationVerdict())
    monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest("server.py"))
    ap = AppProcess(
        app_name=_APP,
        port=9136,
        pid=617,
        proc=_fake_proc(pid=617, returncode=1),
        healthy=False,
        mcp_healthy=False,
    )
    with bmod._lock:
        bmod._processes[_APP] = ap
        bmod._allocated_ports[_APP] = ap.port
    return ap


def _run(target: Callable[[], Any]) -> tuple[threading.Thread, list[Any]]:
    out: list[Any] = []

    def _go() -> None:
        try:
            out.append(target())
        except BaseException as exc:  # noqa: BLE001 — surfaced by the assertion below
            out.append(exc)

    thread = threading.Thread(target=_go, daemon=True)
    thread.start()
    return thread, out


@pytest.mark.parametrize(
    "starter",
    ["start_app_backend", "_start_app_backend"],
    ids=["public-start", "internal-start"],
)
def test_a_start_during_the_drain_waits_instead_of_adopting(
    restart_env: AppProcess, monkeypatch: pytest.MonkeyPatch, starter: str
) -> None:
    drain = _HeldDrain()
    body = _Body(drain)
    monkeypatch.setattr(bmod, "_drain_exited_root_tree", drain)
    monkeypatch.setattr(bmod, "_start_app_backend_body", body)

    restart, restart_out = _run(lambda: bmod._restart_exited_backend(restart_env, 1))
    assert drain.entered.wait(_WAIT)
    with bmod._lock:
        held = bmod._processes.get(_APP)
    assert held is not None and held.starting is True, "the slot must stay reserved"

    start, start_out = _run(lambda: getattr(bmod, starter)(_APP))
    assert not body.adopted.wait(1.0), "a start during the drain adopted the draining server"
    with bmod._lock:
        assert bmod._processes.get(_APP) is held

    drain.release.set()
    restart.join(_WAIT)
    start.join(_WAIT + 25.0)
    assert not restart.is_alive() and not start.is_alive()
    assert restart_out == [True]
    assert not body.adopted.is_set()
    assert len(body.spawned) == 1
    replacement = body.spawned[0]
    assert bmod._processes[_APP] is replacement
    assert start_out in ([None], [replacement])


def test_a_waiter_that_outlasts_its_window_leaves_the_reservation_in_place(
    restart_env: AppProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flock held across the drain tells a timed-out waiter the owner is alive."""
    waited: list[Any] = []

    def _during() -> None:
        waited.append(bmod._await_inflight_spawn(_APP, timeout=0.2))
        with bmod._lock:
            waited.append(bmod._processes.get(_APP))
        drain.release.set()

    drain = _HeldDrain(during=_during)
    body = _Body(drain)
    monkeypatch.setattr(bmod, "_drain_exited_root_tree", drain)
    monkeypatch.setattr(bmod, "_start_app_backend_body", body)

    assert bmod._restart_exited_backend(restart_env, 1) is True
    assert waited[0] is None
    assert waited[1] is not None and waited[1].starting is True
    assert len(body.spawned) == 1 and bmod._processes[_APP] is body.spawned[0]


def test_a_policy_denial_leaves_no_reservation_and_frees_the_flock(
    restart_env: AppProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bmod, "_drain_exited_root_tree", lambda *_a: True)
    monkeypatch.setattr(
        bmod, "_activation_denied", lambda _n, _a: bmod.ActivationVerdict(denied="policy")
    )
    monkeypatch.setattr(
        bmod, "_start_app_backend_body", lambda *_a: pytest.fail("a denied restart spawned")
    )
    assert bmod._restart_exited_backend(restart_env, 1) is False
    assert _APP not in bmod._processes
    with bmod.app_backend_lifecycle_flock(_APP):
        pass


def test_a_transient_denial_restores_the_dead_record_and_retries(
    restart_env: AppProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    verdicts = iter(
        [
            bmod.ActivationVerdict(denied="governance evaluation error: x", transient=True),
            bmod.ActivationVerdict(),
        ]
    )
    drain = _HeldDrain()
    drain.release.set()
    body = _Body(drain)
    monkeypatch.setattr(bmod, "_drain_exited_root_tree", drain)
    monkeypatch.setattr(bmod, "_activation_denied", lambda _n, _a: next(verdicts))
    monkeypatch.setattr(bmod, "_start_app_backend_body", body)

    assert bmod._restart_exited_backend(restart_env, 1) is True
    assert bmod._restart_attempts[_APP] == 2
    assert len(body.spawned) == 1 and bmod._processes[_APP] is body.spawned[0]


def test_a_drain_that_raises_leaves_no_reservation(
    restart_env: AppProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: Any) -> bool:
        raise RuntimeError("drain failed")

    monkeypatch.setattr(bmod, "_drain_exited_root_tree", _boom)
    with pytest.raises(RuntimeError, match="drain failed"):
        bmod._restart_exited_backend(restart_env, 1)
    assert _APP not in bmod._processes
    with bmod.app_backend_lifecycle_flock(_APP):
        pass


def test_a_stop_during_the_drain_wins_and_the_replacement_is_stopped(
    restart_env: AppProcess, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: list[Any] = []

    def _during() -> None:
        stopped.append(bmod.stop_app_backend(_APP))
        with bmod._lock:
            stopped.append(bmod._processes.get(_APP))
        drain.release.set()

    drain = _HeldDrain(during=_during)
    body = _Body(drain)
    monkeypatch.setattr(bmod, "_drain_exited_root_tree", drain)
    monkeypatch.setattr(bmod, "_start_app_backend_body", body)
    monkeypatch.setattr(bmod.platform_compat, "kill_process_tree", lambda *_a, **_k: None)
    monkeypatch.setattr(
        bmod.platform_compat, "kill_process_tree_pinned", lambda *_a, **_k: True, raising=False
    )

    assert bmod._restart_exited_backend(restart_env, 1) is False
    assert stopped[1] is None, "the stop removed the reservation"
    assert bmod._lifecycle_generation[_APP][1] == bmod._LIFECYCLE_STOP
    assert _APP not in bmod._processes

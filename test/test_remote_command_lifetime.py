"""A timed-out or retired guest command cannot leave its process group running."""

import os
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from kiro_crew.remote_session import workspace_tools as wt
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import wait_until

posix_groups = pytest.mark.skipif(
    not hasattr(os, "killpg"), reason="guest commands run on a POSIX workspace"
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); it is dead for this purpose.
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
            return handle.read().split(") ", 1)[1][:1] != "Z"
    except OSError:
        return True


def _background_pid(path) -> int:
    text = wait_until(
        lambda: path.exists() and path.read_text(encoding="ascii").strip(),
        describe=lambda: "the command never started its background job",
    )
    return int(text)


def _wait_dead(pid: int) -> None:
    wait_until(lambda: not _alive(pid), describe=lambda: f"pid {pid} still alive")


@posix_groups
def test_a_timed_out_command_takes_its_whole_group_with_it(tmp_path):
    marker = tmp_path / "bg.pid"
    ws = wt.Workspace(str(tmp_path))
    out = ws.run_command(f"sleep 60 & echo $! > {marker}; echo partial; sleep 60", timeout=1)
    assert out.startswith("partial\n") and out.endswith("[timed out after 1s]")
    _wait_dead(_background_pid(marker))


@posix_groups
def test_a_retired_command_takes_its_whole_group_with_it(tmp_path, monkeypatch):
    marker = tmp_path / "bg.pid"
    real_popen = subprocess.Popen

    class RetiredWhileWaiting(real_popen):
        def wait(self, timeout=None):
            if timeout is not None and timeout > 10:
                _background_pid(marker)
                raise SystemExit(0)  # what main()'s SIGTERM handler raises
            return super().wait(timeout=timeout)

    monkeypatch.setattr(wt.subprocess, "Popen", RetiredWhileWaiting)
    ws = wt.Workspace(str(tmp_path))
    with pytest.raises(SystemExit):
        ws.run_command(f"sleep 60 & echo $! > {marker}; sleep 60", timeout=60)
    _wait_dead(_background_pid(marker))


@posix_groups
def test_a_finished_command_reports_its_output_and_code(tmp_path):
    ws = wt.Workspace(str(tmp_path))
    assert ws.run_command("echo ok; exit 3", timeout=10) == "ok\n\n[exit 3]"


@posix_groups
def test_endless_output_is_kept_as_a_bounded_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(wt, "MAX_OUTPUT_BYTES", 1024)
    seen = []
    real_snapshot = wt._OutputTail.snapshot

    def snapshot(self):
        seen.append(len(self._buf))
        return real_snapshot(self)

    monkeypatch.setattr(wt._OutputTail, "snapshot", snapshot)
    ws = wt.Workspace(str(tmp_path))
    total = 4 * 1024 * 1024
    script = f"import sys; sys.stdout.write('x' * {total - 4} + 'END\\n')"
    out = ws.run_command(f'{sys.executable} -c "{script}"', timeout=60)
    assert out.startswith(f"[{total - 1024} earlier bytes omitted]\n")
    assert out.endswith("END\n\n[exit 0]")
    assert seen == [1024]


def test_the_tail_never_holds_more_than_its_limit():
    tail = wt._OutputTail(10)
    chunks = [b"a" * 7, b"b" * 7, b"c" * 30, b""]
    sizes = []

    class Stream:
        def read1(self, _n):
            if tail._buf:
                sizes.append(len(tail._buf))
            return chunks.pop(0)

        def close(self):
            sizes.append("closed")

    tail.drain(Stream())
    assert all(s == "closed" or s <= 10 for s in sizes) and sizes[-1] == "closed"
    assert tail.snapshot() == (b"c" * 10, 34)


def test_without_process_groups_the_command_itself_is_killed(monkeypatch):
    monkeypatch.delattr(wt.os, "killpg", raising=False)
    killed = []
    wt._kill_group(SimpleNamespace(pid=UNALLOCATABLE_PID, kill=lambda: killed.append(True)))
    assert killed == [True]


def test_with_process_groups_the_group_is_signalled_first(monkeypatch):
    events = []
    monkeypatch.setattr(
        wt.os, "killpg", lambda pid, sig: events.append(("group", pid, sig)), raising=False
    )
    monkeypatch.setattr(wt.signal, "SIGKILL", 9, raising=False)
    wt._kill_group(SimpleNamespace(pid=UNALLOCATABLE_PID, kill=lambda: events.append(("leader",))))
    assert events == [("group", UNALLOCATABLE_PID, 9), ("leader",)]


def test_stdio_retirement_unwinds_and_restores_signal_handler(tmp_path, monkeypatch):
    handlers = []
    previous = signal.getsignal(signal.SIGTERM)
    real_signal = signal.signal

    def install(sig, handler):
        if sig != signal.SIGTERM:
            return real_signal(sig, handler)
        handlers.append(handler)
        return previous

    def serve(_server):
        handlers[0](signal.SIGTERM, None)
        pytest.fail("SIGTERM must unwind the command")

    monkeypatch.setattr(signal, "signal", install)
    monkeypatch.setattr(wt, "serve", serve)
    with pytest.raises(SystemExit):
        wt.main(["--root", str(tmp_path)])
    assert handlers[-1] is previous

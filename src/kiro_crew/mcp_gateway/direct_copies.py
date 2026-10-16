"""Measure what the per-session copies of an MCP server cost right now.

The MCP page offers to share a server and says what that saves. The number has
to be a measurement of THIS host, never an estimate: it is read from the process
table, and when nothing can be read the page shows no number at all. The saving
is computed from PSS (``smaps_rollup``), not RSS, and net of the stub each chat
runs in place of its copy, so a server whose copies share most of their pages
can show no saving at all.

A copy is a process that runs one of the server's declared launches -- its
absolute command as ``argv[0]`` (or ``argv[1]``, for a script run by an
interpreter) followed by that launch's declared args -- together with every
process below it. The args are part of the match because one binary often
serves several servers under different subcommands. Bare command names
(``npx``, ``node``) are not matched: they name every process of that program,
not this server's copies, so counting them would report memory the server does
not use.

Linux only (``/proc``, read through :mod:`kiro_crew.platform_compat`).
Elsewhere, and on any read failure, a server simply has no measurement.
BLOCKING, so call it off the event loop.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from kiro_crew import platform_compat

#: The marker every gateway-side helper carries in its argv. A stub or the
#: shared daemon is never a per-session copy of the server it fronts.
_GATEWAY_MODULE_MARKER = "kiro_crew.mcp_gateway"


#: The stub module every shared server's per-chat stand-in runs as.
_STUB_MARKER = "kiro_crew.mcp_gateway.stub"


@dataclass(frozen=True)
class DirectCopies:
    """The per-session copies of one server running now.

    ``rss_bytes`` is the copies' summed resident set, kept for display only: it
    counts the code pages copies of one program share once per process, so it
    overstates what they cost. ``pss_bytes`` (``smaps_rollup``) charges each
    shared page to its sharers proportionally, which is what the copies cost
    the host; ``None`` when any process in a copy's tree has no PSS reading.
    ``stub_pss_bytes`` is the PSS of one per-chat stub measured on this host,
    ``None`` when no stub runs to measure.
    """

    copies: int
    rss_bytes: int
    pss_bytes: int | None = None
    stub_pss_bytes: int | None = None

    @property
    def saves_bytes(self) -> int | None:
        """What one shared copy would save, from the measured copies.

        Sharing leaves one copy running and gives every chat a stub in place of
        its copy, so the saving is every copy but one average copy, less one
        stub per chat. ``None`` when that cannot be computed from readings of
        this host (fewer than two copies, no PSS, no stub to measure) or when it
        saves nothing: a number would claim otherwise.
        """
        if self.copies < 2 or self.pss_bytes is None or self.stub_pss_bytes is None:
            return None
        saved = self.pss_bytes - self.pss_bytes // self.copies - self.copies * self.stub_pss_bytes
        return saved if saved > 0 else None


def _read_cmdline(proc_root: Path, pid: int) -> list[str]:
    raw = platform_compat.read_proc_cmdline(pid, proc_root=proc_root)
    return [os.fsdecode(part) for part in raw.split(b"\0") if part]


def _matches(argv: list[str], launches: frozenset[tuple[str, ...]]) -> bool:
    """Whether *argv* runs one of *launches*: its command, then its declared args.

    The args matter: one binary often serves several servers under different
    subcommands, and matching the command alone would count all of them.
    """
    if not argv or any(_GATEWAY_MODULE_MARKER in part for part in argv):
        return False
    for start in (0, 1):
        if len(argv) <= start:
            break
        for command, *args in launches:
            if argv[start] == command and argv[start + 1 : start + 1 + len(args)] == args:
                return True
    return False


def measure(
    launches_by_server: Mapping[str, Iterable[Sequence[str] | str]],
    *,
    proc_root: Path = Path("/proc"),
    page_size: int | None = None,
) -> dict[str, DirectCopies]:
    """``{server: DirectCopies}`` for every server with at least one copy running.

    *launches_by_server* maps a server name to the launches its specs declare,
    each ``(command, *args)`` (a bare string is a command with no args). Only
    absolute commands are matched. A server absent from the result has no
    measurement -- not "zero copies", which this cannot tell apart from "not
    readable".
    """
    wanted: dict[str, frozenset[tuple[str, ...]]] = {}
    for server, launches in launches_by_server.items():
        found: set[tuple[str, ...]] = set()
        for launch in launches:
            parts = (launch,) if isinstance(launch, str) else tuple(str(p) for p in launch)
            if not parts or not os.path.isabs(parts[0]):
                continue
            found.add(parts)
            found.add((os.path.realpath(parts[0]), *parts[1:]))
        if found:
            wanted[server] = frozenset(found)
    if not wanted or not proc_root.is_dir():
        return {}
    # One process-table walker: runtime_reconcile's. Imported here because it
    # pulls in the session runtime, which the MCP handlers do not need at import.
    from kiro_crew import runtime_reconcile

    try:
        table = runtime_reconcile.same_uid_process_table(proc_root=proc_root, page_size=page_size)
    except (AttributeError, OSError, ValueError):
        return {}
    pids = list(table)
    argv_of = {pid: _read_cmdline(proc_root, pid) for pid in pids}

    def pss_of(pid: int) -> int | None:
        kb = platform_compat.read_proc_pss_kb(pid, proc_root=proc_root)
        return kb * 1024 if kb >= 0 else None

    def tree_pss(root: int) -> int | None:
        total = 0
        for pid in runtime_reconcile.tree_pids(root, table):
            one = pss_of(pid)
            if one is None:
                return None
            total += one
        return total

    # One per-chat stub, as it costs this host now: the mean over the stubs
    # running. Without one there is nothing to measure, so no saving is shown.
    stub_readings = [
        one
        for pid in pids
        if any(_STUB_MARKER in part for part in argv_of[pid]) and (one := pss_of(pid)) is not None
    ]
    stub_pss = sum(stub_readings) // len(stub_readings) if stub_readings else None

    out: dict[str, DirectCopies] = {}
    for server, commands in wanted.items():
        roots = [pid for pid in pids if _matches(argv_of[pid], commands)]
        root_set = set(roots)
        # A copy that forks a child running the same command is ONE copy.
        roots = [pid for pid in roots if table[pid][0] not in root_set]
        if not roots:
            continue
        pss_trees = [tree_pss(pid) for pid in roots]
        out[server] = DirectCopies(
            len(roots),
            sum(runtime_reconcile.tree_rss(pid, table) for pid in roots),
            (
                None
                if any(p is None for p in pss_trees)
                else sum(p for p in pss_trees if p is not None)
            ),
            stub_pss,
        )
    return out

"""Crew Mode: which agents are conductors, which sessions are Crew Mode roots, and
which sessions a root dispatched.

Crew Mode is a chat on the Crew Mode conductor TEMPLATE. ``agent.crew_mode_agent``
names the one a chat switches to when Crew Mode is turned on, and it is the only place
that names it, so a better conductor is swapped in by changing that one key.

Two lists, for two questions. :func:`conductor_agents` is every shipped conductor plus
the configured one: the dashboard keeps all of them out of a normal chat's agent
picker. :func:`crew_mode_agents` is the Crew Mode family, the configured agent and the
deprecated name that resolves to it: only a chat on one of those is Crew Mode. The
pipeline, security and team-lead conductors are not, so their sessions keep their
folders and stay in the person's lists.

Turning Crew Mode on stamps the agent on the chat (``crew_mode_agent`` on the slot,
persisted with it), so a chat stays in Crew Mode on the conductor it started on after
``agent.crew_mode_agent`` changes; new chats get the new one.

A **Crew Mode root** is a ROOT session (nothing dispatched it) running the family's
agent, or the agent its stamp names, as a template, never as a crewmate. Every session it dispatched, directly
or through a session it dispatched, is its **worker**: the slot list stamps each worker
with ``crew_root`` (its root's key) so the person's own session list can leave it out
and the side panel can list it under its root instead.

A leaf on purpose: it reads slot payload dicts and config objects, never the dashboard
state, so the slot serializer, the session-control handlers and the MCP tools can all
ask it the same question.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

from kiro_crew.agent_files import (
    CONDUCTOR_AGENT_FILENAME,
    LEDGER_CONDUCTOR_AGENT_FILENAME,
    PIPELINE_CONDUCTOR_AGENT_FILENAME,
    SECURITY_CONDUCTOR_AGENT_FILENAME,
    TEAM_LEAD_AGENT_FILENAME,
)

#: The agent Crew Mode runs while ``agent.crew_mode_agent`` is unset.
DEFAULT_CREW_MODE_AGENT: Final[str] = CONDUCTOR_AGENT_FILENAME.removesuffix(".json")

#: The shipped conductors: every agent whose job is dispatching work to other
#: sessions, all kept out of a normal chat's agent picker. Derived from the
#: filenames the runtime installs, so a renamed spec cannot leave a stale literal.
CONDUCTOR_TEMPLATES: Final[frozenset[str]] = frozenset(
    name.removesuffix(".json")
    for name in (
        CONDUCTOR_AGENT_FILENAME,
        PIPELINE_CONDUCTOR_AGENT_FILENAME,
        LEDGER_CONDUCTOR_AGENT_FILENAME,
        SECURITY_CONDUCTOR_AGENT_FILENAME,
        TEAM_LEAD_AGENT_FILENAME,
    )
)

#: The slot field (and metadata key) that stamps the agent Crew Mode turned on with.
STAMP_FIELD: Final[str] = "crew_mode_agent"

#: Deprecated names that resolve to the shipped Crew Mode conductor
#: (``conductor_agents.DEPRECATED_AGENT_SPECS``): a chat on one is on that conductor.
#: Derived from the filenames the runtime installs, so a renamed spec cannot leave a
#: stale literal here.
CONDUCTOR_ALIASES: Final[dict[str, str]] = {
    LEDGER_CONDUCTOR_AGENT_FILENAME.removesuffix(".json"): DEFAULT_CREW_MODE_AGENT,
}

#: The row fields the slot list stamps. ``crew_mode_root`` is True on a Crew Mode
#: root; ``crew_root`` is the root's slot key on each of its workers, absent elsewhere.
ROOT_FIELD: Final[str] = "crew_mode_root"
WORKER_FIELD: Final[str] = "crew_root"

#: The last ``agent.crew_mode_agent`` an explicit config read saw (:func:`remember`):
#: the agent catalog and the agent switch pass the config
#: they loaded. It is the fallback only. Every read without a config asks the live
#: config watcher's snapshot first (:func:`_live_cfg`), which the gateway primes from
#: its boot config and refreshes on every edit of the file, so a custom
#: ``agent.crew_mode_agent`` is in force from gateway start without any browser read.
_configured: str = DEFAULT_CREW_MODE_AGENT


def _live_cfg() -> Any:
    """The live config watcher's snapshot, or ``None`` when it is not armed. No I/O."""
    try:
        from kiro_crew.config import live

        return live.snapshot()
    except Exception:
        return None


def _agent_of(cfg: Any) -> str:
    value = getattr(getattr(cfg, "agent", None), "crew_mode_agent", "")
    return value.strip() if isinstance(value, str) and value.strip() else DEFAULT_CREW_MODE_AGENT


def crew_mode_agent(cfg: Any = None) -> str:
    """The agent a chat switches to when Crew Mode is turned on.

    From *cfg* when given (and remembered); otherwise from the live config snapshot,
    else the last value remembered. Never reads the config file, so the slot
    serializer can ask it on the event loop.
    """
    if cfg is not None:
        remember(cfg)
        return _configured
    live_cfg = _live_cfg()
    if live_cfg is not None:
        return _agent_of(live_cfg)
    return _configured


def remember(cfg: Any) -> None:
    """Record *cfg*'s ``agent.crew_mode_agent`` for readers that cannot load config."""
    global _configured
    _configured = _agent_of(cfg)


def conductor_agents(cfg: Any = None) -> frozenset[str]:
    """Every conductor: the shipped ones plus the configured Crew Mode agent.

    The list a normal chat's agent picker leaves out. Being on one is not Crew Mode
    (:func:`crew_mode_agents` is).
    """
    return CONDUCTOR_TEMPLATES | {crew_mode_agent(cfg)}


def crew_mode_agents(cfg: Any = None) -> frozenset[str]:
    """The Crew Mode family: the configured agent and every deprecated name of it."""
    agent = crew_mode_agent(cfg)
    return frozenset({agent} | {alias for alias, to in CONDUCTOR_ALIASES.items() if to == agent})


def stamp_for_switch(agent: object, agent_kind: object) -> str:
    """The stamp a switch to *agent* records: *agent* when that turns Crew Mode on, else ``""``.

    A switch in the template namespace onto the family's agent is Crew Mode turning
    on. A crewmate carrying the name is that crewmate.
    """
    if agent_kind == "member" or not isinstance(agent, str):
        return ""
    return agent if agent in crew_mode_agents() else ""


def runs_crew_mode(
    agent: object, agent_kind: object, mode: object = "", stamp: object = ""
) -> bool:
    """Whether a session on *agent* (in namespace *agent_kind*) is on Crew Mode's agent.

    The family's agent, or the agent *stamp* names (a chat that turned Crew Mode on
    before ``agent.crew_mode_agent`` changed): a stamp counts only while the chat is
    still on that agent, so a later switch away ends Crew Mode with no clean-up. A
    crewmate that carries a conductor's name is that crewmate, not a conductor.
    """
    if agent_kind == "member" or mode == "member" or not isinstance(agent, str) or not agent:
        return False
    return agent in crew_mode_agents() or (isinstance(stamp, str) and stamp == agent)


def _row_is_root(row: Mapping[str, Any]) -> bool:
    """Whether *row* (a slot payload) is a Crew Mode root.

    Root means no creator by either reading the sidebar nests on: ``created_by``
    (the session-control create stamp) and the crew log's ``parent`` edge (which also
    carries an adoption). App-owned and remote rows never conduct here.
    """
    if row.get("created_by") or row.get("parent") or row.get("app") or row.get("instance_id"):
        return False
    if (row.get("memory_mode") or "persistent") != "persistent":
        return False
    return runs_crew_mode(
        row.get("agent"), row.get("agent_kind"), row.get("mode"), row.get(STAMP_FIELD)
    )


def lineage_is_settled() -> bool:
    """Whether the slot frame's ``parent`` edges are the gateway's settled lineage.

    True while the crew log records and its projection is seeded for this store
    with no failed seed pending: then a row's ``parent`` (already falling back to a
    just-minted child's ``created_by``) is the whole answer, ``None`` included, so a
    released worker stands on its own. False otherwise, and the birth stamp decides.
    In memory only; never raises.
    """
    try:
        from kiro_crew.crew_log import emit as crew_log_emit

        if not crew_log_emit.enabled():
            return False
        from kiro_crew.crew_log.session_tree_projection import projection

        proj = projection()
        return bool(proj.seeded_for_current_store) and not bool(proj.seed_retry_due)
    except Exception:
        return False


def _creator_of(row: Mapping[str, Any], settled: bool = False) -> str:
    """The slot key that holds *row* now, or ``""``.

    The crew log's ``parent`` edge first: it is the current lineage, which an
    adoption (``session_adopt``) moves, so an adopted worker belongs to its new
    root. With *settled* lineage (:func:`lineage_is_settled`) a row whose edge is
    ``None`` has no holder, because ``session_release`` cleared it. Otherwise
    ``created_by`` (the birth stamp) stands in for a missing edge.
    """
    parent = row.get("parent")
    if isinstance(parent, Mapping):
        cited = parent.get("slot") or parent.get("key")
        if isinstance(cited, str) and cited:
            return cited
    if settled and "parent" in row and not row.get("lineage_pending"):
        return ""
    created_by = row.get("created_by")
    if isinstance(created_by, str) and created_by:
        return created_by
    return ""


def _live_ancestor_of(row: Mapping[str, Any]) -> str:
    """The nearest OPEN ancestor the slot frame resolved for *row*, or ``""``.

    ``parent.key`` is that ancestor's slot key (``lineage_parents`` moves it past a
    closed creator); ``parent.slot`` names the creator itself, open or not.
    """
    parent = row.get("parent")
    if isinstance(parent, Mapping):
        key = parent.get("key")
        if isinstance(key, str):
            return key
    return ""


def stamp_rows(rows: Iterable[dict[str, Any]], *, settled: bool = False) -> None:
    """Stamp :data:`ROOT_FIELD` on every row and :data:`WORKER_FIELD` on each worker. IN PLACE.

    A worker is found by walking its creators up to a live Crew Mode root, so a
    grandchild is a worker of the same root. The walk stops on a repeat, which ends
    it on any input (a cycle in forged records nests nothing). Past a creator that
    is not listed (a closed worker) the walk follows the live ancestor the slot
    frame resolved for that row (``parent.key``), so a worker whose own creator
    closed stays in its root's tree; with no live ancestor the walk ends, and a
    worker whose root has closed is shown like any other session.
    """
    rows = list(rows)
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row.get("key")
        if isinstance(key, str):
            by_key[key] = row
    roots = {key for key, row in by_key.items() if _row_is_root(row)}
    for key, row in by_key.items():
        row[ROOT_FIELD] = key in roots
        row.pop(WORKER_FIELD, None)
        if key in roots:
            continue
        seen = {key}
        current = row
        creator = _creator_of(row, settled)
        while creator and creator not in seen:
            if creator in roots:
                row[WORKER_FIELD] = creator
                break
            seen.add(creator)
            above = by_key.get(creator)
            if above is None:
                creator = _live_ancestor_of(current)
                continue
            current = above
            creator = _creator_of(above, settled)


def in_crew_tree(row: Mapping[str, Any] | None) -> bool:
    """Whether a stamped row is a Crew Mode root or one of its workers."""
    return bool(row) and bool(row.get(ROOT_FIELD) or row.get(WORKER_FIELD))  # type: ignore[union-attr]


def workers_of(rows: Iterable[Mapping[str, Any]], root_key: str) -> list[Mapping[str, Any]]:
    """The stamped rows whose root is *root_key*."""
    return [row for row in rows if root_key and row.get(WORKER_FIELD) == root_key]


#: ``GET /api/chat/slots?crew=`` values. ``roots`` lists only Crew Mode roots (the
#: session filter's Crew Mode chip); ``all`` keeps every row, workers included.
VIEW_ROOTS: Final[str] = "roots"
VIEW_ALL: Final[str] = "all"


def list_view(rows: list[dict[str, Any]], view: str, *, agent: bool) -> list[dict[str, Any]]:
    """The session list's Crew Mode view of stamped *rows*.

    A session a Crew Mode root dispatched is that root's to show (its side panel's
    worker list), so a PERSON's list leaves it out unless *view* is ``all``. An
    *agent*'s read keeps every row by default: its folder, status and lineage tools
    resolve their own and their children's rows from this list. ``roots`` returns
    only Crew Mode roots, for any caller.

    The dashboard reads ``crew=all`` and filters client-side (``crewModeView`` in
    ``website/src/lib/crewMode.ts``), because its live slot frame must carry every
    row; this default serves other person clients (scripts, ``curl``).
    """
    if view == VIEW_ROOTS:
        return [r for r in rows if r.get(ROOT_FIELD)]
    if view == VIEW_ALL or agent:
        return rows
    return [r for r in rows if not r.get(WORKER_FIELD)]


def _live_row(slot: Any) -> dict[str, Any]:
    """The fields :func:`stamp_rows` reads, taken off a live slot object."""
    return {
        "key": getattr(slot, "key", ""),
        "created_by": getattr(slot, "_created_by", "") or "",
        "parent": None,
        "agent": getattr(slot, "agent", ""),
        "agent_kind": getattr(slot, "agent_kind", ""),
        STAMP_FIELD: getattr(slot, STAMP_FIELD, "") or "",
        "mode": getattr(slot, "mode", ""),
        "memory_mode": getattr(slot, "memory_mode", "") or "persistent",
        "app": getattr(slot, "_app", "") or "",
        "instance_id": getattr(slot, "instance_id", "") or "",
    }


def live_stamps(
    slots: Mapping[str, Any], parents: Mapping[str, Any] | None = None, *, settled: bool = False
) -> dict[str, tuple[str, bool]]:
    """``{key: (crew_root, crew_mode_root)}`` for every live slot in *slots*.

    The same rule the full slot frame stamps (:func:`stamp_rows`), applied to the
    live slot objects so a ``slot_patch`` frame can restate the two fields when
    a removal or a lineage change moves them; ``crew_root`` is ``""`` on a row
    that is no worker. *parents* is each key's ``parent`` as the frame computed
    it, so an adoption edge counts as the full frame counts it. No I/O.
    """
    rows = [_live_row(s) for s in list(slots.values())]
    for row in rows:
        if parents is not None:
            row["parent"] = parents.get(row["key"])
        else:
            row.pop("parent", None)
    stamp_rows(rows, settled=settled and parents is not None)
    return {
        str(row["key"]): (str(row.get(WORKER_FIELD) or ""), bool(row.get(ROOT_FIELD)))
        for row in rows
    }


def slot_in_crew_tree(
    slots: Mapping[str, Any],
    slot_key: str,
    parents: Mapping[str, Any] | None = None,
    *,
    settled: bool = False,
) -> bool:
    """Whether live slot *slot_key* is a Crew Mode root or one of its workers.

    The same rule the slot list stamps (:func:`stamp_rows` then :func:`in_crew_tree`),
    applied to the live slot objects so a server route can ask it without
    serializing the list. Edges come from ``_created_by``, which session-control
    create stamps on every child, and from *parents* (each key's ``parent`` as the
    slot frame resolves it) when given, so an adoption edge and a closed creator's
    live ancestor count as the full frame counts them. No I/O.
    """
    crew_root, is_root = live_stamps(slots, parents, settled=settled).get(slot_key, ("", False))
    return bool(crew_root or is_root)

"""Tailnet logins granted or revoked while the gateway runs.

``dashboard.tailscale.allowed_logins`` is read once at startup into
:class:`~kiro_crew.dashboard.tailnet.TailnetTrust`. An in-Crew invite that is
accepted, or revoked, has to take effect without a restart, so the invite flow
records the change here as well as in ``config.json`` (which the next start
reads). :func:`kiro_crew.dashboard.tailnet.login_allowed` consults this overlay:

* a revoked login is refused even when the startup allowlist names it;
* a granted login is admitted on top of the startup allowlist.

Only the gateway's own owner-gated invite routes write here, and only with a
login Tailscale's API reported as the accepting user. The overlay never turns
identity trust on: with ``trust_identity`` off no forwarded login is checked at
all, and the invite flow reports that a restart is needed.
"""

from __future__ import annotations

import threading

_LOCK = threading.Lock()
_GRANTED: set[str] = set()
_REVOKED: set[str] = set()


def _norm(login: str) -> str:
    return login.strip().lower()


def grant(login: str) -> None:
    key = _norm(login)
    if not key:
        return
    with _LOCK:
        _REVOKED.discard(key)
        _GRANTED.add(key)


def revoke(login: str) -> None:
    key = _norm(login)
    if not key:
        return
    with _LOCK:
        _GRANTED.discard(key)
        _REVOKED.add(key)


def decision(login: str) -> bool | None:
    """``True`` granted, ``False`` revoked, ``None`` when the overlay says nothing."""
    key = _norm(login)
    with _LOCK:
        if key in _REVOKED:
            return False
        if key in _GRANTED:
            return True
    return None


def reset_for_tests() -> None:
    with _LOCK:
        _GRANTED.clear()
        _REVOKED.clear()

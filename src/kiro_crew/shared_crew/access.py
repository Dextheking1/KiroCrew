"""Who may use which Shared Crew access mode, and what each mode needs.

Three access modes exist and each has its own bar:

* **shared multi-user access** lets several people work in one Crew. It is for
  organization-managed (directory-backed) human identities and tailnet users;
  an individually authenticated consumer account is not eligible;
* **personal remote control** lets one person reach their own machine. Any
  human identity class is eligible, and it stays off until the operator turns
  it on;
* **agent workload identity** lets the Crew act as itself. It needs a cloud
  account setup and a supported hosted container platform.

Shared access and workload identity need a cloud account setup; personal remote
control needs only an individual login. :func:`access_modes_dict` reports the
modes separately, so a client never reads one requirement as the other.

The People surface guides a person who has no shared-access path configured to
one of the setup paths in :data:`SETUP_PATHS`. Each path reports the
availability a source registered for it, and a path with no source reports
``not_in_this_build``. :func:`note_tailnet` is the Tailscale source: the gateway
calls it with the tailnet state it resolved at start.

Every answer here is computed from state already in memory or from the process
environment, so it is safe on the request path.
"""

from __future__ import annotations

import enum
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from kiro_crew.shared_crew.contracts import CapabilityState, Principal, PrincipalKind, Provenance

logger = logging.getLogger(__name__)


# ── identity class ──


class IdentityClass(str, enum.Enum):
    """How a human credential was established, which decides eligibility."""

    #: An organization-managed identity backed by a directory.
    ORGANIZATION_DIRECTORY = "organization_directory"
    #: An individually authenticated consumer account.
    INDIVIDUAL = "individual"
    #: A tailnet user resolved by the Tailscale daemon.
    TAILNET = "tailnet"


#: The key a verified enterprise principal carries its class under, in ``extra``.
IDENTITY_CLASS_KEY = "identity_class"

_TAILNET_PROVENANCE = frozenset({Provenance.TAILSCALE_SERVE, Provenance.TAILSCALE_DIRECT})


def parse_identity_class(raw: object) -> IdentityClass:
    """*raw* as a class; anything unrecognised reads as ``individual``.

    The fallback is the least-privileged class, so a credential that states no
    class, or one this build does not know, is never treated as an
    organization identity.
    """
    try:
        return IdentityClass(raw)
    except ValueError:
        return IdentityClass.INDIVIDUAL


def identity_class_of(principal: Principal) -> IdentityClass:
    """The identity class a principal was established with.

    A tailnet principal is ``tailnet``. An enterprise principal carries the class
    its verifier read from the credential. Every other principal (the local
    operator, a principal with no stated class) reads as ``individual``.
    """
    if principal.provenance in _TAILNET_PROVENANCE:
        return IdentityClass.TAILNET
    if principal.provenance is Provenance.ENTERPRISE:
        return parse_identity_class(principal.extra.get(IDENTITY_CLASS_KEY))
    return IdentityClass.INDIVIDUAL


class EligibilityReason(str, enum.Enum):
    ELIGIBLE = "eligible"
    #: An individual consumer account asked for shared multi-user access.
    INDIVIDUAL_ACCOUNT_NOT_ELIGIBLE = "individual_account_not_eligible"
    #: The principal is not a person.
    NOT_A_PERSON = "not_a_person"


@dataclass(frozen=True)
class Eligibility:
    reason: EligibilityReason
    identity_class: IdentityClass

    @property
    def eligible(self) -> bool:
        return self.reason is EligibilityReason.ELIGIBLE


SHARED_ACCESS_CLASSES = frozenset({IdentityClass.ORGANIZATION_DIRECTORY, IdentityClass.TAILNET})
REMOTE_CONTROL_CLASSES = frozenset(IdentityClass)


def _eligibility(
    principal: Principal, identity_class: Optional[IdentityClass], allowed: frozenset[IdentityClass]
) -> Eligibility:
    cls = identity_class if identity_class is not None else identity_class_of(principal)
    if principal.kind is not PrincipalKind.HUMAN:
        return Eligibility(EligibilityReason.NOT_A_PERSON, cls)
    if cls not in allowed:
        return Eligibility(EligibilityReason.INDIVIDUAL_ACCOUNT_NOT_ELIGIBLE, cls)
    return Eligibility(EligibilityReason.ELIGIBLE, cls)


def shared_access_eligibility(
    principal: Principal, identity_class: Optional[IdentityClass] = None
) -> Eligibility:
    """Whether *principal* may use shared multi-user access."""
    return _eligibility(principal, identity_class, SHARED_ACCESS_CLASSES)


def remote_control_eligibility(
    principal: Principal, identity_class: Optional[IdentityClass] = None
) -> Eligibility:
    """Whether *principal* may remotely control their own machine.

    Every human identity class qualifies, an individual account included.
    """
    return _eligibility(principal, identity_class, REMOTE_CONTROL_CLASSES)


# ── access modes and their requirements ──

MODE_SHARED_MULTI_USER = "shared_multi_user"
MODE_PERSONAL_REMOTE_CONTROL = "personal_remote_control"
MODE_AGENT_WORKLOAD_IDENTITY = "agent_workload_identity"

REQUIRES_CLOUD_ACCOUNT = "cloud_account"
REQUIRES_INDIVIDUAL_LOGIN = "individual_login"

#: Personal remote control is opt-in: off until the operator turns it on.
REMOTE_CONTROL_DEFAULT_ENABLED = False


def access_modes_dict(
    *, shared_state: CapabilityState, workload_state: CapabilityState
) -> dict[str, dict[str, Any]]:
    """The three access modes, each with its own requirement and eligible classes."""
    return {
        MODE_SHARED_MULTI_USER: {
            "state": shared_state.value,
            "requires": REQUIRES_CLOUD_ACCOUNT,
            "identity_classes": sorted(c.value for c in SHARED_ACCESS_CLASSES),
        },
        MODE_PERSONAL_REMOTE_CONTROL: {
            "state": CapabilityState.DISABLED.value,
            "requires": REQUIRES_INDIVIDUAL_LOGIN,
            "identity_classes": sorted(c.value for c in REMOTE_CONTROL_CLASSES),
            "default_enabled": REMOTE_CONTROL_DEFAULT_ENABLED,
        },
        MODE_AGENT_WORKLOAD_IDENTITY: {
            "state": workload_state.value,
            "requires": REQUIRES_CLOUD_ACCOUNT,
        },
    }


# ── workload identity host ──

HOST_EKS = "eks"
HOST_KUBERNETES = "kubernetes"
HOST_ECS = "ecs"
HOST_LOCAL = "local"

#: The hosted container platforms workload identity runs on. No other host, and
#: no static-key fallback, is supported.
SUPPORTED_WORKLOAD_HOSTS: tuple[str, ...] = (HOST_EKS,)

REASON_HOST_NOT_SUPPORTED = "host_not_supported"


def detect_workload_host(env: Optional[Mapping[str, str]] = None) -> str:
    """The container platform this process runs on, from its environment.

    EKS is a Kubernetes pod that was handed a projected workload identity (a web
    identity token file with a role, or the pod identity agent endpoint). A
    Kubernetes pod without one, an ECS task and a plain host are named so a
    caller can say which unsupported host it is on.
    """
    e = os.environ if env is None else env
    if e.get("KUBERNETES_SERVICE_HOST"):
        web_identity = e.get("AWS_WEB_IDENTITY_TOKEN_FILE") and e.get("AWS_ROLE_ARN")
        pod_identity = e.get("AWS_CONTAINER_CREDENTIALS_FULL_URI")
        return HOST_EKS if web_identity or pod_identity else HOST_KUBERNETES
    if e.get("ECS_CONTAINER_METADATA_URI_V4") or e.get("ECS_CONTAINER_METADATA_URI"):
        return HOST_ECS
    return HOST_LOCAL


def workload_host_supported(host: str) -> bool:
    return host in SUPPORTED_WORKLOAD_HOSTS


def workload_host_dict(host: str) -> dict[str, Any]:
    supported = workload_host_supported(host)
    return {
        "supported_hosts": list(SUPPORTED_WORKLOAD_HOSTS),
        "host": host,
        "host_supported": supported,
        "reason": "" if supported else REASON_HOST_NOT_SUPPORTED,
    }


# ── shared-access setup paths ──

SETUP_TAILSCALE = "tailscale"
SETUP_KIRO_LOGIN = "kiro_login"
SETUP_SYSTEMS_MANAGER = "systems_manager"
SETUP_PATHS: tuple[str, ...] = (SETUP_TAILSCALE, SETUP_KIRO_LOGIN, SETUP_SYSTEMS_MANAGER)

REASON_NOT_IN_THIS_BUILD = "not_in_this_build"
REASON_NOT_CONFIGURED = "not_configured"
REASON_TAILNET_UNREACHABLE = "tailnet_unreachable"
REASON_SOURCE_FAILED = "source_failed"


@dataclass(frozen=True)
class SetupPathState:
    state: CapabilityState
    reason: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"state": self.state.value, "reason": self.reason}


SetupPathSource = Callable[[], SetupPathState]

_SOURCES: dict[str, SetupPathSource] = {}
_LOCK = threading.Lock()


def register_setup_path_source(path: str, source: SetupPathSource) -> None:
    """Make *source* the answer for one setup path; a path registers once."""
    if path not in SETUP_PATHS:
        raise ValueError(f"unknown setup path {path!r}")
    with _LOCK:
        if path in _SOURCES:
            raise ValueError(f"setup path {path!r} already has a source")
        _SOURCES[path] = source


def unregister_setup_path_source(path: str) -> None:
    with _LOCK:
        _SOURCES.pop(path, None)


def _reset_setup_paths_for_tests() -> None:
    with _LOCK:
        _SOURCES.clear()


def _tailnet_state(enabled: bool, host: str) -> SetupPathState:
    if not enabled:
        return SetupPathState(CapabilityState.UNAVAILABLE, REASON_NOT_CONFIGURED)
    if not host:
        return SetupPathState(CapabilityState.DEGRADED, REASON_TAILNET_UNREACHABLE)
    return SetupPathState(CapabilityState.AVAILABLE)


def note_tailnet(*, enabled: bool, host: str) -> None:
    """Record the tailnet path the gateway resolved at start.

    ``available`` once Tailscale access is on and this machine's tailnet name
    resolved; ``degraded`` when it is on but no name resolved.
    """
    answer = _tailnet_state(bool(enabled), str(host or ""))
    with _LOCK:
        _SOURCES[SETUP_TAILSCALE] = lambda: answer


def setup_paths_dict() -> dict[str, dict[str, str]]:
    """Each setup path's availability, ``not_in_this_build`` when no source speaks."""
    with _LOCK:
        sources = dict(_SOURCES)
    out: dict[str, dict[str, str]] = {}
    for path in SETUP_PATHS:
        source = sources.get(path)
        if source is None:
            reason = REASON_NOT_CONFIGURED if path == SETUP_TAILSCALE else REASON_NOT_IN_THIS_BUILD
            out[path] = SetupPathState(CapabilityState.UNAVAILABLE, reason).to_dict()
            continue
        try:
            answer = source()
            if not isinstance(answer, SetupPathState):
                raise TypeError("setup path source returned a non-state")
        except Exception:  # noqa: BLE001 -- a failed source claims nothing
            logger.warning("setup path source %s failed", path, exc_info=True)
            answer = SetupPathState(CapabilityState.UNAVAILABLE, REASON_SOURCE_FAILED)
        out[path] = answer.to_dict()
    return out


__all__ = [
    "IDENTITY_CLASS_KEY",
    "REMOTE_CONTROL_DEFAULT_ENABLED",
    "SETUP_PATHS",
    "SUPPORTED_WORKLOAD_HOSTS",
    "Eligibility",
    "EligibilityReason",
    "IdentityClass",
    "SetupPathState",
    "access_modes_dict",
    "detect_workload_host",
    "identity_class_of",
    "note_tailnet",
    "parse_identity_class",
    "register_setup_path_source",
    "remote_control_eligibility",
    "setup_paths_dict",
    "shared_access_eligibility",
    "unregister_setup_path_source",
    "workload_host_dict",
    "workload_host_supported",
]

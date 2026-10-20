"""The execution-state fence invariant: every loop-thread write of a
rollback-restored field bumps the revision.

``CronService._enable_job_locked`` (pause/resume) runs on a worker thread and,
on a save failure, rolls its in-memory change back. It can only roll back
CORRECTLY against a concurrent run on the loop thread because of the
execution-state revision fence (``CronJob.exec_state_revision``): on the
unchanged-revision branch it restores the auto-pause fields and the pre-call
``enabled`` EXACTLY, trusting the revision to mean "no run touched these fields".
That is sound ONLY IF every loop-thread write of one of those four fields --
``enabled`` / ``auto_paused`` / ``user_paused`` / ``consecutive_failures`` --
bumps the revision. A write the fence cannot see (a one-shot park's bare
``enabled = False``) would be clobbered by the restore and, e.g., a completed
one-shot resurrected.

So the invariant: a cron module that mutates the LIVE job on the loop thread
NEVER assigns one of those four fields raw -- it goes through a ``CronJob``
helper that writes-and-bumps under ``exec_state_lock`` (``record_failure`` /
``record_success`` / ``hold_off_schedule`` / ``auto_pause_now`` /
``set_enabled``).

This test DISCOVERS every cron module (``cron.py``, ``cron_service/*.py`` and
``cron_service/service/*.py``) that assigns a fenced field raw, rather than
listing the lifecycle modules by hand -- so a FOURTH raw writer added anywhere
cannot slip past by living in a module the test forgot to name. A module that
turns up with a raw write must either route it through a helper or be added to
``_SANCTIONED`` below WITH a reason; the sanctioned set is the small, explicit
list of modules that legitimately write these fields raw and do NOT race the
worker-thread rollback.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Resolve from THIS file, never a relative cwd path -- xdist workers may chdir.
_CRON_ROOT = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

#: The fields ``_enable_job_locked``'s rollback restores.
_FENCED_FIELDS = frozenset({"enabled", "auto_paused", "user_paused", "consecutive_failures"})

#: Modules that may write a fenced field RAW, each with why it does not race the
#: worker-thread rollback. Every other cron module must route through a helper.
#: Paths are relative to ``src/kiro_crew``.
_SANCTIONED: dict[str, str] = {
    # The rollback itself: _enable_job_locked writes these fields raw under its
    # own exec_state_lock, by construction -- it IS the code the fence serves.
    "cron.py": "_enable_job_locked is the fence's own rollback/mutation",
    # Terminal merges write a STORE COPY under the store lock, which serialises
    # against _enable_job_locked's own save -- they never touch the live object
    # concurrently with the rollback.
    "cron_service/service/run_merge.py": "terminal merge on a store copy under the store lock",
    "cron_service/execution.py": "apply_run_record on a store copy under the store lock",
    # Runs once at CronService.start(), before the timer arms -- no concurrent
    # user enable_job, so no rollback to race.
    "cron_service/service/stall_breaker.py": "runs at startup before the timer arms",
}

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_cron_exec_state_invariant")


def _raw_fenced_writes(source: str) -> list[tuple[int, str]]:
    """(lineno, field) for every raw assignment to a fenced field.

    Covers ``x.enabled = ...``, the tuple form ``x.a, x.b = ...``, augmented
    assignment (``x.consecutive_failures += 1``) and annotated assignment.
    """
    tree = ast.parse(source)
    hits: list[tuple[int, str]] = []

    def _check(target: ast.expr) -> None:
        if isinstance(target, ast.Attribute) and target.attr in _FENCED_FIELDS:
            hits.append((target.lineno, target.attr))
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                _check(elt)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                _check(tgt)
        elif isinstance(node, ast.AugAssign):
            _check(node.target)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            _check(node.target)
    return hits


def _cron_modules() -> list[Path]:
    """Every cron source module: cron.py + cron_service/**.py (not model.py,
    which DEFINES the helpers and legitimately assigns the fields inside them)."""
    mods = [_CRON_ROOT / "cron.py"]
    svc = _CRON_ROOT / "cron_service"
    mods += [p for p in sorted(svc.rglob("*.py")) if p.name not in ("model.py", "__init__.py")]
    return [p for p in mods if p.is_file()]


def test_no_unsanctioned_cron_module_writes_a_fenced_field_raw() -> None:
    offenders: dict[str, list[tuple[int, str]]] = {}
    for path in _cron_modules():
        rel = path.relative_to(_CRON_ROOT).as_posix()
        raw = _raw_fenced_writes(path.read_text(encoding="utf-8"))
        if raw and rel not in _SANCTIONED:
            offenders[rel] = raw
    summary = {m: [f"line {ln}: .{f}" for ln, f in hits] for m, hits in offenders.items()}
    assert not offenders, (
        "a cron module assigns a fence-guarded field (enabled/auto_paused/"
        f"user_paused/consecutive_failures) raw: {summary}. "
        "Every loop-thread write MUST go through a CronJob helper that bumps "
        "exec_state_revision under exec_state_lock (record_failure/record_success/"
        "hold_off_schedule/auto_pause_now/set_enabled), or _enable_job_locked's "
        "rollback silently clobbers it. If the module genuinely does not race the "
        "worker-thread rollback (a store-copy merge under the lock, a startup-only "
        "path), add it to _SANCTIONED with the reason."
    )


def test_the_sanctioned_list_is_not_stale() -> None:
    """Every sanctioned module must still exist AND still contain a raw write --
    otherwise the exemption is dead and should be pruned so the list stays honest."""
    for rel in _SANCTIONED:
        path = _CRON_ROOT / rel
        assert path.is_file(), f"sanctioned module {rel} no longer exists; prune _SANCTIONED"
        assert _raw_fenced_writes(path.read_text(encoding="utf-8")), (
            f"sanctioned module {rel} no longer writes a fenced field raw; "
            "prune it from _SANCTIONED so the exemption cannot hide a future regression"
        )


def test_the_scanner_detects_a_planted_raw_write() -> None:
    """The scanner is not vacuous: a planted raw write to each fenced field is
    found, in the simple, tuple-target and augmented forms."""
    for field in sorted(_FENCED_FIELDS):
        assert _raw_fenced_writes(f"def f(j):\n    j.{field} = False\n") == [(2, field)], field
    assert _raw_fenced_writes("def f(j):\n    j.enabled, j.auto_paused = False, True\n") == [
        (2, "enabled"),
        (2, "auto_paused"),
    ]
    assert _raw_fenced_writes("def f(j):\n    j.consecutive_failures += 1\n") == [
        (2, "consecutive_failures")
    ]

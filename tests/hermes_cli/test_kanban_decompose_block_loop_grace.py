"""Regression for t_e3517120/t_59786b01 (2026-11-18): the auto-decomposer
raced an in-flight human review reply.

A card correctly blocked twice on ``needs_input`` (waiting on a named human
reviewer each time, with real work and a real reply in between) hits the
unblock-loop breaker's ``BLOCK_RECURRENCE_LIMIT`` and is escalated straight
to ``triage`` — by design, since the breaker only compares ``block_kind``,
not whether the two blocks were for the same still-unresolved cause. One
minute later the reviewer's PASS landed and the PR was merged, but the
auto-decomposer had already fanned the card into 4 children on the very
next tick, two of which started real duplicate work on an already-merged
route before a human caught the race.

``kanban_decompose.list_triage_ids(min_triage_age_seconds=...)`` is the fix:
the gateway's auto-decompose tick now withholds a triage card from its
candidate list while it is still inside its grace window after a
``block_loop_detected`` event, giving an in-flight reply time to resolve the
card on its own. Manual ``hermes kanban decompose`` / ``--all`` always pass
``min_triage_age_seconds=0`` (unaffected, immediate by design) and a card
that reached ``triage`` any other way (no ``block_loop_detected`` event) is
never held back regardless of the setting.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decomp


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t"):
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    return tid


def _make_running_again(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


def _escalate_to_triage_via_block_loop(conn, tid, *, kind="needs_input"):
    """Drive a running task through two same-kind blocks so the breaker fires."""
    kb.block_task(conn, tid, reason="waiting on reviewer (round 1)", kind=kind)
    kb.unblock_task(conn, tid)
    _make_running_again(conn, tid)
    kb.block_task(conn, tid, reason="waiting on reviewer (round 2)", kind=kind)
    task = kb.get_task(conn, tid)
    assert task is not None and task.status == "triage", (
        f"expected the breaker to land the card in triage, got {task and task.status!r}"
    )


def test_fresh_block_loop_card_excluded_within_grace_window(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        _escalate_to_triage_via_block_loop(conn, tid)

        # Still in triage, confirmed plain list_triage_ids sees it (no grace = old behaviour).
        assert tid in decomp.list_triage_ids()
        # A 300s grace window withholds it: the event just fired, (now - created_at) < 300.
        assert tid not in decomp.list_triage_ids(min_triage_age_seconds=300)


def test_aged_block_loop_card_is_eligible_once_grace_elapses(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        _escalate_to_triage_via_block_loop(conn, tid)

        # Backdate the block_loop_detected event past the grace window — simulates
        # time having passed with no human reply landing yet.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET created_at = created_at - 400 "
                "WHERE task_id = ? AND kind = 'block_loop_detected'",
                (tid,),
            )
        assert tid in decomp.list_triage_ids(min_triage_age_seconds=300)


def test_manually_triaged_card_is_never_gated_by_grace(kanban_home: Path) -> None:
    """A card with no block_loop_detected event (a plain user-dropped idea,
    or an operator-run ``hermes kanban triage``) must decompose immediately
    regardless of the grace setting — the grace window only protects cards
    the loop breaker itself escalated."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="rough idea", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        assert tid in decomp.list_triage_ids(min_triage_age_seconds=300)
        assert tid in decomp.list_triage_ids(min_triage_age_seconds=10_000)


def test_zero_grace_matches_pre_fix_behaviour(kanban_home: Path) -> None:
    """``min_triage_age_seconds=0`` (the default, and what manual `decompose
    --all` always passes) must decompose the instant a card lands in triage,
    exactly like before this fix existed."""
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        _escalate_to_triage_via_block_loop(conn, tid)
        assert tid in decomp.list_triage_ids(min_triage_age_seconds=0)
        assert tid in decomp.list_triage_ids()

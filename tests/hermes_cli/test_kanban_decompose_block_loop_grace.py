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


def test_comment_after_block_event_pushes_grace_window_forward(kanban_home: Path) -> None:
    """Regression for tony-lean's changes-requested round on t_e428aaf3: a flat
    window measured only from the ``block_loop_detected`` event still races a
    reviewer thread that keeps talking after the block fires (t_e3517120's own
    timeline had a PASS at T+104s, then the merge-exec reply that actually
    resolved things at T+415s -- a single fixed budget from the block alone
    can't cover that). A comment posted on the card AFTER the block event must
    reset the grace window to measure from the comment, not the original block.
    """
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        _escalate_to_triage_via_block_loop(conn, tid)

        # Backdate the block event past the grace window -- on its own this
        # would already make the card eligible (same as
        # test_aged_block_loop_card_is_eligible_once_grace_elapses).
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET created_at = created_at - 400 "
                "WHERE task_id = ? AND kind = 'block_loop_detected'",
                (tid,),
            )
        assert tid in decomp.list_triage_ids(min_triage_age_seconds=300)

        # A reviewer comment lands just now -- well AFTER the (backdated)
        # block event -- which resets the window to measure from the comment.
        kb.add_comment(conn, tid, "tony-lean", "still reviewing, one sec")
        assert tid not in decomp.list_triage_ids(min_triage_age_seconds=300)


def test_comment_before_block_event_does_not_reset_window(kanban_home: Path) -> None:
    """A comment that predates the ``block_loop_detected`` event (e.g. the
    reason text on the block itself, or stale chatter from round 1) must not
    count as in-flight activity -- only a reply posted AFTER the block fired
    proves someone is actively working the card right now."""
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        _escalate_to_triage_via_block_loop(conn, tid)
        kb.add_comment(conn, tid, "someone", "earlier chatter, before any block")

        # Push the comment well before the block event, then push the block
        # event itself past the grace window -- the stale comment must not
        # resurrect a window that has otherwise expired.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_comments SET created_at = created_at - 1000 WHERE task_id = ?",
                (tid,),
            )
            conn.execute(
                "UPDATE task_events SET created_at = created_at - 400 "
                "WHERE task_id = ? AND kind = 'block_loop_detected'",
                (tid,),
            )
        assert tid in decomp.list_triage_ids(min_triage_age_seconds=300)

"""Tests for live auto-decompose settings resolution (issue #49638).

The gateway dispatcher used to capture ``kanban.auto_decompose`` once at boot,
so a user who flipped it to ``false`` to STOP runaway auto-decompose (which had
created and launched tasks they didn't intend) found the flag had no effect
without a full gateway restart. ``_resolve_auto_decompose_settings`` is now
called every tick, reading the current config.
"""

from __future__ import annotations


from gateway.kanban_watchers_common import (
    _resolve_auto_decompose_settings,
    _resolve_auto_decompose_block_loop_grace_seconds,
)




def test_disabled_when_flag_false():
    enabled, _per_tick = _resolve_auto_decompose_settings(
        lambda: {"kanban": {"auto_decompose": False}}
    )
    assert enabled is False


def test_block_loop_grace_default_is_900():
    """No explicit config -> the fail-safe default (900s), not 0 (no grace),
    so a fresh install is protected from the t_e3517120/t_59786b01 race.
    900s (not the original 300s) because the merge-exec reply that actually
    resolved that incident landed at T+415s -- 300s would still expire first."""
    assert _resolve_auto_decompose_block_loop_grace_seconds(dict) == 900


def test_block_loop_grace_reads_configured_value():
    grace = _resolve_auto_decompose_block_loop_grace_seconds(
        lambda: {"kanban": {"auto_decompose_block_loop_grace_seconds": 60}}
    )
    assert grace == 60


def test_block_loop_grace_zero_means_opt_out():
    """A user may explicitly set 0 to restore pre-fix immediate-decompose
    behaviour; that must not be silently promoted back to the default."""
    grace = _resolve_auto_decompose_block_loop_grace_seconds(
        lambda: {"kanban": {"auto_decompose_block_loop_grace_seconds": 0}}
    )
    assert grace == 0


def test_block_loop_grace_fails_safe_on_config_error():
    def _boom():
        raise RuntimeError("config read failed")

    assert _resolve_auto_decompose_block_loop_grace_seconds(_boom) == 900



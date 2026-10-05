from strategy_types import PositionState
from position_reconcile import (
    BrokerSnapshot,
    reconcile_at_start,
    state_to_save,
)


def test_reconcile_keeps_memory_when_broker_matches():
    memory = PositionState(active_symbol="TQQQ", entry_price=100.0, entry_timestamp="2026-09-01T00:00:00Z")
    rec = reconcile_at_start(memory, BrokerSnapshot(tqqq_qty=3))
    assert rec.block_new_orders is False
    assert rec.position_for_decide is memory
    assert rec.position_for_decide.entry_price == 100.0


def test_reconcile_adopts_broker_position_memory_missed():
    rec = reconcile_at_start(PositionState(), BrokerSnapshot(sqqq_qty=1.5))
    assert rec.block_new_orders is False
    assert rec.position_for_decide.active_symbol == "SQQQ"
    assert rec.position_for_decide.entry_price is None


def test_reconcile_drops_phantom_memory_when_broker_is_flat():
    memory = PositionState(active_symbol="TQQQ", entry_price=90.0)
    rec = reconcile_at_start(memory, BrokerSnapshot())
    assert rec.block_new_orders is False
    assert rec.position_for_decide.active_symbol is None


def test_reconcile_blocks_when_broker_holds_both():
    memory = PositionState(active_symbol="TQQQ", entry_price=90.0)
    rec = reconcile_at_start(memory, BrokerSnapshot(tqqq_qty=1, sqqq_qty=1))
    assert rec.block_new_orders is True
    assert rec.position_for_decide is memory


def test_reconcile_blocks_while_an_order_is_open():
    memory = PositionState()
    rec = reconcile_at_start(memory, BrokerSnapshot(open_order_count=1))
    assert rec.block_new_orders is True
    assert rec.position_for_decide is memory


def test_reconcile_memory_tqqq_when_broker_holds_sqqq():
    memory = PositionState(active_symbol="TQQQ", entry_price=80.0)
    rec = reconcile_at_start(memory, BrokerSnapshot(sqqq_qty=2))
    assert rec.block_new_orders is False
    assert rec.position_for_decide.active_symbol == "SQQQ"
    assert rec.position_for_decide.entry_price is None


def test_state_to_save_rejects_partial_flip_memory():
    proposed = PositionState(active_symbol="SQQQ")
    assert state_to_save(proposed, BrokerSnapshot(tqqq_qty=1)) is None
    assert state_to_save(proposed, BrokerSnapshot(open_order_count=2)) is None


def test_state_to_save_requires_a_broker_match():
    proposed = PositionState(active_symbol="TQQQ", entry_price=110.0)
    assert state_to_save(proposed, BrokerSnapshot()) is None
    assert state_to_save(proposed, BrokerSnapshot(tqqq_qty=2)) is proposed
    assert state_to_save(PositionState(), BrokerSnapshot()) is not None
    assert state_to_save(PositionState(), BrokerSnapshot()).active_symbol is None
    assert state_to_save(proposed, BrokerSnapshot(tqqq_qty=1, sqqq_qty=1)) is None

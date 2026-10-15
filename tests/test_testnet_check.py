"""Testnet connection check: key permissions and round-trip size."""

from __future__ import annotations

from decimal import Decimal

from src.execution.plan import InstrumentSpec
from src.live.testnet_check import min_qty_for, permissions_problem

D = Decimal


def test_withdrawal_permission_is_refused():
    info = {"readOnly": "0", "permissions": {"Wallet": ["AccountTransfer", "Withdraw"]}}
    assert "WITHDRAWAL" in permissions_problem(info)


def test_read_only_key_is_refused():
    assert "read-only" in permissions_problem({"readOnly": "1", "permissions": {}})


def test_trading_key_without_withdrawal_passes():
    info = {"readOnly": "0", "permissions": {"ContractTrade": ["Order", "Position"], "Wallet": []}}
    assert permissions_problem(info) is None


def test_roundtrip_size_clears_both_exchange_minimums():
    spec = InstrumentSpec("XRPUSDT", D("0.0001"), D("1"), D("1"), D("100000"), D("5"))
    qty = min_qty_for(spec, D("0.6"))
    assert qty * D("0.6") >= D("5") * D("1.1") and qty % 1 == 0
    assert min_qty_for(spec, D("100")) == 1

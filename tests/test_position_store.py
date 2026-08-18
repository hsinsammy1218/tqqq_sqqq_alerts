from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from config import ConfigError
from position_store import (
    FilePositionStore,
    PositionStoreError,
    SupabasePositionStore,
    parse_supabase_position_payload,
    position_store_from_settings,
)
from strategy_position import position_state_from_dict, position_state_to_dict
from strategy_types import PositionState


def test_position_state_roundtrip_dict():
    original = PositionState(
        active_symbol="TQQQ",
        entry_price=72.5,
        entry_timestamp="2026-05-01T14:30:00Z",
        last_signal="BUY",
        updated_at="2026-05-01T16:30:00Z",
    )
    state, warnings = position_state_from_dict(position_state_to_dict(original))
    assert warnings == []
    assert state == original


def test_file_position_store_roundtrip(tmp_path: Path):
    path = tmp_path / "position_state.json"
    store = FilePositionStore(path)
    assert store.describe() == str(path)
    loaded, warnings = store.load()
    assert warnings == []
    assert loaded.active_symbol is None

    store.save(
        PositionState(
            active_symbol="SQQQ",
            entry_price=14.2,
            entry_timestamp="2026-05-02T15:00:00Z",
            last_signal="MANUAL_SET",
            updated_at="2026-05-02T15:00:00Z",
        )
    )
    loaded, warnings = store.load()
    assert warnings == []
    assert loaded.active_symbol == "SQQQ"
    assert loaded.entry_price == 14.2


def test_file_position_store_save_raises_on_oserror(tmp_path: Path):
    path = tmp_path / "not_a_file"
    path.mkdir()
    with pytest.raises(PositionStoreError, match="Failed to save"):
        FilePositionStore(path).save(PositionState(last_signal="MANUAL_FLAT"))


def test_parse_supabase_empty_list_starts_flat():
    state, warnings = parse_supabase_position_payload([])
    assert warnings == []
    assert state.active_symbol is None


def test_parse_supabase_none_raises():
    with pytest.raises(PositionStoreError, match="no data payload"):
        parse_supabase_position_payload(None)


def test_parse_supabase_non_list_raises():
    with pytest.raises(PositionStoreError, match="unexpected payload type"):
        parse_supabase_position_payload({"symbol": "TQQQ"})


def test_parse_supabase_non_dict_row_raises():
    with pytest.raises(PositionStoreError, match="was not an object"):
        parse_supabase_position_payload(["TQQQ"])


def test_parse_supabase_invalid_symbol_raises():
    with pytest.raises(PositionStoreError, match="Refusing to start flat"):
        parse_supabase_position_payload([{"symbol": "QQQ", "entry_price": 1.0}])


def test_parse_supabase_invalid_entry_price_is_warning():
    state, warnings = parse_supabase_position_payload([{"symbol": "TQQQ", "entry_price": "nope"}])
    assert state.active_symbol == "TQQQ"
    assert state.entry_price is None
    assert any("entry_price" in w for w in warnings)


def test_parse_supabase_row_ok():
    state, warnings = parse_supabase_position_payload(
        [
            {
                "symbol": "TQQQ",
                "entry_price": 70.0,
                "entry_time": "2026-05-01T14:30:00Z",
                "last_signal": "BUY",
                "updated_at": "2026-05-01T16:30:00Z",
            }
        ]
    )
    assert warnings == []
    assert state.active_symbol == "TQQQ"
    assert state.entry_price == 70.0


def test_supabase_store_load_empty(monkeypatch: pytest.MonkeyPatch):
    class _Result:
        data = []

    class _Query:
        def select(self, *_args, **_kwargs):
            return self

        def eq(self, *_args, **_kwargs):
            return self

        def limit(self, *_args, **_kwargs):
            return self

        def execute(self):
            return _Result()

    class _Client:
        def table(self, name: str):
            assert name == "bot_position_state"
            return _Query()

    monkeypatch.setattr("position_store.create_supabase_client", lambda: _Client())
    store = SupabasePositionStore("default")
    state, warnings = store.load()
    assert warnings == []
    assert state.active_symbol is None
    assert store.describe() == "supabase:bot_position_state:default"


def test_supabase_store_load_row(monkeypatch: pytest.MonkeyPatch):
    class _Result:
        data = [
            {
                "symbol": "TQQQ",
                "entry_price": 70.0,
                "entry_time": "2026-05-01T14:30:00Z",
                "last_signal": "BUY",
                "updated_at": "2026-05-01T16:30:00Z",
            }
        ]

    class _Query:
        def select(self, *_args, **_kwargs):
            return self

        def eq(self, *_args, **_kwargs):
            return self

        def limit(self, *_args, **_kwargs):
            return self

        def execute(self):
            return _Result()

    class _Client:
        def table(self, _name: str):
            return _Query()

    monkeypatch.setattr("position_store.create_supabase_client", lambda: _Client())
    state, warnings = SupabasePositionStore("default").load()
    assert warnings == []
    assert state.active_symbol == "TQQQ"
    assert state.entry_price == 70.0


def test_supabase_store_load_raises_on_api_error(monkeypatch: pytest.MonkeyPatch):
    class _Query:
        def select(self, *_args, **_kwargs):
            return self

        def eq(self, *_args, **_kwargs):
            return self

        def limit(self, *_args, **_kwargs):
            return self

        def execute(self):
            raise RuntimeError("network down")

    class _Client:
        def table(self, _name: str):
            return _Query()

    monkeypatch.setattr("position_store.create_supabase_client", lambda: _Client())
    with pytest.raises(PositionStoreError, match="Failed to load"):
        SupabasePositionStore("default").load()


def test_supabase_store_save_upsert(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, object] = {}

    class _Query:
        def upsert(self, row, on_conflict=None):
            captured["row"] = row
            captured["on_conflict"] = on_conflict
            return self

        def execute(self):
            return SimpleNamespace(data=[captured["row"]])

    class _Client:
        def table(self, name: str):
            captured["table"] = name
            return _Query()

    monkeypatch.setattr("position_store.create_supabase_client", lambda: _Client())
    SupabasePositionStore("prod").save(
        PositionState(
            active_symbol=None,
            entry_price=None,
            entry_timestamp=None,
            last_signal="MANUAL_FLAT",
            updated_at="2026-07-14T18:00:00Z",
        )
    )
    assert captured["table"] == "bot_position_state"
    assert captured["on_conflict"] == "bot_id"
    assert captured["row"] == {
        "bot_id": "prod",
        "symbol": None,
        "entry_price": None,
        "entry_time": None,
        "last_signal": "MANUAL_FLAT",
        "updated_at": "2026-07-14T18:00:00Z",
    }


def test_supabase_store_save_raises_on_api_error(monkeypatch: pytest.MonkeyPatch):
    class _Query:
        def upsert(self, *_args, **_kwargs):
            return self

        def execute(self):
            raise RuntimeError("write failed")

    class _Client:
        def table(self, _name: str):
            return _Query()

    monkeypatch.setattr("position_store.create_supabase_client", lambda: _Client())
    with pytest.raises(PositionStoreError, match="Failed to save"):
        SupabasePositionStore("default").save(PositionState(last_signal="BUY"))


def test_position_store_from_settings_rejects_unknown_backend():
    settings = SimpleNamespace(
        position_state_backend="s3",
        position_state_json=Path("position_state.json"),
        position_state_bot_id="default",
    )
    with pytest.raises(ConfigError, match="Unsupported POSITION_STATE_BACKEND"):
        position_store_from_settings(settings)  # type: ignore[arg-type]

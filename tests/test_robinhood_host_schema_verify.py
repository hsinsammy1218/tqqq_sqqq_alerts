"""Deterministic checks for Phase 5R.2 read-only RH schema verify SQL."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "scripts" / "sql" / "verify_robinhood_host_schema.sql"
MIGRATIONS = ROOT / "dashboard" / "supabase" / "migrations"

RH_TABLES = (
    "bot_robinhood_execution_intents",
    "bot_robinhood_entry_reservations",
    "bot_robinhood_equity_baselines",
)

APPLY_ORDER = (
    "20261007160000_bot_robinhood_execution_intents.sql",
    "20261007160100_bot_robinhood_entry_reservations.sql",
    "20261007170000_bot_robinhood_equity_baselines.sql",
    "20261007170100_bot_robinhood_rls_policies.sql",
    "20261007220000_bot_robinhood_rls_drop_authenticated.sql",
)

DDL_FORBIDDEN = re.compile(
    r"\b(create|alter|drop|truncate|insert|update|delete|grant|revoke|copy)\b",
    flags=re.IGNORECASE,
)


def _read(path: Path) -> str:
    assert path.is_file(), f"missing {path}"
    return path.read_text(encoding="utf-8")


def test_verify_script_exists_and_is_read_only():
    sql = _read(VERIFY)
    assert "bot_robinhood_execution_intents" in sql
    assert "APPLIED_AND_VERIFIED" in sql
    assert "MISSING" in sql
    # Strip SQL comments before scanning for DDL/DML verbs.
    stripped = re.sub(r"--[^\n]*", "", sql)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)
    # Allow "create" only inside explanatory comment strings already stripped;
    # remaining body must be SELECT / WITH only.
    statements = [s.strip() for s in stripped.split(";") if s.strip()]
    assert statements, "verify script has no statements"
    for stmt in statements:
        head = stmt.lstrip()[:12].lower()
        assert head.startswith("select") or head.startswith("with"), stmt[:80]
        # No mutating keywords as statement start.
        assert not re.match(
            r"^(create|alter|drop|truncate|insert|update|delete|grant|revoke)\b",
            stmt,
            flags=re.IGNORECASE,
        )


def test_verify_script_covers_all_rh_tables_and_security_checks():
    sql = _read(VERIFY).lower()
    for table in RH_TABLES:
        assert table in sql
    assert "service_role" in sql
    assert "authenticated" in sql
    assert "anon" in sql
    assert "relrowsecurity" in sql or "rls_enabled" in sql
    assert "pg_policies" in sql
    assert "using(true)" in sql.replace(" ", "") or "using (true)" in sql


def test_rh_migration_apply_order_files_exist_and_are_safe():
    """Migrations must be create-if-not-exists / drop-if-exists; no DROP TABLE."""
    for name in APPLY_ORDER:
        path = MIGRATIONS / name
        assert path.is_file(), name
        sql = _read(path).lower()
        assert "drop table" not in sql
        assert "truncate" not in sql
        if "rls" in name or "drop_authenticated" in name:
            assert "drop policy if exists" in sql
        else:
            assert "create table if not exists" in sql
            assert "enable row level security" in sql


def test_verify_script_documents_sql_editor_usage():
    sql = _read(VERIFY)
    assert "SQL Editor" in sql
    assert "Do NOT run migration DDL" in sql or "No DDL" in sql

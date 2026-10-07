"""Static security assertions for Robinhood host-mediated Supabase RLS.

Trading-control tables must be service_role backend only:
anon NO · authenticated NO · service_role AUTHORITY.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "dashboard" / "supabase" / "migrations"
RH_RLS = MIGRATIONS / "20261007170100_bot_robinhood_rls_policies.sql"
RH_RLS_CORRECTIVE = MIGRATIONS / "20261007220000_bot_robinhood_rls_drop_authenticated.sql"
RH_TABLES = (
    "bot_robinhood_execution_intents",
    "bot_robinhood_entry_reservations",
    "bot_robinhood_equity_baselines",
)
AUTHENTICATED_POLICY_NAMES = (
    "bot_rh_exec_intents_authenticated_all",
    "bot_rh_entry_reservations_authenticated_all",
    "bot_rh_equity_baselines_authenticated_all",
)
SERVICE_ROLE_POLICY_NAMES = (
    "bot_rh_exec_intents_service_role_all",
    "bot_rh_entry_reservations_service_role_all",
    "bot_rh_equity_baselines_service_role_all",
)


def _read(path: Path) -> str:
    assert path.is_file(), f"missing migration {path}"
    return path.read_text(encoding="utf-8")


def test_rh_rls_migration_has_no_authenticated_grant():
    sql = _read(RH_RLS)
    sql_l = re.sub(r"\s+", " ", sql.lower())
    # Must not CREATE a policy for authenticated (drop-if-exists is required).
    assert re.search(
        r"create\s+policy\s+\w+\s+.*?to\s+authenticated",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    ) is None
    for name in AUTHENTICATED_POLICY_NAMES:
        assert f"drop policy if exists {name}" in sql_l
        assert f"create policy {name}" not in sql_l


def test_rh_rls_migration_service_role_only_authority():
    sql = _read(RH_RLS).lower()
    for name in SERVICE_ROLE_POLICY_NAMES:
        assert f"create policy {name}" in sql
        assert "to service_role" in sql
    for table in RH_TABLES:
        assert table in sql
    assert "anon" in sql  # documented deny
    assert "authenticated" in sql  # documented deny / drop
    assert "no access" in sql or "deny" in sql


def test_corrective_migration_drops_authenticated_policies():
    sql = _read(RH_RLS_CORRECTIVE).lower()
    for name in AUTHENTICATED_POLICY_NAMES:
        assert f"drop policy if exists {name}" in sql.replace("\n", " ")
    assert "create policy" not in sql


def test_rh_table_migrations_enable_rls_and_store_no_oauth():
    """Schema must not define token/secret columns (comments may mention OAuth)."""
    intent_sql = _read(MIGRATIONS / "20261007160000_bot_robinhood_execution_intents.sql")
    entry_sql = _read(MIGRATIONS / "20261007160100_bot_robinhood_entry_reservations.sql")
    equity_sql = _read(MIGRATIONS / "20261007170000_bot_robinhood_equity_baselines.sql")
    col_forbidden = re.compile(
        r"^\s*(access_token|refresh_token|oauth_token|bearer|authorization|client_secret)\b",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    for sql, table in (
        (intent_sql, "bot_robinhood_execution_intents"),
        (entry_sql, "bot_robinhood_entry_reservations"),
        (equity_sql, "bot_robinhood_equity_baselines"),
    ):
        assert "enable row level security" in sql.lower()
        assert table in sql
        assert col_forbidden.search(sql) is None, f"token column on {table}"


def test_no_next_public_service_role_env():
    """Frontend must never ship service_role via NEXT_PUBLIC_*."""
    hits: list[str] = []
    for path in (ROOT / "dashboard").rglob("*"):
        if not path.is_file():
            continue
        if "node_modules" in path.parts or ".next" in path.parts:
            continue
        if path.suffix not in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".env", ".example"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "NEXT_PUBLIC_SUPABASE_SERVICE_ROLE" in text:
            hits.append(str(path.relative_to(ROOT)))
    assert hits == [], f"SECRET EXPOSURE DETECTED: {hits}"


def test_service_role_client_is_server_only_import_surface():
    """createServiceClient must not be imported from 'use client' modules."""
    client_imports: list[str] = []
    for path in (ROOT / "dashboard").rglob("*.tsx"):
        if "node_modules" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "createServiceClient" not in text:
            continue
        # Client components mark 'use client' near the top.
        head = "\n".join(text.splitlines()[:30])
        if re.search(r"""['"]use client['"]""", head):
            client_imports.append(str(path.relative_to(ROOT)))
    for path in (ROOT / "dashboard").rglob("*.ts"):
        if "node_modules" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "createServiceClient" not in text:
            continue
        head = "\n".join(text.splitlines()[:30])
        if re.search(r"""['"]use client['"]""", head):
            client_imports.append(str(path.relative_to(ROOT)))
    assert client_imports == [], f"SECRET EXPOSURE DETECTED: {client_imports}"


def test_no_authenticated_true_true_on_rh_tables_anywhere_in_migrations():
    pattern = re.compile(
        r"create\s+policy\s+\S+\s+on\s+public\.(bot_robinhood_\w+).*?"
        r"to\s+authenticated\s+using\s*\(\s*true\s*\)\s*with\s+check\s*\(\s*true\s*\)",
        flags=re.IGNORECASE | re.DOTALL,
    )
    offenders: list[str] = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            offenders.append(f"{path.name}:{match.group(1)}")
    assert offenders == [], f"authenticated true/true on RH tables: {offenders}"

#!/usr/bin/env python3
"""Phase 5R.4 — ephemeral PostgreSQL probe for RH migrations (not mocks).

Applies concurrency / uniqueness / RLS checks against a live Postgres that has
the exact bot_robinhood_* migrations applied. Exit 0 on PASS.
"""

from __future__ import annotations

import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import psycopg

DSN = os.environ.get(
    "RH_AUDIT_DATABASE_URL",
    "postgresql://audit:audit@127.0.0.1:5432/rh_audit",
)


def _base_intent(cid: str) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "client_order_id": cid,
        "bot_id": "default",
        "status": "PENDING",
        "strategy_version": "1.0.0",
        "signal_symbol": "QQQ",
        "execution_symbol": "TQQQ",
        "action": "BUY",
        "quantity": 1,
        "signal_id": f"sig-{cid}",
        "purpose": "entry",
        "intent_timestamp": now,
        "expires_at": now + timedelta(hours=1),
        "integrity_digest": "probe",
    }


def insert_intent(conn: psycopg.Connection, cid: str) -> None:
    row = _base_intent(cid)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO bot_robinhood_execution_intents (
              client_order_id, bot_id, status, strategy_version, signal_symbol,
              execution_symbol, action, quantity, signal_id, purpose,
              intent_timestamp, expires_at, integrity_digest
            ) VALUES (
              %(client_order_id)s, %(bot_id)s, %(status)s, %(strategy_version)s,
              %(signal_symbol)s, %(execution_symbol)s, %(action)s, %(quantity)s,
              %(signal_id)s, %(purpose)s, %(intent_timestamp)s, %(expires_at)s,
              %(integrity_digest)s
            )
            """,
            row,
        )
    conn.commit()


def cas_claim(dsn: str, cid: str, worker: str) -> bool:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE bot_robinhood_execution_intents
                SET status = 'CLAIMED', claimed_by = %s, claimed_at = now(),
                    updated_at = now()
                WHERE client_order_id = %s AND bot_id = 'default'
                  AND status = 'PENDING' AND expires_at > now()
                RETURNING client_order_id
                """,
                (worker, cid),
            )
            won = cur.fetchone() is not None
        conn.commit()
        return won


def main() -> int:
    failures: list[str] = []
    print(f"DSN={DSN.split('@')[-1]}")

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT relname, relrowsecurity
                FROM pg_class
                WHERE relname IN (
                  'bot_robinhood_execution_intents',
                  'bot_robinhood_entry_reservations',
                  'bot_robinhood_equity_baselines'
                )
                ORDER BY 1
                """
            )
            rls = cur.fetchall()
            print("RLS enabled:", rls)
            if len(rls) != 3 or not all(row[1] for row in rls):
                failures.append("RLS not enabled on all three RH tables")

            cur.execute(
                """
                SELECT tablename, policyname, roles::text
                FROM pg_policies
                WHERE tablename LIKE 'bot_robinhood%'
                ORDER BY 1, 2
                """
            )
            policies = cur.fetchall()
            print("policies:", policies)
            if len(policies) != 3:
                failures.append(f"expected 3 service_role policies, got {len(policies)}")
            if any("authenticated" in (p[1] or "") for p in policies):
                failures.append("authenticated policy still present")

        # UNIQUE client_order_id
        cid = f"uniq-{uuid.uuid4().hex[:12]}"
        insert_intent(conn, cid)
        try:
            insert_intent(conn, cid)
            failures.append("duplicate client_order_id did not raise 23505")
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            print("PASS unique client_order_id → 23505")

        # Concurrent CAS claim — exactly one winner
        cid2 = f"claim-{uuid.uuid4().hex[:12]}"
        insert_intent(conn, cid2)

    winners = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> bool:
        barrier.wait()
        return cas_claim(DSN, cid2, f"w{i}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(worker, i) for i in range(8)]
        for fut in as_completed(futs):
            if fut.result():
                winners.append(True)

    print(f"concurrent claim winners={len(winners)}")
    if len(winners) != 1:
        failures.append(f"expected exactly 1 claim winner, got {len(winners)}")
    else:
        print("PASS concurrent CAS claim → 1 winner")

    # Reservation PK uniqueness
    day = datetime.now(timezone.utc).date()
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bot_robinhood_entry_reservations
                  (bot_id, trading_day, entry_slot, client_order_id)
                VALUES ('default', %s, 1, 'res-a')
                """,
                (day,),
            )
        conn.commit()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bot_robinhood_entry_reservations
                      (bot_id, trading_day, entry_slot, client_order_id)
                    VALUES ('default', %s, 1, 'res-b')
                    """,
                    (day,),
                )
            conn.commit()
            failures.append("duplicate reservation PK did not raise 23505")
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            print("PASS reservation PK → 23505")

    # Concurrent reservation inserts
    day2 = day + timedelta(days=1)
    res_winners: list[bool] = []
    barrier2 = threading.Barrier(6)

    def res_worker(i: int) -> bool:
        barrier2.wait()
        try:
            with psycopg.connect(DSN) as c:
                with c.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO bot_robinhood_entry_reservations
                          (bot_id, trading_day, entry_slot, client_order_id)
                        VALUES ('default', %s, 1, %s)
                        """,
                        (day2, f"rw-{i}"),
                    )
                c.commit()
            return True
        except psycopg.errors.UniqueViolation:
            return False

    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(res_worker, i) for i in range(6)]
        for fut in as_completed(futs):
            if fut.result():
                res_winners.append(True)
    print(f"concurrent reservation winners={len(res_winners)}")
    if len(res_winners) != 1:
        failures.append(f"expected 1 reservation winner, got {len(res_winners)}")
    else:
        print("PASS concurrent reservation → 1 winner")

    # RLS deny for anon / authenticated.
    # Table owner bypasses RLS unless FORCE — migrations do not FORCE (gap vs
    # PostgREST JWT roles which are non-owners). Force here so the deny probe
    # is meaningful on this ephemeral owner connection.
    with psycopg.connect(DSN) as conn:
        for table in (
            "bot_robinhood_execution_intents",
            "bot_robinhood_entry_reservations",
            "bot_robinhood_equity_baselines",
        ):
            conn.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        conn.commit()
        print("NOTE: FORCE ROW LEVEL SECURITY applied for probe (not in migrations)")

        conn.execute("SET ROLE anon")
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM bot_robinhood_execution_intents")
                n = cur.fetchone()[0]
            print(f"anon SELECT visible_rows={n}")
            if n != 0:
                failures.append(f"anon could see {n} intent rows")
            else:
                print("PASS anon SELECT denied (0 rows)")
        except psycopg.Error as exc:
            print(f"PASS anon SELECT error (fail-closed): {exc}")
        finally:
            conn.execute("RESET ROLE")

        conn.execute("SET ROLE authenticated")
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bot_robinhood_execution_intents (
                      client_order_id, bot_id, status, strategy_version, signal_symbol,
                      execution_symbol, action, quantity, signal_id, purpose,
                      intent_timestamp, expires_at, integrity_digest
                    ) VALUES (
                      %s, 'default', 'PENDING', '1.0.0', 'QQQ', 'TQQQ', 'BUY', 1,
                      'x', 'entry', now(), now() + interval '1 hour', 'probe'
                    )
                    """,
                    (f"authz-{uuid.uuid4().hex[:10]}",),
                )
            conn.commit()
            failures.append("authenticated INSERT succeeded (should deny)")
        except psycopg.Error as exc:
            conn.rollback()
            print(f"PASS authenticated INSERT denied: {type(exc).__name__}")
        finally:
            conn.execute("RESET ROLE")

        # service_role has BYPASSRLS — should see rows even under FORCE
        conn.execute("SET ROLE service_role")
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM bot_robinhood_execution_intents")
                n = cur.fetchone()[0]
            print(f"service_role SELECT count={n}")
            if n < 1:
                failures.append("service_role could not read intents")
            else:
                print("PASS service_role SELECT allowed")
        finally:
            conn.execute("RESET ROLE")

    if failures:
        print("FAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("ALL EPHEMERAL PG PROBES PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

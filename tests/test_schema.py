"""Phase 1: smoke-test the schema and constraints."""
import sqlite3

import pytest


def test_all_tables_present(db):
    rows = db.query_all(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    names = {r["name"] for r in rows}
    expected = {
        "tasks", "artifacts", "events", "pods",
        "budget_ledger", "parent_actions",
    }
    assert expected.issubset(names), names


def test_task_status_check_constraint(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO tasks (task_id, agent_role, goal_text, "
            "created_at, status) VALUES (?, ?, ?, ?, ?)",
            ("t_x", "development", "g", "2026-04-25T00:00:00Z", "BOGUS"),
        )


def test_wal_mode_enabled(db):
    row = db.query_one("PRAGMA journal_mode")
    assert row[0].lower() == "wal"

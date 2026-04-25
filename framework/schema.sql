-- Multi-agent orchestration framework — SQLite schema (Phase 1)

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tasks (
    task_id              TEXT PRIMARY KEY,
    parent_task_id       TEXT,
    agent_role           TEXT NOT NULL,
    goal_text            TEXT NOT NULL,
    input_artifact_ids   TEXT NOT NULL DEFAULT '[]',
    output_artifact_types TEXT NOT NULL DEFAULT '[]',
    recommended_model    TEXT,
    priority             INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    depends_on           TEXT NOT NULL DEFAULT '[]',
    working_dir          TEXT,
    status               TEXT NOT NULL
                         CHECK (status IN (
                             'created','before_gate','ready','claimed',
                             'running','after_gate','done','rejected'
                         )),
    pod_id               TEXT,
    claimed_at           TEXT,
    started_at           TEXT,
    completed_at         TEXT,
    rejection_reason     TEXT,
    retry_count          INTEGER NOT NULL DEFAULT 0,
    archived_at          TEXT,
    worktree_path        TEXT,
    FOREIGN KEY (parent_task_id) REFERENCES tasks(task_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_claim
    ON tasks(status, priority DESC, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_tasks_pod
    ON tasks(pod_id);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id          TEXT PRIMARY KEY,
    artifact_type        TEXT NOT NULL,
    produced_by_task     TEXT NOT NULL,
    produced_by_agent    TEXT NOT NULL,
    produced_at          TEXT NOT NULL,
    tokens_in            INTEGER,
    tokens_out           INTEGER,
    cost_usd             REAL,
    duration_seconds     REAL,
    model                TEXT,
    content              TEXT NOT NULL,
    FOREIGN KEY (produced_by_task) REFERENCES tasks(task_id)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_task
    ON artifacts(produced_by_task);
CREATE INDEX IF NOT EXISTS idx_artifacts_type
    ON artifacts(artifact_type);

CREATE TABLE IF NOT EXISTS events (
    event_id             TEXT PRIMARY KEY,
    ts                   TEXT NOT NULL,
    type                 TEXT NOT NULL,
    task_id              TEXT,
    payload              TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);

CREATE TABLE IF NOT EXISTS pods (
    pod_id               TEXT PRIMARY KEY,
    status               TEXT NOT NULL DEFAULT 'idle'
                         CHECK (status IN ('idle','working','offline')),
    last_seen            TEXT,
    current_task_id      TEXT,
    registered_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_ledger (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                   TEXT NOT NULL,
    pod_id               TEXT NOT NULL,
    task_id              TEXT NOT NULL,
    agent_role           TEXT,
    model                TEXT,
    input_tokens         INTEGER NOT NULL DEFAULT 0,
    output_tokens        INTEGER NOT NULL DEFAULT 0,
    cost_usd             REAL NOT NULL DEFAULT 0.0,
    duration_seconds     REAL NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_budget_ts ON budget_ledger(ts);
CREATE INDEX IF NOT EXISTS idx_budget_task ON budget_ledger(task_id);

CREATE TABLE IF NOT EXISTS parent_actions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                   TEXT NOT NULL,
    tool                 TEXT NOT NULL,
    args                 TEXT NOT NULL DEFAULT '{}',
    result               TEXT,
    caller               TEXT NOT NULL DEFAULT 'parent'
);

CREATE INDEX IF NOT EXISTS idx_parent_actions_ts ON parent_actions(ts);

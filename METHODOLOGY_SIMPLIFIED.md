# Multi-Agent Orchestration Framework — Methodology

> **Audience**: Claude Code, building this framework from scratch.
> **Deliverable**: The framework itself. The chess engine is a deferred demo target, not part of v1.

---

## 1. What This Project Is

A multi-agent orchestration framework built on the Anthropic Claude API. The user supplies a free-form goal and a target git repository. A Claude Code session acts as the **parent** — it plans the work, breaks it into tasks, and dispatches them to **pod workers** (separate processes with their own API keys).

Every task passes through the user twice: once before the pod runs (before gate) and once after (after gate). Nothing moves without explicit user approval. The user sees and can edit everything.

The framework is the deliverable. The chess engine is a demo target that will exercise the framework after v1 is done.

---

## 2. Goals (in priority order)

1. **Full user control.** Every task is gated before and after execution. The user sees the full task spec, can edit it, sees the full artifact output, and approves before anything proceeds.
2. **Avoid context rot.** Pods never accumulate conversation history across tasks. The parent reads framework state from disk rather than relying on memory.
3. **Token optimization.** Adaptive model selection per task (Haiku → Sonnet → Opus by agent role), fresh-context pods, conversational parent reuses context efficiently.
4. **Anti-blackbox.** Every framework tool call, every agent decision, every artifact, and every budget event lives as a structured record. The user can `git log` the entire run.
5. **Research-workflow feel.** Typed artifacts are first-class objects. Per-agent change logs read like a lab notebook. Stats are tracked per task for experimental analysis.

---

## 3. High-Level Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  User's terminal: Claude Code session = THE PARENT           │
│                                                              │
│  • User describes goal in natural language                   │
│  • Claude Code reads framework-state/, drafts plan           │
│  • BEFORE GATE: surfaces task spec, user edits + approves    │
│  • Pod executes task                                         │
│  • AFTER GATE: surfaces artifact + stats, user approves      │
│  • Repeat for each task                                      │
└────────────────────────┬─────────────────────────────────────┘
                         │ (framework tools — bash)
┌────────────────────────▼─────────────────────────────────────┐
│                    FastAPI Backend                           │
│  • SQLite source of truth (tasks, artifacts, events, budget) │
│  • Atomic task claiming                                      │
│  • Pod worker process manager                                │
│  • Tool-call audit logger                                    │
└──────────────────┬───────────────────────────────────────────┘
                   │
            ┌──────▼──────┐
            │   POD A     │
            │ (API key A) │
            │             │
            │ Worker loop:│
            │ claim → run │
            │ → submit    │
            └─────────────┘
```

**One Anthropic API key for the pod:**
- `ANTHROPIC_API_KEY_POD_A` — pod worker

**The parent does not have its own API key.** Claude Code uses whatever credentials the user has logged in with. The parent's reasoning happens inside the Claude Code session.

> v2: add a second pod (`POD_B`) once the single-pod loop is stable.

---

## 4. Repositories

Two locations:

1. **Target repo** — the codebase the framework operates *on*. User-supplied path. Cloned locally; dev agents work on a dedicated branch (`framework/<run-id>`); pushes happen when the user approves a milestone. Never touched outside dev agent tasks.
2. **Framework-state directory** — the framework's own state. Created at init as a subdirectory adjacent to the target repo.

```
framework-state/
├── CLAUDE.md                      # parent instructions for Claude Code
├── config.yaml                    # model assignments, budget cap
├── progress.md                    # global change log
├── rolling_summary.md             # parent-maintained, ≤2k tokens
├── agents/
│   ├── methodology.md             # subagent role configs
│   ├── development.md
│   └── testing.md
├── plan/
│   └── current_plan.md            # active task list
├── artifacts/
│   ├── task_specs/
│   ├── research_briefs/
│   ├── patch_summaries/
│   ├── test_results/
│   └── failure_reports/
├── logs/
│   ├── methodology_agent.md       # per-agent change log
│   ├── development_agent.md
│   ├── testing_agent.md
│   ├── parent_actions.jsonl       # every framework tool call the parent makes
│   └── budget_ledger.jsonl        # append-only token/cost records per task
├── events.jsonl                   # append-only event stream
└── framework.db                   # SQLite source of truth
```

Every state-changing operation commits to this directory. The user gets a complete audit trail via `git log`.

---

## 5. The Parent: Claude Code

A Claude Code session running in the user's terminal acts as the parent. The framework provides Claude Code with:

1. A `CLAUDE.md` at the framework-state root that defines the parent role
2. A set of **framework tools** (bash commands) for state queries and mutations
3. The framework-state directory as readable/writable working memory

### 5.1 The parent's `CLAUDE.md`

The parent's role config tells Claude Code:

- It is the parent in a multi-agent orchestration framework
- The user's instructions are conversational; the parent's job is to translate them into framework tool calls
- It must read framework state from disk, not rely on memory
- Every task requires a before gate and an after gate — no exceptions
- At the before gate, surface the full TaskSpec to the user and apply any edits before approving
- At the after gate, surface the full artifact content and per-task stats before approving
- Every framework tool call is automatically logged; the parent does not need to log them manually
- It must update `rolling_summary.md` after each task is approved at the after gate

The exact `CLAUDE.md` text is produced in Phase 3 of the build.

### 5.2 Framework tools the parent uses

Implemented as bash commands in v1.

| Tool | Purpose |
|---|---|
| `framework state` | Dump current run state: pod status, queue counts, recent events, pending gates |
| `framework db query <sql>` | Read-only SQL queries against `framework.db` |
| `framework artifact get <id>` | Fetch full artifact contents |
| `framework artifact list [--type T] [--task t_X]` | List artifacts with filters |
| `framework plan show` | Show current task list |
| `framework plan create <yaml-file>` | Submit a new plan (creates TaskSpec rows in `before_gate` state) |
| `framework plan edit <task_id> <field> <value>` | Edit a TaskSpec field before approval |
| `framework gate before approve <task_id>` | Flip task from `before_gate` to `ready` |
| `framework gate before reject <task_id> --reason "..."` | Reject a task at the before gate |
| `framework gate after approve <task_id>` | Flip task from `after_gate` to `done` |
| `framework gate after reject <task_id> --reason "..."` | Reject artifact, send task back to `before_gate` for retry |
| `framework subagent invoke <role> <task-yaml>` | Synchronously invoke a subagent role (used for methodology agent at planning time) |
| `framework summary update <new-summary-file>` | Replace `rolling_summary.md` (commits and emits event) |
| `framework run start --goal "..." --target-repo <path>` | Bootstrap a new run |

Every tool call writes to `logs/parent_actions.jsonl`:

```jsonl
{"ts":"2026-04-25T14:30:00Z","tool":"framework_gate_before_approve","args":{"task_id":"t_042"},"result":"ok"}
```

### 5.3 Conversational interaction patterns

The user talks to Claude Code naturally. The two most common patterns:

**Before gate.** Parent surfaces the full TaskSpec. User can say "change the model to Opus" or "add this context to the goal." Parent calls `framework plan edit`, shows the diff, waits for approval. User says "approve" → parent calls `framework gate before approve`.

**After gate.** Parent surfaces the full artifact content plus the per-task stats block (tokens in/out, cost, model, duration). User reviews, says "approve" or "redo this" → parent calls `framework gate after approve` or `framework gate after reject`.

There is no mid-run stats surfacing in conversation. Stats are in the ledger and the watch display. The parent does not narrate token burn unless the user asks.

---

## 6. The Gate System

Gates are SQLite rows tracking task status. Every task passes through both gates. No exceptions.

### Gate flow

```
plan_create → BEFORE GATE → ready → claimed → running → AFTER GATE → done
                  ↓                                           ↓
               rejected                                    rejected → back to before_gate
```

### Before gate

Triggered when the parent calls `framework plan create` or when a rejected task is re-queued.

The parent surfaces to the user:
- Full TaskSpec (goal text, agent role, model, input artifact IDs, output artifact types, priority)
- Any input artifacts the task depends on (fetched via `framework artifact get`)

The user can edit any field via `framework plan edit` before approving. Once approved, the task moves to `ready` and the pod can claim it.

### After gate

Triggered when the pod submits a result artifact.

The parent surfaces to the user:
- Full artifact content
- Per-task stats block:

```
Task:       t_042
Model:      claude-sonnet-4-6
Input tok:  8,123
Output tok: 2,891
Cost:       $0.0432
Duration:   14.2s
```

The user can approve (task moves to `done`, rolling summary updates) or reject (task returns to `before_gate` with the failure noted, user can edit the spec before re-approving).

---

## 7. Live Status Display

A separate terminal pane (not in the Claude Code session):

```bash
python -m framework watch
```

Auto-refreshes every 2 seconds by reading SQLite directly. Shows pod status, queue counts, recent events, current gate (before or after), current budget burn. Uses the `rich` library. Independent of the parent.

---

## 8. Task Lifecycle

```
created → before_gate → ready → claimed → running → after_gate → done
               ↓                                         ↓
            rejected ← ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ rejected
```

States:
- `created`: parent submitted the task spec
- `before_gate`: awaiting user review and approval of the task spec
- `ready`: approved at before gate, pod may claim
- `claimed`: pod has atomically claimed it
- `running`: pod is executing
- `after_gate`: pod submitted result, awaiting user review of artifact
- `done`: approved at after gate
- `rejected`: rejected at either gate; parent re-queues to `before_gate` after edits

Atomic claim is the only transaction that matters:

```sql
BEGIN IMMEDIATE;
UPDATE tasks
SET status = 'claimed', pod_id = ?, claimed_at = ?
WHERE task_id = (
  SELECT task_id FROM tasks
  WHERE status = 'ready'
  ORDER BY priority DESC, created_at ASC
  LIMIT 1
)
RETURNING *;
COMMIT;
```

All claim logic goes through one function. Do not let claim logic spread.

---

## 9. Schemas

### 9.1 TaskSpec

```yaml
task_id: t_042
parent_task_id: t_017             # nullable
agent_role: methodology | development | testing
goal_text: |
  Implement the UCI protocol parser...
input_artifact_ids: [a_research_017]
output_artifact_types: [PatchSummary, TestResult]
recommended_model: claude-haiku-4-5-20251001 | claude-sonnet-4-6 | claude-opus-4-7
priority: 0
created_at: 2026-04-25T14:30:00Z
depends_on: [t_040, t_041]
working_dir: <target_repo_path>
```

No effort tier. No token estimates. No verification_mode. The user is the verifier.

### 9.2 Artifact (base)

```yaml
artifact_id: a_research_017
artifact_type: ResearchBrief | PatchSummary | TestResult | FailureReport | ProgressLogEntry
produced_by_task: t_017
produced_by_agent: methodology
produced_at: 2026-04-25T14:35:12Z
tokens_in: 2400
tokens_out: 1800
cost_usd: 0.0312
duration_seconds: 11.4
model: claude-sonnet-4-6
content: <type-specific body>
```

### 9.3 Type-specific artifact bodies

**ResearchBrief**: `summary` (≤500 tokens), `key_findings` (list), `sources` (URLs), `open_questions` (list).

**PatchSummary**: `files_changed` (list of paths), `diff_stat` (additions/deletions per file), `rationale` (≤300 tokens), `test_targets` (list).

**TestResult**: `tests_run`, `passed`, `failed` (list with names + brief reason), `runtime_seconds`, `coverage_delta` (optional).

**FailureReport**: `failure_mode` (api_error | logic_error | timeout | budget_exceeded | unrecoverable), `error_message`, `retry_count`, `recommended_action` (retry | reroute | escalate).

**ProgressLogEntry**: appended to per-agent `.md` log; format: `timestamp | task_id | one-line outcome | tokens_in/out | cost_usd | files_touched | artifact_ids`.

### 9.4 Event

```jsonl
{"event_id":"e_0001","ts":"2026-04-25T14:30:00Z","type":"task_created","task_id":"t_042","payload":{...}}
```

Event types: `task_created`, `task_before_gate`, `task_approved_before`, `task_rejected_before`, `task_claimed`, `task_completed`, `task_after_gate`, `task_approved_after`, `task_rejected_after`, `artifact_submitted`, `budget_updated`, `summary_updated`, `plan_revised`.

### 9.5 Parent action log

```jsonl
{"ts":"...","tool":"framework_gate_after_approve","args":{"task_id":"t_042"},"result":"ok","caller":"parent"}
```

Every framework tool invocation produces one line. Mandatory.

---

## 10. Subagent Configuration Files

Each `agents/<role>.md` contains:

```markdown
---
role: development
allowed_tools: [filesystem_read, filesystem_write, bash, code_execution]
default_model: claude-sonnet-4-6
output_artifact_contract: [PatchSummary, ProgressLogEntry]
prompt_variables: [task_goal, input_artifacts, working_dir, rolling_summary]
---

# Development Subagent

## Role
You are a development subagent in a multi-agent orchestration framework.

## Inputs (provided at task start)
- task_goal, input_artifacts, working_dir, rolling_summary

## Output contract
1. A PatchSummary artifact
2. A ProgressLogEntry line appended to logs/development_agent.md

## Behavioral rules
- Edit files only inside working_dir
- Never modify framework-state/
- If you cannot complete the task, produce a FailureReport
- Do not request additional context; work with what was provided
```

Pods read this file fresh at the start of every task. No watcher, no caching.

Three subagent configs in v1: `methodology.md`, `development.md`, `testing.md`.

---

## 11. The Planning Loop

One LLM planning subagent (the methodology agent), invoked synchronously by the parent at the start of a run.

1. User: "Start a run on the chess engine repo, goal is implement UCI protocol."
2. Parent calls `framework run start --goal "..." --target-repo <path>`. Bootstrap creates `framework-state/`, initializes SQLite, writes `config.yaml`, clones target repo, writes default agent `.md` files.
3. Parent calls `framework subagent invoke methodology <task-yaml>` with the user goal and a target-repo summary as inputs.
4. Methodology agent produces a structured task list with agent role assignments, model recommendations, dependencies, and rationale.
5. Parent receives the plan and surfaces it to the user in conversation: "Here's what I'm proposing. Each task will go through a before and after gate — approve the plan structure to proceed."
6. User responds. Parent calls `framework plan create` once aligned. All tasks enter `before_gate` state in priority/dependency order.
7. Parent begins working through the before gates one task at a time.

**On rejection or failure**: a rejected task at the after gate returns to `before_gate`. The user can edit the spec and re-approve, or tell the parent to replan. Replanning re-invokes the methodology agent.

---

## 12. The Rolling Summary

The single most important anti-context-rot mechanism for pods. Maintained by the parent.

**Update trigger**: every task approved at the after gate.

**Update mechanism**: parent reads previous summary + the just-approved artifact(s), produces a new summary, and calls `framework summary update <file>`.

**Output schema**:
```markdown
## Goal
<one paragraph>

## Completed milestones
- <bullet>

## Open threads
- <bullet>

## Key decisions
- <bullet with date and rationale>

## Referenceable artifact IDs
- a_017: <one-line description>
```

**Hard cap**: 2000 tokens. If exceeded, parent compresses by dropping oldest "completed milestones" first and notes the compression in `progress.md`.

The summary is passed to pods via the prompt template. Full artifact contents are referenceable by ID — pods request specific artifacts only when their task spec lists them as inputs.

---

## 13. Pod Worker Loop

One Python process. Started by `scripts/start_pod.py <pod_id>`.

```python
def pod_loop(pod_id, api_key):
    while True:
        task = backend.claim_next_task(pod_id)
        if task is None:
            time.sleep(2)
            continue
        try:
            agent_config = load(f"framework-state/agents/{task.agent_role}.md")
            inputs = backend.fetch_artifacts(task.input_artifact_ids)
            summary = backend.fetch_rolling_summary()
            prompt = build_prompt(agent_config, task, inputs, summary)

            result = anthropic.messages.create(
                model=task.recommended_model,
                api_key=api_key,
                ...
            )

            artifacts = parse_output(result, task.output_artifact_types)
            backend.submit_result(task.task_id, artifacts, result.usage)
            backend.append_progress_log(task.agent_role, task, artifacts)
        except Exception as e:
            backend.report_failure(task.task_id, e)
```

Boring, reliable. Do not make this fancy.

---

## 14. Scheduler

Pure code. No LLM.

```python
def claim_next_task(pod_id):
    candidates = query("""
        SELECT * FROM tasks
        WHERE status = 'ready'
        ORDER BY priority DESC, created_at ASC
        LIMIT 1
    """)

    if not candidates:
        return None

    task = candidates[0]
    atomic_update(task.task_id, status='claimed', pod_id=pod_id)
    emit_event('task_claimed', task_id=task.task_id, pod_id=pod_id)
    return task
```

In v1 with one pod, concurrency is not a concern. The atomic claim function still exists for correctness and v2 readiness.

---

## 15. Budget Ledger

Append-only `logs/budget_ledger.jsonl`. One line per pod API call:

```jsonl
{"ts":"...","pod_id":"pod_a","task_id":"t_042","agent_role":"development","model":"claude-sonnet-4-6","input_tokens":8123,"output_tokens":2891,"cost_usd":0.0432,"duration_seconds":14.2}
```

Mirrored in SQLite for fast queries. The watch command reads from SQLite; the JSONL is the durable record for offline experimental analysis.

**Daily cost cap**: configurable in `config.yaml`. When hit, the scheduler stops claiming new tasks and emits a `budget_cap_hit` event. Parent surfaces this to the user at the next interaction.

Note: parent (Claude Code) token usage is not tracked in this ledger — that is metered by Anthropic on the user's account. The ledger covers pod API calls only.

---

## 16. Failure Handling

**Per-call retries**: 3 attempts with exponential backoff (1s, 4s, 16s) on transient errors (rate limits, network timeouts).

**Per-task failure**: after 3 call-level retries fail, pod produces a FailureReport artifact and marks the task `after_gate`. The parent surfaces the FailureReport at the after gate just like any other artifact. The user decides: approve (accept the failure, move on), reject (edit the spec and retry), or tell the parent to replan.

There is no automatic failure routing. The user sees every failure at the after gate and decides.

---

## 17. CLI Commands (for the user, not the parent)

```bash
python -m framework backend       # start FastAPI + pod worker in background
python -m framework watch         # live status terminal pane
python -m framework stop          # graceful shutdown of pod
```

The user starts the backend, opens Claude Code, and lets the parent drive everything else through framework tools.

---

## 18. Configuration (`framework-state/config.yaml`)

```yaml
budget:
  daily_cap_usd: 50.00

models:
  haiku: claude-haiku-4-5-20251001
  sonnet: claude-sonnet-4-6
  opus: claude-opus-4-7
  methodology_default: claude-opus-4-7

rolling_summary:
  max_tokens: 2000

retries:
  per_call: 3
```

---

## 19. Build Phases

Build in this order. Do not skip ahead. Each phase ends with a working, testable deliverable.

### Phase 1: Backend skeleton (no LLM)
- SQLite schema (tasks, artifacts, events, pods, budget, parent_actions)
- FastAPI app with REST endpoints used by both pods and framework tools
- Atomic claim function with `BEGIN IMMEDIATE`
- Event stream writes to both SQLite and `events.jsonl`
- Test by hand-writing task rows and verifying state transitions through the full lifecycle including both gate states

### Phase 2: One pod, hardcoded prompt
- Pod worker loop
- `claim → call Anthropic API with hardcoded prompt → submit result → emit after_gate event`
- Budget ledger writes (tokens, cost, duration per task)
- Test with a hand-written trivial task spec that goes through both gates manually

### Phase 3: Framework tools + parent CLAUDE.md
- Implement all bash framework tools
- Each tool writes to `parent_actions.jsonl`
- Write the parent `CLAUDE.md` with: role description, before/after gate requirement, rolling summary update requirement
- Implement `framework run start` bootstrap
- Test by running Claude Code against a hand-built plan: parent should surface both gates, accept edits, update rolling summary

### Phase 4: Methodology subagent + planning loop
- `agents/methodology.md` config
- `framework subagent invoke` command (synchronous)
- End-to-end test: user asks Claude Code for a small goal; parent invokes methodology agent; surfaces plan; user approves task by task through both gates

### Phase 5: Dev + testing subagents, rolling summary
- `agents/development.md`, `agents/testing.md`
- Per-agent change logs
- Rolling summary update on every after-gate approval
- Per-agent artifact contracts enforced
- End-to-end test: small real project executed with full gate flow

### Phase 6: Polish
- Daily cost cap with auto-pause surfaced at next parent interaction
- Failure flow: FailureReport surfaced at after gate, user decides
- `framework session reset` mechanism for long runs
- Resilience tests: kill pod mid-task, hit budget cap, reject a task at both gates

### Phase 7 (deferred to v2)
- Second pod (`POD_B`) with concurrent claiming
- Experimental loop: candidates, eval results, promote/prune
- Git worktrees for parallel candidate development
- Cross-run memory
- Soft gates with timers
- Migrate framework tools from bash to MCP server
- Optional read-only web UI

---

## 20. What Is Explicitly Deferred

Do not build these in v1.

- Second pod and concurrent execution
- Streamlit / web UI
- Soft gates / countdown timers
- Cross-run memory
- Experimental loop with candidate variants
- Multiple development pods working concurrently on the same repo
- WebSocket live updates
- Distributed deployment
- MCP-based framework tools
- Standalone parent process with its own API key
- Free-tier validator / effort estimator
- Automatic task chaining without gates

---

## 21. Architectural Invariants

These must remain true throughout the build. If you find yourself violating one, stop and reconsider.

1. **Every task passes through both gates.** No task moves from `ready` to `done` without explicit user approval at the before gate and the after gate. No exceptions.
2. **Pods never accumulate context across tasks.** Every pod task starts with a fresh API call.
3. **The parent is a Claude Code session, not a separate process.** No daemon for the parent's reasoning.
4. **Gates are state, not blocking calls.** A gated task is a SQLite row. No code blocks waiting for input.
5. **Pods communicate only through the backend.** No direct pod-to-pod messages.
6. **The backend is the source of truth.** Files are mirrors of SQLite.
7. **All claim logic goes through one function.** `claim_next_task(pod_id)`.
8. **The rolling summary is the only running-state context passed to pods.** No transcripts, no histories.
9. **Stats are recorded per task in the budget ledger.** Every pod API call produces one ledger line with tokens, cost, and duration.
10. **The framework operates on the target repo via local clone.** Not via GitHub API.
11. **Framework state lives in a separate directory from the target codebase.**
12. **Every parent framework tool call is logged automatically to `parent_actions.jsonl`.** Parent does not need to log manually.
13. **The parent reads framework state from disk.** It does not rely on conversational memory for ground truth.
14. **Edits to a TaskSpec at the before gate go through `framework plan edit`.** The parent does not directly modify SQLite or YAML files.

---

## 22. First Concrete Tasks for Claude Code Building This

When you start building, the first three tasks are:

1. Initialize the project: `pyproject.toml`, dependencies (`fastapi`, `uvicorn`, `anthropic`, `rich`, `pyyaml`, `sqlite3` stdlib), directory structure under `framework/`
2. Implement the SQLite schema and FastAPI CRUD endpoints (Phase 1), including both gate states in the task lifecycle
3. Write the atomic `claim_next_task` function and unit-test it

Do not write any LLM-calling code until Phase 1 is fully working and tested. Do not implement framework tools (Phase 3) until the pod can execute a hand-written task through both gates (Phase 2).

---

*End of methodology.*

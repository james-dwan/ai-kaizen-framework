# From demo to your real process

The [tutorial](TUTORIAL.md) runs a sample process. This guide is about running
**yours** — with the agents working a real board your team actually uses. It's
organized as seven steps; do them in order, and resist skipping Step 7.

A mental model first. The framework has exactly two integration seams:

1. **The run log** (`kaizen_runlog.jsonl`) — every metric, Pareto, target, and
   the teammate's evidence all derive from events here. Anything that can emit
   `run_started` / `exception` / `run_completed` events can use the entire
   daily-Kaizen stack, whether or not the process itself is a LangGraph.
2. **The board** — the agents only ever call three methods
   (`create_ticket` / `update_ticket` / `list_tickets`), so local JSON,
   Microsoft Planner, or your own adapter are interchangeable.

Everything else — rules, targets, prompts, standard work, the owner — is the
one YAML config.

---

## Step 1 — Connect your process

### Option A: your process is (or can be) a LangGraph

Keep your node functions exactly as they are and swap the graph builder:

```python
from kaizen import KaizenConfig, KaizenGraphBuilder
from kaizen.kaizen_graph import KaizenState

class OrderState(KaizenState, TypedDict, total=False):   # add your fields
    order_id: str
    total: float

config = KaizenConfig.load("config/kaizen_config.yaml")
builder = KaizenGraphBuilder(OrderState, config)
builder.add_node("validate", validate)      # your existing functions, unchanged
builder.add_node("fulfil", fulfil)
builder.set_entry_point("validate")
builder.add_edge("validate", "fulfil")
builder.set_finish_point("fulfil")
graph = builder.compile()
```

You get per-node abnormality detection, the Jidoka stop/andon routing, and
automatic run-log events for free. If your nodes call LLM agents, tools, or
APIs, nothing changes — the framework watches *state*, not implementation.

### Option B: your process is not a LangGraph

Emit events to the run log from wherever the process lives — a call-centre
platform's webhook, a Power Automate flow, a nightly batch job, even a human
filling in a form:

```python
import uuid
from kaizen import RunLog

runlog = RunLog("kaizen_runlog.jsonl")

run_id = uuid.uuid4().hex[:12]                       # one per call / case / run
runlog.record("run_started", run_id=run_id, process="acme-helpdesk")
runlog.record("exception", run_id=run_id, process="acme-helpdesk",
              rule="customer-complaint", severity="medium",
              sqdip_category="quality", summary="Complaint on call 4411")
runlog.record("run_completed", run_id=run_id, process="acme-helpdesk")
```

That's sufficient for SQDIP, the Pareto, targets, target-miss cards, the daily
reflection, and the teammate. You forgo only the automatic in-flow stop — your
process's own escalation plays that role.

**Data caution:** exception events and card descriptions end up on a shared
board. Keep customer PII out of `summary` fields — reference case IDs, not
contents. (The graph path already truncates state snapshots, but what you name
things is on you.)

## Step 2 — Write the rules: what stops the line vs what's counted

In `config/kaizen_config.yaml`. Two questions per abnormality:

- **Would you halt the process right now if this happened?** Then severity at
  or above `jidoka.stop_on_severity` → it stops the line and raises one card.
  Reserve this for "passing this defect downstream is worse than stopping" —
  compliance breaches, money leaving, unbillable work.
- **Otherwise** it's a *counted defect* — recorded, measured, never carded on
  its own. This should be most of your rules. A board flooded with raw defects
  is the failure mode; the daily review exists to choose problems.

Start with 3–6 rules. You'll add more in the daily kata — that's the point.

## Step 3 — Set the targets your daily review already uses

Targets are what turn counts into work. Write them the way your team already
speaks:

```yaml
targets:
  - name: customer-complaints
    description: customer complaints
    rule: customer-complaint          # counts this rule's events
    volume_from: runs                 # denominator = run_started events
    volume_unit: calls to the Acme helpdesk
    target: 20
    direction: below
```

Miss the target and the daily review raises exactly one card: *"On 20 July,
30 out of 1000 calls to the Acme helpdesk had customer complaints, against the
target of <20."* Set the five `sqdip_targets` too — and pick numbers from a
week of real data, not aspirations, or day one drowns the board in
target-miss cards.

## Step 4 — Move the board to where your team lives (Planner)

Run locally until the rules and targets feel right, then switch:

```yaml
kanban:
  provider: planner
  plan_id: "<planner-plan-id>"
  owner_user_id: "<process owner's Azure AD object id>"
  bucket_ids:
    Problems: "<bucket-id>"
    Daily Kaizen: "<bucket-id>"
    Improvement Ideas: "<bucket-id>"
    Experiments: "<bucket-id>"
```

**One-time tenant setup** (needs an Azure admin, ~30 minutes):

1. Create the plan in Planner/Teams with those four buckets.
2. **App registration** in Entra ID (Azure AD): delegated Microsoft Graph
   permissions `Tasks.ReadWrite` + `Group.Read.All`. Planner's app-only
   support is restricted in many tenants, so the pragmatic pattern is a
   **service account** (e.g. `svc-kaizen@yourorg`) using delegated auth — the
   agents then appear on the board as that account, which is honest and
   auditable.
3. Find the IDs with [Graph Explorer](https://developer.microsoft.com/graph/graph-explorer):
   `GET /me/planner/plans` for the plan ID, then
   `GET /planner/plans/{id}/buckets` for bucket IDs, and
   `GET /users/{owner-upn}` → `id` for `owner_user_id`.
4. Supply a `token_provider` — any zero-arg callable returning a Graph token:

```python
from azure.identity import DeviceCodeCredential   # or ClientSecretCredential, etc.
from kaizen import create_board

credential = DeviceCodeCredential(client_id="<app-client-id>", tenant_id="<tenant-id>")
board = create_board(config.kanban,
    token_provider=lambda: credential.get_token(
        "https://graph.microsoft.com/.default").token)
```

Auth deliberately stays in your hands — use whatever flow your tenant policy
requires. What carries over: statuses map to Planner's progress columns (a
human dragging a task **is** the status change), analyses live in task notes,
checklists and priorities transfer, every write is etag-guarded against
concurrent human edits, and proposal cards arrive *assigned to the owner* in
their own Teams view. Current limits: humans reply in the task **notes** (not
threaded comments) — tell the team; and the adapter is verified against a
faithful Graph fake but do your first live run on a **test plan**, not the
team's real one.

## Step 5 — Run the agents as real services

Three long-lived pieces, all stateless between passes (state is the run log +
the board), so they're cron/container-friendly:

| Agent | Cadence | How |
|---|---|---|
| Your process graph | Whatever the business needs | Wherever it already runs |
| Reflection (SQDIP + target-miss cards + ideas) | Once daily, before the standup | Cron / Scheduled Azure Function: a 10-line script calling `ReflectionAgent(...).daily_reflection()` |
| Kaizen Teammate | Continuous polling | A small always-on service calling `KaizenTeammate(...).watch(interval=300)` |

Practical settings for production:

- **Teammate interval: minutes, not seconds.** Against Graph, every pass reads
  task details per card — `interval=300` is plenty (humans reply on human
  timescales) and stays far from throttling.
- **Secrets:** `.env` is a dev convenience. In production put
  `ANTHROPIC_API_KEY` (and Graph credentials) in your secret store — Azure Key
  Vault, environment injection from your orchestrator — and never in the repo.
- **Cost:** the teammate only calls Claude when a card *changed* (new card or
  human input), so steady-state cost tracks team activity, not polling
  frequency. The daily reflection is one or two calls a day. For a
  single-process deployment expect cents-to-low-dollars per day; the model is
  configurable via `build_default_llm(model=...)` if you want to trade down.
- **Run log:** it's an append-only JSONL file. Rotate it monthly (the agents
  only need the review window plus enough history for Paretos and pilots), and
  back it up — it's your metrics history.
- **The local board server is dev-only.** It has no authentication; never
  expose it beyond localhost. In production the board *is* Planner.

## Step 6 — Governance before the agents touch standard work

Before enabling the propose/pilot/approve loop for real:

- Name a real **`process_owner`** (and `owner_user_id` for Planner) — a person
  who will actually review proposals in the daily kata, not a mailbox.
- Put `config/kaizen_config.yaml` **in git**. Approvals version the file
  locally (`config_history/`), but git gives you review, diff, and blame; the
  natural mature pattern is proposal card in Planner + config change as a PR
  the owner merges.
- Keep **sandbox piloting** as standard work: every approved change gets
  trialed with `sandbox: true` (or the what-if replay) before it runs live.
  Write that into the `daily_kata` list so it's visible standard work.
- The hard guarantee holds in code — agents can propose and pilot but can
  never approve — but the *social* half is yours: the owner reviewing
  proposals daily is what keeps the queue short and the trust high.

## Step 7 — Roll out like a Lean practitioner (this is the one people skip)

- **One process first.** Pick one that hurts, with a team that wants it.
- **Shadow week:** run with `sandbox: true` for a week. Defects are counted
  and the reflection runs, but no cards, no stops, no announcements. Use the
  week's data to calibrate severities and set honest targets.
- **Then turn on the board and the kata** — a 10-minute daily standup around
  the Daily Kaizen card. The framework prepares the conversation; the humans
  interpreting it together is the product. If the standup doesn't happen, the
  tooling is decoration.
- **Let the teammate earn trust.** Its first proposals and questions will be
  reviewed skeptically — good. The Sensei gating its analyses, the owner
  gating its proposals, and it never closing tickets are the trust mechanisms;
  point at them.
- **Improve the system weekly** in its own terms: rules and targets are
  standard work — change them through the proposal loop you're demonstrating.

---

## Adoption checklist

- [ ] Process connected (graph wrapped, or events emitted to the run log)
- [ ] 3–6 rules; stop-severity reserved for genuine andon cases
- [ ] Targets set from a week of real data; SQDIP targets filled in
- [ ] PII kept out of summaries and card text
- [ ] Planner plan + buckets created; app registration + service account; IDs in config; first live run on a test plan
- [ ] Reflection scheduled daily; teammate deployed with a minutes-scale interval
- [ ] Secrets in a real secret store; run-log rotation and backup
- [ ] `process_owner` + `owner_user_id` named; config in git; sandbox piloting written into the kata
- [ ] Shadow week done; daily standup scheduled and actually happening

# Example: Professional Services Invoicing

A complete, runnable example of the AI Jidoka Framework applied to a monthly
invoicing process for a professional services firm.

## The process

```
load_reports → validate_reports → aggregate_hours → calculate_invoice → raise_invoice
```

Each node is a plain Python function. The framework wraps every node with the
abnormality rules defined in [`config/kaizen_config.yaml`](config/kaizen_config.yaml):

| Rule | Severity | What happens |
|---|---|---|
| `missing-delivery-reports` | medium | Ticket created, run continues |
| `negative-or-zero-hours` | medium | Invalid reports excluded + ticketed, run continues |
| `missing-rate-card` | high | **Jidoka stop** — hours would go unbilled |
| `invoice-over-approval-threshold` | high | **Jidoka stop** — human approval required |
| `low-utilisation-warning` | low | Logged for the daily reflection |

The bundled sample data deliberately contains problems: one consultant hasn't
submitted a report, one report has negative hours, and one engagement has no
rate card. Run it and watch the line stop before a bad invoice goes out.

## Run it

From the repository root:

```bash
pip install -e .
cd examples/professional-services-invoicing

# 1. Run the invoicing workflow — exceptions become tickets, the line stops
python invoicing_workflow.py

# 2. Generate the daily Kaizen summary (SQDIP + reflection + kata agenda)
python run_daily_kaizen.py

# Optional: have Claude write the reflection narrative
pip install '.[llm]' && export ANTHROPIC_API_KEY=...
python run_daily_kaizen.py --llm

# Optional: trial changes safely — no tickets, no invoice raised
python invoicing_workflow.py --sandbox
```

Artifacts land next to the scripts:

- `kaizen_board.json` — the shared Kanban board (local provider; swap in
  Microsoft Planner or Lists via the config)
- `kaizen_runlog.jsonl` — the event log the SQDIP metrics are computed from
- `kaizen_reports/kaizen-YYYY-MM-DD.md` — daily Kaizen summaries

## The daily kata

1. AI prepares: `run_daily_kaizen.py` posts the summary to the board.
2. Humans and AI review SQDIP together; every stop is inspected.
3. One exception pattern gets a 5 Whys (the scaffold is already in the ticket).
4. One small countermeasure is agreed — often just an edit to
   `kaizen_config.yaml`: a rule threshold, a prompt wording, a kata step.
5. Changes are trialed with `--sandbox`, then standardized. `KaizenConfig.save()`
   versions the file automatically, so every experiment is reversible.

That loop — not the automation — is the point.

"""Generate the Kaizen dashboard for the invoicing process.

    python invoicing_workflow.py   # produce some runs/exceptions first
    python run_daily_kaizen.py     # optional: include the daily report
    python make_dashboard.py       # writes + opens kaizen_dashboard.html
"""

from __future__ import annotations

import webbrowser
from pathlib import Path

from kaizen import KaizenConfig, RunLog, create_board
from kaizen.dashboard import generate_dashboard

HERE = Path(__file__).parent


def main() -> None:
    config = KaizenConfig.load(str(HERE / "config" / "kaizen_config.yaml"))
    config.data["kanban"]["board_path"] = str(HERE / "kaizen_board.json")

    path = generate_dashboard(
        config=config,
        board=create_board(config.kanban),
        runlog=RunLog(str(HERE / "kaizen_runlog.jsonl")),
        output_path=str(HERE / "kaizen_dashboard.html"),
        reports_dir=str(HERE / "kaizen_reports"),
    )
    print(f"Dashboard written to {path}")
    webbrowser.open(f"file://{path}")


if __name__ == "__main__":
    main()

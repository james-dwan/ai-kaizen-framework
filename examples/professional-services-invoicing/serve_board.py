"""Serve the interactive Kanban board for the invoicing process.

    python serve_board.py           # heuristic Sensei
    python serve_board.py --llm     # Claude-powered Sensei questions

Drag tickets between lanes, click a ticket to edit its analysis, add notes,
and use "Ask the Sensei" to have your 5 Whys reviewed in place.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from kaizen import KaizenConfig, SenseiAgent, build_default_llm, create_board, load_env
from kaizen.board_server import serve_board

HERE = Path(__file__).parent
load_env(str(HERE))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llm", action="store_true", help="Claude-powered Sensei.")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    config = KaizenConfig.load(str(HERE / "config" / "kaizen_config.yaml"))
    config.data["kanban"]["board_path"] = str(HERE / "kaizen_board.json")

    llm = None
    if args.llm:
        try:
            llm = build_default_llm()
        except Exception as exc:
            print(f"[!] Claude unavailable ({exc}); heuristic Sensei only.")

    serve_board(
        config,
        create_board(config.kanban),
        sensei=SenseiAgent(config, llm=llm),
        port=args.port,
    )


if __name__ == "__main__":
    main()

"""Append-only event log shared by the graph, exception handler, and reflection agent.

Every run, node completion, and exception is recorded as one JSON line. The
Reflection Agent computes SQDIP metrics from this log, so the whole improvement
loop works from a single, inspectable source of truth.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


class RunLog:
    def __init__(self, path: str = "kaizen_runlog.jsonl"):
        self.path = Path(path)

    def record(self, event_type: str, **data: Any) -> Dict[str, Any]:
        event = {
            "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "type": event_type,
            **data,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str) + "\n")
        return event

    def events(self, day: Optional[_dt.date] = None) -> List[Dict[str, Any]]:
        """All events, optionally filtered to a single (UTC) day."""
        result = []
        for event in self._iter():
            if day is not None:
                ts = event.get("timestamp", "")
                if not ts.startswith(day.isoformat()):
                    continue
            result.append(event)
        return result

    def _iter(self) -> Iterator[Dict[str, Any]]:
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue

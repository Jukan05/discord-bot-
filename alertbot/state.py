from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

from .common import now_kst

KEEP_DAYS = 14


class State:
    """이미 보낸 항목 ID와 DART 조회 위치(접수번호 워터마크)를 저장."""

    def __init__(self, path: str):
        self.path = path
        self.exists = os.path.exists(path)
        data = {}
        if self.exists:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        self.seen: dict = data.get("seen", {})
        self.dart_watermark: str = data.get("dart_watermark", "")
        self.dart_floor: str = data.get("dart_floor", "")

    def is_seen(self, item_id: str) -> bool:
        return item_id in self.seen

    def mark(self, item_id: str) -> None:
        self.seen.setdefault(item_id, now_kst().isoformat(timespec="seconds"))

    def save(self) -> None:
        cutoff = now_kst() - timedelta(days=KEEP_DAYS)
        self.seen = {
            k: v for k, v in self.seen.items() if datetime.fromisoformat(v) >= cutoff
        }
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"dart_watermark": self.dart_watermark, "dart_floor": self.dart_floor, "seen": self.seen},
                f,
                ensure_ascii=False,
                indent=1,
                sort_keys=True,
            )
            f.write("\n")
        os.replace(tmp, self.path)
        self.exists = True

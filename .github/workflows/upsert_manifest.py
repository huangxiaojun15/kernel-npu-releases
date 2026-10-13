#!/usr/bin/env python3
"""把一份新清单 upsert 进 gh-pages 上的 manifests/manifests.json。

所有清单都放在同一个文件里：``{"manifests": [...]}``。
按 image（完整 tag）去重：同一个 tag 再次发布就替换旧记录，新 tag 追加；
整体按 generated_at 倒序，保证看板左边最新在上面。

不做自动清理 —— 想删旧记录就直接改 manifests.json（改完看板就变）。

用法：
    python3 upsert_manifest.py <manifests.json> <new_manifest.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 2:
        print(
            f"usage: {Path(sys.argv[0]).name} <manifests.json> <new_manifest.json>",
            file=sys.stderr,
        )
        return 2

    out_path = Path(args[0])
    new_path = Path(args[1])

    existing = []
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8")).get("manifests", [])
        except (json.JSONDecodeError, OSError):
            existing = []

    new = json.loads(new_path.read_text(encoding="utf-8"))

    # 必须是「单条清单」。整包数据（{"manifests": [...]}）传进来时要立刻报错，
    # 否则会被当成一条记录塞进去，看板数据就脏了。
    if not isinstance(new, dict) or not new.get("image"):
        print(
            f"::error::{new_path} 不是单条清单（缺少 image 字段）",
            file=sys.stderr,
        )
        return 1

    key = new["image"]

    # 同 tag 覆盖，新 tag 追加
    existing = [m for m in existing if (m.get("image") or "") != key]
    existing.append(new)

    # 最新的排在最前面
    existing.sort(key=lambda m: m.get("generated_at") or "", reverse=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"manifests": existing}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"manifests.json: {len(existing)} 条记录 -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

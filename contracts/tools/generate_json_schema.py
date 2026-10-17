#!/usr/bin/env python
"""生成契约 JSON Schema 产物到 contracts/generated/json/（必须入库）。

用法（仓库根）：make -C contracts gen
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lcode_contracts.schema_export import write_all  # noqa: E402


def main() -> int:
    written = write_all()
    print(f"generated {len(written)} schema files under contracts/generated/json/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

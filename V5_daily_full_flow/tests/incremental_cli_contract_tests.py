from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "components" / "incremental"))

import incremental_engine


def main() -> None:
    parser = incremental_engine.build_parser()
    args = parser.parse_args(
        [
            "--input",
            "orders.json",
            "--state-dir",
            "state",
            "--batch-complete",
            "--excel-builder",
            "build_current_orders_excel.py",
        ]
    )
    assert args.excel_builder == "build_current_orders_excel.py"
    assert not hasattr(args, "node")

    worker = (ROOT / "v5_worker.ps1").read_text(encoding="utf-8-sig")
    assert '"--node"' not in worker
    assert '"--excel-builder", $ExcelBuilder' in worker
    print("Incremental CLI contract tests passed: 3/3")


if __name__ == "__main__":
    main()

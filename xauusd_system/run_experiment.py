"""
run_experiment.py — YAML-driven wrapper around backtest_runner.py.

Usage:
  python run_experiment.py exp-002

Reads experiments/exp-002.yaml, runs the backtest with the specified parameters
(including strategy_overrides), and saves results to results/exp-002.json.
Prints a one-line summary on completion.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# experiments/ and results/ live at the project root.
# This script may be at the root or one level inside (xauusd_system/).
def _find_root() -> Path:
    if (_HERE / "experiments").is_dir():
        return _HERE
    if (_HERE.parent / "experiments").is_dir():
        return _HERE.parent
    raise RuntimeError(f"Cannot locate experiments/ from {_HERE}")

_ROOT = _find_root()

# Lazy import so we don't need PyYAML as a hard dep — stdlib tomllib is 3.11+
# but pyyaml is the standard for YAML. Fall back to a minimal inline parser
# for simple flat configs if pyyaml is absent (unlikely in this venv).
try:
    import yaml as _yaml
    def _load_yaml(path: Path) -> dict:
        with path.open() as fh:
            return _yaml.safe_load(fh)
except ImportError:
    def _load_yaml(path: Path) -> dict:  # type: ignore[misc]
        """Minimal YAML loader for simple flat/nested configs (stdlib only)."""
        import re
        lines = path.read_text().splitlines()
        result: dict = {}
        current_key: str | None = None
        current_dict: dict = result
        for line in lines:
            if not line.strip() or line.strip().startswith("#"):
                continue
            if line.startswith("  ") and current_key and isinstance(result.get(current_key), dict):
                inner = line.strip()
                if ":" in inner:
                    k, _, v = inner.partition(":")
                    v = v.strip().strip('"')
                    try:
                        v_parsed: object = int(v)
                    except ValueError:
                        try:
                            v_parsed = float(v)
                        except ValueError:
                            v_parsed = v if v else None
                    result[current_key][k.strip()] = v_parsed
            elif ":" in line:
                k, _, v = line.partition(":")
                v = v.strip().strip('"')
                if v == "":
                    result[k.strip()] = {}
                    current_key = k.strip()
                else:
                    try:
                        v_parsed = int(v)
                    except ValueError:
                        try:
                            v_parsed = float(v)
                        except ValueError:
                            v_parsed = v if v else None
                    result[k.strip()] = v_parsed
                    current_key = k.strip()
        return result


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)


def _load_config(exp_id: str) -> dict:
    """Load and validate the YAML config for an experiment."""
    yaml_path = _ROOT / "experiments" / f"{exp_id}.yaml"
    if not yaml_path.exists():
        print(f"ERROR: config not found: {yaml_path}", file=sys.stderr)
        sys.exit(1)
    config = _load_yaml(yaml_path)
    for required in ("id", "profile", "from", "to"):
        if required not in config:
            print(f"ERROR: missing required field '{required}' in {yaml_path}", file=sys.stderr)
            sys.exit(1)
    return config


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python run_experiment.py <exp-id>", file=sys.stderr)
        print("  e.g. python run_experiment.py exp-002", file=sys.stderr)
        sys.exit(1)

    exp_id = sys.argv[1].lower()
    if not exp_id.startswith("exp-"):
        exp_id = f"exp-{exp_id}"

    config = _load_config(exp_id)

    profile_name  = config.get("profile", "intraday")
    start         = str(config["from"])
    end           = str(config["to"])
    equity        = float(config.get("equity", 100_000.0))
    overrides     = config.get("strategy_overrides") or {}
    research_mode = bool(config.get("research_mode", False))
    risk_overrides = config.get("risk_overrides") or {}
    out_path      = _ROOT / "results" / f"{exp_id}.json"

    logger.info("Running %s: %s", exp_id.upper(), config.get("description", ""))

    # Import here so the sys.path insertion in backtest_runner fires first
    sys.path.insert(0, str(_HERE))
    from backtest_runner import run_backtest  # noqa: PLC0415

    result = run_backtest(
        profile_name       = profile_name,
        start              = start,
        end                = end,
        equity             = equity,
        out_path           = str(out_path),
        strategy_overrides = overrides,
        research_mode      = research_mode,
        risk_overrides     = risk_overrides or None,
    )

    stats = result.get("stats", {})
    pf    = stats.get("profit_factor") or 0.0
    n     = stats.get("n_trades", 0)
    net   = stats.get("net_pnl") or 0.0
    sign  = "+" if net >= 0 else ""

    print(
        f"\n{exp_id.upper()} complete — "
        f"PF: {pf:.2f} | "
        f"Trades: {n} | "
        f"Net P&L: {sign}${net:,.0f} | "
        f"Results: results/{exp_id}.json"
    )


if __name__ == "__main__":
    main()

# experiments/

Each file is a YAML config for one experiment run.

Naming: `exp-NNN.yaml` where NNN is zero-padded (exp-001.yaml, exp-002.yaml, ...).

Run an experiment:
  python run_experiment.py exp-002

The runner reads the YAML, calls the backtest engine with the specified parameters,
and saves results to results/exp-NNN.json.

## YAML schema

```yaml
id: exp-NNN
description: "Short description of what this experiment tests"
date: YYYY-MM-DD
profile: intraday   # or swing
from: YYYY-MM-DD
to: YYYY-MM-DD
strategy_overrides:
  donchian_period: 20   # any StrategyProfile field
notes: "Optional free-text notes"
```

`strategy_overrides` keys must match fields on `StrategyProfile` in
`xauusd_system/src/core/config.py`. Unknown keys are logged as warnings and skipped.

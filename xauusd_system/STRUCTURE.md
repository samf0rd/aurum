xauusd_system/
├── Dockerfile                         # Multi-stage: base → builder → test → production
├── docker-compose.yml                 # trader + prometheus + grafana + redis
├── pyproject.toml                     # Dependencies, build, pytest, mypy, ruff
├── .env.example                       # Template — copy to .env, never commit .env
│
├── src/
│   ├── main.py                        # Entrypoint — dependency injection only
│   │
│   ├── core/
│   │   ├── __init__.py
│   │   └── interfaces.py              # ALL abstract base classes and data contracts
│   │                                  # Bar, Tick, Signal, Order, Position, RiskState
│   │                                  # IDataFeed, ISignalGenerator, IRegimeDetector
│   │                                  # IRiskEngine, IOrderManager, IBrokerAdapter
│   │                                  # IEventBus, IAlertService
│   │
│   ├── strategy/
│   │   ├── __init__.py
│   │   └── signal_generator.py        # Pure strategy logic (no I/O, no side effects)
│   │                                  # RegimeDetector: Rules 1a, 1b, 1c
│   │                                  # DonchianBreakoutSignalGenerator: Rules 2, 3a, 3b
│   │                                  # Indicator helpers: sma, atr, adx, donchian, vol_ratio
│   │
│   ├── risk/
│   │   ├── __init__.py
│   │   └── engine.py                  # ALL money management rules
│   │                                  # Rule 4: ATR stop placement
│   │                                  # Rule 6: position sizing
│   │                                  # Rule 7: daily loss limit (2%)
│   │                                  # Rule 8: weekly loss limit (5%)
│   │                                  # Rule 9: 1% risk per trade
│   │                                  # Rule 10a: drawdown circuit breaker (15%)
│   │                                  # Rule 10b: gap-caution de-risking
│   │                                  # Rule 10c: spread gate (3× median)
│   │
│   ├── orchestrator/
│   │   ├── __init__.py
│   │   └── engine.py                  # Central run loop — sequences components
│   │                                  # TradingOrchestrator.process_bar()
│   │                                  # Daily bar loop, tick monitor, PnL reset
│   │                                  # OandaBrokerAdapter (lives here as a stub stub)
│   │
│   ├── data/
│   │   ├── __init__.py
│   │   └── oanda_feed.py              # IDataFeed implementation for OANDA v20
│   │                                  # fetch_bars(), stream_ticks(), latest_bar()
│   │
│   ├── orders/
│   │   ├── __init__.py
│   │   └── manager.py                 # IOrderManager implementation
│   │                                  # In-memory position ledger
│   │                                  # on_fill() → Position creation + stop registration
│   │
│   ├── broker/
│   │   ├── __init__.py
│   │   └── oanda_adapter.py           # IBrokerAdapter for OANDA (full implementation)
│   │                                  # Thin REST/WS translation only — no business logic
│   │
│   └── infrastructure/
│       ├── __init__.py
│       └── services.py                # InProcessEventBus, TelegramAlertService
│                                      # EmailAlertService, configure_logging (JSON)
│                                      # MetricsCollector (Prometheus gauges/counters)
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py                    # Shared fixtures
│   └── test_all.py                    # Full unit + integration test suite
│                                      # TestIndicators, TestRegimeDetector
│                                      # TestSignalGenerator, TestRiskEngine
│                                      # TestEventBus, TestOrchestratorPipeline
│
└── config/
    ├── prometheus.yml                 # Scrape config for the trader /metrics endpoint
    └── grafana/
        ├── datasources/
        │   └── prometheus.yml         # Auto-provision Prometheus datasource
        └── dashboards/
            └── trading.json           # Pre-built dashboard: equity, drawdown,
                                       # signals, circuit breaker, spread, positions

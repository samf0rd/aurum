xauusd_system/
├── Dockerfile                         # Multi-stage: base → builder → test → production
│                                      # ENV PYTHONPATH=/app/src; EXPOSE 8080
├── docker-compose.yml                 # Single aurum service only
│                                      # Port 8080 bound to 127.0.0.1 (nginx proxies)
│                                      # Volumes: ./data → /app/data, ./logs → /app/logs
├── pyproject.toml                     # Dependencies, build, pytest, mypy, ruff
│                                      # requests, numpy, fastapi, uvicorn, prometheus-client
│                                      # aiohttp, python-dotenv, websockets
├── .env.example                       # Template — copy to .env, never commit .env
│                                      # TWELVE_DATA_API_KEY, PAPER_MODE, PAPER_EQUITY
│                                      # STRATEGY_PROFILE, PRICE_TICK_INTERVAL
├── CLAUDE.md                          # Instructions for AI-assisted development tasks
│
├── src/
│   ├── main.py                        # Entrypoint — dependency injection only
│   │                                  # Validates TWELVE_DATA_API_KEY at startup
│   │                                  # Wires: TwelveDataFeed → Orchestrator → Dashboard
│   │                                  # Always uses PaperBrokerAdapter (PAPER_MODE=true)
│   │
│   ├── core/
│   │   ├── __init__.py
│   │   ├── interfaces.py              # ALL abstract base classes and data contracts
│   │   │                              # Bar, Tick, Signal, Order, Position, RiskState
│   │   │                              # IDataFeed, ISignalGenerator, IRegimeDetector
│   │   │                              # IRiskEngine, IOrderManager, IBrokerAdapter
│   │   │                              # IEventBus, IAlertService
│   │   └── config.py                  # StrategyProfile frozen dataclass
│   │                                  # SWING: H1, sma=200, donchian=20/10, atr=14
│   │                                  # INTRADAY: M15, same lookbacks, faster clock
│   │                                  # ACTIVE_PROFILE = env STRATEGY_PROFILE (default: intraday)
│   │
│   ├── strategy/
│   │   ├── __init__.py
│   │   └── signal_generator.py        # Pure strategy logic (no I/O, no side effects)
│   │                                  # RegimeDetector: Rules 1a, 1b, 1c
│   │                                  # DonchianBreakoutSignalGenerator: Rules 2, 3a, 3b
│   │                                  # All class constants sourced from ACTIVE_PROFILE
│   │                                  # Indicator helpers: sma, atr, adx, donchian, vol_ratio
│   │
│   ├── risk/
│   │   ├── __init__.py                # RiskEngineAdapter (wraps RiskEngine for orchestrator)
│   │   ├── engine.py                  # ALL money management rules
│   │   │                              # Rule 4: ATR stop placement
│   │   │                              # Rule 6: position sizing (equity × 1% ÷ stop)
│   │   │                              # Rule 7: daily loss limit (2%)
│   │   │                              # Rule 8: weekly loss limit (5%)
│   │   │                              # Rule 9: 1% risk per trade
│   │   │                              # Rule 10a: drawdown circuit breaker (15%)
│   │   │                              # Rule 10b: gap-caution de-risking
│   │   │                              # Rule 10c: spread gate (3× median)
│   │   └── models.py                  # RiskConfig, RiskState
│   │
│   ├── orchestrator/
│   │   ├── __init__.py
│   │   └── engine.py                  # TradingOrchestrator — central run loop
│   │                                  # _bar_loop(interval=60): polls for new M15 bars,
│   │                                  #   runs full pipeline on new timestamp
│   │                                  # _price_loop(interval=PRICE_TICK_INTERVAL): fetches
│   │                                  #   live price, updates dashboard + forming candle
│   │                                  # _daily_reset_loop: resets daily P&L at 00:05 UTC
│   │                                  # _check_intrabar_stop: intrabar stop circuit breaker
│   │
│   ├── data/
│   │   ├── __init__.py
│   │   └── twelvedata_feed.py         # IDataFeed implementation for Twelve Data API
│   │                                  # get_bars(granularity, count) → list[Bar]
│   │                                  #   Maps M15→"15min", H1→"1h", etc.
│   │                                  #   Calls GET /time_series?symbol=XAU/USD
│   │                                  # get_latest_tick() → {bid, ask, timestamp}
│   │                                  #   Calls GET /price?symbol=XAU/USD
│   │                                  #   Synthesises bid/ask with fixed $0.30 spread
│   │                                  # Uses requests (sync) + asyncio.to_thread()
│   │                                  # Rate limit: ~385 calls/day on free tier defaults
│   │
│   ├── orders/
│   │   ├── __init__.py
│   │   └── manager.py                 # DefaultOrderManager — IOrderManager implementation
│   │                                  # In-memory position ledger
│   │                                  # on_fill() → Position creation + stop registration
│   │
│   ├── broker/
│   │   └── __init__.py                # Package stub only — no active adapter here
│   │                                  # (live broker adapters live in execution/brokers/)
│   │
│   ├── execution/
│   │   ├── __init__.py
│   │   ├── engine.py                  # ExecutionEngine: submit, retry, reconcile
│   │   │                              # Exponential backoff, partial fill accumulation
│   │   │                              # Position reconciliation every 30s
│   │   ├── models.py                  # ExecutionOrder, Fill, ExecutionConfig
│   │   ├── logging_config.py          # Structured JSON logging helpers
│   │   ├── brokers/
│   │   │   ├── base.py                # IBrokerAdapter + RetryingBrokerAdapter
│   │   │   └── oanda.py               # OandaAdapter (present but not used in paper mode)
│   │   ├── reconciliation/
│   │   │   └── reconciler.py          # Order + position reconciliation loop
│   │   └── recovery/
│   │       └── network.py             # NetworkRecoveryManager: heartbeat + reconnect
│   │
│   ├── paper/
│   │   ├── __init__.py
│   │   └── paper_broker.py            # PaperBrokerAdapter — IBrokerAdapter implementation
│   │                                  # Simulates fills with realistic slippage
│   │                                  # No external broker account required
│   │                                  # Always active (PAPER_MODE=true)
│   │
│   ├── infrastructure/
│   │   ├── __init__.py
│   │   └── services.py                # InProcessEventBus, TelegramAlertService
│   │                                  # MetricsCollector (Prometheus gauges/counters)
│   │
│   └── dashboard/
│       ├── __init__.py (implicit)
│       ├── api.py                     # FastAPI app: build_app(state, data_feed) → App
│       │                              # REST: /api/bars /api/candles /api/price /api/equity
│       │                              #       /api/trades /api/position /api/stats
│       │                              #       /api/indicators /api/regime /api/bars
│       │                              # WebSocket: /ws/live — broadcasts every 2s:
│       │                              #   {type, profile, current_price, candle, indicators,
│       │                              #    proximity, regime, adx, position, equity, stats}
│       ├── state.py                   # SystemState singleton
│       │                              # equity_curve, trades, position, regime, adx
│       │                              # forming_candle, last_indicators, last_signal_*
│       │                              # Written by orchestrator; read by dashboard API
│       └── static/
│           └── index.html             # Vanilla JS single-page dashboard
│                                      # Lightweight Charts v5 (candlestick + equity sparkline)
│                                      # Connects to /ws/live WebSocket on load
│                                      # No build step required — served as static file
│
├── data/                              # Persisted data — volume-mounted in Docker (gitignored)
│   └── trading.db                    # SQLite database — survives container rebuilds
│
├── logs/                              # Runtime logs (gitignored)
│   ├── equity_curve.jsonl             # Equity snapshots (appended on each bar close)
│   ├── main_stdout.txt                # Application structured JSON log
│   └── main_stderr.txt                # Error output
│
├── tests/
│   ├── __init__.py
│   └── test_all.py                    # Unit + integration test suite
│                                      # TestIndicators, TestRegimeDetector
│                                      # TestSignalGenerator, TestRiskEngine
│                                      # TestEventBus, TestOrchestratorPipeline
│
└── config/
    ├── prometheus.yml                 # Scrape config for /metrics endpoint (port 8000)
    └── grafana/
        ├── datasources/
        │   └── prometheus.yml         # Auto-provision Prometheus datasource
        └── dashboards/
            └── __init__.py            # Placeholder (dashboard JSON not yet created)

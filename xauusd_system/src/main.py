"""
src/main.py  — system entrypoint, dependency injection only.
Set PAPER_MODE=true to run without live OANDA credentials.
"""
from __future__ import annotations
import asyncio, json, logging, os, sys, time
from decimal import Decimal


def _setup_logging() -> None:
    class _J(logging.Formatter):
        def format(self, r):
            d = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r.created)),
                 "level": r.levelname, "logger": r.name, "msg": r.getMessage()}
            if r.exc_info:
                d["exc"] = self.formatException(r.exc_info)
            return json.dumps(d)
    root = logging.getLogger()
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(_J())
    root.addHandler(h)
    lf = os.environ.get("LOG_FILE")
    if lf:
        fh = logging.FileHandler(lf, encoding="utf-8")
        fh.setFormatter(_J())
        root.addHandler(fh)

_setup_logging()
logger = logging.getLogger(__name__)


def _cfg() -> dict:
    paper = os.environ.get("PAPER_MODE", "true").lower() in ("1", "true", "yes")
    if not paper:
        missing = [k for k in ("OANDA_ACCOUNT_ID", "OANDA_API_TOKEN") if not os.environ.get(k)]
        if missing:
            logger.error("Missing env vars: %s", missing); sys.exit(1)
    return {
        "paper":          paper,
        "account_id":     os.environ.get("OANDA_ACCOUNT_ID", ""),
        "api_token":      os.environ.get("OANDA_API_TOKEN", ""),
        "equity":         Decimal(os.environ.get("INITIAL_EQUITY", "10000")),
        "risk_pct":       float(os.environ.get("RISK_FRACTION", "0.01")),
        "max_daily":      float(os.environ.get("MAX_DAILY_LOSS", "0.02")),
        "max_weekly":     float(os.environ.get("MAX_WEEKLY_LOSS", "0.05")),
        "max_dd":         float(os.environ.get("MAX_DRAWDOWN", "0.15")),
        "lookback":       int(os.environ.get("LOOKBACK_BARS", "250")),
        "metrics_port":   int(os.environ.get("METRICS_PORT", "8000")),
        "dashboard_port": int(os.environ.get("DASHBOARD_PORT", "8080")),
    }


async def main() -> None:
    cfg = _cfg()
    logger.info("Starting | paper=%s equity=%s", cfg["paper"], cfg["equity"])

    from infrastructure.services import InProcessEventBus, TelegramAlertService
    bus   = InProcessEventBus()
    alert = TelegramAlertService()

    # Risk engine
    from risk.engine import RiskEngine
    from risk.models import RiskConfig
    risk = RiskEngine(
        initial_equity=cfg["equity"],
        config=RiskConfig(
            risk_pct_normal=Decimal(str(cfg["risk_pct"])),
            daily_loss_limit_pct=Decimal(str(cfg["max_daily"])),
            weekly_loss_limit_pct=Decimal(str(cfg["max_weekly"])),
            max_drawdown_pct=Decimal(str(cfg["max_dd"])),
        ),
    )

    # Wrap with adapter so orchestrator's IRiskEngine interface is satisfied
    from risk import RiskEngineAdapter
    risk = RiskEngineAdapter(risk)

    # Broker
    if cfg["paper"]:
        from paper.paper_broker import PaperBrokerAdapter
        broker = PaperBrokerAdapter(initial_equity=cfg["equity"])
        logger.info("Broker: PAPER")
    else:
        from execution.brokers.oanda import OandaAdapter
        broker = OandaAdapter(account_id=cfg["account_id"], api_token=cfg["api_token"])
        logger.info("Broker: LIVE OANDA")

    # Execution engine — risk coupled via callback only (no import cycle)
    from execution.engine import ExecutionEngine
    from execution.models  import ExecutionConfig

    async def _on_fill(fill) -> None:
        try:
            from decimal import Decimal as D
            pnl = D(str(getattr(fill, "realized_pnl", 0)))
            risk.record_trade_result(pnl=pnl)
        except Exception as e:
            logger.warning("on_fill error: %s", e)

    async def _on_reject(order, reason: str) -> None:
        logger.warning("Rejected | %s", reason)

    async def _on_alert(level: str, msg: str) -> None:
        logger.error("ExecAlert | %s %s", level, msg)

    exec_eng = ExecutionEngine(
        broker=broker, config=ExecutionConfig(),
        on_fill=_on_fill, on_reject=_on_reject, on_alert=_on_alert,
    )

    from strategy.signal_generator import DonchianBreakoutSignalGenerator, RegimeDetector
    from data.oanda_feed import OandaDataFeed
    from orders.manager import DefaultOrderManager
    from dashboard.state import SystemState

    regime    = RegimeDetector()
    signals   = DonchianBreakoutSignalGenerator()
    data_feed = OandaDataFeed(broker)
    order_mgr = DefaultOrderManager(broker=broker, risk_engine=risk, event_bus=bus)
    state     = SystemState(initial_equity=cfg["equity"])

    try:
        from infrastructure.services import MetricsCollector
        MetricsCollector(event_bus=bus, alert_service=alert,
                         broker_adapter=broker, metrics_port=cfg["metrics_port"])
    except Exception as e:
        logger.warning("Metrics disabled: %s", e)

    await exec_eng.start()

    dash_task = None
    try:
        import uvicorn
        from dashboard.api import build_app
        server = uvicorn.Server(uvicorn.Config(
            build_app(state, data_feed), host="0.0.0.0", port=cfg["dashboard_port"],
            log_level="warning", access_log=False,
        ))
        dash_task = asyncio.create_task(server.serve())
        logger.info("Dashboard on http://localhost:%s", cfg["dashboard_port"])
    except ImportError:
        logger.warning("Dashboard disabled — run: pip install uvicorn fastapi websockets")

    from orchestrator.engine import TradingOrchestrator
    orch = TradingOrchestrator(
        data_feed=data_feed, regime_detector=regime, signal_generator=signals,
        risk_engine=risk, order_manager=order_mgr, broker_adapter=broker,
        alert_service=alert, event_bus=bus, system_state=state,
        initial_equity=cfg["equity"], lookback_bars=cfg["lookback"],
    )

    await alert.send("INFO", f"Ready | paper={cfg['paper']}")
    try:
        await orch.run()
    finally:
        await exec_eng.stop()
        if dash_task:
            dash_task.cancel()

if __name__ == "__main__":
    asyncio.run(main())

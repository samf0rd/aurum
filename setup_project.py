#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup_project.py  — XAUUSD Algo Bot Bootstrap
===============================================
Place this file in algo_bot/ alongside the zip files, then run:

    py setup_project.py

It finds zips automatically regardless of whether they have spaces or
underscores in the name, extracts xauusd_system if needed, installs all
engine files, and generates the dashboard + paper broker.
"""
from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

# ── helpers ───────────────────────────────────────────────────────────────────
def ok(m):   print(f"  + {m}")
def info(m): print(f"  > {m}")
def warn(m): print(f"  ! {m}")
def die(m):  print(f"  x {m}"); sys.exit(1)

def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

def touch(p: Path) -> None:
    if not p.exists():
        write(p, "")

# ── locate root ───────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
print(f"\n=== XAUUSD Bootstrap ===\nWorking in: {ROOT}\n")

# ── find a zip by fuzzy name (handles spaces / underscores / case) ─────────────
def find_zip(*keywords: str) -> Path | None:
    """Return first .zip in ROOT whose name contains ALL keywords (case-insensitive)."""
    for p in ROOT.glob("*.zip"):
        name = p.stem.lower().replace(" ", "_").replace("-", "_")
        if all(k.lower() in name for k in keywords):
            return p
    return None

def load_zip(path: Path) -> dict[str, bytes]:
    """Return {bare_filename: bytes} for every file in the zip."""
    out = {}
    with zipfile.ZipFile(path) as zf:
        for entry in zf.namelist():
            if entry.endswith("/"):
                continue
            out[Path(entry).name] = zf.read(entry)
    return out

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Extract xauusd_system if the folder doesn't exist yet
# ─────────────────────────────────────────────────────────────────────────────
XAUUSD = ROOT / "xauusd_system"
SRC    = XAUUSD / "src"

if not XAUUSD.exists():
    sys_zip = find_zip("xauusd", "system") or find_zip("trading", "system") or find_zip("xauusd")
    if not sys_zip:
        die(
            "Cannot find xauusd_system/ folder or a matching zip.\n"
            f"  Zips found: {[p.name for p in ROOT.glob('*.zip')]}\n"
            "  Make sure xauusd_system.zip is in the same folder as this script."
        )
    info(f"Extracting {sys_zip.name} ...")
    with zipfile.ZipFile(sys_zip) as zf:
        zf.extractall(ROOT)
    if not XAUUSD.exists():
        die(f"Extraction done but xauusd_system/ still not found. "
            f"Check that {sys_zip.name} contains a top-level xauusd_system/ folder.")
    ok(f"xauusd_system/ extracted from {sys_zip.name}")
else:
    ok("xauusd_system/ already exists")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Load engine zips
# ─────────────────────────────────────────────────────────────────────────────
exec_zip = find_zip("execution")
risk_zip = find_zip("risk")

if not exec_zip:
    warn("execution engine zip not found — engine files will be skipped")
    exec_files: dict[str, bytes] = {}
else:
    info(f"Reading {exec_zip.name} ...")
    exec_files = load_zip(exec_zip)
    ok(f"Loaded {len(exec_files)} files from {exec_zip.name}")

if not risk_zip:
    warn("risk engine zip not found — risk files will be skipped")
    risk_files: dict[str, bytes] = {}
else:
    info(f"Reading {risk_zip.name} ...")
    risk_files = load_zip(risk_zip)
    ok(f"Loaded {len(risk_files)} files from {risk_zip.name}")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Build full directory tree
# ─────────────────────────────────────────────────────────────────────────────
info("Creating directory tree ...")
for d in [
    SRC / "core",
    SRC / "execution" / "brokers",
    SRC / "execution" / "reconciliation",
    SRC / "execution" / "recovery",
    SRC / "risk",
    SRC / "strategy",
    SRC / "orders",
    SRC / "data",
    SRC / "broker",
    SRC / "paper",
    SRC / "dashboard" / "static",
    SRC / "infrastructure",
    SRC / "orchestrator",
    XAUUSD / "logs",
]:
    d.mkdir(parents=True, exist_ok=True)
    if "logs" not in str(d):
        touch(d / "__init__.py")
ok("Directory tree ready")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — Install execution engine files into correct subfolders
#
#  Flat zip layout  →  destination inside src/execution/
#  engine.py        →  engine.py
#  models.py        →  models.py
#  base.py          →  brokers/base.py
#  oanda.py         →  brokers/oanda.py
#  reconciler.py    →  reconciliation/reconciler.py
#  network.py       →  recovery/network.py
#  logging_config.py→  logging_config.py
# ─────────────────────────────────────────────────────────────────────────────
if exec_files:
    info("Installing execution engine ...")
    EXEC_DEST = SRC / "execution"
    FILE_MAP = {
        "engine.py":         EXEC_DEST / "engine.py",
        "models.py":         EXEC_DEST / "models.py",
        "base.py":           EXEC_DEST / "brokers" / "base.py",
        "oanda.py":          EXEC_DEST / "brokers" / "oanda.py",
        "reconciler.py":     EXEC_DEST / "reconciliation" / "reconciler.py",
        "network.py":        EXEC_DEST / "recovery" / "network.py",
        "logging_config.py": EXEC_DEST / "logging_config.py",
    }
    for fname, dest in FILE_MAP.items():
        if fname in exec_files:
            dest.write_bytes(exec_files[fname])
            ok(f"  {fname} -> {dest.relative_to(ROOT)}")
        else:
            warn(f"  {fname} not found in zip")

    write(EXEC_DEST / "__init__.py", """\
from .engine import ExecutionEngine
from .models import (
    ExecutionConfig, ExecutionOrder, ExecutionPosition,
    Fill, FillAccumulator, OrderSide, OrderStatus,
    OrderType, RetryConfig, TimeInForce,
)
__all__ = [
    "ExecutionEngine", "ExecutionConfig", "ExecutionOrder",
    "ExecutionPosition", "Fill", "FillAccumulator",
    "OrderSide", "OrderStatus", "OrderType", "RetryConfig", "TimeInForce",
]
""")
    ok("  execution/__init__.py written")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — Install risk engine files
# ─────────────────────────────────────────────────────────────────────────────
if risk_files:
    info("Installing risk engine ...")
    RISK_DEST = SRC / "risk"
    for fname in ("engine.py", "models.py", "__init__.py"):
        if fname in risk_files:
            (RISK_DEST / fname).write_bytes(risk_files[fname])
            ok(f"  {fname} -> {RISK_DEST.relative_to(ROOT) / fname}")
        else:
            warn(f"  {fname} not found in zip")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — Write src/main.py
# ─────────────────────────────────────────────────────────────────────────────
info("Writing src/main.py ...")
write(SRC / "main.py", '''\
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
            risk_pct=cfg["risk_pct"],
            max_daily_loss_pct=cfg["max_daily"],
            max_weekly_loss_pct=cfg["max_weekly"],
            max_drawdown_pct=cfg["max_dd"],
        ),
    )

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
            build_app(state), host="0.0.0.0", port=cfg["dashboard_port"],
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
''')
ok("src/main.py written")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 7 — Dashboard state, API, HTML
# ─────────────────────────────────────────────────────────────────────────────
info("Writing dashboard files ...")

write(SRC / "dashboard" / "state.py", '''\
from __future__ import annotations
import json, math, time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional


@dataclass
class TradeRecord:
    trade_id: str; symbol: str; side: str; size: float
    entry_price: float; exit_price: Optional[float]; pnl: Optional[float]
    opened_at: str; closed_at: Optional[str] = None


@dataclass
class PositionSnapshot:
    symbol: str; side: str; size: float
    entry_px: float; current_px: float; unrealized_pnl: float


class SystemState:
    def __init__(self, initial_equity: Decimal):
        self.initial_equity = float(initial_equity)
        self.equity_curve: list[tuple[str, float]] = [
            (time.strftime("%Y-%m-%dT%H:%M:%SZ"), float(initial_equity))]
        self.trades: list[TradeRecord] = []
        self.position: Optional[PositionSnapshot] = None
        self.regime = "NEUTRAL"; self.adx = 0.0
        self.started_at = time.time()
        Path("logs").mkdir(exist_ok=True)

    def record_equity(self, equity: float) -> None:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.equity_curve.append((ts, round(equity, 2)))
        with open("logs/equity_curve.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ts, "equity": equity}) + "\\n")

    def record_trade(self, t: TradeRecord) -> None:
        self.trades.append(t)
        with open("logs/paper_trades.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(t.__dict__) + "\\n")

    def update_position(self, p: Optional[PositionSnapshot]) -> None:
        self.position = p

    def update_regime(self, regime: str, adx: float) -> None:
        self.regime = regime; self.adx = adx

    def get_stats(self) -> dict[str, Any]:
        eq = [e for _, e in self.equity_curve]
        closed = [t for t in self.trades if t.pnl is not None]
        if len(eq) < 2:
            return {"sharpe": 0, "max_drawdown": 0, "win_rate": 0,
                    "total_trades": 0, "total_pnl": 0,
                    "uptime_s": round(time.time() - self.started_at)}
        rets = [eq[i]/eq[i-1]-1 for i in range(1, len(eq))]
        mu = sum(rets)/len(rets)
        var = sum((r-mu)**2 for r in rets)/max(len(rets)-1, 1)
        sharpe = (mu/math.sqrt(var)*math.sqrt(252)) if var > 0 else 0
        peak = eq[0]; mdd = 0.0
        for e in eq:
            if e > peak: peak = e
            mdd = max(mdd, (peak-e)/peak)
        wins = sum(1 for t in closed if (t.pnl or 0) > 0)
        return {
            "sharpe": round(sharpe, 3),
            "max_drawdown": round(mdd*100, 2),
            "win_rate": round(wins/len(closed)*100, 1) if closed else 0,
            "total_trades": len(closed),
            "total_pnl": round(sum(t.pnl or 0 for t in closed), 2),
            "uptime_s": round(time.time() - self.started_at),
        }
''')

write(SRC / "dashboard" / "api.py", '''\
from __future__ import annotations
import asyncio, json, time
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from .state import SystemState

_state: Optional[SystemState] = None

def build_app(state: SystemState) -> FastAPI:
    global _state; _state = state
    app = FastAPI(title="XAUUSD", docs_url=None, redoc_url=None)
    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static)), name="static")

    @app.get("/health")
    async def health(): return {"status": "ok", "uptime_s": round(time.time()-_state.started_at)}

    @app.get("/", response_class=HTMLResponse)
    async def root(): return FileResponse(str(static/"index.html"))

    @app.get("/api/equity")
    async def equity(): return {"curve": _state.equity_curve[-2000:]}

    @app.get("/api/trades")
    async def trades(): return {"trades": [t.__dict__ for t in _state.trades[-200:]]}

    @app.get("/api/stats")
    async def stats(): return _state.get_stats()

    @app.get("/api/position")
    async def position():
        p = _state.position; return p.__dict__ if p else None

    @app.get("/api/regime")
    async def regime(): return {"regime": _state.regime, "adx": round(_state.adx, 2)}

    @app.websocket("/ws/live")
    async def ws(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                await asyncio.sleep(2)
                await ws.send_text(json.dumps({
                    "type": "tick",
                    "equity": _state.equity_curve[-1] if _state.equity_curve else None,
                    "position": _state.position.__dict__ if _state.position else None,
                    "regime": _state.regime,
                    "stats": _state.get_stats(),
                }))
        except (WebSocketDisconnect, Exception): pass
    return app
''')
ok("dashboard/state.py and api.py written")

write(SRC / "dashboard" / "static" / "index.html", """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>XAUUSD Forward-Test</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#0f1117;--bg2:#1a1d27;--bg3:#22263a;--text:#e8e8f0;--muted:#8888a8;
--border:#2e3148;--gold:#f0b429;--green:#22c55e;--red:#ef4444}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:system-ui,sans-serif;font-size:14px}
header{background:var(--bg2);border-bottom:1px solid var(--border);padding:14px 24px;
display:flex;align-items:center;gap:14px}
h1{font-size:17px;font-weight:600;color:var(--gold)}
#dot{width:8px;height:8px;border-radius:50%;background:var(--muted);transition:background .4s}
#dot.on{background:var(--green);box-shadow:0 0 6px var(--green)}
#lbl,#upt{font-size:12px;color:var(--muted)}
#upt{margin-left:auto}
.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;padding:16px 24px 0}
.stat{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:14px}
.sl{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em}
.sv{font-size:22px;font-weight:700;margin-top:4px}
.layout{display:grid;grid-template-columns:1fr 310px;gap:14px;padding:14px 24px}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:14px}
.ct{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
margin-bottom:10px;display:flex;justify-content:space-between;align-items:center}
.badge{padding:3px 10px;border-radius:20px;font-size:12px;font-weight:600}
.BULL{background:rgba(34,197,94,.15);color:var(--green)}
.BEAR{background:rgba(239,68,68,.15);color:var(--red)}
.NEUTRAL{background:rgba(148,163,184,.12);color:#94a3b8}
.pd{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.pi{background:var(--bg3);border-radius:6px;padding:10px}
.pi .l{font-size:11px;color:var(--muted)}
.pi .v{font-size:15px;font-weight:600;margin-top:2px}
.tr{display:grid;grid-template-columns:52px 48px 60px 70px 1fr;gap:6px;
padding:7px 0;border-bottom:1px solid var(--border);font-size:12px;align-items:center}
.tr:last-child{border-bottom:none}
.th{color:var(--muted);font-size:11px;text-transform:uppercase}
.tl{background:rgba(34,197,94,.15);color:var(--green);padding:2px 6px;border-radius:4px}
.ts{background:rgba(239,68,68,.15);color:var(--red);padding:2px 6px;border-radius:4px}
.g{color:var(--green)}.r{color:var(--red)}
canvas{max-height:280px}
</style>
</head>
<body>
<header>
  <h1>&#9889; XAUUSD Forward-Test</h1>
  <div id="dot"></div><span id="lbl">Connecting...</span><span id="upt"></span>
</header>
<div class="stats">
  <div class="stat"><div class="sl">Equity</div><div class="sv" id="s-eq" style="color:var(--gold)">--</div></div>
  <div class="stat"><div class="sl">Sharpe</div><div class="sv" id="s-sh">--</div></div>
  <div class="stat"><div class="sl">Max DD</div><div class="sv r" id="s-dd">--</div></div>
  <div class="stat"><div class="sl">Win Rate</div><div class="sv" id="s-wr">--</div></div>
  <div class="stat"><div class="sl">Trades</div><div class="sv" id="s-tr">--</div></div>
</div>
<div class="layout">
  <div>
    <div class="card">
      <div class="ct">Equity Curve</div>
      <canvas id="ec"></canvas>
    </div>
    <div class="card">
      <div class="ct"><span>Recent Trades</span><span id="tpnl" style="font-size:13px"></span></div>
      <div class="tr th"><span>Time</span><span>Side</span><span>Size</span><span>P&L</span><span>Entry / Exit</span></div>
      <div id="tlist"></div>
    </div>
  </div>
  <div>
    <div class="card">
      <div class="ct"><span>Open Position</span><span id="rb" class="badge NEUTRAL">NEUTRAL</span></div>
      <div id="pos"><span style="color:var(--muted);font-size:13px">No open position</span></div>
    </div>
    <div class="card">
      <div class="ct">Trade P&L</div>
      <canvas id="pc"></canvas>
    </div>
  </div>
</div>
<script>
const $=id=>document.getElementById(id);
const fmtN=v=>v==null?'--':Number(v).toLocaleString(undefined,{maximumFractionDigits:2});
const fmt=v=>v==null?'--':(v>=0?'+':'')+Number(v).toFixed(2);
const t2s=s=>s?s.slice(11,16):'';
const eqC=new Chart($('ec').getContext('2d'),{type:'line',
  data:{labels:[],datasets:[{data:[],borderColor:'#f0b429',borderWidth:1.5,pointRadius:0,
    fill:{target:'origin',above:'rgba(240,180,41,.07)'},tension:.3}]},
  options:{responsive:true,plugins:{legend:{display:false}},
    scales:{x:{ticks:{color:'#555',maxTicksLimit:8},grid:{color:'rgba(255,255,255,.04)'}},
            y:{ticks:{color:'#555'},grid:{color:'rgba(255,255,255,.04)'}}}}});
const pC=new Chart($('pc').getContext('2d'),{type:'bar',
  data:{labels:[],datasets:[{data:[],backgroundColor:[],borderRadius:3}]},
  options:{responsive:true,plugins:{legend:{display:false}},
    scales:{x:{ticks:{color:'#555',maxTicksLimit:10},grid:{display:false}},
            y:{ticks:{color:'#555'},grid:{color:'rgba(255,255,255,.04)'}}}}});

function setEq(c){eqC.data.labels=c.map(([s])=>t2s(s));eqC.data.datasets[0].data=c.map(([,v])=>v);eqC.update('none');}
function setStats(s){
  const eq=s.total_pnl!=null?10000+(s.total_pnl||0):null;
  $('s-eq').textContent=eq?'$'+fmtN(eq):'--';
  $('s-sh').textContent=s.sharpe??'--';
  $('s-dd').textContent=s.max_drawdown!=null?s.max_drawdown+'%':'--';
  $('s-wr').textContent=s.win_rate!=null?s.win_rate+'%':'--';
  $('s-tr').textContent=s.total_trades??'--';
  if(s.uptime_s){const h=Math.floor(s.uptime_s/3600),m=Math.floor(s.uptime_s%3600/60);$('upt').textContent=h+'h '+m+'m';}
  const el=$('tpnl'),p=s.total_pnl;
  el.textContent=p!=null?'Total: '+fmt(p):'';el.style.color=p>=0?'var(--green)':'var(--red)';
}
function setTrades(ts){
  const el=$('tlist');
  if(!ts.length){el.innerHTML='<div style="color:var(--muted);padding:8px 0">No trades yet</div>';return;}
  el.innerHTML=[...ts].reverse().slice(0,20).map(t=>`<div class="tr">
<span>${t2s(t.opened_at)}</span>
<span>${t.side==='LONG'?'<span class="tl">LONG</span>':'<span class="ts">SHORT</span>'}</span>
<span>${t.size??'--'}</span>
<span class="${(t.pnl||0)>=0?'g':'r'}">${fmt(t.pnl)}</span>
<span>${fmtN(t.entry_price)} -> ${t.exit_price?fmtN(t.exit_price):'open'}</span>
</div>`).join('');
  const cl=ts.filter(t=>t.pnl!=null).slice(-30);
  pC.data.labels=cl.map((_,i)=>'#'+(i+1));
  pC.data.datasets[0].data=cl.map(t=>t.pnl);
  pC.data.datasets[0].backgroundColor=cl.map(t=>t.pnl>=0?'rgba(34,197,94,.7)':'rgba(239,68,68,.7)');
  pC.update('none');
}
function setPos(p){
  const el=$('pos');
  if(!p){el.innerHTML='<span style="color:var(--muted);font-size:13px">No open position</span>';return;}
  const c=p.unrealized_pnl>=0?'g':'r';
  el.innerHTML=`<div class="pd">
<div class="pi"><div class="l">Symbol</div><div class="v">${p.symbol}</div></div>
<div class="pi"><div class="l">Side</div><div class="v">${p.side}</div></div>
<div class="pi"><div class="l">Size</div><div class="v">${p.size}</div></div>
<div class="pi"><div class="l">Entry</div><div class="v">$${fmtN(p.entry_px)}</div></div>
<div class="pi"><div class="l">Current</div><div class="v">$${fmtN(p.current_px)}</div></div>
<div class="pi"><div class="l">Unrealized</div><div class="v ${c}">${fmt(p.unrealized_pnl)}</div></div>
</div>`;
}
function setRegime(r){const b=$('rb');b.textContent=r.regime+(r.adx?' ADX '+r.adx:'');b.className='badge '+r.regime;}
async function load(){
  const[eq,tr,st,po,re]=await Promise.all([
    fetch('/api/equity').then(r=>r.json()),fetch('/api/trades').then(r=>r.json()),
    fetch('/api/stats').then(r=>r.json()),fetch('/api/position').then(r=>r.json()),
    fetch('/api/regime').then(r=>r.json())]);
  setEq(eq.curve);setTrades(tr.trades);setStats(st);setPos(po);setRegime(re);
}
function conn(){
  const ws=new WebSocket('ws://'+location.host+'/ws/live');
  const dot=$('dot'),lbl=$('lbl');
  ws.onopen=()=>{dot.classList.add('on');lbl.textContent='Live';};
  ws.onclose=()=>{dot.classList.remove('on');lbl.textContent='Reconnecting...';setTimeout(conn,3000);};
  ws.onmessage=e=>{const d=JSON.parse(e.data);
    if(d.stats)setStats(d.stats);
    if(d.position!==undefined)setPos(d.position);
    if(d.regime)setRegime({regime:d.regime,adx:0});};
}
load().then(conn);
</script>
</body></html>
""")
ok("dashboard/static/index.html written")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 8 — Paper broker
# ─────────────────────────────────────────────────────────────────────────────
info("Writing paper broker ...")
write(SRC / "paper" / "paper_broker.py", '''\
"""
paper/paper_broker.py — simulated broker, drop-in for OandaAdapter.
No network calls. Fill at last price +/- slippage.
1 standard lot = 100 oz for XAU/USD P&L calculation.
"""
from __future__ import annotations
import time, uuid
from decimal import Decimal
from typing import Optional


class _Pos:
    def __init__(self, symbol, side, size, entry):
        self.symbol=symbol; self.side=side; self.size=size
        self.entry_price=entry; self.opened_at=time.time()


class PaperBrokerAdapter:
    PIP = 0.01
    def __init__(self, initial_equity: Decimal, slippage_pips=0.3, spread_pips=0.5):
        self._equity  = float(initial_equity)
        self._slip    = slippage_pips * self.PIP
        self._spread  = spread_pips   * self.PIP
        self._pos: Optional[_Pos] = None
        self._price   = 2000.0
        self._fills: list[dict] = []

    async def connect(self): pass
    async def disconnect(self): pass
    async def get_account_summary(self):
        return {"balance": str(round(self._equity,2)), "currency": "USD"}

    async def place_order(self, order) -> dict:
        side  = getattr(order,"side",None)
        side_s= side.value if hasattr(side,"value") else str(side)
        size  = float(getattr(order,"units",getattr(order,"size",0.01)))
        sym   = getattr(order,"instrument",getattr(order,"symbol","XAU_USD"))
        buy   = "BUY" in side_s.upper()
        px    = self._price + (self._slip if buy else -self._slip)
        if self._pos:
            opp = (buy and self._pos.side=="SHORT") or (not buy and self._pos.side=="LONG")
            if opp: await self._close(self._price)
        if not self._pos:
            self._pos = _Pos(sym, "LONG" if buy else "SHORT", size, px)
        fill = {"fill_id":f"PAPER-{uuid.uuid4().hex[:8].upper()}",
                "price":px,"size":size,"side":side_s}
        self._fills.append(fill); return fill

    async def cancel_order(self, oid): pass
    async def close_position(self, sym): return await self._close(self._price)
    async def get_current_price(self, sym):
        return {"bid":self._price-self._spread/2,"ask":self._price+self._spread/2,"mid":self._price}
    async def get_open_positions(self):
        if not self._pos: return []
        return [{"instrument":self._pos.symbol,"side":self._pos.side,
                 "units":str(self._pos.size),"avg_price":str(self._pos.entry_price)}]
    async def get_open_orders(self): return []
    async def is_connected(self): return True
    async def heartbeat(self): return True

    async def _close(self, price):
        if not self._pos: return {}
        oz = self._pos.size * 100
        pnl = (price-self._pos.entry_price)*oz if self._pos.side=="LONG" \
              else (self._pos.entry_price-price)*oz
        self._equity += pnl; self._pos = None
        return {"pnl":round(pnl,2),"close_price":price}

    def on_bar(self, price: float): self._price = price

    def get_equity(self) -> float:
        if not self._pos: return self._equity
        oz = self._pos.size*100
        upnl = (self._price-self._pos.entry_price)*oz if self._pos.side=="LONG" \
               else (self._pos.entry_price-self._price)*oz
        return self._equity+upnl

    def get_position_snapshot(self):
        if not self._pos: return None
        oz = self._pos.size*100
        upnl = (self._price-self._pos.entry_price)*oz if self._pos.side=="LONG" \
               else (self._pos.entry_price-self._price)*oz
        return {"symbol":self._pos.symbol,"side":self._pos.side,"size":self._pos.size,
                "entry_px":round(self._pos.entry_price,2),"current_px":round(self._price,2),
                "unrealized_pnl":round(upnl,2)}
''')
ok("paper/paper_broker.py written")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 9 — Update pyproject.toml, docker-compose, .env.example
# ─────────────────────────────────────────────────────────────────────────────
info("Updating config files ...")
pj = XAUUSD / "pyproject.toml"
if pj.exists():
    txt = pj.read_text(encoding="utf-8")
    added = []
    for pkg, line in [("fastapi",'    "fastapi>=0.110",\n'),
                      ("uvicorn",'    "uvicorn[standard]>=0.29",\n'),
                      ("websockets",'    "websockets>=12.0",\n')]:
        if pkg not in txt:
            txt = txt.replace('    "python-dotenv>=1.0",\n',
                              '    "python-dotenv>=1.0",\n'+line)
            added.append(pkg)
    write(pj, txt)
    ok(f"pyproject.toml: {('added '+str(added)) if added else 'already up to date'}")

dc = XAUUSD / "docker-compose.yml"
if dc.exists():
    txt = dc.read_text(encoding="utf-8")
    if "dashboard:" not in txt:
        txt = txt.rstrip()+"""
  dashboard:
    build: .
    command: uvicorn src.dashboard.api:app --host 0.0.0.0 --port 8080
    ports: ["8080:8080"]
    environment: [PAPER_MODE=true]
    env_file: .env
    depends_on: [trader]
    restart: unless-stopped
"""
        write(dc, txt); ok("docker-compose.yml updated")
    else:
        ok("docker-compose.yml already has dashboard")

env = XAUUSD / ".env.example"
if env.exists():
    txt = env.read_text(encoding="utf-8")
    if "PAPER_MODE" not in txt:
        txt += "\nPAPER_MODE=true\nDASHBOARD_PORT=8080\n"
        write(env, txt); ok(".env.example updated")

# ─────────────────────────────────────────────────────────────────────────────
# DONE
# ─────────────────────────────────────────────────────────────────────────────
print("""
=== Bootstrap complete ===

Next steps:

  1.  cd xauusd_system
  2.  pip install -e ".[dev]"
  3.  py -c "from src.execution import ExecutionEngine; print('execution OK')"
  4.  py -c "from src.risk import RiskEngine; print('risk OK')"
  5.  copy .env.example .env        (fill in OANDA creds — not needed for paper)
  6.  $env:PAPER_MODE="true"; py -m src.main
  7.  open http://localhost:8080
""")
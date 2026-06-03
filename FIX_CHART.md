# Fix the chart — debug session

## Step 1 — Run these and paste the output back to me

```bash
# 1. What does the bars endpoint actually return?
curl -s "http://localhost:8080/api/bars?count=5" | python -m json.tool

# 2. What does the uvicorn log say?
# (run this in a separate terminal while the server is running)
# Look for any line containing "bars", "get_bars", "error", "exception", or "traceback"

# 3. What does the browser console say?
# Open http://localhost:8080, press F12 → Console tab
# Look for any red errors or lines starting with [chart]
# Copy everything you see
```

## Step 2 — Based on what you find, fix accordingly

### If curl returns `{"bars": []}` (empty list)

The `get_bars()` function is returning nothing. Find it and check why:

```bash
grep -rn "get_bars\|async def get_bars" src/
```

Open that file. The most common reasons it returns empty:
- It's calling OANDA but `PAPER_MODE=true` so it tries a synthetic generator that isn't implemented
- The granularity string is wrong — OANDA expects `"H1"` but it might be sending `"1h"` or `"60"`
- The symbol format is wrong — OANDA expects `"XAU_USD"` not `"XAUUSD"` or `"XAU/USD"`
- It's catching exceptions silently and returning `[]`

Fix: add temporary debug logging right inside `get_bars()`:

```python
async def get_bars(self, symbol, granularity, count):
    print(f"[DEBUG get_bars] symbol={symbol} granularity={granularity} count={count}")
    try:
        # ... existing code ...
        print(f"[DEBUG get_bars] returning {len(bars)} bars")
        return bars
    except Exception as e:
        print(f"[DEBUG get_bars] EXCEPTION: {e}")
        import traceback; traceback.print_exc()
        return []
```

Restart the server, hit the endpoint, check what prints.

### If curl returns bars fine but chart is still black

The data is arriving but Lightweight Charts isn't receiving it. The issue is one of:

**A) Timing** — `loadBars()` runs before the chart is initialized.

Fix: make sure `loadBars()` is called AFTER `chart`, `candles`, `sma`, `dUp`, `dLo` are all created. Check the order in the script. It must be:

```js
// 1. Create chart
const chart = LWC.createChart(...)
const candles = chart.addSeries(LWC.CandlestickSeries, {...})
const sma = chart.addSeries(LWC.LineSeries, {...})
const dUp = chart.addSeries(LWC.LineSeries, {...})
const dLo = chart.addSeries(LWC.LineSeries, {...})

// 2. THEN load bars
loadBars().then(bars => {
  if (!bars || bars.length === 0) {
    console.error('[chart] loadBars returned empty — check /api/bars')
    return
  }
  console.log('[chart] loaded', bars.length, 'bars, first:', bars[0], 'last:', bars[bars.length-1])
  candles.setData(bars)
  sma.setData(smaSeries(bars, 200))
  dUp.setData(donchian(bars, 20, true))
  dLo.setData(donchian(bars, 20, false))
  chart.timeScale().fitContent()
  last = { ...bars[bars.length - 1] }
}).catch(err => console.error('[chart] loadBars failed:', err))
```

**B) Time format wrong** — Lightweight Charts v5 requires `time` as a **unix timestamp in seconds** (integer). If the backend is returning ISO strings like `"2025-06-03T12:00:00Z"`, the chart silently ignores all data points.

Fix: in `loadBars()`, convert after fetching:

```js
async function loadBars() {
  const res = await fetch('/api/bars?symbol=XAU_USD&granularity=H1&count=250')
  const json = await res.json()
  const bars = json.bars.map(b => ({
    ...b,
    // Force time to integer seconds — handles both unix seconds and ISO strings
    time: typeof b.time === 'string'
      ? Math.floor(new Date(b.time).getTime() / 1000)
      : typeof b.time === 'number' && b.time > 1e10
        ? Math.floor(b.time / 1000)  // was milliseconds, convert to seconds
        : b.time                      // already unix seconds, use as-is
  }))
  console.log('[chart] sample bar after conversion:', bars[bars.length - 1])
  return bars
}
```

**C) Chart container has zero height** — if the `#chart` div has no explicit height, the canvas is 0px tall and renders nothing.

Fix: check the CSS for `#chart`. It must have an explicit height:

```css
#chart {
  flex: 1;
  width: 100%;
  min-height: 420px;   /* this line must exist */
}
```

Also check that `.chart-card` has `display: flex; flex-direction: column` so `flex: 1` on `#chart` actually expands it.

**D) ResizeObserver fires before data loads** — the `fit()` call resizes the chart to the container dimensions before candles are set, which confuses the time scale.

Fix: call `chart.timeScale().fitContent()` AFTER `candles.setData(bars)`, not before. Already in the code above — make sure it's in that order.

## Step 3 — Verify the fix

After any change, open browser console and confirm you see:
```
[chart] loaded 250 bars, first: {time: 1234567890, open: ..., ...} last: {time: ..., ...}
```

If you see that log line and the chart is still black, the issue is CSS height.
If you don't see that log line, the issue is the API or timing.

## The Aurum rename

While you're in the code, rename the project everywhere:
- `docker-compose.yml`: `container_name: aurum`
- `index.html` `<title>` tag: `Aurum · XAU/USD · Paper`
- `index.html` header `.brand` text: `Aurum` (keep the `PAPER` pill)
- Any log prefixes or print statements: replace "xauusd" with "aurum" where visible
- `README.md` first line if it exists

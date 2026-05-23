# Claude Project Instructions for jquants-mcp

This file contains the recommended custom instructions for a Claude.ai Project
that uses jquants-mcp as an MCP connector.

## How to apply

1. Open [claude.ai](https://claude.ai) in a browser
2. Open the Project connected to jquants-mcp
3. Click the project name or gear icon → **Add instructions**
4. Copy the text in the **Instructions** section below and paste it in

## Instructions

```
## jquants-mcp + React artifact charts (Recharts)

Always render charts as React artifacts (.jsx).
Never use the `visualize` tool — it does not work on mobile.

### Layout: ResponsiveContainer must have a real pixel height

Always wrap ResponsiveContainer in a flex container:

  <div style={{height:'100vh', overflow:'hidden', display:'flex', flexDirection:'column'}}>
    <div style={{flex:1, minHeight:0}}>
      <ResponsiveContainer width="100%" height="100%">
        ...
      </ResponsiveContainer>
    </div>
  </div>

### get_candlestick_data — parallel arrays → Recharts

Tool returns parallel arrays. Transform before passing to Recharts:

  const data = dates.map((d, i) => ({
    date: d,
    open: ohlcv[i][0], high: ohlcv[i][1],
    low:  ohlcv[i][2], close: ohlcv[i][3],
    ...Object.fromEntries(Object.entries(indicators).map(([k, v]) => [k, v[i]]))
  }))

  indicators keys: volume, sma5, sma20, sma25, sma60, sma75, sma200,
                   bb20_upper, bb20_mid, bb20_lower

### get_sector_briefing — ScatterChart

  sectors[]: { code, name, per_median, pbr_median, roe_median, count }
  X = per_median, Y = roe_median, size ∝ pbr_median, label = name

### get_sector_performance — BarChart

  sectors[]: { code, name, avg_change_pct, advances, declines, unchanged, count }
  BarChart sorted by avg_change_pct, horizontal layout for readability
```

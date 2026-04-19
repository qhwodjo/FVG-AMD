#!/usr/bin/env python3
"""
AMD + FVG Signal Bot
- Pulls live candle data directly from Deriv API
- Runs AMD + FVG logic on every closed candle
- Sends Entry / SL / TP to Telegram for manual trading
- Tiny HTTP server on the side so Render detects an open port
"""

import asyncio
import json
import os
import threading
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import websockets

# ══════════════════════════════════════════════════════
#  CONFIG — set these in Render Environment Variables
# ══════════════════════════════════════════════════════
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DERIV_APP_ID     = os.environ.get("DERIV_APP_ID", "1089")
DERIV_API_TOKEN  = os.environ.get("DERIV_API_TOKEN", "")
PORT             = int(os.environ.get("PORT", 10000))

# ── Symbols to watch ─────────────────────────────────
SYMBOLS = [
    ("cryETHUSD", "ETH/USD", 3600, "1H"),
    ("cryBTCUSD", "BTC/USD", 3600, "1H"),
]

# ── AMD + FVG parameters ─────────────────────────────
ACC_LEN      = 20
ACC_MAX_PCT  = 0.30
MAN_LOOK     = 25
FVG_ATR_MULT = 0.05
ATR_LEN      = 14
ATR_SL_MULT  = 1.5
RRR          = 2.0
COOLDOWN     = 40

DERIV_WS = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"

bot_status = {
    "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    "symbols": {},
}


# ══════════════════════════════════════════════════════
#  HEALTH CHECK SERVER  (satisfies Render port check)
# ══════════════════════════════════════════════════════
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (
            "<h2>AMD + FVG Bot Running</h2>"
            f"<p>Started: {bot_status['started_at']}</p><ul>"
        )
        for sym, info in bot_status["symbols"].items():
            body += f"<li><b>{sym}</b>: {info}</li>"
        body += "</ul>"
        body = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    print(f"[Health] Listening on port {PORT}")
    server.serve_forever()


# ══════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════
def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] Token or Chat ID missing — skipping")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
        r.raise_for_status()
        print("[Telegram] Message sent")
    except Exception as e:
        print(f"[Telegram] Error: {e}")


# ══════════════════════════════════════════════════════
#  AMD + FVG DETECTOR
# ══════════════════════════════════════════════════════
class AMDDetector:
    def __init__(self):
        self.buf      = deque(maxlen=400)
        self.bidx     = 0
        self.phase    = 0
        self.a_hi     = None
        self.a_lo     = None
        self.man_dir  = 0
        self.man_ex   = None
        self.m_bar    = 0
        self.cooldown = 0

    def _atr(self):
        c = list(self.buf)
        if len(c) < ATR_LEN + 1:
            return None
        trs = [
            max(c[i]["high"] - c[i]["low"],
                abs(c[i]["high"] - c[i - 1]["close"]),
                abs(c[i]["low"]  - c[i - 1]["close"]))
            for i in range(1, len(c))
        ]
        return sum(trs[-ATR_LEN:]) / ATR_LEN

    def _hi(self, n):
        return max(c["high"] for c in list(self.buf)[-n:])

    def _lo(self, n):
        return min(c["low"]  for c in list(self.buf)[-n:])

    def _reset(self):
        self.phase   = 0
        self.a_hi    = None
        self.a_lo    = None
        self.man_dir = 0
        self.man_ex  = None

    def update(self, candle: dict):
        self.buf.append(candle)
        self.bidx += 1
        if len(self.buf) < ACC_LEN + 5:
            return None
        atr = self._atr()
        if atr is None:
            return None
        if self.cooldown > 0:
            self.cooldown -= 1
            return None

        hi_n = self._hi(ACC_LEN)
        lo_n = self._lo(ACC_LEN)
        rng  = (hi_n - lo_n) / lo_n * 100
        buf  = list(self.buf)
        cur  = buf[-1]
        p2   = buf[-3] if len(buf) >= 3 else None
        signal = None

        if self.phase == 0:
            if rng <= ACC_MAX_PCT:
                self.phase = 1
                self.a_hi  = hi_n
                self.a_lo  = lo_n

        elif self.phase == 1:
            if rng <= ACC_MAX_PCT:
                self.a_hi = hi_n
                self.a_lo = lo_n
            else:
                self.m_bar   = self.bidx
                self.man_ex  = None
                self.man_dir = 0
                self.phase   = 2

        elif self.phase == 2:
            if self.bidx > self.m_bar + MAN_LOOK:
                self._reset()
                return None

            if self.man_dir == 0:
                if cur["high"] > self.a_hi:
                    self.man_dir = -1
                    self.man_ex  = cur["high"]
                elif cur["low"] < self.a_lo:
                    self.man_dir = 1
                    self.man_ex  = cur["low"]
            else:
                if self.man_dir == 1:
                    self.man_ex = min(self.man_ex, cur["low"])
                else:
                    self.man_ex = max(self.man_ex, cur["high"])

            if self.man_dir != 0 and self.bidx >= self.m_bar + 3 and p2 is not None:
                if self.man_dir == -1:
                    gap = p2["low"] - cur["high"]
                    if gap > 0 and gap >= atr * FVG_ATR_MULT:
                        entry = cur["close"]
                        sl    = self.man_ex + atr * ATR_SL_MULT
                        tp    = entry - (sl - entry) * RRR
                        signal = {"dir": "BEARISH", "entry": entry, "sl": sl, "tp": tp}
                        self.cooldown = COOLDOWN
                        self._reset()

                elif self.man_dir == 1:
                    gap = cur["low"] - p2["high"]
                    if gap > 0 and gap >= atr * FVG_ATR_MULT:
                        entry = cur["close"]
                        sl    = self.man_ex - atr * ATR_SL_MULT
                        tp    = entry + (entry - sl) * RRR
                        signal = {"dir": "BULLISH", "entry": entry, "sl": sl, "tp": tp}
                        self.cooldown = COOLDOWN
                        self._reset()

        return signal


# ══════════════════════════════════════════════════════
#  SIGNAL ALERT
# ══════════════════════════════════════════════════════
def fire_signal(sig: dict, name: str, tf: str):
    d      = sig["dir"]
    entry  = sig["entry"]
    sl     = sig["sl"]
    tp     = sig["tp"]
    risk   = abs(entry - sl)
    reward = abs(entry - tp)
    ts     = datetime.now(timezone.utc).strftime("%Y-%m-%d  %H:%M UTC")
    emoji  = "🔴" if d == "BEARISH" else "🟢"
    action = "SELL  (Short)" if d == "BEARISH" else "BUY   (Long)"
    dp     = 2 if entry > 100 else 5

    msg = (
        f"{emoji} <b>AMD + FVG Signal</b>\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Pair:</b>      {name}\n"
        f"⏱ <b>Timeframe:</b> {tf}\n"
        f"📣 <b>Signal:</b>    {action}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Entry</b>  →  <code>{entry:.{dp}f}</code>\n"
        f"🛑 <b>SL</b>     →  <code>{sl:.{dp}f}</code>  <i>({risk:.{dp}f})</i>\n"
        f"✅ <b>TP</b>     →  <code>{tp:.{dp}f}</code>  <i>({reward:.{dp}f})</i>\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"⚖️  RR  1 : {RRR}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"🕐 <i>{ts}</i>"
    )

    print(f"[Signal] {d} {name} | Entry={entry:.{dp}f}  SL={sl:.{dp}f}  TP={tp:.{dp}f}")
    send_telegram(msg)


# ══════════════════════════════════════════════════════
#  DERIV STREAM
# ══════════════════════════════════════════════════════
async def stream_symbol(sym: str, name: str, gran: int, tf: str):
    detector    = AMDDetector()
    initialized = False
    bot_status["symbols"][name] = "Connecting..."
    print(f"[{name}] Starting {tf} stream...")

    while True:
        try:
            async with websockets.connect(DERIV_WS, ping_interval=30) as ws:

                if DERIV_API_TOKEN:
                    await ws.send(json.dumps({"authorize": DERIV_API_TOKEN}))
                    auth = json.loads(await ws.recv())
                    if "error" in auth:
                        print(f"[{name}] Auth error: {auth['error']['message']}")
                    else:
                        print(f"[{name}] Authorized")

                await ws.send(json.dumps({
                    "ticks_history":     sym,
                    "adjust_start_time": 1,
                    "count":             300,
                    "end":               "latest",
                    "granularity":       gran,
                    "style":             "candles",
                    "subscribe":         1,
                }))

                current_open_time = None
                current_candle    = None

                async for raw in ws:
                    data     = json.loads(raw)
                    msg_type = data.get("msg_type")

                    if msg_type == "candles":
                        hist = data.get("candles", [])
                        for c in hist[:-1]:
                            detector.update({
                                "open":  float(c["open"]),
                                "high":  float(c["high"]),
                                "low":   float(c["low"]),
                                "close": float(c["close"]),
                                "epoch": int(c["epoch"]),
                            })
                        if hist:
                            last = hist[-1]
                            current_open_time = int(last["epoch"])
                            current_candle = {
                                "open":  float(last["open"]),
                                "high":  float(last["high"]),
                                "low":   float(last["low"]),
                                "close": float(last["close"]),
                                "epoch": current_open_time,
                            }
                        initialized = True
                        ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
                        bot_status["symbols"][name] = f"Live — {len(hist)} candles at {ts}"
                        print(f"[{name}] Ready — {len(hist)} candles at {ts}")
                        send_telegram(
                            f"🤖 <b>AMD Bot Online</b>\n"
                            f"📊 Watching <b>{name}</b>  ({tf})\n"
                            f"🕐 {ts}"
                        )

                    elif msg_type == "ohlc":
                        ohlc      = data.get("ohlc", {})
                        open_time = int(ohlc.get("open_time", 0))
                        new_candle = {
                            "open":  float(ohlc["open"]),
                            "high":  float(ohlc["high"]),
                            "low":   float(ohlc["low"]),
                            "close": float(ohlc["close"]),
                            "epoch": open_time,
                        }

                        if current_open_time is None:
                            current_open_time = open_time
                            current_candle    = new_candle
                        elif open_time != current_open_time:
                            if initialized and current_candle is not None:
                                ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
                                bot_status["symbols"][name] = f"Scanning... last close {ts}"
                                sig = detector.update(current_candle)
                                if sig:
                                    fire_signal(sig, name, tf)
                            current_open_time = open_time
                            current_candle    = new_candle
                        else:
                            current_candle = new_candle

                    elif "error" in data:
                        print(f"[{name}] Deriv error: {data['error'].get('message', data)}")

        except websockets.exceptions.ConnectionClosed as e:
            print(f"[{name}] Disconnected ({e}) — reconnecting in 5s...")
            bot_status["symbols"][name] = "Reconnecting..."
            initialized = False
        except Exception as e:
            print(f"[{name}] Error: {e} — reconnecting in 5s...")
            bot_status["symbols"][name] = f"Error: {e}"
            initialized = False

        await asyncio.sleep(5)


# ══════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════
async def main():
    print("=" * 45)
    print("  AMD + FVG Signal Bot  (Deriv + Telegram)")
    print("=" * 45)

    missing = [v for v in ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"]
               if not os.environ.get(v)]
    if missing:
        print(f"WARNING: Missing env vars: {', '.join(missing)}")

    # Start the tiny HTTP health server in a daemon thread
    threading.Thread(target=run_health_server, daemon=True).start()

    # Run all symbol streams concurrently forever
    await asyncio.gather(*[
        stream_symbol(sym, name, gran, tf)
        for sym, name, gran, tf in SYMBOLS
    ])


if __name__ == "__main__":
    asyncio.run(main())
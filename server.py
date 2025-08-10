import os, time, hmac, hashlib
from urllib.parse import urlencode
from typing import Optional, Literal

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()  # charge les clés depuis .env

BINANCE_KEY = os.getenv("BINANCE_KEY", "")
BINANCE_SECRET = os.getenv("BINANCE_SECRET", "")
BASE_URL = os.getenv("BASE_URL", "https://testnet.binance.vision")
ALLOWED_SYMBOLS = set((os.getenv("ALLOWED_SYMBOLS","BTCUSDT,ETHUSDT")).split(","))
MAX_QUOTE_TRADE_USDT = float(os.getenv("MAX_QUOTE_TRADE_USDT", "100"))

if not BINANCE_KEY or not BINANCE_SECRET:
    raise RuntimeError("Configure BINANCE_KEY et BINANCE_SECRET dans .env")

app = FastAPI(title="Binance Trading Proxy (Testnet)")

class OrderIn(BaseModel):
    symbol: str = Field(..., example="BTCUSDT")
    side: Literal["BUY","SELL"]
    type: Literal["MARKET","LIMIT"]
    quote_amount: Optional[float] = Field(None, description="Montant USDT pour MARKET BUY")
    quantity: Optional[float] = None
    price: Optional[float] = None
    confirmed: bool = Field(False, description="Doit être True après confirmation")

def _sign(params: dict) -> str:
    query_string = urlencode(params, doseq=True)
    signature = hmac.new(BINANCE_SECRET.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    return f"{query_string}&signature={signature}"

async def _get(path: str, params: dict | None = None, signed: bool = False):
    headers = {"X-MBX-APIKEY": BINANCE_KEY}
    params = params or {}
    if signed:
        params.update({"timestamp": int(time.time() * 1000)})
        qs = _sign(params)
    else:
        qs = urlencode(params, doseq=True)
    url = f"{BASE_URL}{path}?{qs}" if qs else f"{BASE_URL}{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers=headers)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    return r.json()

async def _post(path: str, params: dict, signed: bool = True):
    headers = {"X-MBX-APIKEY": BINANCE_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    if signed:
        params.update({"timestamp": int(time.time() * 1000)})
        body = _sign(params)
    else:
        body = urlencode(params, doseq=True)
    url = f"{BASE_URL}{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, data=body, headers=headers)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    return r.json() if r.text else {"ok": True}

@app.get("/ping")
async def ping():
    return {"status": "ok"}

@app.get("/price/{symbol}")
async def get_price(symbol: str):
    symbol = symbol.upper()
    data = await _get("/api/v3/ticker/price", {"symbol": symbol}, signed=False)
    return {"symbol": symbol, "price": float(data["price"])}

@app.get("/balance")
async def get_balance():
    data = await _get("/api/v3/account", signed=True)
    balances = {b["asset"]: float(b["free"]) for b in data.get("balances", []) if float(b["free"]) > 0}
    return {"balances": balances}

@app.post("/order")
async def create_order(o: OrderIn):
    symbol = o.symbol.upper()
    if symbol not in ALLOWED_SYMBOLS:
        raise HTTPException(400, f"Pair non autorisée. Autorisées: {sorted(ALLOWED_SYMBOLS)}")
    if not o.confirmed:
        raise HTTPException(400, "Confirmation manquante.")

    params = {"symbol": symbol, "side": o.side, "type": o.type}

    if o.type == "MARKET":
        if o.side == "BUY":
            if not o.quote_amount:
                raise HTTPException(400, "Pour BUY MARKET, fournir quote_amount.")
            if o.quote_amount > MAX_QUOTE_TRADE_USDT:
                raise HTTPException(400, f"Montant > limite {MAX_QUOTE_TRADE_USDT} USDT.")
            params["quoteOrderQty"] = f"{o.quote_amount:.2f}"
        else:
            if not o.quantity:
                raise HTTPException(400, "Pour SELL MARKET, fournir quantity.")
            params["quantity"] = f"{o.quantity:.6f}"

    elif o.type == "LIMIT":
        if not (o.quantity and o.price):
            raise HTTPException(400, "Pour LIMIT, fournir quantity et price.")
        params.update({
            "quantity": f"{o.quantity:.6f}",
            "price": f"{o.price:.2f}",
            "timeInForce": "GTC"
        })

    result = await _post("/api/v3/order", params, signed=True)
    return {"exchange_response": result}
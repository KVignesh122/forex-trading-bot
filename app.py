"""Web dashboard - FastAPI server for monitoring and controlling the bot."""
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config
import db
import learner

logger = logging.getLogger("forex.web")

app = FastAPI(title="Forex Trading Bot", docs_url=None, redoc_url=None)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Will be set by main.py
portfolio = None


def set_portfolio(p):
    global portfolio
    portfolio = p


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/data")
async def get_data():
    if portfolio is None:
        return JSONResponse({"error": "Bot not initialized"}, status_code=503)
    try:
        data = portfolio.get_dashboard_data()
        return JSONResponse(data)
    except Exception as e:
        logger.error(f"Dashboard data error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/start")
async def start_bot():
    if portfolio is None:
        return JSONResponse({"error": "Bot not initialized"}, status_code=503)
    portfolio.running = True
    db.set_state("running", True)
    return JSONResponse({"status": "started"})


@app.post("/api/stop")
async def stop_bot():
    if portfolio is None:
        return JSONResponse({"error": "Bot not initialized"}, status_code=503)
    portfolio.running = False
    db.set_state("running", False)
    return JSONResponse({"status": "stopped"})


@app.post("/api/close-all")
async def close_all_positions():
    if portfolio is None:
        return JSONResponse({"error": "Bot not initialized"}, status_code=503)
    portfolio.close_all()
    return JSONResponse({"status": "all positions closed"})


@app.post("/api/reset")
async def reset_portfolio():
    """Reset the portfolio to initial state (wipes all trades)."""
    if portfolio is None:
        return JSONResponse({"error": "Bot not initialized"}, status_code=503)
    portfolio.running = False
    portfolio.close_all()
    portfolio.balance = config.INITIAL_BALANCE
    portfolio.save_balance()
    # Clear trade history
    with db.get_conn() as conn:
        conn.execute("DELETE FROM trades")
        conn.execute("DELETE FROM equity_history")
        conn.execute("DELETE FROM signal_weights")
    return JSONResponse({"status": "reset complete"})


@app.get("/api/learning")
async def get_learning():
    try:
        summary = learner.get_learning_summary()
        return JSONResponse(summary)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/health")
async def health():
    """Health check endpoint for self-ping keep-alive."""
    return JSONResponse({"status": "ok"})


@app.get("/api/signals/{pair}")
async def get_signals(pair: str):
    """Get current signals for a specific pair."""
    import data_feed
    import strategy

    ticker = pair.replace("/", "") + "=X"
    df = data_feed.fetch_price_data(ticker, period="5d", interval="15m")
    if df is None:
        return JSONResponse({"error": "No data available"}, status_code=404)

    signals = strategy.generate_signals(ticker, df)
    combined = strategy.get_weighted_signal(signals)

    return JSONResponse({
        "pair": pair,
        "signals": {k: round(v, 3) for k, v in signals.items()},
        "combined": round(combined, 3),
    })

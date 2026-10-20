"""FastAPI application entrypoint. Starts the workflow worker on boot."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.webhook import router as webhook_router
from app.jobs.worker import worker_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop_event = asyncio.Event()
    task = asyncio.create_task(worker_loop(stop_event))
    app.state.worker_task = task
    app.state.worker_stop = stop_event
    try:
        yield
    finally:
        stop_event.set()
        await task


app = FastAPI(title="WhatsApp Booking Agent", lifespan=lifespan)
app.include_router(webhook_router, prefix="/api")


@app.get("/")
async def root():
    return {"status": "online", "system": "whatsapp-booking"}


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from src.api.webhook import router as webhook_router
from src.core.logging import setup_logging
from src.review.graph import build_reviewer_app, enter_checkpointer

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.checkpointer_stack = AsyncExitStack()
    await app.state.checkpointer_stack.__aenter__()
    checkpointer = await enter_checkpointer(app.state.checkpointer_stack)
    app.state.reviewer_app = build_reviewer_app(
        checkpointer, interrupt_before=["summarizer"]
    )
    try:
        yield
    finally:
        await app.state.checkpointer_stack.__aexit__(None, None, None)


app = FastAPI(lifespan=lifespan)
app.include_router(webhook_router)

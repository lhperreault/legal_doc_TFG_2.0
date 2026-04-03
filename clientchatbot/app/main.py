"""
FastAPI application entry point.
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.config import settings
from app.api.health import router as health_router
from app.api.clients import router as clients_router
from app.api.chat import router as chat_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown hooks."""
    print(f"[clientchatbot] Starting up (AI provider: {settings.ai_provider})")
    yield
    # Shutdown: flush any buffered messages
    print("[clientchatbot] Shutting down — flushing message buffers...")
    try:
        from app.services.memory import conversation_buffer
        await conversation_buffer.flush_all()
    except Exception as e:
        print(f"[clientchatbot] Flush error on shutdown: {e}")


app = FastAPI(
    title="Client Chatbot",
    description="AI-powered law firm client chatbot",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS
origins = settings.cors_origins.split(",") if settings.cors_origins != "*" else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(health_router)
app.include_router(clients_router)
app.include_router(chat_router)

# Static files (widget.js, embed.html, etc.)
# Mounted at / but FastAPI routers take priority over mounts
public_dir = Path(__file__).parent.parent / "public"
if public_dir.exists():
    app.mount("/static", StaticFiles(directory=str(public_dir)), name="static")
    # Also serve embed.html at root for convenience
    from fastapi.responses import FileResponse

    @app.get("/")
    async def serve_embed():
        embed_path = public_dir / "embed.html"
        if embed_path.exists():
            return FileResponse(str(embed_path))
        return {"message": "Client Chatbot API", "docs": "/docs"}

    @app.get("/widget.js")
    async def serve_widget():
        return FileResponse(str(public_dir / "widget.js"), media_type="application/javascript")

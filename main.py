"""Entry point for the transcription service - imports from app.main"""

import app.main as app_main

# Expose the ASGI app at module level (used by `uvicorn main:app`)
app = app_main.app

if __name__ == "__main__":
    import uvicorn

    from app.config import settings

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.port,
        reload=True,
        log_level=(settings.log_level or "info").lower(),
        # Keep our RichHandler config instead of uvicorn's default logging config.
        log_config=None,
    )


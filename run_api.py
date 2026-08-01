"""Convenience entry point for the dashboard API — equivalent to:

    uvicorn api.main:app --reload --port 8000
"""
import uvicorn

from api import config

if __name__ == "__main__":
    uvicorn.run("api.main:app", host=config.API_HOST, port=config.API_PORT, reload=True)

# main.py
# Clean shutdown (Option A) + tolerant FastMCP ctor + original structure preserved

from __future__ import annotations

import os
import sys
import atexit
import signal
import logging
import inspect
from pathlib import Path
from typing import Dict, Optional, Any, List

import uvicorn
from dotenv import load_dotenv

# TOML loader
try:
    import tomllib as tomli  # py311+
except ModuleNotFoundError:
    import tomli  # backport

from mcp.server.fastmcp import FastMCP
from pihole6api import PiHole6Client

# Import modular components (same layout as your project)
from tools import config, metrics
from resources import common, discovery
from prompts import guide

# ------------------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("pihole-mcp")

# ------------------------------------------------------------------------------
# Env + version
# ------------------------------------------------------------------------------
load_dotenv()

def get_version() -> str:
    try:
        pyproject_path = Path(__file__).parent / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            data = tomli.load(f)
            return data["project"]["version"]
    except Exception:
        return "0.0.0"

# ------------------------------------------------------------------------------
# FastMCP ctor: filter unsupported kwargs so older builds don't crash
# ------------------------------------------------------------------------------
def create_mcp() -> FastMCP:
    desired = {
        "name": "PiHoleMCP",
        "version": get_version(),
        "instructions": "You are a helpful assistant that can help with Pi-hole network management tasks.",
    }
    sig = inspect.signature(FastMCP.__init__)
    allowed = set(sig.parameters.keys())
    # __init__(self, ...) → drop self
    allowed.discard("self")
    safe_kwargs = {k: v for k, v in desired.items() if k in allowed}
    # Prefer keyword call so we don't trip positional differences
    return FastMCP(**safe_kwargs) if safe_kwargs else FastMCP("PiHoleMCP")

mcp = create_mcp()

# ------------------------------------------------------------------------------
# Pi-hole clients (kept same semantics as your original)
# ------------------------------------------------------------------------------
pihole_clients: Dict[str, PiHole6Client] = {}

primary_url = os.getenv("PIHOLE_URL")
primary_password = os.getenv("PIHOLE_PASSWORD")
primary_name = os.getenv("PIHOLE_NAME", primary_url)

if not primary_url or not primary_password:
    raise ValueError("Primary Pi-hole configuration (PIHOLE_URL and PIHOLE_PASSWORD) is required")

pihole_clients[primary_name] = PiHole6Client(primary_url, primary_password)

# Optional instances 2–4
for i in range(2, 5):
    url = os.getenv(f"PIHOLE{i}_URL")
    if url:
        password = os.getenv(f"PIHOLE{i}_PASSWORD")
        name = os.getenv(f"PIHOLE{i}_NAME", url)
        pihole_clients[name] = PiHole6Client(url, password)

# ------------------------------------------------------------------------------
# Cleanup: no sys.exit() — let Uvicorn finish lifespan cleanly
# ------------------------------------------------------------------------------
_sessions_closed = False

def close_pihole_sessions() -> None:
    global _sessions_closed
    if _sessions_closed:
        return
    logger.info("Closing Pi-hole client sessions...")
    for name, client in pihole_clients.items():
        try:
            # Your client exposes close_session(); keep existing behavior
            client.close_session()
            # Log URL if available; else name
            base = getattr(client, "base_url", name)
            logger.info("Successfully closed session for Pi-hole: %s", base)
        except Exception as e:
            logger.error("Error closing session for Pi-hole %s: %s", name, e)
    _sessions_closed = True

atexit.register(close_pihole_sessions)

def signal_handler(sig, frame):
    logger.info("Received shutdown signal, cleaning up...")
    # Do NOT sys.exit() here; just perform cleanup and return.
    close_pihole_sessions()

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ------------------------------------------------------------------------------
# Register resources, tools, prompts (unchanged)
# ------------------------------------------------------------------------------
common.register_resources(mcp, pihole_clients, get_version)
discovery.register_resources(mcp)
config.register_tools(mcp, pihole_clients)
metrics.register_tools(mcp, pihole_clients)
guide.register_prompt(mcp)

# ------------------------------------------------------------------------------
# Optional CLI entrypoint for SSE (not used by OpenAPI wrapper)
# ------------------------------------------------------------------------------
def main():
    logger.info("Starting Pi-hole MCP server...")
    mcp.run()

# Expose the SSE app if you want to serve it directly
app = mcp.sse_app()

if __name__ == "__main__":
    # Serve on 0.0.0.0:8000 for direct SSE testing (OpenAPI wrapper uses its own Uvicorn)
    uvicorn.run(app, host="0.0.0.0", port=8000)

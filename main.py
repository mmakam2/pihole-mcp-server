# main.py
# - Clean shutdown (no sys.exit; no signal handlers when imported)
# - Tolerant FastMCP ctor (drops unknown kwargs)
# - Keeps your existing registrations (tools/resources/prompts)
# - Leaves a sync cleanup function the wrapper can call on FastAPI shutdown

from __future__ import annotations

import os
import sys
import atexit
import signal
import logging
import inspect
from pathlib import Path
from typing import Dict

# Optional: load env from .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# TOML loader (py311 tomllib or backport)
try:
    import tomllib as tomli
except ModuleNotFoundError:
    import tomli  # type: ignore

# --- Project imports (adjust if your layout differs) ---
from mcp.server.fastmcp import FastMCP
from pihole6api import PiHole6Client

# These are the typical modules in the original repo; change if your names differ.
from tools import config, metrics
from resources import common, discovery
from prompts import guide

# ------------------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOGLEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("pihole-mcp")

# ------------------------------------------------------------------------------
# Version helper
# ------------------------------------------------------------------------------
def _get_version() -> str:
    try:
        pyproject_path = Path(__file__).parent / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            data = tomli.load(f)
            return data["project"]["version"]
    except Exception:
        return "0.0.0"

# ------------------------------------------------------------------------------
# Create FastMCP (drop unknown kwargs for older builds)
# ------------------------------------------------------------------------------
def _create_mcp() -> FastMCP:
    desired = {
        "name": "PiHoleMCP",
        "version": _get_version(),
        "instructions": "You can manage and inspect Pi-hole instances.",
    }
    sig = inspect.signature(FastMCP.__init__)
    allowed = set(sig.parameters.keys())
    allowed.discard("self")
    safe = {k: v for k, v in desired.items() if k in allowed}
    return FastMCP(**safe) if safe else FastMCP("PiHoleMCP")

mcp = _create_mcp()

# ------------------------------------------------------------------------------
# Pi-hole client construction (kept minimal; mirrors original env pattern)
#   Primary: PIHOLE_URL + PIHOLE_PASSWORD (+ PIHOLE_NAME)
#   Optionals: PIHOLE2_URL/..., PIHOLE3_URL/..., PIHOLE4_URL/...
# ------------------------------------------------------------------------------
pihole_clients: Dict[str, PiHole6Client] = {}

def _add_instance(url_env: str, pw_env: str, name_env: str | None = None):
    url = os.getenv(url_env)
    if not url:
        return
    pw = os.getenv(pw_env)
    name = os.getenv(name_env, url) if name_env else os.getenv("PIHOLE_NAME", url)
    client = PiHole6Client(url, pw)
    pihole_clients[name] = client

# primary (required)
if not os.getenv("PIHOLE_URL") or not os.getenv("PIHOLE_PASSWORD"):
    raise ValueError("Set PIHOLE_URL and PIHOLE_PASSWORD for the primary Pi-hole.")
_add_instance("PIHOLE_URL", "PIHOLE_PASSWORD", "PIHOLE_NAME")

# optional 2..4
for i in (2, 3, 4):
    _add_instance(f"PIHOLE{i}_URL", f"PIHOLE{i}_PASSWORD", f"PIHOLE{i}_NAME")

# ------------------------------------------------------------------------------
# Cleanup (no sys.exit). Wrapper will also call this during FastAPI shutdown.
# ------------------------------------------------------------------------------
_sessions_closed = False

def close_pihole_sessions() -> None:
    global _sessions_closed
    if _sessions_closed:
        return
    logger.info("Closing Pi-hole client sessions...")
    for name, client in pihole_clients.items():
        try:
            client.close_session()
            base = getattr(client, "base_url", name)
            logger.info("Successfully closed session for Pi-hole: %s", base)
        except Exception as e:
            logger.error("Error closing session for Pi-hole %s: %s", name, e)
    _sessions_closed = True

atexit.register(close_pihole_sessions)

def _signal_handler(sig, frame):
    logger.info("Received shutdown signal, cleaning up...")
    close_pihole_sessions()  # DO NOT sys.exit()

# Only install signal handlers when running this file directly, not when imported by the wrapper
if __name__ == "__main__" and os.getenv("DISABLE_MAIN_SIGNAL_HANDLER") != "1":
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

# ------------------------------------------------------------------------------
# Register project resources/tools/prompts (your original decorators will run)
# ------------------------------------------------------------------------------
common.register_resources(mcp, pihole_clients, _get_version)
discovery.register_resources(mcp)
config.register_tools(mcp, pihole_clients)
metrics.register_tools(mcp, pihole_clients)
guide.register_prompt(mcp)

# ------------------------------------------------------------------------------
# Optional: run the SSE app directly (not used by OpenAPI wrapper)
# ------------------------------------------------------------------------------
def main():
    logger.info("Starting Pi-hole MCP SSE server...")
    mcp.run()

app = mcp.sse_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")))

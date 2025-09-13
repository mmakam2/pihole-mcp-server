"""
FastAPI OpenAPI wrapper for your MCP project (no Docker)
- Captures ALL tools registered via @mcp.tool(...)
- Tolerates older FastMCP ctor kwargs (filters unknown)
- Uses FastAPI shutdown hook to call your project's close_pihole_sessions()
- Endpoints: /healthz, /tools, /call_tool, /debug/introspect
"""
import asyncio
import inspect
from typing import Any, Dict, List, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

CAPTURED_TOOLS: Dict[str, Any] = {}

# ---- Patch FastMCP before importing your project: drop unknown __init__ kwargs + capture @tool ----
try:
    from mcp.server.fastmcp import FastMCP as _FastMCP

    # Filter unknown kwargs for ctor
    _orig_init = _FastMCP.__init__
    _allowed_ctor = set(inspect.signature(_orig_init).parameters.keys())

    def _patched_init(self, *args, **kwargs):
        for k in list(kwargs.keys()):
            if k not in _allowed_ctor:
                kwargs.pop(k, None)
        return _orig_init(self, *args, **kwargs)

    _FastMCP.__init__ = _patched_init  # type: ignore[attr-defined]

    # Capture tool registrations
    _orig_tool = getattr(_FastMCP, "tool", None)
    if callable(_orig_tool):
        def _patched_tool(self, *targs, **tkwargs):
            orig_deco = _orig_tool(self, *targs, **tkwargs)
            def deco(func):
                tool_name = tkwargs.get("name")
                if not tool_name:
                    if targs and isinstance(targs[0], str):
                        tool_name = targs[0]
                    else:
                        tool_name = getattr(func, "__name__", "unnamed_tool")
                if callable(func):
                    CAPTURED_TOOLS[tool_name] = func
                return orig_deco(func)
            return deco
        _FastMCP.tool = _patched_tool  # type: ignore[attr-defined]
except Exception:
    # mcp gets installed by your start script; if import failed here, the below import will also fail
    pass

# ---- Import your project AFTER patches so tools get captured ----
try:
    import main as project_main
except Exception as e:
    raise RuntimeError(f"Failed to import your MCP server 'main.py': {e}")

mcp = getattr(project_main, "mcp", None)
if mcp is None:
    raise RuntimeError("Could not find 'mcp' instance in main.py. Ensure main.py defines FastMCP as 'mcp'.")

# ---- If needed, probe internal registries (fallback) ----
def _looks_like_tool_object(obj: Any) -> Tuple[str, Any] | None:
    if callable(obj) and hasattr(obj, "__name__"):
        return (obj.__name__, obj)
    for attr in ("handler", "func", "callable", "fn"):
        name = getattr(obj, "name", None)
        handler = getattr(obj, attr, None)
        if isinstance(name, str) and callable(handler):
            return (name, handler)
    if isinstance(obj, (tuple, list)) and len(obj) == 2 and isinstance(obj[0], str) and callable(obj[1]):
        return (obj[0], obj[1])
    return None

def _collect(container: Any, out: Dict[str, Any]) -> None:
    if isinstance(container, dict):
        for k, v in container.items():
            if isinstance(k, str):
                if callable(v):
                    out.setdefault(k, v)
                else:
                    guess = _looks_like_tool_object(v)
                    if guess:
                        out.setdefault(*guess)
        return
    if isinstance(container, (list, tuple, set)):
        for item in container:
            guess = _looks_like_tool_object(item)
            if guess:
                out.setdefault(*guess)
        return
    try:
        for name in dir(container):
            if name.startswith("_"):
                continue
            val = getattr(container, name, None)
            if callable(val):
                out.setdefault(name, val)
            else:
                guess = _looks_like_tool_object(val)
                if guess:
                    out.setdefault(*guess)
    except Exception:
        pass

def _probe(root: Any) -> Dict[str, Any]:
    cands = []
    for attr in ("tools", "_tools", "registered_tools", "tool_registry", "registry", "router"):
        cands.append(getattr(root, attr, None))
    for parent_attr in ("server", "_server", "app"):
        p = getattr(root, parent_attr, None)
        if p is None:
            continue
        cands.append(p)
        for attr in ("tools", "_tools", "registered_tools", "tool_registry", "registry", "router"):
            cands.append(getattr(p, attr, None))
    seen = set()
    flat = []
    for x in cands:
        if x is None: continue
        if id(x) in seen: continue
        seen.add(id(x)); flat.append(x)
    found: Dict[str, Any] = {}
    for x in flat:
        _collect(x, found)
        for sub in ("tools", "_tools", "items", "values"):
            try:
                subval = getattr(x, sub, None)
                if callable(subval):
                    subval = subval()
                if subval is not None:
                    _collect(subval, found)
            except Exception:
                pass
    return found

# Build tool map: captured → probed → EXPOSED_TOOLS fallback
TOOL_MAP: Dict[str, Any] = dict(CAPTURED_TOOLS)
if not TOOL_MAP:
    TOOL_MAP = _probe(mcp)
if not TOOL_MAP:
    fallback = getattr(project_main, "EXPOSED_TOOLS", None)
    if isinstance(fallback, dict) and fallback:
        TOOL_MAP = {k: v for k, v in fallback.items() if isinstance(k, str) and callable(v)}
if not TOOL_MAP:
    raise RuntimeError("No tools found (capture failed, no registry, and no EXPOSED_TOOLS).")

# ---- FastAPI app ----
app = FastAPI(
    title="Pi-hole MCP OpenAPI Wrapper",
    description="HTTP facade that calls MCP tools in-process",
    version="0.3.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# Shutdown hook: call your project's cleanup
_close_fn = getattr(project_main, "close_pihole_sessions", None)
_async_cleanup = getattr(project_main, "async_cleanup", None)  # optional
_shutdown = getattr(project_main, "_shutdown", None)           # optional

@app.on_event("shutdown")
async def _do_cleanup():
    try:
        for candidate in (_shutdown, _async_cleanup):
            if candidate and inspect.iscoroutinefunction(candidate):
                await candidate()
                return
        if callable(_close_fn):
            await asyncio.to_thread(_close_fn)
    except Exception as e:
        print(f"[wrapper] cleanup error: {e}")

class CallRequest(BaseModel):
    tool: str = Field(..., description="Name of the MCP tool to call")
    args: Dict[str, Any] = Field(default_factory=dict, description="Arguments passed to the tool")

@app.get("/healthz")
async def healthz():
    return {"status": "ok", "tool_count": len(TOOL_MAP)}

@app.get("/tools", response_model=List[str])
async def list_tools() -> List[str]:
    return sorted(TOOL_MAP.keys())

@app.get("/debug/introspect")
async def debug_introspect():
    return {
        "captured_count": len(CAPTURED_TOOLS),
        "captured_keys": sorted(CAPTURED_TOOLS.keys()),
        "probe_count": len(_probe(mcp)),
        "fallback_keys": sorted((getattr(project_main, "EXPOSED_TOOLS", {}) or {}).keys()),
    }

@app.post("/call_tool")
async def call_tool(req: CallRequest):
    func = TOOL_MAP.get(req.tool)
    if func is None:
        raise HTTPException(status_code=404, detail=f"Tool '{req.tool}' not found")

    # Build kwargs from signature
    try:
        sig = inspect.signature(func)
        kwargs = {}
        for p in sig.parameters.values():
            if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY):
                if p.name in req.args:
                    kwargs[p.name] = req.args[p.name]
                elif p.default is p.empty and p.name not in req.args:
                    raise HTTPException(status_code=400, detail=f"Missing required parameter: {p.name}")
    except (ValueError, TypeError):
        kwargs = dict(req.args)

    # Call sync/async tools
    try:
        if inspect.iscoroutinefunction(func):
            result = await func(**kwargs)
        else:
            result = await asyncio.to_thread(func, **kwargs)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Tool '{req.tool}' raised an error: {e}")

    return {"tool": req.tool, "result": result}

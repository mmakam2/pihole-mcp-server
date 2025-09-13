"""
FastAPI OpenAPI wrapper for your MCP server (no Docker)
- Shim: auto-drop ANY unsupported FastMCP.__init__ kwargs (version, description, etc.)
- Capture tools by monkeypatching FastMCP.tool() BEFORE importing your project
- Deep-probe to find tools; fallback to main.EXPOSED_TOOLS (explicit dict)
- Endpoints: /healthz, /tools, /call_tool, /debug/introspect
"""
import asyncio
import inspect
from typing import Any, Dict, List, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ===== Robust compatibility shim for FastMCP ctor AND capture of @mcp.tool =====
CAPTURED_TOOLS: Dict[str, Any] = {}

try:
    from mcp.server.fastmcp import FastMCP as _FastMCP

    # 1) Filter unknown kwargs on __init__
    _orig_init = _FastMCP.__init__
    _allowed_ctor = set(inspect.signature(_orig_init).parameters.keys())

    def _patched_init(self, *args, **kwargs):
        for k in list(kwargs.keys()):
            if k not in _allowed_ctor:
                kwargs.pop(k, None)
        return _orig_init(self, *args, **kwargs)

    _FastMCP.__init__ = _patched_init  # type: ignore[attr-defined]

    # 2) Wrap .tool() to capture all registered tools
    _orig_tool = getattr(_FastMCP, "tool", None)

    if callable(_orig_tool):
        def _patched_tool(self, *targs, **tkwargs):
            """
            Intercepts tool registration:
              @mcp.tool(name="list_queries") -> capture name & function
            """
            orig_decorator = _orig_tool(self, *targs, **tkwargs)

            def decorator(func):
                # Determine the tool name
                tool_name = tkwargs.get("name")
                if not tool_name:
                    # Some flavors pass name via first arg or default to func.__name__
                    if targs and isinstance(targs[0], str):
                        tool_name = targs[0]
                    else:
                        tool_name = getattr(func, "__name__", None) or "unnamed_tool"

                # Record callable
                if callable(func):
                    CAPTURED_TOOLS[tool_name] = func

                # Continue with original registration
                return orig_decorator(func)

            return decorator

        _FastMCP.tool = _patched_tool  # type: ignore[attr-defined]
except Exception:
    # If mcp isn't installed yet, start script installs it; import below will then work.
    pass

# ===== Import your project AFTER the patches so all tools are captured =====
try:
    import main as pihole_mcp_main
except Exception as e:
    raise RuntimeError(f"Failed to import your MCP server 'main.py': {e}")

mcp = getattr(pihole_mcp_main, "mcp", None)
if mcp is None:
    raise RuntimeError("Could not find 'mcp' instance in main.py. Ensure main.py defines FastMCP as 'mcp' at module scope.")

# ---------- Tool discovery helpers ----------
def _looks_like_tool_object(obj: Any) -> Tuple[str, Any] | None:
    if callable(obj) and hasattr(obj, "__name__"):
        return (obj.__name__, obj)
    for handler_attr in ("handler", "func", "callable", "fn"):
        name = getattr(obj, "name", None)
        handler = getattr(obj, handler_attr, None)
        if isinstance(name, str) and callable(handler):
            return (name, handler)
    if isinstance(obj, (tuple, list)) and len(obj) == 2 and isinstance(obj[0], str) and callable(obj[1]):
        return (obj[0], obj[1])
    return None

def _collect_from_container(container: Any, out: Dict[str, Any]) -> None:
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

def _probe_tool_registry(root: Any) -> Dict[str, Any]:
    candidates: List[Any] = []
    for attr in ("tools", "_tools", "registered_tools", "tool_registry", "registry", "router"):
        candidates.append(getattr(root, attr, None))
    for parent_attr in ("server", "_server", "app"):
        parent = getattr(root, parent_attr, None)
        if parent is None:
            continue
        candidates.append(parent)
        for attr in ("tools", "_tools", "registered_tools", "tool_registry", "registry", "router"):
            candidates.append(getattr(parent, attr, None))
    seen = set()
    flat: List[Any] = []
    for c in candidates:
        if c is None:
            continue
        if id(c) in seen:
            continue
        seen.add(id(c))
        flat.append(c)

    found: Dict[str, Any] = {}
    for c in flat:
        _collect_from_container(c, found)
        for subname in ("tools", "_tools", "items", "values"):
            try:
                sub = getattr(c, subname, None)
                if callable(sub):
                    sub = sub()
                if sub is not None:
                    _collect_from_container(sub, found)
            except Exception:
                pass
    return found

# 1) Prefer captured tools (from patched @mcp.tool)
TOOL_MAP: Dict[str, Any] = dict(CAPTURED_TOOLS)

# 2) If nothing captured, try probing internal registries
if not TOOL_MAP:
    TOOL_MAP = _probe_tool_registry(mcp)

# 3) Fallback to explicit export if provided
if not TOOL_MAP:
    fallback = getattr(pihole_mcp_main, "EXPOSED_TOOLS", None)
    if isinstance(fallback, dict) and fallback:
        TOOL_MAP = {k: v for k, v in fallback.items() if isinstance(k, str) and callable(v)}

if not TOOL_MAP:
    raise RuntimeError("Unable to locate any tools: capture failed, registry not found, and no EXPOSED_TOOLS provided.")

# ---------- FastAPI app ----------
app = FastAPI(
    title="Pi-hole MCP OpenAPI Wrapper",
    description="HTTP facade that calls MCP tools in-process",
    version="0.2.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

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
        "probe_count": len(_probe_tool_registry(mcp)),
        "fallback_keys": sorted((getattr(pihole_mcp_main, "EXPOSED_TOOLS", {}) or {}).keys()),
    }

@app.post("/call_tool")
async def call_tool(req: CallRequest):
    func = TOOL_MAP.get(req.tool)
    if func is None:
        raise HTTPException(status_code=404, detail=f"Tool '{req.tool}' not found")
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

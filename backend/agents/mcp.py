"""User authenticated MCP entry; tools cannot approve or directly commit writes."""
from __future__ import annotations

import asyncio

from langchain_core.tools import StructuredTool
from mcp.server.fastmcp import Context, FastMCP
from starlette.applications import Starlette
from starlette.routing import Route

from backend.agents import config, store
from backend.infrastructure.database import database
from backend.modules.accounts import service as accounts

transports = {}


class Transport:
    async def __call__(self, scope, receive, send):
        await transports[asyncio.get_running_loop()](scope, receive, send)


http_app = Starlette(routes=[Route("/api/agent/mcp", endpoint=Transport())])


def task_status(task_id: str) -> dict:
    """Read current user's task and validated result."""
    config.require_enabled()
    from backend.modules.knowledge.service import public_result
    with database() as cursor:
        return public_result(cursor, store.get(cursor, task_id, accounts.current().id))


def knowledge_query(question: str, kb_ids: list[str], idempotency_key: str) -> dict:
    """Create a budget controlled knowledge query for current user."""
    config.require_enabled()
    from backend.modules.knowledge.api import query, QueryRequest
    return query(QueryRequest(question=question, kb_ids=kb_ids), idempotency_key)


TOOLS = {name: StructuredTool.from_function(function) for name, function in {
    "task_status": task_status, "knowledge_query": knowledge_query}.items()}


def create_server():
    server = FastMCP("VideoDream Agent", stateless_http=True, json_response=True,
                     streamable_http_path="/api/agent/mcp", max_request_body_size=65536)
    @server.tool(name="task_status")
    async def status(ctx: Context, task_id: str) -> dict:
        return await asyncio.to_thread(task_status, task_id)
    @server.tool(name="knowledge_query")
    async def query(ctx: Context, question: str, kb_ids: list[str], idempotency_key: str) -> dict:
        return await asyncio.to_thread(knowledge_query, question, kb_ids, idempotency_key)
    return server


def register_business_tool(name, function, schema):
    """ERP extensions are registered by trusted server code, never by prompts."""
    if not name.startswith("erp_") or name in TOOLS:
        raise ValueError("ERP tools need a unique erp_ name")
    TOOLS[name] = StructuredTool.from_function(function, name=name, args_schema=schema)

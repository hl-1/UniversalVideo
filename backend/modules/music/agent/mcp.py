from __future__ import annotations

import asyncio
import inspect
import json
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field
from starlette.applications import Starlette
from starlette.routing import Route

from backend.modules.music.agent import auth, service
from backend.modules.music.agent.faults import ModelFault


server = FastMCP("VideoDream Music", stateless_http=True, json_response=True,
                 streamable_http_path="/api/music/mcp", max_request_body_size=65536)


async def dispatch(ctx, name, arguments):
    header = ctx.request_context.request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise ValueError("Task authorization required")
    # FastMCP invokes sync tools on its event loop. Keep blocking database/media
    # work and the model runner on a worker while preserving ContextVars.
    return await asyncio.to_thread(service.execute, header[7:], name, arguments)


@server.tool()
async def search_my_music(ctx: Context, query: Annotated[str, Field(max_length=100)]="",
                    offset: Annotated[int,Field(ge=0,le=1000)]=0, limit: Annotated[int,Field(ge=1,le=20)]=20) -> dict:
    """Search current user's playlists, favorites and downloads with pagination."""
    return await dispatch(ctx,"search_my_music",dict(query=query,offset=offset,limit=limit))


@server.tool()
async def list_music_categories(ctx: Context) -> dict:
    """List provider capabilities and versioned scene keyword categories."""
    return await dispatch(ctx,"list_music_categories",{})


@server.tool()
async def search_online_playlists(ctx: Context, provider: Literal["netease","qq"], query: Annotated[str,Field(min_length=1,max_length=100)],
                            page: Annotated[int,Field(ge=1,le=10)]=1) -> dict:
    """Find real online playlists by scene/mood/theme keyword."""
    return await dispatch(ctx,"search_online_playlists",dict(provider=provider,query=query,page=page))


@server.tool()
async def get_playlist_tracks(ctx: Context, provider: Literal["netease","qq"], playlist_id: Annotated[str,Field(pattern=r"^\d{1,24}$")],
                        offset: Annotated[int,Field(ge=0,le=1000)]=0, limit: Annotated[int,Field(ge=1,le=20)]=20) -> dict:
    """Get paginated tracks of an already retrieved online playlist."""
    return await dispatch(ctx,"get_playlist_tracks",dict(provider=provider,playlist_id=playlist_id,offset=offset,limit=limit))


@server.tool()
async def search_online_tracks(ctx: Context, provider: Literal["netease","qq","kugou"], query: Annotated[str,Field(min_length=1,max_length=100)],
                         page: Annotated[int,Field(ge=1,le=20)]=1) -> dict:
    """Search replacement tracks with stable provider identity."""
    return await dispatch(ctx,"search_online_tracks",dict(provider=provider,query=query,page=page))


@server.tool()
async def get_track_features(ctx: Context, keys: Annotated[list[str],Field(min_length=1,max_length=20)]) -> dict:
    """Analyze retrieved candidate lyrics/audio; return confidence and coverage."""
    return await dispatch(ctx,"get_track_features",dict(keys=keys))


@server.tool()
async def validate_tracks(ctx: Context, keys: Annotated[list[str],Field(min_length=1,max_length=20)]) -> dict:
    """Verify identity and probe/decode complete audio; detect preview or mismatches."""
    return await dispatch(ctx,"validate_tracks",dict(keys=keys))


@server.tool()
async def save_playlist(ctx: Context, result_version: Annotated[int,Field(ge=1)], name: Annotated[str | None,Field(max_length=80)]=None) -> dict:
    """Save confirmed result version with separate human save authorization."""
    return await dispatch(ctx,"save_playlist",dict(result_version=result_version,name=name))
transports = {}


class Transport:
    async def __call__(self,scope,receive,send):
        transport = transports.get(asyncio.get_running_loop())
        if transport is None:
            raise RuntimeError("Music MCP transport has not started")
        await transport(scope,receive,send)
http_app = Starlette(routes=[Route("/api/music/mcp",endpoint=Transport())])


@asynccontextmanager
async def lifespan(app):
    # FastAPI event hooks predate this mounted MCP transport; preserve their execution.
    instance = FastMCP("VideoDream Music",stateless_http=True,json_response=True,
                       streamable_http_path="/api/music/mcp",max_request_body_size=65536)
    for name in (*auth.READ_TOOLS,"save_playlist"):
        instance.add_tool(globals()[name])
    loop = asyncio.get_running_loop()
    transports[loop] = instance.streamable_http_app()
    async with instance.session_manager.run():
        for callback in app.router.on_startup:
            result = callback()
            if inspect.isawaitable(result):
                await result
        try:
            yield
        finally:
            transports.pop(loop,None)
            for callback in app.router.on_shutdown:
                result = callback()
                if inspect.isawaitable(result):
                    await result


class LocalClient:
    def __init__(self,token):
        self.token = token

    async def request(self,name=None,arguments=None):
        async with streamablehttp_client("http://127.0.0.1:8001/api/music/mcp",
                headers={"Authorization":"Bearer "+self.token}, timeout=30,sse_read_timeout=300) as (read,write,_):
            async with ClientSession(read,write) as session:
                await session.initialize()
                if name is None:
                    return await session.list_tools()
                response = await session.call_tool(name,arguments or {})
                if response.isError:
                    message = " ".join(c.text for c in response.content if c.type=="text")
                    if name in auth.READ_TOOLS:
                        raise ModelFault("模型工具参数或执行协议错误，已停止调用")
                    raise RuntimeError(message[:500])
                if response.structuredContent:
                    return response.structuredContent
                return json.loads(next(c.text for c in response.content if c.type=="text"))

    def tools(self):
        result = asyncio.run(self.request())
        return [{"type":"function","function":{"name":t.name,"description":t.description,"parameters":t.inputSchema}}
                for t in result.tools if t.name != "save_playlist"]

    def call(self,name,arguments):
        return asyncio.run(self.request(name,arguments))

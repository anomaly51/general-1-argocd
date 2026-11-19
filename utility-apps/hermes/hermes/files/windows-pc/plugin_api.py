"""Ask the Windows Cua Driver for a PNG only while the viewer is open."""

import asyncio

from fastapi import APIRouter, HTTPException, Response
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ImageContent

router = APIRouter()
capture_lock = asyncio.Lock()
remote = StdioServerParameters(
    command="/usr/bin/ssh",
    args=[
        "-F", "/opt/windows-pc-ssh/config", "-T", "hermes-windows-pc",
        r"cua-driver.exe mcp --socket \\.\pipe\cua-driver",
    ],
)


@router.get("/frame")
async def frame(response: Response):
    if capture_lock.locked():
        raise HTTPException(429, "A frame is already being captured")
    try:
        async with capture_lock, asyncio.timeout(7):
            async with stdio_client(remote) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    result = await client.call_tool("get_desktop_state", {"max_image_dimension": 1280})
            if not result.is_error:
                for item in result.content:
                    if (
                        isinstance(item, ImageContent) and item.mime_type == "image/png"
                        and item.data.startswith("iVBORw0KGgo") and len(item.data) <= 4_000_000
                    ):
                        response.headers["Cache-Control"] = "no-store"
                        return {"png": item.data}
    except Exception:
        # Never write desktop frames, SSH errors or commands into a response or log.
        pass
    raise HTTPException(503, "Windows desktop unavailable")

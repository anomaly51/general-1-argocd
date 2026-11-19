import asyncio
import importlib.util
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, Response
from mcp.types import CallToolResult, ImageContent, TextContent

plugin_path = Path(__file__).resolve().parents[1] / "files/windows-pc/plugin_api.py"
spec = importlib.util.spec_from_file_location("windows_pc_viewer", plugin_path)
viewer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(viewer)


class WindowsPcViewerTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, capture):
        @asynccontextmanager
        async def transport(parameters):
            yield object(), object()

        client = AsyncMock()
        client.__aenter__.return_value = client
        client.call_tool.return_value = capture
        response = Response()
        with patch.object(viewer, "stdio_client", transport), patch.object(
            viewer, "ClientSession", return_value=client
        ):
            payload = await viewer.frame(response)
        client.call_tool.assert_awaited_once_with("get_desktop_state", {"max_image_dimension": 1280})
        return payload, response

    async def test_returns_png_without_caching(self):
        image = ImageContent(type="image", mime_type="image/png", data="iVBORw0KGgoAAAA")
        payload, response = await self.fetch(CallToolResult(content=[image]))
        self.assertEqual(payload, {"png": image.data})
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_unavailable_desktop_keeps_details_private(self):
        error = TextContent(type="text", text="private diagnostics")
        with self.assertRaises(HTTPException) as result:
            await self.fetch(CallToolResult(content=[error], is_error=True))
        self.assertEqual(result.exception.status_code, 503)
        self.assertEqual(result.exception.detail, "Windows desktop unavailable")

    async def test_concurrent_viewer_does_not_start_capture(self):
        with patch.object(viewer, "capture_lock", asyncio.Lock()):
            async with viewer.capture_lock:
                with self.assertRaises(HTTPException) as result:
                    await viewer.frame(Response())
        self.assertEqual(result.exception.status_code, 429)

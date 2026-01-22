"""Control plane server for Container B - operator controls."""

import asyncio
import logging
from typing import Optional
from time import time_ns

from aiohttp import web
import orjson

from .types import Event, ControlCommandEvent

logger = logging.getLogger(__name__)


class ControlPlaneServer:
    """
    Local-only HTTP server for operator commands.
    
    Endpoints:
    - POST /pause_quoting
    - POST /set_mode
    - POST /flatten_now
    - POST /set_limits
    - GET /status
    """
    
    def __init__(
        self,
        bind_host: str = "127.0.0.1",
        port: int = 9000,
        out_queue: Optional[asyncio.Queue[Event]] = None,
    ):
        """
        Initialize the control plane server.
        
        Args:
            bind_host: Host to bind to (default localhost only)
            port: Port to bind to
            out_queue: Queue to emit events to (reducer queue)
        """
        self.bind_host = bind_host
        self.port = port
        self._out_queue = out_queue
        
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        
        # State for status endpoint
        self._status_callback: Optional[callable] = None
    
    def set_status_callback(self, callback: callable) -> None:
        """Set callback to get current status."""
        self._status_callback = callback
    
    async def _emit_command(self, command_type: str, payload: dict) -> None:
        """Emit a control command to the reducer."""
        event = ControlCommandEvent(
            event_type=None,
            ts_local_ms=time_ns() // 1_000_000,
            command_type=command_type,
            payload=payload,
        )
        
        if self._out_queue:
            try:
                self._out_queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Control plane queue full")
    
    async def handle_pause_quoting(self, request: web.Request) -> web.Response:
        """
        Handle POST /pause_quoting
        
        Body: {"pause": true/false}
        """
        try:
            data = await request.json()
            pause = data.get("pause", True)
            
            await self._emit_command("pause_quoting", {"pause": pause})
            
            return web.Response(
                status=200,
                content_type="application/json",
                body=orjson.dumps({"ok": True, "pause": pause}),
            )
        except Exception as e:
            return web.Response(
                status=400,
                content_type="application/json",
                body=orjson.dumps({"error": str(e)}),
            )
    
    async def handle_set_mode(self, request: web.Request) -> web.Response:
        """
        Handle POST /set_mode
        
        Body: {"mode": "NORMAL" | "REDUCE_ONLY" | "FLATTEN" | "HALT"}
        """
        try:
            data = await request.json()
            mode = data.get("mode", "")
            
            valid_modes = ["NORMAL", "REDUCE_ONLY", "FLATTEN", "HALT"]
            if mode.upper() not in valid_modes:
                return web.Response(
                    status=400,
                    content_type="application/json",
                    body=orjson.dumps({"error": f"Invalid mode. Must be one of {valid_modes}"}),
                )
            
            await self._emit_command("set_mode", {"mode": mode.upper()})
            
            return web.Response(
                status=200,
                content_type="application/json",
                body=orjson.dumps({"ok": True, "mode": mode.upper()}),
            )
        except Exception as e:
            return web.Response(
                status=400,
                content_type="application/json",
                body=orjson.dumps({"error": str(e)}),
            )
    
    async def handle_flatten_now(self, request: web.Request) -> web.Response:
        """
        Handle POST /flatten_now
        
        Immediately triggers flatten mode.
        """
        try:
            await self._emit_command("flatten_now", {})
            
            logger.warning("OPERATOR: Flatten Now triggered")
            
            return web.Response(
                status=200,
                content_type="application/json",
                body=orjson.dumps({"ok": True, "action": "flatten_now"}),
            )
        except Exception as e:
            return web.Response(
                status=400,
                content_type="application/json",
                body=orjson.dumps({"error": str(e)}),
            )
    
    async def handle_set_limits(self, request: web.Request) -> web.Response:
        """
        Handle POST /set_limits
        
        Body: {"max_reserved_capital": ..., "max_order_size": ..., ...}
        """
        try:
            data = await request.json()
            
            await self._emit_command("set_limits", data)
            
            return web.Response(
                status=200,
                content_type="application/json",
                body=orjson.dumps({"ok": True, "limits": data}),
            )
        except Exception as e:
            return web.Response(
                status=400,
                content_type="application/json",
                body=orjson.dumps({"error": str(e)}),
            )
    
    async def handle_status(self, request: web.Request) -> web.Response:
        """
        Handle GET /status
        
        Returns current system status.
        """
        try:
            status = {}
            
            if self._status_callback:
                status = self._status_callback()
            
            return web.Response(
                status=200,
                content_type="application/json",
                body=orjson.dumps(status),
            )
        except Exception as e:
            return web.Response(
                status=500,
                content_type="application/json",
                body=orjson.dumps({"error": str(e)}),
            )
    
    async def start(self) -> None:
        """Start the HTTP server."""
        self._app = web.Application()
        
        # Add routes
        self._app.router.add_post("/pause_quoting", self.handle_pause_quoting)
        self._app.router.add_post("/set_mode", self.handle_set_mode)
        self._app.router.add_post("/flatten_now", self.handle_flatten_now)
        self._app.router.add_post("/set_limits", self.handle_set_limits)
        self._app.router.add_get("/status", self.handle_status)
        
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        
        self._site = web.TCPSite(self._runner, self.bind_host, self.port)
        await self._site.start()
        
        logger.info(f"Control plane started on http://{self.bind_host}:{self.port}")
    
    async def stop(self) -> None:
        """Stop the HTTP server."""
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        
        logger.info("Control plane stopped")

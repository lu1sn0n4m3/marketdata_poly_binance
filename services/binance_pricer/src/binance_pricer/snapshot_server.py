"""HTTP server for Container A snapshot endpoint."""

import asyncio
import logging
from typing import Optional

from aiohttp import web
import orjson

from .snapshot_store import LatestSnapshotStore

logger = logging.getLogger(__name__)


class SnapshotServer:
    """
    HTTP server that serves the latest snapshot.
    
    Endpoints:
    - GET /snapshot/latest - Returns the latest BinanceSnapshot as JSON
    - GET /health - Health check endpoint
    """
    
    def __init__(
        self,
        store: LatestSnapshotStore,
        host: str = "0.0.0.0",
        port: int = 8080,
    ):
        """
        Initialize the server.
        
        Args:
            store: LatestSnapshotStore to serve from
            host: Host to bind to
            port: Port to bind to
        """
        self.store = store
        self.host = host
        self.port = port
        
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
    
    async def handle_get_latest(self, request: web.Request) -> web.Response:
        """
        Handle GET /snapshot/latest.
        
        Returns the latest snapshot as JSON.
        """
        snapshot = self.store.read_latest_sync()
        
        if snapshot is None:
            return web.Response(
                status=503,
                content_type="application/json",
                body=orjson.dumps({"error": "No snapshot available"}),
            )
        
        return web.Response(
            status=200,
            content_type="application/json",
            body=orjson.dumps(snapshot.to_dict()),
        )
    
    async def handle_health(self, request: web.Request) -> web.Response:
        """
        Handle GET /health.
        
        Returns health status.
        """
        snapshot = self.store.read_latest_sync()
        
        healthy = snapshot is not None and not snapshot.stale
        status_code = 200 if healthy else 503
        
        health_data = {
            "healthy": healthy,
            "has_snapshot": snapshot is not None,
            "stale": snapshot.stale if snapshot else True,
            "seq": snapshot.seq if snapshot else 0,
            "age_ms": snapshot.age_ms if snapshot else -1,
        }
        
        return web.Response(
            status=status_code,
            content_type="application/json",
            body=orjson.dumps(health_data),
        )
    
    async def start(self) -> None:
        """Start the HTTP server."""
        self._app = web.Application()
        self._app.router.add_get("/snapshot/latest", self.handle_get_latest)
        self._app.router.add_get("/health", self.handle_health)
        
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        
        logger.info(f"Snapshot server started on http://{self.host}:{self.port}")
    
    async def stop(self) -> None:
        """Stop the HTTP server."""
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        
        logger.info("Snapshot server stopped")

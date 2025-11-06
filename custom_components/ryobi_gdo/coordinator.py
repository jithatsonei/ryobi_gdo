"""DataUpdateCoordinator for ryobi_gdo."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging

from aiohttp import ClientError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import RyobiApiClient
from .const import CONF_DEVICE_ID, COORDINATOR, DOMAIN

LOGGER = logging.getLogger(__name__)


class RyobiDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(self, hass: HomeAssistant, interval: int, config: ConfigEntry, session) -> None:
        """Initialize."""
        self.interval = timedelta(seconds=interval)
        self.name = f"Ryobi GDO ({config.data.get(CONF_DEVICE_ID)})"
        self.config = config
        self.hass = hass
        self._data = {}
        self.client = RyobiApiClient(
            config.data.get(CONF_USERNAME, ""),
            config.data.get(CONF_PASSWORD, ""),
            session,
            config.data.get(CONF_DEVICE_ID, ""),
        )
        self.client.callback = self.websocket_update

        LOGGER.debug("Data will be update every %s", self.interval)

        super().__init__(hass, LOGGER, name=self.name, update_interval=self.interval)

    async def _async_update_data(self):
        """Return data."""
        result = await self.client.update()
        if result:
            self._data = self.client._data
            return self._data
        raise UpdateFailed()

    async def send_command(self, device: str, command: str, value: bool):
        """Send command to GDO."""
        await self._websocket_check()
        module = self.client.get_module(device)
        module_type = self.client.get_module_type(device)
        data = (module, module_type, command, value)
        ws = self.client.ws
        if ws is None:
            LOGGER.error("Websocket client is not connected, cannot send command")
            return

        if not await ws.wait_until_connected(timeout=10):
            LOGGER.error("Timed out waiting for websocket to connect; dropping command")
            return

        if await ws.send_message(*data):
            return

        LOGGER.warning("Websocket send failed, attempting to reopen connection")
        await self._websocket_check(force_restart=True)

        ws = self.client.ws
        if ws is None:
            LOGGER.error("Websocket client unavailable after reconnect attempt")
            return

        if not await ws.wait_until_connected(timeout=15):
            LOGGER.error(
                "Timed out waiting for websocket to recover after reconnect attempt"
            )
            return

        if not await ws.send_message(*data):
            LOGGER.error("Failed to send command after websocket reconnect")

    async def _websocket_check(self, *, force_restart: bool = False):
        """Handle reconnection of websocket."""
        ws = self.client.ws
        if ws is not None:
            transport_open = ws.has_open_transport()
            if force_restart or ws.state not in ("connected", "starting") or not transport_open:
                last_seen = (
                    datetime.fromtimestamp(ws.last_msg, tz=UTC).isoformat()
                    if ws.last_msg
                    else "unknown"
                )
                LOGGER.warning(
                    "Websocket inactive since %s (state=%s transport=%s)",
                    last_seen,
                    ws.state,
                    "open" if transport_open else "closing",
                )
                if ws.state != "stopped":
                    await ws.mark_unavailable("stale transport")

        if not self.client.ws_listening:
            LOGGER.debug("Attempting websocket reconnection")
            try:
                await self.client.ws_connect()
            except (ClientError, TimeoutError) as err:
                LOGGER.error("Error reconnecting websocket: %s", err)

    async def websocket_update(self):
        """Trigger processing updated websocket data."""
        LOGGER.debug("Processing websocket data")
        ws = self.client.ws
        # Only check websocket if not already stopped/disconnected
        if ws is not None and ws.state not in ("stopped", "disconnected"):
            await self._websocket_check()
        self._data = self.client._data
        coordinator = self.hass.data[DOMAIN][self.config.entry_id][COORDINATOR]
        coordinator.async_set_updated_data(self._data)

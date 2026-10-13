"""RTKey camera entities."""
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.helpers.device_registry import DeviceInfo
from .const import DOMAIN
from .api import RTKeyAPIError
import logging
import asyncio
_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    api = hass.data[DOMAIN][entry.entry_id]
    try:
        cameras = await api.cameras()
    except RTKeyAPIError as err:
        _LOGGER.warning("RTKey camera entities unavailable (%s)", type(err).__name__)
        cameras = list(api._cameras)
        async def _recover_cameras():
            for _ in range(12):
                await asyncio.sleep(1800)
                try:
                    found = await api.cameras(force=True)
                except RTKeyAPIError:
                    continue
                if found:
                    _LOGGER.info("RTKey camera API recovered; reloading integration")
                    await hass.config_entries.async_reload(entry.entry_id)
                    return
        hass.async_create_background_task(
            _recover_cameras(), "RTKey camera recovery",
        )
    async_add_entities([RTKeyCamera(entry, api, item) for item in cameras
                        if item.get("uid") is not None])

class RTKeyCamera(Camera):
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, entry, api, item):
        super().__init__()
        self.api = api
        self.camera_id = item["uid"]
        self._attr_unique_id = f"{entry.entry_id}-camera-{self.camera_id}"
        self._attr_name = str(item.get("title") or f"Камера {self.camera_id}")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_camera_{self.camera_id}")},
            name=self._attr_name,
            manufacturer="Ростелеком")

    async def stream_source(self):
        try:
            return await self.api.stream_url(self.camera_id)
        except RTKeyAPIError as err:
            _LOGGER.warning("RTKey stream URL unavailable (%s)", type(err).__name__)
            return None

    async def async_camera_image(self, width=None, height=None):
        return await self.api.snapshot(self.camera_id)

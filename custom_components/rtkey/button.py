"""Momentary intercom open action, represented as a button."""
from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.device_registry import DeviceInfo
from .const import DOMAIN

async def async_setup_entry(hass, entry, async_add_entities):
    api = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([RTKeyOpenButton(entry, api, item)
                        for item in await api.intercoms() if item.get("id") is not None])

class RTKeyOpenButton(ButtonEntity):
    _attr_icon = "mdi:door-open"

    def __init__(self, entry, api, item):
        self.api = api
        self.intercom_id = item["id"]
        name = str(item.get("name_by_company") or item.get("name")
                   or f"Домофон {self.intercom_id}")
        self._attr_unique_id = f"{entry.entry_id}-open-{self.intercom_id}"
        self._attr_name = f"{name} — открыть"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_intercom_{self.intercom_id}")},
            name=name, manufacturer="Ростелеком")

    async def async_press(self):
        await self.api.open_door(self.intercom_id)

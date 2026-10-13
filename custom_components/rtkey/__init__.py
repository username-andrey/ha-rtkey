"""RTKey integration for Home Assistant."""
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from .api import RTKeyAPI, RTKeyAPIError, RTKeyAuthError
from .const import DOMAIN, CONF_TOKEN, CONF_DEVICE_ID

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["camera", "button"]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    api = RTKeyAPI(hass, entry.data[CONF_TOKEN], entry.data.get(CONF_DEVICE_ID), entry.entry_id)
    await api.load_camera_cache()
    try:
        await api.intercoms()
        try:
            await api.cameras()
        except RTKeyAPIError as err:
            # Camera API is independent of intercom API; keep door controls online.
            _LOGGER.warning("RTKey cameras temporarily unavailable (%s)", type(err).__name__)
    except RTKeyAuthError as err:
        raise ConfigEntryAuthFailed("RTKey authentication failed") from err
    except RTKeyAPIError as err:
        _LOGGER.warning("RTKey startup failed (%s)", type(err).__name__)
        raise ConfigEntryNotReady("RTKey cloud service unavailable") from None
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = api
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return ok

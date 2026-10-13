"""SMS login config flow based on observed RTKey web API."""
import uuid
import logging
_LOGGER = logging.getLogger(__name__)
import voluptuous as vol
from homeassistant import config_entries
from .api import RTKeyAPI, RTKeyAPIError, RTKeyAuthError
from .const import DOMAIN, CONF_TOKEN, CONF_DEVICE_ID

class RTKeyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            phone = user_input["phone"].strip()
            self._device_id = str(uuid.uuid4())
            api = RTKeyAPI(self.hass, "", self._device_id)
            try:
                result = await api.send_code(phone)
                self._code_id = result["data"]["codeId"]
                self._phone = phone
                return await self.async_step_sms()
            except (RTKeyAPIError, KeyError, TypeError) as err:
                _LOGGER.warning("RTKey SMS flow error (%s)", type(err).__name__)
                errors["base"] = "cannot_connect"
        return self.async_show_form(step_id="user",
            data_schema=vol.Schema({vol.Required("phone"): str}), errors=errors)

    async def async_step_sms(self, user_input=None):
        errors = {}
        if user_input is not None:
            api = RTKeyAPI(self.hass, "", self._device_id)
            try:
                result = await api.login(self._code_id, user_input["code"].strip())
                token = result["data"]["accessToken"]
                if not token:
                    raise KeyError("accessToken")
            except RTKeyAuthError:
                errors["base"] = "invalid_auth"
            except (RTKeyAPIError, KeyError, TypeError) as err:
                _LOGGER.warning("RTKey SMS flow error (%s)", type(err).__name__)
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(self._phone)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Ростелеком Ключ",
                    data={CONF_TOKEN: token, CONF_DEVICE_ID: self._device_id})
        return self.async_show_form(step_id="sms",
            data_schema=vol.Schema({vol.Required("code"): str}), errors=errors)

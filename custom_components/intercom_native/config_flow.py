"""Config flow for Intercom Native."""

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow
from homeassistant.helpers.selector import BooleanSelector, NumberSelector, NumberSelectorConfig

from .const import (
    DOMAIN,
    INTERCOM_PORT,
    INTERCOM_UDP_AUDIO_PORT,
    INTERCOM_UDP_CONTROL_PORT,
)
from .audio_format import UDP_SAFE_PAYLOAD_BYTES


def _port_selector():
    return NumberSelector(NumberSelectorConfig(min=1, max=65535, step=1, mode="box"))


class IntercomNativeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Intercom Native."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle install/reconfigure of the single Intercom Native entry.

        Two transport toggles, four port knobs. Defaults follow the
        project standard: 6054 for the data plane (TCP and UDP audio
        share the number on different protocols), 6055 for UDP control.
        """
        current_entry = next(iter(self._async_current_entries()), None)
        existing = current_entry.data if current_entry else {}
        defaults = {
            "use_tcp": existing.get("use_tcp", True),
            "use_udp": existing.get("use_udp", False),
            "tcp_port": existing.get("tcp_port", INTERCOM_PORT),
            "udp_audio_port": existing.get("udp_audio_port", INTERCOM_UDP_AUDIO_PORT),
            "udp_control_port": existing.get("udp_control_port", INTERCOM_UDP_CONTROL_PORT),
            "udp_max_payload": existing.get("udp_max_payload", UDP_SAFE_PAYLOAD_BYTES),
            "advertise_host": existing.get("advertise_host", ""),
        }
        schema = vol.Schema(
            {
                vol.Required("use_tcp", default=defaults["use_tcp"]): BooleanSelector(),
                vol.Required("tcp_port", default=defaults["tcp_port"]): _port_selector(),
                vol.Required("use_udp", default=defaults["use_udp"]): BooleanSelector(),
                vol.Required("udp_audio_port", default=defaults["udp_audio_port"]): _port_selector(),
                vol.Required("udp_control_port", default=defaults["udp_control_port"]): _port_selector(),
                vol.Required("udp_max_payload", default=defaults["udp_max_payload"]): NumberSelector(
                    NumberSelectorConfig(min=576, max=65507, step=1, mode="box")
                ),
                vol.Optional("advertise_host", default=defaults["advertise_host"]): str,
            }
        )
        errors: dict[str, str] = {}

        if user_input is not None:
            # Number selectors hand floats; coerce to int once on the boundary.
            for k in ("tcp_port", "udp_audio_port", "udp_control_port", "udp_max_payload"):
                user_input[k] = int(user_input[k])
            user_input["advertise_host"] = (user_input.get("advertise_host") or "").strip()

            if not user_input["use_tcp"] and not user_input["use_udp"]:
                errors["base"] = "at_least_one_transport"
            elif user_input["use_udp"] and user_input["udp_audio_port"] == user_input["udp_control_port"]:
                errors["base"] = "udp_ports_must_differ"
            elif current_entry is not None:
                return self.async_update_reload_and_abort(
                    current_entry,
                    data=user_input,
                    reason="reconfigure_successful",
                )
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Intercom Native", data=user_input)

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

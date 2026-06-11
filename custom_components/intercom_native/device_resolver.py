"""Cached intercom-device discovery; one scan per HA instance, cache
invalidated on registry change."""

from __future__ import annotations

import logging
from typing import Optional

from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .audio_format import UDP_SAFE_PAYLOAD_BYTES, parse_audio_format_list, require_udp_safe_formats
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
_CACHE_KEY = "device_resolver"


def _format_tokens(formats: list) -> list[str]:
    return [fmt.wire_token() for fmt in formats]


def slugify_route_id(raw: str) -> str:
    """Match the slug ESPHome uses for `esphome.{slug}_start_call` services."""
    return "".join(c if c.isalnum() else "_" for c in (raw or "").lower()).strip("_")


def _esphome_entry_for_host(hass: HomeAssistant, host: str):
    for entry in hass.config_entries.async_entries("esphome"):
        if entry.data.get("host") == host:
            return entry
    return None


def _valid_port(value: str) -> int | None:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def _valid_audio_mode(value: str | None) -> str:
    mode = (value or "full_duplex").strip().lower()
    if mode in ("full_duplex", "mic_only", "speaker_only", "control_only"):
        return mode
    return "full_duplex"


def _match_name(value: str | None, *candidates: str | None) -> bool:
    wanted = (value or "").strip()
    if not wanted:
        return False
    wanted_slug = slugify_route_id(wanted).replace("_", "-")
    for candidate in candidates:
        text = (candidate or "").strip()
        if not text:
            continue
        if text == wanted:
            return True
        if text.lower() == wanted.lower():
            return True
        if slugify_route_id(text).replace("_", "-") == wanted_slug:
            return True
    return False


def parse_intercom_endpoint(value: str | None) -> dict | None:
    """Parse the project endpoint standard published by ESP intercom_api.

    TCP: Name|tcp|IP|tcp_port[|audio_mode[|tx_formats|rx_formats]]
    UDP: Name|udp|IP|audio_port|control_port[|audio_mode[|tx_formats|rx_formats]]
    """
    if not value:
        return None
    text = value.strip()
    if not text or text.lower() in ("unknown", "unavailable"):
        return None
    parts = [part.strip() for part in text.split("|")]
    if len(parts) < 4:
        return None

    name, transport, host = parts[0], parts[1].lower(), parts[2]
    if not name or not host or transport not in ("tcp", "udp"):
        return None

    def parse_formats(first: int) -> tuple[str, list, list] | None:
        mode = _valid_audio_mode(parts[first] if len(parts) > first else None)
        try:
            tx_formats = parse_audio_format_list(parts[first + 1] if len(parts) > first + 1 else None)
            rx_formats = parse_audio_format_list(parts[first + 2] if len(parts) > first + 2 else None)
        except ValueError as err:
            _LOGGER.warning("Invalid intercom endpoint audio formats in %r: %s", text, err)
            return None
        return mode, tx_formats, rx_formats

    if transport == "tcp":
        tcp_port = _valid_port(parts[3])
        if tcp_port is None or len(parts) not in (4, 5, 6, 7):
            return None
        parsed_tail = parse_formats(4)
        if parsed_tail is None:
            return None
        mode, tx_formats, rx_formats = parsed_tail
        return {
            "name": name,
            "transport": "tcp",
            "host": host,
            "tcp_port": tcp_port,
            "udp_audio_port": None,
            "udp_control_port": None,
            "audio_mode": mode,
            "tx_formats": tx_formats,
            "rx_formats": rx_formats,
        }

    if len(parts) not in (5, 6, 7, 8):
        return None
    audio_port = _valid_port(parts[3])
    control_port = _valid_port(parts[4])
    if audio_port is None or control_port is None:
        return None
    parsed_tail = parse_formats(5)
    if parsed_tail is None:
        return None
    mode, tx_formats, rx_formats = parsed_tail
    return {
        "name": name,
        "transport": "udp",
        "host": host,
        "tcp_port": None,
        "udp_audio_port": audio_port,
        "udp_control_port": control_port,
        "audio_mode": mode,
        "tx_formats": tx_formats,
        "rx_formats": rx_formats,
    }


class IntercomDeviceResolver:
    """Single source of truth for "which intercom devices exist"."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._devices: Optional[list[dict]] = None
        self._unsubs: list = []

    def install_listeners(self) -> None:
        """Idempotent."""
        if self._unsubs:
            return

        @callback
        def _invalidate(_event) -> None:
            self._devices = None

        self._unsubs.append(
            self.hass.bus.async_listen("entity_registry_updated", _invalidate)
        )
        self._unsubs.append(
            self.hass.bus.async_listen("device_registry_updated", _invalidate)
        )

        @callback
        def _invalidate_endpoint(event) -> None:
            entity_id = event.data.get("entity_id") or ""
            if "intercom_endpoint" in entity_id:
                self._devices = None

        self._unsubs.append(
            self.hass.bus.async_listen("state_changed", _invalidate_endpoint)
        )

    def shutdown(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()
        self._devices = None

    def route_id_for_host(self, host: str) -> str:
        """ESPHome node_name slug for `host`. Used as PBX-lite route_id and
        as the prefix for `esphome.{slug}_start_call`."""
        entry = _esphome_entry_for_host(self.hass, host)
        if entry is None:
            # Fallback: hostname (.local) in entry.data["host"] vs IP from
            # device_registry. Walk device_registry for any device whose
            # connections include this IP, then walk its config_entries.
            entry = self._esphome_entry_via_device(host)
            if entry is None:
                return ""
        raw = entry.data.get("device_name") or entry.title or ""
        return slugify_route_id(raw)

    def _esphome_entry_via_device(self, host: str):
        device_registry = dr.async_get(self.hass)
        for device in device_registry.devices.values():
            for conn_type, conn_value in device.connections:
                if conn_value != host:
                    continue
                if 'ip' not in conn_type.lower() and conn_type != 'network_ip':
                    continue
                for entry_id in device.config_entries:
                    entry = self.hass.config_entries.async_get_entry(entry_id)
                    if entry and entry.domain == "esphome":
                        return entry
        return None

    async def list_devices(self) -> list[dict]:
        """Cached intercom device list (registry events invalidate)."""
        if self._devices is not None:
            return self._devices

        entity_registry = er.async_get(self.hass)
        device_registry = dr.async_get(self.hass)

        # First pass: device IDs owning an intercom_endpoint entity, plus
        # an entities-by-device bucket for the second pass.
        intercom_device_ids: set[str] = set()
        entities_by_device: dict[str, list] = {}
        for entity in entity_registry.entities.values():
            if entity.device_id is None:
                continue
            entities_by_device.setdefault(entity.device_id, []).append(entity)
            if "intercom_endpoint" in entity.entity_id:
                intercom_device_ids.add(entity.device_id)

        out: list[dict] = []
        for device_id in intercom_device_ids:
            device = device_registry.async_get(device_id)
            if not device:
                continue

            esphome_id = self._device_esphome_id(device)
            entities = self._collect_entities(entities_by_device.get(device_id, []))
            endpoint_entity_id = entities.get("intercom_endpoint")
            endpoint_state = self.hass.states.get(endpoint_entity_id) if endpoint_entity_id else None
            endpoint = parse_intercom_endpoint(endpoint_state.state if endpoint_state else None)
            if endpoint is None:
                _LOGGER.debug(
                    "Skipping intercom device %s: missing/invalid intercom_endpoint",
                    device.name or esphome_id or device_id,
                )
                continue
            if endpoint["transport"] == "udp":
                max_payload = int(
                    self.hass.data.get(DOMAIN, {})
                    .get("transport_config", {})
                    .get("udp_max_payload", UDP_SAFE_PAYLOAD_BYTES)
                )
                try:
                    require_udp_safe_formats(
                        endpoint["tx_formats"],
                        context=f"{endpoint['name']} UDP tx_formats",
                        max_payload=max_payload,
                    )
                    require_udp_safe_formats(
                        endpoint["rx_formats"],
                        context=f"{endpoint['name']} UDP rx_formats",
                        max_payload=max_payload,
                    )
                except ValueError as err:
                    _LOGGER.warning("Skipping UDP intercom device %s: %s", endpoint["name"], err)
                    continue

            route_id = self.route_id_for_host(endpoint["host"])
            if not route_id:
                route_id = self._route_id_from_device(device)

            out.append({
                "device_id": device_id,
                "name": endpoint["name"],
                "route_id": route_id,
                "host": endpoint["host"],
                "transport": endpoint["transport"],
                "tcp_port": endpoint["tcp_port"],
                "udp_audio_port": endpoint["udp_audio_port"],
                "udp_control_port": endpoint["udp_control_port"],
                "udp_max_payload": (
                    int(
                        self.hass.data.get(DOMAIN, {})
                        .get("transport_config", {})
                        .get("udp_max_payload", UDP_SAFE_PAYLOAD_BYTES)
                    )
                    if endpoint["transport"] == "udp"
                    else UDP_SAFE_PAYLOAD_BYTES
                ),
                "audio_mode": endpoint["audio_mode"],
                "tx_formats": _format_tokens(endpoint["tx_formats"]),
                "rx_formats": _format_tokens(endpoint["rx_formats"]),
                "esphome_id": esphome_id,
                "entities": entities,
            })

        self._devices = out
        return out

    async def resolve_target(self, call: ServiceCall) -> Optional[dict]:
        """Match a service call's target selector to one of our devices."""
        device_ids: set[str] = set()
        names: list[str] = []
        entity_registry = er.async_get(self.hass)
        for source in [call.data, getattr(call, "target", None) or {}]:
            ids = source.get("device_id")
            if isinstance(ids, str):
                device_ids.add(ids)
            elif isinstance(ids, list):
                device_ids.update(ids)
            eids = source.get("entity_id")
            if isinstance(eids, str):
                eids = [eids]
            if eids:
                for eid in eids:
                    entry = entity_registry.async_get(eid)
                    if entry and entry.device_id:
                        device_ids.add(entry.device_id)
            name = source.get("name") or source.get("friendly_name")
            if isinstance(name, str):
                names.append(name)
        if not device_ids and not names:
            return None
        for dev in await self.list_devices():
            if dev["device_id"] in device_ids:
                return dev
            if any(_match_name(name, dev.get("name"), dev.get("esphome_id"), dev.get("route_id")) for name in names):
                return dev
        return None

    async def resolve_selector(self, selector: str | None) -> Optional[dict]:
        """Resolve a legacy card selector once and return the canonical device."""
        wanted = (selector or "").strip()
        if not wanted:
            return None
        for dev in await self.list_devices():
            if dev.get("device_id") == wanted:
                return dev
            if _match_name(wanted, dev.get("name"), dev.get("esphome_id"), dev.get("route_id")):
                return dev
        return None

    def _route_id_from_device(self, device) -> str:
        """Walk device.config_entries; slugify the linked esphome entry."""
        for entry_id in device.config_entries:
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry and entry.domain == "esphome":
                raw = entry.data.get("device_name") or entry.title or ""
                return slugify_route_id(raw)
        return ""

    @staticmethod
    def _device_esphome_id(device) -> Optional[str]:
        for domain, identifier in device.identifiers:
            if domain == "esphome":
                return identifier
        return None

    @staticmethod
    def _collect_entities(entities) -> dict[str, str]:
        out: dict[str, str] = {}
        for entity in entities:
            eid = entity.entity_id
            if "intercom_state" in eid and "intercom_state" not in out:
                out["intercom_state"] = eid
            elif "intercom_endpoint" in eid and "intercom_endpoint" not in out:
                out["intercom_endpoint"] = eid
            elif "intercom_transport" in eid and "intercom_transport" not in out:
                # source of truth for "udp"/"tcp" (no mDNS-timing dep).
                out["intercom_transport"] = eid
            elif ("incoming_caller" in eid or eid.endswith("_caller")) and "incoming_caller" not in out:
                out["incoming_caller"] = eid
            elif "destination" in eid and "destination" not in out:
                out["destination"] = eid
            elif (
                ("intercom_last_reason" in eid or "last_reason" in eid or "end_reason" in eid)
                and "last_reason" not in out
            ):
                out["last_reason"] = eid
            elif eid.startswith("button.") and "previous" in eid and "previous" not in out:
                out["previous"] = eid
            elif eid.startswith("button.") and "next" in eid and "next" not in out:
                out["next"] = eid
            elif eid.startswith("button.") and "decline" in eid and "decline" not in out:
                out["decline"] = eid
            elif eid.startswith("button.") and "call" in eid and "decline" not in eid and "call" not in out:
                out["call"] = eid
        return out


def get_resolver(hass: HomeAssistant) -> IntercomDeviceResolver:
    bucket = hass.data.setdefault(DOMAIN, {})
    resolver = bucket.get(_CACHE_KEY)
    if resolver is None:
        resolver = IntercomDeviceResolver(hass)
        resolver.install_listeners()
        bucket[_CACHE_KEY] = resolver
    return resolver

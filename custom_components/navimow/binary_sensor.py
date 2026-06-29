"""Binary sensor platform for Navimow integration."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NavimowCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Navimow binary sensors from a config entry."""
    data = hass.data[DOMAIN][config_entry.entry_id]
    devices = data["devices"]
    coordinators: dict[str, NavimowCoordinator] = data["coordinators"]

    entities = [
        NavimowChargingSensor(coordinators[device.id]) for device in devices
    ]
    async_add_entities(entities)


class NavimowChargingSensor(
    CoordinatorEntity[NavimowCoordinator], BinarySensorEntity
):
    """Battery charging state, inferred from the battery trend.

    The Navimow SDK does not report a charging status (see ChargeTracker), so
    this entity reflects the coordinator's inferred charging state.
    """

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING
    _attr_translation_key = "battery_charging"

    def __init__(self, coordinator: NavimowCoordinator) -> None:
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{DOMAIN}_{device.id}_battery_charging"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            name=device.name,
            manufacturer="Navimow",
            model=device.model or "Unknown",
            sw_version=device.firmware_version or None,
            serial_number=device.serial_number or device.id,
        )

    @property
    def is_on(self) -> bool | None:
        """Return True if charging, False if not, None until a trend is known."""
        return self.coordinator.charging

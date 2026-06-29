"""DataUpdateCoordinator for Navimow integration."""
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from mower_sdk.api import MowerAPI
from mower_sdk.models import (
    Device,
    DeviceAttributesMessage,
    DeviceStateMessage,
    DeviceStatus,
)
from mower_sdk.sdk import NavimowSDK

from .const import (
    CHARGE_STALL_TIMEOUT,
    DOMAIN,
    HTTP_FALLBACK_MIN_INTERVAL,
    MQTT_STALE_SECONDS,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class ChargeTracker:
    """Infers charging state from successive battery readings.

    The Navimow SDK never reports a charging status — its ``MowerStatus.CHARGING``
    value is unreachable because the cloud ``vehicleState`` has no charging value
    and the docked mower reports ``isDocked``. Charging is therefore inferred from
    the battery trend: a rising level means charging, a falling level means not,
    and a level that has been flat beyond the stall window (while not full) is
    treated as no longer charging (e.g. dock not seated, charging fault).
    """

    def __init__(self, stall_timeout: float) -> None:
        self._stall_timeout = stall_timeout
        self._charging: bool | None = None
        self._last_battery: int | None = None
        self._last_rise: float | None = None

    @property
    def charging(self) -> bool | None:
        return self._charging

    def observe(self, battery: int | None, now: float) -> bool | None:
        """Feed a battery reading; returns the inferred charging state."""
        if battery is None:
            return self._charging
        prev = self._last_battery
        if prev is None:
            # First reading: no trend yet, charging stays unknown.
            self._last_battery = battery
            self._last_rise = now
            return self._charging
        if battery > prev:
            self._charging = True
            self._last_rise = now
        elif battery < prev:
            self._charging = False
        elif (
            self._charging
            and battery < 100
            and self._last_rise is not None
            and now - self._last_rise > self._stall_timeout
        ):
            self._charging = False
        self._last_battery = battery
        return self._charging


class NavimowCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for Navimow data updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        sdk: NavimowSDK,
        api: MowerAPI,
        device: Device,
        oauth_session: config_entry_oauth2_flow.OAuth2Session | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self.sdk = sdk
        self.api = api
        self.device = device
        self.oauth_session = oauth_session
        self.data: dict[str, Any] = {}
        self._last_state: DeviceStateMessage | None = None
        self._last_attributes: DeviceAttributesMessage | None = None
        self._last_mqtt_update: float | None = None
        self._last_mqtt_state_update: float | None = None
        self._last_http_fetch: float | None = None
        self._last_data_source: str | None = None
        self._charge = ChargeTracker(CHARGE_STALL_TIMEOUT)

    @property
    def charging(self) -> bool | None:
        """Inferred battery charging state (None until a trend is known)."""
        return self._charge.charging

    async def async_setup(self) -> None:
        """Register callbacks from SDK."""
        self.sdk.on_state(self._handle_state)
        self.sdk.on_attributes(self._handle_attributes)

    def _build_data(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "state": self._last_state,
            "attributes": self._last_attributes,
            "meta": {
                "last_data_source": self._last_data_source,
                "last_mqtt_update_monotonic": self._last_mqtt_update,
                "last_mqtt_state_update_monotonic": self._last_mqtt_state_update,
                "last_http_fetch_monotonic": self._last_http_fetch,
                "charging": self._charge.charging,
            },
        }

    def _device_status_to_state(self, status: DeviceStatus) -> DeviceStateMessage:
        error: dict[str, Any] | None = None
        if status.error_code and status.error_code.value != "none":
            error = {
                "code": status.error_code.value,
                "message": status.error_message,
            }
        return DeviceStateMessage(
            device_id=status.device_id,
            timestamp=status.timestamp,
            state=status.status.value,
            battery=status.battery,
            signal_strength=status.signal_strength,
            position=status.position,
            error=error,
            metrics=None,
        )

    async def _async_ensure_valid_token(self) -> str | None:
        if not self.oauth_session:
            return None
        try:
            token: dict[str, Any] | None
            if hasattr(self.oauth_session, "async_ensure_token_valid"):
                await self.oauth_session.async_ensure_token_valid()
                token = self.oauth_session.token
            elif hasattr(self.oauth_session, "async_get_valid_token"):
                token = await self.oauth_session.async_get_valid_token()
            else:
                token = self.oauth_session.token
        except ConfigEntryAuthFailed:
            # 确定性认证失败（refresh_token 缺失或被服务端拒绝）→ 直接上报，让 HA 引导用户重新认证
            raise
        except Exception as err:
            # 瞬态错误（网络超时、DNS 等）→ 不立即触发重新认证流程。
            # 尝试沿用缓存中的 access_token；若缓存也不可用才升级为认证失败。
            _LOGGER.warning(
                "Token refresh failed (likely transient), falling back to cached token: %s", err
            )
            cached = getattr(self.oauth_session, "token", None)
            if cached and cached.get("access_token"):
                token = cached
            else:
                raise ConfigEntryAuthFailed(
                    f"Token refresh failed and no cached token available: {err}"
                ) from err
        if not token or not token.get("access_token"):
            raise ConfigEntryAuthFailed("No access token after refresh")
        access_token = token["access_token"]
        self.api.set_token(access_token)
        return access_token

    async def _async_update_data(self) -> dict[str, Any]:
        # 每次 update 都主动刷新 token，确保 api._token 与 oauth_session 保持同步。
        # 若仅在 HTTP fallback 时刷新，MQTT 正常推数据期间 token 长期不更新，
        # 过期后用户下发指令会立即收到 CODE_OAUTH_INFO_ILLEGAL。
        try:
            await self._async_ensure_valid_token()
        except ConfigEntryAuthFailed:
            raise

        cached_state = self.sdk.get_cached_state(self.device.id)
        if cached_state is not None:
            self._last_state = cached_state
            self._last_data_source = "mqtt_cache"

        cached_attrs = self.sdk.get_cached_attributes(self.device.id)
        if cached_attrs is not None:
            self._last_attributes = cached_attrs

        now = time.monotonic()
        # Use state-specific freshness here. Attributes packets can still arrive
        # while mower activity/state is stale, which would otherwise suppress
        # the HTTP fallback and leave Home Assistant showing old status.
        is_state_stale = (
            self._last_mqtt_state_update is None
            or now - self._last_mqtt_state_update > MQTT_STALE_SECONDS
        )
        can_http_fetch = (
            self._last_http_fetch is None
            or now - self._last_http_fetch > HTTP_FALLBACK_MIN_INTERVAL
        )
        if is_state_stale and not self.sdk.is_connected:
            _LOGGER.warning(
                "MQTT appears disconnected for device %s; relying on HTTP fallback",
                self.device.id,
            )
        if is_state_stale and can_http_fetch:
            try:
                status = await self.api.async_get_device_status(self.device.id)
                _LOGGER.debug(
                    "HTTP fallback success: device=%s battery=%s status=%s",
                    self.device.id,
                    status.battery,
                    status.status.value if status.status else "unknown",
                )
                self._last_state = self._device_status_to_state(status)
                self._last_http_fetch = now
                self._last_data_source = "http_fallback"
                self._charge.observe(self._last_state.battery, now)
                # Push immediately so entities update without waiting for the
                # next coordinator tick.
                self.data = self._build_data()
                self.async_set_updated_data(self.data)
            except ConfigEntryAuthFailed:
                raise
            except Exception as err:
                _LOGGER.warning(
                    "HTTP fallback failed for device %s: %s", self.device.id, err
                )

        # Covers the mqtt_cache path (battery from cached state); the fallback
        # path already observed above, re-observing the same value is a no-op.
        if self._last_state is not None:
            self._charge.observe(self._last_state.battery, now)

        _LOGGER.debug(
            "Coordinator update: device=%s source=%s mqtt_ts=%s mqtt_state_ts=%s http_ts=%s charging=%s",
            self.device.id,
            self._last_data_source,
            self._last_mqtt_update,
            self._last_mqtt_state_update,
            self._last_http_fetch,
            self._charge.charging,
        )
        self.data = self._build_data()
        return self.data

    def _handle_state(self, state: DeviceStateMessage) -> None:
        if state.device_id != self.device.id:
            return
        _LOGGER.debug(
            "MQTT state received: device=%s state=%s battery=%s",
            state.device_id,
            state.state,
            state.battery,
        )
        now = time.monotonic()
        self._last_mqtt_update = now
        self._last_mqtt_state_update = now
        self._last_data_source = "mqtt_push"
        self.hass.loop.call_soon_threadsafe(self._update_from_state, state)

    def _handle_attributes(self, attrs: DeviceAttributesMessage) -> None:
        if attrs.device_id != self.device.id:
            return
        _LOGGER.debug(
            "MQTT attributes received: device=%s",
            attrs.device_id,
        )
        self._last_mqtt_update = time.monotonic()
        self.hass.loop.call_soon_threadsafe(self._update_from_attributes, attrs)

    def _update_from_state(self, state: DeviceStateMessage) -> None:
        self._last_state = state
        self._last_data_source = "mqtt_push"
        self._charge.observe(state.battery, time.monotonic())
        self.async_set_updated_data(self._build_data())

    def _update_from_attributes(self, attrs: DeviceAttributesMessage) -> None:
        self._last_attributes = attrs
        self.async_set_updated_data(self._build_data())

    def get_device_state(self) -> DeviceStateMessage | None:
        return self.data.get("state")

    def get_device_attributes(self) -> DeviceAttributesMessage | None:
        return self.data.get("attributes")

    def get_device_info(self) -> Any | None:
        return self.data.get("device")

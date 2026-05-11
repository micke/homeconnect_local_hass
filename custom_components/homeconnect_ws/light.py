"""Light entities."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.components.light.const import DEFAULT_MAX_KELVIN, DEFAULT_MIN_KELVIN
from homeassistant.util.color import (
    brightness_to_value,
    color_rgb_to_hex,
    match_max_scale,
    rgb_hex_to_rgb_list,
    value_to_brightness,
)
from homeassistant.util.scaling import scale_ranged_value_to_int_range
from homeconnect_websocket.message import Action
from homeconnect_websocket.message import Message as HC_Message

from .entity import HCEntity
from .helpers import create_entities, error_decorator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback
    from homeconnect_websocket.entities import Entity as HcEntity

    from . import HCConfigEntry, HCData
    from .entity_descriptions.descriptions_definitions import HCLightEntityDescription

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    config_entry: HCConfigEntry,
    async_add_entites: AddEntitiesCallback,
) -> None:
    """Set up light platform."""
    entities = create_entities({"light": HCLight}, config_entry.runtime_data)
    async_add_entites(entities)


class HCLight(HCEntity, LightEntity):
    """Light Entity."""

    entity_description: HCLightEntityDescription
    _brightness_entity: HcEntity | None = None
    _color_temperature_entity: HcEntity | None = None
    _color_entity: HcEntity | None = None
    _color_mode_entity: HcEntity | None = None
    _color_temp_inverted: bool = False

    def __init__(
        self,
        entity_description: HCLightEntityDescription,
        runtime_data: HCData,
    ) -> None:
        super().__init__(entity_description, runtime_data)
        if entity_description.brightness_entity is not None:
            self._brightness_entity = self._runtime_data.appliance.entities[
                entity_description.brightness_entity
            ]
            self._entities.append(self._brightness_entity)

        if entity_description.color_temperature_entity is not None:
            self._color_temperature_entity = self._runtime_data.appliance.entities[
                entity_description.color_temperature_entity
            ]
            self._entities.append(self._color_temperature_entity)
            self._color_temp_inverted = (
                "Cooking.Hood.Setting.ColorTemperature" in self._runtime_data.appliance.entities
            )

        if entity_description.color_entity is not None:
            self._color_entity = self._runtime_data.appliance.entities[
                entity_description.color_entity
            ]
            self._entities.append(self._color_entity)

        if entity_description.color_mode_entity is not None:
            self._color_mode_entity = self._runtime_data.appliance.entities[
                entity_description.color_mode_entity
            ]
            self._entities.append(self._color_mode_entity)

        if self._color_entity:
            self._attr_supported_color_modes = {ColorMode.RGB}
            self._attr_color_mode = ColorMode.RGB
        elif self._color_temperature_entity and self._brightness_entity:
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_max_color_temp_kelvin = DEFAULT_MAX_KELVIN
            self._attr_min_color_temp_kelvin = DEFAULT_MIN_KELVIN
        elif self._brightness_entity:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
            self._attr_color_mode = ColorMode.BRIGHTNESS
        else:
            self._attr_supported_color_modes = {ColorMode.ONOFF}
            self._attr_color_mode = ColorMode.ONOFF

    @property
    def is_on(self) -> bool | None:
        return bool(self._entity.value)

    @property
    def brightness(self) -> int | None:
        if self._color_entity is not None:
            if self._color_entity.value is None:
                return None
            rgb = rgb_hex_to_rgb_list(self._color_entity.value.strip("#"))
            return max(rgb)
        if self._brightness_entity is not None:
            return value_to_brightness((1, 100), self._brightness_entity.value)
        return None

    @property
    def color_temp_kelvin(self) -> int | None:
        if self._color_temperature_entity is not None:
            if self._color_temp_inverted:
                return scale_ranged_value_to_int_range(
                    (101, 0),
                    (DEFAULT_MIN_KELVIN + 1, DEFAULT_MAX_KELVIN),
                    self._color_temperature_entity.value,
                )

            return scale_ranged_value_to_int_range(
                (1, 100),
                (DEFAULT_MIN_KELVIN + 1, DEFAULT_MAX_KELVIN),
                self._color_temperature_entity.value,
            )
        return None

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        if self._color_entity is not None:
            if self._color_entity.value is None:
                return None
            rgb = rgb_hex_to_rgb_list(self._color_entity.value.strip("#"))
            return match_max_scale((255,), rgb)
        return None

    _WRITABLE_WAIT_TIMEOUT = 3.0

    async def _wait_until_writable(self, entity: HcEntity) -> None:
        """Wait until the appliance reports the entity as writable, or give up."""
        if getattr(entity, "available", True):
            return
        event = asyncio.Event()

        async def callback(_: HcEntity) -> None:
            if getattr(entity, "available", True):
                event.set()

        entity.register_callback(callback)
        try:
            async with asyncio.timeout(self._WRITABLE_WAIT_TIMEOUT):
                await event.wait()
        except TimeoutError:
            pass
        finally:
            entity.unregister_callback(callback)

    @error_decorator
    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness = kwargs.get(ATTR_BRIGHTNESS, self.brightness)
        rgb = kwargs.get(ATTR_RGB_COLOR, self.rgb_color)

        attribute_writes: list[dict[str, Any]] = []
        wait_target: HcEntity | None = None

        if self._attr_color_mode == ColorMode.RGB and (
            ATTR_BRIGHTNESS in kwargs or ATTR_RGB_COLOR in kwargs
        ):
            if rgb is None:
                rgb = (255, 255, 255)
            if brightness is None:
                brightness = 255
            rgb_with_brightness = tuple(color * brightness // 255 for color in rgb)
            attribute_writes.append(
                {
                    "uid": self._color_entity.uid,
                    "value": "#" + color_rgb_to_hex(*rgb_with_brightness),
                }
            )
            wait_target = self._color_entity
            if (
                self._color_mode_entity is not None
                and self._color_mode_entity.value != "CustomColor"
            ):
                color_mode_value = self._color_mode_entity._rev_enumeration["CustomColor"]  # noqa: SLF001
                attribute_writes.append(
                    {"uid": self._color_mode_entity.uid, "value": color_mode_value}
                )

        elif (
            self._attr_color_mode in (ColorMode.BRIGHTNESS, ColorMode.COLOR_TEMP)
            and ATTR_BRIGHTNESS in kwargs
        ):
            value_in_range = int(
                max(
                    brightness_to_value((1, 100), brightness),
                    self._brightness_entity.min,
                )
            )
            attribute_writes.append(
                {"uid": self._brightness_entity.uid, "value": value_in_range}
            )
            wait_target = self._brightness_entity

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            if self._color_temp_inverted:
                value_in_range = int(
                    scale_ranged_value_to_int_range(
                        (DEFAULT_MIN_KELVIN + 1, DEFAULT_MAX_KELVIN),
                        (101, 0),
                        kwargs[ATTR_COLOR_TEMP_KELVIN],
                    )
                )
            else:
                value_in_range = int(
                    scale_ranged_value_to_int_range(
                        (DEFAULT_MIN_KELVIN + 1, DEFAULT_MAX_KELVIN),
                        (1, 100),
                        kwargs[ATTR_COLOR_TEMP_KELVIN],
                    )
                )
            attribute_writes.append(
                {"uid": self._color_temperature_entity.uid, "value": value_in_range}
            )
            if wait_target is None:
                wait_target = self._color_temperature_entity

        await self._dispatch_turn_on(attribute_writes, wait_target)

    async def _dispatch_turn_on(
        self,
        attribute_writes: list[dict[str, Any]],
        wait_target: HcEntity | None,
    ) -> None:
        is_off = self._entity.value is not True
        if is_off and attribute_writes:
            # Two-phase: appliances like Bosch/Siemens hoods gate sub-entities
            # on the light being on (and validate batched writes upfront), so a
            # combined message is rejected. Send the on-switch alone, wait for
            # the target sub-entity to become writable, then send the rest.
            await self._send_data([{"uid": self._entity.uid, "value": True}])
            if wait_target is not None:
                await self._wait_until_writable(wait_target)
            await self._send_data(attribute_writes)
        elif is_off:
            await self._send_data([{"uid": self._entity.uid, "value": True}])
        elif attribute_writes:
            await self._send_data(attribute_writes)

    async def _send_data(self, data: list[dict[str, Any]]) -> None:
        await self._runtime_data.appliance.session.send_sync(
            HC_Message(resource="/ro/values", action=Action.POST, data=data)
        )

    @error_decorator
    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._entity.set_value(False)

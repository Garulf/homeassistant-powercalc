from __future__ import annotations
from homeassistant.components import light

from homeassistant.core import State
import logging

from typing import Optional
from collections import defaultdict
import os;
import gzip;
from csv import reader
from functools import partial

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP,
    ATTR_COLOR_MODE,
    ATTR_HS_COLOR,
    COLOR_MODE_COLOR_TEMP,
    COLOR_MODE_HS
)

from .strategy_interface import PowerCalculationStrategyInterface
import homeassistant.helpers.entity_registry as er
from .errors import ModelNotSupported, LutFileNotFound, StrategyConfigurationError
from .light_model import LightModel

_LOGGER = logging.getLogger(__name__)

class LutRegistry:
    def __init__(self) -> None:
        self._lookup_dictionaries = {}
    
    async def get_lookup_dictionary(self, light_model: LightModel, color_mode: str) -> dict | None:
        cache_key = f'{light_model.manufacturer}_{light_model.model}_{color_mode}'
        lookup_dict = self._lookup_dictionaries.get(cache_key)
        if (lookup_dict == None):
            defaultdict_of_dict = partial(defaultdict, dict)
            lookup_dict = defaultdict(defaultdict_of_dict)

            with self.get_lut_file(light_model, color_mode) as csv_file:
                csv_reader = reader(csv_file)
                next(csv_reader) #skip header row

                for row in csv_reader:
                    if (color_mode == COLOR_MODE_HS):
                        lookup_dict[int(row[0])][int(row[1])][int(row[2])] = float(row[3])
                    else:
                        lookup_dict[int(row[0])][int(row[1])] = float(row[2])

            lookup_dict = dict(lookup_dict)
            self._lookup_dictionaries[cache_key] = lookup_dict

        return lookup_dict
    
    def get_lut_file(self, light_model: LightModel, color_mode: str):
        path = os.path.join(
            light_model.get_directory(),
            f'{color_mode}.csv'
        )

        gzip_path = f'{path}.gz'
        if (os.path.exists(gzip_path)):
            _LOGGER.debug("Loading data file: %s", gzip_path)
            return gzip.open(gzip_path, 'rt')

        elif (os.path.exists(path)):
            _LOGGER.debug("Loading data file: %s", path)
            return open(path, 'r')

        raise LutFileNotFound("Data file not found: %s")

        
class LutStrategy(PowerCalculationStrategyInterface):
    def __init__(self, lut_registry: LutRegistry, model: LightModel) -> None:
        self._lut_registry = lut_registry
        self._model = model

    async def calculate(self, light_state: State) -> Optional[int]:
        """Calculate the power consumption based on brightness, mired, hsl values."""
        attrs = light_state.attributes
        color_mode = attrs.get(ATTR_COLOR_MODE)
        brightness = attrs.get(ATTR_BRIGHTNESS)
        if (brightness == None):
            _LOGGER.error("No brightness for entity: %s", light_state.entity_id)
            return None

        try:
            lookup_table = await self._lut_registry.get_lookup_dictionary(self._model, color_mode)
        except LutFileNotFound:
            _LOGGER.error("Lookup table not found")
            return None

        power = 0
        if (color_mode == COLOR_MODE_HS):
            hs = attrs[ATTR_HS_COLOR]
            hue = int(hs[0] / 360 * 65535) 
            sat = int(hs[1] / 100 * 255)
            _LOGGER.debug("Looking up power usage for bri:%s hue:%s sat:%s}", brightness, hue, sat)
            hue_values = self.get_closest_from_dictionary(lookup_table, brightness)
            sat_values = self.get_closest_from_dictionary(hue_values, hue)
            power = self.get_closest_from_dictionary(sat_values, sat)
        elif (color_mode == COLOR_MODE_COLOR_TEMP):
            mired = attrs[ATTR_COLOR_TEMP]
            _LOGGER.debug("Looking up power usage for bri:%s mired:%s", brightness, mired)
            mired_values = self.get_closest_from_dictionary(lookup_table, brightness)
            power = self.get_closest_from_dictionary(mired_values, mired)

        _LOGGER.debug("Power:%s", power)
        return power

    def get_closest_from_dictionary(self, dict: dict, search_key):
        return dict.get(search_key) or dict[
            min(dict.keys(), key = lambda key: abs(key-search_key))
        ]
    
    async def validate_config(
        self,
        entity_entry: er.RegistryEntry,
    ):
        if (entity_entry.domain != light.DOMAIN):
            raise StrategyConfigurationError("Only light entities can use the LUT mode")

        if (self._model.manufacturer is None):
            _LOGGER.error("Manufacturer not supplied for entity: %s", entity_entry.entity_id)


        if (self._model.model is None):
            _LOGGER.error("Model not supplied for entity: %s", entity_entry.entity_id)
            return

        supported_color_modes = entity_entry.capabilities['supported_color_modes']
        for color_mode in supported_color_modes:
            try:
                await self._lut_registry.get_lookup_dictionary(self._model, color_mode)
            except LutFileNotFound:
                raise ModelNotSupported("No lookup file found for mode", color_mode)
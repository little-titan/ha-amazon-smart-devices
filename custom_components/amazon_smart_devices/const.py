"""Constants for the Amazon Smart Devices integration."""

from datetime import timedelta
import logging

LOGGER = logging.getLogger(__package__)

DOMAIN = "amazon_smart_devices"

CONF_LOGIN_DATA = "login_data"

# Alexa cloud polling. 60 s matches what the Alexa app itself does for a
# thermostat tile; going much lower risks Amazon throttling the account.
SCAN_INTERVAL = timedelta(seconds=60)

# How often the endpoint list is re-read to pick up added/renamed thermostats.
DISCOVERY_INTERVAL = timedelta(hours=6)

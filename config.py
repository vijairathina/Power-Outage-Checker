"""
Configuration loader for TNEB Scraper & Home Assistant Integration.
Loads from config.json, environment variables, or default fallbacks.
"""

import json
import os
from typing import Any, Dict

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULT_CONFIG: Dict[str, Any] = {
    "circle": "KRISHNAGIRI",
    "filter": "Hosur|Mathigiri|Mookandapalli|Zuzuvadi|Bagalur|Sipcot",
    "all_records": False,
    "daily_check_time": "08:00",
    "home_assistant": {
        "enabled": False,
        "url": "http://homeassistant.local:8123",
        "token": "",
        "entity_id": "sensor.tneb_power_outage",
        "send_persistent_notification": False
    }
}


def load_config(config_path: str = CONFIG_FILE) -> Dict[str, Any]:
    """Load configuration from file or create default template if missing."""
    config = DEFAULT_CONFIG.copy()

    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                user_config = json.load(f)
                config.update(user_config)
                # Merge nested home_assistant dict
                if "home_assistant" in user_config:
                    ha_merged = DEFAULT_CONFIG["home_assistant"].copy()
                    ha_merged.update(user_config["home_assistant"])
                    config["home_assistant"] = ha_merged
                # If url and token are configured, automatically ensure enabled is True
                if config["home_assistant"].get("url") and config["home_assistant"].get("token"):
                    config["home_assistant"]["enabled"] = True
        except Exception as e:
            print(f"[Config] Error reading {config_path}: {e}. Using defaults.")
    else:
        # Create initial default config file
        save_config(config, config_path)

    # Allow environment variable overrides
    if os.environ.get("HASS_URL"):
        config["home_assistant"]["url"] = os.environ["HASS_URL"]
        config["home_assistant"]["enabled"] = True
    if os.environ.get("HASS_TOKEN"):
        config["home_assistant"]["token"] = os.environ["HASS_TOKEN"]
        config["home_assistant"]["enabled"] = True
    if os.environ.get("HASS_ENTITY_ID"):
        config["home_assistant"]["entity_id"] = os.environ["HASS_ENTITY_ID"]
    if os.environ.get("TNEB_CIRCLE"):
        config["circle"] = os.environ["TNEB_CIRCLE"]
    if os.environ.get("TNEB_FILTER"):
        config["filter"] = os.environ["TNEB_FILTER"]

    return config


def save_config(config: Dict[str, Any], config_path: str = CONFIG_FILE):
    """Save configuration to JSON file."""
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
    except Exception as e:
        print(f"[Config] Error saving {config_path}: {e}")

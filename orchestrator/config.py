"""Загрузка конфига из JSON-файла + реестр ключей."""
import json
import os
from enum import Enum


class ConfigKey(str, Enum):
    SECRET_KEY = "SECRET_KEY"
    LOGO_PATH = "LOGO_PATH"
    RUN_SERVICE_URL = "RUN_SERVICE_URL"


def load_config():
    """Читает config.json и возвращает dict."""
    filepath = os.path.join(os.path.dirname(__file__), "config_prod.json")
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[config] Файл {filepath} не найден, использую пустой конфиг")
        return {}
    except json.JSONDecodeError as e:
        print(f"[config] Ошибка парсинга {filepath}: {e}")
        return {}

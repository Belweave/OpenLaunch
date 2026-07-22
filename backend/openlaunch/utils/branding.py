from __future__ import annotations

import unicodedata

APP_NAME_MAX_LENGTH = 80


def normalize_app_name(value: object) -> str:
    """Return a display-safe application name or raise for invalid input."""
    if not isinstance(value, str):
        raise ValueError('Application name must be text')
    name = value.strip()
    if not name:
        raise ValueError('Application name cannot be empty')
    if len(name) > APP_NAME_MAX_LENGTH:
        raise ValueError(f'Application name must be {APP_NAME_MAX_LENGTH} characters or fewer')
    if any(unicodedata.category(character).startswith('C') for character in name):
        raise ValueError('Application name cannot contain control characters')
    return name

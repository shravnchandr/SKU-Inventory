"""Route modules. Each register(app, data) adds its routes to the app; endpoint
names are the function names, as before the split, so url_for() calls in the
templates are unaffected."""

from __future__ import annotations

from flask import Flask

from ..datacache import DataCache
from . import exports, imports, inventory, pages, review, settings


def register_all(app: Flask, data: DataCache) -> None:
    for module in (pages, settings, inventory, review, imports, exports):
        module.register(app, data)

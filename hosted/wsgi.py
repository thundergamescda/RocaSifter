"""Gunicorn target. Environment secrets are supplied by the hosting platform."""
from .app import create_app
app=create_app()

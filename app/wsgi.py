"""Точка входа для gunicorn и flask run."""

from app import create_app

app = create_app()

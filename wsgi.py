#!/usr/bin/env python3
"""WSGI entry point for production deployment."""
from app import create_app

app = create_app()

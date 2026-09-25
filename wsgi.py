import logging

from app import create_app

logging.basicConfig(level=logging.INFO)
app = create_app()

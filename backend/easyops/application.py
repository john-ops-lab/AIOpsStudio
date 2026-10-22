"""Full local application, including the read-only AG-UI projection."""
from .main import create_app as create_base_app
from .agui import configure_agui


def create_app():
    app = create_base_app()
    configure_agui(app)
    return app

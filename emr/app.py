"""Application wiring."""

from __future__ import annotations

from . import seed
from .web import ui
from .web.router import App, serve, set_base
from .web.routes import register_all


def create_app() -> App:
    seed.bootstrap()
    app = App()
    register_all(app)
    app.error_page = _error_page
    return app


def _error_page(status: int, message: str) -> str:
    detail = ""
    if status == 500:
        detail = ui.code_block(message)
    body = ui.empty_state(
        {404: "That page does not exist.",
         405: "That action is not allowed here."}.get(status, "Something went wrong."),
        ui.button("Back to dashboard", "/", style="z-button-primary")) + detail
    return ui.page(f"{status}", "dashboard", body)


def main(host: str = "127.0.0.1", port: int = 8765, base_uri: str = "") -> None:
    set_base(base_uri)
    serve(create_app(), host, port)

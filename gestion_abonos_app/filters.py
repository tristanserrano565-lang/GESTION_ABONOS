from flask import Flask

from . import utils


def register_filters(app: Flask) -> None:
    app.add_template_filter(utils.human_datetime, "human_datetime")
    app.add_template_filter(utils.simple_human_date, "simple_human_date")

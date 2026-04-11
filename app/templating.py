"""Single Jinja2 environment so every page gets build/version globals."""

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.build_info import format_build_lines
from app.config import settings

templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

_footer, _dash = format_build_lines(settings.app_version)
templates.env.globals["app_build_line"] = _footer
templates.env.globals["app_build_short"] = _dash

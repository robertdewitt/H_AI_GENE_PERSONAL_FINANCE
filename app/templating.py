"""Single Jinja2 environment so every page gets build/version globals.

app_build_line and app_build_short are registered as callables so that
Jinja2 invokes them on each render — the dirty flag is therefore always
current without requiring a server restart after a commit.
"""

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.build_info import get_footer, get_short
from app.config import settings

templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

# Lambdas so Jinja2 calls them fresh on every request.
templates.env.globals["app_build_line"] = lambda: get_footer(settings.app_version)
templates.env.globals["app_build_short"] = lambda: get_short(settings.app_version)

"""Compatibility entrypoint for the source-project module name.

The full SSXMOD/WAF implementation lives in `waf_cookie_generator.py`.
This module keeps the source-project import path working.
"""

from .waf_cookie_generator import *  # noqa: F401,F403


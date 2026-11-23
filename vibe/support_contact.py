"""Public product contact shared with native and web clients via support-email.txt."""
from importlib.resources import files
from typing import Final

SUPPORT_EMAIL: Final[str] = files('vibe').joinpath('support-email.txt').read_text(encoding='ascii').strip()

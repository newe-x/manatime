import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture
def browser_context_args(browser_context_args):
    return {**browser_context_args, "viewport": {"width": 1366, "height": 900}, "locale": "pt-BR",
            "timezone_id": "America/Sao_Paulo", "color_scheme": "light"}

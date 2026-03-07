"""
Pytest configuration and fixtures for test suite.
"""

from pixlvault.picture_tagger import PictureTagger
from pixlvault.server import Server


def pytest_addoption(parser):
    """Add custom command line options."""
    parser.addoption(
        "--force-cpu",
        action="store_true",
        default=False,
        help="Force CPU inference for all models (disable GPU usage)",
    )
    parser.addoption(
        "--fast-captions",
        action="store_true",
        default=False,
        help="Use minimal tokens for faster caption generation (for CI)",
    )
    parser.addoption(
        "--max-vram-gb",
        type=float,
        default=None,
        help="VRAM budget in GB applied to all Server instances (e.g. 4.0). "
             "Overrides the persisted user config value.",
    )


def pytest_configure(config):
    """Set static attributes on PictureTagger from command line options."""
    PictureTagger.FORCE_CPU = config.getoption("--force-cpu")
    PictureTagger.FAST_CAPTIONS = config.getoption("--fast-captions")
    Server.DEFAULT_MAX_VRAM_GB = config.getoption("--max-vram-gb")

"""Version lookup shared by the offline builder and public API."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("flotilla-compose")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

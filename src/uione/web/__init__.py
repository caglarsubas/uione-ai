"""The web workspace.

Served as static files with no build step and no dependencies. For an air-gapped
product a ``node_modules`` tree is a supply chain the customer's security team
must review and we must patch; here the entire client is three files they can
read.
"""

from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"

__all__ = ["STATIC_DIR"]

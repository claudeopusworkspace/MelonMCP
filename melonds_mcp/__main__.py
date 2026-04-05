"""CLI entry point: python -m melonds_mcp"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path


def _configure_logging() -> None:
    """Set up logging to both stderr and a rotating file.

    Log file lives at ``<data_dir>/melonds_mcp.log`` (next to savestates, etc.).
    Rotates at 10 MB, keeps 3 backups.
    """
    data_dir = Path(os.environ.get("MELONDS_DATA_DIR", Path.cwd()))
    log_path = data_dir / "melonds_mcp.log"

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(name)s] [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler — primary diagnostic output
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)

    # Stderr handler — only warnings and above (stdout is reserved for MCP JSON-RPC)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(fmt)
    stderr_handler.setLevel(logging.WARNING)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.addHandler(stderr_handler)

    logging.getLogger(__name__).info("Logging initialized — file: %s", log_path)


def main() -> None:
    # No SDL env var setup needed — melonDS core has no SDL dependency

    # Configure logging BEFORE importing anything else
    _configure_logging()

    # MCP stdio transport uses stdout for JSON-RPC.
    # Redirect stdout to stderr during setup to prevent corruption.
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        from melonds_mcp.server import create_server

        server = create_server()
    finally:
        sys.stdout = real_stdout

    logger = logging.getLogger(__name__)
    logger.info("MCP server starting (stdio transport)")
    server.run(transport="stdio")


if __name__ == "__main__":
    main()

"""
Shared utilities for the Minecraft server wrapper.

Provides logging setup, timestamp formatting, and common helpers.
"""

import logging
import os
import socket
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def get_hostname() -> str:
    """Get the current machine's hostname."""
    return socket.gethostname()


def get_timestamp() -> str:
    """Get current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def get_backup_timestamp() -> str:
    """Get timestamp formatted for backup filenames."""
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def parse_timestamp(ts: str) -> datetime:
    """Parse an ISO format timestamp string to datetime."""
    # Handle both with and without timezone
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        # Try parsing without timezone and assume UTC
        return datetime.fromisoformat(ts.replace('Z', '+00:00'))


def timestamp_age_seconds(ts: str) -> float:
    """Get the age of a timestamp in seconds."""
    parsed = parse_timestamp(ts)
    now = datetime.now(timezone.utc)
    # Ensure both are timezone-aware
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed).total_seconds()


def check_file_permissions(path: Path) -> tuple[bool, str]:
    """
    Check if a file has secure permissions (not world-readable).

    Returns:
        Tuple of (is_secure, message)
    """
    if not path.exists():
        return True, "File does not exist"

    try:
        mode = path.stat().st_mode
        # Check if world-readable or world-writable
        if mode & stat.S_IROTH or mode & stat.S_IWOTH:
            return False, f"Warning: {path} is world-accessible (mode: {oct(mode)})"
        return True, "Permissions OK"
    except OSError as e:
        return False, f"Could not check permissions: {e}"


def setup_logging(
    log_file: Optional[Path] = None,
    level: str = "INFO",
    console_output: bool = True
) -> logging.Logger:
    """
    Set up logging for the wrapper.

    Args:
        log_file: Path to log file (None for no file logging)
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        console_output: Whether to also log to console

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger("mc-server")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Clear any existing handlers
    logger.handlers.clear()

    # Create formatter
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # File handler
    if log_file:
        try:
            file_handler = logging.FileHandler(log_file, encoding='utf-8')
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError as e:
            print(f"[mc-server] Warning: Could not create log file: {e}")

    # Console handler (only for wrapper messages, not server output)
    if console_output:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter("[mc-server] %(message)s"))
        # Only show INFO and above on console
        console_handler.setLevel(logging.INFO)
        logger.addHandler(console_handler)

    return logger


def get_logger() -> logging.Logger:
    """Get the mc-server logger instance."""
    return logging.getLogger("mc-server")


def format_duration(seconds: float) -> str:
    """Format a duration in seconds to human-readable string."""
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{mins}m {secs}s"
    else:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}h {mins}m"


def format_size(size_bytes: int) -> str:
    """Format a size in bytes to human-readable string."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def confirm_action(prompt: str, default: bool = False) -> bool:
    """
    Ask user for confirmation.

    Args:
        prompt: The question to ask
        default: Default answer if user just presses Enter

    Returns:
        True if user confirmed, False otherwise
    """
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        try:
            response = input(prompt + suffix).strip().lower()
            if not response:
                return default
            if response in ('y', 'yes'):
                return True
            if response in ('n', 'no'):
                return False
            print("Please enter 'y' or 'n'")
        except EOFError:
            return False


def choose_option(prompt: str, options: list[str], allow_cancel: bool = True) -> Optional[int]:
    """
    Present a numbered list of options and get user choice.

    Args:
        prompt: The question/header to display
        options: List of option strings
        allow_cancel: Whether to allow cancellation (returns None)

    Returns:
        Index of chosen option, or None if cancelled
    """
    print(prompt)
    for i, option in enumerate(options, 1):
        print(f"  {i}. {option}")
    if allow_cancel:
        print(f"  0. Cancel")

    while True:
        try:
            response = input("Enter choice: ").strip()
            if not response:
                continue
            try:
                choice = int(response)
                if allow_cancel and choice == 0:
                    return None
                if 1 <= choice <= len(options):
                    return choice - 1
                print(f"Please enter a number between {'0' if allow_cancel else '1'} and {len(options)}")
            except ValueError:
                print("Please enter a number")
        except EOFError:
            return None


class Colors:
    """ANSI color codes for terminal output."""
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    RESET = '\033[0m'
    BOLD = '\033[1m'

    @classmethod
    def enabled(cls) -> bool:
        """Check if colors should be enabled (TTY check)."""
        return os.isatty(1)  # stdout is a TTY

    @classmethod
    def wrap(cls, text: str, color: str) -> str:
        """Wrap text in color codes if enabled."""
        if cls.enabled():
            return f"{color}{text}{cls.RESET}"
        return text

    @classmethod
    def error(cls, text: str) -> str:
        """Format text as error (red)."""
        return cls.wrap(text, cls.RED)

    @classmethod
    def success(cls, text: str) -> str:
        """Format text as success (green)."""
        return cls.wrap(text, cls.GREEN)

    @classmethod
    def warning(cls, text: str) -> str:
        """Format text as warning (yellow)."""
        return cls.wrap(text, cls.YELLOW)

    @classmethod
    def info(cls, text: str) -> str:
        """Format text as info (cyan)."""
        return cls.wrap(text, cls.CYAN)

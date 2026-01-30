"""
Lock file management and heartbeat for the Minecraft server wrapper.

Handles creating, reading, updating, and validating lock files,
as well as the background heartbeat thread.
"""

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from .utils import get_hostname, get_timestamp, timestamp_age_seconds, get_logger


@dataclass
class LockInfo:
    """Information from a lock file."""
    hostname: str
    started_at: str
    last_heartbeat: str
    pid: int

    def is_stale(self, threshold_seconds: int) -> bool:
        """Check if the lock is stale (heartbeat too old)."""
        try:
            age = timestamp_age_seconds(self.last_heartbeat)
            return age > threshold_seconds
        except (ValueError, TypeError):
            # If we can't parse the timestamp, consider it stale
            return True

    def is_own_machine(self) -> bool:
        """Check if this lock is from the current machine."""
        return self.hostname == get_hostname()

    def heartbeat_age(self) -> float:
        """Get the age of the last heartbeat in seconds."""
        try:
            return timestamp_age_seconds(self.last_heartbeat)
        except (ValueError, TypeError):
            return float('inf')

    def to_dict(self) -> dict:
        """Convert to dictionary for YAML serialization."""
        return {
            'hostname': self.hostname,
            'started_at': self.started_at,
            'last_heartbeat': self.last_heartbeat,
            'pid': self.pid,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'LockInfo':
        """Create LockInfo from dictionary."""
        return cls(
            hostname=data.get('hostname', 'unknown'),
            started_at=data.get('started_at', ''),
            last_heartbeat=data.get('last_heartbeat', ''),
            pid=data.get('pid', 0),
        )


class LockError(Exception):
    """Lock-related error."""
    pass


class LockManager:
    """Manages the server lock file and heartbeat."""

    def __init__(self, lock_file: Path, heartbeat_interval: int = 30, stale_threshold: int = 60):
        """
        Initialize the lock manager.

        Args:
            lock_file: Path to the lock file
            heartbeat_interval: Seconds between heartbeat updates
            stale_threshold: Seconds before a lock is considered stale
        """
        self.lock_file = lock_file
        self.heartbeat_interval = heartbeat_interval
        self.stale_threshold = stale_threshold
        self.logger = get_logger()

        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop = threading.Event()
        self._lock_held = False

    def read_lock(self) -> Optional[LockInfo]:
        """
        Read the current lock file.

        Returns:
            LockInfo if lock exists and is valid, None otherwise
        """
        if not self.lock_file.exists():
            return None

        try:
            with open(self.lock_file, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)

            if not data or not isinstance(data, dict):
                self.logger.warning("Lock file exists but is empty or invalid")
                return None

            return LockInfo.from_dict(data)

        except yaml.YAMLError as e:
            self.logger.error(f"Lock file has invalid YAML: {e}")
            return None
        except OSError as e:
            self.logger.error(f"Could not read lock file: {e}")
            return None

    def write_lock(self, pid: int) -> bool:
        """
        Write a new lock file.

        Args:
            pid: Process ID of the server

        Returns:
            True if successful
        """
        timestamp = get_timestamp()
        lock_info = LockInfo(
            hostname=get_hostname(),
            started_at=timestamp,
            last_heartbeat=timestamp,
            pid=pid,
        )

        try:
            with open(self.lock_file, 'w', encoding='utf-8') as f:
                yaml.dump(lock_info.to_dict(), f, default_flow_style=False)

            self._lock_held = True
            self.logger.debug(f"Wrote lock file: {self.lock_file}")
            return True

        except OSError as e:
            self.logger.error(f"Could not write lock file: {e}")
            return False

    def update_heartbeat(self) -> bool:
        """
        Update the heartbeat timestamp in the lock file.

        Returns:
            True if successful
        """
        lock_info = self.read_lock()
        if not lock_info:
            self.logger.error("Cannot update heartbeat: lock file missing")
            return False

        if not lock_info.is_own_machine():
            self.logger.error("Cannot update heartbeat: lock owned by different machine")
            return False

        lock_info.last_heartbeat = get_timestamp()

        try:
            with open(self.lock_file, 'w', encoding='utf-8') as f:
                yaml.dump(lock_info.to_dict(), f, default_flow_style=False)
            return True
        except OSError as e:
            self.logger.error(f"Could not update heartbeat: {e}")
            return False

    def delete_lock(self) -> bool:
        """
        Delete the lock file.

        Returns:
            True if successful or file didn't exist
        """
        if not self.lock_file.exists():
            return True

        try:
            self.lock_file.unlink()
            self._lock_held = False
            self.logger.info("Deleted lock file")
            return True
        except OSError as e:
            self.logger.error(f"Could not delete lock file: {e}")
            return False

    def check_lock_status(self) -> tuple[str, Optional[LockInfo]]:
        """
        Check the current lock status and determine appropriate action.

        Returns:
            Tuple of (status, lock_info) where status is one of:
            - "free": No lock, safe to proceed
            - "owned": Locked by this machine (stale, crashed)
            - "other_active": Locked by another machine with active heartbeat
            - "other_stale": Locked by another machine but stale
        """
        lock_info = self.read_lock()

        if lock_info is None:
            return ("free", None)

        if lock_info.is_own_machine():
            if lock_info.is_stale(self.stale_threshold):
                return ("owned", lock_info)
            else:
                # This shouldn't happen - we have a fresh lock from ourselves
                # This could mean another instance is running
                return ("owned", lock_info)
        else:
            if lock_info.is_stale(self.stale_threshold):
                return ("other_stale", lock_info)
            else:
                return ("other_active", lock_info)

    def acquire_lock(self, pid: int, race_wait: int = 10) -> bool:
        """
        Attempt to acquire the lock with race condition prevention.

        This implements the race prevention protocol:
        1. Create lock file with own hostname
        2. Wait for sync propagation
        3. Re-read lock file
        4. If hostname matches, we won the race

        Args:
            pid: Process ID to write in lock
            race_wait: Seconds to wait for sync propagation

        Returns:
            True if lock acquired successfully
        """
        # Write initial lock
        if not self.write_lock(pid):
            return False

        self.logger.info(f"Created lock file, waiting {race_wait}s for sync propagation...")
        time.sleep(race_wait)

        # Re-read to check for race condition
        lock_info = self.read_lock()

        if lock_info is None:
            self.logger.error("Lock file disappeared during race wait")
            return False

        if lock_info.hostname != get_hostname():
            self.logger.warning(
                f"Race condition detected! Lock now owned by {lock_info.hostname}"
            )
            self._lock_held = False
            return False

        self.logger.info("Lock verified after race wait")
        return True

    def start_heartbeat(self) -> None:
        """Start the background heartbeat thread."""
        if self._heartbeat_thread is not None:
            self.logger.warning("Heartbeat thread already running")
            return

        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="heartbeat",
            daemon=True
        )
        self._heartbeat_thread.start()
        self.logger.debug("Started heartbeat thread")

    def stop_heartbeat(self) -> None:
        """Stop the background heartbeat thread."""
        if self._heartbeat_thread is None:
            return

        self._heartbeat_stop.set()
        self._heartbeat_thread.join(timeout=self.heartbeat_interval + 5)

        if self._heartbeat_thread.is_alive():
            self.logger.warning("Heartbeat thread did not stop cleanly")

        self._heartbeat_thread = None
        self.logger.debug("Stopped heartbeat thread")

    def _heartbeat_loop(self) -> None:
        """Background thread that updates the heartbeat periodically."""
        while not self._heartbeat_stop.wait(timeout=self.heartbeat_interval):
            if not self.update_heartbeat():
                self.logger.error("Heartbeat update failed")
                # Continue trying - don't want to stop just because one update failed

    @property
    def is_locked(self) -> bool:
        """Check if we currently hold the lock."""
        return self._lock_held

    def get_raw_contents(self) -> Optional[str]:
        """Get the raw contents of the lock file for debugging."""
        if not self.lock_file.exists():
            return None

        try:
            with open(self.lock_file, 'r', encoding='utf-8') as f:
                return f.read()
        except OSError:
            return None

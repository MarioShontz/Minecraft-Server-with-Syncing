"""
Syncthing API client for the Minecraft server wrapper.

Handles pausing/resuming folder sync and checking sync status.
Uses urllib from stdlib to avoid external dependencies.
"""

import json
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Any, Optional

from .utils import get_logger


class SyncthingError(Exception):
    """Syncthing API error."""
    pass


class SyncthingUnavailable(SyncthingError):
    """Syncthing is not reachable."""
    pass


@dataclass
class FolderStatus:
    """Status of a Syncthing folder."""
    state: str  # "idle", "syncing", "scanning", "sync-preparing", etc.
    global_bytes: int
    local_bytes: int
    need_bytes: int
    need_files: int
    errors: int
    pull_errors: int

    @property
    def is_synced(self) -> bool:
        """Check if folder is fully synced (idle with nothing needed)."""
        return self.state == "idle" and self.need_bytes == 0 and self.need_files == 0

    @property
    def is_syncing(self) -> bool:
        """Check if folder is actively syncing."""
        return self.state in ("syncing", "sync-preparing", "sync-waiting")

    @property
    def has_errors(self) -> bool:
        """Check if there are sync errors."""
        return self.errors > 0 or self.pull_errors > 0

    def __str__(self) -> str:
        if self.is_synced:
            return "Up to Date"
        elif self.is_syncing:
            if self.global_bytes > 0:
                percent = (self.local_bytes / self.global_bytes) * 100
                return f"Syncing ({percent:.1f}%)"
            return "Syncing"
        elif self.has_errors:
            return f"Error ({self.errors} errors)"
        else:
            return f"State: {self.state}"


class SyncthingClient:
    """Client for Syncthing REST API."""

    def __init__(self, url: str, api_key: str, folder_id: str):
        """
        Initialize Syncthing client.

        Args:
            url: Base URL for Syncthing API (e.g., "http://localhost:8384")
            api_key: API key for authentication
            folder_id: ID of the folder to manage
        """
        self.url = url.rstrip('/')
        self.api_key = api_key
        self.folder_id = folder_id
        self.logger = get_logger()
        self._enabled = bool(api_key)

    @property
    def enabled(self) -> bool:
        """Check if Syncthing management is enabled."""
        return self._enabled

    def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[dict] = None,
        timeout: int = 10
    ) -> dict[str, Any]:
        """
        Make a request to the Syncthing API.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint (e.g., "/rest/db/status")
            data: Optional JSON data to send
            timeout: Request timeout in seconds

        Returns:
            JSON response as dictionary

        Raises:
            SyncthingUnavailable: If Syncthing is not reachable
            SyncthingError: For other API errors
        """
        url = f"{self.url}{endpoint}"

        headers = {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
        }

        body = json.dumps(data).encode('utf-8') if data else None

        try:
            request = urllib.request.Request(
                url,
                data=body,
                headers=headers,
                method=method
            )

            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status == 200:
                    content = response.read().decode('utf-8')
                    if content:
                        return json.loads(content)
                    return {}
                else:
                    raise SyncthingError(f"API returned status {response.status}")

        except urllib.error.URLError as e:
            if isinstance(e.reason, ConnectionRefusedError):
                raise SyncthingUnavailable(f"Syncthing not reachable at {self.url}")
            raise SyncthingUnavailable(f"Could not connect to Syncthing: {e.reason}")
        except urllib.error.HTTPError as e:
            raise SyncthingError(f"API error: {e.code} {e.reason}")
        except json.JSONDecodeError as e:
            raise SyncthingError(f"Invalid JSON response: {e}")
        except TimeoutError:
            raise SyncthingUnavailable(f"Connection to Syncthing timed out")

    def check_connection(self) -> bool:
        """
        Check if Syncthing is reachable.

        Returns:
            True if reachable, False otherwise
        """
        if not self.enabled:
            return False

        try:
            self._request("GET", "/rest/system/status")
            return True
        except SyncthingError:
            return False

    def get_folder_status(self) -> FolderStatus:
        """
        Get the current status of the managed folder.

        Returns:
            FolderStatus object

        Raises:
            SyncthingError: If status cannot be retrieved
        """
        if not self.enabled:
            raise SyncthingError("Syncthing management is disabled (no API key)")

        response = self._request(
            "GET",
            f"/rest/db/status?folder={self.folder_id}"
        )

        return FolderStatus(
            state=response.get("state", "unknown"),
            global_bytes=response.get("globalBytes", 0),
            local_bytes=response.get("localBytes", 0),
            need_bytes=response.get("needBytes", 0),
            need_files=response.get("needFiles", 0),
            errors=response.get("errors", 0),
            pull_errors=response.get("pullErrors", 0),
        )

    def get_folder_config(self) -> dict[str, Any]:
        """Get the folder configuration."""
        if not self.enabled:
            raise SyncthingError("Syncthing management is disabled")

        response = self._request("GET", "/rest/config/folders")

        for folder in response:
            if folder.get("id") == self.folder_id:
                return folder

        raise SyncthingError(f"Folder '{self.folder_id}' not found in Syncthing config")

    def is_folder_paused(self) -> bool:
        """Check if the folder is currently paused."""
        try:
            folder_config = self.get_folder_config()
            return folder_config.get("paused", False)
        except SyncthingError:
            return False

    def pause_folder(self) -> bool:
        """
        Pause syncing for the managed folder.

        Returns:
            True if successfully paused, False otherwise
        """
        if not self.enabled:
            self.logger.warning("Syncthing management disabled, skipping pause")
            return False

        try:
            # Get current folder config
            folder_config = self.get_folder_config()

            if folder_config.get("paused", False):
                self.logger.debug("Folder already paused")
                return True

            # Update folder config to set paused=true
            folder_config["paused"] = True

            self._request(
                "PUT",
                f"/rest/config/folders/{self.folder_id}",
                data=folder_config
            )

            self.logger.info(f"Paused Syncthing folder: {self.folder_id}")
            return True

        except SyncthingError as e:
            self.logger.error(f"Failed to pause folder: {e}")
            return False

    def resume_folder(self) -> bool:
        """
        Resume syncing for the managed folder.

        Returns:
            True if successfully resumed, False otherwise
        """
        if not self.enabled:
            self.logger.warning("Syncthing management disabled, skipping resume")
            return False

        try:
            # Get current folder config
            folder_config = self.get_folder_config()

            if not folder_config.get("paused", False):
                self.logger.debug("Folder already resumed")
                return True

            # Update folder config to set paused=false
            folder_config["paused"] = False

            self._request(
                "PUT",
                f"/rest/config/folders/{self.folder_id}",
                data=folder_config
            )

            self.logger.info(f"Resumed Syncthing folder: {self.folder_id}")
            return True

        except SyncthingError as e:
            self.logger.error(f"Failed to resume folder: {e}")
            return False

    def wait_for_sync(self, timeout: int = 300, poll_interval: int = 5) -> bool:
        """
        Wait for folder to finish syncing.

        Args:
            timeout: Maximum time to wait in seconds
            poll_interval: Time between status checks in seconds

        Returns:
            True if synced, False if timeout or error
        """
        if not self.enabled:
            return True

        self.logger.info("Waiting for Syncthing to finish syncing...")
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                status = self.get_folder_status()

                if status.is_synced:
                    self.logger.info("Syncthing folder is up to date")
                    return True

                if status.has_errors:
                    self.logger.warning(f"Syncthing has errors: {status}")
                    return False

                elapsed = int(time.time() - start_time)
                self.logger.debug(f"Sync status: {status} (waited {elapsed}s)")
                print(f"\r[mc-server] {status} (waited {elapsed}s)...", end="", flush=True)

                time.sleep(poll_interval)

            except SyncthingError as e:
                self.logger.error(f"Error checking sync status: {e}")
                return False

        print()  # Newline after progress
        self.logger.warning(f"Sync wait timed out after {timeout}s")
        return False

    def trigger_scan(self) -> bool:
        """
        Trigger a rescan of the folder.

        Returns:
            True if scan triggered, False otherwise
        """
        if not self.enabled:
            return False

        try:
            self._request(
                "POST",
                f"/rest/db/scan?folder={self.folder_id}"
            )
            self.logger.info("Triggered folder rescan")
            return True
        except SyncthingError as e:
            self.logger.error(f"Failed to trigger scan: {e}")
            return False

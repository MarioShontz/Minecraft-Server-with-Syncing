"""
Backup management for the Minecraft server wrapper.

Handles creating, listing, restoring, and pruning backups.
"""

import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .utils import get_logger, get_backup_timestamp, format_size, Colors


@dataclass
class BackupInfo:
    """Information about a backup."""
    path: Path
    timestamp: datetime
    size: int

    @property
    def name(self) -> str:
        """Get the backup filename."""
        return self.path.name

    @property
    def age_days(self) -> float:
        """Get the age of the backup in days."""
        return (datetime.now() - self.timestamp).total_seconds() / 86400

    def __str__(self) -> str:
        return f"{self.name} ({format_size(self.size)}, {self.age_days:.1f} days old)"


class BackupError(Exception):
    """Backup-related error."""
    pass


class BackupManager:
    """Manages world backups."""

    BACKUP_PREFIX = "backup_"
    BACKUP_SUFFIX = ".zip"

    def __init__(
        self,
        backup_folder: Path,
        world_folder: Path,
        auto_prune: bool = True,
        keep_minimum: int = 5,
        keep_days: int = 30
    ):
        """
        Initialize the backup manager.

        Args:
            backup_folder: Where to store backups
            world_folder: Path to the world folder to backup
            auto_prune: Whether to automatically prune old backups
            keep_minimum: Minimum number of backups to keep
            keep_days: Delete backups older than this (if above minimum)
        """
        self.backup_folder = backup_folder
        self.world_folder = world_folder
        self.auto_prune = auto_prune
        self.keep_minimum = keep_minimum
        self.keep_days = keep_days
        self.logger = get_logger()

        # Track last backup time for change detection
        self._last_backup_time: Optional[datetime] = None

    def _parse_backup_filename(self, filename: str) -> Optional[datetime]:
        """
        Parse a backup filename to extract the timestamp.

        Expected format: backup_YYYY-MM-DD_HH-MM-SS.zip

        Returns:
            datetime if valid, None otherwise
        """
        if not filename.startswith(self.BACKUP_PREFIX):
            return None
        if not filename.endswith(self.BACKUP_SUFFIX):
            return None

        timestamp_str = filename[len(self.BACKUP_PREFIX):-len(self.BACKUP_SUFFIX)]

        try:
            return datetime.strptime(timestamp_str, "%Y-%m-%d_%H-%M-%S")
        except ValueError:
            return None

    def list_backups(self) -> list[BackupInfo]:
        """
        List all valid backups, sorted by timestamp (newest first).

        Returns:
            List of BackupInfo objects
        """
        if not self.backup_folder.exists():
            return []

        backups = []

        for file_path in self.backup_folder.glob(f"{self.BACKUP_PREFIX}*{self.BACKUP_SUFFIX}"):
            timestamp = self._parse_backup_filename(file_path.name)
            if timestamp is None:
                continue

            try:
                size = file_path.stat().st_size
            except OSError:
                continue

            backups.append(BackupInfo(
                path=file_path,
                timestamp=timestamp,
                size=size,
            ))

        # Sort by timestamp, newest first
        backups.sort(key=lambda b: b.timestamp, reverse=True)
        return backups

    def get_latest_backup(self) -> Optional[BackupInfo]:
        """Get the most recent backup, if any."""
        backups = self.list_backups()
        return backups[0] if backups else None

    def world_changed_since_backup(self) -> bool:
        """
        Check if the world has changed since the last backup.

        This is a simple check based on modification times.

        Returns:
            True if world has changed or no backups exist
        """
        latest = self.get_latest_backup()
        if latest is None:
            return True

        if not self.world_folder.exists():
            return False

        # Check if any file in world folder is newer than the backup
        backup_time = latest.timestamp.timestamp()

        for file_path in self.world_folder.rglob("*"):
            if file_path.is_file():
                try:
                    if file_path.stat().st_mtime > backup_time:
                        return True
                except OSError:
                    continue

        return False

    def create_backup(self, description: Optional[str] = None) -> BackupInfo:
        """
        Create a new backup of the world folder.

        Args:
            description: Optional description (not currently used in filename)

        Returns:
            BackupInfo for the new backup

        Raises:
            BackupError: If backup fails
        """
        if not self.world_folder.exists():
            raise BackupError(f"World folder does not exist: {self.world_folder}")

        # Ensure backup folder exists
        self.backup_folder.mkdir(parents=True, exist_ok=True)

        # Generate backup filename
        timestamp = get_backup_timestamp()
        backup_name = f"{self.BACKUP_PREFIX}{timestamp}{self.BACKUP_SUFFIX}"
        backup_path = self.backup_folder / backup_name

        self.logger.info(f"Creating backup: {backup_name}")
        print(f"[mc-server] Creating backup: {backup_name}...")

        try:
            # Create zip file
            with zipfile.ZipFile(backup_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                file_count = 0
                for file_path in self.world_folder.rglob("*"):
                    if file_path.is_file():
                        # Calculate relative path within the world folder
                        arc_name = file_path.relative_to(self.world_folder.parent)
                        zf.write(file_path, arc_name)
                        file_count += 1

                        # Progress indicator for large worlds
                        if file_count % 100 == 0:
                            print(f"\r[mc-server] Backed up {file_count} files...", end="", flush=True)

                print(f"\r[mc-server] Backed up {file_count} files", end="")

            # Get size of created backup
            size = backup_path.stat().st_size
            print(f" ({format_size(size)})")

            self._last_backup_time = datetime.now()
            self.logger.info(f"Backup created: {backup_name} ({format_size(size)})")

            return BackupInfo(
                path=backup_path,
                timestamp=datetime.strptime(timestamp, "%Y-%m-%d_%H-%M-%S"),
                size=size,
            )

        except (OSError, zipfile.BadZipFile) as e:
            # Clean up partial backup
            if backup_path.exists():
                try:
                    backup_path.unlink()
                except OSError:
                    pass
            raise BackupError(f"Failed to create backup: {e}")

    def restore_backup(self, backup: BackupInfo, target: Optional[Path] = None) -> bool:
        """
        Restore a backup.

        Args:
            backup: BackupInfo of the backup to restore
            target: Where to restore (defaults to world folder location)

        Returns:
            True if successful

        Raises:
            BackupError: If restore fails
        """
        if not backup.path.exists():
            raise BackupError(f"Backup file not found: {backup.path}")

        target = target or self.world_folder

        self.logger.info(f"Restoring backup: {backup.name}")
        print(f"[mc-server] Restoring backup: {backup.name}...")

        # If world folder exists, rename it as a safety measure
        old_world = None
        if target.exists():
            old_world = target.parent / f"{target.name}.old"
            if old_world.exists():
                shutil.rmtree(old_world)
            target.rename(old_world)
            self.logger.debug(f"Moved existing world to {old_world}")

        try:
            # Extract backup
            with zipfile.ZipFile(backup.path, 'r') as zf:
                # The backup includes the "world" folder name in the archive
                zf.extractall(target.parent)

            self.logger.info(f"Restored backup successfully")
            print(f"[mc-server] {Colors.success('Backup restored successfully')}")

            # Remove old world backup
            if old_world and old_world.exists():
                shutil.rmtree(old_world)
                self.logger.debug(f"Removed old world backup")

            return True

        except (OSError, zipfile.BadZipFile) as e:
            # Try to restore old world
            if old_world and old_world.exists():
                if target.exists():
                    shutil.rmtree(target)
                old_world.rename(target)
                self.logger.warning("Restored old world after failed restore")

            raise BackupError(f"Failed to restore backup: {e}")

    def prune_backups(self) -> list[BackupInfo]:
        """
        Remove old backups according to retention policy.

        Keeps at least keep_minimum backups.
        Removes backups older than keep_days (only if above minimum).

        Returns:
            List of BackupInfo that were deleted
        """
        if not self.auto_prune:
            return []

        backups = self.list_backups()
        deleted = []

        # Don't prune if we have fewer than minimum
        if len(backups) <= self.keep_minimum:
            return []

        cutoff = datetime.now() - timedelta(days=self.keep_days)

        for i, backup in enumerate(backups):
            # Always keep minimum number of backups
            if len(backups) - len(deleted) <= self.keep_minimum:
                break

            # Delete if older than cutoff
            if backup.timestamp < cutoff:
                try:
                    backup.path.unlink()
                    deleted.append(backup)
                    self.logger.info(f"Pruned old backup: {backup.name}")
                except OSError as e:
                    self.logger.error(f"Failed to delete backup {backup.name}: {e}")

        if deleted:
            print(f"[mc-server] Pruned {len(deleted)} old backup(s)")

        return deleted

    def get_backup_by_index(self, index: int) -> Optional[BackupInfo]:
        """
        Get a backup by its index in the sorted list.

        Args:
            index: 0-based index (0 = newest)

        Returns:
            BackupInfo or None if index out of range
        """
        backups = self.list_backups()
        if 0 <= index < len(backups):
            return backups[index]
        return None

    def print_backup_list(self) -> None:
        """Print a formatted list of backups to the console."""
        backups = self.list_backups()

        if not backups:
            print("\n[mc-server] No backups found")
            return

        total_size = sum(b.size for b in backups)

        print(f"\n[mc-server] {Colors.info('Available Backups')} ({len(backups)} backups, {format_size(total_size)} total)")
        print()

        for i, backup in enumerate(backups):
            age_str = f"{backup.age_days:.1f} days ago"
            if backup.age_days < 1:
                hours = backup.age_days * 24
                age_str = f"{hours:.1f} hours ago"

            marker = Colors.success("(latest)") if i == 0 else ""
            print(f"  {i+1}. {backup.timestamp.strftime('%Y-%m-%d %H:%M:%S')} - {format_size(backup.size)} - {age_str} {marker}")

        print()

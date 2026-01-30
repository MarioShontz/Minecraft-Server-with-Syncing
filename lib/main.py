"""
Main orchestration for the Minecraft server wrapper.

Handles CLI parsing, startup/shutdown sequences, and signal handling.
"""

import argparse
import os
import signal
import sys
from pathlib import Path
from typing import Optional

from .config import Config, ConfigError, load_config, validate_config
from .syncthing import SyncthingClient, SyncthingError, SyncthingUnavailable
from .lock import LockManager, LockInfo
from .backup import BackupManager, BackupError
from .integrity import check_world_integrity, print_integrity_report
from .server import MinecraftServer, ServerError
from .console import Console
from .utils import (
    setup_logging, get_logger, get_hostname, Colors,
    confirm_action, choose_option, format_duration
)


class Wrapper:
    """Main wrapper orchestration class."""

    def __init__(self, config: Config):
        self.config = config
        self.logger = get_logger()

        # Initialize components
        self.syncthing = SyncthingClient(
            url=config.syncthing.url,
            api_key=config.syncthing.api_key,
            folder_id=config.syncthing.folder_id,
        )

        self.lock_manager = LockManager(
            lock_file=config.lock_file,
            heartbeat_interval=config.safety.heartbeat_interval,
            stale_threshold=config.safety.stale_threshold,
        )

        self.backup_manager = BackupManager(
            backup_folder=config.backup.folder,
            world_folder=config.world_folder,
            auto_prune=config.backup.auto_prune,
            keep_minimum=config.backup.keep_minimum,
            keep_days=config.backup.keep_days,
        )

        self.server = MinecraftServer(
            server_folder=config.server.folder,
            jar_name=config.server.jar_name,
            java_path=config.server.java_path,
            min_memory=config.server.min_memory,
            max_memory=config.server.max_memory,
            extra_args=config.server.extra_args,
        )

        self._syncthing_paused = False
        self._shutdown_in_progress = False

    def preflight_checks(self) -> bool:
        """
        Run pre-flight checks before starting.

        Returns:
            True if all checks pass
        """
        print(f"\n{Colors.info('Running pre-flight checks...')}")
        all_ok = True

        # Check server folder
        if not self.config.server.folder.exists():
            print(f"  {Colors.error('✗')} Server folder not found: {self.config.server.folder}")
            return False
        print(f"  {Colors.success('✓')} Server folder: {self.config.server.folder}")

        # Check server JAR
        jar_ok, jar_msg = self.server.check_jar()
        if jar_ok:
            print(f"  {Colors.success('✓')} {jar_msg}")
        else:
            print(f"  {Colors.error('✗')} {jar_msg}")
            all_ok = False

        # Check Java
        java_ok, java_msg = self.server.check_java()
        if java_ok:
            print(f"  {Colors.success('✓')} Java: {java_msg}")
        else:
            print(f"  {Colors.error('✗')} {java_msg}")
            all_ok = False

        # Check backup folder
        if not self.config.backup.folder.exists():
            try:
                self.config.backup.folder.mkdir(parents=True)
                print(f"  {Colors.success('✓')} Created backup folder: {self.config.backup.folder}")
            except OSError as e:
                print(f"  {Colors.error('✗')} Cannot create backup folder: {e}")
                all_ok = False
        else:
            print(f"  {Colors.success('✓')} Backup folder: {self.config.backup.folder}")

        # Check Syncthing
        if self.syncthing.enabled:
            if self.syncthing.check_connection():
                print(f"  {Colors.success('✓')} Syncthing: Connected to {self.config.syncthing.url}")
            else:
                print(f"  {Colors.warning('!')} Syncthing: Not reachable at {self.config.syncthing.url}")
                # Not a fatal error
        else:
            print(f"  {Colors.warning('!')} Syncthing: Disabled (no API key)")

        # Config validation warnings
        warnings = validate_config(self.config)
        for warning in warnings:
            print(f"  {Colors.warning('!')} {warning}")

        print()
        return all_ok

    def check_sync_status(self) -> bool:
        """
        Check Syncthing sync status and wait if needed.

        Returns:
            True if OK to proceed
        """
        if not self.syncthing.enabled:
            return True

        try:
            status = self.syncthing.get_folder_status()

            if status.is_synced:
                self.logger.info("Syncthing folder is up to date")
                print(f"[mc-server] Syncthing: {Colors.success('Up to Date')}")
                return True

            if status.has_errors:
                print(f"[mc-server] Syncthing: {Colors.error('Has errors')}")
                print(f"[mc-server] {status}")
                if not confirm_action("Proceed anyway?"):
                    return False
                return True

            if status.is_syncing:
                print(f"[mc-server] Syncthing is currently syncing...")
                if self.syncthing.wait_for_sync(timeout=self.config.safety.sync_wait_timeout):
                    return True
                else:
                    print(f"\n[mc-server] {Colors.warning('Sync wait timed out')}")
                    if not confirm_action("Proceed anyway?"):
                        return False
                    return True

            # Unknown state
            print(f"[mc-server] Syncthing state: {status.state}")
            return True

        except SyncthingUnavailable as e:
            print(f"[mc-server] {Colors.warning('Syncthing not reachable:')} {e}")
            if not confirm_action("Proceed without sync management? (risky)"):
                return False
            return True

        except SyncthingError as e:
            print(f"[mc-server] {Colors.error('Syncthing error:')} {e}")
            if not confirm_action("Proceed anyway?"):
                return False
            return True

    def handle_lock(self) -> bool:
        """
        Handle lock file checks and acquisition.

        Returns:
            True if lock acquired successfully
        """
        status, lock_info = self.lock_manager.check_lock_status()

        if status == "free":
            self.logger.info("No existing lock file found")
            print(f"[mc-server] No existing lock file found")
            return True

        elif status == "owned":
            # Our own machine has a stale lock (crash recovery)
            return self._handle_own_crash(lock_info)

        elif status == "other_active":
            # Another machine has an active lock
            print(f"\n{Colors.error('Server is already running!')}")
            print(f"  Hostname: {lock_info.hostname}")
            print(f"  Started:  {lock_info.started_at}")
            print(f"  Heartbeat: {lock_info.heartbeat_age():.0f}s ago")
            print(f"\nCannot start while another machine is hosting.")
            return False

        elif status == "other_stale":
            # Another machine has a stale lock
            return self._handle_other_crash(lock_info)

        return False

    def _handle_own_crash(self, lock_info: LockInfo) -> bool:
        """Handle recovery from our own crash."""
        print(f"\n{Colors.warning('Detected unclean shutdown from previous session')}")
        print(f"  Started:   {lock_info.started_at}")
        print(f"  Last seen: {lock_info.last_heartbeat}")

        # Run integrity check
        print(f"\n{Colors.info('Running world integrity check...')}")
        report = check_world_integrity(self.config.world_folder)
        print_integrity_report(report)

        if report.has_issues:
            print(f"\n{Colors.warning('World may have corruption.')}")
            choice = choose_option(
                "What would you like to do?",
                [
                    "Recover: Clean up lock and proceed with startup",
                    "Restore: Restore from a backup before starting",
                    "Abort: Exit without changes",
                ]
            )

            if choice == 0:  # Recover
                self.lock_manager.delete_lock()
                return True
            elif choice == 1:  # Restore
                if self._restore_backup_interactive():
                    self.lock_manager.delete_lock()
                    return True
                return False
            else:  # Abort or cancel
                return False
        else:
            choice = choose_option(
                "World appears healthy. What would you like to do?",
                [
                    "Recover: Clean up lock and proceed with startup",
                    "Abort: Exit without changes",
                ]
            )

            if choice == 0:  # Recover
                self.lock_manager.delete_lock()
                return True
            else:
                return False

    def _handle_other_crash(self, lock_info: LockInfo) -> bool:
        """Handle recovery when another machine crashed."""
        print(f"\n{Colors.warning('Another machine appears to have crashed')}")
        print(f"  Hostname: {lock_info.hostname}")
        print(f"  Started:  {lock_info.started_at}")
        print(f"  Last seen: {lock_info.last_heartbeat} ({lock_info.heartbeat_age():.0f}s ago)")

        # Run integrity check
        print(f"\n{Colors.info('Running world integrity check...')}")
        report = check_world_integrity(self.config.world_folder)
        print_integrity_report(report)

        if not confirm_action("\nTake over and start the server?"):
            return False

        if report.has_issues:
            choice = choose_option(
                "World has issues. What would you like to do?",
                [
                    "Continue: Proceed with current world",
                    "Restore: Restore from a backup first",
                    "Abort: Exit without changes",
                ]
            )

            if choice == 0:  # Continue
                self.lock_manager.delete_lock()
                return True
            elif choice == 1:  # Restore
                if self._restore_backup_interactive():
                    self.lock_manager.delete_lock()
                    return True
                return False
            else:
                return False
        else:
            self.lock_manager.delete_lock()
            return True

    def _restore_backup_interactive(self) -> bool:
        """Interactive backup restoration."""
        backups = self.backup_manager.list_backups()

        if not backups:
            print(f"{Colors.error('No backups available!')}")
            return False

        self.backup_manager.print_backup_list()

        try:
            choice = int(input("Enter backup number to restore (0 to cancel): "))
            if choice == 0:
                return False
            if 1 <= choice <= len(backups):
                backup = backups[choice - 1]
                if confirm_action(f"Restore backup from {backup.timestamp}?"):
                    self.backup_manager.restore_backup(backup)
                    return True
            else:
                print("Invalid selection")
                return False
        except ValueError:
            print("Invalid input")
            return False
        except BackupError as e:
            print(f"{Colors.error('Restore failed:')} {e}")
            return False

    def pre_start_backup(self) -> bool:
        """
        Create pre-start backup if world changed.

        Returns:
            True if successful (or no backup needed)
        """
        if not self.config.world_folder.exists():
            self.logger.info("No world folder yet, skipping pre-start backup")
            return True

        if self.backup_manager.world_changed_since_backup():
            print(f"[mc-server] World has changed since last backup")
            try:
                self.backup_manager.create_backup()
                return True
            except BackupError as e:
                print(f"[mc-server] {Colors.error('Backup failed:')} {e}")
                if not confirm_action("Continue without backup?"):
                    return False
                return True
        else:
            print(f"[mc-server] World unchanged since last backup, skipping")
            return True

    def pause_syncthing(self) -> bool:
        """Pause Syncthing sync."""
        if not self.syncthing.enabled:
            return True

        if self.syncthing.pause_folder():
            self._syncthing_paused = True
            return True
        else:
            print(f"[mc-server] {Colors.warning('Failed to pause Syncthing')}")
            if not confirm_action("Continue anyway?"):
                return False
            return True

    def resume_syncthing(self) -> None:
        """Resume Syncthing sync."""
        if not self.syncthing.enabled or not self._syncthing_paused:
            return

        if self.syncthing.resume_folder():
            self._syncthing_paused = False
        else:
            print(f"[mc-server] {Colors.warning('Failed to resume Syncthing')}")

    def acquire_lock(self) -> bool:
        """
        Acquire the lock with race condition prevention.

        Returns:
            True if lock acquired
        """
        # We don't have PID yet, use 0 as placeholder
        # We'll update it after server starts
        if not self.lock_manager.write_lock(pid=0):
            print(f"[mc-server] {Colors.error('Failed to create lock file')}")
            return False

        print(f"[mc-server] Created lock file, waiting {self.config.safety.race_wait}s for sync...")

        import time
        time.sleep(self.config.safety.race_wait)

        # Re-read to check for race condition
        lock_info = self.lock_manager.read_lock()

        if lock_info is None:
            print(f"[mc-server] {Colors.error('Lock file disappeared!')}")
            return False

        if lock_info.hostname != get_hostname():
            print(f"[mc-server] {Colors.error('Race condition! Lock claimed by')} {lock_info.hostname}")
            return False

        print(f"[mc-server] Lock verified")
        return True

    def start_server(self) -> bool:
        """
        Start the Minecraft server.

        Returns:
            True if server started successfully
        """
        try:
            pid = self.server.start()

            # Update lock with real PID
            self.lock_manager.write_lock(pid=pid)
            self.lock_manager.start_heartbeat()

            print(f"[mc-server] Server started (PID: {pid})")
            self.logger.info(f"Server started (PID: {pid})")
            return True

        except ServerError as e:
            print(f"[mc-server] {Colors.error('Failed to start server:')} {e}")
            self.logger.error(f"Failed to start server: {e}")
            return False

    def shutdown(self) -> int:
        """
        Perform shutdown sequence.

        Returns:
            Exit code
        """
        if self._shutdown_in_progress:
            return 0

        self._shutdown_in_progress = True
        exit_code = 0

        print(f"\n{Colors.info('Shutting down...')}")
        self.logger.info("Starting shutdown sequence")

        # Stop server
        if self.server.is_running:
            if not self.server.stop(timeout=60):
                self.logger.warning("Server did not stop cleanly")
                exit_code = 1

        # Stop heartbeat
        self.lock_manager.stop_heartbeat()

        # Create post-stop backup
        if self.config.world_folder.exists():
            print(f"[mc-server] Creating shutdown backup...")
            try:
                self.backup_manager.create_backup()
            except BackupError as e:
                print(f"[mc-server] {Colors.warning('Backup failed:')} {e}")

        # Delete lock
        self.lock_manager.delete_lock()

        # Prune old backups
        self.backup_manager.prune_backups()

        # Resume Syncthing
        self.resume_syncthing()

        # Cleanup
        self.server.cleanup()

        print(f"[mc-server] {Colors.success('Shutdown complete')}")
        self.logger.info("Shutdown complete")

        return exit_code

    def run_interactive(self) -> int:
        """
        Run the wrapper in interactive mode.

        Returns:
            Exit code
        """
        # Setup signal handlers
        def signal_handler(signum, frame):
            signame = signal.Signals(signum).name
            print(f"\n[mc-server] Received {signame}")
            self.logger.info(f"Received signal: {signame}")
            self.shutdown()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        try:
            signal.signal(signal.SIGHUP, signal_handler)
        except AttributeError:
            pass  # SIGHUP not available on Windows

        # Pre-flight checks
        if not self.preflight_checks():
            return 1

        # Check sync status
        if not self.check_sync_status():
            return 1

        # Handle lock
        if not self.handle_lock():
            return 1

        # Pre-start backup
        if not self.pre_start_backup():
            return 1

        # Acquire lock (Syncthing must still be running so the lock file syncs)
        if not self.acquire_lock():
            return 1

        # Pause Syncthing (after lock is verified, so sync is no longer needed)
        if not self.pause_syncthing():
            self.lock_manager.delete_lock()
            return 1

        # Start server
        if not self.start_server():
            self.lock_manager.delete_lock()
            self.resume_syncthing()
            return 1

        # Run interactive console
        console = Console(
            server=self.server,
            backup_manager=self.backup_manager,
            syncthing_client=self.syncthing,
            on_shutdown=None  # We handle shutdown ourselves
        )

        try:
            console.start()
        except Exception as e:
            self.logger.error(f"Console error: {e}")

        # Shutdown
        return self.shutdown()


def cmd_status(config: Config) -> int:
    """Show status without starting."""
    wrapper = Wrapper(config)

    print(f"\n{Colors.info('Minecraft Server Wrapper Status')}")
    print("=" * 50)

    # Lock status
    status, lock_info = wrapper.lock_manager.check_lock_status()
    print(f"\n{Colors.info('Lock Status:')}")
    if status == "free":
        print(f"  Status: {Colors.success('Available')}")
    elif status == "owned":
        print(f"  Status: {Colors.warning('Stale lock (own machine)')}")
        print(f"  Last seen: {lock_info.last_heartbeat}")
    elif status == "other_active":
        print(f"  Status: {Colors.error('Running on another machine')}")
        print(f"  Hostname: {lock_info.hostname}")
        print(f"  Started: {lock_info.started_at}")
    elif status == "other_stale":
        print(f"  Status: {Colors.warning('Stale lock (other machine)')}")
        print(f"  Hostname: {lock_info.hostname}")
        print(f"  Last seen: {lock_info.last_heartbeat}")

    # Syncthing status
    print(f"\n{Colors.info('Syncthing:')}")
    if wrapper.syncthing.enabled:
        try:
            sync_status = wrapper.syncthing.get_folder_status()
            print(f"  Status: {sync_status}")
            print(f"  Paused: {wrapper.syncthing.is_folder_paused()}")
        except SyncthingError as e:
            print(f"  Status: {Colors.error('Error:')} {e}")
    else:
        print(f"  Status: {Colors.warning('Disabled')}")

    # Backup status
    print(f"\n{Colors.info('Backups:')}")
    backups = wrapper.backup_manager.list_backups()
    print(f"  Count: {len(backups)}")
    if backups:
        print(f"  Latest: {backups[0].timestamp.strftime('%Y-%m-%d %H:%M')}")

    print()
    return 0


def cmd_backup(config: Config) -> int:
    """Create a backup without starting."""
    wrapper = Wrapper(config)

    print(f"[mc-server] Creating backup...")
    try:
        backup = wrapper.backup_manager.create_backup()
        print(f"[mc-server] {Colors.success('Backup created:')} {backup.name}")
        return 0
    except BackupError as e:
        print(f"[mc-server] {Colors.error('Backup failed:')} {e}")
        return 1


def cmd_restore(config: Config) -> int:
    """Restore from a backup."""
    wrapper = Wrapper(config)

    backups = wrapper.backup_manager.list_backups()

    if not backups:
        print(f"[mc-server] {Colors.error('No backups available!')}")
        return 1

    wrapper.backup_manager.print_backup_list()

    try:
        choice = int(input("Enter backup number to restore (0 to cancel): "))
        if choice == 0:
            print("Cancelled")
            return 0
        if 1 <= choice <= len(backups):
            backup = backups[choice - 1]
            if confirm_action(f"Restore backup from {backup.timestamp}?"):
                wrapper.backup_manager.restore_backup(backup)
                return 0
            else:
                print("Cancelled")
                return 0
        else:
            print("Invalid selection")
            return 1
    except ValueError:
        print("Invalid input")
        return 1
    except BackupError as e:
        print(f"[mc-server] {Colors.error('Restore failed:')} {e}")
        return 1


def run() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Minecraft Server Wrapper - Safe multi-user server management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  ./mc-server.py              Start server in interactive mode
  ./mc-server.py --status     Show current status
  ./mc-server.py --backup     Create a backup without starting
  ./mc-server.py --restore    Restore from a backup
        """
    )

    parser.add_argument(
        '--status',
        action='store_true',
        help='Show current status without starting'
    )
    parser.add_argument(
        '--backup',
        action='store_true',
        help='Create a backup without starting'
    )
    parser.add_argument(
        '--restore',
        action='store_true',
        help='Restore from a backup'
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable debug logging'
    )

    args = parser.parse_args()

    # Load configuration
    try:
        config = load_config()
    except ConfigError as e:
        print(f"[mc-server] {Colors.error('Configuration error:')} {e}")
        return 1

    # Setup logging
    log_level = "DEBUG" if args.debug else config.logging.level
    setup_logging(
        log_file=config.log_file,
        level=log_level,
        console_output=False  # We handle console output manually
    )

    logger = get_logger()
    logger.info(f"mc-server wrapper starting on {get_hostname()}")

    # Handle subcommands
    if args.status:
        return cmd_status(config)
    elif args.backup:
        return cmd_backup(config)
    elif args.restore:
        return cmd_restore(config)
    else:
        # Normal interactive mode
        wrapper = Wrapper(config)
        return wrapper.run_interactive()

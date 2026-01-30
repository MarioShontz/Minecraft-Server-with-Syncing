"""
Interactive console for the Minecraft server wrapper.

Handles user input, built-in commands, and pass-through to the server.
"""

import readline
import sys
import threading
from typing import Callable, Optional

from .server import MinecraftServer
from .backup import BackupManager
from .syncthing import SyncthingClient
from .utils import get_logger, format_duration, Colors


class Console:
    """Interactive console for the server wrapper."""

    PROMPT = "[mc-server] > "

    # Built-in commands that are handled by the wrapper
    BUILTIN_COMMANDS = {
        'quit': 'Trigger safe shutdown sequence',
        'exit': 'Same as quit',
        'backup': 'Create a manual backup without stopping',
        'status': 'Show current server status',
        'help': 'Show available commands',
    }

    # Commands that should be intercepted and redirected
    PROTECTED_COMMANDS = {'stop'}

    def __init__(
        self,
        server: MinecraftServer,
        backup_manager: BackupManager,
        syncthing_client: SyncthingClient,
        on_shutdown: Optional[Callable[[], None]] = None
    ):
        """
        Initialize the console.

        Args:
            server: MinecraftServer instance
            backup_manager: BackupManager instance
            syncthing_client: SyncthingClient instance
            on_shutdown: Callback function for shutdown
        """
        self.server = server
        self.backup_manager = backup_manager
        self.syncthing_client = syncthing_client
        self.on_shutdown = on_shutdown
        self.logger = get_logger()

        self._running = False
        self._output_thread: Optional[threading.Thread] = None
        self._shutdown_requested = False

    def start(self) -> None:
        """Start the interactive console."""
        self._running = True
        self._shutdown_requested = False

        # Start output thread to display server output
        self._output_thread = threading.Thread(
            target=self._output_loop,
            name="console-output",
            daemon=True
        )
        self._output_thread.start()

        # Main input loop
        self._input_loop()

    def stop(self) -> None:
        """Stop the console."""
        self._running = False
        if self._output_thread:
            self._output_thread.join(timeout=2)
            self._output_thread = None

    def _output_loop(self) -> None:
        """Background thread that displays server output."""
        while self._running and self.server.is_running:
            line = self.server.read_line(timeout=0.1)
            if line:
                # Print server output without the prompt
                # Clear current line, print output, restore prompt
                sys.stdout.write('\r' + ' ' * 80 + '\r')  # Clear line
                sys.stdout.write(line)
                if not line.endswith('\n'):
                    sys.stdout.write('\n')
                sys.stdout.write(self.PROMPT)
                sys.stdout.flush()

    def _input_loop(self) -> None:
        """Main loop that reads and processes user input."""
        print(f"\n{Colors.info('Minecraft Server Console')}")
        print(f"Type 'help' for available commands, 'quit' to shutdown safely.\n")

        while self._running and self.server.is_running:
            try:
                # Read input
                line = input(self.PROMPT).strip()

                if not line:
                    continue

                # Process the command
                self._process_command(line)

                # Check if shutdown was requested
                if self._shutdown_requested:
                    break

            except EOFError:
                # Ctrl+D pressed
                print()
                self._cmd_quit()
                break
            except KeyboardInterrupt:
                # Ctrl+C is handled by signal handler
                print()
                continue

        self._running = False

    def _process_command(self, line: str) -> None:
        """Process a command line."""
        cmd = line.lower().split()[0] if line.split() else ""

        # Check for built-in commands
        if cmd in ('quit', 'exit'):
            self._cmd_quit()
        elif cmd == 'backup':
            self._cmd_backup()
        elif cmd == 'status':
            self._cmd_status()
        elif cmd == 'help':
            self._cmd_help()
        elif cmd in self.PROTECTED_COMMANDS:
            self._handle_protected(cmd, line)
        else:
            # Pass through to server
            self.server.send_command(line)

    def _cmd_quit(self) -> None:
        """Handle quit command."""
        print(f"\n{Colors.info('Initiating safe shutdown...')}")
        self._shutdown_requested = True
        self._running = False

        if self.on_shutdown:
            self.on_shutdown()

    def _cmd_backup(self) -> None:
        """Handle backup command."""
        print()
        try:
            backup = self.backup_manager.create_backup()
            print(f"{Colors.success('Backup created:')} {backup.name}")
        except Exception as e:
            print(f"{Colors.error('Backup failed:')} {e}")
        print()

    def _cmd_status(self) -> None:
        """Handle status command."""
        print(f"\n{Colors.info('Server Status')}")
        print("-" * 40)

        # Server info
        if self.server.is_running:
            uptime = self.server.uptime
            print(f"  Status:    {Colors.success('Running')}")
            print(f"  PID:       {self.server.pid}")
            print(f"  Uptime:    {format_duration(uptime) if uptime else 'Unknown'}")
        else:
            print(f"  Status:    {Colors.error('Stopped')}")

        # Backup info
        latest_backup = self.backup_manager.get_latest_backup()
        if latest_backup:
            print(f"  Last backup: {latest_backup.timestamp.strftime('%Y-%m-%d %H:%M')}")
        else:
            print(f"  Last backup: None")

        # Syncthing info
        if self.syncthing_client.enabled:
            try:
                is_paused = self.syncthing_client.is_folder_paused()
                sync_status = "Paused" if is_paused else "Active"
                print(f"  Syncthing: {sync_status}")
            except Exception:
                print(f"  Syncthing: {Colors.warning('Unreachable')}")
        else:
            print(f"  Syncthing: Disabled")

        print("-" * 40)
        print()

    def _cmd_help(self) -> None:
        """Handle help command."""
        print(f"\n{Colors.info('Available Commands')}")
        print("-" * 40)
        print(f"\n{Colors.info('Wrapper Commands:')}")
        for cmd, desc in self.BUILTIN_COMMANDS.items():
            print(f"  {cmd:12} - {desc}")

        print(f"\n{Colors.info('Server Commands:')}")
        print("  Any other command is passed directly to the Minecraft server.")
        print("  Common commands: list, say <msg>, op <player>, whitelist add <player>")

        print(f"\n{Colors.warning('Protected Commands:')}")
        print("  stop         - Use 'quit' instead for safe shutdown")
        print("-" * 40)
        print()

    def _handle_protected(self, cmd: str, full_line: str) -> None:
        """Handle a protected command."""
        if cmd == 'stop':
            print(f"\n{Colors.warning('The stop command bypasses safe shutdown.')}")
            print("Use 'quit' for a safe shutdown that:")
            print("  - Creates a backup")
            print("  - Cleans up the lock file")
            print("  - Resumes Syncthing")
            print()

            response = input("Do you want to use safe shutdown instead? [Y/n]: ").strip().lower()
            if response in ('', 'y', 'yes'):
                self._cmd_quit()
            else:
                print(f"{Colors.warning('Sending raw stop command...')}")
                self.server.send_command('stop')


class NonInteractiveOutput:
    """Simple output handler for non-interactive mode."""

    def __init__(self, server: MinecraftServer):
        self.server = server
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start displaying server output."""
        self._running = True
        self._thread = threading.Thread(
            target=self._output_loop,
            name="output-display",
            daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop displaying output."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def _output_loop(self) -> None:
        """Display server output."""
        while self._running and self.server.is_running:
            line = self.server.read_line(timeout=0.1)
            if line:
                sys.stdout.write(line)
                if not line.endswith('\n'):
                    sys.stdout.write('\n')
                sys.stdout.flush()

"""
Minecraft server process management for the wrapper.

Handles starting, stopping, and monitoring the Minecraft server process.
"""

import os
import subprocess
import shutil
import time
from pathlib import Path
from typing import Optional, IO

from .utils import get_logger


class ServerError(Exception):
    """Server-related error."""
    pass


class MinecraftServer:
    """Manages the Minecraft server process."""

    def __init__(
        self,
        server_folder: Path,
        jar_name: str = "server.jar",
        java_path: str = "java",
        min_memory: str = "1G",
        max_memory: str = "4G",
        extra_args: Optional[list[str]] = None
    ):
        """
        Initialize the server manager.

        Args:
            server_folder: Path to the server folder
            jar_name: Name of the server JAR file
            java_path: Path to Java executable
            min_memory: Minimum heap size (e.g., "1G")
            max_memory: Maximum heap size (e.g., "4G")
            extra_args: Additional JVM arguments
        """
        self.server_folder = server_folder
        self.jar_name = jar_name
        self.java_path = java_path
        self.min_memory = min_memory
        self.max_memory = max_memory
        self.extra_args = extra_args or []
        self.logger = get_logger()

        self._process: Optional[subprocess.Popen] = None
        self._start_time: Optional[float] = None

    @property
    def jar_path(self) -> Path:
        """Get the full path to the server JAR."""
        return self.server_folder / self.jar_name

    @property
    def is_running(self) -> bool:
        """Check if the server process is running."""
        if self._process is None:
            return False
        return self._process.poll() is None

    @property
    def pid(self) -> Optional[int]:
        """Get the server process ID, if running."""
        if self._process is None:
            return None
        return self._process.pid

    @property
    def uptime(self) -> Optional[float]:
        """Get server uptime in seconds, if running."""
        if self._start_time is None or not self.is_running:
            return None
        return time.time() - self._start_time

    @property
    def stdin(self) -> Optional[IO]:
        """Get the server's stdin stream."""
        if self._process is None:
            return None
        return self._process.stdin

    @property
    def stdout(self) -> Optional[IO]:
        """Get the server's stdout stream."""
        if self._process is None:
            return None
        return self._process.stdout

    @property
    def stderr(self) -> Optional[IO]:
        """Get the server's stderr stream."""
        if self._process is None:
            return None
        return self._process.stderr

    def check_java(self) -> tuple[bool, str]:
        """
        Check if Java is installed and get version.

        Returns:
            Tuple of (is_available, version_string)
        """
        try:
            result = subprocess.run(
                [self.java_path, "-version"],
                capture_output=True,
                text=True,
                timeout=10
            )
            # Java outputs version to stderr
            version_output = result.stderr or result.stdout
            # Extract first line which typically contains version
            version_line = version_output.strip().split('\n')[0]
            return True, version_line
        except FileNotFoundError:
            return False, f"Java not found at: {self.java_path}"
        except subprocess.TimeoutExpired:
            return False, "Java check timed out"
        except Exception as e:
            return False, f"Error checking Java: {e}"

    def check_jar(self) -> tuple[bool, str]:
        """
        Check if the server JAR exists.

        Returns:
            Tuple of (exists, message)
        """
        if not self.jar_path.exists():
            return False, f"Server JAR not found: {self.jar_path}"
        return True, f"Server JAR: {self.jar_path}"

    def build_command(self) -> list[str]:
        """Build the command to start the server."""
        cmd = [
            self.java_path,
            f"-Xms{self.min_memory}",
            f"-Xmx{self.max_memory}",
        ]

        # Add extra JVM arguments
        cmd.extend(self.extra_args)

        # Add JAR and nogui flag
        cmd.extend([
            "-jar", str(self.jar_path),
            "nogui"
        ])

        return cmd

    def start(self) -> int:
        """
        Start the Minecraft server.

        Returns:
            Process ID of the server

        Raises:
            ServerError: If server fails to start
        """
        if self.is_running:
            raise ServerError("Server is already running")

        # Verify JAR exists
        jar_ok, jar_msg = self.check_jar()
        if not jar_ok:
            raise ServerError(jar_msg)

        # Verify Java is available
        java_ok, java_msg = self.check_java()
        if not java_ok:
            raise ServerError(java_msg)

        cmd = self.build_command()
        self.logger.info(f"Starting server: {' '.join(cmd)}")

        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # Merge stderr into stdout
                cwd=self.server_folder,
                text=True,
                bufsize=1,  # Line buffered
            )
            self._start_time = time.time()

            self.logger.info(f"Server started with PID: {self._process.pid}")
            return self._process.pid

        except OSError as e:
            raise ServerError(f"Failed to start server: {e}")

    def send_command(self, command: str) -> bool:
        """
        Send a command to the server.

        Args:
            command: Command to send (without newline)

        Returns:
            True if command was sent
        """
        if not self.is_running or self._process is None:
            return False

        try:
            self._process.stdin.write(command + "\n")
            self._process.stdin.flush()
            self.logger.debug(f"Sent command: {command}")
            return True
        except (OSError, BrokenPipeError) as e:
            self.logger.error(f"Failed to send command: {e}")
            return False

    def stop(self, timeout: int = 60) -> bool:
        """
        Stop the server gracefully.

        Sends the 'stop' command and waits for the process to exit.

        Args:
            timeout: Maximum time to wait for shutdown in seconds

        Returns:
            True if stopped gracefully, False if force killed
        """
        if not self.is_running:
            return True

        self.logger.info("Sending stop command to server...")
        print("[mc-server] Sending stop command to server...")

        # Send stop command
        if not self.send_command("stop"):
            self.logger.warning("Failed to send stop command, attempting force kill")
            return self.kill()

        # Wait for process to exit
        try:
            self._process.wait(timeout=timeout)
            self.logger.info("Server stopped gracefully")
            print("[mc-server] Server stopped gracefully")
            return True
        except subprocess.TimeoutExpired:
            self.logger.warning(f"Server did not stop within {timeout}s, force killing")
            print(f"[mc-server] Server did not stop within {timeout}s, force killing...")
            return self.kill()

    def kill(self) -> bool:
        """
        Force kill the server process.

        Returns:
            True if killed successfully
        """
        if not self.is_running or self._process is None:
            return True

        try:
            self._process.kill()
            self._process.wait(timeout=5)
            self.logger.info("Server force killed")
            return True
        except subprocess.TimeoutExpired:
            self.logger.error("Failed to kill server process")
            return False
        except OSError as e:
            self.logger.error(f"Error killing server: {e}")
            return False

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        """
        Wait for the server process to exit.

        Args:
            timeout: Maximum time to wait (None = wait forever)

        Returns:
            Exit code, or None if still running after timeout
        """
        if self._process is None:
            return None

        try:
            return self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def read_line(self, timeout: float = 0.1) -> Optional[str]:
        """
        Read a line from server output (non-blocking).

        Args:
            timeout: How long to wait for a line

        Returns:
            Line of output, or None if no output available
        """
        if not self.is_running or self._process is None:
            return None

        import select

        # Check if there's data available to read
        try:
            readable, _, _ = select.select([self._process.stdout], [], [], timeout)
            if readable:
                line = self._process.stdout.readline()
                return line if line else None
            return None
        except (ValueError, OSError):
            # stdout closed
            return None

    def cleanup(self) -> None:
        """Clean up the process resources."""
        if self._process is not None:
            try:
                if self._process.stdin:
                    self._process.stdin.close()
                if self._process.stdout:
                    self._process.stdout.close()
                if self._process.stderr:
                    self._process.stderr.close()
            except OSError:
                pass
            self._process = None
            self._start_time = None

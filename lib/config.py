"""
Configuration loading and validation for the Minecraft server wrapper.

Handles loading config.yaml and secrets.yaml files.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .utils import get_hostname, check_file_permissions, get_logger, Colors


@dataclass
class ServerConfig:
    """Minecraft server configuration."""
    folder: Path
    jar_name: str = "server.jar"
    java_path: str = "java"
    min_memory: str = "1G"
    max_memory: str = "4G"
    extra_args: list[str] = field(default_factory=list)


@dataclass
class BackupConfig:
    """Backup configuration."""
    folder: Path
    auto_prune: bool = True
    keep_minimum: int = 5
    keep_days: int = 30


@dataclass
class SyncthingConfig:
    """Syncthing configuration."""
    url: str = "http://localhost:8384"
    folder_id: str = "minecraft-server"
    api_key: str = ""


@dataclass
class SafetyConfig:
    """Safety-related configuration."""
    heartbeat_interval: int = 30
    stale_threshold: int = 60
    race_wait: int = 10
    sync_wait_timeout: int = 300


@dataclass
class LoggingConfig:
    """Logging configuration."""
    file: str = "mc-server.log"
    level: str = "INFO"


@dataclass
class Config:
    """Complete wrapper configuration."""
    server: ServerConfig
    backup: BackupConfig
    syncthing: SyncthingConfig
    safety: SafetyConfig
    logging: LoggingConfig

    @property
    def lock_file(self) -> Path:
        """Path to the lock file."""
        return self.server.folder / "server.lock"

    @property
    def log_file(self) -> Path:
        """Path to the log file."""
        return self.server.folder / self.logging.file

    @property
    def world_folder(self) -> Path:
        """Path to the world folder."""
        return self.server.folder / "world"

    @property
    def server_jar(self) -> Path:
        """Path to the server JAR file."""
        return self.server.folder / self.server.jar_name


class ConfigError(Exception):
    """Configuration error."""
    pass


def find_config_file() -> Optional[Path]:
    """
    Find the config file in standard locations.

    Returns:
        Path to config file, or None if not found
    """
    # Check same directory as script first
    script_dir = Path(__file__).parent.parent
    local_config = script_dir / "config.yaml"
    if local_config.exists():
        return local_config

    # Check user config directory
    user_config = Path.home() / ".config" / "mc-server" / "config.yaml"
    if user_config.exists():
        return user_config

    return None


def find_secrets_file() -> Optional[Path]:
    """
    Find the unified secrets.yaml file.

    Returns:
        Path to secrets file, or None if not found
    """
    script_dir = Path(__file__).parent.parent

    # Check standard locations for unified secrets.yaml
    for location in [
        script_dir / "secrets.yaml",
        Path.home() / ".config" / "mc-server" / "secrets.yaml",
    ]:
        if location.exists():
            return location

    return None


def load_yaml_file(path: Path) -> dict[str, Any]:
    """Load a YAML file and return its contents."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
            return data if data else {}
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}")
    except OSError as e:
        raise ConfigError(f"Could not read {path}: {e}")


def validate_path(path_str: str, description: str, must_exist: bool = True) -> Path:
    """Validate and return a Path object."""
    if not path_str:
        raise ConfigError(f"{description} path is required")

    path = Path(path_str).expanduser().resolve()

    if must_exist and not path.exists():
        raise ConfigError(f"{description} does not exist: {path}")

    return path


def parse_server_config(data: dict[str, Any]) -> ServerConfig:
    """Parse server configuration section."""
    server_data = data.get('server', {})

    folder_str = server_data.get('folder', '')
    if not folder_str:
        # Default to script directory
        folder = Path(__file__).parent.parent
    else:
        folder = validate_path(folder_str, "Server folder")

    return ServerConfig(
        folder=folder,
        jar_name=server_data.get('jar_name', 'server.jar'),
        java_path=server_data.get('java_path', 'java'),
        min_memory=server_data.get('min_memory', '1G'),
        max_memory=server_data.get('max_memory', '4G'),
        extra_args=server_data.get('extra_args', []),
    )


def parse_backup_config(data: dict[str, Any], server_folder: Path) -> BackupConfig:
    """Parse backup configuration section."""
    backup_data = data.get('backup', {})

    folder_str = backup_data.get('folder', '')
    if not folder_str:
        raise ConfigError("Backup folder path is required in config.yaml")

    folder = Path(folder_str).expanduser().resolve()

    # Create backup folder if it doesn't exist
    if not folder.exists():
        try:
            folder.mkdir(parents=True)
            get_logger().info(f"Created backup folder: {folder}")
        except OSError as e:
            raise ConfigError(f"Could not create backup folder: {e}")

    return BackupConfig(
        folder=folder,
        auto_prune=backup_data.get('auto_prune', True),
        keep_minimum=backup_data.get('keep_minimum', 5),
        keep_days=backup_data.get('keep_days', 30),
    )


def parse_syncthing_config(data: dict[str, Any], secrets: dict[str, Any]) -> SyncthingConfig:
    """Parse Syncthing configuration section."""
    syncthing_data = data.get('syncthing', {})

    # Look up API key by hostname in unified secrets file
    hostname = get_hostname()
    machines = secrets.get('machines', {})
    machine_config = machines.get(hostname, {})
    api_key = machine_config.get('syncthing_api_key', '')

    return SyncthingConfig(
        url=syncthing_data.get('url', 'http://localhost:8384'),
        folder_id=syncthing_data.get('folder_id', 'minecraft-server'),
        api_key=api_key,
    )


def parse_safety_config(data: dict[str, Any]) -> SafetyConfig:
    """Parse safety configuration section."""
    safety_data = data.get('safety', {})

    return SafetyConfig(
        heartbeat_interval=safety_data.get('heartbeat_interval', 30),
        stale_threshold=safety_data.get('stale_threshold', 60),
        race_wait=safety_data.get('race_wait', 10),
        sync_wait_timeout=safety_data.get('sync_wait_timeout', 300),
    )


def parse_logging_config(data: dict[str, Any]) -> LoggingConfig:
    """Parse logging configuration section."""
    logging_data = data.get('logging', {})

    return LoggingConfig(
        file=logging_data.get('file', 'mc-server.log'),
        level=logging_data.get('level', 'INFO'),
    )


def ensure_directories(config_data: dict[str, Any]) -> bool:
    """
    Verify that all required directories from the config exist, creating them if needed.

    Scans config values for paths that use ~ (home directory) and ensures
    the directories exist. This handles the case where the wrapper is run
    on a new machine that doesn't have the folder structure yet.

    Returns:
        True if all directories are OK, False if any failed to create
    """
    logger = get_logger()

    # Collect all directory paths from config that use ~
    paths_to_check: list[tuple[str, str]] = []  # (description, raw_path)

    # Server folder
    server_folder = config_data.get('server', {}).get('folder', '')
    if server_folder:
        paths_to_check.append(("Server folder", server_folder))

    # Backup folder
    backup_folder = config_data.get('backup', {}).get('folder', '')
    if backup_folder:
        paths_to_check.append(("Backup folder", backup_folder))

    if not paths_to_check:
        return True

    print(f"\n{Colors.info('Verifying required directories...')}")
    all_ok = True

    for description, raw_path in paths_to_check:
        # Only handle paths that reference home directory
        if not raw_path.startswith("~"):
            continue

        resolved = Path(raw_path).expanduser().resolve()
        if resolved.exists():
            print(f"  {Colors.success('✓')} {description}: {resolved}")
            logger.info(f"Directory verified: {description} -> {resolved}")
        else:
            try:
                resolved.mkdir(parents=True, exist_ok=True)
                print(f"  {Colors.success('✓')} {description}: Created {resolved}")
                logger.info(f"Directory created: {description} -> {resolved}")
            except OSError as e:
                print(f"  {Colors.error('✗')} {description}: Failed to create {resolved}: {e}")
                logger.error(f"Failed to create directory: {description} -> {resolved}: {e}")
                all_ok = False

    return all_ok


def load_config() -> Config:
    """
    Load and validate the complete configuration.

    Returns:
        Validated Config object

    Raises:
        ConfigError: If configuration is invalid or missing
    """
    logger = get_logger()

    # Find and load config file
    config_path = find_config_file()
    if not config_path:
        raise ConfigError(
            "Config file not found. Expected config.yaml in script directory "
            "or ~/.config/mc-server/config.yaml"
        )

    logger.debug(f"Loading config from: {config_path}")
    config_data = load_yaml_file(config_path)

    # Ensure all required directories exist
    if not ensure_directories(config_data):
        raise ConfigError("Failed to create one or more required directories")

    # Find and load secrets file
    secrets_path = find_secrets_file()
    secrets_data = {}

    if secrets_path:
        # Check file permissions on secrets file
        is_secure, msg = check_file_permissions(secrets_path)
        if not is_secure:
            logger.warning(msg)
            print(f"[mc-server] {msg}")
            print("[mc-server] Consider running: chmod 600 " + str(secrets_path))

        logger.debug(f"Loading secrets from: {secrets_path}")
        secrets_data = load_yaml_file(secrets_path)
    else:
        raise ConfigError(
            "Secrets file not found. Expected secrets.yaml in script directory "
            "or ~/.config/mc-server/secrets.yaml"
        )

    # Parse all sections
    server_config = parse_server_config(config_data)
    backup_config = parse_backup_config(config_data, server_config.folder)
    syncthing_config = parse_syncthing_config(config_data, secrets_data)
    safety_config = parse_safety_config(config_data)
    logging_config = parse_logging_config(config_data)

    # Validate Syncthing API key is present (required for safe operation)
    hostname = get_hostname()
    if not syncthing_config.api_key:
        raise ConfigError(
            f"Syncthing API key not found for hostname '{hostname}'. "
            f"Add entry to secrets.yaml under 'machines.{hostname}.syncthing_api_key'"
        )

    return Config(
        server=server_config,
        backup=backup_config,
        syncthing=syncthing_config,
        safety=safety_config,
        logging=logging_config,
    )


def validate_config(config: Config) -> list[str]:
    """
    Perform additional validation on the loaded config.

    Returns:
        List of warning messages (empty if all OK)
    """
    warnings = []

    # Check server JAR exists
    if not config.server_jar.exists():
        warnings.append(f"Server JAR not found: {config.server_jar}")

    # Check world folder exists (may not exist on first run)
    if not config.world_folder.exists():
        warnings.append(f"World folder not found: {config.world_folder} (may be created on first run)")

    # Validate memory settings
    for mem in [config.server.min_memory, config.server.max_memory]:
        if not mem[-1].upper() in ['K', 'M', 'G']:
            warnings.append(f"Memory setting '{mem}' should end with K, M, or G")

    return warnings

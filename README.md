# Minecraft Server Wrapper

A Python-based wrapper for safely managing a shared Minecraft server between multiple users via Syncthing.

## Features

- **Concurrency Prevention**: Lock file system prevents multiple users from running the server simultaneously
- **Syncthing Integration**: Automatically pauses/resumes folder sync during gameplay
- **Automatic Backups**: Creates backups before and after each session
- **Crash Recovery**: Detects and recovers from unclean shutdowns
- **World Integrity Checks**: Scans region files for corruption
- **Interactive Console**: Full access to Minecraft server commands with built-in wrapper commands

## Requirements

- Python 3.8+
- PyYAML (`pip install pyyaml`)
- Java (for running Minecraft server. Only tested with OpenJDK 21)
- Syncthing (for sync management)
- Each member device configured with a hostname in router settings.

## Quick Start

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
   ```bash
   brew install openjdk@21
   ```
2. **Configure the wrapper:**
   - Edit `config.yaml` and set the backup folder path
   - create `secrets.{MyMacbookAir}.yaml` with your Syncthing API key (see below)

3. **Make the script executable:**
   ```bash
   chmod +x mc-server.py
   ```

4. **Run the server:**
   ```bash
   ./mc-server.py
   ```

## Configuration

### config.yaml

Shared configuration file for all users:

```yaml
server:
  folder: ""           # Leave empty to use script directory
  jar_name: "server.jar"
  java_path: "java"
  min_memory: "1G"
  max_memory: "4G"

backup:
  folder: "/path/to/backups"  # REQUIRED - outside synced folder
  keep_minimum: 5
  keep_days: 30

syncthing:
  url: "http://localhost:8384"
  folder_id: "minecraft-server"
```

### secrets.{hostname}.yaml

Machine-specific secrets file (e.g., `secrets.MyMacbookAir.yaml`):

```yaml
syncthing:
  api_key: "your-api-key-here"
```

To get your Syncthing API key:
1. Open Syncthing web UI (http://localhost:8384)
2. Go to Actions > Settings > General
3. Copy the API Key

## Usage

### Normal Startup
```bash
./mc-server.py
```

### Check Status
```bash
./mc-server.py --status
```

### Create Backup
```bash
./mc-server.py --backup
```

### Restore from Backup
```bash
./mc-server.py --restore
```

## Console Commands

Once the server is running, you have access to these wrapper commands:

| Command | Description |
|---------|-------------|
| `quit` or `exit` | Trigger safe shutdown sequence |
| `backup` | Create a manual backup without stopping |
| `status` | Show current server status |
| `help` | Show available commands |

Any other command is passed directly to the Minecraft server (e.g., `list`, `say`, `op`).

## How It Works

### Startup Sequence

1. Pre-flight checks (Java, JAR file, folders)
2. Wait for Syncthing to finish syncing
3. Check for existing locks (handle crashes if needed)
4. Create pre-start backup (if world changed)
5. Pause Syncthing folder
6. Acquire lock with race condition prevention
7. Start Minecraft server
8. Enter interactive console mode

### Shutdown Sequence

1. Send `stop` command to server
2. Wait for graceful shutdown (force kill after 60s)
3. Create post-stop backup
4. Delete lock file
5. Prune old backups
6. Resume Syncthing folder

### Lock File

The `server.lock` file contains:
```yaml
hostname: MyMacbookAir
started_at: 2025-01-29T10:30:00Z
last_heartbeat: 2025-01-29T11:45:30Z
pid: 12345
```

The heartbeat is updated every 30 seconds. A lock is considered stale after 60 seconds without an update.

## Troubleshooting

### "Server is already running on another machine"
Wait for the other user to stop their server, or if they've crashed, the lock will become stale after 60 seconds.

### Syncthing not pausing
Make sure your API key is correct in `secrets.{hostname}.yaml` and that the `folder_id` in `config.yaml` matches your Syncthing folder ID.

### World corruption detected
The wrapper will offer to restore from a backup. Choose the most recent backup from before the corruption occurred.

## File Structure

```
minecraft-server/
├── mc-server.py           # Entry point
├── config.yaml            # Shared configuration
├── secrets.MyMacbookAir.yaml # My secrets
├── secrets.FriendsMacbookAir.yaml  # Friend's secrets
├── requirements.txt       # Python dependencies
├── lib/                   # Wrapper library
│   ├── __init__.py
│   ├── main.py           # Main orchestration
│   ├── config.py         # Configuration loading
│   ├── syncthing.py      # Syncthing API client
│   ├── backup.py         # Backup management
│   ├── lock.py           # Lock file management
│   ├── integrity.py      # World integrity checks
│   ├── server.py         # Server process management
│   ├── console.py        # Interactive console
│   └── utils.py          # Shared utilities
├── server.jar             # Minecraft server
├── world/                 # Minecraft world
├── server.lock            # Lock file (runtime)
└── mc-server.log          # Log file (runtime)
```

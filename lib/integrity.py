"""
World integrity checks for the Minecraft server wrapper.

Scans Minecraft region files (.mca) to detect corruption.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .utils import get_logger, format_size, Colors


# Minecraft region files use 4096-byte sectors
SECTOR_SIZE = 4096


@dataclass
class RegionFileIssue:
    """Issue found with a region file."""
    file: Path
    issue_type: str  # "zero_byte", "truncated", "unreadable"
    details: str

    def __str__(self) -> str:
        return f"{self.file.name}: {self.issue_type} - {self.details}"


@dataclass
class IntegrityReport:
    """Report from world integrity check."""
    world_folder: Path
    total_files: int = 0
    checked_files: int = 0
    issues: list[RegionFileIssue] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def has_issues(self) -> bool:
        """Check if any issues were found."""
        return len(self.issues) > 0 or self.error is not None

    @property
    def is_healthy(self) -> bool:
        """Check if world appears healthy."""
        return not self.has_issues

    def summary(self) -> str:
        """Get a summary string of the report."""
        if self.error:
            return f"Error during check: {self.error}"

        if not self.has_issues:
            return f"World appears healthy ({self.checked_files} region files checked)"

        return (
            f"Found {len(self.issues)} issues in {self.checked_files} region files:\n"
            + "\n".join(f"  - {issue}" for issue in self.issues)
        )


def check_region_file(file_path: Path) -> Optional[RegionFileIssue]:
    """
    Check a single region file for corruption.

    Args:
        file_path: Path to the .mca file

    Returns:
        RegionFileIssue if problem found, None if OK
    """
    try:
        size = file_path.stat().st_size

        # Check for zero-byte files
        if size == 0:
            return RegionFileIssue(
                file=file_path,
                issue_type="zero_byte",
                details="File is empty (0 bytes)"
            )

        # Check for truncated files (size should be multiple of sector size)
        # A valid region file should be at least 8192 bytes (2 sectors for headers)
        if size < SECTOR_SIZE * 2:
            return RegionFileIssue(
                file=file_path,
                issue_type="truncated",
                details=f"File too small ({format_size(size)}, expected at least 8KB)"
            )

        # Check if size is a multiple of sector size
        if size % SECTOR_SIZE != 0:
            return RegionFileIssue(
                file=file_path,
                issue_type="truncated",
                details=f"Size ({format_size(size)}) not a multiple of {SECTOR_SIZE} bytes"
            )

        return None

    except OSError as e:
        return RegionFileIssue(
            file=file_path,
            issue_type="unreadable",
            details=str(e)
        )


def find_region_folders(world_folder: Path) -> list[Path]:
    """
    Find all region folders in a world.

    Minecraft stores region files in:
    - world/region/ (Overworld)
    - world/DIM-1/region/ (Nether)
    - world/DIM1/region/ (End)

    Returns:
        List of paths to region folders
    """
    region_folders = []

    # Overworld
    overworld = world_folder / "region"
    if overworld.exists():
        region_folders.append(overworld)

    # Nether
    nether = world_folder / "DIM-1" / "region"
    if nether.exists():
        region_folders.append(nether)

    # End
    end = world_folder / "DIM1" / "region"
    if end.exists():
        region_folders.append(end)

    # Also check for modded dimensions (DIM* pattern)
    for dim_folder in world_folder.glob("DIM*"):
        if dim_folder.is_dir():
            region = dim_folder / "region"
            if region.exists() and region not in region_folders:
                region_folders.append(region)

    return region_folders


def check_world_integrity(world_folder: Path) -> IntegrityReport:
    """
    Check the integrity of a Minecraft world.

    Scans all region files (.mca) and checks for common corruption indicators:
    - Zero-byte files
    - Truncated files (size not multiple of 4096)
    - Unreadable files

    Args:
        world_folder: Path to the world folder

    Returns:
        IntegrityReport with findings
    """
    logger = get_logger()
    report = IntegrityReport(world_folder=world_folder)

    if not world_folder.exists():
        report.error = "World folder does not exist"
        return report

    # Find all region folders
    region_folders = find_region_folders(world_folder)

    if not region_folders:
        logger.warning("No region folders found in world")
        report.error = "No region folders found (world may be empty or invalid)"
        return report

    # Scan all region files
    for region_folder in region_folders:
        for mca_file in region_folder.glob("*.mca"):
            report.total_files += 1
            report.checked_files += 1

            issue = check_region_file(mca_file)
            if issue:
                report.issues.append(issue)
                logger.warning(f"Issue found: {issue}")

    logger.debug(f"Checked {report.checked_files} region files, found {len(report.issues)} issues")
    return report


def print_integrity_report(report: IntegrityReport) -> None:
    """
    Print a formatted integrity report to the console.

    Args:
        report: IntegrityReport to print
    """
    if report.error:
        print(f"\n{Colors.error('World Integrity Check: ERROR')}")
        print(f"  {report.error}")
        return

    if report.is_healthy:
        print(f"\n{Colors.success('World Integrity Check: PASSED')}")
        print(f"  Checked {report.checked_files} region files")
        print("  No issues found")
    else:
        print(f"\n{Colors.warning('World Integrity Check: ISSUES FOUND')}")
        print(f"  Checked {report.checked_files} region files")
        print(f"  Found {len(report.issues)} issues:\n")

        for issue in report.issues:
            issue_color = Colors.RED if issue.issue_type == "zero_byte" else Colors.YELLOW
            print(f"  {Colors.wrap('•', issue_color)} {issue.file.name}")
            print(f"    Type: {issue.issue_type}")
            print(f"    {issue.details}")
            print()


def get_world_stats(world_folder: Path) -> dict:
    """
    Get basic statistics about a world.

    Returns:
        Dictionary with world stats
    """
    stats = {
        'exists': world_folder.exists(),
        'region_folders': 0,
        'region_files': 0,
        'total_size': 0,
    }

    if not world_folder.exists():
        return stats

    region_folders = find_region_folders(world_folder)
    stats['region_folders'] = len(region_folders)

    for region_folder in region_folders:
        for mca_file in region_folder.glob("*.mca"):
            stats['region_files'] += 1
            try:
                stats['total_size'] += mca_file.stat().st_size
            except OSError:
                pass

    return stats

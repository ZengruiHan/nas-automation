#!/usr/bin/env python3
"""Safe automation helper for organizing files on a NAS.

The script defaults to dry-run mode. Add --apply to commands that change files.
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import getpass
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from string import Formatter
from typing import Any, Iterable


DEFAULT_IGNORE_FILES = {".DS_Store", "Thumbs.db"}
DEFAULT_IGNORE_DIRS = {".git", "__pycache__", "@eaDir", "#recycle", ".Trash", ".Trashes"}
HASH_CHUNK_SIZE = 1024 * 1024
ARCHIVE_SUFFIXES = {
    "zip": ".zip",
    "rar": ".rar",
    "7z": ".7z",
    "tar": ".tar",
    "tar_gz": ".tar.gz",
    "tar_bz2": ".tar.bz2",
    "tar_xz": ".tar.xz",
}
ARCHIVE_SUFFIX_ALIASES = {
    "zip": (".zip",),
    "rar": (".rar",),
    "7z": (".7z",),
    "tar": (".tar",),
    "tar_gz": (".tar.gz", ".tgz"),
    "tar_bz2": (".tar.bz2", ".tbz2", ".tbz"),
    "tar_xz": (".tar.xz", ".txz"),
}
EXTERNAL_EXTRACTORS = ("7zz", "7z", "7za")


@dataclass(frozen=True)
class Operation:
    action: str
    source: Path | None
    target: Path | None
    reason: str


@dataclass(frozen=True)
class Rule:
    name: str
    source: str
    target: str
    extensions: tuple[str, ...]
    older_than_days: float | None
    newer_than_days: float | None
    min_size_mb: float | None
    max_size_mb: float | None
    rename_template: str
    recursive: bool


@dataclass(frozen=True)
class Config:
    roots: dict[str, Path]
    ignore_files: tuple[str, ...]
    ignore_dirs: tuple[str, ...]
    organize_rules: tuple[Rule, ...]


class ConfigError(ValueError):
    """Raised when the JSON config is invalid."""


class ArchiveError(RuntimeError):
    """Raised when an archive cannot be extracted."""


class ArchivePasswordError(ArchiveError):
    """Raised when an archive needs a password that was not provided."""


class ArchiveUnsupportedError(ArchiveError):
    """Raised when an archive uses features unsupported by available tools."""


class ArchiveUnsafePathError(ArchiveError):
    """Raised when a ZIP member would write outside the extraction folder."""


class SafeDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def expand_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def normalize_extension(ext: str) -> str:
    if ext == "*":
        return ext
    ext = ext.strip().lower()
    if not ext:
        return ext
    return ext if ext.startswith(".") else "." + ext


def load_config(path: Path) -> Config:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc

    roots_raw = data.get("roots", {})
    if not isinstance(roots_raw, dict):
        raise ConfigError("'roots' must be an object")
    roots = {str(key): expand_path(str(value)) for key, value in roots_raw.items()}

    ignore_files = tuple(sorted(DEFAULT_IGNORE_FILES | set(data.get("ignore_files", data.get("ignore", [])))))
    ignore_dirs = tuple(sorted(DEFAULT_IGNORE_DIRS | set(data.get("ignore_dirs", []))))

    rules = []
    for index, raw in enumerate(data.get("organize", []), start=1):
        if not isinstance(raw, dict):
            raise ConfigError(f"Rule #{index} must be an object")
        name = str(raw.get("name", f"rule-{index}"))
        source = raw.get("source")
        target = raw.get("target")
        if not source or not target:
            raise ConfigError(f"Rule {name!r} must include 'source' and 'target'")
        extensions = tuple(normalize_extension(str(ext)) for ext in raw.get("extensions", ["*"]))
        rules.append(
            Rule(
                name=name,
                source=str(source),
                target=str(target),
                extensions=extensions,
                older_than_days=_optional_float(raw.get("older_than_days")),
                newer_than_days=_optional_float(raw.get("newer_than_days")),
                min_size_mb=_optional_float(raw.get("min_size_mb")),
                max_size_mb=_optional_float(raw.get("max_size_mb")),
                rename_template=str(raw.get("rename_template", "{name}")),
                recursive=bool(raw.get("recursive", True)),
            )
        )

    return Config(
        roots=roots,
        ignore_files=ignore_files,
        ignore_dirs=ignore_dirs,
        organize_rules=tuple(rules),
    )


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def resolve_config_path(value: str, roots: dict[str, Path]) -> Path:
    if value in roots:
        return roots[value]
    first, remainder = split_first_path_part(value)
    if first in roots:
        return (roots[first] / remainder).resolve()
    return expand_path(value)


def split_first_path_part(value: str) -> tuple[str, Path]:
    parts = Path(value).parts
    if not parts:
        return value, Path()
    if Path(value).is_absolute():
        return value, Path()
    first = parts[0]
    remainder = Path(*parts[1:]) if len(parts) > 1 else Path()
    return first, remainder


def should_ignore(path: Path, file_patterns: Iterable[str], dir_patterns: Iterable[str]) -> bool:
    patterns = dir_patterns if path.is_dir() else file_patterns
    return any(fnmatch.fnmatch(path.name, pattern) for pattern in patterns)


def iter_files(
    root: Path,
    ignore_files: Iterable[str] = DEFAULT_IGNORE_FILES,
    ignore_dirs: Iterable[str] = DEFAULT_IGNORE_DIRS,
    recursive: bool = True,
    include_symlinks: bool = False,
) -> Iterable[Path]:
    if not root.exists():
        return
    if not recursive:
        for child in sorted(root.iterdir()):
            if child.is_file() and (include_symlinks or not child.is_symlink()):
                if not should_ignore(child, ignore_files, ignore_dirs):
                    yield child
        return

    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        dirnames[:] = [
            dirname
            for dirname in sorted(dirnames)
            if not should_ignore(current / dirname, ignore_files, ignore_dirs)
            and (include_symlinks or not (current / dirname).is_symlink())
        ]
        for filename in sorted(filenames):
            path = current / filename
            if include_symlinks or not path.is_symlink():
                if not should_ignore(path, ignore_files, ignore_dirs):
                    yield path


def build_template_context(path: Path) -> dict[str, str]:
    stat = path.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime)
    ext = path.suffix.lower().lstrip(".")
    return {
        "name": path.name,
        "stem": path.stem,
        "suffix": path.suffix,
        "ext": ext or "no-extension",
        "year": f"{mtime.year:04d}",
        "month": f"{mtime.month:02d}",
        "day": f"{mtime.day:02d}",
        "ym": f"{mtime.year:04d}-{mtime.month:02d}",
    }


def render_template(template: str, path: Path) -> Path:
    context = SafeDict(build_template_context(path))
    rendered = Formatter().vformat(template, (), context)
    return Path(rendered)


def matches_rule(path: Path, rule: Rule, now: datetime) -> bool:
    suffix = path.suffix.lower()
    if "*" not in rule.extensions and suffix not in rule.extensions:
        return False

    stat = path.stat()
    size_mb = stat.st_size / (1024 * 1024)
    if rule.min_size_mb is not None and size_mb < rule.min_size_mb:
        return False
    if rule.max_size_mb is not None and size_mb > rule.max_size_mb:
        return False

    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    age_days = (now - mtime).total_seconds() / 86400
    if rule.older_than_days is not None and age_days < rule.older_than_days:
        return False
    if rule.newer_than_days is not None and age_days > rule.newer_than_days:
        return False
    return True


def plan_organize(config: Config, include_symlinks: bool = False) -> list[Operation]:
    operations: list[Operation] = []
    now = datetime.now(timezone.utc)

    for rule in config.organize_rules:
        source = resolve_config_path(rule.source, config.roots)
        if not source.exists():
            operations.append(Operation("skip", source, None, f"source missing for rule {rule.name!r}"))
            continue

        target_base = resolve_config_path(rule.target, config.roots)
        files = list(
            iter_files(
                source,
                ignore_files=config.ignore_files,
                ignore_dirs=config.ignore_dirs,
                recursive=rule.recursive,
                include_symlinks=include_symlinks,
            )
        )
        for path in files:
            if not matches_rule(path, rule, now):
                continue
            target_dir = (
                resolve_rendered_target(rule.target, config.roots, path)
                if "{" in rule.target
                else target_base
            )
            target_name = render_template(rule.rename_template, path).name
            target = unique_destination(target_dir / target_name)
            if path.resolve() == target.resolve():
                continue
            operations.append(Operation("move", path, target, f"rule {rule.name!r}"))

    return operations


def resolve_rendered_target(template: str, roots: dict[str, Path], path: Path) -> Path:
    first, remainder = split_first_path_part(template)
    if first in roots:
        rendered_remainder = render_template(str(remainder), path)
        return (roots[first] / rendered_remainder).resolve()
    return expand_path(str(render_template(template, path)))


def unique_destination(target: Path) -> Path:
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    parent = target.parent
    for index in range(1, 10_000):
        candidate = parent / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find a free destination for {target}")


def apply_operations(operations: Iterable[Operation], apply: bool) -> int:
    count = 0
    for op in operations:
        prefix = "APPLY" if apply else "DRY-RUN"
        if op.action == "move" and op.source and op.target:
            print(f"[{prefix}] move {op.source} -> {op.target} ({op.reason})")
            if apply:
                op.target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(op.source), str(op.target))
            count += 1
        elif op.action == "remove-empty-dir" and op.source:
            print(f"[{prefix}] remove empty dir {op.source} ({op.reason})")
            if apply:
                op.source.rmdir()
            count += 1
        elif op.action == "skip":
            print(f"[SKIP] {op.source}: {op.reason}")
    return count


def scan(root: Path, top: int, include_symlinks: bool) -> int:
    files = list(iter_files(root, recursive=True, include_symlinks=include_symlinks))
    total_size = sum(path.stat().st_size for path in files)
    by_ext: Counter[str] = Counter(path.suffix.lower() or "[no extension]" for path in files)
    largest = sorted(files, key=lambda item: item.stat().st_size, reverse=True)[:top]

    print(f"Root: {root}")
    print(f"Files: {len(files):,}")
    print(f"Total size: {human_size(total_size)}")
    print("\nBy extension:")
    for ext, count in by_ext.most_common(25):
        print(f"  {ext:16} {count:8,}")

    if largest:
        print(f"\nLargest {len(largest)} files:")
        for path in largest:
            print(f"  {human_size(path.stat().st_size):>10}  {path}")
    return 0


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_duplicates(root: Path, min_size_mb: float, export_csv: Path | None, include_symlinks: bool) -> int:
    min_size = int(min_size_mb * 1024 * 1024)
    by_size: dict[int, list[Path]] = defaultdict(list)
    for path in iter_files(root, recursive=True, include_symlinks=include_symlinks):
        size = path.stat().st_size
        if size >= min_size:
            by_size[size].append(path)

    by_hash: dict[tuple[int, str], list[Path]] = defaultdict(list)
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        for path in paths:
            by_hash[(size, sha256_file(path))].append(path)

    duplicate_groups = [sorted(paths) for paths in by_hash.values() if len(paths) > 1]
    duplicate_groups.sort(key=lambda group: (-group[0].stat().st_size, str(group[0])))

    rows: list[dict[str, str]] = []
    for group_index, group in enumerate(duplicate_groups, start=1):
        keep = group[0]
        size = keep.stat().st_size
        print(f"\nGroup {group_index}: {len(group)} copies, {human_size(size)} each")
        print(f"  keep: {keep}")
        for duplicate in group[1:]:
            print(f"  dup:  {duplicate}")
        for path in group:
            rows.append(
                {
                    "group": str(group_index),
                    "role": "keep" if path == keep else "duplicate",
                    "size_bytes": str(size),
                    "path": str(path),
                }
            )

    print(f"\nDuplicate groups: {len(duplicate_groups)}")
    if export_csv:
        export_csv.parent.mkdir(parents=True, exist_ok=True)
        with export_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["group", "role", "size_bytes", "path"])
            writer.writeheader()
            writer.writerows(rows)
        print(f"CSV written: {export_csv}")
    return 0


def archive_type(path: Path, forced_extensions: dict[str, str] | None = None) -> str | None:
    try:
        with path.open("rb") as handle:
            header = handle.read(512)
    except OSError:
        return None

    if header.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if header.startswith(b"Rar!\x1a\x07\x00") or header.startswith(b"Rar!\x1a\x07\x01\x00"):
        return "rar"
    if header.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        try:
            if zipfile.is_zipfile(path):
                return "zip"
        except OSError:
            return None
    if is_tar_archive(path):
        if header.startswith(b"\x1f\x8b"):
            return "tar_gz"
        if header.startswith(b"BZh"):
            return "tar_bz2"
        if header.startswith(b"\xfd7zXZ\x00"):
            return "tar_xz"
        return "tar"
    if forced_extensions:
        return forced_extensions.get(path.suffix.lower())
    return None


def is_supported_archive(path: Path) -> bool:
    return archive_type(path) is not None


def is_zip_archive(path: Path) -> bool:
    try:
        return path.is_file() and zipfile.is_zipfile(path)
    except OSError:
        return False


def corrected_archive_path(path: Path, kind: str) -> Path:
    suffix = ARCHIVE_SUFFIXES[kind]
    if has_archive_suffix(path, kind):
        return path
    if path.suffix:
        return path.with_suffix(suffix)
    return path.with_name(path.name + suffix)


def default_extract_dir(path: Path) -> Path:
    lower_name = path.name.lower()
    for suffixes in ARCHIVE_SUFFIX_ALIASES.values():
        for suffix in sorted(suffixes, key=len, reverse=True):
            if lower_name.endswith(suffix):
                return path.with_name(path.name[: -len(suffix)])
    return path.with_suffix("")


def has_archive_suffix(path: Path, kind: str) -> bool:
    lower_name = path.name.lower()
    return any(lower_name.endswith(suffix) for suffix in ARCHIVE_SUFFIX_ALIASES[kind])


def is_tar_archive(path: Path) -> bool:
    try:
        return path.is_file() and tarfile.is_tarfile(path)
    except (OSError, tarfile.TarError):
        return False


def safe_zip_member_path(info: zipfile.ZipInfo) -> Path:
    raw_name = info.filename.replace("\\", "/")
    member = PurePosixPath(raw_name)
    if raw_name.startswith("/") or member.is_absolute() or ".." in member.parts:
        raise ArchiveUnsafePathError(f"unsafe member path {info.filename!r}")
    return Path(*member.parts)


def extract_zip_to_dir(zip_path: Path, extract_dir: Path, passwords: list[str]) -> None:
    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        for info in infos:
            safe_zip_member_path(info)
        encrypted = any(info.flag_bits & 0x1 for info in infos)

    if encrypted and not passwords:
        raise ArchivePasswordError("password required")

    candidates: list[str | None] = passwords if encrypted else [None]
    password_errors: list[str] = []

    for password in candidates:
        staging = unique_destination(extract_dir.with_name(extract_dir.name + ".__extracting__"))
        staging.mkdir(parents=True, exist_ok=False)
        try:
            with zipfile.ZipFile(zip_path) as archive:
                pwd = password.encode("utf-8") if password is not None else None
                for info in archive.infolist():
                    relative = safe_zip_member_path(info)
                    target = staging / relative
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue

                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        target = unique_destination(target)
                    with archive.open(info, "r", pwd=pwd) as source, target.open("wb") as destination:
                        shutil.copyfileobj(source, destination)
                    apply_zip_mtime(target, info)

            staging.rename(extract_dir)
            return
        except RuntimeError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            if encrypted:
                password_errors.append(str(exc))
                continue
            raise ArchiveError(str(exc)) from exc
        except NotImplementedError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise ArchiveUnsupportedError(str(exc)) from exc
        except (OSError, zipfile.BadZipFile) as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise ArchiveError(str(exc)) from exc

    detail = "; ".join(password_errors) if password_errors else "password did not work"
    raise ArchivePasswordError(detail)


def safe_tar_member_path(info: tarfile.TarInfo) -> Path:
    raw_name = info.name.replace("\\", "/")
    member = PurePosixPath(raw_name)
    if raw_name.startswith("/") or member.is_absolute() or ".." in member.parts:
        raise ArchiveUnsafePathError(f"unsafe member path {info.name!r}")
    if info.issym() or info.islnk():
        raise ArchiveUnsafePathError(f"refusing link member {info.name!r}")
    if not member.parts:
        raise ArchiveUnsafePathError(f"empty member path {info.name!r}")
    return Path(*member.parts)


def extract_tar_to_dir(tar_path: Path, extract_dir: Path) -> None:
    staging = unique_destination(extract_dir.with_name(extract_dir.name + ".__extracting__"))
    staging.mkdir(parents=True, exist_ok=False)
    try:
        with tarfile.open(tar_path, mode="r:*") as archive:
            for info in archive:
                relative = safe_tar_member_path(info)
                target = staging / relative
                if info.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    apply_tar_mtime(target, info)
                    continue
                if not info.isfile():
                    continue

                source = archive.extractfile(info)
                if source is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    target = unique_destination(target)
                with source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
                apply_tar_mtime(target, info)

        staging.rename(extract_dir)
    except (OSError, tarfile.TarError, ArchiveUnsafePathError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise ArchiveError(str(exc)) from exc


def find_external_extractor(extractor: str | None = None) -> str | None:
    if extractor:
        expanded = Path(extractor).expanduser()
        if expanded.exists():
            return str(expanded)
        return shutil.which(extractor)
    for candidate in EXTERNAL_EXTRACTORS:
        path = shutil.which(candidate)
        if path:
            return path
    return None


def extract_with_7z(
    archive_path: Path,
    extract_dir: Path,
    passwords: list[str],
    extractor: str | None,
) -> None:
    executable = find_external_extractor(extractor)
    if not executable:
        raise ArchiveUnsupportedError("RAR/7Z extraction requires 7zz, 7z, or 7za on PATH")

    candidates: list[str | None] = [None] + passwords
    errors: list[str] = []

    for password in candidates:
        staging = unique_destination(extract_dir.with_name(extract_dir.name + ".__extracting__"))
        staging.mkdir(parents=True, exist_ok=False)
        command = [
            executable,
            "x",
            "-y",
            "-bd",
            "-bso0",
            "-bsp0",
            f"-o{staging}",
        ]
        if password is not None:
            command.append(f"-p{password}")
        else:
            command.append("-p")
        command.append(str(archive_path))

        try:
            result = subprocess.run(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=None,
            )
            if result.returncode == 0:
                staging.rename(extract_dir)
                return
            errors.append(summarize_extractor_error(result.stderr or result.stdout))
        except OSError as exc:
            errors.append(str(exc))
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    detail = "; ".join(error for error in errors if error) or "extractor failed"
    if "password" in detail.lower() or "wrong password" in detail.lower():
        raise ArchivePasswordError(detail)
    raise ArchiveError(detail)


def summarize_extractor_error(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return "extractor failed"
    return " | ".join(lines[-4:])


def extract_archive_to_dir(
    archive_path: Path,
    archive_kind: str,
    extract_dir: Path,
    passwords: list[str],
    extractor: str | None,
) -> None:
    if archive_kind.startswith("tar"):
        extract_tar_to_dir(archive_path, extract_dir)
        return

    if archive_kind == "zip":
        try:
            extract_zip_to_dir(archive_path, extract_dir, passwords)
            return
        except ArchiveUnsupportedError:
            pass
        except ArchiveError:
            raise

    extract_with_7z(archive_path, extract_dir, passwords, extractor)


def apply_zip_mtime(target: Path, info: zipfile.ZipInfo) -> None:
    try:
        mtime = datetime(*info.date_time).timestamp()
    except (TypeError, ValueError, OSError):
        return
    try:
        os.utime(target, (mtime, mtime))
    except OSError:
        pass


def apply_tar_mtime(target: Path, info: tarfile.TarInfo) -> None:
    try:
        os.utime(target, (info.mtime, info.mtime))
    except (OSError, ValueError):
        pass


def load_passwords(values: list[str], password_file: str | None, ask_password: bool) -> list[str]:
    passwords: list[str] = []
    passwords.extend(values)

    if password_file:
        path = expand_path(password_file)
        for line in path.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if value and not value.startswith("#"):
                passwords.append(value)

    if ask_password:
        value = getpass.getpass("Archive password to try: ")
        if value:
            passwords.append(value)

    return dedupe_preserve_order(passwords)


def dedupe_preserve_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def parse_forced_extensions(values: list[str]) -> dict[str, str]:
    forced: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"Invalid --force-extension value {value!r}; use .ext=archive_type")
        raw_extension, raw_kind = value.split("=", 1)
        extension = raw_extension.strip().lower()
        kind = raw_kind.strip().lower().replace("-", "_")
        if not extension:
            raise ValueError(f"Invalid --force-extension value {value!r}; extension is empty")
        if not extension.startswith("."):
            extension = "." + extension
        if kind not in ARCHIVE_SUFFIXES:
            supported = ", ".join(sorted(ARCHIVE_SUFFIXES))
            raise ValueError(f"Unsupported archive type {kind!r}; supported types: {supported}")
        forced[extension] = kind
    return forced


def list_archive_candidates(
    root: Path,
    include_symlinks: bool,
    forced_extensions: dict[str, str] | None = None,
) -> list[tuple[Path, str]]:
    candidates: list[tuple[Path, str]] = []
    for path in iter_files(root, recursive=True, include_symlinks=include_symlinks):
        kind = archive_type(path, forced_extensions)
        if kind:
            candidates.append((path, kind))
    return candidates


def list_zip_candidates(root: Path, include_symlinks: bool) -> list[Path]:
    return [
        path
        for path, kind in list_archive_candidates(root, include_symlinks)
        if kind == "zip"
    ]


def preview_deep_unzip(
    root: Path,
    include_symlinks: bool,
    delete_archives: bool,
    extractor: str | None,
    forced_extensions: dict[str, str] | None,
) -> int:
    candidates = list_archive_candidates(root, include_symlinks, forced_extensions)
    if not candidates:
        print(f"No supported archives found under {root}")
        return 0

    external_extractor = find_external_extractor(extractor)
    if any(kind in {"rar", "7z"} for _path, kind in candidates) and not external_extractor:
        print("Warning: RAR/7Z extraction requires 7zz, 7z, or 7za on PATH.")

    print(f"Found {len(candidates)} supported archive(s) under {root}")
    for path, kind in candidates:
        corrected = corrected_archive_path(path, kind)
        if corrected != path:
            corrected = unique_destination(corrected)
            print(f"[DRY-RUN] rename {path} -> {corrected}")
        extract_from = corrected
        extract_dir = default_extract_dir(extract_from)
        if extract_dir.exists():
            print(f"[DRY-RUN] skip extract {extract_from} ({kind}): output exists at {extract_dir}")
            if delete_archives:
                print(f"[DRY-RUN] keep {extract_from}: not extracted in this run")
        else:
            print(f"[DRY-RUN] extract {extract_from} ({kind}) -> {extract_dir}")
            if delete_archives:
                print(f"[DRY-RUN] delete {extract_from} after successful extraction")

    print("\nNo files changed. Re-run with --apply to rename and extract.")
    print("Dry-run only shows currently visible archive layers; deeper layers appear after extraction.")
    return 0


def deep_unzip(
    root: Path,
    passwords: list[str],
    apply: bool,
    max_depth: int,
    include_symlinks: bool,
    delete_archives: bool,
    extractor: str | None,
    forced_extensions: dict[str, str] | None,
) -> int:
    if not root.exists():
        raise FileNotFoundError(root)
    if max_depth < 1:
        raise ValueError("--max-depth must be at least 1")
    if not apply:
        return preview_deep_unzip(root, include_symlinks, delete_archives, extractor, forced_extensions)

    processed: set[Path] = set()
    renamed = 0
    extracted = 0
    deleted = 0
    skipped = 0
    failed = 0

    for depth in range(1, max_depth + 1):
        candidates = [
            (path, kind)
            for path, kind in list_archive_candidates(root, include_symlinks, forced_extensions)
            if path.resolve() not in processed
        ]
        if not candidates:
            print("\nNo more supported archives found.")
            break

        print(f"\nPass {depth}: {len(candidates)} supported archive(s)")
        for path, kind in candidates:
            current = path
            corrected = corrected_archive_path(current, kind)
            if corrected != current:
                corrected = unique_destination(corrected)
                print(f"[APPLY] rename {current} -> {corrected}")
                corrected.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(current), str(corrected))
                current = corrected
                renamed += 1

            extract_dir = default_extract_dir(current)
            if extract_dir.exists():
                print(f"[SKIP] extract {current} ({kind}): output exists at {extract_dir}")
                if delete_archives:
                    print(f"[SKIP] keep {current}: archive was not extracted in this run")
                skipped += 1
                processed.add(current.resolve())
                continue

            print(f"[APPLY] extract {current} ({kind}) -> {extract_dir}")
            try:
                extract_archive_to_dir(current, kind, extract_dir, passwords, extractor)
            except ArchivePasswordError as exc:
                print(f"[SKIP] {current}: {exc}")
                failed += 1
            except (ArchiveUnsupportedError, ArchiveUnsafePathError, ArchiveError) as exc:
                print(f"[SKIP] {current}: {exc}")
                failed += 1
            else:
                extracted += 1
                if delete_archives:
                    print(f"[APPLY] delete {current}")
                    try:
                        current.unlink()
                    except OSError as exc:
                        print(f"[SKIP] could not delete {current}: {exc}")
                        failed += 1
                    else:
                        deleted += 1
            finally:
                processed.add(current.resolve())
    else:
        print(f"\nStopped after --max-depth={max_depth}. There may still be nested archives.")

    print("\nSummary:")
    print(f"  Renamed:   {renamed}")
    print(f"  Extracted: {extracted}")
    print(f"  Deleted:   {deleted}")
    print(f"  Skipped:   {skipped}")
    print(f"  Failed:    {failed}")
    return 2 if failed else 0


def plan_clean_empty_dirs(root: Path) -> list[Operation]:
    operations: list[Operation] = []
    if not root.exists():
        return [Operation("skip", root, None, "root missing")]

    planned_empty: set[Path] = set()
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        path = Path(dirpath)
        if path == root:
            continue

        try:
            children = list(path.iterdir())
        except OSError:
            continue

        if any(child not in planned_empty for child in children):
            continue
        planned_empty.add(path)
        operations.append(Operation("remove-empty-dir", path, None, "no children"))
    return operations


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safe NAS file automation helper")
    parser.add_argument("--include-symlinks", action="store_true", help="include symlinked files/directories")

    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="summarize files under a root")
    scan_parser.add_argument("root", help="folder to scan")
    scan_parser.add_argument("--top", type=int, default=20, help="number of largest files to show")

    organize_parser = subparsers.add_parser("organize", help="move files according to JSON rules")
    organize_parser.add_argument("--config", required=True, help="path to JSON rules")
    organize_parser.add_argument("--apply", action="store_true", help="actually move files")

    duplicates_parser = subparsers.add_parser("duplicates", help="report duplicate files by sha256")
    duplicates_parser.add_argument("root", help="folder to scan")
    duplicates_parser.add_argument("--min-size-mb", type=float, default=1.0, help="skip files smaller than this")
    duplicates_parser.add_argument("--export-csv", help="optional CSV output path")

    deep_unzip_parser = subparsers.add_parser(
        "deep-unzip",
        aliases=["deep-extract"],
        help="fix archive suffixes and recursively extract nested ZIP/RAR/7Z/TAR archives",
    )
    deep_unzip_parser.add_argument("root", help="folder to scan")
    deep_unzip_parser.add_argument("--apply", action="store_true", help="actually rename and extract archives")
    deep_unzip_parser.add_argument(
        "--delete-archives",
        action="store_true",
        help="delete each archive after it is successfully extracted in this run",
    )
    deep_unzip_parser.add_argument("--max-depth", type=int, default=20, help="maximum recursive extraction passes")
    deep_unzip_parser.add_argument(
        "--extractor",
        help="path or command name for 7z/7zz/7za, required for RAR and 7Z extraction",
    )
    deep_unzip_parser.add_argument(
        "--force-extension",
        action="append",
        default=[],
        metavar="EXT=TYPE",
        help="treat files with an extension as an archive type, e.g. .mp4=zip; can be repeated",
    )
    deep_unzip_parser.add_argument(
        "--password",
        action="append",
        default=[],
        help="archive password to try; can be repeated",
    )
    deep_unzip_parser.add_argument("--password-file", help="text file with one password per line")
    deep_unzip_parser.add_argument("--ask-password", action="store_true", help="prompt for one password")

    clean_parser = subparsers.add_parser("clean-empty-dirs", help="remove empty folders")
    clean_parser.add_argument("root", help="folder to clean")
    clean_parser.add_argument("--apply", action="store_true", help="actually remove directories")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    try:
        if args.command == "scan":
            return scan(expand_path(args.root), args.top, args.include_symlinks)

        if args.command == "organize":
            config = load_config(expand_path(args.config))
            operations = plan_organize(config, include_symlinks=args.include_symlinks)
            count = apply_operations(operations, apply=args.apply)
            print(f"\nPlanned operations: {count}")
            if not args.apply:
                print("No files changed. Re-run with --apply to execute.")
            return 0

        if args.command == "duplicates":
            export_csv = expand_path(args.export_csv) if args.export_csv else None
            return find_duplicates(
                expand_path(args.root),
                args.min_size_mb,
                export_csv,
                args.include_symlinks,
            )

        if args.command in {"deep-unzip", "deep-extract"}:
            passwords = load_passwords(args.password, args.password_file, args.ask_password)
            forced_extensions = parse_forced_extensions(args.force_extension)
            return deep_unzip(
                expand_path(args.root),
                passwords,
                args.apply,
                args.max_depth,
                args.include_symlinks,
                args.delete_archives,
                args.extractor,
                forced_extensions,
            )

        if args.command == "clean-empty-dirs":
            operations = plan_clean_empty_dirs(expand_path(args.root))
            count = apply_operations(operations, apply=args.apply)
            print(f"\nPlanned operations: {count}")
            if not args.apply:
                print("No directories removed. Re-run with --apply to execute.")
            return 0
    except (ConfigError, OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    return 1


if __name__ == "__main__":
    raise SystemExit(main())

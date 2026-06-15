#!/usr/bin/env python3
"""Safe automation helper for organizing files on a NAS.

The script defaults to dry-run mode. Add --apply to commands that change files.
"""

from __future__ import annotations

import argparse
import codecs
import csv
import fnmatch
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from string import Formatter
from typing import Any, Iterable


DEFAULT_IGNORE_FILES = {".DS_Store", "Thumbs.db", "._*"}
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
DEFAULT_ZIP_NAME_ENCODINGS = ("cp932", "shift_jis")
ZIP_UTF8_FLAG = 0x800
MOJIBAKE_CHARS = frozenset(
    "¡¢£¤¥¦§¨©ª«¬®¯°±²³´µ¶·¸¹º»¼½¾¿"
    "ÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖ×ØÙÚÛÜÝÞß"
    "àáâãäåæçèéêëìíîïðñòóôõö÷øùúûüýþÿ"
    "ÇüéâäàåçêëèïîìÄÅÉæÆôöòûùÿÖÜ"
    "¢£¥₧ƒáíóúñÑªº¿⌐¬½¼¡«»"
    "░▒▓│┤╡╢╖╕╣║╗╝╜╛┐└┴┬├─┼╞╟╚╔╩╦╠═╬"
    "╧╨╤╥╙╘╒╓╫╪┘┌█▄▌▐▀�"
)
NUMBERED_SPLIT_ARCHIVE_RE = re.compile(
    r"^(?P<stem>.+)\.(?P<kind>zip|rar|7z)\.(?P<part>\d{3,})$",
    re.IGNORECASE,
)
RAR_PART_SPLIT_ARCHIVE_RE = re.compile(
    r"^(?P<stem>.+)\.part(?P<part>\d+)\.rar$",
    re.IGNORECASE,
)
RAR_OLD_SPLIT_FOLLOWER_RE = re.compile(r"^(?P<stem>.+)\.r(?P<part>\d{2,3})$", re.IGNORECASE)
ZIP_OLD_SPLIT_FOLLOWER_RE = re.compile(r"^(?P<stem>.+)\.z(?P<part>\d{2,3})$", re.IGNORECASE)
VIDEO_EXTENSIONS = {
    ".3g2",
    ".3gp",
    ".asf",
    ".avi",
    ".divx",
    ".f4v",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ogv",
    ".rm",
    ".rmvb",
    ".ts",
    ".vob",
    ".webm",
    ".wmv",
}


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


@dataclass(frozen=True)
class SplitArchiveInfo:
    kind: str
    family: str
    stem: str
    part_number: int
    part_width: int
    is_first_volume: bool


class ConfigError(ValueError):
    """Raised when the JSON config is invalid."""


class ArchiveError(RuntimeError):
    """Raised when an archive cannot be extracted."""


class ArchivePasswordError(ArchiveError):
    """Raised when an archive needs a password that was not provided."""


class ArchiveUnsupportedError(ArchiveError):
    """Raised when an archive uses features unsupported by available tools."""


class ArchiveFilenameEncodingError(ArchiveError):
    """Raised when extracted filenames look like mojibake."""


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


def split_archive_info(path: Path) -> SplitArchiveInfo | None:
    name = path.name

    numbered = NUMBERED_SPLIT_ARCHIVE_RE.match(name)
    if numbered:
        part_text = numbered.group("part")
        part_number = int(part_text)
        return SplitArchiveInfo(
            kind=numbered.group("kind").lower(),
            family="numbered",
            stem=numbered.group("stem"),
            part_number=part_number,
            part_width=len(part_text),
            is_first_volume=part_number == 1,
        )

    rar_part = RAR_PART_SPLIT_ARCHIVE_RE.match(name)
    if rar_part:
        part_text = rar_part.group("part")
        part_number = int(part_text)
        return SplitArchiveInfo(
            kind="rar",
            family="rar_part",
            stem=rar_part.group("stem"),
            part_number=part_number,
            part_width=len(part_text),
            is_first_volume=part_number == 1,
        )

    rar_old = RAR_OLD_SPLIT_FOLLOWER_RE.match(name)
    if rar_old:
        part_text = rar_old.group("part")
        return SplitArchiveInfo(
            kind="rar",
            family="rar_old",
            stem=rar_old.group("stem"),
            part_number=int(part_text) + 2,
            part_width=len(part_text),
            is_first_volume=False,
        )

    zip_old = ZIP_OLD_SPLIT_FOLLOWER_RE.match(name)
    if zip_old:
        part_text = zip_old.group("part")
        return SplitArchiveInfo(
            kind="zip",
            family="zip_old",
            stem=zip_old.group("stem"),
            part_number=int(part_text),
            part_width=len(part_text),
            is_first_volume=False,
        )

    return None


def same_split_archive_group(left: SplitArchiveInfo, right: SplitArchiveInfo) -> bool:
    if left.family != right.family or left.kind != right.kind:
        return False
    if left.stem.casefold() != right.stem.casefold():
        return False
    if left.family == "numbered" and left.part_width != right.part_width:
        return False
    return True


def split_archive_volumes(path: Path) -> tuple[Path, ...]:
    info = split_archive_info(path)
    if not info or not info.is_first_volume:
        followers = numbered_split_followers_for_base(path)
        if followers:
            return (path, *followers)
        return (path,)

    try:
        siblings = list(path.parent.iterdir())
    except OSError:
        return (path,)

    volumes: list[tuple[int, str, Path]] = []
    for sibling in siblings:
        if not sibling.is_file():
            continue
        sibling_info = split_archive_info(sibling)
        if sibling_info and same_split_archive_group(info, sibling_info):
            volumes.append((sibling_info.part_number, sibling.name.casefold(), sibling))

    volumes.sort()
    return tuple(volume for _part_number, _name, volume in volumes) or (path,)


def numbered_split_followers_for_base(path: Path) -> tuple[Path, ...]:
    suffix = path.suffix.lower()
    if suffix not in {".zip", ".rar", ".7z"}:
        return ()

    stem = path.with_suffix("").name.casefold()
    kind = suffix.lstrip(".")

    try:
        siblings = list(path.parent.iterdir())
    except OSError:
        return ()

    followers: list[tuple[int, str, Path]] = []
    for sibling in siblings:
        if not sibling.is_file():
            continue
        sibling_info = split_archive_info(sibling)
        if (
            sibling_info
            and sibling_info.family == "numbered"
            and sibling_info.kind == kind
            and sibling_info.stem.casefold() == stem
            and sibling_info.part_number > 1
        ):
            followers.append((sibling_info.part_number, sibling.name.casefold(), sibling))

    followers.sort()
    return tuple(follower for _part_number, _name, follower in followers)


def prepare_numbered_split_aliases_for_base(path: Path) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    followers = numbered_split_followers_for_base(path)
    if not followers:
        return path, None

    kind = path.suffix.lower().lstrip(".")
    stem = path.with_suffix("").name
    temp_dir = tempfile.TemporaryDirectory(prefix=f"{path.name}.__split_input__.", dir=str(path.parent))
    alias_dir = Path(temp_dir.name)

    try:
        create_file_alias(path, alias_dir / f"{stem}.{kind}.001")
        for follower in followers:
            follower_info = split_archive_info(follower)
            if not follower_info:
                continue
            part = f"{follower_info.part_number:0{follower_info.part_width}d}"
            create_file_alias(follower, alias_dir / f"{stem}.{kind}.{part}")
    except OSError:
        temp_dir.cleanup()
        raise

    return alias_dir / f"{stem}.{kind}.001", temp_dir


def create_file_alias(source: Path, target: Path) -> None:
    try:
        target.symlink_to(source)
    except OSError:
        os.link(source, target)


def old_style_split_companions(path: Path) -> tuple[Path, ...]:
    suffix = path.suffix.lower()
    if suffix not in {".rar", ".zip"}:
        return ()

    stem = path.with_suffix("").name.casefold()
    pattern = RAR_OLD_SPLIT_FOLLOWER_RE if suffix == ".rar" else ZIP_OLD_SPLIT_FOLLOWER_RE

    try:
        siblings = list(path.parent.iterdir())
    except OSError:
        return ()

    companions: list[tuple[int, str, Path]] = []
    for sibling in siblings:
        if not sibling.is_file():
            continue
        match = pattern.match(sibling.name)
        if match and match.group("stem").casefold() == stem:
            companions.append((int(match.group("part")), sibling.name.casefold(), sibling))

    companions.sort()
    return tuple(companion for _part_number, _name, companion in companions)


def archive_delete_targets(path: Path) -> tuple[Path, ...]:
    targets = list(split_archive_volumes(path))
    targets.extend(old_style_split_companions(path))

    unique: list[Path] = []
    seen: set[Path] = set()
    for target in targets:
        resolved = target.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(target)
    return tuple(unique)


def archive_uses_split_volumes(path: Path) -> bool:
    return (
        split_archive_info(path) is not None
        or bool(numbered_split_followers_for_base(path))
        or bool(old_style_split_companions(path))
    )


def first_split_volume_path(path: Path) -> Path | None:
    info = split_archive_info(path)
    if not info or info.is_first_volume:
        return None

    if info.family == "numbered":
        first_part = f"{1:0{info.part_width}d}"
        standard_first = path.with_name(f"{info.stem}.{info.kind}.{first_part}")
        if standard_first.exists():
            return standard_first
        base_first = path.with_name(f"{info.stem}.{info.kind}")
        if base_first.exists():
            return base_first
        return standard_first
    if info.family == "rar_part":
        first_part = f"{1:0{info.part_width}d}"
        return path.with_name(f"{info.stem}.part{first_part}.rar")
    if info.family == "rar_old":
        return path.with_name(f"{info.stem}.rar")
    if info.family == "zip_old":
        return path.with_name(f"{info.stem}.zip")
    return None


def archive_type(path: Path, forced_extensions: dict[str, str] | None = None) -> str | None:
    split_info = split_archive_info(path)
    if split_info:
        return split_info.kind if split_info.is_first_volume else None

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
    suffix_kind = archive_type_from_suffix(path)
    if suffix_kind:
        return suffix_kind
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


def archive_type_from_suffix(path: Path) -> str | None:
    lower_name = path.name.lower()
    ordered_aliases = sorted(
        ARCHIVE_SUFFIX_ALIASES.items(),
        key=lambda item: max(len(suffix) for suffix in item[1]),
        reverse=True,
    )
    for kind, suffixes in ordered_aliases:
        if any(lower_name.endswith(suffix) for suffix in suffixes):
            return kind
    return None


def corrected_archive_path(path: Path, kind: str) -> Path:
    split_info = split_archive_info(path)
    if split_info and split_info.is_first_volume:
        return path
    suffix = ARCHIVE_SUFFIXES[kind]
    if has_archive_suffix(path, kind):
        return path
    if path.suffix:
        return path.with_suffix(suffix)
    return path.with_name(path.name + suffix)


def default_extract_dir(path: Path) -> Path:
    split_info = split_archive_info(path)
    if split_info:
        return path.with_name(split_info.stem)

    lower_name = path.name.lower()
    for suffixes in ARCHIVE_SUFFIX_ALIASES.values():
        for suffix in sorted(suffixes, key=len, reverse=True):
            if lower_name.endswith(suffix):
                return path.with_name(path.name[: -len(suffix)])
    return path.with_suffix("")


def has_archive_suffix(path: Path, kind: str) -> bool:
    split_info = split_archive_info(path)
    if split_info and split_info.is_first_volume:
        return split_info.kind == kind
    lower_name = path.name.lower()
    return any(lower_name.endswith(suffix) for suffix in ARCHIVE_SUFFIX_ALIASES[kind])


def is_tar_archive(path: Path) -> bool:
    try:
        return path.is_file() and tarfile.is_tarfile(path)
    except (OSError, tarfile.TarError):
        return False


def open_zip_file(zip_path: Path, metadata_encoding: str | None = None) -> zipfile.ZipFile:
    if metadata_encoding:
        return zipfile.ZipFile(zip_path, metadata_encoding=metadata_encoding)
    return zipfile.ZipFile(zip_path)


def select_zip_name_encoding(zip_path: Path, requested_encodings: list[str]) -> str | None:
    if requested_encodings:
        for encoding in requested_encodings:
            try:
                with open_zip_file(zip_path, encoding) as archive:
                    archive.infolist()
                return encoding
            except (LookupError, UnicodeDecodeError, zipfile.BadZipFile):
                continue
        raise ArchiveError(f"none of the requested ZIP name encodings worked: {', '.join(requested_encodings)}")

    try:
        with open_zip_file(zip_path) as archive:
            default_infos = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"not a valid ZIP archive: {exc}") from exc

    if not any(not info.flag_bits & ZIP_UTF8_FLAG for info in default_infos):
        return None

    default_names = [info.filename for info in default_infos]
    default_score = zip_name_mojibake_score(default_names)
    best_encoding: str | None = None
    best_score = default_score
    best_names: list[str] = default_names

    for encoding in DEFAULT_ZIP_NAME_ENCODINGS:
        try:
            with open_zip_file(zip_path, encoding) as archive:
                names = [info.filename for info in archive.infolist()]
        except (LookupError, UnicodeDecodeError, zipfile.BadZipFile):
            continue

        score = zip_name_mojibake_score(names)
        if score < best_score:
            best_encoding = encoding
            best_score = score
            best_names = names

    if best_encoding and default_score - best_score >= 6 and zip_names_have_japanese_text(best_names):
        return best_encoding
    return None


def zip_name_mojibake_score(names: Iterable[str]) -> float:
    score = 0.0
    for name in names:
        for char in name:
            code = ord(char)
            if char in MOJIBAKE_CHARS:
                score += 4.0
            elif code < 32:
                score += 10.0
            elif 0x0080 <= code <= 0x00FF:
                score += 2.0
            elif 0x2500 <= code <= 0x259F:
                score += 5.0

            if is_japanese_name_char(char):
                score -= 2.0
            elif is_cjk_unified_char(char):
                score -= 0.25
    return score


def zip_names_have_japanese_text(names: Iterable[str]) -> bool:
    return any(is_japanese_name_char(char) for name in names for char in name)


def extracted_tree_names_look_mojibake(root: Path) -> bool:
    names: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        names.extend(dirnames)
        names.extend(filenames)
        if len(names) >= 2000:
            break

    if not names:
        return False

    score = zip_name_mojibake_score(names)
    return score >= max(8.0, len(names) * 1.5)


def is_japanese_name_char(char: str) -> bool:
    code = ord(char)
    return (
        0x3040 <= code <= 0x30FF
        or 0x31F0 <= code <= 0x31FF
        or 0xFF66 <= code <= 0xFF9F
    )


def is_cjk_unified_char(char: str) -> bool:
    code = ord(char)
    return 0x4E00 <= code <= 0x9FFF


def safe_zip_member_path(info: zipfile.ZipInfo) -> Path:
    raw_name = info.filename.replace("\\", "/")
    member = PurePosixPath(raw_name)
    if raw_name.startswith("/") or member.is_absolute() or ".." in member.parts:
        raise ArchiveUnsafePathError(f"unsafe member path {info.filename!r}")
    return Path(*member.parts)


def extract_zip_to_dir(
    zip_path: Path,
    extract_dir: Path,
    passwords: list[str],
    zip_name_encodings: list[str],
) -> None:
    metadata_encoding = select_zip_name_encoding(zip_path, zip_name_encodings)
    if metadata_encoding:
        print(f"[INFO] ZIP filename encoding: {metadata_encoding} for {zip_path}")

    try:
        with open_zip_file(zip_path, metadata_encoding) as archive:
            infos = archive.infolist()
            for info in infos:
                safe_zip_member_path(info)
            encrypted = any(info.flag_bits & 0x1 for info in infos)
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"not a valid ZIP archive: {exc}") from exc

    if encrypted and not passwords:
        raise ArchivePasswordError("password required")

    candidates: list[str | None] = passwords if encrypted else [None]
    password_errors: list[str] = []

    for password in candidates:
        staging = unique_destination(extract_dir.with_name(extract_dir.name + ".__extracting__"))
        staging.mkdir(parents=True, exist_ok=False)
        try:
            with open_zip_file(zip_path, metadata_encoding) as archive:
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
    validate_filenames: bool = False,
) -> None:
    executable = find_external_extractor(extractor)
    if not executable:
        raise ArchiveUnsupportedError("7z extraction requires 7zz, 7z, or 7za on PATH")

    candidates: list[str | None] = passwords if passwords else [None]
    errors: list[str] = []

    try:
        archive_for_extractor, split_aliases = prepare_numbered_split_aliases_for_base(archive_path)
    except OSError as exc:
        raise ArchiveError(f"could not prepare split archive aliases: {exc}") from exc

    try:
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
            command.append(str(archive_for_extractor))

            try:
                result = subprocess.run(
                    command,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=None,
                )
                if result.returncode == 0:
                    if validate_filenames and extracted_tree_names_look_mojibake(staging):
                        raise ArchiveFilenameEncodingError(
                            "7z extracted filenames look garbled; retrying Python ZIP filename decoding"
                        )
                    staging.rename(extract_dir)
                    return
                errors.append(summarize_extractor_error(result.stderr or result.stdout))
            except OSError as exc:
                errors.append(str(exc))
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
    finally:
        if split_aliases:
            split_aliases.cleanup()

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
    prefer_7z_for_zip: bool,
    zip_name_encodings: list[str],
) -> None:
    if archive_kind.startswith("tar"):
        extract_tar_to_dir(archive_path, extract_dir)
        return

    split_archive = archive_uses_split_volumes(archive_path)
    if archive_kind == "zip" and split_archive and not find_external_extractor(extractor):
        raise ArchiveUnsupportedError("split ZIP extraction requires 7zz, 7z, or 7za on PATH")

    detected_zip_name_encoding: str | None = None
    if archive_kind == "zip" and not split_archive and not zip_name_encodings:
        try:
            detected_zip_name_encoding = select_zip_name_encoding(archive_path, [])
        except ArchiveError:
            detected_zip_name_encoding = None

    python_zip_tried = False
    python_zip_error: ArchiveError | None = None
    if archive_kind == "zip" and zip_name_encodings and not split_archive:
        python_zip_tried = True
        try:
            extract_zip_to_dir(archive_path, extract_dir, passwords, zip_name_encodings)
            return
        except ArchiveError as exc:
            python_zip_error = exc

    if (
        archive_kind == "zip"
        and (split_archive or prefer_7z_for_zip or python_zip_error or detected_zip_name_encoding)
        and find_external_extractor(extractor)
    ):
        try:
            if detected_zip_name_encoding and not prefer_7z_for_zip:
                print(
                    f"[INFO] ZIP filename encoding looks like {detected_zip_name_encoding}; "
                    "trying 7z first for speed"
                )
            extract_with_7z(
                archive_path,
                extract_dir,
                passwords,
                extractor,
                validate_filenames=detected_zip_name_encoding is not None,
            )
            return
        except ArchiveFilenameEncodingError as exc:
            print(f"[INFO] {exc}")
            seven_zip_error = exc
        except ArchiveError as exc:
            seven_zip_error = exc
    else:
        seven_zip_error = None

    if archive_kind == "zip" and split_archive:
        raise seven_zip_error or ArchiveUnsupportedError("split ZIP extraction requires 7zz, 7z, or 7za on PATH")

    if archive_kind == "zip":
        if python_zip_tried:
            if seven_zip_error is not None and python_zip_error is not None:
                raise ArchiveError(f"Python zip failed: {python_zip_error}; 7z failed: {seven_zip_error}") from seven_zip_error
            if python_zip_error is not None:
                raise python_zip_error
        try:
            extract_zip_to_dir(archive_path, extract_dir, passwords, zip_name_encodings)
            return
        except ArchiveUnsupportedError:
            if seven_zip_error is not None:
                raise ArchiveError(f"7z failed: {seven_zip_error}; Python zip unsupported") from seven_zip_error
            pass
        except ArchiveError as exc:
            if seven_zip_error is not None:
                raise ArchiveError(f"7z failed: {seven_zip_error}; Python zip failed: {exc}") from exc
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


def parse_zip_name_encodings(values: list[str]) -> list[str]:
    encodings: list[str] = []
    for value in values:
        encoding = value.strip()
        if not encoding:
            raise ValueError("ZIP name encoding cannot be empty")
        try:
            codecs.lookup(encoding)
        except LookupError as exc:
            raise ValueError(f"Unknown ZIP name encoding {encoding!r}") from exc
        encodings.append(encoding)
    return dedupe_preserve_order(encodings)


def list_archive_candidates(
    root: Path,
    include_symlinks: bool,
    forced_extensions: dict[str, str] | None = None,
) -> list[tuple[Path, str]]:
    candidates: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for path in iter_files(root, recursive=True, include_symlinks=include_symlinks):
        candidate_path = path
        kind = archive_type(path, forced_extensions)
        if not kind:
            first_volume = first_split_volume_path(path)
            if first_volume and first_volume.exists():
                candidate_path = first_volume
                kind = archive_type(first_volume, forced_extensions)
        if kind:
            resolved = candidate_path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            candidates.append((candidate_path, kind))
    return candidates


def missing_split_first_volumes(root: Path, include_symlinks: bool) -> list[tuple[Path, Path]]:
    missing: list[tuple[Path, Path]] = []
    seen_expected: set[Path] = set()
    for path in iter_files(root, recursive=True, include_symlinks=include_symlinks):
        first_volume = first_split_volume_path(path)
        if not first_volume or first_volume.exists():
            continue
        resolved = first_volume.resolve()
        if resolved in seen_expected:
            continue
        seen_expected.add(resolved)
        missing.append((path, first_volume))
    return missing


def print_missing_split_first_volume_warnings(root: Path, include_symlinks: bool) -> int:
    missing = missing_split_first_volumes(root, include_symlinks)
    if not missing:
        return 0

    print("\nWarning: found split archive follower volumes without their first volume.")
    print("Split archives must be extracted from the first volume, for example .7z.001 or .7z.")
    for follower, expected in missing[:20]:
        print(f"  follower: {follower}")
        print(f"  missing:  {expected}")
    if len(missing) > 20:
        print(f"  ... {len(missing) - 20} more missing first volume(s)")
    return len(missing)


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
    force_extensions_first_pass_only: bool,
    zip_name_encodings: list[str],
    prefer_7z_for_zip: bool,
) -> int:
    candidates = list_archive_candidates(root, include_symlinks, forced_extensions)
    if not candidates:
        print(f"No supported archives found under {root}")
        print_missing_split_first_volume_warnings(root, include_symlinks)
        return 0

    external_extractor = find_external_extractor(extractor)
    if any(kind in {"rar", "7z"} for _path, kind in candidates) and not external_extractor:
        print("Warning: RAR/7Z extraction requires 7zz, 7z, or 7za on PATH.")
    if prefer_7z_for_zip:
        if external_extractor:
            print(f"ZIP archives will be extracted with: {external_extractor}")
        else:
            print("Warning: --prefer-7z was set, but 7zz/7z/7za was not found. ZIP extraction will use Python.")
    if forced_extensions and force_extensions_first_pass_only:
        print("Forced extensions will only be used during the first extraction pass.")
    if zip_name_encodings:
        print(f"ZIP filename encodings will be tried first: {', '.join(zip_name_encodings)}")
    else:
        print(f"Japanese ZIP filename auto-detection is enabled: {', '.join(DEFAULT_ZIP_NAME_ENCODINGS)}")
    print_missing_split_first_volume_warnings(root, include_symlinks)

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
                delete_targets = archive_delete_targets(extract_from)
                if len(delete_targets) > 1:
                    print(f"[DRY-RUN] keep split archive group: not extracted in this run")
                else:
                    print(f"[DRY-RUN] keep {extract_from}: not extracted in this run")
        else:
            print(f"[DRY-RUN] extract {extract_from} ({kind}) -> {extract_dir}")
            if delete_archives:
                delete_targets = archive_delete_targets(extract_from)
                if len(delete_targets) == 1:
                    print(f"[DRY-RUN] delete {extract_from} after successful extraction")
                else:
                    print(f"[DRY-RUN] delete split archive group after successful extraction:")
                    for target in delete_targets:
                        print(f"  {target}")

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
    force_extensions_first_pass_only: bool,
    zip_name_encodings: list[str],
    prefer_7z_for_zip: bool,
) -> int:
    if not root.exists():
        raise FileNotFoundError(root)
    if max_depth < 1:
        raise ValueError("--max-depth must be at least 1")
    if not apply:
        return preview_deep_unzip(
            root,
            include_symlinks,
            delete_archives,
            extractor,
            forced_extensions,
            force_extensions_first_pass_only,
            zip_name_encodings,
            prefer_7z_for_zip,
        )

    processed: set[Path] = set()
    renamed = 0
    extracted = 0
    deleted = 0
    skipped = 0
    failed = 0

    for depth in range(1, max_depth + 1):
        active_forced_extensions = (
            forced_extensions
            if forced_extensions and (depth == 1 or not force_extensions_first_pass_only)
            else None
        )
        candidates = [
            (path, kind)
            for path, kind in list_archive_candidates(root, include_symlinks, active_forced_extensions)
            if path.resolve() not in processed
        ]
        if not candidates:
            print("\nNo more supported archives found.")
            print_missing_split_first_volume_warnings(root, include_symlinks)
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
                    delete_targets = archive_delete_targets(current)
                    if len(delete_targets) > 1:
                        print(f"[SKIP] keep split archive group: archive was not extracted in this run")
                    else:
                        print(f"[SKIP] keep {current}: archive was not extracted in this run")
                skipped += 1
                processed.add(current.resolve())
                continue

            print(f"[APPLY] extract {current} ({kind}) -> {extract_dir}")
            try:
                extract_archive_to_dir(
                    current,
                    kind,
                    extract_dir,
                    passwords,
                    extractor,
                    prefer_7z_for_zip,
                    zip_name_encodings,
                )
            except ArchivePasswordError as exc:
                print(f"[SKIP] {current}: {exc}")
                failed += 1
            except (ArchiveUnsupportedError, ArchiveUnsafePathError, ArchiveError) as exc:
                print(f"[SKIP] {current}: {exc}")
                failed += 1
            else:
                extracted += 1
                if delete_archives:
                    for target in archive_delete_targets(current):
                        print(f"[APPLY] delete {target}")
                        try:
                            target.unlink()
                        except OSError as exc:
                            print(f"[SKIP] could not delete {target}: {exc}")
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


def is_video_file(path: Path, video_extensions: set[str] = VIDEO_EXTENSIONS) -> bool:
    return path.suffix.lower() in video_extensions


def plan_flatten_videos(
    root: Path,
    target_dir: Path | None = None,
    rename_from_parent: bool = False,
    include_symlinks: bool = False,
) -> tuple[list[Operation], list[Operation]]:
    if not root.exists():
        return [Operation("skip", root, None, "root missing")], []
    if not root.is_dir():
        raise ValueError(f"{root} is not a directory")

    root = root.resolve()
    target_dir = (target_dir or root).resolve()
    move_operations: list[Operation] = []
    reserved_targets: set[Path] = set()
    skip_target_tree = target_dir != root and path_is_relative_to(target_dir, root)

    for path in iter_files(root, recursive=True, include_symlinks=include_symlinks):
        if not is_video_file(path):
            continue
        if skip_target_tree and path_is_relative_to(path.resolve(), target_dir):
            continue
        if path.parent.resolve() == target_dir:
            continue

        target_name = flatten_video_target_name(path, rename_from_parent)
        target = unique_destination_with_reserved(target_dir / target_name, reserved_targets)
        reserved_targets.add(target.resolve())
        move_operations.append(Operation("move", path, target, "flatten video"))

    empty_dir_operations = plan_empty_dirs_after_moves(root, move_operations, protected_dirs=(target_dir,))
    return move_operations, empty_dir_operations


def flatten_video_target_name(path: Path, rename_from_parent: bool) -> str:
    if not rename_from_parent:
        return path.name
    parent_name = path.parent.name.strip() or path.stem
    return f"{parent_name}{path.suffix}"


def path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def unique_destination_with_reserved(target: Path, reserved_targets: set[Path]) -> Path:
    candidate = target
    if not candidate.exists() and candidate.resolve() not in reserved_targets:
        return candidate

    stem = target.stem
    suffix = target.suffix
    parent = target.parent
    for index in range(1, 10_000):
        candidate = parent / f"{stem} ({index}){suffix}"
        if not candidate.exists() and candidate.resolve() not in reserved_targets:
            return candidate
    raise RuntimeError(f"Could not find a free destination for {target}")


def plan_empty_dirs_after_moves(
    root: Path,
    move_operations: list[Operation],
    protected_dirs: Iterable[Path] = (),
) -> list[Operation]:
    moved_sources = {op.source.resolve() for op in move_operations if op.source}
    protected = {root.resolve(), *(path.resolve() for path in protected_dirs)}
    planned_empty: set[Path] = set()
    operations: list[Operation] = []

    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        path = Path(dirpath)
        if path.resolve() in protected:
            continue

        try:
            children = list(path.iterdir())
        except OSError:
            continue

        has_remaining_child = False
        for child in children:
            resolved_child = child.resolve()
            if resolved_child in moved_sources:
                continue
            if child.is_dir() and resolved_child in planned_empty:
                continue
            has_remaining_child = True
            break

        if has_remaining_child:
            continue
        planned_empty.add(path.resolve())
        operations.append(Operation("remove-empty-dir", path, None, "empty after moving videos"))

    return operations


def flatten_videos(
    root: Path,
    target_dir: Path | None,
    rename_from_parent: bool,
    apply: bool,
    include_symlinks: bool,
) -> int:
    move_operations, empty_dir_operations = plan_flatten_videos(
        root,
        target_dir=target_dir,
        rename_from_parent=rename_from_parent,
        include_symlinks=include_symlinks,
    )
    move_count = apply_operations(move_operations, apply=apply)
    empty_count = apply_operations(empty_dir_operations, apply=apply)

    print("\nSummary:")
    print(f"  Videos moved:       {move_count}")
    print(f"  Empty dirs removed: {empty_count}")
    if not apply:
        print("No files changed. Re-run with --apply to execute.")
    return 0


def resolve_flatten_target_dir(root: Path, value: str | None) -> Path | None:
    if not value:
        return None
    target = Path(value).expanduser()
    if target.is_absolute():
        return target.resolve()
    return (root / target).resolve()


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

    flatten_videos_parser = subparsers.add_parser(
        "flatten-videos",
        help="move videos from subfolders to the root and remove emptied folders",
    )
    flatten_videos_parser.add_argument("root", help="folder whose top level should receive videos")
    flatten_videos_parser.add_argument("--apply", action="store_true", help="actually move videos and remove folders")
    flatten_videos_parser.add_argument(
        "--target-dir",
        help="folder that should receive videos; relative paths are resolved under root",
    )
    flatten_videos_parser.add_argument(
        "--rename-from-parent",
        action="store_true",
        help="rename moved videos to the original video's deepest parent folder name",
    )

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
        "--prefer-7z",
        action="store_true",
        help="extract ZIP archives with 7z/7zz/7za when available for better compatibility",
    )
    deep_unzip_parser.add_argument(
        "--force-extension",
        action="append",
        default=[],
        metavar="EXT=TYPE",
        help="treat files with an extension as an archive type, e.g. .mp4=zip; can be repeated",
    )
    deep_unzip_parser.add_argument(
        "--force-extension-first-pass-only",
        action="store_true",
        help="use --force-extension rules only during the first extraction pass",
    )
    deep_unzip_parser.add_argument(
        "--zip-name-encoding",
        action="append",
        default=[],
        metavar="ENCODING",
        help="encoding for non-UTF-8 ZIP filenames, e.g. cp932 or shift_jis; can be repeated",
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

        if args.command == "flatten-videos":
            root = expand_path(args.root)
            return flatten_videos(
                root,
                resolve_flatten_target_dir(root, args.target_dir),
                args.rename_from_parent,
                args.apply,
                args.include_symlinks,
            )

        if args.command in {"deep-unzip", "deep-extract"}:
            passwords = load_passwords(args.password, args.password_file, args.ask_password)
            forced_extensions = parse_forced_extensions(args.force_extension)
            zip_name_encodings = parse_zip_name_encodings(args.zip_name_encoding)
            return deep_unzip(
                expand_path(args.root),
                passwords,
                args.apply,
                args.max_depth,
                args.include_symlinks,
                args.delete_archives,
                args.extractor,
                forced_extensions,
                args.force_extension_first_pass_only,
                zip_name_encodings,
                args.prefer_7z,
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

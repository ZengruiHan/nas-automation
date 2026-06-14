# NAS File Manager

A conservative Python automation script for managing files on a NAS. It uses
only the Python standard library and defaults to dry-run mode.

## Quick Start

Scan a folder:

```bash
python3 nas_file_manager.py scan /Volumes/NAS --top 20
```

Find duplicates without deleting anything:

```bash
python3 nas_file_manager.py duplicates /Volumes/NAS --min-size-mb 5 --export-csv duplicates.csv
```

Preview recursive archive extraction:

```bash
python3 nas_file_manager.py deep-extract /Volumes/NAS
```

Fix missing or wrong archive suffixes and recursively extract nested ZIP, RAR,
7Z, and TAR files:

```bash
python3 nas_file_manager.py deep-extract /Volumes/NAS --apply
```

Keep only the final extracted contents by deleting each archive after successful
extraction:

```bash
python3 nas_file_manager.py deep-extract /Volumes/NAS --apply --delete-archives
```

Try passwords while extracting encrypted archives:

```bash
python3 nas_file_manager.py deep-extract /Volumes/NAS --apply --delete-archives --password-file passwords.txt
```

Preview moving videos from nested folders to the top level:

```bash
python3 nas_file_manager.py flatten-videos /Volumes/NAS
```

Move videos to the top level and remove emptied folders:

```bash
python3 nas_file_manager.py flatten-videos /Volumes/NAS --apply
```

Preview rule-based organization:

```bash
python3 nas_file_manager.py organize --config nas_rules.example.json
```

Apply the planned moves:

```bash
python3 nas_file_manager.py organize --config nas_rules.example.json --apply
```

Preview empty folder cleanup:

```bash
python3 nas_file_manager.py clean-empty-dirs /Volumes/NAS
```

Actually remove empty folders:

```bash
python3 nas_file_manager.py clean-empty-dirs /Volumes/NAS --apply
```

## Rule Format

Edit `nas_rules.example.json` or copy it to your own config file. A rule has:

- `source`: a root key or path to scan.
- `target`: a root key/path plus optional templates like `{year}`, `{month}`,
  `{day}`, `{ym}`, `{ext}`, `{stem}`, `{suffix}`, and `{name}`.
- `extensions`: file extensions to match. Use `["*"]` for all files.
- `older_than_days` / `newer_than_days`: optional age filters based on modified time.
- `min_size_mb` / `max_size_mb`: optional file size filters.
- `rename_template`: defaults to `{name}`.
- `recursive`: defaults to `true`.

The script never deletes duplicate files. The `duplicates` command reports them
and can export a CSV so you can review before deciding what to remove.

## Flatten Videos

The `flatten-videos` command scans a root folder recursively and moves common
video files from nested folders into the root folder itself. It then removes
folders that became empty after the move.

Common video extensions include `.mp4`, `.mkv`, `.mov`, `.avi`, `.wmv`, `.flv`,
`.webm`, `.m4v`, `.mpg`, `.mpeg`, `.3gp`, `.ts`, `.mts`, `.m2ts`, `.vob`,
`.ogv`, `.rm`, `.rmvb`, `.asf`, `.divx`, and `.f4v`.

If a file with the same name already exists in the root, the moved file gets a
numbered suffix such as `movie (1).mp4`.

Examples:

```bash
python3 nas_file_manager.py flatten-videos /Volumes/NAS
python3 nas_file_manager.py flatten-videos /Volumes/NAS --apply
```

## Deep Archive Extraction

The `deep-extract` command scans every file under a root and identifies ZIP,
RAR, 7Z, TAR, TAR.GZ/TGZ, TAR.BZ2/TBZ2, and TAR.XZ/TXZ archives by content, not
by filename. This catches archives with no extension or with the wrong
extension. `deep-unzip` remains available as a backward-compatible alias.
It also recognizes split archive first volumes such as `.7z.001`, `.zip.001`,
`.rar.001`, `.part1.rar`, and `.part01.rar`.

For each supported archive, it will:

- Rename the file to `.zip`, `.rar`, `.7z`, `.tar`, `.tar.gz`, `.tar.bz2`, or
  `.tar.xz` when the suffix is missing or wrong.
- Keep split archive names unchanged, extract only the first volume, and skip
  follower volumes such as `.002`, `.003`, `.r00`, and `.z01`.
- Extract it into a sibling folder with the same base name.
- Scan the extracted contents again, repeating until no more supported archives
  are found or `--max-depth` is reached.
- Keep the original archives in place unless `--delete-archives` is set.
- With `--delete-archives`, delete each archive only after it has been
  successfully extracted in the current run. Split archive groups are deleted
  together after the first volume extracts successfully.
- Skip extraction if the destination folder already exists, which makes repeat
  runs safer.

Examples:

```bash
python3 nas_file_manager.py deep-extract /Volumes/NAS
python3 nas_file_manager.py deep-extract /Volumes/NAS --apply --max-depth 50
python3 nas_file_manager.py deep-extract /Volumes/NAS --apply --delete-archives
python3 nas_file_manager.py deep-extract /Volumes/NAS --apply --password "secret"
python3 nas_file_manager.py deep-extract /Volumes/NAS --apply --password-file passwords.txt
python3 nas_file_manager.py deep-extract /Volumes/NAS --apply --ask-password
```

Some ZIP files use features that Python's built-in ZIP support cannot handle
well. If `7z`, `7zz`, or `7za` is available, use it for ZIP files too:

```bash
python3 nas_file_manager.py deep-extract /Volumes/NAS --apply --delete-archives --prefer-7z --password-file passwords.txt
```

If files are deliberately mislabeled with a non-archive extension, force that
extension to be treated as an archive type:

```bash
python3 nas_file_manager.py deep-extract /Volumes/NAS --apply --delete-archives --force-extension .mp4=zip
```

If only the already-visible files should be treated this way, and extracted
contents should go back to normal archive detection, limit forced extensions to
the first extraction pass:

```bash
python3 nas_file_manager.py deep-extract /Volumes/NAS --apply --delete-archives --force-extension .mp4=zip --force-extension-first-pass-only
```

Use forced extensions only when you are sure those files are disguised
archives. A real video file forced as ZIP will fail to extract.

Password files use one password per line. Blank lines and lines starting with
`#` are ignored. ZIP and TAR files can be extracted with Python's standard
library. RAR and 7Z files require `7zz`, `7z`, or `7za` on `PATH`, or a path
supplied with `--extractor`. Use `--prefer-7z` when ZIP files need 7-Zip-style
compatibility.

Example with an explicit extractor:

```bash
python3 nas_file_manager.py deep-extract /Volumes/NAS --apply --delete-archives --extractor /usr/bin/7z
```

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
and 7Z files:

```bash
python3 nas_file_manager.py deep-extract /Volumes/NAS --apply
```

Keep only the final extracted contents by deleting each archive after successful
extraction:

```bash
python3 nas_file_manager.py deep-extract /Volumes/NAS --apply --delete-archives
```

Try passwords while extracting encrypted ZIP files:

```bash
python3 nas_file_manager.py deep-extract /Volumes/NAS --apply --delete-archives --password-file passwords.txt
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

## Deep Archive Extraction

The `deep-extract` command scans every file under a root and identifies ZIP,
RAR, and 7Z archives by content, not by filename. This catches archives with no
extension or with the wrong extension. `deep-unzip` remains available as a
backward-compatible alias.

For each supported archive, it will:

- Rename the file to `.zip`, `.rar`, or `.7z` when the suffix is missing or wrong.
- Extract it into a sibling folder with the same base name.
- Scan the extracted contents again, repeating until no more supported archives
  are found or `--max-depth` is reached.
- Keep the original archives in place unless `--delete-archives` is set.
- With `--delete-archives`, delete each archive only after it has been
  successfully extracted in the current run.
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

Password files use one password per line. Blank lines and lines starting with
`#` are ignored. ZIP files can be extracted with Python's standard library. RAR
and 7Z files require `7zz`, `7z`, or `7za` on `PATH`, or a path supplied with
`--extractor`.

Example with an explicit extractor:

```bash
python3 nas_file_manager.py deep-extract /Volumes/NAS --apply --delete-archives --extractor /usr/bin/7z
```

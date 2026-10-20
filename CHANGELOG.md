# Changelog

Notable changes to HardlyReading. Versions are tagged on GitHub with matching
release notes.

## [1.4.0] — 2026-10-20

### Fixed

- **A Download button on files you already had.** When every item in a job was
  already on disk (checksum match), the job still sat in *Ready* with a
  *Download* button that would fetch nothing. It now finishes immediately as
  *Done* with "already have all N item(s)". The card also hides Download when
  there is nothing left to fetch, which covers jobs left in that state by older
  versions.

### Added

- **Reading progress in the Library** — a progress bar plus `page/total · percent`
  once you open a file. At the end the row dims, the bar turns green and it is
  badged *finished*.
- **Clear reading history** in Settings, which forgets every saved page position
  and touches no files.

## [1.3.0] — 2026-10-20

### Fixed

- **Downloads disappearing from the Library.** The Library was built only from
  queue state, so removing a job — or losing `state.json` — hid files that were
  still on disk. The Library now merges finished jobs with a scan of the
  download folder. Folder-only files get a synthetic `folder:<series>` id, so
  they open in the reader, report page counts, keep reading position and can be
  deleted like any other.

### Changed

- **Add all results to queue** asks for confirmation first. One click could
  queue a thousand items.

## [1.2.0] — 2026-10-20

### Changed

- **Search results are ticked, not added on click.** Clicking a card selects it;
  a bar appears with the count and an *Add to queue* button. Already-queued
  items are skipped rather than erroring.
- **Library deleting is explicit.** The permanent ✕ on every row is gone.
  *Select* turns on tick boxes and a *Delete* action that confirms the filenames.

### Added

- **Queue-state badges on every search card**, one symbol per state: in library,
  queued, ready, resuming, downloading, resolving, verifying, failed, cancelled,
  unavailable.
- **Per-card actions** — Remove from queue for waiting work, Open in library for
  finished work. Deleting a file stays in the Library.
- `GET /api/covered`, so the grid knows an item's state before you click.

## [1.1.0] — 2026-10-20

### Added

- **Search spinner** — the status line and the results area show progress while
  the request is in flight, instead of looking frozen.
- Empty queue and library states carry a next-step hint.
- *Add to queue* stays disabled until there is something to add.

### Changed

- Slightly brighter secondary text for readability.
- README explains the four files, the resolve → plan → download → verify →
  library pipeline, and every setting with its default.
- Docstrings along the orchestration paths.

## [1.0.0] — 2026-10-20

First release.

- Paste archive.org collection, item or search links; collections pull every member
- In-app search scoped to documents
- One artifact per issue: CBR → CBZ → image-PDF → text-PDF, preferring originals
- Byte size and MD5 verification against archive.org
- Resume after a crash; finished files skipped
- Retry ladder: 15m → 1h → 3h → 8h, with genuinely missing items marked unavailable
- Komga-ready output: one folder per series, `<Series> NNN (Year).ext`, ordered by
  printed issue number
- Checksum-based skipping of files already in the destination
- Library with server-side reading progress in SQLite
- Built-in CBZ / CBR / PDF reader; CBR needs `unrar`, `bsdtar` or `7z`
- Cross-platform launcher that finds `aria2c` and opens the browser

[1.4.0]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v1.4.0
[1.3.0]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v1.3.0
[1.2.0]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v1.2.0
[1.1.0]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v1.1.0
[1.0.0]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v1.0.0

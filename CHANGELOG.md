# Changelog

Notable changes to HardlyReading. Versions are tagged on GitHub with matching
release notes.

## [1.9.0] — 2026-10-20

### Added

- **Paging on every tab** — 10 / 25 / 50 / 100 per page, prev/next and a
  jump-to-page list, with "showing 11–20 of 63". Queue rows keep their real
  position number across pages, so "move to top" still means what it says.
  Sizes are remembered per tab.
- **The library lists work that is not on disk yet.** A queued, planned or failed
  item appears with its state, a **Download** (or **Retry**) button, and a link to
  the item page on archive.org. Files mid-download are marked partial and cannot
  be opened.
- **Search descriptions** from archive.org, tags stripped and entities decoded.
- **Support the Internet Archive** buttons under the paste box and under the
  search results.

### Fixed

- **An empty download folder wrote into the app directory.** The new "Use
  default" button saved `""`, and two joins used the setting raw, so a download
  landed in the process working directory where the library scan never looked.
  `Engine.download_root()` resolves empty or whitespace to the default.
- Not-downloaded rows could be selected and then failed to delete; they carry no
  tick now.
- A missing download folder no longer hides behind pending rows.
- The PDF zoom key listener and resize handler were never detached, so a second
  PDF shared keystrokes with the first.

## [1.8.0] — 2026-10-20

### Fixed

- **The reader opened zoomed in and needed scrolling.** Pages are now fitted to
  the pane — width *and* height — for both CBZ/CBR and PDF, so a page is fully
  visible when it opens.
- **Settings had an empty "Download folder" box with no explanation.** Empty now
  reads as what it means, with the effective path shown as the placeholder and
  in a line underneath; a "Use default" button appears once you set a custom one.

### Added

- **Zoom controls in the reader:** `−`, `+` and `Fit`, plus the `-`, `+` and `0`
  keys. Zoom is a multiplier on fit, so it behaves the same at any window size.

### Changed

- **Downloads default to `~/Documents/HardlyReading`**, not Downloads — these are
  files you keep and read, not a download tray. XDG is honoured, so a relocated
  or localised Documents folder is respected.

## [1.7.0] — 2026-10-20

### Added

- **Installable as an app (PWA).** A web manifest, generated icons and a service
  worker let the browser install HardlyReading in its own window with the HR
  icon. The shell opens offline and shows "Disconnected" instead of a network
  error. `/api/*` is never cached, so a stale queue is impossible. An offline
  install cannot search or download anything — archive.org is the whole point.
- README discloses at the top that the project is built with DeepSeek V4.1 Flash
  running in a Hermes agent.

### Fixed

- `README` link to Limitations from the header.

## [1.6.1] — 2026-10-20

### Fixed

- **One bad job could stop the queue.** The worker loop had no catch around a
  job's own handling, so an exception escaping it killed the thread and every
  waiting job sat there until you added something else. A failure is now
  recorded against that job and the queue moves on to the next one.
- **A job that resolved to nothing reported success.** An unknown identifier, or
  an item with no PDF/CBZ/RAR, finished as "done" with no files. It is now
  "failed · no downloadable files"; the "done" path is kept for the case that
  actually is success — everything already on disk.

## [1.6.0] — 2026-10-20

### Added

- **You control the queue order.** Every waiting job has `⤒` top, `▲` up one,
  `▼` down one and `⤓` bottom, plus a `#n` chip showing its real place in the run
  order. The buttons disable at the ends instead of doing nothing. Order is saved
  in `state.json` and survives a restart; only waiting work can be moved, because
  reordering a download in flight would promise what the transfer cannot honour.
- `POST /api/move` accepts `{id, index}` or `{id, delta}` and answers 409 for a
  job that cannot be reordered.
- The library's empty state now names the download folder, and says plainly when
  that folder does not exist — an empty library and a missing folder are
  different problems and used to look identical.

## [1.5.0] — 2026-10-20

### Fixed

- **PDFs had no reading progress.** They were opened in the browser's built-in
  viewer, which is a plugin: page script cannot read the current page, so there
  was no position to save and no history entry. PDFs are now drawn by pdf.js on a
  canvas — one page, fit to width, arrows or `←`/`→` — and every page turn writes
  to the same SQLite table the CBZ reader uses.
- **Page counts of zero.** `pdf_page_count()` required the file to start with
  `%PDF`. One real upload begins with 259 bytes of a PHP error page, so a valid
  52-page PDF reported no pages at all. The header is now located rather than
  required at byte zero.

### Changed

- **The queue queues.** Adding a link starts downloading immediately. The file-plan
  screen is now opt-in (`review_before_download`), off by default.
- pdf.js 4.10.38 is vendored in `vendor/` (about 1.7 MB, Apache-2.0) instead of
  loaded from a CDN, so the app works without internet beyond archive.org.
  `/vendor/` is served with a realpath containment check.

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

[1.9.0]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v1.9.0
[1.8.0]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v1.8.0
[1.7.0]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v1.7.0
[1.6.1]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v1.6.1
[1.6.0]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v1.6.0
[1.5.0]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v1.5.0
[1.4.0]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v1.4.0
[1.3.0]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v1.3.0
[1.2.0]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v1.2.0
[1.1.0]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v1.1.0
[1.0.0]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v1.0.0

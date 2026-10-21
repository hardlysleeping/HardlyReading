# Changelog

Notable changes to HardlyReading. Versions are tagged on GitHub with matching
release notes.

## [2.2.1] — 2026-10-20

### Added

- **PDFs get real covers in the library.** They used a "PDF" placeholder
  because a PDF has no page list to read a first page out of. Page one is now
  rendered with poppler's `pdftoppm` and cached like any other thumbnail.
  Optional by design: the binary is not bundled, and without it the row keeps
  its placeholder instead of showing a broken image.



### Changed

- **One control height.** The queue row held four buttons at 37×25, 52×29,
  31×12 and 58×21. Everything interactive is 28 px tall now: Read, the folder
  icon, the dismiss cross, the details pill.
- **Search cards line up.** Title, blurb and the `more` link each reserve their
  space, so metadata sits at the same height across a row instead of stepping
  down wherever a card happened to have a description. Measured: eight cards,
  all 345 px, every `.y` and `.d` at identical offsets.
- The `more` control hugs its own text instead of stretching the card width as a
  divider, and "Add all results" is a secondary-size button rather than the
  largest control in the header.



### Changed

- **Queue card actions are one row, not a stack.** Open folder / Read / Remove
  ran down the right edge, which held a tall empty column on every short card.
  They now sit side by side under the card, with the status pill alone in the
  right column.
- **Open folder is an icon** — an inline folder glyph that inherits the button's
  colour, so there is no asset to load and nothing to render wrong.
- Remove is a single ✕ icon; there were two remove buttons on the same card, one
  labelled and one not.



### Added

- **The queue is grouped by what it is doing.** Four sections, in the order you
  care about them: **Downloading now**, **Needs attention** (failed), **Waiting**
  (in run order) and **Finished** (newest first), each with a count. Before, a
  finished job and a running one looked the same in one flat list.
- **Concurrent transfers are visible.** aria2 runs several files at once (-j),
  so the running card lists every file in flight with its own percentage, size
  and speed — five thin bars rather than one anonymous number.

### Fixed

- A job that finished with nothing on disk said **Done** in green. It now says
  **Nothing downloaded**, and loses the Open folder / Read buttons that had
  nothing to act on.
- Speed and the progress bar appear only while a job is running; a finished card
  used to show a full bar and a stale "36 MB/s".
- `queueRows()` read the global snapshot instead of the state it was given, and
  the four status buckets overlapped, which rendered the same card twice.



### Added

- **Descriptions are indexed, not re-fetched.** A `descriptions` table in the
  same SQLite file as reading progress stores each item's blurb the first time
  it is seen, keyed by identifier. Repeat lookups went from 141 ms to 6 ms, and
  a library row shows its text without a network call.
- **Empty is cached too**: "this item has no description" is an answer worth
  remembering, otherwise every open would ask again for the items that will
  never have one.
- The queue resolver writes to the same index, so a book downloaded after the
  fact carries the description the search card showed.



### Added

- **Runs on boot.** `contrib/hardlyreading.service` is a systemd user unit that
  starts the server, restarts it if it dies, and survives a reboot;
  `loginctl enable-linger` covers the case where nobody logs in. The README
  explains the two paths to edit.

### Changed

- **An expanded library row shows the same description its search card would.**
  Rows carrying no stored blurb now ask archive.org once when opened
  (`/api/desc`), through the same filter the search cards use, so the two views
  cannot disagree.
- Descriptions are stored **per item** rather than per job: a collection's blurb
  used to be copied onto every issue in it, which made expanded rows describe the
  collection instead of the book.
- A row whose item has no description says so in muted italic — *No description
  for this item on archive.org* — instead of showing an empty panel that reads
  like a bug.



### Fixed

- **A downloaded book could not be opened at all.** 2.1.3 removed the duplicate
  Read from the expanded panel, but the row itself never had one, so every
  library row had zero buttons and reading anything already on disk was
  impossible. Read sits on the row now, next to Details, and opens the book.
- That Read button also shared the `data-open` attribute with the queue's
  "Open folder", so the library handler swallowed the queue's clicks. The
  library action is `data-read`, which the queue already used for its own
  per-job Read.

## [2.1.5] — 2026-10-20

### Changed

- **Search grid no longer squashed.** Cards were 150 px wide, seven to a row,
  with nowhere for a title or a blurb to sit. They are 215 px minimum now (237 px
  at a normal window), the cover is taller, and the text inside has room.
- **Expanding is discoverable.** A `▾ more` arrow sits under every description
  and flips to `▴ less`; the queue's toggle reads `▾ show files` / `▴ hide files`;
  the library's `▾ details` is a bordered pill instead of bare text.
- **Library rows show their description while collapsed**, not only when the row
  is open.

### Notes

- A description still only appears where archive.org has one that says
  something. Two books in a three-book library show none because their item's
  description is literally its own title.

## [2.1.4] — 2026-10-20

### Fixed

- **Books already in the library showed no description.** The blurb was only
  stored when a job resolved, so everything queued earlier had none, and the
  folder scan had no way to know which job a file on disk came from. Jobs now
  backfill their description once at startup, and files found by the scan are
  matched back to their job by name so a downloaded book explains itself too.

## [2.1.3] — 2026-10-20

### Added

- **Descriptions in the queue and the library.** A queue card carries the
  source's own blurb under its title (two lines until the card is open); an
  expanded library row shows it above its facts. The job keeps the first useful
  description it resolves and passes it down to its library rows.

### Changed

- "Show in folder" is **gone**, along with its route. It opened a file-manager
  window on every click, and one click too many buried the desktop.
- Expanding is now something you can see: queue cards say **show files** /
  **hide files**, and library rows carry a **▾ details / ▴ hide** toggle pinned
  to the right edge instead of a bare chevron sitting next to the status word.
- Queue descriptions are 12 px on a brighter grey so they read as text.
- A row's State reads in plain words ("ready to download", "download failed")
  rather than the internal queue status, and a row with no file no longer claims
  a format of "pending".
- The expanded library panel no longer repeats the row's Read button.

## [2.1.2] — 2026-10-20

### Fixed

- **Search descriptions were there but did not read as descriptions.** Most
  archive.org collections type their own title into the description field, so
  cards showed the same words twice, and the real blurbs were 11.5 px dim grey
  clamped to two lines — technically present, practically invisible. A
  description that only repeats the title (or is under four words) is now
  dropped, and the remaining ones are brighter, larger, and set off by a rule so
  they read as a caption rather than as more metadata. Measured on a live
  search: 25 cards, 15 descriptions shown, none of them a repeat of its title.

## [2.1.1] — 2026-10-20

### Added

- **Library rows expand.** Clicking a row opens its detail instead of throwing a
  book at the reader: full file name, size, page count, where you are in it, and
  the format. The open panel carries the row's own actions — **Read**,
  **Show in folder**, **Delete**, and a link to the item on archive.org when
  there is one. Clicking the row again closes it, and an open row stays open
  across the library poll.
- **Show in folder** opens the file manager at the book's folder, or says so
  plainly when there is no file manager to open (a server, a container).

### Changed

- A plain click on a library row no longer opens the reader. Reading is an
  explicit Read in the row's detail, so a stray click on a list of files cannot
  take over the screen.

### Fixed

- The row summary and the detail panel disagreed about reading position
  (`13/52` against `14 of 52`); both now report the stored position.
- Archives were labelled by their storage kind in the detail panel; CBR and CBZ
  now print as such instead of `CB7`-style truncation.

## [2.1.0] — 2026-10-20

### Added

- **Read without downloading.** Library rows for work that is not on disk get a
  **Read** button next to Download. The reader streams page images from
  archive.org (`/api/stream`), so a 53-page issue opens in a second instead of
  waiting for a 50 MB archive — and nothing is written to the library folder.
  The same reader shell, fit/zoom, arrows and saved progress apply, and the page
  position is stored in the same table the downloaded books use.
- A quiet note under the reader controls says what streaming costs: it needs a
  connection and the pages are lower resolution than the file. It carries a
  **Download it** button and can be dismissed.

### Notes

- archive.org publishes no page count for these items and serves pages *past*
  the end as repeats rather than errors, so the total comes from the item's OCR
  page index. When no derivative lists the leaves the counter says "?" instead
  of guessing.

## [2.0.2] — 2026-10-20

### Changed

- The Internet Archive support link is a small quiet pill — dim 11.5 px text, no
  accent border, lifting only on hover — so it reads as thanks rather than as
  the next step. Label shortened to "Support the Archive".

## [2.0.1] — 2026-10-20

### Fixed

- **Library scan resolved the folder twice, once wrongly.** `_library_from_jobs`
  still joined `settings["download_dir"]` raw, so an empty value would have meant
  the app directory for exactly the path that rebuilds the library. It uses
  `Engine.download_root()` now, as does the `dir` reported to the UI.
- **A failed PDF open left the previous reader's listeners installed.**
  `pdfState` was set before pdf.js had loaded, so an exception skipped the
  cleanup and a later close removed nothing. State is now claimed only once the
  document is open, and every listener reference is cleared up front.
- **A cover that failed to load went blank.** The error handler hid the image
  inside a fixed-size box; it now swaps in the placeholder. Covers load eagerly
  so a lazy list cannot leave an unloaded slot that looks like a missing cover.

## [2.0.0] — 2026-10-20

### Added

- **Covers.** Search results already had them; queue cards now show the item's
  cover from archive.org, and library rows show page one of the file, scaled to
  96 px by Pillow and cached under the config directory. A page of ten full-size
  scans would be tens of megabytes; a cached thumbnail costs a few kB.
- **Descriptions in search results**, clamped to two lines and expanded in place
  on click — the click expands the text instead of toggling the selection.
- **Expandable queue cards.** Clicking a card lists the files inside the job.
  The list is fetched when you open it (`/api/job`), not with the 1.2 s poll, and
  the card stays open across repaints.

### Changed

- Screenshots re-shot against the current UI.

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

[2.1.7]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.1.7
[2.1.6]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.1.6
[2.1.5]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.1.5
[2.1.4]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.1.4
[2.1.3]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.1.3
[2.1.2]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.1.2
[2.1.1]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.1.1
[2.1.0]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.1.0
[2.0.2]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.0.2
[2.0.1]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.0.1
[2.0.0]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.0.0
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

# Changelog

Notable changes to HardlyReading. Versions are tagged on GitHub with matching
release notes.

## [2.7.3] — 2026-10-22

### Fixed

- **The unlock overlay covered the app even when no password was set.**
  `.gate{display:flex}` beat the `hidden` attribute because no `.gate[hidden]`
  rule existed, so the lock screen sat over the queue and library for everyone
  from 2.7.0 onward, with no password to type. It is hidden now, and a test
  fails if any class that sets `display` is ever used on a hidden element
  without a matching `[hidden]` rule.

## [2.7.2] — 2026-10-22

### Fixed

- **A failed listener swap could still end up open to the network.** When the
  loopback bind failed, the stored `local_only` flag was rewritten from the
  socket state — and with nothing listening that read as "not local", so the
  app came back on all interfaces at the next start. The request is kept
  instead, and a restart retries loopback. The listener now also reports "no
  host" between closing the old socket and binding the new one, rather than
  claiming the old address.

## [2.7.1] — 2026-10-22

### Fixed

- The password test asserted `0600` on every platform; Windows reports `0666`
  because `chmod` there cannot remove inherited ACL entries. The check is
  POSIX-only now, and the README says so instead of promising `0600` everywhere.

## [2.7.0] — 2026-10-22

### Added

- **A password for the app, off by default.** Set one in Settings and every API
  call needs a signed-in session; the page shell and `/api/auth` stay open so the
  unlock box can draw. The hash is PBKDF2-SHA256, 200,000 rounds, in its own
  `auth.json` with mode `0600` — never in `state.json`, which the browser reads.
  Sessions live in memory, so a restart signs every device out. Five wrong
  guesses lock that client for 30 seconds, counted per address. The first
  password can only be set from the machine running the app; otherwise whoever
  reached an open port first could claim it.
- **Local only.** A Settings toggle that moves the listener between loopback and
  all interfaces without a restart, so a toggle cannot interrupt the queue.
- **Categories for the queue and the library.** Two scrolling strips, one per
  list, with up to three pinned first, reordering, renaming and deletion that
  never touches files or jobs. A library file borrows the category of the job
  that produced it. New links land in the selected tab's category.
- **Hidden items.** Right-click a library row (long-press on a phone) to file it
  away without deleting anything; the eye in the toolbar cycles hidden-only,
  dimmed and visible.

### Fixed

- **An empty category tab broke the library.** The branch used `catTab`, which
  only exists inside the strip renderer, so selecting a category with no files
  threw `ReferenceError` and the list never repainted. It is `catTabLib` now.
- **"Local only" could fail open.** If the loopback bind ran out of retries, the
  fallback opened the port on all interfaces — the opposite of what was asked.
  It now reopens loopback or leaves nothing listening, and the setting is
  reverted so it matches the socket that actually exists.
- **Unauthenticated bodies were read before the gate**, with no size limit, so
  an open port could be made to buffer anything. The gate runs first and bodies
  over 1 MB are refused with 413 without being read.
- **Renaming a category to a name already in use** was allowed, leaving two tabs
  with the same label; it is rejected the same way adding one is.
- Filing a job refreshed only the library strip instead of both.

## [2.6.1] — 2026-10-21

### Fixed

- **`IA_CONFIG_DIR`** now overrides the config folder on every platform. On
  Windows and macOS the native location was hard-coded, so a second instance or
  a test run could not be kept away from the real state file. CI found it.

### Added

- **A test suite** (`tests/test_engine.py`, 28 tests, no network, temp folders
  only) covering the shared file budget, job clamping, queue reordering, resume,
  filename sanitising, deep-paging fallback, disk reporting, caches and reading
  positions.
- **CI on Linux, Windows and macOS** (Python 3.10 and 3.13) so the cross-platform
  paths are exercised rather than asserted.

## [2.6.0] — 2026-10-21

### Added

- **Several jobs at once.** `Jobs at once` (1–4, default 2) sizes a worker pool
  instead of one thread taking the queue in turn. The file budget is shared, so
  two jobs means about six files each rather than twenty-four in total — more
  parallelism without turning the machine into a heater. The header counts what
  is running ("3 downloading") instead of naming one job.
- **A Disk panel in Settings.** Used and free space for the download folder with
  a bar, the cache sizes, and **Clear** to empty the cover and streamed-page
  caches. Read live from the filesystem, not from startup.
- **Keep free (MB)** is now a setting, so the pause threshold is visible instead
  of a constant in the code.

### Fixed

- A `aria.txt` was left behind in every series folder after a job; it is removed
  once the batch it describes finishes, including when the job pauses.
- Windows: the Documents folder now follows OneDrive redirection before falling
  back to `%USERPROFILE%\Documents`.
- The status header could only describe one running job.

## [2.5.2] — 2026-10-21

### Fixed

- **The Download button was disabled when the whole job would not fit**, even
  though the line beside it promised the job would pause instead. Starting is
  allowed now; the pause is the safety net.
- **A failed free-space check looked like a full disk.** `free_bytes` returns 0
  on error (permissions, an unusual filesystem) and the download refused to
  start for every file. 0 now means "unknown" and the check is skipped.
- The **Paused** pill had no colour of its own and rendered as plain grey
  beside the amber `Ready` pill.
- A paused job kept the previous per-file progress rows and speed in its card.
- Deep-paging fallback was invisible: the search said page 1 while the pager
  claimed the page you asked for. It now says archive.org stopped that deep.
- `space_reserve_mb` existed in code but not in the settings, so it could not be
  changed through the API at all.

## [2.5.1] — 2026-10-21

### Changed

- Queue screenshot re-shot on a real waiting queue, so the reorder arrows, the
  `#1 #2 #3` run-order badges and `the newest 7 of 18` are visible in the docs.
- New screenshot of the **How many** / **starting from** picker, linked from the
  README section that explains it.

## [2.5.0] — 2026-10-21

### Added

- **Queue reordering.** Every waiting card has move-to-top, move-up, move-down
  and move-to-bottom. The position badge is the run order (`#1` runs next), not
  a row number, and the card moves the instant you click rather than after the
  next poll.
- **How many issues to take.** A count picker beside *Add to queue* — presets or
  any number — plus a direction: the first issue, or the newest. The listing is
  trimmed while resolving, so "the newest 25" of a 5,000-item collection never
  enters the queue in full. The card shows `the newest 25 of 312`.
- **Pause when the disk is nearly full.** Downloads run in batches that fit,
  keeping 256 MB spare. If the next file will not fit the job pauses with the
  numbers ("needs 412 MB but only 240 MB free"), keeps its place, and offers
  **Resume** — which re-checks and continues, or pauses again, without spending
  a retry.
- `IA_DOWNLOAD_DIR` environment override, so a test instance cannot write into
  the real library.

### Fixed

- **Resume re-resolved the whole listing.** Resuming a paused or interrupted job
  went back through `/metadata` for every item (a thousand requests on a large
  search) instead of continuing from the plan it already had.
- "N duplicates skipped" counted issues dropped by the count the user chose,
  so "the first 2 of 8" claimed 6 duplicates that were never duplicates.
- The custom count box stayed visible when the picker was set to a preset:
  `display:inline-flex` overrode the `hidden` attribute.
- Move-to-top/up/down/bottom were disabled on the wrong cards, because the guard
  compared row indexes in the rendered list rather than positions in the run
  order.
- An optimistic reorder repainted the new numbers onto the old order, so a card
  could show `#2` while sitting first.

## [2.4.3] — 2026-10-20

### Changed

- README rewritten around what the app does now: cover grid and list views,
  filter chips and favourites, streaming a book without downloading, the
  grouped queue, the start-on-boot switch, and PDF covers. Limitations gained
  the honest ones — search is live rather than local, streaming needs a
  connection, lendable items still fail, PDF covers need poppler, start-on-boot
  is systemd-only.
- Screenshots re-shot from the current build: search with descriptions, library
  grid, library list with an open detail panel, the finished queue, and the PDF
  reader.

## [2.4.2] — 2026-10-20

### Fixed

- **Deep paging returned `{"error": "'response'"}`.** archive.org refuses pages
  beyond its 10,000-result limit instead of sending an empty page. The search now
  falls back to page 1, says `page: 1` and `deep_paging: true` instead of echoing
  the refused page or surfacing a raw KeyError.
- `rows` was clamped to a minimum of 10, so a caller asking for 3 got 10. It
  honours 1–100 now.

## [2.4.1] — 2026-10-20

### Fixed

- **A large PDF looked empty in the reader.** `/api/book` answered every request
  with the whole file, so pdf.js could not fetch the cross-reference table at the
  tail or a single page — it waited for the entire scan. The route serves byte
  ranges now (206 with `Content-Range`, suffix ranges for the tail, 416 for an
  impossible one). A 164 MB, 196-page file opens on page 1 in about a second.
- **Dragging to copy text collapsed the row.** Any click on a library row, queue
  card, grid card or search card toggled it, including the click that ends a
  text selection. Those handlers ignore a click while text is selected.

## [2.4.0] — 2026-10-20

### Added

- **Start when the computer starts** (Settings → Start-up), **off by default**.
  Switching it on writes a systemd user unit for this install's folder, port and
  interpreter, enables it, and tellingly does *not* start a second copy while the
  app is already running. Switching it off disables and removes the unit.
- The hint under the toggle explains what it actually buys: the queue survives a
  restart either way — `state.json` holds it and aria2 resumes `.aria2` partial
  files — so the toggle only decides whether the server comes back on its own.

### Fixed

- The generated unit ran whatever Python launched the app, which could be a
  virtualenv that will not exist later. It now prefers a system interpreter.
- Enabling used `systemctl --user enable --now`, which started a second server
  fighting for the same port. It enables only.

### Notes

- Automatic start is wired for systemd on Linux. Elsewhere the toggle reports
  that it is unsupported rather than pretending; the queue and partial downloads
  still survive a restart.

## [2.3.0] — 2026-10-20

### Added

- **Grid view.** A list/grid switch in the library, remembered per browser.
  Grid draws posters with the cover, a star, a read shortcut, the title and
  either a progress bar or "unread"/"finished". Covers are requested at 220 px
  there instead of the 96 px a list row needs.
- **Filter chips**: All, ★ Favourites, Reading, Unread, Finished, Not downloaded.
  They stack with the text filter, and the pager counts the filtered set.
- **Favourites.** A star on every row and grid card, stored in SQLite by book
  key — the same key reading progress uses, so a favourite survives a rescan,
  a re-download and a restart.

### Changed

- `/api/thumb` takes a width (`w`), clamped to 64–320 px, so one endpoint serves
  both the 34 px list row and the 220 px grid tile.

## [2.2.1] — 2026-10-20

### Added

- **PDFs get real covers in the library.** They used a "PDF" placeholder
  because a PDF has no page list to read a first page out of. Page one is now
  rendered with poppler's `pdftoppm` and cached like any other thumbnail.
  Optional by design: the binary is not bundled, and without it the row keeps
  its placeholder instead of showing a broken image.

## [2.2.0] — 2026-10-20

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

## [2.1.10] — 2026-10-20

### Changed

- **Queue card actions are one row, not a stack.** Open folder / Read / Remove
  ran down the right edge, which held a tall empty column on every short card.
  They now sit side by side under the card, with the status pill alone in the
  right column.
- **Open folder is an icon** — an inline folder glyph that inherits the button's
  colour, so there is no asset to load and nothing to render wrong.
- Remove is a single ✕ icon; there were two remove buttons on the same card, one
  labelled and one not.

## [2.1.9] — 2026-10-20

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

## [2.1.8] — 2026-10-20

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

## [2.1.7] — 2026-10-20

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

## [2.1.6] — 2026-10-20

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

[2.7.3]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.7.3
[2.7.2]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.7.2
[2.7.1]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.7.1
[2.7.0]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.7.0
[2.6.1]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.6.1
[2.6.0]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.6.0
[2.5.2]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.5.2
[2.5.1]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.5.1
[2.5.0]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.5.0
[2.4.3]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.4.3
[2.4.2]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.4.2
[2.4.1]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.4.1
[2.4.0]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.4.0
[2.3.0]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.3.0
[2.2.1]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.2.1
[2.2.0]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.2.0
[2.1.10]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.1.10
[2.1.9]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.1.9
[2.1.8]: https://github.com/hardlysleeping/HardlyReading/releases/tag/v2.1.8
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

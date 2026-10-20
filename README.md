# HardlyReading

Desktop-friendly web app for grabbing magazines, comics and PDFs from
[Internet Archive](https://archive.org) and reading them locally.

Paste a collection link (or an item link, or a search result), it resolves every
member, downloads one good file per issue — **CBZ/CBR first, then PDF** — verifies
each one against archive.org's checksum, and files it into a Komga-compatible
folder so a comic server can read it without manual tidying.

Everything runs on your own machine. No account, no cloud, no telemetry.

> ### ☕ This app is useless without the Internet Archive
>
> Every search, every scan and every download comes from
> [archive.org](https://archive.org) — a non-profit library that runs on
> donations. If HardlyReading is useful to you,
> **[donate to keep it online →](https://archive.org/donate)**

---

![Download queue](docs/queue.png)

## How it works

Four files, no framework, no build step:

| File | Role |
|---|---|
| `run.py` | Launcher. Checks Python and `aria2c`, then starts the server and opens your browser. |
| `engine.py` | Everything that isn't the web layer: talking to archive.org, resolving links, planning filenames, downloading, verifying, the queue, and reading positions. |
| `server.py` | A small `http.server` wrapper. Serves `index.html` and exposes the engine as JSON endpoints. |
| `index.html` | The whole UI — HTML, CSS and JS in one file. Polls `/api/state` and re-renders. |

A job moves through these stages:

```
paste link ──▶ resolve ──▶ plan names ──▶ download ──▶ verify ──▶ library
```

1. **Resolve.** A `/details/` item is one entry. A collection or a search is
   turned into a list of identifiers via archive.org's advancedsearch API, then
   each item is probed in parallel. `pick_file()` chooses one artifact per item:
   CBR → CBZ → image-PDF → text-PDF, preferring `source == original` and the
   largest file within a format.
2. **Plan names.** `plan_filenames()` builds `<Series> NNN (Year).ext` for every
   entry, sorted by printed issue number. This happens *before* downloading, so
   the name used on disk, the name used for verification and the name shown in
   the UI are the same string.
3. **Download.** `aria2c` fetches each file, resuming partials with `-c`. Files
   already present at the right size are skipped.
4. **Verify.** Size and MD5 are checked against archive.org's metadata. A
   mismatch is a failure, not a silent pass.
5. **Library.** Entries with a verified file appear on the Library tab. Reading
   position is written to SQLite keyed by `ia:<identifier>:<artifact>`.

The engine owns a single worker thread (`Engine._run`). Jobs sit in a list,
each with a status; the worker takes the next runnable one and advances it. The
UI never drives a download directly — it only posts intent and reads back state.

## What it does

- **Paste links** — `archive.org/details/...` collections, single items, or
  archive.org search URLs. Collections pull every member.
- **Search inside the app** — title-scoped so you get the publication you typed,
  not 1,000 full-text matches. Filters for collections and multi-issue listings.
- **Picks the best file per issue** — CBR → CBZ → image-PDF → text-PDF, prefers
  original scans over derivatives, skips superseded `history/` copies.
- **Verifies every download** — byte size and MD5 against archive.org's metadata.
  Worth doing: archive.org sometimes serves an error page with HTTP 200.
- **Resumes after a crash** — restart the app and interrupted downloads continue
  from where they stopped, finished files are skipped.
- **Retries sensibly** — transient failures back off 15m → 1h → 3h → 8h. Genuinely
  missing items are marked unavailable instead of retrying forever.
- **Komga-ready output** — one folder per series, files named `<Series> NNN (Year).ext`
  so they sort in reading order.
- **Library + reader** — browse what you downloaded, open a CBZ or PDF, progress
  is saved server-side in SQLite so it follows you between devices.

## Screenshots

| Search | Library |
|---|---|
| ![Search](docs/search.png) | ![Library](docs/library.png) |

Reader — fits the page, arrow keys to turn, remembers your place:

![Reader](docs/reader.png)

## Requirements

- **Python 3.8+** (stdlib only — nothing to `pip install`)
- **aria2c** — the downloader. Required.

Install aria2:

| Platform | Command |
|---|---|
| Windows | `winget install aria2.aria2` |
| macOS | `brew install aria2` |
| Debian / Ubuntu | `sudo apt install aria2` |
| Fedora | `sudo dnf install aria2` |
| Arch | `sudo pacman -S aria2` |

## Run it

```bash
python run.py
```

Your browser opens at `http://127.0.0.1:8788`. Leave the terminal window open
while you use it; close it to stop.

`run.py` checks your Python, finds aria2c, and tells you exactly what to install
if anything is missing — it will not half-start.

## Using it

1. **Paste links** into the box on the Downloads tab, or search archive.org in the
   Search tab and add results.
2. A **Ready** card appears showing how many items and how much disk it needs.
   Download is disabled if the grab will not fit.
3. Press **Download**. Progress, speed and current file update live.
4. When it finishes, **Read** opens it in the built-in reader, or **Open folder**
   shows the files.

Files land in your downloads folder, under one subfolder per series:

```
~/Downloads/HardlyReading/
└── MAD Magazine/
    ├── MAD Magazine 179 (1976).cbz
    └── MAD Magazine 180 (1976).cbz
```

Point Komga (or any comic server) at `~/Downloads/HardlyReading` and each
subfolder becomes a series.

## The reader

Deliberately minimal: one page at a time, fitted to the window, `←`/`→` or the
arrow buttons to turn, `Esc` to close. CBZ pages are read straight out of the zip
by the server; PDFs are handed to your browser's own viewer.

Reading position is stored in SQLite on the machine running the app
(`history.db`), keyed by the archive.org identifier — so the same book resumes
in the same place whatever device or browser you open it from.

## Settings

The gear in the header opens a panel; changes save as you edit and persist in
`state.json`:

| Setting | Default | What it does |
|---|---|---|
| Show "reader" label | on | The small label in the reader header. Off gives a clean page. |
| Ask before downloading | on | Show the file plan and total size, then wait for you. |
| Skip files I already have | on | Drop entries whose checksum is already in the folder. |
| Komga folder naming | on | One folder per series with issue-ordered filenames. Off keeps archive.org's original filenames in a folder named after the link. |
| Download folder | `~/Downloads/HardlyReading` | Where files go. |
| Parallel files | 12 | How many files aria2 fetches at once. |
| Pause between jobs | 30s | Gap between one job finishing and the next starting. |

Job cards can be **cancelled** before they start and **removed** from the list,
and library items can be **deleted** (file plus its reading position).

## Where things are stored

| What | Windows | macOS | Linux |
|---|---|---|---|
| Settings & queue | `%APPDATA%\hardlyreading` | `~/Library/Application Support/hardlyreading` | `~/.config/hardlyreading` |
| Downloads | `%USERPROFILE%\Downloads\HardlyReading` | `~/Downloads/HardlyReading` | `~/Downloads/HardlyReading` |

## Limitations

Worth knowing before you point it at a 500-issue collection:

- **Archive.org's metadata is often wrong.** Publication years in particular get
  batch-assigned at upload time, so the year in a filename can be the scan year
  rather than the cover date. Issue numbers are far more reliable, which is why
  they drive the ordering.
- **Ordering is by issue number, not archive's sort order.** Archive's own sorts
  are unreliable for this: unsorted is arbitrary, `year asc` groups by scan year
  with arbitrary ties, and `titleSorter` only works within a single title (a
  collection holding fifteen different magazines alphabeticalises the spin-offs).
- **One file per issue.** If an issue exists as both CBR and PDF, the CBR wins;
  the PDF is not also downloaded.
- **Only documents.** Search is pinned to `mediatype:texts` — no video, audio or
  software.
- **No re-verification sweep.** Files are verified when downloaded, not re-checked
  later. If a file gets corrupted on disk, delete it and grab it again.
- **CBR needs a RAR extractor.** CBZ is read with the Python stdlib; CBR is RAR,
  so reading one requires `unrar`, `bsdtar`, or `7z` on PATH. Without one, CBR
  files still download and are named correctly — they just aren't readable in-app.
- **PDF page progress is best-effort.** CBZ/CBR position is tracked server-side
  and follows you between devices. PDFs are shown in the browser's own viewer,
  which does not reliably expose the current page, so a PDF's position may not
  survive a reload.
- **Tested on Linux.** `run.py` handles Windows and macOS paths, the `aria2c.exe`
  lookup and the winget fallback, but has not been run on those platforms — treat
  them as untested rather than supported.

## Skipping what you already have

Before downloading a resolved collection, HardlyReading checks the destination
folder against archive.org's checksums and drops any file whose MD5 is already
there — so re-running a grab, or grabbing an overlapping collection, only
fetches what is actually missing. The checksum index is cached in
`.md5-index.json` so it is not rehashed on every run.

## Legal

Internet Archive hosts material under a wide range of rights. Downloading
something does not give you the right to redistribute it. Check what you are
downloading.

## License

GPL-3.0

## Support

[![Donate with PayPal](https://www.paypalobjects.com/en_US/i/btn/btn_donateCC_LG.gif)](https://www.paypal.com/donate/?hosted_button_id=RNT5W4XNC64EY)

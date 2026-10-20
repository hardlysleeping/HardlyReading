#!/usr/bin/env python3
"""HardlyReader launcher — the only file a normal user has to run.

    python run.py          (or double-click run.py on Windows)

What it does, in order:

1. Refuses to start on a Python too old to have the stdlib features used here.
2. Finds aria2c — next to this file, in a `bin/` folder, on PATH, or installed
   by the platform package manager. If none of those work it prints the one
   command to run and exits, rather than failing halfway through a download.
3. Starts the local web server and opens the browser.

Nothing here is Linux-specific: the same file is the entry point on Windows,
macOS and Linux.
"""

import os
import shutil
import socket
import subprocess
import sys

MIN_PY = (3, 8)
HERE = os.path.dirname(os.path.abspath(__file__))


def die(msg: str, hint: str = "") -> None:
    print()
    print("  " + msg)
    if hint:
        print()
        for line in hint.strip().splitlines():
            print("  " + line)
    print()
    print("Press Enter to close.")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass
    sys.exit(1)


def check_python() -> None:
    # Never write .pyc files. A stale __pycache__ from an earlier version can be
    # loaded in preference to the current source and silently run old code --
    # which is maddening to debug and costs nothing to avoid at this size.
    sys.dont_write_bytecode = True
    if sys.version_info < MIN_PY:
        die(
            f"HardlyReader needs Python {MIN_PY[0]}.{MIN_PY[1]} or newer "
            f"- this is {sys.version.split()[0]}.",
            "Install the current Python from https://python.org/downloads\n"
            "(on Windows, tick 'Add Python to PATH' during setup).",
        )


def aria2_candidates():
    """Every place an aria2c binary might plausibly live, best first."""
    exe = "aria2c.exe" if os.name == "nt" else "aria2c"
    yield os.path.join(HERE, exe)
    yield os.path.join(HERE, "bin", exe)
    found = shutil.which("aria2c")
    if found:
        yield found
    if os.name == "nt":
        # winget puts it under LocalAppData before PATH picks it up.
        local = os.environ.get("LOCALAPPDATA") or ""
        if local:
            yield os.path.join(local, "Microsoft", "WinGet", "Links", exe)


def works(binary: str) -> bool:
    """Actually run it — a present-but-broken binary is worse than a missing one."""
    try:
        r = subprocess.run([binary, "--version"], capture_output=True,
                           timeout=15)
        return r.returncode == 0 and b"aria2" in (r.stdout + r.stderr).lower()
    except (OSError, subprocess.SubprocessError):
        return False


def install_hint() -> str:
    if os.name == "nt":
        return ("Install aria2 with one of these, then run this again:\n\n"
                "    winget install aria2.aria2\n\n"
                "Or download aria2-*.zip from https://github.com/aria2/aria2/releases\n"
                "and drop aria2c.exe next to run.py.")
    if sys.platform == "darwin":
        return ("Install aria2 with:\n\n"
                "    brew install aria2\n\n"
                "…then run this again.")
    return ("Install aria2 with your package manager:\n\n"
            "    sudo apt install aria2        (Debian / Ubuntu)\n"
            "    sudo dnf install aria2        (Fedora)\n"
            "    sudo pacman -S aria2          (Arch)\n\n"
            "…then run this again.")


def find_aria2() -> str:
    for path in aria2_candidates():
        if os.path.isfile(path) or shutil.which(path):
            if works(path):
                return path
    # Nothing usable yet — offer to install it where the user can say yes.
    print()
    print("  aria2 is the downloader HardlyReader uses. It isn't installed yet.")
    print()
    print("  " + install_hint().replace("\n", "\n  "))
    die("Downloads cannot start without aria2.",
        "Everything else (searching, browsing the archive) needs it too,\n"
        "so please install it and run HardlyReader again.")


def check_port_free(host: str, port: int) -> int:
    """Walk forward from the preferred port until one is free."""
    for candidate in range(port, port + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, candidate))
                return candidate
            except OSError:
                continue
    return port  # let the server produce the real error


def main() -> None:
    check_python()
    aria2 = find_aria2()
    os.environ["IA_ARIA2"] = aria2

    port = check_port_free("127.0.0.1", 8788)

    print()
    print("  HardlyReader")
    print("  " + "-" * 40)
    print(f"  downloader : {aria2}")
    print(f"  address    : http://127.0.0.1:{port}")
    print()
    print("  Leave this window open while you use the app.")
    print("  Close it (or press Ctrl+C) to stop.")
    print()

    sys.argv = [sys.argv[0], "--port", str(port)]
    import server
    server.main()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  stopped.")
    except Exception as exc:  # noqa: BLE001
        import traceback
        print()
        traceback.print_exc()
        die(f"HardlyReader stopped unexpectedly: {exc}",
            "If this keeps happening, copy the text above into a bug report.")

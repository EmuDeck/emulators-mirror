#!/usr/bin/env python3
import datetime
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MIRROR = os.environ.get("GITHUB_REPOSITORY", "EmuDeck/emulators-mirror")
TAG = "latest"
INDEX = "index.json"
TOKEN = os.environ.get("GH_TOKEN", "")
EMUDECK = Path(os.environ.get("EMUDECK_REPO", "emudeck")).resolve()
DRY_RUN = "--dry-run" in sys.argv
TARGETS = (("linux", "x86"), ("linux", "arm"), ("windows", "x86"))
SOURCES = (
    (re.compile(r"^https://github\.com/([^/]+/[^/]+)/releases/download/[^/]+/([^/?#]+)$"), None),
    (re.compile(r"^https://github\.com/([^/]+/[^/]+)/raw/.+/([^/?#]+)$"), None),
    (re.compile(r"^https://gitlab\.com/([^/]+/[^/]+)/-/package_files/\d+/download$"), None),
    (re.compile(r"^https://www\.richwhitehouse\.com/jaguar/builds/([^/?#]+)$"), "richwhitehouse/bigpemu"),
)
SKIP = re.compile(r"^https://github\.com/EmuDeck/|_libretro\.(dll|so|dylib)\.zip$", re.I)
DOWNLOAD = re.compile(r"\.(zip|7z|appimage|exe|tar\.gz|tar\.xz|dll)$", re.I)
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) EmuDeck-mirror"
KEEP_DAYS = 90
NOT_MIRRORED = {"ryujinx_install"}
INSTALL_TIMEOUT = 180
PROBLEMS = []
REAL_RUN = subprocess.run
REAL_POPEN = subprocess.Popen
REAL_CHECK_OUTPUT = subprocess.check_output


def problem(text):
    """Logs something that failed so the run ends red, without stopping the rest of the mirror."""
    print(f"  ! {text}")
    PROBLEMS.append(text)


def timeout(signum, frame):
    """Stops an EmuDeck installer that takes longer than INSTALL_TIMEOUT."""
    raise TimeoutError(f"took more than {INSTALL_TIMEOUT}s")


def source_of(url):
    """Repo (or site) key and file name of an emulator download, or None if it is not mirrored. A None name is read from the server."""
    if not isinstance(url, str) or not url.startswith(("https://", "http://")) or SKIP.search(url):
        return None
    for pattern, repo in SOURCES:
        match = pattern.match(url)
        if not match:
            continue
        if repo:
            return repo, urllib.parse.unquote(match.group(1))
        if match.lastindex == 2:
            return match.group(1), urllib.parse.unquote(match.group(2))
        return match.group(1), None
    parsed = urllib.parse.urlparse(url)
    last = urllib.parse.unquote(parsed.path.rstrip("/").split("/")[-1])
    return parsed.netloc.lower(), last if "." in last else None


def file_name(url):
    """Real file name of a download whose URL does not include it (GitLab packages)."""
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        disposition = response.headers.get("Content-Disposition", "")
    match = re.search(r'filename="?([^";]+)"?', disposition)
    return match.group(1) if match else url.rstrip("/").split("/")[-1]


def collect_urls():
    """Runs every EmuDeck *_install for each system and CPU with downloads stubbed, returning the GitHub release files it would download."""
    sandbox = Path(tempfile.mkdtemp())
    os.environ["HOME"] = str(sandbox)
    os.environ["APPDATA"] = str(sandbox / "AppData")
    os.environ["EMUDECK_MIRROR_INDEX"] = f"file://{sandbox}/no-mirror.json"
    settings = sandbox / ".config" / "EmuDeck" / "settings.json"
    (settings.parent / "logs").mkdir(parents=True)
    shutil.copy(Path(__file__).with_name("settings.json"), settings)
    sys.path.insert(0, str(EMUDECK / "python"))

    import requests
    import core.all as emudeck

    found = set()

    class SkippedDownload(requests.RequestException):
        """Raised instead of downloading, so EmuDeck handles it like a failed download."""

    def record(*args, **kwargs):
        """Keeps the GitHub release URL among the arguments of a download EmuDeck tried and pretends it worked."""
        for value in (*args, *kwargs.values()):
            source = source_of(value)
            if source and source[0].lower() != MIRROR.lower():
                found.add(value)
        return True

    real_get = requests.get

    def get(url, *args, **kwargs):
        """Lets searches (APIs, download pages, release JSONs) through and records file downloads instead of doing them."""
        url = str(url)
        if kwargs.get("stream") or DOWNLOAD.search(urllib.parse.urlparse(url).path):
            record(url)
            raise SkippedDownload("download skipped by the mirror")
        if url.startswith("https://api.github.com/") and TOKEN:
            kwargs["headers"] = {**(kwargs.get("headers") or {}), "Authorization": f"Bearer {TOKEN}"}
        return real_get(url, *args, **kwargs)

    requests.get = get
    subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 0, "", "")
    subprocess.Popen = lambda *a, **k: None
    subprocess.check_output = lambda *a, **k: ""

    modules = [m for name, m in sys.modules.items() if name.startswith(("functions", "core")) and m is not None]
    for module in modules:
        for stub in ("install_emu", "safeDownload"):
            if stub in module.__dict__:
                module.__dict__[stub] = record

    installs = sorted(name for name, fn in vars(emudeck).items()
                      if callable(fn) and name.endswith("_install")
                      and getattr(fn, "__module__", "").startswith("functions.emus_scripts"))
    installs = [name for name in installs + ["srm_install", "esde_install"] if name not in NOT_MIRRORED]

    for system, cpu in TARGETS:
        for module in modules:
            module.__dict__["system"] = system
            module.__dict__["cpu_arch"] = cpu
        for name in installs:
            before = len(found)
            signal.signal(signal.SIGALRM, timeout)
            signal.alarm(INSTALL_TIMEOUT)
            try:
                getattr(emudeck, name)()
            except KeyboardInterrupt:
                raise
            except SkippedDownload:
                pass
            except BaseException as error:
                problem(f"{system}/{cpu} {name}: {type(error).__name__}: {error}")
            finally:
                signal.alarm(0)
            if len(found) > before:
                print(f"  {system}/{cpu} {name}: {len(found) - before} file(s)")

    requests.get = real_get
    subprocess.run = REAL_RUN
    subprocess.Popen = REAL_POPEN
    subprocess.check_output = REAL_CHECK_OUTPUT
    return sorted(found)


def gh(*args):
    """Runs a gh CLI command against the mirror repo."""
    REAL_RUN(["gh", *args, "-R", MIRROR], check=True)


def asset_key(name):
    """Version-agnostic key of a file, so a new version replaces the old one but a new format does not."""
    return re.sub(r"\d+", "#", name.lower())


def mirror_file(repo, name, taken):
    """Name of the mirrored copy: the original file name, prefixed with its repo only if another repo already uses it."""
    file = re.sub(r"[^A-Za-z0-9._+-]", ".", name)
    if taken.get(file.lower(), repo.lower()) != repo.lower():
        file = re.sub(r"[^A-Za-z0-9._+-]", ".", f"{repo.replace('/', '__')}__{name}")
    return file


def load_index():
    """Current index.json of the mirror release, or an empty one."""
    try:
        with urllib.request.urlopen(f"https://github.com/{MIRROR}/releases/download/{TAG}/{INDEX}", timeout=60) as response:
            return json.load(response)
    except (urllib.error.URLError, ValueError):
        return {"repos": {}}


def save_index(index):
    """Uploads index.json to the mirror release."""
    index["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    if DRY_RUN:
        return
    path = Path(tempfile.gettempdir()) / INDEX
    path.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")
    try:
        gh("release", "upload", TAG, str(path), "--clobber")
    except Exception as error:
        problem(f"index.json: {error}")


def upload(source, file):
    """Downloads a file and uploads it to the mirror release with the given name."""
    print(f"  ↑ {file}")
    if DRY_RUN:
        return
    folder = Path(tempfile.mkdtemp())
    path = folder / file
    try:
        request = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=600) as response, open(path, "wb") as out:
            shutil.copyfileobj(response, out)
        gh("release", "upload", TAG, str(path), "--clobber")
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def delete(file):
    """Removes a file from the mirror release."""
    print(f"  ✗ {file}")
    if DRY_RUN:
        return
    try:
        gh("release", "delete-asset", TAG, file, "--yes")
    except Exception as error:
        problem(f"could not delete {file}: {error}")


def ensure_release():
    """Creates the single mirror release if it does not exist yet."""
    if DRY_RUN or REAL_RUN(["gh", "release", "view", TAG, "-R", MIRROR], capture_output=True).returncode == 0:
        return
    gh("release", "create", TAG, "--title", "Emulators mirror",
       "--notes", "Nightly copy of the emulators EmuDeck downloads from GitHub. EmuDeck uses it when the upstream download fails.")


def main():
    """Mirrors what EmuDeck's installers download today, keeping files gone upstream for KEEP_DAYS days."""
    print("Asking EmuDeck what it downloads...")
    wanted = {}
    for url in collect_urls():
        repo, name = source_of(url)
        if name is None:
            try:
                name = file_name(url)
            except Exception as error:
                problem(f"{url}: {error}")
                continue
        wanted.setdefault(repo, {})[asset_key(name)] = {"name": name, "source": url}

    ensure_release()
    index = load_index()
    index.setdefault("repos", {})
    today = datetime.date.today()
    taken = {asset["file"].lower(): entry["repo"].lower()
             for entry in index["repos"].values() for asset in entry["assets"]}

    for repo in sorted(set(wanted) | {entry["repo"] for entry in index["repos"].values()}, key=str.lower):
        print(repo)
        new = wanted.get(repo, {})
        if not new:
            problem(f"{repo}: EmuDeck no longer downloads it, keeping the last good files")
        old = {asset["key"]: asset for asset in index["repos"].get(repo.lower(), {}).get("assets", [])}
        merged = {}
        for key, asset in old.items():
            if key in new or (today - datetime.date.fromisoformat(asset["seen"])).days <= KEEP_DAYS:
                merged[key] = asset
            else:
                delete(asset["file"])
        for key, asset in new.items():
            previous = old.get(key)
            file = mirror_file(repo, asset["name"], taken)
            if previous and previous["source"] == asset["source"] and previous["file"] == file:
                merged[key] = {**previous, "seen": today.isoformat()}
                continue
            try:
                upload(asset["source"], file)
            except Exception as error:
                problem(f"{repo} {asset['name']}: {type(error).__name__}: {error}")
                continue
            taken[file.lower()] = repo.lower()
            if previous and previous["file"] != file:
                delete(previous["file"])
                taken.pop(previous["file"].lower(), None)
            merged[key] = {**asset, "key": key, "file": file, "seen": today.isoformat(),
                           "url": f"https://github.com/{MIRROR}/releases/download/{TAG}/{file}"}
        if merged:
            index["repos"][repo.lower()] = {"repo": repo, "assets": sorted(merged.values(), key=lambda a: a["name"])}
        else:
            index["repos"].pop(repo.lower(), None)
        save_index(index)

    if PROBLEMS:
        print(f"\n{len(PROBLEMS)} problem(s), the rest of the mirror was updated:")
        for text in PROBLEMS:
            print(f"  - {text}")
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import datetime
import json
import os
import re
import shutil
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
RELEASE_FILE = re.compile(r"^https://github\.com/([^/]+/[^/]+)/releases/download/[^/]+/([^/?#]+)$")
KEEP_DAYS = 90
REAL_RUN = subprocess.run
REAL_POPEN = subprocess.Popen
REAL_CHECK_OUTPUT = subprocess.check_output


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

    def record(url, *args, **kwargs):
        """Keeps a URL EmuDeck tried to download and pretends the download worked."""
        match = RELEASE_FILE.match(url) if isinstance(url, str) else None
        if match and match.group(1).lower() != MIRROR.lower():
            found.add(url)
        return True

    real_get = requests.get

    def get(url, *args, **kwargs):
        """Lets GitHub API searches through with the Action token and records any other download."""
        if str(url).startswith("https://api.github.com/"):
            if TOKEN:
                kwargs["headers"] = {**(kwargs.get("headers") or {}), "Authorization": f"Bearer {TOKEN}"}
            return real_get(url, *args, **kwargs)
        record(str(url))
        raise requests.RequestException("download skipped by the mirror")

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
    installs.append("srm_install")

    for system, cpu in TARGETS:
        for module in modules:
            module.__dict__["system"] = system
            module.__dict__["cpu_arch"] = cpu
        for name in installs:
            before = len(found)
            try:
                getattr(emudeck, name)()
            except Exception as error:
                print(f"  {system}/{cpu} {name}: {type(error).__name__}: {error}")
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


def mirror_file(repo, name):
    """Name of the mirrored copy of a file inside the release."""
    return re.sub(r"[^A-Za-z0-9._-]", ".", f"{repo.replace('/', '__')}__{name}")


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
    gh("release", "upload", TAG, str(path), "--clobber")


def upload(source, file):
    """Downloads a file and uploads it to the mirror release with the given name."""
    print(f"  ↑ {file}")
    if DRY_RUN:
        return
    folder = Path(tempfile.mkdtemp())
    path = folder / file
    try:
        request = urllib.request.Request(source, headers={"User-Agent": "EmuDeck-mirror"})
        with urllib.request.urlopen(request, timeout=600) as response, open(path, "wb") as out:
            shutil.copyfileobj(response, out)
        gh("release", "upload", TAG, str(path), "--clobber")
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def delete(file):
    """Removes a file from the mirror release."""
    print(f"  ✗ {file}")
    if not DRY_RUN:
        gh("release", "delete-asset", TAG, file, "--yes")


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
        repo, name = RELEASE_FILE.match(url).groups()
        name = urllib.parse.unquote(name)
        wanted.setdefault(repo, {})[asset_key(name)] = {"name": name, "source": url}

    ensure_release()
    index = load_index()
    index.setdefault("repos", {})
    today = datetime.date.today()

    for repo in sorted(set(wanted) | {entry["repo"] for entry in index["repos"].values()}, key=str.lower):
        print(repo)
        new = wanted.get(repo, {})
        old = {asset["key"]: asset for asset in index["repos"].get(repo.lower(), {}).get("assets", [])}
        merged = {}
        for key, asset in old.items():
            if key in new or (today - datetime.date.fromisoformat(asset["seen"])).days <= KEEP_DAYS:
                merged[key] = asset
            else:
                delete(asset["file"])
        for key, asset in new.items():
            previous = old.get(key)
            if previous and previous["source"] == asset["source"]:
                merged[key] = {**previous, "seen": today.isoformat()}
                continue
            file = mirror_file(repo, asset["name"])
            try:
                upload(asset["source"], file)
            except (urllib.error.URLError, subprocess.CalledProcessError, OSError) as error:
                print(f"  ! {asset['name']}: {error}")
                continue
            if previous and previous["file"] != file:
                delete(previous["file"])
            merged[key] = {**asset, "key": key, "file": file, "seen": today.isoformat(),
                           "url": f"https://github.com/{MIRROR}/releases/download/{TAG}/{file}"}
        if merged:
            index["repos"][repo.lower()] = {"repo": repo, "assets": sorted(merged.values(), key=lambda a: a["name"])}
        else:
            index["repos"].pop(repo.lower(), None)
        save_index(index)


if __name__ == "__main__":
    main()

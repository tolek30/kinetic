from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE_DATA = ROOT / "data"
APP_DATA = ROOT / "dist" / "data"
EXE_NAME = os.environ.get("KINETIC_EXE_NAME", "KINETIC")


def migrate_existing_user_data() -> None:
    APP_DATA.mkdir(parents=True, exist_ok=True)
    config = SOURCE_DATA / "config.json"
    if config.is_file() and not (APP_DATA / "config.json").exists():
        shutil.copy2(config, APP_DATA / "config.json")

    uploads = SOURCE_DATA / "uploads"
    if uploads.is_dir():
        target_uploads = APP_DATA / "uploads"
        target_uploads.mkdir(parents=True, exist_ok=True)
        for source in uploads.iterdir():
            if source.is_file() and not (target_uploads / source.name).exists():
                shutil.copy2(source, target_uploads / source.name)

    rooms = SOURCE_DATA / "rooms"
    if rooms.is_dir():
        shutil.copytree(rooms, APP_DATA / "rooms", dirs_exist_ok=True)

    for filename in ("desktop-settings.json", "desktop-client.json"):
        source = SOURCE_DATA / filename
        target = APP_DATA / filename
        if source.is_file() and not target.exists():
            shutil.copy2(source, target)


def build() -> None:
    public_data = f"{ROOT / 'public'};public"
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        EXE_NAME,
        "--add-data",
        public_data,
        "--collect-all",
        "webview",
        "--collect-all",
        "pythonnet",
        "--collect-all",
        "clr_loader",
        "--collect-all",
        "websockets",
        "--collect-all",
        "PIL",
        "--hidden-import",
        "webview.platforms.winforms",
        "--hidden-import",
        "webview.platforms.edgechromium",
        "desktop.py",
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    migrate_existing_user_data()
    print(f"Built: {ROOT / 'dist' / (EXE_NAME + '.exe')}")


if __name__ == "__main__":
    build()

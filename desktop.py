from __future__ import annotations

import json
import ipaddress
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import uvicorn
import webview

import app as service

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
DATA_DIR = service.DATA_DIR
DESKTOP_SETTINGS = DATA_DIR / "desktop-settings.json"
CLIENT_FILE = DATA_DIR / "desktop-client.json"
PID_FILE = DATA_DIR / "desktop.pid"


def read_json(path: Path, fallback: dict) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else fallback
    except (OSError, json.JSONDecodeError):
        return fallback


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def normalize_host(address: str, port: int) -> str:
    value = address.strip()
    if not value:
        raise ValueError("Enter the Host computer's LAN address.")
    if "://" not in value:
        value = "http://" + value
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Enter a valid Host address, such as 192.168.1.20.")
    netloc = parsed.netloc if parsed.port else f"{parsed.hostname}:{port}"
    return urlunsplit((parsed.scheme, netloc, "", "", "")).rstrip("/")


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def available_local_port(preferred: int) -> int:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", preferred))
            return preferred
    except OSError:
        return free_local_port()


def create_start_menu_shortcut() -> None:
    try:
        start_menu = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        start_menu.mkdir(parents=True, exist_ok=True)
        shortcut_path = start_menu / "KINETIC.lnk"
        
        if getattr(sys, "frozen", False):
            target = sys.executable
            workdir = str(Path(target).parent)
        else:
            root_dir = Path(__file__).resolve().parent
            start_cmd = root_dir / "start.cmd"
            target = str(start_cmd) if start_cmd.exists() else sys.executable
            workdir = str(root_dir)

        ps_command = (
            f"$WScriptShell = New-Object -ComObject WScript.Shell; "
            f"$Shortcut = $WScriptShell.CreateShortcut('{shortcut_path}'); "
            f"$Shortcut.TargetPath = '{target}'; "
            f"$Shortcut.WorkingDirectory = '{workdir}'; "
            f"$Shortcut.Description = 'KINETIC Streaming Cam & Controller'; "
            f"$Shortcut.IconLocation = '{target},0'; "
            f"$Shortcut.Save()"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_command], check=True, timeout=5)
        
        try:
            import ctypes
            ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
        except Exception:
            pass
    except Exception:
        pass


class DesktopBridge:
    def __init__(self):
        self._window = None
        self._server = None
        self._server_thread = None
        self._local_port = 0

    def get_defaults(self) -> dict:
        saved = read_json(DESKTOP_SETTINGS, {})
        host_settings = service.host_settings
        return {
            "lastMode": saved.get("lastMode", "host"),
            "hostName": host_settings.get("name", "Host"),
            "hostPort": saved.get("hostPort", 7399),
            "accessCode": host_settings.get("accessCode", ""),
            "maxRooms": host_settings.get("maxRooms", 10),
            "publicUrl": host_settings.get("publicUrl", ""),
            "hostAddress": saved.get("hostAddress", ""),
            "instanceName": saved.get("instanceName", "My Cam"),
            "instancePort": saved.get("instancePort", 7399),
        }

    def detect_public_url(self, port: int) -> dict:
        try:
            import urllib.request
            with urllib.request.urlopen("https://api.ipify.org", timeout=5) as response:
                address = response.read(128).decode("ascii").strip()
            if not ipaddress.ip_address(address).is_global:
                raise ValueError("The detected address is not public.")
            return {"ok": True, "publicUrl": f"http://{address}:{int(port)}"}
        except Exception:
            return {"ok": False, "error": "Could not detect a public IP. Enter your public IP or domain manually."}

    def start_host(self, name: str, port: int, access_code: str, max_rooms: int, public_url: str = "") -> dict:
        try:
            port = int(port)
            max_rooms = int(max_rooms)
            access_code = access_code.strip()
            if not name.strip():
                raise ValueError("Enter a Host name.")
            if not 1024 <= port <= 65535:
                raise ValueError("Choose a port between 1024 and 65535.")
            if not access_code:
                raise ValueError("Create an invite code for Instances.")
            if not 1 <= max_rooms <= 10:
                raise ValueError("Host capacity must be between 1 and 10 cams.")

            settings = {
                "name": name.strip(),
                "accessCode": access_code,
                "maxRooms": max_rooms,
                "publicUrl": public_url.strip(),
                "desktop": True,
            }
            service.configure_host(settings, port)
            self._start_server("0.0.0.0", port)
            write_json(DESKTOP_SETTINGS, {"lastMode": "host", "hostPort": port})
            self._window.set_title(f"KINETIC Host | {name.strip()}")
            self._window.load_url(f"http://127.0.0.1:{port}/host")
            return {"ok": True}
        except Exception as error:
            return {"ok": False, "error": str(error)}

    def join_host(self, address: str, port: int, access_code: str, name: str) -> dict:
        try:
            port = int(port)
            if not 1 <= port <= 65535:
                raise ValueError("Choose a port between 1 and 65535.")
            if not name.strip():
                raise ValueError("Enter a name for this stream cam.")
            host_url = normalize_host(address, port)
            client_data = read_json(CLIENT_FILE, {})
            client_key = client_data.get("clientKey") or uuid.uuid4().hex
            write_json(CLIENT_FILE, {"clientKey": client_key})
            payload = json.dumps({
                "name": name.strip(),
                "clientKey": client_key,
                "accessCode": access_code.strip(),
            }).encode("utf-8")
            request = urllib.request.Request(
                f"{host_url}/api/instances/join",
                data=payload,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=8) as response:
                    joined = json.loads(response.read())
            except urllib.error.HTTPError as error:
                detail = json.loads(error.read()).get("error", "Host refused the connection.")
                raise ValueError(detail) from error
            except (urllib.error.URLError, TimeoutError) as error:
                raise ValueError("Could not reach the Host. Check its LAN address, port, and firewall.") from error

            instance = joined.get("instance", {})
            instance_id = instance.get("id")
            token = instance.get("token")
            if not instance_id or not token:
                raise ValueError("The Host returned an invalid Instance session.")

            saved_settings = read_json(DESKTOP_SETTINGS, {})
            local_port = available_local_port(int(saved_settings.get("localPort", 7400)))
            service.configure_instance({
                "hostUrl": host_url,
                "instanceId": instance_id,
                "token": token,
                "roomName": instance.get("name", name.strip()),
            }, local_port)
            self._start_server("127.0.0.1", local_port)
            write_json(DESKTOP_SETTINGS, {
                "lastMode": "instance",
                "hostAddress": address.strip(),
                "instanceName": name.strip(),
                "instancePort": port,
                "localPort": local_port,
            })
            self._window.set_title(f"KINETIC Instance | {name.strip()}")
            self._window.load_url(f"http://127.0.0.1:{local_port}/?instanceId=host")
            return {"ok": True, "overlayUrl": joined.get("overlayUrl")}
        except Exception as error:
            return {"ok": False, "error": str(error)}

    def _start_server(self, bind_host: str, port: int) -> None:
        if self._server_thread and self._server_thread.is_alive():
            raise RuntimeError("A KINETIC service is already running in this window.")
        self._local_port = port
        config = uvicorn.Config(
            service.app,
            host=bind_host,
            port=port,
            log_level="warning",
            ws_max_size=1024 * 1024,
        )
        self._server = uvicorn.Server(config)
        self._server_thread = threading.Thread(target=self._server.run, daemon=True)
        self._server_thread.start()

        health_url = f"http://127.0.0.1:{port}/api/health"
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if not self._server_thread.is_alive():
                raise RuntimeError("The local service stopped. The port may already be in use.")
            try:
                with urllib.request.urlopen(health_url, timeout=0.5):
                    return
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.1)
        self._server.should_exit = True
        raise RuntimeError("The local service did not start in time.")

    def shutdown(self):
        if self._server:
            self._server.should_exit = True

    def create_shortcut(self) -> dict:
        try:
            create_start_menu_shortcut()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def apply_update(self, download_url: str) -> dict:
        try:
            if not download_url or not download_url.startswith("http"):
                raise ValueError("Invalid download URL.")
            temp_dir = Path(os.environ.get("TEMP", Path.home()))
            update_exe = temp_dir / "KINETIC_update.exe"
            
            req = urllib.request.Request(download_url, headers={"User-Agent": "KINETIC-Updater"})
            with urllib.request.urlopen(req, timeout=30) as response, open(update_exe, "wb") as f:
                shutil.copyfileobj(response, f)
                
            target_exe = sys.executable
            if not getattr(sys, "frozen", False):
                return {"ok": False, "error": "Updates are only supported in packaged desktop builds."}
                
            batch_script = temp_dir / "kinetic_update.bat"
            batch_content = f"""@echo off
timeout /t 2 /nobreak >nul
move /y "{update_exe}" "{target_exe}"
start "" "{target_exe}"
del "%~f0"
"""
            batch_script.write_text(batch_content, encoding="utf-8")
            subprocess.Popen(["cmd.exe", "/c", str(batch_script)], creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0)
            
            if self._window:
                self._window.destroy()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def main() -> None:
    create_start_menu_shortcut()
    if PID_FILE.exists():
        try:
            existing_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            os.kill(existing_pid, 0)
            print("KINETIC desktop is already running.")
            return
        except (OSError, ValueError):
            pass
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    bridge = DesktopBridge()
    setup_page = (ROOT / "public" / "setup.html").as_uri()
    window = webview.create_window(
        "KINETIC | Host or Instance",
        setup_page,
        js_api=bridge,
        width=720,
        height=820,
        min_size=(600, 650),
        resizable=True,
        background_color="#08090b",
    )
    bridge._window = window
    window.events.closed += lambda *_args: bridge.shutdown()
    try:
        webview.start(debug=False)
    finally:
        bridge.shutdown()
        if bridge._server_thread and bridge._server_thread.is_alive():
            bridge._server_thread.join(timeout=5)
        try:
            if PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
                PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    main()

"""Native Windows WebView2 shell for the WSL workbench."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen

APP_DATA = Path(os.environ["LOCALAPPDATA"]) / "ColabVideoWorkbench"
URL = "http://127.0.0.1:8765"


def wsl(*command):
    config_path = APP_DATA / "desktop-config.json"
    config = json.loads(config_path.read_text(encoding="utf-8-sig")) if config_path.is_file() else {}
    distro = os.environ.get("COLAB_WORKBENCH_DISTRO") or config.get("distro", "Ubuntu-24.04")
    folder = os.environ.get("COLAB_WORKBENCH_ROOT") or config.get("linux_root")
    if not folder:
        raise RuntimeError("请先运行 workbench/install-windows.ps1，或设置 COLAB_WORKBENCH_ROOT。")
    return ["wsl.exe", "-d", distro, "--cd", folder, "--", *command]


class Bridge:
    def copy_agent_text(self, text):
        if not isinstance(text, str) or not 1 <= len(text) <= 16000:
            raise ValueError("Invalid Agent instructions")
        subprocess.run(["clip.exe"], input=("\ufeff" + text).encode("utf-16-le"), check=True,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
        return True

    def open_codex_login(self):
        script = 'export PATH="$HOME/.local/bin:$PATH"; codex login --device-auth; status=$?; printf "\nExit: %s. Press Enter to close." "$status"; read -r ignored'
        subprocess.Popen(wsl("bash", "-c", script), creationflags=subprocess.CREATE_NEW_CONSOLE)
        return True


    def open_setup(self, mode):
        if mode not in {"install", "auth"}:
            raise ValueError("Invalid setup mode")
        script = 'bash workbench/bootstrap.sh "$1"; status=$?; printf "\\nExit: %s. Press Enter to close." "$status"; read -r ignored'
        subprocess.Popen(wsl("bash", "-c", script, "setup", mode),
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
        return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-window", action="store_true")
    args = parser.parse_args()
    subprocess.run(wsl("python3", "workbench/app.py", "ensure"), check=True, timeout=35,
                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    with urlopen(URL + "/api/health", timeout=5) as response:
        if json.load(response)["app"] != "colab-video-workbench":
            raise RuntimeError("Unexpected local service.")
    import webview

    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    APP_DATA.mkdir(exist_ok=True)
    window = webview.create_window("Colab 影像工作台", URL, js_api=Bridge(),
                                   width=1360, height=880, min_size=(880, 580), hidden=args.smoke_window)
    proof = []

    def verify():
        for _ in range(45):
            try:
                value = window.evaluate_js('document.querySelector("#title")?.textContent')
                if value and value != "Colab 影像工作台":
                    proof.append(value)
                    break
            except Exception:
                pass
            time.sleep(.2)
        window.destroy()

    webview.start(verify if args.smoke_window else None, gui="edgechromium",
                  private_mode=False, storage_path=str(APP_DATA / "webview"))
    if args.smoke_window:
        if not proof:
            raise RuntimeError("WebView2 window did not load the project.")
        print("WebView2 loaded project: " + proof[0])


if __name__ == "__main__":
    try:
        main()
    except Exception as problem:
        if "--smoke-window" in sys.argv:
            raise
        ctypes.windll.user32.MessageBoxW(0, str(problem), "Colab 影像工作台", 16)
        sys.exit(1)

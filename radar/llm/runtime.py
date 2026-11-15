"""llama.cpp (llama-server) süreç yöneticisi.

LM Studio AÇIK OLMADAN yerel model çalıştırır. llama-server şu sırayla aranır:
  1. Ayarlardaki / LLAMA_SERVER ortam değişkenindeki yol
  2. runtime/llama.cpp/ (bu uygulamanın indirdiği sürüm)
  3. LM Studio'nun diskte tuttuğu kendi llama.cpp çalışma zamanı (extensions/backends)
  4. PATH
Hiçbiri yoksa `download_llama_cpp()` resmî llama.cpp sürümünü indirir.
"""

from __future__ import annotations

import atexit
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from pathlib import Path

import httpx

from ..paths import LOCAL_DIR, RUNTIME_DIR, read_json, write_json
from .models import gpu_memory, list_models, lmstudio_homes, load_settings, resolve_model

IS_WIN = sys.platform == "win32"
EXE = "llama-server.exe" if IS_WIN else "llama-server"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class RuntimeError_(RuntimeError):
    """Kullanıcıya gösterilebilir çalışma zamanı hatası."""


# ---------------------------------------------------------------------------
# llama-server'ı bulma
# ---------------------------------------------------------------------------

def _version_key(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def lmstudio_backends() -> list[dict]:
    """LM Studio'nun indirdiği llama.cpp motorları (llama-server içerenler)."""
    found = []
    for home in lmstudio_homes():
        base = home / "extensions" / "backends"
        if not base.is_dir():
            continue
        for d in base.iterdir():
            exe = d / EXE
            if not exe.is_file():
                continue
            man = read_json(d / "backend-manifest.json", {}) or {}
            name = (man.get("name") or d.name).lower()
            accel = "cuda12" if "cuda12" in name else "cuda" if "cuda" in name else \
                    "vulkan" if "vulkan" in name else "metal" if "metal" in name or sys.platform == "darwin" else "cpu"
            vendor = [base / "vendor" / v for v in man.get("vendor_lib_package_names", [])]
            found.append({
                "exe": exe, "accel": accel, "version": man.get("version", "0"),
                "lib_dirs": [d] + [v for v in vendor if v.is_dir()],
                "origin": f"LM Studio çalışma zamanı {man.get('version', '')} ({accel})",
            })
    return found


def find_llama_server(settings: dict | None = None) -> dict | None:
    """{'exe': Path, 'lib_dirs': [Path], 'origin': str} ya da None."""
    settings = settings or load_settings()
    for manual in (settings.get("llama_server"), os.environ.get("LLAMA_SERVER")):
        if manual and Path(manual).is_file():
            return {"exe": Path(manual), "lib_dirs": [Path(manual).parent], "origin": "elle belirtilen yol"}

    own = sorted((RUNTIME_DIR / "llama.cpp").rglob(EXE)) if (RUNTIME_DIR / "llama.cpp").is_dir() else []
    if own:
        exe = own[-1]
        return {"exe": exe, "lib_dirs": [exe.parent], "origin": f"indirilen llama.cpp ({exe.parent.name})"}

    backends = lmstudio_backends()
    if backends:
        has_nvidia = gpu_memory() is not None
        order = ["cuda12", "cuda", "metal", "vulkan", "cpu"] if has_nvidia else ["metal", "vulkan", "cpu", "cuda12", "cuda"]
        backends.sort(key=lambda b: (order.index(b["accel"]), tuple(-x for x in _version_key(b["version"]))))
        return backends[0]

    on_path = shutil.which("llama-server")
    if on_path:
        return {"exe": Path(on_path), "lib_dirs": [], "origin": "PATH"}
    return None


def download_llama_cpp(log=print) -> Path:
    """Resmî llama.cpp sürümünü runtime/llama.cpp/<etiket>/ altına indirir, llama-server yolunu döndürür."""
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if IS_WIN:
        if gpu_memory():
            patterns = [r"^llama-.*-bin-win-cuda-12[\d.]*-x64\.zip$", r"^cudart-llama-bin-win-cuda-12[\d.]*-x64\.zip$"]
        else:
            patterns = [r"^llama-.*-bin-win-vulkan-x64\.zip$"]
    elif sys.platform == "darwin":
        patterns = [rf"^llama-.*-bin-macos-{'arm64' if arm else 'x64'}\.(tar\.gz|zip)$"]
    else:
        patterns = [rf"^llama-.*-bin-ubuntu-{'arm64' if arm else 'x64'}\.(tar\.gz|zip)$"]

    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(follow_redirects=True, timeout=120, headers=headers) as client:
        releases = client.get("https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=8").json()
        chosen, tag = [], ""
        for rel in releases if isinstance(releases, list) else []:
            assets = rel.get("assets", [])
            picks = [next((a for a in assets if re.match(p, a["name"])), None) for p in patterns]
            if picks[0]:
                chosen, tag = [p for p in picks if p], rel["tag_name"]
                break
        if not chosen:
            raise RuntimeError_("llama.cpp sürümleri arasında bu platforma uygun paket bulunamadı.")
        dest = RUNTIME_DIR / "llama.cpp" / tag
        dest.mkdir(parents=True, exist_ok=True)
        for asset in chosen:
            log(f"İndiriliyor: {asset['name']} ({asset['size'] / 1e6:.0f} MB)")
            tmp = dest / asset["name"]
            with client.stream("GET", asset["browser_download_url"], headers={"Accept": "application/octet-stream"}) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
            if tmp.name.endswith(".zip"):
                with zipfile.ZipFile(tmp) as z:
                    z.extractall(dest)
            else:
                with tarfile.open(tmp) as t:
                    t.extractall(dest, filter="data")
            tmp.unlink()
    exes = sorted(dest.rglob(EXE))
    if not exes:
        raise RuntimeError_("İndirilen pakette llama-server bulunamadı.")
    # cudart paketi ayrı klasöre açılırsa DLL'leri llama-server'ın yanına taşı
    for dll in dest.rglob("*.dll"):
        if dll.parent != exes[0].parent and not (exes[0].parent / dll.name).exists():
            shutil.copy2(dll, exes[0].parent / dll.name)
    if not IS_WIN:
        for f in exes[0].parent.iterdir():
            if f.is_file() and "." not in f.name:
                f.chmod(0o755)
    log(f"llama.cpp {tag} hazır: {exes[0]}")
    return exes[0]


_HELP_CACHE: dict[str, str] = {}


def _help_text(server: dict) -> str:
    key = str(server["exe"])
    if key not in _HELP_CACHE:
        try:
            out = subprocess.run([key, "--help"], capture_output=True, text=True, timeout=30,
                                 env=_env(server), creationflags=_NO_WINDOW, errors="replace")
            _HELP_CACHE[key] = out.stdout + out.stderr
        except (OSError, subprocess.SubprocessError):
            _HELP_CACHE[key] = ""
    return _HELP_CACHE[key]


def _env(server: dict) -> dict:
    env = dict(os.environ)
    dirs = [str(d) for d in server.get("lib_dirs", [])]
    if dirs:
        env["PATH"] = os.pathsep.join(dirs + [env.get("PATH", "")])
        if not IS_WIN:
            env["LD_LIBRARY_PATH"] = os.pathsep.join(dirs + [env.get("LD_LIBRARY_PATH", "")])
    return env


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Tek bir llama-server süreci
# ---------------------------------------------------------------------------

class LlamaServer:
    def __init__(self, server: dict, model: dict, role: str, settings: dict):
        self.server, self.model, self.role = server, model, role
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.log_path = LOCAL_DIR / f"llama-{role}.log"
        self.proc: subprocess.Popen | None = None
        self.last_used = time.time()
        self.started_at = 0.0
        self.args = self._build_args(settings)

    def _build_args(self, s: dict) -> list[str]:
        help_text = _help_text(self.server)
        has = lambda flag: flag in help_text      # noqa: E731
        args = [str(self.server["exe"]), "-m", self.model["path"], "--host", "127.0.0.1", "--port", str(self.port)]
        if has("--no-webui"):
            args.append("--no-webui")
        ngl = str(s.get("gpu_layers", "auto")).lower()
        if ngl not in ("", "auto") or "'auto'" not in help_text:
            args += ["-ngl", "999" if ngl in ("", "auto", "all") else ngl]
        if self.role == "embed":
            args += ["--embedding", "-c", "4096", "-np", "4"]
        else:
            args += ["-c", str(int(s.get("ctx", 16384))), "-np", str(int(s.get("parallel", 4)))]
            mode = str(s.get("reasoning", "off")).lower()
            if mode in ("off", "on"):
                if re.search(r"--reasoning \[on\|off", help_text):
                    args += ["--reasoning", mode]
                elif mode == "off" and has("--reasoning-budget"):
                    args += ["--reasoning-budget", "0"]
        return args

    def start(self, timeout: float = 300) -> None:
        LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        log = open(self.log_path, "w", encoding="utf-8", errors="replace")
        log.write(" ".join(self.args) + "\n\n")
        log.flush()
        self.proc = subprocess.Popen(self.args, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     env=_env(self.server), creationflags=_NO_WINDOW)
        self.started_at = time.time()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError_(f"Model yüklenemedi ({self.model['file']}):\n{self.log_tail()}")
            try:
                if httpx.get(self.base_url + "/health", timeout=3).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        self.stop()
        raise RuntimeError_(f"Model {timeout:.0f} sn içinde hazır olmadı ({self.model['file']}).\n{self.log_tail()}")

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def log_tail(self, n: int = 25) -> str:
        try:
            return "\n".join(self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
        except OSError:
            return ""


# ---------------------------------------------------------------------------
# Yönetici: rol başına (chat / embed) bir süreç
# ---------------------------------------------------------------------------

class Runtime:
    def __init__(self):
        self._servers: dict[str, LlamaServer] = {}
        self._lock = threading.RLock()
        self._watcher: threading.Thread | None = None
        self.last_error = ""
        atexit.register(self.stop)
        self._kill_stale()

    # -- yardımcılar --------------------------------------------------------
    @staticmethod
    def _process_name(pid: int) -> str:
        """Çalışan sürecin adı; süreç yoksa boş dize."""
        try:
            if IS_WIN:
                out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                     capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW).stdout
                return out.split(",")[0].strip('" \r\n').lower() if f'"{pid}"' in out else ""
            cmd = Path(f"/proc/{pid}/cmdline")
            return cmd.read_bytes().split(b"\0")[0].decode("utf-8", "replace").lower() if cmd.exists() else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    def _kill_stale(self) -> None:
        """Sahibi (uygulama ya da tarama betiği) artık çalışmayan llama-server süreçlerini kapatır.
        Her sahip kendi dosyasını yazar; başka bir çalışan sürecin modeline dokunulmaz."""
        for f in LOCAL_DIR.glob("runtime_pids_*.json") if LOCAL_DIR.exists() else []:
            try:
                owner = int(f.stem.rsplit("_", 1)[1])
            except ValueError:
                continue
            if owner != os.getpid() and "python" in self._process_name(owner):
                continue                    # sahibi hâlâ çalışıyor
            for pid in (read_json(f, []) or []):
                if "llama-server" in self._process_name(pid):
                    try:
                        if IS_WIN:
                            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=10,
                                           creationflags=_NO_WINDOW)
                        else:
                            os.kill(pid, 15)
                    except (OSError, subprocess.SubprocessError):
                        pass
            f.unlink(missing_ok=True)

    def _save_pids(self) -> None:
        path = LOCAL_DIR / f"runtime_pids_{os.getpid()}.json"
        try:
            pids = [s.proc.pid for s in self._servers.values() if s.alive]
            if pids:
                write_json(path, pids)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass

    def _watch(self) -> None:
        while True:
            time.sleep(30)
            limit = float(load_settings().get("idle_stop_min", 10)) * 60
            with self._lock:
                for role, srv in list(self._servers.items()):
                    if not srv.alive or (limit > 0 and time.time() - srv.last_used > limit):
                        srv.stop()
                        del self._servers[role]
                self._save_pids()

    # -- dış arayüz ---------------------------------------------------------
    def ensure(self, role: str) -> str:
        """Rolün (chat | embed) modeli yüklü değilse yükler; OpenAI uyumlu kök adresi döndürür.
        Engelleyici çağrıdır; async koddan `asyncio.to_thread(runtime.ensure, rol)` ile çağırın."""
        settings = load_settings()
        if settings.get("backend") == "external":
            return settings["external_url"].rstrip("/")
        with self._lock:
            srv = self._servers.get(role)
            model = resolve_model(role, settings)
            if model is None:
                kind = "sohbet" if role == "chat" else "gömme (embedding)"
                raise RuntimeError_(f"Hiç {kind} modeli bulunamadı. LM Studio ile bir GGUF model indirin "
                                    "ya da Ayarlar'dan model klasörü ekleyin.")
            if srv and srv.alive and srv.model["path"] == model["path"]:
                srv.last_used = time.time()
                return srv.base_url + "/v1"
            if srv:
                srv.stop()
                self._servers.pop(role, None)

            server = find_llama_server(settings)
            if server is None:
                raise RuntimeError_("llama-server bulunamadı. Ayarlar sekmesinden 'llama.cpp indir' düğmesini kullanın.")

            # Ekran kartı belleği yetmeyecekse diğer roldeki modeli boşalt
            gpu = gpu_memory()
            need = model["size_gb"] * 1024 * 1.15 + 1024
            if gpu and gpu["free"] < need:
                for other in list(self._servers):
                    self._servers.pop(other).stop()

            srv = LlamaServer(server, model, role, settings)
            try:
                srv.start()
            except RuntimeError_ as exc:
                self.last_error = str(exc)
                raise
            self.last_error = ""
            self._servers[role] = srv
            self._save_pids()
            if self._watcher is None:
                self._watcher = threading.Thread(target=self._watch, daemon=True)
                self._watcher.start()
            return srv.base_url + "/v1"

    def stop(self, role: str | None = None) -> None:
        with self._lock:
            for r in [role] if role else list(self._servers):
                srv = self._servers.pop(r, None)
                if srv:
                    srv.stop()
            self._save_pids()

    def status(self) -> dict:
        settings = load_settings()
        server = find_llama_server(settings)
        models = list_models(settings)
        with self._lock:
            loaded = {r: {"model": s.model["id"], "port": s.port, "since": int(time.time() - s.started_at)}
                      for r, s in self._servers.items() if s.alive}
        return {
            "backend": settings.get("backend"),
            "llama_server": str(server["exe"]) if server else "",
            "llama_origin": server["origin"] if server else "",
            "gpu": gpu_memory(),
            "loaded": loaded,
            "selected": {r: (m["id"] if (m := resolve_model(r, settings, models)) else "") for r in ("chat", "embed")},
            "last_error": self.last_error,
        }


runtime = Runtime()

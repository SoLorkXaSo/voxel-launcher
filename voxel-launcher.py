#!/usr/bin/env python
"""
VOXEL Launcher — Minecraft-лаунчер ексклюзивно для Arch Linux (і похідних: EndeavourOS, Manjaro, CachyOS…).

Залежності (pacman):  sudo pacman -S python tk python-requests pciutils xdg-utils polkit
З AUR:                yay -S python-minecraft-launcher-lib
Фони (рекомендовано): python-pillow  (без нього замість міста — плоский градієнт)
Необов'язково:        gamemode mangohud libnotify libsecret glfw openal jre21-openjdk

Діагностика без GUI:  voxel-launcher --doctor
Зависає/гальмує?      voxel-launcher --debug   (стек зависання + повільні кадри)  або  --no-anim
VOXEL Pack без GUI:   voxel-launcher --install-pack 1.20.1 [--jar voxelboost-1.0.0.jar]
"""
import faulthandler, hashlib, json, math, os, queue, random, re, shlex, shutil, subprocess, sys, threading, time, traceback, uuid, zipfile, zlib
import http.server, urllib.parse
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog
    from tkinter import font as tkfont
    HAVE_TK = True
except ImportError:
    HAVE_TK = False

    class _Base:  # щоб модуль імпортувався без tkinter (напр. для --doctor)
        pass

    class tk:  # noqa: N801
        Tk = Frame = Canvas = Toplevel = Entry = Text = Listbox = _Base
        TclError = Exception
try:
    import minecraft_launcher_lib as mll
except ImportError:
    mll = None
try:
    import requests
except ImportError:
    requests = None
try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter
except ImportError:
    Image = None
try:
    from PIL import ImageTk
except ImportError:  # python-pillow без підтримки Tk
    ImageTk = None

# ---------------------------------------------------------------- XDG-шляхи
HOME = Path.home()
DATA = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / "voxel-launcher"
CONF = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "voxel-launcher"
STATE = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state")) / "voxel-launcher"
CONFIG = CONF / "config.json"
JVM_DIR = Path("/usr/lib/jvm")
REDIRECT = "https://login.live.com/oauth20_desktop.srf"  # запасний (ручний) вхід
# Публічний ідентифікатор застосунку (не секрет). Можна змінити в Налаштуваннях → «Azure Client ID».
DEFAULT_CLIENT_ID = "e1e85831-6a59-4502-bafc-53be595417f6"
LOGIN_TIMEOUT = 240  # секунд очікування входу в браузері
UA = {"User-Agent": "VoxelLauncher-Arch/2.0 (personal project)"}
MODS = ["fabric-api", "sodium", "lithium", "ferrite-core", "modernfix", "immediatelyfast"]
GC = ("-XX:+UseG1GC -XX:+ParallelRefProcEnabled -XX:MaxGCPauseMillis=200 "
      "-XX:+UnlockExperimentalVMOptions -XX:+DisableExplicitGC -XX:G1NewSizePercent=30 "
      "-XX:G1MaxNewSizePercent=40 -XX:G1HeapRegionSize=8M -XX:G1ReservePercent=20 "
      "-XX:G1HeapWastePercent=5 -XX:G1MixedGCCountTarget=4 -XX:InitiatingHeapOccupancyPercent=15 "
      "-XX:G1MixedGCLiveThresholdPercent=90 -XX:G1RSetUpdatingPauseTimePercent=5 "
      "-XX:SurvivorRatio=32 -XX:+PerfDisableSharedMem -XX:MaxTenuringThreshold=1").split()
GPU_MODES = ["auto", "nvidia", "mesa"]


# ---------------------------------------------------------------- Arch-специфіка (без GUI)
def is_arch():
    if Path("/etc/arch-release").exists():
        return True
    try:
        txt = Path("/etc/os-release").read_text().lower()
    except OSError:
        return False
    vals = " ".join(v.strip("\"' ") for v in re.findall(r"^(?:id|id_like)=(.*)$", txt, re.M)).split()
    return "arch" in vals


def mem_total_mb():
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 8192


def java_bin(major):
    """Шлях до java з пакета jreN-openjdk (схема каталогів Arch: /usr/lib/jvm/java-N-openjdk)."""
    for sub in ("bin/java", "jre/bin/java"):  # jre/ — лише для Java 8
        p = JVM_DIR / f"java-{major}-openjdk" / sub
        if p.exists():
            return str(p)
    return None


def find_java(major):
    exact = java_bin(major)
    if exact or major < 17:  # Java 8/11 потребують точної версії
        return exact
    for m in range(major + 1, 40):  # новіша Java сумісна, якщо гра вимагає 17+
        found = java_bin(m)
        if found:
            return found
    return None


def detect_gpus():
    try:
        out = subprocess.run(["lspci", "-mm"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    gpus = []
    for line in out.splitlines():
        try:
            f = shlex.split(line)
        except ValueError:
            continue
        if len(f) > 3 and any(k in f[1] for k in ("VGA", "3D controller", "Display controller")):
            vendor = "NVIDIA" if "NVIDIA" in f[2] else "AMD" if re.search(r"AMD|ATI", f[2]) else \
                     "Intel" if "Intel" in f[2] else f[2].split()[0]
            m = re.search(r"\[(.+?)\]", f[3])
            gpus.append(f"{vendor} {m.group(1) if m else f[3]}")
    return gpus


def lwjgl_flags():
    """Системні GLFW/OpenAL (Arch Wiki: Minecraft) — нативний Wayland і PipeWire-звук."""
    return [f"-Dorg.lwjgl.{p}.libname={so}" for p, so in
            (("glfw", "/usr/lib/libglfw.so"), ("openal", "/usr/lib/libopenal.so")) if Path(so).exists()]


def build_env(opt, gpu):
    env = os.environ.copy()
    if opt:
        env["mesa_glthread"] = "true"             # багатопотоковий GL у Mesa
        env["__GL_THREADED_OPTIMIZATIONS"] = "1"  # те саме для NVIDIA
    if gpu == "nvidia":                           # PRIME render offload
        env.update(__NV_PRIME_RENDER_OFFLOAD="1", __GLX_VENDOR_LIBRARY_NAME="nvidia",
                   __VK_LAYER_NV_optimus="NVIDIA_only")
    elif gpu == "mesa":
        env["DRI_PRIME"] = "1"
    return env


def wrappers(gamemode, mangohud):
    w = []
    if gamemode and shutil.which("gamemoderun"):
        w.append("gamemoderun")
    if mangohud and shutil.which("mangohud"):
        w.append("mangohud")
    return w


def notify(title, msg):
    if shutil.which("notify-send"):
        subprocess.Popen(["notify-send", "-a", "Voxel Launcher", title, msg])


def secret_get():
    if not shutil.which("secret-tool"):
        return None
    try:
        r = subprocess.run(["secret-tool", "lookup", "app", "voxel-launcher"],
                           capture_output=True, text=True, timeout=8)
        return r.stdout.strip() or None
    except Exception:
        return None


def secret_set(value):
    """Токен зберігається в Secret Service (gnome-keyring / KWallet). True — вдалося."""
    if not shutil.which("secret-tool"):
        return False
    try:
        r = subprocess.run(["secret-tool", "store", "--label=Voxel Launcher", "app", "voxel-launcher"],
                           input=value, text=True, timeout=10, capture_output=True)
        return r.returncode == 0
    except Exception:
        return False


def offline_uuid(nick):
    h = bytearray(hashlib.md5(("OfflinePlayer:" + nick).encode()).digest())
    h[6] = (h[6] & 0x0F) | 0x30
    h[8] = (h[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(h)))


_CFG_LOCK = threading.RLock()


def atomic_write(path, text, mode=0o600):
    """Запис через тимчасовий файл + os.replace: обрив не залишає напівзаписаного файлу,
    а права виставляються ще при створенні (токен ніколи не буває читабельним для інших)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_cfg():
    try:
        text = CONFIG.read_text("utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        raise ValueError("корінь конфігу не є об'єктом")
    except Exception:
        # не затираємо пошкоджений конфіг мовчки: зберігаємо копію, щоб збірки можна було відновити
        try:
            CONFIG.replace(CONFIG.with_name(f"config.json.bak-{int(time.time())}"))
        except OSError:
            pass
        return {}


def save_cfg(cfg):
    with _CFG_LOCK:
        atomic_write(CONFIG, json.dumps(cfg, indent=2, ensure_ascii=False))


def rgb(c):
    return [int(c[i:i + 2], 16) for i in (1, 3, 5)]


def mix(a, b, t):
    return "#%02x%02x%02x" % tuple(int(x + (y - x) * t) for x, y in zip(rgb(a), rgb(b)))


def shade(c, f):
    return mix(c, "#ffffff", f) if f >= 0 else mix(c, "#000000", -f)


def doctor():
    ok = lambda b: "✔" if b else "✘"
    print(f"Arch Linux / похідний : {ok(is_arch())}")
    print(f"tkinter               : {ok(HAVE_TK)}   (sudo pacman -S tk)")
    if HAVE_TK:
        print(f"Tk версія             : {tk.TkVersion}   DISPLAY={os.environ.get('DISPLAY', '-')} WAYLAND={os.environ.get('WAYLAND_DISPLAY', '-')}")
    print(f"python-requests       : {ok(requests)}   (sudo pacman -S python-requests)")
    print(f"python-pillow (фони)  : {ok(Image)}   (sudo pacman -S python-pillow)")
    print(f"minecraft-launcher-lib: {ok(mll)}   (yay -S python-minecraft-launcher-lib)")
    for tool, pkg in (("pkexec", "polkit"), ("lspci", "pciutils"), ("gamemoderun", "gamemode"),
                      ("mangohud", "mangohud"), ("notify-send", "libnotify"), ("secret-tool", "libsecret")):
        print(f"{tool:<22}: {ok(shutil.which(tool))}   ({pkg})")
    for lib, pkg in (("/usr/lib/libglfw.so", "glfw"), ("/usr/lib/libopenal.so", "openal")):
        print(f"{lib:<22}: {ok(Path(lib).exists())}   ({pkg})")
    print("Java:", ", ".join(f"{m}" for m in (8, 11, 17, 21, 25) if java_bin(m)) or "жодної (jre21-openjdk)")
    print("GPU :", "; ".join(detect_gpus()) or "не знайдено")
    print(f"Сесія: {os.environ.get('XDG_SESSION_TYPE', '?')}   RAM: {mem_total_mb()} МБ")


# ---------------------------------------------------------------- збірки, моди, VOXEL Pack (без GUI)
BUILDS = DATA / "builds"
VOXEL_PACK = {"mods": ["fabric-api", "sodium", "lithium", "ferrite-core", "modernfix", "immediatelyfast"],
              "own_mod_id": "voxelboost"}


class ManualDownload(Exception):
    """Автор моду заборонив завантаження поза сайтом — потрібно відкрити сторінку."""
    def __init__(self, page):
        super().__init__(page)
        self.page = page


def valid_name(n):
    return bool(re.fullmatch(r"[\w\-][\w .\-]{0,31}", n or ""))


def build_dir(name):
    if not valid_name(name):
        raise ValueError(f"недопустима назва збірки: {name!r}")
    return BUILDS / name


def open_url(url):
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def link_shared(inst):
    """Спільні resourcepacks/shaderpacks для всіх збірок (симлінки). Світи не ділимо — це небезпечно."""
    for d in ("resourcepacks", "shaderpacks"):
        target = DATA / "shared" / d
        target.mkdir(parents=True, exist_ok=True)
        link = inst / d
        if link.is_symlink():
            continue
        if link.exists():
            if any(link.iterdir()):
                continue  # не чіпаємо непорожню папку
            link.rmdir()
        link.symlink_to(target)


def manifest_load(mods):
    try:
        return json.loads((mods / ".voxel-mods.json").read_text())
    except Exception:
        return {}


def manifest_save(mods, m):
    atomic_write(mods / ".voxel-mods.json", json.dumps(m, indent=1), 0o644)


def mod_path(mods, name):
    """Шлях файлу мода лише всередині mods/ (ім'я з маніфесту не довіряємо: ../ відсікається)."""
    return mods / Path(str(name)).name


class Modrinth:
    name, API = "Modrinth", "https://api.modrinth.com/v2"

    def search(self, q, mc):
        r = requests.get(f"{self.API}/search", headers=UA, timeout=20, params={
            "query": q, "limit": 25, "index": "relevance" if q else "downloads",
            "facets": json.dumps([["project_type:mod"], [f"versions:{mc}"], ["categories:fabric"]])})
        r.raise_for_status()
        return [dict(id=h["project_id"], title=h["title"], desc=h["description"], downloads=h["downloads"],
                     author=h["author"], page=f"https://modrinth.com/mod/{h['slug']}", blocked=False)
                for h in r.json()["hits"]]

    def files(self, pid, mc):
        r = requests.get(f"{self.API}/project/{pid}/version", headers=UA, timeout=20,
                         params={"game_versions": json.dumps([mc]), "loaders": json.dumps(["fabric"])})
        r.raise_for_status()
        vs = r.json()
        vs = [v for v in vs if v["version_type"] == "release"] or vs
        if not vs:
            return None
        v = vs[0]
        f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
        return dict(key=v["project_id"], filename=f["filename"], url=f["url"], sha1=f["hashes"].get("sha1"),
                    page=f"https://modrinth.com/project/{v['project_id']}",
                    deps=[d["project_id"] for d in v["dependencies"]
                          if d["dependency_type"] == "required" and d.get("project_id")])


class CurseForge:
    name, API = "CurseForge", "https://api.curseforge.com/v1"

    def __init__(self, key):
        self.h = {**UA, "x-api-key": key, "Accept": "application/json"}

    def _get(self, path, **params):
        r = requests.get(self.API + path, headers=self.h, params=params, timeout=20)
        r.raise_for_status()
        return r.json()["data"]

    def search(self, q, mc):
        data = self._get("/mods/search", gameId=432, classId=6, searchFilter=q, gameVersion=mc,
                         modLoaderType=4, sortField=2, sortOrder="desc", pageSize=25)
        return [dict(id=m["id"], title=m["name"], desc=m.get("summary", ""), downloads=m.get("downloadCount", 0),
                     author=(m.get("authors") or [{"name": "?"}])[0]["name"],
                     page=(m.get("links") or {}).get("websiteUrl") or f"https://www.curseforge.com/projects/{m['id']}",
                     blocked=m.get("allowModDistribution") is False) for m in data]

    def files(self, pid, mc):
        fs = self._get(f"/mods/{pid}/files", gameVersion=mc, modLoaderType=4, pageSize=10)
        fs = [f for f in fs if f.get("releaseType") == 1] or fs
        if not fs:
            return None
        f = max(fs, key=lambda x: x["fileDate"])
        url = f.get("downloadUrl")
        if not url:
            try:
                url = self._get(f"/mods/{pid}/files/{f['id']}/download-url")
            except Exception:
                url = None
        return dict(key=f"cf:{pid}", filename=f["fileName"], url=url, page=f"https://www.curseforge.com/projects/{pid}",
                    sha1=next((h["value"] for h in f.get("hashes", []) if h.get("algo") == 1), None),
                    deps=[d["modId"] for d in f.get("dependencies", []) if d.get("relationType") == 3])


def _is_fabric_jar(p):
    try:
        with zipfile.ZipFile(p) as z:
            return "fabric.mod.json" in z.namelist()
    except Exception:
        return False


def install_mod(provider, pid, mc, mods, seen=None, depth=0):
    """Ставить мод і його обов'язкові залежності. Стару версію того ж моду замінює (без дублікатів).
    Вимкнений користувачем мод (.jar.disabled) лишається вимкненим. Невдалі залежності потрапляють
    у результат з префіксом «⚠», а не губляться мовчки."""
    seen = set() if seen is None else seen
    if pid in seen or depth > 3:
        return []
    seen.add(pid)
    info = provider.files(pid, mc)
    if not info:
        raise RuntimeError(f"немає файлу для {mc} (Fabric)")
    if not info["url"]:
        raise ManualDownload(info["page"])
    name = Path(info["filename"]).name  # захист від ../ у назві
    if not name.endswith(".jar") or not info["url"].startswith("https://"):
        raise RuntimeError("підозрілий файл відхилено")
    mods.mkdir(parents=True, exist_ok=True)
    m = manifest_load(mods)
    old = m.get(info["key"])
    old_base = Path(str(old)).name[:-9] if old and str(old).endswith(".disabled") else (Path(str(old)).name if old else None)
    on_path, off_path = mods / name, mods / (name + ".disabled")
    # мод вважається вимкненим, якщо користувач перейменував саме цей файл у .disabled
    disabled = off_path.exists() or bool(old and str(old).endswith(".disabled"))  # стан успадковується й при оновленні версії
    final = off_path if disabled else on_path
    if not (on_path.exists() or off_path.exists()):
        resp = requests.get(info["url"], headers=UA, timeout=120)
        resp.raise_for_status()
        data = resp.content
        if info["sha1"] and hashlib.sha1(data).hexdigest() != info["sha1"].lower():
            raise RuntimeError("хеш не збігається, файл відхилено")
        tmp = mods / (name + ".part")
        tmp.write_bytes(data)
        if not _is_fabric_jar(tmp):
            tmp.unlink(missing_ok=True)
            raise RuntimeError("завантажено не Fabric-мод (немає fabric.mod.json), файл відхилено")
        os.replace(tmp, final)
    if old and old_base != name:  # стара версія іншого імені (обидва стани: увімкнена/вимкнена)
        mod_path(mods, old).unlink(missing_ok=True)
        if old_base:
            mod_path(mods, old_base).unlink(missing_ok=True)
    m[info["key"]] = final.name
    manifest_save(mods, m)
    done = [name]
    for dep in info["deps"]:
        try:
            done += install_mod(provider, dep, mc, mods, seen, depth + 1)
        except ManualDownload as e:
            done.append(f"⚠ вручну: {e.page}")
        except Exception as e:
            done.append(f"⚠ залежність {dep}: {e}"[:140])
    return done


def _vtuple(s):
    parts = re.findall(r"\d+", str(s).split("-")[0].split("+")[0])
    return tuple(int(x) for x in parts)


def _vcmp(a, b):
    n = max(len(a), len(b))
    a, b = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def _spec_ok(spec, mc):
    """Один предикат Fabric: >=, <=, >, <, =, ~, ^, маски 1.20.x, * або точна версія."""
    spec = spec.strip()
    if spec in ("", "*"):
        return True
    m = re.fullmatch(r"(>=|<=|>|<|=|~|\^)?\s*([0-9][0-9A-Za-z.+\-]*)", spec)
    if not m:
        return False
    op, ver = m.group(1) or "=", m.group(2)
    if re.search(r"(^|\.)[xX*]($|\.)", ver):
        pre = _vtuple(re.split(r"\.[xX*]", ver)[0])
        return mc[:len(pre)] == pre
    v = _vtuple(ver)
    if not v:
        return False
    c = _vcmp(mc, v)
    if op == "=":
        return c == 0
    if op == ">=":
        return c >= 0
    if op == "<=":
        return c <= 0
    if op == ">":
        return c > 0
    if op == "<":
        return c < 0
    upper = (v[0], v[1] + 1) if op == "~" and len(v) >= 2 else (v[0] + 1,)  # ~1.20.1 -> <1.21 ; ^1.20 -> <2
    return c >= 0 and _vcmp(mc, upper) < 0


def mc_matches(req, mc_str):
    """req — рядок або список з fabric.mod.json: «||» — АБО, пробіл — І."""
    mc = _vtuple(mc_str)
    if not mc:
        return False
    alts = req if isinstance(req, list) else [req]
    for alt in alts:
        for branch in str(alt).split("||"):
            toks = re.findall(r"(?:>=|<=|>|<|=|~|\^)?\s*[^\s]+", re.sub(r"(>=|<=|>|<|=|~|\^)\s+", r"\1", branch.strip()))
            if toks and all(_spec_ok(t, mc) for t in toks):
                return True
    return False


def own_jar_problem(p, mc):
    """None, якщо .jar — наш Voxel Boost для версії mc; інакше причина відмови."""
    try:
        if p.name.endswith(("-sources.jar", "-dev.jar")):
            return "це не основний .jar"
        with zipfile.ZipFile(p) as z:
            meta = json.loads(z.read("fabric.mod.json"))
    except Exception:
        return "не схоже на Fabric-мод"
    if meta.get("id") != VOXEL_PACK["own_mod_id"]:
        return "це не Voxel Boost"
    req = meta.get("depends", {}).get("minecraft", "")
    return None if mc_matches(req, mc) else f"зібраний для Minecraft {req}, а не {mc}"


def find_own_jar(mc):
    for d in (DATA / "mods" / "voxelboost", DATA / "mods" / mc):
        for j in (sorted(d.glob("*.jar"), key=lambda p: [int(x) for x in re.findall(r"\d+", p.stem)],
                         reverse=True) if d.is_dir() else []):
            if own_jar_problem(j, mc) is None:
                return j
    return None


def place_jar(src, mods):
    mods.mkdir(parents=True, exist_ok=True)
    m, key = manifest_load(mods), "own:voxelboost"
    old = m.get(key)
    if old and old != src.name:
        mod_path(mods, old).unlink(missing_ok=True)
    shutil.copy2(src, mods / src.name)
    m[key] = src.name
    manifest_save(mods, m)


def install_pack(cfg, mc, say=lambda t, p=None: None, own_jar=None, with_own=True):
    """VOXEL Pack: окрема збірка «VOXEL Pack <версія>» = Fabric + оптимізаційні моди + наш Voxel Boost."""
    name = f"VOXEL Pack {mc}"
    mods = build_dir(name) / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    notes, prov, slugs = [], Modrinth(), VOXEL_PACK["mods"]
    for i, slug in enumerate(slugs):
        say(f"Мод: {slug}", i / len(slugs))
        try:
            notes += [x for x in install_mod(prov, slug, mc, mods) if x.startswith("⚠")]
        except Exception as e:
            notes.append(f"{slug}: {e}")
    if not any(mods.glob("*.jar")) and not any(mods.glob("*.jar.disabled")):
        raise RuntimeError("жодного моду не встановлено: " + (notes[0] if notes else "перевірте з'єднання"))
    if with_own:
        jar = Path(own_jar) if own_jar else find_own_jar(mc)
        why = own_jar_problem(jar, mc) if jar else f"файл voxelboost-*.jar не знайдено (покладіть у {DATA / 'mods' / 'voxelboost'})"
        if why is None:
            place_jar(jar, mods)
        else:
            notes.append(f"Voxel Boost не додано: {why}")
    b = cfg.setdefault("builds", {}).setdefault(name, {})  # наявні налаштування (фон тощо) зберігаємо
    b.update({"version": mc, "loader": "fabric", "auto_opt": False})
    say("Збірку встановлено", 1)
    return name, notes


def _cli_value(argv, flag):
    i = argv.index(flag)
    if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
        sys.exit(f"Після {flag} потрібне значення. Приклад: voxel-launcher --install-pack 1.20.1 [--jar voxelboost-1.0.0.jar]")
    return argv[i + 1]


def cli_pack(argv):
    if not is_arch():
        sys.exit("Цей лаунчер працює лише на Arch Linux та похідних.")
    if requests is None:
        sys.exit("Немає python-requests: sudo pacman -S python-requests")
    mc = _cli_value(argv, "--install-pack")
    if not re.fullmatch(r"\d+\.\d+(\.\d+)?", mc):
        sys.exit(f"Недопустима версія Minecraft: {mc!r} (очікується, напр., 1.20.1)")
    jar = Path(_cli_value(argv, "--jar")) if "--jar" in argv else None
    cfg = load_cfg()
    try:
        name, notes = install_pack(cfg, mc, lambda t, p=None: print(t), jar)
    except Exception as e:
        sys.exit(f"Помилка: {e}")  # ненульовий код виходу, збірку в конфіг не записуємо
    hb = ((cfg.get("hotbar") or []) + [None] * 9)[:9]
    if name in hb:
        pass
    elif None in hb:
        hb[hb.index(None)] = name
    else:
        notes.append("хотбар заповнений — збірку не додано в хотбар (оберіть її в «Збірках»)")
    cfg["hotbar"] = hb
    save_cfg(cfg)
    print(f"Готово: збірка «{name}» ({build_dir(name)})")
    for n in notes:
        print("  ⚠", n)


# ================================================================ ТЕМА ТА ФОНОВІ СЦЕНИ
ACCENTS = {"cyan": ("#19e3ff", "#8b5cf6"), "violet": ("#a78bfa", "#19e3ff"), "magenta": ("#ff3df0", "#19e3ff")}
C = {}
ANIM = [True]          # глобальний перемикач анімацій (налаштування + системний «reduce motion»)
NO_ANIM = bool(os.environ.get("VOXEL_NO_ANIM")) or "--no-anim" in sys.argv   # «аварійний вимикач» анімацій
DEBUG = bool(os.environ.get("VOXEL_DEBUG")) or "--debug" in sys.argv         # журнал повільних кадрів + watchdog зависань
UIF = HF = MONO = "sans-serif"
SCENES = {
    "neon": dict(top="#0a0620", mid="#2a0f4d", hor="#ff4fa3", n1="#19e3ff", n2="#ff3df0", moon="#ffe9f6"),
    "dusk": dict(top="#0b1030", mid="#3a2a6a", hor="#ff9a6a", n1="#ffb347", n2="#19e3ff", moon="#fff1d6"),
    "teal": dict(top="#050d1c", mid="#0c2f4a", hor="#3be0d0", n1="#19e3ff", n2="#8b5cf6", moon="#dffcff"),
}
SCENE_STYLES = list(SCENES)
CHANGELOG = ["Бічна навігація, сторінки: Головна, Збірки, Моди, VOXEL Pack, Журнал, Налаштування",
             "Нічне аніме-місто з неоном і дощем; власний фон для кожної збірки",
             "Менеджер модів: вимкнути/увімкнути/видалити встановлені моди",
             "Кольори акценту, анімації з повагою до системного «reduce motion»"]


def set_theme(name):
    a, b = ACCENTS.get(name, ACCENTS["cyan"])
    C.update(bg="#07090f", bg2="#0a0e18", panel="#0f1524", panel2="#151d33", line="#202c48", text="#e8ecf8",
             dim="#8591ad", accent=a, accent2=b, ok="#36e0a0", warn="#ffc857", bad="#ff4d6d",
             accent_dim=mix(a, "#0f1524", .82))


set_theme("cyan")


def reduced_motion():
    """Системне налаштування «зменшити рух» (GNOME). Для інших середовищ — вимикається у Налаштуваннях."""
    try:
        r = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "enable-animations"],
                           capture_output=True, text=True, timeout=2)
    except Exception:
        return False
    return r.stdout.strip() == "false"


def init_fonts():
    global UIF, HF, MONO
    have = set(tkfont.families())
    UIF = next((f for f in ("Inter", "Noto Sans", "Cantarell", "DejaVu Sans") if f in have), "sans-serif")
    HF = next((f for f in ("Orbitron", "Oxanium", "Rajdhani", "Exo 2", "Noto Sans", "DejaVu Sans") if f in have), UIF)
    MONO = next((f for f in ("JetBrains Mono", "DejaVu Sans Mono", "Noto Sans Mono") if f in have), "monospace")


def _grad_rows(d, w, h, stops):
    for y in range(h):
        t = y / max(h - 1, 1)
        for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
            if p0 <= t <= p1:
                d.line([(0, y), (w, y)], fill=tuple(rgb(mix(c0, c1, (t - p0) / ((p1 - p0) or 1)))))
                break


def _cover(im, w, h):
    s = max(w / im.width, h / im.height)
    im = im.resize((max(w, int(im.width * s + .5)), max(h, int(im.height * s + .5))), Image.Resampling.LANCZOS)
    x, y = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((x, y, x + w, y + h))


def render_scene(w, h, style, seed, page_bg, custom=None):
    """Процедурне аніме-кіберпанк місто (або користувацьке зображення) + затемнення під текст. -> PIL.Image"""
    P, rnd = SCENES.get(style, SCENES["neon"]), random.Random(seed)
    tup = lambda c: tuple(rgb(c))
    if custom:
        img = Image.blend(_cover(Image.open(custom).convert("RGB"), w, h), Image.new("RGB", (w, h), page_bg), .3)
    else:
        img = Image.new("RGB", (w, h))
        d = ImageDraw.Draw(img)
        glow = Image.new("RGB", (w, h))
        g = ImageDraw.Draw(glow)
        _grad_rows(d, w, h, [(0, P["top"]), (.5, P["mid"]), (.8, P["hor"]), (1, P["mid"])])
        for _ in range(int(w * h / 8000)):  # зорі
            d.point((rnd.randrange(w), rnd.randrange(max(1, int(h * .55)))), fill=tup(mix(P["moon"], P["top"], rnd.uniform(.3, .8))))
        mx, my, mr = int(w * .72), int(h * .26), max(18, int(h * .1))  # місяць
        g.ellipse([mx - mr * 1.5, my - mr * 1.5, mx + mr * 1.5, my + mr * 1.5], fill=tup(mix(P["hor"], "#000000", .55)))
        d.ellipse([mx - mr, my - mr, mx + mr, my + mr], fill=tup(P["moon"]))
        for dx, dy, r in ((-.3, -.2, .22), (.25, .15, .16), (-.1, .35, .12)):
            cx, cy, rr = mx + dx * mr, my + dy * mr, r * mr
            d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=tup(mix(P["moon"], P["mid"], .22)))
        hz, signs = int(h * .8), []
        for k, (dark, lo, hi, wins) in enumerate(((.5, .12, .3, False), (.75, .18, .42, True), (.93, .22, .55, True))):
            col = tup(mix(P["mid"], "#000000", dark)) if k < 2 else tup(mix("#000000", P["top"], .35))
            x = -rnd.randrange(60)
            while x < w:  # три паралакс-шари хмарочосів
                bw, bh = rnd.randrange(34, 96 if k else 70), int(h * rnd.uniform(lo, hi))
                top = hz - bh
                d.rectangle([x, top, x + bw, h], fill=col)
                if rnd.random() < .4:
                    d.rectangle([x + bw // 3, top - 12, x + bw // 3 + 4, top], fill=col)
                if wins:
                    for wy in range(top + 10, hz - 6, 13):
                        for wx in range(x + 7, x + bw - 7, 11):
                            if rnd.random() < .26:
                                wc = tup(P["n1"] if rnd.random() < .25 else "#ffd9a0")
                                d.rectangle([wx, wy, wx + 3, wy + 6], fill=wc)
                                g.rectangle([wx, wy, wx + 3, wy + 6], fill=wc)
                if k == 2 and bh > h * .25 and rnd.random() < .55:  # неонові вивіски
                    sx, sy, sl = x + rnd.randrange(4, max(5, bw - 10)), top + rnd.randrange(12, max(13, bh // 2)), rnd.randrange(34, 80)
                    sc = tup(rnd.choice((P["n1"], P["n2"])))
                    d.rectangle([sx, sy, sx + 5, sy + sl], fill=sc)
                    g.rectangle([sx - 1, sy - 1, sx + 6, sy + sl + 1], fill=sc)
                    signs.append((sx, sc))
                x += bw + rnd.randrange(-8, 6)
        for y in range(hz, h):  # мокра вулиця
            d.line([(0, y), (w, y)], fill=tup(mix("#000000", P["mid"], .25 * (1 - (y - hz) / max(1, h - hz)) + .05)))
        for sx, sc in signs:  # відблиски неону на асфальті
            g.rectangle([sx, hz + 4, sx + 5, min(h - 1, hz + int((h - hz) * .8))], fill=tuple(int(v * .55) for v in sc))
        img = ImageChops.screen(img, glow.filter(ImageFilter.GaussianBlur(9)))
        img = ImageChops.screen(img, glow.filter(ImageFilter.GaussianBlur(2)))
        rain = Image.new("RGB", (w, h))
        r = ImageDraw.Draw(rain)
        for _ in range(int(w * h / 2600)):
            x, y = rnd.randrange(w), rnd.randrange(h)
            r.line([(x, y), (x - 3, y + 15)], fill=(58, 76, 108))
        img = ImageChops.screen(img, rain)
    # затемнення зліва (під текст) і плавний перехід униз у колір сторінки
    left = Image.new("L", (w, h), 0)
    ramp = ImageChops.invert(Image.linear_gradient("L").rotate(90, expand=True)).resize((max(1, int(w * .72)), h))
    left.paste(ramp.point(lambda v: int(v * .82)), (0, 0))
    bot = Image.new("L", (w, h), 0)
    fade = Image.linear_gradient("L").resize((w, max(1, int(h * .38))))
    bot.paste(fade, (0, h - fade.height))
    return Image.composite(Image.new("RGB", (w, h), page_bg), img, ImageChops.lighter(left, bot))



# ================================================================ ЯДРО (уся логіка без GUI)
def secret_clear():
    if shutil.which("secret-tool"):
        try:
            subprocess.run(["secret-tool", "clear", "app", "voxel-launcher"], timeout=8, capture_output=True)
        except Exception:
            pass


class Core:
    """Конфіг, акаунт, збірки, моди, запуск гри. Не залежить від tkinter, тому тестується окремо."""

    def __init__(self):
        self.cfg = load_cfg()
        self.cfg.setdefault("builds", {})
        self.account, self.versions, self.gpus = None, [], None
        self.status, self.pct, self.phase = "Готово до гри", 0.0, "idle"
        self.post = lambda fn: fn()          # App підставляє безпечну чергу головного потоку
        self.ask = lambda title, msg: False  # App підставляє діалог підтвердження
        self.max_ram = max(2048, min(16384, mem_total_mb() - 1536)) // 256 * 256
        self.migrate()

    # ------------------------------------------------------------ налаштування
    def get(self, key, default=None):
        return self.cfg.get(key, default)

    def set(self, key, value):
        self.cfg[key] = value
        save_cfg(self.cfg)

    @property
    def ram(self):
        auto = max(2048, min(6144, mem_total_mb() // 2)) // 512 * 512
        return max(1024, min(self.cfg.get("ram") or auto, self.max_ram))

    def progress(self, text=None, pct=None):
        if text is not None:
            self.status = text
        if pct is not None:
            self.pct = max(0.0, min(1.0, pct))

    # ------------------------------------------------------------ збірки
    @property
    def builds(self):
        return self.cfg["builds"]

    @property
    def current(self):
        return self.cfg.get("current")

    def ensure_build(self, name, version=None, loader="fabric", auto=True):
        self.builds.setdefault(name, {"version": version or name, "loader": loader, "auto_opt": auto})

    def migrate(self):
        """Старі конфіги тримали в хотбарі просто версії — перетворюємо їх на збірки."""
        hb = self.cfg.get("hotbar") or []
        for n in hb:
            if n and n not in self.builds and re.fullmatch(r"\d+(\.\d+)+", n):
                self.ensure_build(n)
        if self.current not in self.builds:
            self.cfg["current"] = next((n for n in hb if n in self.builds), next(iter(self.builds), None))

    def select(self, name):
        if name in self.builds:
            self.set("current", name)

    def create_build(self, name, version, loader, auto=False):
        """None — успіх, інакше текст помилки."""
        name = (name or "").strip()
        if not valid_name(name):
            return "Назва: до 32 символів (літери, цифри, пробіл, . _ -)"
        if name in self.builds:
            return "Збірка з такою назвою вже є"
        if not version:
            return "Оберіть версію Minecraft"
        build_dir(name).mkdir(parents=True, exist_ok=True)
        self.builds[name] = {"version": version, "loader": loader, "auto_opt": bool(auto)}
        if not self.current:
            self.cfg["current"] = name
        save_cfg(self.cfg)
        return None

    def delete_build(self, name):
        shutil.rmtree(build_dir(name), ignore_errors=True)  # симлінки на спільні папки лише видаляються
        self.builds.pop(name, None)
        if self.current == name:
            self.cfg["current"] = next(iter(self.builds), None)
        save_cfg(self.cfg)

    def set_bg(self, name, path):
        """Власний фон збірки: копіюється в її папку (щоб не залежати від оригіналу). None — успіх."""
        b = self.builds[name]
        if not path:
            b.pop("bg", None)
            save_cfg(self.cfg)
            return None
        try:
            if Image is not None:
                with Image.open(path) as im:
                    im.verify()
            dest = build_dir(name) / ("background" + Path(path).suffix.lower())
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        except Exception as e:
            return f"Не вдалося використати зображення: {e}"[:100]
        b["bg"] = str(dest)
        save_cfg(self.cfg)
        return None

    def scene_for(self, name):
        """(стиль, власний фон) — стиль детермінований за назвою (crc32, а не hash())."""
        seed = zlib.crc32((name or "x").encode())
        custom = (self.builds.get(name or "") or {}).get("bg")
        return SCENE_STYLES[seed % len(SCENE_STYLES)], (custom if custom and Path(custom).exists() else None), seed & 0xFFFF

    def installed(self, name):
        b = self.builds.get(name)
        if not b:
            return False
        v = DATA / "game" / "versions"
        if not (v / b["version"] / f"{b['version']}.json").exists():
            return False
        if b["loader"] == "fabric":
            return any(p.name.startswith("fabric-loader-") and p.name.endswith("-" + b["version"]) for p in v.iterdir())
        return True

    # ------------------------------------------------------------ моди збірки
    def list_mods(self, name):
        d = build_dir(name) / "mods"
        if not d.is_dir():
            return []
        rows = [(p.name, not p.name.endswith(".disabled")) for p in d.iterdir()
                if p.name.endswith((".jar", ".jar.disabled"))]
        return sorted(rows, key=lambda r: r[0].lower())

    def mod_count(self, name):
        return sum(1 for _, on in self.list_mods(name) if on)

    def _rename_in_manifest(self, mods, old, new):
        m = manifest_load(mods)
        for k, v in list(m.items()):
            if v == old:
                if new:
                    m[k] = new
                else:
                    del m[k]
        manifest_save(mods, m)

    def toggle_mod(self, name, fn):
        """None — успіх, інакше текст помилки (файл-двійник не перезаписується)."""
        d = build_dir(name) / "mods"
        src = d / Path(fn).name
        dst = d / (src.name[:-9] if src.name.endswith(".disabled") else src.name + ".disabled")
        if not src.exists():
            return f"Файл {src.name} не знайдено"
        if dst.exists():
            return f"Вже існує {dst.name} — приберіть один із двох файлів вручну"
        src.rename(dst)
        self._rename_in_manifest(d, src.name, dst.name)
        return None

    def remove_mod(self, name, fn):
        d = build_dir(name) / "mods"
        (d / Path(fn).name).unlink(missing_ok=True)
        self._rename_in_manifest(d, Path(fn).name, None)

    def read_log(self, name, n=300):
        try:
            text = (STATE / "logs" / f"{name}.log").read_text("utf-8", errors="replace")
        except OSError:
            return None
        return "".join(text.splitlines(True)[-n:])

    # ------------------------------------------------------------ система
    def load_gpus(self, done):
        def work():
            self.gpus = detect_gpus()
            self.post(done)
        threading.Thread(target=work, daemon=True).start()

    def system_info(self):
        jv = [str(m) for m in (8, 11, 17, 21, 25) if java_bin(m)]
        gpu = " · ".join(self.gpus) if self.gpus else ("визначаю…" if self.gpus is None else "не визначено")
        mark = lambda b: "✔" if b else "✘"
        return [("Java (pacman)", ", ".join(jv) or "не встановлено", bool(jv)),
                ("GPU", gpu, bool(self.gpus)),
                ("Сесія", os.environ.get("XDG_SESSION_TYPE", "?"), True),
                ("RAM", f"{mem_total_mb() // 1024} ГБ", True),
                ("GameMode / MangoHud", f"{mark(shutil.which('gamemoderun'))} / {mark(shutil.which('mangohud'))}", True)]

    def load_versions(self, done):
        def work():
            try:
                vs = [v["id"] for v in mll.utils.get_version_list() if v["type"] == "release"]
            except Exception:
                vs = [v["id"] for v in mll.utils.get_installed_versions(str(DATA / "game"))
                      if re.fullmatch(r"\d+(\.\d+)+", v["id"])]
            self.versions = vs
            self.post(done)
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ акаунт Microsoft
    def client_id(self):
        return (self.cfg.get("client_id") or "").strip() or DEFAULT_CLIENT_ID

    def login_browser(self, cid, cancel):
        """Вхід без копіювання URL: браузер повертає код на локальний http://localhost:<порт>.
        Блокуючий — викликати з потоку. cancel — threading.Event для переривання."""
        ok_page = ("<!doctype html><meta charset=utf-8><title>VOXEL</title>"
                   "<body style='background:#0a0d14;color:#e6f7ff;font:18px sans-serif;display:grid;"
                   "place-items:center;height:100vh;margin:0'><div style='text-align:center'>"
                   "<h2 style='color:%s'>%s</h2><p>%s</p></div></body>")
        box = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if "code" not in q and "error" not in q:  # favicon тощо
                    self.send_response(404)
                    self.end_headers()
                    return
                box["url"], box["q"] = f"http://localhost:{box['port']}{self.path}", q
                good = "code" in q
                body = (ok_page % ("#19e6ff", "Вхід виконано", "Можна закрити цю вкладку й повернутись до лаунчера.")
                        if good else ok_page % ("#ff5470", "Не вдалося увійти", "Поверніться до лаунчера.")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        try:
            srv.timeout = 0.5
            box["port"] = srv.server_address[1]
            redirect = f"http://localhost:{box['port']}"
            url, state, verifier = mll.microsoft_account.get_secure_login_data(cid, redirect)
            try:
                open_url(url)
            except OSError:
                raise RuntimeError("Не вдалося відкрити браузер (потрібен xdg-utils)")
            end = time.time() + LOGIN_TIMEOUT
            while "url" not in box:
                if cancel.is_set():
                    raise RuntimeError("Вхід скасовано")
                if time.time() > end:
                    raise RuntimeError("Час очікування входу вичерпано")
                srv.handle_request()
        finally:
            srv.server_close()
        if "error" in box["q"]:
            raise RuntimeError((box["q"].get("error_description") or box["q"]["error"])[0][:90])
        try:
            code = mll.microsoft_account.parse_auth_code_url(box["url"], state)
        except Exception:
            raise RuntimeError("Невірна відповідь входу (state) — спробуйте ще раз")
        data = mll.microsoft_account.complete_login(cid, None, redirect, code, verifier)
        self.store_token(cid, data["refresh_token"])
        self.set_account(data)

    def login_url(self, cid):
        return mll.microsoft_account.get_secure_login_data(cid, REDIRECT)

    def login_finish(self, cid, back, state, verifier):  # блокуючий — викликати з потоку
        code = mll.microsoft_account.parse_auth_code_url(back.strip(), state)
        data = mll.microsoft_account.complete_login(cid, None, REDIRECT, code, verifier)
        self.store_token(cid, data["refresh_token"])
        self.set_account(data)

    def store_token(self, cid, token):
        if cid != DEFAULT_CLIENT_ID:
            self.cfg["client_id"] = cid
        else:
            self.cfg.pop("client_id", None)
        if secret_set(token):
            self.cfg.pop("refresh_token", None)
        else:  # немає secret-tool / сервісу — файл із правами 600
            self.cfg["refresh_token"] = token
        save_cfg(self.cfg)

    def set_account(self, d):
        self.account = {"name": d["name"], "uuid": d["id"], "token": d["access_token"]}

    def refresh_login(self, done):
        cid = self.client_id()

        def work():
            rt = secret_get() or self.cfg.get("refresh_token")
            if not rt:
                return
            try:
                data = mll.microsoft_account.complete_refresh(cid, None, REDIRECT, rt)
                self.store_token(cid, data["refresh_token"])
                self.set_account(data)
                self.post(done)
            except Exception:
                pass  # токен прострочений — користувач увійде знову
        threading.Thread(target=work, daemon=True).start()

    def logout(self):
        self.account = None
        secret_clear()
        self.cfg.pop("refresh_token", None)
        save_cfg(self.cfg)

    # ------------------------------------------------------------ запуск
    def cb(self, label):
        m = {"max": 1}
        return {"setStatus": lambda t: self.progress(f"{label}: {t}"),
                "setMax": lambda v: m.update(max=max(v, 1)),
                "setProgress": lambda v: self.progress(pct=v / m["max"])}

    def install_opt_mods(self, inst, mc):
        """Оптимізаційні моди для збірок з «авто-оптимізацією»."""
        mods = inst / "mods"
        mods.mkdir(parents=True, exist_ok=True)
        local = DATA / "mods" / mc  # власні збірки, напр. voxelboost.jar
        if local.is_dir():
            for jar in local.glob("*.jar"):
                if not (mods / jar.name).exists() and not (mods / (jar.name + ".disabled")).exists():
                    shutil.copy2(jar, mods / jar.name)
        slugs = VOXEL_PACK["mods"]
        failed = 0
        for i, slug in enumerate(slugs):
            self.progress(f"Мод: {slug}", i / len(slugs))
            try:
                if any(x.startswith("⚠") for x in install_mod(Modrinth(), slug, mc, mods)):
                    failed += 1
            except Exception:
                failed += 1  # недоступний для цієї версії або немає мережі — спробуємо наступного разу
        return failed

    def resolve_java(self, ver, root):
        """Системна Java з pacman -> (за згодою) pkexec pacman -> runtime Mojang."""
        info = mll.runtime.get_version_runtime_information(ver, root)
        major = int(info["javaMajorVersion"]) if info else 8
        exe = find_java(major)
        if exe:
            return exe, f"Java {major} (pacman)"
        pkg = f"jre{major}-openjdk"
        if shutil.which("pkexec") and self.ask("Java", f"Для цієї версії потрібен {pkg}.\nВстановити через pacman?"):
            self.progress(f"pacman -S {pkg}…", .05)
            r = subprocess.run(["pkexec", "pacman", "-S", "--needed", "--noconfirm", pkg], capture_output=True, text=True)
            exe = find_java(major)
            if exe:
                return exe, f"Java {major} (pacman)"
            self.progress(("pacman: " + (r.stderr.strip().splitlines() or ["помилка"])[-1])[:90])
            time.sleep(2)
        name = info["name"] if info else "jre-legacy"
        mll.runtime.install_jvm_runtime(name, root, callback=self.cb("Java"))
        return mll.runtime.get_executable_path(name, root), f"Java {major} (Mojang)"

    def can_launch(self, name):
        if self.phase != "idle":
            return "Гра вже запускається"
        if not name or name not in self.builds:
            return "Спершу оберіть або створіть збірку"
        nick = self.cfg.get("nick") or "Player"
        if not self.account and not re.fullmatch(r"[A-Za-z0-9_]{3,16}", nick):
            return "Нік: 3–16 символів (латиниця, цифри, _) — змініть у блоці «Акаунт»"
        return None

    def launch(self, name, done=lambda: None):
        err = self.can_launch(name)
        if err:
            self.progress(err)
            return err
        self.phase = "loading"
        threading.Thread(target=self._run, args=(name, done), daemon=True).start()
        return None

    def _run(self, name, done):
        try:
            self.run_game(name)
        finally:
            self.phase = "idle"
            self.post(done)

    def run_game(self, name):
        cfg = self.cfg
        opt, ram = cfg.get("optimize", True), self.ram
        try:
            b = self.builds[name]
            ver, root, inst = b["version"], str(DATA / "game"), build_dir(name)
            inst.mkdir(parents=True, exist_ok=True)
            self.progress("Minecraft: перевірка файлів", 0)
            mll.install.install_minecraft_version(ver, root, callback=self.cb("Minecraft"))
            launch = ver
            if b["loader"] == "fabric":
                if mll.fabric.is_minecraft_version_supported(ver):
                    mll.fabric.install_fabric(ver, root, callback=self.cb("Fabric"))
                    launch = f"fabric-loader-{mll.fabric.get_latest_loader_version()}-{ver}"
                    if opt and b.get("auto_opt") and not b.get("opt_done"):
                        # лише один раз: інакше кожен запуск повертав би моди, які користувач вимкнув
                        if self.install_opt_mods(inst, ver) == 0:
                            b["opt_done"] = True
                            save_cfg(self.cfg)
                else:
                    self.progress("Fabric недоступний для цієї версії — запуск без модів")
            if cfg.get("share"):
                link_shared(inst)
            exe, jinfo = self.resolve_java(ver, root)
            nick = cfg.get("nick") or "Player"
            acc = self.account or {"name": nick, "uuid": offline_uuid(nick), "token": "0"}
            jvm = [f"-Xms{ram // 2}M", f"-Xmx{ram}M"] + (GC if opt else []) + (lwjgl_flags() if cfg.get("sysl") else [])
            opts = {"username": acc["name"], "uuid": acc["uuid"], "token": acc["token"], "launcherName": "Voxel",
                    "launcherVersion": "3.0", "gameDirectory": str(inst), "jvmArguments": jvm}
            if exe:
                opts["executablePath"] = exe
            cmd = wrappers(cfg.get("gamemode"), cfg.get("mangohud")) + mll.command.get_minecraft_command(launch, root, opts)
            log = STATE / "logs" / f"{name}.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            self.phase = "running"
            self.progress(f"Гра запущена · {jinfo}", 1)
            # вивід — прямо у файл: гра переживе закриття лаунчера
            with open(log, "w", encoding="utf-8") as lf:
                p = subprocess.Popen(cmd, cwd=str(inst), env=build_env(opt, cfg.get("gpu", "auto")), stdout=lf,
                                     stderr=subprocess.STDOUT, start_new_session=True)
                code = p.wait()
            if code == 0:
                self.progress("Гру закрито", 0)
            else:
                self.progress(f"Гра впала (код {code}). Дивіться вкладку «Журнал»", 0)
                notify("Minecraft впав", f"Код {code}. Лог: {log}")
        except Exception as e:
            self.progress(f"Помилка: {e}"[:100], 0)



# ================================================================ GUI: допоміжні функції та віджети
def lbl(parent, text="", size=10, color=None, bold=False, fam=None, **kw):
    return tk.Label(parent, text=text, bg=kw.pop("bg", parent.cget("bg")), fg=color or C["text"],
                    font=(fam or UIF, size, "bold" if bold else "normal"), **kw)


def entry(parent, width=24, show=None):
    return tk.Entry(parent, width=width, show=show or "", font=(UIF, 11), bg=C["bg"], fg=C["text"],
                    insertbackground=C["accent"], relief="flat", highlightthickness=1,
                    highlightbackground=C["line"], highlightcolor=C["accent"])


def listbox(parent, height=10):
    return tk.Listbox(parent, height=height, bg=C["bg"], fg=C["text"], selectbackground=C["accent_dim"],
                      selectforeground=C["accent"], bd=0, highlightthickness=1, highlightbackground=C["line"],
                      highlightcolor=C["accent"], activestyle="none", exportselection=False, font=(MONO, 10))


def menu(parent, var, values, command=None):
    om = tk.OptionMenu(parent, var, *(list(values) or ["—"]), command=command)
    om.config(bg=C["panel2"], fg=C["text"], activebackground=C["accent_dim"], activeforeground=C["accent"],
              relief="flat", bd=0, highlightthickness=1, highlightbackground=C["line"], font=(UIF, 10), padx=10)
    om["menu"].config(bg=C["panel2"], fg=C["text"], activebackground=C["accent"], activeforeground="#021018",
                      font=(UIF, 10), bd=0)
    return om


def card(parent, title=None):
    """Панель із тонким контуром і акцентною смужкою; контент — у .inner."""
    outer = tk.Frame(parent, bg=C["panel"], highlightthickness=1, highlightbackground=C["line"])
    tk.Frame(outer, bg=C["accent"], height=2).pack(fill="x")
    body = tk.Frame(outer, bg=C["panel"])
    body.pack(fill="both", expand=True, padx=16, pady=12)
    if title:
        lbl(body, title.upper(), 9, C["dim"], True).pack(anchor="w", pady=(0, 8))
    outer.inner = tk.Frame(body, bg=C["panel"])
    outer.inner.pack(fill="both", expand=True)
    return outer


def header(parent, title, sub=""):
    f = tk.Frame(parent, bg=C["bg"])
    lbl(f, title, 22, "#ffffff", True, HF).pack(anchor="w")
    if sub:
        lbl(f, sub, 10, C["dim"]).pack(anchor="w", pady=(2, 0))
    return f


def clear(frame):
    for w in frame.winfo_children():
        w.destroy()
    return frame


def setting_row(parent, title, desc=""):
    """Рядок налаштування: назва й опис зліва, елемент керування справа. Повертає правий контейнер."""
    r = tk.Frame(parent, bg=parent.cget("bg"))
    r.pack(fill="x", pady=7)
    left = tk.Frame(r, bg=parent.cget("bg"))
    left.pack(side="left", fill="x", expand=True)
    lbl(left, title, 11, C["text"], True).pack(anchor="w")
    if desc:
        lbl(left, desc, 9, C["dim"], wraplength=440, justify="left").pack(anchor="w")
    right = tk.Frame(r, bg=parent.cget("bg"))
    right.pack(side="right", padx=(12, 0))
    return right


class Tooltip:
    def __init__(self, widget, text=""):
        self.w, self.text, self.job, self.tip = widget, text, None, None

    def bind(self):
        self.w.bind("<Enter>", self.enter, add="+")
        self.w.bind("<Leave>", self.leave, add="+")
        self.w.bind("<ButtonPress>", self.leave, add="+")
        return self

    def enter(self, e=None):
        self.leave()
        if self.text:
            self.job = self.w.after(550, self.show)

    def show(self):
        try:
            x, y = self.w.winfo_pointerx() + 14, self.w.winfo_pointery() + 18
            self.tip = tk.Toplevel(self.w)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry(f"+{x}+{y}")
            tk.Label(self.tip, text=self.text, bg=C["panel2"], fg=C["text"], font=(UIF, 9), padx=8, pady=4,
                     highlightthickness=1, highlightbackground=C["accent"]).pack()
        except tk.TclError:
            self.tip = None

    def leave(self, e=None):
        try:
            if self.job:
                self.w.after_cancel(self.job)
            if self.tip:
                self.tip.destroy()
        except tk.TclError:
            pass
        self.job = self.tip = None


def draw_icon(cv, kind, x, y, s, color, tag):
    """Контурні іконки одного стилю (лінія 2px), намальовані примітивами Canvas."""
    k = s / 24.0
    P = lambda *p: [v * k + (x if i % 2 == 0 else y) for i, v in enumerate(p)]
    line = lambda *p: cv.create_line(P(*p), fill=color, width=2, capstyle="round", joinstyle="round", tags=tag)
    poly = lambda *p: cv.create_polygon(P(*p), fill="", outline=color, width=2, joinstyle="round", tags=tag)
    if kind == "home":
        poly(3, 11, 12, 3, 21, 11, 21, 21, 3, 21)
        line(10, 21, 10, 14, 14, 14, 14, 21)
    elif kind == "library":
        for a, b in ((3, 3), (13, 3), (3, 13), (13, 13)):
            poly(a, b, a + 8, b, a + 8, b + 8, a, b + 8)
    elif kind == "mods":
        poly(4, 4, 20, 4, 20, 20, 4, 20)
        line(12, 8, 12, 16)
        line(8, 12, 16, 12)
    elif kind == "pack":
        poly(12, 2, 21, 7, 21, 17, 12, 22, 3, 17, 3, 7)
        line(12, 12, 12, 22)
        line(12, 12, 3, 7)
        line(12, 12, 21, 7)
    elif kind == "log":
        line(4, 6, 20, 6)
        line(4, 12, 20, 12)
        line(4, 18, 14, 18)
    elif kind == "settings":
        line(4, 7, 20, 7)
        line(4, 17, 20, 17)
        cv.create_oval(P(7, 5, 11, 9), outline=color, fill=C["bg2"], width=2, tags=tag)
        cv.create_oval(P(13, 15, 17, 19), outline=color, fill=C["bg2"], width=2, tags=tag)


class CButton:
    """Кнопка зі зрізаними кутами, що малюється на будь-якому Canvas (зокрема поверх фонового зображення)."""

    def __init__(self, cv, x, y, w, h, text, command, kind="ghost", size=10, tip=None, group="ui"):
        self.cv, self.x, self.y, self.w, self.h = cv, x, y, w, h
        self.text, self.cmd, self.kind, self.size, self.group = text, command, kind, size, group
        self.tag = f"cb{id(self)}"
        self.hover = self.target = self.pulse = 0.0
        self.pressed = self.focus = False
        self.armed = False  # натиснуто в межах кнопки; НЕ скидається подіями Leave (див. on_release)
        self.enabled, self.job, self.inside = True, None, False
        self.tt = Tooltip(cv, tip)
        self.draw()
        for ev, fn in (("<Enter>", self.on_enter), ("<Leave>", self.on_leave),
                       ("<ButtonPress-1>", self.on_press), ("<ButtonRelease-1>", self.on_release)):
            cv.tag_bind(self.tag, ev, fn)

    def destroy(self):
        self._unbind_release()
        for ev in ("<Enter>", "<Leave>", "<ButtonPress-1>", "<ButtonRelease-1>"):
            self.cv.tag_unbind(self.tag, ev)
        self.cv.delete(self.tag)
        self.tt.leave()

    def colors(self):
        a, h, k = C["accent"], self.hover, self.kind
        if not self.enabled:
            return C["panel2"], C["dim"], C["line"], 0.0
        if k == "primary":
            return mix(a, "#ffffff", .22 * h), "#021018", a, .3 + .7 * max(h, self.pulse)
        if k == "danger":
            return mix(C["panel2"], C["bad"], .15 + .6 * h), mix(C["bad"], "#ffffff", .5 * h), C["bad"], 0.0
        if k == "chip_on":
            return C["accent_dim"], a, a, 0.0
        if k == "chip":
            return mix(C["panel2"], a, .18 * h), mix(C["dim"], C["text"], h), mix(C["line"], a, h), 0.0
        return mix(C["panel2"], a, .12 * h), C["text"], mix(C["line"], a, h), 0.0  # ghost

    def draw(self):
        c, tg = self.cv, (self.tag, self.group)
        c.delete(self.tag)
        fill, fg, line, glow = self.colors()
        x, y, w, h, k = self.x, self.y, self.w, self.h, min(10, self.h // 3)
        pts = lambda i: [x + i, y + i, x + w - k, y + i, x + w - i, y + k, x + w - i, y + h - i, x + k, y + h - i, x + i, y + h - k]
        if glow > 0:  # неонове сяйво: кільця, що тьмяніють назовні
            for off, ring in ((6, .12), (4, .25), (2, .45)):
                c.create_polygon(pts(-off), fill="", outline=mix(C["bg"], C["accent"], ring * glow), width=2, tags=tg)
        c.create_polygon(pts(0), fill=fill, outline=line, width=1, tags=tg)
        d = 1 if self.pressed else 0
        c.create_text(x + w / 2 + d, y + h / 2 + d, text=self.text, fill=fg, font=(UIF, self.size, "bold"), tags=tg)
        if self.focus:
            c.create_polygon(pts(3), fill="", outline=C["accent2"], dash=(3, 3), tags=tg)

    def set(self, text=None, kind=None, enabled=None):
        ch = False
        if text is not None and text != self.text:
            self.text, ch = text, True
        if kind is not None and kind != self.kind:
            self.kind, ch = kind, True
        if enabled is not None and enabled != self.enabled:
            self.enabled, ch = enabled, True
        if ch:
            self.draw()

    def go(self, target):
        self.target = target
        if not ANIM[0]:
            self.hover = target
            self.draw()
        else:
            self.step()

    def step(self):
        try:
            if self.job:
                self.cv.after_cancel(self.job)
            d = self.target - self.hover
            self.hover = self.target if abs(d) < .06 else self.hover + d * .35
            self.draw()
            self.job = None if self.hover == self.target else self.cv.after(16, self.step)
        except tk.TclError:
            pass  # віджет уже знищено

    def on_enter(self, e=None):
        # draw() видаляє й створює елементи полотна під курсором -> Tk при перепікові знову шле <Enter>
        # -> on_enter -> draw -> ... нескінченний цикл перемальовувань (інтерфейс «зависає»). Тому — запобіжник.
        if self.inside:
            return
        self.inside = True
        if self.enabled:
            self.cv.config(cursor="hand2")
            self.go(1.0)
            self.tt.enter()

    def on_leave(self, e=None):
        if not self.inside:
            return
        self.inside = False
        self.cv.config(cursor="")
        self.pressed = False
        self.go(0.0)
        self.tt.leave()

    def on_press(self, e=None):
        self.tt.leave()
        if self.enabled:
            self.armed = self.pressed = True
            # draw() нижче видаляє елемент під курсором -> Tk втрачає «поточний елемент» і ButtonRelease
            # більше не доходить до tag_bind. Тому відпускання ловимо на самому Canvas (одноразово).
            self._unbind_release()
            self._rel_id = self.cv.bind("<ButtonRelease-1>", self._on_canvas_release, add="+")
            self.draw()

    def _unbind_release(self):
        rid = getattr(self, "_rel_id", None)
        self._rel_id = None
        if rid:
            try:
                self.cv.unbind("<ButtonRelease-1>", rid)
            except tk.TclError:
                pass

    def _on_canvas_release(self, e):
        self._unbind_release()
        self.on_release(e)

    def on_release(self, e=None):
        # draw() у on_press перестворює елементи під курсором -> Tk шле «хибний» <Leave>, який скидав pressed,
        # і клік губився. Тому рішення «було натискання» береться з armed, а влучання — з координат відпускання.
        was, self.armed, self.pressed = self.armed, False, False
        if was:
            self.draw()
            if self.enabled and (e is None or self._hit(e)):
                self.cmd()

    def _hit(self, e):
        try:
            x, y = self.cv.canvasx(e.x), self.cv.canvasy(e.y)
        except (tk.TclError, AttributeError):
            x, y = e.x, e.y
        return self.x <= x <= self.x + self.w and self.y <= y <= self.y + self.h


class NeonButton(tk.Canvas):
    """Самостійна кнопка (обгортка над CButton): фокус з клавіатури, Enter/Space."""

    def __init__(self, parent, text, command, w=140, h=36, kind="ghost", size=10, tip=None):
        super().__init__(parent, width=w + 12, height=h + 12, bg=parent.cget("bg"), highlightthickness=0, takefocus=True)
        self.b = CButton(self, 6, 6, w, h, text, command, kind, size, tip, group="btn")
        self.bind("<FocusIn>", lambda e: self._focus(True))
        self.bind("<FocusOut>", lambda e: self._focus(False))
        for ev in ("<Return>", "<space>"):
            self.bind(ev, lambda e: self.b.enabled and self.b.cmd())

    def _focus(self, on):
        self.b.focus = on
        self.b.draw()

    def set(self, **kw):
        self.b.set(**kw)


class Switch(tk.Canvas):
    """Перемикач з плавним рухом. command(value) може повернути False, щоб відхилити зміну."""

    def __init__(self, parent, value, command=None, tip=None):
        super().__init__(parent, width=52, height=32, bg=parent.cget("bg"), highlightthickness=0, cursor="hand2",
                         takefocus=True)
        self.on, self.cmd, self.pos, self.focus, self.job = bool(value), command, 1.0 if value else 0.0, False, None
        self.draw()
        self.bind("<Button-1>", lambda e: self.toggle())
        for ev in ("<space>", "<Return>"):
            self.bind(ev, lambda e: self.toggle())
        self.bind("<FocusIn>", lambda e: self._focus(True))
        self.bind("<FocusOut>", lambda e: self._focus(False))
        if tip:
            Tooltip(self, tip).bind()

    def _focus(self, on):
        self.focus = on
        self.draw()

    def draw(self):
        self.delete("all")
        track = mix(C["line"], C["accent"], self.pos)
        self.create_oval(2, 6, 22, 26, fill=track, outline="")
        self.create_oval(28, 6, 48, 26, fill=track, outline="")
        self.create_rectangle(12, 6, 38, 26, fill=track, outline="")
        kx = 6 + 28 * self.pos
        self.create_oval(kx, 8, kx + 16, 24, fill=mix(C["dim"], "#ffffff", self.pos), outline="")
        if self.focus:
            self.create_rectangle(0, 3, 51, 29, outline=C["accent2"], dash=(2, 2))

    def toggle(self):
        new = not self.on
        if self.cmd and self.cmd(new) is False:
            return
        self.set(new)

    def set(self, value):
        self.on = bool(value)
        self.glide()

    def glide(self):
        target = 1.0 if self.on else 0.0
        try:
            if self.job:
                self.after_cancel(self.job)
            if not ANIM[0] or abs(target - self.pos) < .05:
                self.pos, self.job = target, None
            else:
                self.pos += (target - self.pos) * .4
                self.job = self.after(16, self.glide)
            self.draw()
        except tk.TclError:
            pass


class Segmented(tk.Frame):
    def __init__(self, parent, options, value, command, size=9):
        super().__init__(parent, bg=parent.cget("bg"))
        self.value, self.command, self.items = value, command, {}
        for val, text in options:
            b = NeonButton(self, text, lambda v=val: self.pick(v), 22 + 8 * len(text), 30,
                           "chip_on" if val == value else "chip", size)
            b.pack(side="left", padx=1)
            self.items[val] = b

    def pick(self, v):
        self.value = v
        for k, b in self.items.items():
            b.set(kind="chip_on" if k == v else "chip")
        self.command(v)


class NavItem(tk.Canvas):
    def __init__(self, parent, kind, text, command):
        super().__init__(parent, height=44, bg=C["bg2"], highlightthickness=0, cursor="hand2", takefocus=True)
        self.kind, self.text, self.cmd = kind, text, command
        self.active = self.hover = self.focus = False
        self.draw()
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False))
        self.bind("<FocusIn>", lambda e: self._set(focus=True))
        self.bind("<FocusOut>", lambda e: self._set(focus=False))
        self.bind("<Button-1>", lambda e: self.cmd())
        for ev in ("<Return>", "<space>"):
            self.bind(ev, lambda e: self.cmd())

    def _set(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.draw()

    def set_active(self, on):
        self._set(active=on)

    def draw(self):
        self.delete("all")
        col = C["accent"] if self.active else (C["text"] if self.hover else C["dim"])
        if self.active:
            self.create_rectangle(0, 0, 400, 44, fill=C["accent_dim"], outline="")
            self.create_rectangle(0, 6, 3, 38, fill=C["accent"], outline="")
        elif self.hover:
            self.create_rectangle(0, 0, 400, 44, fill=C["panel"], outline="")
        draw_icon(self, self.kind, 20, 10, 24, col, "ic")
        self.create_text(56, 22, text=self.text, anchor="w", fill=col, font=(UIF, 11, "bold" if self.active else "normal"))
        if self.focus:
            self.create_rectangle(2, 2, 228, 42, outline=C["accent2"], dash=(3, 3))


class StatusBar(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=C["bg2"], height=34, highlightthickness=1, highlightbackground=C["line"])
        self.pack_propagate(False)
        self.txt = lbl(self, "", 10, C["dim"])
        self.txt.pack(side="left", padx=16)
        self.pc = lbl(self, "", 10, C["accent"], True)
        self.pc.pack(side="right", padx=16)
        self.bar = tk.Canvas(self, width=240, height=6, bg=C["line"], highlightthickness=0)
        self.bar.pack(side="right")
        self.fill = self.bar.create_rectangle(0, 0, 0, 6, fill=C["accent"], outline="")
        self.shown = None

    def update_state(self, text, pct):
        key = (text, round(pct, 3))
        if key == self.shown:
            return
        self.shown = key
        bad = text.startswith(("Помилка", "Гра впала"))
        self.txt.config(text=text[:130], fg=C["bad"] if bad else C["dim"])
        self.bar.coords(self.fill, 0, 0, 240 * pct, 6)
        self.pc.config(text=f"{int(pct * 100)}%" if pct > 0 else "")


class ScrollFrame(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=C["bg"])
        self.cv = tk.Canvas(self, bg=C["bg"], highlightthickness=0)
        sb = tk.Scrollbar(self, orient="vertical", command=self.cv.yview, bg=C["panel2"], troughcolor=C["bg"],
                          bd=0, highlightthickness=0, width=10)
        self.cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.cv.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.cv, bg=C["bg"])
        self.win = self.cv.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.cv.configure(scrollregion=self.cv.bbox("all")))
        self.cv.bind("<Configure>", lambda e: self.cv.itemconfigure(self.win, width=e.width))
        self.bind("<Enter>", self._on)
        self.bind("<Leave>", self._off)

    def _on(self, e=None):
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind_all(ev, self.wheel)

    def _off(self, e=None):
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.unbind_all(ev)

    def wheel(self, e):
        step = -1 if (getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0) else 1
        self.cv.yview_scroll(step * 2, "units")


class Modal(tk.Toplevel):
    """Діалог у стилі лаунчера: підтвердження або введення тексту."""

    def __init__(self, app, title, text, ok="OK", cancel="Скасувати", field=False, secret=False, danger=False):
        super().__init__(app)
        self.result, self.entry = None, None
        self.title(title)
        self.configure(bg=C["bg"])
        self.transient(app)
        self.resizable(False, False)
        box = card(self)
        box.pack(fill="both", expand=True, padx=1, pady=1)
        f = box.inner
        lbl(f, title, 14, "#ffffff", True, HF).pack(anchor="w")
        lbl(f, text, 10, C["dim"], wraplength=440, justify="left").pack(anchor="w", pady=(6, 10))
        if field:
            self.entry = entry(f, 46, "•" if secret else None)
            self.entry.pack(fill="x", pady=(0, 10))
            self.entry.focus_set()
        row = tk.Frame(f, bg=C["panel"])
        row.pack(anchor="e")
        NeonButton(row, cancel, lambda: self.finish(False), 110, 34, "ghost", 10).pack(side="left", padx=2)
        NeonButton(row, ok, lambda: self.finish(True), 120, 34, "danger" if danger else "primary", 10).pack(side="left")
        self.bind("<Escape>", lambda e: self.finish(False))
        self.bind("<Return>", lambda e: self.finish(True))
        self.update_idletasks()
        x = app.winfo_rootx() + (app.winfo_width() - self.winfo_reqwidth()) // 2
        y = app.winfo_rooty() + (app.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.after(60, self.take_focus)  # НЕ wait_visibility(): під XWayland він може не повернутись

    def take_focus(self):
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
        except tk.TclError:
            pass  # не кожен віконний менеджер дозволяє grab — діалог усе одно працює

    def finish(self, ok):
        self.result = ((self.entry.get().strip() if self.entry else True) if ok else None)
        self.destroy()


def confirm(app, title, text, ok="Так", cancel="Ні", danger=False):
    m = Modal(app, title, text, ok, cancel, danger=danger)
    app.wait_window(m)
    return bool(m.result)


def prompt(app, title, text, secret=False):
    m = Modal(app, title, text, "OK", "Скасувати", field=True, secret=secret)
    app.wait_window(m)
    return m.result or None



# ================================================================ GUI: сторінки
NAV = [("home", "Головна", "home"), ("library", "Збірки", "library"), ("mods", "Моди", "mods"),
       ("pack", "VOXEL Pack", "pack"), ("logs", "Журнал", "log"), ("settings", "Налаштування", "settings")]


def net_err(e):
    """Людське пояснення мережевих помилок (стан «немає з'єднання»)."""
    if requests is not None and isinstance(e, requests.exceptions.ConnectionError):
        return "Немає з'єднання з інтернетом або сервіс недоступний"
    if requests is not None and isinstance(e, requests.exceptions.Timeout):
        return "Сервіс не відповів вчасно — спробуйте ще раз"
    return str(e)[:90]


class Page(tk.Frame):
    def __init__(self, app):
        super().__init__(app.stage, bg=C["bg"])
        self.app, self.core = app, app.core

    def alive(self):
        try:
            return bool(self.winfo_exists())
        except tk.TclError:
            return False

    def say(self, text):
        """Повідомлення в рядку стану сторінки з будь-якого потоку."""
        self.app.post(lambda: self.alive() and self.msg.config(text=text[:120]))

    def refresh(self):
        pass


class HomePage(Page):
    def __init__(self, app):
        super().__init__(app)
        self.size, self.bg_key, self.job, self.item = (0, 0), None, None, None
        self.btns, self.drops, self.photos, self.all_btns, self.n = {}, [], [], [], 0
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=3, minsize=300)
        self.grid_rowconfigure(2, weight=2, minsize=210)
        self.hero = tk.Canvas(self, bg=C["bg"], highlightthickness=0)
        self.hero.grid(row=0, column=0, sticky="nsew")
        self.hero.bind("<Configure>", self.on_resize)
        self.chips = tk.Frame(self, bg=C["bg"])
        self.chips.grid(row=1, column=0, sticky="ew", padx=28)
        row = tk.Frame(self, bg=C["bg"])
        row.grid(row=2, column=0, sticky="nsew", padx=28, pady=(0, 18))
        row.grid_rowconfigure(0, weight=1)
        for i in range(3):
            row.grid_columnconfigure(i, weight=1, uniform="c")
        self.cards = [card(row, t) for t in ("Акаунт", "Система", "Що нового")]
        for i, c in enumerate(self.cards):
            c.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0 if i == 2 else 8))
        for line in CHANGELOG:
            lbl(self.cards[2].inner, "▸ " + line, 9, C["dim"], wraplength=270, justify="left").pack(anchor="w", pady=2)

    # ---------------------------------------------------------- герой-блок
    def T(self, x, y, text, size, color, bold=False, fam=None):
        f = (fam or UIF, size, "bold" if bold else "normal")
        self.hero.create_text(x + 2, y + 2, text=text, anchor="w", fill="#000000", font=f, tags="ui")
        self.hero.create_text(x, y, text=text, anchor="w", fill=color, font=f, tags="ui")

    def pill(self, x, y, text, color):
        c = self.hero
        wd = tkfont.Font(family=UIF, size=9, weight="bold").measure(text) + 36
        c.create_polygon(x, y, x + wd - 8, y, x + wd, y + 8, x + wd, y + 26, x + 8, y + 26, x, y + 18,
                         fill=C["bg"], outline=color, tags="ui")
        c.create_oval(x + 11, y + 10, x + 17, y + 16, fill=color, outline="", tags="ui")
        c.create_text(x + 25, y + 13, text=text, anchor="w", fill=color, font=(UIF, 9, "bold"), tags="ui")

    def mk(self, *a, **k):
        b = CButton(*a, **k)  # перший аргумент — полотно, його передає викликач
        self.all_btns.append(b)
        return b

    def draw_hero(self):
        w, h = self.size
        if w < 100 or h < 100:
            return
        c, core, app = self.hero, self.core, self.app
        for old in self.all_btns:
            old.destroy()
        c.delete("ui")
        self.btns, self.all_btns = {}, []
        name = core.current
        b = core.builds.get(name or "")
        x0, y0 = 48, max(30, int(h * .5 - 125))
        if b:
            self.T(x0, y0, "ПОТОЧНА ЗБІРКА", 10, C["accent"], True)
            self.T(x0, y0 + 40, name[:26], 30, "#ffffff", True, HF)
            meta = f"Minecraft {b['version']}  ·  {b['loader'].capitalize()}"
            if b["loader"] == "fabric":
                meta += f"  ·  {core.mod_count(name)} модів"
            self.T(x0, y0 + 84, f"{meta}  ·  {core.ram} МБ", 11, "#c8d0e6")
            ready = core.installed(name)
            self.pill(x0, y0 + 108, "Встановлено · готово до запуску" if ready else "Файли гри завантажаться при запуску",
                      C["ok"] if ready else C["warn"])
            y = y0 + 152
            self.btns["play"] = self.mk(c, x0, y, 240, 58, "▶  ГРАТИ", app.play, "primary", 15, "Запустити збірку (Enter)")
            bx = x0 + 258
            for text, cmd, wd, tip in (("Моди", lambda: app.mods_for(name), 96, "Встановити та керувати модами"),
                                       ("Тека", lambda: open_url(str(build_dir(name))), 90, "Відкрити папку збірки"),
                                       ("Фон…", lambda: app.choose_bg(name), 90, "Власний фон збірки")):
                self.mk(c, bx, y + 10, wd, 38, text, cmd, "ghost", 10, tip)
                bx += wd + 10
        else:
            self.T(x0, y0, "ЛАУНЧЕР ГОТОВИЙ", 10, C["accent"], True)
            self.T(x0, y0 + 40, "Створіть першу збірку", 28, "#ffffff", True, HF)
            self.T(x0, y0 + 84, "Збірка — окрема папка з модами, світами й налаштуваннями.", 11, "#c8d0e6")
            y = y0 + 124
            self.mk(c, x0, y, 200, 52, "Нова збірка", lambda: app.show("library"), "primary", 12)
            self.mk(c, x0 + 216, y + 6, 190, 40, "VOXEL Pack + мод", lambda: app.show("pack"), "ghost", 10)
        c.tag_raise("ui")
        self.sync()

    def sync(self):
        pb = self.btns.get("play")
        if pb:
            label = {"idle": "▶  ГРАТИ", "loading": "ЗАВАНТАЖЕННЯ…", "running": "ГРА ЗАПУЩЕНА"}[self.core.phase]
            pb.set(text=label, enabled=self.core.phase == "idle")

    def tick(self):
        self.sync()
        self.n += 1
        pb = self.btns.get("play")
        if pb and pb.enabled and ANIM[0] and self.n % 3 == 0:  # м'яке «дихання» неону (~10 к/с достатньо)
            p = .5 + .5 * math.sin(time.time() * 2.2)
            if abs(p - pb.pulse) > .04:
                pb.pulse = p
                pb.draw()
        if ANIM[0] and self.drops and self.n % 2 == 0:  # дощ ~15 к/с: менше перемальовувань великого полотна
            w, h = self.size
            for i, sp in self.drops:
                self.hero.move(i, -4.0 * sp, 20.0 * sp)
                co = self.hero.coords(i)
                if co and (co[1] > h or co[0] < -20):
                    x = random.randrange(w + 40)
                    self.hero.coords(i, x, -16, x - 4, -2)

    # ---------------------------------------------------------- фон
    def on_resize(self, e):
        if (e.width, e.height) == self.size:
            return  # <Configure> приходить і при простому зсуві (анімація сторінки) — не перемальовуємо
        self.size = (e.width, e.height)
        self.draw_hero()
        if self.job:
            self.after_cancel(self.job)
        self.job = self.after(150, self.request_bg)  # debounce: не рендеримо на кожен піксель

    def request_bg(self):
        w, h = self.size
        if w < 100 or h < 100:
            return
        style, custom, seed = self.core.scene_for(self.core.current)
        key = (style, custom, w // 8, h // 8)
        if key == self.bg_key:
            return
        self.bg_key = key

        def work():
            img = None
            if Image is not None and ImageTk is not None:
                for src in (custom, None):  # якщо власний файл зіпсовано — малюємо місто
                    try:
                        img = render_scene(w, h, style, seed, C["bg"], src)
                        break
                    except Exception:
                        continue
            frames = [Image.blend(Image.new("RGB", img.size, C["bg"]), img, t) for t in (.3, .6, .85)] \
                if img is not None and ANIM[0] else []
            self.app.post(lambda: self.apply_bg(img, key, frames))
        threading.Thread(target=work, daemon=True).start()

    def apply_bg(self, img, key, frames):
        if key != self.bg_key or not self.alive():
            return
        c = self.hero
        c.delete("bgimg")
        if img is None:  # запасний фон без Pillow
            w, h = self.size
            for i in range(24):
                c.create_rectangle(0, i * h / 24, w, (i + 1) * h / 24 + 1, outline="", tags="bgimg",
                                   fill=mix(mix(C["accent"], C["bg"], .85), C["bg"], i / 23))
            self.photos = []
        else:
            self.photos = [ImageTk.PhotoImage(f) for f in frames + [img]]
            self.item = c.create_image(0, 0, image=self.photos[0], anchor="nw", tags="bgimg")
            for n in range(1, len(self.photos)):  # плавна поява фону
                c.after(n * 45, lambda ph=self.photos[n]: self.show_frame(ph))
        c.tag_lower("bgimg")
        self.make_rain()
        c.tag_raise("ui")

    def show_frame(self, ph):
        try:
            self.hero.itemconfig(self.item, image=ph)
        except tk.TclError:
            pass

    def make_rain(self):
        c = self.hero
        c.delete("rain")
        self.drops = []
        w, h = self.size
        if not ANIM[0] or w < 100:
            return
        for _ in range(26):
            x, y, sp = random.randrange(w), random.randrange(h), random.uniform(.7, 1.4)
            self.drops.append((c.create_line(x, y, x - 4, y + 14, fill="#5d7aa8", tags="rain"), sp))
        c.tag_raise("rain", "bgimg")

    # ---------------------------------------------------------- решта
    def refresh(self):
        self.bg_key = None if self.core.current != getattr(self, "_cur", None) or self.size == (0, 0) else self.bg_key
        self._cur = self.core.current
        self.draw_hero()
        self.fill_chips()
        self.fill_account()
        self.fill_system()
        self.request_bg()

    def fill_chips(self):
        clear(self.chips)
        lbl(self.chips, "ЗБІРКИ", 9, C["dim"], True).pack(side="left", padx=(0, 10), pady=10)
        for i, n in enumerate(list(self.core.builds)[:9]):
            NeonButton(self.chips, f"{i + 1}  {n[:14]}", lambda n=n: self.app.select_build(n),
                       40 + 7 * min(len(n), 14), 28, "chip_on" if n == self.core.current else "chip", 9,
                       "Клавіша " + str(i + 1)).pack(side="left", padx=1, pady=8)
        NeonButton(self.chips, "+ Нова", lambda: self.app.show("library"), 80, 28, "ghost", 9).pack(side="left", padx=4)

    def fill_account(self):
        f, core = clear(self.cards[0].inner), self.core
        if core.account:
            lbl(f, "● " + core.account["name"], 13, C["ok"], True).pack(anchor="w")
            lbl(f, "Microsoft-акаунт · онлайн", 9, C["dim"]).pack(anchor="w", pady=(2, 10))
            NeonButton(f, "Вийти", self.app.logout, 100, 30, "ghost", 9).pack(anchor="w")
            return
        lbl(f, "Офлайн-режим", 12, C["text"], True).pack(anchor="w")
        lbl(f, "Нік для одиночної гри", 9, C["dim"]).pack(anchor="w", pady=(2, 4))
        e = entry(f, 20)
        e.insert(0, core.get("nick", "Player"))
        e.pack(anchor="w", fill="x")
        save = lambda ev=None: core.set("nick", e.get().strip())
        e.bind("<FocusOut>", save)
        e.bind("<Return>", save)
        NeonButton(f, "Увійти через Microsoft", self.app.ms_login, 190, 30, "ghost", 9).pack(anchor="w", pady=(10, 0))

    def fill_system(self):
        f = clear(self.cards[1].inner)
        for k, v, ok in self.core.system_info():
            r = tk.Frame(f, bg=C["panel"])
            r.pack(fill="x", pady=1)
            lbl(r, k, 9, C["dim"]).pack(side="left")
            lbl(r, v, 9, C["text"] if ok else C["warn"], wraplength=200, justify="right").pack(side="right")


class LibraryPage(Page):
    def __init__(self, app):
        super().__init__(app)
        self.names, self.loader = [], "fabric"
        for i, wt in enumerate((2, 3)):
            self.grid_columnconfigure(i, weight=wt, uniform="lib")
        self.grid_rowconfigure(1, weight=1)
        header(self, "Збірки", "Кожна збірка — окрема папка з модами, світами та налаштуваннями").grid(
            row=0, column=0, columnspan=2, sticky="ew", padx=28, pady=(24, 10))
        left = tk.Frame(self, bg=C["bg"])
        left.grid(row=1, column=0, sticky="nsew", padx=(28, 10), pady=(0, 20))
        right = tk.Frame(self, bg=C["bg"])
        right.grid(row=1, column=1, sticky="nsew", padx=(10, 28), pady=(0, 20))
        self.lb = listbox(left, 8)
        self.lb.pack(fill="both", expand=True, pady=(0, 12))
        self.lb.bind("<<ListboxSelect>>", lambda e: self.show_detail())
        form = card(left, "Нова збірка")
        form.pack(fill="x")
        f = form.inner
        self.name = entry(f, 24)
        self.name.pack(fill="x")
        Tooltip(self.name, "До 32 символів: літери, цифри, пробіл, . _ -").bind()
        r = tk.Frame(f, bg=C["panel"])
        r.pack(fill="x", pady=8)
        self.ver = tk.StringVar()
        self.menu_box = tk.Frame(r, bg=C["panel"])
        self.menu_box.pack(side="left")
        Segmented(r, [("fabric", "Fabric"), ("vanilla", "Vanilla")], "fabric",
                  lambda v: setattr(self, "loader", v)).pack(side="left", padx=8)
        r2 = tk.Frame(f, bg=C["panel"])
        r2.pack(fill="x")
        self.auto = Switch(r2, False, None, "Автоматично ставити Sodium, Lithium та ін. при запуску")
        self.auto.pack(side="left")
        lbl(r2, "Авто-оптимізація", 10, C["text"]).pack(side="left", padx=8)
        NeonButton(f, "Створити збірку", self.create, 170, 34, "primary", 10).pack(anchor="w", pady=(10, 0))
        self.msg = lbl(f, "", 9, C["warn"], wraplength=300, justify="left")
        self.msg.pack(anchor="w", pady=(6, 0))
        self.detail = card(right, "Деталі")
        self.detail.pack(fill="both", expand=True)

    def refresh(self):
        core = self.core
        self.names = list(core.builds)
        self.lb.delete(0, "end")
        for n in self.names:
            b = core.builds[n]
            self.lb.insert("end", f"{'●' if n == core.current else ' '} {n[:24]:<24} {b['version']:<8} {b['loader']}")
        if self.names:
            self.lb.selection_set(self.names.index(core.current) if core.current in self.names else 0)
        vers = core.versions or ["1.20.1"]
        clear(self.menu_box)
        if self.ver.get() not in vers:
            self.ver.set(vers[0])
        menu(self.menu_box, self.ver, vers).pack()
        self.show_detail()

    def sel(self):
        s = self.lb.curselection()
        return self.names[s[0]] if s and s[0] < len(self.names) else None

    def create(self):
        err = self.core.create_build(self.name.get(), self.ver.get(), self.loader, self.auto.on)
        if err:
            return self.msg.config(text=err)
        self.core.select(self.name.get().strip())
        self.name.delete(0, "end")
        self.msg.config(text="")
        self.app.refresh_all()

    def show_detail(self):
        f, core, n = clear(self.detail.inner), self.core, self.sel()
        if not n:
            lbl(f, "Збірок ще немає", 14, C["text"], True).pack(anchor="w")
            lbl(f, "Створіть першу ліворуч або встановіть готовий VOXEL Pack.", 10, C["dim"]).pack(anchor="w", pady=6)
            NeonButton(f, "VOXEL Pack + мод", lambda: self.app.show("pack"), 180, 34, "primary", 10).pack(anchor="w", pady=8)
            return
        b = core.builds[n]
        lbl(f, n, 20, "#ffffff", True, HF).pack(anchor="w")
        lbl(f, f"Minecraft {b['version']}  ·  {b['loader']}  ·  {core.mod_count(n)} модів", 10, C["dim"]).pack(anchor="w", pady=(2, 6))
        ok = core.installed(n)
        lbl(f, "● Встановлено" if ok else "● Завантажиться при першому запуску", 10, C["ok"] if ok else C["warn"], True).pack(anchor="w")
        lbl(f, str(build_dir(n)), 9, C["dim"], wraplength=380, justify="left").pack(anchor="w", pady=6)
        row = tk.Frame(f, bg=C["panel"])
        row.pack(anchor="w", pady=(8, 0))
        for text, cmd, w, kind in (("Обрати й грати", lambda: self.app.play_build(n), 150, "primary"),
                                   ("Моди", lambda: self.app.mods_for(n), 80, "ghost"),
                                   ("Тека", lambda: open_url(str(build_dir(n))), 70, "ghost")):
            NeonButton(row, text, cmd, w, 34, kind, 10).pack(side="left", padx=2)
        row2 = tk.Frame(f, bg=C["panel"])
        row2.pack(anchor="w", pady=4)
        NeonButton(row2, "Фон…", lambda: self.app.choose_bg(n), 80, 32, "ghost", 9, "Власне зображення (PNG/JPG/WebP)").pack(side="left", padx=2)
        if b.get("bg"):
            NeonButton(row2, "Скинути фон", lambda: self.app.reset_bg(n), 120, 32, "ghost", 9).pack(side="left", padx=2)
        NeonButton(row2, "Видалити", lambda: self.app.delete_build(n), 100, 32, "danger", 9).pack(side="left", padx=2)


class ModsPage(Page):
    def __init__(self, app):
        super().__init__(app)
        self.build, self.src, self.res, self.inst = tk.StringVar(), "Modrinth", [], []
        for i, wt in enumerate((3, 2)):
            self.grid_columnconfigure(i, weight=wt, uniform="m")
        self.grid_rowconfigure(2, weight=1)
        header(self, "Моди", "Modrinth та CurseForge · обов'язкові залежності ставляться автоматично").grid(
            row=0, column=0, columnspan=2, sticky="ew", padx=28, pady=(24, 8))
        bar = tk.Frame(self, bg=C["bg"])
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", padx=28, pady=(0, 10))
        lbl(bar, "Збірка", 9, C["dim"], True).pack(side="left", padx=(0, 8))
        self.menu_box = tk.Frame(bar, bg=C["bg"])
        self.menu_box.pack(side="left")
        Segmented(bar, [("Modrinth", "Modrinth"), ("CurseForge", "CurseForge")], "Modrinth",
                  lambda v: setattr(self, "src", v)).pack(side="left", padx=14)
        self.q = entry(bar, 30)
        self.q.pack(side="left", ipady=4)
        self.q.bind("<Return>", lambda e: self.search())
        NeonButton(bar, "Пошук", self.search, 90, 32, "primary", 10).pack(side="left", padx=8)
        left, right = card(self, "Каталог"), card(self, "Встановлені")
        left.grid(row=2, column=0, sticky="nsew", padx=(28, 8), pady=(0, 8))
        right.grid(row=2, column=1, sticky="nsew", padx=(8, 28), pady=(0, 8))
        self.lb = listbox(left.inner, 12)
        self.lb.pack(fill="both", expand=True)
        self.lb.bind("<<ListboxSelect>>", self.on_select)
        self.info = lbl(left.inner, "", 9, C["dim"], wraplength=420, justify="left")
        self.info.pack(anchor="w", pady=6)
        r = tk.Frame(left.inner, bg=C["panel"])
        r.pack(anchor="w")
        NeonButton(r, "Встановити", self.install, 130, 34, "primary", 10).pack(side="left")
        NeonButton(r, "Сторінка", self.page, 100, 34, "ghost", 10).pack(side="left", padx=6)
        self.il = listbox(right.inner, 12)
        self.il.pack(fill="both", expand=True)
        r2 = tk.Frame(right.inner, bg=C["panel"])
        r2.pack(anchor="w", pady=(8, 0))
        NeonButton(r2, "Вимк/Увімк", self.toggle, 120, 34, "ghost", 9, "Вимкнений мод не завантажується грою").pack(side="left")
        NeonButton(r2, "Видалити", self.remove, 100, 34, "danger", 9).pack(side="left", padx=6)
        self.msg = lbl(self, "Порожній пошук покаже найпопулярніші моди для версії збірки", 10, C["dim"])
        self.msg.grid(row=3, column=0, columnspan=2, sticky="w", padx=28, pady=(0, 12))

    def refresh(self):
        names = list(self.core.builds)
        clear(self.menu_box)
        if self.build.get() not in names:
            self.build.set(self.core.current if self.core.current in names else (names[0] if names else ""))
        if names:
            menu(self.menu_box, self.build, names, lambda v: self.show_installed()).pack()
        else:
            lbl(self.menu_box, "немає збірок", 10, C["warn"]).pack()
        self.show_installed()

    def cur(self):
        return self.core.builds.get(self.build.get())

    def provider(self):
        if self.src == "Modrinth":
            return Modrinth()
        key = (self.core.get("cf_key") or os.environ.get("CURSEFORGE_API_KEY")
               or prompt(self.app, "CurseForge API", "Вставте безкоштовний API-ключ (console.curseforge.com):", True) or "").strip()
        if not key:
            return None
        self.core.set("cf_key", key)
        return CurseForge(key)

    def sel(self):
        s = self.lb.curselection()
        return self.res[s[0]] if s and s[0] < len(self.res) else None

    def search(self):
        b = self.cur()
        if not b:
            return self.say("Спершу створіть збірку (розділ «Збірки»)")
        prov, q = self.provider(), self.q.get().strip()
        if not prov:
            return
        self.say("Пошук…")

        def work():
            try:
                res = prov.search(q, b["version"])
            except Exception as e:
                if isinstance(prov, CurseForge) and any(c in str(e) for c in ("401", "403")):
                    self.core.cfg.pop("cf_key", None)  # хибний ключ — спитаємо знову
                return self.say("Помилка пошуку: " + net_err(e))
            self.app.post(lambda: self.show(res))
        threading.Thread(target=work, daemon=True).start()

    def show(self, res):
        if not self.alive():
            return
        self.res = res
        self.lb.delete(0, "end")
        for r in res:
            n = r["downloads"]
            short = f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}K" if n >= 1e3 else str(n)
            self.lb.insert("end", f"{r['title'][:30]:<30} {short:>7} {r['author'][:12]}" + (" 🔒" if r["blocked"] else ""))
        self.say(f"Знайдено: {len(res)}" if res else "Нічого не знайдено для цієї версії та Fabric")

    def on_select(self, _=None):
        r = self.sel()
        if r:
            self.info.config(text=r["desc"][:280] + ("\n🔒 Автор дозволяє завантаження лише з сайту." if r["blocked"] else ""))

    def page(self):
        r = self.sel()
        if r:
            open_url(r["page"])

    def install(self):
        name, r, b = self.build.get(), self.sel(), self.cur()
        if not (b and r):
            return self.say("Оберіть мод зі списку")
        if b["loader"] != "fabric":
            return self.say("Ця збірка vanilla — моди потребують Fabric (створіть збірку з Fabric)")
        prov = self.provider()
        if not prov:
            return
        mods = build_dir(name) / "mods"
        self.say(f"Встановлення {r['title']}…")

        def work():
            try:
                done = install_mod(prov, r["id"], b["version"], mods)
            except ManualDownload as e:
                open_url(e.page)
                return self.say("Автор дозволяє лише завантаження з сайту — відкрив сторінку")
            except Exception as e:
                return self.say("Помилка: " + net_err(e))
            self.say("Встановлено: " + ", ".join(done))
            self.app.post(self.show_installed)
        threading.Thread(target=work, daemon=True).start()

    def show_installed(self):
        if not self.alive():
            return
        self.inst = self.core.list_mods(self.build.get()) if self.build.get() in self.core.builds else []
        self.il.delete(0, "end")
        for fn, on in self.inst:
            self.il.insert("end", ("✔ " if on else "✘ ") + fn.replace(".disabled", "  (вимкнено)"))

    def pick_inst(self):
        s = self.il.curselection()
        return self.inst[s[0]][0] if s and s[0] < len(self.inst) else None

    def toggle(self):
        fn = self.pick_inst()
        if fn:
            err = self.core.toggle_mod(self.build.get(), fn)
            if err:
                self.say(err)
            self.show_installed()
            self.app.refresh_home()

    def remove(self):
        fn = self.pick_inst()
        if fn and confirm(self.app, "Видалити мод", f"Видалити файл {fn}?", "Видалити", "Скасувати", True):
            self.core.remove_mod(self.build.get(), fn)
            self.show_installed()
            self.app.refresh_home()


class PackPage(Page):
    def __init__(self, app):
        super().__init__(app)
        self.grid_columnconfigure(0, weight=1)
        header(self, "VOXEL Pack", "Готова збірка: Fabric, оптимізаційні моди й наш власний мод").grid(
            row=0, column=0, sticky="ew", padx=28, pady=(24, 10))
        c = card(self, "Що входить")
        c.grid(row=1, column=0, sticky="ew", padx=28, pady=(0, 12))
        for line in ("▸ Fabric API, Sodium, Lithium, FerriteCore, ModernFix, ImmediatelyFast (Modrinth)",
                     "▸ Voxel Boost — екран налаштувань у стилі Sodium (Right Shift), зібраний вами .jar",
                     "▸ Створюється окрема збірка «VOXEL Pack <версія>». Voxel Boost зараз зібраний лише для 1.20.1"):
            lbl(c.inner, line, 10, C["text"]).pack(anchor="w", pady=2)
        f = card(self, "Установка")
        f.grid(row=2, column=0, sticky="ew", padx=28)
        r = tk.Frame(f.inner, bg=C["panel"])
        r.pack(anchor="w")
        lbl(r, "Версія Minecraft", 10, C["dim"]).pack(side="left", padx=(0, 8))
        self.ver = tk.StringVar()
        self.menu_box = tk.Frame(r, bg=C["panel"])
        self.menu_box.pack(side="left")
        r2 = tk.Frame(f.inner, bg=C["panel"])
        r2.pack(anchor="w", pady=10)
        self.own = Switch(r2, True, None, "Потрібен ваш voxelboost-*.jar (див. інструкцію зі збірки мода)")
        self.own.pack(side="left")
        lbl(r2, "Додати Voxel Boost", 10, C["text"]).pack(side="left", padx=8)
        self.btn = NeonButton(f.inner, "Встановити VOXEL Pack", self.install, 220, 38, "primary", 11)
        self.btn.pack(anchor="w")
        self.msg = lbl(f.inner, "", 10, C["dim"], wraplength=760, justify="left")
        self.msg.pack(anchor="w", pady=(10, 0))

    def refresh(self):
        vers = self.core.versions or ["1.20.1"]
        clear(self.menu_box)
        if self.ver.get() not in vers:
            self.ver.set("1.20.1" if "1.20.1" in vers else vers[0])
        menu(self.menu_box, self.ver, vers).pack()

    def install(self):
        mc, own = self.ver.get(), None
        if self.own.on:
            own = find_own_jar(mc)
            if not own:
                p = filedialog.askopenfilename(parent=self.app, title="Оберіть voxelboost-….jar", filetypes=[("JAR", "*.jar")])
                own = Path(p) if p else None
        self.btn.set(enabled=False)
        self.say("Встановлення…")

        def work():
            try:
                name, notes = install_pack(self.core.cfg, mc, self.core.progress, own, self.own.on)
            except Exception as e:
                self.app.post(lambda: self.btn.set(enabled=True))
                return self.say("Помилка: " + net_err(e))

            def done():
                self.core.select(name)
                self.btn.set(enabled=True)
                self.app.refresh_all()
                self.msg.config(text=f"Готово: збірку «{name}» обрано." + "".join("\n⚠ " + n for n in notes[:4]))
            self.app.post(done)
        threading.Thread(target=work, daemon=True).start()


class LogsPage(Page):
    def __init__(self, app):
        super().__init__(app)
        self.build = tk.StringVar()
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        header(self, "Журнал", "Вивід останнього запуску вибраної збірки — тут видно причини збоїв").grid(
            row=0, column=0, sticky="ew", padx=28, pady=(24, 8))
        bar = tk.Frame(self, bg=C["bg"])
        bar.grid(row=1, column=0, sticky="ew", padx=28, pady=(0, 8))
        self.menu_box = tk.Frame(bar, bg=C["bg"])
        self.menu_box.pack(side="left")
        NeonButton(bar, "Оновити", self.load, 100, 32, "ghost", 10).pack(side="left", padx=8)
        NeonButton(bar, "Тека логів", lambda: open_url(str(STATE / "logs")), 120, 32, "ghost", 10).pack(side="left")
        wrap = tk.Frame(self, bg=C["bg"])
        wrap.grid(row=2, column=0, sticky="nsew", padx=28, pady=(0, 16))
        self.t = tk.Text(wrap, bg=C["panel"], fg=C["text"], font=(MONO, 9), bd=0, highlightthickness=1,
                         highlightbackground=C["line"], wrap="none", state="disabled", insertbackground=C["accent"])
        sb = tk.Scrollbar(wrap, command=self.t.yview, bg=C["panel2"], troughcolor=C["bg"], bd=0, width=10)
        self.t.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.t.pack(side="left", fill="both", expand=True)
        self.t.tag_config("err", foreground=C["bad"])
        self.msg = lbl(self, "", 9, C["dim"])

    def refresh(self):
        names = list(self.core.builds)
        clear(self.menu_box)
        if self.build.get() not in names:
            self.build.set(self.core.current if self.core.current in names else (names[0] if names else ""))
        if names:
            menu(self.menu_box, self.build, names, lambda v: self.load()).pack()
        self.load()

    def load(self):
        text = self.core.read_log(self.build.get()) if self.build.get() else None
        self.t.config(state="normal")
        self.t.delete("1.0", "end")
        if text is None:
            self.t.insert("end", "Журналу ще немає — запустіть гру цією збіркою.")
        for line in (text or "").splitlines(True):
            self.t.insert("end", line, "err" if any(k in line for k in ("ERROR", "Exception", "FATAL")) else ())
        self.t.config(state="disabled")
        self.t.see("end")


class SettingsPage(Page):
    def __init__(self, app):
        super().__init__(app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        header(self, "Налаштування", "Зміни зберігаються одразу").grid(row=0, column=0, sticky="ew", padx=28, pady=(24, 8))
        self.sf = ScrollFrame(self)
        self.sf.grid(row=1, column=0, sticky="nsew", padx=(28, 12), pady=(0, 12))
        self.build()

    def refresh(self):
        clear(self.sf.inner)
        self.build()

    def sw(self, f, title, desc, key, default=False, check=None, after=None):
        core = self.core
        r = setting_row(f, title, desc)

        def on(v):
            if v and check:
                err = check()
                if err:
                    core.progress(err)
                    return False
            core.set(key, v)
            if after:
                after(v)
        Switch(r, core.get(key, default), on).pack()

    def build(self):
        core, app, inner = self.core, self.app, self.sf.inner

        def box(title):
            c = card(inner, title)
            c.pack(fill="x", pady=6)
            return c.inner
        # --- акаунт
        f = box("Акаунт")
        if core.account:
            r = setting_row(f, f"Microsoft: {core.account['name']}", "Онлайн-режим. Токен — у Secret Service (libsecret) або у файлі з правами 600.")
            NeonButton(r, "Вийти", app.logout, 100, 32, "ghost", 9).pack()
        else:
            r = setting_row(f, "Microsoft-акаунт", "Вхід у браузері одним кліком. Без входу працює офлайн-режим.")
            NeonButton(r, "Увійти", app.ms_login, 100, 32, "primary", 9).pack()
        r = setting_row(f, "Нік (офлайн)", "3–16 символів: латиниця, цифри, _")
        e = entry(r, 18)
        e.insert(0, core.get("nick", "Player"))
        e.pack()
        e.bind("<FocusOut>", lambda ev: core.set("nick", e.get().strip()))
        r = setting_row(f, "Azure Client ID (необов'язково)", "Порожнє — вбудований ID застосунку VoxelLauncherPersonal")
        e2 = entry(r, 34)
        e2.insert(0, core.get("client_id", ""))
        e2.pack()
        e2.bind("<FocusOut>", lambda ev: core.set("client_id", e2.get().strip()))
        # --- гра
        f = box("Гра")
        r = setting_row(f, "Оперативна пам'ять", "Значення -Xmx. Для збірок з модами зазвичай 4–6 ГБ")
        lab = lbl(r, f"{core.ram} МБ", 10, C["accent"], True)
        lab.pack(side="right", padx=(8, 0))
        sc = tk.Scale(r, from_=1024, to=core.max_ram, resolution=256, orient="horizontal", length=220, showvalue=False,
                      bg=C["panel"], fg=C["text"], troughcolor=C["bg"], highlightthickness=0, bd=0,
                      sliderrelief="flat", activebackground=C["accent"],
                      command=lambda v: lab.config(text=f"{int(float(v))} МБ"))
        sc.set(core.ram)
        sc.bind("<ButtonRelease-1>", lambda ev: core.set("ram", int(sc.get())))
        sc.pack(side="right")
        self.sw(f, "Оптимізація", "G1GC-прапори Aikar, mesa_glthread та авто-моди для збірок з відповідним прапорцем", "optimize", True)
        self.sw(f, "Спільні resourcepacks і shaderpacks", "Одна папка на всі збірки (симлінки). Світи не діляться", "share")
        # --- linux
        f = box("Arch Linux")
        r = setting_row(f, "Відеокарта", "Для гібридної графіки: PRIME offload NVIDIA або DRI_PRIME для Mesa")
        Segmented(r, [("auto", "Авто"), ("nvidia", "NVIDIA"), ("mesa", "DRI_PRIME")], core.get("gpu", "auto"),
                  lambda v: core.set("gpu", v)).pack()
        self.sw(f, "Системні GLFW + OpenAL", "Нативний Wayland і звук через PipeWire (pacman -S glfw openal)", "sysl", False,
                lambda: None if lwjgl_flags() else "Немає бібліотек: sudo pacman -S glfw openal")
        self.sw(f, "GameMode", "Режим продуктивності на час гри (pacman -S gamemode)", "gamemode", False,
                lambda: None if shutil.which("gamemoderun") else "Немає gamemoderun: sudo pacman -S gamemode")
        self.sw(f, "MangoHud", "Оверлей FPS та температур (pacman -S mangohud)", "mangohud", False,
                lambda: None if shutil.which("mangohud") else "Немає mangohud: sudo pacman -S mangohud")
        # --- вигляд
        f = box("Вигляд")
        r = setting_row(f, "Акцентний колір", "Застосовується одразу")
        Segmented(r, [("cyan", "Cyan"), ("violet", "Violet"), ("magenta", "Magenta")], core.get("accent", "cyan"),
                  app.set_accent).pack()
        self.sw(f, "Анімації", "Дощ, плавні переходи, підсвічування. Вимикається автоматично, якщо в системі обрано «менше руху»",
                "anim", True, after=lambda v: ANIM.__setitem__(0, v))
        # --- інтеграції
        f = box("Інтеграції")
        r = setting_row(f, "CurseForge API-ключ", "Безкоштовний, з console.curseforge.com. Для Modrinth ключ не потрібен")
        e3 = entry(r, 34, "•")
        e3.insert(0, core.get("cf_key", ""))
        e3.pack()
        e3.bind("<FocusOut>", lambda ev: core.set("cf_key", e3.get().strip()))


PAGES = {"home": HomePage, "library": LibraryPage, "mods": ModsPage, "pack": PackPage, "logs": LogsPage,
         "settings": SettingsPage}


# ================================================================ GUI: застосунок
class App(tk.Tk):
    def __init__(self, core):
        super().__init__(className="voxel-launcher")
        self.core = core
        set_theme(core.get("accent", "cyan"))
        init_fonts()
        ANIM[0] = bool(core.get("anim", True)) and not NO_ANIM
        threading.Thread(target=self.check_motion, daemon=True).start()  # gsettings не блокує старт
        self.title("VOXEL Launcher")
        self.geometry(core.get("geometry") or "1180x740")
        self.minsize(1000, 660)
        self.configure(bg=C["bg"])
        self.q, self.disp, self.pages, self.page_name, self.nav = queue.Queue(), 0.0, {}, None, {}
        self.post = self.q.put
        core.post, core.ask = self.post, self.ask
        self.build_shell()
        for i, (n, _, _) in enumerate(NAV):
            self.bind(f"<Control-Key-{i + 1}>", lambda e, n=n: self.show(n))
        self.bind("<Key>", self.on_key)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.show("home", animate=False)
        core.load_versions(self.refresh_all)
        core.load_gpus(self.refresh_all)
        core.refresh_login(self.refresh_all)
        self.tick()

    def check_motion(self):
        if ANIM[0] and reduced_motion():
            self.post(lambda: ANIM.__setitem__(0, False))

    def report_callback_exception(self, exc, val, tb):
        traceback.print_exception(exc, val, tb)
        self.core.progress(f"Помилка інтерфейсу: {val}"[:100])  # показуємо в рядку стану, а не мовчимо

    # ---------------------------------------------------------- оболонка
    def build_shell(self):
        self.shell = tk.Frame(self, bg=C["bg"])
        self.shell.pack(fill="both", expand=True)
        side = tk.Frame(self.shell, bg=C["bg2"], width=232, highlightthickness=1, highlightbackground=C["line"])
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        logo = tk.Canvas(side, height=92, bg=C["bg2"], highlightthickness=0)
        logo.pack(fill="x")
        logo.create_text(22, 40, text="VOXEL", anchor="w", fill="#ffffff", font=(HF, 24, "bold"))
        logo.create_rectangle(22, 60, 86, 63, fill=C["accent"], outline="")
        logo.create_text(22, 76, text="ARCH LINUX LAUNCHER", anchor="w", fill=C["dim"], font=(UIF, 8, "bold"))
        for n, text, icon in NAV:
            it = NavItem(side, icon, text, lambda n=n: self.show(n))
            it.pack(fill="x", pady=1)
            self.nav[n] = it
        self.acc_chip = lbl(side, "", 9, C["dim"], wraplength=200, justify="left")
        self.acc_chip.pack(side="bottom", anchor="w", padx=20, pady=16)
        main = tk.Frame(self.shell, bg=C["bg"])
        main.pack(side="left", fill="both", expand=True)
        self.statusbar = StatusBar(main)
        self.statusbar.pack(side="bottom", fill="x")
        self.stage = tk.Frame(main, bg=C["bg"])
        self.stage.pack(fill="both", expand=True)
        self.update_chip()

    def update_chip(self):
        a = self.core.account
        self.acc_chip.config(text=f"● {a['name']}\nMicrosoft · онлайн" if a else "○ Офлайн-режим\nнік: " + self.core.get("nick", "Player"),
                             fg=C["ok"] if a else C["dim"])

    def tween(self, ms, fn):
        t0 = time.time()

        def step():
            t = min((time.time() - t0) * 1000 / ms, 1.0)
            fn(1 - (1 - t) ** 3)
            if t < 1:
                self.after(16, step)
        step()

    def show(self, name, animate=True):
        page = self.pages.get(name)
        if page is None:
            page = self.pages[name] = PAGES[name](self)
        old = self.pages.get(self.page_name)
        if old is not None and old is not page:
            old.place_forget()
        for n, it in self.nav.items():
            it.set_active(n == name)
        self.page_name = name
        page.refresh()
        if animate and ANIM[0] and old is not page:  # плавний виїзд сторінки
            self.tween(180, lambda t: page.place(x=int(36 * (1 - t)), y=0, relwidth=1, relheight=1))
        else:
            page.place(x=0, y=0, relwidth=1, relheight=1)

    def rebuild(self):
        cur = self.page_name or "home"
        self.shell.destroy()
        self.pages, self.page_name, self.nav = {}, None, {}
        self.configure(bg=C["bg"])
        self.build_shell()
        self.show(cur, animate=False)

    def refresh_all(self):
        for p in self.pages.values():
            p.refresh()
        self.update_chip()

    def refresh_home(self):
        if "home" in self.pages:
            self.pages["home"].refresh()

    def tick(self):
        t0 = time.time()
        try:
            try:
                while True:
                    fn = self.q.get_nowait()
                    try:
                        fn()
                    except Exception:
                        traceback.print_exc()  # помилка в колбеку не має зупиняти цикл інтерфейсу
            except queue.Empty:
                pass
            target = self.core.pct
            self.disp = target if (not ANIM[0] or abs(target - self.disp) < .002) else self.disp + (target - self.disp) * .25
            self.statusbar.update_state(self.core.status, self.disp)
            page = self.pages.get(self.page_name)
            if page is not None and hasattr(page, "tick"):
                page.tick()
        except Exception:
            traceback.print_exc()  # цикл не має вмирати від помилки в сторінці
        finally:
            if DEBUG:
                dt = time.time() - t0
                if dt > .12:
                    print(f"[voxel] повільний кадр: {dt * 1000:.0f} мс (сторінка: {self.page_name})", file=sys.stderr)
                try:
                    faulthandler.cancel_dump_traceback_later()
                    faulthandler.dump_traceback_later(6)  # якщо tick не викликався 6 с — стек усіх потоків у stderr
                except (RuntimeError, OSError, ValueError, AttributeError):
                    pass  # stderr без файлового дескриптора (запуск без терміналу)
            self.after(33, self.tick)

    # ---------------------------------------------------------- дії
    def ask(self, title, msg):
        """Запитання з фонового потоку: діалог відкривається в головному."""
        ev, res = threading.Event(), [False]

        def show():
            res[0] = confirm(self, title, msg)
            ev.set()
        self.post(show)
        ev.wait()
        return res[0]

    def play(self):
        err = self.core.launch(self.core.current, self.refresh_home)
        if err:
            self.statusbar.update_state(err, 0)

    def select_build(self, name):
        self.core.select(name)
        self.refresh_all()

    def play_build(self, name):
        self.select_build(name)
        self.show("home")

    def mods_for(self, name):
        self.core.select(name)
        self.show("mods")
        self.pages["mods"].build.set(name)
        self.pages["mods"].show_installed()

    def choose_bg(self, name):
        p = filedialog.askopenfilename(parent=self, title="Фон збірки", filetypes=[
            ("Зображення", "*.png *.jpg *.jpeg *.webp *.bmp"), ("Усі файли", "*.*")])
        if p:
            err = self.core.set_bg(name, p)
            self.core.progress(err or "Фон збірки змінено")
            self.refresh_all()

    def reset_bg(self, name):
        self.core.set_bg(name, None)
        self.core.progress("Фон скинуто — використовується стандартне місто")
        self.refresh_all()

    def delete_build(self, name):
        if confirm(self, "Видалити збірку", f"Видалити «{name}» разом зі світами, налаштуваннями й модами?\nЦе незворотно.",
                   "Видалити", "Скасувати", True):
            self.core.delete_build(name)
            self.refresh_all()

    def set_accent(self, name):
        self.core.set("accent", name)
        set_theme(name)
        self.rebuild()

    def ms_login(self):
        """Вхід одним кліком: браузер → автоматичне повернення. Кнопка «Вручну» — запасний варіант."""
        core = self.core
        if getattr(self, "_login_busy", False):
            return
        cid, cancel, out = core.client_id(), threading.Event(), {}
        dlg = Modal(self, "Вхід у Microsoft",
                    "Відкрито браузер — увійдіть там, лаунчер сам підхопить результат.\n\n"
                    "Не відкрилось або браузер показує помилку redirect? Натисніть «Вручну».",
                    ok="Вручну", cancel="Скасувати")

        def work():
            try:
                core.progress("Вхід у Microsoft: чекаю на браузер…")
                core.login_browser(cid, cancel)
                out["ok"] = True
            except Exception as e:
                out["err"] = net_err(e)
            self.post(lambda: dlg.winfo_exists() and dlg.destroy())
        self._login_busy = True
        threading.Thread(target=work, daemon=True).start()
        self.wait_window(dlg)
        cancel.set()
        self._login_busy = False
        if out.get("ok"):
            core.progress("Вхід виконано")
            self.refresh_all()
        elif dlg.result is True:
            self.ms_login_manual(cid)
        elif out.get("err") and "скасовано" not in out["err"]:
            core.progress(f"Помилка входу: {out['err']}")
        else:
            core.progress("Вхід скасовано")

    def ms_login_manual(self, cid):
        core = self.core
        try:
            url, state, verifier = core.login_url(cid)
        except Exception as e:
            return core.progress(f"Помилка: {e}"[:100])
        open_url(url)
        back = prompt(self, "Вхід у Microsoft", "Після входу відкриється порожня сторінка. Скопіюйте ПОВНИЙ URL з адресного рядка й вставте сюди:")
        if not back:
            return

        def work():
            try:
                core.progress("Вхід у Microsoft…")
                core.login_finish(cid, back, state, verifier)
                core.progress("Вхід виконано")
                self.post(self.refresh_all)
            except Exception as e:
                core.progress(f"Помилка входу: {net_err(e)}")
        threading.Thread(target=work, daemon=True).start()

    def logout(self):
        if confirm(self, "Вийти з акаунта", "Токен буде видалено. Далі гра запускатиметься в офлайн-режимі."):
            self.core.logout()
            self.refresh_all()

    def on_key(self, e):
        if isinstance(self.focus_get(), (tk.Entry, tk.Text, tk.Listbox)) or self.page_name != "home":
            return
        if e.char and e.char in "123456789":
            names = list(self.core.builds)
            if int(e.char) - 1 < len(names):
                self.select_build(names[int(e.char) - 1])
        elif e.keysym == "Return":
            self.play()

    def close(self):
        try:
            self.core.set("geometry", f"{self.winfo_width()}x{self.winfo_height()}")
        finally:
            self.destroy()




def main():
    if "--doctor" in sys.argv:
        return doctor()
    if "--install-pack" in sys.argv:
        return cli_pack(sys.argv)
    if not is_arch():
        sys.exit("Цей лаунчер працює лише на Arch Linux та похідних (/etc/arch-release).")
    missing = [m for m, v in (("tk (sudo pacman -S tk)", HAVE_TK), ("python-requests (sudo pacman -S python-requests)", requests),
                              ("python-minecraft-launcher-lib (yay -S python-minecraft-launcher-lib)", mll)) if not v]
    if missing:
        sys.exit("Не вистачає: " + ", ".join(missing) + "\nПеревірка: voxel-launcher --doctor")
    if DEBUG:
        try:
            faulthandler.enable()
        except (RuntimeError, OSError, ValueError, AttributeError):
            pass
        print("[voxel] налагодження: якщо інтерфейс зависне на 6+ с, нижче з'явиться стек усіх потоків", file=sys.stderr)
    App(Core()).mainloop()


if __name__ == "__main__":
    main()

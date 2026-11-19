"""Oversub 沉浸字幕
抓系统播放的声音 -> 本机 Whisper 识别中文 -> 本地离线翻译 -> 悬浮窗显示英文字幕
不联网、不花钱；在线引擎留了接口，填了 key 就能切过去。
"""

import json
import http.server
import os
import queue
import re
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = (Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False) else Path(__file__).resolve().parent)
CONFIG_PATH = ROOT / "config.json"
LOG_PATH = ROOT / "oversub.log"
HISTORY_DIR = ROOT / "history"
WORD_PATH = ROOT / "words.jsonl"
FAV_PATH = ROOT / "fav.jsonl"
PAIR_PATH = ROOT / "pairs.jsonl"
REVIEW_PATH = ROOT / "review.jsonl"
TEMPLATE_PATH = ROOT / "templates.json"
TTS_DIR = ROOT / "tts_cache"

_SCENETALK_SRV = {"port": 0, "srv": None}


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """只给情景译页面用：本机回环、不写访问日志。"""

    def log_message(self, *args):
        pass


def scenetalk_port():
    """把 SceneTalk 目录挂到 127.0.0.1 的临时端口。

    页面必须走 http 源：file:// 下 Chromium 会禁掉 localStorage，
    语料库会存不进去；麦克风等安全上下文能力也要求 localhost。
    """
    if _SCENETALK_SRV["port"]:
        return _SCENETALK_SRV["port"]
    import functools
    # 源码运行在 Oversub\ 下，页面在项目根的 SceneTalk\；打包后页面会被拷进程序目录
    cands = [os.path.join(str(ROOT), "SceneTalk"),
             os.path.join(os.path.dirname(str(ROOT)), "SceneTalk"),
             os.path.join(str(ROOT), "web", "SceneTalk")]
    root = next((c for c in cands
                 if os.path.isfile(os.path.join(c, "SceneTalk.html"))), None)
    if not root:
        raise RuntimeError("找不到 SceneTalk.html（应在 SceneTalk\\ 目录下）")
    srv = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_QuietHandler, directory=root))
    _SCENETALK_SRV["srv"] = srv
    _SCENETALK_SRV["port"] = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return _SCENETALK_SRV["port"]

# 全局热键：默认 Ctrl+Alt+G（全部可在设置里改）
HOTKEY_ID = 0xB001
HOTKEY_BASE = 0xB000
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
VK_Z = 0x5A


def release_hotkey_modifiers(combo=""):
    """全局热键是 Ctrl+Alt 组合。如果热键在 Alt 还按着时把窗口抬到前台，
    原来的前台应用（尤其是游戏）可能收不到 Alt 的抬起消息，表现为 Alt 一直卡住。
    这里在热键动作后补发一次修饰键抬起；用扫描码是为了让 DirectInput 游戏也认。
    只补发这个组合里用到的修饰键，避免误放用户正按着的 Shift。"""
    try:
        import ctypes
        low = (combo or "").lower()
        u = ctypes.windll.user32
        KEYUP = 0x0002
        EXT = 0x0001
        if "alt" in low:
            u.keybd_event(0x12, 0x38, KEYUP, 0)              # 左 Alt
            u.keybd_event(0x12, 0x38, KEYUP | EXT, 0)        # 右 Alt（扩展键）
        if "ctrl" in low:
            u.keybd_event(0x11, 0x1D, KEYUP, 0)              # 左 Ctrl
            u.keybd_event(0x11, 0x1D, KEYUP | EXT, 0)        # 右 Ctrl
    except Exception:
        pass

_VK_NAMED = {"ESC": 0x1B, "SPACE": 0x20, "TAB": 0x09, "ENTER": 0x0D, "BACKSPACE": 0x08,
             "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
             "HOME": 0x24, "END": 0x23, "PGUP": 0x21, "PGDN": 0x22, "DEL": 0x2E,
             "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
             "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B}


def foreground_fullscreen():
    """当前前台窗口是否占满整个屏幕（独占全屏 / 无边框全屏都算）。
    返回 (是否全屏, 窗口标题)。"""
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return False, ""
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) == __import__("os").getpid():
            return False, ""          # 自己的窗口不算（比如主界面全屏）
        rect = wintypes.RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False, ""

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", ctypes.c_uint)]

        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        mon = u.MonitorFromWindow(hwnd, 2)
        u.GetMonitorInfoW(mon, ctypes.byref(mi))
        m = mi.rcMonitor
        full = (abs(rect.left - m.left) <= 2 and abs(rect.top - m.top) <= 2
                and abs(rect.right - m.right) <= 2 and abs(rect.bottom - m.bottom) <= 2)
        buf = ctypes.create_unicode_buffer(256)
        u.GetWindowTextW(hwnd, buf, 256)
        return bool(full), buf.value
    except Exception:
        return False, ""


def _parse_hotkey(text):
    """把 "Ctrl+Alt+Z" 这类写法转成 (修饰键掩码, 虚拟键码)；写法不认识就返回 None。"""
    import re
    parts = [p.strip() for p in re.split(r"\+", str(text or "")) if p.strip()]
    if not parts:
        return None
    mods = 0
    vk = None
    for p in parts:
        up = p.upper()
        if up in ("CTRL", "CONTROL"):
            mods |= MOD_CONTROL
        elif up == "ALT":
            mods |= MOD_ALT
        elif up == "SHIFT":
            mods |= MOD_SHIFT
        elif up in ("WIN", "META", "SUPER"):
            mods |= MOD_WIN
        elif len(up) == 1 and up.isalnum():
            vk = ord(up)
        elif p in (",", ".", "/", ";", "'", "-", "=", "`", "[", "]", "\\"):
            vk = {",": 0xBC, ".": 0xBE, "/": 0xBF, ";": 0xBA, "'": 0xDE,
                  "-": 0xBD, "=": 0xBB, "`": 0xC0, "[": 0xDB, "]": 0xDD, "\\": 0xDC}[p]
        elif up in _VK_NAMED:
            vk = _VK_NAMED[up]
        elif re.fullmatch(r"F\d{1,2}", up):
            n = int(up[1:])
            if 1 <= n <= 24:
                vk = 0x70 + n - 1
    if vk is None:
        return None
    if mods == 0 and not (0x70 <= vk <= 0x87):
        return None          # 裸字母/数字键容易误触；F1–F24 允许单键
    return mods, vk
ICON_PATH = ROOT / "oversub3.ico"

# 让 ctypes 能按名字找到系统 DLL（本机环境下 find_library 找不到 ole32）
_SYS32 = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32")
if _SYS32.lower() not in os.environ.get("PATH", "").lower():
    os.environ["PATH"] = _SYS32 + os.pathsep + os.environ.get("PATH", "")

# Argos 默认会去下载 stanza 的分句资源（国内连不上），强制用包内自带的 MiniSBD
os.environ.setdefault("ARGOS_CHUNK_TYPE", "MINISBD")
# HuggingFace 官方站在国内常被墙，走镜像；已经设过就不覆盖
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

DEFAULT_CONFIG = {
    "engine": "local",
    "base_url": "",
    "model": "",
    "api_key": "",
    "whisper_model": "auto",     # auto = 有显卡优先 medium（本地有模型时），否则 turbo；无显卡 small
    "hotwords_custom": "",       # 自定义热词：常被听错的人名/术语/网络词，空格分隔，会并入 ASR 偏置
    "device": "auto",
    "show_source": True,
    "font_size": 22,
    "overlay_prev_rows": 0,       # 字幕条里除当前句外还留几句历史；0 = 只显示当前句（对话框形态）
    "overlay_idle_hide": 8,       # 多久没有新句就把上一句淡出（秒）
    "opacity": 0.9,
    "window": {"x": 400, "y": 840, "w": 1000, "h": 120},
    "loopback_device": "",
    "sensitivity": 0.006,
    "hold_scale": 1.0,
    "show_overlay": True,
    "overlay_pos": "top",         # bottom / bottom_high / top / free：字幕条出现的位置（默认顶部锚定）
    "overlay_skin": "dialog_dark",  # 字幕皮肤：dialog_dark / galgame_light / atri / lyrics / cinema
    "overlay_style": "dialog",    # dialog = Galgame 对话框（高度恒定、底边死锁）｜ banner = 旧自适应条
    "overlay_dialog_h": 0,        # 对话框固定高度；0 = 按字号自动算（约 3 行正文）
    "overlay_gap_top": 8,         # 顶部锚定时离屏幕上沿的像素；调大可避开游戏 HUD
    "diarize": False,
    "max_speakers": 4,
    "spk_mode": "balanced",       # strict / balanced / loose
    "main_win": {},               # 主窗口上次的位置和大小，关掉记住
    "last_tab": 0,                # 上次停留的页面
    "pinned": False,              # 窗口是否置顶
    "animations": True,           # 界面淡入 / 抽屉动画总开关
    "ui_lang": "zh",              # 界面语言：zh / en
    "keep_rows": 300,             # 表格里最多放多少行（更多留在磁盘上）
    "keep_days": 0,               # 记录保留天数，0 = 永久保留
    "auto_read": False,           # 新句子自动朗读
    "read_rate": 0,               # 语速 -3..3，0 为正常
    "loop_read": False,           # 跟读模式：重复朗读当前句
    "tts_queue_max": 3,           # 朗读队列上限，超过丢弃最旧的待读句
    "tts_skip_short": True,       # 自动朗读跳过短语气词 / 重复句
    "tts_volumes": {},            # 各语种独立音量 0-100
    "tts_min_words": {},          # 各语种自动朗读最小词数 / 字数
    "tts_gap": 0.4,               # 两句朗读之间静音间隔（秒）
    "tts_loop_pause": 1.2,        # 跟读留白：读完停多久
    "tts_skip_duplicate": True,   # 自动朗读过滤重复句
    "tts_skip_fullscreen": True,  # 全屏游戏时禁止自动朗读
    "tts_duck_only": False,       # 仅当原声安静时才自动朗读
    "tts_duck_threshold": 0.035,  # 原声 RMS 阈值（0-1）
    "shadow_mode": False,         # 跟读模式：读一遍 → 留白 → 可选重播一遍
    "shadow_repeat": True,        # 跟读模式是否自动重播一遍
    "tts_muted": False,           # 紧急静音：清队 + 暂停朗读
    "tts_output_device": "",      # 朗读输出设备关键字（留空=系统默认）
    "word_hl": True,              # 朗读时逐词高亮
    "click_through": True,        # 常态下字幕条对鼠标穿透（热键才接管）
    "asr_quality": "accurate",    # accurate = beam5+VAD，fast = beam1
    # 热词是提示词：喂太多，静音时模型会照着词表“念”出来（没说却识别出设定词）。
    # off = 完全不用；custom = 只用你自己填的词（默认）；full = 内置词表 + 上一句实词
    "asr_hotwords_mode": "custom",
    "latency_mode": "balanced",   # fast 3.6s / balanced 5s / accurate 6.5s 语音强切；断句静音同步调整
    # 全局热键一律用 Ctrl+Alt+字母：避开 F1~F12（游戏技能/存档）、Ctrl+L（浏览器地址栏）、
    # Ctrl+,（VS Code 设置）这类主流应用占用率很高的键。全部可在设置里改。
    "hotkey_toggle": "Ctrl+Alt+G",      # 切换字幕交互模式（避开 QQ 的 Ctrl+Alt+Z）
    "hotkey_fav_last": "Ctrl+Alt+D",    # 收藏上一句
    "hotkey_history": "Ctrl+Alt+H",     # 打开历史字幕面板
    "hotkey_clear": "Ctrl+Alt+X",       # 清空字幕流
    "hotkey_subtitle": "Ctrl+Alt+V",    # 开 / 关字幕显示
    "hotkey_capture": "Ctrl+Alt+R",     # 开 / 关音频识别
    "hotkey_library": "Ctrl+Alt+L",     # 唤起素材库
    "hotkey_selftest": "Ctrl+Alt+T",    # 切换自测模式
    "hotkey_f5_capture": "Ctrl+Alt+P",  # 开始 / 暂停捕获
    "hotkey_f6_fav": "Ctrl+Alt+U",      # 收藏当前句
    "hotkey_f7_sub": "Ctrl+Alt+B",      # 显示 / 隐藏悬浮字幕
    "hotkey_f8_drawer": "Ctrl+Alt+J",   # 开 / 关历史抽屉
    "hotkey_f9_autoread": "Ctrl+Alt+Y", # 开 / 关自动朗读（避开 QQ 截图 Ctrl+Alt+A）
    "hotkey_f10_mute": "Ctrl+Alt+M",    # 紧急静音：清队 + 暂停朗读
    "hotkey_font_up": "Ctrl+Alt+=",     # 字幕字号 +2
    "hotkey_font_down": "Ctrl+Alt+-",   # 字幕字号 -2
    "hotkey_ctrl_l": "",                # 旧的 Ctrl+L 让给浏览器地址栏，默认停用
    "hotkey_ctrl_lab": "Ctrl+Alt+,",    # 打开 Lab 快速配置（不再用 VS Code 的 Ctrl+,）
    "hotkey_panic": "Ctrl+Alt+Q",       # 强制退出交互（防止光标卡在字幕层）
    "hotkey_scheme": 3,                 # 键位方案版本：从旧版 F5~F10 自动迁到低冲突方案
    "hotkey_custom": False,             # 用户手改过键位就不再自动覆盖
    "interactive_timeout": 3,           # 交互模式无鼠标操作多少秒自动退出，0 = 不自动退出
    "scroll_rows": 10,                  # 字幕条滚动缓冲区保留多少行（8~12，含当前一句）
    "hotkey_fav_easy": "Ctrl+Alt+1",    # 收藏上一句并标「简单」
    "hotkey_fav_mid": "Ctrl+Alt+2",     # 收藏上一句并标「中等」
    "hotkey_fav_hard": "Ctrl+Alt+3",    # 收藏上一句并标「困难」
    "save_audio": True,                 # 收藏时是否保存音频切片（低配可关）
    "noise_level": 0,                   # 降噪强度 0 关 / 1 中 / 2 强（强降噪会削辅音，默认关）
    "vad_sensitivity": "medium",        # high 小声也认 / medium / low 抗噪
    "scene": "fps",
    "scene_recent": [],                     # 当前场景预设 fps / video / chat / custom
    "idle_pause": 12,                   # 多久没语音就暂停刷新字幕（0 = 不暂停）
    "style_preset": "game",
    "theme": "dark",                    # light / dark；暗色为游戏场景优先             # 字幕样式预设 game / film / minimal / custom
    "style_profiles": {
        "game": {"font_size": 24, "opacity": 0.92, "scroll_rows": 10, "anchor": "bottom"},
        "film": {"font_size": 26, "opacity": 0.82, "scroll_rows": 10, "anchor": "upper"},
        "minimal": {"font_size": 22, "opacity": 0.55, "scroll_rows": 8, "anchor": "bottom"},
    },
    "scene_profiles": {
        "fps": {"noise_level": 0, "vad_sensitivity": "low", "sensitivity": 0.008},
        "video": {"noise_level": 0, "vad_sensitivity": "medium", "sensitivity": 0.005},
        "chat": {"noise_level": 1, "vad_sensitivity": "high", "sensitivity": 0.003},
    },
    "session_gap": 120,                 # 静音超过多久算新会话（秒）
    "perf_mode": "balanced",            # high / balanced / eco
    "hard_underline": True,             # 交互模式下给「困难」生词加细下划线
    "mic_assist": False,                # 双向语音：麦克风说中文，出英文
    "mic_autoread": False,              # 说出的英文自动朗读（口语化音色）
    "mic_device": "",                   # 麦克风设备名，空 = 系统默认
    "source_on": True,                  # 外源音频（队友/视频）识别开关
    "role": "teammate",                 # 句对角色关系：teammate / streamer / stranger
    "strip_fillers": True,              # 存句对时剥离 uh/like/you know，字幕原样保留
    "save_out_pairs": True,             # 把“你说中文→英文”自动存进输出链路
    "reply_preload": True,              # 预加载适合回应上一句的候选英文
    "spk_names": {},                    # 说话人重命名：{"说话人1": "队友 A"}
    "show_spk_col": True,               # 记录表里显示说话人列
    "overlay_bilingual": False,         # 悬浮条对照模式：中文 ｜ 英文 并排（默认关）
    "filter_short": True,               # 过滤过短识别（单字 / 语气词）
    "min_chars": 2,                     # 中文至少几个字才记录、上字幕
    "source_lang": "zh",                # 源语言（ASR 抓的语言）：zh / en，跟随目标语言自动配对
    "target_lang": "en",                # 目标语言（译文输出）：en / zh，切到中文即英文识别+中文翻译
    "rtl_overlay": False,               # 右到左渲染开关（阿拉伯语/希伯来语预留）
    "font_family": "",                  # 全局兜底字体；空 = 各语种自己的推荐字体
    "lang_fonts": {},                   # 各语种独立渲染字体
    "lang_profiles": {},                # 每语种一套独立配置（模型/字号/TTS/过滤阈值）
    "tts_rates": {},                    # 各语种 TTS 语速（-4~4）
    "tts_rate_scales": {},              # 各语种语速倍率 0.5~2.0（界面用）
    "tts_voices": {},                   # 各语种 TTS 音色名
    "min_words": 2,                     # 空格分词语种的最短单词数
    "mt_size": "small",                 # 翻译模型尺寸（预留：small/base）
    "filter_by_lang": False,            # 生词本按当前目标语种隔离筛选
}

# ---------------------------------------------------------------- 字幕皮肤
# 只影响外观（底色 / 描边 / 圆角 / 文字颜色 / 对齐），不动采集、翻译、历史等任何逻辑。
# 加新皮肤：在下面加一条即可，渲染处按 key 取值，不用改别的地方。
OVERLAY_SKINS = {
    # 默认：深色半透明卡片，微圆角，白字（现有形态）
    "dialog_dark": dict(
        name="深色卡片（默认）", bg=(16, 18, 22), radius=14,
        edge=(255, 255, 255), edge_alpha=None,
        main="#FFFFFF", sub="#9E9A91",
        hist="#D8D3C9", hist_sub="#9B978F",
        align="left", badge_bg=(140, 150, 165, 20), badge_fg="#93A0AD"),
    # 柚子社那类（千恋万花 / 天色幻想岛）：米白半透明、圆角更大、深色文字、
    # 框顶一层浅高光，看起来像“纸质对话框”
    "galgame_light": dict(
        name="Galgame 浅色（柚子社风）", bg=(246, 242, 235), radius=18,
        edge=(255, 255, 255), edge_alpha=200,
        main="#23232A", sub="#6B6660",
        hist="#3F3D38", hist_sub="#7A756C",
        align="left", badge_bg=(58, 58, 66, 18), badge_fg="#4A4A54"),
    # ATRI 那类冷调科幻：深蓝黑、细亮边、小圆角、冷白字
    "atri": dict(
        name="ATRI 冷调（深蓝科幻）", bg=(14, 21, 30), radius=10,
        edge=(150, 192, 226), edge_alpha=95,
        main="#EEF5FC", sub="#8FA5BA",
        hist="#C4D2E1", hist_sub="#7B90A4",
        align="left", badge_bg=(150, 192, 226, 22), badge_fg="#A9C3DA"),
    # 音乐播放器的滚动歌词：不画实心框，只垫一层上下渐隐的暗雾，旧句暗、当前句最亮
    "lyrics": dict(
        name="滚动歌词（无框居中）", bg=(0, 0, 0), radius=0, grad=True,
        edge=None, edge_alpha=None,
        main="#FFFFFF", sub="#B6B1A8",
        hist="#8B8780", hist_sub="#6C6862",
        align="center", badge_bg=(255, 255, 255, 0), badge_fg="#8B8780"),
    # 电影字幕：横向宽底、上下淡出，白字居中
    "cinema": dict(
        name="电影字幕（宽底白字）", bg=(0, 0, 0), radius=6, grad=True,
        edge=None, edge_alpha=0,
        main="#FFFFFF", sub="#C9C4BA",
        hist="#A9A49B", hist_sub="#807B73",
        align="center", badge_bg=(255, 255, 255, 0), badge_fg="#8B8780"),
}


def skin_of(cfg):
    key = str(cfg.get("overlay_skin") or "dialog_dark")
    return key, OVERLAY_SKINS.get(key) or OVERLAY_SKINS["dialog_dark"]


def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass


def history_file(day=None):
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    return HISTORY_DIR / ((day or time.strftime("%Y-%m-%d")) + ".jsonl")


def history_path(day):
    """某天的记录文件：优先未压缩的，其次是压缩过的。"""
    plain = HISTORY_DIR / (day + ".jsonl")
    if plain.exists():
        return plain
    gz = HISTORY_DIR / (day + ".jsonl.gz")
    return gz if gz.exists() else None


def _open_history(path):
    if str(path).endswith(".gz"):
        import gzip
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "r", encoding="utf-8")


_HIST_Q = queue.Queue()
_HIST_LOCK = threading.Lock()
_HIST_WRITER = None
_HIST_STOP = threading.Event()


def _flush_history_now():
    """把内存队列里的记录一次性批量落盘，2 秒一批，避免逐句 open/write。"""
    lines = []
    while True:
        try:
            lines.append(_HIST_Q.get_nowait())
        except queue.Empty:
            break
    if not lines:
        return 0
    with _HIST_LOCK:
        with open(history_file(), "a", encoding="utf-8") as f:
            for ln in lines:
                f.write(ln + "\n")
    return len(lines)


def _hist_writer_loop():
    while not _HIST_STOP.is_set():
        _HIST_STOP.wait(2.0)
        try:
            _flush_history_now()
        except Exception as e:
            log("批量写盘失败：" + repr(e))


def _ensure_hist_writer():
    global _HIST_WRITER
    if _HIST_WRITER is None or not _HIST_WRITER.is_alive():
        _HIST_WRITER = threading.Thread(target=_hist_writer_loop, daemon=True)
        _HIST_WRITER.start()


# ---------------------------------------------------------------- 异常兜底：备份 / CPU
BACKUP_DIR = ROOT / "backup"
_CPU_PREV = {"idle": None, "total": None}


def cpu_load_percent():
    """用 GetSystemTimes 估算整机 CPU 占用（不依赖 psutil）。两次调用之间取增量。"""
    try:
        import ctypes
        from ctypes import wintypes
        idle = wintypes.ULARGE_INTEGER()
        kernel = wintypes.ULARGE_INTEGER()
        user = wintypes.ULARGE_INTEGER()
        if not ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return 0.0
        i = idle.value; t = kernel.value + user.value
        prev_idle = _CPU_PREV["idle"]
        prev_total = _CPU_PREV["total"]
        _CPU_PREV["idle"], _CPU_PREV["total"] = i, t
        if prev_total is None or t <= prev_total:
            return 0.0
        di = i - prev_idle; dt = t - prev_total
        return max(0.0, min(100.0, 100.0 * (dt - di) / float(dt)))
    except Exception:
        return 0.0


def is_fullscreen_foreground():
    """前台窗口是否铺满整块屏幕（判断是否在全屏游戏 / 全屏视频）。"""
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return False
        r = wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return False
        sw = ctypes.windll.user32.GetSystemMetrics(0)
        sh = ctypes.windll.user32.GetSystemMetrics(1)
        w = r.right - r.left; h = r.bottom - r.top
        return w >= sw * 0.95 and h >= sh * 0.95
    except Exception:
        return False


def backup_data():
    """把当天记录和关键 jsonl 增量备份到 backup/时间戳/，保留最近 12 份。"""
    try:
        _flush_history_now()
        stamp = time.strftime("%Y-%m-%d_%H%M")
        dst = BACKUP_DIR / stamp
        dst.mkdir(parents=True, exist_ok=True)
        today = time.strftime("%Y-%m-%d")
        src = HISTORY_DIR / ("%s.jsonl" % today)
        if src.exists():
            import shutil
            shutil.copy2(src, dst / src.name)
        for name in ("pairs.jsonl", "fav.jsonl", "words.jsonl", "review.jsonl"):
            p = ROOT / name
            if p.exists():
                import shutil
                shutil.copy2(p, dst / name)
        olds = sorted([d for d in BACKUP_DIR.iterdir() if d.is_dir()])
        for d in olds[:-12]:
            import shutil
            shutil.rmtree(d, ignore_errors=True)
        return True
    except Exception as e:
        log("备份失败：" + repr(e))
        return False


def restore_latest_backup_if_needed():
    """崩溃重启：当天记录文件缺失时，从最近备份里捞回来。"""
    try:
        today = time.strftime("%Y-%m-%d")
        target = HISTORY_DIR / ("%s.jsonl" % today)
        if target.exists() and target.stat().st_size > 0:
            return False
        if not BACKUP_DIR.exists():
            return False
        import shutil
        for d in sorted(BACKUP_DIR.iterdir(), reverse=True):
            p = d / ("%s.jsonl" % today)
            if d.is_dir() and p.exists() and p.stat().st_size > 0:
                HISTORY_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, target)
                log("已从备份恢复当天记录：" + str(p))
                return True
    except Exception as e:
        log("恢复备份失败：" + repr(e))
    return False


# ---------------------------------------------------------------- 动画 / 字体
ANIM = {"on": True}
_ANIM_WATCH = {}          # 保留动画引用，避免被回收


def fade_in(widget, ms=120):
    """功能型淡入：只改透明度，不弹跳、不缩放；全局开关关闭时直接显形。"""
    if not ANIM["on"] or widget is None:
        return
    try:
        from PySide6.QtWidgets import QGraphicsOpacityEffect
        from PySide6.QtCore import QPropertyAnimation, QEasingCurve
        eff = widget.graphicsEffect()
        if not isinstance(eff, QGraphicsOpacityEffect):
            eff = QGraphicsOpacityEffect(widget)
            widget.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", widget)
        anim.setDuration(int(ms))
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.InQuad)   # 线性偏缓，无回弹
        _ANIM_WATCH[id(widget)] = anim
        if len(_ANIM_WATCH) > 40:
            for k in list(_ANIM_WATCH)[:20]:
                _ANIM_WATCH.pop(k, None)
        anim.start()
    except Exception:
        pass


def preload_lang_fonts(cfg):
    """预热各语种字体：让首次出现日文 / 韩文时直接有字形，不闪方块。"""
    try:
        from PySide6.QtGui import QFont, QFontDatabase, QFontMetrics
        families = set(QFontDatabase.families())
        wanted = set()
        for f in (cfg.get("lang_fonts") or {}).values():
            if f:
                wanted.add(str(f))
        for f in ("Microsoft YaHei UI", "Yu Gothic UI", "Malgun Gothic",
                  "Segoe UI", "Arial"):
            if f in families:
                wanted.add(f)
        samples = {"Microsoft YaHei UI": "中文测试", "Yu Gothic UI": "日本語の表示",
                   "Malgun Gothic": "한국어 표시"}
        cache = {}
        for fam in wanted:
            font = QFont(fam, 12)
            fm = QFontMetrics(font)
            cache[fam] = fm.horizontalAdvance(samples.get(fam, "Aa 123"))
        log("字体预热：%d 个语种字体" % len(cache))
        return cache
    except Exception as e:
        log("字体预热失败：" + repr(e))
        return {}


# ---------------------------------------------------------------- 界面中英切换
UI_LANG = {"code": "zh"}
UI_MAP = {
    "Capture｜捕获台": "Capture", "Library｜语言库": "Library", "Lab｜实验室": "Lab",
    "捕获台": "Capture", "语言库": "Library", "实验室": "Lab",
    "开始捕获": "Start capture", "暂停": "Pause", "继续": "Resume",
    "收藏": "Favorite", "朗读": "Read aloud", "循环": "Loop", "清队": "Clear queue",
    "设置": "Settings", "帮助": "Help", "置顶": "Always on top",
    "导出记录": "Export records", "打开日志": "Open log",
    "暗色（游戏优先）": "Dark (game first)", "浅色": "Light",
    "石墨（低饱和暗灰）": "Graphite (muted dark)", "深海（夜间冷色）": "Ocean (night cool)",
    "暖纸（护眼米色）": "Warm paper (eye care)", "高对比（纯黑亮字）": "High contrast",
    "外观与主题": "Appearance & theme", "识别规则": "Recognition rules",
    "语种管理": "Languages", "模型管理": "Models", "词典插件": "Dictionary plugins",
    "界面主题": "Theme", "字幕主题": "Subtitle theme",
    "中→英（你会说）": "Chinese → English", "英→中（你听懂）": "English → Chinese",
    "自测模式": "Self-test", "素材库": "Library", "生词本": "Wordbook",
    "收藏夹": "Favorites", "会话": "Sessions", "句对": "Pairs",
    "搜索": "Search", "全部": "All", "就绪": "Ready", "识别中": "Listening",
    "暂停中": "Paused", "模型加载中": "Loading model", "等待声音…": "Waiting for audio…",
}


def _tr(text):
    if UI_LANG["code"] != "en":
        return text
    s = str(text)
    if s in UI_MAP:
        return UI_MAP[s]
    for zh, en in UI_MAP.items():
        if len(zh) >= 2 and s.startswith(zh):
            return en + s[len(zh):]
    return text


def apply_ui_lang(root, lang="zh"):
    """中文 / English 界面切换：只翻静态控件文字，业务数据不碰。"""
    UI_LANG["code"] = "en" if str(lang).lower().startswith("en") else "zh"
    if root is None or not hasattr(root, "findChildren"):
        return                                  # Overlay 是普通对象，不参与遍历
    try:
        from PySide6.QtWidgets import (QWidget, QLabel, QAbstractButton, QComboBox,
                                       QTabWidget, QGroupBox)
        for w in root.findChildren(QWidget):
            if isinstance(w, QTabWidget):
                for i in range(w.count()):
                    cur = w.tabText(i)
                    zh = w.tabBar().tabData(i) or cur
                    if UI_LANG["code"] == "en":
                        w.setTabText(i, _tr(zh))
                    else:
                        w.setTabText(i, zh)
                continue
            if isinstance(w, (QLabel, QAbstractButton, QGroupBox)):
                if w.property("keep_zh"):
                    continue
                if isinstance(w, QLabel) and ("<" in w.text() and ">" in w.text()):
                    continue                       # 富文本标签保持原样，避免样式丢失
                if not w.property("zh_text"):
                    w.setProperty("zh_text", w.text())
                zh = w.property("zh_text")
                w.setText(_tr(zh) if UI_LANG["code"] == "en" else zh)
            elif isinstance(w, QComboBox):
                for i in range(w.count()):
                    cur = w.itemText(i)
                    if not w.itemData(i, 0x0100):      # 记下原中文（UserRole+256 之类不用）
                        pass
                # 只处理固定选项：按当前文本缓存
                if not w.property("zh_items"):
                    w.setProperty("zh_items", [w.itemText(i) for i in range(w.count())])
                items = w.property("zh_items")
                w.blockSignals(True)
                for i, zh in enumerate(items[:w.count()]):
                    w.setItemText(i, _tr(zh) if UI_LANG["code"] == "en" else zh)
                w.blockSignals(False)
    except Exception as e:
        log("界面语言切换失败：" + repr(e))


READ_PATH = ROOT / "tts_read.jsonl"


def mark_read_text(text):
    """记录「这句已经朗读过」，复习可筛未听过发音的句子。"""
    text = (text or "").strip()
    if not text:
        return
    try:
        with open(READ_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "text": text},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_texts():
    out = set()
    try:
        if READ_PATH.exists():
            for ln in READ_PATH.read_text(encoding="utf-8").splitlines():
                try:
                    out.add(str(json.loads(ln).get("text") or "").strip())
                except Exception:
                    pass
    except Exception:
        pass
    return out


def append_history(ts, zh, en, spk="", scene=""):
    try:
        uid = "%s-%s" % (str(ts).replace(":", "").replace("-", "").replace(" ", ""),
                         abs(hash((zh, en, spk))) % (10 ** 8))
        rec = {"id": uid,
               "source_text": zh, "target_text": en,
               "speaker_id": spk, "tags": ([scene] if scene else []),
               "memory_state": "new", "quality": "ok",
               "audio_snippet": None, "created_at": ts,
               "t": ts, "zh": zh, "en": en, "spk": spk, "scene": scene,
               "count": 1,
               "source_lang": CURRENT_LANGS["sl"], "target_lang": CURRENT_LANGS["tl"]}
        _ensure_hist_writer()
        _HIST_Q.put(json.dumps(rec, ensure_ascii=False))
    except Exception as e:
        log("写历史失败：" + repr(e))


def delete_history_entries(day, keys):
    """从当天 jsonl 里删掉指定 (ts, en) 的记录，返回删掉几条。
    已经压成 .gz 的旧日期只读，不动。"""
    keys = {(str(t), str(e)) for t, e in keys}
    _flush_history_now()
    try:
        p = HISTORY_DIR / ("%s.jsonl" % day)
        if not p.exists():
            return 0
        rows, kept, gone = [], [], 0
        for ln in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(ln)
            except Exception:
                continue
            rows.append(r)
            if (str(r.get("t", "")), str(r.get("en", ""))) in keys:
                gone += 1
            else:
                kept.append(r)
        if gone:
            p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in kept)
                         + ("\n" if kept else ""), encoding="utf-8")
        return gone
    except Exception as e:
        log("删除记录失败：" + repr(e))
        return 0


# 静音 / 短文本过滤：一句话里只有语气词或单字，就别进字幕也别进表格
ZH_FILLERS = {"啊", "哦", "嗯", "呃", "唉", "诶", "咦", "喂", "呵", "哈", "噢", "喔",
              "对", "好", "是的", "好的", "行", "可以"}
EN_FILLERS = {"yeah", "yes", "no", "ok", "okay", "oh", "ah", "uh", "huh", "hey", "hi",
              "hello", "thanks", "thank you", "sure", "cool", "nice", "wow", "sorry", "please"}


def _clean_words(s):
    return "".join(ch for ch in str(s or "") if ch.isalnum() or ch == "'")


def should_filter_short(zh, en, cfg):
    """返回 True：这句太短/全是语气词，不生成记录、不推字幕。
    计数单位按源语言走：中日按字符，英/法/德等按单词，阈值各自独立。"""
    try:
        if not bool(cfg.get("filter_short", True)):
            return False
        sl = str((cfg or {}).get("source_lang") or "zh")
        unit = lang_meta(sl).get("unit", "char")
        zh_c = _clean_words(zh)
        en_c = _clean_words(en)
        if unit == "word":
            words = [w for w in str(zh or "").replace(",", " ").split() if _clean_words(w)]
            if len(words) < int(cfg.get("min_words") or 2):
                return True
        elif len(zh_c) < int(cfg.get("min_chars") or 2):
            return True
        if zh_c in ZH_FILLERS:
            words = [w for w in str(en or "").replace(",", " ").split() if w]
            if not words or len(words) <= 2:
                return True
        if en_c.lower() in EN_FILLERS and len(zh_c) <= 3:
            return True
    except Exception:
        return False
    return False


BAD_PATH = ROOT / "bad_sentences.jsonl"
QUAL_NAMES = {"native": "地道", "machine": "机器感重", "error": "识别错误"}
QUAL_MARKS = {"native": "✅", "machine": "⚠️", "error": "❌"}


def quality_weight(qual):
    """复习权重：识别错误直接隔离（0），机器感重降权，地道按原权重。"""
    return {"native": 1.0, "machine": 0.6, "error": 0.0}.get(str(qual or ""), 1.0)


def is_bad_sentence(en):
    try:
        if not BAD_PATH.exists() or not str(en or "").strip():
            return False
        key = str(en).strip().lower()
        for ln in BAD_PATH.read_text(encoding="utf-8").splitlines():
            try:
                if str(json.loads(ln).get("en", "")).strip().lower() == key:
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def mark_sentence_bad(en, bad=True):
    """识别错误的句子记进隔离表：之后不再进生词本、句对复习池。"""
    key = str(en or "").strip()
    if not key:
        return
    rows = []
    try:
        if BAD_PATH.exists():
            for ln in BAD_PATH.read_text(encoding="utf-8").splitlines():
                try:
                    rows.append(json.loads(ln))
                except Exception:
                    pass
    except Exception:
        pass
    rows = [r for r in rows if str(r.get("en", "")).strip().lower() != key.lower()]
    if bad:
        rows.append({"en": key, "t": time.strftime("%Y-%m-%d %H:%M:%S")})
    try:
        BAD_PATH.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
                            + ("\n" if rows else ""), encoding="utf-8")
    except Exception as e:
        log("写隔离表失败：" + repr(e))


def update_history_row(ts, en, patch, zh=""):
    """就地改当天某条记录（场景标签 / 质量标注 / 备选英文）。
    旧日期已经压成 .gz，只读，不去动它。"""
    try:
        p = HISTORY_DIR / (time.strftime("%Y-%m-%d") + ".jsonl")
        if not p.exists():
            return False
        rows = []
        hit = False
        for ln in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if (not hit and str(r.get("t", "")) == str(ts)
                    and str(r.get("en", "")) == str(en)
                    and (not zh or str(r.get("zh", "")) == str(zh))):
                r.update(patch)
                hit = True
            rows.append(r)
        if not hit:
            return False
        p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                     encoding="utf-8")
        return True
    except Exception as e:
        log("改记录失败：" + repr(e))
        return False


def append_history_alt(ts, en, alt, zh="", day=None):
    """给某条记录挂一条备选表达（同义 / 另一种说法），返回是否成功。"""
    day = day or str(ts)[:10] or time.strftime("%Y-%m-%d")
    try:
        p = HISTORY_DIR / ("%s.jsonl" % day)
        if not p.exists():
            return False
        rows, hit = [], False
        for ln in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if (not hit and str(r.get("t", "")) == str(ts)
                    and str(r.get("en", "")) == str(en)
                    and (not zh or str(r.get("zh", "")) == str(zh))):
                alts = list(r.get("alts") or [])
                if not any((a.get("en") if isinstance(a, dict) else a) == alt for a in alts):
                    alts.append({"en": alt})
                r["alts"] = alts
                hit = True
            rows.append(r)
        if not hit:
            return False
        p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                     encoding="utf-8")
        return True
    except Exception as e:
        log("补充同义失败：" + repr(e))
        return False


def history_alts(en, zh=""):
    """某句话下挂着的备选英文（从今天记录里读）。"""
    out = []
    try:
        for r in read_history(None, limit=2000):
            if str(r.get("en", "")) == str(en) and (not zh or str(r.get("zh", "")) == str(zh)):
                out.extend(r.get("alts") or [])
    except Exception:
        pass
    return out


def _secs(ts):
    try:
        s = str(ts).strip()
        if " " in s:
            s = s.split(" ")[-1]
        h, m, sec = s.split(":")[-3:]
        return int(h) * 3600 + int(m) * 60 + int(sec)
    except Exception:
        return None


SCENE_NAMES = {"fps": "FPS / 游戏", "video": "看视频", "chat": "语音聊天", "": "未标注"}
SCENE_SHORT = {"fps": "FPS", "video": "影视", "chat": "闲聊", "": "未标"}

# ---------------------------------------------------------------- 语种注册表
# 每条：名称 / 原生写法 / 翻译包 / TTS 标识 / 文字方向 / 是否可用
# installed=True 表示这套语种当前真的能用（有本地翻译包 + 界面可切）。
# 其余几条是预留条目：不会出现在下拉里，也不会给任何“点了就下载”的假入口。
LANG_REGISTRY = {
    "zh": {"name": "中文", "native": "中文", "asr": "faster-whisper-zh",
           "mt": "argos-en-zh", "tts": "SAPI zh-CN（Huihui）", "rtl": False,
           "installed": True, "dict": "zh"},
    "en": {"name": "英语", "native": "English", "asr": "faster-whisper-small",
           "mt": "argos-zh-en", "tts": "SAPI en-US（Zira）", "rtl": False,
           "installed": True, "dict": "en"},
    "ja": {"name": "日语", "native": "日本語", "asr": "faster-whisper-ja",
           "mt": "argos-zh-ja", "tts": "SAPI ja-JP", "rtl": False,
           "installed": False, "dict": "ja"},
    "ko": {"name": "韩语", "native": "한국어", "asr": "faster-whisper-ko",
           "mt": "argos-zh-ko", "tts": "SAPI ko-KR", "rtl": False,
           "installed": False, "dict": "ko"},
    "fr": {"name": "法语", "native": "Français", "asr": "faster-whisper-fr",
           "mt": "argos-zh-fr", "tts": "SAPI fr-FR", "rtl": False,
           "installed": False, "dict": "fr"},
    "de": {"name": "德语", "native": "Deutsch", "asr": "faster-whisper-de",
           "mt": "argos-zh-de", "tts": "SAPI de-DE", "rtl": False,
           "installed": False, "dict": "de"},
    "es": {"name": "西班牙语", "native": "Español", "asr": "faster-whisper-es",
           "mt": "argos-zh-es", "tts": "SAPI es-ES", "rtl": False,
           "installed": False, "dict": "es"},
    "ar": {"name": "阿拉伯语", "native": "العربية", "asr": "faster-whisper-ar",
           "mt": "argos-zh-ar", "tts": "SAPI ar-SA", "rtl": True,
           "installed": False, "dict": "ar"},
}
SOURCE_LANGS = {
    "zh": {"name": "中文", "native": "中文", "asr": "faster-whisper-small",
           "installed": True, "rtl": False, "unit": "char", "font": "Microsoft YaHei"},
    "en": {"name": "英语", "native": "English", "asr": "faster-whisper-small",
           "installed": True, "rtl": False, "unit": "word", "font": "Segoe UI"},
}
# 补默认：字符计数单位 / 推荐字体（防日韩文、西里尔文方块乱码）
for _code, _m in LANG_REGISTRY.items():
    _m.setdefault("unit", "char" if _code in ("ja", "ko", "zh") else "word")
    _m.setdefault("font", {"en": "Segoe UI", "zh": "Microsoft YaHei",
                           "ja": "Yu Gothic UI", "ko": "Malgun Gothic",
                           "fr": "Segoe UI", "de": "Segoe UI", "es": "Segoe UI",
                           "ar": "Segoe UI"}.get(_code, "Segoe UI"))

# 每个语种的识别热词：只做词形偏置，不堆成完整句子，避免占满解码上下文。
# 后半段刻意放同音混淆高发词（萝莉/罗列、上线/上先这类），比补通用词更能纠偏。
ASR_HOTWORDS = {
    "zh": ("反恐精英 拆包 下包 中路 长道 短道 闪光弹 烟雾弹 燃烧弹 爆头 残局 "
           "保枪 经济局 转点 大狙 沙鹰 炼狱小镇 荒漠迷城 炙热沙城 核子危机 "
           "香蕉道 警家 匪家 拱门 跳台 棺材 VIP 书房 市场 锅炉房 下水道 阳台 "
           "连接 A大 A小 B点 中门 小道 拉枪 补枪 直架 突破 残血 空枪 压枪 扫射 "
           "鸟狙 连狙 强起 假拆 爆弹 高闪 背闪 干拉 掩体 复活 护甲 "
           "萝莉 御姐 二次元 手办 番剧 声优 同人 弹幕 立绘 抽卡 保底 氪金 "
           "掉率 副本 上线 掉线 卡顿 延迟 外挂 账号 匹配 队友 "
           "吃饭 睡觉 上班 下班 加班 开会 需求 工资 房租 快递 外卖"),
    "en": ("counter-terrorist defuse mid long short flash smoke molotov headshot "
           "clutch save eco rotate AWP Deagle banana arch ramp pit heaven market "
           "connector stairs window drop peek trade anchor rush retake "
           "loli anime cosplay figure episode stream queue lag ping buff nerf"),
}

# Whisper 在静音段 / 纯音乐 / 游戏环境音上最爱吐的“整句幻觉”。
# 只在整句（去标点后）就是这些词时丢；"hot" 出现在 "hot Desert Eagle" 里是正常词，不能误杀。
ASR_HALLUC_SHORT = {
    "thank you", "thanks", "thank you very much", "thanks a lot", "bye", "bye bye",
    "you", "hot", "so", "yeah", "okay", "ok", "music", "applause", "silence",
    "谢谢观看", "谢谢大家", "谢谢", "字幕", "请不吝点赞", "感谢收看",
}
# 片尾字幕 / 订阅引导类：短句里命中即丢（长句保留，可能是真的在聊）
ASR_HALLUC_FRAG = (
    "请不吝点赞", "点赞 订阅", "订阅 转发", "字幕由", "字幕组", "打赏",
    "优优独播", "独播剧场", "中文字幕", "字幕提供", "字幕下载",
    "subtitles by", "amara.org", "thanks for watching", "please subscribe",
    "like and subscribe", "subscribe to",
)
# 设置面板和速查面板共用这一份，改键位只改这里，不会两边对不上。
HOTKEY_SPECS = (
    ("hotkey_toggle", "切换字幕交互模式", "Ctrl+Alt+G（避开 QQ 的 Ctrl+Alt+Z）", "字幕"),
    ("hotkey_f7_sub", "显示/隐藏字幕", "Ctrl+Alt+B（原 F7）", "字幕"),
    ("hotkey_f8_drawer", "字幕历史抽屉", "Ctrl+Alt+J（原 F8）", "字幕"),
    ("hotkey_subtitle", "开/关字幕显示", "Ctrl+Alt+V，屏幕想干净就一键收掉；识别照跑", "字幕"),
    ("hotkey_f5_capture", "开始/暂停捕获", "Ctrl+Alt+P（原 F5 在游戏里常是快速存档）", "捕获"),
    ("hotkey_capture", "开/关音频识别", "Ctrl+Alt+R，彻底停采集，省 CPU", "捕获"),
    ("hotkey_clear", "清空字幕流", "Ctrl+Alt+X", "捕获"),
    ("hotkey_fav_last", "收藏上一句", "Ctrl+Alt+D，纯键盘存档", "收藏"),
    ("hotkey_f6_fav", "收藏当前句", "Ctrl+Alt+U（原 F6）", "收藏"),
    ("hotkey_fav_easy", "收藏并标「简单」", "Ctrl+Alt+1", "收藏"),
    ("hotkey_fav_mid", "收藏并标「中等」", "Ctrl+Alt+2", "收藏"),
    ("hotkey_fav_hard", "收藏并标「困难」", "Ctrl+Alt+3", "收藏"),
    ("hotkey_f9_autoread", "开/关自动朗读", "Ctrl+Alt+Y（原 F9；避开 QQ 截图 Ctrl+Alt+A）", "朗读"),
    ("hotkey_f10_mute", "紧急静音", "Ctrl+Alt+M（原 F10），清队并暂停朗读", "朗读"),
    ("hotkey_library", "跳到语言库", "Ctrl+Alt+L，直接抬出 Library 页", "学习"),
    ("hotkey_selftest", "切换自测模式", "Ctrl+Alt+T，学习时一键遮住译文", "学习"),
    ("hotkey_history", "侧边历史面板", "Ctrl+Alt+H，全屏抓不住光标时的后备入口", "学习"),
    ("hotkey_panic", "强制退出交互", "Ctrl+Alt+Q，逃生键：光标卡在字幕层一按就还回去", "应急"),
    ("hotkey_ctrl_lab", "打开设置面板", "Ctrl+Alt+,（不再用 VS Code 的 Ctrl+,）", "应急"),
    ("hotkey_ctrl_l", "（旧 Ctrl+L，默认停用）", "留空不绑定；浏览器地址栏就是 Ctrl+L", "应急"),
)
HOTKEY_GROUP_ORDER = ("字幕", "捕获", "收藏", "朗读", "学习", "应急")
HOTKEY_GROUP_HINT = {
    "字幕": "字幕条怎么显示、怎么让鼠标回到游戏",
    "捕获": "开始/暂停抓取，以及清屏",
    "收藏": "不切窗口也能把这一句存进语言库",
    "朗读": "TTS 自动朗读与紧急静音",
    "学习": "跳到语言库、自测、历史面板",
    "应急": "出问题时的一键退出",
}

# 当前生效的语种对：写记录时统一从这里取，避免各处再拼 zh/en
CURRENT_LANGS = {"sl": "zh", "tl": "en"}


def set_langs(cfg):
    sl = str((cfg or {}).get("source_lang") or "zh")
    tl = str((cfg or {}).get("target_lang") or "en")
    sl = sl if sl in SOURCE_LANGS else "zh"
    tl = tl if tl in LANG_REGISTRY else "en"
    if sl == tl:                 # 源和目标撞车时，源自动换成另一种可用源语言
        sl = "en" if sl != "en" else "zh"
    CURRENT_LANGS["sl"] = sl
    CURRENT_LANGS["tl"] = tl
    return CURRENT_LANGS


def lang_meta(code):
    code = str(code or "")
    if code in LANG_REGISTRY:
        m = dict(LANG_REGISTRY[code])
        m["code"] = code
        m["kind"] = "target"
        return m
    if code in SOURCE_LANGS:
        m = dict(SOURCE_LANGS[code])
        m["code"] = code
        m["kind"] = "source"
        return m
    return {"code": code, "name": code or "未标", "native": code, "rtl": False,
            "installed": False, "kind": "unknown"}


def lang_name(code):
    return lang_meta(code).get("name") or str(code or "")


def lang_available(code):
    return bool(lang_meta(code).get("installed"))


def pair_label(sl, tl):
    """通用对照标签：源文本 ↔ 目标文本（不再写死「中-英」）。"""
    return "%s → %s" % (lang_name(sl), lang_name(tl))


def lang_font(code, cfg=None):
    """该语种的渲染字体：独立配置 > 注册表推荐 > 全局兜底。"""
    cfg = cfg or {}
    fonts = cfg.get("lang_fonts") or {}
    fam = str(fonts.get(str(code)) or "").strip()
    if fam:
        return fam
    fam = str(lang_meta(code).get("font") or "").strip()
    if fam:
        return fam
    return str(cfg.get("font_family") or "").strip()

PERF_MODES = {
    # 一键档位：一次把缓冲、识别精度、音频切片一起调好
    "high": {"pre_roll_blocks": 10, "save_audio": True, "asr_quality": "accurate",
             "name": "高性能（完整缓存 + 高精度 + 留音频）"},
    "balanced": {"pre_roll_blocks": 8, "save_audio": True, "asr_quality": "accurate",
                 "name": "均衡（中等缓冲，默认）"},
    "eco": {"pre_roll_blocks": 4, "save_audio": False, "asr_quality": "fast",
            "name": "省电 / 低占用（关音频切片 + 小缓冲）"},
}


def group_sessions(rows, gap=120):
    """按时间间隔把一天的句子切成会话：一局游戏 / 一段视频就是一个包。
    间隔超过 gap 秒（或场景换了）就开新会话。"""
    out = []
    cur = None
    last = None
    for r in rows:
        sec = _secs(r.get("t"))
        scene = str(r.get("scene") or "")
        if cur is not None and sec is not None and last is not None:
            jump = sec - last
            if jump < 0:            # 跨零点，按新会话处理
                jump = 86400
            if jump >= gap or (scene and cur["scene"] and scene != cur["scene"]):
                cur = None
        if cur is None:
            cur = {"start": r.get("t", ""), "end": r.get("t", ""), "n": 0,
                   "scene": scene, "rows": []}
            out.append(cur)
        cur["end"] = r.get("t", cur["end"])
        cur["n"] += 1
        cur["rows"].append(r)
        if scene and not cur["scene"]:
            cur["scene"] = scene
        last = sec if sec is not None else last
    return out


def session_label(sess):
    name = SCENE_NAMES.get(sess.get("scene") or "", sess.get("scene") or "未标注")
    return "%s · %s–%s · %d 句" % (name, sess.get("start", ""), sess.get("end", ""), sess.get("n", 0))


# ---------------------------------------------------------------- Utterance 层
# 对外统一按 Utterance（话语单元）读，底层仍旧是 history 的 jsonl；
# 旧记录没有新字段，这里做兼容映射，不迁移、不改盘上数据。
UTTERANCE_FIELDS = ("id", "session_id", "source_text", "source_lang", "target_text",
                    "target_lang", "speaker_id", "context", "tags", "memory_state",
                    "quality", "audio_snippet", "created_at")


def _utt_id(r):
    if r.get("id"):
        return str(r["id"])
    import hashlib
    raw = "|".join(str(r.get(k) or "") for k in ("t", "zh", "en", "spk"))
    return "u-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _session_id(sess, idx):
    return "S" + str(sess.get("start") or idx).replace(":", "").replace("-", "").replace(" ", "")[:14]


def to_utterance(r, rows, i, sid):
    """把一条 history 记录投影成 Utterance；context 取前后各 3 句。"""
    q = str(r.get("qual") or "")
    state = r.get("memory_state") or ("excluded" if q == "error" else "new")
    ctx = []
    for j in range(max(0, i - 3), min(len(rows), i + 4)):
        if j == i:
            continue
        c = rows[j]
        ctx.append({"source_text": c.get("zh", ""), "target_text": c.get("en", ""),
                    "speaker_id": c.get("spk", ""), "created_at": c.get("t", "")})
    return {
        "id": _utt_id(r),
        "session_id": sid,
        "source_text": r.get("source_text") or r.get("zh", ""),
        "source_lang": r.get("source_lang") or "zh",
        "target_text": r.get("target_text") or r.get("en", ""),
        "target_lang": r.get("target_lang") or "en",
        "speaker_id": r.get("speaker_id") or r.get("spk", ""),
        "context": ctx,
        "tags": list(r.get("tags") or ([r["scene"]] if r.get("scene") else [])),
        "memory_state": state,
        "quality": {"native": "ok"}.get(q, q or "ok"),
        "audio_snippet": r.get("audio_snippet"),
        "created_at": r.get("created_at") or r.get("t", ""),
    }


def read_utterances(day=None, limit=800, gap=120):
    rows = read_history(day, limit)
    sessions = group_sessions(rows, gap=gap)
    sid_map = {}
    for k, s in enumerate(sessions):
        sid = _session_id(s, k)
        for r in s["rows"]:
            sid_map[id(r)] = sid
    return [to_utterance(r, rows, i, sid_map.get(id(r), "")) for i, r in enumerate(rows)]


# ---------------------------------------------------------------- 记忆调度（间隔复习）
STEPS = (1, 3, 7, 14, 30)


def _add_days(day, n):
    try:
        t = time.mktime(time.strptime(day, "%Y-%m-%d")) + int(n) * 86400
        return time.strftime("%Y-%m-%d", time.localtime(t))
    except Exception:
        return day


def read_review():
    """读取每个素材的复习状态，后写的覆盖先写的。"""
    out = {}
    try:
        if not REVIEW_PATH.exists():
            return out
        for ln in REVIEW_PATH.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get("id"):
                out[str(r["id"])] = r
    except Exception as e:
        log("读复习状态失败：" + repr(e))
    return out


def mark_review(uid, grade):
    """grade: again / good / known。返回更新后的状态。"""
    recs = read_review()
    old = recs.get(str(uid)) or {}
    step = int(old.get("step") or 0)
    reps = int(old.get("reps") or 0)
    lapses = int(old.get("lapses") or 0)
    today = time.strftime("%Y-%m-%d")
    if grade == "again":
        step = 0
        reps = 0
        lapses += 1
        state = "review"
    elif grade == "known":
        step = len(STEPS) - 1
        reps += 1
        state = "known"
    else:
        step = min(step + 1, len(STEPS) - 1)
        reps += 1
        state = "review"
    days = STEPS[step] if state != "known" else 30
    rec = {"id": str(uid), "state": state, "step": step, "reps": reps,
           "lapses": lapses, "last": today, "due": _add_days(today, days)}
    try:
        with open(REVIEW_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        log("写复习状态失败：" + repr(e))
    return rec


def review_label(uid, recs=None):
    r = (recs or read_review()).get(str(uid)) or {}
    state = r.get("state") or "new"
    if state == "known":
        return "已掌握"
    if state == "review":
        due = str(r.get("due") or "")
        today = time.strftime("%Y-%m-%d")
        return "待复习" if due <= today else "复习中"
    return "新"


def export_session_text(path, sess):
    lines = ["# 会话复盘 · %s" % session_label(sess), ""]
    for r in sess["rows"]:
        spk = (" [%s]" % r["spk"]) if r.get("spk") else ""
        lines += ["[%s]%s" % (r.get("t", ""), spk),
                  "EN  %s" % r.get("en", ""),
                  "ZH  %s" % r.get("zh", ""),
                  ""]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def export_session_srt(path, sess):
    """SRT 给播放器复盘用：每句从它自己的时间点开始，到下一句开始为止
    （最后一句默认 3 秒）。句子是按到达时间记的，够对齐视频里的大致位置。"""
    def srt_ts(sec):
        h = int(sec // 3600); m = int((sec % 3600) // 60)
        s = sec % 60
        return ("%02d:%02d:%06.3f" % (h, m, s)).replace(".", ",")

    rows = sess["rows"]
    out = []
    for i, r in enumerate(rows):
        a = _secs(r.get("t")) or 0
        b = _secs(rows[i + 1].get("t")) if i + 1 < len(rows) else None
        end = b if (b is not None and b > a) else a + 3
        if end - a > 8:
            end = a + 8
        body = "%s\n%s" % (r.get("en", ""), r.get("zh", ""))
        out += [str(i + 1), "%s --> %s" % (srt_ts(a), srt_ts(end)), body, ""]
    Path(path).write_text("\n".join(out), encoding="utf-8")


def read_history(day=None, limit=800):
    out = []
    try:
        if day in (None, time.strftime("%Y-%m-%d")):
            _flush_history_now()      # 批量写盘：读之前先把内存里的落到文件
        p = history_path(day or time.strftime("%Y-%m-%d"))
        if p is None:
            return out
        with _open_history(p) as f:
            lines = f.read().splitlines()[-limit:]
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    except Exception as e:
        log("读历史失败：" + repr(e))
    return out


def search_history(day, query, limit=300):
    """直接在磁盘上逐行搜，命中的最后 limit 条返回，不把整天记录读进内存。"""
    hits = []
    q = (query or "").strip().lower()
    if not q:
        return hits
    try:
        p = history_path(day)
        if p is None:
            return hits
        with _open_history(p) as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                if q in str(r.get("zh", "")).lower() or q in str(r.get("en", "")).lower():
                    hits.append(r)
                    if len(hits) > limit * 4:      # 别无限攒
                        hits = hits[-limit:]
                    continue
                # 多语种：备选译文（同一条挂多个语种/同义词）也参与检索
                for a in (r.get("alts") or []):
                    a_en = a.get("en") if isinstance(a, dict) else str(a)
                    if q in str(a_en or "").lower():
                        hits.append(r)
                        break
    except Exception as e:
        log("搜索历史失败：" + repr(e))
    return hits[-limit:]


def history_days(limit=365):
    """有哪些天有记录，新的在前。压缩过的也算。"""
    try:
        days = set()
        for p in HISTORY_DIR.glob("*.jsonl*"):
            name = p.name
            days.add(name[:-3] if name.endswith(".gz") else name[:-6])
        return sorted(days, reverse=True)[:limit]
    except Exception:
        return []


def housekeeping(keep_days=0):
    """旧日期的记录压成 .gz（体积约为原来的 1/8），只在 keep_days>0 时才删更早的。"""
    try:
        import gzip
        today = time.strftime("%Y-%m-%d")
        ymd = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        freed = 0
        for p in sorted(HISTORY_DIR.glob("*.jsonl")):
            day = p.name[:-6]
            if day in (today, ymd):
                continue                     # 最近两天保持明文，方便直接看
            gz = p.with_suffix(p.suffix + ".gz")
            raw = p.stat().st_size
            with open(p, "rb") as fin, gzip.open(gz, "wb") as fout:
                fout.write(fin.read())
            if gz.exists() and gz.stat().st_size < raw:
                p.unlink()
                freed += raw - gz.stat().st_size
        if keep_days and keep_days > 0:
            cut = time.strftime("%Y-%m-%d", time.localtime(time.time() - keep_days * 86400))
            for p in list(HISTORY_DIR.glob("*.jsonl*")):
                day = p.name[:-3] if p.name.endswith(".gz") else p.name[:-6]
                if day < cut:
                    freed += p.stat().st_size
                    p.unlink()
                    log("按保留期删除更早的记录：" + day)
        if freed:
            log("记录整理：压缩旧日期并回收约 %.1f MB" % (freed / 1048576.0))
    except Exception as e:
        log("记录整理失败：" + repr(e))


LOW_CONF = -0.9      # Whisper 平均 logprob 低于它就算「可能听错」，文字变淡提醒核对


def append_word(word, meaning, sentence="", spk="", low_conf=False, scene="", sess=""):
    """同一个单词只建一张卡：新句子挂到这张卡的语境列表里（上限 12 条），
    复习时可以在这张卡里翻不同语境。同一句话重复收藏不重复挂。"""
    try:
        key = (word or "").strip().lower()
        if not key:
            return False
        if sentence and is_bad_sentence(sentence):
            log("跳过隔离句的生词：%s｜%s" % (word, sentence))
            return False
        rows = read_words()
        hit = next((r for r in rows if str(r.get("w", "")).strip().lower() == key), None)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if hit is not None:
            ctxs = hit.setdefault("ctx", [])
            if any(str(c.get("s", "")).strip() == (sentence or "").strip() for c in ctxs):
                return False
            if sentence:
                ctxs.append({"s": sentence, "t": now, "spk": spk, "scene": scene, "sess": sess})
                hit["ctx"] = ctxs[-12:]
            if not hit.get("m") and meaning:
                hit["m"] = meaning
            if low_conf:
                hit["low_conf"] = True
            if len(rows) and hit is not None:
                rewrite_words(rows)
            log("生词卡已聚合：%s（共 %d 条语境）" % (word, len(hit.get("ctx", []))))
            return True
        row = {
            "t": now, "w": word, "m": meaning, "s": sentence, "spk": spk,
            "source_lang": CURRENT_LANGS["sl"], "target_lang": CURRENT_LANGS["tl"],
            "ctx": ([{"s": sentence, "t": now, "spk": spk, "scene": scene, "sess": sess}]
                    if sentence else []),
        }
        if low_conf:
            row["low_conf"] = True
        with open(WORD_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception as e:
        log("写生词失败：" + repr(e))
        return False


def read_words():
    out = []
    try:
        if WORD_PATH.exists():
            for ln in WORD_PATH.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(ln))
                except Exception:
                    pass
    except Exception as e:
        log("读生词失败：" + repr(e))
    return out


def rewrite_words(rows):
    try:
        WORD_PATH.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""),
            encoding="utf-8")
    except Exception as e:
        log("写生词失败：" + repr(e))


# ---------------------------------------------------------------- 双向句对（两条独立链路）
FILLERS = ("uh", "um", "er", "erm", "like", "you know", "i mean", "sort of", "kind of")


def strip_fillers(text):
    """只作用于存进句对库的文本，实时字幕原样保留（听真实语速用原声切片）。"""
    t = " " + (text or "").strip() + " "
    for f in ("you know", "i mean", "sort of", "kind of"):
        t = t.replace(" " + f + " ", " ")
    words = [w for w in t.split() if w.strip(",.!?;:").lower() not in
             ("uh", "um", "er", "erm", "like")]
    out = " ".join(words).strip().strip(",;:")
    if out and out[0].isalpha():
        out = out[0].upper() + out[1:]
    return out


def _pair_norm(text):
    import re
    t = re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())
    return [w for w in t.split() if w]


def _content_words(text):
    """去掉虚词只留实词：I'm down vs I fell down 的差别就在 fell 这个词上。"""
    stop = {"i", "you", "he", "she", "it", "we", "they", "me", "my", "your", "our",
            "is", "am", "are", "was", "were", "be", "been", "do", "does", "did",
            "the", "a", "an", "to", "of", "in", "on", "at", "for", "and", "or", "but",
            "this", "that", "so", "just", "very", "really"}
    return [w for w in _pair_norm(text) if w not in stop]


def pair_similarity(a, b):
    """两条英文表达有多接近（0~1）。顺序相似 60% + 实词重合 40%，
    这样 I'm down 和 I fell down 不会被当成同一种说法。"""
    import difflib
    wa, wb = _pair_norm(a), _pair_norm(b)
    if not wa or not wb:
        return 0.0
    seq = difflib.SequenceMatcher(None, wa, wb).ratio()
    ca, cb = _content_words(a), _content_words(b)
    if ca and cb:
        jac = len(set(ca) & set(cb)) / float(len(set(ca) | set(cb)))
    else:
        jac = 0.0
    if not ca or not cb:
        return seq * 0.8          # 全是虚词（如 I'm down）时不能给满分
    return 0.6 * seq + 0.4 * jac


def read_pairs():
    out = []
    try:
        if PAIR_PATH.exists():
            for ln in PAIR_PATH.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(ln))
                except Exception:
                    pass
    except Exception as e:
        log("读句对失败：" + repr(e))
    return out


def rewrite_pairs(rows):
    try:
        PAIR_PATH.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""),
            encoding="utf-8")
    except Exception as e:
        log("写句对失败：" + repr(e))


def append_pair(direction, en, zh, scene="", role="", sess="", ctx="", fav=False, raw_en=""):
    """direction: "in" = 外源英文 → 中文（听懂）；"out" = 你说中文 → 英文（会说）。
    两条链路各存各的库，不共用词库；只在“同义对齐”时互相参照，用来提示备选表达。"""
    try:
        en = (en or "").strip()
        zh = (zh or "").strip()
        if not en or not zh:
            return None
        rows = read_pairs()
        # 去重：同方向、同英文（忽略大小写）、同中文意
        for r in rows:
            if r.get("dir") == direction and str(r.get("en", "")).lower() == en.lower() \
                    and str(r.get("zh", "")).strip() == zh:
                return r
        now = time.monotonic()
        row = {
            "t": time.strftime("%Y-%m-%d %H:%M:%S"),
            "dir": direction, "en": en, "zh": zh,
            "raw": raw_en or en,
            "scene": scene, "role": role, "sess": sess, "ctx": (ctx or "")[:80],
            "source_lang": CURRENT_LANGS["sl"], "target_lang": CURRENT_LANGS["tl"],
            "fav": bool(fav), "reuse": 0,
            "rev": {"n": 0, "next": 0.0, "ease": 2.5},
        }
        # 同义对齐校验：主动储备句（中文→英文）和已听过的原生英文比
        if direction == "out":
            natives = [r for r in rows if r.get("dir") == "in"
                       and str(r.get("zh", "")).strip() == zh]
            if natives:
                best = max(pair_similarity(en, r.get("en", "")) for r in natives)
                row["align"] = round(best, 2)
                row["alt"] = best < 0.65
                if row["alt"]:
                    row["alt_note"] = "备选表达，和你听过的原生说法有区别"
        # 对话链：同会话 40 秒内一前一后的两条，串成一组
        chain = None
        for r in reversed(rows[-6:]):
            if r.get("sess") == sess and r.get("dir") != direction:
                try:
                    dt = time.mktime(time.strptime(r["t"], "%Y-%m-%d %H:%M:%S"))
                    if time.time() - dt <= 40:
                        chain = r.get("chain")
                except Exception:
                    pass
                break
        row["chain"] = chain or ("%s-%d" % (sess or time.strftime("%H%M"), int(now * 100) % 100000))
        rows.append(row)
        if len(rows) > 4000:            # 别让文件无限长
            rows = rows[-2000:]
        rewrite_pairs(rows)
        return row
    except Exception as e:
        log("写句对失败：" + repr(e))
        return None


def pair_gaps(rows=None):
    """表达缺口：同一个中文意思，你既听过原生英文、又主动查过英文 → 听懂但说不出。"""
    rows = rows if rows is not None else read_pairs()
    heard = {}
    said = {}
    for r in rows:
        if r.get("isolated"):
            continue
        key = str(r.get("zh", "")).strip()
        if not key:
            continue
        if r.get("dir") == "in":
            heard.setdefault(key, []).append(r)
        else:
            said.setdefault(key, []).append(r)
    return {k: {"heard": heard[k], "said": said[k]} for k in set(heard) & set(said)}


def pragmatic_conflicts(rows=None):
    """同一个中文在不同场景拿到的英文风格差异很大 → 语用边界提示要做。"""
    rows = rows if rows is not None else read_pairs()
    by_zh = {}
    for r in rows:
        by_zh.setdefault(str(r.get("zh", "")).strip(), []).append(r)
    out = []
    for zh, items in by_zh.items():
        ens = [str(r.get("en", "")) for r in items if r.get("en")]
        scenes = set(str(r.get("scene") or "") for r in items)
        if len(ens) >= 2 and len(scenes) >= 2:
            lo = min(pair_similarity(ens[0], e) for e in ens[1:])
            if lo < 0.3:
                out.append({"zh": zh, "items": items})
    return out


def sync_pair_quality(en, zh, qual):
    """把质量标注同步进句对：识别错误的隔离出复习池，机器感重的降权。"""
    key_en = str(en or "").strip()
    key_zh = str(zh or "").strip()
    rows = read_pairs()
    n = 0
    for r in rows:
        if str(r.get("en", "")).strip() == key_en and (
                not key_zh or str(r.get("zh", "")).strip() == key_zh):
            r["qual"] = qual
            r["weight"] = quality_weight(qual)
            if qual == "error":
                r["isolated"] = True
            else:
                r.pop("isolated", None)
            n += 1
    if n:
        rewrite_pairs(rows)
    return n


def bump_pair_count(en, zh, count):
    """重复句合并后，把「听懂」链路上的句对也记上出现次数，复习队列据此优先。"""
    rows = read_pairs()
    changed = False
    for r in rows:
        if r.get("dir") == "in" and str(r.get("en", "")).strip() == str(en).strip():
            if count > int(r.get("count") or 1):
                r["count"] = int(count)
                changed = True
    if changed:
        rewrite_pairs(rows)
    return changed


def pair_review_pool(rows=None):
    """复习池：剔除识别错误的句子；高频重复句排最前，其次按质量权重。"""
    rows = rows if rows is not None else read_pairs()
    pool = [r for r in rows if not r.get("isolated")
            and quality_weight(r.get("qual")) > 0]
    pool.sort(key=lambda r: (-int(r.get("count") or 0), -quality_weight(r.get("qual")),
                             str(r.get("t", ""))))
    return pool


def pair_reuse_rate(rows=None):
    rows = rows if rows is not None else read_pairs()
    total = [r for r in rows if r.get("dir") == "in"]
    used = [r for r in total if int(r.get("reuse") or 0) > 0]
    return (len(used), len(total))


def suggest_reply(scene="", exclude=""):
    """应答预加载：同一场景里，你收集过的、适合接话的英文表达。
    不做模型推理，只从你自己的输出句对里挑——保证推荐的都是你说得出来的话。"""
    rows = read_pairs()
    cands = [r for r in rows if r.get("dir") == "out" and r.get("en")
             and not r.get("isolated")
             and (not scene or r.get("scene") == scene)
             and pair_similarity(r["en"], exclude) < 0.9]
    return [r["en"] for r in cands[-5:]]


def mark_pair_reuse(said_en, threshold=0.8):
    """语音同源匹配：麦克风里说出的一句英文，和你收集过的原生英文对上了 → 记正向反馈。"""
    rows = read_pairs()
    best, hit = 0.0, None
    for r in rows:
        if r.get("dir") != "in":
            continue
        s = pair_similarity(said_en, r.get("en", ""))
        if s > best:
            best, hit = s, r
    if hit is not None and best >= threshold:
        hit["reuse"] = int(hit.get("reuse") or 0) + 1
        hit["reuse_t"] = time.strftime("%Y-%m-%d %H:%M:%S")
        rewrite_pairs(rows)
        return hit
    return None


def load_templates():
    try:
        if TEMPLATE_PATH.exists():
            data = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def save_templates(data):
    try:
        TEMPLATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        log("写应答模板失败：" + repr(e))
        return False


def template_reply(zh):
    """自定义应答模板优先：你说这句话时，直接用你固定记住的说法。"""
    data = load_templates()
    key = (zh or "").strip()
    for k, v in data.items():
        if k.strip() == key and v:
            return str(v[0]).strip()
    return ""



# ---------------------------------------------------------------- 收藏（句子 + 单词）
def append_fav(kind, text, extra="", zh="", en="", spk="", diff=""):
    """kind: line（整句）或 word（单词）。同一个对象不重复收藏。
    diff: 快速难度标记（简单/中等/困难），可空。"""
    try:
        for r in read_fav():
            if r.get("k") == kind and r.get("v") == text:
                return False
        row = {
            "t": time.strftime("%Y-%m-%d %H:%M:%S"),
            "k": kind, "v": text, "x": extra, "zh": zh, "en": en, "spk": spk,
            "source_lang": CURRENT_LANGS["sl"], "target_lang": CURRENT_LANGS["tl"],
        }
        if diff:
            row["diff"] = diff
        with open(FAV_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception as e:
        log("写收藏失败：" + repr(e))
        return False


def read_fav():
    out = []
    try:
        if FAV_PATH.exists():
            for ln in FAV_PATH.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(ln))
                except Exception:
                    pass
    except Exception as e:
        log("读收藏失败：" + repr(e))
    return out


def rewrite_fav(rows):
    try:
        FAV_PATH.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""),
            encoding="utf-8")
    except Exception as e:
        log("写收藏失败：" + repr(e))


# ---------------------------------------------------------------- 朗读（SAPI）
VOICE_HINTS = {"zh": "Huihui", "en": "Zira", "ja": "Haruka", "ko": "Heami",
               "fr": "Hortense", "de": "Hedda", "es": "Helena", "ar": "Naayf"}
class TTSEngine:
    """用系统自带 SAPI 合成一段 WAV 再播放：合成只要几十毫秒，还能读出准确时长，
    拿这个时长去对齐逐词高亮，比边说话边等回调稳得多。"""

    def __init__(self):
        self._voice = None
        self._voices = None
        self._lock = threading.Lock()
        self._n = 0

    def _ensure(self):
        if self._voice is not None:
            return True
        try:
            # Qt 主线程已经定了别的 COM 线程模型，SAPI 只能在朗读线程里自己初始化一遍
            try:
                import ctypes
                ctypes.windll.ole32.CoInitialize(None)
            except Exception:
                pass
            import comtypes.client as cc
            self._voice = cc.CreateObject("SAPI.SpVoice")
            v = self._voice.GetVoices()
            self._voices = [v.Item(i).GetDescription() for i in range(v.Count)]
            # 朗读输出设备：和采集音频源解耦，可单独指定耳机 / 扬声器
            try:
                want = str(getattr(self, "output_device", "") or "").strip()
                if want:
                    outs = self._voice.GetAudioOutputs()
                    for i in range(outs.Count):
                        if want.lower() in outs.Item(i).GetDescription().lower():
                            self._voice.AudioOutput = outs.Item(i)
                            log("朗读输出设备：%s" % outs.Item(i).GetDescription())
                            break
            except Exception:
                pass
            log("朗读就绪（SAPI）：%s" % "、".join(self._voices))
            return True
        except Exception as e:
            log("朗读引擎不可用：" + repr(e))
            return False

    def set_output_device(self, name):
        self.output_device = str(name or "")
        try:
            if self._voice is not None and self.output_device:
                outs = self._voice.GetAudioOutputs()
                for i in range(outs.Count):
                    if self.output_device.lower() in outs.Item(i).GetDescription().lower():
                        self._voice.AudioOutput = outs.Item(i)
                        break
        except Exception:
            pass

    @staticmethod
    def _installed():
        """从注册表读系统音色，避免主线程为了显示名字去碰 COM。"""
        names = []
        try:
            import winreg
            for root in (r"SOFTWARE\Microsoft\Speech\Voices\Tokens",
                         r"SOFTWARE\WOW6432Node\Microsoft\Speech\Voices\Tokens"):
                try:
                    k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root)
                except OSError:
                    continue
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(k, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        desc = winreg.QueryValueEx(
                            winreg.OpenKey(k, sub + r"\Attributes"), "Name")[0]
                        names.append(str(desc))
                    except Exception:
                        pass
        except Exception:
            pass
        return names

    def has_voice(self, lang="en"):
        """该语种是否有可用系统音色（缺包时返回 False）。"""
        if str(lang) == "en":
            return True
        want = VOICE_HINTS.get(str(lang or "en"), "")
        names = (self._voices or []) or self._installed()
        return any(want.lower() in n.lower() for n in names)

    def voice_name(self, lang="en"):
        want = VOICE_HINTS.get(str(lang or "en"), "Zira")
        names = (self._voices or []) or self._installed()
        for n in names:
            if want.lower() in n.lower():
                return n.split(" - ")[0]
        if str(lang) == "en":
            for n in names:
                if "zira" in n.lower() or "david" in n.lower() or "english" in n.lower():
                    return n.split(" - ")[0]
        return (names or ["系统默认"])[0].split(" - ")[0]

    def say(self, text, lang="en", rate=0, voice=None, volume=100):
        """合成到临时 WAV，返回 (路径, 时长秒)。合成完就立刻可以播放。
        voice 可指定该语种独立音色，空则按注册表推荐。volume 独立于系统音量。"""
        if not self._ensure():
            return None, 0.0
        try:
            import comtypes.client as cc
            TTS_DIR.mkdir(parents=True, exist_ok=True)
            want = str(voice or "").strip() or VOICE_HINTS.get(str(lang or "en"), "Zira")
            idx = 0
            for i, n in enumerate(self._voices):
                if want.lower() in n.lower():
                    idx = i
                    break
            if idx == 0 and str(lang) != "en":
                log("TTS 缺少 %s 语音包，暂时用系统默认音色" % lang)
            import hashlib
            key = hashlib.sha1(("%s|%s|%s|%s|%s" % (lang, rate, voice or "", volume, text))
                               .encode("utf-8")).hexdigest()[:18]
            cache_dir = TTS_DIR / "cache"
            cached = cache_dir / ("%s.wav" % key)
            if str(lang) and cached.exists() and cached.stat().st_size > 0:
                import wave
                with wave.open(str(cached), "rb") as w:
                    return str(cached), w.getnframes() / float(w.getframerate())
            with self._lock:
                self._voice.Voice = self._voice.GetVoices().Item(idx)
                self._voice.Rate = max(-4, min(4, int(rate)))
                self._voice.Volume = max(0, min(100, int(volume)))
                self._n += 1
                cache_dir.mkdir(parents=True, exist_ok=True)
                path = cached
                fs = cc.CreateObject("SAPI.SpFileStream")
                fs.Open(str(path), 3, False)
                self._voice.AudioOutputStream = fs
                self._voice.Speak(str(text))
                fs.Close()
            import wave
            with wave.open(str(path), "rb") as w:
                dur = w.getnframes() / float(w.getframerate())
            try:
                files = sorted(cache_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
                if len(files) > 800:
                    for old in files[:-400]:
                        old.unlink(missing_ok=True)
            except Exception:
                pass
            return str(path), dur
        except Exception as e:
            log("朗读合成失败：" + repr(e))
            return None, 0.0

    @staticmethod
    def play(path):
        try:
            import ctypes
            # 用 winmm 直接放：打包后 winsound 模块可能不在，windll 一定在
            ctypes.windll.winmm.PlaySoundW(str(path), None, 0x00020000)   # SND_FILENAME，同步
        except Exception as e:
            log("朗读播放失败：" + repr(e))

    @staticmethod
    def stop():
        try:
            import ctypes
            ctypes.windll.winmm.PlaySoundW(None, None, 0)
        except Exception:
            pass


def _split_units(text):
    """按词切分但保留空格，用来做逐词高亮。"""
    import re
    return re.findall(r"[^\s]+|\s+", text or "")


# ---------------------------------------------------------------- 说话人区分
class SpeakerEncoder:
    """轻量声纹：MFCC 统计量当特征。不下载模型，纯 numpy，CPU 上毫秒级。"""

    def __init__(self, sr=16000, n_fft=512, n_mels=40, n_mfcc=20, win=0.025, hop=0.010):
        import numpy as np
        self.np = np
        self.sr = sr
        self.n_fft = n_fft
        self.n_mels = n_mels
        self.n_mfcc = n_mfcc
        self.win_len = max(64, int(sr * win))
        self.hop_len = max(16, int(sr * hop))
        self.hamming = np.hamming(self.win_len).astype("float32")
        self.mel_fb = self._mel_filterbank().astype("float32")
        self.dct_m = self._dct_matrix().astype("float32")

    def _mel_filterbank(self):
        np = self.np
        def hz2mel(f): return 2595.0 * np.log10(1.0 + f / 700.0)
        def mel2hz(m): return 700.0 * (10 ** (m / 2595.0) - 1.0)
        lo, hi = hz2mel(60.0), hz2mel(self.sr / 2.0)
        pts = mel2hz(np.linspace(lo, hi, self.n_mels + 2))
        bins = np.floor((self.n_fft + 1) * pts / self.sr).astype(int)
        fb = np.zeros((self.n_mels, self.n_fft // 2 + 1))
        for i in range(self.n_mels):
            a, b, c = bins[i], bins[i + 1], bins[i + 2]
            if b <= a:
                b = a + 1
            if c <= b:
                c = b + 1
            fb[i, a:b] = np.linspace(0, 1, b - a, endpoint=False)
            fb[i, b:c] = np.linspace(1, 0, c - b, endpoint=False)
        return fb

    def _dct_matrix(self):
        np = self.np
        k = np.arange(self.n_mfcc)[:, None]
        n = np.arange(self.n_mels)[None, :]
        m = np.cos(np.pi * k * (2 * n + 1) / (2 * self.n_mels))
        m[0] /= np.sqrt(2.0)
        return m * np.sqrt(2.0 / self.n_mels)

    def embed(self, audio):
        np = self.np
        x = np.asarray(audio, dtype="float32").ravel()
        if x.size < self.win_len * 2:
            return None
        x = x - float(x.mean())
        if x.size > 1:
            x = np.concatenate([[x[0]], x[1:] - 0.97 * x[:-1]])
        n_frames = 1 + (x.size - self.win_len) // self.hop_len
        idx = np.arange(self.win_len)[None, :] + self.hop_len * np.arange(n_frames)[:, None]
        frames = x[idx] * self.hamming
        spec = np.abs(np.fft.rfft(frames, n=self.n_fft))
        mel = np.log(spec @ self.mel_fb.T + 1e-10)
        mfcc = mel @ self.dct_m.T
        c = mfcc[:, 1:]
        feat = np.concatenate([c.mean(axis=0), c.std(axis=0), [np.log(float(x.std()) + 1e-8)]])
        n = float(np.linalg.norm(feat))
        return feat / n if n > 0 else None


class SpeakerTracker:
    """在线聚类：跟已有声纹比余弦相似度，够像就算同一人；认不准就不标，别乱归堆。"""

    def __init__(self, threshold=0.62, max_speakers=4):
        import numpy as np
        self.np = np
        self.thr = float(threshold)
        self.max_k = int(max_speakers)
        self.cent = []              # 归一化的质心
        self.n = []                 # 每个质心累计过多少句（用来控制更新的步长）
        self.last_sim = 0.0

    @property
    def centroids(self):
        return list(self.cent)

    def _update(self, i, emb):
        # 步长衰减，最多 1/12：避免某一句噪声把质心拽跑
        alpha = 1.0 / float(min(self.n[i] + 1, 12))
        v = (1.0 - alpha) * self.cent[i] + alpha * emb
        nv = float(self.np.linalg.norm(v))
        self.cent[i] = v / nv if nv > 0 else v
        self.n[i] += 1

    def assign(self, emb, allow_new=True):
        if emb is None:
            return None
        np = self.np
        if not self.cent:
            if not allow_new:
                return None
            self.cent.append(emb.astype("float64"))
            self.n.append(1)
            return 1
        sims = [float(np.dot(emb, c)) for c in self.cent]
        best = int(np.argmax(sims))
        self.last_sim = sims[best]
        if sims[best] >= self.thr:
            self._update(best, emb.astype("float64"))
            return best + 1
        if allow_new and len(self.cent) < self.max_k:
            nv = float(np.linalg.norm(emb))
            self.cent.append(emb.astype("float64") / nv if nv > 0 else emb.astype("float64"))
            self.n.append(1)
            return len(self.cent)
        if len(self.cent) == 1 and sims[best] >= 0.30:
            # 只有一个人被登记过，多半是单人节目/讲述，别因为背景音乐就干脆不标
            self._update(best, emb.astype("float64"))
            return 1
        if sims[best] >= self.thr - 0.12:      # 差得不多：勉强算这个人，但质心几乎不动
            self._update(best, emb.astype("float64"))
            return best + 1
        return None                            # 认不出来就不标，别污染已有的人


# 说话人配色：深色字幕条上用亮一点的，浅色界面用深一点的同色系
SPK_COLORS = ["#7BD8A8", "#F0C674", "#7FB8EA", "#CBA6E9", "#E7A08A", "#9BD0C8"]
SPK_TEXT_COLORS = ["#3B82F6", "#B47B1E", "#2F6FA8", "#7A4EA8", "#A9543B", "#327F76"]
SPK_TINTS = ["#EAF6EF", "#FCF4E4", "#EAF1FB", "#F4ECFA", "#FBEDE7", "#E9F5F3"]


def spk_index(spk):
    try:
        return max(1, int(str(spk).replace("说话人", "").strip() or 1))
    except Exception:
        return 1


def spk_color(spk, palette=None):
    p = palette or SPK_COLORS
    return p[(spk_index(spk) - 1) % len(p)]


def spk_tint(spk):
    return SPK_TINTS[(spk_index(spk) - 1) % len(SPK_TINTS)]


def spk_alias(spk, cfg=None):
    """显示名：重命名过就用别名，否则还是「说话人N」。颜色始终按原始编号算，改名不换色。"""
    if not spk:
        return ""
    names = (cfg or {}).get("spk_names") or {}
    return str(names.get(str(spk)) or spk)


ASR_SIZES = {
    "small": {"desc": "低占用、速度快，游戏时最稳（推荐）", "mb": 480},
    "base": {"desc": "准确率更高，CPU 和内存占用也更高", "mb": 145},
    "medium": {"desc": "更准但延迟约翻倍，需要约 1.5G 显存/内存", "mb": 1500},
}


def asr_model_cached(name):
    """模型是否已经在本地缓存里（faster-whisper 走 HF 镜像下载）。"""
    try:
        if name == "medium" and _local_medium_dir():
            return True
        hub = Path.home() / ".cache" / "huggingface" / "hub"
        for pat in ("models--Systran--faster-whisper-%s" % name,
                    "models--guillaumekln--faster-whisper-%s" % name,
                    "faster-whisper-%s" % name):
            p = hub / pat
            if p.exists():
                snaps = p / "snapshots"
                if snaps.exists():
                    if any(snaps.iterdir()):
                        return True
                elif any(p.iterdir()):
                    return True
    except Exception:
        pass
    return False


def argos_installed():
    """离线翻译包是否存在（不 import argos，避免设置页一开就卡）。"""
    try:
        for p in (Path.home() / ".local" / "share" / "argos-translate" / "packages",
                  Path(os.environ.get("LOCALAPPDATA", "")) / "argos-translate" / "packages"):
            if p.exists() and any(p.iterdir()):
                return True
    except Exception:
        pass
    return False


SPK_MODEL_DIR = Path.home() / ".cache" / "spkrec-ecapa-voxceleb"
SPK_MODEL_FILES = ("hyperparams.yaml", "embedding_model.ckpt", "classifier.ckpt",
                   "label_encoder.txt", "mean_var_norm_emb.ckpt")


def patch_symlink():
    """Windows 普通账户没权限建符号链接，speechbrain 取模型时会撞上；换成复制。"""
    import pathlib
    import shutil
    if getattr(pathlib.Path, "_oversub_patched", False):
        return
    orig_path_link = pathlib.Path.symlink_to
    orig_os_link = os.symlink

    def _safe_path_link(self, target, target_is_directory=False):
        try:
            return orig_path_link(self, target, target_is_directory)
        except OSError:
            src = pathlib.Path(target)
            if src.is_dir():
                shutil.copytree(src, self, dirs_exist_ok=True)
            else:
                shutil.copy2(src, self)

    def _safe_os_link(src, dst, target_is_directory=False, **kw):
        try:
            return orig_os_link(src, dst, target_is_directory, **kw)
        except OSError:
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)

    pathlib.Path.symlink_to = _safe_path_link
    os.symlink = _safe_os_link
    pathlib.Path._oversub_patched = True


class EcapaEncoder:
    """真正能分清人的声纹：speechbrain 的 ECAPA-TDNN（VoxCeleb 预训练）。"""

    threshold = 0.62          # 余弦相似度，实测同人 0.71~0.81 / 不同人 0.33~0.55

    def __init__(self):
        patch_symlink()
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        self._ensure_model()
        from speechbrain.inference.speaker import EncoderClassifier
        self.clf = EncoderClassifier.from_hparams(
            source=str(SPK_MODEL_DIR), savedir=str(SPK_MODEL_DIR), run_opts={"device": "cpu"})

    @staticmethod
    def _ensure_model():
        if (SPK_MODEL_DIR / "embedding_model.ckpt").exists():
            return
        SPK_MODEL_DIR.mkdir(parents=True, exist_ok=True)
        from huggingface_hub import hf_hub_download
        for f in SPK_MODEL_FILES:
            hf_hub_download("speechbrain/spkrec-ecapa-voxceleb", f, local_dir=str(SPK_MODEL_DIR))

    def embed(self, audio):
        import numpy as np
        import torch
        x = np.asarray(audio, dtype="float32")
        if x.size < 8000:                       # 不到 0.5 秒，声纹不可靠
            return None
        x = x - float(x.mean())
        peak = float(np.abs(x).max())
        if peak > 0:
            x = x / peak
        x = torch.tensor(x).unsqueeze(0)
        with torch.no_grad():
            e = self.clf.encode_batch(x).detach().numpy().ravel()
        nn = float(np.linalg.norm(e))
        return e / nn if nn > 0 else None


def make_speaker_encoder():
    """优先 ECAPA；装不上就退回 MFCC 方案（区分度差很多，但聊胜于无）。"""
    try:
        enc = EcapaEncoder()
        log("声纹就绪：ECAPA-TDNN")
        return enc, enc.threshold
    except Exception as e:
        log("ECAPA 声纹不可用，退回 MFCC：" + repr(e))
        log(traceback.format_exc())
        try:
            enc = SpeakerEncoder()
            return enc, 0.97
        except Exception as e2:
            log("MFCC 声纹也不可用：" + repr(e2))
            return None, 0.0


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    raw = {}
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            raw = data if isinstance(data, dict) else {}
            for k, v in data.items():
                if k in cfg and isinstance(cfg[k], dict) and isinstance(v, dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
    except Exception as e:
        log("读取配置失败：" + repr(e))
    # 旧版把 F5~F10 / Ctrl+L / Ctrl+, 当默认键，这些在游戏和浏览器里全是高频键。
    # 没手动改过键位的配置，一次性迁到低冲突方案；改过就尊重用户。
    # 注意：必须看文件里有没有写过 hotkey_scheme，不能看合并后的 cfg（新默认已经是 3）。
    try:
        if int(raw.get("hotkey_scheme") or 0) < 3 and not bool(raw.get("hotkey_custom")):
            for k, v in DEFAULT_CONFIG.items():
                if k.startswith("hotkey_") and k not in ("hotkey_scheme", "hotkey_custom"):
                    cfg[k] = v
            cfg["hotkey_scheme"] = 3
            cfg["hotkey_custom"] = False
            save_config(cfg)
            log("快捷键已迁移到低冲突默认方案（F5~F10 不再默认占用）")
    except Exception as e:
        log("迁移快捷键失败：" + repr(e))
    try:
        set_langs(cfg)          # 写入记录前，先把当前语种对定下来
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        set_langs(cfg)
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log("保存配置失败：" + repr(e))


# ---------------------------------------------------------------- 音频采集
class NoiseGuard:
    """轻量降噪（纯 numpy，不引入 scipy）：
    频域高通砍低频轰鸣，噪声谱减法压稳态 BGM/枪声爆炸；level 0 关 / 1 中 / 2 强。"""

    def __init__(self, level=0):
        self.level = int(level or 0)
        self.noise = None
        self.win = None
        self.freq = None

    def process(self, block, sr=16000):
        import numpy as np
        x = np.asarray(block, dtype="float32").reshape(-1)
        if self.level <= 0 or len(x) < 256:
            return x
        if self.win is None or len(self.win) != len(x):
            self.win = np.hanning(len(x)).astype("float32")
            self.freq = np.fft.rfftfreq(len(x), 1.0 / sr)
        sp = np.fft.rfft(x * self.win)
        mag = np.abs(sp)
        if self.noise is None or len(self.noise) != len(mag):
            self.noise = mag.copy()
        # 噪声谱：往下跟得快、往上涨得慢，免得把人声也学成噪声
        self.noise = np.where(mag < self.noise,
                              0.7 * self.noise + 0.3 * mag,
                              0.98 * self.noise + 0.02 * mag)
        gain = np.clip((mag - (1.8 if self.level >= 2 else 1.0) * self.noise) / (mag + 1e-9),
                       0.0, 1.0)
        gain = (0.2 + 0.8 * gain) if self.level >= 2 else (0.55 + 0.45 * gain)
        keep = np.ones_like(mag)
        if self.freq is not None:
            keep[self.freq < 120] = 0.0       # 低频轰鸣（爆炸、BGM 低频、风扇）
        out = np.fft.irfft(sp * gain * keep, n=len(x))
        return (out * 2.0).astype("float32")


class AudioCapture(threading.Thread):
    """抓系统回放（扬声器 loopback），按音量断句。"""

    SR = 16000
    BLOCK = 3200          # 0.2 秒
    PRE_ROLL = 6          # 1.2 秒前置缓存：断句点往前多带一点，句头不容易切掉
    TAIL = 1              # 断句时保留 0.2 秒尾音，句尾不容易截断
    MAX_LEN = 25          # 默认 5 秒语音强切；latency_mode 会按快速/均衡/准确再调
    SILENCE = 2           # 连续 0.4 秒静音断句

    MODE_SEC = {"fast": 3.2, "balanced": 4.2, "accurate": 5.5}   # 语音时长，不含前置缓存

    def __init__(self, cfg, out_queue, on_state):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.q = out_queue
        self.on_state = on_state
        self.running = False
        self._stop = threading.Event()
        self.last_audio = None          # 最近一句的原始音频，收藏时可以切片存下来
        self.last_meta = None           # 最近一句的时间戳 {"t0","t1","dur"}

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            import numpy as np
            import soundcard
        except Exception as e:
            self.on_state("error", "缺少依赖：" + repr(e))
            return

        device_name = (self.cfg.get("loopback_device") or "").strip()
        mic = None
        try:
            if device_name:
                mic = soundcard.get_microphone(device_name, include_loopback=True)
            else:
                spk = soundcard.default_speaker()
                mic = soundcard.get_microphone(spk.name, include_loopback=True)
        except Exception as e:
            log("打开回放设备失败：" + repr(e))
            # 设备被拔 / 改名：按默认扬声器 → 其它回放设备顺序回退，不让采集直接停
            candidates = []
            try:
                candidates.append(soundcard.default_speaker().name)
            except Exception:
                pass
            try:
                for m in soundcard.all_microphones(include_loopback=True):
                    if "Loopback" in str(m.name) and str(m.name) not in candidates:
                        candidates.append(str(m.name))
            except Exception:
                pass
            for name in candidates:
                try:
                    mic = soundcard.get_microphone(str(name), include_loopback=True)
                    if mic is not None:
                        self.cfg["loopback_device"] = str(name)
                        log("音频设备回退到：" + str(name))
                        self.on_state("warn", "原音频设备不可用，已回退到：" + str(name))
                        break
                except Exception:
                    mic = None
            if mic is None:
                self.on_state("error", "音频设备都打不开，检查系统输出设备")
                return

        thresh = float(self.cfg.get("sensitivity") or 0.006)
        # VAD 灵敏度：高＝小声也能触发，低＝只认明显的语音，压住背景噪声
        vad = str(self.cfg.get("vad_sensitivity") or "medium").lower()
        mult = {"high": 0.5, "medium": 1.0, "low": 1.8}.get(vad, 1.0)
        thresh = max(0.0012, thresh * mult)
        silence_need = 3 if vad == "low" else self.SILENCE
        latency_mode = str(self.cfg.get("latency_mode") or "balanced").lower()
        if latency_mode == "fast":
            silence_need = 1             # 0.2 秒就断：连麦短句更快出字幕，长句可能切得碎一点
        max_blocks = int(self.MODE_SEC.get(latency_mode, 5.0) * self.SR / self.BLOCK)
        log("断句模式：%s（语音最长 %.1f 秒 / 静音 %.1f 秒）"
            % (latency_mode, max_blocks * self.BLOCK / self.SR, silence_need * self.BLOCK / self.SR))
        guard = NoiseGuard(int(self.cfg.get("noise_level") or 0))
        # 前置缓存长度跟着性能档位走：省电档少留一点，少占内存和拷贝
        pre_roll = max(3, int(self.cfg.get("pre_roll_blocks") or self.PRE_ROLL))
        if guard.level:
            log("降噪：%s（高频通透 + 稳态噪声压制）" % ("强" if guard.level >= 2 else "中等"))
        self.running = True
        self.on_state("listening", "正在听：" + str(mic.name))

        try:
            with mic.recorder(samplerate=self.SR, channels=1) as rec:
                pre = []
                buf = None
                silence = 0
                speech_blocks = 0          # 这一段里真正有声的块数（不含前置缓存）
                recent_levels = []          # 自适应底噪：只看最近 1 秒的最安静块，避免系统回放底噪一直在阈值上
                noise_floor = 0.01
                pos = 0                     # 已读样本总数（做时间戳用）
                base = time.time()          # 采集起点，换算成墙钟时间
                seg_start = 0
                while not self._stop.is_set():
                    block = rec.record(numframes=self.BLOCK)
                    if block is None or len(block) == 0:
                        continue
                    if guard.level:                      # 先压噪，再算音量判句
                        block = guard.process(block).reshape(-1, 1)
                    n = len(block)
                    # 每秒重读一次参数：改降噪 / VAD / 阈值不用重启收音
                    if pos % self.SR < n:
                        try:
                            vad = str(self.cfg.get("vad_sensitivity") or "medium").lower()
                            mult = {"high": 0.5, "medium": 1.0, "low": 1.8}.get(vad, 1.0)
                            thresh = max(0.0012, float(self.cfg.get("sensitivity") or 0.006) * mult)
                            silence_need = 3 if vad == "low" else self.SILENCE
                            latency_mode = str(self.cfg.get("latency_mode") or "balanced").lower()
                            if latency_mode == "fast":
                                silence_need = 1
                            max_blocks = int(self.MODE_SEC.get(latency_mode, 5.0) * self.SR / self.BLOCK)
                            pre_roll = max(3, int(self.cfg.get("pre_roll_blocks") or self.PRE_ROLL))
                            lv = int(self.cfg.get("noise_level") or 0)
                            if lv != guard.level:
                                guard.level = lv
                                guard.noise = None
                                log("降噪档位切到：%s" % (["关", "中等", "强"][min(lv, 2)]))
                        except Exception:
                            pass
                    level = float(np.sqrt(np.mean(np.square(block))))
                    gate = max(thresh, noise_floor * 1.5)
                    if buf is None:
                        # 系统 loopback 往往有 0.006 左右的数字底噪，刚好压在原阈值上：
                        # 不抬门槛就会把底噪当人声，永远等不到静音断句，只能每 6 秒强切。
                        recent_levels.append(level)
                        if len(recent_levels) > 5:
                            recent_levels.pop(0)
                        noise_floor = min(recent_levels)
                        gate = max(thresh, noise_floor * 1.5)
                        if level > gate:
                            # 前置缓存最多 2 秒（10 块）：句头被静音切掉的概率大幅降低
                            buf = np.concatenate(pre + [block], axis=0)
                            seg_start = pos - len(pre) * self.BLOCK
                            pre = []
                            silence = 0
                            speech_blocks = 1
                        else:
                            pre.append(block)
                            if len(pre) > pre_roll:
                                pre.pop(0)
                    else:
                        buf = np.concatenate((buf, block), axis=0)
                        voiced = level > gate * 0.85
                        silence = 0 if voiced else silence + 1
                        if voiced:
                            speech_blocks += 1
                        if silence >= silence_need or len(buf) >= (max_blocks + pre_roll) * self.BLOCK:
                            # 1 块≈0.2 秒。少于 3 块（0.6 秒）的多半是门限被背景音碰了一下，
                            # 送进 ASR 只会得到“保留口语”这类提示词幻觉；直接丢掉，不生成字幕。
                            if speech_blocks < 3:
                                buf = None
                                pre = []
                                silence = 0
                                speech_blocks = 0
                                continue
                            trim = max(0, silence - self.TAIL) * self.BLOCK   # 留一点尾音
                            if trim:
                                buf = buf[: len(buf) - trim]
                            if len(buf) > self.BLOCK:
                                seg = buf.reshape(-1).astype("float32")
                                seg_end = pos + n - trim
                                t0 = base + seg_start / float(self.SR)
                                t1 = base + max(seg_start, seg_end) / float(self.SR)
                                meta = {"t0": t0, "t1": t1,
                                        "dur": len(seg) / float(self.SR),
                                        "label": "%s–%s" % (time.strftime("%H:%M:%S", time.localtime(t0)),
                                                            time.strftime("%H:%M:%S", time.localtime(t1)))}
                                self.last_audio = seg
                                self.last_meta = meta
                                log("切片 %.1f 秒（%s）" % (meta["dur"],
                                    "静音断句" if silence >= silence_need else "长句强切"))
                                try:
                                    self.q.put_nowait((seg, meta))
                                except queue.Full:
                                    try:
                                        self.q.get_nowait()
                                    except queue.Empty:
                                        pass
                                    try:
                                        self.q.put_nowait((seg, meta))
                                    except queue.Full:
                                        pass
                                    self.dropped = getattr(self, "dropped", 0) + 1
                            buf = None
                            silence = 0
                    pos += n
        except Exception as e:
            self.on_state("warn", "音频流中断，点「开始捕获」重新采集")
            log("采集异常：" + traceback.format_exc())
        finally:
            self.running = False


# ---------------------------------------------------------------- 识别 + 翻译
HALLUCINATION_WORDS = (
    "字幕", "Amara", "amara", "感谢观看", "请不吝点赞", "点赞", "订阅", "打赏",
    "明镜", "点点", "关注", "索利", "Solya", "MING PAO", "字幕由", "YouTube",
    "简体中文转写", "普通话的句子",
)

# 纯语气词/应答：队友连麦时这类断句碎块极多，单独成行只会刷屏，直接丢掉
FILLER_ONLY = {
    "嗯", "嗯嗯", "啊", "哦", "噢", "呃", "唉", "诶", "好", "好的", "是", "是的", "对", "对的",
    "行了", "好吧", "哈哈", "哈哈哈", "笑死", "谢谢", "OK", "ok", "OK了", "收到", "走", "上",
}


# 游戏术语先用标准英文替换再交给 Argos：否则 "炼狱小镇/沙鹰" 会被逐字翻成
# purgatory town / sandhawk，识别明明对了、英文字幕照样看不懂。
GAME_TERMS_ZH_EN = (
    ("炼狱小镇", "Inferno"), ("炼狱", "Inferno"), ("荒漠迷城", "Mirage"),
    ("炙热沙城", "Dust II"), ("沙漠二", "Dust II"), ("沙二", "Dust II"),
    ("核子危机", "Nuke"), ("远古遗迹", "Ancient"),
    ("列车停放站", "Train"), ("列车停放", "Train"), ("列车站", "Train"),
    ("死亡游乐园", "Overpass"), ("游乐园", "Overpass"), ("殒命大厦", "Vertigo"),
    ("大厦", "Vertigo"), ("阿努比斯", "Anubis"),
    ("大沙鹰", "Desert Eagle"), ("沙鹰", "Desert Eagle"), ("大狙", "AWP"),
    ("鸟狙", "SSG 08"), ("连狙", "auto-sniper"),
    ("烟雾弹", "smoke grenade"), ("闪光弹", "flashbang"),
    ("燃烧弹", "molotov"), ("手雷", "HE grenade"), ("拆包", "defuse the bomb"),
    ("下包", "plant the bomb"), ("保枪", "save the gun"), ("经济局", "eco round"),
    ("强起", "force buy"), ("半起", "half buy"), ("转点", "rotate"),
    ("A大", "A long"), ("A小道", "A short"), ("A小", "A short"),
    ("B点", "B site"), ("A点", "A site"),
    ("中路", "mid"), ("长道", "long"), ("短道", "short"), ("小道", "short"),
    ("香蕉道", "banana"), ("警家", "CT spawn"), ("匪家", "T spawn"),
    ("拱门", "arch"), ("棺材", "coffin"), ("白车", "white car"),
    ("书房", "library"), ("市场", "market"), ("锅炉房", "boiler"),
    ("下水道", "sewer"), ("阳台", "balcony"), ("连接", "connector"),
    ("拉枪", "trade frag"), ("补枪", "trade frag"), ("直架", "hold the angle"),
    ("爆头", "headshot"), ("残血", "low HP"), ("大残", "low HP"),
    ("空枪", "missed shot"), ("压枪", "recoil control"), ("扫射", "spray"),
    ("点射", "tap fire"), ("背闪", "turn from flash"), ("高闪", "high flash"),
)


CONTEXT_ONLY_TERMS = {"炼狱", "大厦", "游乐园", "小道", "长道", "短道", "市场",
                      "书房", "阳台", "连接", "列车停放", "列车站"}
STRONG_GAME_MARKERS = ("沙鹰", "大狙", "鸟狙", "连狙", "烟雾弹", "闪光弹", "燃烧弹",
                       "下包", "拆包", "保枪", "经济局", "强起", "转点", "爆头",
                       "残局", "大残", "香蕉道", "警家", "匪家", "拱门", "棺材",
                       "白车", "锅炉房", "下水道", "炼狱小镇", "荒漠迷城", "炙热沙城",
                       "核子危机", "远古遗迹", "死亡游乐园", "殒命大厦", "阿努比斯",
                       "A大", "A小", "A点", "B点", "拉枪", "补枪", "直架")


EN_POST_FIXES = (
    ("Dexter Eagle", "Desert Eagle"), ("dexter eagle", "Desert Eagle"),
    ("Sandhawk", "Desert Eagle"), ("sandhawk", "Desert Eagle"),
    ("rottate", "rotate"), ("Rottate", "Rotate"),
    ("mode frag", "trade frag"), ("Mode frag", "Trade frag"),
)


def fix_en_terms(en):
    s = en or ""
    for bad, good in EN_POST_FIXES:
        s = s.replace(bad, good)
    return s


def _bad_repeat(en):
    """Argos 遇到又长又口语的中文时，会退化成 “No, no, no…” 或整句复读。
    这种译文比不翻更糟，检测出来就改走分句翻译。"""
    import re as _re
    from collections import Counter
    words = _re.findall(r"[A-Za-z']+", (en or "").lower())
    if len(words) < 8:
        return False
    top = Counter(words).most_common(1)[0][1]
    if top / float(len(words)) >= 0.45:
        return True
    if len(words) >= 12 and len(set(words)) <= 3:
        return True
    return False


def translate_guarded(tr, zh, sl="zh", tl="en"):
    """整句先翻；退化成复读就按逗号/句号拆开逐段翻，再拼回去。
    源语言不是中文时走直翻：游戏术语替换和英文复读检测都不适用。"""
    if tr is None:
        return ""
    if str(sl) != "zh":
        try:
            return (tr.translate(zh) or "").strip()
        except Exception:
            return ""
    import re as _re
    src = prep_zh_for_translation(zh)
    try:
        en = fix_en_terms(tr.translate(src))
    except Exception:
        en = ""
    if en and not _bad_repeat(en):
        return en
    chunks = [c.strip() for c in _re.split(r"[，。！？；,.!?;]", zh) if len(c.strip()) >= 2]
    if len(chunks) <= 1:
        return en
    parts = []
    for c in chunks:
        try:
            t = fix_en_terms(tr.translate(prep_zh_for_translation(c))).strip()
        except Exception:
            t = ""
        if t and not _bad_repeat(t) and t not in parts:
            parts.append(t)
    if parts:
        return " ".join(parts)
    return en


def prep_zh_for_translation(zh):
    """只替换源文里的游戏术语，源字幕仍显示中文，英文译文用标准叫法。
    像“大厦 / 游乐园 / 炼狱”这种日常词，只有同句里出现明显游戏术语时才替换，
    避免把电视剧里的“大厦”翻成 Vertigo。"""
    s = zh
    game_ctx = any(k in zh for k in STRONG_GAME_MARKERS)
    for zh_term, en_term in GAME_TERMS_ZH_EN:
        if zh_term in s:
            if zh_term in CONTEXT_ONLY_TERMS and not game_ctx:
                continue
            s = s.replace(zh_term, en_term)
    return s


def find_cublas_dir():
    """找 cuBLAS 运行库。CTranslate2 的 CUDA 路径只缺这两个 DLL，找到就能用显卡。
    优先 exe 同级的 cuda_libs（绿色包），其次 %LOCALAPPDATA%，最后 site-packages。"""
    import sys
    bases = []
    try:
        bases.append(os.path.dirname(os.path.abspath(sys.executable)))
    except Exception:
        pass
    try:
        bases.append(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass
    try:
        bases.append(os.getcwd())
    except Exception:
        pass
    cands = [os.path.join(b, "cuda_libs", "nvidia", "cublas", "bin") for b in bases]
    cands += [os.path.join(b, "cuda", "bin") for b in bases]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        cands += [os.path.join(meipass, "cuda"), os.path.join(meipass, "cuda", "bin")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(os.path.join(local, "Oversub", "cuda"))
    for p in list(sys.path):
        if p and os.path.isdir(p):
            cands.append(os.path.join(p, "nvidia", "cublas", "bin"))
    for d in cands:
        try:
            if (os.path.isfile(os.path.join(d, "cublas64_12.dll"))
                    or os.path.isfile(os.path.join(d, "cublasLt64_12.dll"))):
                return d
        except Exception:
            continue
    return ""


def _local_medium_dir():
    """本地 models\\medium 是否存在；返回目录，没装返回 None。"""
    import sys
    bases = []
    try:
        bases.append(os.path.dirname(os.path.abspath(sys.executable)))
    except Exception:
        pass
    try:
        bases.append(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass
    for b in bases:
        p = os.path.join(b, "models", "medium")
        if os.path.isfile(os.path.join(p, "model.bin")):
            return p
    return None


def default_asr_model():
    """auto 的实际落点：有显卡且本地有 medium 就用 medium（中文比 turbo 准一档），
    否则有显卡用 turbo，没显卡退回 small。"""
    if not find_cublas_dir():
        return "small"
    return "medium" if _local_medium_dir() else "turbo"


def resolve_model_id(name):
    """把 auto/medium 解析成实际模型路径；medium 优先用随包/本地 models 目录。"""
    if name in ("", "auto"):
        return resolve_model_id(default_asr_model())
    if name == "medium":
        p = _local_medium_dir()
        if p:
            return p
    return name



class Pipeline(threading.Thread):
    def __init__(self, cfg, seg_queue, ui_queue, on_state, rec_queue=None, tts_busy=None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.q = seg_queue
        self.ui = ui_queue
        self.rec = rec_queue
        self.tts_busy = tts_busy          # 朗读期间不收音，免得把自己的声音又识别回来
        self.on_state = on_state
        self.model = None
        self.translator = None
        self.fallback_tr = None          # 在线不可用时的本地备用
        self.t2s = None                 # 繁 -> 简 转换器
        self.spk_enc = None             # 声纹编码器
        self.spk_trk = None             # 在线聚类
        self.ts = None                  # 翻译线程
        self._stop = threading.Event()
        self.last_zh = ""

    def stop(self):
        self._stop.set()
        if self.ts is not None:
            self.ts.stop()
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass

    def _load_whisper(self):
        import numpy as np
        from faster_whisper import WhisperModel

        want = self.cfg.get("device") or "auto"
        name = self.cfg.get("whisper_model") or "auto"
        effective = resolve_model_id(name)
        # medium 用 int8_float16：显存占用约一半，实测速度和 float16 基本一致，
        # 6GB 的 3060 在游戏/视频同时占显存时也放得下。
        gpu_ct = "int8_float16" if os.path.basename(str(effective)).lower() == "medium" else "float16"
        # 高精度模型可能还没下载完 / 磁盘不够：失败时退回已缓存的 small，
        # 至少保证采集能继续，不要让整个识别线程起不来。
        names = [effective] + (["small"] if effective not in ("small", "base") else [])
        attempts = []
        if want in ("auto", "cuda"):
            # 打包环境常常缺 cuBLAS：先找运行库并显式加载，缺了就直接 CPU，
            # 别等模型加载完、推第一句时才炸（用户会表现为「说了没反应」）
            try:
                import ctypes
                dll_dir = find_cublas_dir()
                if dll_dir:
                    os.add_dll_directory(dll_dir)
                    os.environ["PATH"] = dll_dir + os.pathsep + os.environ.get("PATH", "")
                    ctypes.WinDLL(os.path.join(dll_dir, "cublas64_12.dll"))
                    for extra in ("cublasLt64_12.dll", "nvblas64_12.dll"):
                        p = os.path.join(dll_dir, extra)
                        if os.path.isfile(p):
                            ctypes.WinDLL(p)
                    log("已加载显卡加速库：" + dll_dir)
                    attempts.append(("cuda", gpu_ct))
                else:
                    ctypes.WinDLL("cublas64_12.dll")
                    attempts.append(("cuda", gpu_ct))
            except OSError:
                log("未找到 cuBLAS，跳过显卡，直接用 CPU 识别")
        attempts.append(("cpu", "int8"))

        # 2 秒极低幅噪，逼它真做一次推理：显卡缺 cuBLAS/cuDNN 的错误在推理时才暴露
        dummy = (np.random.uniform(-0.02, 0.02, 32000)).astype("float32")
        sl_pre = str(CURRENT_LANGS.get("sl") or "zh")
        for nm in names:
            for dev, ct in attempts:
                try:
                    self.ui.put(("state", "正在加载识别模型 %s…" % nm, ""))
                    m = WhisperModel(nm, device=dev, compute_type=ct)
                    list(m.transcribe(dummy, language=sl_pre, beam_size=1, vad_filter=False))
                    # VAD 的 onnx 是懒加载：不预热的话，第一句真实语音要额外等 1.3～1.5 秒。
                    # 用同一段极低幅噪把 VAD 也跑一次，首句就和后续一样快。
                    try:
                        list(m.transcribe(dummy, language=sl_pre, beam_size=1, vad_filter=True,
                                          vad_parameters=dict(min_silence_duration_ms=450,
                                                              speech_pad_ms=420)))
                    except Exception as e:
                        log("VAD 预热失败（不影响识别）：%r" % e)
                    log("Whisper 就绪：%s / %s / %s" % (nm, dev, ct))
                    # 记下真正加载成功的模型，状态栏显示实际档位，而不是配置里的 auto
                    self.asr_model_loaded = os.path.basename(str(nm).rstrip("\\/")) or str(nm)
                    self.asr_device = dev
                    self.ui.put(("state", "识别就绪（%s · %s）" % (self.asr_model_loaded, dev), "ok"))
                    return m
                except Exception as e:
                    log("Whisper(%s/%s/%s) 不可用：%r" % (nm, dev, ct, e))
            if len(names) > 1:
                log("%s 加载失败，尝试退回 small" % nm)
        raise RuntimeError("语音模型加载失败，看 oversub.log")

    def _load_translator(self):
        try:
            from argostranslate import translate as argos
        except Exception as e:
            if self.cfg.get("engine") == "online" and self.cfg.get("api_key"):
                log("离线翻译不可用，在线引擎照常工作：" + repr(e))
                return None
            raise
        langs = argos.get_installed_languages()
        sl = CURRENT_LANGS.get("sl", "zh")
        tl = CURRENT_LANGS.get("tl", "en")
        src = next((l for l in langs if l.code == sl), None)
        dst = next((l for l in langs if l.code == tl), None)
        # 双向都建：说话时语种会来回切，方向必须按句挑，只锁一个包就会出现“英文不翻中文”
        self.tr_map = {}
        for a, b in ((sl, tl), (tl, sl)):
            la = next((l for l in langs if l.code == a), None)
            lb = next((l for l in langs if l.code == b), None)
            if la is None or lb is None:
                continue
            try:
                t2 = la.get_translation(lb)
            except Exception:
                t2 = None
            if t2 is not None:
                self.tr_map[(a, b)] = t2
        if not src or not dst:
            if self.cfg.get("engine") == "online" and self.cfg.get("api_key"):
                log("离线备用翻译没装：在线引擎仍可用")
                return None
            raise RuntimeError("离线翻译包没装：%s→%s 语言包缺失" % (sl, tl))
        tr = self.tr_map.get((sl, tl)) or src.get_translation(dst)
        if tr is None:
            if self.cfg.get("engine") == "online" and self.cfg.get("api_key"):
                return None
            raise RuntimeError("离线翻译包没装：%s→%s 路径不存在" % (sl, tl))
        # 两个方向都预热一次，不然切方向后的第一句要等十几秒
        for (a, b), t2 in self.tr_map.items():
            try:
                t2.translate("你好" if a == "zh" else "hello")
            except Exception:
                pass
        if self.cfg.get("engine") == "online" and self.cfg.get("api_key"):
            self.fallback_tr = tr          # 网络抖动时无缝切回本地
            log("离线备用翻译就绪（在线优先）")
            return None
        log("离线翻译就绪")
        return tr

    def _translate_online(self, text, sl=None, tl=None):
        import urllib.request
        base = (self.cfg.get("base_url") or "").rstrip("/")
        if not base:
            return None
        sl_cur = sl or CURRENT_LANGS.get("sl", "zh")
        tl_cur = tl or CURRENT_LANGS.get("tl", "en")
        if sl_cur == "en" and tl_cur == "zh":
            sys_prompt = ("你是字幕翻译。把英文口语翻成简洁自然的简体中文字幕："
                          "只输出中文正文，不加引号、不加解释，保留说话人的口气，"
                          "游戏术语用玩家习惯的说法。")
        else:
            sys_prompt = ("你是影视字幕翻译。把中文翻成简洁自然的英语字幕：只输出英文正文，"
                          "不加引号、不加解释，一句尽量不超过 20 个词，保留说话人的口气。")
        body = json.dumps({
            "model": self.cfg.get("model") or "deepseek-chat",
            "temperature": 0.2,
            "max_tokens": 400,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": text},
            ],
        }).encode("utf-8")
        req = urllib.request.Request(base + "/chat/completions", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + self.cfg.get("api_key"))
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data["choices"][0]["message"]["content"] or "").strip()

    def _run_asr(self, audio):
        # accurate：多路候选 + 先做 VAD 切掉静音段（识别准，CPU 上慢一点）；
        # fast：原来的单路快解，赶时间时用。VAD 的 onnx 模型缺席时自动降级重跑。
        import numpy as np
        try:
            x = np.asarray(audio, dtype="float32").reshape(-1)
            x = x - float(x.mean())
            peak = float(np.max(np.abs(x))) if x.size else 0.0
            if peak > 0.0005:
                x *= min(4.0, 0.92 / peak)      # 游戏原声偏小时补增益，最多 4 倍
            audio = np.clip(x, -1.0, 1.0)
        except Exception:
            pass
        # 先快速判语种：用户会中英混说，固定按配置语种解码会把英文当中文翻，
        # 结果就是“说英文只出英文”。只采信 zh/en 且置信度够的结果，其余回退配置语种。
        sl_cfg = CURRENT_LANGS.get("sl", "zh")
        det, prob = sl_cfg, 0.0
        try:
            # 注意：这里不能传 vad_parameters=dict(...)，detect_language 只认 VadOptions，
            # 传 dict 会抛 'dict' object has no attribute 'threshold'（实测 faster-whisper 1.2.1）
            d, p, _alts = self.model.detect_language(audio, vad_filter=True)
            if float(p) >= 0.55 and d in ("zh", "en"):
                det, prob = d, float(p)
        except Exception as e:
            log("语种检测失败，按配置语种识别：" + repr(e))
        self.last_det_lang = det
        self.last_det_prob = prob
        sl = det
        # 热词是**提示词**，不是白名单：静音 / 听不清时模型会照着词表“念”，
        # 于是出现“我没说阿鲁比斯，它却打出阿鲁比斯”。所以默认只吃用户自己填的词，
        # 内置大词表和上一句实词都改成 opt-in（asr_hotwords_mode = custom / full / off）。
        mode = str(self.cfg.get("asr_hotwords_mode") or "custom")
        hotwords = ""
        custom = re.sub(r"[，,、;；\n]+", " ", str(self.cfg.get("hotwords_custom") or "")).strip()
        if mode in ("custom", "full"):
            hotwords = custom
        if mode == "full":
            hotwords = (ASR_HOTWORDS.get(sl, "") + " " + custom).strip()
            ctx = ""
            try:
                prev = str(self.last_zh or "").strip()
                if prev and sl == "zh":
                    ctx = re.sub(r"[。，！？、,.!?…\s”“]", "", prev)[-10:]
                elif prev:
                    ctx = " ".join(re.findall(r"[A-Za-z']+", prev)[-6:])
            except Exception:
                ctx = ""
            if ctx:
                hotwords = (hotwords + " " + ctx).strip()
        # 热词会占解码上下文，超长反而拖慢且挤掉句子本身
        hotwords = hotwords[:300]
        common = dict(language=(sl if sl in ("zh", "en") else "zh"),
                      condition_on_previous_text=False,
                      without_timestamps=True,
                      temperature=[0.0, 0.2, 0.4],
                      repetition_penalty=1.1, no_repeat_ngram_size=3)
        if hotwords:
            common["hotwords"] = hotwords
        # 不再给 initial_prompt：medium 在 3 秒左右的真实片段上会把提示词原样念出来
        # （实测出现“普通话口词”“保留口语”），比错一两个词更伤阅读。
        if str(self.cfg.get("asr_quality") or "accurate") == "fast":
            tries = [dict(beam_size=1, vad_filter=False)]
        else:
            # 静音阈值从 500 降到 450：口语连读常被切在词中间，切碎后单个短片段解码
            # 最容易出同音错词；补边从 300 提到 420，把句首尾的气口留给模型。
            tries = [dict(beam_size=5, vad_filter=True,
                          vad_parameters=dict(min_silence_duration_ms=450, speech_pad_ms=420)),
                     dict(beam_size=5, vad_filter=False),
                     dict(beam_size=1, vad_filter=False)]
        last = None
        for kw in tries:
            try:
                segs, _info = self.model.transcribe(audio, **common, **kw)
                # 必须在这里就把生成器吃完：显卡缺库的错误是在遍历段时抛的
                segs = list(segs)
                bad = ("保留口语", "不要翻译", "逐字转写", "以下是普通话", "请按简体中文",
                       "普通话口词", "口语词，")
                out = []
                for s in segs:
                    # 丢掉模型自己都不信是语音的段（纯音乐、静音上的幻觉大多来自这里）
                    if getattr(s, "no_speech_prob", 0) > 0.6 and getattr(s, "avg_logprob", 0) < -0.7:
                        continue
                    if getattr(s, "avg_logprob", 0) < -1.05:
                        continue
                    t = (getattr(s, "text", "") or "").strip()
                    # 提示词被原样念出来的幻觉段：宁可这半秒不出字幕，也不要污染上下文
                    if any(k in t for k in bad):
                        log("丢弃提示词幻觉段：%s" % t[:40])
                        continue
                    # 整句幻觉（“hot”“Thank you.”）：静音 / 环境音解码出来的短句，
                    # 不丢就会进翻译、进历史，看起来就像“莫名其妙翻译了一句”。
                    core = re.sub(r"[\s。，,.!?！？、…~\"'‘’“”\-]+", " ", t).strip().lower()
                    if core:
                        if core in ASR_HALLUC_SHORT:
                            log("丢弃整句幻觉：%s" % t[:30])
                            continue
                        if len(core) <= 12 and any(k in core for k in ASR_HALLUC_FRAG):
                            log("丢弃片尾/订阅幻觉：%s" % t[:30])
                            continue
                        # 整句几乎只由词库里的词拼成：这是模型在念提示词，不是真在说话
                        hw_src = ASR_HOTWORDS.get(sl, "").lower()
                        if hw_src and 0 < len(core) <= 12:
                            toks = [w for w in re.split(r"[^\w\u4e00-\u9fff]+", t) if w]
                            if toks and all(w.lower() in hw_src for w in toks):
                                log("丢弃词库幻觉：%s" % t[:30])
                                continue
                    # 重复循环（“黄语语语语…”）：压缩比过高或同一字连续 5 次以上
                    cr = float(getattr(s, "compression_ratio", 0) or 0)
                    if (cr > 2.4 and len(t) >= 8) or re.search(r"(.)\1{4,}", t):
                        log("丢弃重复幻觉段：%s" % t[:40])
                        continue
                    out.append(s)
                return out
            except Exception as e:
                last = e
                log("识别参数 %s 不可用，降级重试：%r" % (kw.get("vad_filter"), e))
                if any(k in str(e).lower() for k in ("cublas", "cudnn", "cuda")):
                    raise            # 显卡库缺失：换参数也没用，交给上层切 CPU
        raise last

    def run(self):
        stage = "ASR"
        try:
            try:
                from opencc import OpenCC
                self.t2s = OpenCC("t2s")
                log("繁简转换就绪（OpenCC）")
            except Exception as e:
                log("没装 OpenCC，繁体可能残留：" + repr(e))
            self.on_state("loading", "ASR 模型加载中…")
            self.model = self._load_whisper()
            self.on_state("asr_ready", "ASR 就绪：%s / %s" % (
                getattr(self, "asr_model_loaded", None) or self.cfg.get("whisper_model") or "small",
                "CPU" if str(self.cfg.get("device") or "cpu").lower() == "cpu"
                else str(self.cfg.get("device")).upper()))
            stage = "翻译模型"
            self.on_state("loading", "翻译模型加载中…")
            self.translator = self._load_translator()
            def _tr2(text, sl=None, tl=None):
                a = sl or CURRENT_LANGS.get("sl", "zh")
                b = tl or ("en" if a == "zh" else "zh")
                tr = (getattr(self, "tr_map", {}) or {}).get((a, b)) or self.translator
                return translate_guarded(tr, text, a, b) if tr else ""

            def _fb2(text, sl=None, tl=None):
                a = sl or CURRENT_LANGS.get("sl", "zh")
                b = tl or ("en" if a == "zh" else "zh")
                tr = ((getattr(self, "tr_map", {}) or {}).get((a, b))
                      or getattr(self, "fallback_tr", None))
                return translate_guarded(tr, text, a, b) if tr else ""

            self.ts = TranslateWorker(self.cfg, self.ui, self.rec,
                                      _tr2 if (getattr(self, "tr_map", None) or self.translator) else None,
                                      self._translate_online,
                                      _fb2 if (getattr(self, "tr_map", None)
                                               or getattr(self, "fallback_tr", None)) else None)
            self.ts.start()
            self.on_state("trans_ready", "翻译就绪：%s" % (
                "在线接口" if self.cfg.get("engine") == "online"
                else "Argos 离线（%s→%s）" % (CURRENT_LANGS["sl"], CURRENT_LANGS["tl"])))
            if self.cfg.get("diarize", False):
                self.on_state("loading", "声纹模型加载中…")
                self.spk_enc, thr = make_speaker_encoder()
                if self.spk_enc is not None:
                    mode = self.cfg.get("spk_mode") or "balanced"
                    if isinstance(self.spk_enc, EcapaEncoder):
                        thr = {"strict": 0.70, "balanced": 0.62, "loose": 0.50}.get(mode, 0.62)
                    self.spk_trk = SpeakerTracker(
                        threshold=thr, max_speakers=int(self.cfg.get("max_speakers") or 4))
                    log("声纹阈值：%.2f（%s）" % (thr, mode))
            self.on_state("ready", "就绪")
        except Exception as e:
            msg = str(e)
            low = msg.lower()
            if "space" in low or "磁盘" in msg:
                msg += "（磁盘空间不足，先清理再试）"
            elif "no such file" in low or "not found" in low or "找不到" in msg:
                msg += "（模型文件缺失，去 Lab → 模型管理器重新下载）"
            self.on_state("error", "%s加载失败：%s" % (stage, msg))
            log("加载失败：" + traceback.format_exc())
            return

        while not self._stop.is_set():
            item = self.q.get()
            if item is None or self._stop.is_set():
                break
            meta = None
            if isinstance(item, tuple) and len(item) == 2:
                item, meta = item            # (音频, 时间戳信息)
            if self.tts_busy is not None and self.tts_busy.is_set():
                continue                 # 正在朗读，这段声音是字幕自己，别转录
            try:
                emb = None
                if self.spk_enc is not None:
                    try:
                        emb = self.spk_enc.embed(item)      # 先算声纹，别等字幕确认才做
                    except Exception:
                        emb = None
                try:
                    t_asr = time.time()
                    segs = self._run_asr(item)
                except Exception as e:
                    if any(k in str(e).lower() for k in ("cublas", "cudnn", "cuda")):
                        log("推理时显卡不可用，切回 CPU：" + repr(e))
                        self.ui.put(("state", "显卡跑不动，改用 CPU 识别", ""))
                        self.cfg["device"] = "cpu"      # 记住，下次启动直接用 CPU
                        save_config(self.cfg)
                        from faster_whisper import WhisperModel
                        self.model = WhisperModel(self.cfg.get("whisper_model") or "small",
                                                 device="cpu", compute_type="int8")
                        segs = self._run_asr(item)
                        self.ui.put(("state", "已切到 CPU 识别，继续", "ok"))
                    else:
                        self.asr_fails = getattr(self, "asr_fails", 0) + 1
                        log("识别失败（第 %d 次）：%r" % (self.asr_fails, e))
                        if self.asr_fails >= 3 and not getattr(self, "_degraded", False):
                            try:
                                from faster_whisper import WhisperModel
                                self.model = WhisperModel("base", device="cpu",
                                                          compute_type="int8")
                                self.cfg["whisper_model"] = "base"
                                self.cfg["device"] = "cpu"
                                save_config(self.cfg)
                                self._degraded = True
                                self.asr_fails = 0
                                self.ui.put(("state", "识别连续出错，已切轻量模型 base 继续采集", "warn"))
                                log("熔断：已切换到 base 轻量模型")
                            except Exception as e2:
                                log("熔断切换失败：" + repr(e2))
                                self.ui.put(("state", "识别反复出错，采集仍在，建议重启引擎", "warn"))
                        else:
                            self.ui.put(("state", "这一句识别失败，继续下一句", "warn"))
                        continue
                zh = "".join(s.text for s in segs).strip()
                # 源语种取本句检测结果；目标默认按配置，只有撞车（同语言）才翻成对向。
                # 说中文 → 出英文，说英文 → 出中文，配置不用手动切。
                sl_now = getattr(self, "last_det_lang", None) or CURRENT_LANGS.get("sl", "zh")
                tl_now = CURRENT_LANGS.get("tl", "en")
                if sl_now == tl_now:
                    tl_now = "en" if tl_now == "zh" else "zh"
                if self.t2s and sl_now == "zh":
                    zh = self.t2s.convert(zh)      # 兜底：简体提示词失效时，硬转一遍
                # Argos 会按句切分后再翻译；ASR 常把两句连成 "先别近 等我闪光" 不带标点，
                # 模型只翻前半句。这里用逗号把口语停顿补回来，译文才不会少半截（仅中文）
                import re as _re
                if sl_now == "zh" and zh and not _re.search(r"[，。！？、,.!?]", zh):
                    zh = _re.sub(r"\s+", "，", zh).strip("，") + "。"
                if sl_now != "zh":
                    # 空格分词语种按词数过滤，避免蹦出 "uh"、"ok" 这种碎片
                    if len(split_words(zh, sl_now)) < int(self.cfg.get("min_words") or 2):
                        continue
                elif len(zh) < int(self.cfg.get("min_chars") or 2):
                    continue
                if zh == self.last_zh:
                    continue
                # Whisper 在静音/纯音乐上爱幻觉出片尾字幕，直接丢掉
                if any(b in zh for b in HALLUCINATION_WORDS):
                    continue
                if zh.strip("，。,，.!！?？… ") in FILLER_ONLY:
                    continue                 # 纯语气词，不占一行，避免连麦碎句刷屏
                self.last_zh = zh
                asr_cost = time.time() - t_asr
                back = (time.time() - meta["t1"]) if meta and meta.get("t1") else 0.0
                log("识别完成：音频 %.1f 秒，耗时 %.2f 秒，落后 %.2f 秒" % (
                    len(item) / 16000.0, asr_cost, back))
                spk = ""
                if self.spk_trk is not None:
                    try:
                        long_enough = len(item) >= 14400       # 不足 0.9 秒的声音不拿来开新的人
                        idx = self.spk_trk.assign(emb, allow_new=long_enough)
                        if idx:
                            spk = "说话人%d" % idx
                            log("声纹：%s（相似度 %.3f，音频 %.1f 秒）"
                                % (spk, getattr(self.spk_trk, "last_sim", 0.0), len(item) / 16000.0))
                    except Exception:
                        log("声纹判定失败：" + traceback.format_exc())
                self.ui.put(("source", zh, ""))

                # 翻译交给独立线程：ASR 不等模型，队列里排着也不会卡住下一句识别
                if self.ts is not None:
                    self.ts.submit({"zh": zh, "segs": segs, "spk": spk,
                                    "item": item, "meta": meta,
                                    "sl": sl_now, "tl": tl_now})
                else:
                    t0 = time.time()
                    try:
                        sl_cur = sl_now
                        tl_cur = tl_now
                        tr_cur = (getattr(self, "tr_map", {}) or {}).get((sl_cur, tl_cur)) or self.translator
                        if self.cfg.get("engine") == "online" and self.cfg.get("api_key"):
                            en = self._translate_online(
                                prep_zh_for_translation(zh) if sl_cur == "zh" else zh,
                                sl_cur, tl_cur) or ""
                            if sl_cur == "zh" and _bad_repeat(en) and tr_cur is not None:
                                en = translate_guarded(tr_cur, zh, sl_cur, tl_cur)
                        else:
                            en = (translate_guarded(tr_cur, zh, sl_cur, tl_cur) if tr_cur else "")
                    except Exception as e:
                        log("翻译失败：" + repr(e))
                        en = ""
                    if en:
                        self.ui.put(("target", en, "%.1fs" % (time.time() - t0), spk, item, 0.0, meta))
                        if self.rec is not None:
                            self.rec.put((time.strftime("%H:%M:%S"), zh, en, spk))
            except Exception:
                log("识别失败：" + traceback.format_exc())
                self.ui.put(("state", "识别出错，看 oversub.log", "error"))


class TranslateWorker(threading.Thread):
    """翻译独立线程：ASR 一出结果就丢队列，UI 只等最终译文；
    同一句短时间内重复出现直接复用上次译文，不反复调度模型。"""

    def __init__(self, cfg, ui, rec, translate_fn, online_fn, fallback_fn=None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.ui = ui
        self.rec = rec
        self.tr_fn = translate_fn
        self.online_fn = online_fn
        self.fb_fn = fallback_fn
        self._fell_back = False
        self.q = queue.Queue()
        self._stop = threading.Event()
        self.cache = {}
        self.hits = 0

    def submit(self, job):
        self.q.put(job)

    def stop(self):
        self._stop.set()
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass

    def run(self):
        while not self._stop.is_set():
            try:
                job = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None or self._stop.is_set():
                break
            zh = job["zh"]
            # 方向按句取：中文句走 zh→en，英文句走 en→zh，缓存也要按方向分开
            sl_j = job.get("sl") or CURRENT_LANGS.get("sl", "zh")
            tl_j = job.get("tl") or ("en" if sl_j == "zh" else "zh")
            t0 = time.time()
            en = None
            ckey = (sl_j, tl_j, zh)
            hit = self.cache.get(ckey)
            if hit and time.time() - hit[1] <= 5.0:
                en = hit[0]
                self.hits += 1
            if en is None:
                try:
                    if self.cfg.get("engine") == "online" and self.cfg.get("api_key"):
                        en = self.online_fn(zh, sl_j, tl_j) or ""
                    else:
                        en = self.tr_fn(zh, sl_j, tl_j) if self.tr_fn else ""
                except Exception as e:
                    log("在线翻译失败，切本地离线：" + repr(e))
                    en = ""
                    if self.fb_fn is not None:
                        try:
                            en = self.fb_fn(zh, sl_j, tl_j) or ""
                            if en and not self._fell_back:
                                self._fell_back = True
                                self.ui.put(("state", "网络不稳，已无缝切到离线翻译", "warn"))
                        except Exception as e2:
                            log("离线备用也失败：" + repr(e2))
                if en:
                    self.cache[ckey] = (en, time.time())
                    if len(self.cache) > 200:
                        for k in list(self.cache)[:100]:
                            self.cache.pop(k, None)
            if not en:
                self.ui.put(("target", "（这句没翻出来）", "", job.get("spk", "")))
                continue
            try:
                conf = sum(float(getattr(s, "avg_logprob", 0)) for s in job["segs"]) / max(
                    1, len(job["segs"]))
            except Exception:
                conf = 0.0
            log("字幕%s：%s  =>  %s" % (("[" + job.get("spk", "") + "]") if job.get("spk") else "",
                                      zh, en))
            self.ui.put(("target", en, "%.1fs" % (time.time() - t0), job.get("spk", ""),
                         job.get("item"), conf, job.get("meta")))
            if self.rec is not None:
                self.rec.put((time.strftime("%H:%M:%S"), zh, en, job.get("spk", "")))


# ---------------------------------------------------------------- 单词查义
class WordBook:
    """单词级查义：优先离线 en→zh（Argos 包），设了在线 key 时走在线模型。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.tr = None
        self.cache = {}
        self._tried = False

    def _ensure_offline(self):
        if self._tried:
            return
        self._tried = True
        try:
            try:
                import torch          # 先让 torch 把自带 DLL 目录挂进搜索路径，sentencepiece 才找得到依赖
            except Exception:
                pass
            from argostranslate import translate as argos
            langs = argos.get_installed_languages()
            en = next((l for l in langs if l.code == "en"), None)
            zh = next((l for l in langs if l.code == "zh"), None)
            if en and zh:
                self.tr = en.get_translation(zh)
                if self.tr is not None:
                    for w in ("word", "computer", "government"):   # 预热，头几次调用容易返回空
                        try:
                            self.tr.translate(w)
                        except Exception:
                            pass
                    log("单词词典就绪（离线 en→zh）")
        except Exception as e:
            log("单词词典加载失败：" + repr(e))

    def _online(self, word, sentence):
        import urllib.request
        base = (self.cfg.get("base_url") or "").rstrip("/")
        if not base:
            return ""
        body = json.dumps({
            "model": self.cfg.get("model") or "deepseek-chat",
            "temperature": 0.1,
            "max_tokens": 200,
            "messages": [
                {"role": "system", "content": "你在做英语学习词典。只输出中文释义：先词性（n./v./adj.），"
                                              "再一句不超过 15 个字的中文解释，不要英文解释、不要例句、不要引号。"},
                {"role": "user", "content": "单词：%s\n（出现在句子：%s）" % (word, sentence)},
            ],
        }).encode("utf-8")
        req = urllib.request.Request(base + "/chat/completions", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + self.cfg.get("api_key"))
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data["choices"][0]["message"]["content"] or "").strip()

    def lookup(self, word, sentence=""):
        w = (word or "").strip()
        if not w:
            return ""
        key = w.lower()
        if key in self.cache:
            return self.cache[key]
        meaning = ""
        if self.cfg.get("engine") == "online" and self.cfg.get("api_key"):
            try:
                meaning = self._online(w, sentence)
            except Exception as e:
                log("在线取词义失败：" + repr(e))
        if not meaning:
            self._ensure_offline()
            if self.tr is not None:
                for attempt in (w, w.lower()):      # 偶尔第一次会给空，重试一次
                    try:
                        meaning = (self.tr.translate(attempt) or "").strip()
                    except Exception as e:
                        log("离线取词义失败：" + repr(e))
                    if meaning:
                        break
        if not meaning:
            meaning = "（离线词典没这个词，可以切在线引擎或先记下来）"
        self.cache[key] = meaning
        return meaning


SCENE_GLOSSARY = {
    # 游戏（FPS / MOBA / 生存）
    "peek": {"game": "探身查看，只露一点身位", "general": "偷看"},
    "clutch": {"game": "残局收割，一打多拿下回合", "general": "紧握；关键时刻"},
    "loot": {"game": "捡装备 / 搜刮物资", "general": "战利品"},
    "respawn": {"game": "复活重生", "general": "重新生成"},
    "flank": {"game": "绕后包抄", "general": "侧翼"},
    "camp": {"game": "蹲点守位（camping）", "general": "露营"},
    "rush": {"game": "快攻冲点", "general": "冲；匆忙"},
    "rotate": {"game": "转点换位", "general": "轮换"},
    "gank": {"game": "游走偷袭（多打少）", "general": "抓人"},
    "feed": {"game": "送人头", "general": "喂养"},
    "farm": {"game": "刷钱刷经验", "general": "农场；耕种"},
    "poke": {"game": "远程消耗", "general": "戳"},
    "kite": {"game": "拉扯风筝（边退边打）", "general": "风筝"},
    "stun": {"game": "眩晕控制", "general": "使震惊"},
    "buff": {"game": "增益效果", "general": "增强"},
    "nerf": {"game": "削弱（官方下调强度）", "general": "削弱"},
    "aggro": {"game": "仇恨 / 拉怪", "general": "激怒"},
    "noob": {"game": "新手 / 菜鸟", "general": "菜鸟"},
    "smurf": {"game": "小号炸鱼", "general": "蓝精灵"},
    "gg": {"game": "打完收工（good game）", "general": "好局"},
    "afk": {"game": "人离开键盘", "general": "挂机"},
    "ping": {"game": "延迟 / 打点标记", "general": "网络延迟"},
    "lag": {"game": "卡顿掉帧", "general": "滞后"},
    "spray": {"game": "扫射（压枪连发）", "general": "喷洒"},
    "recoil": {"game": "后坐力", "general": "反冲"},
    "headshot": {"game": "爆头", "general": "头部特写"},
    "wallbang": {"game": "穿墙击杀", "general": "穿墙"},
    "choke": {"game": "关键局手抖失误", "general": "窒息"},
    "carry": {"game": "带飞全场", "general": "携带"},
    "bait": {"game": "钓鱼诱敌", "general": "诱饵"},
    "meta": {"game": "版本强势打法", "general": "元；自我"},
    "op": {"game": "过于强势（overpowered）", "general": " overpowered"},
    # 影视 / 剧情
    "plot": {"film": "剧情线", "general": "情节"},
    "twist": {"film": "剧情反转", "general": "扭转"},
    "reveal": {"film": "揭底 / 真相摊开", "general": "揭示"},
    "cut": {"film": "镜头切换", "general": "切；削减"},
    "take": {"film": "一条镜头（NG 几次）", "general": "拿；花费"},
    "scene": {"film": "场次 / 一场戏", "general": "场景"},
    "shot": {"film": "镜头；一个画面", "general": "射击"},
    "spoiler": {"film": "剧透", "general": "破坏者"},
    "cameo": {"film": "客串彩蛋", "general": "客串"},
    "montage": {"film": "蒙太奇剪辑段", "general": "剪辑组合"},
    # 口语 / 连麦
    "chill": {"chat": "放轻松，别紧张", "general": "寒冷；放松"},
    "hype": {"chat": "上头 / 炒热气氛", "general": "大肆宣传"},
    "salty": {"chat": "有点破防、不服气", "general": "咸的"},
    "toxic": {"chat": "嘴臭、爱喷人", "general": "有毒"},
    "vibe": {"chat": "氛围、感觉", "general": "气氛"},
    "lowkey": {"chat": "低调地、有点", "general": "低调"},
    "highkey": {"chat": "很明显地", "general": "高调"},
    "sus": {"chat": "可疑（suspicious）", "general": "可疑"},
    "flex": {"chat": "炫耀", "general": "弯曲；弹性"},
    "ggs": {"chat": "打得不错", "general": "好局"},
}


def scene_category(scene):
    """把场景预设映射到词库分类：FPS/看视频/语音聊天。"""
    return {"fps": "game", "video": "film", "chat": "chat"}.get(str(scene or ""), "general")


def gloss_line(word, scene="fps"):
    """气泡用的一行场景释义：优先场景词库，附一句通用义。
    返回空串表示词库里没有，交给通用词典去查。"""
    w = (word or "").strip().lower()
    ent = SCENE_GLOSSARY.get(w)
    if not ent:
        return ""
    cat = scene_category(scene)
    label = {"game": "游戏", "film": "影视", "chat": "口语"}.get(cat)
    head = ent.get(cat)
    if not head:
        return ""
    tail = ent.get("general")
    if tail and tail != head and cat != "general":
        return "【%s】%s · 【通用】%s" % (label, head, tail)
    return "【%s】%s" % (label or "通用", head)


DICT_PARSERS = {}          # 语种 → 分词解析器；当前只挂英语，其他按需加载


def register_dict(lang, fn):
    """插件式词典：不同语种各自挂一个分词/检索解析器，互不干扰。"""
    DICT_PARSERS[str(lang)] = fn
    return fn


def _parse_en(text):
    import re
    seen, out = set(), []
    for w in re.findall(r"[A-Za-z][A-Za-z'\-]*", text or ""):
        low = w.lower()
        if len(low) < 2 or low in seen:
            continue
        seen.add(low)
        out.append(w)
    return out


register_dict("en", _parse_en)


def split_words(text, lang="en"):
    """按目标语种选解析器。没装词典的语种返回空表，不假装能拆词。"""
    fn = DICT_PARSERS.get(str(lang or "en"))
    if fn is None:
        return []
    return fn(text)


# ---------------------------------------------------------------- 悬浮窗（Qt）
def _make_mark_label():
    """字幕英文标签的子类：交互模式下给「困难」生词画一条极细下划线。
    非交互模式（穿透/游戏中）完全不画，画面上看不到任何额外东西。"""
    from PySide6.QtWidgets import QLabel
    from PySide6.QtGui import QPainter, QPen, QColor
    import re as _re

    class MarkLabel(QLabel):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._marks = set()
            self._mark_on = False

        def set_marks(self, words):
            self._marks = set(str(w).lower() for w in (words or []))
            self.update()

        def set_mark_mode(self, on):
            self._mark_on = bool(on)
            self.update()

        def paintEvent(self, ev):
            super().paintEvent(ev)
            if not self._mark_on or not self._marks:
                return
            text = self.text()
            if not text:
                return
            try:
                fm = self.fontMetrics()
                p = QPainter(self)
                pen = QPen(QColor(150, 196, 170, 190))
                pen.setWidthF(1.0)
                p.setPen(pen)
                y = self.contentsRect().bottom() - 1
                left = self.contentsRect().left()
                for m in _re.finditer(r"[A-Za-z][A-Za-z'\-]*", text):
                    word = m.group(0)
                    if word.lower() not in self._marks:
                        continue
                    x0 = left + fm.horizontalAdvance(text[:m.start()])
                    x1 = x0 + fm.horizontalAdvance(word)
                    p.drawLine(int(x0), int(y), int(x1), int(y))
                p.end()
            except Exception:
                pass

    return MarkLabel


class Overlay:
    def __init__(self, cfg, ui_queue, on_quit, on_toggle, on_settings):
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                                       QPushButton, QFrame, QApplication,
                                       QCheckBox, QGraphicsDropShadowEffect, QSizePolicy)
        from PySide6.QtGui import QFont, QColor, QCursor

        self.Qt = Qt
        self.cfg = cfg
        self.q = ui_queue
        self.tts = TTSEngine()
        try:
            self.tts.set_output_device(self.cfg.get("tts_output_device") or "")
        except Exception:
            pass
        self._read_jobs = queue.Queue()
        self._loop_active = False
        self._tts_warm = set()
        self._read_abort = threading.Event()
        self._read_events = queue.Queue()
        self._read_thread = None
        self._speaking = False
        self.tts_busy = threading.Event()
        self._hl_timer = None
        self._hl_text = ""
        self._hl_color = "#FFFFFF"

        app = QApplication.instance() or QApplication(sys.argv)
        self.app = app
        app.setQuitOnLastWindowClosed(False)

        win = cfg.get("window") or {}
        self.win = QWidget()
        self.win.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.win.setAttribute(Qt.WA_TranslucentBackground, True)
        self.win.setWindowOpacity(1.0)        # 整窗不透明，透明只作用在卡片底色上，文字永远清晰
        self.MARGIN = 18                      # 给投影留的余量
        # Galgame 对话框形态：占屏宽约 76%（640–1200），不铺满整行；高度按 _dialog_h 恒定
        try:
            from PySide6.QtGui import QGuiApplication
            _sw = QGuiApplication.primaryScreen().availableGeometry().width()
        except Exception:
            _sw = 1600
        _dw = int(min(1200, max(640, _sw * 0.76)))
        self.win.resize(int(win.get("w") or _dw), int(win.get("h") or self._dialog_h()))
        self.win.move(int(win.get("x") or 400), int(win.get("y") or 820))
        self.win.setWindowTitle("Oversub 沉浸字幕")

        outer = QVBoxLayout(self.win)
        outer.setContentsMargins(self.MARGIN, self.MARGIN, self.MARGIN, self.MARGIN)

        self.panel = QFrame()
        self.panel.setObjectName("panel")
        self._glass()                                    # 卡片底色：透明度只加在这里
        outer.addWidget(self.panel)

        # 这里不要再给 panel 加 QGraphicsDropShadowEffect：
        # 阴影效果会把整块卡片渲染进离屏 pixmap，字幕换行/换句时旧文字
        # 会留在缓存里，表现为旧句和状态文字压在英文字幕上。
        # 视觉上改用卡片自带的细边线区分层次。

        hrow = QHBoxLayout(self.panel)
        hrow.setContentsMargins(0, 0, 0, 0)
        hrow.setSpacing(0)

        lay = QVBoxLayout()
        # Galgame 对话框的内边距：文字离左 26、上下各 16 左右，读起来像一块对话框而不是通知
        lay.setContentsMargins(26, 14, 24, 16)
        lay.setSpacing(4)
        hrow.addLayout(lay, 1)

        # 状态 / 控制行整体装进一个容器：对话框形态下默认收起，只有进交互模式才露出来
        self.top_bar_w = QWidget()
        self.top_bar_w.setMinimumHeight(24)
        # 横向撑满、纵向固定：不撑满的话按钮会被左侧状态文字挤掉宽度，最后一个都看不见
        self.top_bar_w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        bar = QHBoxLayout(self.top_bar_w)
        bar.setSpacing(8)
        bar.setContentsMargins(0, 0, 0, 0)
        self.dot = QLabel("●")
        self.dot.setStyleSheet("color:#8C8781;font-size:9px;")
        self.status = QLabel("准备中…")
        self.status.setFont(self._font(10, False, "Segoe UI", 0.4, "Microsoft YaHei"))
        self.status.setStyleSheet("color:#A29D94;")
        bar.addWidget(self.dot)
        bar.addWidget(self.status)
        bar.addStretch(1)

        self.spk_label = QLabel("")
        self.spk_label.setFont(self._font(10, True, "Microsoft YaHei"))
        # Galgame 对话框里的说话人胶囊：淡底 + 圆角，名字像“铭牌”挂在正文上方
        self.spk_label.setStyleSheet(
            "color:#7BC39B;background:rgba(123,195,155,28);"
            "border-radius:8px;padding:1px 8px;")
        self.spk_label.setVisible(False)
        lay.addWidget(self.spk_label, 0, self.Qt.AlignLeft)

        # 备用快捷按钮（与下面原始收藏 / 朗读重复）：保留对象与连接，不放进界面；
        # 可见的底部按钮用带状态同步的原始 btn_fav / btn_read
        self.btn_fav_bar = QPushButton("☆ 收藏")
        self.btn_fav_bar.setObjectName("fav")
        self.btn_fav_bar.setFixedHeight(22)
        self.btn_fav_bar.setFont(self._font(10, False, "Microsoft YaHei"))
        self.btn_fav_bar.setToolTip("收藏当前这一句（Ctrl+Alt+U）")
        self.btn_fav_bar.clicked.connect(self._fav_current)
        self.btn_fav_bar.hide()
        self.btn_speak_bar = QPushButton("🔊 朗读")
        self.btn_speak_bar.setFixedHeight(22)
        self.btn_speak_bar.setFont(self._font(10, False, "Microsoft YaHei"))
        self.btn_speak_bar.setToolTip("朗读当前句，再点一次停止")
        self.btn_speak_bar.clicked.connect(self._read_clicked)
        self.btn_speak_bar.hide()

        self.btn_pause = QPushButton("开始")
        self.btn_settings = QPushButton("设置")
        self.btn_quit = QPushButton("关闭")
        self.btn_quit.setObjectName("quit")
        for b in (self.btn_pause, self.btn_settings, self.btn_quit):
            b.setFixedHeight(22)
            b.setCursor(QCursor(Qt.PointingHandCursor))
            b.setFont(self._font(10, False, "Microsoft YaHei"))
            bar.addWidget(b)
        self.btn_pause.clicked.connect(on_toggle)
        self.btn_settings.clicked.connect(on_settings)
        self.btn_quit.clicked.connect(on_quit)

        # 自动朗读开关（贴着状态行，只占一个小滑块的位置）
        self.chk_auto = QCheckBox("自动朗读")
        self.chk_auto.setChecked(bool(cfg.get("auto_read", False)))
        self.chk_auto.setFont(self._font(10, False, "Microsoft YaHei"))
        self.chk_auto.setStyleSheet("QCheckBox{color:#A29D94;}QCheckBox::indicator{width:26px;height:13px;}")
        self.chk_auto.setCursor(QCursor(Qt.PointingHandCursor))
        bar.addWidget(self.chk_auto)
        self.tts_voice = QLabel(self.tts.voice_name("en"))
        self.tts_voice.setFont(self._font(9, False, "Microsoft YaHei"))
        self.tts_voice.setStyleSheet("color:#6E6A63;")
        bar.addWidget(self.tts_voice)
        ly = QVBoxLayout()
        ly.setContentsMargins(0, 0, 0, 0)
        ly.setSpacing(4)
        # 状态 / 操作条不放在这儿：Galgame 的按钮都贴在对话框下沿，放最上面手指要往上够
        # 真正的挂载点在 lay.addStretch(1) 之后（见下面“底部操作条”）

        self.src_label = QLabel("")
        self.src_label.setWordWrap(True)
        self.src_label.setFont(self._font(10, False, "Microsoft YaHei"))
        self.src_label.setStyleSheet("color:#9E9A91;")
        self.src_label.setVisible(bool(cfg.get("show_source")))
        lay.addWidget(self.src_label)

        self.dst_label = _make_mark_label()("")
        self.dst_label.setWordWrap(True)
        self.dst_label.setFont(self._font(int(cfg.get("font_size") or 22), True, "Segoe UI", 0.2))
        self.dst_label.setStyleSheet("color:#FFFFFF;")
        lay.addWidget(self.dst_label)

        # 交互模式下：这句话末尾会浮出一个小 ☆；右上角是极淡的收藏提示
        self.btn_star = QPushButton("☆", self.panel)
        self.btn_star.setFixedSize(20, 20)
        self.btn_star.setCursor(QCursor(Qt.PointingHandCursor))
        self.btn_star.setFont(self._font(12, False, "Segoe UI"))
        self.btn_star.setStyleSheet(
            "QPushButton{background:rgba(255,255,255,10);border:none;border-radius:10px;color:#D8D3C9;}"
            "QPushButton:hover{background:rgba(255,255,255,32);color:#FFFFFF;}")
        self.btn_star.setToolTip("收藏整句")
        self.btn_star.clicked.connect(self._fav_current)
        self.btn_star.hide()

        # 穿透态抓手：字幕条本体整窗穿透（不挡游戏点击），旁边挂一个始终能点的小胶囊，
        # ⋯ 进入交互模式、☆ 一键收藏当前句。原来要靠热键进模式再找星标，游戏里太绕。
        self.hit = QWidget()
        self.hit.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.hit.setAttribute(Qt.WA_TranslucentBackground, True)
        self.hit.setWindowTitle("Oversub 快捷操作")
        _hl = QHBoxLayout(self.hit)
        _hl.setContentsMargins(0, 0, 0, 0)
        _hl.setSpacing(2)
        _hit_qss = ("QPushButton{background-color:rgba(20,22,26,230);color:#D8D3C9;"
                    "border:1px solid rgba(255,255,255,40);border-radius:8px;"
                    "font-size:13px;padding:0px;}"
                    "QPushButton:hover{background-color:rgba(70,76,86,240);color:#FFFFFF;}")
        self.btn_hit_more = QPushButton("⋯")
        self.btn_hit_more.setFixedSize(26, 22)
        self.btn_hit_more.setFlat(True)
        self.btn_hit_more.setCursor(Qt.PointingHandCursor)
        self.btn_hit_more.setStyleSheet(_hit_qss)
        self.btn_hit_more.setToolTip("点一下进入交互模式（可选中字幕、拖宽、展开历史）")
        self.btn_hit_more.clicked.connect(lambda: self.set_interactive(True))
        self.btn_hit_anchor = QPushButton("⇅")
        self.btn_hit_anchor.setFixedSize(26, 22)
        self.btn_hit_anchor.setFlat(True)
        self.btn_hit_anchor.setCursor(Qt.PointingHandCursor)
        self.btn_hit_anchor.setStyleSheet(_hit_qss)
        self.btn_hit_anchor.setToolTip("一键把字幕条换到屏幕顶部 / 底部（挡到 HUD 时用）")
        self.btn_hit_anchor.clicked.connect(self._toggle_anchor)
        self.btn_hit_fav = QPushButton("☆")
        self.btn_hit_fav.setFixedSize(26, 22)
        self.btn_hit_fav.setFlat(True)
        self.btn_hit_fav.setCursor(Qt.PointingHandCursor)
        self.btn_hit_fav.setStyleSheet(_hit_qss)
        self.btn_hit_fav.setToolTip("收藏当前这句（不用进交互模式）")
        self.btn_hit_fav.clicked.connect(self._fav_current)
        _hl.addWidget(self.btn_hit_anchor)
        _hl.addWidget(self.btn_hit_more)
        _hl.addWidget(self.btn_hit_fav)
        self.hit.hide()

        self.toast = QLabel("", self.panel)
        self.toast.setFont(self._font(10, False, "Microsoft YaHei"))
        self.toast.setStyleSheet(
            "color:#BFD9C8;background:rgba(20,24,21,150);border-radius:8px;padding:2px 8px;")
        self.toast.hide()

        # 底部操作条：循环 / 清队 / 字号 + 队列与模式提示，并入底部单行（bar），
        # 和 Galgame 一样按钮都贴在对话框下沿，正文占上方区域
        self.btn_fav = QPushButton("☆ 收藏")
        self.btn_fav.setObjectName("fav")
        self.btn_read = QPushButton("朗读")
        self.btn_loop = QPushButton("循环")
        self.btn_loop.setCheckable(True)
        self.btn_loop.setChecked(bool(cfg.get("loop_read", False)))
        self.btn_clear_q = QPushButton("清队")
        self.btn_font_minus = QPushButton("A−")
        self.btn_font_plus = QPushButton("A+")
        self.btn_font_minus.setToolTip("字幕字号 -2（Ctrl+Alt+-）")
        self.btn_font_plus.setToolTip("字幕字号 +2（Ctrl+Alt+=）")
        for b in (self.btn_fav, self.btn_read, self.btn_loop, self.btn_clear_q,
                  self.btn_font_minus, self.btn_font_plus):
            b.setFixedHeight(22)
            b.setCursor(QCursor(Qt.PointingHandCursor))
            b.setFont(self._font(10, False, "Microsoft YaHei"))
            bar.addWidget(b)
        self.queue_hint = QLabel("")
        self.queue_hint.setFont(self._font(9, False, "Microsoft YaHei"))
        self.queue_hint.setStyleSheet("color:#6E6A63;")
        self.queue_hint.setVisible(False)
        bar.addWidget(self.queue_hint)
        self.mode_hint = QLabel("")
        self.mode_hint.setFont(self._font(9, False, "Microsoft YaHei"))
        self.mode_hint.setStyleSheet("color:#B47B1E;")
        self.mode_hint.setVisible(False)
        bar.addWidget(self.mode_hint)
        self.btn_fav.clicked.connect(self._fav_current)
        self.btn_read.clicked.connect(self._read_clicked)
        self.btn_clear_q.clicked.connect(self.clear_read_queue)
        self.btn_font_minus.clicked.connect(lambda: self.bump_font(-2))
        self.btn_font_plus.clicked.connect(lambda: self.bump_font(2))
        self.btn_loop.toggled.connect(self._loop_toggled)
        self.chk_auto.toggled.connect(self._auto_toggled)

        # 交互模式事件：鼠标移到卡片上浮出 ☆；在字幕上划词/双击选词后弹小气泡
        from PySide6.QtCore import QObject

        class _Filt(QObject):
            def __init__(self, ov):
                super().__init__()
                self.ov = ov

            def eventFilter(self, obj, ev):
                return self.ov._filter(obj, ev)

        self._filt = _Filt(self)
        self.panel.installEventFilter(self._filt)
        self.dst_label.installEventFilter(self._filt)
        self._bubble = None
        self._pins = []
        self._look_q = queue.Queue()
        self._look_timer = None

        # 交互模式下按 ESC 直接退出，把鼠标还给游戏
        try:
            from PySide6.QtGui import QShortcut, QKeySequence
            self._esc = QShortcut(QKeySequence(Qt.Key_Escape), self.win)
            self._esc.activated.connect(lambda: self.set_interactive(False))
        except Exception:
            self._esc = None

        # 滚动缓冲区：旧句在上、当前句在下，读起来像正常字幕，不会上下错位
        from PySide6.QtWidgets import QWidget, QVBoxLayout
        self.hist_box = QWidget()
        self._hist_lay = QVBoxLayout(self.hist_box)
        self._hist_lay.setContentsMargins(0, 0, 0, 0)
        self._hist_lay.setSpacing(8)
        self.hist_box.setVisible(False)
        # 插到状态行之后、源文/译文之前：历史永远在当前句上方
        lay.insertWidget(0, self.hist_box)

        self.divider = QFrame()
        self.divider.setFixedHeight(1)
        self.divider.setStyleSheet("background:rgba(255,255,255,20);")
        self.divider.setVisible(False)
        lay.insertWidget(1, self.divider)

        # 历史在上面、正文居中、按钮在最下面 —— Galgame 对话框的排法
        lay.addStretch(1)
        lay.addWidget(self.top_bar_w)

        self._hist = []             # 当前句之前的历史（滚动缓冲区里除最新一句外的部分）
        self._fav_guard = {}        # 句子防重复：key -> 最近一次收藏的时刻
        self._word_guard = {}       # 单词防重复：词+原句 -> 时刻
        self._recent = []           # 最近 10 句完整记录（侧边迷你历史面板用）
        self._hist_panel = None
        self._hist_open = False
        self._hist_plain = ""
        self._shown_at = 0.0        # 当前这句出现在屏幕上的时刻
        self._hold_ms = 0           # 这句至少要停留多久
        self._pending = None        # 还没来得及换上去的最新一句
        self._pending_at = 0.0      # 那句到达的时刻（碎句合并判间隔用）
        self._frozen = False        # 鼠标悬停时冻结换句，方便慢慢读
        self._status_backup = None
        self._cur_spk = ""          # 当前这句的说话人
        self._cur_en = ""           # 当前这句的英文（对照模式下也用它做朗读/收藏）
        self._cur_zh = ""           # 当前这句的中文原文（换句时带进历史）
        self._cur_audio = None      # 当前这句的原始音频（收藏时切片）
        self._cur_conf = 0.0        # 当前这句的识别置信度（平均 logprob）
        self._cur_meta = None       # 当前这句的时间戳（采集端的绝对时间）
        self._zh_next = ""          # 刚识别到、还没配上译文的中文
        self._last_voice_at = time.monotonic()   # 最近一次有语音的时刻（静音过滤用）
        self._idle_on = False
        self._sess_start = time.strftime("%H:%M:%S")   # 当前会话包的起点（一局 / 一段视频）
        self._sess_at = time.monotonic()
        self._hard_words = set()
        self._mic_card = None
        self._mic_hide_timer = None

        self._drag_pos = None
        self.panel.mousePressEvent = self._press
        self.panel.mouseMoveEvent = self._move
        self.panel.mouseReleaseEvent = self._release
        self.panel.enterEvent = self._enter
        self.panel.leaveEvent = self._leave

        self.timer = QTimer()
        self.timer.timeout.connect(self._tick)
        self.timer.start(100)        # 实时卡片 100ms 节流，一句连续说也不会反复重绘
        # 上一句不会一直贴在屏幕上：安静一会儿就自己淡出，只留当前句
        self._hist_idle = QTimer(self.win)
        self._hist_idle.setSingleShot(True)
        self._hist_idle.timeout.connect(self._idle_hide_history)
        # 历史面板的自动收起不靠 Leave 事件（窗口不抢焦点时它来得不稳定）：用轮询看着光标
        self._hist_watch = QTimer(self.win)
        self._hist_watch.setInterval(400)
        self._hist_watch.timeout.connect(self._watch_hist_panel)
        self._hist_hovered = False
        self._hist_out_since = None
        self._interactive = False
        self._apply_mode_visuals(False)     # 启动时是常态：字幕上不挂任何按钮

    @staticmethod
    def _font(size, bold=False, family="Segoe UI", spacing=0.0, fallback=None):
        from PySide6.QtGui import QFont
        f = QFont(family, int(size))
        f.setBold(bool(bold))
        if fallback:
            f.setFamilies([family, fallback])
        if spacing:
            try:
                f.setLetterSpacing(QFont.AbsoluteSpacing, float(spacing))
            except Exception:
                pass
        return f

    def _prev_size(self):
        return max(11, int((self.cfg.get("font_size") or 22) * 0.62))

    def _hist_size(self):
        return max(12, int((self.cfg.get("font_size") or 22) * 0.74))

    @staticmethod
    def _text_height(label, width, text=None):
        """按字体实际排版量高度，避免 QLabel 的缓存高估。"""
        from PySide6.QtCore import QRect, Qt
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(label.font())
        s = label.text() if text is None else text
        r = fm.boundingRect(QRect(0, 0, int(width), 10000), Qt.TextWordWrap, s)
        return max(fm.height(), r.height() + 2)

    @staticmethod
    def _esc(t):
        return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def _scroll_rows(self):
        """字幕条里一共显示几行（含当前句）。默认 3 行 = 当前句 + 上一句 + 再上一句。
        连续看视频时留 2 句回看足够，又不会把画面铺满；0 行历史时只显示当前句。"""
        try:
            prev = int(self.cfg.get("overlay_prev_rows", 2))
        except Exception:
            prev = 2
        return max(1, min(4, prev + 1))

    def _render_history(self):
        """滚动缓冲区：按“旧在上、新在下”的顺序摆成一列；越旧越淡，交互模式统一提亮。"""
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtWidgets import QLabel, QWidget, QVBoxLayout
        if not hasattr(self, "hist_box"):
            return
        try:
            while self._hist_lay.count():
                it = self._hist_lay.takeAt(0)
                w = it.widget() if it else None
                if w:
                    # 不要 setParent(None)：那会把旧行变成独立顶层窗口，
                    # 在 deleteLater 生效前浮在字幕上，出现“旧句跑到第二行和英文重叠”的残影。
                    w.hide()
                    w.deleteLater()
            self._hist_widgets = []
            rows = [t for t in self._hist if t]
            if not rows:
                self.hist_box.setVisible(False)
                self.divider.setVisible(False)
                return
            rows = rows[: max(0, self._scroll_rows() - 1)]
            if not rows:
                # 对话框形态只显示当前句：历史为空时整块历史区收起，
                # 不能留一个空 div 把正文挤下去
                self.hist_box.setVisible(False)
                if hasattr(self, "divider"):
                    self.divider.setVisible(False)
                return
            rows = list(reversed(rows))          # 旧的在上，紧挨当前句的是上一句
            n = len(rows)
            on = bool(getattr(self, "_interactive", False))
            en_size = self._hist_size()
            zh_size = max(10, int(round(en_size * 0.84)))
            inner = max(220, self.win.width() - self.MARGIN * 2 - 3 - 35)
            _skey, _sk = skin_of(self.cfg)
            _light = bool(_sk.get("bg")) and sum(_sk["bg"]) / 3.0 > 150
            _halign = _Qt.AlignHCenter if _sk.get("align") == "center" else _Qt.AlignLeft
            for i, item in enumerate(rows):
                en, zh, spk = item[0], item[1], item[2]
                age = (n - 1 - i)                # 0 = 最新
                base = _sk.get("hist") or "#D8D3C9"
                strength = (0.92 if on else 0.62) - min(0.42, age * 0.07)
                if on:
                    strength = max(0.45, strength)     # 交互模式提亮，可读可选中
                col = self._dim(base, strength, _light)
                zh_col = self._dim(_sk.get("hist_sub") or "#9B978F", strength - 0.12, _light)
                w = QWidget()
                v = QVBoxLayout(w)
                v.setContentsMargins(0, 0, 0, 0)
                v.setSpacing(1)
                # 每行带序号：1 是最旧，数字越大越新，第一次看也知道往哪边读
                lab = QLabel("%2d  %s" % (i + 1, en))
                lab.setTextFormat(_Qt.PlainText)
                lab.setWordWrap(True)
                lab.setFont(self._font(en_size, False,
                                       lang_font(CURRENT_LANGS.get("tl", "en"), self.cfg) or "Segoe UI"))
                lab.setStyleSheet("color:%s;background:transparent;" % col)
                lab.setAlignment(_halign)
                lab.setFixedHeight(self._text_height(lab, inner))
                lab.setProperty("sentence", en)
                lab.setProperty("zh", zh)
                lab.setTextInteractionFlags(
                    _Qt.TextSelectableByMouse | _Qt.TextSelectableByKeyboard
                    if on else _Qt.NoTextInteraction)
                if on:
                    lab.setCursor(_Qt.IBeamCursor)
                lab.installEventFilter(self._filt)
                v.addWidget(lab)
                zh_h = 0
                # 历史行强制双语：只留译文的话，回看时看不出这行原来是什么语言。
                # 源文用小一号灰字，最多两行，免得把字幕条顶出屏幕。
                if zh and self.cfg.get("show_source", True) \
                        and str(zh).strip() != str(en).strip():
                    z = QLabel(str(zh))
                    z.setTextFormat(_Qt.PlainText)
                    z.setWordWrap(True)
                    z.setFont(self._font(zh_size, False, "Microsoft YaHei"))
                    z.setStyleSheet("color:%s;background:transparent;" % zh_col)
                    z.setAlignment(_halign)
                    z.setTextInteractionFlags(_Qt.NoTextInteraction)
                    z.setFixedHeight(min(self._text_height(z, inner), int(zh_size * 2.4)))
                    zh_h = z.height() + 1
                    v.addWidget(z)
                w.setFixedHeight(lab.height() + zh_h)
                self._hist_lay.addWidget(w)
                self._hist_widgets.append(w)
            self.hist_box.setVisible(True)
            self.divider.setVisible(True)
        except Exception as e:
            log("滚动字幕渲染失败：" + repr(e))

    @staticmethod
    def _dim(hex_color, strength, light=False):
        """把颜色往背景色方向调，做旧句用（越旧越暗 / 浅色皮肤上越旧越淡）。"""
        try:
            h = hex_color.lstrip("#")
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            base = (250, 248, 244) if light else (34, 32, 30)
            return "#%02X%02X%02X" % tuple(
                int(base[i] + (v - base[i]) * strength) for i, v in enumerate((r, g, b)))
        except Exception:
            return "#8E8A82"

    # 鼠标移进来就停止换句，移开继续
    # ---------------- 朗读（点播 / 自动 / 循环 + 逐词高亮） ----------------
    def _ensure_read_thread(self):
        if self._read_thread and self._read_thread.is_alive():
            return
        self._read_thread = threading.Thread(target=self._read_worker, daemon=True)
        self._read_thread.start()

    def _read_worker(self):
        while True:
            job = self._read_jobs.get()
            if job is None:
                return
            text, rate, voice = job[0], job[1], (job[2] if len(job) > 2 else None)
            lang = job[3] if len(job) > 3 else CURRENT_LANGS.get("tl", "en")
            volume = job[4] if len(job) > 4 else 100
            pause = job[5] if len(job) > 5 else 0.0
            repeats = int(job[6]) if len(job) > 6 else 0
            if self._read_abort.is_set() or self.cfg.get("tts_muted"):
                self._read_events.put(("end", text, 0.0))
                continue
            if lang not in self._tts_warm:
                self._tts_warm.add(lang)
                self._read_events.put(("load", text, 0.0))
            path, dur = self.tts.say(text, lang, rate, voice, volume)
            if not path:
                self._read_events.put(("end", text, 0.0))
                continue
            self._read_events.put(("start", text, dur))
            log("朗读中（%.2f 秒）：%s" % (dur, text[:60]))
            self.tts_busy.set()
            try:
                self.tts.play(path)
            finally:
                self.tts_busy.clear()
            self._read_events.put(("end", text, dur))
            # 朗读间隙 / 跟读留白：只等这个朗读线程，不影响识别与字幕
            try:
                gap = float(pause) if pause else float(self.cfg.get("tts_gap") or 0.0)
                if self._loop_active and self.cfg.get("loop_read"):
                    gap = float(self.cfg.get("tts_loop_pause") or 1.2)
                if gap > 0:
                    total = min(3.0, max(0.0, gap))
                    end_t = time.monotonic() + total
                    while time.monotonic() < end_t:
                        if self._read_abort.is_set():
                            break
                        time.sleep(min(0.1, end_t - time.monotonic()))
            except Exception:
                pass
            # 循环跟读 / 跟读模式重播：读完再接一句同样的
            try:
                if self._loop_active and self.cfg.get("loop_read") \
                        and text == (self._cur_en or "").strip():
                    self._enqueue_read((text, rate, voice, lang, volume, 0.0, 0))
                elif repeats > 0 and not self.cfg.get("tts_muted") \
                        and not self._read_abort.is_set():
                    self._enqueue_read((text, rate, voice, lang, volume,
                                        float(self.cfg.get("tts_loop_pause") or 1.2),
                                        repeats - 1))
            except Exception:
                pass

    def preload_tts(self, lang="en"):
        """后台低优先级预合成一句，第一次朗读不空等音色加载。"""
        lang = str(lang or "en")
        if lang in self._tts_warm:
            return
        self._tts_warm.add(lang)

        def _run():
            try:
                sample = "Hello there" if lang == "en" else "你好"
                path, dur = self.tts.say(sample, lang, 0,
                                         (self.cfg.get("tts_voices") or {}).get(lang), 20)
                log("TTS 预热完成：%s（%.2f 秒）" % (lang, dur))
            except Exception as e:
                log("TTS 预热失败：" + repr(e))
        threading.Thread(target=_run, daemon=True).start()

    def update_tts_ready(self, lang="en"):
        """音色缺失时按钮置灰并给 hover 提示，不弹窗。"""
        try:
            ok = self.tts.has_voice(lang)
            if not ok and str(lang) != "en":
                self.btn_read.setStyleSheet("color:#6E6A63;")
                self.btn_read.setToolTip("请下载对应语种 TTS 音色包")
                self.tts_voice.setText("音色缺失")
                self.tts_voice.setToolTip("请下载对应语种 TTS 音色包")
            else:
                self.btn_read.setStyleSheet("")
                self.btn_read.setToolTip("")
                self.tts_voice.setText(self.tts.voice_name(lang))
                self.tts_voice.setToolTip("")
            try:
                self.chk_auto.setEnabled(bool(ok) or str(lang) == "en")
                self.chk_auto.setToolTip("" if ok else "请到 Lab「语音 / TTS」下载该语种音色包")
            except Exception:
                pass
        except Exception:
            pass

    def _enqueue_read(self, job):
        """FIFO 朗读队列：超过上限丢最旧的待读句，避免越排越多、声音拖尾。"""
        try:
            cap = max(1, int(self.cfg.get("tts_queue_max") or 3))
            try:
                if not (len(job) > 6 and int(job[6]) > 0) and any(
                        isinstance(j, tuple) and j and j[0] == job[0]
                        for j in list(self._read_jobs.queue)):
                    return                       # 同句已在排队，不重复合成
            except Exception:
                pass
            dropped = False
            while self._read_jobs.qsize() >= cap:
                try:
                    self._read_jobs.get_nowait()
                    dropped = True
                except queue.Empty:
                    break
            self._read_jobs.put(job)
            self._read_abort.clear()
            if dropped and time.monotonic() - getattr(self, "_skip_toast_at", 0) > 4:
                self._skip_toast_at = time.monotonic()
                self._show_toast("已跳过较早朗读任务")
            self._ensure_read_thread()
            self._update_queue_hint()
        except Exception as e:
            log("朗读入队失败：" + repr(e))

    def clear_read_queue(self):
        """一键清空待朗读队列（当前正在读的那句也停掉）。"""
        self.stop_read()
        self.set_status("朗读队列已清空", "ok")

    def _tts_rate_for(self, tl):
        scale = float((self.cfg.get("tts_rate_scales") or {}).get(tl, 0) or 0)
        if scale > 0:
            return max(-4, min(4, int(round((scale - 1.0) * 4.0))))
        return float((self.cfg.get("tts_rates") or {}).get(tl, self.cfg.get("read_rate") or 0))

    def _maybe_auto_read(self, en, audio=None):
        """自动实时朗读：短句 / 重复句 / 原声太吵时不读，避免声音轰炸。"""
        if not self.cfg.get("auto_read", False):
            return
        if self.cfg.get("tts_skip_fullscreen", True) and is_fullscreen_foreground():
            return                            # 全屏游戏 / 电影：不朗读，别盖原声
        if self.cfg.get("tts_duck_only") and audio is not None:
            try:
                import numpy as np
                rms = float(np.sqrt(np.mean(np.square(
                    np.asarray(audio, dtype=np.float32) / 32768.0))))
                if rms > float(self.cfg.get("tts_duck_threshold") or 0.035):
                    return                    # 原声还响着，读了也是打架
            except Exception:
                pass
        text = (en or "").strip()
        if not text:
            return
        if not self.tts.has_voice(CURRENT_LANGS.get("tl", "en")):
            return                            # 没装音色包：静默跳过，不报错
        if self.cfg.get("tts_skip_short", True):
            tl0 = CURRENT_LANGS.get("tl", "en")
            minw = int((self.cfg.get("tts_min_words") or {}).get(
                tl0, self.cfg.get("min_words") or 2))
            minw = max(2, minw)
            if len(text.split()) < minw:
                return
            if (self.cfg.get("tts_skip_duplicate", True)
                    and text == getattr(self, "_last_auto_read", "")):
                return
        self._last_auto_read = text
        mark_read_text(text)
        tl = CURRENT_LANGS.get("tl", "en")
        rates = self.cfg.get("tts_rates") or {}
        r = self._tts_rate_for(tl)
        vol = int((self.cfg.get("tts_volumes") or {}).get(tl, 100))
        if self.cfg.get("tts_muted"):
            return
        if self.cfg.get("shadow_mode"):
            self._enqueue_read((text, r, (self.cfg.get("tts_voices") or {}).get(tl), tl,
                                vol, float(self.cfg.get("tts_loop_pause") or 1.2),
                                1 if self.cfg.get("shadow_repeat", True) else 0))
        else:
            self._enqueue_read((text, r, (self.cfg.get("tts_voices") or {}).get(tl),
                                tl, vol, 0.0))

    def read_current(self, interrupt=False):
        text = (self._cur_en or self.dst_label.text()).strip()
        if not text or text == "等待声音…":
            return
        if interrupt:
            self.stop_read()
        tl = CURRENT_LANGS.get("tl", "en")
        rates = self.cfg.get("tts_rates") or {}
        r = self._tts_rate_for(tl)
        vol = int((self.cfg.get("tts_volumes") or {}).get(tl, 100))
        self._enqueue_read((text, r, (self.cfg.get("tts_voices") or {}).get(tl), tl, vol, 0.0))

    def read_text(self, text, lang="en", rate=None):
        """朗读任意一段文本（双向语音用它读英文，顺手走同一个队列，逐词高亮也不错乱）。"""
        text = (text or "").strip()
        if not text:
            return
        tl = str(lang or CURRENT_LANGS.get("tl", "en"))
        r = (self._tts_rate_for(tl) if rate is None else float(rate))
        voice = (self.cfg.get("tts_voices") or {}).get(tl)
        vol = int((self.cfg.get("tts_volumes") or {}).get(tl, 100))
        mark_read_text(text)
        self._enqueue_read((text, r, voice, tl, vol, 0.0))

    def refresh_hard_words(self):
        """把标记困难的单词收成一个集合：交互模式下给它们画细下划线。
        只收实词（长度 ≥ 4，去掉常见虚词），不然一句难句整行都划线，反而更乱。"""
        stop = {"this", "that", "these", "those", "with", "from", "have", "here", "there",
                "what", "when", "where", "which", "your", "yours", "they", "them", "then",
                "than", "will", "would", "could", "should", "just", "like", "into", "over",
                "need", "want", "going", "gonna", "round", "about", "still", "been", "were"}
        words = set()
        try:
            for r in read_fav():
                if str(r.get("diff") or "") != "困难":
                    continue
                if r.get("k") == "word":
                    w = str(r.get("v", "")).strip().lower()
                    if len(w) >= 3:
                        words.add(w)
                elif r.get("k") == "line":
                    for w in split_words(str(r.get("v", ""))):
                        wl = w.lower()
                        if len(wl) >= 4 and wl not in stop:
                            words.add(wl)
        except Exception:
            pass
        try:
            dst = getattr(getattr(self, "dst_label", None), "set_marks", None)
            if dst:
                self._hard_words = words
                dst(words)
                log("困难生词下划线：%d 个词" % len(words))
        except Exception as e:
            log("更新困难生词失败：" + repr(e))

    def _show_mic_card(self, zh, en, hint=""):
        """麦克风翻译的小卡片：贴在字幕条上方，只显示「你说的中文 / 英文」。"""
        from PySide6.QtCore import Qt as _Qt, QTimer
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtWidgets import QWidget, QFrame, QVBoxLayout, QLabel
        try:
            if self._mic_card is None:
                w = QWidget(None, _Qt.Tool | _Qt.FramelessWindowHint | _Qt.WindowStaysOnTopHint
                            | _Qt.WindowDoesNotAcceptFocus)
                w.setAttribute(_Qt.WA_TranslucentBackground, True)
                w.setAttribute(_Qt.WA_ShowWithoutActivating, True)
                lay = QVBoxLayout(w)
                lay.setContentsMargins(0, 0, 0, 0)
                card = QFrame()
                card.setStyleSheet(
                    "QFrame{background:rgba(28,27,25,225);border:1px solid #3E3A33;border-radius:10px;}")
                lay.addWidget(card)
                cv = QVBoxLayout(card)
                cv.setContentsMargins(12, 8, 12, 9)
                cv.setSpacing(2)
                tag = QLabel("你说")
                tag.setStyleSheet("color:#7BC39B;font-size:10px;")
                cv.addWidget(tag)
                self._mic_zh = QLabel("")
                self._mic_zh.setStyleSheet("color:#B4AFA6;font-size:11px;")
                self._mic_zh.setWordWrap(True)
                cv.addWidget(self._mic_zh)
                self._mic_en = QLabel("")
                self._mic_en.setStyleSheet("color:#FFFFFF;font-size:14px;font-weight:600;")
                self._mic_en.setWordWrap(True)
                cv.addWidget(self._mic_en)
                self._mic_hint = QLabel("")
                self._mic_hint.setStyleSheet("color:#8FA8B8;font-size:10px;")
                self._mic_hint.setWordWrap(True)
                cv.addWidget(self._mic_hint)
                self._mic_card = w
            self._mic_zh.setText((zh or "").strip())
            self._mic_en.setText((en or "").strip())
            self._mic_hint.setText((hint or "").strip())
            self._mic_hint.setVisible(bool(hint))
            self._mic_card.adjustSize()
            scr = QGuiApplication.primaryScreen()
            geo = scr.availableGeometry()
            x = geo.center().x() - self._mic_card.width() // 2
            y = geo.bottom() - self._mic_card.height() - 190
            self._mic_card.move(x, y)
            self._mic_card.show()
            if self._mic_hide_timer is not None:
                self._mic_hide_timer.stop()
            self._mic_hide_timer = QTimer()
            self._mic_hide_timer.setSingleShot(True)
            self._mic_hide_timer.timeout.connect(lambda: self._mic_card and self._mic_card.hide())
            self._mic_hide_timer.start(9000)
        except Exception as e:
            log("麦克风小卡片失败：" + repr(e))

    def stop_read(self):
        self._loop_active = False
        self._read_abort.set()
        self.tts.stop()
        try:
            while True:
                self._read_jobs.get_nowait()
        except queue.Empty:
            pass
        self._stop_highlight()
        self._speaking = False
        self.btn_read.setText("朗读")
        self._update_queue_hint()

    def _update_queue_hint(self):
        n = self._read_jobs.qsize()
        if n > 0 and self._speaking:
            self.queue_hint.setText("待朗读：%d" % n)
            self.queue_hint.setVisible(True)
        elif n > 1:
            self.queue_hint.setText("待朗读：%d" % n)
            self.queue_hint.setVisible(True)
        else:
            self.queue_hint.setVisible(False)

    def _start_highlight(self, text, duration):
        """按合成出来的真实时长，等分推进高亮；只是视觉反馈，不挡音频。"""
        self._pending_hl = (text, max(0.2, float(duration)))
        if not self.cfg.get("word_hl", True):
            return
        from PySide6.QtCore import QTimer
        if self._hl_timer is None:
            self._hl_timer = QTimer()
            self._hl_timer.timeout.connect(self._tick_highlight)
        self._hl_text = text
        self._hl_started = time.monotonic()
        self._hl_total = max(0.2, float(duration))
        self._hl_idx = -1
        self._hl_timer.start(90)

    def _stop_highlight(self):
        if self._hl_timer is not None:
            self._hl_timer.stop()
        if self._hl_text:
            self._hl_text = ""
            self.dst_label.setStyleSheet("color:%s;" % (self._hl_color or "#FFFFFF"))
        self._update_queue_hint()

    def _tick_highlight(self):
        from PySide6.QtCore import Qt
        if not self._hl_text:
            return
        words = [w for w in self._hl_text.split() if w]
        if not words:
            return
        progress = min(1.0, (time.monotonic() - self._hl_started) / self._hl_total)
        idx = min(len(words) - 1, int(progress * len(words)))
        if idx == self._hl_idx:
            return
        self._hl_idx = idx
        base = self._hl_color or "#FFFFFF"
        parts = []
        for i, w in enumerate(self._hl_text.split()):
            if i == idx:
                parts.append('<span style="color:#FFFFFF;background-color:rgba(255,255,255,40);">%s</span>' % self._esc(w))
            elif i < idx:
                parts.append('<span style="color:%s;">%s</span>' % (base, self._esc(w)))
            else:
                parts.append('<span style="color:%s;">%s</span>' % (self._dim(base, 0.55), self._esc(w)))
        self.dst_label.setTextFormat(Qt.RichText)
        self.dst_label.setText(" ".join(parts))

    # ---------------- 朗读按钮 ----------------
    def _read_clicked(self):
        if self._speaking:
            self.stop_read()
        else:
            self.read_current(interrupt=True)

    def _loop_toggled(self, on):
        self._loop_active = bool(on)
        self.cfg["loop_read"] = bool(on)
        save_config(self.cfg)
        if on:
            if not self.chk_auto.isChecked():
                self.chk_auto.setChecked(True)      # 循环跟读时自动朗读也打开
            self.read_current(interrupt=True)

    def _auto_toggled(self, on):
        self.cfg["auto_read"] = bool(on)
        save_config(self.cfg)
        if on:
            self.read_current(interrupt=True)

    def _save_audio_arr(self, arr):
        """把一句的原始音频存成 wav，收藏时一起带上（复习能听原声）。"""
        if not self.cfg.get("save_audio", True):
            return ""              # 低配机器可以关掉：不占磁盘、不做重采样写盘
        try:
            if arr is None or len(arr) < 8000:
                return ""
            import numpy as np
            import wave
            day = time.strftime("%Y-%m-%d")
            out = ROOT / "fav_audio" / day
            out.mkdir(parents=True, exist_ok=True)
            name = time.strftime("%H%M%S") + ("_%d.wav" % int((time.time() * 1000) % 1000))
            path = out / name
            data = (np.clip(arr, -1.0, 1.0) * 32767).astype("int16")
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(data.tobytes())
            return "fav_audio/%s/%s" % (day, name)
        except Exception as e:
            log("音频切片保存失败：" + repr(e))
            return ""

    def _flash_star(self, text="已收藏"):
        """收藏成功的轻量反馈：☆ 变绿、1 秒后复原；不弹窗、不遮画面。"""
        from PySide6.QtCore import QTimer
        try:
            if getattr(self, "_star_style_backup", None) is None:
                self._star_style_backup = self.btn_star.styleSheet() or ""
            self.btn_star.setText("✓")
            self.btn_star.setStyleSheet(self._star_style_backup +
                                        "color:#10B981;font-weight:700;")
            try:
                self.btn_fav_bar.setText("✓ 已收藏")
            except Exception:
                pass
            QTimer.singleShot(1000, self._restore_star)
        except Exception:
            pass

    def _restore_star(self):
        try:
            self.btn_star.setText("☆")
            if getattr(self, "_star_style_backup", None) is not None:
                self.btn_star.setStyleSheet(self._star_style_backup)
            try:
                self.btn_fav_bar.setText("☆ 收藏")
            except Exception:
                pass
        except Exception:
            pass

    def _fav_line(self, en, zh, spk, audio, conf, prev_en="", next_en="", diff="", seg_meta=None):
        en = (en or "").strip()
        if not en or en == "等待声音…":
            return False
        # 防重复入库：同一句短时间内重复点收藏，不新增条目，只给「已收藏」反馈
        now = time.monotonic()
        key = "line:" + en
        last = self._fav_guard.get(key)
        if last and now - last < 120:
            if last > 0:
                self._fav_guard[key] = -now        # -now 表示“重复过”，避免反复写
            self._show_toast("已收藏")
            self._flash_star()
            log("重复收藏已略过：%s" % en)
            return False
        self._fav_guard[key] = now
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        wav = self._save_audio_arr(audio)
        extra = ts
        if diff:
            extra = "难度 " + diff + " · " + ts
        if seg_meta and seg_meta.get("label"):
            extra += " · ⏱ " + str(seg_meta.get("label"))
        if conf:
            extra += " · 置信 %.2f" % float(conf)
        if prev_en:
            extra += " · 上句「%s」" % prev_en[:40]
        if next_en:
            extra += " · 下句「%s」" % next_en[:40]
        if wav:
            extra += " · ♪ " + wav
        payload = {"extra": extra, "zh": zh or "", "en": en, "spk": spk or ""}
        if diff:
            payload["diff"] = diff
        ok = append_fav("line", en, **payload)
        if ok:
            self._show_toast("已收藏" + ("（%s）" % diff if diff else ""))
            self._flash_star()
            # 外源链路句对：英文 → 中文（听懂维度），收藏即落库
            try:
                append_pair("in",
                            strip_fillers(en) if self.cfg.get("strip_fillers", True) else en,
                            zh or "", scene=str(self.cfg.get("scene") or ""),
                            role=str(self.cfg.get("role") or ""),
                            sess=getattr(self, "_sess_start", ""),
                            ctx=prev_en, fav=True, raw_en=en)
            except Exception as e:
                log("句对（外源）写入失败：" + repr(e))
            if diff == "困难":
                self.refresh_hard_words()      # 新标了困难词，下划线集合跟着更新
        return ok

    def fav_current(self, diff=""):
        prev_en = self._hist[0][0] if self._hist else ""
        return self._fav_line(self._cur_en or self.dst_label.text().replace("\n", " "),
                              self._cur_zh, self._cur_spk, self._cur_audio, self._cur_conf,
                              prev_en=prev_en, diff=diff, seg_meta=self._cur_meta)

    def fav_last(self, diff=""):
        """纯键盘收藏上一句：手还在键鼠上时不用挪鼠标。diff 可选，直接打难度标签。"""
        if not self._hist:
            self._show_toast("还没有上一句")
            return False
        en, zh, spk = self._hist[0][0], self._hist[0][1], self._hist[0][2]
        audio = self._hist[0][3] if len(self._hist[0]) > 3 else None
        conf = self._hist[0][4] if len(self._hist[0]) > 4 else 0.0
        seg_meta = self._hist[0][5] if len(self._hist[0]) > 5 else None
        prev_en = self._hist[1][0] if len(self._hist) > 1 else ""
        ok = self._fav_line(en, zh, spk, audio, conf, prev_en=prev_en,
                            next_en=self._cur_en or self.dst_label.text().replace("\n", " "),
                            diff=diff, seg_meta=seg_meta)
        if ok:
            log("快捷键收藏上一句%s：%s" % (("（" + diff + "）") if diff else "", en))
        return ok

    def clear_stream(self):
        """清空字幕流：当前这句和历史一起清掉，收听不中断。"""
        self._pending = None
        self._hist = []
        self._cur_zh = ""
        self._cur_audio = None
        self._cur_conf = 0.0
        self._zh_next = ""
        self._stop_highlight()
        self._hide_bubble()
        from PySide6.QtCore import Qt as _Qt
        self.dst_label.setTextFormat(_Qt.PlainText)
        self.dst_label.setText("")
        self.src_label.setText("")
        self._render_history()
        self._shrunk_once = False
        self._fit_height()
        self.set_status("字幕流已清空", "ok")
        self._show_toast("已清空")
        log("清空字幕流")

    def _fav_current(self):
        ok = self.fav_current()
        if ok:
            from PySide6.QtCore import QTimer
            self.btn_fav.setText("已收藏")
            QTimer.singleShot(1400, lambda: self.btn_fav.setText("☆ 收藏"))

    # ---------------- 交互模式：悬浮 ☆、划词、微型气泡 ----------------
    def _filter(self, obj, ev):
        from PySide6.QtCore import QEvent, QTimer
        from PySide6.QtWidgets import QLabel
        # 历史面板：鼠标离开 0.6 秒自动收起（勾了「钉住」就不收）
        if obj is getattr(self, "_hist_panel", None):
            if ev.type() == QEvent.Leave:
                QTimer.singleShot(600, self._maybe_hide_hist_panel)
            return False
        # Ctrl+滚轮调字幕字号：交互模式下直接滚，不用进设置
        if obj is self.panel and ev.type() == QEvent.Wheel:
            from PySide6.QtCore import Qt as _Qt
            if ev.modifiers() & _Qt.ControlModifier:
                self.bump_font(2 if ev.angleDelta().y() > 0 else -2)
                return True
        if not getattr(self, "_interactive", False) and not obj.property("sentence"):
            return False
        if obj is self.panel:
            if ev.type() == QEvent.Enter:
                self._place_star()
            elif ev.type() == QEvent.Leave and self._bubble is None:
                self.btn_star.hide()
        elif isinstance(obj, QLabel) and obj.property("sentence"):
            if ev.type() == QEvent.MouseButtonRelease:
                try:
                    sel = (obj.selectedText() or "").strip()
                except Exception:
                    sel = ""
                if sel:
                    QTimer.singleShot(0, lambda: self._on_selection(obj))
                else:
                    # 单击（没划词）＝把这句定位到主窗口记录表，方便收藏/打标签
                    QTimer.singleShot(0, lambda: self._locate(obj.property("sentence"),
                                                               obj.property("zh")))
        elif obj is self.dst_label and ev.type() == QEvent.MouseButtonRelease:
            try:
                sel = (obj.selectedText() or "").strip()
            except Exception:
                sel = ""
            if sel:
                QTimer.singleShot(0, self._on_selection)
            else:
                QTimer.singleShot(0, lambda: self._locate(
                    obj.property("sentence") or self._cur_en, self._cur_zh))
        return False

    def _locate(self, en, zh=""):
        """点悬浮字幕 → 主窗口记录表跳到这一行并高亮。"""
        try:
            cb = getattr(self, "on_locate", None)
            if cb is not None and (en or zh):
                cb(str(en or ""), str(zh or ""))
                self._show_toast("已定位到记录")
        except Exception as e:
            log("定位记录失败：" + repr(e))

    def _place_star(self):
        """短句把 ☆ 贴在句子末尾；一行放不下时退到卡片右缘。"""
        try:
            g = self.dst_label.geometry()
            text = (self.dst_label.text() or "").replace("\n", " ")
            fm = self.dst_label.fontMetrics()
            w = fm.horizontalAdvance(text)
            if 0 < w < g.width() - 34:
                x = g.left() + w + 8
                y = g.top() + max(0, (g.height() - 20) // 2)
            else:
                x = max(4, g.left() + g.width() - 26)
                y = max(4, g.top() + g.height() - 24)
            self.btn_star.move(max(4, x), max(4, y))
            self.btn_star.raise_()
            self.btn_star.show()
        except Exception:
            self.btn_star.show()

    # ---------------- 侧边迷你历史面板（最近 10 句，可收起） ----------------
    def _ensure_hist_panel(self):
        if self._hist_panel is not None:
            return self._hist_panel
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtWidgets import (QWidget, QFrame, QVBoxLayout, QHBoxLayout,
                                       QLabel, QPushButton, QScrollArea)
        w = QWidget(None, _Qt.Tool | _Qt.FramelessWindowHint | _Qt.WindowStaysOnTopHint
                    | _Qt.WindowDoesNotAcceptFocus)
        w.setAttribute(_Qt.WA_TranslucentBackground, True)
        w.setAttribute(_Qt.WA_ShowWithoutActivating, True)
        shell = QVBoxLayout(w)
        shell.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("histCard")
        card.setStyleSheet("QFrame#histCard{background:rgba(24,24,27,246);"
                           "border:1px solid rgba(255,255,255,30);border-radius:12px;}")
        shell.addWidget(card)
        cv = QVBoxLayout(card)
        cv.setContentsMargins(14, 12, 14, 12)
        cv.setSpacing(8)

        top = QHBoxLayout()
        title = QLabel("最近 6 句")
        title.setStyleSheet("color:#F5F1EA;font-size:14px;font-weight:600;background:transparent;")
        self._hist_count = QLabel("")
        self._hist_count.setStyleSheet("color:#9C978E;font-size:12px;background:transparent;")
        from PySide6.QtWidgets import QCheckBox as _QCB
        self._hist_pin = _QCB("钉住")
        self._hist_pin.setStyleSheet(
            "QCheckBox{color:#B9B4AA;font-size:11px;background:transparent;}"
            "QCheckBox::indicator{width:13px;height:13px;}")
        self._hist_pin.setToolTip("勾选后鼠标移开也不自动收起")
        btn_fold = QPushButton("收起")
        btn_fold.setFixedHeight(24)
        btn_fold.setCursor(_Qt.PointingHandCursor)
        btn_fold.setStyleSheet(
            "QPushButton{background:rgba(255,255,255,14);border:none;border-radius:10px;"
            "color:#E8E3D9;font-size:11px;padding:0 11px;}"
            "QPushButton:hover{background:rgba(150,196,170,70);}")
        btn_fold.clicked.connect(lambda: self.set_hist_panel(False))
        top.addWidget(title)
        top.addWidget(self._hist_count)
        top.addStretch(1)
        top.addWidget(self._hist_pin)
        top.addWidget(btn_fold)
        cv.addLayout(top)

        self._hist_body = QWidget()
        self._hist_rows = QVBoxLayout(self._hist_body)
        self._hist_rows.setContentsMargins(0, 0, 0, 0)
        self._hist_rows.setSpacing(8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(_Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}"
                             "QScrollBar:vertical{background:transparent;width:8px;margin:2px 0;}"
                             "QScrollBar::handle:vertical{background:rgba(220,215,205,90);"
                             "border-radius:4px;min-height:24px;}"
                             "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}")
        scroll.viewport().setStyleSheet("background:transparent;")
        scroll.setWidget(self._hist_body)
        scroll.setMaximumHeight(500)
        cv.addWidget(scroll)

        hint = QLabel("点 ☆ 收藏整句 · 选中英文单词可快速查义")
        hint.setStyleSheet("color:#948F87;font-size:11px;background:transparent;")
        hint.setWordWrap(True)
        cv.addWidget(hint)

        w.setFixedWidth(420)
        w.adjustSize()
        try:
            from PySide6.QtGui import QGuiApplication, QCursor
            scr = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
            geo = scr.availableGeometry()
            w.move(geo.right() - w.width() - 24, geo.top() + (geo.height() - w.height()) // 2)
        except Exception:
            pass
        self._hist_panel = w
        try:
            w.installEventFilter(self._filt)
        except Exception:
            pass
        return w

    def _resize_hist_panel(self):
        """按当前条数把面板贴到鼠标所在屏幕右侧；条数变化时跟着长高，最多 600px。"""
        try:
            if self._hist_panel is None:
                return
            from PySide6.QtGui import QGuiApplication, QCursor
            scr = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
            geo = scr.availableGeometry()
            n = len(self._recent[:6])
            try:
                self._hist_rows.activate()
                body_h = self._hist_body.sizeHint().height()
            except Exception:
                body_h = 0
            h = 150 if n <= 0 else min(600, max(180, body_h + 116))
            h = max(150, min(h, geo.height() - 120))
            self._hist_panel.resize(420, h)
            if getattr(self, "_hist_top", None) is None:
                self._hist_top = geo.top() + max(40, (geo.height() - h) // 2)
            self._hist_panel.move(geo.right() - 420 - 24, self._hist_top)
        except Exception:
            pass

    def _render_hist_panel(self):
        """把最近 10 句铺进侧边面板：英文可选中、句末 ☆ 可收藏。"""
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtWidgets import QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton
        try:
            panel = self._ensure_hist_panel()
            lay = self._hist_rows
            while lay.count():
                item = lay.takeAt(0)
                wdg = item.widget() if item else None
                if wdg:
                    wdg.hide()
                    wdg.deleteLater()
            items = self._recent[:6]
            self._hist_count.setText("%d 条" % len(items) if items else "")
            if not items:
                empty = QLabel("还没有字幕 · 等第一句")
                empty.setStyleSheet("color:#9C978E;font-size:13px;background:transparent;")
                lay.addWidget(empty)
                lay.addStretch(1)
                self._resize_hist_panel()
                return
            for i, row in enumerate(items):
                en, zh, spk, audio, conf = row[:5]
                row = QFrame()
                row.setObjectName("histRow")
                row.setStyleSheet(
                    "QFrame#histRow{background:rgba(255,255,255,18);"
                    "border-left:3px solid rgba(156,199,180,150);border-radius:8px;}")
                rv = QHBoxLayout(row)
                rv.setContentsMargins(14, 12, 10, 12)
                rv.setSpacing(8)
                col = QVBoxLayout()
                col.setSpacing(3)
                if spk:
                    s = QLabel(spk_alias(spk, self.cfg))
                    s.setStyleSheet("color:#A9C9BB;font-size:11px;background:transparent;")
                    col.addWidget(s)
                lab = QLabel(en)
                lab.setWordWrap(True)
                lab.setTextFormat(_Qt.PlainText)
                lab.setTextInteractionFlags(_Qt.TextSelectableByMouse | _Qt.TextSelectableByKeyboard)
                lab.setCursor(_Qt.IBeamCursor)
                lab.setProperty("sentence", en)
                lab.setStyleSheet("color:#FAF7F1;font-size:16px;font-weight:500;background:transparent;")
                lab.installEventFilter(self._filt)
                col.addWidget(lab)
                if zh:
                    z = QLabel(zh)
                    z.setWordWrap(True)
                    z.setStyleSheet("color:#C6C0B6;font-size:13px;background:transparent;")
                    z.setTextInteractionFlags(_Qt.NoTextInteraction)
                    col.addWidget(z)
                rv.addLayout(col, 1)
                star = QPushButton("☆")
                star.setFixedSize(26, 26)
                star.setCursor(_Qt.PointingHandCursor)
                star.setStyleSheet(
                    "QPushButton{background:transparent;border:none;color:#D8A24A;font-size:15px;}"
                    "QPushButton:hover{color:#F0BE68;}")
                star.clicked.connect(lambda chk=False, idx=i: self._fav_recent(idx))
                rv.addWidget(star, 0, _Qt.AlignTop)
                lay.addWidget(row)
            lay.addStretch(1)
            self._resize_hist_panel()
        except Exception as e:
            log("历史面板渲染失败：" + repr(e))

    def _fav_recent(self, idx):
        try:
            en, zh, spk, audio, conf = self._recent[idx][:5]
            seg_meta = self._recent[idx][5] if len(self._recent[idx]) > 5 else None
        except Exception:
            return
        prev_en = self._recent[idx + 1][0] if idx + 1 < len(self._recent) else ""
        next_en = self._recent[idx - 1][0] if idx - 1 >= 0 else ""
        if self._fav_line(en, zh, spk, audio, conf, prev_en=prev_en, next_en=next_en,
                          seg_meta=seg_meta):
            log("侧边历史收藏：%s" % en)
            self._show_toast("已收藏")
            self._render_hist_panel()

    def set_hist_panel(self, on):
        if not on:
            if self._hist_panel is not None:
                self._hist_panel.hide()
            self._hist_open = False
            try:
                self._hist_watch.stop()
            except Exception:
                pass
            self._hist_hovered = False
            self._hist_out_since = None
            self._hist_top = None
            return
        # 不依赖交互模式：全屏游戏放不出光标时，这个独立小窗就是后备入口
        self._ensure_hist_panel()
        self._hist_top = None
        self._render_hist_panel()
        try:
            self._resize_hist_panel()
            self._hist_panel.show()
            self._hist_panel.raise_()
        except Exception:
            pass
        self._hist_open = True
        self._hist_hovered = False       # 热键叫出来的面板不会 0.6 秒就自己消失，先碰过才计时
        self._hist_out_since = None
        try:
            self._hist_watch.start()
        except Exception:
            pass

    def _cursor_in_hist_panel(self):
        """用 Win32 的真实窗口矩形判断光标是否在面板上，避开 Qt 逻辑/物理坐标的比例差。"""
        try:
            import ctypes
            import ctypes.wintypes as wt
            pt = wt.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            r = wt.RECT()
            ctypes.windll.user32.GetWindowRect(int(self._hist_panel.winId()), ctypes.byref(r))
            return r.left <= pt.x < r.right and r.top <= pt.y < r.bottom
        except Exception:
            return False

    def _watch_hist_panel(self):
        """鼠标碰过面板、又离开 0.6 秒，就收起。不依赖 Leave 事件。"""
        if not self._hist_open or self._hist_panel is None:
            try:
                self._hist_watch.stop()
            except Exception:
                pass
            return
        try:
            if getattr(self, "_hist_pin", None) is not None and self._hist_pin.isChecked():
                return
        except Exception:
            pass
        inside = self._cursor_in_hist_panel()
        if inside:
            self._hist_hovered = True
            self._hist_out_since = None
            return
        if not self._hist_hovered:
            return                            # 从没碰过，就别自作主张关掉
        if self._hist_out_since is None:
            self._hist_out_since = time.monotonic()
            return
        if time.monotonic() - self._hist_out_since >= 0.6:
            self.set_hist_panel(False)

    def _maybe_hide_hist_panel(self):
        if not self._hist_open or self._hist_panel is None:
            return
        if not self._hist_hovered:
            return
        try:
            if getattr(self, "_hist_pin", None) is not None and self._hist_pin.isChecked():
                return
            if self._cursor_in_hist_panel():
                return
        except Exception:
            pass
        self.set_hist_panel(False)

    def bump_font(self, delta):
        """字幕字号实时加减，保存并重排卡片高度。"""
        try:
            cur = int(self.cfg.get("font_size") or 22)
            new = max(13, min(44, cur + int(delta)))
            if new == cur:
                self._show_toast("字号 %d 已是边界" % cur)
                return
            self.cfg["font_size"] = new
            save_config(self.cfg)
            self.update_look()
            self._fit_height()
            self._show_toast("字幕字号 %d" % new)
        except Exception as e:
            log("调整字幕字号失败：" + repr(e))

    def toggle_history_panel(self):
        self.set_hist_panel(not self._hist_open)
        log("历史面板：%s" % ("打开" if self._hist_open else "收起"))

    def _show_toast(self, text):
        from PySide6.QtCore import QTimer
        try:
            self.toast.setText(text)
            self.toast.adjustSize()
            self.toast.move(max(8, self.panel.width() - self.toast.width() - 16), 8)
            self.toast.raise_()
            self.toast.show()
            QTimer.singleShot(1500, self.toast.hide)
        except Exception:
            pass

    def _apply_selectable(self, on):
        """只有英文原文可以划词：中文译文和历史行的中文都不给选中，避免误拾中文。"""
        from PySide6.QtCore import Qt as _Qt
        try:
            self.src_label.setTextInteractionFlags(_Qt.NoTextInteraction)
            self.src_label.unsetCursor()
            if on:
                self.dst_label.setTextInteractionFlags(
                    _Qt.TextSelectableByMouse | _Qt.TextSelectableByKeyboard)
                self.dst_label.setCursor(_Qt.IBeamCursor)
            else:
                self.dst_label.setTextInteractionFlags(_Qt.NoTextInteraction)
                self.dst_label.unsetCursor()
            self._render_history()      # 缓冲区整体跟着模式改亮度与可选性
        except Exception as e:
            log("切换可选中失败：" + repr(e))

    def _on_selection(self, label=None):
        lab = label or self.dst_label
        try:
            text = (lab.selectedText() or "").strip()
        except Exception:
            text = ""
        if not text:
            return
        text = text.strip(" \t\r\n.,!?;:\"'()（）[]")
        if not text:
            return
        sentence = lab.property("sentence") or self.dst_label.text().replace("\n", " ")
        if " " in text:
            self._show_toast("已选中短语 · 点 ☆ 收藏整句")
            return
        self._show_bubble(text, sentence)

    def _mini_btn(self, text, color="#D8D3C9"):
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtWidgets import QPushButton
        b = QPushButton(text)
        b.setFixedHeight(20)
        b.setCursor(_Qt.PointingHandCursor)
        b.setStyleSheet(
            "QPushButton{background:rgba(255,255,255,8);border:none;border-radius:8px;"
            "color:%s;font-size:11px;padding:0 6px;}"
            "QPushButton:hover{background:rgba(255,255,255,28);color:#FFFFFF;}" % color)
        return b

    def _hide_bubble(self):
        try:
            if self._bubble is not None:
                self._bubble.close()
        except Exception:
            pass
        self._bubble = None
        if getattr(self, "_look_timer", None) is not None:
            self._look_timer.stop()

    def _show_bubble(self, word, sentence=""):
        from PySide6.QtCore import Qt as _Qt, QTimer
        from PySide6.QtGui import QCursor, QGuiApplication
        from PySide6.QtWidgets import QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel
        self._hide_bubble()

        b = QWidget(None, _Qt.Tool | _Qt.FramelessWindowHint | _Qt.WindowStaysOnTopHint
                    | _Qt.WindowDoesNotAcceptFocus)
        b.setAttribute(_Qt.WA_TranslucentBackground, True)
        b.setAttribute(_Qt.WA_ShowWithoutActivating, True)

        shell = QVBoxLayout(b)
        shell.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setStyleSheet("QFrame{background:#1C1B19;border:1px solid #34302A;border-radius:10px;}")
        card.setGraphicsEffect(None)
        shell.addWidget(card)

        cv = QVBoxLayout(card)
        cv.setContentsMargins(10, 7, 10, 7)
        cv.setSpacing(3)

        wl = QLabel(word)
        wl.setStyleSheet("color:#FFFFFF;font-size:13px;font-weight:600;")
        cv.addWidget(wl)
        ml = QLabel("查询中…")
        ml.setStyleSheet("color:#9C978D;font-size:11px;")
        ml.setWordWrap(True)
        ml.setMaximumWidth(200)
        cv.addWidget(ml)

        row = QHBoxLayout()
        row.setSpacing(4)
        bf = self._mini_btn("收藏")
        bp = self._mini_btn("固定")
        for x in (bf, bp):
            row.addWidget(x)
        row.addStretch(1)
        cv.addLayout(row)

        state = {"meaning": ""}

        def do_translate():
            sent = sentence or self.dst_label.text().replace("\n", " ")
            gloss = gloss_line(word, self.cfg.get("scene") or "fps")   # 场景释义优先
            if gloss:
                self._look_q.put((word, gloss))
                return
            try:
                book = getattr(self, "book", None)
                if book is None:
                    book = WordBook(self.cfg)
                    self.book = book
                m = book.lookup(word, sent)
            except Exception as e:
                m = ""
                log("快速翻译失败：" + repr(e))
            if len(m) > 70:
                m = m[:70] + "…"          # 气泡只给一眼能看完的释义，完整解析留生词本
            self._look_q.put((word, m or "查不到这个词，生词本里可手动补"))

        def poll_look():
            from PySide6.QtCore import QTimer
            try:
                w, m = self._look_q.get_nowait()
            except queue.Empty:
                return
            if self._bubble is b:
                state["meaning"] = m
                ml.setText(m)

        def flash_word():
            """收藏成功的轻量反馈：单词变绿 + 释义位置写一句提示，1 秒复原。"""
            from PySide6.QtCore import QTimer
            wl.setStyleSheet("color:#10B981;font-size:13px;font-weight:700;")
            QTimer.singleShot(1000, lambda: wl.setStyleSheet(
                "color:#FFFFFF;font-size:13px;font-weight:600;") if self._bubble is b else None)

        def do_fav():
            phrase = sentence or self.dst_label.text().replace("\n", " ")
            key = "word:%s|%s" % (word.lower(), phrase)
            now = time.monotonic()
            last = self._word_guard.get(key)
            if last and now - last < 120:
                ml.setText("已收藏")
                flash_word()
                return
            self._word_guard[key] = now
            low = bool(getattr(self, "_cur_conf", 0.0) and self._cur_conf < LOW_CONF)
            if append_word(word, state["meaning"], phrase, self._cur_spk, low_conf=low,
                           scene=self.cfg.get("scene") or "",
                           sess=getattr(self, "_sess_start", "")):
                ml.setText("已收藏 · 语境已挂到词卡" + ("（置信偏低，已标注）" if low else ""))
                flash_word()
            else:
                ml.setText("这句已经在这张词卡里了")
                flash_word()

        def do_pin():
            self._pin(word, state["meaning"] or "（还没查）")
            self._hide_bubble()

        bf.clicked.connect(do_fav)
        bp.clicked.connect(do_pin)

        b.adjustSize()
        pos = QCursor.pos()
        scr = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        geo = scr.availableGeometry()
        x = pos.x() + 14
        y = pos.y() + 12
        if x + b.width() > geo.right() - 6:
            x = max(geo.left() + 6, pos.x() - b.width() - 12)
        if y + b.height() > geo.bottom() - 6:
            y = max(geo.top() + 6, pos.y() - b.height() - 12)
        b.move(x, y)
        b.show()
        self._bubble = b
        do_translate()          # 选中即查，气泡只承担"一眼看完"

        from PySide6.QtCore import QTimer
        self._look_timer = QTimer()
        self._look_timer.timeout.connect(poll_look)
        self._look_timer.start(120)
        QTimer.singleShot(9000, lambda: self._hide_bubble() if self._bubble is b else None)

    def _pin(self, word, meaning):
        """固定气泡：钉在屏幕右上角，一点就关，方便多看两眼。"""
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtWidgets import QWidget, QFrame, QVBoxLayout, QLabel
        from PySide6.QtGui import QGuiApplication
        w = QWidget(None, _Qt.Tool | _Qt.FramelessWindowHint | _Qt.WindowStaysOnTopHint
                    | _Qt.WindowDoesNotAcceptFocus)
        w.setAttribute(_Qt.WA_TranslucentBackground, True)
        w.setAttribute(_Qt.WA_ShowWithoutActivating, True)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setStyleSheet("QFrame{background:#1C1B19;border:1px solid #3A352E;border-radius:10px;}")
        lay.addWidget(card)
        cv = QVBoxLayout(card)
        cv.setContentsMargins(12, 8, 12, 9)
        cv.setSpacing(2)
        head = QLabel(word)
        head.setStyleSheet("color:#FFFFFF;font-size:13px;font-weight:600;")
        cv.addWidget(head)
        body = QLabel(meaning)
        body.setWordWrap(True)
        body.setMaximumWidth(230)
        body.setStyleSheet("color:#B4AFA6;font-size:11px;")
        cv.addWidget(body)
        tip = QLabel("点一下收起")
        tip.setStyleSheet("color:#6E6A63;font-size:10px;")
        cv.addWidget(tip)
        w.adjustSize()
        scr = QGuiApplication.primaryScreen()
        geo = scr.availableGeometry()
        w.move(geo.right() - w.width() - 24, geo.top() + 90)
        w.mousePressEvent = lambda ev: w.close()
        w.show()
        self._pins.append(w)

    def _chrome(self):
        return [self.btn_pause, self.btn_settings, self.btn_quit, self.chk_auto,
                self.tts_voice, self.btn_fav_bar, self.btn_speak_bar,
                self.btn_fav, self.btn_read, self.btn_loop,
                self.btn_clear_q, self.btn_font_minus, self.btn_font_plus]

    def _apply_mode_visuals(self, on):
        """非交互：字幕上不给任何按钮，只留字幕本身；交互：露出控件 + 淡描边 + 角落提示 + 箭头光标。"""
        for w in self._chrome():
            try:
                w.setVisible(bool(on))
            except Exception:
                pass
        if not on:
            self.mode_hint.setVisible(False)
            self.queue_hint.setVisible(False)
            self.btn_star.hide()
        self._glass()      # 描边跟着模式变
        self._corner_hint(bool(on))
        try:
            from PySide6.QtCore import Qt as _Qt
            if on:
                self.win.setCursor(_Qt.ArrowCursor)
            else:
                self.win.unsetCursor()
        except Exception:
            pass

    def _corner_hint(self, on):
        """屏幕右下角一行极小提示，告诉玩家现在能不能点字幕。"""
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtWidgets import QLabel
        if on:
            if getattr(self, "_corner", None) is None:
                c = QLabel("字幕交互模式｜ESC 退出")
                c.setWindowFlags(_Qt.Tool | _Qt.FramelessWindowHint
                                 | _Qt.WindowStaysOnTopHint | _Qt.WindowDoesNotAcceptFocus)
                c.setAttribute(_Qt.WA_TranslucentBackground, True)
                c.setAttribute(_Qt.WA_ShowWithoutActivating, True)
                c.setStyleSheet("color:#A8D6BA;background:rgba(16,20,17,140);"
                                "padding:3px 9px;border-radius:7px;font-size:11px;")
                c.adjustSize()
                scr = QGuiApplication.primaryScreen().availableGeometry()
                c.move(scr.right() - c.width() - 22, scr.bottom() - c.height() - 22)
                self._corner = c
            self._corner.show()
        elif getattr(self, "_corner", None) is not None:
            self._corner.hide()

    def force_exit_interactive(self):
        """备用逃生键：不管什么状态都把穿透还回去，防止光标卡在字幕层。"""
        try:
            if self._interactive:
                self.set_interactive(False)
                self._show_toast("已强制退出交互")
                log("强制退出交互（备用热键）")
            else:
                from PySide6.QtCore import Qt as _Qt
                self.win.setAttribute(_Qt.WA_TransparentForMouseEvents, True)
                self._show_toast("已是穿透状态")
        except Exception as e:
            log("强制退出失败：" + repr(e))

    def set_interactive(self, on):
        """交互模式：解除穿透，鼠标能选字幕文字；退出后恢复穿透，鼠标还给游戏。
        只改窗口的鼠标属性和文字可选性——识别、VAD、翻译、朗读线程一律照跑，不额外占算力。"""
        from PySide6.QtCore import Qt as _Qt
        self._interactive = bool(on)
        if on:
            self._itv_at = time.monotonic()      # 无操作计时起点
            self._itv_pos = None
            try:
                fs, title = foreground_fullscreen()
            except Exception:
                fs, title = False, ""
            if fs:
                self._fs_fullscreen = True
                self._show_toast("全屏游戏 · 光标可能放不出来，试试 Ctrl+Alt+H 面板")
                log("交互模式：前台是全屏窗口「%s」，独占全屏可能抓不住光标；"
                    "建议改成无边框窗口，或直接用 Ctrl+Alt+H 的历史面板" % title)
        try:
            self.win.setAttribute(_Qt.WA_TransparentForMouseEvents, not on)
        except Exception:
            pass
        if on:
            try:
                self.win.raise_()
                self.win.activateWindow()
            except Exception:
                pass
        self.mode_hint.setText("交互模式 · 可选中字幕 / 拖右下角调宽" if on else "")
        self.mode_hint.setVisible(bool(on))
        self._apply_selectable(bool(on))
        self._apply_mode_visuals(bool(on))
        self._sync_handle()          # 进交互收起抓手，退出交互再挂回来
        try:
            # 困难生词下划线只在交互模式画：非交互时画面上一点额外东西都没有
            self.dst_label.set_mark_mode(bool(on) and bool(self.cfg.get("hard_underline", True)))
            if on and not self._hard_words:
                self.refresh_hard_words()
        except Exception:
            pass
        if not on:
            self.btn_star.hide()
            self._hide_bubble()
            # 侧边历史面板不跟着退出：全屏抓不住光标时它就是唯一能用的入口

    def _enter(self, ev):
        self._frozen = True
        self._status_backup = self.status.text()
        self.set_status("已冻结 · 移开继续", "ok")

    def _leave(self, ev):
        self._frozen = False
        if self._status_backup:
            self.status.setText(self._status_backup)
        self._flush_target()

    # 拖动 / 右下角调宽
    def _press(self, ev):
        from PySide6.QtCore import Qt
        if ev.button() != Qt.LeftButton:
            return
        self._drag_pos = None
        self._resizing = False
        self._drag_moved = False
        # 交互模式下贴近右下角 18px 起拖 = 调字幕条宽度，别的区域仍是移动窗口
        try:
            if getattr(self, "_interactive", False):
                p = ev.position()
                if p.x() >= self.panel.width() - 18 and p.y() >= self.panel.height() - 18:
                    self._resizing = True
                    self._rs_base = (ev.globalPosition().x(), self.win.width())
                    return
        except Exception:
            pass
        self._drag_pos = ev.globalPosition().toPoint() - self.win.frameGeometry().topLeft()

    def _move(self, ev):
        from PySide6.QtCore import Qt
        # 在字幕条外松手时收不到 mouseRelease，下一次移动先补一次收尾
        if getattr(self, "_resizing", False) and not (ev.buttons() & Qt.LeftButton):
            self._release(ev)
            return
        if (not getattr(self, "_resizing", False) and self._drag_pos is not None
                and not (ev.buttons() & Qt.LeftButton)):
            self._release(ev)
            return
        if getattr(self, "_resizing", False) and ev.buttons() & Qt.LeftButton:
            try:
                base_x, base_w = self._rs_base
                scr = self.win.screen().availableGeometry()
                w = max(480, min(scr.width() - self.MARGIN * 2, base_w + (ev.globalPosition().x() - base_x)))
                self.win.resize(int(w), self.win.height())
                self._fit_height()
            except Exception:
                pass
            return
        if self._drag_pos is not None and ev.buttons() & Qt.LeftButton:
            self.win.move(ev.globalPosition().toPoint() - self._drag_pos)
            self._drag_moved = True

    def _release(self, ev):
        if getattr(self, "_resizing", False):
            self._resizing = False
            try:
                win = self.cfg.setdefault("window", {})
                win["w"] = int(self.win.width())
                save_config(self.cfg)
                self._show_toast("字幕宽度 %d" % self.win.width())
            except Exception as e:
                log("保存字幕宽度失败：" + repr(e))
            return
        if not getattr(self, "_drag_moved", False):
            self._drag_pos = None
            return
        self._drag_moved = False
        self._drag_pos = None
        try:
            scr = self._avail_rect()
            x, y = self.win.x(), self.win.y()
            w, h = self.win.width(), self.win.height()
            center_x = scr.left() + (scr.width() - w) // 2
            preset = "free"
            # 拖到中线附近就吸附到正中；拖到底部安全线附近就吸附成「底部居中」
            if abs(x - center_x) <= 24:
                x = center_x
            if abs(scr.bottom() - (y + h) - 72) <= 24:
                y = scr.bottom() - h - 72
                preset = "bottom"
            elif abs(y - (scr.top() + 96)) <= 24:
                y = scr.top() + 96
                preset = "top"
            self.cfg["overlay_pos"] = preset
            self.win.move(x, y)
            self._remember_geometry()
            save_config(self.cfg)
            self._show_toast("字幕位置已保存")
        except Exception as e:
            log("保存字幕位置失败：" + repr(e))

    def show(self):
        self.win.show()
        self.apply_position()
        self.win.raise_()

    def _avail_rect(self):
        from PySide6.QtGui import QGuiApplication
        scr = self.win.screen() or QGuiApplication.primaryScreen()
        return scr.availableGeometry()

    def apply_position(self, preset=None, save=False):
        """按预设把字幕条摆好：默认底部居中，离任务栏留 72px，不贴着画面底边。"""
        try:
            if preset is not None:
                self.cfg["overlay_pos"] = str(preset)
            preset = str(self.cfg.get("overlay_pos") or "bottom")
            scr = self._avail_rect()
            # 对话框模式：高度锁死在固定档位，留白按屏高比例算（换分辨率观感一致）
            dialog = str(self.cfg.get("overlay_style") or "dialog") == "dialog"
            gap = max(40, int(scr.height() * 0.055)) if dialog else 72
            w = max(480, self.win.width())
            if dialog:
                h = self._dialog_h() if self._has_text() else 92
            else:
                h = max(100, self.win.height())
            self.win.resize(w, h)
            if preset == "free":
                win = self.cfg.get("window") or {}
                x = int(win.get("x") or (scr.left() + (scr.width() - w) // 2))
                y = int(win.get("y") or (scr.bottom() - h - gap))
                self.win.move(x, y)
            else:
                x = scr.left() + (scr.width() - w) // 2
                if preset == "top":
                    # 顶部固定 = 贴住屏幕上沿；留白只靠窗口自身的 MARGIN，
                    # 不能再按屏高比例加 60px，否则看起来像“浮在中上”而不是顶住。
                    # overlay_gap_top 让用户自己往下压一点，避开游戏顶部血条 / 任务 HUD。
                    gap_top = int(self.cfg.get("overlay_gap_top") or 8)
                    y = scr.top() + max(0, min(200, gap_top))
                elif preset == "bottom_high":
                    y = scr.bottom() - h - int(scr.height() * 0.16)
                else:
                    y = scr.bottom() - h - gap
                self.win.move(x, y)
            self._clamp_to_screen()
            self._sync_handle()
            if save:
                self._remember_geometry()
                save_config(self.cfg)
            log("字幕条几何：%dx%d @%d,%d（%s/%s）" % (
                self.win.width(), self.win.height(), self.win.x(), self.win.y(),
                preset, str(self.cfg.get("overlay_style"))))
        except Exception as e:
            log("摆放字幕条失败：" + repr(e))

    def _clamp_to_screen(self):
        """把字幕条夹回可用屏幕区域：改分辨率、拔掉副屏、窗口记忆失效时都不让字幕跑出屏幕。"""
        try:
            scr = self._avail_rect()
            w = max(1, self.win.width())
            h = max(1, self.win.height())
            x = min(max(scr.left(), self.win.x()), max(scr.left(), scr.right() - w))
            y = min(max(scr.top(), self.win.y()), max(scr.top(), scr.bottom() - h))
            if (x, y) != (self.win.x(), self.win.y()):
                self.win.move(x, y)
        except Exception as e:
            log("字幕条位置收敛失败：" + repr(e))

    def _remember_geometry(self):
        try:
            geo = self.win.geometry()
            self.cfg["window"] = {"x": geo.x(), "y": geo.y(),
                                  "w": geo.width(), "h": geo.height()}
        except Exception:
            pass

    def set_status(self, text, tone=""):
        color = {"ok": "#10B981", "listening": "#10B981", "info": "#3B82F6", "loading": "#3B82F6", "warn": "#F59E0B", "error": "#EF4444"}.get(tone, "#9AA3B2")
        self.dot.setStyleSheet("color:%s;font-size:10px;" % color)
        self.status.setText(text)
        fade_in(self.status, 120)
        # 状态文字变化后整块重绘，避免上一段状态在半透明卡片上留下残字
        self._schedule_full_repaint()

    def _tick(self):
        try:
            while True:
                parts = self.q.get_nowait()
                kind = parts[0]
                a = parts[1] if len(parts) > 1 else ""
                b = parts[2] if len(parts) > 2 else ""
                spk = parts[3] if len(parts) > 3 else ""
                if kind == "source":
                    self._zh_next = a
                    self._last_voice_at = time.monotonic()
                    if self._idle_on:
                        self._idle_on = False
                        self.set_status("识别中", "ok")
                    self.src_label.setText(a)
                    self._fit_height()
                elif kind == "target":
                    audio = parts[4] if len(parts) > 4 else None
                    conf = parts[5] if len(parts) > 5 else 0.0
                    seg_meta = parts[6] if len(parts) > 6 else None
                    self._pending = (a, b, spk, audio, conf, seg_meta)
                    self._pending_at = time.monotonic()   # 到达时刻，碎句合并按它算间隔
                    self._last_voice_at = time.monotonic()
                    # 会话分袋：静音超过 session_gap 就开新的会话包（一局游戏 / 一段视频）
                    try:
                        gap = float(self.cfg.get("session_gap") or 120)
                        if time.monotonic() - self._sess_at > gap:
                            self._sess_start = time.strftime("%H:%M:%S")
                            log("新会话包：%s" % self._sess_start)
                    except Exception:
                        pass
                    self._sess_at = time.monotonic()
                    if self._idle_on:
                        self._idle_on = False
                        self.set_status("识别中", "ok")
                    self._flush_target()
                elif kind == "state":
                    self.set_status(a, b)
                elif kind == "say":
                    # 双向语音：麦克风说中文 → 英文小卡片（可带应答建议）
                    self._show_mic_card(a, b, parts[3] if len(parts) > 3 else "")
                    if self.cfg.get("mic_autoread") and b:
                        try:
                            self.read_text(b, lang="en")
                        except Exception:
                            pass
        except queue.Empty:
            pass
        # 静音过滤：长时间没语音就把字幕停住，不刷新、不往缓冲区里塞空行
        try:
            idle = float(self.cfg.get("idle_pause") or 0)
            if idle > 0 and (time.monotonic() - self._last_voice_at) > idle and not self._idle_on:
                self._idle_on = True
                self.set_status("安静中 · 字幕暂停", "")
        except Exception:
            pass
        try:
            while True:
                ev = self._read_events.get_nowait()
                if ev[0] == "load":
                    self._speaking = True
                    self.btn_read.setText("合成中")
                elif ev[0] == "start":
                    self._speaking = True
                    self.btn_read.setText("暂停")
                    self._start_highlight(ev[1], ev[2])
                elif ev[0] == "end":
                    self._speaking = False
                    self.btn_read.setText("朗读")
                    self._stop_highlight()
                    self._update_queue_hint()
        except queue.Empty:
            pass

    def set_speaker(self, spk):
        """说话人用小标签 + 正文字体颜色一起区分：换人换色。"""
        use = bool(spk) and bool(self.cfg.get("diarize", False))
        if not use:
            self.spk_label.setText("")
            self.spk_label.setVisible(False)
            _k2, _sk2 = skin_of(self.cfg)
            self.dst_label.setStyleSheet("color:%s;" % _sk2["main"])
            self._hl_color = _sk2["main"]
            return
        color = spk_color(spk)
        self._hl_color = color
        self.spk_label.setText("● " + spk_alias(spk, self.cfg))
        self.spk_label.setStyleSheet(
            "color:%s;font-weight:600;padding:0 8px 0 0;" % color)
        self.spk_label.setVisible(True)
        self.dst_label.setStyleSheet("color:%s;" % color)

    def _hold_for(self, text):
        """在屏幕上至少要待多久：够扫一眼就行。

        以前 balanced 是 850ms + 85ms/词、最长 3.2 秒，连续视频里上一句还没走
        下一句就排队了。现在整体压短，让字幕跟着说话节奏滚动，宁可快一点，
        也不把延迟越攒越多。
        """
        words = len([w for w in text.replace("—", " ").split() if w])
        scale = float(self.cfg.get("hold_scale") or 1.0)
        mode = str(self.cfg.get("latency_mode") or "balanced").lower()
        base, per_word, floor, cap = {
            "fast": (520, 55, 700, 1900),
            "balanced": (650, 65, 850, 2400),
            "accurate": (820, 85, 1050, 2800),
        }.get(mode, (650, 65, 850, 2400))
        ms = (base + per_word * words) * scale
        return int(max(floor, min(cap, ms)))

    def _flush_target(self):
        """够短读完就换句；没到最短停留就先挂起，别让延迟越攒越多。"""
        if self._pending is None:
            return
        if self._frozen:                       # 鼠标停在字幕条上，先不换
            self.QTimer_once(400)
            return
        elapsed = (time.monotonic() - self._shown_at) * 1000
        floor = self._hold_for(self._cur_en or self.dst_label.text())
        wait = floor - elapsed
        # 后面已经排队超过 0.9 秒：说明当前句挂太久了，优先让新句上来，别让延迟越攒越多
        if wait > 0 and (time.monotonic() - getattr(self, "_pending_at", 0.0)) >= 0.9:
            wait = 0
        if wait > 0:
            self.QTimer_once(int(wait) + 15)
            return

        en, extra, spk, audio, conf, seg_meta = self._pending
        self._pending = None
        if not str(en).strip():
            # 静音过滤：没识别出文字就不换行、不进缓冲、不留空行
            return
        zh_new = self._zh_next or ""
        if should_filter_short(zh_new, en, self.cfg):
            self._zh_next = ""
            return
        self.set_speaker(spk)
        old = self._cur_en or self.dst_label.text()
        merged = False
        if old and old != en:
            recency = time.monotonic() - getattr(self, "_pending_at", 0.0)
            # 断句碎块就地合并：只并「同一说话人 + 确实是续句」的情况。
            # 之前只要上句短就无条件并，两个不同人 / 不相干的短句会被粘成一行。
            same_spk = str(spk or "") == str(self._cur_spk or "")
            tail_open = not str(old).rstrip().endswith((".", "?", "!", "。", "？", "！"))
            is_cont = str(en).startswith(str(old)) or (
                tail_open and len(str(old).split()) <= 3)
            if recency < 1.2 and same_spk and is_cont:
                if not str(en).startswith(str(old)):
                    en = (str(old) + " " + str(en)).strip()
                zh_new = (self._cur_zh + " " + zh_new).strip()
                merged = True
                log("碎句合并：%s" % en)
            else:
                self._hist.insert(0, (old, self._cur_zh, self._cur_spk, self._cur_audio,
                                      self._cur_conf, self._cur_meta))
                # 滚动缓冲区：除当前这一句外，再留 scroll_rows-1 条
                keep = max(1, self._scroll_rows() - 1)
                self._hist = self._hist[:keep]
                self._render_history()
        self._cur_spk = spk
        self._cur_zh = zh_new
        self._cur_en = en
        self._zh_next = ""
        self._cur_audio = audio
        self._cur_conf = conf
        self._cur_meta = seg_meta
        if not merged:
            self._recent.insert(0, (en, self._cur_zh, spk, audio, conf, seg_meta))
            self._recent = self._recent[:10]
        if self._hist_open:
            self._render_hist_panel()
        self._stop_highlight()
        from PySide6.QtCore import Qt as _Qt
        self.dst_label.setTextFormat(_Qt.PlainText)
        if self.cfg.get("overlay_bilingual", False) and zh_new:
            # 对照模式：中文 ｜ 英文 挤在同一行，卡片不会因为多一行变高
            self.dst_label.setText("%s  ｜  %s" % (zh_new, en))
            self.dst_label.setFont(self._font(max(13, int((self.cfg.get("font_size") or 22) * 0.72)),
                                              True, "Segoe UI"))
        else:
            # 主体选“另一种语言”：说中文就主体英文（译文）、副行中文原文；
            # 说英文就主体中文（译文）、副行英文原文。原来无论如何都只画 en，
            # 说英文时等于只看到英文，中文永远不出现。
            _cjk = re.compile(r"[\u4e00-\u9fff]")
            en_is_cjk = bool(_cjk.search(str(en or "")))
            zh_is_cjk = bool(_cjk.search(str(zh_new or "")))
            if zh_is_cjk and not en_is_cjk:
                main_txt, sub_txt = zh_new, en
            elif en_is_cjk and not zh_is_cjk:
                main_txt, sub_txt = en, zh_new
            else:
                main_txt, sub_txt = en, ""
            self.dst_label.setFont(self._font(int(self.cfg.get("font_size") or 22), True,
                                              "Segoe UI", 0.2))
            # 副行（另一种语言）走上方小字位，主体大字留在下面，读起来才知道哪句是译文
            try:
                fs_now = int(self.cfg.get("font_size") or 22)
                if sub_txt and str(sub_txt).strip() != str(main_txt).strip():
                    self.src_label.setFont(self._font(max(12, int(fs_now * 0.6)),
                                                      False, "Microsoft YaHei"))
                    self.src_label.setText(str(sub_txt))
                    self.src_label.setVisible(bool(self.cfg.get("show_source", True)))
                else:
                    self.src_label.setText("")
            except Exception:
                pass
            self.dst_label.setText(main_txt)
        self.dst_label.setProperty("sentence", en)
        self.dst_label.setProperty("zh", zh_new)
        try:
            self._maybe_auto_read(en, audio)
        except Exception as e:
            log("自动朗读失败：" + repr(e))
        try:
            self.dst_label.set_marks(getattr(self, "_hard_words", set()))
            self.dst_label.set_mark_mode(bool(self._interactive)
                                         and bool(self.cfg.get("hard_underline", True)))
        except Exception:
            pass
        # 置信度可视化：识别没把握的句子文字轻微变淡，不标红，也不影响朗读和收藏
        try:
            from PySide6.QtWidgets import QGraphicsOpacityEffect
            if conf and float(conf) < LOW_CONF:
                eff = QGraphicsOpacityEffect(self.dst_label)
                eff.setOpacity(0.7)
                self.dst_label.setGraphicsEffect(eff)
            else:
                self.dst_label.setGraphicsEffect(None)
        except Exception:
            pass
        self._shown_at = time.monotonic()
        self._hold_ms = self._hold_for(en)
        try:
            sec = max(2, min(30, int(self.cfg.get("overlay_idle_hide", 8))))
            self._hist_idle.start(sec * 1000)
        except Exception:
            pass
        self._apply_skin()
        self._fit_height()
        lag_txt = ""
        try:
            if seg_meta and seg_meta.get("t1"):
                lag_txt = "，端到端 %.1f 秒" % (time.time() - float(seg_meta["t1"]))
        except Exception:
            pass
        log("屏幕显示（最短 %.1f 秒%s）：%s" % (floor / 1000.0, lag_txt, en))
        if self.chk_auto.isChecked():
            self.read_current()
        if extra:
            # 翻译耗时只写日志，不再刷到字幕条顶部：那行状态一变长，
            # 半透明卡片上容易留下压在字幕行上的残影，而且对观看没有价值。
            log("译文生成（%s）：%s" % (extra, en))

    def QTimer_once(self, ms):
        from PySide6.QtCore import QTimer
        QTimer.singleShot(ms, self._flush_target)

    def _idle_hide_history(self):
        """静音一阵后把上一句收起来，字幕条回落到单行高度。"""
        try:
            if self.hist_box.isVisibleTo(self.win):
                self.hist_box.hide()
                self.divider.hide()
                self._fit_height()
        except Exception:
            pass

    def _full_repaint(self):
        """半透明窗口整窗重画：只重画局部会留下上一帧的字形残影。"""
        try:
            self.panel.repaint(self.panel.rect())
            self.win.repaint(self.win.rect())
        except Exception:
            pass

    def _schedule_full_repaint(self):
        try:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self._full_repaint)
            # 布局在本次事件循环末尾还会再动一次，延迟再擦一遍才干净
            QTimer.singleShot(60, self._full_repaint)
        except Exception:
            pass

    def _dialog_h(self):
        """Galgame 对话框的固定高度：约 3 行正文 + 上下留白，跟着字号微调。"""
        try:
            n = int(self.cfg.get("overlay_dialog_h") or 0)
        except Exception:
            n = 0
        if n >= 96:
            return n
        try:
            fs = int(self.cfg.get("font_size") or 20)
        except Exception:
            fs = 20
        return max(200, min(340, int(fs * 1.5 * 3) + 168))

    def _toggle_anchor(self):
        """抓手上的 ⇅：在顶部 / 底部之间来回换，专治挡住游戏 HUD 或挡往站立角色。"""
        try:
            cur = str(self.cfg.get("overlay_pos") or "top")
            self.cfg["overlay_pos"] = "bottom_high" if cur == "top" else "top"
            self.cfg.pop("window", None)
            self.apply_position(save=True)
            self._fit_height()
            self._sync_handle()
            self._show_toast("字幕条已移到" + ("下部" if self.cfg["overlay_pos"] == "bottom_high"
                                           else "顶部"))
            log("字幕条锚点切换：" + str(self.cfg["overlay_pos"]))
        except Exception as e:
            log("切换字幕条位置失败：" + repr(e))

    def _sync_handle(self):
        """让抓手胶囊跟着字幕条走：贴右上角外侧，穿透态常驻，交互态收起。"""
        try:
            if getattr(self, "hit", None) is None:
                return
            if not self.win.isVisible() or bool(getattr(self, "_interactive", False)):
                self.hit.hide()
                return
            g = self.win.geometry()
            scr = self._avail_rect()
            # 先贴在字幕条右外侧；右边没位置（窗口已贴屏幕右缘）再退回内侧
            x = g.right() + 4
            if x + 82 > scr.right():
                x = g.right() - 82
            y = max(scr.top() + 2, g.top() + 6)
            self.hit.move(x, y)
            self.hit.show()
            self.hit.raise_()
        except Exception as e:
            log("抓手同步失败：" + repr(e))

    def _apply_skin(self):
        """把皮肤的副行颜色、对齐方式应用到当前句（主体颜色由说话人色/皮肤主色决定）。"""
        _k, sk = skin_of(self.cfg)
        al = self.Qt.AlignHCenter if sk.get("align") == "center" else self.Qt.AlignLeft
        light = sum(sk["bg"]) / 3.0 > 150 if sk.get("bg") else False
        try:
            self.src_label.setStyleSheet("color:%s;" % sk["sub"])
            self.src_label.setAlignment(al)
            self.dst_label.setAlignment(al)
            self.status.setStyleSheet("color:%s;" % sk["sub"])
            self.dot.setStyleSheet("color:%s;font-size:9px;" % sk["sub"])
            # 复选框 / 音色标签跟着皮肤明暗走，避免浅底上浅字、深底上深字
            if hasattr(self, "chk_auto"):
                self.chk_auto.setStyleSheet(
                    "QCheckBox{color:%s;}" % ("#2A2A30" if light else "#E8E4DB"))
            if hasattr(self, "tts_voice"):
                self.tts_voice.setStyleSheet(
                    "color:%s;" % ("#5A564F" if light else "#B6B1A8"))
        except Exception:
            pass

    def _has_text(self):
        """当前句有没有内容：有就展开成对话框高度，没有就收成状态栏。"""
        try:
            return bool((self.src_label.text() or "").strip()
                        or (self.dst_label.toPlainText() or "").strip())
        except Exception:
            return True

    def _fit_height(self):
        """把卡片量准；对话框模式下窗口高度恒定，不再随句子长短上下跳。"""
        try:
            inner = max(220, self.win.width() - self.MARGIN * 2 - 3 - 35)
            # QLabel 自动换行的 sizeHint / heightForWidth 都会高估且滞后，按字体自己量行高
            for lab in (self.src_label, self.dst_label):
                if not lab.isVisibleTo(self.win):
                    continue
                lab.setFixedHeight(self._text_height(lab, inner) if lab.text() else 0)
            if self.hist_box.isVisibleTo(self.win):
                total = 0
                for w in getattr(self, "_hist_widgets", []):
                    v = w.layout()
                    if v is not None:
                        for i in range(v.count()):
                            lab = v.itemAt(i).widget()
                            if lab is not None and lab.isVisibleTo(self.hist_box):
                                lab.setFixedHeight(self._text_height(lab, inner))
                        v.invalidate()
                        v.activate()
                        w.setFixedHeight(v.sizeHint().height())
                        total += w.height()
                if total:
                    total += self._hist_lay.spacing() * max(0, len(self._hist_widgets) - 1)
                # 对话框高度是固定的：历史区最多占一半，剩下的留给当前句，免得把正文挤没
                if str(self.cfg.get("overlay_style") or "dialog") == "dialog":
                    total = min(total, int(self._dialog_h() * 0.42))
                self.hist_box.setFixedHeight(max(0, total))
            self.panel.layout().invalidate()
            self.panel.layout().activate()
            h = self.panel.sizeHint().height() + self.MARGIN * 2
            # A 案（Galgame 对话框）：高度恒定、底边死锁。内容超长只在卡片内裁，
            # 窗口尺寸和位置都不动，就不会出现“字幕条自己往上凸起”。
            if str(self.cfg.get("overlay_style") or "dialog") == "dialog":
                if self._has_text() or self.hist_box.isVisibleTo(self.win):
                    h = self._dialog_h()
                else:
                    # 空闲态（静音 / 刚启动还没有句子）收成一条窄框，不白占一块画面
                    h = 92
            else:
                h = max(118, min(420, h))
            if abs(self.win.height() - h) > 3:
                old_bottom = self.win.y() + self.win.height()
                self.win.layout().invalidate()
                self.win.setMinimumSize(0, 0)     # 布局会把窗口最小高度顶到旧值，先清掉
                self.win.resize(self.win.width(), h)
                # 底部预设要“往上长”，否则变高会顶出屏幕；自由位置也保底边，当前句不跟着上下跳
                preset = str(self.cfg.get("overlay_pos") or "bottom")
                if preset == "top":
                    self.win.move(self.win.x(), self.win.y())
                elif preset in ("bottom", "bottom_high"):
                    scr = self._avail_rect()
                    gap = 72 if preset == "bottom" else 200
                    self.win.move(self.win.x(), scr.bottom() - self.win.height() - gap)
                else:
                    # 自由位置：靠近屏幕上半部时改为“顶部固定”，否则长高会一路往上顶出屏幕；
                    # 靠近下半部仍按底边固定，当前句不会被历史行推着上下跳。
                    scr = self._avail_rect()
                    if old_bottom - self.win.height() < scr.top() or self.win.y() <= scr.center().y():
                        self.win.move(self.win.x(), max(scr.top(), self.win.y()))
                    else:
                        self.win.move(self.win.x(), old_bottom - self.win.height())
                self._clamp_to_screen()
        except Exception:
            pass
        # 窗口高度/布局刚变过，整块重绘一次，避免旧行文字留在透明区域的残影
        try:
            self.panel.repaint(self.panel.rect())
            self.win.repaint(self.win.rect())
        except Exception:
            pass
        self._schedule_full_repaint()
        self._sync_handle()

    def _glass(self, value=None):
        """只有卡片底色带透明度；文字、按钮字、色条全都保持不透明，拖到多淡文字都清晰。
        底色 / 描边 / 圆角由 overlay_skin 决定：换皮肤只换外观，不动任何流水线逻辑。"""
        try:
            a = float(self.cfg.get("opacity") if value is None else value)
        except Exception:
            a = 0.9
        a = max(0.15, min(1.0, a))
        bg = int(round(a * 255))
        line = int(round(min(1.0, a + 0.1) * 255))          # 描边比底色稍微实一点，淡的时候还看得出边界
        _key, skin = skin_of(self.cfg)
        edge_alpha = skin.get("edge_alpha")
        if edge_alpha is None:
            edge_alpha = line
        if getattr(self, "_interactive", False):
            # 交互模式：整圈描边换成低饱和的淡绿，提示"这层现在能点"
            edge = "border:1px solid rgba(150,196,170,120);"
        elif skin.get("edge") is None or edge_alpha <= 0:
            edge = "border:none;"
        else:
            e = skin["edge"]
            edge = "border:1px solid rgba(%d,%d,%d,%d);" % (
                e[0], e[1], e[2], min(255, int(edge_alpha)))
        if skin.get("bg") is None:
            background = "background:transparent;"
        elif skin.get("grad"):
            # 歌词 / 电影字幕：上下淡出的软底，浅色壁纸上字也读得清，但不至于压成一块黑板
            b = skin["bg"]
            # 软底只垫到“能读清”的程度：太黑就成了黑板，压住游戏画面
            a1 = min(bg, 150)
            a0 = int(a1 * 0.4)
            background = ("background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
                          "stop:0 rgba(%d,%d,%d,%d),stop:0.42 rgba(%d,%d,%d,%d),"
                          "stop:0.58 rgba(%d,%d,%d,%d),stop:1 rgba(%d,%d,%d,%d));"
                          % (b[0], b[1], b[2], a0, b[0], b[1], b[2], a1,
                             b[0], b[1], b[2], a1, b[0], b[1], b[2], a0))
        else:
            b = skin["bg"]
            background = "background:rgba(%d,%d,%d,%d);" % (b[0], b[1], b[2], bg)
        # 浅色皮肤上按钮也得换成深字浅底，否则白底白字直接看不见；
        # 无框 / 软底皮肤（歌词、电影）垫在浅色壁纸上时，按钮也要有实体底才看得清。
        light = sum(skin["bg"]) / 3.0 > 150 if skin.get("bg") else False
        if light:
            btn = ("QPushButton{background:rgba(255,255,255,215);border:1px solid rgba(30,30,36,70);"
                   "border-radius:9px;color:#26262C;font-size:11px;padding:3px 12px;}"
                   "QPushButton:hover{background:rgba(255,255,255,240);color:#111114;}"
                   "QPushButton:pressed{background:rgba(232,229,222,240);}"
                   "QPushButton#fav{background:rgba(46,128,90,225);border-color:rgba(46,128,90,225);color:#FFFFFF;}"
                   "QPushButton#fav:hover{background:rgba(58,150,106,235);color:#FFFFFF;}")
        else:
            btn = ("QPushButton{background:rgba(24,26,31,210);border:1px solid rgba(255,255,255,70);"
                   "border-radius:9px;color:#F4F1EA;font-size:11px;padding:3px 12px;}"
                   "QPushButton:hover{background:rgba(58,64,74,225);color:#FFFFFF;border-color:rgba(255,255,255,110);}"
                   "QPushButton:pressed{background:rgba(78,84,94,235);}"
                   "QPushButton#fav{background:rgba(46,128,90,220);border-color:rgba(123,195,155,120);color:#FFFFFF;}"
                   "QPushButton#fav:hover{background:rgba(58,150,106,235);color:#FFFFFF;}")
        self.panel.setStyleSheet(
            "#panel{%s%sborder-radius:%dpx;}"
            "QLabel{background:transparent;}"
            "%s"
            "QPushButton#quit:hover{background:rgba(194,112,61,58);color:#F2BA95;"
            "border-color:rgba(242,186,149,96);}"
            % (background, edge, int(skin.get("radius") or 14), btn)
        )

    def preview_opacity(self, value):
        """设置面板拖动时直接生效，不用等保存。"""
        self._glass(value)

    def update_look(self):
        self._glass()
        self._apply_skin()
        self.src_label.setVisible(bool(self.cfg.get("show_source"))
                                  and not bool(self.cfg.get("overlay_bilingual", False)))
        self.dst_label.setFont(self._font(int(self.cfg.get("font_size") or 22), True, lang_font(CURRENT_LANGS.get("tl", "en"), self.cfg) or "Segoe UI", 0.2))
        try:
            # 文字方向按目标语种走：阿拉伯语等 RTL 语种自动从右到左排版
            from PySide6.QtCore import Qt as _Qt
            rtl = bool(lang_meta(CURRENT_LANGS.get("tl", "en")).get("rtl")) \
                or bool(self.cfg.get("rtl_overlay", False))
            self.dst_label.setLayoutDirection(
                _Qt.RightToLeft if rtl else _Qt.LeftToRight)
        except Exception:
            pass
        # 切换对照模式时，当前这句立刻按新排版重画
        if self._cur_en:
            if self.cfg.get("overlay_bilingual", False) and self._cur_zh:
                self.dst_label.setText("%s  ｜  %s" % (self._cur_zh, self._cur_en))
                self.dst_label.setFont(self._font(
                    max(13, int((self.cfg.get("font_size") or 22) * 0.72)), True, "Segoe UI"))
            else:
                self.dst_label.setText(self._cur_en)
        self._render_history()          # 缓冲区按新字号重排
        if not self.cfg.get("diarize", False):
            self.spk_label.setVisible(False)
        self.set_speaker(self._cur_spk)
        if self.dst_label.text():                              # 改完倍率，当前这句也按新值重算
            self._shown_at = time.monotonic()
            self._hold_ms = self._hold_for(self.dst_label.text())
        self._fit_height()
        # 调样式不等于有新句子：安静状态下的上一句保持收着，别一改设置又把高度撑回去
        try:
            if hasattr(self, "_hist_idle") and not self._hist_idle.isActive():
                self._idle_hide_history()
        except Exception:
            pass

    def stop(self):
        try:
            geo = self.win.geometry()
            self.cfg["window"] = {"x": geo.x(), "y": geo.y(), "w": geo.width(), "h": geo.height()}
        except Exception:
            pass
        try:
            self.win.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 设置窗口
def build_settings(owner):
    from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                                   QComboBox, QSpinBox, QDoubleSpinBox, QCheckBox,
                                   QRadioButton, QButtonGroup, QPushButton, QFrame, QGridLayout,
                                   QAbstractItemView, QWidget)
    from PySide6.QtCore import Qt

    cfg = owner.cfg
    dlg = QDialog()
    dlg.setWindowTitle("Oversub 设置")
    dlg.setModal(True)
    dlg.resize(560, 620)
    theme = str(cfg.get("theme") or "dark")
    dlg.setStyleSheet(theme_qss(theme) + "QDialog{background:%s;}" % THEME_COLORS.get(theme, THEME_COLORS["dark"])["bg"])
    root = QVBoxLayout(dlg)
    root.setContentsMargins(20, 16, 20, 16)
    root.setSpacing(8)

    def head(text):
        lb = QLabel(text)
        lb.setStyleSheet("color:#8C8781;font-size:11px;letter-spacing:1px;margin-top:8px;")
        root.addWidget(lb)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color:#E6E4E1;")
        root.addWidget(line)

    def _preview_font(px):
        """字号也实时预览，跟透明度一样不用等保存。"""
        try:
            owner.overlay.cfg["font_size"] = int(px)
            owner.overlay.dst_label.setFont(owner.overlay._font(px, True, "Segoe UI", 0.2))
            owner.overlay._render_history()
            owner.overlay._fit_height()
        except Exception:
            pass

    head("翻译引擎")
    row = QHBoxLayout()
    rb_local = QRadioButton("本地离线（免费，翻得生硬些）")
    rb_online = QRadioButton("在线模型（需 key，接近人话）")
    grp = QButtonGroup(dlg)
    grp.addButton(rb_local)
    grp.addButton(rb_online)
    (rb_local if cfg.get("engine") != "online" else rb_online).setChecked(True)
    row.addWidget(rb_local)
    row.addWidget(rb_online)
    row.addStretch(1)
    root.addLayout(row)

    grid = QGridLayout()
    grid.setColumnStretch(1, 1)
    edits = {}
    for i, (key, label, ph) in enumerate((
            ("base_url", "接口地址", "https://api.deepseek.com/v1"),
            ("model", "模型名", "deepseek-chat"),
            ("api_key", "API Key", "sk-…"))):
        lb = QLabel(label)
        e = QLineEdit(str(cfg.get(key) or ""))
        e.setPlaceholderText(ph)
        grid.addWidget(lb, i, 0)
        grid.addWidget(e, i, 1)
        edits[key] = e
    root.addLayout(grid)

    head("语种")
    row = QHBoxLayout()
    row.addWidget(QLabel("源语言"))
    src_box = QComboBox()
    for code, meta in SOURCE_LANGS.items():
        src_box.addItem("%s %s" % (meta.get("name"), meta.get("native")))
    src_box.setEnabled(False)
    src_box.setToolTip("源语言不用手动选：说中文按中文转写，说英文按英文转写，由每句话自动判定")
    row.addWidget(src_box, 1)
    row.addSpacing(12)
    row.addWidget(QLabel("目标语言"))
    tgt_box = QComboBox()
    tgt_codes = []
    for code, meta in LANG_REGISTRY.items():
        # 只列真正可用的语种：没有离线包的语种不进下拉，避免选了一个用不了的选项
        if not meta.get("installed"):
            continue
        tgt_codes.append(code)
        tgt_box.addItem("%s %s" % (meta.get("name"), meta.get("native")))
    cur_tl = str(cfg.get("target_lang") or "en")
    if cur_tl in tgt_codes:
        tgt_box.setCurrentIndex(tgt_codes.index(cur_tl))
    row.addWidget(tgt_box, 1)
    root.addLayout(row)
    lang_status = QLabel("")
    lang_status.setStyleSheet("color:#8C8781;font-size:11px;")
    lang_status.setWordWrap(True)
    root.addWidget(lang_status)

    state = {"tl": cur_tl if lang_available(cur_tl) else "en"}

    def _lang_desc(code):
        m = lang_meta(code)
        return "%s：识别通用 Whisper 模型 ｜ 翻译本地 Argos 包 ｜ 朗读 Windows 系统语音 ｜ %s" % (
            m.get("native", code),
            "RTL 从右到左" if m.get("rtl") else "LTR 从左到右")

    def _update_lang_status(*_a):
        code = tgt_codes[tgt_box.currentIndex()]
        meta = lang_meta(code)
        lang_status.setText("当前目标语种：%s。%s" % (meta.get("name"), _lang_desc(code)))
    _update_lang_status()

    def _on_lang_pick(idx):
        from PySide6.QtWidgets import QMessageBox
        code = tgt_codes[idx]
        if lang_available(code):
            state["tl"] = code
            _update_lang_status()
            return
        # 真话：这个语种当前没有可用的离线翻译包，不是“点一下就会下载”
        QMessageBox.information(
            root.parentWidget() or None, "该语种暂不可用",
            "「%s」当前版本没有内置离线翻译包，只有 中文 ↔ 英语 可用。\n"
            "识别模型本身支持多语种，缺的是对应语种的翻译包。"
            % lang_meta(code).get("name"))
        tgt_box.blockSignals(True)
        tgt_box.setCurrentIndex(tgt_codes.index(state["tl"]))
        tgt_box.blockSignals(False)
        _update_lang_status()
    tgt_box.currentIndexChanged.connect(_on_lang_pick)

    # 翻译模型与语种配套：以后加语言只需在这里选目标语种
    row = QHBoxLayout()
    row.addWidget(QLabel("翻译模型"))
    mt_box = QComboBox()
    mt_box.addItem("small（轻量，速度快）", "small")
    mt_box.addItem("base（更准，稍慢）", "base")
    cur_mt = str(cfg.get("mt_size") or "small")
    for i in range(mt_box.count()):
        if mt_box.itemData(i) == cur_mt:
            mt_box.setCurrentIndex(i)
    row.addWidget(mt_box, 1)
    row.addSpacing(12)
    chk_rtl = QCheckBox("RTL 从右到左渲染（阿拉伯语 / 希伯来语预留）")
    chk_rtl.setChecked(bool(cfg.get("rtl_overlay", False)))
    row.addWidget(chk_rtl, 1)
    root.addLayout(row)

    row = QHBoxLayout()
    row.addWidget(QLabel("全局兜底字体"))
    font_edit = QLineEdit(str(cfg.get("font_family") or ""))
    font_edit.setPlaceholderText("留空 = 各语种用推荐字体（如 Yu Gothic UI / Malgun Gothic）")
    row.addWidget(font_edit, 1)
    root.addLayout(row)

    # 语种管理面板：列出全部语种，状态 / 字体 / 下载与删除入口
    from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QHeaderView
    # ---------------- 本地翻译包：真实状态 + 真下载 / 真卸载 ----------------
    # 以前这里是一张 12 行的假表（全标「未下载」），按钮只弹一句“下载入口（预留）”。
    # 现在只列真实安装过的东西，按钮也走 Argos 的真实接口：缺包就下，有包就能卸。
    pkg_hint = QLabel(
        "识别：一个多语种 Whisper 模型就够，不需要按语种单独下载。\n"
        "翻译：中文 ↔ 英语 用本地 Argos 离线包，下表是真实安装状态。\n"
        "朗读：走 Windows 自带语音（SAPI），音色随系统语言包，软件里不下载音色。")
    pkg_hint.setStyleSheet("color:#8C8781;font-size:11px;")
    pkg_hint.setWordWrap(True)
    root.addWidget(pkg_hint)

    pkg_table = QTableWidget(0, 3)
    pkg_table.setHorizontalHeaderLabels(["语言对", "包版本", "状态"])
    pkg_table.verticalHeader().setVisible(False)
    pkg_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    pkg_table.setSelectionBehavior(QAbstractItemView.SelectRows)
    pkg_table.setColumnWidth(0, 150)
    pkg_table.setColumnWidth(1, 90)
    pkg_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
    pkg_table.setMinimumHeight(120)
    pkg_table.setMaximumHeight(170)
    root.addWidget(pkg_table)

    pkg_state = QLabel("")
    pkg_state.setStyleSheet("color:#8C8781;font-size:11px;")
    pkg_state.setWordWrap(True)
    root.addWidget(pkg_state)

    row = QHBoxLayout()
    btn_pkg_fill = QPushButton("检查并补装翻译包")
    btn_pkg_del = QPushButton("卸载选中的包")
    for b in (btn_pkg_fill, btn_pkg_del):
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedHeight(28)
        row.addWidget(b)
    row.addStretch(1)
    root.addLayout(row)

    _pkg_busy = {"on": False}

    def _installed_pairs():
        try:
            import argostranslate.package as AP
            return [(p.from_code, p.to_code, str(getattr(p, "package_version", "")))
                    for p in AP.get_installed_packages()]
        except Exception as e:
            log("读取本地翻译包失败：" + repr(e))
            return []

    def _refresh_pkg_table():
        rows = _installed_pairs()
        pkg_table.setRowCount(len(rows))
        for i, (a, b, v) in enumerate(rows):
            for j, val in enumerate(("%s → %s" % (a, b), v, "已安装")):
                it = QTableWidgetItem(val)
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                pkg_table.setItem(i, j, it)
        if not rows:
            pkg_state.setText("本机还没有任何翻译包，点「检查并补装翻译包」会真的去 Argos 源下载。")

    _refresh_pkg_table()

    def _need_pairs():
        sl = str(cfg.get("source_lang") or "zh")
        tl = str(cfg.get("target_lang") or "en")
        pairs = [(sl, tl)]
        if (tl, sl) not in pairs:
            pairs.append((tl, sl))
        return pairs

    def _fill_packages():
        if _pkg_busy["on"]:
            return
        from PySide6.QtWidgets import QApplication
        pairs = _need_pairs()
        have = [(a, b) for a, b, _v in _installed_pairs()]
        missing = [p for p in pairs if p not in have]
        if not missing:
            pkg_state.setText("当前语种对的翻译包都已安装：%s。如需重装，先卸载再补装。"
                              % "、".join("%s → %s" % p for p in pairs))
            return
        _pkg_busy["on"] = True
        btn_pkg_fill.setEnabled(False)
        pkg_state.setText("正在从 Argos 源下载 %s（约 100MB 级，取决于网络，过程不可中断）……"
                          % "、".join("%s → %s" % p for p in missing))
        QApplication.processEvents()

        def _worker():
            res = {"ok": [], "err": "", "done": False}
            try:
                import argostranslate.package as AP
                AP.update_package_index()
                for a, b in missing:
                    AP.install_package_for_language_pair(a, b)
                    res["ok"].append((a, b))
            except Exception as e:
                res["err"] = "%r" % e
            res["done"] = True
            holder["res"] = res

        import threading
        holder = {}
        threading.Thread(target=_worker, daemon=True).start()

        from PySide6.QtCore import QTimer

        def _poll():
            res = holder.get("res")
            if res is None or not res.get("done"):
                QTimer.singleShot(400, _poll)
                return
            _after_pkg_fill([res.get("ok") or [], res.get("err") or ""])

        QTimer.singleShot(400, _poll)
        return

    def _after_pkg_fill(payload):
        ok, err = (payload[0], payload[1]) if payload else ([], "")
        _pkg_busy["on"] = False
        btn_pkg_fill.setEnabled(True)
        _refresh_pkg_table()
        if ok:
            pkg_state.setText("已安装：%s。可以回到顶部切换语种了。"
                              % "、".join("%s → %s" % p for p in ok))
            log("翻译包下载完成：%s" % ok)
        elif err:
            pkg_state.setText("下载失败：%s（网络不通或被墙时，只能手动放离线包）" % err)
            log("翻译包下载失败：%s" % err)
        else:
            pkg_state.setText("没有可下载的语言对。")

    def _uninstall_selected():
        from PySide6.QtWidgets import QMessageBox
        i = pkg_table.currentRow()
        rows = _installed_pairs()
        if not (0 <= i < len(rows)):
            QMessageBox.information(root.parentWidget() or None, "翻译包", "先在表里选一行。")
            return
        a, b, _v = rows[i]
        ans = QMessageBox.question(
            root.parentWidget() or None, "卸载翻译包",
            "确定卸载 %s → %s 的本地翻译包？卸载后该方向将无法翻译，可以再点补装重新下载。"
            % (a, b))
        if ans != QMessageBox.Yes:
            return
        try:
            import argostranslate.package as AP
            for p in AP.get_installed_packages():
                if (p.from_code, p.to_code) == (a, b):
                    AP.uninstall(p)
                    break
            log("已卸载翻译包：%s → %s" % (a, b))
            _refresh_pkg_table()
            pkg_state.setText("已卸载 %s → %s。" % (a, b))
        except Exception as e:
            pkg_state.setText("卸载失败：%r" % e)
            log("卸载翻译包失败：%r" % e)

    btn_pkg_fill.clicked.connect(_fill_packages)
    btn_pkg_del.clicked.connect(_uninstall_selected)

    head("语音识别")
    row = QHBoxLayout()
    row.addWidget(QLabel("识别模型（ASR）"))
    model_box = QComboBox()
    model_box.addItem("自动（有显卡 medium，无显卡 small）", "auto")
    model_box.addItem("中文 - base（最快，精度最低）", "base")
    model_box.addItem("中文 - small（速度优先）", "small")
    model_box.addItem("中文 - medium（高精度，显卡推荐）", "medium")
    model_box.addItem("中文 - turbo（速度优先，长句稍弱）", "turbo")
    cur_model = str(cfg.get("whisper_model") or "auto")
    for i in range(model_box.count()):
        if model_box.itemData(i) == cur_model:
            model_box.setCurrentIndex(i)
    model_box.setToolTip("当前 ASR 固定抓中文；以后放开源语言时，这里跟着语种注册表走")
    row.addWidget(model_box, 1)
    row.addSpacing(12)
    row.addWidget(QLabel("翻译目标语种"))
    cur_tl_label = QLabel(lang_name(cfg.get("target_lang") or "en"))
    cur_tl_label.setStyleSheet("font-weight:600;color:#10B981;")
    row.addWidget(cur_tl_label)
    row.addStretch(1)
    root.addLayout(row)
    row = QHBoxLayout()
    row.addWidget(QLabel("精度"))
    asr_box = QComboBox()
    asr_box.addItems(["准确优先（beam5 + 静音过滤，稍慢）", "速度优先（原来那样，最快）"])
    asr_box.setCurrentIndex(0 if str(cfg.get("asr_quality") or "accurate") == "accurate" else 1)
    row.addWidget(asr_box, 1)
    root.addLayout(row)
    row = QHBoxLayout()
    row.addWidget(QLabel("响应速度"))
    lat_box = QComboBox()
    lat_box.addItem("快速（语音最长 3.2 秒，0.2 秒断句）", "fast")
    lat_box.addItem("均衡（语音最长 4.2 秒，0.4 秒断句，默认）", "balanced")
    lat_box.addItem("准确（语音最长 5.5 秒，0.4 秒断句）", "accurate")
    cur_lat = str(cfg.get("latency_mode") or "balanced")
    for i in range(lat_box.count()):
        if lat_box.itemData(i) == cur_lat:
            lat_box.setCurrentIndex(i)
    lat_box.setToolTip("越短越跟手，但一句话可能被切在两段；连麦短句多用快速")
    row.addWidget(lat_box, 1)
    root.addLayout(row)
    row = QHBoxLayout()
    row.addWidget(QLabel("自定义热词"))
    hot_box = QLineEdit(str(cfg.get("hotwords_custom") or ""))
    hot_box.setPlaceholderText("常被听错的词，空格分隔，例如：萝莉 御姐 抽卡 老陈")
    hot_box.setToolTip("只在「热词模式」不是关闭时生效，只在本机处理，不上传")
    row.addWidget(hot_box, 1)
    root.addLayout(row)
    row = QHBoxLayout()
    row.addWidget(QLabel("热词模式"))
    hw_box = QComboBox()
    hw_box.addItem("只用手填的词（推荐）", "custom")
    hw_box.addItem("只用内置词表 + 手填", "full")
    hw_box.addItem("关闭热词", "off")
    cur_hw = str(cfg.get("asr_hotwords_mode") or "custom")
    for i in range(hw_box.count()):
        if hw_box.itemData(i) == cur_hw:
            hw_box.setCurrentIndex(i)
    hw_box.setToolTip("热词会当成提示词喂给模型：静音或听不清时，模型可能照着词表“念”出你没说的设定词。"
                      "默认只用手填的词；出现“没说话却打出设定词”就切到关闭")
    row.addWidget(hw_box, 1)
    root.addLayout(row)
    def _preview_hw():
        cfg["asr_hotwords_mode"] = str(hw_box.currentData() or "custom")
        save_config(cfg)
    hw_box.currentIndexChanged.connect(lambda _i: _preview_hw())
    asr_hint = QLabel("想让识别更准：先选「准确优先」；口音重就把模型换成 medium 或 turbo（首次使用会自动下载约 1.5G，CPU 上更慢）。")
    asr_hint.setStyleSheet("color:#8C8781;font-size:11px;")
    asr_hint.setWordWrap(True)
    root.addWidget(asr_hint)

    # 离线模型管理：尺寸特点、缓存状态、按需下载；翻译模型的状态也在这里看
    mm = QHBoxLayout()
    model_desc = QLabel("")
    model_desc.setStyleSheet("color:#5E5A52;font-size:11px;")
    model_desc.setWordWrap(True)
    mm.addWidget(model_desc, 1)

    def _cache_text(name):
        hit = asr_model_cached(name)
        return "%s：%s（约 %dMB）· %s" % (
            name, ASR_SIZES.get(name, {}).get("desc", ""), ASR_SIZES.get(name, {}).get("mb", 0),
            "已下载" if hit else "未下载")

    def _update_model_desc(*_a):
        mid = (model_box.currentData() or "auto")
        if mid == "auto":
            mid = default_asr_model()
        model_desc.setText(_cache_text(mid))
    model_box.currentIndexChanged.connect(_update_model_desc)
    _update_model_desc()

    btn_dl = QPushButton("下载这个模型")
    btn_dl.setCursor(Qt.PointingHandCursor)
    btn_dl.setFixedHeight(26)
    mm.addWidget(btn_dl)
    root.addLayout(mm)

    tr_status = QLabel("离线翻译（Argos %s）：%s" % (
        pair_label(CURRENT_LANGS.get("sl", "zh"), CURRENT_LANGS.get("tl", "en")),
        "已安装" if argos_installed() else "首次使用会自动准备"))
    tr_status.setStyleSheet("color:#5E5A52;font-size:11px;")
    root.addWidget(tr_status)

    dl_state = QLabel("")
    dl_state.setStyleSheet("color:#5E5A52;font-size:11px;")
    root.addWidget(dl_state)

    def _download_model():
        name = (model_box.currentData() or "auto")
        if name == "auto":
            name = default_asr_model()
        if asr_model_cached(name):
            dl_state.setText("%s 已经下载好了，切换后点「保存并应用」即可。" % name)
            return
        dl_state.setText("正在后台下载 %s（约 %dMB），完成后会写进运行日志。" % (
            name, ASR_SIZES.get(name, {}).get("mb", 0)))

        def work():
            try:
                from faster_whisper import WhisperModel
                WhisperModel(name, device="cpu", compute_type="int8")
                log("ASR 模型已下载：%s" % name)
            except Exception as e:
                log("ASR 模型下载失败：%s｜%r" % (name, e))

        threading.Thread(target=work, daemon=True).start()

    btn_dl.clicked.connect(_download_model)

    row = QHBoxLayout()
    chk_short = QCheckBox("过滤过短识别（单字 / 语气词不记录、不推字幕）")
    chk_short.setChecked(bool(cfg.get("filter_short", True)))
    row.addWidget(chk_short)
    row.addSpacing(12)
    row.addWidget(QLabel("最短"))
    min_chars = QSpinBox()
    min_chars.setRange(1, 8)
    min_chars.setSuffix(" 字")
    min_chars.setValue(int(cfg.get("min_chars") or 2))
    row.addWidget(min_chars)
    row.addSpacing(10)
    row.addWidget(QLabel("按词语言"))
    min_words = QSpinBox()
    min_words.setRange(1, 10)
    min_words.setSuffix(" 词")
    min_words.setValue(int(cfg.get("min_words") or 2))
    min_words.setToolTip("英/法/德等空格分词语种按单词计数，中日按字符计数，阈值各自独立")
    row.addWidget(min_words)
    row.addStretch(1)
    root.addLayout(row)
    short_hint = QLabel("「啊」「哦」「Yeah.」这类碎片会被丢掉，表格和字幕都不再被塞满；想全收就把勾去掉。")
    short_hint.setStyleSheet("color:#8C8781;font-size:11px;")
    short_hint.setWordWrap(True)
    root.addWidget(short_hint)

    row = QHBoxLayout()
    row.addWidget(QLabel("回放设备"))
    dev_box = QComboBox()
    devices = owner.list_loopback()
    dev_box.addItems(devices)
    cur = cfg.get("loopback_device") or (devices[0] if devices else "")
    if cur:
        dev_box.setCurrentText(cur)
    row.addWidget(dev_box, 1)
    root.addLayout(row)
    hint = QLabel("默认跟系统扬声器走；用耳机或虚拟声卡时在这里指定。")
    hint.setStyleSheet("color:#8C8781;font-size:11px;")
    root.addWidget(hint)

    head("显示与断句")
    row = QHBoxLayout()
    row.addWidget(QLabel("字号"))
    size = QSpinBox()
    size.setRange(13, 44)
    size.setValue(int(cfg.get("font_size") or 22))
    row.addWidget(size)
    row.addSpacing(18)
    row.addWidget(QLabel("背景不透明度"))
    alpha = QDoubleSpinBox()
    alpha.setRange(0.15, 1.0)
    alpha.setSingleStep(0.02)
    alpha.setValue(float(cfg.get("opacity") or 0.9))
    # 拖动/改数就立刻作用到字幕条上，不用等保存；文字始终不透明
    alpha.valueChanged.connect(lambda v: owner.overlay.preview_opacity(float(v)))
    size.valueChanged.connect(lambda v: _preview_font(int(v)))
    row.addWidget(alpha)
    row.addStretch(1)
    root.addLayout(row)

    row2 = QHBoxLayout()
    row2.addWidget(QLabel("字幕条上一句"))
    prev_n = QSpinBox()
    prev_n.setRange(0, 3)
    prev_n.setValue(int(cfg.get("overlay_prev_rows", 2)))
    prev_n.setToolTip("字幕条里除当前句外还留几句；0 = 只显示当前句，不挡视野")
    row2.addWidget(prev_n)
    row2.addWidget(QLabel("句（0 只留当前句，最多 3 句）"))
    row2.addStretch(1)
    root.addLayout(row2)

    def _preview_prev(v):
        try:
            owner.overlay.cfg["overlay_prev_rows"] = int(v)
            owner.overlay._render_history()
            owner.overlay._fit_height()
        except Exception:
            pass
    prev_n.valueChanged.connect(lambda v: _preview_prev(int(v)))

    row3 = QHBoxLayout()
    row3.addWidget(QLabel("字幕位置"))
    pos_box = QComboBox()
    for label, key in (("底部居中（默认）", "bottom"),
                       ("底部偏上", "bottom_high"),
                       ("顶部居中", "top"),
                       ("自由位置（拖动后记住）", "free")):
        pos_box.addItem(label, key)
    try:
        idx = max(0, [pos_box.itemData(i) for i in range(pos_box.count())]
                  .index(str(cfg.get("overlay_pos") or "bottom")))
        pos_box.setCurrentIndex(idx)
    except Exception:
        pos_box.setCurrentIndex(0)
    pos_box.setMinimumWidth(190)
    row3.addWidget(pos_box)
    btn_reset_pos = QPushButton("复位到屏幕顶部")
    btn_reset_pos.setObjectName("ghost")
    btn_reset_pos.setCursor(Qt.PointingHandCursor)
    btn_reset_pos.setToolTip("字幕条拖出屏幕、或换了显示器后一键找回")
    row3.addWidget(btn_reset_pos)
    row3.addStretch(1)
    root.addLayout(row3)

    def _reset_pos():
        try:
            owner.overlay.cfg["overlay_pos"] = "top"
            owner.overlay.cfg.pop("window", None)
            cfg["overlay_pos"] = "top"
            cfg.pop("window", None)
            owner.overlay.apply_position(save=True)
            owner.overlay._fit_height()
            try:
                pos_box.setCurrentIndex(2)
            except Exception:
                pass
            save_config(cfg)
        except Exception as e:
            log("复位字幕条位置失败：" + repr(e))
    btn_reset_pos.clicked.connect(_reset_pos)

    def _preview_pos():
        try:
            owner.overlay.cfg["overlay_pos"] = str(pos_box.currentData() or "bottom")
            owner.overlay.apply_position()
        except Exception:
            pass
    pos_box.currentIndexChanged.connect(lambda _i: _preview_pos())

    row3b = QHBoxLayout()
    row3b.addWidget(QLabel("字幕形态"))
    sty_box = QComboBox()
    sty_box.addItem("对话框（高度固定，推荐）", "dialog")
    sty_box.addItem("字幕条（高度随内容伸缩）", "banner")
    sty_box.setCurrentIndex(
        1 if str(cfg.get("overlay_style") or "dialog") == "banner" else 0)
    sty_box.setMinimumWidth(190)
    row3b.addWidget(sty_box)
    row3b.addStretch(1)
    root.addLayout(row3b)

    def _preview_style():
        try:
            owner.overlay.cfg["overlay_style"] = str(sty_box.currentData() or "dialog")
            owner.overlay.apply_position()
            owner.overlay._fit_height()
        except Exception:
            pass
    sty_box.currentIndexChanged.connect(lambda _i: _preview_style())

    row3c = QHBoxLayout()
    row3c.addWidget(QLabel("顶部留白"))
    gap_spin = QSpinBox()
    gap_spin.setRange(0, 200)
    gap_spin.setSingleStep(4)
    gap_spin.setSuffix(" px")
    try:
        gap_spin.setValue(int(cfg.get("overlay_gap_top") or 8))
    except Exception:
        gap_spin.setValue(8)
    gap_spin.setToolTip("顶部锚定时离屏幕上沿的距离；游戏顶部有血条 / 任务条时往下压 30–60px")
    row3c.addWidget(gap_spin)
    tail_hint = QLabel("只影响顶部锚定，拖动实时生效")
    tail_hint.setStyleSheet("color:#8C8781;font-size:11px;")
    row3c.addWidget(tail_hint)
    row3c.addStretch(1)
    root.addLayout(row3c)

    def _preview_gap(v):
        try:
            cfg["overlay_gap_top"] = int(v)
            owner.overlay.cfg["overlay_gap_top"] = int(v)
            owner.overlay.apply_position(save=True)
        except Exception:
            pass
    gap_spin.valueChanged.connect(lambda v: _preview_gap(int(v)))

    row3d = QHBoxLayout()
    row3d.addWidget(QLabel("字幕皮肤"))
    skin_box = QComboBox()
    for _k, _v in OVERLAY_SKINS.items():
        skin_box.addItem(_v.get("name") or _k, _k)
    _cur_sk = str(cfg.get("overlay_skin") or "dialog_dark")
    for i in range(skin_box.count()):
        if skin_box.itemData(i) == _cur_sk:
            skin_box.setCurrentIndex(i)
    skin_box.setToolTip("只换外观：底色 / 描边 / 圆角 / 文字颜色与对齐；"
                        "采集、翻译、历史行数这些逻辑都不受影响")
    row3d.addWidget(skin_box, 1)
    root.addLayout(row3d)

    def _preview_skin():
        k = str(skin_box.currentData() or "dialog_dark")
        cfg["overlay_skin"] = k
        try:
            owner.overlay.cfg["overlay_skin"] = k
            owner.overlay.update_look()
            owner.overlay._render_history()
            owner.overlay._fit_height()
        except Exception as e:
            log("切换字幕皮肤失败：" + repr(e))
        save_config(cfg)
    skin_box.currentIndexChanged.connect(lambda _i: _preview_skin())
    pos_hint = QLabel("默认底部居中、离任务栏 72px；交互模式下拖动字幕条，靠近中线 / 底部会自动吸附，松手记住。")
    pos_hint.setStyleSheet("color:#8C8781;font-size:11px;")
    pos_hint.setWordWrap(True)
    root.addWidget(pos_hint)
    size_hint = QLabel("字号也能不进设置直接调：Ctrl+Alt+= 放大 / Ctrl+Alt+- 缩小；"
                       "交互模式下 Ctrl+滚轮，或字幕条上的 A− / A+。")
    size_hint.setStyleSheet("color:#8C8781;font-size:11px;")
    size_hint.setWordWrap(True)
    root.addWidget(size_hint)

    chk_src = QCheckBox("同时显示中文原文")
    chk_src.setChecked(bool(cfg.get("show_source")))
    root.addWidget(chk_src)

    chk_bilingual = QCheckBox("对照模式：悬浮条并排显示「中文 ｜ 英文」（更短，默认关）")
    chk_bilingual.setChecked(bool(cfg.get("overlay_bilingual", False)))
    root.addWidget(chk_bilingual)
    bil_hint = QLabel("开着的时候中文和英文挤在同一行，卡片不会多长一行；关掉就是纯英文沉浸。")
    bil_hint.setStyleSheet("color:#8C8781;font-size:11px;")
    root.addWidget(bil_hint)

    # 字幕样式预设：游戏 / 观影 / 极简，字号、透明度、行数一起换
    head("性能与扩展")
    row_perf = QHBoxLayout()
    row_perf.addWidget(QLabel("性能档位"))
    perf_box = QComboBox()
    perf_box.addItems([PERF_MODES[k]["name"] for k in ("high", "balanced", "eco")])
    perf_box.setCurrentIndex({"high": 0, "balanced": 1, "eco": 2}.get(
        str(cfg.get("perf_mode") or "balanced"), 1))
    row_perf.addWidget(perf_box)
    row_perf.addStretch(1)
    root.addLayout(row_perf)

    def _apply_perf(idx):
        key = ("high", "balanced", "eco")[idx]
        m = PERF_MODES[key]
        sr_combo.setCurrentIndex(0 if m["pre_roll_blocks"] <= 4 else
                                 (2 if m["pre_roll_blocks"] >= 10 else 1))
        chk_audio.setChecked(bool(m["save_audio"]))
        asr_box.setCurrentIndex(0 if m["asr_quality"] == "accurate" else 1)
        log("性能档位切到：%s" % m["name"])
    perf_box.currentIndexChanged.connect(_apply_perf)
    perf_hint = QLabel("高性能：完整音频缓存、高精度识别、保留音频切片；均衡：中等缓冲；"
                       "省电：关切片 + 小缓冲 + 快识别，笔记本玩游戏用。")
    perf_hint.setStyleSheet("color:#8C8781;font-size:11px;")
    perf_hint.setWordWrap(True)
    root.addWidget(perf_hint)

    chk_mic = QCheckBox("双向语音辅助（麦克风说中文 → 悬停显示英文，联机发言用）")
    chk_mic.setChecked(bool(cfg.get("mic_assist")))
    root.addWidget(chk_mic)
    chk_mic_read = QCheckBox("说出的英文自动朗读（说中文即出英文朗读；关掉只看文字）")
    chk_mic_read.setChecked(bool(cfg.get("mic_autoread")))
    root.addWidget(chk_mic_read)
    mic_hint = QLabel("默认关闭，不影响收字幕；开着的时候会额外占一点 CPU 和麦克风。")
    mic_hint.setStyleSheet("color:#8C8781;font-size:11px;")
    root.addWidget(mic_hint)

    chk_src_on = QCheckBox("外源音频识别（队友 / 视频；关掉只留麦克风链路）")
    chk_src_on.setChecked(bool(cfg.get("source_on", True)))
    root.addWidget(chk_src_on)

    row_role = QHBoxLayout()
    row_role.addWidget(QLabel("句对角色关系"))
    role_box = QComboBox()
    role_box.addItems(["队友之间", "主播 / 观众", "陌生人闲聊"])
    role_box.setCurrentIndex({"teammate": 0, "streamer": 1, "stranger": 2}.get(
        str(cfg.get("role") or "teammate"), 0))
    row_role.addWidget(role_box)
    row_role.addStretch(1)
    root.addLayout(row_role)

    chk_strip = QCheckBox("存入句对时剥离 uh / like / you know（字幕仍原样显示）")
    chk_strip.setChecked(bool(cfg.get("strip_fillers", True)))
    root.addWidget(chk_strip)
    chk_saveout = QCheckBox("“你说中文→英文”自动存进输出链路（用于中→英自测）")
    chk_saveout.setChecked(bool(cfg.get("save_out_pairs", True)))
    root.addWidget(chk_saveout)
    chk_preload = QCheckBox("应答预加载：说中文回复时，推荐贴合上一句的英文")
    chk_preload.setChecked(bool(cfg.get("reply_preload", True)))
    root.addWidget(chk_preload)

    chk_hard = QCheckBox("交互模式下给「困难」生词画细下划线（非交互模式完全不可见）")
    chk_hard.setChecked(bool(cfg.get("hard_underline", True)))
    root.addWidget(chk_hard)

    # 字幕样式预设：游戏 / 观影 / 极简，字号、透明度、行数一起换
    def _place_anchor(anchor):
        """按预设把人放到合适的高度：游戏靠底避准星，观影偏上避人脸。"""
        try:
            from PySide6.QtWidgets import QApplication as _QApp
            win = owner.overlay.win
            scr = win.screen() or _QApp.primaryScreen()
            geo = scr.availableGeometry()
            h = win.height()
            y = geo.top() + 120 if str(anchor) == "upper" else geo.bottom() - h - 46
            win.move(win.x(), max(geo.top(), min(y, geo.bottom() - h)))
        except Exception:
            pass

    row_style = QHBoxLayout()
    row_style.addWidget(QLabel("字幕样式"))
    style_box = QComboBox()
    style_box.addItems(["游戏（粗字高对比 · 底部靠下避开准星）",
                        "观影（柔和 · 位置偏上避开人脸）",
                        "极简（低透明度 · 遮挡最小）",
                        "自定义"])
    style_box.setCurrentIndex({"game": 0, "film": 1, "minimal": 2}.get(
        str(cfg.get("style_preset") or "game"), 3))
    row_style.addWidget(style_box)
    row_style.addStretch(1)
    root.addLayout(row_style)
    style_hint = QLabel("换预设会连字号 / 透明度 / 行数 / 垂直位置一起换；你在某个预设上手动调过的值，保存后会记回这个预设。")
    style_hint.setStyleSheet("color:#8C8781;font-size:11px;")
    style_hint.setWordWrap(True)
    root.addWidget(style_hint)

    def _apply_style(idx):
        keys = {0: "game", 1: "film", 2: "minimal"}.get(idx)
        if not keys:
            return
        prof = (cfg.get("style_profiles") or {}).get(keys) or {}
        if prof.get("font_size"):
            size.setValue(int(prof["font_size"]))
        if prof.get("opacity"):
            alpha.setValue(float(prof["opacity"]))
        if prof.get("scroll_rows"):
            sr_combo.setCurrentIndex(0 if int(prof["scroll_rows"]) <= 8 else
                                     (2 if int(prof["scroll_rows"]) >= 12 else 1))
        _place_anchor(prof.get("anchor") or "bottom")
    style_box.currentIndexChanged.connect(_apply_style)

    head("朗读（跟读练习）")
    row = QHBoxLayout()
    chk_auto = QCheckBox("新句子自动朗读")
    chk_auto.setChecked(bool(cfg.get("auto_read", False)))
    row.addWidget(chk_auto)
    row.addSpacing(12)
    chk_hl = QCheckBox("朗读时逐词高亮")
    chk_hl.setChecked(bool(cfg.get("word_hl", True)))
    row.addWidget(chk_hl)
    row.addStretch(1)
    root.addLayout(row)

    row = QHBoxLayout()
    row.addWidget(QLabel("语速"))
    rate = QSpinBox()
    rate.setRange(-4, 4)
    rate.setValue(int(cfg.get("read_rate") or 0))
    row.addWidget(rate)
    row.addWidget(QLabel("（0 正常，负数更慢，跟读建议 -2 ~ -1）"))
    row.addStretch(1)
    root.addLayout(row)

    voice_hint = QLabel("音色：%s（英文） · %s（中文）。本机系统自带这两个，想换可在 Windows 设置里加语言语音包。"
                        % (owner.overlay.tts.voice_name("en"), owner.overlay.tts.voice_name("zh")))
    voice_hint.setStyleSheet("color:#8C8781;font-size:11px;")
    voice_hint.setWordWrap(True)
    root.addWidget(voice_hint)

    hl_hint = QLabel("字幕条常态对鼠标穿透，游戏里点不到它。按 %s 进入交互模式，就能点朗读 / 循环 / 收藏、选中文字；"
                     "再按一次（或按 ESC / %s）恢复穿透，鼠标还给游戏。"
                     % (cfg.get("hotkey_toggle") or "Ctrl+Alt+G",
                        cfg.get("hotkey_panic") or "Ctrl+Alt+Q"))
    hl_hint.setStyleSheet("color:#8C8781;font-size:11px;")
    hl_hint.setWordWrap(True)
    root.addWidget(hl_hint)

    head("快捷键（都可改）")
    from PySide6.QtWidgets import QKeySequenceEdit
    from PySide6.QtGui import QKeySequence as _QKS
    hk_grid = QGridLayout()
    hk_grid.setColumnStretch(1, 1)
    hk_edits = {}
    hk_tips = {}
    hk_default_tips = {}
    # 键位清单统一来自模块级 HOTKEY_SPECS，和速查面板共用，避免两处不同步
    for i, (key, label, tip, _grp) in enumerate(HOTKEY_SPECS):
        lb = QLabel(label)
        ed = QKeySequenceEdit()
        try:
            ed.setKeySequence(_QKS(str(cfg.get(key) or ""))
                              if cfg.get(key) else _QKS())
        except Exception:
            pass
        hk_tip = QLabel(tip)
        hk_tip.setStyleSheet("color:#8C8781;font-size:11px;")
        hk_grid.addWidget(lb, i, 0)
        hk_grid.addWidget(ed, i, 1)
        hk_grid.addWidget(hk_tip, i, 2)
        hk_edits[key] = ed
        hk_tips[key] = hk_tip
        hk_default_tips[key] = tip
    root.addLayout(hk_grid)

    hk_btns = QHBoxLayout()
    hk_rec = QPushButton("恢复推荐键位")
    hk_chk = QPushButton("检查冲突")
    hk_state = QLabel("")
    hk_state.setStyleSheet("color:#8C8781;font-size:11px;")
    hk_btns.addWidget(hk_rec)
    hk_btns.addWidget(hk_chk)
    hk_btns.addWidget(hk_state)
    hk_btns.addStretch(1)
    root.addLayout(hk_btns)

    def _hk_risk(seq):
        s = (seq or "").strip()
        if not s:
            return ""
        parts = [p.strip().upper() for p in s.split("+") if p.strip()]
        base = parts[-1] if parts else ""
        mods = set(parts[:-1])
        if base in ("F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9",
                    "F10", "F11", "F12"):
            return "单键 %s 是游戏高频键（存档/技能），建议加 Ctrl+Alt" % base
        if base == "L" and "CTRL" in mods and "ALT" not in mods:
            return "Ctrl+L 是浏览器地址栏"
        if base == "," and "CTRL" in mods and "ALT" not in mods:
            return "Ctrl+, 是 VS Code 设置页"
        if base in ("A",) and "CTRL" in mods and "ALT" in mods:
            return "Ctrl+Alt+A 是 QQ 截图"
        if base == "Z" and "CTRL" in mods and "ALT" in mods:
            return "Ctrl+Alt+Z 是 QQ 提取消息"
        if "CTRL" in mods and "ALT" not in mods and len(parts) == 2:
            return "Ctrl+字母 常被浏览器/编辑器占用，建议再加 Alt"
        return ""

    def _hk_check():
        seqs = {}
        ok = True
        for k, ed in hk_edits.items():
            seq = ed.keySequence().toString()
            if not seq:
                hk_tips[k].setStyleSheet("color:#8C8781;font-size:11px;")
                continue
            if seq in seqs:
                hk_tips[k].setText("与「%s」重复" % seqs[seq])
                hk_tips[k].setStyleSheet("color:#EF4444;font-size:11px;")
                ok = False
            else:
                seqs[seq] = k
                risk = _hk_risk(seq)
                if risk:
                    hk_tips[k].setText(risk)
                    hk_tips[k].setStyleSheet("color:#F59E0B;font-size:11px;")
                    ok = False
                else:
                    hk_tips[k].setText(hk_default_tips.get(k, ""))
                    hk_tips[k].setStyleSheet("color:#10B981;font-size:11px;")
        hk_state.setText("没有发现重复或高风险键位" if ok else "有重复 / 高风险键位，见右侧提示")
        hk_state.setStyleSheet("color:%s;font-size:11px;" % ("#10B981" if ok else "#F59E0B"))
        return ok

    def _hk_fill_defaults():
        for k, ed in hk_edits.items():
            val = DEFAULT_CONFIG.get(k, "")
            ed.setKeySequence(_QKS(str(val)) if val else _QKS())
        _hk_check()

    hk_rec.clicked.connect(lambda: _hk_fill_defaults())
    hk_chk.clicked.connect(lambda: _hk_check())
    hk_hint = QLabel("默认全部用 Ctrl+Alt+字母，避开 F1~F12、Ctrl+L、Ctrl+, 这些游戏 / 浏览器 / 编辑器的高频键；"
                     "改键时优先保持 Ctrl+Alt 前缀，单键或纯 Ctrl+字母很容易被别的程序抢走。"
                     "留空 = 这个动作不绑定全局键。")
    hk_hint.setStyleSheet("color:#8C8781;font-size:11px;")
    hk_hint.setWordWrap(True)
    root.addWidget(hk_hint)

    to_row = QHBoxLayout()
    to_row.addWidget(QLabel("交互模式超时"))
    to_combo = QComboBox()
    to_combo.addItems(["3 秒（推荐）", "5 秒", "不自动退出"])
    _to_now = int(cfg.get("interactive_timeout", 3) or 0)
    to_combo.setCurrentIndex(2 if _to_now <= 0 else (1 if _to_now >= 5 else 0))
    to_row.addWidget(to_combo)
    to_row.addWidget(QLabel("（无鼠标操作多久自动退回游戏）"))
    to_row.addStretch(1)
    root.addLayout(to_row)

    sr_row = QHBoxLayout()
    sr_row.addWidget(QLabel("滚动缓冲区"))
    sr_combo = QComboBox()
    sr_combo.addItems(["8 行", "10 行", "12 行"])
    _sr_now = int(cfg.get("scroll_rows") or 10)
    sr_combo.setCurrentIndex(0 if _sr_now <= 8 else (2 if _sr_now >= 12 else 1))
    sr_row.addWidget(sr_combo)
    sr_row.addWidget(QLabel("（字幕条上留几句可回头看；交互模式整块都能选中）"))
    sr_row.addStretch(1)
    root.addLayout(sr_row)

    idle_row = QHBoxLayout()
    idle_row.addWidget(QLabel("静音暂停刷新"))
    idle_box = QComboBox()
    idle_box.addItems(["8 秒", "12 秒（默认）", "20 秒", "不暂停"])
    _idle = int(cfg.get("idle_pause") or 12)
    idle_box.setCurrentIndex({8: 0, 12: 1, 20: 2, 0: 3}.get(_idle, 1))
    idle_row.addWidget(idle_box)
    idle_row.addWidget(QLabel("（长时间没语音就把字幕停住，不刷新、不产生空行）"))
    idle_row.addStretch(1)
    root.addLayout(idle_row)

    head("说话人区分")
    chk_spk = QCheckBox("标注每句是谁说的（说话人1 / 2 / 3…）")
    chk_spk.setChecked(bool(cfg.get("diarize", False)))
    root.addWidget(chk_spk)
    spk_hint = QLabel("按声音纹理归人：同一个人的句子归成一类，最多 4 人。素材够长才稳，一句话太短偶尔会认错。")
    spk_hint.setStyleSheet("color:#8C8781;font-size:11px;")
    spk_hint.setWordWrap(True)
    root.addWidget(spk_hint)

    chk_spk_col = QCheckBox("记录表里显示说话人列（不用时关掉，省横向空间）")
    chk_spk_col.setChecked(bool(cfg.get("show_spk_col", True)))
    root.addWidget(chk_spk_col)

    row = QHBoxLayout()
    row.addWidget(QLabel("声纹灵敏度"))
    spk_mode = QComboBox()
    spk_mode.addItems(["严格（宁可不标，不标错）", "均衡（默认）", "宽松（尽量都标上）"])
    spk_mode.setCurrentIndex({"strict": 0, "balanced": 1, "loose": 2}.get(cfg.get("spk_mode"), 1))
    row.addWidget(spk_mode, 1)
    root.addLayout(row)

    row = QHBoxLayout()
    row.addWidget(QLabel("最短停留"))
    hold = QDoubleSpinBox()
    hold.setRange(0.5, 2.5)
    hold.setSingleStep(0.1)
    hold.setDecimals(1)
    hold.setValue(float(cfg.get("hold_scale") or 1.0))
    row.addWidget(hold)
    row.addWidget(QLabel("倍（默认 1.0；每句约 0.9～2.8 秒，觉得太快可提高到 1.3 以上）"))
    row.addStretch(1)
    root.addLayout(row)

    row = QHBoxLayout()
    row.addWidget(QLabel("断句阈值"))
    sens = QDoubleSpinBox()
    sens.setRange(0.001, 0.05)
    sens.setDecimals(3)
    sens.setSingleStep(0.001)
    sens.setValue(float(cfg.get("sensitivity") or 0.006))
    row.addWidget(sens)
    row.addWidget(QLabel("背景音乐大就调高，人声小就调低"))
    row.addStretch(1)
    root.addLayout(row)

    # 场景预设：一键切降噪 / VAD / 阈值三件套，并记住每个场景上次用的值
    row_scene = QHBoxLayout()
    row_scene.addWidget(QLabel("场景预设"))
    scene_box = QComboBox()
    scene_box.addItems(["FPS / 射击（保口音）", "看视频（降噪轻）", "语音聊天 / 连麦", "自定义"])
    _sc = str(cfg.get("scene") or "fps")
    scene_box.setCurrentIndex({"fps": 0, "video": 1, "chat": 2}.get(_sc, 3))
    row_scene.addWidget(scene_box)
    row_scene.addSpacing(12)
    row_scene.addWidget(QLabel("降噪"))
    noise_box = QComboBox()
    noise_box.addItems(["关", "中等", "强"])
    noise_box.setCurrentIndex(max(0, min(2, int(cfg.get("noise_level") or 0))))
    row_scene.addWidget(noise_box)
    row_scene.addSpacing(12)
    row_scene.addWidget(QLabel("VAD 灵敏度"))
    vad_box = QComboBox()
    vad_box.addItems(["高（小声也认）", "中", "低（抗噪）"])
    vad_box.setCurrentIndex({"high": 0, "medium": 1, "low": 2}.get(
        str(cfg.get("vad_sensitivity") or "medium"), 1))
    row_scene.addWidget(vad_box)
    row_scene.addStretch(1)
    root.addLayout(row_scene)
    scene_hint = QLabel("降噪默认关：强降噪会削掉 s/sh/x 这类辅音，口音更容易被听错；只有背景音乐盖住人声时才开到中等。")
    scene_hint.setStyleSheet("color:#8C8781;font-size:11px;")
    scene_hint.setWordWrap(True)
    root.addWidget(scene_hint)

    # 切换预设时立刻把三件套带过来（真正保存仍在点「保存并应用」）
    def _apply_scene(idx):
        keys = {0: "fps", 1: "video", 2: "chat"}.get(idx)
        if not keys:
            return
        prof = (cfg.get("scene_profiles") or {}).get(keys) or {}
        noise_box.setCurrentIndex(max(0, min(2, int(prof.get("noise_level", 1)))))
        vad_box.setCurrentIndex({"high": 0, "medium": 1, "low": 2}.get(
            str(prof.get("vad_sensitivity") or "medium"), 1))
        if prof.get("sensitivity"):
            sens.setValue(float(prof["sensitivity"]))
    scene_box.currentIndexChanged.connect(_apply_scene)

    # 记录占多少地方：表格行数 + 保留天数
    row_keep = QHBoxLayout()
    row_keep.addWidget(QLabel("列表显示条数"))
    keep_rows = QComboBox()
    keep_rows.addItems(["100 条", "300 条（默认）", "500 条"])
    keep_rows.setCurrentIndex({100: 0, 300: 1, 500: 2}.get(int(cfg.get("keep_rows") or 300), 1))
    row_keep.addWidget(keep_rows)
    row_keep.addSpacing(14)
    row_keep.addWidget(QLabel("记录保留"))
    keep_days = QComboBox()
    keep_days.addItems(["永久保留（默认）", "保留 90 天", "保留 30 天", "保留 7 天"])
    keep_days.setCurrentIndex({0: 0, 90: 1, 30: 2, 7: 3}.get(int(cfg.get("keep_days") or 0), 0))
    row_keep.addWidget(keep_days)
    row_keep.addStretch(1)
    root.addLayout(row_keep)

    keep_hint = QLabel("记录按天存在磁盘上；旧日期会自动压缩成 .gz（体积约原来八分之一）。"
                       "列表只把最近这些条放进内存，更早的靠搜索在磁盘里查。")
    keep_hint.setWordWrap(True)
    keep_hint.setStyleSheet("color:#8C8781;font-size:11px;")
    root.addWidget(keep_hint)

    chk_audio = QCheckBox("收藏时保存音频切片（复习能听原声；低配或不想占磁盘就关掉）")
    chk_audio.setChecked(bool(cfg.get("save_audio", True)))
    root.addWidget(chk_audio)
    audio_hint = QLabel("切片带 2 秒前置缓存，句头和句尾都不会被切掉；每条收藏记录带「⏱ 时:分:秒–时:分:秒」。关掉后收藏照旧，只是没有 wav。")
    audio_hint.setStyleSheet("color:#8C8781;font-size:11px;")
    audio_hint.setWordWrap(True)
    root.addWidget(audio_hint)

    root.addStretch(1)
    btns = QHBoxLayout()
    btns.addStretch(1)
    cancel = QPushButton("取消")
    ok = QPushButton("保存并应用")
    ok.setObjectName("primary")
    btns.addWidget(cancel)
    btns.addWidget(ok)
    root.addLayout(btns)
    def _cancel():
        owner.overlay.update_look()      # 取消：把实时预览退回到已保存的样子
        dlg.reject()

    cancel.clicked.connect(_cancel)

    def apply():
        old_engine = cfg.get("engine")
        old_model = cfg.get("whisper_model")
        old_online = (cfg.get("base_url"), cfg.get("model"), cfg.get("api_key"))
        old_spk = bool(cfg.get("diarize", False))
        old_spk_mode = cfg.get("spk_mode") or "balanced"
        cfg["engine"] = "online" if rb_online.isChecked() else "local"
        for k, e in edits.items():
            cfg[k] = e.text().strip()
        cfg["whisper_model"] = (model_box.currentData() or "auto")
        cfg["asr_quality"] = "accurate" if asr_box.currentIndex() == 0 else "fast"
        cfg["latency_mode"] = (lat_box.currentData() or "balanced")
        # 自定义热词只做归一化（各种分隔符统一成空格），保留用户原写法
        cfg["hotwords_custom"] = re.sub(r"[，,、;；\n]+", " ", hot_box.text()).strip()
        cfg["asr_hotwords_mode"] = str(hw_box.currentData() or "custom")
        cfg["read_rate"] = int(rate.value())
        cfg["word_hl"] = bool(chk_hl.isChecked())
        cfg["auto_read"] = bool(chk_auto.isChecked())
        try:
            owner.overlay.chk_auto.setChecked(bool(chk_auto.isChecked()))
            owner.overlay.cfg["read_rate"] = cfg["read_rate"]
            owner.overlay.cfg["word_hl"] = cfg["word_hl"]
        except Exception:
            pass
        # 热键：写法不认识就保留原值，别把自己弄失效
        old_hk = {k: cfg.get(k) for k in ("hotkey_toggle", "hotkey_fav_last",
                                          "hotkey_fav_easy", "hotkey_fav_mid", "hotkey_fav_hard",
                                          "hotkey_history", "hotkey_clear", "hotkey_panic",
                                          "hotkey_subtitle", "hotkey_capture",
                                          "hotkey_library", "hotkey_selftest",
                                          "hotkey_f5_capture", "hotkey_f6_fav", "hotkey_f7_sub",
                                          "hotkey_f8_drawer", "hotkey_f9_autoread",
                                          "hotkey_f10_mute",
                                          "hotkey_ctrl_l", "hotkey_ctrl_lab")}
        # 同一组合键绑两个动作，注册时后一个必被跳过；先拦住，别让用户以为生效了
        _dup = {}
        for k, ed in hk_edits.items():
            s = ed.keySequence().toString()
            if not s:
                continue
            if s in _dup:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(dlg, "快捷键重复",
                                    "「%s」同时绑给了两个动作，请先改掉再保存。" % s)
                return
            _dup[s] = k
        for k, ed in hk_edits.items():
            seq = ed.keySequence().toString()
            if seq and _parse_hotkey(seq):
                cfg[k] = seq
            elif not seq:
                cfg[k] = ""                  # 留空 = 主动解绑这个全局键
            else:
                owner.overlay.cfg[k] = old_hk.get(k)
                cfg[k] = old_hk.get(k)
            if old_hk.get(k) != cfg.get(k):
                cfg["hotkey_custom"] = True
        cfg["hotkey_scheme"] = 3
        cfg["interactive_timeout"] = 0 if to_combo.currentIndex() == 2 else (
            5 if to_combo.currentIndex() == 1 else 3)
        cfg["scroll_rows"] = [8, 10, 12][sr_combo.currentIndex()]
        cfg["style_preset"] = {0: "game", 1: "film", 2: "minimal"}.get(
            style_box.currentIndex(), "custom")
        cfg["idle_pause"] = [8, 12, 20, 0][idle_box.currentIndex()]
        try:
            owner.overlay.cfg["idle_pause"] = cfg["idle_pause"]
        except Exception:
            pass
        try:
            owner.overlay.cfg["scroll_rows"] = cfg["scroll_rows"]
            owner.overlay._hist = owner.overlay._hist[: max(1, cfg["scroll_rows"] - 1)]
            owner.overlay._render_history()
            owner.overlay._fit_height()
        except Exception:
            pass
        if any(old_hk[k] != cfg.get(k) for k in old_hk):
            try:
                owner.overlay.cfg.update({k: cfg[k] for k in old_hk})
                owner.reload_hotkeys()      # 原地重注册，不用重启
            except Exception as e:
                log("保存快捷键后重注册失败：" + repr(e))
        cfg["loopback_device"] = dev_box.currentText()
        cfg["font_size"] = int(size.value())
        cfg["overlay_prev_rows"] = int(prev_n.value())
        cfg["opacity"] = float(alpha.value())
        cfg["show_source"] = bool(chk_src.isChecked())
        cfg["overlay_bilingual"] = bool(chk_bilingual.isChecked())
        cfg["overlay_pos"] = str(pos_box.currentData() or "bottom")
        cfg["overlay_style"] = str(sty_box.currentData() or "dialog")
        cfg["filter_short"] = bool(chk_short.isChecked())
        cfg["min_chars"] = int(min_chars.value())
        cfg["min_words"] = int(min_words.value())
        cfg["target_lang"] = state["tl"]
        # 语种对双向配对：目标选中文就识别英文，其余情况识别中文（不再固定中文源）
        cfg["source_lang"] = "en" if state["tl"] == "zh" else "zh"
        if cfg["source_lang"] == cfg["target_lang"]:
            cfg["source_lang"] = "en" if cfg["target_lang"] != "en" else "zh"
        cfg["mt_size"] = mt_box.currentData() or "small"
        cfg["rtl_overlay"] = bool(chk_rtl.isChecked())
        cfg["font_family"] = font_edit.text().strip()
        # 旧的逐语种字体表已移除：保留本机已存的逐语种字体设置，不再从已删除的表格读取
        cfg["lang_fonts"] = dict(cfg.get("lang_fonts") or {})
        try:
            profiles = dict(cfg.get("lang_profiles") or {})
            profiles[cfg["target_lang"]] = {
                "font_size": cfg["font_size"], "opacity": cfg["opacity"],
                "whisper_model": cfg["whisper_model"],
                "tts_rate": float((cfg.get("tts_rates") or {}).get(
                    cfg["target_lang"], cfg.get("read_rate") or 0)),
                "min_chars": cfg["min_chars"],
                "min_words": int(min_words.value()),
                "show_source": cfg["show_source"],
                "overlay_bilingual": cfg["overlay_bilingual"],
            }
            cfg["lang_profiles"] = profiles
        except Exception:
            pass
        set_langs(cfg)
        try:
            owner.main.refresh_lang_headers()
            owner.main._fill_lang_box()
            owner.main.apply_lang_fonts()
            owner.overlay.cfg["target_lang"] = cfg["target_lang"]
            owner.overlay.cfg["rtl_overlay"] = cfg["rtl_overlay"]
            owner.overlay.cfg["lang_fonts"] = cfg["lang_fonts"]
            owner.overlay.cfg["font_family"] = cfg["font_family"]
            owner.overlay.update_look()
        except Exception:
            pass
        try:
            owner.overlay.cfg["overlay_bilingual"] = cfg["overlay_bilingual"]
            owner.overlay.cfg["show_source"] = cfg["show_source"]
            owner.overlay.cfg["filter_short"] = cfg["filter_short"]
            owner.overlay.cfg["min_chars"] = cfg["min_chars"]
        except Exception:
            pass
        try:
            profs = dict(cfg.get("style_profiles") or {})
            if cfg["style_preset"] != "custom":
                profs[cfg["style_preset"]] = {
                    "font_size": cfg["font_size"], "opacity": cfg["opacity"],
                    "scroll_rows": cfg["scroll_rows"],
                    "anchor": "upper" if cfg["style_preset"] == "film" else "bottom",
                }
            cfg["style_profiles"] = profs
            _place_anchor("upper" if cfg["style_preset"] == "film" else "bottom")
        except Exception:
            pass
        cfg["hold_scale"] = float(hold.value())
        cfg["sensitivity"] = float(sens.value())
        # 音源：降噪强度 + VAD 灵敏度 + 当前场景（每个场景记住自己那套值）
        cfg["noise_level"] = int(noise_box.currentIndex())
        cfg["vad_sensitivity"] = ["high", "medium", "low"][vad_box.currentIndex()]
        _scene_keys = {0: "fps", 1: "video", 2: "chat"}.get(scene_box.currentIndex(), "custom")
        cfg["scene"] = _scene_keys
        try:
            profiles = dict(cfg.get("scene_profiles") or {})
            if _scene_keys != "custom":
                profiles[_scene_keys] = {
                    "noise_level": cfg["noise_level"],
                    "vad_sensitivity": cfg["vad_sensitivity"],
                    "sensitivity": cfg["sensitivity"],
                }
            cfg["scene_profiles"] = profiles
        except Exception:
            pass
        cfg["diarize"] = bool(chk_spk.isChecked())
        cfg["spk_mode"] = ["strict", "balanced", "loose"][spk_mode.currentIndex()]
        cfg["keep_rows"] = [100, 300, 500][keep_rows.currentIndex()]
        cfg["keep_days"] = [0, 90, 30, 7][keep_days.currentIndex()]
        cfg["save_audio"] = bool(chk_audio.isChecked())
        cfg["perf_mode"] = ("high", "balanced", "eco")[perf_box.currentIndex()]
        try:
            m = PERF_MODES[cfg["perf_mode"]]
            cfg["pre_roll_blocks"] = int(m["pre_roll_blocks"])
            cfg["mic_assist"] = bool(chk_mic.isChecked())
            cfg["mic_autoread"] = bool(chk_mic_read.isChecked())
            cfg["hard_underline"] = bool(chk_hard.isChecked())
            cfg["source_on"] = bool(chk_src_on.isChecked())
            cfg["role"] = ["teammate", "streamer", "stranger"][role_box.currentIndex()]
            cfg["strip_fillers"] = bool(chk_strip.isChecked())
            cfg["save_out_pairs"] = bool(chk_saveout.isChecked())
            cfg["reply_preload"] = bool(chk_preload.isChecked())
            cfg["show_spk_col"] = bool(chk_spk_col.isChecked())
            try:
                owner.main.set_spk_col_visible(bool(cfg["show_spk_col"]))
                owner.main.refresh_chips()
            except Exception:
                pass
            owner.overlay.cfg.update({
                "pre_roll_blocks": cfg["pre_roll_blocks"],
                "hard_underline": cfg["hard_underline"],
                "source_on": cfg["source_on"],
                "role": cfg["role"],
                "strip_fillers": cfg["strip_fillers"],
                "reply_preload": cfg["reply_preload"],
            })
            # 外源链路开关：关掉就停采集，只留麦克风
            if cfg["source_on"]:
                if not (owner.capture and owner.capture.running) and not owner.paused:
                    owner.start_capture()
            else:
                owner.stop_capture()
            if cfg["mic_assist"]:
                owner.start_mic()
            else:
                owner.stop_mic()
            if cfg["hard_underline"] and not owner.overlay._hard_words:
                owner.overlay.refresh_hard_words()
        except Exception as e:
            log("应用性能/语音选项失败：" + repr(e))
        try:
            owner.overlay.cfg["save_audio"] = cfg["save_audio"]
        except Exception:
            pass
        save_config(cfg)
        housekeeping(int(cfg["keep_days"]))
        try:
            owner.main._max_rows = int(cfg["keep_rows"])
            owner.main.load_day(owner.main.day_box.currentText())
        except Exception:
            pass
        owner.overlay.update_look()
        online_now = (cfg.get("base_url"), cfg.get("model"), cfg.get("api_key"))
        if old_engine != cfg["engine"] or old_model != cfg["whisper_model"] or old_online != online_now \
                or old_spk != cfg["diarize"] or old_spk_mode != cfg["spk_mode"]:
            owner.restart_engine()
        else:
            owner.stop_capture()
            owner.paused = False
            owner.start_capture()
        dlg.accept()

    ok.clicked.connect(apply)
    # 各处 hint 文字还留着旧的浅色写死值，按当前主题统一改一遍，避免深色下看不清
    tc = THEME_COLORS.get(theme, THEME_COLORS["dark"])
    for w in dlg.findChildren(QWidget):
        ss = w.styleSheet()
        if not ss:
            continue
        for old, new in (("#8C8781", tc["muted"]), ("#5E5A52", tc["muted"]),
                         ("#E6E4E1", tc["border"])):
            ss = ss.replace(old, new)
        w.setStyleSheet(ss)
    dlg.exec()


# ---------------------------------------------------------------- 拆词查义窗口
def build_word_dialog(parent, book, sentence, spk, words, on_saved, conf=None, scene=""):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QCursor
    from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
                                   QTableWidgetItem, QPushButton, QHeaderView,
                                   QAbstractItemView, QApplication)

    dlg = QDialog(parent)
    dlg.setWindowTitle("拆词查义")
    dlg.setModal(True)
    dlg.resize(760, 560)
    dlg.setStyleSheet(
        "QDialog{background:#FFFFFF;}"
        "QLabel{color:#37352F;font-size:12px;}"
        "QPushButton{background:#FFFFFF;border:1px solid #E1DFDB;border-radius:8px;"
        "padding:6px 14px;color:#37352F;font-size:12px;}"
        "QPushButton:hover{background:#F2F1EE;}"
        "QPushButton#primary{background:#3B82F6;border:1px solid #3B82F6;color:#FFFFFF;font-weight:600;}"
        "QTableWidget{background:#FFFFFF;border:1px solid #E6E4E1;border-radius:10px;"
        "gridline-color:#EFEDE9;font-size:13px;}"
        "QHeaderView::section{background:#FAF9F7;border:none;border-bottom:1px solid #EFEDE9;"
        "padding:6px;color:#8A867E;font-size:11px;}"
    )
    v = QVBoxLayout(dlg)
    v.setContentsMargins(18, 16, 18, 14)
    v.setSpacing(10)

    head = QLabel(sentence)
    head.setWordWrap(True)
    head.setStyleSheet("color:#1F1E1B;font-size:13px;font-weight:600;")
    v.addWidget(head)
    if spk:
        tag = QLabel("来自 " + spk)
        tag.setStyleSheet("color:%s;font-size:11px;" % spk_color(spk, SPK_TEXT_COLORS))
        v.addWidget(tag)
    hint = QLabel("勾上不认识的 → 右下角「加入生词本」；勾上想留着的 → 「☆ 收藏选中的单词」。双击行可以看完整原句。")
    hint.setStyleSheet("color:#8A867E;font-size:11px;")
    v.addWidget(hint)

    tb = QTableWidget(len(words), 3)
    tb.setHorizontalHeaderLabels(["选", "单词", "释义"])
    tb.setColumnWidth(0, 40)
    tb.setColumnWidth(1, 150)
    tb.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
    tb.verticalHeader().setVisible(False)
    tb.setEditTriggers(QAbstractItemView.NoEditTriggers)
    tb.setSelectionBehavior(QAbstractItemView.SelectRows)
    v.addWidget(tb, 1)

    mp = scene or ""

    def translate_one(w):
        g = gloss_line(w, mp)
        return g or book.lookup(w, sentence)

    QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
    try:
        for i, w in enumerate(words):
            chk = QTableWidgetItem("")
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Unchecked)
            tb.setItem(i, 0, chk)
            tb.setItem(i, 1, QTableWidgetItem(w))
            tb.setItem(i, 2, QTableWidgetItem(translate_one(w)))
    finally:
        QApplication.restoreOverrideCursor()

    row = QHBoxLayout()
    btn_all = QPushButton("全选")
    btn_none = QPushButton("全不选")
    row.addWidget(btn_all)
    row.addWidget(btn_none)
    row.addStretch(1)
    btn_close = QPushButton("关闭")
    btn_fav = QPushButton("☆ 收藏选中的单词")
    btn_save = QPushButton("加入生词本")
    btn_save.setObjectName("primary")
    for b in (btn_close, btn_fav, btn_save):
        b.setFixedHeight(32)
    row.addWidget(btn_close)
    row.addWidget(btn_fav)
    row.addWidget(btn_save)
    v.addLayout(row)

    def set_all(state):
        for i in range(tb.rowCount()):
            tb.item(i, 0).setCheckState(state)

    def save():
        n = 0
        for i in range(tb.rowCount()):
            if tb.item(i, 0).checkState() == Qt.Checked:
                w = tb.item(i, 1).text()
                m = tb.item(i, 2).text()
                if append_word(w, m, sentence, spk,
                               low_conf=(conf is not None and float(conf) < LOW_CONF),
                               scene=scene):
                    n += 1
        if n:
            on_saved()
            dlg.accept()
        else:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(dlg, "加入生词本", "先勾选要记的单词。")

    def fav_words():
        n = 0
        for i in range(tb.rowCount()):
            if tb.item(i, 0).checkState() == Qt.Checked:
                w = tb.item(i, 1).text()
                m = tb.item(i, 2).text()
                if append_fav("word", w, extra=m, en=sentence, spk=spk):
                    n += 1
        from PySide6.QtWidgets import QMessageBox
        if n:
            on_saved()
            dlg.accept()
        else:
            QMessageBox.information(dlg, "收藏单词", "先勾选要收藏的单词（已收藏过的不会重复添加）。")

    btn_all.clicked.connect(lambda: set_all(Qt.Checked))
    btn_none.clicked.connect(lambda: set_all(Qt.Unchecked))
    btn_close.clicked.connect(dlg.reject)
    btn_fav.clicked.connect(fav_words)
    btn_save.clicked.connect(save)
    dlg.exec()


# ---------------------------------------------------------------- 生词卡（多语境）
def build_word_card_dialog(parent, rec):
    """一张词卡：单词 + 释义 + 所有挂过的句子，可一条条翻。
    同一个词在不同对局 / 不同视频里的用法都留在这里，这才是情境复习的用处。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                                   QPushButton, QTableWidget, QTableWidgetItem,
                                   QHeaderView, QAbstractItemView)

    dlg = QDialog(parent)
    dlg.setWindowTitle("生词卡 · %s" % rec.get("w", ""))
    dlg.setModal(True)
    dlg.resize(720, 480)
    dlg.setStyleSheet(
        "QDialog{background:#FFFFFF;}"
        "QLabel{color:#37352F;font-size:12px;}"
        "QPushButton{background:#FFFFFF;border:1px solid #E1DFDB;border-radius:8px;"
        "padding:6px 14px;color:#37352F;font-size:12px;}"
        "QPushButton:hover{background:#F2F1EE;}"
        "QTableWidget{background:#FFFFFF;border:1px solid #E6E4E1;border-radius:10px;"
        "gridline-color:#EFEDE9;font-size:13px;}"
        "QHeaderView::section{background:#FAF9F7;border:none;border-bottom:1px solid #EFEDE9;"
        "padding:6px;color:#8A867E;font-size:11px;}"
    )
    v = QVBoxLayout(dlg)
    v.setContentsMargins(18, 16, 18, 14)
    v.setSpacing(8)

    head = QHBoxLayout()
    wl = QLabel(rec.get("w", ""))
    wl.setStyleSheet("color:#1F1E1B;font-size:17px;font-weight:700;")
    head.addWidget(wl)
    ml = QLabel(str(rec.get("m", "")))
    ml.setStyleSheet("color:#5E5A52;font-size:12px;")
    ml.setWordWrap(True)
    head.addWidget(ml, 1)
    v.addLayout(head)

    if rec.get("low_conf"):
        warn = QLabel("识别置信度较低，建议核对后再当例句背诵")
        warn.setStyleSheet("color:#B47A3C;font-size:11px;")
        v.addWidget(warn)

    ctxs = rec.get("ctx") or ([{"s": rec.get("s", ""), "t": rec.get("t", "")}]
                              if rec.get("s") else [])
    cnt = QLabel("已收集 %d 条语境（同一个词在不同场景里的用法都留在这儿）" % len(ctxs))
    cnt.setStyleSheet("color:#8A867E;font-size:11px;")
    v.addWidget(cnt)

    tb = QTableWidget(len(ctxs), 4)
    tb.setHorizontalHeaderLabels(["时间", "场景", "会话", "例句"])
    tb.setColumnWidth(0, 130)
    tb.setColumnWidth(1, 60)
    tb.setColumnWidth(2, 80)
    tb.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
    tb.verticalHeader().setVisible(False)
    tb.setEditTriggers(QAbstractItemView.NoEditTriggers)
    tb.setSelectionBehavior(QAbstractItemView.SelectRows)
    tb.setWordWrap(True)
    for i, c in enumerate(reversed(ctxs)):
        tb.setItem(i, 0, QTableWidgetItem(str(c.get("t", ""))))
        tb.setItem(i, 1, QTableWidgetItem(str(c.get("scene", ""))))
        tb.setItem(i, 2, QTableWidgetItem(str(c.get("sess", ""))))
        tb.setItem(i, 3, QTableWidgetItem(str(c.get("s", ""))))
    tb.resizeRowsToContents()
    v.addWidget(tb, 1)

    foot = QHBoxLayout()
    foot.addStretch(1)
    btn = QPushButton("关闭")
    btn.setFixedHeight(32)
    btn.clicked.connect(dlg.accept)
    foot.addWidget(btn)
    v.addLayout(foot)
    dlg.exec()


# ---------------------------------------------------------------- 主窗口
class MicAssist(threading.Thread):
    """双向语音（可选）：麦克风听中文 → 翻成英文 → 悬浮小卡片（可选自动朗读）。

    默认关闭；不想要发言的时候在设置里关掉即可，不影响收字幕。
    模型直接复用 Pipeline 已经加载好的 Whisper 和翻译器，不额外占内存。"""

    SR = 16000
    BLOCK = 3200
    MAX_LEN = 40          # 最长 8 秒强切

    def __init__(self, cfg, ui_queue, get_model, get_translate, tts_busy=None, on_state=None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.ui = ui_queue
        self.get_model = get_model
        self.get_translate = get_translate
        self.tts_busy = tts_busy
        self.on_state = on_state or (lambda *a: None)
        self.running = True
        self._stop = threading.Event()

    def stop(self):
        self.running = False
        self._stop.set()

    def run(self):
        import numpy as np
        try:
            import soundcard
            name = (self.cfg.get("mic_device") or "").strip()
            mic = soundcard.get_microphone(name) if name else soundcard.default_microphone()
        except Exception as e:
            log("麦克风打不开：" + repr(e))
            self.on_state("error", "麦克风打不开：" + str(e))
            return
        log("双向语音已开：%s（说中文 → 出英文）" % getattr(mic, "name", "默认麦克风"))
        self.on_state("listening", "麦克风已开 · 说中文出英文")
        try:
            with mic.recorder(samplerate=self.SR, channels=1) as rec:
                buf = None
                silence = 0
                while not self._stop.is_set():
                    if getattr(self, "tts_busy", None) is not None and self.tts_busy.is_set():
                        time.sleep(0.05)       # 正在朗读时别把自己的声音又收进来
                        continue
                    blk = rec.record(numframes=self.BLOCK)
                    if blk is None or len(blk) == 0:
                        continue
                    level = float(np.sqrt(np.mean(np.square(blk))))
                    if buf is None:
                        if level > 0.012:
                            buf = blk.copy()
                            silence = 0
                    else:
                        buf = np.concatenate((buf, blk), axis=0)
                        silence = 0 if level > 0.012 else silence + 1
                        if silence >= 3 or len(buf) >= self.MAX_LEN * self.BLOCK:
                            seg = buf.reshape(-1)[:-max(0, (silence - 1) * self.BLOCK)]
                            buf, silence = None, 0
                            if len(seg) > self.SR // 3:
                                self._handle(seg)
        except Exception as e:
            log("麦克风采集失败：" + repr(e))
            self.on_state("error", "麦克风采集失败")
        finally:
            log("双向语音已停")

    def _handle(self, audio):
        import re
        try:
            model = self.get_model()
            if model is None:
                return
            segs, _ = model.transcribe(audio, language="zh", beam_size=3,
                                      condition_on_previous_text=False, without_timestamps=True)
            zh = "".join(s.text for s in segs).strip()
            if not zh:
                return
            # 你直接说了英文：不翻译，拿去做“语音同源匹配”，给正向反馈
            if not re.search(r"[一-鿿]", zh):
                hit = mark_pair_reuse(strip_fillers(zh) if self.cfg.get("strip_fillers", True) else zh)
                if hit:
                    log("表达复用成功：%s（%d 次）" % (hit.get("en"), int(hit.get("reuse") or 0)))
                    self.ui.put(("say", "复用成功 · 这句你之前收藏过", hit.get("en", "")))
                return
            # 自定义应答模板优先于模型生成
            en = template_reply(zh)
            if not en:
                tr = self.get_translate()
                if tr is not None and hasattr(tr, "translate"):
                    en = (tr.translate(zh) or "").strip()
                else:
                    en = ""
            if not en:
                return
            if self.cfg.get("save_out_pairs", True):
                append_pair("out",
                            strip_fillers(en) if self.cfg.get("strip_fillers", True) else en,
                            zh, scene=str(self.cfg.get("scene") or ""),
                            role=str(self.cfg.get("role") or ""),
                            sess=time.strftime("%H:%M:%S"), ctx="", raw_en=en)
            hint = ""
            if self.cfg.get("reply_preload", True):
                cands = suggest_reply(str(self.cfg.get("scene") or ""), exclude=en)
                if cands:
                    hint = "可回：" + cands[-1]
            self.ui.put(("say", zh, en, hint))
            log("双向语音：%s → %s" % (zh, en))
        except Exception as e:
            log("双向语音识别失败：" + repr(e))


# ---------------------------------------------------------------- 视觉主题
THEME_COLORS = {
    "light": {
        "bg": "#F7F8FA", "card": "#FFFFFF", "nav": "#FFFFFF", "border": "#E5E7EB",
        "text": "#1F2937", "muted": "#6B7280", "chip_bg": "#F3F4F6",
        "field": "#FFFFFF", "field_border": "#E5E7EB", "hover": "#F3F4F6",
        "header": "#FAFBFC", "grid": "#EEF0F3", "sel": "#E8F0FE",
        "accent": "#2563EB", "accent_hover": "#3B82F6", "ok": "#10B981",
        "warn": "#F59E0B", "err": "#EF4444",
    },
    "dark": {
        "bg": "#0F1115", "card": "#171A21", "nav": "#13161C", "border": "#262B36",
        "text": "#E6E8EC", "muted": "#9AA3B2", "chip_bg": "#1E232C",
        "field": "#1B1F27", "field_border": "#2C3240", "hover": "#232936",
        "header": "#1A1E25", "grid": "#242A34", "sel": "#22314B",
        "accent": "#3B82F6", "accent_hover": "#5B95F8", "ok": "#10B981",
        "warn": "#F59E0B", "err": "#EF4444",
    },
    "graphite": {                       # 石墨：低饱和中性暗灰，长时间看不刺眼
        "bg": "#141516", "card": "#1C1D1F", "nav": "#17181A", "border": "#2C2E31",
        "text": "#D9DBDF", "muted": "#8E9299", "chip_bg": "#242628",
        "field": "#1F2124", "field_border": "#323538", "hover": "#2A2C2F",
        "header": "#212325", "grid": "#2B2E31", "sel": "#30353B",
        "accent": "#9DB4C0", "accent_hover": "#B7CBD4", "ok": "#5FBF8F",
        "warn": "#D9A24A", "err": "#E07070",
    },
    "ocean": {                          # 深海：暗蓝底，冷色，适合夜间观影
        "bg": "#0B1220", "card": "#111B2E", "nav": "#0E1728", "border": "#21304A",
        "text": "#DCE6F5", "muted": "#8FA3BF", "chip_bg": "#17243A",
        "field": "#142034", "field_border": "#243450", "hover": "#1B2B45",
        "header": "#13203A", "grid": "#1F2F4A", "sel": "#1E3A63",
        "accent": "#38BDF8", "accent_hover": "#7DD3FC", "ok": "#3FC7A6",
        "warn": "#E5B24F", "err": "#F07A7A",
    },
    "paper": {                          # 暖纸：米色护眼，白天地铁 / 咖啡店场景
        "bg": "#F5EFE3", "card": "#FFFBF2", "nav": "#FBF5E9", "border": "#E3D9C6",
        "text": "#3B342A", "muted": "#7A6F5E", "chip_bg": "#EFE6D5",
        "field": "#FFFDF7", "field_border": "#DED3BF", "hover": "#F3EADB",
        "header": "#F8F1E4", "grid": "#EAE0CE", "sel": "#F2E3C8",
        "accent": "#2F7D6D", "accent_hover": "#25695B", "ok": "#3E8E5A",
        "warn": "#B57A1E", "err": "#C0503F",
    },
    "contrast": {                       # 高对比：纯黑底亮字，弱视 / 强光环境
        "bg": "#000000", "card": "#0A0A0A", "nav": "#050505", "border": "#3A3A3A",
        "text": "#F5F5F5", "muted": "#C0C0C0", "chip_bg": "#141414",
        "field": "#111111", "field_border": "#444444", "hover": "#1C1C1C",
        "header": "#0D0D0D", "grid": "#303030", "sel": "#1F3A66",
        "accent": "#FFD400", "accent_hover": "#FFE45C", "ok": "#22C55E",
        "warn": "#FBBF24", "err": "#F87171", "btn_text": "#000000",
    },
}

THEME_LABELS = {
    "dark": "暗夜竞技",
    "light": "极简白",
    "graphite": "石墨工业",
    "ocean": "深海夜航",
    "paper": "暖纸阅读",
    "contrast": "硬边高对比",
}
THEME_ORDER = ["dark", "light", "graphite", "ocean", "paper", "contrast"]

# 风格不只是换颜色：圆角、边线、字号、标题字体一起变，六套才有明显区别
THEME_STYLE = {
    "dark":     {"radius": 8,  "card_border": 1, "title": 22, "font": "Microsoft YaHei UI",
                 "card_pad": 14, "tag": "紧凑 · 游戏"},
    "light":    {"radius": 14, "card_border": 1, "title": 24, "font": "Microsoft YaHei UI",
                 "card_pad": 18, "tag": "留白 · 清爽"},
    "graphite": {"radius": 3,  "card_border": 1, "title": 21, "font": "Microsoft YaHei UI",
                 "card_pad": 13, "tag": "方正 · 工业"},
    "ocean":    {"radius": 16, "card_border": 1, "title": 25, "font": "Microsoft YaHei UI",
                 "card_pad": 18, "tag": "圆润 · 夜航"},
    "paper":    {"radius": 5,  "card_border": 1, "title": 25, "font": "Georgia, Microsoft YaHei UI",
                 "card_pad": 17, "tag": "纸感 · 阅读"},
    "contrast": {"radius": 0,  "card_border": 2, "title": 26, "font": "Microsoft YaHei UI",
                 "card_pad": 12, "tag": "硬边 · 高对比"},
}


def theme_style(theme):
    return THEME_STYLE.get(theme, THEME_STYLE["dark"])


def tcol(key, cfg=None, theme=None):
    """按当前主题取一个颜色令牌，供局部 setStyleSheet / setForeground 用。"""
    name = theme or str((cfg or {}).get("theme") or "dark")
    return THEME_COLORS.get(name, THEME_COLORS["dark"]).get(key, "#9AA3B2")


def pop_word_card(main, word, sentence="", spk=""):
    """点词卡片：后台查义 → 就地显示 → 一键存进生词本（带上这句做语境）。"""
    try:
        from PySide6.QtCore import Qt as _Qt, QTimer
        from PySide6.QtWidgets import (QFrame, QVBoxLayout, QHBoxLayout, QLabel,
                                       QPushButton, QApplication)
        from PySide6.QtGui import QCursor
    except Exception:
        return None
    cfg = getattr(main, "cfg", {}) or {}
    try:
        old = getattr(main, "_word_card", None)
        if old is not None:
            old.deleteLater()
    except Exception:
        pass
    card = QFrame(None, _Qt.Popup | _Qt.FramelessWindowHint)
    card.setObjectName("wordCard")
    card.setStyleSheet("QFrame#wordCard{background:%s;border:1px solid %s;border-radius:8px;}"
                       % (tcol("card", cfg), tcol("border", cfg)))
    lay = QVBoxLayout(card)
    lay.setContentsMargins(14, 10, 14, 10)
    lay.setSpacing(6)

    head = QLabel(word)
    head.setStyleSheet("font-size:16px;font-weight:600;color:%s;" % tcol("text", cfg))
    # 已经存过的词，直接告诉用户，别让他重复点“存生词”
    try:
        hit = next((r for r in read_words()
                    if str(r.get("w", "")).strip().lower() == word.lower()), None)
    except Exception:
        hit = None
    if hit is not None:
        nctx = len(hit.get("ctx") or []) or 1
        head.setText("%s   已在生词本 · %d 条语境" % (word, nctx))
    mean = QLabel("查询中…")
    mean.setWordWrap(True)
    mean.setMinimumWidth(200)
    mean.setMaximumWidth(340)
    mean.setStyleSheet("font-size:13px;color:%s;" % tcol("muted", cfg))
    state = {"meaning": ""}

    row = QHBoxLayout()
    row.setSpacing(8)
    btn_save = QPushButton("＋ 存生词")
    btn_save.setObjectName("primary")
    btn_save.setFixedHeight(26)
    btn_save.setEnabled(False)
    if hit is not None:
        btn_save.setText("已在生词本")
    btn_close = QPushButton("关闭")
    btn_close.setFixedHeight(26)
    row.addWidget(btn_save)
    row.addWidget(btn_close)
    row.addStretch(1)

    lay.addWidget(head)
    lay.addWidget(mean)
    lay.addLayout(row)

    def save():
        m = state["meaning"]
        if not m:
            return
        ok = append_word(word, m, sentence, spk, scene=str(cfg.get("scene") or ""))
        btn_save.setText("已存 ✓" if ok else "这句已经存过")
        btn_save.setEnabled(False)
        try:
            main.refresh_words()
            main.set_status("已存生词：%s" % word, "ok")
        except Exception:
            pass
        QTimer.singleShot(900, card.close)

    btn_save.clicked.connect(save)
    btn_close.clicked.connect(card.close)

    def fill(meaning):
        try:
            state["meaning"] = meaning or ""
            mean.setText(meaning or "（没查到释义，可以到 Lab 换在线引擎）")
            btn_save.setEnabled(bool(state["meaning"]))
            card.adjustSize()
        except Exception:
            pass

    def work():
        try:
            m = main.book.lookup(word, sentence)
        except Exception as e:
            log("点词查义失败：" + repr(e))
            m = ""
        QTimer.singleShot(0, lambda: fill(m))

    threading.Thread(target=work, daemon=True).start()

    card.adjustSize()
    pos = QCursor.pos()
    try:
        scr = QApplication.primaryScreen()
        g = scr.availableGeometry() if scr is not None else None
    except Exception:
        g = None
    if g is not None:
        x = min(max(g.left() + 8, pos.x() - 24), g.right() - card.width() - 8)
        y = min(max(g.top() + 8, pos.y() + 18), g.bottom() - card.height() - 8)
        card.move(x, y)
    else:
        card.move(pos.x() - 24, pos.y() + 18)
    card.show()
    try:
        main._word_card = card
    except Exception:
        pass
    return card


def pop_hotkey_sheet(owner):
    """快捷键速查面板：打开就能看全所有键位，底部直接跳去改。"""
    from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                                   QPushButton, QScrollArea, QWidget, QFrame)
    from PySide6.QtCore import Qt

    cfg = getattr(owner, "cfg", {}) or {}
    theme = str(cfg.get("theme") or "dark")
    base = THEME_COLORS.get(theme, THEME_COLORS["dark"])
    old = getattr(owner, "_hk_sheet", None)
    if old is not None:
        try:
            old.close()
            old.deleteLater()
        except Exception:
            pass

    dlg = QDialog(None)
    dlg.setWindowTitle("快捷键速查")
    dlg.resize(540, 620)
    dlg.setStyleSheet(theme_qss(theme) + "QDialog{background:%s;}" % base["bg"])
    root = QVBoxLayout(dlg)
    root.setContentsMargins(18, 14, 18, 12)
    root.setSpacing(8)

    title = QLabel("快捷键")
    title.setStyleSheet("font-size:17px;font-weight:600;color:%s;" % tcol("text", cfg))
    root.addWidget(title)
    sub = QLabel("游戏全屏时也能直接按，不用切回窗口。要改键位点底部「修改键位」。")
    sub.setStyleSheet("color:%s;font-size:11px;" % tcol("muted", cfg))
    sub.setWordWrap(True)
    root.addWidget(sub)

    find = QLineEdit()
    find.setPlaceholderText("搜动作名或按键，例如：收藏 / Ctrl+Alt+U")
    find.setFixedHeight(28)
    root.addWidget(find)

    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    body = QWidget()
    body.setStyleSheet("background:transparent;")
    bl = QVBoxLayout(body)
    bl.setContentsMargins(0, 0, 6, 0)
    bl.setSpacing(2)
    area.setWidget(body)
    root.addWidget(area, 1)

    rows = []            # (检索文本, 行控件, 所属分组容器)
    group_blocks = []

    for grp in HOTKEY_GROUP_ORDER:
        specs = [s for s in HOTKEY_SPECS if s[3] == grp]
        if not specs:
            continue
        box = QWidget()
        box.setStyleSheet("background:transparent;")
        vl = QVBoxLayout(box)
        vl.setContentsMargins(0, 8, 0, 6)
        vl.setSpacing(4)
        gh = QLabel(grp)
        gh.setStyleSheet("color:%s;font-size:12px;letter-spacing:1px;" % tcol("muted", cfg))
        vl.addWidget(gh)
        hint = HOTKEY_GROUP_HINT.get(grp)
        if hint:
            h = QLabel(hint)
            h.setStyleSheet("color:%s;font-size:10px;" % tcol("muted", cfg))
            vl.addWidget(h)
        for key, label, tip, _g in specs:
            r = QFrame()
            r.setStyleSheet("QFrame{background:%s;border-radius:8px;}" % base.get("card", base["bg"]))
            rl = QHBoxLayout(r)
            rl.setContentsMargins(10, 7, 10, 7)
            rl.setSpacing(10)
            nv = QVBoxLayout()
            nv.setSpacing(1)
            name = QLabel(label)
            name.setStyleSheet("font-size:13px;color:%s;background:transparent;" % tcol("text", cfg))
            desc = QLabel(tip)
            desc.setStyleSheet("color:%s;font-size:10px;background:transparent;" % tcol("muted", cfg))
            desc.setWordWrap(True)
            nv.addWidget(name)
            nv.addWidget(desc)
            rl.addLayout(nv, 1)
            bind = str(cfg.get(key) or "").strip()
            cap = QLabel(bind or "未绑定")
            cap.setAlignment(Qt.AlignCenter)
            cap.setFixedHeight(24)
            cap.setMinimumWidth(96)
            if bind:
                cap.setStyleSheet("background:%s;color:%s;border-radius:6px;padding:2px 8px;"
                                  "font-family:Consolas,monospace;font-size:12px;"
                                  % (base.get("chip_bg", base["card"]), tcol("text", cfg)))
            else:
                cap.setStyleSheet("color:%s;font-size:11px;background:transparent;" % tcol("muted", cfg))
            rl.addWidget(cap)
            vl.addWidget(r)
            rows.append((("%s %s %s %s" % (label, tip, bind, grp)).lower(), r, box))
        group_blocks.append((box, vl))
        bl.addWidget(box)
    bl.addStretch(1)

    def do_filter(text):
        q = str(text or "").strip().lower()
        # 用命中标记决定分组显隐：不能读 row.isVisible()，父容器一隐藏它永远是 False
        hit = {}
        for hay, row, box in rows:
            vis = (not q) or (q in hay)
            row.setVisible(vis)
            if vis:
                hit[id(box)] = True
        for box, _vl in group_blocks:
            box.setVisible(bool(hit.get(id(box))) or not q)

    find.textChanged.connect(do_filter)

    btns = QHBoxLayout()
    btns.setSpacing(8)
    b_edit = QPushButton("修改键位")
    b_edit.setObjectName("primary")
    b_edit.setFixedHeight(30)
    b_reset = QPushButton("恢复推荐键位")
    b_reset.setFixedHeight(30)
    b_close = QPushButton("关闭")
    b_close.setFixedHeight(30)
    btns.addWidget(b_edit)
    btns.addWidget(b_reset)
    btns.addStretch(1)
    btns.addWidget(b_close)
    root.addLayout(btns)

    def go_edit():
        dlg.close()
        try:
            owner.open_settings()
        except Exception as e:
            log("打开设置面板失败：" + repr(e))

    def do_reset():
        for key, _label, _tip, _g in HOTKEY_SPECS:
            if key in DEFAULT_CONFIG:
                cfg[key] = DEFAULT_CONFIG[key]
        cfg["hotkey_custom"] = False
        save_config(cfg)
        for tgt in (owner, getattr(owner, "app", None), getattr(owner, "main", None)):
            fn = getattr(tgt, "reload_hotkeys", None)
            if callable(fn):
                try:
                    fn()
                except Exception as e:
                    log("重新注册热键失败：" + repr(e))
                break
        try:
            owner.set_status("已恢复推荐键位", "ok")
        except Exception:
            pass
        dlg.close()
        pop_hotkey_sheet(owner)

    b_edit.clicked.connect(go_edit)
    b_reset.clicked.connect(do_reset)
    b_close.clicked.connect(dlg.close)

    try:
        owner._hk_sheet = dlg
    except Exception:
        pass
    dlg.show()
    find.setFocus()
    return dlg


def theme_qss(theme):
    c = THEME_COLORS.get(theme, THEME_COLORS["dark"])
    s = theme_style(theme)
    r = s["radius"]
    rs = max(0, r - 3)
    bw = s["card_border"]
    fam = s["font"]
    title = s["title"]
    return (
        "QWidget#root{background:%(bg)s;}"
        "QWidget{background:transparent;color:%(text)s;font-size:12px;"
        "font-family:%(fam)s;}"
        "QFrame#nav{background:%(nav)s;border-right:%(bw)spx solid %(border)s;}"
        "QLabel{color:%(text)s;background:transparent;}"
        "QLabel#title{font-size:%(title)spx;font-weight:700;color:%(text)s;"
        "letter-spacing:0.5px;}"
        "QLabel#desc{color:%(muted)s;font-size:12px;}"
        "QLabel#chip{color:%(muted)s;font-size:11px;background:%(chip_bg)s;"
        "border-radius:%(rs)spx;padding:3px 9px;}"
        "QLineEdit#chip{color:%(muted)s;font-size:11px;background:%(chip_bg)s;"
        "border:none;border-radius:%(rs)spx;padding:3px 9px;}"
        "QFrame#card{background:%(card)s;border:%(bw)spx solid %(border)s;"
        "border-radius:%(r)spx;}"
        "QFrame#soft{background:%(chip_bg)s;border:none;border-radius:%(r)spx;}"
        "QLabel#captionZh{color:%(muted)s;font-size:15px;}"
        "QLabel#captionEn{color:%(text)s;font-size:20px;font-weight:600;}"
        "QLabel#stateText{font-size:13px;font-weight:600;color:%(text)s;}"
        "QLabel#liveSrc{font-size:15px;color:%(muted)s;}"
        "QLabel#liveTgt{font-size:22px;font-weight:600;color:%(text)s;}"
        "QLabel#sectionTitle{font-size:15px;font-weight:600;color:%(text)s;}"
        "QLabel#cardTitle{font-size:13px;font-weight:600;color:%(text)s;"
        "letter-spacing:0.4px;}"
        "QLabel#cardFront{font-size:21px;font-weight:600;color:%(text)s;}"
        "QLabel#cardBack{font-size:16px;color:%(muted)s;}"
        "QLabel#dialogHead{font-size:17px;font-weight:700;color:%(text)s;}"
        "QLabel#dialogSub{font-size:13px;color:%(muted)s;}"
        "QLabel#warnText{color:%(warn)s;}"
        "QLabel#hint{color:%(muted)s;font-size:11px;background:transparent;padding:0 6px;}"
        "QPushButton{background:%(field)s;border:1px solid %(field_border)s;"
        "border-radius:%(r)spx;padding:6px 14px;color:%(text)s;font-size:12px;}"
        "QPushButton:hover{background:%(hover)s;border-color:%(accent)s;}"
        "QPushButton:pressed{background:%(sel)s;border-color:%(accent)s;}"
        "QPushButton:checked{background:%(sel)s;border-color:%(accent)s;color:%(accent)s;}"
        "QPushButton#primary{background:%(accent)s;border:1px solid %(accent)s;"
        "color:%(btn_text)s;font-weight:600;padding:8px 22px;}"
        "QPushButton#primary:hover{background:%(accent_hover)s;}"
        "QPushButton#navbtn{border:none;background:transparent;text-align:left;"
        "padding-left:11px;font-size:16px;font-weight:600;color:%(muted)s;}"
        "QPushButton#navbtn:hover{background:%(hover)s;color:%(text)s;"
        "border-radius:%(r)spx;}"
        "QPushButton#navbtn:checked{background:%(sel)s;color:%(accent)s;"
        "border-left:3px solid %(accent)s;}"
        "QTableWidget{background:%(card)s;border:1px solid %(border)s;border-radius:%(r)spx;"
        "gridline-color:%(grid)s;font-size:12px;color:%(text)s;"
        "selection-background-color:%(sel)s;selection-color:%(text)s;}"
        "QHeaderView::section{background:%(header)s;border:none;"
        "border-bottom:1px solid %(border)s;padding:6px;color:%(muted)s;font-size:11px;}"
        "QCheckBox{color:%(text)s;font-size:12px;background:transparent;}"
        "QComboBox,QLineEdit,QSpinBox,QDoubleSpinBox,QKeySequenceEdit{"
        "background:%(field)s;border:1px solid %(field_border)s;border-radius:%(r)spx;"
        "padding:5px;font-size:12px;color:%(text)s;}"
        "QComboBox:hover,QLineEdit:hover{border-color:%(accent)s;}"
        "QComboBox QAbstractItemView{background:%(card)s;color:%(text)s;"
        "selection-background-color:%(sel)s;border:1px solid %(border)s;}"
        "QTabWidget::pane{border:none;background:transparent;}"
        "QTabBar::tab{background:transparent;border:none;padding:7px 14px;"
        "color:%(muted)s;font-size:13px;}"
        "QTabBar::tab:selected{color:%(text)s;font-weight:600;"
        "border-bottom:2px solid %(accent)s;}"
        "QScrollArea{border:none;background:transparent;}"
        "QScrollBar:vertical{background:transparent;width:10px;}"
        "QScrollBar::handle:vertical{background:%(border)s;border-radius:%(rs)spx;}"
        "QMenu{background:%(card)s;color:%(text)s;border:1px solid %(border)s;"
        "border-radius:%(r)spx;padding:4px;}"
        "QMenu::item{padding:6px 18px;border-radius:%(rs)spx;}"
        "QMenu::item:selected{background:%(sel)s;}"
        "QStatusBar{background:%(bg)s;}"
    ) % dict(c, r=r, rs=rs, bw=bw, fam=fam, title=title,
             btn_text=c.get("btn_text", "#FFFFFF"))


class MainWindow:
    """客户端主界面：状态 / 当前字幕 / 历史记录 / 导出 / 托盘"""

    def _restore_geometry(self):
        """上次关掉时窗口在哪、多大，这次就还放哪。位置跑到屏幕外（拔了副屏）就退回默认。"""
        try:
            from PySide6.QtGui import QGuiApplication
            from PySide6.QtCore import QRect
            saved = self.cfg.get("main_win") or {}
            w = int(saved.get("w") or 940)
            h = int(saved.get("h") or 660)
            x, y = saved.get("x"), saved.get("y")
            if x is None or y is None:
                return
            rect = QRect(int(x), int(y), w + int(saved.get("fx") or 16), h + int(saved.get("fy") or 40))
            for scr in QGuiApplication.screens():
                inter = scr.availableGeometry().intersected(rect)
                if inter.width() >= 240 and inter.height() >= 120:   # 还算露得出来
                    avail = scr.availableGeometry()
                    w = min(w, avail.width())
                    h = min(h, avail.height())
                    x = min(max(int(x), avail.left()), avail.right() - w + 1)
                    y = min(max(int(y), avail.top()), avail.bottom() - h + 1)
                    self.win.resize(w, h)
                    self.win.move(int(x), int(y))       # 存的是外框左上角，move 也按外框摆
                    return
        except Exception:
            pass

    def _remember_geometry(self):
        """位置/大小变了就落盘（每 2 秒检查一次，没变不写）。存的是外框，跟 Windows 拖窗口时看到的一致。"""
        try:
            fg = self.win.frameGeometry()
            geo = self.win.geometry()
            # 标题条尺寸（约 160×28）不是用户的真实选择，别把它写进配置污染下次启动
            if geo.width() < 600 or geo.height() < 400:
                return
            now = {"x": fg.x(), "y": fg.y(), "w": geo.width(), "h": geo.height(),
                   "fx": fg.width() - geo.width(), "fy": fg.height() - geo.height()}
            if now != (self.cfg.get("main_win") or {}):
                self.cfg["main_win"] = now
                save_config(self.cfg)
            tab = self.tabs.currentIndex()
            if tab != int(self.cfg.get("last_tab") or 0):
                self.cfg["last_tab"] = tab
                save_config(self.cfg)
        except Exception:
            pass

    def _start_geo_timer(self):
        try:
            from PySide6.QtCore import QTimer
            self._geo_timer = QTimer(self.win)
            self._geo_timer.setInterval(2000)
            self._geo_timer.timeout.connect(self._remember_geometry)
            self._geo_timer.start()
        except Exception:
            pass

    def _perf_guard(self):
        """CPU 持续偏高：自动关闭朗读 / 切片 / 精细识别，先保住基础链路。"""
        p = cpu_load_percent()
        if p >= 85.0:
            self._cpu_high = getattr(self, "_cpu_high", 0) + 1
        else:
            self._cpu_high = 0
        if self._cpu_high >= 2 and not getattr(self, "_cpu_degraded", False):
            self._cpu_degraded = True
            self.cfg["auto_read"] = False
            self.cfg["save_audio"] = False
            self.cfg["asr_quality"] = "fast"
            save_config(self.cfg)
            try:
                self.owner.overlay.chk_auto.blockSignals(True)
                self.owner.overlay.chk_auto.setChecked(False)
                self.owner.overlay.chk_auto.blockSignals(False)
                self.owner.overlay.cfg["auto_read"] = False
            except Exception:
                pass
            self.set_status("CPU 偏高（%.0f%%），已自动降级：关朗读 / 切片，改用快速识别" % p, "warn")
            log("CPU 自动降级：%.1f%%" % p)

    def _do_backup(self):
        if backup_data():
            log("素材已增量备份到 backup/")

    def __init__(self, owner, cfg):
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtGui import QAction, QFont, QIcon
        from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                                       QFrame, QTableWidget, QTableWidgetItem, QHeaderView,
                                       QCheckBox, QFileDialog, QMessageBox, QSystemTrayIcon,
                                       QMenu, QAbstractItemView, QTabWidget, QComboBox, QLineEdit,
                                       QStackedWidget, QRadioButton, QInputDialog,
                                       QSlider, QSpinBox, QDoubleSpinBox)

        self.owner = owner
        self.cfg = cfg
        self._max_rows = int(cfg.get("keep_rows") or 300)
        self._filtered = False
        self._fav_keys = set()

        self.win = QWidget()
        self.win.setWindowTitle("Oversub 沉浸字幕")
        self.win.resize(940, 660)
        # 最小尺寸兜底：某些启动路径下布局尚未参与计算，窗口会被压成 160×28 标题条
        self.win.setMinimumSize(900, 600)
        self._restore_geometry()
        self.win.setStyleSheet(theme_qss(str(self.cfg.get("theme") or "dark")))
        self.win.setObjectName("root")

        # 左侧固定窄导航（48px 图标模式，hover 展开文字）
        class _NavBtn(QPushButton):
            def __init__(self, glyph, full, parent=None):
                super().__init__(glyph, parent)
                self.glyph = glyph
                self.full = full
                self.setFixedSize(40, 40)
                self.setCursor(Qt.PointingHandCursor)
                self.setToolTip(full)
                self.setObjectName("navbtn")

            # 不在悬停时展开侧栏：鼠标划过就变宽会让整个界面跟着移位。
            # 展开改由用户显式点击左上角的 ≡ 控制。
            def set_expanded(self, on):
                self.setText(self.full if on else self.glyph)
                # 侧栏内容左右各 6px margin，按钮宽 = 侧栏宽 - 12
                self.setFixedWidth(156 if on else 40)

        outer = QHBoxLayout(self.win)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        nav = QFrame()
        nav.setObjectName("nav")
        nav.setFixedWidth(52)
        # 主题由全局 QSS 控制，不再写死浅色
        nvl = QVBoxLayout(nav)
        nvl.setContentsMargins(6, 14, 6, 14)
        nvl.setSpacing(6)
        self.nav_frame = nav
        self.nav_expanded = False
        self.btn_nav_toggle = QPushButton("≡")
        self.btn_nav_toggle.setObjectName("navbtn")
        self.btn_nav_toggle.setFixedSize(40, 40)
        self.btn_nav_toggle.setCursor(Qt.PointingHandCursor)
        self.btn_nav_toggle.setToolTip("展开 / 收起侧栏")
        self.btn_nav_toggle.clicked.connect(self._toggle_nav)
        nvl.addWidget(self.btn_nav_toggle)
        nvl.addSpacing(4)
        self.nav_buttons = []
        for glyph, full, prefix in (("捕", "Capture 捕获台", "Capture"),
                                    ("库", "Library 语言库", "Library"),
                                    ("说", "SceneTalk 情景译", "Talk"),
                                    ("验", "Lab 实验室", "Lab")):
            b = _NavBtn(glyph, full, nav)
            b.setCheckable(True)
            b.setProperty("grp", prefix)
            b.clicked.connect(lambda _c=False, p=prefix: self._nav_go(p))
            nvl.addWidget(b)
            self.nav_buttons.append(b)
        nvl.addStretch(1)

        right = QWidget()
        right.setObjectName("root")
        root = QVBoxLayout(right)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)
        outer.addWidget(nav)
        outer.addWidget(right, 1)

        # 顶部：只保留标题、一处会话 chip、三个主操作
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        # 品牌露出：英文名独立成标题，中文名做副标，一眼能对上是哪个产品
        brand = QHBoxLayout()
        brand.setSpacing(8)
        t = QLabel("Oversub")
        t.setObjectName("title")
        t.setToolTip("Oversub｜沉浸字幕：抓系统播放的声音，实时把听到的话变成屏幕上的对照字幕")
        b2 = QLabel("沉浸字幕")
        b2.setObjectName("desc")
        b2.setAlignment(Qt.AlignBottom)
        b2.setContentsMargins(0, 0, 0, 3)
        brand.addWidget(t)
        brand.addWidget(b2)
        brand.addStretch(1)
        titles.addLayout(brand)
        d = QLabel("")
        d.setObjectName("desc")
        d.hide()
        head.addLayout(titles, 1)
        # 状态就贴在标题右边，不再单独占一整张状态卡
        self.head_state = QHBoxLayout()
        self.head_state.setSpacing(6)
        head.addLayout(self.head_state)

        self.top_session = QLabel("会话：未开始")
        self.top_session.setObjectName("chip")
        self.top_pair = QLabel("中文 → 英语")
        self.top_pair.setObjectName("chip")
        self.top_hint = QLabel("%s 交互｜%s 素材库｜%s 自测" % (
            cfg.get("hotkey_toggle") or "Ctrl+Alt+G",
            cfg.get("hotkey_library") or "Ctrl+Alt+L",
            cfg.get("hotkey_selftest") or "Ctrl+Alt+T"))
        self.top_hint.setObjectName("chip")
        # 会话名已经在捕获区有一处可编辑的入口，顶栏不再重复
        self.top_session.hide()
        self.top_pair.hide()
        self.top_hint.hide()
        head.addSpacing(8)

        self.btn_pin = QPushButton("置顶")
        self.btn_pin.setCheckable(True)
        self.btn_help = QPushButton("帮助")
        self.btn_settings = QPushButton("设置")
        self.btn_batch = QPushButton("批量操作")
        self.btn_export = QPushButton("导出学习记录")
        self.btn_log = QPushButton("运行日志")
        for b in (self.btn_pin, self.btn_help, self.btn_settings):
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(30)
            b.hide()                     # 低频操作全部收进「更多」，顶栏只留一个入口

        # 低频入口收进「更多」，不再和主操作挤在同一行
        self.btn_more = QPushButton("更多")
        self.btn_more.setCursor(Qt.PointingHandCursor)
        self.btn_more.setFixedHeight(30)
        more_menu = QMenu(self.btn_more)
        self.more_menu = more_menu
        for b, label in ((self.btn_batch, "批量操作"),
                         (self.btn_export, "导出学习记录"),
                         (self.btn_log, "运行日志")):
            b.hide()
            more_menu.addAction(label, b.click)
        more_menu.addSeparator()
        act_pin = more_menu.addAction("窗口置顶")
        act_pin.setCheckable(True)
        act_pin.toggled.connect(self.btn_pin.setChecked)
        self.btn_pin.toggled.connect(act_pin.setChecked)
        more_menu.addAction("帮助", self.btn_help.click)
        more_menu.addAction("设置", self.btn_settings.click)
        # 主题不再只能进 Lab 换：更多菜单里直接给一份，随手切、立即生效
        more_menu.addSeparator()
        theme_menu = more_menu.addMenu("界面主题")
        self._theme_actions = {}
        for key in THEME_ORDER:
            act = theme_menu.addAction(THEME_LABELS.get(key, key))
            act.setCheckable(True)
            act.setChecked(str(self.cfg.get("theme") or "dark") == key)
            act.triggered.connect(lambda _c=False, k=key: self._quick_theme(k))
            self._theme_actions[key] = act
        self.btn_more.setMenu(more_menu)
        head.addWidget(self.btn_more)
        # 风格入口放在顶栏，不用进设置找；切换即时生效
        head.addSpacing(8)
        self.head_theme = QComboBox()
        self.head_theme.setFixedHeight(30)
        self.head_theme.setMinimumWidth(118)
        self.head_theme.setToolTip("界面风格：换的是圆角、边线、字号和配色，不只是换颜色")
        for key in THEME_ORDER:
            self.head_theme.addItem(THEME_LABELS[key], key)
        try:
            self.head_theme.setCurrentIndex(max(0, THEME_ORDER.index(str(self.cfg.get("theme") or "dark"))))
        except Exception:
            self.head_theme.setCurrentIndex(0)
        self.head_theme.currentIndexChanged.connect(self._on_head_theme)
        head.addWidget(self.head_theme)
        # 快捷键以前埋在设置长表里，入口提到顶栏：看一眼就知道有哪些、在哪改
        self.btn_hk = QPushButton("快捷键")
        self.btn_hk.setCursor(Qt.PointingHandCursor)
        self.btn_hk.setFixedHeight(30)
        self.btn_hk.setToolTip("查看全部 %d 个快捷键，可搜索，也能直接改键位" % len(HOTKEY_SPECS))
        self.btn_hk.clicked.connect(self.show_hotkeys)
        head.addWidget(self.btn_hk)
        root.addLayout(head)
        self.btn_pin.toggled.connect(self.toggle_pin)
        self.btn_help.clicked.connect(self.show_help)

        # 状态：只留圆点 + 一句状态，贴在标题行
        self.dot = QLabel("●")
        self.dot.setStyleSheet("color:%s;font-size:10px;" % tcol("muted", self.cfg))
        self.status = QLabel("准备中…")
        self.status.setStyleSheet("color:%s;font-size:12px;" % tcol("muted", self.cfg))
        self.head_state.addWidget(self.dot)
        self.head_state.addWidget(self.status)
        # 真正要看的「识别中 / 已暂停」在控制行，顶栏只留标题和「更多」，不再挂一个孤立圆点
        self.dot.hide()
        self.status.hide()
        # 引擎/ASR/翻译/设备这些技术细节收进状态文字的 tooltip，不再常驻占位
        self.chip_engine = QLabel("引擎：离线")
        self.chip_asr = QLabel("ASR：-")
        self.chip_trans = QLabel("翻译：-")
        self.chip_dev = QLabel("设备：-")
        for c in (self.chip_engine, self.chip_asr, self.chip_trans, self.chip_dev):
            c.setObjectName("chip")
            c.hide()
        # 目标语种选择器：默认英语，后续语种直接追加在这里；下放到捕获控制行
        self.lang_box = QComboBox()
        self.lang_box.setMinimumWidth(140)
        self.lang_box.setToolTip("目标语种：切换后悬浮字幕、记录、收藏、生词本统一使用该语种")
        self._fill_lang_box()

        # 当前字幕：一张卡说清「这一句 + 当前状态 + 三个动作」
        cap = QFrame()
        cap.setObjectName("card")
        cv = QVBoxLayout(cap)
        cv.setContentsMargins(18, 12, 18, 14)
        cv.setSpacing(7)

        meta = QHBoxLayout()
        meta.setSpacing(8)
        self.lbl_session = QLineEdit("本会话 · 未开始")
        self.lbl_session.setObjectName("chip")
        self.lbl_session.setFrame(False)
        self.lbl_session.setFixedWidth(220)
        self.lbl_session.setToolTip("点一下直接改会话名，回车生效（不弹窗）")
        self.lbl_session.editingFinished.connect(self._rename_session)
        meta.addWidget(self.lbl_session)
        meta.addStretch(1)
        self.live_state = QLabel("就绪")
        self.live_state.setObjectName("chip")
        meta.addWidget(self.live_state)
        self.btn_live_speak = QPushButton("朗读")
        self.btn_live_tag = QPushButton("标签")
        self.btn_live_fav = QPushButton("☆ 收藏")
        for b in (self.btn_live_speak, self.btn_live_tag, self.btn_live_fav):
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(26)
            b.setStyleSheet("font-size:12px;padding:4px 12px;")
            meta.addWidget(b)
        cv.addLayout(meta)

        # 译文行改成可点词：单击某个词直接查义并存生词，双击仍走整句拆词
        from PySide6.QtWidgets import QTextBrowser

        class _WordText(QTextBrowser):
            def __init__(self, init_text="", parent=None):
                super().__init__(parent)
                self._raw = str(init_text or "")
                self._color = ""
                self.on_word = None
                self.setOpenLinks(False)
                self.setFrameShape(QFrame.NoFrame)
                self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
                self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
                self.setStyleSheet("background:transparent;border:none;")
                self.anchorClicked.connect(self._hit)
                self.document().documentLayout().documentSizeChanged.connect(self._fit)
                super().setText(self._markup(self._raw))

            def _hit(self, url):
                w = url.toString().strip()
                if w and callable(self.on_word):
                    self.on_word(w)

            def set_text(self, s, color=""):
                self._raw = str(s or "")
                if color:
                    self._color = color
                super().setText(self._markup(self._raw))
                self._fit()

            def setText(self, s):          # 兼容原 QLabel 的调用方式
                self.set_text(s)

            def text(self):                # 外部仍按原句读取
                return self._raw

            def _markup(self, s):
                import html as _html
                import re as _re
                c = self._color or "inherit"
                parts, pos = [], 0
                for m in _re.finditer(r"[A-Za-z][A-Za-z'’-]*", s):
                    parts.append(_html.escape(s[pos:m.start()]).replace(" ", "&nbsp;"))
                    w = m.group(0)
                    parts.append('<a href="%s" style="color:%s;text-decoration:none">%s</a>'
                                 % (_html.escape(w, quote=True), c, _html.escape(w)))
                    pos = m.end()
                parts.append(_html.escape(s[pos:]).replace(" ", "&nbsp;"))
                return '<span style="white-space:pre-wrap">%s</span>' % "".join(parts)

            def _fit(self, *a):
                try:
                    w = self.viewport().width()
                    if w <= 0:
                        return
                    self.document().setTextWidth(w)
                    h = max(28, int(self.document().size().height()) + 8)
                    if abs(self.height() - h) > 2:
                        self.setFixedHeight(h)
                except Exception:
                    pass

            def resizeEvent(self, ev):
                super().resizeEvent(ev)
                self._fit()

        self.cap_zh = QLabel("等待音频")
        self.cap_zh.setObjectName("captionZh")
        self.cap_zh.setWordWrap(True)
        self.cap_zh.setToolTip("双击：查这个意思下收集过的所有目标语言说法")
        self.cap_en = _WordText("选择音频源与语种，点「开始捕获」")
        self.cap_en.setObjectName("captionEn")
        self.cap_en.setStyleSheet("background:transparent;border:none;color:%s;"
                                  % tcol("text", owner.cfg))
        self.cap_en.setToolTip("单击单词：查义并存生词 ｜ 双击：拆整句")
        self.cap_en.on_word = lambda w: getattr(
            self, "_on_word_clicked", lambda _x: None)(w)
        self.cap_en._color = tcol("text", owner.cfg)
        cv.addWidget(self.cap_zh)
        cv.addWidget(self.cap_en)
        # 旧的「收藏这句」保留对象供状态刷新使用，但不再占界面
        self.btn_fav_line = QPushButton("☆ 收藏这句")
        self.btn_fav_line.hide()
        self.cap_frame = cap
        root.addWidget(cap)

        # 记录 / 生词本 两个页
        self.tabs = QTabWidget()

        rec_tab = QWidget()
        rv = QVBoxLayout(rec_tab)
        rv.setContentsMargins(0, 6, 0, 0)
        rv.setSpacing(10)

        # ---- 捕获控制组：主按钮 + 语种 + 悬浮字幕 + 历史，一条搞定
        self.btn_quick_cfg = QPushButton("快速配置")
        self.btn_quick_cfg.clicked.connect(owner.open_settings)
        self.btn_quick_cfg.hide()
        try:
            self.more_menu.addAction("快速配置", self.btn_quick_cfg.click)
        except Exception:
            pass
        self.btn_hist_toggle = QPushButton("展开历史 ▾")
        self.btn_hist_toggle.setCursor(Qt.PointingHandCursor)
        self.btn_hist_toggle.setFixedHeight(30)

        ctl = QHBoxLayout()
        ctl.setSpacing(8)
        self.btn_capture = QPushButton("开始捕获")
        self.btn_capture.setObjectName("primary")
        self.btn_capture.setCursor(Qt.PointingHandCursor)
        self.btn_capture.setFixedHeight(34)
        self.btn_capture.clicked.connect(self._on_capture_btn)
        ctl.addWidget(self.btn_capture)
        # 源/译文只随句更新，供朗读、双击查词等逻辑读取，不在界面上重复显示
        self.live_src = QLabel("源：等待声音…")
        self.live_src.hide()
        self.live_tgt = QLabel("译：—")
        self.live_tgt.hide()
        self.lbl_src_lang = QLabel("源语种：中文")
        self.lbl_src_lang.hide()
        self.lbl_tgt_lang = QLabel("目标语种：英语")
        self.lbl_tgt_lang.hide()
        self.lbl_audio_dev = QLabel("音频源：系统默认")
        self.lbl_audio_dev.hide()
        ctl.addStretch(1)
        # 常驻状态徽章：引擎 / 音频设备 / 信号一眼可见，不用去 tooltip 里翻
        self.badge = QLabel("○ 未开始")
        self.badge.setCursor(Qt.PointingHandCursor)
        self.badge.setToolTip("点一下打开 Lab 调整")
        self.badge.mousePressEvent = lambda _e: owner.open_settings()
        ctl.addWidget(self.badge)
        arrow = QLabel("中文 →")
        arrow.setStyleSheet("color:%s;font-size:12px;" % tcol("muted", self.cfg))
        ctl.addWidget(arrow)
        ctl.addWidget(self.lang_box)
        self.chk_overlay = QCheckBox("悬浮字幕")
        self.chk_overlay.setChecked(bool(cfg.get("show_overlay", True)))
        self.chk_overlay.toggled.connect(self.on_overlay_toggle)
        ctl.addWidget(self.chk_overlay)
        ctl.addWidget(self.btn_hist_toggle)
        rv.addLayout(ctl)

        # ---- 外观快捷条：皮肤 / 位置 / 字号 / 快捷键，全部放在首页第一屏 ----
        # 这些以前只埋在 Lab 里，用户得先知道「实验室」在哪才找得到；挪到首页后不用翻菜单。
        look = QHBoxLayout()
        look.setSpacing(8)
        look.addWidget(QLabel("字幕皮肤"))
        self.ov_skin = QComboBox()
        for _k, _v in OVERLAY_SKINS.items():
            self.ov_skin.addItem(_v.get("name") or _k, _k)
        _cur_sk = str(cfg.get("overlay_skin") or "dialog_dark")
        for _i in range(self.ov_skin.count()):
            if self.ov_skin.itemData(_i) == _cur_sk:
                self.ov_skin.setCurrentIndex(_i)
        self.ov_skin.setMinimumWidth(190)
        self.ov_skin.setToolTip("换字幕框的外观：底色 / 描边 / 圆角 / 文字颜色，立即生效")
        self.ov_skin.currentIndexChanged.connect(self._on_skin_chip)
        look.addWidget(self.ov_skin)

        self.ov_pos = QComboBox()
        for _t, _v in (("顶部", "top"), ("底部", "bottom"),
                       ("底部偏上", "bottom_high"), ("自由位置", "free")):
            self.ov_pos.addItem(_t, _v)
        _cur_pos = str(cfg.get("overlay_pos") or "top")
        for _i in range(self.ov_pos.count()):
            if self.ov_pos.itemData(_i) == _cur_pos:
                self.ov_pos.setCurrentIndex(_i)
        self.ov_pos.setToolTip("字幕条贴在屏幕哪里")
        self.ov_pos.currentIndexChanged.connect(self._on_pos_chip)
        look.addWidget(self.ov_pos)

        self.btn_fs_down = QPushButton("A−")
        self.btn_fs_down.setFixedHeight(28)
        self.btn_fs_down.setFixedWidth(40)
        self.btn_fs_down.setToolTip("字幕字号减一档")
        self.btn_fs_down.clicked.connect(lambda: self._font_step(-1))
        look.addWidget(self.btn_fs_down)
        self.lbl_fs = QLabel("字号 %d" % int(cfg.get("font_size") or 22))
        self.lbl_fs.setObjectName("hint")
        look.addWidget(self.lbl_fs)
        self.btn_fs_up = QPushButton("A+")
        self.btn_fs_up.setFixedHeight(28)
        self.btn_fs_up.setFixedWidth(40)
        self.btn_fs_up.setToolTip("字幕字号加一档")
        self.btn_fs_up.clicked.connect(lambda: self._font_step(1))
        look.addWidget(self.btn_fs_up)

        self.btn_keys_quick = QPushButton("快捷键一览")
        self.btn_keys_quick.setFixedHeight(28)
        self.btn_keys_quick.setToolTip("所有键位都在这里，底部能直接改")
        self.btn_keys_quick.clicked.connect(lambda: pop_hotkey_sheet(self.owner))
        look.addWidget(self.btn_keys_quick)
        look.addStretch(1)
        rv.addLayout(look)

        # ---- 历史抽屉默认收起；空出来的位置放最近三句，避免一屏空白 ----
        self.btn_hist_toggle.clicked.connect(self._toggle_hist_drawer)
        self.cap_filler = QFrame()
        self.cap_filler.setObjectName("soft")
        _cf = QVBoxLayout(self.cap_filler)
        _cf.setContentsMargins(18, 12, 18, 12)
        _cf.setSpacing(7)
        rhead = QHBoxLayout()
        rt = QLabel("最近三句")
        rt.setObjectName("cardTitle")
        rhead.addWidget(rt)
        rhead.addStretch(1)
        rh = QLabel("只看刚刚，完整记录点「展开历史」")
        rh.setObjectName("hint")
        rhead.addWidget(rh)
        _cf.addLayout(rhead)
        self.recent_rows = []
        for _i in range(3):
            rw = QWidget()
            rl = QVBoxLayout(rw)
            rl.setContentsMargins(0, 2, 0, 2)
            rl.setSpacing(1)
            z = QLabel("")
            z.setObjectName("hint")
            e = QLabel("")
            e.setStyleSheet("font-size:13px;font-weight:600;")
            rl.addWidget(z)
            rl.addWidget(e)
            _cf.addWidget(rw)
            self.recent_rows.append((rw, z, e))
        self.cap_empty_hint = QLabel("还没有句子，开始捕获后会在这里显示最近三句")
        self.cap_empty_hint.setObjectName("hint")
        self.cap_empty_hint.setAlignment(Qt.AlignCenter)
        _cf.addWidget(self.cap_empty_hint)
        _cf.addStretch(1)
        self.recent_footer = QLabel("Ctrl+Alt+G 交互　Ctrl+Alt+H 历史　Ctrl+Alt+V 字幕开关")
        self.recent_footer.setObjectName("hint")
        self.recent_footer.setAlignment(Qt.AlignRight)
        _cf.addWidget(self.recent_footer)
        rv.addWidget(self.cap_filler, 0)
        rv.addStretch(1)

        self.hist_box = QWidget()
        hv = QVBoxLayout(self.hist_box)
        hv.setContentsMargins(0, 0, 0, 0)
        hv.setSpacing(8)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("查看"))
        self.day_box = QComboBox()
        self.day_box.setMinimumWidth(130)
        bar.addWidget(self.day_box)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜源文本或目标译文（任意语种）…")
        self.search.setClearButtonEnabled(True)
        bar.addWidget(self.search, 1)
        self.btn_hist_filter = QPushButton("筛选")
        self.btn_hist_filter.setCheckable(True)
        self.btn_hist_filter.setFixedHeight(28)
        self.btn_hist_filter.setToolTip("场景、反向自测、质量统计")
        bar.addWidget(self.btn_hist_filter)
        hv.addLayout(bar)

        # 场景标签筛选 + 反向自测 + 本次会话质量统计（默认收起）
        self.hist_filter_wrap = QWidget()
        fbar = QHBoxLayout(self.hist_filter_wrap)
        fbar.setContentsMargins(0, 0, 0, 0)
        fbar.addWidget(QLabel("场景筛选"))
        self.scene_boxes = {}
        for key, label in (("fps", "FPS / 游戏"), ("video", "看视频"), ("chat", "语音聊天")):
            cb = QCheckBox(label)
            cb.toggled.connect(self._on_scene_filter)
            self.scene_boxes[key] = cb
            fbar.addWidget(cb)
        self.chk_selftest = QCheckBox("反向自测（隐藏目标译文列）")
        self.chk_selftest.toggled.connect(self.set_selftest)
        fbar.addWidget(self.chk_selftest)
        fbar.addStretch(1)
        self.rec_stats = QLabel("")
        self.rec_stats.setObjectName("chip")
        fbar.addWidget(self.rec_stats)
        hv.addWidget(self.hist_filter_wrap)
        self.hist_filter_wrap.hide()
        self.btn_hist_filter.toggled.connect(self.hist_filter_wrap.setVisible)

        # 历史表：以「识别出来的中文」为锚点，英文是翻译输出
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["时间", "说话人", "识别原文（中文）", "目标译文", "场景", "质量", "收藏", "听"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setColumnWidth(0, 66)
        self.table.setColumnWidth(1, 92)
        self.table.setColumnWidth(2, 280)
        self.table.setColumnWidth(3, 280)
        self.table.setColumnWidth(4, 82)
        self.table.setColumnWidth(5, 84)
        self.table.setColumnWidth(6, 44)
        self.table.setColumnWidth(7, 40)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(True)
        self.table.verticalHeader().setDefaultSectionSize(52)
        self.refresh_lang_headers()
        self.table.cellClicked.connect(self.on_row_star)
        hv.addWidget(self.table, 1)
        self.hist_box.setVisible(False)
        rv.addWidget(self.hist_box, 2)
        self.tabs.addTab(rec_tab, "Capture｜捕获台")

        # ---------------- Library 语言素材库（资产中心） ----------------
        lib_tab = QWidget()
        lbv = QVBoxLayout(lib_tab)
        lbv.setContentsMargins(0, 6, 0, 0)
        lbv.setSpacing(8)

        lrow = QHBoxLayout()
        lrow.addWidget(QLabel("视图"))
        self.lib_view = QComboBox()
        self.lib_view.addItems(["时序", "卡片复习", "语义聚类", "会话", "说话人"])
        lrow.addWidget(self.lib_view)
        self.lib_search = QLineEdit()
        self.lib_search.setPlaceholderText("搜源文本 / 目标译文 / 标签…")
        self.lib_search.setClearButtonEnabled(True)
        lrow.addWidget(self.lib_search, 1)
        self.lib_due = QLabel("待复习 0")
        self.lib_due.setObjectName("chip")
        lrow.addWidget(self.lib_due)
        self.btn_lib_filter = QPushButton("筛选")
        self.btn_lib_filter.setCheckable(True)
        self.btn_lib_filter.setCursor(Qt.PointingHandCursor)
        self.btn_lib_filter.setFixedHeight(28)
        self.btn_lib_filter.setToolTip("状态、质量、收藏、原声、未朗读、导入")
        lrow.addWidget(self.btn_lib_filter)
        self.btn_lib_refresh = QPushButton("刷新")
        self.btn_lib_refresh.setCursor(Qt.PointingHandCursor)
        self.btn_lib_refresh.setFixedHeight(28)
        lrow.addWidget(self.btn_lib_refresh)
        lbv.addLayout(lrow)

        lfilter = QHBoxLayout()
        self.lib_pair = QComboBox()
        self.lib_pair.setToolTip("语种对筛选：中文→英语、中文→日语互不混")
        lfilter.addWidget(self.lib_pair)
        self.lib_scene = QComboBox()
        lfilter.addWidget(self.lib_scene)
        self.lib_state = QComboBox()
        self.lib_state.addItems(["全部状态", "新", "待复习", "已掌握"])
        lfilter.addWidget(self.lib_state)
        self.lib_quality = QComboBox()
        self.lib_quality.addItems(["全部质量", "✅地道", "⚠️机器感", "❌识别错误"])
        lfilter.addWidget(self.lib_quality)
        self.lib_chk_fav = QCheckBox("仅收藏")
        lfilter.addWidget(self.lib_chk_fav)
        self.lib_chk_audio = QCheckBox("仅带原声")
        lfilter.addWidget(self.lib_chk_audio)
        self.lib_chk_unread = QCheckBox("仅未朗读")
        lfilter.addWidget(self.lib_chk_unread)
        self.btn_lib_import = QPushButton("导入 CSV / JSONL")
        self.btn_lib_import.setCursor(Qt.PointingHandCursor)
        self.btn_lib_import.setFixedHeight(28)
        self.btn_lib_import.clicked.connect(self.lib_import)
        lfilter.addWidget(self.btn_lib_import)
        lfilter.addStretch(1)
        lbv.addLayout(lfilter)
        # 高级筛选默认收起：只留语种对、场景两个常用条件
        self._lib_adv_filters = [self.lib_state, self.lib_quality, self.lib_chk_fav,
                                 self.lib_chk_audio, self.lib_chk_unread, self.btn_lib_import]
        for w in self._lib_adv_filters:
            w.hide()
        self.btn_lib_filter.toggled.connect(
            lambda on: [w.setVisible(bool(on)) for w in self._lib_adv_filters])

        self.lib_stack = QStackedWidget()
        # 1) 时序
        self.lib_time = QTableWidget(0, 5)
        self.lib_time.setHorizontalHeaderLabels(["时间", "源文本", "目标译文", "说话人", "记忆状态"])
        self.lib_time.verticalHeader().setVisible(False)
        self.lib_time.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.lib_time.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.lib_time.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.lib_time.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.lib_time.setWordWrap(True)

        # 2) 卡片复习
        cardp = QWidget()
        cv2 = QVBoxLayout(cardp)
        cv2.setContentsMargins(0, 0, 0, 0)
        cv2.setSpacing(8)
        crow = QHBoxLayout()
        self.lib_mode_out = QRadioButton("输出模式（看源文本，回忆目标语言）")
        self.lib_mode_in = QRadioButton("理解模式（看译文，回忆源文本）")
        self.lib_mode_out.setChecked(True)
        crow.addWidget(self.lib_mode_out)
        crow.addWidget(self.lib_mode_in)
        crow.addStretch(1)
        cv2.addLayout(crow)
        lcard = QFrame()
        lcard.setObjectName("card")
        ccl = QVBoxLayout(lcard)
        ccl.setContentsMargins(18, 14, 18, 14)
        ccl.setSpacing(8)
        self.lib_card_meta = QLabel("")
        self.lib_card_meta.setObjectName("chip")
        ccl.addWidget(self.lib_card_meta)
        self.lib_card_front = QLabel("")
        self.lib_card_front.setWordWrap(True)
        self.lib_card_front.setObjectName("cardFront")
        ccl.addWidget(self.lib_card_front)
        self.lib_card_back = QLabel("")
        self.lib_card_back.setWordWrap(True)
        self.lib_card_back.setObjectName("cardBack")
        ccl.addWidget(self.lib_card_back)
        self.lib_card_ctx = QLabel("")
        self.lib_card_ctx.setWordWrap(True)
        self.lib_card_ctx.setObjectName("chip")
        ccl.addWidget(self.lib_card_ctx)
        self.lib_card_prag = QLabel("")
        self.lib_card_prag.setWordWrap(True)
        self.lib_card_prag.setObjectName("chip")
        self.lib_card_prag.hide()          # 语用提示默认折叠
        ccl.addWidget(self.lib_card_prag)
        self.lib_card_ctx.hide()           # 上下文默认折叠，需要时再展开
        ccl.addStretch(1)
        cv2.addWidget(lcard, 1)
        cbtn = QHBoxLayout()
        lbl_nav_prev = QPushButton("上一张")
        lbl_nav_next = QPushButton("下一张")
        btn_reveal = QPushButton("显示答案")
        btn_dictation = QPushButton("听写模式")
        btn_known = QPushButton("标记掌握")
        btn_again = QPushButton("忘了，再练")
        btn_read = QPushButton("播放原声 / 朗读")
        btn_word = QPushButton("加入生词本")
        btn_syn = QPushButton("补充同义")
        btn_del = QPushButton("删除本条")
        btn_ctx = QPushButton("语境回放")
        btn_exp = QPushButton("导出 / 导入")
        btn_more = QPushButton("展开上下文")
        # 复习卡片只留六个主动作，其余收进「更多」
        for b in (lbl_nav_prev, lbl_nav_next, btn_reveal, btn_dictation, btn_known, btn_again):
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(30)
            cbtn.addWidget(b)
        self._lib_aux_btns = [btn_read, btn_word, btn_syn, btn_del, btn_ctx, btn_exp, btn_more]
        for b in self._lib_aux_btns:
            b.setParent(lcard)
            b.hide()
        btn_card_more = QPushButton("更多 ▾")
        btn_card_more.setCursor(Qt.PointingHandCursor)
        btn_card_more.setFixedHeight(30)
        card_menu = QMenu(btn_card_more)
        for label, b in (("播放原声 / 朗读", btn_read), ("加入生词本", btn_word),
                         ("补充同义", btn_syn), ("语境回放", btn_ctx),
                         ("展开上下文", btn_more), ("导出 / 导入", btn_exp),
                         ("删除本条", btn_del)):
            card_menu.addAction(label, b.click)
        btn_card_more.setMenu(card_menu)
        cbtn.addWidget(btn_card_more)
        cbtn.addStretch(1)
        cv2.addLayout(cbtn)
        self._lib_reveal_btn = btn_reveal
        self._lib_prev_btn = lbl_nav_prev
        self._lib_next_btn = lbl_nav_next
        self._lib_known_btn = btn_known
        self._lib_again_btn = btn_again
        btn_reveal.clicked.connect(self.lib_reveal)
        lbl_nav_prev.clicked.connect(lambda: self.lib_step(-1))
        lbl_nav_next.clicked.connect(lambda: self.lib_step(1))
        btn_known.clicked.connect(lambda: self.lib_mark("known"))
        btn_again.clicked.connect(lambda: self.lib_mark("again"))
        btn_read.clicked.connect(self.lib_play)
        btn_dictation.clicked.connect(self.lib_dictation)
        btn_word.clicked.connect(self.lib_add_word)
        btn_syn.clicked.connect(self.lib_add_synonym)
        btn_del.clicked.connect(self.lib_delete)
        btn_ctx.clicked.connect(self.lib_context_play)
        btn_exp.clicked.connect(self.lib_export_menu)

        def _toggle_more():
            on = not self.lib_card_ctx.isVisible()
            self.lib_card_ctx.setVisible(on)
            self.lib_card_prag.setVisible(on)
            btn_more.setText("收起上下文" if on else "展开上下文")

        btn_more.clicked.connect(_toggle_more)
        self.lib_mode_out.toggled.connect(lambda _v: self.lib_render())

        # 3) 语义聚类  4) 会话  5) 说话人
        self.lib_cluster = QTableWidget(0, 3)
        self.lib_cluster.setHorizontalHeaderLabels(["含义（源文本）", "表达数", "目标语言表达"])
        self.lib_cluster.verticalHeader().setVisible(False)
        self.lib_cluster.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.lib_cluster.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.lib_cluster.setWordWrap(True)

        self.lib_sess = QTableWidget(0, 4)
        self.lib_sess.setHorizontalHeaderLabels(["会话", "时间范围", "句数", "场景"])
        self.lib_sess.verticalHeader().setVisible(False)
        self.lib_sess.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.lib_sess.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)

        self.lib_spk = QTableWidget(0, 3)
        self.lib_spk.setHorizontalHeaderLabels(["说话人", "句数", "最近一句"])
        self.lib_spk.verticalHeader().setVisible(False)
        self.lib_spk.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.lib_spk.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)

        # 空状态：不是空白表格，直接给下一步动作
        self.lib_empty = QWidget()
        ev = QVBoxLayout(self.lib_empty)
        ev.setContentsMargins(0, 60, 0, 0)
        ev.setSpacing(10)
        self.lib_empty_title = QLabel("这里还没有素材")
        self.lib_empty_title.setObjectName("title")
        self.lib_empty_title.setAlignment(Qt.AlignCenter)
        self.lib_empty_desc = QLabel("去捕获台采集第一组句对：选好音频源和语种，点「开始捕获」，句子会自动落到这里。")
        self.lib_empty_desc.setObjectName("desc")
        self.lib_empty_desc.setWordWrap(True)
        self.lib_empty_desc.setAlignment(Qt.AlignCenter)
        self.btn_lib_empty_go = QPushButton("去捕获台采集")
        self.btn_lib_empty_go.setObjectName("primary")
        self.btn_lib_empty_go.setCursor(Qt.PointingHandCursor)
        self.btn_lib_empty_go.clicked.connect(lambda: self._nav_go("Capture"))
        erow = QHBoxLayout()
        erow.addStretch(1)
        erow.addWidget(self.btn_lib_empty_go)
        erow.addStretch(1)
        ev.addWidget(self.lib_empty_title)
        ev.addWidget(self.lib_empty_desc)
        ev.addLayout(erow)
        ev.addStretch(1)

        for w in (self.lib_time, cardp, self.lib_cluster, self.lib_sess, self.lib_spk,
                  self.lib_empty):
            self.lib_stack.addWidget(w)
        lbv.addWidget(self.lib_stack, 1)

        # 批量操作悬浮条：勾选后才出现，不占常态布局
        self.lib_bulk = QFrame()
        self.lib_bulk.setObjectName("card")
        bh = QHBoxLayout(self.lib_bulk)
        bh.setContentsMargins(12, 6, 12, 6)
        self.lib_bulk_lbl = QLabel("已选 0 条")
        self.lib_bulk_lbl.setObjectName("chip")
        bh.addWidget(self.lib_bulk_lbl)
        self.btn_bulk_known = QPushButton("标记已掌握")
        self.btn_bulk_export = QPushButton("批量导出")
        self.btn_bulk_del = QPushButton("批量删除")
        self.btn_bulk_read = QPushButton("连续朗读")
        for b in (self.btn_bulk_known, self.btn_bulk_export, self.btn_bulk_del,
                  self.btn_bulk_read):
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(26)
            bh.addWidget(b)
        bh.addStretch(1)
        self.btn_bulk_clear = QPushButton("取消选择")
        self.btn_bulk_clear.setFixedHeight(26)
        bh.addWidget(self.btn_bulk_clear)
        self.lib_bulk.hide()
        lbv.addWidget(self.lib_bulk)
        self.tabs.addTab(lib_tab, "Library｜素材库")
        self.lib_time.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.lib_time.itemSelectionChanged.connect(self._lib_bulk_update)
        self.btn_bulk_known.clicked.connect(lambda: self._lib_bulk_action("known"))
        self.btn_bulk_export.clicked.connect(lambda: self._lib_bulk_action("export"))
        self.btn_bulk_del.clicked.connect(lambda: self._lib_bulk_action("delete"))
        self.btn_bulk_read.clicked.connect(lambda: self._lib_bulk_action("read"))
        self.btn_bulk_clear.clicked.connect(self.lib_time.clearSelection)

        for w in (self.lib_view, self.lib_pair, self.lib_scene, self.lib_state,
                  self.lib_quality):
            w.currentIndexChanged.connect(self.refresh_library)
        self.lib_search.textChanged.connect(self.refresh_library)
        self.lib_chk_fav.toggled.connect(self.refresh_library)
        self.lib_chk_audio.toggled.connect(self.refresh_library)
        self.lib_chk_unread.toggled.connect(self.refresh_library)
        self.btn_lib_refresh.clicked.connect(lambda: self.refresh_library(force=True))
        self._lib_all = []
        self._lib_rows = []
        self._lib_idx = 0
        self._lib_revealed = False
        self.btn_live_fav.clicked.connect(self.fav_current)
        self.btn_live_speak.clicked.connect(
            lambda: self.speak_row(self.live_tgt.text().replace("译：", "", 1)))
        self.btn_live_tag.clicked.connect(self._pick_scene_tag)
        self._sync_capture_ui()

        # 生词本
        word_tab = QWidget()
        wv = QVBoxLayout(word_tab)
        wv.setContentsMargins(0, 6, 0, 0)
        wv.setSpacing(8)
        wbar = QHBoxLayout()
        self.word_count = QLabel("生词 0 个")
        self.word_count.setObjectName("chip")
        wbar.addWidget(self.word_count)
        wbar.addStretch(1)
        self.word_scene_hint = QLabel("场景筛选：全部")
        self.word_scene_hint.setObjectName("chip")
        wbar.addWidget(self.word_scene_hint)
        self.btn_word_export = QPushButton("导出生词本")
        self.btn_word_del = QPushButton("删除选中")
        for b in (self.btn_word_export, self.btn_word_del):
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(30)
            wbar.addWidget(b)
        wv.addLayout(wbar)

        self.word_table = QTableWidget(0, 5)
        self.word_table.setHorizontalHeaderLabels(["时间", "单词", "目标释义", "语境", "原句"])
        self.word_table.verticalHeader().setVisible(False)
        self.word_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.word_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.word_table.setColumnWidth(0, 130)
        self.word_table.setColumnWidth(1, 110)
        self.word_table.setColumnWidth(2, 230)
        self.word_table.setColumnWidth(3, 60)
        self.word_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.word_table.setWordWrap(True)
        self.word_table.verticalHeader().setDefaultSectionSize(44)
        self.word_table.cellDoubleClicked.connect(self.open_word_card)
        wv.addWidget(self.word_table, 1)
        self.chk_selftest_word = QCheckBox("反向自测（隐藏释义，点单词揭晓）")
        self.chk_selftest_word.toggled.connect(self.set_selftest)
        wv.addWidget(self.chk_selftest_word)
        self.chk_lang_filter = QCheckBox("按语种筛选（只显示当前目标语种）")
        self.chk_lang_filter.setChecked(bool(self.cfg.get("filter_by_lang", False)))
        self.chk_lang_filter.setToolTip("中文→英语、中文→日语的学习记录互相隔离，不混在一起复习")
        self.chk_lang_filter.toggled.connect(self._on_lang_filter)
        wv.addWidget(self.chk_lang_filter)
        self.word_reveal = QLabel("点一个单词 → 在这里揭晓释义")
        self.word_reveal.setObjectName("chip")
        self.word_reveal.setWordWrap(True)
        wv.addWidget(self.word_reveal)
        self.word_table.cellClicked.connect(self.on_word_click)
        self.tabs.addTab(word_tab, "Library｜生词本")

        # 收藏（句子 + 单词）
        fav_tab = QWidget()
        fv = QVBoxLayout(fav_tab)
        fv.setContentsMargins(0, 6, 0, 0)
        fv.setSpacing(8)
        fbar = QHBoxLayout()
        self.fav_count = QLabel("收藏 0 条")
        self.fav_count.setObjectName("chip")
        fbar.addWidget(self.fav_count)
        self.fav_hint = QLabel("右键记录里的一行、或点上面「☆ 收藏这句」就能收进来")
        self.fav_hint.setObjectName("chip")
        fbar.addWidget(self.fav_hint)
        fbar.addStretch(1)
        self.btn_fav_export = QPushButton("导出收藏")
        self.btn_fav_del = QPushButton("删除选中")
        for b in (self.btn_fav_export, self.btn_fav_del):
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(30)
            fbar.addWidget(b)
        fv.addLayout(fbar)

        self.fav_table = QTableWidget(0, 5)
        self.fav_table.setHorizontalHeaderLabels(["类型", "时间", "内容", "源文本 / 释义", "说话人"])
        self.fav_table.verticalHeader().setVisible(False)
        self.fav_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.fav_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.fav_table.setColumnWidth(0, 62)
        self.fav_table.setColumnWidth(1, 130)
        self.fav_table.setColumnWidth(2, 300)
        self.fav_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.fav_table.setColumnWidth(4, 76)
        self.fav_table.setWordWrap(True)
        self.fav_table.verticalHeader().setDefaultSectionSize(46)
        fv.addWidget(self.fav_table, 1)
        self.tabs.addTab(fav_tab, "Library｜收藏")

        # 会话（自动按时间段分袋：一局游戏 / 一段视频）
        sess_tab = QWidget()
        sv = QVBoxLayout(sess_tab)
        sv.setContentsMargins(0, 6, 0, 0)
        sv.setSpacing(8)
        sbar = QHBoxLayout()
        self.sess_count = QLabel("会话 0 个")
        self.sess_count.setObjectName("chip")
        sbar.addWidget(self.sess_count)
        self.sess_hint = QLabel("静音超过 2 分钟自动切新会话；来源、开始时间、时长、句数都在")
        self.sess_hint.setObjectName("chip")
        sbar.addWidget(self.sess_hint)
        sbar.addStretch(1)
        self.btn_sess_text = QPushButton("导出文本")
        self.btn_sess_srt = QPushButton("导出 SRT")
        for b in (self.btn_sess_text, self.btn_sess_srt):
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(30)
            sbar.addWidget(b)
        sv.addLayout(sbar)

        self.sess_table = QTableWidget(0, 5)
        self.sess_table.setHorizontalHeaderLabels(["开始", "来源", "时长", "句数", "生词"])
        self.sess_table.verticalHeader().setVisible(False)
        self.sess_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.sess_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.sess_table.setColumnWidth(0, 110)
        self.sess_table.setColumnWidth(1, 120)
        self.sess_table.setColumnWidth(2, 90)
        self.sess_table.setColumnWidth(3, 70)
        self.sess_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.sess_table.setWordWrap(True)
        self.sess_table.verticalHeader().setDefaultSectionSize(38)
        self.sess_table.cellDoubleClicked.connect(self.open_session)
        sv.addWidget(self.sess_table, 1)
        self.tabs.addTab(sess_tab, "Library｜会话")

        self.btn_sess_text.clicked.connect(lambda: self.export_session("txt"))
        self.btn_sess_srt.clicked.connect(lambda: self.export_session("srt"))

        # 双向句对（两条链路：听懂 / 会说）
        pair_tab = QWidget()
        pv = QVBoxLayout(pair_tab)
        pv.setContentsMargins(0, 6, 0, 0)
        pv.setSpacing(8)
        pbar = QHBoxLayout()
        self.pair_count = QLabel("句对 0 条")
        self.pair_count.setObjectName("chip")
        pbar.addWidget(self.pair_count)
        self.pair_filter = QComboBox()
        self.pair_filter.addItems(["全部", "听力（英→中）", "输出（中→英）", "表达缺口",
                                   "对话链", "语用冲突"])
        self.pair_filter.currentIndexChanged.connect(self.refresh_pairs)
        pbar.addWidget(self.pair_filter)
        pbar.addStretch(1)
        self.btn_pair_dict = QPushButton("对照听写")
        self.btn_pair_merge = QPushButton("合并同义")
        self.btn_pair_tpl = QPushButton("应答模板")
        self.btn_pair_rate = QPushButton("复用率")
        for b in (self.btn_pair_dict, self.btn_pair_merge, self.btn_pair_tpl, self.btn_pair_rate):
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(30)
            pbar.addWidget(b)
        pv.addLayout(pbar)

        self.pair_table = QTableWidget(0, 6)
        self.pair_table.setHorizontalHeaderLabels(["时间", "链路", "目标文本", "源文本", "场景 / 角色", "标记"])
        self.pair_table.verticalHeader().setVisible(False)
        self.pair_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.pair_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.pair_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.pair_table.setColumnWidth(0, 130)
        self.pair_table.setColumnWidth(1, 76)
        self.pair_table.setColumnWidth(2, 300)
        self.pair_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.pair_table.setColumnWidth(4, 120)
        self.pair_table.setColumnWidth(5, 150)
        self.pair_table.setWordWrap(True)
        self.pair_table.verticalHeader().setDefaultSectionSize(40)
        self.pair_table.cellDoubleClicked.connect(self.open_pair_detail)
        pv.addWidget(self.pair_table, 1)
        self.tabs.addTab(pair_tab, "Library｜句对")

        # Lab 实验室：能力底座（语种 / 模型 / 词典 / 识别规则 / 外观）
        from PySide6.QtWidgets import QScrollArea, QSpinBox
        from PySide6.QtCore import Qt as _LabQt
        lab_tab = QWidget()
        lab_root = QVBoxLayout(lab_tab)
        lab_root.setContentsMargins(0, 6, 0, 0)
        lab_root.setSpacing(8)
        lab_head = QHBoxLayout()
        lab_title = QLabel("Lab 实验室")
        lab_title.setObjectName("sectionTitle")
        lab_title.setToolTip("能力底座：语种、模型、词典、规则、主题都在这里长出来")
        lab_head.addWidget(lab_title)
        # 长描述不再横贯一整行，收进标题 tooltip
        lab_desc = QLabel("")
        lab_desc.setObjectName("chip")
        lab_desc.hide()
        lab_head.addStretch(1)
        self.btn_lab_settings = QPushButton("完整设置")
        self.btn_lab_log = QPushButton("运行日志")
        for b in (self.btn_lab_settings, self.btn_lab_log):
            b.setCursor(_LabQt.PointingHandCursor)
            b.setFixedHeight(28)
            lab_head.addWidget(b)
        lab_root.addLayout(lab_head)

        # Lab 也分组：基础 / 语音 / 规则 / 外观，一次只显示一组，别把八张卡堆一屏
        self.lab_nav = QHBoxLayout()
        self.lab_nav.setSpacing(6)
        self.lab_nav_btns = []
        self.lab_stack = QStackedWidget()
        self._lab_page_layouts = {}
        for i, (key, label) in enumerate((("base", "基础 · 模型语种"),
                                          ("voice", "语音 · TTS"),
                                          ("rules", "规则 · 识别"),
                                          ("look", "外观 · 风格"))):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setCursor(_LabQt.PointingHandCursor)
            b.setFixedHeight(28)
            b.clicked.connect(lambda _c=False, k=i: self._lab_nav(k))
            self.lab_nav.addWidget(b)
            self.lab_nav_btns.append(b)
            page = QWidget()
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            inner = QWidget()
            v = QVBoxLayout(inner)
            v.setContentsMargins(2, 2, 8, 2)
            v.setSpacing(10)
            scroll.setWidget(inner)
            self.lab_stack.addWidget(scroll)
            self._lab_page_layouts[key] = v
        self.lab_nav.addStretch(1)
        lab_root.addLayout(self.lab_nav)
        lab_root.addWidget(self.lab_stack, 1)
        self.lab_nav_btns[0].setChecked(True)

        def lab_card(title, hint="", target=None):
            f = QFrame()
            f.setObjectName("card")
            v = QVBoxLayout(f)
            v.setContentsMargins(14, 10, 14, 12)
            v.setSpacing(6)
            t = QLabel(title)
            t.setObjectName("cardTitle")
            v.addWidget(t)
            if hint:
                h = QLabel(hint)
                h.setObjectName("chip")
                h.setWordWrap(True)
                v.addWidget(h)
            (target or self._lab_page_layouts["base"]).addWidget(f)
            return v

        # 1) 语种管理器
        v1 = lab_card("语种管理器", "列表来自 LANG_REGISTRY：新增语种只需登记一条，Capture、Library 的语种框会自动出现。")
        self.lab_lang_table = QTableWidget(len(LANG_REGISTRY), 5)
        self.lab_lang_table.setHorizontalHeaderLabels(["语种", "支持方向", "配套包", "状态", "词典"])
        self.lab_lang_table.verticalHeader().setVisible(False)
        self.lab_lang_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.lab_lang_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.lab_lang_table.setColumnWidth(0, 90)
        self.lab_lang_table.setColumnWidth(1, 120)
        self.lab_lang_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.lab_lang_table.setColumnWidth(3, 70)
        self.lab_lang_table.setColumnWidth(4, 90)
        self.lab_lang_table.setMinimumHeight(190)
        v1.addWidget(self.lab_lang_table)
        self.lab_lang_hint = QLabel("")
        self.lab_lang_hint.setObjectName("chip")
        self.lab_lang_hint.setWordWrap(True)
        v1.addWidget(self.lab_lang_hint)

        # 2) 模型管理器
        v2 = lab_card("模型管理器", "ASR 与翻译模型分开管理；改尺寸会写进配置，识别引擎在下次启动时按新模型加载。")
        mrow = QHBoxLayout()
        mrow.addWidget(QLabel("识别模型（ASR）"))
        self.lab_asr = QComboBox()
        self.lab_asr.addItem("自动（有显卡 medium，无显卡 small）", "auto")
        self.lab_asr.addItem("base（低占用）", "base")
        self.lab_asr.addItem("small（均衡）", "small")
        self.lab_asr.addItem("medium（高精度，约 1.5G）", "medium")
        self.lab_asr.addItem("turbo（速度优先，长句稍弱）", "turbo")
        mrow.addWidget(self.lab_asr, 1)
        mrow.addWidget(QLabel("翻译模型"))
        self.lab_mt = QComboBox()
        self.lab_mt.addItem("small（轻量）", "small")
        self.lab_mt.addItem("base（更准）", "base")
        mrow.addWidget(self.lab_mt, 1)
        btn_lab_models = QPushButton("保存模型选择")
        btn_lab_models.setCursor(_LabQt.PointingHandCursor)
        btn_lab_models.setFixedHeight(28)
        mrow.addWidget(btn_lab_models)
        v2.addLayout(mrow)
        self.lab_model_status = QLabel("")
        self.lab_model_status.setObjectName("chip")
        self.lab_model_status.setWordWrap(True)
        v2.addWidget(self.lab_model_status)

        # 3) 词典插件市场
        v3 = lab_card("词典插件市场", "双击单词查义由词典插件提供，不是硬编码；每个语种挂自己的分词 / 释义解析器。")
        self.lab_dict_table = QTableWidget(len(LANG_REGISTRY), 2)
        self.lab_dict_table.setHorizontalHeaderLabels(["语种", "词典插件"])
        self.lab_dict_table.verticalHeader().setVisible(False)
        self.lab_dict_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.lab_dict_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.lab_dict_table.setMaximumHeight(190)
        v3.addWidget(self.lab_dict_table)

        # 4) 识别规则
        v4 = lab_card("识别规则", "过滤、长度、说话人、原声切片；立即影响 Capture 的记录与字幕。",
                      target=self._lab_page_layouts["rules"])
        r1 = QHBoxLayout()
        self.lab_chk_short = QCheckBox("过滤过短识别（语气词 / 单字）")
        self.lab_chk_diar = QCheckBox("区分说话人")
        self.lab_chk_audio = QCheckBox("保存音频切片")
        for c in (self.lab_chk_short, self.lab_chk_diar, self.lab_chk_audio):
            r1.addWidget(c)
        r1.addStretch(1)
        v4.addLayout(r1)
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("最短字符"))
        self.lab_min_chars = QSpinBox()
        self.lab_min_chars.setRange(1, 8)
        self.lab_min_chars.setSuffix(" 字")
        r2.addWidget(self.lab_min_chars)
        r2.addWidget(QLabel("最短单词"))
        self.lab_min_words = QSpinBox()
        self.lab_min_words.setRange(1, 10)
        self.lab_min_words.setSuffix(" 词")
        r2.addWidget(self.lab_min_words)
        r2.addWidget(QLabel("最大说话人"))
        self.lab_max_spk = QSpinBox()
        self.lab_max_spk.setRange(1, 8)
        r2.addWidget(self.lab_max_spk)
        btn_lab_rules = QPushButton("保存识别规则")
        btn_lab_rules.setCursor(_LabQt.PointingHandCursor)
        btn_lab_rules.setFixedHeight(28)
        r2.addWidget(btn_lab_rules)
        r2.addStretch(1)
        v4.addLayout(r2)
        self.lab_filter_log = QLabel("已静默过滤 0 条短句")
        self.lab_filter_log.setObjectName("chip")
        self.lab_filter_log.setWordWrap(True)
        v4.addWidget(self.lab_filter_log)

        # 4.5) 语音 / TTS（按语种隔离）
        vtts = lab_card("语音 / TTS",
                        "音色、语速、音量、队列与跟读留白按语种独立保存；离线引擎优先。",
                        target=self._lab_page_layouts["voice"])
        trow = QHBoxLayout()
        trow.addWidget(QLabel("配置语种"))
        self.tts_lang_box = QComboBox()
        for code, meta in LANG_REGISTRY.items():
            if not meta.get("installed"):
                continue        # 只列真正能朗读的语种，和字幕下拉保持一致
            self.tts_lang_box.addItem("%s（%s）" % (meta["name"], meta["native"]), code)
        trow.addWidget(self.tts_lang_box)
        trow.addWidget(QLabel("TTS 引擎"))
        self.tts_engine_box = QComboBox()
        self.tts_engine_box.addItem("Windows 离线语音（SAPI）", "offline")
        self.tts_engine_box.setToolTip("当前只接了这一种；在线 TTS 没有实现，所以不放在下拉里")
        self.tts_engine_box.setEnabled(False)
        trow.addWidget(self.tts_engine_box)
        trow.addStretch(1)
        vtts.addLayout(trow)

        vrow = QHBoxLayout()
        vrow.addWidget(QLabel("音色"))
        self.tts_voice_box = QComboBox()
        vrow.addWidget(self.tts_voice_box, 1)
        self.btn_tts_pack = QPushButton("音色来源")
        self.btn_tts_pack.setToolTip("查系统里有没有这个语种的语音包，没有就告诉你去哪装（软件内不下载音色）")
        vrow.addWidget(self.btn_tts_pack)
        vtts.addLayout(vrow)
        orow = QHBoxLayout()
        orow.addWidget(QLabel("朗读输出设备"))
        self.tts_out_dev = QLineEdit()
        self.tts_out_dev.setPlaceholderText("留空 = 系统默认；可填耳机 / 扬声器名称关键字")
        orow.addWidget(self.tts_out_dev, 1)
        vtts.addLayout(orow)

        rrow = QHBoxLayout()
        rrow.addWidget(QLabel("语速"))
        self.tts_rate = QSlider(Qt.Horizontal)
        self.tts_rate.setRange(50, 200)
        self.tts_rate.setFixedWidth(180)
        self.tts_rate_val = QLabel("1.00x")
        rrow.addWidget(self.tts_rate)
        rrow.addWidget(self.tts_rate_val)
        rrow.addWidget(QLabel("音量"))
        self.tts_vol = QSlider(Qt.Horizontal)
        self.tts_vol.setRange(0, 100)
        self.tts_vol.setFixedWidth(140)
        self.tts_vol_val = QLabel("100")
        rrow.addWidget(self.tts_vol)
        rrow.addWidget(self.tts_vol_val)
        rrow.addStretch(1)
        vtts.addLayout(rrow)

        arow2 = QHBoxLayout()
        arow2.addWidget(QLabel("自动朗读最少词数"))
        self.tts_min = QSpinBox()
        self.tts_min.setRange(1, 8)
        arow2.addWidget(self.tts_min)
        arow2.addWidget(QLabel("队列上限"))
        self.tts_qmax = QSpinBox()
        self.tts_qmax.setRange(1, 10)
        arow2.addWidget(self.tts_qmax)
        arow2.addWidget(QLabel("朗读间隙"))
        self.tts_gap = QDoubleSpinBox()
        self.tts_gap.setRange(0.2, 2.0)
        self.tts_gap.setSingleStep(0.1)
        arow2.addWidget(self.tts_gap)
        arow2.addWidget(QLabel("跟读留白"))
        self.tts_loop_gap = QDoubleSpinBox()
        self.tts_loop_gap.setRange(0.5, 3.0)
        self.tts_loop_gap.setSingleStep(0.1)
        arow2.addWidget(self.tts_loop_gap)
        vtts.addLayout(arow2)

        crow2 = QHBoxLayout()
        self.chk_tts_hl = QCheckBox("朗读时同步逐词高亮")
        self.chk_tts_dup = QCheckBox("过滤重复句")
        self.chk_tts_full = QCheckBox("全屏游戏时禁止自动朗读")
        self.chk_tts_duck = QCheckBox("仅原声安静时自动朗读")
        for c in (self.chk_tts_hl, self.chk_tts_dup, self.chk_tts_full, self.chk_tts_duck):
            crow2.addWidget(c)
        crow2.addStretch(1)
        vtts.addLayout(crow2)
        srow = QHBoxLayout()
        self.chk_shadow = QCheckBox("跟读模式：读一遍 → 留白 → 再读一遍")
        self.chk_shadow_repeat = QCheckBox("留白后自动重播")
        srow.addWidget(self.chk_shadow)
        srow.addWidget(self.chk_shadow_repeat)
        srow.addStretch(1)
        vtts.addLayout(srow)
        self.tts_pack_hint = QLabel("")
        self.tts_pack_hint.setObjectName("chip")
        vtts.addWidget(self.tts_pack_hint)

        # 5) 外观与风格
        v5 = lab_card("外观与风格", "六套风格换的是圆角、边线、字号、标题字体和配色，点一下立即预览。",
                      target=self._lab_page_layouts["look"])
        self.lab_theme = QComboBox()
        self.lab_theme.setMinimumWidth(180)
        for key in THEME_ORDER:
            self.lab_theme.addItem(THEME_LABELS[key], key)
        self.lab_theme.hide()          # 实际选择用下面的预览块，保留下拉供旧代码同步
        thint = QLabel("选择界面风格")
        thint.setObjectName("chip")
        v5.addWidget(thint)
        self._theme_tiles = {}
        for row_keys in (THEME_ORDER[:3], THEME_ORDER[3:]):
            trow = QHBoxLayout()
            trow.setSpacing(8)
            for key in row_keys:
                b = QPushButton()
                b.setCursor(_LabQt.PointingHandCursor)
                b.setFixedHeight(56)
                b.setMinimumWidth(150)
                b.clicked.connect(lambda _c=False, k=key: self._quick_theme(k))
                trow.addWidget(b, 1)
                self._theme_tiles[key] = b
            v5.addLayout(trow)
        self._refresh_theme_tiles(str(self.cfg.get("theme") or "dark"))
        srow0 = QHBoxLayout()
        srow0.addWidget(QLabel("字幕主题"))
        self.lab_style = QComboBox()
        self.lab_style.addItems(["游戏（粗字强描边）", "观影（柔和偏上）", "极简（低透明）"])
        srow0.addWidget(self.lab_style)
        self.lab_style_hint = QLabel("")
        self.lab_style_hint.setObjectName("chip")
        srow0.addWidget(self.lab_style_hint, 1)
        v5.addLayout(srow0)
        urow = QHBoxLayout()
        urow.addWidget(QLabel("界面语言"))
        self.lab_uilang = QComboBox()
        self.lab_uilang.addItem("中文", "zh")
        self.lab_uilang.addItem("English", "en")
        urow.addWidget(self.lab_uilang)
        urow.addStretch(1)
        v5.addLayout(urow)
        self.lab_hotkey_hint = QLabel("")
        self.lab_hotkey_hint.setObjectName("chip")
        self.lab_hotkey_hint.setWordWrap(True)
        v5.addWidget(self.lab_hotkey_hint)
        self.chk_anim = QCheckBox("界面动画（120ms 淡入 / 抽屉），低配机可关")
        v5.addWidget(self.chk_anim)
        qrow = QHBoxLayout()
        qrow.addWidget(QLabel("朗读队列上限"))
        self.spin_tts_q = QSpinBox()
        self.spin_tts_q.setRange(1, 10)
        self.spin_tts_q.setValue(int(self.cfg.get("tts_queue_max") or 3))
        qrow.addWidget(self.spin_tts_q)
        qrow.addWidget(QLabel("超出丢最旧的待读句"))
        qrow.addStretch(1)
        v5.addLayout(qrow)

        for v in self._lab_page_layouts.values():
            v.addStretch(1)
        self.tabs.addTab(lab_tab, "Lab｜实验室")

        # ---------------- SceneTalk 情景译：并入主程序，不再是独立网页工具 ----------------
        # 说的是“我想说的这句英文怎么说”，和 Capture（听）共用一份语料库与设置；
        # 页面用本机回环服务提供（浏览器的 localStorage / 安全上下文都依赖 http 源）。
        self.talk_tab = QWidget()
        tk = QVBoxLayout(self.talk_tab)
        tk.setContentsMargins(0, 6, 0, 0)
        tk.setSpacing(8)
        self.talk_ph = QLabel("点左侧「说」进入情景译；第一次打开需要两秒左右。")
        self.talk_ph.setObjectName("desc")
        self.talk_ph.setAlignment(Qt.AlignCenter)
        tk.addWidget(self.talk_ph, 1)
        self._talk_view = None
        self.tabs.addTab(self.talk_tab, "SceneTalk｜情景译")

        # ---- 顶层只留三个模块：隐藏原生的 7 个页签，改由侧栏 + 次级导航条切换 ----
        try:
            self.tabs.tabBar().hide()
        except Exception:
            pass
        self.lib_subnav = QWidget()
        snl = QHBoxLayout(self.lib_subnav)
        snl.setContentsMargins(0, 0, 0, 0)
        snl.setSpacing(6)
        self._lib_sub_btns = []
        # 五个子页收成三组：素材 / 复习（生词·收藏）/ 记录（会话·句对）
        b_mat = QPushButton("素材")
        b_mat.setCheckable(True)
        b_mat.setCursor(Qt.PointingHandCursor)
        b_mat.setFixedHeight(28)
        b_mat.clicked.connect(lambda _c=False: self._goto_tab(1))
        snl.addWidget(b_mat)
        self._lib_sub_btns.append(({1}, b_mat))

        for label, items in (("复习 ▾", (("生词本", 2), ("收藏", 3))),
                             ("记录 ▾", (("会话", 4), ("句对", 5)))):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(28)
            m = QMenu(b)
            for sub, idx in items:
                m.addAction(sub, lambda _c=False, k=idx: self._goto_tab(k))
            b.setMenu(m)
            snl.addWidget(b)
            self._lib_sub_btns.append(({idx for _s, idx in items}, b))
        snl.addStretch(1)
        root.addWidget(self.lib_subnav)
        self.lib_subnav.hide()
        self.tabs.currentChanged.connect(lambda i: self._sync_nav_state(i))

        # 启动时恢复上次的侧栏展开状态与停留页面
        if bool(cfg.get("nav_expanded")):
            self.nav_expanded = False
            self._toggle_nav()

        btn_lab_models.clicked.connect(self.lab_save_models)
        self.chk_anim.toggled.connect(self.on_anim_toggle)
        self.spin_tts_q.valueChanged.connect(self.on_tts_queue_max)
        self.lab_uilang.currentIndexChanged.connect(self.on_ui_lang)
        self.tts_lang_box.currentIndexChanged.connect(self.lab_tts_refresh)
        self.btn_tts_pack.clicked.connect(self.lab_tts_pack)
        self.tts_out_dev.editingFinished.connect(self.lab_tts_device)
        for w in (self.tts_voice_box, self.tts_rate, self.tts_vol, self.tts_min,
                  self.tts_qmax, self.tts_gap, self.tts_loop_gap, self.chk_tts_hl,
                  self.chk_tts_dup, self.chk_tts_full, self.chk_tts_duck,
                  self.chk_shadow, self.chk_shadow_repeat):
            if isinstance(w, (QSlider, QSpinBox, QDoubleSpinBox)):
                w.valueChanged.connect(lambda _v=0: self.lab_tts_apply())
            elif isinstance(w, QCheckBox):
                w.toggled.connect(lambda _v=False: self.lab_tts_apply())
            elif isinstance(w, QComboBox):
                w.currentIndexChanged.connect(lambda _i=0: self.lab_tts_apply())
        btn_lab_rules.clicked.connect(self.lab_save_rules)
        self.lab_style.currentIndexChanged.connect(self.lab_apply_style)
        self.lab_theme.currentIndexChanged.connect(self.lab_apply_theme)
        self.tabs.currentChanged.connect(
            lambda i: self.lab_refresh() if self.tabs.tabText(i) == "Lab｜实验室" else None)

        self.btn_pair_dict.clicked.connect(self.pair_dictation)
        self.btn_pair_merge.clicked.connect(self.merge_pairs)
        self.btn_pair_tpl.clicked.connect(self.templates_dialog)
        self.btn_pair_rate.clicked.connect(self.show_reuse_rate)
        self.btn_lab_settings.clicked.connect(owner.open_settings)
        self.btn_lab_log.clicked.connect(lambda: self.btn_log.click())

        root.addWidget(self.tabs, 1)

        # 底部极简状态栏：音频 / 模型 / 就绪这些信息已收进顶部状态行 tooltip，
        # 不再在页脚重复占一行
        barframe = QFrame()
        bh = QHBoxLayout(barframe)
        bh.setContentsMargins(2, 0, 2, 0)
        bh.setSpacing(14)
        self.bar_audio = QLabel("音频：准备中")
        self.bar_model = QLabel("模型：准备中")
        self.bar_state = QLabel("就绪")
        for w in (self.bar_audio, self.bar_model, self.bar_state):
            w.setObjectName("chip")
            bh.addWidget(w)
        bh.addStretch(1)
        barframe.hide()
        root.addWidget(barframe)

        # 底部：悬浮字幕开关已随捕获控制行走；这里只留低频入口，全部隐藏
        foot = QHBoxLayout()
        self.btn_pause = QPushButton("开始")
        self.btn_pause.setObjectName("primary")
        self.btn_pause.setCursor(Qt.PointingHandCursor)
        self.btn_pause.setFixedHeight(36)
        self.btn_pause.hide()
        foot.addStretch(1)

        self.btn_clear = QPushButton("清空列表")
        self.btn_folder = QPushButton("历史文件夹")
        for b in (self.btn_clear, self.btn_folder):
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(30)
            b.hide()
        # 清空 / 历史文件夹是低频操作，跟批量、导出一起收进「更多」
        try:
            menu = self.btn_more.menu()
            menu.addAction("清空列表", self.btn_clear.click)
            menu.addAction("历史文件夹", self.btn_folder.click)
        except Exception:
            pass
        root.addLayout(foot)

        # 信号
        self.btn_settings.clicked.connect(owner.open_settings)
        self.btn_export.clicked.connect(self.export_menu)
        self.btn_batch.clicked.connect(self.batch_menu)
        self.btn_log.clicked.connect(self.open_log)
        self.btn_pause.clicked.connect(self.on_pause)
        self.btn_clear.clicked.connect(self.clear_rows)
        self.btn_folder.clicked.connect(self.open_history_dir)
        self.table.cellDoubleClicked.connect(self.on_cell_double)
        # 右键一行 → 收藏这句（不占用双击，双击还是拆词查义）
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.on_table_menu)
        self.day_box.currentTextChanged.connect(self.load_day)
        # 输入时不要每敲一个字就扫一遍文件，停手 350ms 再搜
        self._search_timer = QTimer(self.win)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(350)
        self._search_timer.timeout.connect(lambda: self.apply_filter(self.search.text()))
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        self.btn_word_export.clicked.connect(self.export_words)
        self.btn_word_del.clicked.connect(self.delete_words)
        self.btn_fav_export.clicked.connect(self.export_fav)
        self.btn_fav_del.clicked.connect(self.delete_fav)
        self.btn_fav_line.clicked.connect(self.fav_current)
        # 双击查词 / 查同义说法：绑在唯一显示字幕的「当前字幕」卡上
        self.cap_en.mouseDoubleClickEvent = lambda ev: self.open_word_dialog(
            self.cap_en.text(), getattr(getattr(owner, "overlay", None), "_cur_spk", ""))
        self.cap_zh.mouseDoubleClickEvent = lambda ev: self.show_synonym_english(self.cap_zh.text())

        self.book = WordBook(owner.cfg)

        # 图标 / 托盘
        self.icon = QIcon(str(ICON_PATH)) if ICON_PATH.exists() else QIcon()
        self.win.setWindowIcon(self.icon)
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(self.icon, self.win)
            self.tray.setToolTip("Oversub 沉浸字幕")
            menu = QMenu()
            act_show = QAction("显示主窗口", menu)
            act_overlay = QAction("显示/隐藏悬浮条", menu)
            act_pause = QAction("暂停/继续", menu)
            act_reset = QAction("重置字幕条位置", menu)
            act_quit = QAction("退出", menu)
            for a in (act_show, act_overlay, act_pause, act_reset):
                menu.addAction(a)
            menu.addSeparator()
            menu.addAction(act_quit)
            self.tray.setContextMenu(menu)
            act_show.triggered.connect(self.show)
            act_overlay.triggered.connect(self.on_overlay_toggle_click)
            act_pause.triggered.connect(self.on_pause)
            act_reset.triggered.connect(self._reset_overlay_pos)
            act_quit.triggered.connect(owner.quit)
            self.tray.activated.connect(self._tray_activated)
            self.tray.show()

        self.win.closeEvent = self._close_event

        # 启动时按预设摆正一次：上次把字幕条拖出屏幕（配置里 y 超屏）也能自动拉回来
        try:
            owner.overlay.apply_position()
        except Exception as e:
            log("启动摆放字幕条失败：" + repr(e))

        # 拉记录
        self.timer = QTimer()
        self.timer.timeout.connect(self._drain)
        self.timer.start(180)

        # 异常兜底：CPU 守卫 + 定时备份
        self._cpu_timer = QTimer(self.win)
        self._cpu_timer.setInterval(20000)
        self._cpu_timer.timeout.connect(self._perf_guard)
        self._cpu_timer.start()
        self._backup_timer = QTimer(self.win)
        self._backup_timer.setInterval(10 * 60 * 1000)
        self._backup_timer.timeout.connect(self._do_backup)
        self._backup_timer.start()

        self.refresh_chips()
        self.set_spk_col_visible(bool(self.cfg.get("show_spk_col", True)))
        self._fav_keys = self._refresh_fav_cache()
        self.load_today()
        self._refresh_recent_preview()
        try:
            self.refresh_library(force=True)
        except Exception as e:
            log("素材库初始化失败：" + repr(e))
        try:
            self.lab_refresh()
        except Exception as e:
            log("实验室初始化失败：" + repr(e))
        try:
            self._lab_nav(int(cfg.get("lab_section") or 0))
        except Exception:
            pass
        self.lang_box.currentIndexChanged.connect(self.on_lang_box)
        self.apply_lang_fonts()
        ANIM["on"] = bool(self.cfg.get("animations", True))
        self._font_cache = preload_lang_fonts(self.cfg)
        try:
            idx = int(self.cfg.get("last_tab") or 0)
            if 0 <= idx < self.tabs.count():
                if idx == 7:
                    self._ensure_talk_view()       # 上次停在情景译：恢复时把页面一起载入
                self.tabs.setCurrentIndex(idx)      # 恢复上次停留的页面
        except Exception:
            pass
        self._sync_nav_state()
        try:
            apply_ui_lang(self.win, self.cfg.get("ui_lang") or "zh")
            apply_ui_lang(self.owner.overlay, self.cfg.get("ui_lang") or "zh")
        except Exception as e:
            log("恢复界面语言失败：" + repr(e))

    # ---------------- 数据 ----------------
    def refresh_lang_headers(self):
        """表头不写死语言：中文列按源语言命名，第三列统一叫「目标译文」，悬停提示当前语种。"""
        sl = str(self.cfg.get("source_lang") or "zh")
        tl = str(self.cfg.get("target_lang") or "en")
        m_sl, m_tl = lang_meta(sl), lang_meta(tl)
        it2 = self.table.horizontalHeaderItem(2)
        if it2 is not None:
            # 中英混说时每句语种可能不同，表头不再写死某一种语言
            it2.setText("识别源文本")
            it2.setToolTip("ASR 识别出的原文；中英混说时每句语种可能不同，"
                           "悬停单元格可看这一句的语种")
        it3 = self.table.horizontalHeaderItem(3)
        if it3 is not None:
            it3.setText("目标译文")
            it3.setToolTip("当前目标语种：%s %s（%s）｜ 字符方向：%s" % (
                m_tl.get("name", tl), m_tl.get("native", ""), tl,
                "RTL 从右到左" if m_tl.get("rtl") else "LTR 从左到右"))

    def _fill_lang_box(self):
        from PySide6.QtCore import Qt as _Qt
        self.lang_box.blockSignals(True)
        self.lang_box.clear()
        for code, meta in LANG_REGISTRY.items():
            if not meta.get("installed"):
                continue        # 用不了的语种不进主界面下拉，避免选到一个没东西的选项
            nm, nat = meta.get("name"), meta.get("native")
            label = nm if nm == nat else "%s %s" % (nm, nat)
            self.lang_box.addItem(label, code)
        cur = str(self.cfg.get("target_lang") or "en")
        for i in range(self.lang_box.count()):
            if self.lang_box.itemData(i) == cur:
                self.lang_box.setCurrentIndex(i)
                break
        self.lang_box.blockSignals(False)

    def _profile_of_current(self):
        tl = str(self.cfg.get("target_lang") or "en")
        return {
            "font_size": int(self.cfg.get("font_size") or 22),
            "opacity": float(self.cfg.get("opacity") or 0.9),
            "whisper_model": str(self.cfg.get("whisper_model") or "small"),
            "tts_rate": float((self.cfg.get("tts_rates") or {}).get(
                tl, self.cfg.get("read_rate") or 0)),
            "min_chars": int(self.cfg.get("min_chars") or 2),
            "min_words": int(self.cfg.get("min_words") or 2),
            "show_source": bool(self.cfg.get("show_source", True)),
            "overlay_bilingual": bool(self.cfg.get("overlay_bilingual", False)),
        }

    def switch_lang(self, code):
        """切换目标语种：存好旧语种的独立配置，再加载新语种的配套参数。"""
        code = str(code or "")
        if not lang_available(code):
            return False
        old = str(self.cfg.get("target_lang") or "en")
        profiles = dict(self.cfg.get("lang_profiles") or {})
        if old and old != code:
            profiles[old] = self._profile_of_current()
        self.cfg["lang_profiles"] = profiles
        self.cfg["target_lang"] = code
        # 双向配对：目标选中文 → 英文识别 + 翻成中文；目标选其他语种 → 中文识别 + 翻成该语种
        want_sl = "en" if code == "zh" else "zh"
        if want_sl == code:
            want_sl = "en" if code != "en" else "zh"
        self.cfg["source_lang"] = want_sl
        prof = dict(profiles.get(code) or {})
        for key in ("font_size", "opacity", "whisper_model", "min_chars", "min_words",
                    "show_source", "overlay_bilingual"):
            if key in prof:
                self.cfg[key] = prof[key]
        set_langs(self.cfg)
        save_config(self.cfg)
        # ASR 语言与翻译模型都是引擎启动时加载的：引擎在跑就重启一次，否则下次开始捕获自动生效
        try:
            was_capturing = bool(getattr(getattr(self.owner, "capture", None), "running", False))
            if getattr(self.owner, "pipeline", None) is not None:
                self.owner.restart_engine()
                if was_capturing:
                    self.owner.start_capture()      # 采集本来在跑，切语种后自动接回来
                log("语种对已切到 %s，引擎已重启" % pair_label(
                    self.cfg["source_lang"], self.cfg["target_lang"]))
        except Exception as e:
            log("切语种后重启引擎失败：" + repr(e))
        try:
            for key in ("target_lang", "source_lang", "font_size", "opacity", "font_family",
                        "lang_fonts", "rtl_overlay"):
                self.owner.overlay.cfg[key] = self.cfg.get(key)
            self.owner.overlay.update_look()
            try:
                self.owner.overlay.tts_voice.setText(
                    self.owner.overlay.tts.voice_name(str(self.cfg.get("target_lang") or "en")))
                self.owner.overlay.preload_tts(str(self.cfg.get("target_lang") or "en"))
                self.owner.overlay.update_tts_ready(str(self.cfg.get("target_lang") or "en"))
                self.lab_tts_refresh()
            except Exception:
                pass
        except Exception:
            pass
        self.refresh_chips()
        self.refresh_lang_headers()
        self.apply_lang_fonts()
        if self.cfg.get("filter_by_lang"):
            self.refresh_words()
            self.refresh_fav()
        self.set_status("已切到目标语种：%s（配置与字体已同步）" % lang_name(code), "ok")
        log("目标语种切换：%s" % code)
        return True

    def on_lang_box(self, idx):
        from PySide6.QtWidgets import QMessageBox
        code = self.lang_box.itemData(idx)
        if not code or code == self.cfg.get("target_lang"):
            return
        if lang_available(code):
            self.switch_lang(code)
            return
        meta = lang_meta(code)
        QMessageBox.information(
            self.win, "该语种暂不可用",
            "「%s」当前版本没有内置本地翻译包，只有 中文 ↔ 英语 可用。\n"
            "识别模型本身支持多语种，缺的是对应方向的翻译包；不会假装提供一个下载按钮。\n\n"
            "现在仍继续使用%s。"
            % (meta.get("name"), lang_name(self.cfg.get("target_lang") or "en")))
        self._fill_lang_box()

    def apply_lang_fonts(self):
        """按目标语种切字体，并把 RTL 开关作用到表格/字幕，防止日韩文、西里尔文方块。"""
        try:
            from PySide6.QtGui import QFont
            from PySide6.QtCore import Qt as _Qt
            tl = str(self.cfg.get("target_lang") or "en")
            fam = lang_font(tl, self.cfg)
            rtl = bool(self.cfg.get("rtl_overlay")) or bool(lang_meta(tl).get("rtl"))
            for w in (self.cap_en, self.table):
                f = QFont(w.font())
                if fam:
                    f.setFamily(fam)
                w.setFont(f)
            self.cap_en.setLayoutDirection(_Qt.RightToLeft if rtl else _Qt.LeftToRight)
        except Exception as e:
            log("语种字体应用失败：" + repr(e))

    def refresh_chips(self):
        c = self.cfg
        self.chip_engine.setText("引擎：%s" % ("在线" if c.get("engine") == "online" else "离线"))
        # ASR 和翻译分开标注，别让「离线模型 small」混成一个说不清的模型
        dev_name = str(c.get("device") or "cpu").lower()
        dev_label = {"cpu": "CPU", "cuda": "GPU"}.get(dev_name, "")
        if dev_name == "auto":
            dev_label = "GPU" if find_cublas_dir() else "CPU"
        asr_name = str(c.get("whisper_model") or "auto")
        if asr_name == "auto":
            asr_name = default_asr_model()
        self.chip_asr.setText("ASR：%s - %s / %s" % (
            lang_name(c.get("source_lang") or "zh"), asr_name, dev_label))
        tl = str(c.get("target_lang") or "en")
        self.chip_trans.setText("翻译：%s · %s" % (
            lang_name(tl),
            "在线接口" if c.get("engine") == "online" else "Argos 离线"))
        dev = (c.get("loopback_device") or "系统默认扬声器")
        if len(dev) > 22:
            dev = dev[:20] + "…"
        self.chip_dev.setText("设备：%s" % dev)
        try:
            self.status.setToolTip(" ｜ ".join(w.text() for w in (
                self.chip_engine, self.chip_asr, self.chip_trans, self.chip_dev)))
        except Exception:
            pass
        if hasattr(self, "bar_model"):
            self.bar_model.setText("模型：ASR %s ｜ 翻译 %s" % (
                c.get("whisper_model") or "small",
                "在线" if c.get("engine") == "online" else lang_name(tl)))
            self.bar_audio.setText("音频：%s" % dev)

    def _drain(self):
        q = self.owner.rec_q
        got = 0
        while got < 40:
            try:
                item = q.get_nowait()
            except queue.Empty:
                break
            got += 1
            ts, zh, en = item[0], item[1], item[2]
            spk = item[3] if len(item) > 3 else ""
            if should_filter_short(zh, en, self.cfg):
                self._filtered_n = getattr(self, "_filtered_n", 0) + 1
                self._filtered_last = "%s / %s" % (zh, en)
                log("静默过滤短句：%s => %s" % (zh, en))
                continue                        # 短句过滤：不记录、不推字幕
            hit = self._find_repeat_row(zh, ts, spk)
            if hit is not None:
                self._bump_repeat(hit, ts)
                continue
            self.add_row(ts, zh, en, spk)
            append_history(ts, zh, en, spk, scene=str(self.cfg.get("scene") or ""))

    def _find_repeat_row(self, zh, ts, spk=""):
        """同一段音频里又出现同一句中文：找回来做计数，不新开一行。"""
        try:
            gap = int(self.cfg.get("session_gap") or 120)
            day = str(ts)[:10]
            t_new = _secs(ts)
            for row in range(min(self.table.rowCount(), 40)):
                d = self._row_data(row)
                if not d or str(d.get("zh", "")) != str(zh):
                    continue
                if spk and str(d.get("spk", "")) != str(spk):
                    continue
                if str(d.get("ts", ""))[:10] != day:
                    continue
                t_old = _secs(d.get("ts", ""))
                if t_old is not None and t_new is not None and abs(t_new - t_old) <= gap:
                    return row
        except Exception as e:
            log("重复句查找失败：" + repr(e))
        return None

    def _bump_repeat(self, row, ts):
        from PySide6.QtCore import Qt as _Qt
        d = self._row_data(row)
        if not d:
            return
        d["count"] = int(d.get("count") or 1) + 1
        n = d["count"]
        it = self.table.item(row, 2)
        if it is not None:
            it.setText("%s  ×%d" % (d.get("zh", ""), n))
            it.setToolTip("这句话本段音频里出现了 %d 次（同句已合并）" % n)
        item0 = self.table.item(row, 0)
        if item0 is not None:
            item0.setData(_Qt.UserRole, d)
        update_history_row(d.get("ts", ""), d.get("en", ""), {"count": n}, d.get("zh", ""))
        try:
            bump_pair_count(d.get("en", ""), d.get("zh", ""), n)
        except Exception:
            pass
        self.set_status("重复句已合并：×%d" % n, "ok")

    def _insert_row(self, ts, spk, en, zh, scene="", qual="", alts=None, count=1):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QTableWidgetItem
        self.table.insertRow(0)
        alts = alts or []
        shown_spk = spk_alias(spk, self.cfg)
        fav_on = (str(en).strip(), str(zh).strip()) in self._fav_keys
        scene_name = SCENE_SHORT.get(str(scene or ""), "未标")
        qual_mark = QUAL_MARKS.get(str(qual or ""), "—")
        en_show = str(en)
        if alts:
            en_show += "（+%d 备选）" % len(alts)
        zh_show = str(zh) if int(count or 1) <= 1 else "%s  ×%d" % (zh, int(count))
        # 记录要能看出先后：每行一个自增号，最新的号最大，直接写进时间列
        try:
            self._row_seq = int(getattr(self, "_row_seq", 0)) + 1
        except Exception:
            self._row_seq = 1
        vals = ("#%d  %s" % (self._row_seq, ts), shown_spk, zh_show, en_show,
                scene_name, qual_mark, "★" if fav_on else "☆", "▶")
        for i, v in enumerate(vals):
            it = QTableWidgetItem(str(v))
            it.setToolTip(str(v))
            if i == 0:
                # 最新一条：序号用强调色，一眼看出这条是刚发生的
                it.setForeground(QColor(tcol("accent", self.cfg)))
            if i == 1 and spk:
                it.setForeground(QColor(spk_color(spk, SPK_TEXT_COLORS)))
                it.setBackground(QColor(spk_tint(spk)))
            if i == 4:
                it.setForeground(QColor(tcol("muted", self.cfg)))
                it.setToolTip("点击这个标签 → 按该场景筛选（可多选）")
            if i == 5:
                it.setTextAlignment(Qt.AlignCenter)
                it.setToolTip({"native": "✅ 地道：正常进入复习池",
                               "machine": "⚠️ 机器感重：复习时降低权重",
                               "error": "❌ 识别错误：隔离，不进复习池"}.get(
                                   str(qual or ""), "点击循环标注：✅ 地道 → ⚠️ 机器感重 → ❌ 识别错误"))
                if qual == "error":
                    it.setForeground(QColor(tcol("err", self.cfg)))
                elif qual == "machine":
                    it.setForeground(QColor(tcol("warn", self.cfg)))
                elif qual == "native":
                    it.setForeground(QColor(tcol("accent", self.cfg)))
            if i == 6:
                it.setTextAlignment(Qt.AlignCenter)
                it.setForeground(QColor(tcol("accent", self.cfg) if fav_on
                                        else tcol("muted", self.cfg)))
            if i == 7:
                it.setTextAlignment(Qt.AlignCenter)
                it.setForeground(QColor(tcol("muted", self.cfg)))
                it.setToolTip("点一下听这句英文（对局口语略快）")
            self.table.setItem(0, i, it)
        self.table.item(0, 0).setData(Qt.UserRole, {
            "ts": ts, "en": en, "zh": zh, "spk": spk, "scene": scene,
            "qual": qual, "alts": alts, "count": int(count or 1)})
        self.table.resizeRowToContents(0)

    def add_row(self, ts, zh, en, spk=""):
        self.cap_zh.setText(zh)
        # 逐个词可点：说话人配色同步给词链接，没有说话人就用主文本色
        self.cap_en.set_text(
            en, spk_color(spk, SPK_TEXT_COLORS) if spk else tcol("text", self.cfg))
        if hasattr(self, "live_src"):
            self.live_src.setText("源：%s" % zh)
            self.live_tgt.setText("译：%s" % en)
            self.live_state.setText("识别中 · %s" % (spk or "—"))
        self._insert_row(ts, spk, en, zh, scene=str(self.cfg.get("scene") or ""))
        while self.table.rowCount() > self._max_rows:
            self.table.removeRow(self.table.rowCount() - 1)
        self.table.scrollToTop()
        self._refresh_recent_preview()

    def _refresh_recent_preview(self):
        """Capture 首页空位里的最近三句；完整记录仍在历史抽屉。"""
        try:
            n = min(3, self.table.rowCount())
            for i, (rw, z, e) in enumerate(self.recent_rows):
                if i < n:
                    it_z = self.table.item(i, 2)
                    it_e = self.table.item(i, 3)
                    z.setText(it_z.text() if it_z is not None else "")
                    e.setText(it_e.text() if it_e is not None else "")
                    rw.setVisible(True)
                else:
                    rw.setVisible(False)
            self.cap_empty_hint.setVisible(n == 0)
        except Exception as e:
            log("刷新最近三句失败：" + repr(e))
        self._sync_preview_star()

    def _toggle_nav(self):
        """侧栏展开/收起：只改侧栏自身宽度，右侧内容区宽度由布局自动吸收，不产生位移跳变。"""
        self.nav_expanded = not self.nav_expanded
        self.nav_frame.setFixedWidth(168 if self.nav_expanded else 52)
        self.btn_nav_toggle.setFixedWidth(156 if self.nav_expanded else 40)
        for b in self.nav_buttons:
            b.set_expanded(self.nav_expanded)
        self.cfg["nav_expanded"] = self.nav_expanded
        save_config(self.cfg)

    def _goto_tab(self, idx):
        """统一切页出口：同步侧栏高亮、Library 次级导航、次级导航条显隐。"""
        idx = max(0, min(idx, self.tabs.count() - 1))
        self.tabs.setCurrentIndex(idx)
        self._sync_nav_state(idx)

    def _lab_nav(self, idx):
        """Lab 内部分组：一次只看一组，避免八张卡一屏堆满。"""
        try:
            idx = max(0, min(idx, self.lab_stack.count() - 1))
            self.lab_stack.setCurrentIndex(idx)
            for i, b in enumerate(self.lab_nav_btns):
                b.setChecked(i == idx)
            self.cfg["lab_section"] = idx
        except Exception as e:
            log("Lab 分组切换失败：" + repr(e))

    def _sync_nav_state(self, idx=None):
        try:
            if idx is None:
                idx = self.tabs.currentIndex()
            if idx == 0:
                group = "Capture"
            elif idx == 6:
                group = "Lab"
            elif idx == 7:
                group = "Talk"
            else:
                group = "Library"
            for b in self.nav_buttons:
                b.setChecked(b.property("grp") == group)
            # Library 的 5 个子页才显示次级导航
            self.lib_subnav.setVisible(group == "Library")
            # 当前字幕只在 Capture 页有意义，切到其他模块不占屏
            self.cap_frame.setVisible(group == "Capture")
            for idx_set, b in self._lib_sub_btns:
                b.setChecked(idx in idx_set)
            if 1 <= idx <= 5:
                self.cfg["last_lib_tab"] = idx
        except Exception as e:
            log("切页状态同步失败：" + repr(e))

    def _ensure_talk_view(self):
        """懒加载情景译：第一次点「说」才创建 Web 视图，冷启动不受影响。"""
        if getattr(self, "_talk_view", None) is not None:
            return self._talk_view
        try:
            from PySide6.QtCore import QUrl
            from PySide6.QtWebEngineWidgets import QWebEngineView
            port = scenetalk_port()
            view = QWebEngineView(self.talk_tab)
            lay = self.talk_tab.layout()
            if lay is not None:
                lay.replaceWidget(self.talk_ph, view)
            self.talk_ph.hide()
            view.setUrl(QUrl("http://127.0.0.1:%d/SceneTalk.html" % port))
            self._talk_view = view
            log("情景译已嵌入主程序：127.0.0.1:%d" % port)
        except Exception as e:
            log("情景译载入失败：" + repr(e))
            try:
                self.talk_ph.setText("情景译载入失败：%s" % e)
            except Exception:
                pass
        return getattr(self, "_talk_view", None)

    def _nav_go(self, prefix):
        """侧栏四个入口：Capture / Library / Talk / Lab。Library 落到上次停留的子页。"""
        if prefix == "Capture":
            self._goto_tab(0)
        elif prefix == "Lab":
            self._goto_tab(6)
        elif prefix == "Talk":
            # 情景译是内嵌网页：第一次进来才启动 Chromium，不拖慢冷启动
            self._ensure_talk_view()
            self._goto_tab(7)
        else:
            last = int(self.cfg.get("last_lib_tab") or 1)
            self._goto_tab(last if 1 <= last <= 5 else 1)

    def _rename_session(self):
        txt = self.lbl_session.text().strip()
        if " · 20" in txt:
            txt = txt.split(" · 20")[0]
        self.cfg["session_name"] = txt
        save_config(self.cfg)
        self._sync_capture_ui()
        self.set_status("会话已重命名：%s" % (txt or "未命名"), "ok")

    def _sync_capture_ui(self):
        # 三态：未开始 / 收听中 / 已暂停。之前把“未开始”也画成“暂停捕获”，
        # 第一次点反而进暂停，得点两次才真正开始。
        running = bool(getattr(self.owner, "capture", None)
                       and getattr(self.owner.capture, "running", False))
        paused = bool(getattr(self.owner, "paused", False))
        if not running and not paused:
            self.btn_capture.setText("开始捕获")
            state, tone = "未开始", ""
        elif paused:
            self.btn_capture.setText("继续捕获")
            state, tone = "已暂停", "warn"
        else:
            self.btn_capture.setText("暂停捕获")
            state, tone = "收听中", "ok"
        try:
            self.owner.main._badge_set(state, tone)
        except Exception:
            pass
        self.live_state.setText(state)
        self.lbl_src_lang.setText("源语种：%s" % lang_name(self.cfg.get("source_lang") or "zh"))
        self.lbl_tgt_lang.setText("目标语种：%s" % lang_name(self.cfg.get("target_lang") or "en"))
        dev = str(self.cfg.get("loopback_device") or "系统默认")
        self.lbl_audio_dev.setText("音频源：%s" % (dev if len(dev) <= 18 else dev[:16] + "…"))
        scene = SCENE_SHORT.get(str(self.cfg.get("scene") or ""), "未标注")
        custom = str(self.cfg.get("session_name") or "").strip()
        head = custom or ("本会话 · %s" % scene)
        if not self.lbl_session.hasFocus():
            self.lbl_session.setText("%s · %s" % (head, time.strftime("%Y-%m-%d %H:%M")))
        if hasattr(self, "top_session"):
            self.top_session.setText("会话：%s" % scene)
        if hasattr(self, "top_pair"):
            self.top_pair.setText("%s → %s" % (
                lang_name(self.cfg.get("source_lang") or "zh"),
                lang_name(self.cfg.get("target_lang") or "en")))
        if hasattr(self, "bar_state"):
            self.bar_state.setText("已暂停" if paused else "捕获中")
            self.bar_audio.setText("音频：%s" % (
                "已暂停" if paused else (dev if len(dev) <= 16 else dev[:14] + "…")))

    def toggle_pin(self, on):
        from PySide6.QtCore import Qt as _PinQt
        self.win.setWindowFlag(_PinQt.WindowStaysOnTopHint, bool(on))
        self.win.show()
        self.cfg["pinned"] = bool(on)
        save_config(self.cfg)
        self.set_status("窗口置顶：%s" % ("开" if on else "关"), "ok")

    def show_help(self):
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.information(
            self.win, "Oversub 帮助",
            "模块\n"
            "  Capture 捕获台：实时字幕卡片 + 捕获控制；历史在底部抽屉。\n"
            "  Library 语言库：素材库 / 生词本 / 收藏 / 会话 / 句对。\n"
            "  Lab 实验室：语种、模型、词典、识别规则、外观。\n\n"
            "核心快捷键（默认 Ctrl+Alt+字母，全部可在设置里改）\n"
            "  %s  开始 / 暂停捕获\n"
            "  %s  收藏当前句\n"
            "  %s  唤起 / 隐藏悬浮字幕\n"
            "  %s  打开素材库\n"
            "  %s  切换自测模式\n"
            "  %s  字幕交互模式\n"
            "  %s  历史面板\n"
            "  %s  清空字幕流\n"
            "  （F5~F10 / Ctrl+L / Ctrl+, 已不再默认占用，避让游戏与浏览器）"
            % (self.cfg.get("hotkey_f5_capture") or "Ctrl+Alt+P",
               self.cfg.get("hotkey_f6_fav") or "Ctrl+Alt+U",
               self.cfg.get("hotkey_subtitle") or "Ctrl+Alt+V",
               self.cfg.get("hotkey_library") or "Ctrl+Alt+L",
               self.cfg.get("hotkey_selftest") or "Ctrl+Alt+T",
               self.cfg.get("hotkey_toggle") or "Ctrl+Alt+G",
               self.cfg.get("hotkey_history") or "Ctrl+Alt+H",
               self.cfg.get("hotkey_clear") or "Ctrl+Alt+X"))

    def toggle_selftest_shortcut(self):
        on = not self.chk_selftest.isChecked()
        self.chk_selftest.setChecked(on)
        try:
            if on:
                self.lib_mode_in.setChecked(True)      # 自测时看译文回忆源文本
            else:
                self.lib_mode_out.setChecked(True)
            self.lib_revealed = False
            self.lib_render()
        except Exception:
            pass
        self.set_status("自测模式：%s" % ("开" if on else "关"), "ok")

    def _on_capture_btn(self):
        try:
            running = bool(getattr(self.owner, "capture", None)
                           and getattr(self.owner.capture, "running", False))
            if not running and not getattr(self.owner, "paused", False):
                # 未开始 → 第一次点击就是开始，而不是被当成“暂停”
                self.owner.start_capture()
            else:
                self.owner.toggle_pause()
        except Exception as e:
            log("切换捕获失败：" + repr(e))
        self._sync_capture_ui()

    def _on_skin_chip(self):
        try:
            k = str(self.ov_skin.currentData() or "dialog_dark")
            self.cfg["overlay_skin"] = k
            ov = self.owner.overlay
            ov.cfg["overlay_skin"] = k
            ov.update_look()
            ov._render_history()
            ov._fit_height()
            save_config(self.cfg)
            self.set_status("字幕皮肤：" + (OVERLAY_SKINS.get(k) or {}).get("name", k), "ok")
        except Exception as e:
            log("切换字幕皮肤失败：" + repr(e))

    def _on_pos_chip(self):
        try:
            p = str(self.ov_pos.currentData() or "top")
            self.cfg["overlay_pos"] = p
            self.owner.overlay.apply_position(preset=p, save=True)
            self.set_status("字幕位置：" + str(self.ov_pos.currentText()), "ok")
        except Exception as e:
            log("切换字幕位置失败：" + repr(e))

    def _font_step(self, step):
        try:
            fs = int(self.cfg.get("font_size") or 22)
            fs = max(14, min(48, fs + int(step) * 2))
            self.cfg["font_size"] = fs
            self.lbl_fs.setText("字号 %d" % fs)
            self.owner.overlay.cfg["font_size"] = fs
            self.owner.overlay.update_look()
            self.owner.overlay._fit_height()
            save_config(self.cfg)
        except Exception as e:
            log("调整字幕字号失败：" + repr(e))

    def _toggle_hist_drawer(self):
        vis = self.hist_box.isHidden()
        self.hist_box.setVisible(vis)
        # 抽屉收起时留白吸收空间；展开时把空间全部让给历史列表
        try:
            self.cap_filler.setVisible(not vis)
        except Exception:
            pass
        if vis:
            fade_in(self.hist_box)
        self.btn_hist_toggle.setText("收起历史 ▴" if vis else "展开历史 ▾")

    def on_anim_toggle(self, on):
        ANIM["on"] = bool(on)
        self.cfg["animations"] = bool(on)
        save_config(self.cfg)
        self.set_status("界面动画：%s" % ("开" if on else "关（瞬时切换）"), "ok")

    def on_tts_queue_max(self, n):
        self.cfg["tts_queue_max"] = int(n)
        save_config(self.cfg)
        self.set_status("朗读队列上限：%d 句" % int(n), "ok")

    def on_ui_lang(self, idx):
        code = self.lab_uilang.itemData(idx) or "zh"
        self.cfg["ui_lang"] = code
        save_config(self.cfg)
        apply_ui_lang(self.win, code)
        apply_ui_lang(self.owner.overlay, code)
        self.set_status("界面语言：%s" % ("English" if code == "en" else "中文"), "ok")

    # ---------------- TTS 配置 ----------------
    def _tts_lang(self):
        return self.tts_lang_box.currentData() or "en"

    def lab_tts_refresh(self):
        code = self._tts_lang()
        voices = list(self.owner.overlay.tts._installed())
        self.tts_voice_box.blockSignals(True)
        self.tts_voice_box.clear()
        self.tts_voice_box.addItem("系统推荐（自动）", "")
        for v in voices:
            self.tts_voice_box.addItem(v.split(" - ")[0], v)
        cur = (self.cfg.get("tts_voices") or {}).get(code) or ""
        idx = self.tts_voice_box.findData(cur)
        self.tts_voice_box.setCurrentIndex(max(0, idx))
        self.tts_voice_box.blockSignals(False)
        scale = float((self.cfg.get("tts_rate_scales") or {}).get(code) or 1.0)
        self.tts_rate.blockSignals(True)
        self.tts_rate.setValue(int(round(scale * 100)))
        self.tts_rate.blockSignals(False)
        self.tts_rate_val.setText("%.2fx" % scale)
        vol = int((self.cfg.get("tts_volumes") or {}).get(code, 100))
        self.tts_vol.blockSignals(True)
        self.tts_vol.setValue(vol)
        self.tts_vol.blockSignals(False)
        self.tts_vol_val.setText(str(vol))
        self.tts_min.blockSignals(True)
        self.tts_min.setValue(int((self.cfg.get("tts_min_words") or {}).get(
            code, self.cfg.get("min_words") or 2)))
        self.tts_min.blockSignals(False)
        self.tts_qmax.blockSignals(True)
        self.tts_qmax.setValue(int(self.cfg.get("tts_queue_max") or 3))
        self.tts_qmax.blockSignals(False)
        self.tts_gap.blockSignals(True)
        self.tts_gap.setValue(float(self.cfg.get("tts_gap") or 0.4))
        self.tts_gap.blockSignals(False)
        self.tts_loop_gap.blockSignals(True)
        self.tts_loop_gap.setValue(float(self.cfg.get("tts_loop_pause") or 1.2))
        self.tts_loop_gap.blockSignals(False)
        for chk, key, default in ((self.chk_tts_hl, "word_hl", True),
                                  (self.chk_tts_dup, "tts_skip_duplicate", True),
                                  (self.chk_tts_full, "tts_skip_fullscreen", True),
                                  (self.chk_tts_duck, "tts_duck_only", False),
                                  (self.chk_shadow, "shadow_mode", False),
                                  (self.chk_shadow_repeat, "shadow_repeat", True)):
            chk.blockSignals(True)
            chk.setChecked(bool(self.cfg.get(key, default)))
            chk.blockSignals(False)
        self.tts_pack_hint.setText("当前语种 TTS：%s ｜ 音色来自系统 SAPI，离线可用"
                                   % LANG_REGISTRY.get(code, {}).get("tts", "SAPI"))
        if hasattr(self, "tts_out_dev"):
            self.tts_out_dev.blockSignals(True)
            self.tts_out_dev.setText(str(self.cfg.get("tts_output_device") or ""))
            self.tts_out_dev.blockSignals(False)

    def lab_tts_apply(self):
        code = self._tts_lang()
        voices = dict(self.cfg.get("tts_voices") or {})
        voices[code] = self.tts_voice_box.currentData() or ""
        self.cfg["tts_voices"] = voices
        scales = dict(self.cfg.get("tts_rate_scales") or {})
        scales[code] = round(self.tts_rate.value() / 100.0, 2)
        self.cfg["tts_rate_scales"] = scales
        vols = dict(self.cfg.get("tts_volumes") or {})
        vols[code] = int(self.tts_vol.value())
        self.cfg["tts_volumes"] = vols
        mins = dict(self.cfg.get("tts_min_words") or {})
        mins[code] = int(self.tts_min.value())
        self.cfg["tts_min_words"] = mins
        self.cfg["tts_queue_max"] = int(self.tts_qmax.value())
        self.cfg["tts_gap"] = round(float(self.tts_gap.value()), 2)
        self.cfg["tts_loop_pause"] = round(float(self.tts_loop_gap.value()), 2)
        self.cfg["word_hl"] = bool(self.chk_tts_hl.isChecked())
        self.cfg["tts_skip_duplicate"] = bool(self.chk_tts_dup.isChecked())
        self.cfg["tts_skip_fullscreen"] = bool(self.chk_tts_full.isChecked())
        self.cfg["tts_duck_only"] = bool(self.chk_tts_duck.isChecked())
        self.cfg["shadow_mode"] = bool(self.chk_shadow.isChecked())
        self.cfg["shadow_repeat"] = bool(self.chk_shadow_repeat.isChecked())
        save_config(self.cfg)
        self.tts_rate_val.setText("%.2fx" % (self.tts_rate.value() / 100.0))
        self.tts_vol_val.setText(str(self.tts_vol.value()))
        self.set_status("朗读设置已更新（%s）" % LANG_REGISTRY.get(code, {}).get("name", code), "ok")

    def lab_tts_device(self):
        name = self.tts_out_dev.text().strip()
        self.cfg["tts_output_device"] = name
        save_config(self.cfg)
        try:
            self.owner.overlay.tts.set_output_device(name)
        except Exception as e:
            log("设置朗读输出设备失败：" + repr(e))
        self.set_status("朗读输出：%s" % (name or "系统默认"), "ok")

    def lab_tts_pack(self):
        from PySide6.QtWidgets import QMessageBox
        code = self._tts_lang()
        # 音色来自 Windows 自己的 SAPI，软件里没有“音色包”可下载，这里只做真实检测 + 指路
        _kw = {"zh": "Chinese", "en": "English", "ja": "Japanese", "ko": "Korean",
               "fr": "French", "de": "German", "es": "Spanish", "ar": "Arabic"}
        names = []
        try:
            names = list(getattr(self.owner.overlay.tts, "_voices", []) or [])
        except Exception:
            names = []
        word = _kw.get(code, "")
        have = [v for v in names if word and word.lower() in str(v).lower()]
        label = LANG_REGISTRY.get(code, {}).get("name", code)
        if have:
            QMessageBox.information(
                self.win, "朗读音色",
                "朗读用的是 Windows 自带语音（SAPI），软件内不需要下载音色包。\n\n"
                "语种：%s\n系统里检测到：%s\n\n"
                "想加更多音色：Windows 设置 → 时间和语言 → 语言 → 添加语言，勾选「语音包」。"
                % (label, "、".join(have[:5])))
        else:
            QMessageBox.warning(
                self.win, "系统里没有这个语种的音色",
                "「%s」在系统里没有找到语音包，朗读按钮会是灰的。\n\n"
                "安装方法：Windows 设置 → 时间和语言 → 语言 → 添加语言 → "
                "在「语音」一栏勾上对应语音包，装完回到这里直接可用。" % label)

    def _pick_scene_tag(self):
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self.win)
        scenes = {"fps": "FPS / 游戏", "video": "看视频", "chat": "语音聊天"}
        recents = [k for k in (self.cfg.get("scene_recent") or []) if k in scenes][:3]
        for key in recents:
            act = menu.addAction("最近 · %s" % scenes[key])
            act.triggered.connect(lambda _c=False, k=key: self._set_scene_tag(k))
        if recents:
            menu.addSeparator()
        for key, label in scenes.items():
            act = menu.addAction(label)
            act.triggered.connect(lambda _c=False, k=key: self._set_scene_tag(k))
        menu.exec(self.btn_live_tag.mapToGlobal(self.btn_live_tag.rect().bottomLeft()))

    def _set_scene_tag(self, key):
        self.cfg["scene"] = key
        recent = [k for k in (self.cfg.get("scene_recent") or []) if k != key]
        self.cfg["scene_recent"] = ([key] + recent)[:3]
        save_config(self.cfg)
        self._sync_capture_ui()
        self.set_status("本会话场景：%s" % SCENE_SHORT.get(key, key), "ok")

    # ---------------- Library 素材库 ----------------
    def _lib_state_of(self, u, recs):
        return review_label(u["id"], recs)

    def refresh_library(self, _arg=None, force=False):
        from PySide6.QtWidgets import QTableWidgetItem
        now = time.time()
        if force or not hasattr(self, "_lib_all") or now - getattr(self, "_lib_ts", 0) > 8:
            try:
                self._lib_all = read_utterances(None, limit=2000)
            except Exception as e:
                log("读素材失败：" + repr(e))
                self._lib_all = []
            self._lib_ts = now
        else:
            self._lib_all = getattr(self, "_lib_all", [])
        recs = read_review()
        pairs = sorted({(u["source_lang"], u["target_lang"]) for u in self._lib_all})
        want = ["全部语种对"] + ["%s→%s" % (lang_name(a), lang_name(b)) for a, b in pairs]
        cur = self.lib_pair.currentText()
        self.lib_pair.blockSignals(True)
        if [self.lib_pair.itemText(i) for i in range(self.lib_pair.count())] != want:
            self.lib_pair.clear()
            for i, lab in enumerate(want):
                self.lib_pair.addItem(lab)
                if i and pairs:
                    self.lib_pair.setItemData(i, pairs[i - 1])
        self.lib_pair.setCurrentText(cur if cur in want else want[0])
        self.lib_pair.blockSignals(False)

        self.lib_scene.blockSignals(True)
        cur_s = self.lib_scene.currentText()
        self.lib_scene.clear()
        self.lib_scene.addItem("全部场景")
        self.lib_scene.addItem("仅未标注")
        for k, lab in (("fps", "FPS / 游戏"), ("video", "看视频"), ("chat", "语音聊天")):
            self.lib_scene.addItem(lab)
            self.lib_scene.setItemData(self.lib_scene.count() - 1, k)
        self.lib_scene.setCurrentText(cur_s if cur_s else "全部场景")
        self.lib_scene.blockSignals(False)

        self._lib_rows = self._lib_filtered(recs)
        due = sum(1 for u in self._lib_rows if self._lib_state_of(u, recs) == "待复习")
        new_n = sum(1 for u in self._lib_rows if self._lib_state_of(u, recs) == "新")
        self.lib_due.setText("待复习 %d · 新 %d · 共 %d" % (
            due, new_n, len(self._lib_rows)))
        view = self.lib_view.currentIndex()
        if not self._lib_rows:
            self.lib_stack.setCurrentIndex(5)      # 空状态页：引导去 Capture
            if hasattr(self, "lib_bulk"):
                self.lib_bulk.hide()
            return
        self.lib_stack.setCurrentIndex(view)
        if view == 0:
            self._lib_fill_timeline()
        elif view == 1:
            self.lib_render()
        elif view == 2:
            self._lib_fill_cluster()
        elif view == 3:
            self._lib_fill_sessions()
        else:
            self._lib_fill_speakers()

    def _lib_filtered(self, recs=None):
        recs = recs or read_review()
        q = self.lib_search.text().strip().lower()
        pair = self.lib_pair.currentData()
        scene = self.lib_scene.currentData()
        state_sel = self.lib_state.currentText()
        qual_sel = self.lib_quality.currentText()
        fav_only = self.lib_chk_fav.isChecked()
        audio_only = self.lib_chk_audio.isChecked()
        unread_only = getattr(self, "lib_chk_unread", None) is not None and self.lib_chk_unread.isChecked()
        heard = read_texts() if unread_only else set()
        fav_keys = self._fav_keys if hasattr(self, "_fav_keys") else set()
        out = []
        for u in self._lib_all:
            if pair and (u["source_lang"], u["target_lang"]) != tuple(pair):
                continue
            tags = " ".join(str(t) for t in (u.get("tags") or []))
            if scene:
                if scene == "仅未标注" and u.get("tags"):
                    continue
                if scene != "仅未标注" and scene not in (u.get("tags") or []):
                    continue
            if state_sel != "全部状态" and self._lib_state_of(u, recs) != state_sel:
                continue
            qmap = {"✅地道": "ok", "⚠️机器感": "machine", "❌识别错误": "error"}
            if qual_sel != "全部质量" and u.get("quality") != qmap.get(qual_sel):
                continue
            if fav_only and (u["target_text"].strip(), u["source_text"].strip()) not in fav_keys:
                continue
            if audio_only and not u.get("audio_snippet"):
                continue
            if unread_only and (u["target_text"] or "").strip() in heard:
                continue
            if q and q not in (u["source_text"] + " " + u["target_text"] + " " + tags).lower():
                continue
            out.append(u)
        return list(reversed(out))

    def _lib_fill_timeline(self):
        from PySide6.QtWidgets import QTableWidgetItem
        recs = read_review()
        self._lib_recs = recs
        self._lib_tl_loaded = 0
        self.lib_time.setRowCount(0)
        if not getattr(self, "_lib_tl_conn", False):
            self._lib_tl_conn = True
            self.lib_time.verticalScrollBar().valueChanged.connect(self._lib_timeline_more)
        self._lib_timeline_page(80)

    def _lib_timeline_page(self, n):
        """增量渲染：先画 80 行，滚到底再补下一批，几千条也不一次性建控件。"""
        from PySide6.QtWidgets import QTableWidgetItem
        recs = getattr(self, "_lib_recs", read_review())
        start = getattr(self, "_lib_tl_loaded", 0)
        end = min(len(self._lib_rows), start + n)
        for i in range(start, end):
            u = self._lib_rows[i]
            self.lib_time.insertRow(i)
            for j, v in enumerate((u["created_at"], u["source_text"], u["target_text"],
                                   u["speaker_id"], self._lib_state_of(u, recs))):
                self.lib_time.setItem(i, j, QTableWidgetItem(str(v)))
        self._lib_tl_loaded = end

    def _lib_timeline_more(self, _v=None):
        bar = self.lib_time.verticalScrollBar()
        if bar.value() >= bar.maximum() - 24:
            if getattr(self, "_lib_tl_loaded", 0) < len(self._lib_rows):
                self._lib_timeline_page(80)

    def _lib_fill_cluster(self):
        from PySide6.QtWidgets import QTableWidgetItem
        import difflib            # 源句近似聚类：同一意思的不同说法并到一组
        clusters = []
        for u in self._lib_rows:
            src = u["source_text"].strip()
            placed = False
            for c in clusters:
                if difflib.SequenceMatcher(None, src, c["rep"]).ratio() >= 0.62:
                    c["src"].append(src)
                    c["tgt"].append(u["target_text"].strip())
                    placed = True
                    break
            if not placed:
                clusters.append({"rep": src, "src": [src], "tgt": [u["target_text"].strip()]})
        self.lib_cluster.setRowCount(0)
        for i, c in enumerate(sorted(clusters, key=lambda x: -len(x["tgt"]))):
            self.lib_cluster.insertRow(i)
            srcs = []
            for x in c["src"]:
                if x not in srcs:
                    srcs.append(x)
            tgts = []
            for x in c["tgt"]:
                if x not in tgts:
                    tgts.append(x)
            self.lib_cluster.setItem(i, 0, QTableWidgetItem("  ／  ".join(srcs[:3])))
            self.lib_cluster.setItem(i, 1, QTableWidgetItem("%d 句 / %d 种说法" % (
                len(c["tgt"]), len(tgts))))
            self.lib_cluster.setItem(i, 2, QTableWidgetItem("  ／  ".join(tgts)))
        self.lib_cluster.resizeRowsToContents()

    def _lib_sessions(self):
        groups = {}
        for u in self._lib_rows:
            groups.setdefault(u["session_id"], []).append(u)
        return groups

    def _lib_fill_sessions(self):
        from PySide6.QtWidgets import QTableWidgetItem
        self.lib_sess.setRowCount(0)
        for i, (sid, us) in enumerate(self._lib_sessions().items()):
            self.lib_sess.insertRow(i)
            tags = [t for t in (us[-1].get("tags") or []) if t]
            scene = SCENE_SHORT.get(tags[0], tags[0]) if tags else "未标注"
            self.lib_sess.setItem(i, 0, QTableWidgetItem("%s · %s" % (
                SCENE_SHORT.get(tags[0], "未标注") if tags else "未标注", sid)))
            self.lib_sess.setItem(i, 1, QTableWidgetItem("%s → %s" % (
                us[-1]["created_at"], us[0]["created_at"])))
            self.lib_sess.setItem(i, 2, QTableWidgetItem(str(len(us))))
            self.lib_sess.setItem(i, 3, QTableWidgetItem(scene))
        self.lib_sess.resizeRowsToContents()

    def _lib_fill_speakers(self):
        from PySide6.QtWidgets import QTableWidgetItem
        groups = {}
        for u in self._lib_rows:
            groups.setdefault(u["speaker_id"] or "未识别", []).append(u)
        self.lib_spk.setRowCount(0)
        for i, (spk, us) in enumerate(sorted(groups.items(), key=lambda kv: -len(kv[1]))):
            self.lib_spk.insertRow(i)
            self.lib_spk.setItem(i, 0, QTableWidgetItem(spk))
            self.lib_spk.setItem(i, 1, QTableWidgetItem(str(len(us))))
            self.lib_spk.setItem(i, 2, QTableWidgetItem("%s ｜ %s" % (
                us[0]["source_text"], us[0]["target_text"])))
        self.lib_spk.resizeRowsToContents()

    def lib_render(self):
        if not self._lib_rows:
            self.lib_card_meta.setText("还没有素材：回 Capture 捕获几句，再回来复习")
            self.lib_card_front.setText("—")
            self.lib_card_back.setText("")
            self.lib_card_ctx.setText("")
            return
        self._lib_idx = max(0, min(self._lib_idx, len(self._lib_rows) - 1))
        u = self._lib_rows[self._lib_idx]
        recs = read_review()
        out_mode = self.lib_mode_out.isChecked()
        front, back = (u["source_text"], u["target_text"]) if out_mode else (
            u["target_text"], u["source_text"])
        self.lib_card_front.setText(front)
        self.lib_card_back.setText(back if self._lib_revealed else "（点「显示答案」再揭晓）")
        tags = "·".join(SCENE_SHORT.get(t, t) for t in (u.get("tags") or [])) or "未标注"
        qual = {"ok": "✅地道", "machine": "⚠️机器感", "error": "❌识别错误"}.get(
            u.get("quality"), "—")
        self.lib_card_meta.setText("%s ｜ %s ｜ %s ｜ %s ｜ %d/%d" % (
            u["created_at"], u["speaker_id"] or "—", tags, qual,
            self._lib_idx + 1, len(self._lib_rows)))
        prev = next_txt = ""
        for c in u.get("context") or []:
            t = c.get("created_at", "")
            if t and t < u["created_at"]:
                prev = c.get("target_text", "")
            elif t and t > u["created_at"] and not next_txt:
                next_txt = c.get("target_text", "")
        self.lib_card_ctx.setText("上下文　上一句：%s　下一句：%s" % (prev or "—", next_txt or "—"))
        self.lib_card_prag.setText(self._prag_hint(u, out_mode))

    def _prag_hint(self, u, out_mode):
        """规则推断的语用提示：不联网、不编模型输出，只给场景与句式边界。"""
        tl = (u["target_text"] or "").lower()
        scene = (u.get("tags") or [""])[0]
        words = len(split_words(u["target_text"], u["target_lang"] or "en"))
        slang = any(w in tl for w in (" gonna", " wanna", " ain't", " dude", " bro", " lol"))
        formal = any(w in tl for w in ("would you", "could you", "please", "may i", "excuse me"))
        if scene == "fps":
            use = "对局内即时喊话，适合交火 / 配合，赛后复盘也自然"
            avoid = "不要照搬进正式邮件或陌生场合"
        elif scene == "video":
            use = "影视 / 视频语境，适合理解台词与语感"
            avoid = "台词可能带戏剧化语气，日常对话要降调"
        elif scene == "chat":
            use = "语音闲聊，适合熟人之间的自然回应"
            avoid = "正式书面场合建议换成完整句"
        else:
            use = "通用语境，可作为该含义的参考表达"
            avoid = "使用前先看上下文，避免跨场景误用"
        tone = "偏口语" if slang else ("偏正式" if formal else "中性")
        length = "短句，反应快" if words and words <= 6 else "完整句，信息更全"
        return "语用（规则推断）：语气%s、%s ｜ 适合：%s ｜ 注意：%s" % (
            tone, length, use, avoid)

    def lib_reveal(self):
        self._lib_revealed = True
        self.lib_render()

    def lib_step(self, step):
        if not self._lib_rows:
            return
        self._lib_idx = (self._lib_idx + step) % len(self._lib_rows)
        self._lib_revealed = False
        self.lib_render()

    def lib_mark(self, grade):
        if not self._lib_rows:
            return
        u = self._lib_rows[self._lib_idx]
        rec = mark_review(u["id"], grade)
        self.set_status("已标记：%s → %s（下次 %s）" % (
            "掌握" if grade == "known" else "再练", review_label(u["id"]), rec.get("due", "")), "ok")
        self.refresh_library(force=True)

    def lib_dictation(self):
        """听写模式：藏译文，只播发音；写完再点「显示答案」对照。"""
        try:
            if hasattr(self, "lib_mode_out") and not self.lib_mode_out.isChecked():
                self.lib_mode_out.setChecked(True)
            self._lib_revealed = False
            self.lib_render()
            self.lib_play()
            self.set_status("听写：先听发音写下来，再点「显示答案」对照", "ok")
        except Exception as e:
            log("听写模式失败：" + repr(e))

    def lib_play(self):
        if not self._lib_rows:
            return
        u = self._lib_rows[self._lib_idx]
        try:
            self.owner.overlay.read_text(u["target_text"], lang=u.get("target_lang") or "en")
        except Exception as e:
            log("素材朗读失败：" + repr(e))

    def lib_add_word(self):
        if not self._lib_rows:
            return
        u = self._lib_rows[self._lib_idx]
        words = split_words(u["target_text"], u["target_lang"] or "en")
        word = max(words, key=len) if words else u["target_text"][:20]
        append_word(word, u["source_text"], u["target_text"], u["speaker_id"],
                    scene=(u.get("tags") or [""])[0])
        self.set_status("已加入生词本：%s" % word, "ok")

    def lib_add_synonym(self):
        from PySide6.QtWidgets import QInputDialog
        if not self._lib_rows:
            return
        u = self._lib_rows[self._idx_safe()]
        alt, ok = QInputDialog.getText(self.win, "补充同义表达",
                                       "另一种目标语言说法（和「%s」同义）：" % u["source_text"])
        if not ok or not alt.strip():
            return
        if append_history_alt(u["created_at"], u["target_text"], alt.strip(), u["source_text"]):
            self.set_status("已补充备选表达：%s" % alt.strip(), "ok")
            self.refresh_library(force=True)
        else:
            self.set_status("这条记录找不到（可能已压缩成旧日期），补充失败", "warn")

    def _lib_bulk_rows(self):
        rows = sorted({i.row() for i in self.lib_time.selectionModel().selectedRows()})
        out = []
        for r in rows:
            if 0 <= r < len(getattr(self, "_lib_rows", [])):
                out.append(self._lib_rows[r])
        return out

    def _lib_bulk_update(self):
        n = len(self._lib_bulk_rows())
        self.lib_bulk_lbl.setText("已选 %d 条" % n)
        self.lib_bulk.setVisible(bool(n))
        if n:
            self.lib_bulk_lbl.setToolTip("Shift 可连续多选；Ctrl 可跳选")

    def _lib_bulk_action(self, action):
        rows = self._lib_bulk_rows()
        if not rows:
            return
        if action == "known":
            for u in rows:
                mark_review(u["id"], "known")
            self.refresh_library(force=True)
            self.set_status("已把 %d 条标记为掌握" % len(rows), "ok")
        elif action == "export":
            self.lib_export_rows(rows)
        elif action == "read":
            ov = self.owner.overlay
            for u in rows:
                lang = str(u.get("target_lang") or "en")
                text = (u.get("target_text") or "").strip()
                if not text:
                    continue
                r = ov._tts_rate_for(lang)
                vol = int((self.cfg.get("tts_volumes") or {}).get(lang, 100))
                ov._read_jobs.put((text, r, (self.cfg.get("tts_voices") or {}).get(lang),
                                   lang, vol, 0.0))
                mark_read_text(text)
            ov._ensure_read_thread()
            ov._update_queue_hint()
            self.set_status("连续朗读 %d 条素材（听力训练）" % len(rows), "ok")
        elif action == "delete":
            from PySide6.QtWidgets import QMessageBox
            ok = QMessageBox.question(
                self.win, "批量删除",
                "将删除选中的 %d 条素材（危险操作，不可撤销）。继续？" % len(rows),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ok != QMessageBox.Yes:
                return
            keys = [(u["created_at"], u["target_text"]) for u in rows]
            delete_history_entries(str(rows[0]["created_at"])[:10], keys)
            self.lib_time.clearSelection()
            self.refresh_library(force=True)
            self.set_status("已删除 %d 条素材" % len(rows), "ok")

    def lib_export_rows(self, rows):
        from PySide6.QtWidgets import QFileDialog
        import csv
        path, _ = QFileDialog.getSaveFileName(self.win, "导出选中素材",
                                              str(ROOT / "oversub_selected.csv"), "CSV (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["时间", "源文本", "目标译文", "source_lang", "target_lang",
                            "说话人", "标签", "质量", "记忆状态"])
                for u in rows:
                    w.writerow([u["created_at"], u["source_text"], u["target_text"],
                                u["source_lang"], u["target_lang"], u["speaker_id"],
                                " ".join(str(t) for t in (u.get("tags") or [])),
                                u["quality"], u["memory_state"]])
            self.set_status("已导出 %d 条素材" % len(rows), "ok")
        except Exception as e:
            log("导出选中失败：" + repr(e))

    def lib_delete(self):
        from PySide6.QtWidgets import QMessageBox
        if not self._lib_rows:
            return
        u = self._lib_rows[self._idx_safe()]
        if QMessageBox.question(self.win, "删除素材",
                                "删掉这一条？\n\n%s\n%s" % (u["source_text"], u["target_text"]),
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        n = delete_history_entries(u["created_at"][:10],
                                  [(u["created_at"], u["target_text"])])
        self.set_status("已删除 %d 条素材" % n, "ok" if n else "warn")
        self.refresh_library(force=True)

    def lib_export(self):
        from PySide6.QtWidgets import QFileDialog
        import csv
        if not self._lib_rows:
            return
        path, _ = QFileDialog.getSaveFileName(self.win, "导出素材库",
                                              str(ROOT / "library_export.csv"),
                                              "CSV (*.csv)")
        if not path:
            return
        recs = read_review()
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["时间", "源文本", "目标译文", "source_lang", "target_lang",
                            "说话人", "标签", "质量", "记忆状态"])
                for u in self._lib_rows:
                    w.writerow([u["created_at"], u["source_text"], u["target_text"],
                                u["source_lang"], u["target_lang"], u["speaker_id"],
                                "、".join(str(t) for t in (u.get("tags") or [])),
                                u.get("quality") or "ok", self._lib_state_of(u, recs)])
            self.set_status("已导出 %d 条到 %s" % (len(self._lib_rows), path), "ok")
        except Exception as e:
            log("导出素材库失败：" + repr(e))
            self.set_status("导出失败：%s" % e, "warn")

    def lib_context_play(self):
        if not self._lib_rows:
            return
        u = self._lib_rows[self._idx_safe()]
        seq = []
        for c in u.get("context") or []:
            if c.get("created_at", "") < u["created_at"]:
                seq = [c.get("source_text", "")]
        seq.append(u["source_text"])
        for c in u.get("context") or []:
            if c.get("created_at", "") > u["created_at"]:
                seq.append(c.get("source_text", ""))
                break
        try:
            for txt in [s for s in seq if s]:
                self.owner.overlay.read_text(txt, lang=u["source_lang"] or "zh")
            self.set_status("语境回放：上一句 → 本句 → 下一句（无原声切片时用 TTS）", "ok")
        except Exception as e:
            log("语境回放失败：" + repr(e))

    def lib_export_menu(self):
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self.win)
        menu.addAction("导出 CSV（全字段）").triggered.connect(self.lib_export)
        menu.addAction("导出 Anki CSV").triggered.connect(self.lib_export_anki)
        menu.addAction("导出 OpenAI JSONL").triggered.connect(self.lib_export_openai)
        menu.addSeparator()
        menu.addAction("导入 CSV / JSONL").triggered.connect(self.lib_import)
        menu.exec(self.btn_lib_refresh.mapToGlobal(self.btn_lib_refresh.rect().bottomLeft()))

    def lib_export_anki(self):
        from PySide6.QtWidgets import QFileDialog
        import csv
        if not self._lib_rows:
            return
        path, _ = QFileDialog.getSaveFileName(self.win, "导出 Anki CSV",
                                              str(ROOT / "anki_library.csv"), "CSV (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["目标译文", "源文本", "标签", "source_lang", "target_lang"])
                for u in self._lib_rows:
                    w.writerow([u["target_text"], u["source_text"],
                                " ".join(str(t) for t in (u.get("tags") or [])),
                                u["source_lang"], u["target_lang"]])
            self.set_status("已导出 Anki CSV：%d 条" % len(self._lib_rows), "ok")
        except Exception as e:
            log("导出 Anki 失败：" + repr(e))

    def lib_export_openai(self):
        from PySide6.QtWidgets import QFileDialog
        if not self._lib_rows:
            return
        path, _ = QFileDialog.getSaveFileName(self.win, "导出 OpenAI JSONL",
                                              str(ROOT / "oversub_openai.jsonl"),
                                              "JSONL (*.jsonl)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                for u in self._lib_rows:
                    f.write(json.dumps({"messages": [
                        {"role": "system", "content": "你是 %s→%s 语境翻译助手，输出地道、贴合场景的译文。" % (
                            u["source_lang"], u["target_lang"])},
                        {"role": "user", "content": u["source_text"]},
                        {"role": "assistant", "content": u["target_text"]},
                    ]}, ensure_ascii=False) + "\n")
            self.set_status("已导出 OpenAI JSONL：%d 条" % len(self._lib_rows), "ok")
        except Exception as e:
            log("导出 OpenAI 失败：" + repr(e))

    def lib_import(self):
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        import csv
        path, _ = QFileDialog.getOpenFileName(self.win, "导入素材",
                                              str(ROOT), "CSV / JSONL (*.csv *.jsonl)")
        if not path:
            return
        added, skipped = 0, 0
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                if path.lower().endswith(".jsonl"):
                    for ln in f:
                        ln = ln.strip()
                        if not ln:
                            continue
                        try:
                            r = json.loads(ln)
                        except Exception:
                            skipped += 1
                            continue
                        if "messages" in r:      # OpenAI 微调格式反解
                            msgs = r.get("messages") or []
                            src = tgt = ""
                            for m in msgs:
                                if m.get("role") == "user":
                                    src = m.get("content", "")
                                elif m.get("role") == "assistant":
                                    tgt = m.get("content", "")
                        else:
                            src = r.get("source_text") or r.get("源文本") or r.get("zh") or ""
                            tgt = r.get("target_text") or r.get("目标译文") or r.get("en") or ""
                        if src and tgt:
                            append_history(time.strftime("%Y-%m-%d %H:%M:%S"), src, tgt,
                                           r.get("speaker_id", ""), scene="import")
                            added += 1
                        else:
                            skipped += 1
                else:
                    for r in csv.DictReader(f):
                        src = (r.get("源文本") or r.get("source_text")
                               or r.get("中文识别文本") or "")
                        tgt = (r.get("目标译文") or r.get("target_text")
                               or r.get("英文翻译") or "")
                        if src and tgt:
                            append_history(time.strftime("%Y-%m-%d %H:%M:%S"), src, tgt,
                                           r.get("说话人", ""), scene="import")
                            added += 1
                        else:
                            skipped += 1
            self.refresh_library(force=True)
            self.set_status("导入完成：新增 %d 条，跳过 %d 条" % (added, skipped), "ok")
        except Exception as e:
            log("导入失败：" + repr(e))
            QMessageBox.information(self.win, "导入失败", str(e))

    def _idx_safe(self):
        return max(0, min(self._lib_idx, len(self._lib_rows) - 1)) if self._lib_rows else 0

    # ---------------- Lab 实验室 ----------------
    def lab_refresh(self):
        from PySide6.QtWidgets import QTableWidgetItem
        if not hasattr(self, "lab_lang_table"):
            return
        codes = list(LANG_REGISTRY.keys())
        self.lab_lang_table.setRowCount(len(codes))
        for i, code in enumerate(codes):
            m = LANG_REGISTRY[code]
            vals = (m.get("name"), "zh→%s" % code,
                    "ASR %s ｜ 翻译 %s ｜ TTS %s" % (m.get("asr"), m.get("mt"), m.get("tts")),
                    "可用" if m.get("installed") else "未接入",
                    "已挂载" if code in DICT_PARSERS else "未挂载")
            for j, v in enumerate(vals):
                self.lab_lang_table.setItem(i, j, QTableWidgetItem(str(v)))
        n_ok = sum(1 for c in codes if LANG_REGISTRY[c].get("installed"))
        self.lab_lang_hint.setText(
            "共 %d 个语种，其中 %d 个可用（有本地翻译包）；未接入的不出现在下拉里，也不提供假下载。"
            % (len(codes), n_ok))

        self.lab_dict_table.setRowCount(len(codes))
        for i, code in enumerate(codes):
            fn = DICT_PARSERS.get(code)
            name = getattr(fn, "__name__", "") if fn else "—"
            self.lab_dict_table.setItem(i, 0, QTableWidgetItem(
                "%s %s" % (LANG_REGISTRY[code].get("name"), code)))
            self.lab_dict_table.setItem(i, 1, QTableWidgetItem(
                ("内置解析器 %s" % name) if fn else "未挂载（双击查词会提示不支持）"))

        cur_asr = str(self.cfg.get("whisper_model") or "small")
        for k in range(self.lab_asr.count()):
            if self.lab_asr.itemData(k) == cur_asr:
                self.lab_asr.setCurrentIndex(k)
        cur_mt = str(self.cfg.get("mt_size") or "small")
        for k in range(self.lab_mt.count()):
            if self.lab_mt.itemData(k) == cur_mt:
                self.lab_mt.setCurrentIndex(k)
        self.lab_model_status.setText(
            "当前：ASR %s ｜ 翻译 %s ｜ %s" % (
                cur_asr, cur_mt, "在线接口" if self.cfg.get("engine") == "online" else "Argos 离线"))

        self.lab_chk_short.setChecked(bool(self.cfg.get("filter_short", True)))
        self.lab_chk_diar.setChecked(bool(self.cfg.get("diarize", False)))
        self.lab_chk_audio.setChecked(bool(self.cfg.get("save_audio", True)))
        self.lab_min_chars.setValue(int(self.cfg.get("min_chars") or 2))
        self.lab_min_words.setValue(int(self.cfg.get("min_words") or 2))
        self.lab_max_spk.setValue(int(self.cfg.get("max_speakers") or 4))

        preset = str(self.cfg.get("style_preset") or "game")
        self.lab_style.blockSignals(True)
        self.lab_style.setCurrentIndex({"game": 0, "film": 1, "minimal": 2}.get(preset, 0))
        self.lab_style.blockSignals(False)
        cur_theme = str(self.cfg.get("theme") or "dark")
        self.lab_theme.blockSignals(True)
        for k in range(self.lab_theme.count()):
            if self.lab_theme.itemData(k) == cur_theme:
                self.lab_theme.setCurrentIndex(k)
        self.lab_theme.blockSignals(False)
        if hasattr(self, "chk_anim"):
            self.chk_anim.blockSignals(True)
            self.chk_anim.setChecked(bool(self.cfg.get("animations", True)))
            self.chk_anim.blockSignals(False)
        if hasattr(self, "spin_tts_q"):
            self.spin_tts_q.blockSignals(True)
            self.spin_tts_q.setValue(int(self.cfg.get("tts_queue_max") or 3))
            self.spin_tts_q.blockSignals(False)
        if hasattr(self, "lab_uilang"):
            self.lab_uilang.blockSignals(True)
            self.lab_uilang.setCurrentIndex(
                1 if str(self.cfg.get("ui_lang") or "zh") == "en" else 0)
            self.lab_uilang.blockSignals(False)
        if hasattr(self, "tts_lang_box"):
            self.lab_tts_refresh()
        if hasattr(self, "lab_filter_log"):
            self.lab_filter_log.setText("已静默过滤 %d 条短句%s" % (
                getattr(self, "_filtered_n", 0),
                ("　最近：" + getattr(self, "_filtered_last", "")) if getattr(
                    self, "_filtered_n", 0) else ""))
        self.lab_style_hint.setText("字号 %s ｜ 透明度 %.2f" % (
            self.cfg.get("font_size") or 22, float(self.cfg.get("opacity") or 0.9)))
        hks = [k for k in self.cfg if str(k).startswith("hotkey_") and self.cfg.get(k)]
        self.lab_hotkey_hint.setText("已配置 %d 个全局热键：%s" % (
            len(hks), "、".join("%s=%s" % (k.replace("hotkey_", ""), self.cfg[k]) for k in hks[:6])))

    def lab_save_models(self):
        self.cfg["whisper_model"] = self.lab_asr.currentData() or "auto"
        self.cfg["mt_size"] = self.lab_mt.currentData() or "small"
        save_config(self.cfg)
        self.lab_model_status.setText("已保存：ASR %s ｜ 翻译 %s（识别引擎下次启动按新模型加载）" % (
            self.cfg["whisper_model"], self.cfg["mt_size"]))
        self.refresh_chips()
        self.set_status("模型选择已保存", "ok")

    def lab_save_rules(self):
        self.cfg["filter_short"] = bool(self.lab_chk_short.isChecked())
        self.cfg["diarize"] = bool(self.lab_chk_diar.isChecked())
        self.cfg["save_audio"] = bool(self.lab_chk_audio.isChecked())
        self.cfg["min_chars"] = int(self.lab_min_chars.value())
        self.cfg["min_words"] = int(self.lab_min_words.value())
        self.cfg["max_speakers"] = int(self.lab_max_spk.value())
        save_config(self.cfg)
        self.set_status("识别规则已保存（说话人策略下次启动完全生效）", "ok")

    def lab_apply_theme(self, idx):
        if not hasattr(self, "lab_theme"):
            return
        self._apply_theme(self.lab_theme.currentData() or "dark", sync_lab=False)

    def _apply_theme(self, theme, sync_lab=True):
        if theme not in THEME_COLORS:
            return
        self.cfg["theme"] = theme
        self.win.setStyleSheet(theme_qss(theme))
        # 译文行的词链接是内联颜色，换主题要跟着重刷，否则还是上一套配色
        try:
            self.cap_en.set_text(self.cap_en.text(), tcol("text", self.cfg))
        except Exception:
            pass
        save_config(self.cfg)
        for k, act in getattr(self, "_theme_actions", {}).items():
            act.setChecked(k == theme)
        # 顶栏下拉与 Lab 预览块同步，不触发二次切换
        self._theme_syncing = True
        try:
            if hasattr(self, "head_theme"):
                self.head_theme.blockSignals(True)
                for i in range(self.head_theme.count()):
                    if self.head_theme.itemData(i) == theme:
                        self.head_theme.setCurrentIndex(i)
                self.head_theme.blockSignals(False)
        except Exception:
            pass
        if sync_lab and hasattr(self, "lab_theme"):
            self.lab_theme.blockSignals(True)
            for k in range(self.lab_theme.count()):
                if self.lab_theme.itemData(k) == theme:
                    self.lab_theme.setCurrentIndex(k)
            self.lab_theme.blockSignals(False)
        try:
            self._refresh_theme_tiles(theme)
        except Exception:
            pass
        self._theme_syncing = False
        # 表格单元格的说明文字是按旧主题刷的颜色，换主题后重画一遍，避免深底深字
        try:
            self.refresh_chips()
            if hasattr(self, "tabs") and self.tabs.currentIndex() == 0:
                self.load_day(self.day_box.currentText())
            elif hasattr(self, "tabs") and self.tabs.currentIndex() == 1:
                self.lib_render()
        except Exception as e:
            log("切换主题后重绘失败：" + repr(e))
        self.set_status("界面风格：%s" % THEME_LABELS.get(theme, theme), "ok")

    def _quick_theme(self, key):
        """顶栏「更多 → 界面主题」用的快捷切换，不进 Lab 也能换。"""
        self._apply_theme(key)

    def _on_head_theme(self, _idx=0):
        if getattr(self, "_theme_syncing", False):
            return
        try:
            self._apply_theme(self.head_theme.currentData() or "dark")
        except Exception as e:
            log("顶栏换风格失败：" + repr(e))

    def _refresh_theme_tiles(self, current=None):
        """Lab 里的六套风格预览块：直接显示卡片色、文字色、强调色和形状。"""
        current = current or str(self.cfg.get("theme") or "dark")
        for key, btn in getattr(self, "_theme_tiles", {}).items():
            c = THEME_COLORS.get(key, THEME_COLORS["dark"])
            s = theme_style(key)
            on = key == current
            btn.setText(("✓ " if on else "") + THEME_LABELS.get(key, key)
                        + "\n" + s.get("tag", ""))
            btn.setStyleSheet(
                "QPushButton{text-align:left;padding:6px 11px;font-size:12px;"
                "background:%s;color:%s;border:1px solid %s;border-left:5px solid %s;"
                "border-radius:%dpx;font-weight:%s;}"
                "QPushButton:hover{border-color:%s;}"
                % (c["card"], c["text"], c["accent"] if on else c["border"],
                   c["accent"], s["radius"], "700" if on else "500", c["accent_hover"]))

    def lab_apply_style(self, idx):
        preset = {0: "game", 1: "film", 2: "minimal"}.get(idx, "game")
        self.cfg["style_preset"] = preset
        save_config(self.cfg)
        try:
            self.owner.overlay.cfg["style_preset"] = preset
            self.owner.overlay.update_look()
        except Exception as e:
            log("切换主题失败：" + repr(e))

    def refresh_days(self):
        days = history_days()
        today = time.strftime("%Y-%m-%d")
        if today not in days:
            days.insert(0, today)
        cur = self.day_box.currentText() or today
        self.day_box.blockSignals(True)
        self.day_box.clear()
        self.day_box.addItems(days)
        self.day_box.setCurrentText(cur if cur in days else days[0])
        self.day_box.blockSignals(False)

    def load_today(self):
        self.refresh_days()
        self.load_day(self.day_box.currentText() or time.strftime("%Y-%m-%d"))
        self.refresh_words()
        self.refresh_fav()

    def _selected_scenes(self):
        if not hasattr(self, "scene_boxes"):
            return set()
        return {k for k, cb in self.scene_boxes.items() if cb.isChecked()}

    def _scene_ok(self, scene):
        sel = self._selected_scenes()
        if not sel:
            return True
        return str(scene or "") in sel

    def _on_scene_filter(self, *_a):
        self.word_scene_hint.setText(
            "场景筛选：" + ("、".join(SCENE_SHORT.get(k, k) for k in self._selected_scenes())
                          if self._selected_scenes() else "全部"))
        self.load_day(self.day_box.currentText() or time.strftime("%Y-%m-%d"))
        self.refresh_words()
        self.refresh_fav()

    def set_selftest(self, on):
        """反向自测：藏起英文/释义，先自己想，再点中文（或单词）揭晓。"""
        from PySide6.QtCore import Qt as _Qt
        on = bool(on)
        for cb in (getattr(self, "chk_selftest", None), getattr(self, "chk_selftest_word", None)):
            if cb is not None and cb.isChecked() != on:
                cb.blockSignals(True)
                cb.setChecked(on)
                cb.blockSignals(False)
        try:
            self.table.setColumnHidden(3, on)
        except Exception:
            pass
        try:
            self.word_table.setColumnHidden(2, on)
        except Exception:
            pass
        if on:
            self.word_reveal.setText("点一个单词 → 在这里揭晓释义")
            self.word_reveal.show()
        else:
            self.word_reveal.setText("")
            self.word_reveal.hide()

    def _reveal_english(self, row):
        from PySide6.QtCore import Qt as _Qt
        data = self.table.item(row, 0).data(_Qt.UserRole) if self.table.item(row, 0) else None
        if isinstance(data, dict) and data.get("en"):
            self.cap_zh.setText(data.get("zh", ""))
            self.cap_en.setText(data.get("en", ""))
            self._sync_preview_star()
            self.set_status("答案在预览卡里 · 再点一次也没关系", "ok")

    def _refresh_stats(self):
        """本次会话的翻译质量分布：够不够地道一眼看到。"""
        try:
            rows = read_history(self.day_box.currentText() or time.strftime("%Y-%m-%d"),
                                limit=self._max_rows)
            sess = group_sessions(rows, gap=int(self.cfg.get("session_gap") or 120))
            last = sess[-1]["rows"] if sess else rows
            n_bad = sum(1 for r in last if r.get("qual") == "machine")
            n_err = sum(1 for r in last if r.get("qual") == "error")
            n_ok = sum(1 for r in last if r.get("qual") == "native")
            self.rec_stats.setText("本次会话 %d 句 ｜ ✅%d ⚠️%d ❌%d" % (
                len(last), n_ok, n_bad, n_err))
        except Exception as e:
            log("质量统计失败：" + repr(e))

    def load_day(self, day):
        """只把最近 keep_rows 条放进表格，整天记录留在磁盘上，不在内存里铺开。"""
        if not day:
            return
        self.table.setRowCount(0)
        for r in read_history(day, limit=self._max_rows):
            if not self._scene_ok(r.get("scene")):
                continue
            self._insert_row(r.get("t", ""), r.get("spk", ""), r.get("en", ""),
                             r.get("zh", ""), scene=r.get("scene", ""),
                             qual=r.get("qual", ""), alts=r.get("alts") or [],
                             count=r.get("count") or 1)
        while self.table.rowCount() > self._max_rows:
            self.table.removeRow(self.table.rowCount() - 1)
        if self.table.rowCount():
            self.table.resizeRowsToContents()
            # 预览卡跟着最新一句走，收藏星标才有同步的锚点
            if self.table.item(0, 2) and self.table.item(0, 3):
                self.cap_zh.setText(self.table.item(0, 2).text())
                self.cap_en.setText(self.table.item(0, 3).text())
                self._sync_preview_star()
        self.apply_filter(self.search.text())
        self._refresh_stats()
        try:
            self.refresh_sessions()
        except Exception:
            pass
        try:
            self.refresh_pairs()
        except Exception:
            pass
        self._refresh_recent_preview()

    def apply_filter(self, text):
        """搜索时直接扫磁盘，命中更早的记录也能显示（最多 keep_rows 条）。"""
        q = (text or "").strip()
        day = self.day_box.currentText() or time.strftime("%Y-%m-%d")
        if q:
            hits = search_history(day, q, limit=self._max_rows)
            self._filtered = True
            self.table.setRowCount(0)
            for r in hits:
                if not self._scene_ok(r.get("scene")):
                    continue
                self._insert_row(r.get("t", ""), r.get("spk", ""), r.get("en", ""),
                                 r.get("zh", ""), scene=r.get("scene", ""),
                                 qual=r.get("qual", ""), alts=r.get("alts") or [],
                                 count=r.get("count") or 1)
            if self.table.rowCount():
                self.table.resizeRowsToContents()
                if self.table.item(0, 2) and self.table.item(0, 3):
                    self.cap_zh.setText(self.table.item(0, 2).text())
                    self.cap_en.setText(self.table.item(0, 3).text())
                    self._sync_preview_star()
            return
        if self._filtered:                    # 从搜索状态退回来，重新载入当天尾部
            self._filtered = False
            self.load_day(day)
            return
        for r in range(self.table.rowCount()):
            self.table.setRowHidden(r, False)

    # ---------------- 生词本 ----------------
    def on_cell_double(self, row, col):
        data = self._row_data(row) or {}
        zh = str(data.get("zh", ""))
        en = str(data.get("en", ""))
        spk = str(data.get("spk", ""))
        if col == 3:
            self.open_word_dialog(en, spk)
        elif col == 2:
            self.show_synonym_english(zh)

    def show_synonym_english(self, zh):
        """双击中文：把这个意思下收集过的所有英文说法一次列出来。"""
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem
        key = (zh or "").strip()
        if not key:
            return
        hits = []
        for r in read_pairs():
            if str(r.get("zh", "")).strip() == key and r.get("en"):
                role_txt = {"teammate": "队友", "streamer": "主播",
                            "stranger": "陌生人"}.get(r.get("role") or "", "—")
                hits.append((
                    "句对 · %s" % ("听力" if r.get("dir") == "in" else "输出"),
                    r.get("en", ""), r.get("t", ""),
                    "%s / %s" % (SCENE_NAMES.get(r.get("scene") or "", "—"), role_txt)))
        for r in read_fav():
            if r.get("k") == "line" and str(r.get("zh", "")).strip() == key and r.get("v"):
                hits.append(("收藏", r.get("v", ""), r.get("t", ""), r.get("spk", "")))
        # 手动挂上去的备选英文也算这个意思的说法
        try:
            for r in read_history(None, limit=2000):
                if str(r.get("zh", "")).strip() == key:
                    for a in (r.get("alts") or []):
                        text = a.get("en") if isinstance(a, dict) else str(a)
                        if text:
                            hits.append(("备选", text, r.get("t", ""), "手动追加"))
        except Exception:
            pass
        dlg = QDialog(self.win)
        dlg.setWindowTitle("同义英文 · %s" % key)
        dlg.resize(720, 480)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("「%s」这个意思，你收集到的目标语言说法（%d 条）：" % (key, len(hits))))
        tb = QTableWidget(len(hits), 4)
        tb.setHorizontalHeaderLabels(["来源", "目标文本", "时间", "场景 / 说话人"])
        tb.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        tb.verticalHeader().setVisible(False)
        tb.setEditTriggers(QAbstractItemView.NoEditTriggers)
        for i, (src, en, t, extra) in enumerate(hits):
            tb.setItem(i, 0, QTableWidgetItem(src))
            tb.setItem(i, 1, QTableWidgetItem(en))
            tb.setItem(i, 2, QTableWidgetItem(t))
            tb.setItem(i, 3, QTableWidgetItem(str(extra or "")))
        tb.resizeRowsToContents()
        v.addWidget(tb)
        if not hits:
            v.addWidget(QLabel("还没有别的说法。多听几句、或多收藏几次，这里会慢慢攒出同义集合。"))
        dlg.exec()

    def open_word_dialog(self, sentence, spk="", conf=None):
        words = split_words(sentence)
        if not words:
            QMessageBox.information(self.win, "拆词", "这句里没有可查的英文单词。")
            return
        build_word_dialog(self.win, self.book, sentence, spk, words, self.refresh_words,
                          conf=conf, scene=self.cfg.get("scene") or "")

    def _on_word_clicked(self, word):
        """单击译文里的某个词：取词 → 后台查义 → 弹词卡，可一键存生词。"""
        import re as _re
        w = _re.sub(r"[^A-Za-z'’-]", "", str(word or "")).strip("'-’")
        if len(w) < 2:
            return
        sentence, spk = "", ""
        try:
            sentence = self.cap_en.text().strip()
            d = self._row_data(0) or {}
            spk = d.get("spk", "") or ""
        except Exception:
            pass
        try:
            pop_word_card(self, w, sentence, spk)
        except Exception as e:
            log("点词卡片失败：" + repr(e))

    def _on_lang_filter(self, on):
        self.cfg["filter_by_lang"] = bool(on)
        save_config(self.cfg)
        self.word_scene_hint.setText("按语种筛选：" + ("只显示 %s" % lang_name(
            self.cfg.get("target_lang") or "en") if on else "全部语种"))
        self.refresh_words()
        self.refresh_fav()

    def refresh_words(self):
        from PySide6.QtWidgets import QTableWidgetItem
        rows = read_words()
        if self.cfg.get("filter_by_lang"):
            tl = str(self.cfg.get("target_lang") or "en")
            rows = [r for r in rows if str(r.get("target_lang") or "en") == tl]
        sel = self._selected_scenes()
        if sel:
            def _hit(r):
                return any(str(c.get("scene") or "") in sel for c in (r.get("ctx") or []))
            rows = [r for r in rows if _hit(r)]
        self._word_rows = list(reversed(rows))
        self.word_table.setRowCount(0)
        for r in self._word_rows:
            nctx = len(r.get("ctx") or []) or (1 if r.get("s") else 0)
            meaning = str(r.get("m", ""))
            if r.get("low_conf"):
                meaning += " ｜ 识别置信度较低，建议核对"
            self.word_table.insertRow(0)
            for i, v in enumerate((r.get("t", ""), r.get("w", ""), meaning,
                                  "×%d" % nctx if nctx else "", r.get("s", ""))):
                self.word_table.setItem(0, i, QTableWidgetItem(str(v)))
        if self.word_table.rowCount():
            self.word_table.resizeRowsToContents()
        self.word_count.setText("生词 %d 个" % len(rows))
        if sel:
            self.word_scene_hint.setText("按场景筛选：%s" % "、".join(
                SCENE_SHORT.get(k, k) for k in sel))

    def on_word_click(self, row, col):
        if col != 1 or not self.chk_selftest_word.isChecked():
            return
        try:
            rec = self._word_rows[row]
        except Exception:
            return
        self.word_reveal.setText("释义：%s（原句：%s）" % (
            rec.get("m", ""), rec.get("s", "")))
        self.word_reveal.show()

    def open_word_card(self, row, col):
        try:
            rec = self._word_rows[row]
        except Exception:
            return
        build_word_card_dialog(self.win, rec)

    # ---------------- 会话（按时间段自动分袋） ----------------
    def refresh_sessions(self):
        from PySide6.QtWidgets import QTableWidgetItem
        day = self.day_box.currentText() or time.strftime("%Y-%m-%d")
        rows = read_history(day, limit=2000)
        gap = int(self.cfg.get("session_gap") or 120)
        self._sessions = group_sessions(rows, gap=gap)
        self.sess_table.setRowCount(0)
        words = read_words()
        # 生词按会话标签匹配（收藏时记了 sess）
        for s in self._sessions:
            tag = s.get("start", "")
            n_words = 0
            for w in words:
                for c in (w.get("ctx") or []):
                    if str(c.get("sess") or "") == tag:
                        n_words += 1
                        break
            self.sess_table.insertRow(0)
            dur = ""
            try:
                a = _secs(s["start"]); b = _secs(s["end"])
                if a is not None and b is not None:
                    dur = "%d 分 %d 秒" % ((b - a) // 60, (b - a) % 60)
            except Exception:
                pass
            for i, v in enumerate((s["start"], SCENE_NAMES.get(s.get("scene") or "", "未标注"),
                                  dur, "%d 句" % s["n"], ("%d 个" % n_words) if n_words else "")):
                self.sess_table.setItem(0, i, QTableWidgetItem(str(v)))
        self.sess_count.setText("会话 %d 个" % len(self._sessions))

    def _picked_session(self):
        idx = self.sess_table.currentRow()
        if idx < 0:
            QMessageBox.information(self.win, "会话", "先选中一个会话。")
            return None
        # 表是新的在上，与 _sessions 顺序相反
        try:
            return self._sessions[len(self._sessions) - 1 - idx]
        except Exception:
            return None

    def open_session(self, row, col):
        sess = self._picked_session()
        if not sess:
            return
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QTableWidget, QTableWidgetItem, QLabel
        dlg = QDialog(self.win)
        dlg.setWindowTitle("会话 · %s" % session_label(sess))
        dlg.resize(760, 520)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(session_label(sess)))
        tb = QTableWidget(len(sess["rows"]), 3)
        tb.setHorizontalHeaderLabels(["时间", "目标文本", "源文本"])
        tb.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        tb.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        tb.verticalHeader().setVisible(False)
        for i, r in enumerate(sess["rows"]):
            tb.setItem(i, 0, QTableWidgetItem(str(r.get("t", ""))))
            tb.setItem(i, 1, QTableWidgetItem(str(r.get("en", ""))))
            tb.setItem(i, 2, QTableWidgetItem(str(r.get("zh", ""))))
        tb.resizeRowsToContents()
        v.addWidget(tb)
        dlg.exec()

    def export_session(self, kind="txt"):
        sess = self._picked_session()
        if not sess:
            return
        day = self.day_box.currentText() or time.strftime("%Y-%m-%d")
        default = str(Path.home() / ("%s-会话-%s.%s" % (
            day, sess["start"].replace(":", ""), "srt" if kind == "srt" else "md")))
        path, _ = QFileDialog.getSaveFileName(
            self.win, "导出会话", default,
            "SRT 字幕 (*.srt)" if kind == "srt" else "Markdown (*.md);;文本 (*.txt)")
        if not path:
            return
        try:
            if kind == "srt":
                export_session_srt(path, sess)
            else:
                export_session_text(path, sess)
            QMessageBox.information(self.win, "导出会话", "已导出：\n" + path)
        except Exception as e:
            QMessageBox.warning(self.win, "导出失败", repr(e))

    # ---------------- 双向句对 ----------------
    def refresh_pairs(self, *a):
        from PySide6.QtWidgets import QTableWidgetItem
        rows = read_pairs()
        mode = self.pair_filter.currentIndex()
        if mode == 1:
            rows = [r for r in rows if r.get("dir") == "in"]
        elif mode == 2:
            rows = [r for r in rows if r.get("dir") == "out"]
        elif mode == 3:
            gaps = pair_gaps(rows)
            rows = [r for r in rows if str(r.get("zh", "")).strip() in gaps]
        elif mode == 4:
            seen = {}
            for r in rows:
                ch = r.get("chain")
                if ch:
                    seen.setdefault(ch, []).append(r)
            rows = [r for ch, g in seen.items() if len(g) >= 2 for r in g]
        elif mode == 5:
            conf = pragmatic_conflicts(rows)
            keys = set()
            for c in conf:
                for r in c["items"]:
                    keys.add((r.get("t"), r.get("en")))
            rows = [r for r in rows if (r.get("t"), r.get("en")) in keys]
        self._pair_rows = list(reversed(rows))
        self.pair_table.setRowCount(0)
        for r in self._pair_rows:
            mark = []
            if r.get("alt"):
                mark.append("备选表达")
            if r.get("fav"):
                mark.append("已收藏")
            if int(r.get("reuse") or 0) > 0:
                mark.append("复用 %d 次" % int(r["reuse"]))
            if r.get("merged"):
                mark.append("合并组")
            self.pair_table.insertRow(0)
            vals = (r.get("t", ""), "英→中" if r.get("dir") == "in" else "中→英",
                    r.get("en", ""), r.get("zh", ""),
                    "%s / %s" % (SCENE_NAMES.get(r.get("scene") or "", "—"),
                                 {"teammate": "队友", "streamer": "主播",
                                  "stranger": "陌生人"}.get(r.get("role") or "", "—")),
                    " · ".join(mark))
            for i, v in enumerate(vals):
                self.pair_table.setItem(0, i, QTableWidgetItem(str(v)))
        self.pair_count.setText("句对 %d 条" % len(self._pair_rows))

    def _picked_pair(self):
        idx = self.pair_table.currentRow()
        if idx < 0:
            return None
        try:
            return self._pair_rows[idx]
        except Exception:
            return None

    def open_pair_detail(self, row, col):
        rec = self._picked_pair()
        if not rec:
            return
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton, QHBoxLayout
        dlg = QDialog(self.win)
        dlg.setWindowTitle("句对 · %s" % rec.get("en", "")[:30])
        dlg.resize(680, 520)
        v = QVBoxLayout(dlg)
        v.setSpacing(8)
        en = QLabel(rec.get("en", ""))
        en.setObjectName("dialogHead")
        en.setWordWrap(True)
        v.addWidget(en)
        zh = QLabel(rec.get("zh", ""))
        zh.setObjectName("dialogSub")
        zh.setWordWrap(True)
        v.addWidget(zh)
        info = "链路：%s ｜ 场景：%s ｜ 角色：%s ｜ 会话：%s" % (
            "外源英文 → 中文（听懂）" if rec.get("dir") == "in" else "中文 → 英文（会说）",
            SCENE_NAMES.get(rec.get("scene") or "", "—"),
            {"teammate": "队友", "streamer": "主播", "stranger": "陌生人"}.get(rec.get("role") or "", "—"),
            rec.get("sess") or "—")
        v.addWidget(QLabel(info))
        if rec.get("alt"):
            tip = QLabel("【备选表达】和你听过的原生说法有区别（对齐度 %.2f），复习时优先记原生那句"
                         % float(rec.get("align") or 0))
            tip.setObjectName("warnText")
            v.addWidget(tip)
        if rec.get("raw") and rec.get("raw") != rec.get("en"):
            v.addWidget(QLabel("原声（含口头填充词）：%s" % rec["raw"]))
        chain = [r for r in read_pairs() if r.get("chain") == rec.get("chain")
                 and (r.get("t"), r.get("en")) != (rec.get("t"), rec.get("en"))]
        if chain:
            v.addWidget(QLabel("对话链里的其它句（按顺序回忆应答）："))
            for c in chain[-4:]:
                v.addWidget(QLabel("· %s ｜ %s" % (c.get("en", ""), c.get("zh", ""))))
        # 跨会话同义串联：同一个中文意思，别的会话 / 场景里的说法
        same = [r for r in read_pairs()
                if str(r.get("zh", "")).strip() == str(rec.get("zh", "")).strip()
                and (r.get("t"), r.get("en")) != (rec.get("t"), rec.get("en"))]
        if same:
            v.addWidget(QLabel("同一个意思，别处收集到的说法（跨会话同义串联）："))
            for s in same[-8:]:
                v.addWidget(QLabel("· %s ｜ %s（%s / %s）" % (
                    s.get("en", ""), s.get("sess") or "",
                    SCENE_NAMES.get(s.get("scene") or "", "—"),
                    {"teammate": "队友", "streamer": "主播",
                     "stranger": "陌生人"}.get(s.get("role") or "", "—"))))
        rowb = QHBoxLayout()
        btn_read = QPushButton("朗读原声（听语速）")
        btn_tpl = QPushButton("设为我的应答模板")
        btn_close = QPushButton("关闭")
        rowb.addStretch(1)
        for b in (btn_read, btn_tpl, btn_close):
            rowb.addWidget(b)
        v.addLayout(rowb)

        def read_it():
            try:
                self.owner.overlay.read_text(rec.get("raw") or rec.get("en", ""), lang="en")
            except Exception as e:
                log("朗读句对失败：" + repr(e))

        def to_tpl():
            data = load_templates()
            data.setdefault(str(rec.get("zh", "")).strip(), [])
            if rec.get("en") and rec["en"] not in data[str(rec.get("zh", "")).strip()]:
                data[str(rec.get("zh", "")).strip()].append(rec["en"])
            save_templates(data)
            QMessageBox.information(dlg, "应答模板", "已加入：%s → %s" % (rec.get("zh"), rec.get("en")))
        btn_read.clicked.connect(read_it)
        btn_tpl.clicked.connect(to_tpl)
        btn_close.clicked.connect(dlg.accept)
        dlg.exec()

    def pair_dictation(self):
        rec = self._picked_pair()
        if not rec:
            QMessageBox.information(self.win, "对照听写", "先选中一条句对。")
            return
        from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QLineEdit,
                                       QPushButton, QHBoxLayout)
        from PySide6.QtCore import Qt
        dlg = QDialog(self.win)
        dlg.setWindowTitle("对照听写 · %s" % ("听 → 写中文" if rec.get("dir") == "in" else "看中文 → 写英文"))
        dlg.resize(560, 260)
        v = QVBoxLayout(dlg)
        if rec.get("dir") == "in":
            v.addWidget(QLabel("听到："))
            heard = QLabel(rec.get("en", ""))
            heard.setWordWrap(True)
            heard.setStyleSheet("font-size:16px;font-weight:700;")
            v.addWidget(heard)
            v.addWidget(QLabel("写出你听出的中文意思："))
            inp = QLineEdit()
            v.addWidget(inp)
            target = rec.get("zh", "")
            btn_play = QPushButton("再放一遍")
        else:
            v.addWidget(QLabel("中文：%s" % rec.get("zh", "")))
            v.addWidget(QLabel("写出你想到的英文（会和你收集的原生表达比对）："))
            inp = QLineEdit()
            v.addWidget(inp)
            target = rec.get("en", "")
            btn_play = QPushButton("听标准说法")
        res = QLabel("")
        res.setWordWrap(True)
        v.addWidget(res)
        rowb = QHBoxLayout()
        btn_chk = QPushButton("比对")
        btn_close = QPushButton("关闭")
        rowb.addStretch(1)
        rowb.addWidget(btn_play)
        rowb.addWidget(btn_chk)
        rowb.addWidget(btn_close)
        v.addLayout(rowb)

        def play():
            text = rec.get("en", "") if rec.get("dir") == "in" else rec.get("en", "")
            self.owner.overlay.read_text(rec.get("raw") or text, lang="en")

        def check():
            got = inp.text().strip()
            if not got:
                return
            if rec.get("dir") == "in":
                sim = pair_similarity(got, target) if got.isascii() else (
                    1.0 if got in target else 0.0)
                # 中文按字重合算
                if not got.isascii():
                    hit = sum(1 for ch in got if ch in target) / max(1, len(got))
                    sim = hit
            else:
                sim = pair_similarity(got, target)
                natives = [r.get("en", "") for r in read_pairs() if r.get("dir") == "in"
                           and str(r.get("zh", "")).strip() == str(rec.get("zh", "")).strip()]
                best = max([pair_similarity(got, n) for n in natives] or [sim])
                if best >= 0.7:
                    res.setText("在母语者常用范围内（相近度 %.0f%%）。" % (best * 100))
                    res.setStyleSheet("color:%s;" % tcol("ok", self.cfg))
                    return
                res.setText("和原生表达差得较远（相近度 %.0f%%）。参考：%s"
                            % (best * 100, natives[0] if natives else target))
                res.setStyleSheet("color:%s;" % tcol("warn", self.cfg))
                return
            if sim >= 0.6:
                res.setText("对上了（相近度 %.0f%%）。" % (sim * 100))
                res.setStyleSheet("color:%s;" % tcol("ok", self.cfg))
            else:
                res.setText("接近度 %.0f%%。参考：%s" % (sim * 100, target))
                res.setStyleSheet("color:%s;" % tcol("warn", self.cfg))

        btn_play.clicked.connect(play)
        btn_chk.clicked.connect(check)
        btn_close.clicked.connect(dlg.accept)
        play()
        dlg.exec()

    def merge_pairs(self):
        rows = read_pairs()
        sel = set()
        for i in self.pair_table.selectedIndexes():
            try:
                sel.add(self._pair_rows[i.row()]["t"] + "|" + self._pair_rows[i.row()]["en"])
            except Exception:
                pass
        if len(sel) < 2:
            QMessageBox.information(self.win, "合并同义", "先按住 Ctrl 选中两条以上（最好同一个中文意思）。")
            return
        group = "M" + time.strftime("%H%M%S")
        n = 0
        for r in rows:
            if (r.get("t", "") + "|" + r.get("en", "")) in sel:
                r["merged"] = group
                n += 1
        rewrite_pairs(rows)
        self.refresh_pairs()
        QMessageBox.information(self.win, "合并同义", "已把 %d 条合并成一组（%s），复习时集中记同义表达。" % (n, group))

    def templates_dialog(self):
        from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLineEdit, QHBoxLayout,
                                       QPushButton, QLabel, QListWidget)
        dlg = QDialog(self.win)
        dlg.setWindowTitle("自定义应答模板")
        dlg.resize(560, 400)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("你说这句中文时，优先用你固定记住的英文（不走模型生成）："))
        r1 = QHBoxLayout()
        zh_in = QLineEdit()
        zh_in.setPlaceholderText("中文触发短语，例如：帮我架枪")
        en_in = QLineEdit()
        en_in.setPlaceholderText("英文应答，例如：Cover me")
        r1.addWidget(zh_in, 1)
        r1.addWidget(en_in, 1)
        v.addLayout(r1)
        lst = QListWidget()
        v.addWidget(lst, 1)

        def reload():
            lst.clear()
            for k, vals in load_templates().items():
                for x in vals:
                    lst.addItem("%s → %s" % (k, x))
        btn_row = QHBoxLayout()
        btn_add = QPushButton("添加")
        btn_close = QPushButton("关闭")
        btn_row.addStretch(1)
        btn_row.addWidget(btn_add)
        btn_row.addWidget(btn_close)
        v.addLayout(btn_row)

        def add():
            k, x = zh_in.text().strip(), en_in.text().strip()
            if not k or not x:
                return
            data = load_templates()
            data.setdefault(k, [])
            if x not in data[k]:
                data[k].append(x)
            save_templates(data)
            zh_in.clear()
            en_in.clear()
            reload()
        btn_add.clicked.connect(add)
        btn_close.clicked.connect(dlg.accept)
        reload()
        dlg.exec()

    def show_reuse_rate(self):
        used, total = pair_reuse_rate()
        pct = (used * 100.0 / total) if total else 0
        gaps = pair_gaps()
        QMessageBox.information(
            self.win, "复用率看板",
            "听过的原生英文 %d 条，其中你自己在麦克风里说出来过 %d 条（复用率 %.0f%%）。\n"
            "表达缺口（听懂了但说不出）%d 个中文意思。" % (total, used, pct, len(gaps)))


    def export_words(self):
        rows = read_words()
        if not rows:
            QMessageBox.information(self.win, "导出生词本", "生词本还是空的。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self.win, "导出生词本", str(Path.home() / ("生词本-%s.md" % time.strftime("%Y%m%d"))),
            "Markdown (*.md);;文本 (*.txt)")
        if not path:
            return
        try:
            lines = ["# 沉浸字幕生词本 %s" % time.strftime("%Y-%m-%d"), ""]
            for r in rows:
                lines.append("- **%s** %s%s" % (
                    r.get("w", ""), r.get("m", ""),
                    "（识别置信度较低，建议核对）" if r.get("low_conf") else ""))
                ctxs = r.get("ctx") or ([{"s": r.get("s", ""), "t": r.get("t", "")}]
                                       if r.get("s") else [])
                for c in ctxs:
                    lines.append("  - %s  （%s%s）" % (
                        c.get("s", ""), c.get("t", ""),
                        " · " + c["scene"] if c.get("scene") else ""))
                lines.append("")
            Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
            QMessageBox.information(self.win, "导出生词本", "已导出：\n" + path)
        except Exception as e:
            QMessageBox.warning(self.win, "导出失败", repr(e))

    def delete_words(self):
        sel = set(i.row() for i in self.word_table.selectedIndexes())
        if not sel:
            QMessageBox.information(self.win, "删除生词", "先选中要删的行。")
            return
        rows = read_words()
        keep = [r for idx, r in enumerate(rows) if (len(rows) - 1 - idx) not in sel]
        rewrite_words(keep)
        self.refresh_words()

    # ---------------- 收藏 ----------------
    def on_table_menu(self, pos):
        """右键记录里的一行：收藏 / 标注质量 / 追加备选英文 / 拆词 / 查同义 / 改名。"""
        from PySide6.QtWidgets import QMenu
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        data = self._row_data(row) or {}
        zh = str(data.get("zh", ""))
        en = str(data.get("en", ""))
        spk = str(data.get("spk", ""))
        ts = str(data.get("ts", ""))
        if not en.strip():
            return
        fav_on = (en.strip(), zh.strip()) in self._fav_keys
        qual = str(data.get("qual") or "")
        menu = QMenu(self.win)
        act_fav = menu.addAction("★ 取消收藏" if fav_on else "☆ 收藏这句")
        act_native = menu.addAction(("✅ " if qual == "native" else "") + "标为地道")
        act_machine = menu.addAction(("⚠️ " if qual == "machine" else "") + "标为机器感重（降权）")
        act_error = menu.addAction(("❌ " if qual == "error" else "") + "标为识别错误（隔离）")
        act_clear = menu.addAction("清除质量标注")
        menu.addSeparator()
        act_alts = menu.addAction("追加备选英文…")
        act_word = menu.addAction("拆词查义（英文）")
        act_syn = menu.addAction("查同义英文（中文）")
        act_name = menu.addAction("重命名这个说话人…")
        act_zh = menu.addAction("复制识别原文")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen == act_fav:
            self._fav_keys = self._refresh_fav_cache()
            self.add_fav_line(ts, zh, en, spk)
        elif chosen in (act_native, act_machine, act_error, act_clear):
            q = {act_native: "native", act_machine: "machine",
                 act_error: "error", act_clear: ""}[chosen]
            self._apply_quality(ts, en, zh, q)
        elif chosen == act_alts:
            self.edit_alts(row)
        elif chosen == act_word:
            self.open_word_dialog(en, spk)
        elif chosen == act_syn:
            self.show_synonym_english(zh)
        elif chosen == act_name and spk:
            self.rename_speaker(spk)
        elif chosen == act_zh and zh:
            try:
                from PySide6.QtWidgets import QApplication
                QApplication.clipboard().setText(zh)
            except Exception:
                pass

    def _apply_quality(self, ts, en, zh, qual):
        if not update_history_row(ts, en, {"qual": qual} if qual else {"qual": ""}, zh=zh):
            QMessageBox.information(self.win, "质量标注", "这条记录在别的日期或已压缩，只读。")
            return
        mark_sentence_bad(en, bad=(qual == "error"))
        if qual == "error":
            self._purge_word_context(en)
        sync_pair_quality(en, zh, qual)
        self.load_day(self.day_box.currentText() or time.strftime("%Y-%m-%d"))
        self.refresh_words()
        self.set_status("已标注：%s" % (QUAL_NAMES.get(qual, "未标注")), "ok")
        log("质量标注：%s → %s" % (en, QUAL_NAMES.get(qual, "未标注")))

    def rename_speaker(self, shown):
        """给说话人起个名字（队友 A / 主播 / 路人），颜色按原始编号固定不变。"""
        from PySide6.QtWidgets import QInputDialog
        # 反查原始编号：表里显示的可能是别名
        raw = shown
        names = self.cfg.get("spk_names") or {}
        for k, v in names.items():
            if str(v) == str(shown):
                raw = k
                break
        text, ok = QInputDialog.getText(self.win, "重命名说话人",
                                        "这个名字只在这台机器上显示：",
                                        text=str(names.get(raw) or raw))
        if not ok:
            return
        names = dict(self.cfg.get("spk_names") or {})
        name = text.strip()
        if not name or name == raw:
            names.pop(raw, None)
        else:
            names[raw] = name
        self.cfg["spk_names"] = names
        save_config(self.cfg)
        try:
            self.owner.cfg["spk_names"] = names
            self.owner.overlay.cfg["spk_names"] = names
        except Exception:
            pass
        day = self.day_box.currentText() or time.strftime("%Y-%m-%d")
        self.load_day(day)
        log("说话人重命名：%s → %s" % (raw, name or raw))

    def _refresh_fav_cache(self):
        keys = set()
        for r in read_fav():
            if r.get("k") == "line" and r.get("v"):
                keys.add((str(r.get("v", "")).strip(), str(r.get("zh", "")).strip()))
        return keys

    def set_spk_col_visible(self, on):
        try:
            self.table.setColumnHidden(1, not bool(on))
            if on:
                self.table.setColumnWidth(1, 96)
        except Exception:
            pass

    def locate_sentence(self, en, zh=""):
        """悬浮字幕点一句 → 主窗口记录表跳到对应行、选中并短暂高亮。"""
        from PySide6.QtCore import Qt as _Qt, QTimer
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QAbstractItemView
        try:
            self.tabs.setCurrentIndex(0)
            self.win.show()
            self.win.raise_()
            self.win.activateWindow()
            hit = -1
            for row in range(self.table.rowCount()):
                data = self._row_data(row) or {}
                if str(data.get("en", "")).strip() == str(en or "").strip() and (
                        not zh or str(data.get("zh", "")).strip() == str(zh or "").strip()):
                    hit = row
                    break
            if hit < 0:
                # 场景筛选把这一行藏起来了就先放开筛选再试一次
                if any(cb.isChecked() for cb in self.scene_boxes.values()):
                    for cb in self.scene_boxes.values():
                        cb.blockSignals(True)
                        cb.setChecked(False)
                        cb.blockSignals(False)
                    self.load_day(self.day_box.currentText() or time.strftime("%Y-%m-%d"))
                    return self.locate_sentence(en, zh)
                self.set_status("记录里没找到这一句（可能超出当前保留行数）", "")
                return
            self.table.selectRow(hit)
            self.table.setCurrentCell(hit, 2)
            self.table.scrollToItem(self.table.item(hit, 2),
                                    QAbstractItemView.PositionAtCenter)
            flash = []
            for col in (2, 3):
                it = self.table.item(hit, col)
                if it is not None:
                    flash.append((it, it.background()))
                    it.setBackground(QColor(tcol("sel", self.cfg)))
            def restore():
                for it, bg in flash:
                    it.setBackground(bg)
            QTimer.singleShot(1600, restore)
            self.set_status("已定位：%s" % (zh or en), "ok")
        except Exception as e:
            log("定位记录失败：" + repr(e))

    def _sync_preview_star(self):
        en = self.cap_en.text().strip()
        zh = self.cap_zh.text().strip()
        on = (en, zh) in self._fav_keys
        self.btn_fav_line.setText("★ 已收藏" if on else "☆ 收藏这句")
        if hasattr(self, "btn_live_fav"):
            self.btn_live_fav.setText("★ 已收藏" if on else "☆ 收藏")

    def _row_data(self, row):
        from PySide6.QtCore import Qt as _Qt
        item = self.table.item(row, 0)
        if not item:
            return None
        data = item.data(_Qt.UserRole)
        if isinstance(data, dict):
            return data
        if isinstance(data, tuple) and len(data) >= 4:
            return {"ts": data[0], "en": data[1], "zh": data[2], "spk": data[3]}
        return None

    def on_row_star(self, row, col):
        data = self._row_data(row)
        if not data:
            return
        if col == 6:
            self.add_fav_line(data.get("ts", ""), data.get("zh", ""),
                              data.get("en", ""), data.get("spk", ""))
            self._sync_preview_star()
        elif col == 7:
            self.speak_row(data.get("en", ""))
        elif col == 5:
            self.cycle_quality(row)
        elif col == 4:
            if data.get("scene"):
                self._toggle_scene(data.get("scene", ""))
            else:
                self.set_status("这条没有场景标签", "")
        elif col == 2 and self.chk_selftest.isChecked():
            self._reveal_english(row)

    def _toggle_scene(self, scene):
        scene = str(scene or "")
        cb = self.scene_boxes.get(scene)
        if cb is None:
            return
        cb.setChecked(not cb.isChecked())

    def cycle_quality(self, row):
        """点一下循环标注：✅ 地道 → ⚠️ 机器感重 → ❌ 识别错误 → 取消标注。"""
        data = self._row_data(row)
        if not data:
            return
        cur = str(data.get("qual") or "")
        order = ["", "native", "machine", "error"]
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "native"
        self._apply_quality(data.get("ts", ""), data.get("en", ""), data.get("zh", ""), nxt)

    def _purge_word_context(self, en):
        """识别错误的那句从生词卡的语境里摘掉，别让错句当例句背。"""
        try:
            rows = read_words()
            changed = False
            for w in rows:
                ctx = [c for c in (w.get("ctx") or []) if str(c.get("s", "")).strip() != str(en).strip()]
                if len(ctx) != len(w.get("ctx") or []):
                    w["ctx"] = ctx
                    changed = True
                    if not ctx:
                        w["_drop"] = True
            rows = [w for w in rows if not w.get("_drop")]
            if changed:
                rewrite_words(rows)
                log("隔离错句，已从生词语境摘除：" + en)
        except Exception as e:
            log("隔离生词语境失败：" + repr(e))

    def show_hotkeys(self):
        """顶栏入口：弹出快捷键速查面板（可搜索、可直接跳去改键位）。"""
        try:
            pop_hotkey_sheet(self)
        except Exception as e:
            log("快捷键面板打开失败：" + repr(e))

    def _hk_first_hint(self):
        """首次启动：把「快捷键」入口点亮 8 秒 + 状态行一句话，不弹窗、不重复。"""
        from PySide6.QtCore import QTimer
        try:
            self.btn_hk.setStyleSheet(
                "QPushButton{background:%s;color:#FFFFFF;border:none;border-radius:8px;"
                "padding:4px 14px;font-size:13px;}" % tcol("accent", self.cfg))
            QTimer.singleShot(8000, lambda: self.btn_hk.setStyleSheet(""))
        except Exception:
            pass
        try:
            self.set_status("快捷键 %d 个：点顶栏「快捷键」查看或修改 ｜ %s 进字幕交互"
                            % (len(HOTKEY_SPECS),
                               self.cfg.get("hotkey_toggle") or "Ctrl+Alt+G"), "ok")
        except Exception:
            pass

    def edit_alts(self, row):
        """给一条记录挂备选英文：一条中文可以有多条英文，表格里只显示折叠计数。"""
        from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QLineEdit,
                                       QPushButton, QHBoxLayout, QListWidget)
        data = self._row_data(row)
        if not data:
            return
        ts, en, zh = data.get("ts", ""), data.get("en", ""), data.get("zh", "")
        alts = list(data.get("alts") or [])
        dlg = QDialog(self.win)
        dlg.setWindowTitle("备选英文 · %s" % zh[:24])
        dlg.resize(620, 420)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("中文：%s" % zh))
        v.addWidget(QLabel("主英文：%s" % en))
        v.addWidget(QLabel("备选英文（你查到的、听到别人说的别的说法）："))
        lst = QListWidget()
        for a in alts:
            lst.addItem(a.get("en", "") if isinstance(a, dict) else str(a))
        v.addWidget(lst, 1)
        rowb = QHBoxLayout()
        inp = QLineEdit()
        inp.setPlaceholderText("例如：Hold the angle / We're holding this site")
        btn_add = QPushButton("追加")
        btn_del = QPushButton("删除选中")
        btn_close = QPushButton("关闭")
        rowb.addWidget(inp, 1)
        rowb.addWidget(btn_add)
        rowb.addWidget(btn_del)
        rowb.addWidget(btn_close)
        v.addLayout(rowb)
        status = QLabel("")
        status.setObjectName("chip")
        v.addWidget(status)

        def current_list():
            return [lst.item(i).text() for i in range(lst.count())]

        def add():
            text = inp.text().strip()
            if not text:
                return
            if text == en or text in current_list():
                status.setText("这条已经在里面了")
                return
            alts.append({"en": text, "t": time.strftime("%Y-%m-%d %H:%M:%S"), "src": "manual"})
            if update_history_row(ts, en, {"alts": alts}, zh=zh):
                lst.addItem(text)
                inp.clear()
                status.setText("已追加")
            else:
                status.setText("这条记录在别的日期，只读")

        def remove():
            i = lst.currentRow()
            if i < 0:
                return
            text = lst.item(i).text()
            alts[:] = [a for a in alts
                       if (a.get("en") if isinstance(a, dict) else str(a)) != text]
            if update_history_row(ts, en, {"alts": alts}, zh=zh):
                lst.takeItem(i)
                status.setText("已移除")

        btn_add.clicked.connect(add)
        btn_del.clicked.connect(remove)
        btn_close.clicked.connect(dlg.accept)
        dlg.exec()

    def add_fav_line(self, ts, zh, en, spk=""):
        """星标是开关：已收藏点一下取消，没收藏就收进来。表格与预览框共用这一份状态。"""
        en = (en or "").strip()
        zh = (zh or "").strip()
        if not en:
            self.set_status("这句还没有英文译文，等字幕出来再收藏", "warn")
            return
        self._fav_keys = self._refresh_fav_cache()
        if (en, zh) in self._fav_keys:
            rows = read_fav()
            keep = [r for r in rows
                    if not (r.get("k") == "line" and str(r.get("v", "")).strip() == en
                            and str(r.get("zh", "")).strip() == zh)]
            rewrite_fav(keep)
            log("取消收藏：" + en)
        elif append_fav("line", en, extra=ts, zh=zh, en=en, spk=spk):
            log("收藏整句：" + en)
        self._fav_keys = self._refresh_fav_cache()
        self.refresh_fav()
        self.refresh_row_stars()
        self._sync_preview_star()

    def refresh_row_stars(self):
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QTableWidgetItem
        for row in range(self.table.rowCount()):
            data = self._row_data(row) or {}
            en = str(data.get("en", ""))
            zh = str(data.get("zh", ""))
            on = (en.strip(), zh.strip()) in self._fav_keys
            it = self.table.item(row, 6)
            if it is None:
                it = QTableWidgetItem("")
                it.setTextAlignment(_Qt.AlignCenter)
                self.table.setItem(row, 6, it)
            it.setText("★" if on else "☆")
            it.setForeground(QColor(tcol("accent", self.cfg) if on
                                    else tcol("muted", self.cfg)))

    def fav_current(self):
        """当前字幕那行大字 → 收藏整句。"""
        en = self.cap_en.text().strip()
        zh = self.cap_zh.text().strip()
        if not en or en == "等待声音…":
            self.set_status("还没抓到句子，等字幕出来再收藏", "warn")
            return
        spk = getattr(getattr(self.owner, "overlay", None), "_cur_spk", "") or ""
        self.add_fav_line(time.strftime("%Y-%m-%d %H:%M:%S"), zh, en, spk)

    def _fav_scene(self, r):
        """收藏条目的场景：从同名句对里反查，句对没有就返回空。"""
        en = str(r.get("v") if r.get("k") == "line" else (r.get("en") or "")).strip()
        zh = str(r.get("zh") or "").strip()
        try:
            for p in read_pairs():
                if str(p.get("en", "")).strip() == en and (
                        not zh or str(p.get("zh", "")).strip() == zh):
                    return str(p.get("scene") or "")
        except Exception:
            pass
        return ""

    def refresh_fav(self):
        from PySide6.QtWidgets import QTableWidgetItem
        rows = read_fav()
        if self.cfg.get("filter_by_lang"):
            tl = str(self.cfg.get("target_lang") or "en")
            rows = [r for r in rows if str(r.get("target_lang") or "en") == tl]
        sel = self._selected_scenes()
        if sel:
            rows = [r for r in rows if self._fav_scene(r) in sel]
        self.fav_table.setRowCount(0)
        for r in reversed(rows):
            self.fav_table.insertRow(0)
            kind = "句子" if r.get("k") == "line" else "单词"
            note = (r.get("zh") if r.get("k") == "line" else r.get("x")) or ""
            vals = (kind, r.get("t", ""), r.get("v", ""), note, r.get("spk", ""))
            for i, v in enumerate(vals):
                self.fav_table.setItem(0, i, QTableWidgetItem(str(v)))
        if self.fav_table.rowCount():
            self.fav_table.resizeRowsToContents()
        n_line = sum(1 for r in rows if r.get("k") == "line")
        n_word = sum(1 for r in rows if r.get("k") == "word")
        self._fav_keys = self._refresh_fav_cache()
        self._sync_preview_star()
        self.fav_count.setText("收藏 %d 条（句子 %d / 单词 %d）" % (len(rows), n_line, n_word))

    def export_fav(self):
        rows = read_fav()
        if not rows:
            QMessageBox.information(self.win, "导出收藏", "收藏夹还是空的。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self.win, "导出收藏", str(Path.home() / ("收藏-%s.md" % time.strftime("%Y%m%d"))),
            "Markdown (*.md);;文本 (*.txt)")
        if not path:
            return
        try:
            lines = ["# 沉浸字幕收藏 %s" % time.strftime("%Y-%m-%d"), ""]
            sents = [r for r in rows if r.get("k") == "line"]
            words = [r for r in rows if r.get("k") == "word"]
            if sents:
                lines += ["## 收藏的句子", ""]
                for r in sents:
                    lines += ["- %s" % r.get("v", ""), "  %s" % r.get("zh", ""),
                              "  %s %s" % (r.get("spk", ""), r.get("t", ""))]
            if words:
                lines += ["", "## 收藏的单词", ""]
                for r in words:
                    lines += ["- **%s** %s" % (r.get("v", ""), r.get("x", "")),
                              "  %s" % (r.get("en") or r.get("zh") or ""), "  %s" % r.get("t", "")]
            Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
            QMessageBox.information(self.win, "导出收藏", "已导出：\n" + path)
        except Exception as e:
            QMessageBox.warning(self.win, "导出失败", repr(e))

    def delete_fav(self):
        sel = set(i.row() for i in self.fav_table.selectedIndexes())
        if not sel:
            QMessageBox.information(self.win, "删除收藏", "先选中要删的行。")
            return
        rows = read_fav()
        keep = [r for idx, r in enumerate(rows) if (len(rows) - 1 - idx) not in sel]
        rewrite_fav(keep)
        self.refresh_fav()

    def _badge_text(self):
        """徽章里除了状态，还要写清是哪个引擎、哪路音频，才好判断问题出在哪。"""
        eng = ("在线模型" if (str(self.cfg.get("engine") or "") == "online"
                             and self.cfg.get("api_key")) else "本地 Argos")
        dev = str(self.cfg.get("loopback_device") or "系统默认音频")
        if len(dev) > 14:
            dev = dev[:12] + "…"
        return "%s ｜ %s" % (eng, dev)

    def _badge_set(self, state, tone=""):
        if not hasattr(self, "badge"):
            return
        c = THEME_COLORS.get(str(self.cfg.get("theme") or "dark"), THEME_COLORS["dark"])
        color = {"ok": c["ok"], "listening": c["ok"], "loading": c["warn"],
                 "warn": c["warn"], "error": c["err"]}.get(tone, c["muted"])
        mark = {"ok": "●", "listening": "●", "warn": "▲", "error": "■",
                "loading": "◐"}.get(tone, "○")
        self.badge.setText("%s %s ｜ %s" % (mark, state, self._badge_text()))
        self.badge.setStyleSheet(
            "color:%s;font-size:11px;background:rgba(127,127,127,20);"
            "border-radius:8px;padding:3px 9px;" % color)
        self.badge.setToolTip("状态：%s\n引擎：%s\n音频源：%s\n左键打开 Lab"
                              % (state, self._badge_text(),
                                 self.cfg.get("loopback_device") or "系统默认音频"))

    def set_status(self, text, tone=""):
        c = THEME_COLORS.get(str(self.cfg.get("theme") or "dark"), THEME_COLORS["dark"])
        color = {"ok": c["ok"], "listening": c["ok"], "info": c["accent"],
                 "loading": c["accent"], "warn": c["warn"], "error": c["err"]}.get(
                     tone, c["muted"])
        self.dot.setStyleSheet("color:%s;font-size:11px;" % color)
        self.status.setText(text)
        self._badge_set(text, tone)
        fade_in(self.status, 120)

    # ---------------- 交互 ----------------
    def _reset_overlay_pos(self):
        """字幕条跑出屏幕 / 换过显示器后找回：清位置记忆，回到屏幕顶部。"""
        try:
            ov = self.owner.overlay
            ov.cfg["overlay_pos"] = "top"
            ov.cfg.pop("window", None)
            ov.apply_position(save=True)
            ov._fit_height()
            save_config(ov.cfg)
            self.set_status("字幕条已复位到屏幕顶部", "ok")
            log("字幕条位置已重置（托盘入口）")
        except Exception as e:
            log("重置字幕条位置失败：" + repr(e))

    def on_pause(self):
        self.owner.toggle_pause()
        running = bool(self.owner.capture and getattr(self.owner.capture, "running", False))
        if not running and not self.owner.paused:
            self.btn_pause.setText("开始")
        else:
            self.btn_pause.setText("继续" if self.owner.paused else "暂停")

    def on_overlay_toggle(self, checked):
        self.cfg["show_overlay"] = bool(checked)
        self.owner.set_overlay_visible(bool(checked))

    def on_overlay_toggle_click(self):
        self.chk_overlay.setChecked(not self.chk_overlay.isChecked())

    def clear_rows(self):
        self.table.setRowCount(0)
        self._refresh_recent_preview()

    def _selected_rows(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self._row_data(r) for r in rows if self._row_data(r)]

    def speak_row(self, en):
        """表格里的发音入口：点一下听这句英文，语速比对局慢读稍快一点。"""
        text = str(en or "").strip()
        if not text:
            return
        try:
            base = float(self.cfg.get("read_rate") or 0)
            self.owner.overlay.read_text(text, rate=max(-10.0, min(10.0, base + 6)))
            self.set_status("朗读：%s" % text[:40], "ok")
        except Exception as e:
            log("表格朗读失败：" + repr(e))

    def batch_menu(self):
        from PySide6.QtWidgets import QMenu, QMessageBox
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(self.win, "批量操作",
                                    "先在表格里选中几条（Ctrl 点选 / Shift 连选），再点这里。")
            return
        menu = QMenu(self.win)
        act_fav = menu.addAction("批量加入生词本（%d 条句子）" % len(rows))
        sub = menu.addMenu("批量打场景标签")
        scene_acts = {}
        for key in ("fps", "video", "chat"):
            scene_acts[sub.addAction(SCENE_NAMES.get(key, key))] = key
        act_del = menu.addAction("批量删除无效识别（%d 条）" % len(rows))
        chosen = menu.exec(self.btn_batch.mapToGlobal(self.btn_batch.rect().bottomLeft()))
        if chosen == act_fav:
            self._batch_fav(rows)
        elif chosen in scene_acts:
            self._batch_scene(rows, scene_acts[chosen])
        elif chosen == act_del:
            self._batch_delete(rows)

    def _batch_fav(self, rows):
        added = 0
        for d in rows:
            if append_fav("line", str(d.get("en", "")).strip(), extra="",
                          zh=str(d.get("zh", "")), en=str(d.get("en", "")),
                          spk=str(d.get("spk", ""))):
                added += 1
        self._fav_keys = self._refresh_fav_cache()
        self.refresh_fav()
        self.refresh_row_stars()
        self._sync_preview_star()
        self.set_status("批量收藏：新增 %d 条（重复的自动跳过）" % added, "ok")

    def _batch_scene(self, rows, scene):
        changed = 0
        for d in rows:
            if update_history_row(d.get("ts", ""), d.get("en", ""), {"scene": scene},
                                  d.get("zh", "")):
                changed += 1
        self.load_day(self.day_box.currentText() or time.strftime("%Y-%m-%d"))
        self.set_status("批量打标签：%d 条 → %s" % (changed, SCENE_SHORT.get(scene, scene)), "ok")

    def _batch_delete(self, rows):
        from PySide6.QtWidgets import QMessageBox
        day = self.day_box.currentText() or time.strftime("%Y-%m-%d")
        keys = [(d.get("ts", ""), d.get("en", "")) for d in rows]
        for d in rows:
            mark_sentence_bad(d.get("en", ""), True)
        gone = delete_history_entries(day, keys)
        if not gone:
            QMessageBox.information(
                self.win, "批量删除",
                "这一天没有可改的 jsonl（旧日期已经压成只读压缩包），没有删掉东西。")
            return
        self.load_day(day)
        self._refresh_stats()
        self.set_status("批量删除：清掉 %d 条无效识别（已同时隔离）" % gone, "ok")

    def export_menu(self):
        """导出学习记录：和运行日志彻底分开，格式可选。"""
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self.win)
        act_anki = menu.addAction("导出 Anki CSV（中文, 英文, 时间, 场景）")
        act_plain = menu.addAction("导出纯对照文本（一行中文一行英文）")
        act_md = menu.addAction("导出 Markdown（带说话人）")
        chosen = menu.exec(self.btn_export.mapToGlobal(self.btn_export.rect().bottomLeft()))
        if chosen == act_anki:
            self.export_anki()
        elif chosen == act_plain:
            self.export_plain()
        elif chosen == act_md:
            self.export_records()

    def _day_rows(self):
        day = self.day_box.currentText() or time.strftime("%Y-%m-%d")
        rows = read_history(day, limit=2000)
        rows = [r for r in rows if self._scene_ok(r.get("scene"))
                and str(r.get("zh", "")).strip() and str(r.get("en", "")).strip()]
        return rows

    def export_anki(self):
        import csv
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        rows = self._day_rows()
        if not rows:
            QMessageBox.information(self.win, "导出 Anki CSV", "这一天没有可导出的中英句对。")
            return
        day = self.day_box.currentText() or time.strftime("%Y-%m-%d")
        path, _ = QFileDialog.getSaveFileName(
            self.win, "导出 Anki CSV", str(Path.home() / ("Anki-句对-%s.csv" % day.replace("-", ""))),
            "CSV (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["中文识别文本", "目标译文", "时间", "场景标签",
                            "source_lang", "target_lang"])
                for r in rows:
                    w.writerow([r.get("zh", ""), r.get("en", ""), r.get("t", ""),
                                SCENE_SHORT.get(r.get("scene") or "", "未标"),
                                r.get("source_lang") or r.get("sl") or CURRENT_LANGS["sl"],
                                r.get("target_lang") or r.get("tl") or CURRENT_LANGS["tl"]])
            QMessageBox.information(
                self.win, "导出 Anki CSV",
                "已导出 %d 条：\n%s\n\n导入 Anki 时字段顺序就是 中文 → 英文 → 时间 → 场景。" % (len(rows), path))
        except Exception as e:
            QMessageBox.warning(self.win, "导出失败", repr(e))

    def export_plain(self):
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        rows = self._day_rows()
        if not rows:
            QMessageBox.information(self.win, "导出纯对照文本", "这一天没有可导出的中英句对。")
            return
        day = self.day_box.currentText() or time.strftime("%Y-%m-%d")
        path, _ = QFileDialog.getSaveFileName(
            self.win, "导出纯对照文本", str(Path.home() / ("中英对照-%s.txt" % day.replace("-", ""))),
            "纯文本 (*.txt)")
        if not path:
            return
        try:
            out = []
            for r in rows:
                out += [str(r.get("zh", "")).strip(), str(r.get("en", "")).strip(), ""]
            Path(path).write_text("\n".join(out), encoding="utf-8")
            QMessageBox.information(self.win, "导出纯对照文本",
                                    "已导出 %d 组中英对照：\n%s" % (len(rows), path))
        except Exception as e:
            QMessageBox.warning(self.win, "导出失败", repr(e))

    def export_records(self):
        if not self.table.rowCount():
            QMessageBox.information(self.win, "导出记录", "现在还没有记录。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self.win, "导出记录", str(Path.home() / ("沉浸字幕-%s.md" % time.strftime("%Y%m%d"))),
            "Markdown (*.md);;文本 (*.txt)")
        if not path:
            return
        try:
            lines = ["# 沉浸字幕记录 %s" % time.strftime("%Y-%m-%d"), ""]
            for r in range(self.table.rowCount() - 1, -1, -1):
                ts = self.table.item(r, 0).text() if self.table.item(r, 0) else ""
                spk = self.table.item(r, 1).text() if self.table.item(r, 1) else ""
                zh = self.table.item(r, 2).text() if self.table.item(r, 2) else ""
                en = self.table.item(r, 3).text() if self.table.item(r, 3) else ""
                head = ("**%s** " % ts) + (("[%s] " % spk) if spk else "")
                lines += ["- %s%s" % (head, zh), "  %s" % en]
            Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
            QMessageBox.information(self.win, "导出记录", "已导出：\n" + path)
        except Exception as e:
            QMessageBox.warning(self.win, "导出失败", repr(e))

    def open_log(self):
        try:
            os.startfile(str(LOG_PATH))
        except Exception as e:
            QMessageBox.warning(self.win, "打不开日志", repr(e))

    def open_history_dir(self):
        try:
            HISTORY_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(str(HISTORY_DIR))
        except Exception as e:
            QMessageBox.warning(self.win, "打不开目录", repr(e))

    def _tray_activated(self, reason):
        from PySide6.QtWidgets import QSystemTrayIcon
        if reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger):
            self.show()

    def _close_event(self, ev):
        from PySide6.QtWidgets import QSystemTrayIcon
        if self.tray is not None:
            ev.ignore()
            self.win.hide()
            self.tray.showMessage("Oversub 沉浸字幕", "已最小化到托盘，双击图标可以再打开。",
                                  QSystemTrayIcon.Information, 2500)
        else:
            self.owner.quit()

    def show(self):
        # 先用 showNormal 从最小化/最大化恢复（否则窗口会一直躺在任务栏），
        # 再强制套用上次保存的尺寸：showNormal 自己可能把窗口压回 160×28 标题条。
        from PySide6.QtCore import Qt as _Qt, QTimer as _QTimer
        self.win.showNormal()
        self.win.show()
        self._restore_geometry()
        self.win.resize(max(900, self.win.width()), max(600, self.win.height()))
        self._native_restore_window()
        self.win.raise_()
        self.win.activateWindow()
        # 布局在 show 之后还会再算一次，0ms / 250ms 各校准一次，防止被压回标题条
        _QTimer.singleShot(0, self._restore_geometry)
        _QTimer.singleShot(250, self._reassert_geometry)

    def _reassert_geometry(self):
        """窗口被压成标题条、异常过小或缩在任务栏时，强制回到上次保存的尺寸。"""
        try:
            if self.win.isMinimized():
                self.win.showNormal()
            # 先做 Win32 兜底：Qt 侧的 width/height 在“缩在任务栏”时仍是旧值，
            # 只看 Qt 尺寸会误判成“正常”，从而永远不恢复。
            self._native_restore_window()
            if self.win.width() >= 900 and self.win.height() >= 600:
                return
            self._restore_geometry()
            if self.win.width() < 900 or self.win.height() < 600:
                from PySide6.QtGui import QGuiApplication
                scr = (self.win.screen() or QGuiApplication.primaryScreen()).availableGeometry()
                w = min(int((self.cfg.get("main_win") or {}).get("w") or 1100), scr.width())
                h = min(int((self.cfg.get("main_win") or {}).get("h") or 760), scr.height())
                self.win.resize(max(900, w), max(600, h))
                self.win.move(scr.left() + max(0, (scr.width() - self.win.width()) // 2),
                              scr.top() + max(0, (scr.height() - self.win.height()) // 2))
            self.win.raise_()
            self.win.activateWindow()
            self._native_restore_window()
        except Exception:
            pass

    def _native_restore_window(self):
        """Win32 兜底：系统把窗口以最小化/标题条状态交回来时，按保存尺寸强制恢复。"""
        try:
            import ctypes
            from ctypes import wintypes as wt
            saved = self.cfg.get("main_win") or {}
            x, y = saved.get("x"), saved.get("y")
            if x is None or y is None:
                return
            hwnd = int(self.win.winId())
            u = ctypes.windll.user32
            class _R(ctypes.Structure):
                _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                            ("r", ctypes.c_long), ("b", ctypes.c_long)]
            r = _R()
            u.GetWindowRect(hwnd, ctypes.byref(r))
            ww, wh = r.r - r.l, r.b - r.t
            if not u.IsIconic(hwnd) and ww >= 900 and wh >= 600:
                return
            w = int(saved.get("w") or 1100) + int(saved.get("fx") or 16)
            h = int(saved.get("h") or 760) + int(saved.get("fy") or 40)
            u.ShowWindow(hwnd, 9)                       # SW_RESTORE
            u.SetWindowPos(hwnd, 0, int(x), int(y), w, h, 0x0040)   # SWP_SHOWWINDOW
        except Exception as e:
            log("窗口原生恢复失败：" + repr(e))

    def hide(self):
        self.win.hide()


# ---------------------------------------------------------------- 编排
class App:
    def __init__(self, cfg):
        self.cfg = cfg
        restore_latest_backup_if_needed()
        self.ui_q = queue.Queue()
        self.rec_q = queue.Queue()
        self.seg_q = queue.Queue(maxsize=10)     # 有界缓冲：识别跟不上时丢最旧的段，不无限吃内存
        self.capture = None
        self.pipeline = None
        self.mic_assist = None
        self.paused = False
        self.overlay = Overlay(cfg, self.ui_q, self.quit, self.toggle_pause, self.open_settings)
        self.main = MainWindow(self, cfg)
        self.overlay.book = self.main.book          # 交互模式里选词查义，复用同一本词典
        self.overlay.on_locate = self.main.locate_sentence   # 点字幕 → 定位记录表那一行
        self.overlay.last_audio_getter = lambda: (self.capture.last_audio if self.capture else None)
        self._interactive = False
        self._hotkey_id = 0
        self._hk_prev = False
        self._hk_last = 0.0
        self._lb_prev = False
        try:
            self.overlay.refresh_hard_words()
        except Exception:
            pass
        # 首次启动一次性点亮「快捷键」入口：只做一次，不弹窗打断
        if not cfg.get("hk_hint_shown"):
            cfg["hk_hint_shown"] = True
            save_config(cfg)
            try:
                from PySide6.QtCore import QTimer
                QTimer.singleShot(900, self.main._hk_first_hint)
            except Exception:
                pass

    # ---------------- 全局热键（四个，都可改键） ----------------
    def _hotkey_defs(self):
        """(名字, 组合键, 动作)。动作在热键线程里只记名字，真正执行回主线程。"""
        return [
            ("切换交互模式", str(self.cfg.get("hotkey_toggle") or "Ctrl+Alt+Z"), self.toggle_interactive),
            ("收藏上一句", str(self.cfg.get("hotkey_fav_last") or "Ctrl+Alt+D"), self.overlay.fav_last),
            ("历史面板", str(self.cfg.get("hotkey_history") or "Ctrl+Alt+H"), self.overlay.toggle_history_panel),
            ("清空字幕流", str(self.cfg.get("hotkey_clear") or "Ctrl+Alt+X"), self.overlay.clear_stream),
            ("开/关字幕显示", str(self.cfg.get("hotkey_subtitle") or "Ctrl+Alt+V"), self.toggle_subtitle),
            ("开/关音频识别", str(self.cfg.get("hotkey_capture") or "Ctrl+Alt+R"), self.toggle_capture_hotkey),
            ("收藏·简单", str(self.cfg.get("hotkey_fav_easy") or "Ctrl+Alt+1"),
             lambda: self.overlay.fav_last(diff="简单")),
            ("收藏·中等", str(self.cfg.get("hotkey_fav_mid") or "Ctrl+Alt+2"),
             lambda: self.overlay.fav_last(diff="中等")),
            ("收藏·困难", str(self.cfg.get("hotkey_fav_hard") or "Ctrl+Alt+3"),
             lambda: self.overlay.fav_last(diff="困难")),
            ("强制退出交互", str(self.cfg.get("hotkey_panic") or "Ctrl+Alt+Q"),
             self.overlay.force_exit_interactive),
            ("唤起素材库", str(self.cfg.get("hotkey_library") or "Ctrl+Alt+L"),
             self.open_library_hotkey),
            ("切换自测模式", str(self.cfg.get("hotkey_selftest") or "Ctrl+Alt+T"),
             self.toggle_selftest_hotkey),
            ("字幕字号 +", str(self.cfg.get("hotkey_font_up") or "Ctrl+Alt+="),
             lambda: self.overlay.bump_font(2)),
            ("字幕字号 -", str(self.cfg.get("hotkey_font_down") or "Ctrl+Alt+-"),
             lambda: self.overlay.bump_font(-2)),
            ("开始/暂停捕获", str(self.cfg.get("hotkey_f5_capture") or "Ctrl+Alt+P"),
             self.toggle_capture_hotkey),
            ("收藏当前句", str(self.cfg.get("hotkey_f6_fav") or "Ctrl+Alt+U"),
             self.overlay.fav_last),
            ("字幕显示", str(self.cfg.get("hotkey_f7_sub") or "Ctrl+Alt+B"),
             self.toggle_subtitle),
            ("历史抽屉", str(self.cfg.get("hotkey_f8_drawer") or "Ctrl+Alt+J"),
             self.toggle_drawer_hotkey),
            ("自动朗读", str(self.cfg.get("hotkey_f9_autoread") or "Ctrl+Alt+Y"),
             self.toggle_autoread_hotkey),
            ("紧急静音", str(self.cfg.get("hotkey_f10_mute") or "Ctrl+Alt+M"),
             self.toggle_mute_hotkey),
            # 旧的 Ctrl+L 入口默认留空：浏览器地址栏就是 Ctrl+L，不再抢
            ("Ctrl+L 素材库", str(self.cfg.get("hotkey_ctrl_l") or ""),
             self.open_library_hotkey),
            ("Ctrl+, Lab", str(self.cfg.get("hotkey_ctrl_lab") or "Ctrl+Alt+,"),
             self.open_lab_hotkey),
        ]

    def _build_hotkey_defs(self):
        defs = []
        seen = {}
        for i, (label, combo, fn) in enumerate(self._hotkey_defs()):
            combo = str(combo or "").strip()
            if not combo:
                continue                       # 留空 = 这个动作不绑全局键
            parsed = _parse_hotkey(combo)
            if not parsed:
                log("热键写法不认识：%s = %s" % (label, combo))
                continue
            key = (parsed[0], parsed[1])
            if key in seen:
                log("热键冲突：%s 与 %s 都是 %s，后者不注册" % (seen[key], label, combo))
                try:
                    self.ui_q.put(("state", "热键冲突：%s 与 %s 都绑定了 %s" % (
                        seen[key], label, combo), "warn"))
                except Exception:
                    pass
                continue
            seen[key] = label
            defs.append({"id": HOTKEY_BASE + i + 1, "label": label, "combo": combo,
                         "mods": parsed[0], "vk": parsed[1], "fn": fn})
        return defs

    def reload_hotkeys(self):
        """改完键位后原地重注册：注销旧 id → 按新配置注册，不用重启。"""
        try:
            self._hk_defs = self._build_hotkey_defs()
            self._hk_by_id = {d["id"]: d for d in self._hk_defs}
            self._hk_keydown.clear()
            self._hk_last_run.clear()
            self._hk_reload_q.put(self._hk_defs)
            log("热键已按新配置重新注册（%d 个）" % len(self._hk_defs))
        except Exception as e:
            log("重新注册热键失败：" + repr(e))

    def open_history(self):
        """把主窗口抬出来并切到「记录」页：那里能点句子、双击单词。"""
        try:
            self.main.show()
            self.main.raise_()
            self.main.activateWindow()
            self.main.tabs.setCurrentIndex(0)
            log("热键：打开历史字幕面板")
        except Exception as e:
            log("打开历史面板失败：" + repr(e))

    def open_library_hotkey(self):
        try:
            self.main.show()
            self.main.raise_()
            self.main.activateWindow()
            self.main.tabs.setCurrentIndex(1)
            log("热键：唤起素材库")
        except Exception as e:
            log("唤起素材库失败：" + repr(e))

    def toggle_drawer_hotkey(self):
        try:
            self.main.show()
            self.main.raise_()
            self.main.activateWindow()
            self.main._toggle_hist_drawer()
        except Exception as e:
            log("切换历史抽屉失败：" + repr(e))

    def toggle_mute_hotkey(self):
        try:
            muted = not bool(self.cfg.get("tts_muted"))
            self.cfg["tts_muted"] = muted
            save_config(self.cfg)
            if muted:
                self.overlay.clear_read_queue()
            self.main.set_status("朗读已静音（F10 恢复）" if muted else "朗读已恢复", "warn" if muted else "ok")
        except Exception as e:
            log("紧急静音失败：" + repr(e))

    def toggle_autoread_hotkey(self):
        try:
            ov = self.overlay
            on = not bool(self.cfg.get("auto_read"))
            ov.chk_auto.blockSignals(True)
            ov.chk_auto.setChecked(on)
            ov.chk_auto.blockSignals(False)
            ov._auto_toggled(on)
            self.main.set_status("自动朗读：%s" % ("开" if on else "关"), "ok")
        except Exception as e:
            log("切换自动朗读失败：" + repr(e))

    def open_lab_hotkey(self):
        try:
            self.main.show()
            self.main.raise_()
            self.main._nav_go("Lab")
            log("热键：打开 Lab 快速配置")
        except Exception as e:
            log("打开 Lab 失败：" + repr(e))

    def toggle_selftest_hotkey(self):
        try:
            self.main.toggle_selftest_shortcut()
        except Exception as e:
            log("切换自测模式失败：" + repr(e))

    def install_hotkey(self):
        """注册全部全局热键：独立消息线程收 WM_HOTKEY，主线程定时器分发；
        另外每个组合键都做按键状态轮询兜底，热键被别的程序占用时也能用。
        改键时往队列里塞新定义，由同一条线程注销旧的再注册新的，避免多线程抢消息。"""
        try:
            import ctypes

            class MSG(ctypes.Structure):
                _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                            ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_size_t),
                            ("time", ctypes.c_uint), ("pt_x", ctypes.c_long),
                            ("pt_y", ctypes.c_long), ("lPrivate", ctypes.c_uint)]

            self._hk_hits = []
            self._hk_keydown = {}
            self._hk_last_run = {}
            self._hk_reload_q = queue.Queue()
            self._hk_defs = self._build_hotkey_defs()
            self._hk_by_id = {d["id"]: d for d in self._hk_defs}
            self._hk_reload_q.put(self._hk_defs)

            def loop():
                u = ctypes.windll.user32
                cur = []
                msg = MSG()
                while True:
                    try:
                        newdefs = self._hk_reload_q.get_nowait()
                    except queue.Empty:
                        newdefs = None
                    if newdefs is not None:
                        for d in cur:
                            try:
                                u.UnregisterHotKey(None, d["id"])
                            except Exception:
                                pass
                        cur = newdefs
                        for d in cur:
                            ok = u.RegisterHotKey(None, d["id"], d["mods"] | MOD_NOREPEAT, d["vk"])
                            d["active"] = bool(ok)
                            log("全局热键 %s：%s %s" % (
                                d["label"], d["combo"],
                                "已注册" if ok else "被其它程序占用，已停用（请到设置里改键）"))
                        continue
                    if u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                        if msg.message == 0x0312:
                            self._hk_hits.append(int(msg.wParam))
                    else:
                        time.sleep(0.05)

            threading.Thread(target=loop, daemon=True).start()
            from PySide6.QtCore import QTimer
            self._hotkey_timer = QTimer()
            self._hotkey_timer.timeout.connect(self._poll_hotkey)
            self._hotkey_timer.start(80)
        except Exception as e:
            log("注册热键失败：" + repr(e))

    def _poll_hotkey(self):
        """两路兜底：系统热键消息 + 直接读按键状态（热键被占用时也能用）。"""
        fired = []
        while self._hk_hits:
            try:
                fired.append(self._hk_hits.pop())
            except IndexError:
                break
        try:
            import ctypes
            u = ctypes.windll.user32
            for d in self._hk_defs:
                if d.get("active") is False:
                    continue        # 被别的程序占用就不抢，避免和主应用同时触发
                down = bool(u.GetAsyncKeyState(d["vk"]) & 0x8000)
                if d["mods"] & MOD_CONTROL:
                    down = down and bool(u.GetAsyncKeyState(0x11) & 0x8000)
                if d["mods"] & MOD_ALT:
                    down = down and bool(u.GetAsyncKeyState(0x12) & 0x8000)
                if d["mods"] & MOD_SHIFT:
                    down = down and bool(u.GetAsyncKeyState(0x10) & 0x8000)
                if d["mods"] & MOD_WIN:
                    down = down and (bool(u.GetAsyncKeyState(0x5B) & 0x8000)
                                     or bool(u.GetAsyncKeyState(0x5C) & 0x8000))
                if down and not self._hk_keydown.get(d["id"]):
                    fired.append(d["id"])
                self._hk_keydown[d["id"]] = down
        except Exception:
            pass

        now = time.monotonic()
        for hid in fired:
            d = self._hk_by_id.get(hid)
            if not d:
                continue
            if now - self._hk_last_run.get(hid, 0.0) < 0.35:
                continue        # 消息和轮询同时命中时只执行一次
            self._hk_last_run[hid] = now
            try:
                d["fn"]()
            except Exception as e:
                log("热键执行失败：%s %r" % (d["label"], e))
            finally:
                release_hotkey_modifiers(d.get("combo", ""))
            if d["label"] == "切换交互模式":
                log("字幕交互模式：%s" % ("开（可选中文字）" if self.overlay._interactive else "关（鼠标穿透回游戏）"))

        # 点字幕以外的任何地方（比如游戏画面）就退出交互，把鼠标还给游戏
        try:
            from PySide6.QtGui import QCursor
            lb = bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
            if lb and not self._lb_prev and self.overlay._interactive:
                if not self.overlay.win.frameGeometry().contains(QCursor.pos()):
                    self.overlay.set_interactive(False)
                    log("交互模式：点到字幕外，自动退出")
            self._lb_prev = lb
        except Exception:
            pass

        # 无操作自动退回：秒数可配（3 / 5 / 0=不自动退出），慢慢查单词可以关掉
        try:
            from PySide6.QtGui import QCursor
            if self.overlay._interactive:
                timeout = float(self.cfg.get("interactive_timeout", 3) or 0)
                if timeout <= 0:
                    self.overlay._itv_at = time.monotonic()   # 关闭超时就一直续命
                else:
                    cur = QCursor.pos()
                    if cur != getattr(self.overlay, "_itv_pos", None):
                        self.overlay._itv_pos = cur
                        self.overlay._itv_at = time.monotonic()
                    elif time.monotonic() - getattr(self.overlay, "_itv_at", time.monotonic()) > timeout:
                        self.overlay.set_interactive(False)
                        log("交互模式：%.1f 秒无操作，自动退回游戏" % timeout)
        except Exception:
            pass

    def toggle_interactive(self):
        self.overlay.set_interactive(not getattr(self.overlay, "_interactive", False))
        log("字幕交互模式：%s" % ("开（可选中文字）" if self.overlay._interactive else "关（鼠标穿透回游戏）"))

    def set_overlay_visible(self, visible):
        try:
            if visible:
                self.overlay.show()
            else:
                self.overlay.win.hide()
        except Exception as e:
            log("切悬浮条失败：" + repr(e))

    def toggle_subtitle(self):
        """热键开关字幕显示：屏幕干净，但识别和记录照跑。"""
        visible = self.overlay.win.isVisible()
        self.set_overlay_visible(not visible)
        log("字幕显示：%s" % ("关" if visible else "开"))

    def toggle_capture_hotkey(self):
        """热键开关音频识别：不想听的时候彻底停掉采集，省 CPU。"""
        if self.capture and self.capture.running:
            self.stop_capture()
            self.cfg["source_on"] = False
            try:
                self.overlay.cfg["source_on"] = False
            except Exception:
                pass
            self.overlay.set_status("已停止收听", "")
            log("音频识别：关")
        else:
            self.cfg["source_on"] = True
            self.paused = False
            self.start_capture()
            self.overlay.set_status("识别中", "ok")
            log("音频识别：开")

    def _pipeline_state(self, kind, msg):
        tone = "ok" if kind in ("ready", "asr_ready", "trans_ready") else (
            "error" if kind == "error" else "")
        if kind == "asr_ready":
            self.main.chip_asr.setText("ASR：" + msg.split("：", 1)[-1] + " · 就绪")
        elif kind == "trans_ready":
            self.main.chip_trans.setText("翻译：" + msg.split("：", 1)[-1] + " · 就绪")
        elif kind == "error":
            # 单独标出是哪一段加载失败，不笼统卡在「正在加载」
            if msg.startswith("ASR"):
                self.main.chip_asr.setText("ASR：加载失败")
            elif msg.startswith("翻译"):
                self.main.chip_trans.setText("翻译：加载失败")
        self.ui_q.put(("state", msg, tone))
        self.main.set_status(msg, tone)
        if kind == "ready" and not self.paused and bool(self.cfg.get("auto_start_capture", False)):
            # 默认不自动开录：按钮的「开始捕获」必须是用户点了才真的开始，
            # 否则按钮文案落在「开始」而引擎已在收听，第一次点击就变成了暂停。
            self.start_capture()
            self.start_mic()

    def _audio_state(self, kind, msg):
        tone = "ok" if kind == "listening" else "error"
        self.ui_q.put(("state", msg, tone))
        self.main.set_status(msg, tone)
        if kind == "error":
            self.paused = True

    def start_pipeline(self):
        self.pipeline = Pipeline(self.cfg, self.seg_q, self.ui_q, self._pipeline_state, self.rec_q,
                                 self.overlay.tts_busy)
        self.pipeline.start()

    def start_capture(self):
        if not bool(self.cfg.get("source_on", True)):
            log("外源音频识别已关：只保留麦克风链路")
            return
        if self.capture and self.capture.running:
            return
        self.capture = AudioCapture(self.cfg, self.seg_q, self._audio_state)
        self.capture.start()
        try:
            self.overlay.btn_pause.setText("暂停")
        except Exception:
            pass
        try:
            self.main._sync_capture_ui()
        except Exception:
            pass

    def _mic_translate(self):
        """麦克风侧用的翻译器：优先离线 Argos，在线引擎就走在线接口。"""
        p = self.pipeline
        if p is None:
            return None
        if getattr(p, "translator", None) is not None:
            tr = p.translator

            class _GameTerm:
                def translate(self, text):
                    return translate_guarded(tr, text)

            return _GameTerm()

        class _Online:
            def __init__(self, p):
                self.p = p

            def translate(self, text):
                return self.p._translate_online(text) or ""

        return _Online(p)

    def start_mic(self):
        if not self.cfg.get("mic_assist"):
            return
        if self.mic_assist and self.mic_assist.running:
            return
        self.mic_assist = MicAssist(
            self.cfg, self.ui_q,
            lambda: self.pipeline.model if self.pipeline else None,
            self._mic_translate,
            self.overlay.tts_busy, self._audio_state)
        self.mic_assist.start()

    def stop_mic(self):
        if self.mic_assist:
            self.mic_assist.stop()
            self.mic_assist = None

    def stop_capture(self):
        if self.capture:
            self.capture.stop()
            self.capture = None
        try:
            self.overlay.btn_pause.setText("开始")
        except Exception:
            pass
        try:
            self.main._sync_capture_ui()
        except Exception:
            pass

    def toggle_pause(self):
        running = bool(self.capture and getattr(self.capture, "running", False))
        if not running and not self.paused:
            # 从未开始：第一次点就是开始。原来直接落进暂停分支，要点两次才真正开始。
            self.paused = False
            self.start_capture()
            self.overlay.set_status("收听中", "ok")
            self.overlay.btn_pause.setText("暂停")
            self.main.set_status("收听中", "ok")
            try:
                self.main._sync_capture_ui()
            except Exception:
                pass
            return
        if self.paused:
            self.paused = False
            self.start_capture()
            self.overlay.set_status("已恢复收听", "ok")
            self.overlay.btn_pause.setText("暂停")
            self.main.set_status("已恢复收听", "ok")
            self.main.btn_pause.setText("暂停")
        else:
            self.paused = True
            self.stop_capture()
            self.overlay.set_status("已暂停", "")
            self.overlay.btn_pause.setText("继续")
            self.main.set_status("已暂停", "")
            self.main.btn_pause.setText("继续")
        try:
            self.main._sync_capture_ui()
        except Exception:
            pass

    def restart_engine(self):
        self.stop_capture()
        if self.pipeline:
            self.pipeline.stop()
            self.pipeline = None
        while True:
            try:
                self.seg_q.get_nowait()
            except queue.Empty:
                break
        self.paused = False
        self.start_pipeline()

    def open_settings(self):
        build_settings(self)
        try:
            self.main.refresh_chips()
        except Exception:
            pass

    def list_loopback(self):
        try:
            import soundcard
            names = [str(m.name) for m in soundcard.all_microphones(include_loopback=True)
                     if "Loopback" in str(m.name)]
            spk = soundcard.default_speaker()
            if str(spk.name) not in names:
                names.insert(0, str(spk.name))
            return names
        except Exception as e:
            log("列设备失败：" + repr(e))
            return []

    def quit(self):
        try:
            self.main._remember_geometry()      # 退出前把窗口位置存下来
        except Exception:
            pass
        save_config(self.cfg)
        try:
            _flush_history_now()          # 退出前把内存里的记录落盘
        except Exception:
            pass
        try:
            backup_data()                 # 退出再存一份备份
        except Exception:
            pass
        try:
            if self._hotkey_id:
                import ctypes
                ctypes.windll.user32.UnregisterHotKey(None, self._hotkey_id)
        except Exception:
            pass
        try:
            self.overlay.stop_read()
        except Exception:
            pass
        self.stop_capture()
        if self.pipeline:
            self.pipeline.stop()
        self.overlay.stop()
        try:
            if self.main.tray:
                self.main.tray.hide()
        except Exception:
            pass
        os._exit(0)

    def run(self):
        self.start_pipeline()
        self._start_ipc()
        self.main.show()
        # 显示之后窗口才有真正的边框，这时再摆一次位置，才不会每启一次往下掉一个标题栏
        self.main._restore_geometry()
        self.main._start_geo_timer()
        if bool(self.cfg.get("pinned")):
            self.main.btn_pin.setChecked(True)      # 恢复上次置顶状态
        if self.cfg.get("show_overlay", True):
            self.overlay.show()
        try:
            tl = str(self.cfg.get("target_lang") or "en")
            self.overlay.preload_tts(tl)
            self.overlay.update_tts_ready(tl)
        except Exception:
            pass
        self.install_hotkey()
        self.overlay.set_interactive(False)      # 常态穿透，热键才接管鼠标
        self.overlay.app.exec()

    def _start_ipc(self):
        """第二个实例通过本地套接字让我把主窗口抬起来。"""
        try:
            from PySide6.QtNetwork import QLocalServer
            self.server = QLocalServer()
            QLocalServer.removeServer("OversubActivate-9F2C4B1E")
            if self.server.listen("OversubActivate-9F2C4B1E"):
                self.server.newConnection.connect(self._on_ipc)
        except Exception as e:
            log("本地套接字启动失败：" + repr(e))

    def _on_ipc(self):
        try:
            sock = self.server.nextPendingConnection()
            if sock:
                sock.readAll()
                sock.disconnectFromServer()
            self.main.show()
            if self.cfg.get("show_overlay", True):
                self.overlay.show()
        except Exception as e:
            log("接收激活请求失败：" + repr(e))


def activate_existing():
    """已有实例在跑：悄悄让它把窗口抬起来，然后本次启动直接退出，不弹任何提示。"""
    try:
        from PySide6.QtNetwork import QLocalSocket
        s = QLocalSocket()
        s.connectToServer("OversubActivate-9F2C4B1E")
        if s.waitForConnected(1500):
            s.write(b"show")
            s.flush()
            s.waitForBytesWritten(800)
            s.disconnectFromServer()
            log("已有实例在运行，已唤醒它的窗口，本次启动退出")
            return True
    except Exception as e:
        log("激活已有实例失败：" + repr(e))
    return False


def check_deps():
    missing = []
    for mod, pipname in (("numpy", "numpy"), ("soundcard", "soundcard"),
                         ("faster_whisper", "faster-whisper"),
                         ("argostranslate", "argostranslate"),
                         ("PySide6", "pyside6")):
        try:
            __import__(mod)
        except Exception:
            missing.append(pipname)
    return missing


_MUTEX_HANDLE = None          # 单实例锁，进程活着就一直持有


def acquire_single_instance():
    """返回 True 表示本次是唯一实例；False 表示已经有一个在跑了。"""
    global _MUTEX_HANDLE
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
        _MUTEX_HANDLE = k32.CreateMutexW(None, False, "Local\\OversubSubtitle-9F2C4B1E")
        if not _MUTEX_HANDLE:
            return True                       # 拿不到锁就放行，别把程序堵死
        return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
    except Exception as e:
        log("单实例检查失败：" + repr(e))
        return True


def main():
    if "--selftest-words" in sys.argv:
        # 打包后的自检：不开界面，只验证离线词库能不能用
        cfg = load_config()
        book = WordBook(cfg)
        for w in ("opportunity", "government", "mid-level"):
            log("自检词义 %s => %s" % (w, book.lookup(w, "")))
        log("自检完成")
        return

    missing = check_deps()
    if missing:
        msg = ("这几种库还没装：\n\n" + "、".join(missing) +
               "\n\n在命令行里跑一次（只装一次，之后离线可用）：\n\n"
               "python -m pip install faster-whisper argostranslate soundcard pyside6")
        log(msg)
        try:
            import tkinter as tk
            from tkinter import messagebox
            r = tk.Tk(); r.withdraw()
            messagebox.showerror("缺少运行库", msg)
            r.destroy()
        except Exception:
            print(msg)
        return

    cfg = load_config()
    if not acquire_single_instance():
        # 已经有实例：把它的窗口抬起来，然后静默退出，不弹任何提示
        activate_existing()
        os._exit(0)
    housekeeping(int(cfg.get("keep_days") or 0))     # 压缩旧记录、按需清理
    App(cfg).run()


if __name__ == "__main__":
    main()


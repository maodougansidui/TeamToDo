#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TeamToDo v3.2 Server
====================
内网任务协同系统后端服务。

运行环境要求
------------
* 仅使用 Python 标准库，无需 pip 安装任何依赖
* 支持 Python 3.6 ~ 3.12

启动方式
--------
    python server.py                        # 默认 8765 端口
    python server.py --port 9000            # 指定端口
    python server.py --db "\\\\srv\\share\\tasks.db"   # 共享文件夹数据库
    python server.py --no-browser           # 不自动打开浏览器（已默认关闭）
    python server.py --host 127.0.0.1       # 仅本机访问

配置优先级：命令行参数 > 同目录 config.json > 环境变量 > 默认值
"""

import argparse
import base64
import datetime
import hashlib
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from functools import wraps
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

IS_FROZEN = getattr(sys, "frozen", False)

# Excel 导入导出（纯标准库实现，位于同目录）
excelutil = None
if not IS_FROZEN:
    # 开发/源码模式：从 backend/ 目录导入
    try:
        import excelutil
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        try:
            import excelutil
        except ImportError:
            excelutil = None
else:
    # PyInstaller 打包模式：excelutil 放在 EXE 同目录，需要显式搜索
    _exe_dir = os.path.dirname(os.path.abspath(sys.executable)) if sys.executable else ""
    _candidates = []
    if _exe_dir:
        _candidates.append(_exe_dir)
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        _candidates.append(meipass)
    for _p in _candidates:
        sys.path.insert(0, _p)
        try:
            import excelutil
            break
        except ImportError:
            sys.path.remove(_p)
    else:
        excelutil = None

try:                                  # Python 3.7+
    from http.server import ThreadingHTTPServer as _BaseServer
except ImportError:                   # Python 3.6 兼容
    import socketserver
    class _BaseServer(socketserver.ThreadingMixIn, HTTPServer):
        daemon_threads = True
        allow_reuse_address = True


# ============================================================
#  基础常量
# ============================================================
APP_NAME     = "TeamToDo"
VERSION      = "3.4.0"
DEFAULT_PORT = 8765
PORT_TRIES   = 30
SESSION_TTL  = 8 * 3600
DB_TIMEOUT   = 20

# v3.2 换肤：可选主题（key 落库，颜色由前端 CSS 变量映射）
DEFAULT_THEMES = [
    {"key": "blue",   "name": "经典蓝", "primary": "#2563EB", "dark": False},
    {"key": "teal",   "name": "科技青", "primary": "#0D9488", "dark": False},
    {"key": "green",  "name": "生机绿", "primary": "#16A34A", "dark": False},
    {"key": "purple", "name": "典雅紫", "primary": "#7C3AED", "dark": False},
    {"key": "orange", "name": "活力橙", "primary": "#EA580C", "dark": False},
    {"key": "dark",   "name": "暗夜蓝", "primary": "#3B82F6", "dark": True},
]

IS_FROZEN = getattr(sys, "frozen", False)


# ============================================================
#  控制台输出（兼容 Win7 GBK 控制台 / 无控制台环境）
# ============================================================
def _init_stdio():
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(errors="replace", line_buffering=True)
        except Exception:
            try:
                import codecs
                enc = getattr(stream, "encoding", None) or "utf-8"
                setattr(sys, stream_name,
                        codecs.getwriter(enc)(getattr(stream, "buffer"),
                                              errors="replace"))
            except Exception:
                pass

_init_stdio()

_LOG_FILE = None
_LOG_LOCK = threading.Lock()


def log(msg, to_file=True):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        print(line)
    except Exception:
        pass
    if to_file and _LOG_FILE:
        try:
            with _LOG_LOCK:
                with open(_LOG_FILE, "a", encoding="utf-8", errors="replace") as f:
                    f.write("%s %s\n" % (time.strftime("%Y-%m-%d"), line))
        except Exception:
            pass


# ============================================================
#  路径解析
# ============================================================
def exe_dir():
    if IS_FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def bundle_dir():
    p = getattr(sys, "_MEIPASS", None)
    if IS_FROZEN and p:
        return p
    return exe_dir()


def find_subdir(name):
    ed, bd = exe_dir(), bundle_dir()
    cands = [
        os.path.join(ed, name),
        os.path.join(bd, name),
        os.path.join(os.path.dirname(ed), name),
    ]
    seen = set()
    for c in cands:
        c = os.path.normpath(c)
        if c in seen:
            continue
        seen.add(c)
        if os.path.isdir(c):
            return c
    return os.path.normpath(cands[0])


def _writable(path):
    try:
        if not os.path.isdir(path):
            os.makedirs(path)
    except Exception:
        return False
    try:
        return os.access(path, os.W_OK)
    except Exception:
        return False


def data_dir():
    candidates = [exe_dir()]
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~")
    if base:
        candidates.append(os.path.join(base, APP_NAME))
    candidates.append(os.path.join(os.path.expanduser("~"), "." + APP_NAME.lower()))
    for p in candidates:
        if _writable(p):
            return p
    return exe_dir()


STATIC_DIR = find_subdir("static")
DB_DIR     = find_subdir("db")


# ============================================================
#  配置体系
# ============================================================
DEFAULT_HOST = "127.0.0.1"
LAN_HOST     = "0.0.0.0"

DEFAULT_CONFIG = {
    "app": {
        "title":    "TeamToDo管理平台",
        "subtitle": "内部任务协同管理平台",
    },
    "server": {
        "host":         DEFAULT_HOST,
        "port":         DEFAULT_PORT,
        "open_browser": False,          # 默认关闭自动打开浏览器
        "lan_access":   False,
    },
    "database": {
        "folder":   "",
        "file":     "tasks.db",
        "path":     "",
        "mode":     "auto",
        "timeout":  DB_TIMEOUT,
        "journal_mode": "",
        "backup_dir": "",
        "keep_backups": 10,
        "allow_local_fallback": False,
    },
    "security": {
        "allow_register": True,
    },
    "update": {
        # 自动更新：从共享文件夹读取更新包
        "enabled":     False,   # 总开关
        "folder":      "",      # 共享文件夹里存放更新包的目录，如 //server/share/TeamToDoUpdate
        "manifest":    "manifest.json",
        "auto_check":  True,    # 启动/前端轮询时自动检测
        "check_interval": 300,  # 前端轮询间隔（秒）
        "auto_install": False,  # True=发现更新直接装；False=提示用户确认
    },
}

CONFIG_PATH = None
APP_TITLE    = DEFAULT_CONFIG["app"]["title"]
APP_SUBTITLE = DEFAULT_CONFIG["app"]["subtitle"]
SERVER_HOST  = DEFAULT_HOST
LAN_ACCESS   = False
ALLOW_REGISTER = True

# 自动更新
UPDATE_ENABLED      = False
UPDATE_FOLDER       = ""
UPDATE_MANIFEST     = "manifest.json"
UPDATE_INTERVAL     = 300
UPDATE_AUTO_INSTALL = False
UPDATE_STATE = {"last_check": 0, "available": None, "error": "", "applied": False}


def _blank_config():
    return dict((s, dict(d)) for s, d in DEFAULT_CONFIG.items())


def _merge_into(cfg, raw):
    for section, defaults in DEFAULT_CONFIG.items():
        user = raw.get(section)
        if isinstance(user, dict):
            cfg.setdefault(section, dict(defaults))
            cfg[section].update(dict((k, v) for k, v in user.items()
                                     if not k.startswith("_")))
    return cfg


def _apply_legacy(cfg, raw):
    if "port" in raw:
        cfg["server"]["port"] = raw["port"]
    if "host" in raw:
        cfg["server"]["host"] = raw["host"]
        if str(raw["host"]) not in ("127.0.0.1", "localhost", "::1"):
            cfg["server"]["lan_access"] = True
    if "db" in raw:
        cfg["database"]["path"] = raw["db"]
    if "no_browser" in raw:
        cfg["server"]["open_browser"] = not bool(raw["no_browser"])
    return cfg


def _apply_env(cfg):
    env_map = (
        ("TEAMTODO_DB_FOLDER", ("database", "folder")),
        ("TEAMTODO_DB_FILE",   ("database", "file")),
        ("TEAMTODO_DB",        ("database", "path")),
        ("TEAMTODO_APP_TITLE", ("app", "title")),
        ("TEAMTODO_UPDATE_ENABLED",      ("update", "enabled")),
        ("TEAMTODO_UPDATE_FOLDER",       ("update", "folder")),
        ("TEAMTODO_UPDATE_MANIFEST",     ("update", "manifest")),
        ("TEAMTODO_UPDATE_AUTO_INSTALL", ("update", "auto_install")),
    )
    bool_keys = {("update", "enabled"), ("update", "auto_install"),
                 ("update", "auto_check")}
    for var, (sec, key) in env_map:
        v = os.environ.get(var)
        if v is None or v == "":
            continue
        if (sec, key) in bool_keys:
            cfg[sec][key] = str(v).strip().lower() in ("1", "true", "yes", "on")
        else:
            cfg[sec][key] = v
    if os.environ.get("TEAMTODO_PORT"):
        try:
            cfg["server"]["port"] = int(os.environ["TEAMTODO_PORT"])
        except ValueError:
            pass
    if os.environ.get("TEAMTODO_HOST"):
        cfg["server"]["host"] = os.environ["TEAMTODO_HOST"]
    return cfg


def find_config_file():
    cands = [
        os.path.join(exe_dir(), "config.json"),
        os.path.join(os.path.dirname(exe_dir()), "config.json"),
    ]
    seen = set()
    for c in cands:
        c = os.path.normpath(c)
        if c in seen:
            continue
        seen.add(c)
        if os.path.isfile(c):
            return c
    return None


def _find_local_override():
    if not CONFIG_PATH:
        return None
    p = os.path.join(os.path.dirname(CONFIG_PATH), "config.local.json")
    return p if os.path.isfile(p) else None


def _read_json(path):
    for enc in ("utf-8-sig", "gb18030"):
        try:
            with open(path, "r", encoding=enc) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except UnicodeDecodeError:
            continue
        except Exception as e:
            log("[WARN] %s 无法解析，已忽略: %s" % (os.path.basename(path), e))
            return {}
    log("[WARN] %s 编码无法识别，已忽略" % path)
    return {}


def _merged_defaults(raw):
    cfg = _blank_config()
    cfg = _merge_into(cfg, raw)
    local = _find_local_override()
    if local:
        log("[INFO] 已加载本机覆盖配置: %s" % local)
        cfg = _merge_into(cfg, _read_json(local))
    return cfg


def load_config():
    global CONFIG_PATH
    path = find_config_file()
    raw = {}
    if path:
        CONFIG_PATH = path
        raw = _read_json(path)
        if not raw and os.path.getsize(path) > 2:
            log("[WARN] 提示：JSON 中的路径请用正斜杠，例如 //10.0.0.1/share/db")
    cfg = _merged_defaults(raw)
    cfg = _apply_legacy(cfg, raw)
    cfg = _apply_env(cfg)
    return cfg


def resolve_db_path(dbcfg):
    if dbcfg.get("path"):
        path = str(dbcfg["path"])
    else:
        folder = str(dbcfg.get("folder") or "").strip()
        fname = str(dbcfg.get("file") or "tasks.db").strip() or "tasks.db"
        if folder:
            path = os.path.join(folder, fname)
        else:
            path = os.path.join(data_dir(), fname)
    path = os.path.abspath(os.path.expanduser(path.replace("/", os.sep)))
    mode = str(dbcfg.get("mode") or "auto").lower()
    if mode == "auto":
        mode = "shared" if is_network_path(path) else "local"
    return path, mode


def parse_args(cfg):
    p = argparse.ArgumentParser(add_help=True,
                                description="%s Server v%s" % (APP_NAME, VERSION))
    p.add_argument("--port", type=int, default=int(cfg["server"]["port"]))
    p.add_argument("--host", default=cfg["server"]["host"])
    p.add_argument("--lan", action="store_true", default=bool(cfg["server"]["lan_access"]),
                   help="允许局域网其它电脑访问（默认只监听本机 127.0.0.1）")
    p.add_argument("--db", default=None,
                   help="数据库完整路径（优先级最高）")
    p.add_argument("--db-folder", default=None,
                   help="数据库所在目录（共享文件夹路径）")
    p.add_argument("--no-browser", action="store_true",
                   default=not bool(cfg["server"]["open_browser"]),
                   help="不自动打开浏览器")
    p.add_argument("--share", action="store_true", help="强制共享盘模式")
    p.add_argument("--local", action="store_true", help="强制本地单机模式")
    p.add_argument("--show-config", action="store_true", help="打印当前配置后退出")
    return p.parse_args()


# ============================================================
#  网络工具
# ============================================================
def port_free(port, host="0.0.0.0"):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return False
    except Exception:
        pass
    finally:
        try:
            s.close()
        except Exception:
            pass

    s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s2.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            s2.close()
        except Exception:
            pass


def pick_port(start, host):
    for p in range(start, start + PORT_TRIES):
        if port_free(p, host):
            return p
    return None


def lan_ip():
    for target in (("10.255.255.255", 1), ("192.168.255.255", 1), ("172.31.255.255", 1)):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(target)
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            continue
        finally:
            try:
                s.close()
            except Exception:
                pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "127.0.0.1"


def is_network_path(path):
    if not path:
        return False
    p = path.replace("/", os.sep)
    if p.startswith(os.sep + os.sep) or p.startswith("\\\\"):
        return True
    if os.name == "nt" and len(p) >= 2 and p[1] == ":":
        try:
            import ctypes
            drive = ctypes.c_wchar_p(p[:2] + "\\")
            return ctypes.windll.kernel32.GetDriveTypeW(drive) == 4
        except Exception:
            return False
    return False


# ============================================================
#  数据库
# ============================================================
DB_PATH    = ""
NETWORK_DB = False
DB_READY   = False
DB_MODE    = "local"
DB_TIMEOUT_SEC = DB_TIMEOUT
DB_JOURNAL = ""
DB_LAST_ERROR = ""


def setup_db(cfg, args):
    global DB_PATH, NETWORK_DB, DB_MODE, DB_TIMEOUT_SEC, DB_JOURNAL

    dbcfg = dict(cfg["database"])
    if getattr(args, "db", None):
        dbcfg["path"] = args.db
        dbcfg["folder"] = ""
    elif getattr(args, "db_folder", None):
        dbcfg["folder"] = args.db_folder
        dbcfg["path"] = ""
    if getattr(args, "share", False):
        dbcfg["mode"] = "shared"
    if getattr(args, "local", False):
        dbcfg["mode"] = "local"

    DB_PATH, DB_MODE = resolve_db_path(dbcfg)
    NETWORK_DB = (DB_MODE == "shared")
    try:
        DB_TIMEOUT_SEC = max(5, int(dbcfg.get("timeout") or DB_TIMEOUT))
    except (TypeError, ValueError):
        DB_TIMEOUT_SEC = DB_TIMEOUT
    DB_JOURNAL = str(dbcfg.get("journal_mode") or "").strip().upper()
    if not DB_JOURNAL:
        DB_JOURNAL = "TRUNCATE" if NETWORK_DB else "WAL"


def backup_db(cfg):
    dest_dir = str(cfg["database"].get("backup_dir") or "").strip()
    if not dest_dir or not os.path.isfile(DB_PATH):
        return None
    try:
        if not os.path.isdir(dest_dir):
            os.makedirs(dest_dir)
        base = os.path.splitext(os.path.basename(DB_PATH))[0]
        name = "%s_%s.db" % (base, time.strftime("%Y%m%d_%H%M%S"))
        dest = os.path.join(dest_dir, name)
        src = get_db()
        try:
            dst = sqlite3.connect(dest)
            try:
                if hasattr(src, "backup"):
                    src.backup(dst)
                else:
                    dst.close()
                    shutil.copy2(DB_PATH, dest)
                    dst = sqlite3.connect(dest)
            finally:
                try:
                    dst.close()
                except Exception:
                    pass
        finally:
            src.close()

        keep = cfg["database"].get("keep_backups") or 10
        try:
            keep = max(1, int(keep))
        except (TypeError, ValueError):
            keep = 10
        files = sorted(
            [f for f in os.listdir(dest_dir) if f.startswith(base + "_") and f.endswith(".db")],
            reverse=True)
        for old in files[keep:]:
            try:
                os.remove(os.path.join(dest_dir, old))
            except Exception:
                pass
        return dest
    except Exception as e:
        log("[WARN] 数据库备份失败: %s" % e)
        return None


def check_db_location():
    d = os.path.dirname(DB_PATH) or "."
    if not os.path.isdir(d):
        try:
            os.makedirs(d)
        except Exception:
            return False, "目录不存在且无法创建: %s" % d
    if not os.path.isdir(d):
        return False, "目录不存在: %s" % d
    if not os.access(d, os.W_OK):
        return False, "没有写入权限: %s" % d

    probe = os.path.join(d, ".teamtodo_probe_%d" % os.getpid())
    try:
        with open(probe, "w") as f:
            f.write("1")
    except Exception as e:
        return False, "无法在该目录创建文件: %s (%s)" % (d, e)
    try:
        os.remove(probe)
    except Exception:
        pass
    return True, ""


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SEC, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=%d" % (DB_TIMEOUT_SEC * 1000))
        conn.execute("PRAGMA temp_store=MEMORY")
        if NETWORK_DB:
            conn.execute("PRAGMA journal_mode=%s" % DB_JOURNAL)
            conn.execute("PRAGMA synchronous=FULL")
        else:
            try:
                mode = conn.execute("PRAGMA journal_mode=%s" % DB_JOURNAL).fetchone()[0]
                if str(mode).lower() not in (DB_JOURNAL.lower(), "wal", "truncate", "delete"):
                    conn.execute("PRAGMA journal_mode=DELETE")
            except Exception:
                try:
                    conn.execute("PRAGMA journal_mode=DELETE")
                except Exception:
                    pass
            conn.execute("PRAGMA synchronous=NORMAL")
    except Exception as e:
        log("[WARN] 数据库 PRAGMA 设置失败: %s" % e)
    return conn


# ============================================================
#  数据库增量迁移（兼容旧数据库自动升级）
# ============================================================
def db_migrate():
    """检测并补充旧数据库中缺失的表和字段，避免 SQL 错误"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SEC, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=%d" % (DB_TIMEOUT_SEC * 1000))
    except Exception:
        return

    def col_exists(table, col):
        try:
            r = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
            return any(row[1] == col for row in r)
        except Exception:
            return False

    def table_exists(table):
        try:
            r = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            return r is not None
        except Exception:
            return False

    try:
        # 全新数据库：核心表还没建（schema.sql 稍后才执行），本次迁移无意义。
        # 直接跳过，等 db_init() 建表后再跑一次，避免刷出无用的报错日志。
        if not table_exists("tasks"):
            log("[MIGRATE] 全新数据库，核心表尚未创建，跳过本次迁移")
            return

        for col, defn in [
            ("task_no",     "TEXT"),
            ("task_type",   "TEXT DEFAULT 'NORMAL'"),
            ("category",    "TEXT DEFAULT '日常任务'"),
            ("source",      "TEXT"),
            ("dept_id",     "INTEGER"),
            ("cycle_rule",  "TEXT"),
            ("next_trigger","TEXT"),
            ("last_trigger","TEXT"),
            ("parent_id",   "INTEGER"),
        ]:
            if not col_exists("tasks", col):
                conn.execute("ALTER TABLE tasks ADD COLUMN %s %s" % (col, defn))
                log("[MIGRATE] tasks.%s 已补充" % col)

        # --- users 表新增字段 ---
        if not col_exists("users", "role_id"):
            conn.execute("ALTER TABLE users ADD COLUMN role_id INTEGER")
            log("[MIGRATE] users.role_id 已补充")

        # --- task_comments 表新增字段 ---
        if not col_exists("task_comments", "timestamp"):
            conn.execute(
                "ALTER TABLE task_comments ADD COLUMN timestamp TEXT DEFAULT (datetime('now','localtime'))")
            log("[MIGRATE] task_comments.timestamp 已补充")

        # --- task_followers 表（兼容旧数据库） ---
        if not table_exists("task_followers"):
            conn.execute("""CREATE TABLE IF NOT EXISTS task_followers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(task_id, user_id))""")
            log("[MIGRATE] task_followers 表已创建")

        # --- roles 表 ---
        if not table_exists("roles"):
            conn.execute("""CREATE TABLE IF NOT EXISTS roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                is_system INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now','localtime')))""")
            log("[MIGRATE] roles 表已创建")

        # --- role_permissions 表 ---
        if not table_exists("role_permissions"):
            conn.execute("""CREATE TABLE IF NOT EXISTS role_permissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role_id INTEGER REFERENCES roles(id) ON DELETE CASCADE,
                permission TEXT NOT NULL,
                UNIQUE(role_id, permission))""")
            log("[MIGRATE] role_permissions 表已创建")

        # --- task_relations 表 ---
        if not table_exists("task_relations"):
            conn.execute("""CREATE TABLE IF NOT EXISTS task_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
                related_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
                relation_type TEXT NOT NULL,
                condition TEXT DEFAULT 'COMPLETED',
                notify_users TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now','localtime')))""")
            log("[MIGRATE] task_relations 表已创建")

        # --- 索引 ---
        for idx, sql in [
            ("idx_tasks_dept",      "CREATE INDEX IF NOT EXISTS idx_tasks_dept      ON tasks(dept_id)"),
            ("idx_tasks_due",       "CREATE INDEX IF NOT EXISTS idx_tasks_due       ON tasks(due_date)"),
            ("idx_tasks_parent",    "CREATE INDEX IF NOT EXISTS idx_tasks_parent    ON tasks(parent_id)"),
            ("idx_tasks_status",    "CREATE INDEX IF NOT EXISTS idx_tasks_status    ON tasks(status)"),
            ("idx_tasks_created",   "CREATE INDEX IF NOT EXISTS idx_tasks_created   ON tasks(created_at)"),
            ("idx_tasks_completed", "CREATE INDEX IF NOT EXISTS idx_tasks_completed ON tasks(completed_at)"),
            ("idx_tasks_assignee",  "CREATE INDEX IF NOT EXISTS idx_tasks_assignee  ON tasks(assignee_id)"),
            ("idx_tasks_createdby", "CREATE INDEX IF NOT EXISTS idx_tasks_createdby ON tasks(created_by)"),
        ]:
            try:
                conn.execute(sql)
            except Exception:
                pass

        # --- v3.2：换肤 / 个性化设置（独立容错，避免旧库缺表中断迁移）---
        try:
            if not table_exists("user_settings"):
                conn.execute("""CREATE TABLE IF NOT EXISTS user_settings (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    theme TEXT DEFAULT 'blue',
                    sidebar_collapsed INTEGER DEFAULT 0,
                    density TEXT DEFAULT 'medium',
                    settings TEXT,
                    updated_at TEXT DEFAULT (datetime('now','localtime')))""")
                log("[MIGRATE] user_settings 表已创建")
        except Exception as e:
            log("[MIGRATE] user_settings 表创建失败: %s" % e)

        try:
            if table_exists("users") and not col_exists("users", "theme"):
                conn.execute("ALTER TABLE users ADD COLUMN theme TEXT DEFAULT 'blue'")
                log("[MIGRATE] users.theme 已补充")
        except Exception as e:
            log("[MIGRATE] users.theme 补充失败: %s" % e)

        # --- task_comments 表新增字段（v3.2.5 部门确认）---
        try:
            if not col_exists("task_comments", "confirmed_dept"):
                conn.execute("ALTER TABLE task_comments ADD COLUMN confirmed_dept INTEGER DEFAULT 0")
                log("[MIGRATE] task_comments.confirmed_dept 已补充")
        except Exception as e:
            log("[MIGRATE] task_comments.confirmed_dept 补充失败: %s" % e)
        try:
            if not col_exists("task_comments", "confirmed_by"):
                conn.execute("ALTER TABLE task_comments ADD COLUMN confirmed_by INTEGER")
                log("[MIGRATE] task_comments.confirmed_by 已补充")
        except Exception as e:
            log("[MIGRATE] task_comments.confirmed_by 补充失败: %s" % e)
        try:
            if not col_exists("task_comments", "confirmed_at"):
                conn.execute("ALTER TABLE task_comments ADD COLUMN confirmed_at TEXT")
                log("[MIGRATE] task_comments.confirmed_at 已补充")
        except Exception as e:
            log("[MIGRATE] task_comments.confirmed_at 补充失败: %s" % e)

        # --- pending_confirmations 表（v3.2.5，v3.3 升级唯一约束）---
        try:
            need_rebuild = False
            if table_exists("pending_confirmations"):
                # 旧库的 UNIQUE(comment_id) 会导致同一反馈只能通知一个管理员，需重建
                try:
                    sql_txt = conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table'"
                        " AND name='pending_confirmations'").fetchone()
                    if sql_txt and "dept_head_uid" not in (sql_txt[0] or ""):
                        need_rebuild = True
                    elif sql_txt and "UNIQUE(comment_id, dept_head_uid)" not in \
                            (sql_txt[0] or "").replace(" ", ""):
                        need_rebuild = True
                except Exception:
                    pass
                if need_rebuild:
                    conn.execute("ALTER TABLE pending_confirmations RENAME TO _pc_old")
                    log("[MIGRATE] pending_confirmations 旧表已暂存，准备重建")
            if not table_exists("pending_confirmations"):
                conn.execute("""CREATE TABLE IF NOT EXISTS pending_confirmations (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    comment_id    INTEGER REFERENCES task_comments(id) ON DELETE CASCADE,
                    dept_id       INTEGER NOT NULL,
                    dept_head_uid INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    task_id       INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
                    task_title    TEXT,
                    sender_name   TEXT,
                    created_at    TEXT    DEFAULT (datetime('now','localtime')),
                    UNIQUE(comment_id, dept_head_uid))""")
                if need_rebuild:
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO pending_confirmations"
                            "(id,comment_id,dept_id,dept_head_uid,task_id,task_title,"
                            " sender_name,created_at)"
                            " SELECT id,comment_id,dept_id,dept_head_uid,task_id,"
                            " task_title,sender_name,created_at FROM _pc_old")
                        conn.execute("DROP TABLE _pc_old")
                        log("[MIGRATE] pending_confirmations 数据已迁移并支持多管理员通知")
                    except Exception as e2:
                        log("[MIGRATE] pending_confirmations 数据迁移失败: %s" % e2)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_dept ON pending_confirmations(dept_head_uid)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_task ON pending_confirmations(task_id)")
                log("[MIGRATE] pending_confirmations 表已创建")
        except Exception as e:
            log("[MIGRATE] pending_confirmations 表创建失败: %s" % e)

        # --- v3.2.5 台账收集表 ---
        try:
            if not table_exists("ledger_templates"):
                conn.execute("""CREATE TABLE IF NOT EXISTS ledger_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT,
                    columns TEXT NOT NULL,
                    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at TEXT DEFAULT (datetime('now','localtime')),
                    updated_at TEXT DEFAULT (datetime('now','localtime')))""")
                log("[MIGRATE] ledger_templates 表已创建")
            if not table_exists("ledger_submissions"):
                conn.execute("""CREATE TABLE IF NOT EXISTS ledger_submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    template_id INTEGER REFERENCES ledger_templates(id) ON DELETE CASCADE,
                    submitter TEXT NOT NULL,
                    data TEXT NOT NULL,
                    submitted_at TEXT DEFAULT (datetime('now','localtime')))""")
                log("[MIGRATE] ledger_submissions 表已创建")
        except Exception as e:
            log("[MIGRATE] 台账收集表创建失败: %s" % e)

        # --- v3.3：任务多部门 / 多责任人关联表 ---
        try:
            if not table_exists("task_departments"):
                conn.execute("""CREATE TABLE IF NOT EXISTS task_departments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
                    dept_id INTEGER REFERENCES organizations(id) ON DELETE CASCADE,
                    created_at TEXT DEFAULT (datetime('now','localtime')),
                    UNIQUE(task_id, dept_id))""")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_td_task ON task_departments(task_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_td_dept ON task_departments(dept_id)")
                log("[MIGRATE] task_departments 表已创建")
        except Exception as e:
            log("[MIGRATE] task_departments 表创建失败: %s" % e)

        try:
            if not table_exists("task_assignees"):
                conn.execute("""CREATE TABLE IF NOT EXISTS task_assignees (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    created_at TEXT DEFAULT (datetime('now','localtime')),
                    UNIQUE(task_id, user_id))""")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ta_task ON task_assignees(task_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ta_user ON task_assignees(user_id)")
                log("[MIGRATE] task_assignees 表已创建")
        except Exception as e:
            log("[MIGRATE] task_assignees 表创建失败: %s" % e)

        # --- v3.3：把存量单值数据回填到关联表（幂等，可重复执行）---
        try:
            if table_exists("task_departments"):
                conn.execute(
                    "INSERT OR IGNORE INTO task_departments(task_id, dept_id)"
                    " SELECT id, dept_id FROM tasks WHERE dept_id IS NOT NULL AND dept_id > 0")
        except Exception as e:
            log("[MIGRATE] task_departments 回填失败: %s" % e)
        try:
            if table_exists("task_assignees"):
                conn.execute(
                    "INSERT OR IGNORE INTO task_assignees(task_id, user_id)"
                    " SELECT id, assignee_id FROM tasks WHERE assignee_id IS NOT NULL AND assignee_id > 0")
        except Exception as e:
            log("[MIGRATE] task_assignees 回填失败: %s" % e)

        log("[MIGRATE] 数据库迁移完成")
    except Exception as e:
        log("[MIGRATE] 迁移过程出错（可能无害）: %s" % e)
    finally:
        conn.close()


def db_init():
    schema = None
    for cand in (os.path.join(DB_DIR, "schema.sql"),
                 os.path.join(exe_dir(), "schema.sql"),
                 os.path.join(bundle_dir(), "schema.sql"),
                 os.path.join(bundle_dir(), "db", "schema.sql")):
        if os.path.isfile(cand):
            schema = cand
            break
    if not schema:
        log("[WARN] 未找到 schema.sql，跳过自动建表")
        return False

    try:
        sql = open(schema, "r", encoding="utf-8-sig").read()
    except Exception as e:
        log("[ERROR] schema.sql 读取失败: %s" % e)
        return False

    last_err = None
    for attempt in range(10):
        conn = None
        try:
            conn = get_db()
            conn.executescript(sql)
            n_user = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            n_org  = conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0]
            n_task = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            conn.close()
            log("[OK] 数据库就绪: %s" % DB_PATH)
            log("     用户 %s 个 / 组织 %s 个 / 任务 %s 条" % (n_user, n_org, n_task))
            # v3.4：补齐六个岗位预设（幂等，不影响已有自定义权限）
            seed_role_presets()
            return True
        except sqlite3.OperationalError as e:
            last_err = e
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            time.sleep(0.6)
            continue
        except Exception as e:
            last_err = e
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            break

    log("[ERROR] 数据库初始化失败: %s" % last_err)
    log("        schema=%s" % schema)
    return False


# ============================================================
#  会话
# ============================================================
_sessions = {}
_SESSION_LOCK = threading.Lock()


def session_create(user_row):
    token = secrets.token_hex(24)
    with _SESSION_LOCK:
        _sessions[token] = {
            "user_id":      user_row["id"],
            "username":     user_row["username"],
            "display_name": user_row["display_name"] if "display_name" in user_row.keys() else user_row["username"],
            "role":         user_row["role"],
            "org_id":       user_row["org_id"] if "org_id" in user_row.keys() else None,
            "role_id":      user_row["role_id"] if "role_id" in user_row.keys() else None,
            "expire_at":    time.time() + SESSION_TTL,
        }
    return token


def session_get(token):
    with _SESSION_LOCK:
        s = _sessions.get(token)
        if not s:
            return None
        if s["expire_at"] < time.time():
            _sessions.pop(token, None)
            return None
        return s


def session_delete(token):
    with _SESSION_LOCK:
        _sessions.pop(token, None)


def session_sweep():
    with _SESSION_LOCK:
        now = time.time()
        for k in [k for k, v in _sessions.items() if v["expire_at"] < now]:
            _sessions.pop(k, None)


# ============================================================
#  权限系统
# ============================================================
# 权限项（唯一真源，供后端校验 + 前端渲染，避免前后端两份定义漂移）
# group/label/desc 用于「岗位权限」配置界面分组展示，解决"权限分配比较乱"的问题。
_PERMISSION_META = [
    # ---- 任务权限 ----
    {"group": "任务权限", "key": "task.view_all", "label": "查看本部门及下级全部任务",
     "desc": "可见范围 = 本人所在部门 + 全部下级部门的任务（处室/科室负责人、班组长）"},
    {"group": "任务权限", "key": "task.view_org", "label": "查看本部门任务",
     "desc": "仅可见本人所在部门的任务，不含下级部门"},
    {"group": "任务权限", "key": "task.create", "label": "创建任务",
     "desc": "可新建任务，并分发给本部门的直接下级部门"},
    {"group": "任务权限", "key": "task.edit", "label": "编辑任务"},
    {"group": "任务权限", "key": "task.delete", "label": "删除任务"},
    {"group": "任务权限", "key": "task.assign", "label": "分发 / 指派任务",
     "desc": "可分发给直接下级部门，或指派本部门人员；也用于把上级下发的任务继续向下分配"},
    {"group": "任务权限", "key": "task.claim", "label": "认领部门待认领任务",
     "desc": "可认领本部门任务池中的待认领任务，并继续向下分发"},
    {"group": "任务权限", "key": "task.feedback", "label": "反馈任务进展",
     "desc": "可对任务发表进展反馈，反馈后自动通知上级管理员待确认"},
    # ---- 人员管理 ----
    {"group": "人员管理", "key": "user.manage", "label": "管理人员"},
    {"group": "人员管理", "key": "user.create", "label": "添加人员"},
    {"group": "人员管理", "key": "user.edit", "label": "编辑人员"},
    {"group": "人员管理", "key": "user.disable", "label": "删除 / 停用人员"},
    # ---- 组织管理 ----
    {"group": "组织管理", "key": "org.manage", "label": "管理组织架构"},
    {"group": "组织管理", "key": "org.create", "label": "创建部门"},
    {"group": "组织管理", "key": "org.edit", "label": "编辑部门"},
    {"group": "组织管理", "key": "org.delete", "label": "删除部门"},
    # ---- 岗位管理 ----
    {"group": "岗位管理", "key": "role.manage", "label": "管理岗位"},
    {"group": "岗位管理", "key": "role.create", "label": "创建岗位"},
    {"group": "岗位管理", "key": "role.edit", "label": "编辑岗位"},
    {"group": "岗位管理", "key": "role.delete", "label": "删除岗位"},
    # ---- 其它 ----
    {"group": "其它", "key": "report.view", "label": "查看报表 / 数据大屏"},
    {"group": "其它", "key": "settings.manage", "label": "系统设置"},
]

_ALL_PERMISSIONS = [p["key"] for p in _PERMISSION_META]


def _id_list(raw):
    """把 GROUP_CONCAT 的 "1,2,3" 或列表统一转成 int 列表

    v3.4 修复：/api/tasks 列表接口此前直接返回 GROUP_CONCAT 字符串，
    前端把它当数组用（.forEach / .length）导致
    "dIds.forEach is not a function"，编辑任务表单直接报错。
    """
    if raw is None:
        return []
    out = []
    if isinstance(raw, (list, tuple, set)):
        it = raw
    else:
        it = str(raw).split(",")
    for x in it:
        s = str(x).strip()
        if s.isdigit():
            n = int(s)
            if n not in out:
                out.append(n)
    return out

# 未分配岗位（role_id 为空）的普通用户基础权限，保持历史行为不变
_USER_PERMISSIONS = ["task.create", "task.edit", "task.assign", "task.claim",
                     "task.feedback", "report.view"]

# 岗位预设：名称 -> (说明, 权限列表)
# 说明：分配了岗位的用户，其**任务类权限**以岗位为准（不再叠加 _USER_PERMISSIONS），
#       这样"班组成员只能反馈、班组长能分发"这类规则才能真正生效。
_ROLE_PRESETS = [
    ("处室负责人", "处室负责人：可见本处室及下级全部任务，可创建任务并分发给下属科室",
     ["task.view_all", "task.create", "task.edit", "task.delete", "task.assign",
      "task.claim", "task.feedback", "report.view"]),
    ("处室计划工程师", "处室计划工程师：与处室负责人一致的可见范围与任务分发权限",
     ["task.view_all", "task.create", "task.edit", "task.assign",
      "task.claim", "task.feedback", "report.view"]),
    ("科室负责人", "科室负责人：可见本科室及下级班组全部任务，可创建任务并分发给下属班组，"
                  "可认领上级处室下发的任务继续分发",
     ["task.view_all", "task.create", "task.edit", "task.delete", "task.assign",
      "task.claim", "task.feedback", "report.view"]),
    ("科室计划工程师", "科室计划工程师：与科室负责人一致的可见范围与任务分发权限",
     ["task.view_all", "task.create", "task.edit", "task.assign",
      "task.claim", "task.feedback", "report.view"]),
    ("班组长", "班组长：可见本班组全部工作任务，可认领上级科室下发的任务并分配给班组成员",
     ["task.view_all", "task.edit", "task.assign", "task.claim", "task.feedback"]),
    ("班组成员", "班组成员：仅可见自己的任务，可反馈任务进展",
     ["task.feedback"]),
]


def seed_role_presets():
    """按需创建「处室 / 科室 / 班组」六个岗位预设（幂等）

    - 岗位不存在            → 创建并按预设写入权限
    - 岗位已存在但权限为空   → 按预设补齐
      （修复 v3.3「保存权限后查看不到已保存权限」造成的空权限遗留数据）
    - 岗位已存在且已有权限   → 不动，避免覆盖用户自定义配置
    """
    try:
        conn = get_db()
    except Exception as e:
        log("[MIGRATE] 岗位预设跳过（数据库不可用）: %s" % e)
        return
    created = filled = 0
    try:
        for name, desc, perms in _ROLE_PRESETS:
            row = conn.execute("SELECT id FROM roles WHERE name=?", (name,)).fetchone()
            if row:
                rid = int(row["id"])
                cnt = conn.execute(
                    "SELECT COUNT(*) AS c FROM role_permissions WHERE role_id=?",
                    (rid,)).fetchone()["c"]
                if cnt:
                    continue          # 已有自定义权限，保持不动
                filled += 1
            else:
                cur = conn.execute(
                    "INSERT INTO roles(name,description,is_system) VALUES(?,?,0)", (name, desc))
                rid = int(cur.lastrowid)
                created += 1
            for p in perms:
                conn.execute(
                    "INSERT OR IGNORE INTO role_permissions(role_id,permission) VALUES(?,?)",
                    (rid, p))
        if created or filled:
            log("[MIGRATE] 岗位预设：新建 %d 个，补齐空权限 %d 个" % (created, filled))
    except Exception as e:
        log("[MIGRATE] 岗位预设失败: %s" % e)
    finally:
        conn.close()


def user_permissions(conn, user_row):
    """获取用户的所有权限（系统角色 + 岗位权限）

    取值规则（v3.4）：
      - superadmin                → 全部权限
      - admin                     → 管理员权限集 ∪ 岗位权限
      - user + 未分配岗位          → _USER_PERMISSIONS（历史默认，行为不变）
      - user + 已分配岗位          → **岗位权限完全决定**
        （这样「班组成员只能反馈」「班组长能分发」才能真正生效）

    注意：sqlite3.Row 没有 .get()，取值要用 try/except 或 keys() 判断。
    """
    perms = set()

    def pick(key, default=None):
        try:
            v = user_row[key]
        except (IndexError, KeyError):
            return default
        return default if v is None else v

    role    = pick("role", "user")
    role_id = pick("role_id")

    # 岗位权限（先取，后面按角色决定是否叠加）
    role_perms = set()
    role_is_system = False
    if role_id:
        rows = conn.execute(
            "SELECT permission FROM role_permissions WHERE role_id=?", (role_id,)).fetchall()
        role_perms = set(r["permission"] for r in rows)
        try:
            rr = conn.execute("SELECT is_system FROM roles WHERE id=?", (role_id,)).fetchone()
            role_is_system = bool(rr and rr["is_system"])
        except Exception:
            role_is_system = False

    if role == "superadmin":
        perms.update(_ALL_PERMISSIONS)
    elif role == "admin":
        perms.update(["task.create", "task.edit", "task.delete", "task.assign",
                      "task.view_all", "task.view_org", "task.claim", "task.feedback",
                      "user.manage", "user.create", "user.edit", "user.disable",
                      "org.manage", "org.create", "org.edit", "org.delete",
                      "role.manage", "report.view"])
        perms.update(role_perms)
    else:
        if role_id and not role_is_system:
            # 已分配**自定义岗位**：任务权限以岗位为准
            # （这样「班组成员只能反馈」「班组长能分发」才能真正生效）
            perms.update(role_perms)
        else:
            # 未分配岗位，或只绑定了「超级管理员/管理员/普通用户」这类系统内置角色
            # → 保持历史默认权限，避免把老用户权限清空
            perms.update(_USER_PERMISSIONS)
            perms.update(role_perms)

    return perms


def _session_perms(s):
    """按会话用户实时计算权限集合（系统角色 + 岗位）

    每次请求都重算，保证「岗位权限保存后立即生效」，无需重新登录。
    """
    try:
        conn = get_db()
    except Exception:
        return set()
    try:
        row = conn.execute("SELECT * FROM users WHERE id=?", (s["user_id"],)).fetchone()
        if not row:
            return set()
        return user_permissions(conn, dict(row))
    except Exception:
        return set()
    finally:
        conn.close()


def require_auth(f):
    @wraps(f)
    def wrapper(self, *a, **kw):
        s = session_get(self._get_token())
        if not s:
            return self._json({"error": "请先登录"}, 401)
        self._session = s
        # v3.4：所有需要登录的接口都注入权限集合，
        # 否则「岗位权限」（如 task.view_all）对 role=user 的用户完全不起作用。
        self._perms = _session_perms(s)
        return f(self, *a, **kw)
    return wrapper


def require_admin(f):
    @wraps(f)
    def wrapper(self, *a, **kw):
        s = session_get(self._get_token())
        if not s:
            return self._json({"error": "请先登录"}, 401)
        if s["role"] not in ("admin", "superadmin"):
            return self._json({"error": "权限不足，需要管理员权限"}, 403)
        self._session = s
        return f(self, *a, **kw)
    return wrapper


def require_permission(perm):
    def decorator(f):
        @wraps(f)
        def wrapper(self, *a, **kw):
            s = session_get(self._get_token())
            if not s:
                return self._json({"error": "请先登录"}, 401)
            conn = get_db()
            try:
                row = conn.execute("SELECT * FROM users WHERE id=?", (s["user_id"],)).fetchone()
                if not row:
                    return self._json({"error": "用户不存在"}, 404)
                perms = user_permissions(conn, dict(row))
                # 超级管理员拥有所有权限
                if s["role"] == "superadmin" or perm in perms:
                    self._session = s
                    self._perms = perms
                    return f(self, *a, **kw)
                return self._json({"error": "权限不足（需要: %s）" % perm}, 403)
            finally:
                conn.close()
        return wrapper
    return decorator


# ============================================================
#  HTTP 处理器
# ============================================================
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version   = "%s/%s" % (APP_NAME, VERSION)

    # ---------- 基础 ----------
    def _send(self, status, body=b"", content_type="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization,Content-Type")
        self.send_header("Cache-Control", "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, data, status=200):
        return self._send(status, json.dumps(data, ensure_ascii=False))

    def _get_token(self):
        auth = self.headers.get("Authorization", "") or ""
        return auth[7:] if auth.startswith("Bearer ") else None

    def _parse_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _audit(self, action, table="", record_id=0, detail=None):
        s = getattr(self, "_session", None)
        if not s:
            return
        conn = None
        try:
            conn = get_db()
            conn.execute(
                "INSERT INTO audit_log(user_id,action,table_name,record_id,detail,ip_addr)"
                " VALUES(?,?,?,?,?,?)",
                (s["user_id"], action, table, record_id,
                 json.dumps(detail or {}, ensure_ascii=False),
                 self.client_address[0] if self.client_address else ""))
        except Exception:
            pass
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def do_OPTIONS(self):
        self._send(204, b"")

    def log_message(self, fmt, *args):
        try:
            msg = fmt % args
        except Exception:
            msg = str(args)
        if "/api/" in msg:
            log(msg, to_file=False)

    def log_error(self, fmt, *args):
        try:
            log("[ERROR] " + (fmt % args))
        except Exception:
            pass

    # ---------- 路由 ----------
    def do_GET(self):
        try:
            self._route_get()
        except Exception:
            log("[ERROR] GET %s\n%s" % (self.path, traceback.format_exc()))
            try:
                self._json({"error": "服务器内部错误"}, 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            self._route_post()
        except Exception:
            log("[ERROR] POST %s\n%s" % (self.path, traceback.format_exc()))
            try:
                self._json({"error": "服务器内部错误"}, 500)
            except Exception:
                pass

    def do_PUT(self):
        try:
            self._route_put()
        except Exception:
            log("[ERROR] PUT %s\n%s" % (self.path, traceback.format_exc()))
            try:
                self._json({"error": "服务器内部错误"}, 500)
            except Exception:
                pass

    def do_DELETE(self):
        try:
            self._route_delete()
        except Exception:
            log("[ERROR] DELETE %s\n%s" % (self.path, traceback.format_exc()))
            try:
                self._json({"error": "服务器内部错误"}, 500)
            except Exception:
                pass

    def _route_get(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        qs     = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._serve_file("index.html")
        if path in ("/login.html", "/login"):
            return self._serve_file("login.html")
        if path == "/favicon.ico":
            return self._send(204, b"")
        if path.startswith("/static/"):
            return self._serve_file(path[len("/static/"):])

        # 系统
        if path == "/api/ping":
            return self._json({"ok": True, "app": APP_NAME, "version": VERSION,
                               "server_time": time.strftime("%Y-%m-%d %H:%M:%S")})
        if path == "/api/config":
            return self._json({"ok": True, "config": {
                "title":    APP_TITLE,
                "subtitle": APP_SUBTITLE,
                "version":  VERSION,
                "db":       db_status(),
                "server":   {"host": SERVER_HOST, "lan_access": LAN_ACCESS},
            }})
        if path == "/api/system/info":
            return self._json({"ok": True, "info": {
                "app": APP_NAME, "title": APP_TITLE, "version": VERSION,
                "python": sys.version.split()[0],
                "platform": sys.platform,
                "db": db_status(),
            }})

        # 自动更新
        if path == "/api/update/check":
            force = (qs.get("force") or [""])[0] in ("1", "true", "yes")
            info, err = update_check(force=force)
            return self._json({"ok": True,
                               "enabled": UPDATE_ENABLED,
                               "update": info,
                               "error": err,
                               "interval": UPDATE_INTERVAL})

        # 权限（items 带分组/说明，前端权限配置界面直接使用，避免两份定义漂移）
        if path == "/api/permissions":
            return self._json({"ok": True,
                               "permissions": _ALL_PERMISSIONS,
                               "items": _PERMISSION_META})

        # 个人设置 / 换肤（v3.2）
        if path == "/api/settings":
            return self._api_settings_get()

        # 任务多维筛选的可选值（v3.2）
        if path == "/api/tasks/filters":
            return self._api_task_filter_options()

        # Excel 导出（v3.2，二进制响应）
        if path.startswith("/api/export/"):
            return self._api_export(path[len("/api/export/"):], qs)

        # 诊断接口（v3.2.2-dbg）
        if path == "/api/debug/info":
            return self._api_debug_info(qs)
        if path == "/api/debug/export-test":
            return self._api_debug_export_test(qs.get("kind", ["tasks"])[0])
        if path == "/api/debug/export-test-download":
            return self._api_debug_export_download(qs.get("kind", ["tasks"])[0])

        # 任务
        if path == "/api/tasks":
            return self._api_tasks_list(qs)
        if path == "/api/tasks/stats":
            return self._api_tasks_stats(qs)
        if path == "/api/tasks/summary":
            return self._api_tasks_summary(qs)
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[1] == "tasks" and parts[2].isdigit():
            return self._api_task_get(parts[2])
        if len(parts) == 4 and parts[1] == "tasks" and parts[2].isdigit() and parts[3] == "comments":
            return self._api_comments_list(parts[2])
        if len(parts) == 4 and parts[1] == "tasks" and parts[2].isdigit() and parts[3] == "relations":
            return self._api_relations_list(parts[2])
        if len(parts) == 4 and parts[1] == "tasks" and parts[2].isdigit() and parts[3] == "progress":
            return self._api_task_progress(parts[2])

        # 人员 / 组织 / 角色
        if path == "/api/users":
            return self._api_users_list()
        if path == "/api/users/assignable":
            return self._api_users_assignable()
        if path == "/api/users/me":
            return self._api_me()
        if path == "/api/orgs":
            return self._api_orgs_list()
        if path == "/api/orgs/assignable":
            return self._api_orgs_assignable()
        if path == "/api/roles":
            return self._api_roles_list()

        parts = [p for p in path.split("/") if p]
        # 角色权限
        if len(parts) == 4 and parts[1] == "roles" and parts[3] == "permissions" and parts[2].isdigit():
            return self._api_role_permissions_get(parts[2])

        # 待确认反馈通知列表（GET）
        if path == "/api/confirmations/pending":
            return self._api_confirmations_pending()
        if path.startswith("/api/confirmations/pending"):
            parts2 = path.strip("/").split("/")
            if len(parts2) == 3 and parts2[2].isdigit():
                return self._api_confirmations_pending(int(parts2[2]))

        # 台账收集（GET）
        if path == "/api/ledger/templates":
            return self._api_ledger_templates_list()
        if path.startswith("/api/ledger/submissions/"):
            tpl_id = path.rstrip("/").split("/")[-1]
            if tpl_id.isdigit():
                return self._api_ledger_submissions_list(int(tpl_id))

        return self._json({"error": "Not Found"}, 404)

    def _route_post(self):
        path = urlparse(self.path).path
        body = self._parse_body()

        if path == "/api/auth/login":
            return self._api_login(body)
        if path == "/api/auth/logout":
            return self._api_logout()
        if path == "/api/auth/register":
            return self._api_register(body)
        if path == "/api/tasks":
            return self._api_tasks_create(body)
        if path == "/api/orgs":
            return self._api_orgs_create(body)
        if path == "/api/users":
            return self._api_users_create(body)
        if path == "/api/roles":
            return self._api_roles_create(body)

        # Excel 导入（v3.2）
        if path.startswith("/api/import/"):
            return self._api_import(path[len("/api/import/"):], body)

        # 自动更新：执行替换并重启
        if path == "/api/update/apply":
            ok, msg = update_apply()
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)

        parts = [p for p in path.split("/") if p]
        if len(parts) == 4 and parts[1] == "tasks" and parts[2].isdigit() and parts[3] == "claim":
            return self._api_task_claim(parts[2], body)
        if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "comments":
            return self._api_comments_create(parts[2], body)
        if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "relations":
            return self._api_relations_create(parts[2], body)
        # v3.3：确认反馈同时容许 POST /api/confirmations/<id>（与 PUT 等价）
        if len(parts) == 3 and parts[1] == "confirmations" and parts[2].isdigit():
            return self._api_confirmation_confirm(parts[2])

        # 台账收集 POST（v3.2.5）
        if path == "/api/ledger/templates":
            return self._api_ledger_templates_create(body)
        if path.startswith("/api/ledger/submissions/"):
            tpl_id = path.rstrip("/").split("/")[-1]
            if tpl_id.isdigit():
                return self._api_ledger_submission_create(int(tpl_id), body)
        if path == "/api/ledger/template/download":
            return self._api_ledger_template_download(body)

        return self._json({"error": "Not Found"}, 404)

    def _route_put(self):
        path = urlparse(self.path).path
        body = self._parse_body()

        # 个人设置 / 换肤（v3.2）
        if path == "/api/settings":
            return self._api_settings_update(body)

        if path == "/api/users/me":
            return self._api_me_update(body)
        if path.startswith("/api/tasks/"):
            return self._api_tasks_update(path.rstrip("/").split("/")[-1], body)
        if path.startswith("/api/orgs/"):
            return self._api_orgs_update(path.rstrip("/").split("/")[-1], body)
        if path.startswith("/api/users/"):
            return self._api_users_update(path.rstrip("/").split("/")[-1], body)
        if path.startswith("/api/roles/"):
            return self._api_roles_update(path.rstrip("/").split("/")[-1], body)

        parts = [p for p in path.split("/") if p]
        # 角色权限更新
        if len(parts) == 4 and parts[1] == "roles" and parts[3] == "permissions" and parts[2].isdigit():
            return self._api_role_permissions_update(parts[2], body)
        # 确认反馈
        if len(parts) == 3 and parts[1] == "confirmations" and parts[2].isdigit():
            return self._api_confirmation_confirm(parts[2])

        return self._json({"error": "Not Found"}, 404)

    def _route_delete(self):
        path = urlparse(self.path).path
        parts = [p for p in path.split("/") if p]

        if len(parts) == 5 and parts[1] == "tasks" and parts[3] == "comments":
            return self._api_comment_delete(parts[4])
        if len(parts) == 5 and parts[1] == "tasks" and parts[3] == "relations":
            return self._api_relation_delete(parts[4])
        if path.startswith("/api/tasks/"):
            return self._api_tasks_delete(path.rstrip("/").split("/")[-1])
        if path.startswith("/api/orgs/"):
            return self._api_orgs_delete(path.rstrip("/").split("/")[-1])
        if path.startswith("/api/users/"):
            return self._api_users_delete(path.rstrip("/").split("/")[-1])
        if path.startswith("/api/roles/"):
            return self._api_roles_delete(path.rstrip("/").split("/")[-1])
        return self._json({"error": "Not Found"}, 404)

    # ---------- 静态文件 ----------
    MIME = {
        "html": "text/html", "htm": "text/html", "css": "text/css",
        "js": "application/javascript", "json": "application/json",
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "gif": "image/gif", "svg": "image/svg+xml", "ico": "image/x-icon",
        "woff": "font/woff", "woff2": "font/woff2", "txt": "text/plain",
    }

    def _serve_file(self, rel_path):
        rel_path = rel_path.replace("/", os.sep).lstrip(os.sep)
        full = os.path.normpath(os.path.join(STATIC_DIR, rel_path))
        base = os.path.normpath(STATIC_DIR)
        if not full.startswith(base):
            return self._json({"error": "Forbidden"}, 403)
        if not os.path.isfile(full):
            return self._json({"error": "File not found: %s" % rel_path}, 404)
        ext = os.path.splitext(full)[1].lower().lstrip(".")
        try:
            with open(full, "rb") as f:
                data = f.read()
        except Exception as e:
            log("[ERROR] 读取静态文件失败 %s : %s" % (full, e))
            return self._json({"error": "Read error"}, 500)
        self._send(200, data, self.MIME.get(ext, "application/octet-stream"))

    # ============================================================
    #  任务
    # ============================================================
    @require_auth
    def _api_tasks_list(self, qs):
        conn = get_db()
        try:
            s = self._session
            status   = (qs.get("status")   or [None])[0]
            mine     = (qs.get("mine")     or [None])[0]
            search   = (qs.get("q")        or [None])[0]
            dept_id  = (qs.get("dept_id")  or [None])[0]
            view     = (qs.get("view")      or [None])[0]
            try:
                page  = max(1, int((qs.get("page")  or [1])[0]))
            except ValueError:
                page = 1
            try:
                limit = min(500, max(1, int((qs.get("limit") or [50])[0])))
            except ValueError:
                limit = 50
            offset = (page - 1) * limit

            where, params = self._build_task_filter(qs, conn)

            # 排序（v3.2 多维筛选）
            sort  = (qs.get("sort") or ["priority"])[0]
            desc  = (qs.get("order") or ["desc"])[0].lower() == "desc"
            order = "DESC" if desc else "ASC"
            sort_map = {
                "priority":   "t.priority " + order,
                "due_date":   "t.due_date IS NULL, t.due_date " + ("ASC" if desc else "DESC"),
                "created_at": "t.created_at " + order,
                "updated_at": "t.updated_at " + order,
                "status":     "t.status " + order,
                "title":      "t.title " + order,
                "task_no":    "t.task_no " + order,
            }
            order_sql = sort_map.get(sort, "t.priority DESC, t.due_date ASC NULLS LAST")
            where_sql = where          # _build_task_filter 已返回拼接好的 SQL
            total = conn.execute(
                "SELECT COUNT(*) AS cnt FROM tasks t WHERE " + where_sql, params
            ).fetchone()["cnt"]

            rows = conn.execute(
                "SELECT t.*, u.display_name AS assignee_name,"
                "       c.display_name AS creator_name,"
                "       o.name         AS org_name,"
                "       d.name         AS dept_name,"
                " (SELECT cm.content  FROM task_comments cm WHERE cm.task_id=t.id"
                "  ORDER BY cm.id DESC LIMIT 1) AS latest_comment,"
                " (SELECT cm.timestamp FROM task_comments cm WHERE cm.task_id=t.id"
                "  ORDER BY cm.id DESC LIMIT 1) AS latest_comment_time,"
                " (SELECT cu.display_name FROM task_comments cm"
                "  LEFT JOIN users cu ON cm.user_id=cu.id WHERE cm.task_id=t.id"
                "  ORDER BY cm.id DESC LIMIT 1) AS latest_comment_user,"
                " (SELECT COUNT(*) FROM task_comments cm WHERE cm.task_id=t.id) AS comment_count,"
                " (SELECT COUNT(*) FROM tasks s WHERE s.parent_id=t.id) AS subtask_total,"
                " (SELECT COUNT(*) FROM tasks s WHERE s.parent_id=t.id"
                "   AND s.status IN ('DONE','CLOSED')) AS subtask_done,"
                # --- v3.3 多部门 / 多责任人聚合 ---
                " (SELECT GROUP_CONCAT(td.dept_id) FROM task_departments td"
                "   WHERE td.task_id=t.id) AS dept_ids,"
                " (SELECT GROUP_CONCAT(od.name, '、') FROM task_departments td"
                "   LEFT JOIN organizations od ON td.dept_id=od.id"
                "   WHERE td.task_id=t.id) AS dept_names,"
                " (SELECT COUNT(*) FROM task_departments td"
                "   WHERE td.task_id=t.id) AS dept_count,"
                " (SELECT GROUP_CONCAT(ta.user_id) FROM task_assignees ta"
                "   WHERE ta.task_id=t.id) AS assignee_ids,"
                " (SELECT GROUP_CONCAT(ua.display_name, '、') FROM task_assignees ta"
                "   LEFT JOIN users ua ON ta.user_id=ua.id"
                "   WHERE ta.task_id=t.id) AS assignee_names,"
                " (SELECT COUNT(*) FROM task_assignees ta"
                "   WHERE ta.task_id=t.id) AS assignee_count"
                " FROM tasks t"
                " LEFT JOIN users u ON t.assignee_id = u.id"
                " LEFT JOIN users c ON t.created_by  = c.id"
                " LEFT JOIN organizations o ON t.org_id = o.id"
                " LEFT JOIN organizations d ON t.dept_id = d.id"
                " WHERE " + where_sql +
                " ORDER BY " + order_sql + ", t.id DESC LIMIT ? OFFSET ?",
                params + [limit, offset]
            ).fetchall()
            # v3.4：dept_ids / assignee_ids 统一转成数组返回，
            # 与 /api/tasks/<id> 保持一致，避免前端 .forEach 报错
            tasks = []
            for r in rows:
                d = dict(r)
                d["dept_ids"]     = _id_list(d.get("dept_ids"))
                d["assignee_ids"] = _id_list(d.get("assignee_ids"))
                tasks.append(d)
            return self._json({"ok": True, "tasks": tasks,
                               "total": total, "page": page, "limit": limit})
        finally:
            conn.close()

    @require_auth
    def _api_tasks_summary(self, qs):
        """任务汇总：与 /api/tasks 使用同一套可见性规则，保证统计与实际数据一致"""
        conn = get_db()
        try:
            s = self._session
            dept_id = (qs.get("dept_id") or [None])[0]

            # 与任务列表完全一致的可见性过滤（v3.3 统一规则）
            w0, p0 = self._task_scope(conn)
            where, params = ["1=1"], []
            if w0 != "1=1":
                where.append(w0)
                params.extend(p0)
            if dept_id:
                where.append("(t.dept_id = ? OR EXISTS("
                             " SELECT 1 FROM task_departments td"
                             " WHERE td.task_id=t.id AND td.dept_id=?))")
                params.extend([int(dept_id), int(dept_id)])
            w = " AND ".join(where)

            DONE_SQL = "t.status IN ('DONE','CLOSED')"
            # 总览计数
            cnt = conn.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(CASE WHEN t.status='TODO' THEN 1 ELSE 0 END) AS todo,"
                " SUM(CASE WHEN t.status='DOING' THEN 1 ELSE 0 END) AS doing,"
                " SUM(CASE WHEN %s THEN 1 ELSE 0 END) AS done,"
                " SUM(CASE WHEN t.due_date IS NOT NULL AND t.due_date != ''"
                "   AND NOT %s AND date(t.due_date) < date('now')"
                "   THEN 1 ELSE 0 END) AS overdue,"
                " SUM(CASE WHEN t.due_date IS NOT NULL AND t.due_date != ''"
                "   AND NOT %s AND date(t.due_date) >= date('now')"
                "   AND date(t.due_date) <= date('now','+7 days')"
                "   THEN 1 ELSE 0 END) AS upcoming"
                " FROM tasks t WHERE %s" % (DONE_SQL, DONE_SQL, DONE_SQL, w),
                params).fetchone()
            summary = {
                "total":    cnt["total"]    or 0,
                "todo":     cnt["todo"]     or 0,
                "doing":    cnt["doing"]    or 0,
                "done":     cnt["done"]     or 0,
                "overdue":  cnt["overdue"]  or 0,
                "upcoming": cnt["upcoming"] or 0,
            }

            # 部门待认领数量：仅「本人所在部门」已分发但尚未指定责任人的任务
            try:
                pw, pp = self._my_pool_clause(conn)
                prows = conn.execute(
                    "SELECT COUNT(DISTINCT t.id) AS c FROM tasks t"
                    " WHERE t.assignee_id IS NULL"
                    " AND NOT EXISTS(SELECT 1 FROM task_assignees ta"
                    "                 WHERE ta.task_id=t.id)"
                    " AND (t.dept_id IS NOT NULL OR EXISTS("
                    "   SELECT 1 FROM task_departments td WHERE td.task_id=t.id))"
                    " AND " + pw +
                    " AND NOT " + DONE_SQL, pp).fetchone()
                summary["pool_count"] = (prows["c"] if prows else 0) or 0
            except Exception as e:
                log("[SUMMARY] pool_count 计算失败: %s" % e)
                summary["pool_count"] = 0

            # 按部门统计：同时覆盖主部门与关联部门，避免多部门任务漏统计
            dept_rows = conn.execute(
                "SELECT d.id, d.name,"
                " COUNT(t.id) AS task_count,"
                " SUM(CASE WHEN t.status='TODO' THEN 1 ELSE 0 END) AS todo_count,"
                " SUM(CASE WHEN t.status='DOING' THEN 1 ELSE 0 END) AS doing_count,"
                " SUM(CASE WHEN %s THEN 1 ELSE 0 END) AS done_count"
                " FROM organizations d"
                " LEFT JOIN tasks t ON (t.dept_id = d.id OR EXISTS("
                "   SELECT 1 FROM task_departments td"
                "   WHERE td.task_id=t.id AND td.dept_id=d.id)) AND %s"
                " GROUP BY d.id, d.name HAVING COUNT(t.id) > 0"
                " ORDER BY task_count DESC" % (DONE_SQL, w),
                params).fetchall()

            # 近期截止 / 逾期明细
            sel = ("SELECT t.id, t.task_no, t.title, t.status, t.priority, t.due_date,"
                   " u.display_name AS assignee_name, d.name AS dept_name"
                   " FROM tasks t"
                   " LEFT JOIN users u ON t.assignee_id = u.id"
                   " LEFT JOIN organizations d ON t.dept_id = d.id")
            upcoming_rows = conn.execute(
                sel + " WHERE t.due_date IS NOT NULL AND t.due_date != ''"
                " AND NOT " + DONE_SQL +
                " AND date(t.due_date) >= date('now')"
                " AND date(t.due_date) <= date('now','+7 days') AND " + w +
                " ORDER BY t.due_date ASC LIMIT 20", params).fetchall()
            overdue_rows = conn.execute(
                sel + " WHERE t.due_date IS NOT NULL AND t.due_date != ''"
                " AND NOT " + DONE_SQL +
                " AND date(t.due_date) < date('now') AND " + w +
                " ORDER BY t.due_date ASC LIMIT 20", params).fetchall()

            return self._json({"ok": True, "summary": dict(
                summary,
                by_dept=[dict(r) for r in dept_rows],
                upcoming=[dict(r) for r in upcoming_rows],
                overdue=[dict(r) for r in overdue_rows],
            )})
        finally:
            conn.close()

    # ============================================================
    #  v3.2 数据大屏 / 换肤 / 多维筛选 / Excel 导入导出
    # ============================================================
    def _dept_scope_ids(self, conn):
        """当前用户所在部门及其所有下级部门 id（部门任务池可见范围）"""
        s = self._session
        oid = s.get("org_id")
        if not oid:
            return []
        try:
            rows = conn.execute("SELECT id, parent_id FROM organizations").fetchall()
        except Exception:
            return []
        children = {}
        for r in rows:
            children.setdefault(r["parent_id"], []).append(r["id"])
        out, stack = [], [oid]
        while stack:
            cur = stack.pop()
            if cur in out:
                continue
            out.append(cur)
            stack.extend(children.get(cur, []))
        return out

    def _direct_child_dept_ids(self, conn):
        """当前用户所在部门的**直接下级**部门 id（不穿透多层级分发）"""
        s = self._session
        oid = s.get("org_id")
        if not oid:
            return []
        try:
            rows = conn.execute(
                "SELECT id FROM organizations WHERE parent_id = ? ORDER BY sort_order, id",
                (oid,)).fetchall()
        except Exception:
            return []
        return [r["id"] for r in rows]

    def _my_pool_clause(self, conn=None):
        """「部门待认领」的范围条件（表别名固定为 t）

        v3.4：待认领 = **仅本人所在部门**尚未指派责任人的任务，不含下级部门。
        此前沿用可见性范围（含全部下级），上级部门会把下级的待认领任务也算进
        角标，数量虚高且认领不到。超级管理员作为全局调度者仍可见全部。
        """
        s = self._session
        if s.get("role") == "superadmin":
            return "1=1", []
        oid = s.get("org_id")
        if not oid:
            return "0=1", []
        return ("(t.dept_id = ? OR EXISTS(SELECT 1 FROM task_departments td"
                " WHERE td.task_id=t.id AND td.dept_id=?))", [oid, oid])

    def _scope_clause(self, conn=None):
        """部门管理员的可见性条件：本部门及所有下级部门的全部任务

        覆盖三种来源：
          1) 主部门 tasks.dept_id 落在可见部门集合内
          2) 关联部门 task_departments.dept_id 落在可见部门集合内（多部门分发）
          3) 主责任人 / 创建人是我，或我是关联责任人（多责任人）
        """
        s = self._session
        ids = []
        own = conn is not None
        if not own:
            conn = get_db()
        try:
            ids = self._dept_scope_ids(conn)
        finally:
            if not own:
                conn.close()
        conds = ["t.org_id = ?", "t.assignee_id = ?", "t.created_by = ?"]
        params = [s.get("org_id") or 1, s["user_id"], s["user_id"]]
        if ids:
            ph = ",".join("?" * len(ids))
            conds.append("t.dept_id IN (%s)" % ph)
            params.extend(ids)
            conds.append(
                "EXISTS(SELECT 1 FROM task_departments td"
                " WHERE td.task_id = t.id AND td.dept_id IN (%s))" % ph)
            params.extend(ids)
        conds.append(
            "EXISTS(SELECT 1 FROM task_assignees ta"
            " WHERE ta.task_id = t.id AND ta.user_id = ?)")
        params.append(s["user_id"])
        return "(" + " OR ".join(conds) + ")", params

    def _user_scope_clause(self, conn=None):
        """普通用户：只能看到自己相关的任务（指派给我 / 我创建 / 我关注）"""
        s = self._session
        uid = s["user_id"]
        conds = ["t.assignee_id = ?", "t.created_by = ?",
                 "EXISTS(SELECT 1 FROM task_assignees ta"
                 " WHERE ta.task_id = t.id AND ta.user_id = ?)",
                 "EXISTS(SELECT 1 FROM task_followers tf"
                 " WHERE tf.task_id = t.id AND tf.user_id = ?)"]
        return "(" + " OR ".join(conds) + ")", [uid, uid, uid, uid]

    def _own_org_scope_clause(self, conn=None):
        """「查看本部门任务」：本部门（不含下级部门）的任务 + 与我相关的任务

        仅当岗位显式授予 task.view_org 时生效（默认用户不含该权限，
        因此不会改变既有用户的可见范围）。
        """
        s = self._session
        oid = s.get("org_id") or 1
        uid = s["user_id"]
        conds = [
            "t.dept_id = ?",
            "t.org_id = ?",
            "t.assignee_id = ?",
            "t.created_by = ?",
            "EXISTS(SELECT 1 FROM task_departments td"
            " WHERE td.task_id=t.id AND td.dept_id = ?)",
            "EXISTS(SELECT 1 FROM task_assignees ta"
            " WHERE ta.task_id=t.id AND ta.user_id = ?)",
            "EXISTS(SELECT 1 FROM task_followers tf"
            " WHERE tf.task_id=t.id AND tf.user_id = ?)",
        ]
        return "(" + " OR ".join(conds) + ")", [oid, oid, uid, uid, oid, uid, uid]

    def _task_scope(self, conn=None):
        """返回 (where_sql, params)，全站统一的可见性规则（表别名固定为 t）

        v3.4 可见性分级：
          - superadmin             → 全部任务（1=1）
          - admin / task.view_all  → 本部门及**所有下级**部门的全部任务
                                     （处室负责人、科室负责人、班组长等）
          - task.view_org          → 仅本部门任务（不含下级）
          - 其它（如班组成员）      → 只能看到自己相关的任务
        """
        s = self._session
        perms = getattr(self, "_perms", set())
        if s["role"] == "superadmin":
            return "1=1", []
        if s["role"] == "admin" or "task.view_all" in perms:
            return self._scope_clause(conn)
        if "task.view_org" in perms:
            return self._own_org_scope_clause(conn)
        return self._user_scope_clause(conn)

    def _build_task_filter(self, qs, conn=None):
        """v3.2 多维筛选：构建任务查询条件（表别名固定为 t），返回 (where_sql, params)

        支持参数（多选一律用英文逗号分隔）：
          status / priority / task_type / category / source
          dept_id / assignee_id / created_by / parent_id
          mine=assigned|created|followed     unassigned=1      tags=标签
          due_from / due_to / created_from / created_to
          view=overdue|upcoming   overdue=1   upcoming=1   no_due=1   only_root=1
          q=关键词（标题/描述/编号/负责人）
        """
        qs = qs or {}
        s = self._session
        perms = getattr(self, "_perms", set())
        where, params = [], []
        w0, p0 = self._task_scope(conn)
        if w0 != "1=1":
            where.append(w0)
            params.extend(p0)
        where.insert(0, "1=1")

        def _one(key):
            v = qs.get(key)
            v = v[0] if isinstance(v, (list, tuple)) else v
            return "" if v is None else str(v).strip()

        def vals(key):
            v = _one(key)
            return [x.strip() for x in v.split(",") if x.strip()] if v else []

        def in_clause(key, col):
            vs = vals(key)
            if vs:
                where.append("%s IN (%s)" % (col, ",".join("?" * len(vs))))
                params.extend(vs)

        def int_clause(key, col):
            vs = []
            for x in vals(key):
                try:
                    vs.append(int(x))
                except ValueError:
                    pass
            if vs:
                where.append("%s IN (%s)" % (col, ",".join("?" * len(vs))))
                params.extend(vs)

        in_clause("status", "t.status")
        in_clause("priority", "t.priority")
        in_clause("task_type", "t.task_type")
        in_clause("category", "t.category")
        in_clause("source", "t.source")
        int_clause("created_by", "t.created_by")

        # 部门 / 责任人筛选需覆盖多对多关联表
        dept_vals = []
        for x in vals("dept_id"):
            try:
                dept_vals.append(int(x))
            except ValueError:
                pass
        if dept_vals:
            ph = ",".join("?" * len(dept_vals))
            where.append("(t.dept_id IN (%s) OR EXISTS("
                         " SELECT 1 FROM task_departments td"
                         " WHERE td.task_id=t.id AND td.dept_id IN (%s)))" % (ph, ph))
            params.extend(dept_vals)
            params.extend(dept_vals)
        asg_vals = []
        for x in vals("assignee_id"):
            try:
                asg_vals.append(int(x))
            except ValueError:
                pass
        if asg_vals:
            ph = ",".join("?" * len(asg_vals))
            where.append("(t.assignee_id IN (%s) OR EXISTS("
                         " SELECT 1 FROM task_assignees ta"
                         " WHERE ta.task_id=t.id AND ta.user_id IN (%s)))" % (ph, ph))
            params.extend(asg_vals)
            params.extend(asg_vals)

        mine = _one("mine")
        if mine == "assigned":
            where.append("(t.assignee_id = ? OR EXISTS("
                         " SELECT 1 FROM task_assignees ta"
                         " WHERE ta.task_id=t.id AND ta.user_id=?))")
            params.extend([s["user_id"], s["user_id"]])
        elif mine == "created":
            where.append("t.created_by = ?")
            params.append(s["user_id"])
        elif mine == "followed":
            where.append("EXISTS (SELECT 1 FROM task_followers f"
                         " WHERE f.task_id=t.id AND f.user_id=?)")
            params.append(s["user_id"])
        if _one("unassigned") == "1":
            where.append("(t.assignee_id IS NULL AND NOT EXISTS("
                         " SELECT 1 FROM task_assignees ta WHERE ta.task_id=t.id))")

        for tag in vals("tags"):
            where.append("(t.tags LIKE ?)")
            params.append("%" + tag + "%")

        def date_range(fkey, tkey, col):
            f, t = _one(fkey), _one(tkey)
            if f:
                where.append("date(%s) >= date(?)" % col)
                params.append(f[:10])
            if t:
                where.append("date(%s) <= date(?)" % col)
                params.append(t[:10])

        date_range("due_from", "due_to", "t.due_date")
        date_range("created_from", "created_to", "t.created_at")

        def flag(key):
            return _one(key) in ("1", "true", "True")

        view = _one("view")
        if view == "upcoming" or flag("upcoming"):
            where.append("t.status NOT IN ('DONE','CLOSED')")
            where.append("t.due_date IS NOT NULL AND t.due_date != ''")
            where.append("date(t.due_date) >= date('now')")
            where.append("date(t.due_date) <= date('now','+7 days')")
        if view == "overdue" or flag("overdue"):
            where.append("t.status NOT IN ('DONE','CLOSED')")
            where.append("t.due_date IS NOT NULL AND t.due_date != ''")
            where.append("date(t.due_date) < date('now')")
        if flag("no_due"):
            where.append("(t.due_date IS NULL OR t.due_date = '')")
        if flag("only_root"):
            where.append("(t.parent_id IS NULL)")
        if flag("pool"):
            # v3.4：待认领只列「本人所在部门」的任务池（与角标口径一致）
            pw, pp = self._my_pool_clause(conn)
            where.append("(t.assignee_id IS NULL AND NOT EXISTS("
                         " SELECT 1 FROM task_assignees ta WHERE ta.task_id=t.id)"
                         " AND (t.dept_id IS NOT NULL OR EXISTS("
                         " SELECT 1 FROM task_departments td WHERE td.task_id=t.id))"
                         " AND " + pw + ")")
            params.extend(pp)
        pv = vals("parent_id")
        if pv:
            try:
                where.append("t.parent_id = ?")
                params.append(int(pv[0]))
            except ValueError:
                where.pop()

        q = _one("q") or _one("search")
        if q:
            like = "%" + q + "%"
            where.append("(t.title LIKE ? OR t.description LIKE ? OR t.task_no LIKE ?"
                         " OR EXISTS (SELECT 1 FROM users su WHERE su.id=t.assignee_id"
                         "   AND su.display_name LIKE ?))")
            params.extend([like, like, like, like])

        return " AND ".join(where), params

    @require_auth
    def _api_tasks_stats(self, qs=None):
        """任务数据大屏：一次返回全部图表所需数据（range=today|week|month|year）"""
        qs = qs or {}
        rng = (qs.get("range") or ["month"])[0]
        if rng not in ("today", "week", "month", "year"):
            rng = "month"
        today = datetime.date.today()
        if rng == "today":
            start_date = today - datetime.timedelta(days=6)
        elif rng == "week":
            start_date = today - datetime.timedelta(weeks=7)
        elif rng == "month":
            start_date = today - datetime.timedelta(days=180)
        else:
            start_date = today - datetime.timedelta(days=365)
        start_str = start_date.strftime("%Y-%m-%d")

        conn = get_db()
        try:
            w, a = self._task_scope()
            DONE = "t.status IN ('DONE','CLOSED')"
            OVERDUE = ("NOT %s AND t.due_date IS NOT NULL AND t.due_date != ''"
                       " AND date(t.due_date) < date('now')" % DONE)
            wsql = w + " AND date(t.created_at) >= date(?)"
            aa = a + [start_str]

            # 1) 总览
            row = conn.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(CASE WHEN t.status='TODO' THEN 1 ELSE 0 END) AS todo,"
                " SUM(CASE WHEN t.status='DOING' THEN 1 ELSE 0 END) AS doing,"
                " SUM(CASE WHEN %(D)s THEN 1 ELSE 0 END) AS done,"
                " SUM(CASE WHEN %(O)s THEN 1 ELSE 0 END) AS overdue,"
                " SUM(CASE WHEN NOT %(D)s AND t.due_date IS NOT NULL AND t.due_date != ''"
                "   AND date(t.due_date) >= date('now')"
                "   AND date(t.due_date) <= date('now','+7 days') THEN 1 ELSE 0 END) AS upcoming,"
                " SUM(CASE WHEN %(D)s AND t.completed_at IS NOT NULL AND t.completed_at != ''"
                "   AND t.due_date IS NOT NULL AND t.due_date != ''"
                "   AND date(t.completed_at) <= date(t.due_date) THEN 1 ELSE 0 END) AS on_time"
                " FROM tasks t WHERE %(W)s" % {"D": DONE, "O": OVERDUE, "W": wsql}, aa).fetchone()
            total    = row["total"] or 0
            done_cnt = row["done"] or 0
            on_time_rate = round((row["on_time"] or 0) * 100.0 / done_cnt, 1) if done_cnt else 0.0
            summary = {
                "total": total,
                "todo": row["todo"] or 0,
                "doing": row["doing"] or 0,
                "done": done_cnt,
                "overdue": row["overdue"] or 0,
                "upcoming": row["upcoming"] or 0,
                "on_time_rate": on_time_rate,
            }

            # 2) 状态分布（四类互斥）
            sd = conn.execute(
                "SELECT SUM(CASE WHEN %(O)s THEN 1 ELSE 0 END) AS overdue,"
                " SUM(CASE WHEN NOT %(D)s AND NOT (%(O)s) AND t.status='DOING' THEN 1 ELSE 0 END) AS doing,"
                " SUM(CASE WHEN NOT %(D)s AND NOT (%(O)s) AND t.status<>'DOING' THEN 1 ELSE 0 END) AS todo,"
                " SUM(CASE WHEN %(D)s THEN 1 ELSE 0 END) AS done"
                " FROM tasks t WHERE %(W)s" % {"D": DONE, "O": OVERDUE, "W": wsql}, aa).fetchone()
            status_dist = [
                {"key": "done",    "name": "已完成", "value": sd["done"] or 0},
                {"key": "doing",   "name": "进行中", "value": sd["doing"] or 0},
                {"key": "todo",    "name": "待处理", "value": sd["todo"] or 0},
                {"key": "overdue", "name": "已逾期", "value": sd["overdue"] or 0},
            ]

            # 3) 趋势
            pairs = []
            if rng == "today":
                grp = "strftime('%%m-%%d', %s)"
                for i in range(6, -1, -1):
                    d = today - datetime.timedelta(days=i)
                    k = d.strftime("%m-%d")
                    pairs.append((k, k))
            elif rng == "week":
                grp = "strftime('%%Y-%%W', %s)"
                for i in range(7, -1, -1):
                    d = today - datetime.timedelta(weeks=i)
                    monday = d - datetime.timedelta(days=d.weekday())
                    pairs.append((monday.strftime("%Y-%W"), monday.strftime("%m-%d")))
            else:
                grp = "strftime('%%Y-%%m', %s)"
                n = 6 if rng == "month" else 12
                y, m = today.year, today.month
                for i in range(n - 1, -1, -1):
                    mm = m - i
                    yy = y + (mm - 1) // 12
                    mm = (mm - 1) % 12 + 1
                    pairs.append(("%04d-%02d" % (yy, mm), "%02d月" % mm))
            keys = [p[0] for p in pairs]
            idx = {k: i for i, k in enumerate(keys)}

            def _series(col):
                out = [0] * len(keys)
                sql = ("SELECT %s AS k, COUNT(*) AS c FROM tasks t"
                       " WHERE %s AND %s IS NOT NULL AND %s != '' AND date(%s) >= date(?)"
                       " GROUP BY k" % (grp % col, wsql, col, col, col))
                for r in conn.execute(sql, aa + [start_str]):
                    i = idx.get(r["k"])
                    if i is not None:
                        out[i] = r["c"]
                return out

            trend = {"labels": [p[1] for p in pairs],
                     "created": _series("t.created_at"),
                     "completed": _series("t.completed_at")}

            # 4) 部门负载 TOP8
            dept_top = [dict(r) for r in conn.execute(
                "SELECT COALESCE(d.name,'未分配') AS name, COUNT(*) AS total,"
                " SUM(CASE WHEN %(D)s THEN 1 ELSE 0 END) AS done,"
                " SUM(CASE WHEN %(O)s THEN 1 ELSE 0 END) AS overdue"
                " FROM tasks t LEFT JOIN organizations d ON t.dept_id=d.id"
                " WHERE %(W)s GROUP BY COALESCE(d.name,'未分配')"
                " ORDER BY total DESC LIMIT 8" % {"D": DONE, "O": OVERDUE, "W": wsql}, aa).fetchall()]

            # 5) 优先级分布
            pmap = {"H": "高", "M": "中", "L": "低"}
            prow = {r["k"]: r["c"] for r in conn.execute(
                "SELECT COALESCE(t.priority,'M') AS k, COUNT(*) AS c FROM tasks t"
                " WHERE %s GROUP BY k" % wsql, aa).fetchall()}
            priority_dist = [{"key": k, "name": pmap.get(k, k), "value": prow.get(k, 0)}
                             for k in ("H", "M", "L")]

            # 6) 人员完成排行 TOP5
            user_top = [dict(r) for r in conn.execute(
                "SELECT u.id, u.display_name AS name, COUNT(*) AS done"
                " FROM tasks t JOIN users u ON t.assignee_id=u.id"
                " WHERE %(W)s AND %(D)s AND t.completed_at IS NOT NULL AND t.completed_at != ''"
                "   AND date(t.completed_at) >= date(?)"
                " GROUP BY u.id ORDER BY done DESC LIMIT 5"
                % {"W": wsql, "D": DONE}, aa + [start_str]).fetchall()]

            # 7) 逾期清单（全量，不受时间范围限制）
            overdue_list = [dict(r) for r in conn.execute(
                "SELECT t.id, t.task_no, t.title, t.due_date, t.status,"
                " COALESCE(u.display_name,'未指派') AS assignee_name,"
                " CAST(julianday('now') - julianday(t.due_date) AS INTEGER) AS overdue_days"
                " FROM tasks t LEFT JOIN users u ON t.assignee_id=u.id"
                " WHERE %(W)s AND %(O)s"
                " ORDER BY t.due_date ASC LIMIT 20" % {"W": w, "O": OVERDUE}, a).fetchall()]

            # 8) 兼容旧前端的 stats 字段
            legacy = {"total": total, "TODO": 0, "DOING": 0, "DONE": 0, "CLOSED": 0}
            for r in conn.execute(
                    "SELECT t.status AS s, COUNT(*) AS c FROM tasks t WHERE %s GROUP BY t.status"
                    % wsql, aa).fetchall():
                if r["s"] in legacy:
                    legacy[r["s"]] = r["c"]
        finally:
            conn.close()

        return self._json({"ok": True, "range": rng, "summary": summary,
                           "status_dist": status_dist, "trend": trend,
                           "dept_top": dept_top, "priority_dist": priority_dist,
                           "user_top": user_top, "overdue_list": overdue_list,
                           "stats": legacy,
                           "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")})

    @require_auth
    def _api_settings_get(self):
        uid = self._session["user_id"]
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM user_settings WHERE user_id=?", (uid,)).fetchone()
            u = conn.execute("SELECT theme FROM users WHERE id=?", (uid,)).fetchone()
        finally:
            conn.close()
        extra = {}
        if row and row["settings"]:
            try:
                extra = json.loads(row["settings"])
            except Exception:
                extra = {}
        theme = "blue"
        if row and row["theme"]:
            theme = row["theme"]
        elif u and u["theme"]:
            theme = u["theme"]
        return self._json({"ok": True, "settings": {
            "theme": theme,
            "sidebar_collapsed": bool(row["sidebar_collapsed"]) if row else False,
            "density": (row["density"] if row and row["density"] else "medium"),
            "themes": DEFAULT_THEMES,
            "extra": extra,
        }})

    @require_auth
    def _api_settings_update(self, body):
        uid = self._session["user_id"]
        theme = body.get("theme")
        if theme is not None:
            theme = str(theme)
            if theme not in [t["key"] for t in DEFAULT_THEMES]:
                return self._json({"error": "不支持的主题"}, 400)
        density = body.get("density")
        if density is not None and str(density) not in ("compact", "medium", "airy"):
            return self._json({"error": "不支持的密度模式"}, 400)
        conn = get_db()
        try:
            conn.execute("INSERT OR IGNORE INTO user_settings(user_id) VALUES(?)", (uid,))
            if theme is not None:
                conn.execute(
                    "UPDATE user_settings SET theme=?, updated_at=datetime('now','localtime')"
                    " WHERE user_id=?", (theme, uid))
                try:
                    conn.execute("UPDATE users SET theme=? WHERE id=?", (theme, uid))
                except Exception:
                    pass
            if "sidebar_collapsed" in body:
                conn.execute("UPDATE user_settings SET sidebar_collapsed=? WHERE user_id=?",
                             (1 if body["sidebar_collapsed"] else 0, uid))
            if density is not None:
                conn.execute("UPDATE user_settings SET density=? WHERE user_id=?",
                             (str(density), uid))
            if isinstance(body.get("extra"), dict):
                conn.execute("UPDATE user_settings SET settings=? WHERE user_id=?",
                             (json.dumps(body["extra"], ensure_ascii=False), uid))
            self._audit("UPDATE", "user_settings", uid, body)
        finally:
            conn.close()
        return self._json({"ok": True})

    # ---------- 多维筛选可选值（v3.2 / v3.4 收紧部门范围） ----------
    @require_auth
    def _api_task_filter_options(self):
        conn = get_db()
        try:
            s = self._session
            my_org = s.get("org_id")
            is_super = (s["role"] == "superadmin")
            # v3.4：筛选部门只展示「本部门 + 直属下级部门」，
            # 不再把全部部门都塞进筛选框（越权可见 + 列表过长）
            if is_super or not my_org:
                depts = conn.execute(
                    "SELECT id, name FROM organizations ORDER BY sort_order, id").fetchall()
            else:
                depts = conn.execute(
                    "SELECT id, name FROM organizations"
                    " WHERE id=? OR parent_id=?"
                    " ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END, sort_order, id",
                    (my_org, my_org, my_org)).fetchall()
            scope_ids = [r["id"] for r in depts]

            # 负责人候选同样收敛到上述部门范围（超管/无部门时保持全局）
            if scope_ids and not is_super:
                ph = ",".join("?" * len(scope_ids))
                users = conn.execute(
                    "SELECT id, display_name FROM users"
                    " WHERE COALESCE(status,'active')!='disabled' AND org_id IN (%s)"
                    " ORDER BY display_name" % ph, scope_ids).fetchall()
            else:
                users = conn.execute(
                    "SELECT id, display_name FROM users WHERE COALESCE(status,'active')!='disabled'"
                    " ORDER BY display_name").fetchall()

            cats = conn.execute(
                "SELECT DISTINCT category FROM tasks"
                " WHERE category IS NOT NULL AND category!='' ORDER BY category").fetchall()
            srcs = conn.execute(
                "SELECT DISTINCT source FROM tasks"
                " WHERE source IS NOT NULL AND source!='' ORDER BY source").fetchall()
            tag_rows = conn.execute(
                "SELECT tags FROM tasks WHERE tags IS NOT NULL AND tags!=''").fetchall()
        finally:
            conn.close()
        tags = []
        for r in tag_rows:
            try:
                for t in json.loads(r["tags"] or "[]"):
                    if t and t not in tags:
                        tags.append(t)
            except Exception:
                pass
        return self._json({"ok": True, "options": {
            "status": [{"key": "TODO", "name": "待处理"}, {"key": "DOING", "name": "进行中"},
                       {"key": "DONE", "name": "已完成"}, {"key": "CLOSED", "name": "已关闭"}],
            "priority": [{"key": "H", "name": "高"}, {"key": "M", "name": "中"},
                         {"key": "L", "name": "低"}],
            "task_type": [{"key": "NORMAL", "name": "普通任务"},
                          {"key": "CYCLIC", "name": "周期性任务"}],
            "category": [r["category"] for r in cats],
            "source": [r["source"] for r in srcs],
            "dept": [dict(r) for r in depts],
            "user": [dict(r) for r in users],
            "tags": tags,
        }})

    # ---------- Excel 导出（v3.2） ----------
    XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def _xlsx_response(self, rows, sheet_name, file_name):
        data = excelutil.write_xlsx(rows, sheet_name)
        try:
            from urllib.parse import quote
            encoded = quote(file_name)
        except Exception:
            encoded = "export.xlsx"
        return self._send(200, data, self.XLSX_MIME, {
            "Content-Disposition":
                "attachment; filename=export.xlsx; filename*=UTF-8''%s" % encoded})

    @staticmethod
    def _task_export_rows(rows):
        st_map = {"TODO": "待处理", "DOING": "进行中", "DONE": "已完成", "CLOSED": "已关闭"}
        pri_map = {"H": "高", "M": "中", "L": "低"}
        out = [["任务编号", "标题", "描述", "状态", "优先级", "任务类型", "任务种类",
                "来源", "负责人", "部门", "创建人", "截止日期", "创建时间", "完成时间",
                "预计工时", "实际工时"]]
        for r in rows:
            out.append([
                r.get("task_no") or "", r.get("title") or "", r.get("description") or "",
                st_map.get(r.get("status"), r.get("status") or ""),
                pri_map.get(r.get("priority"), r.get("priority") or ""),
                "周期性任务" if r.get("task_type") == "CYCLIC" else "普通任务",
                r.get("category") or "", r.get("source") or "",
                r.get("assignee_name") or "", r.get("dept_name") or "",
                r.get("creator_name") or "", r.get("due_date") or "",
                r.get("created_at") or "", r.get("completed_at") or "",
                r.get("estimated_hours") if r.get("estimated_hours") is not None else "",
                r.get("actual_hours") if r.get("actual_hours") is not None else "",
            ])
        return out

    @require_auth
    def _api_export(self, kind, qs):
        if excelutil is None:
            return self._json({"error": "Excel 模块不可用，无法导出"}, 500)
        kind = (kind or "").split("?")[0].strip("/")
        stamp = time.strftime("%Y%m%d")
        conn = get_db()
        try:
            if kind == "template":
                t = (qs.get("type") or ["tasks"])[0]
                if t == "users":
                    return self._xlsx_response(
                        [["登录名", "姓名", "部门", "角色", "岗位", "手机", "邮箱", "状态", "密码"],
                         ["zhangwei", "张伟", "运行一部", "普通用户", "检修工", "13800000000",
                          "zhangwei@example.com", "启用", "123456"]],
                        "人员导入模板", "人员导入模板_%s.xlsx" % stamp)
                if t == "orgs":
                    return self._xlsx_response(
                        [["部门名称", "上级部门", "排序", "说明"],
                         ["运行一部", "总公司", "1", "负责机组运行"]],
                        "部门导入模板", "部门导入模板_%s.xlsx" % stamp)
                return self._xlsx_response(
                    [["标题", "任务编号", "描述", "状态", "优先级", "任务类型", "任务种类",
                      "来源", "负责人", "部门", "截止日期", "预计工时"],
                     ["示例：3 号机组大修准备", "", "填写任务描述", "待处理", "高",
                      "普通任务", "大修任务", "计划", "王建国", "运行一部", "2026-09-30", "8"]],
                    "任务导入模板", "任务导入模板_%s.xlsx" % stamp)

            if kind == "tasks":
                where, params = self._build_task_filter(qs, conn)
                rows = conn.execute(
                    "SELECT t.*, u.display_name AS assignee_name,"
                    " c.display_name AS creator_name, d.name AS dept_name"
                    " FROM tasks t"
                    " LEFT JOIN users u ON t.assignee_id=u.id"
                    " LEFT JOIN users c ON t.created_by=c.id"
                    " LEFT JOIN organizations d ON t.dept_id=d.id"
                    " WHERE " + where +
                    " ORDER BY t.priority DESC, t.due_date ASC, t.id DESC",
                    params).fetchall()
                body = self._task_export_rows([dict(r) for r in rows])
                return self._xlsx_response(body, "任务清单", "任务清单_%s.xlsx" % stamp)

            if kind == "users":
                rows = conn.execute(
                    "SELECT u.id,u.username,u.display_name,u.role,u.status,u.phone,u.email,"
                    " o.name AS org_name, r.name AS role_name"
                    " FROM users u LEFT JOIN organizations o ON u.org_id=o.id"
                    " LEFT JOIN roles r ON u.role_id=r.id ORDER BY u.id").fetchall()
                out = [["登录名", "姓名", "部门", "角色", "岗位", "手机", "邮箱", "状态"]]
                role_map = {"user": "普通用户", "admin": "管理员", "superadmin": "超级管理员"}
                for r in rows:
                    out.append([r["username"], r["display_name"], r["org_name"] or "",
                                role_map.get(r["role"], r["role"] or ""), r["role_name"] or "",
                                r["phone"] or "", r["email"] or "",
                                "启用" if (r["status"] or "active") == "active" else "禁用"])
                return self._xlsx_response(out, "人员清单", "人员清单_%s.xlsx" % stamp)

            if kind == "orgs":
                rows = conn.execute(
                    "SELECT o.id,o.name,o.parent_id,o.sort_order,o.description,"
                    " p.name AS parent_name,"
                    " (SELECT COUNT(*) FROM users u WHERE u.org_id=o.id) AS user_count,"
                    " (SELECT COUNT(*) FROM organizations c WHERE c.parent_id=o.id) AS child_count"
                    " FROM organizations o LEFT JOIN organizations p ON o.parent_id=p.id"
                    " ORDER BY o.sort_order, o.id").fetchall()
                out = [["部门名称", "上级部门", "排序", "直属人数", "子部门数", "说明"]]
                for r in rows:
                    out.append([r["name"], r["parent_name"] or "", r["sort_order"] or 0,
                                r["user_count"], r["child_count"], r["description"] or ""])
                return self._xlsx_response(out, "组织架构", "组织架构_%s.xlsx" % stamp)
        finally:
            conn.close()
        return self._json({"error": "不支持的导出类型"}, 404)

    # ================================================================
    # 诊断接口（v3.2.2-dbg）— 用于排查导出失败问题
    # ================================================================
    def _diag_info(self):
        """返回完整系统诊断信息"""
        import sys as _sys, os as _os, traceback as _tb

        # excelutil 导入尝试详情
        eu_status = "未加载"
        eu_error = ""
        eu_excelutil_file = None
        eu_candidates = []
        eu_loaded_from = ""

        if excelutil is None:
            # 复现导入探测路径
            _candidates = []
            if IS_FROZEN:
                _exe_dir = _os.path.dirname(_os.path.abspath(_sys.executable))
                _meipass = getattr(_sys, "_MEIPASS", "")
                _candidates = [_exe_dir, _meipass] if _meipass else [_exe_dir]
            else:
                _candidates = [_os.path.dirname(_os.path.abspath(__file__))]

            for _p in _candidates:
                if _p:
                    eu_candidates.append(_p)

            # 尝试实际导入并捕获错误
            for _p in _candidates:
                if not _p:
                    continue
                if _p not in _sys.path:
                    _sys.path.insert(0, _p)
                try:
                    import excelutil as _eu_test
                    eu_status = "加载成功"
                    eu_loaded_from = _p
                    break
                except Exception as _e:
                    eu_status = "加载失败"
                    eu_error = str(_e)
        else:
            eu_status = "已加载"
            eu_loaded_from = str(getattr(excelutil, "__file__", "未知"))

        # 尝试直接执行 write_xlsx
        eu_write_test = ""
        eu_write_result = ""
        if excelutil is not None:
            try:
                test_rows = [["测试", "列1"], ["数据1", "值1"]]
                result = excelutil.write_xlsx(test_rows, "诊断测试")
                eu_write_result = "成功，大小 %d 字节" % len(result)
            except Exception as _e:
                eu_write_test = "失败: %s" % _e

        # 系统环境
        env_info = {
            "IS_FROZEN": IS_FROZEN,
            "sys.executable": getattr(_sys, "executable", "未知"),
            "sys._MEIPASS": getattr(_sys, "_MEIPASS", ""),
            "cwd": _os.getcwd(),
            "python_version": _sys.version,
            "searched_paths": eu_candidates,
            "excelutil_file_found": eu_excelutil_file,
            "excelutil_status": eu_status,
            "excelutil_error": eu_error,
            "excelutil_loaded_from": eu_loaded_from,
            "excelutil_write_test": eu_write_result or eu_write_test,
        }

        return env_info

    @require_auth
    def _api_debug_info(self, body):
        """GET /api/debug/info — 返回完整诊断信息"""
        return self._json({"ok": True, "info": self._diag_info()})

    @require_auth
    def _api_debug_export_test(self, kind):
        """GET /api/debug/export-test?kind=tasks — 分阶段 JSON 诊断（不返回文件）"""
        kind = (kind or "").strip("/")
        result = {
            "kind": kind,
            "phases": [],
            "all_passed": True,
        }

        # 阶段1: excelutil 加载
        p1 = {"phase": 1, "name": "excelutil 模块加载", "passed": excelutil is not None}
        if excelutil is None:
            p1["error"] = "excelutil 未加载（为 None）"
            p1["detail"] = self._diag_info()
            result["all_passed"] = False
        else:
            p1["info"] = "excelutil 加载成功"
            p1["module_file"] = getattr(excelutil, "__file__", "未知")
        result["phases"].append(p1)

        if excelutil is None:
            return self._json(result, 500)

        # 阶段2: write_xlsx 调用
        p2 = {"phase": 2, "name": "write_xlsx 函数调用"}
        try:
            test_rows = [["编号", "标题", "状态"], ["T001", "诊断测试任务", "待处理"]]
            data = excelutil.write_xlsx(test_rows, "导出测试")
            p2["passed"] = True
            p2["info"] = "write_xlsx 调用成功"
            p2["data_size"] = len(data)
        except Exception as e:
            p2["passed"] = False
            p2["error"] = str(e)
            p2["traceback"] = traceback.format_exc()
            result["all_passed"] = False
        result["phases"].append(p2)

        # 阶段3: HTTP 响应构造（只检测，不返回——避免双写响应）
        p3 = {"phase": 3, "name": "HTTP 响应构造"}
        try:
            stamp = time.strftime("%Y%m%d")
            from urllib.parse import quote
            _encoded = quote("export_test_%s.xlsx" % stamp)
            _fake_headers = {
                "Content-Disposition":
                    "attachment; filename=export_test.xlsx; filename*=UTF-8''%s" % _encoded
            }
            # 只验证数据可序列化 + 构造头信息，不调用 _send 写响应
            assert isinstance(data, bytes) and len(data) > 0, "Excel 数据为空"
            p3["passed"] = True
            p3["info"] = "响应构造成功（Content-Disposition 就绪，文件 %d 字节）" % len(data)
            p3["headers"] = _fake_headers
        except Exception as e:
            p3["passed"] = False
            p3["error"] = str(e)
            p3["traceback"] = traceback.format_exc()
            result["all_passed"] = False
        result["phases"].append(p3)

        status = 200 if result["all_passed"] else 500
        return self._json(result, status)

    @require_auth
    def _api_debug_export_download(self, kind):
        """GET /api/debug/export-test-download?kind=tasks — 直接下载测试 Excel（供手动验证）"""
        kind = (kind or "").strip("/")
        if excelutil is None:
            return self._json({"error": "excelutil 未加载"}, 500)
        try:
            test_rows = [["编号", "标题", "状态"], ["T001", "诊断测试任务", "待处理"]]
            data = excelutil.write_xlsx(test_rows, "导出测试")
            stamp = time.strftime("%Y%m%d")
            return self._send(200, data, self.XLSX_MIME, {
                "Content-Disposition":
                    "attachment; filename=diag_test_%s.xlsx; filename*=UTF-8''diag_test_%s.xlsx" % (stamp, stamp)
            })
        except Exception as e:
            return self._json({"error": "导出失败: " + str(e), "traceback": traceback.format_exc()}, 500)

    # ---------- Excel 导入（v3.2） ----------
    def _decode_upload(self, body):
        raw_b64 = body.get("file") or body.get("data") or ""
        if not raw_b64:
            raise ValueError("没有收到文件内容")
        if isinstance(raw_b64, str) and raw_b64.startswith("data:"):
            raw_b64 = raw_b64.split(",", 1)[-1]
        try:
            return base64.b64decode(raw_b64)
        except Exception:
            raise ValueError("文件内容解析失败")

    @staticmethod
    def _norm_status(v):
        m = {"待处理": "TODO", "待办": "TODO", "未开始": "TODO",
             "进行中": "DOING", "执行中": "DOING", "处理中": "DOING",
             "已完成": "DONE", "完成": "DONE", "已关闭": "CLOSED", "关闭": "CLOSED"}
        v = (v or "").strip()
        if v in m:
            return m[v]
        return v.upper() if v.upper() in ("TODO", "DOING", "DONE", "CLOSED") else "TODO"

    @staticmethod
    def _norm_priority(v):
        m = {"高": "H", "高优先级": "H", "紧急": "H", "中": "M", "中优先级": "M",
             "低": "L", "低优先级": "L"}
        v = (v or "").strip()
        if v in m:
            return m[v]
        return v.upper() if v.upper() in ("H", "M", "L") else "M"

    @require_auth
    def _api_import(self, kind, body):
        if excelutil is None:
            return self._json({"error": "Excel 模块不可用，无法导入"}, 500)
        kind = (kind or "").strip("/")
        if kind not in ("tasks", "users", "orgs"):
            return self._json({"error": "不支持的导入类型"}, 404)
        if kind == "tasks":
            perms = getattr(self, "_perms", set())
            if self._session["role"] not in ("admin", "superadmin") and "task.create" not in perms:
                return self._json({"error": "没有导入任务的权限"}, 403)
        else:
            if self._session["role"] not in ("admin", "superadmin"):
                return self._json({"error": "需要管理员权限"}, 403)

        try:
            raw = self._decode_upload(body)
            rows = excelutil.read_xlsx(raw)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except excelutil.ExcelError as e:
            return self._json({"error": str(e)}, 400)
        if len(rows) < 2:
            return self._json({"error": "表格内容为空（至少需要表头和一行数据）"}, 400)

        header = [str(x).strip() for x in rows[0]]
        result = {"created": 0, "failed": 0, "errors": [], "skipped": 0}
        mode = (body.get("mode") or "create")
        conn = get_db()
        try:
            for i, r in enumerate(rows[1:], start=2):
                rec = {}
                for j, h in enumerate(header):
                    if h and j < len(r):
                        rec[h] = (r[j] or "").strip()
                try:
                    if kind == "tasks":
                        self._import_task_row(conn, rec, result)
                    elif kind == "users":
                        self._import_user_row(conn, rec, result, mode)
                    else:
                        self._import_org_row(conn, rec, result, mode)
                except Exception as e:
                    result["failed"] += 1
                    if len(result["errors"]) < 50:
                        result["errors"].append("第 %d 行：%s" % (i, e))
        finally:
            conn.close()
        self._audit("IMPORT", kind, 0, {"created": result["created"],
                                        "failed": result["failed"]})
        return self._json({"ok": True, "result": result})

    def _import_task_row(self, conn, rec, result):
        title = rec.get("标题") or rec.get("任务标题") or ""
        if not title:
            result["skipped"] += 1
            return
        s = self._session
        due = rec.get("截止日期") or rec.get("截止时间") or ""
        if due:
            try:
                float(due)
                due = excelutil.xlsx_serial_to_date(due) or due
            except ValueError:
                pass
            due = due.replace("/", "-")[:10]
        assignee_id = None
        aname = rec.get("负责人") or ""
        if aname:
            u = conn.execute("SELECT id FROM users WHERE display_name=? OR username=? LIMIT 1",
                             (aname, aname)).fetchone()
            if u:
                assignee_id = u["id"]
        dept_id = None
        dname = rec.get("部门") or ""
        if dname:
            d = conn.execute("SELECT id FROM organizations WHERE name=? LIMIT 1", (dname,)).fetchone()
            if d:
                dept_id = d["id"]
        task_no = rec.get("任务编号") or ""
        if not task_no:
            task_no = self._gen_task_no(conn)
        ttype = rec.get("任务类型") or rec.get("类型") or ""
        try:
            est = float(rec.get("预计工时") or 0) or None
        except ValueError:
            est = None
        conn.execute(
            "INSERT INTO tasks(task_no,title,description,task_type,category,source,"
            "  status,priority,assignee_id,dept_id,created_by,org_id,tags,"
            "  due_date,estimated_hours)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_no, title, rec.get("描述") or rec.get("任务描述") or "",
             "CYCLIC" if "周期" in ttype else "NORMAL",
             rec.get("任务种类") or rec.get("种类") or "日常任务",
             rec.get("来源") or "导入",
             self._norm_status(rec.get("状态")),
             self._norm_priority(rec.get("优先级")),
             assignee_id, dept_id, s["user_id"], s.get("org_id") or 1,
             json.dumps([rec["标签"]], ensure_ascii=False) if rec.get("标签") else "[]",
             due, est))
        result["created"] += 1

    def _import_user_row(self, conn, rec, result, mode):
        username = rec.get("登录名") or rec.get("用户名") or ""
        if not username:
            result["skipped"] += 1
            return
        exist = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if exist and mode != "update":
            raise ValueError("登录名已存在：%s" % username)
        org_id = None
        oname = rec.get("部门") or ""
        if oname:
            o = conn.execute("SELECT id FROM organizations WHERE name=? LIMIT 1", (oname,)).fetchone()
            org_id = o["id"] if o else None
        role_map = {"普通用户": "user", "用户": "user", "管理员": "admin", "超级管理员": "superadmin"}
        role = role_map.get(rec.get("角色") or "", rec.get("角色") or "user")
        if role not in ("user", "admin", "superadmin"):
            role = "user"
        role_id = None
        rname = rec.get("岗位") or ""
        if rname:
            rr = conn.execute("SELECT id FROM roles WHERE name=? LIMIT 1", (rname,)).fetchone()
            role_id = rr["id"] if rr else None
        status = "disabled" if (rec.get("状态") or "") in ("禁用", "停用", "disabled") else "active"
        if exist:
            conn.execute(
                "UPDATE users SET display_name=?, org_id=?, role=?, role_id=?, status=?,"
                " phone=?, email=?, updated_at=datetime('now','localtime') WHERE id=?",
                (rec.get("姓名") or username, org_id, role, role_id, status,
                 rec.get("手机") or "", rec.get("邮箱") or "", exist["id"]))
            result["created"] += 1
            return
        pw = rec.get("密码") or "123456"
        conn.execute(
            "INSERT INTO users(username,display_name,password,org_id,role,role_id,status,phone,email)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (username, rec.get("姓名") or username,
             hashlib.sha256(pw.encode("utf-8")).hexdigest(), org_id, role, role_id, status,
             rec.get("手机") or "", rec.get("邮箱") or ""))
        result["created"] += 1

    def _import_org_row(self, conn, rec, result, mode):
        name = rec.get("部门名称") or rec.get("名称") or ""
        if not name:
            result["skipped"] += 1
            return
        parent_id = None
        pname = rec.get("上级部门") or ""
        if pname:
            p = conn.execute("SELECT id FROM organizations WHERE name=? LIMIT 1", (pname,)).fetchone()
            parent_id = p["id"] if p else None
        try:
            sort_order = int(rec.get("排序") or 0)
        except ValueError:
            sort_order = 0
        exist = conn.execute("SELECT id FROM organizations WHERE name=?", (name,)).fetchone()
        if exist:
            if mode == "update":
                conn.execute(
                    "UPDATE organizations SET parent_id=?, sort_order=?, description=?,"
                    " updated_at=datetime('now','localtime') WHERE id=?",
                    (parent_id, sort_order, rec.get("说明") or "", exist["id"]))
                result["created"] += 1
            else:
                result["skipped"] += 1
            return
        conn.execute(
            "INSERT INTO organizations(name,parent_id,sort_order,description)"
            " VALUES(?,?,?,?)", (name, parent_id, sort_order, rec.get("说明") or ""))
        result["created"] += 1

    @require_auth
    def _api_task_get(self, tid):
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT t.*, u.display_name AS assignee_name,"
                "       c.display_name AS creator_name,"
                "       o.name AS org_name,"
                "       d.name AS dept_name,"
                " (SELECT GROUP_CONCAT(td.dept_id) FROM task_departments td"
                "   WHERE td.task_id=t.id) AS dept_ids,"
                " (SELECT GROUP_CONCAT(od.name, '、') FROM task_departments td"
                "   LEFT JOIN organizations od ON td.dept_id=od.id"
                "   WHERE td.task_id=t.id) AS dept_names,"
                " (SELECT GROUP_CONCAT(ta.user_id) FROM task_assignees ta"
                "   WHERE ta.task_id=t.id) AS assignee_ids,"
                " (SELECT GROUP_CONCAT(ua.display_name, '、') FROM task_assignees ta"
                "   LEFT JOIN users ua ON ta.user_id=ua.id"
                "   WHERE ta.task_id=t.id) AS assignee_names,"
                " (SELECT COUNT(*) FROM task_comments cm WHERE cm.task_id=t.id) AS comment_count"
                " FROM tasks t"
                " LEFT JOIN users u ON t.assignee_id = u.id"
                " LEFT JOIN users c ON t.created_by  = c.id"
                " LEFT JOIN organizations o ON t.org_id = o.id"
                " LEFT JOIN organizations d ON t.dept_id = d.id"
                " WHERE t.id=?", (tid,)).fetchone()

            if not row:
                return self._json({"error": "任务不存在"}, 404)

            # --- v3.3 可见性校验：不能通过直读 ID 绕过任务隔离 ---
            w0, p0 = self._task_scope(conn)
            if w0 != "1=1":
                chk = conn.execute(
                    "SELECT COUNT(*) AS c FROM tasks t WHERE t.id=? AND " + w0,
                    [tid] + list(p0)).fetchone()
                if not chk or not chk["c"]:
                    return self._json({"error": "无权查看该任务"}, 403)
        finally:
            conn.close()

        # 多部门 / 多责任人转为数组返回，便于前端渲染
        task = dict(row)
        task["dept_ids"]     = _id_list(task.get("dept_ids"))
        task["assignee_ids"] = _id_list(task.get("assignee_ids"))
        return self._json({"ok": True, "task": task})

    @require_auth
    def _api_task_claim(self, tid, body=None):
        """认领部门待认领任务，并可在认领时直接指定后续去向

        v3.4：认领时可同时选择
          - dept_ids      继续分发给本部门的**直接下级**部门（任务下移一层）
          - assignee_ids  直接指派给责任人（限本部门及下级部门人员）
        两者都不传 → 等价于「认领给自己」（向后兼容旧行为）。
        """
        body = body or {}
        conn = get_db()
        try:
            s = self._session
            perms = getattr(self, "_perms", set())
            is_mgr = s["role"] in ("admin", "superadmin") or "task.assign" in perms

            row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if not row:
                return self._json({"error": "任务不存在"}, 404)
            # v3.3：已有责任人（含多责任人关联）时不可重复认领
            has_asg = bool(row["assignee_id"]) or bool(conn.execute(
                "SELECT 1 FROM task_assignees WHERE task_id=? LIMIT 1", (tid,)).fetchone())
            if has_asg:
                return self._json({"error": "该任务已指派，无法重复认领"}, 400)
            # v3.3：分发部门可能来自关联表
            my_depts = set()
            if row["dept_id"]:
                my_depts.add(row["dept_id"])
            for r in conn.execute(
                    "SELECT dept_id FROM task_departments WHERE task_id=?", (tid,)).fetchall():
                if r["dept_id"]:
                    my_depts.add(r["dept_id"])
            if not my_depts:
                return self._json({"error": "该任务未分发到部门，不能认领"}, 400)
            # v3.4：只能认领「本人所在部门」任务池中的任务（与角标口径一致）
            my_org = s.get("org_id")
            if s["role"] != "superadmin" and my_org not in my_depts:
                return self._json({"error": "只能认领本部门待认领的任务"}, 403)

            new_depts = self._normalize_int_list(body.get("dept_ids"))
            new_asg   = self._normalize_int_list(body.get("assignee_ids"))

            # ---- 认领时继续向下分发 ----
            if new_depts:
                if s["role"] not in ("admin", "superadmin") and "task.assign" not in perms:
                    return self._json({"error": "没有分发任务的权限（需要: task.assign）"}, 403)
                ok, err = self._check_dept_dispatch(conn, new_depts)
                if not ok:
                    return self._json({"error": err}, 403)
                self._sync_task_departments(conn, int(tid), new_depts)

            # ---- 认领时直接指派责任人 ----
            if new_asg:
                if not is_mgr:
                    return self._json({"error": "没有指派权限，只能认领给自己"}, 403)
                ok, err = self._check_assignee_scope(conn, new_asg)
                if not ok:
                    return self._json({"error": err}, 403)
                self._sync_task_assignees(conn, int(tid), new_asg)

            # 状态统一推进为「进行中」，认领人自动关注
            conn.execute(
                "UPDATE tasks SET status=CASE WHEN status='TODO' THEN 'DOING' ELSE status END,"
                " updated_at=datetime('now','localtime') WHERE id=?", (tid,))
            conn.execute("INSERT OR IGNORE INTO task_followers(task_id,user_id) VALUES(?,?)",
                         (tid, s["user_id"]))

            # ---- 未选择去向 → 认领给自己 ----
            if not new_depts and not new_asg:
                conn.execute("UPDATE tasks SET assignee_id=? WHERE id=?", (s["user_id"], tid))
                conn.execute("INSERT OR IGNORE INTO task_assignees(task_id,user_id) VALUES(?,?)",
                             (tid, s["user_id"]))

            self._audit("CLAIM", "tasks", int(tid))
        finally:
            conn.close()
        return self._json({"ok": True})

    def _gen_task_no(self, conn):
        """生成任务编号：T + 年月日 + 4位序号"""
        today = time.strftime("%Y%m%d")
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM tasks WHERE task_no LIKE ?",
            ("T" + today + "%",)).fetchone()
        seq = (row["cnt"] if row else 0) + 1
        return "T%s%04d" % (today, seq)

    # ============================================================
    #  v3.3 多部门 / 多责任人工具
    # ============================================================
    def _normalize_int_list(self, val):
        """把 [1,2] / "1,2" / 1 统一成去重后的 int 列表"""
        if val is None or val == "":
            return []
        if isinstance(val, (list, tuple, set)):
            items = list(val)
        else:
            items = str(val).split(",")
        out = []
        for x in items:
            try:
                n = int(x)
            except (TypeError, ValueError):
                continue
            if n > 0 and n not in out:
                out.append(n)
        return out

    def _check_assignee_scope(self, conn, user_ids):
        """校验指派范围：非超管/管理员只能指派给本部门及下级部门内的人员

        v3.4：班组长等岗位型管理者只能把任务指派给自己科室/班组的成员，
        不能跨部门随意指派。
        """
        s = self._session
        if s["role"] in ("admin", "superadmin"):
            return True, None
        ids = set(self._dept_scope_ids(conn))
        if not ids:
            return False, "当前账号未设置所属部门，无法指派责任人"
        try:
            ph = ",".join("?" * len(user_ids))
            rows = conn.execute(
                "SELECT id, org_id, display_name FROM users WHERE id IN (%s)" % ph,
                user_ids).fetchall()
        except Exception:
            return True, None
        bad = [r["display_name"] or ("用户#%s" % r["id"]) for r in rows
               if r["org_id"] not in ids]
        if bad:
            return False, ("只能指派给本部门及下级部门的人员：%s" % "、".join(bad))
        if len(rows) != len(set(int(u) for u in user_ids)):
            return False, "指派对象中存在无效人员"
        return True, None

    def _check_dept_dispatch(self, conn, dept_ids):
        """校验分发层级：非 superadmin 只能分发给本人部门的**直接下级**部门

        返回 (ok, error_message)
        """
        s = self._session
        if s["role"] == "superadmin":
            return True, None
        allowed = set(self._direct_child_dept_ids(conn))
        if not allowed:
            return False, ("当前部门没有下级部门，无法分发任务。"
                           "请先在「组织架构」中创建下级部门")
        bad = [d for d in dept_ids if d not in allowed]
        if bad:
            try:
                ph = ",".join("?" * len(bad))
                names = [r["name"] for r in conn.execute(
                    "SELECT name FROM organizations WHERE id IN (%s)" % ph,
                    bad).fetchall()]
            except Exception:
                names = []
            label = "、".join(names) if names else "、".join(str(b) for b in bad)
            return False, ("只能将任务分发给本部门的直接下级部门，"
                           "不能跨层级分发：%s" % label)
        return True, None

    def _sync_task_departments(self, conn, task_id, dept_ids):
        """全量同步任务的分发部门（先删后插），并维护 tasks.dept_id 主部门"""
        try:
            conn.execute("DELETE FROM task_departments WHERE task_id=?", (task_id,))
            for d in dept_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO task_departments(task_id,dept_id) VALUES(?,?)",
                    (task_id, d))
            conn.execute("UPDATE tasks SET dept_id=? WHERE id=?",
                         (dept_ids[0] if dept_ids else None, task_id))
        except Exception as e:
            log("[TASK] 同步分发部门失败: %s" % e)

    def _sync_task_assignees(self, conn, task_id, user_ids):
        """全量同步任务的责任人（先删后插），并维护 tasks.assignee_id 主责任人"""
        try:
            conn.execute("DELETE FROM task_assignees WHERE task_id=?", (task_id,))
            for u in user_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO task_assignees(task_id,user_id) VALUES(?,?)",
                    (task_id, u))
                conn.execute(
                    "INSERT OR IGNORE INTO task_followers(task_id,user_id) VALUES(?,?)",
                    (task_id, u))
            conn.execute("UPDATE tasks SET assignee_id=? WHERE id=?",
                         (user_ids[0] if user_ids else None, task_id))
        except Exception as e:
            log("[TASK] 同步责任人失败: %s" % e)

    @require_auth
    def _api_tasks_create(self, body):
        if not (body.get("title") or "").strip():
            return self._json({"error": "任务标题不能为空"}, 400)
        conn = get_db()
        try:
            s = self._session
            perms = getattr(self, "_perms", set())
            # v3.4：创建任务需要 task.create 权限（岗位可精确控制）
            if s["role"] not in ("admin", "superadmin") and "task.create" not in perms:
                return self._json({"error": "没有创建任务的权限（需要: task.create）"}, 403)

            # --- v3.3 解析多部门 / 多责任人 ---
            dept_ids = self._normalize_int_list(body.get("dept_ids"))
            if not dept_ids:
                # 兼容旧的单值字段
                dept_ids = self._normalize_int_list(body.get("dept_id"))
            assignee_ids = self._normalize_int_list(body.get("assignee_ids"))
            if not assignee_ids:
                assignee_ids = self._normalize_int_list(body.get("assignee_id"))

            # --- 分发层级校验：不能穿透多层级 ---
            if dept_ids:
                ok, err = self._check_dept_dispatch(conn, dept_ids)
                if not ok:
                    return self._json({"error": err}, 403)

            task_no = self._gen_task_no(conn)

            # 解析周期性规则
            cycle_rule = None
            next_trigger = None
            task_type = body.get("task_type", "NORMAL")
            if task_type == "CYCLIC" and body.get("cycle_rule"):
                cycle_rule = json.dumps(body.get("cycle_rule"), ensure_ascii=False)
                # 计算下次触发时间（简单逻辑，实际可扩展）
                next_trigger = body.get("next_trigger") or ""

            # 主部门 / 主责任人写入单值字段，保持向后兼容
            main_dept = dept_ids[0] if dept_ids else None
            main_asg  = assignee_ids[0] if assignee_ids else None

            cur = conn.execute(
                "INSERT INTO tasks(task_no,title,description,task_type,category,source,"
                "  status,priority,assignee_id,dept_id,created_by,org_id,tags,"
                "  due_date,estimated_hours,cycle_rule,next_trigger,parent_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_no, body.get("title","").strip(), body.get("description",""),
                 task_type, body.get("category","日常任务"), body.get("source",""),
                 body.get("status","TODO"), body.get("priority","M"),
                 main_asg, main_dept,
                 s["user_id"], body.get("org_id", s.get("org_id") or 1),
                 json.dumps(body.get("tags",[]), ensure_ascii=False),
                 body.get("due_date"), body.get("estimated_hours"),
                 cycle_rule, next_trigger, body.get("parent_id")))
            task_id = int(cur.lastrowid)

            # --- 写入多对多关联表 ---
            if dept_ids:
                self._sync_task_departments(conn, task_id, dept_ids)
            if assignee_ids:
                self._sync_task_assignees(conn, task_id, assignee_ids)

            self._audit("CREATE", "tasks", task_id, body)
        finally:
            conn.close()
        return self._json({"ok": True, "id": task_id, "task_no": task_no,
                           "dept_ids": dept_ids, "assignee_ids": assignee_ids}, 201)

    @require_auth
    def _api_tasks_update(self, tid, body):
        conn = get_db()
        try:
            s = self._session
            perms = getattr(self, "_perms", set())
            is_mgr = s["role"] in ("admin", "superadmin") or "task.assign" in perms

            # 指派权限：管理员或具备 task.assign 的可指派；普通用户只能认领给自己
            if "assignee_ids" in body or "assignee_id" in body:
                new_list = (self._normalize_int_list(body.get("assignee_ids"))
                            or self._normalize_int_list(body.get("assignee_id")))
                if not is_mgr:
                    if not new_list or any(int(u) != int(s["user_id"]) for u in new_list):
                        return self._json({"error": "没有指派权限，只能认领给自己"}, 403)
                elif new_list and s["role"] not in ("admin", "superadmin"):
                    # 岗位型管理者（班组长/科室负责人等）只能指派本部门及下级人员
                    ok, err = self._check_assignee_scope(conn, new_list)
                    if not ok:
                        return self._json({"error": err}, 403)

            # --- v3.3 多部门更新：同步层级校验 ---
            dept_ids = None
            if "dept_ids" in body or "dept_id" in body:
                dept_ids = (self._normalize_int_list(body.get("dept_ids"))
                            or self._normalize_int_list(body.get("dept_id")))
                # v3.4：仅当分发部门**确实发生变化**时才校验权限与层级，
                # 避免只改标题/状态时被误判为一次分发操作而报 403
                try:
                    cur_ids = set()
                    crow = conn.execute("SELECT dept_id FROM tasks WHERE id=?", (tid,)).fetchone()
                    if crow and crow["dept_id"]:
                        cur_ids.add(int(crow["dept_id"]))
                    for r in conn.execute(
                            "SELECT dept_id FROM task_departments WHERE task_id=?", (tid,)).fetchall():
                        if r["dept_id"]:
                            cur_ids.add(int(r["dept_id"]))
                except Exception:
                    cur_ids = None
                if cur_ids is None or set(int(d) for d in dept_ids) != cur_ids:
                    if s["role"] not in ("admin", "superadmin") and "task.assign" not in perms:
                        return self._json({"error": "没有分发任务的权限（需要: task.assign）"}, 403)
                    if dept_ids:
                        ok, err = self._check_dept_dispatch(conn, dept_ids)
                        if not ok:
                            return self._json({"error": err}, 403)

            assignee_ids = None
            if "assignee_ids" in body or "assignee_id" in body:
                assignee_ids = (self._normalize_int_list(body.get("assignee_ids"))
                                or self._normalize_int_list(body.get("assignee_id")))

            fields = ["title", "description", "status", "priority",
                      "tags", "due_date", "org_id", "estimated_hours",
                      "actual_hours", "task_type", "category", "source", "parent_id",
                      "cycle_rule", "next_trigger"]
            sets, vals = [], []
            for k in fields:
                if k in body:
                    v = body[k]
                    if k == "tags" or k == "cycle_rule":
                        v = json.dumps(v, ensure_ascii=False) if v else None
                    sets.append("%s=?" % k)
                    vals.append(v)

            # 只有单值字段变化时才更新主字段（避免与多对多同步冲突）
            if dept_ids is None and "dept_id" in body:
                sets.append("dept_id=?")
                vals.append(body.get("dept_id"))
            if assignee_ids is None and "assignee_id" in body:
                sets.append("assignee_id=?")
                vals.append(body.get("assignee_id"))

            if sets:
                sets.append("updated_at=datetime('now','localtime')")
                vals.append(tid)
                conn.execute("UPDATE tasks SET " + ",".join(sets) + " WHERE id=?", vals)
            elif dept_ids is None and assignee_ids is None:
                return self._json({"error": "没有需要更新的字段"}, 400)

            # --- 同步多对多关联表 ---
            if dept_ids is not None:
                self._sync_task_departments(conn, int(tid), dept_ids)
            if assignee_ids is not None:
                self._sync_task_assignees(conn, int(tid), assignee_ids)

            # 任务完成时，触发关联任务的提醒
            if body.get("status") == "DONE":
                self._trigger_relations(conn, int(tid))

            self._audit("UPDATE", "tasks", int(tid) if str(tid).isdigit() else 0, body)
        finally:
            conn.close()
        return self._json({"ok": True})

    def _trigger_relations(self, conn, task_id):
        """触发任务关联关系"""
        rows = conn.execute(
            "SELECT * FROM task_relations WHERE task_id=? AND is_active=1 AND condition='COMPLETED'",
            (task_id,)).fetchall()
        for r in rows:
            notify_users = []
            if r["notify_users"]:
                try:
                    notify_users = json.loads(r["notify_users"])
                except Exception:
                    pass
            log("[INFO] 任务 %d 完成，触发关联任务 %d（%s），通知: %s" % (
                task_id, r["related_id"], r["relation_type"], notify_users))

    @require_auth
    def _api_tasks_delete(self, tid):
        conn = get_db()
        try:
            conn.execute("DELETE FROM tasks WHERE id=?", (tid,))
            self._audit("DELETE", "tasks", int(tid) if str(tid).isdigit() else 0)
        finally:
            conn.close()
        return self._json({"ok": True})

    # ============================================================
    #  任务进度（子任务汇总）
    # ============================================================
    @require_auth
    def _api_task_progress(self, tid):
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM tasks WHERE parent_id=?", (tid,)).fetchone()
            done = conn.execute(
                "SELECT COUNT(*) AS done FROM tasks WHERE parent_id=? AND status IN ('DONE','CLOSED')",
                (tid,)).fetchone()
            subtasks = conn.execute(
                "SELECT id, task_no, title, status, assignee_name"
                " FROM tasks t LEFT JOIN users u ON t.assignee_id=u.id"
                " WHERE parent_id=? ORDER BY status, updated_at DESC", (tid,)).fetchall()
        finally:
            conn.close()
        total = row["total"] if row else 0
        done_c = done["done"] if done else 0
        return self._json({"ok": True, "progress": {
            "total": total, "done": done_c,
            "percent": round(done_c / total * 100, 1) if total > 0 else 0,
            "subtasks": [dict(r) for r in subtasks],
        }})

    # ============================================================
    #  任务关联
    # ============================================================
    @require_auth
    def _api_relations_list(self, tid):
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT r.*, t.task_no AS related_no, t.title AS related_title, t.status AS related_status"
                " FROM task_relations r LEFT JOIN tasks t ON r.related_id=t.id"
                " WHERE r.task_id=?", (tid,)).fetchall()
        finally:
            conn.close()
        return self._json({"ok": True, "relations": [dict(r) for r in rows]})

    @require_auth
    def _api_relations_create(self, tid, body):
        related_id = body.get("related_id")
        if not related_id:
            return self._json({"error": "关联任务ID不能为空"}, 400)
        if str(related_id) == str(tid):
            return self._json({"error": "不能关联自身"}, 400)
        conn = get_db()
        try:
            cur = conn.execute(
                "INSERT INTO task_relations(task_id,related_id,relation_type,condition,notify_users,is_active)"
                " VALUES(?,?,?,?,?,?)",
                (tid, related_id, body.get("relation_type","TRIGGER"),
                 body.get("condition","COMPLETED"),
                 json.dumps(body.get("notify_users",[]), ensure_ascii=False),
                 1 if body.get("is_active", True) else 0))
        finally:
            conn.close()
        return self._json({"ok": True, "id": int(cur.lastrowid)}, 201)

    @require_auth
    def _api_relation_delete(self, rid):
        conn = get_db()
        try:
            conn.execute("DELETE FROM task_relations WHERE id=?", (rid,))
        finally:
            conn.close()
        return self._json({"ok": True})

    # ============================================================
    #  评论
    # ============================================================
    @require_auth
    def _api_comments_list(self, tid):
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT c.*, u.display_name AS user_name, u.avatar_color"
                " FROM task_comments c LEFT JOIN users u ON c.user_id = u.id"
                " WHERE c.task_id = ? ORDER BY c.created_at ASC", (tid,)).fetchall()
        finally:
            conn.close()
        return self._json({"ok": True, "comments": [dict(r) for r in rows]})

    @require_auth
    def _api_comments_create(self, tid, body):
        if not (body.get("content") or "").strip():
            return self._json({"error": "评论内容不能为空"}, 400)
        perms = getattr(self, "_perms", set())
        if (self._session["role"] not in ("admin", "superadmin")
                and "task.feedback" not in perms):
            return self._json({"error": "没有反馈任务进展的权限（需要: task.feedback）"}, 403)
        conn = get_db()
        try:
            cur = conn.execute(
                "INSERT INTO task_comments(task_id,user_id,content,timestamp,attach_urls)"
                " VALUES(?,?,?,datetime('now','localtime'),?)",
                (tid, self._session["user_id"], body.get("content","").strip(),
                 json.dumps(body.get("attach_urls",[]), ensure_ascii=False)))
            cid = cur.lastrowid

            # --- v3.3：通知负责部门的管理员（含上级分发部门），
            #     不再只通知第一个；多部门分发时每个部门的管理员都会收到 ---
            try:
                task_row = conn.execute(
                    "SELECT title, dept_id FROM tasks WHERE id=?", (tid,)).fetchone()
                # 汇总该任务的全部分发部门（主部门 + 关联部门）
                dept_ids = set()
                if task_row and task_row["dept_id"]:
                    dept_ids.add(task_row["dept_id"])
                for r in conn.execute(
                        "SELECT dept_id FROM task_departments WHERE task_id=?",
                        (tid,)).fetchall():
                    if r["dept_id"]:
                        dept_ids.add(r["dept_id"])
                # 兜底：没有任何分发部门时，用任务所属 org_id 推断
                if not dept_ids and task_row:
                    oid = conn.execute("SELECT org_id FROM tasks WHERE id=?",
                                       (tid,)).fetchone()
                    if oid and oid["org_id"]:
                        dept_ids.add(oid["org_id"])

                sender = conn.execute("SELECT display_name FROM users WHERE id=?",
                                      (self._session["user_id"],)).fetchone()
                sender_name = (sender["display_name"] or "") if sender else ""
                task_title = (task_row["title"] or "") if task_row else ""

                # 目标部门 = 分发部门 + 其**上级**部门（上级同样需要确认下属反馈）
                # 这样才能保证「下发任务的管理员」一定能在待确认页面看到反馈。
                notify_ids = set(dept_ids)
                try:
                    par_rows = conn.execute(
                        "SELECT id, parent_id FROM organizations").fetchall()
                    parent_of = dict((r["id"], r["parent_id"]) for r in par_rows)
                    for did in list(dept_ids):
                        seen = set()
                        cur = parent_of.get(did)
                        # 向上追溯最多 5 层，避免脏数据造成死循环
                        for _ in range(5):
                            if not cur or cur in seen or cur in notify_ids:
                                break
                            seen.add(cur)
                            notify_ids.add(cur)
                            cur = parent_of.get(cur)
                except Exception:
                    pass

                # v3.4：管理者岗位 = 拥有 task.view_all 的岗位
                #（处室负责人/计划工程师、科室负责人/计划工程师、班组长）
                # 这样「班组成员反馈后通知班组长和科室负责人」才能真正生效。
                mgr_role_ids = set()
                try:
                    for r in conn.execute(
                            "SELECT DISTINCT role_id FROM role_permissions"
                            " WHERE permission='task.view_all' AND role_id IS NOT NULL").fetchall():
                        if r["role_id"]:
                            mgr_role_ids.add(int(r["role_id"]))
                except Exception:
                    pass

                for did in notify_ids:
                    # 该部门下的全部管理者（管理员角色 / 管理者岗位），排除反馈者本人
                    if mgr_role_ids:
                        ph = ",".join("?" * len(mgr_role_ids))
                        heads = conn.execute(
                            "SELECT id FROM users WHERE org_id=?"
                            "   AND COALESCE(status,'active')='active'"
                            "   AND (role IN ('admin','superadmin') OR role_id IN (%s))"
                            "   AND id != ?" % ph,
                            [did] + sorted(mgr_role_ids) + [self._session["user_id"]]).fetchall()
                    else:
                        heads = conn.execute(
                            "SELECT id FROM users WHERE org_id=?"
                            "   AND COALESCE(status,'active')='active'"
                            "   AND (role_id IN (1,2) OR role IN ('admin','superadmin'))"
                            "   AND id != ?", (did, self._session["user_id"])).fetchall()
                    for h in heads:
                        conn.execute(
                            "INSERT OR IGNORE INTO pending_confirmations"
                            "(comment_id,dept_id,dept_head_uid,task_id,task_title,sender_name)"
                            " VALUES(?,?,?,?,?,?)",
                            (cid, did, h["id"], tid, task_title, sender_name))
            except Exception as e:
                log("[COMMENT] 待确认通知写入失败（不影响反馈提交）: %s" % e)
            row = conn.execute(
                "SELECT c.*, u.display_name AS user_name, u.avatar_color"
                " FROM task_comments c LEFT JOIN users u ON c.user_id=u.id"
                " WHERE c.id=?", (cid,)).fetchone()
            self._audit("CREATE", "task_comments", int(cid), {"task_id": tid})
        finally:
            conn.close()
        return self._json({"ok": True, "comment": dict(row) if row else None}, 201)

    @require_auth
    def _api_comment_delete(self, cid):
        conn = get_db()
        try:
            conn.execute("DELETE FROM task_comments WHERE id=? AND user_id=?",
                         (cid, self._session["user_id"]))
        finally:
            conn.close()
        return self._json({"ok": True})

    # ============================================================
    #  部门负责人确认反馈
    # ============================================================
    @require_auth
    def _api_confirmations_pending(self, uid=None):
        """返回待确认反馈列表

        v3.3 规则：
          - 超级管理员：可见全部部门的待确认反馈
          - 部门管理员：本部门及所有下级部门的待确认反馈
          - 普通用户：仅自己名下（被指派为部门负责人）的待确认反馈
        """
        s = self._session
        conn = get_db()
        try:
            base = ("SELECT pc.*, t.title AS task_title, t.task_no,"
                    "       tc.content AS comment_content, tc.timestamp AS comment_time,"
                    "       u.display_name AS sender_name, u.avatar_color AS sender_color,"
                    "       o.name AS dept_name"
                    " FROM pending_confirmations pc"
                    " JOIN tasks t ON pc.task_id=t.id"
                    " JOIN task_comments tc ON pc.comment_id=tc.id"
                    " LEFT JOIN users u ON tc.user_id=u.id"
                    " LEFT JOIN organizations o ON pc.dept_id=o.id")

            if s["role"] == "superadmin":
                rows = conn.execute(base + " ORDER BY pc.created_at DESC").fetchall()
            elif s["role"] == "admin":
                ids = self._dept_scope_ids(conn)
                if ids:
                    ph = ",".join("?" * len(ids))
                    rows = conn.execute(
                        base + " WHERE pc.dept_id IN (%s)"
                        " ORDER BY pc.created_at DESC" % ph, ids).fetchall()
                else:
                    rows = conn.execute(
                        base + " WHERE pc.dept_head_uid=?"
                        " ORDER BY pc.created_at DESC", (s["user_id"],)).fetchall()
            else:
                target_uid = uid if uid else s["user_id"]
                rows = conn.execute(
                    base + " WHERE pc.dept_head_uid=?"
                    " ORDER BY pc.created_at DESC", (target_uid,)).fetchall()
        finally:
            conn.close()
        return self._json({"ok": True, "confirmations": [dict(r) for r in rows]})

    @require_auth
    def _api_confirmation_confirm(self, pc_id):
        """部门负责人确认反馈"""
        s = self._session
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT pc.*, tc.task_id FROM pending_confirmations pc"
                " JOIN task_comments tc ON pc.comment_id=tc.id"
                " WHERE pc.id=?", (pc_id,)).fetchone()
            if not row:
                return self._json({"error": "该反馈已被确认或不存在"}, 404)

            # v3.3 权限：超管 / 负责部门的管理员 / 通知对象本人
            allowed = (s["role"] == "superadmin"
                       or str(row["dept_head_uid"]) == str(s["user_id"]))
            if not allowed and s["role"] == "admin":
                allowed = row["dept_id"] in set(self._dept_scope_ids(conn))
            if not allowed:
                return self._json({"error": "无权确认此通知"}, 403)

            conn.execute(
                "UPDATE task_comments SET confirmed_dept=1,confirmed_by=?,confirmed_at=datetime('now','localtime')"
                " WHERE id=?", (s["user_id"], row["comment_id"]))
            # 同一反馈可能通知了多个管理员，确认后一并清除
            conn.execute("DELETE FROM pending_confirmations WHERE comment_id=?",
                         (row["comment_id"],))
        finally:
            conn.close()
        return self._json({"ok": True})

    # ============================================================
    #  台账收集（v3.2.5）
    # ============================================================
    @require_auth
    def _api_ledger_templates_list(self):
        """列出所有模板"""
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT lt.*, u.display_name AS creator_name,"
                " (SELECT COUNT(*) FROM ledger_submissions ls WHERE ls.template_id=lt.id) AS submission_count"
                " FROM ledger_templates lt"
                " LEFT JOIN users u ON lt.created_by=u.id"
                " ORDER BY lt.created_at DESC").fetchall()
        finally:
            conn.close()
        return self._json({"ok": True, "templates": [dict(r) for r in rows]})

    @require_auth
    def _api_ledger_templates_create(self, body):
        """创建模板：{name, description, columns: [{title, key}...]}"""
        name = (body.get("name") or "").strip()
        columns = body.get("columns") or []
        if not name:
            return self._json({"error": "模板名称不能为空"}, 400)
        if not isinstance(columns, list) or not columns:
            return self._json({"error": "至少需要定义一列"}, 400)
        conn = get_db()
        try:
            cur = conn.execute(
                "INSERT INTO ledger_templates(name,description,columns,created_by)"
                " VALUES(?,?,?,?)",
                (name, (body.get("description") or "").strip(),
                 json.dumps(columns, ensure_ascii=False),
                 self._session["user_id"]))
            tpl_id = cur.lastrowid
        finally:
            conn.close()
        return self._json({"ok": True, "template_id": tpl_id}, 201)

    def _api_ledger_template_download(self, body):
        """下载模板 Excel：{template_id}"""
        tpl_id = body.get("template_id")
        if not tpl_id:
            return self._json({"error": "缺少 template_id"}, 400)
        conn = get_db()
        try:
            row = conn.execute("SELECT * FROM ledger_templates WHERE id=?", (tpl_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            return self._json({"error": "模板不存在"}, 404)
        columns = json.loads(row["columns"] or "[]")
        headers = [c.get("title", c.get("key", "")) for c in columns]
        headers.append("提交人")
        try:
            data = excelutil.write_xlsx([headers], sheet_name=row["name"])
        except Exception as e:
            return self._json({"error": "生成 Excel 失败: " + str(e)}, 500)
        self._send(data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                   filename=("台账模板_" + row["name"] + ".xlsx").encode("utf-8"))

    @require_auth
    def _api_ledger_submissions_list(self, template_id):
        """列出某模板的所有提交"""
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT * FROM ledger_submissions WHERE template_id=? ORDER BY submitted_at DESC",
                (template_id,)).fetchall()
        finally:
            conn.close()
        return self._json({"ok": True, "submissions": [dict(r) for r in rows]})

    def _api_ledger_submission_create(self, template_id, body):
        """导入提交：{file: base64, submitter}"""
        submitter = (body.get("submitter") or "").strip()
        if not submitter:
            return self._json({"error": "提交人姓名不能为空"}, 400)
        file_b64 = body.get("file")
        if not file_b64:
            return self._json({"error": "缺少文件内容"}, 400)
        import base64
        raw = base64.b64decode(file_b64)
        conn = get_db()
        try:
            row = conn.execute("SELECT * FROM ledger_templates WHERE id=?", (template_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            return self._json({"error": "模板不存在"}, 404)
        # 解析 Excel
        try:
            parsed = excelutil.read_xlsx(raw)
        except Exception as e:
            return self._json({"error": "解析 Excel 失败: " + str(e)}, 400)
        conn = get_db()
        try:
            cur = conn.execute(
                "INSERT INTO ledger_submissions(template_id,submitter,data) VALUES(?,?,?)",
                (template_id, submitter, json.dumps(parsed, ensure_ascii=False)))
            sub_id = cur.lastrowid
        finally:
            conn.close()
        return self._json({"ok": True, "submission_id": sub_id, "rows": len(parsed) - 1}, 201)

    # ============================================================
    #  组织架构
    # ============================================================
    @require_auth
    def _api_orgs_list(self):
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT o.*, p.name AS parent_name,"
                " (SELECT COUNT(*) FROM users u WHERE u.org_id=o.id) AS user_count"
                " FROM organizations o LEFT JOIN organizations p ON o.parent_id=p.id"
                " ORDER BY o.sort_order, o.id").fetchall()
        finally:
            conn.close()
        return self._json({"ok": True, "orgs": [dict(r) for r in rows]})

    @require_auth
    def _api_orgs_assignable(self):
        """返回当前用户可分发任务的部门列表

        v3.3 分发层级规则：一级只能分发给**直接下级**部门，不能穿透多层级。
          - superadmin              → 全部部门（便于集中调度）
          - admin / 普通用户        → 本人所在部门的**直接下级**部门
          - 附带返回 my_org（用于前端展示「本部门」标识）
        """
        conn = get_db()
        try:
            s = self._session
            my_org_id = s.get("org_id") or 1
            all_orgs = conn.execute(
                "SELECT id, name, parent_id FROM organizations ORDER BY sort_order, id"
            ).fetchall()

            if s["role"] == "superadmin":
                rows = list(all_orgs)
            else:
                # 只取直接下级部门，不穿透多层级
                rows = [r for r in all_orgs if r["parent_id"] == my_org_id]

            my_org = None
            for r in all_orgs:
                if r["id"] == my_org_id:
                    my_org = dict(r)
                    break

            return self._json({
                "ok": True,
                "orgs": [dict(r) for r in rows],
                "my_org": my_org,
                "is_superadmin": s["role"] == "superadmin",
            })
        finally:
            conn.close()

    @require_admin
    def _api_orgs_create(self, body):
        if not (body.get("name") or "").strip():
            return self._json({"error": "组织名称不能为空"}, 400)
        conn = get_db()
        try:
            cur = conn.execute(
                "INSERT INTO organizations(name,parent_id,sort_order,description)"
                " VALUES(?,?,?,?)",
                (body["name"].strip(), body.get("parent_id"),
                 body.get("sort_order", 0), body.get("description", "")))
        finally:
            conn.close()
        return self._json({"ok": True, "id": int(cur.lastrowid)}, 201)

    @require_admin
    def _api_orgs_update(self, oid, body):
        conn = get_db()
        try:
            fields = ["name", "parent_id", "sort_order", "description"]
            sets = ["%s=?" % k for k in fields if k in body]
            if not sets:
                return self._json({"ok": True})
            sets.append("updated_at=datetime('now','localtime')")
            vals = [body[k] for k in fields if k in body] + [oid]
            conn.execute("UPDATE organizations SET " + ",".join(sets) + " WHERE id=?", vals)
        finally:
            conn.close()
        return self._json({"ok": True})

    @require_admin
    def _api_orgs_delete(self, oid):
        conn = get_db()
        try:
            cnt = conn.execute("SELECT COUNT(*) AS cnt FROM users WHERE org_id=?",
                               (oid,)).fetchone()["cnt"]
            if cnt > 0:
                return self._json({"error": "该组织下还有 %d 个用户，无法删除" % cnt}, 400)
            conn.execute("DELETE FROM organizations WHERE id=?", (oid,))
        finally:
            conn.close()
        return self._json({"ok": True})

    # ============================================================
    #  岗位（角色）
    # ============================================================
    @require_admin
    def _api_roles_list(self):
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT r.*,"
                " (SELECT COUNT(*) FROM users u WHERE u.role_id=r.id) AS user_count,"
                # v3.4 修复：roles 表本身没有 permissions 列，之前直接 SELECT r.*
                # 导致前端读不到已保存的权限（保存后"查看不了权限分配情况"）
                " (SELECT GROUP_CONCAT(rp.permission) FROM role_permissions rp"
                "   WHERE rp.role_id=r.id) AS permissions"
                " FROM roles r ORDER BY r.is_system DESC, r.id").fetchall()
        finally:
            conn.close()
        roles = []
        for r in rows:
            d = dict(r)
            d["permissions"] = [p for p in str(d.get("permissions") or "").split(",") if p]
            d["perm_count"]  = len(d["permissions"])
            roles.append(d)
        return self._json({"ok": True, "roles": roles})

    @require_admin
    def _api_roles_create(self, body):
        name = (body.get("name") or "").strip()
        if not name:
            return self._json({"error": "岗位名称不能为空"}, 400)
        conn = get_db()
        try:
            cur = conn.execute(
                "INSERT INTO roles(name,description,is_system) VALUES(?,?,0)",
                (name, body.get("description", "")))
            role_id = int(cur.lastrowid)
            # 保存初始权限
            perms = body.get("permissions", [])
            for p in perms:
                conn.execute(
                    "INSERT OR IGNORE INTO role_permissions(role_id,permission) VALUES(?,?)",
                    (role_id, p))
        finally:
            conn.close()
        return self._json({"ok": True, "id": role_id}, 201)

    @require_admin
    def _api_roles_update(self, rid, body):
        conn = get_db()
        try:
            sets, vals = [], []
            for k in ("name", "description"):
                if k in body:
                    sets.append("%s=?" % k)
                    vals.append(body[k])
            if sets:
                vals.append(rid)
                conn.execute("UPDATE roles SET " + ",".join(sets) + " WHERE id=? AND is_system=0", vals)
        finally:
            conn.close()
        return self._json({"ok": True})

    @require_admin
    def _api_roles_delete(self, rid):
        conn = get_db()
        try:
            cnt = conn.execute(
                "SELECT COUNT(*) AS cnt FROM users WHERE role_id=?", (rid,)).fetchone()["cnt"]
            if cnt > 0:
                return self._json({"error": "该岗位下还有 %d 个用户，无法删除" % cnt}, 400)
            row = conn.execute("SELECT is_system FROM roles WHERE id=?", (rid,)).fetchone()
            if row and row["is_system"]:
                return self._json({"error": "系统内置岗位不可删除"}, 400)
            conn.execute("DELETE FROM roles WHERE id=? AND is_system=0", (rid,))
        finally:
            conn.close()
        return self._json({"ok": True})

    @require_admin
    def _api_role_permissions_get(self, rid):
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT permission FROM role_permissions WHERE role_id=?", (rid,)).fetchall()
        finally:
            conn.close()
        return self._json({"ok": True, "permissions": [r["permission"] for r in rows]})

    @require_admin
    def _api_role_permissions_update(self, rid, body):
        conn = get_db()
        try:
            row = conn.execute("SELECT is_system FROM roles WHERE id=?", (rid,)).fetchone()
            if not row:
                return self._json({"error": "岗位不存在"}, 404)
            perms = body.get("permissions", [])
            conn.execute("DELETE FROM role_permissions WHERE role_id=?", (rid,))
            for p in perms:
                if p in _ALL_PERMISSIONS:
                    conn.execute(
                        "INSERT INTO role_permissions(role_id,permission) VALUES(?,?)",
                        (rid, p))
        finally:
            conn.close()
        return self._json({"ok": True})

    # ============================================================
    #  用户
    # ============================================================
    @require_admin
    def _api_users_list(self):
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT u.id,u.username,u.display_name,u.org_id,u.role,u.role_id,u.status,"
                "       u.phone,u.email,u.avatar_color,u.created_at,o.name AS org_name,"
                "       r.name AS role_name"
                " FROM users u"
                " LEFT JOIN organizations o ON u.org_id=o.id"
                " LEFT JOIN roles r ON u.role_id=r.id"
                " ORDER BY u.id").fetchall()
        finally:
            conn.close()
        return self._json({"ok": True, "users": [dict(r) for r in rows]})

    @require_auth
    def _api_users_assignable(self):
        """可选责任人列表（供岗位型管理者指派任务使用）

        v3.4：班组长 / 科室负责人等岗位型管理者不是系统 admin，
        但需要给自己管辖范围内的人员指派任务。
        返回范围：超管/管理员 → 全部；其它 → 本部门及**所有下级部门**的人员。
        """
        s = self._session
        conn = get_db()
        try:
            if s["role"] in ("admin", "superadmin"):
                ids = None
            else:
                ids = self._dept_scope_ids(conn)
            sql = ("SELECT u.id,u.username,u.display_name,u.org_id,u.role,u.role_id,u.status,"
                   "       o.name AS org_name, r.name AS role_name"
                   " FROM users u"
                   " LEFT JOIN organizations o ON u.org_id=o.id"
                   " LEFT JOIN roles r ON u.role_id=r.id"
                   " WHERE COALESCE(u.status,'active')!='disabled'")
            params = []
            if ids:
                sql += " AND u.org_id IN (%s)" % ",".join("?" * len(ids))
                params = list(ids)
            elif ids is not None:
                # 没有部门信息 → 只能看到自己，避免越权
                sql += " AND u.id = ?"
                params = [s["user_id"]]
            rows = conn.execute(sql + " ORDER BY u.id", params).fetchall()
        finally:
            conn.close()
        return self._json({"ok": True, "users": [dict(r) for r in rows]})

    @require_admin
    def _api_users_create(self, body):
        username = (body.get("username") or "").strip()
        if not username:
            return self._json({"error": "登录名不能为空"}, 400)
        pw = body.get("password") or "123456"
        conn = get_db()
        try:
            cur = conn.execute(
                "INSERT INTO users(username,display_name,password,org_id,role,role_id,status,phone,email)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (username, body.get("display_name") or username,
                 hashlib.sha256(pw.encode("utf-8")).hexdigest(),
                 body.get("org_id", self._session.get("org_id") or 1),
                 body.get("role", "user"), body.get("role_id"),
                 body.get("status", "active"),
                 body.get("phone", ""), body.get("email", "")))
            uid = int(cur.lastrowid)
            self._audit("CREATE", "users", uid, {"username": username})
        except sqlite3.IntegrityError:
            return self._json({"error": "登录名已存在"}, 400)
        finally:
            conn.close()
        return self._json({"ok": True, "id": uid, "default_password": pw}, 201)

    @require_admin
    def _api_users_update(self, uid, body):
        conn = get_db()
        try:
            fields = ["display_name", "org_id", "role", "role_id", "status", "phone", "email"]
            sets, vals = [], []
            for k in fields:
                if k in body:
                    sets.append("%s=?" % k)
                    vals.append(body[k])
            if body.get("password"):
                sets.append("password=?")
                vals.append(hashlib.sha256(body["password"].encode("utf-8")).hexdigest())
            if not sets:
                return self._json({"ok": True})
            sets.append("updated_at=datetime('now','localtime')")
            conn.execute("UPDATE users SET " + ",".join(sets) + " WHERE id=?", vals + [uid])
            self._audit("UPDATE", "users", int(uid) if str(uid).isdigit() else 0, body)
        finally:
            conn.close()
        return self._json({"ok": True})

    @require_admin
    def _api_users_delete(self, uid):
        if str(uid) == str(self._session["user_id"]):
            return self._json({"error": "不能删除自己"}, 400)
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT id, username, display_name, role FROM users WHERE id=?",
                (uid,)).fetchone()
            if not row:
                return self._json({"error": "用户不存在"}, 404)
            # 保护超级管理员账号，避免误删导致系统失管
            if row["role"] == "superadmin":
                return self._json({"error": "不能删除超级管理员账号"}, 403)
            # 保护最后一个可用的管理员
            if row["role"] == "admin":
                cnt = conn.execute(
                    "SELECT COUNT(*) AS c FROM users"
                    " WHERE role IN ('admin','superadmin') AND status='active'"
                    " AND id != ?", (uid,)).fetchone()
                if not cnt or not cnt["c"]:
                    return self._json({"error": "至少需要保留一名管理员"}, 400)
            conn.execute(
                "UPDATE users SET status='disabled', avatar_color=avatar_color,"
                " updated_at=datetime('now','localtime') WHERE id=?", (uid,))
            self._audit("DELETE", "users", int(uid) if str(uid).isdigit() else 0,
                        {"username": row["username"], "display_name": row["display_name"]})
        finally:
            conn.close()
        return self._json({"ok": True})

    @require_auth
    def _api_me(self):
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT u.id,u.username,u.display_name,u.org_id,u.role,u.role_id,u.status,"
                "       u.phone,u.email,u.avatar_color,o.name AS org_name,"
                "       r.name AS role_name"
                " FROM users u"
                " LEFT JOIN organizations o ON u.org_id=o.id"
                " LEFT JOIN roles r ON u.role_id=r.id"
                " WHERE u.id=?", (self._session["user_id"],)).fetchone()
            if row:
                d = dict(row)
                # 补充权限列表（系统角色 + 岗位权限）
                d["permissions"] = sorted(user_permissions(conn, row))
            else:
                d = None
        finally:
            conn.close()
        return self._json({"ok": True, "user": d})

    @require_auth
    def _api_me_update(self, body):
        conn = get_db()
        try:
            fields = ["display_name", "phone", "email"]
            sets, vals = [], []
            for k in fields:
                if k in body:
                    sets.append("%s=?" % k)
                    vals.append(body[k])
            if body.get("password"):
                sets.append("password=?")
                vals.append(hashlib.sha256(body["password"].encode("utf-8")).hexdigest())
            if not sets:
                return self._json({"ok": True})
            conn.execute("UPDATE users SET " + ",".join(sets) + " WHERE id=?",
                         vals + [self._session["user_id"]])
        finally:
            conn.close()
        return self._json({"ok": True})

    # ============================================================
    #  认证
    # ============================================================
    def _api_login(self, body):
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not username or not password:
            return self._json({"error": "用户名和密码不能为空"}, 400)
        pw_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE username=? AND password=? AND status='active'",
                (username, pw_hash)).fetchone()
        finally:
            conn.close()
        if not row:
            return self._json({"error": "用户名或密码错误"}, 401)
        token = session_create(dict(row))
        self._audit("LOGIN", "users", row["id"], {"username": username})
        return self._json({
            "ok": True, "token": token,
            "user": {"id": row["id"], "username": row["username"],
                     "display_name": row["display_name"], "role": row["role"],
                     "org_id": row["org_id"] if "org_id" in row.keys() else None,
                     "role_id": row["role_id"] if "role_id" in row.keys() else None}
        })

    def _api_register(self, body):
        if not ALLOW_REGISTER:
            return self._json({"error": "已关闭自助注册，请联系管理员开通账号"}, 403)
        username = (body.get("username") or "").strip()
        if not username:
            return self._json({"error": "登录名不能为空"}, 400)
        pw = body.get("password") or "123456"
        conn = get_db()
        try:
            org_id = body.get("org_id", 1)
            cur = conn.execute(
                "INSERT INTO users(username,display_name,password,org_id,role,status)"
                " VALUES(?,?,?,?,?,?)",
                (username, body.get("display_name") or username,
                 hashlib.sha256(pw.encode("utf-8")).hexdigest(), org_id, "user", "active"))
            uid = int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return self._json({"error": "登录名已存在"}, 400)
        finally:
            conn.close()
        token = session_create({"id": uid, "username": username,
                                "display_name": body.get("display_name") or username,
                                "role": "user", "org_id": body.get("org_id", 1),
                                "role_id": None})
        return self._json({"ok": True, "token": token,
                           "user": {"id": uid, "username": username,
                                    "display_name": body.get("display_name") or username,
                                    "role": "user"}}, 201)

    @require_auth
    def _api_logout(self):
        session_delete(self._get_token())
        return self._json({"ok": True})


# ============================================================
#  单实例检测
# ============================================================
def probe_running(port):
    import http.client
    for p in (port, port + 1):
        try:
            c = http.client.HTTPConnection("127.0.0.1", p, timeout=1)
            c.request("GET", "/api/ping")
            r = c.getresponse()
            if r.status == 200:
                data = json.loads(r.read().decode("utf-8", "replace"))
                c.close()
                if data.get("app") == APP_NAME:
                    return p, data.get("version", "?")
            c.close()
        except Exception:
            continue
    return None, None


def db_mode_label():
    if DB_MODE == "shared":
        return "共享盘多人协同（%s 日志，已关闭 WAL）" % DB_JOURNAL
    return "本机单机（%s 日志）" % DB_JOURNAL


def db_status():
    ok, reason = True, DB_LAST_ERROR
    if not DB_LAST_ERROR:
        ok, reason = check_db_location()
    return {
        "path":      DB_PATH,
        "mode":      DB_MODE,
        "shared":    NETWORK_DB,
        "journal":   DB_JOURNAL,
        "ready":     DB_READY,
        "reachable": ok,
        "error":     reason or "",
    }


def write_port_file(port, host):
    try:
        with open(os.path.join(exe_dir(), "port.txt"), "w", encoding="utf-8") as f:
            f.write(str(port))
    except Exception:
        pass


# ============================================================
#  自动更新（共享文件夹 → 本地替换 → 重启）
# ============================================================
def _ver_tuple(v):
    """把 '3.2.0' 转成 (3,2,0) 便于比较"""
    try:
        return tuple(int(x) for x in str(v).strip().lstrip("vV").split(".") if x != "")
    except Exception:
        return (0,)


def _ver_cmp(a, b):
    """a>b 返回 1，a<b 返回 -1，相等返回 0"""
    ta, tb = _ver_tuple(a), _ver_tuple(b)
    n = max(len(ta), len(tb))
    ta += (0,) * (n - len(ta))
    tb += (0,) * (n - len(tb))
    return (ta > tb) - (ta < tb)


def update_manifest_path():
    if not UPDATE_FOLDER:
        return None
    return os.path.join(UPDATE_FOLDER, UPDATE_MANIFEST)


def update_read_manifest():
    """读取共享文件夹里的更新清单"""
    if not UPDATE_ENABLED or not UPDATE_FOLDER:
        return None, "未启用自动更新"
    mp = update_manifest_path()
    if not mp or not os.path.isfile(mp):
        return None, "共享更新目录未找到清单文件"
    try:
        with open(mp, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        return None, "清单解析失败: %s" % e
    if not isinstance(raw, dict) or not raw.get("version"):
        return None, "清单缺少 version 字段"
    return raw, ""


def update_check(force=False):
    """检测是否有新版本，返回 (info_dict, error_str)"""
    now = time.time()
    if not force and UPDATE_STATE["available"] and (now - UPDATE_STATE["last_check"]) < 60:
        return UPDATE_STATE["available"], UPDATE_STATE["error"]
    UPDATE_STATE["last_check"] = now
    if not UPDATE_ENABLED:
        UPDATE_STATE.update({"available": None, "error": ""})
        return None, ""
    mf, err = update_read_manifest()
    if err:
        UPDATE_STATE.update({"available": None, "error": err})
        return None, err
    latest = str(mf.get("version"))
    if _ver_cmp(latest, VERSION) <= 0:
        info = {"has_update": False, "current": VERSION, "latest": latest}
        UPDATE_STATE.update({"available": info, "error": ""})
        return info, ""
    files, missing = [], []
    for rel in (mf.get("files") or []):
        src = os.path.join(UPDATE_FOLDER, rel.replace("/", os.sep))
        (files if os.path.isfile(src) else missing).append(rel)
    info = {
        "has_update":   True,
        "current":      VERSION,
        "latest":       latest,
        "files":        files,
        "missing":      missing,
        "changelog":    mf.get("changelog") or [],
        "release_date": mf.get("release_date") or "",
        "required":     bool(mf.get("required", False)),
        "ready":        not missing,
    }
    UPDATE_STATE.update({"available": info, "error": ""})
    return info, ""


def _update_cleanup_old():
    """清理上次更新残留的 .update-old / 旧备份目录"""
    try:
        for root, _dirs, names in os.walk(exe_dir()):
            for n in list(names):
                if n.endswith(".update-old"):
                    try:
                        os.remove(os.path.join(root, n))
                    except Exception:
                        pass
    except Exception:
        pass


def _update_startup_scan():
    """启动时后台扫一次更新"""
    try:
        _update_cleanup_old()
        info, err = update_check(force=True)
        if err:
            log("[UPDATE] 检测失败: %s" % err)
        elif info and info.get("has_update"):
            log("[UPDATE] 发现新版本 %s（当前 %s）" % (info["latest"], VERSION))
            if UPDATE_AUTO_INSTALL and info.get("ready"):
                log("[UPDATE] 已配置为自动安装，开始更新…")
                update_apply()
    except Exception as e:
        log("[UPDATE] 启动扫描异常: %s" % e)


def update_apply():
    """把共享文件夹里的更新文件替换到本地并重启，返回 (ok, message)"""
    if not UPDATE_ENABLED:
        return False, "未启用自动更新"
    info, err = update_check(force=True)
    if err:
        return False, err
    if not info or not info.get("has_update"):
        return False, "已是最新版本"
    if info.get("missing"):
        return False, "共享目录缺少文件: %s" % ", ".join(info["missing"])

    dest_root   = exe_dir()
    src_root    = UPDATE_FOLDER
    backup_root = os.path.join(dest_root, "update_backup")
    applied = []
    try:
        os.makedirs(backup_root, exist_ok=True)
        for rel in info["files"]:
            rel  = rel.replace("/", os.sep)
            src  = os.path.join(src_root, rel)
            dest = os.path.join(dest_root, rel)
            os.makedirs(os.path.dirname(dest) or dest_root, exist_ok=True)
            # 备份旧文件
            if os.path.isfile(dest):
                bak = os.path.join(backup_root, rel)
                os.makedirs(os.path.dirname(bak) or backup_root, exist_ok=True)
                try:
                    shutil.copy2(dest, bak)
                except Exception:
                    pass
            # Windows 不能直接覆盖正在运行的 exe：先改名（允许），再写入新文件
            old_marker = dest + ".update-old"
            if os.path.isfile(dest):
                try:
                    if os.path.isfile(old_marker):
                        os.remove(old_marker)
                    os.rename(dest, old_marker)
                except Exception:
                    pass
            shutil.copy2(src, dest)
            applied.append(rel)
    except Exception as e:
        log("[ERROR] 更新失败: %s" % e)
        return False, "更新写入失败: %s" % e

    # 记录版本，避免重启后重复提示
    try:
        with open(os.path.join(dest_root, "update_applied.txt"), "w", encoding="utf-8") as f:
            f.write(info["latest"])
    except Exception:
        pass
    log("[UPDATE] 已替换文件: %s" % ", ".join(applied))
    threading.Timer(1.5, _restart_self).start()
    return True, "已更新 %d 个文件，服务即将重启" % len(applied)


def _restart_self():
    """重启本程序（EXE 或脚本）"""
    try:
        if IS_FROZEN:
            args = [sys.executable] + list(sys.argv[1:])
        else:
            args = [sys.executable, os.path.abspath(sys.argv[0])] + list(sys.argv[1:])
        log("[UPDATE] 重启: %s" % " ".join(args))
        subprocess.Popen(args, cwd=exe_dir())
    except Exception as e:
        log("[ERROR] 重启失败: %s" % e)
        return
    time.sleep(0.4)
    os._exit(0)


# ============================================================
#  主程序
# ============================================================
def main():
    global _LOG_FILE, DB_READY, APP_TITLE, APP_SUBTITLE, DB_LAST_ERROR
    global SERVER_HOST, LAN_ACCESS, ALLOW_REGISTER
    global UPDATE_ENABLED, UPDATE_FOLDER, UPDATE_MANIFEST, UPDATE_INTERVAL, UPDATE_AUTO_INSTALL

    cfg  = load_config()
    args = parse_args(cfg)
    APP_TITLE    = str(cfg["app"].get("title") or DEFAULT_CONFIG["app"]["title"])
    APP_SUBTITLE = str(cfg["app"].get("subtitle") or DEFAULT_CONFIG["app"]["subtitle"])

    host = str(cfg["server"].get("host") or DEFAULT_HOST)
    LAN_ACCESS = bool(cfg["server"].get("lan_access"))
    if LAN_ACCESS and host in ("127.0.0.1", "localhost", "::1"):
        host = LAN_HOST
    if getattr(args, "lan", False):
        host = LAN_HOST
        LAN_ACCESS = True
    args.host = host
    SERVER_HOST = host
    ALLOW_REGISTER = bool(cfg["security"].get("allow_register", True))

    # 自动更新
    ucfg = cfg.get("update") or {}
    UPDATE_ENABLED      = bool(ucfg.get("enabled", False))
    UPDATE_FOLDER       = str(ucfg.get("folder") or "")
    UPDATE_MANIFEST     = str(ucfg.get("manifest") or "manifest.json")
    UPDATE_INTERVAL     = int(ucfg.get("check_interval") or 300)
    UPDATE_AUTO_INSTALL = bool(ucfg.get("auto_install", False))
    if UPDATE_ENABLED and UPDATE_FOLDER:
        threading.Thread(target=_update_startup_scan, daemon=True).start()

    # 日志
    try:
        log_dir = os.path.join(data_dir(), "logs")
        os.makedirs(log_dir, exist_ok=True)
        _LOG_FILE = os.path.join(log_dir, "server.log")
    except Exception:
        _LOG_FILE = None

    setup_db(cfg, args)

    if getattr(args, "show_config", False):
        print("")
        print("配置文件: %s" % (CONFIG_PATH or "(未找到，使用内置默认值)"))
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        print("解析结果: 数据库=%s  模式=%s" % (DB_PATH, DB_MODE))
        return 0

    print("")
    print("=" * 60)
    print("  %s  v%s" % (APP_TITLE, VERSION))
    print("  %s Server / Python %s / %s" % (APP_NAME, sys.version.split()[0], sys.platform))
    print("=" * 60)

    # ---- 共享盘预检 ----
    ok, reason = check_db_location()
    if not ok:
        DB_LAST_ERROR = reason
        print("")
        print("  [错误] 无法访问数据库目录")
        print("         %s" % reason)
        print("")
        print("  请检查：")
        print("    1. 电脑是否已连接到公司内网（能否打开共享文件夹）")
        print("    2. 共享文件夹地址是否正确（见 config.json 的 database.folder）")
        print("    3. 你是否拥有该共享文件夹的写入权限")
        print("")
        if cfg["database"].get("allow_local_fallback"):
            fallback = os.path.join(data_dir(), cfg["database"].get("file") or "tasks.db")
            print("  [提示] 已开启本地回退，改用本机数据库：%s" % fallback)
            print("        注意：本机数据与共享数据互不相通，联网后请手动合并。")
            print("")
            cfg["database"]["path"] = fallback
            setup_db(cfg, args)
        else:
            log("[ERROR] 数据库目录不可用: %s" % reason)
            return 3
    DB_LAST_ERROR = ""

    # 单实例检测
    running_port, running_ver = probe_running(args.port)
    if running_port:
        print("")
        print("  服务已经在运行（端口 %d，版本 %s）" % (running_port, running_ver))
        print("  地址: http://127.0.0.1:%d" % running_port)
        print("  已为你打开浏览器；本窗口不会再启动第二个服务。")
        print("")
        write_port_file(running_port, args.host)
        # 即使默认关闭浏览器，这里单实例也打开，让用户知道已运行
        if not args.no_browser:
            try:
                import webbrowser
                webbrowser.open("http://127.0.0.1:%d" % running_port)
            except Exception:
                pass
        return 0

    db_migrate()          # 先迁移旧数据库结构，兼容 v3.0 数据库
    DB_READY = db_init()
    if DB_READY:
        db_migrate()      # 首次初始化建表后再补一次，确保 v3.2 新表/新字段就位
    if not DB_READY:
        print("")
        print("  [提示] 数据库初始化未完成，服务仍会启动但功能可能异常。")
        print("         请检查数据库路径是否可写: %s" % DB_PATH)
        print("")

    backup_file = backup_db(cfg) if cfg["database"].get("backup_dir") else None

    port = pick_port(args.port, args.host)
    if port is None:
        print("")
        print("  [错误] 端口 %d ~ %d 全部被占用，无法启动。" % (args.port, args.port + PORT_TRIES - 1))
        print("         请用 --port 指定其它端口，或修改 config.json。")
        print("")
        return 2
    if port != args.port:
        print("  [提示] 端口 %d 已被占用，自动改用 %d" % (args.port, port))

    try:
        server = _BaseServer((args.host, port), Handler)
    except Exception as e:
        print("")
        print("  [错误] 服务启动失败: %s" % e)
        print("         常见原因：端口被占用、IP 地址不存在")
        print("")
        log("[ERROR] 绑定失败 %s:%s : %s" % (args.host, port, e))
        return 2

    server.daemon_threads = True
    url = "http://127.0.0.1:%d" % port

    write_port_file(port, args.host)

    print("")
    print("  服务已启动")
    print("  ------------------------------------------------------------")
    print("  本机访问   : %s" % url)
    if args.host in ("0.0.0.0", "", "::"):
        print("  监听地址   : 全部网卡（局域网可访问）")
        print("  局域网地址 : http://%s:%d" % (lan_ip(), port))
    else:
        print("  监听地址   : %s（仅本机回环，不对外提供访问）" % args.host)
    print("  数据库     : %s" % DB_PATH)
    print("  数据库模式 : %s" % db_mode_label())
    if backup_file:
        print("  数据备份   : %s" % backup_file)
    print("  静态资源   : %s" % STATIC_DIR)
    print("  日志文件   : %s" % (_LOG_FILE or "(未开启)"))
    print("  登录账号   : admin / admin123")
    print("  ------------------------------------------------------------")
    print("  关闭服务：在本窗口按 Ctrl+C，或直接关闭此窗口")
    print("")

    log("[OK] 服务启动 http://%s:%d  db=%s mode=%s" % (args.host, port, DB_PATH, DB_MODE))

    # 自动打开浏览器（默认已关闭，可在 config.json 中启用）
    if not args.no_browser:
        try:
            import webbrowser
            def _open():
                time.sleep(1.0)
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
            t = threading.Thread(target=_open, daemon=True)
            t.start()
        except Exception:
            pass

    sweeper = threading.Event()

    def _sweep_loop():
        while not sweeper.wait(300):
            try:
                session_sweep()
            except Exception:
                pass
    t = threading.Thread(target=_sweep_loop, daemon=True)
    t.start()

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("")
        print("  正在停止服务...")
    finally:
        sweeper.set()
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
        log("[OK] 服务已停止")
        print("  服务已停止。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        try:
            log("[FATAL] " + traceback.format_exc())
        except Exception:
            pass
        traceback.print_exc()
        if IS_FROZEN or os.name == "nt":
            try:
                input("发生错误，按回车键退出...")
            except Exception:
                pass
        sys.exit(1)

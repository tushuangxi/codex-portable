#!/usr/bin/env python3
"""
Codex CLI Portable — 配置中心 (Config Center)

A self-contained, dependency-free (stdlib-only) local web config panel,
styled consistently with the OpenClaw / Hermes / Claude portable config centers.

It reads and writes the cc-switch SQLite database at data/.cc-switch/cc-switch.db
AND the codex-native auth.json + config.toml at data/.codex/. Both stores stay
in sync so the launcher, cc-switch GUI, and this panel are interoperable.

Usage:
  python3 lib/config_server.py            # serve on 127.0.0.1:17590
"""
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
import urllib.request
import urllib.error
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PORTABLE_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "lib" else SCRIPT_DIR
DATA_DIR = PORTABLE_ROOT / "data"
CCS_DIR = DATA_DIR / ".cc-switch"
CCS_DB = CCS_DIR / "cc-switch.db"

PORT = 17590          # config-center port (distinct from cc-switch GUI)
APP_TYPE = "codex"    # which cc-switch app_type this panel manages

# ── Provider catalog ────────────────────────────────────────────────
# Codex CLI talks the OpenAI wire protocol (responses API or chat
# completions). Third-party providers must expose an OpenAI-compatible
# endpoint. The key is stored in auth.json as OPENAI_API_KEY, and the
# base_url + model go into config.toml.
PROVIDERS = [
    {"id": "openai", "name": "OpenAI 官方", "base_url": "https://api.openai.com/v1",
     "models": ["gpt-4.1", "gpt-4.1-mini", "o3", "o3-mini", "o4-mini"],
     "key_hint": "sk-...", "note": "官方直连，需要 OpenAI 账号"},
    {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
     "models": ["deepseek-chat", "deepseek-reasoner"],
     "key_hint": "sk-...", "note": "国产，性价比高，OpenAI 兼容"},
    {"id": "minimax", "name": "MiniMax (海螺)", "base_url": "https://api.minimaxi.com/v1",
     "models": ["MiniMax-M2.7", "MiniMax-M2.5"],
     "key_hint": "粘贴 MiniMax API Key", "note": "国产，OpenAI 兼容端点"},
    {"id": "zhipu", "name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "models": ["glm-4.6", "glm-4.5-air", "glm-4.5-flash"],
     "key_hint": "粘贴智谱 API Key", "note": "国产，OpenAI 兼容"},
    {"id": "kimi", "name": "Kimi / Moonshot", "base_url": "https://api.moonshot.cn/v1",
     "models": ["kimi-k2-thinking", "moonshot-v1-128k"],
     "key_hint": "sk-...", "note": "国产，长上下文"},
    {"id": "groq", "name": "Groq", "base_url": "https://api.groq.com/openai/v1",
     "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
     "key_hint": "gsk_...", "note": "超快推理，免费额度"},
    {"id": "custom", "name": "自定义 / 中转站", "base_url": "",
     "models": [], "custom": True,
     "key_hint": "粘贴中转站 API Key", "note": "填写中转站/自建网关的 base_url"},
]


# ═══════════════════════════════════════════════════════════════
#  cc-switch DB read / write
# ═══════════════════════════════════════════════════════════════
def _connect():
    CCS_DIR.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(CCS_DB), timeout=5.0)


def _ensure_schema(db):
    """Create the providers table if the DB is brand new, matching the
    cc-switch column layout so the GUI stays interoperable."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS providers (
            id TEXT NOT NULL,
            app_type TEXT NOT NULL,
            name TEXT NOT NULL,
            settings_config TEXT NOT NULL,
            website_url TEXT,
            category TEXT,
            created_at INTEGER,
            sort_index INTEGER,
            notes TEXT,
            icon TEXT,
            icon_color TEXT,
            meta TEXT NOT NULL DEFAULT '{}',
            is_current BOOLEAN NOT NULL DEFAULT 0,
            in_failover_queue BOOLEAN NOT NULL DEFAULT 0,
            PRIMARY KEY (id, app_type)
        )
    """)
    db.commit()


def read_current():
    """Return the currently-active claude provider as a dict, or None."""
    if not CCS_DB.exists():
        return None
    try:
        db = _connect()
        row = db.execute(
            "SELECT id, name, settings_config FROM providers "
            "WHERE app_type=? AND is_current=1 LIMIT 1", (APP_TYPE,)
        ).fetchone()
        db.close()
        if not row:
            return None
        cfg = json.loads(row[2])
        env = cfg.get("env", {})
        return {
            "id": row[0], "name": row[1],
            "base_url": (env.get("OPENAI_BASE_URL") or "").strip(),
            "api_key": (env.get("OPENAI_API_KEY") or "").strip(),
            "model": (env.get("CODEX_MODEL") or "").strip(),
        }
    except Exception:
        return None


def list_providers():
    out = []
    if not CCS_DB.exists():
        return out
    try:
        db = _connect()
        rows = db.execute(
            "SELECT id, name, is_current FROM providers WHERE app_type=? "
            "ORDER BY is_current DESC, name", (APP_TYPE,)
        ).fetchall()
        db.close()
        for r in rows:
            out.append({"id": r[0], "name": r[1], "active": bool(r[2])})
    except Exception:
        pass
    return out


def save_provider(name, base_url, api_key, model):
    """Insert/replace a provider row and mark it current. Writes the
    cc-switch settings_config shape for codex (OPENAI_API_KEY +
    OPENAI_BASE_URL), so the launcher and cc-switch GUI both read it."""
    base_url = (base_url or "").strip().rstrip("/")
    api_key = (api_key or "").strip()
    model = (model or "").strip()
    if not base_url or not api_key:
        raise ValueError("base_url 和 api_key 不能为空")

    env = {
        "OPENAI_BASE_URL": base_url,
        "OPENAI_API_KEY": api_key,
    }
    if model:
        env["CODEX_MODEL"] = model
    settings = {"env": env}
    meta = {"apiFormat": "openai"}

    db = _connect()
    _ensure_schema(db)
    pid = str(uuid.uuid4())
    db.execute("UPDATE providers SET is_current=0 WHERE app_type=?", (APP_TYPE,))
    db.execute(
        "INSERT INTO providers (id, app_type, name, settings_config, "
        "created_at, sort_index, meta, is_current) "
        "VALUES (?,?,?,?,?,?,?,1)",
        (pid, APP_TYPE, name or "Custom",
         json.dumps(settings, ensure_ascii=False),
         int(time.time() * 1000), 0, json.dumps(meta)),
    )
    db.commit()
    db.close()

    # Also write auth.json + config.toml for codex CLI direct consumption.
    # Codex reads CODEX_HOME/auth.json for the key and config.toml for
    # model/provider settings. The launcher sets CODEX_HOME=data/.codex.
    codex_dir = DATA_DIR / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    auth = {"OPENAI_API_KEY": api_key}
    _atomic_write(codex_dir / "auth.json",
                  json.dumps(auth, ensure_ascii=False, indent=2))
    if model or base_url != "https://api.openai.com/v1":
        toml_lines = []
        if base_url != "https://api.openai.com/v1":
            toml_lines.append('model_provider = "custom"')
            toml_lines.append(f'model = "{model or "gpt-4.1"}"')
            toml_lines.append("")
            toml_lines.append("[model_providers.custom]")
            toml_lines.append(f'name = "{name or "Custom"}"')
            toml_lines.append(f'base_url = "{base_url}"')
            toml_lines.append('wire_api = "responses"')
            toml_lines.append('env_key = "OPENAI_API_KEY"')
        else:
            toml_lines.append(f'model = "{model}"')
        _atomic_write(codex_dir / "config.toml", "\n".join(toml_lines) + "\n")

    return pid


def _atomic_write(path, content):
    """Write file atomically via tmp+rename."""
    from pathlib import Path
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    try:
        os.replace(str(tmp), str(p))
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        raise


def activate_provider(pid):
    db = _connect()
    db.execute("UPDATE providers SET is_current=0 WHERE app_type=?", (APP_TYPE,))
    db.execute("UPDATE providers SET is_current=1 WHERE id=? AND app_type=?",
               (pid, APP_TYPE))
    db.commit()
    db.close()


# ═══════════════════════════════════════════════════════════════
#  API key connectivity test
# ═══════════════════════════════════════════════════════════════
def test_key(base_url, api_key, model):
    """Minimal OpenAI /v1/models probe. Returns (ok, message).

    TLS resilience: portable Pythons sometimes ship without a usable
    system trust store. Try certifi first if available, then default."""
    import ssl
    base_url = (base_url or "").strip().rstrip("/")
    api_key = (api_key or "").strip()
    if not base_url or not api_key:
        return False, "缺少 base_url 或 api_key"
    url = base_url + "/models"
    req = urllib.request.Request(url, method="GET", headers={
        "authorization": f"Bearer {api_key}",
        "user-agent": "CodexPortable/ConfigCenter",
    })
    contexts = []
    try:
        import certifi  # type: ignore
        contexts.append(ssl.create_default_context(cafile=certifi.where()))
    except Exception:
        pass
    contexts.append(None)

    last_err = "无法连接"
    for ctx in contexts:
        try:
            kwargs = {"timeout": 15}
            if ctx is not None:
                kwargs["context"] = ctx
            with urllib.request.urlopen(req, **kwargs) as resp:
                if 200 <= resp.status < 300:
                    body = resp.read(2000).decode("utf-8", "replace")
                    count = ""
                    try:
                        data = json.loads(body)
                        if isinstance(data, dict) and "data" in data:
                            count = f" ({len(data['data'])} 个模型)"
                    except Exception:
                        pass
                    return True, f"连接成功{count}"
                return False, f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return False, "API Key 无效或无权限 (HTTP %d)" % e.code
            if e.code in (400, 404):
                return True, "端点可达 (HTTP %d)" % e.code
            try:
                detail = e.read(300).decode("utf-8", "replace")
            except Exception:
                detail = ""
            return False, f"HTTP {e.code} {detail[:120]}"
        except Exception as e:
            last_err = f"无法连接: {str(e)[:120]}"
            continue
    return False, last_err


# ═══════════════════════════════════════════════════════════════
#  Embedded UI (rich, tabbed, onboarding wizard). Styled to match the
#  OpenClaw / Hermes portable config centers: warm dark theme, cards,
#  tabs, first-run wizard. Loaded from lib/config_ui.html.
# ═══════════════════════════════════════════════════════════════
_UI_FILE = SCRIPT_DIR / "config_ui.html"


def _load_page():
    try:
        return _UI_FILE.read_text(encoding="utf-8")
    except Exception:
        return ("<html><body style='font-family:sans-serif;padding:40px'>"
                "<h2>配置中心 UI 文件缺失</h2><p>lib/config_ui.html 未找到。"
                "请重新下载发布包。</p></body></html>")


PAGE = _load_page()


# ═══════════════════════════════════════════════════════════════
#  HTTP handler
# ═══════════════════════════════════════════════════════════════
class Handler(BaseHTTPRequestHandler):
    timeout = 30

    def _host_ok(self):
        host = self.headers.get("Host", "")
        try:
            port = self.server.server_address[1]
        except Exception:
            port = PORT
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _reject_host(self):
        if self._host_ok():
            return False
        self.send_response(421)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error":"Host mismatch"}')
        return True

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self._reject_host():
            return
        try:
            if self.path in ("/", "/index.html"):
                self._html(PAGE)
            elif self.path == "/api/state":
                self._json({
                    "providers_catalog": PROVIDERS,
                    "current": read_current(),
                    "saved": list_providers(),
                    "has_config": read_current() is not None,
                })
            elif self.path == "/api/heartbeat":
                self._json({"alive": True})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)[:200]}, 500)

    def do_POST(self):
        if self._reject_host():
            return
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 1_000_000)
            raw = self.rfile.read(n) if n else b"{}"
            data = json.loads(raw or b"{}")
        except Exception:
            self._json({"ok": False, "error": "bad request body"}, 400)
            return
        try:
            if self.path == "/api/save":
                save_provider(data.get("name", ""), data.get("base_url", ""),
                              data.get("api_key", ""), data.get("model", ""))
                self._json({"ok": True})
            elif self.path == "/api/test":
                ok, msg = test_key(data.get("base_url", ""),
                                   data.get("api_key", ""), data.get("model", ""))
                self._json({"ok": ok, "message": msg})
            elif self.path == "/api/activate":
                activate_provider(data.get("id", ""))
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": "not found"}, 404)
        except Exception as e:
            self._json({"ok": False, "error": str(e)[:200]}, 400)


def main():
    server = None
    actual = PORT
    for p in range(PORT, PORT + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            actual = p
            break
        except OSError:
            continue
    if server is None:
        print(f"  [!] 端口 {PORT}-{PORT+9} 都被占用", file=sys.stderr)
        sys.exit(1)
    url = f"http://127.0.0.1:{actual}"
    print(f"  配置中心: {url}")
    if not os.environ.get("CODEX_BROWSER_OPENED"):
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

import os, sqlite3, secrets, time
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import requests

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.getenv("DATABASE_PATH", os.path.join(APP_DIR, "shares.db"))
TV_TOKEN = os.getenv("TV_BEARER_TOKEN", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "10"))
BASE = "https://www.textverified.com/api/pub/v2"

app = FastAPI(title="Textverified Public SMS Share")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS shares (
            token TEXT PRIMARY KEY, reservation_id TEXT NOT NULL,
            number TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL)""")
init_db()

def admin_check(password):
    if not ADMIN_PASSWORD or password != ADMIN_PASSWORD:
        raise HTTPException(401, "管理员密码错误或未设置")

def tv_get(path, params=None):
    if not TV_TOKEN:
        raise HTTPException(503, "服务器尚未配置 TV_BEARER_TOKEN")
    try:
        r = requests.get(BASE + path, headers={"Authorization": "Bearer " + TV_TOKEN,
                         "Accept": "application/json"}, params=params, timeout=25)
    except requests.RequestException as e:
        raise HTTPException(502, "Textverified API 连接失败")
    if r.status_code >= 400:
        raise HTTPException(r.status_code, "Textverified API 返回错误：" + r.text[:400])
    try: return r.json()
    except Exception: return {}

def extract_list(data):
    if isinstance(data, list): return data
    if isinstance(data, dict):
        for k in ("data", "results", "items", "reservations", "sms"):
            if isinstance(data.get(k), list): return data[k]
    return []

@app.get("/", response_class=HTMLResponse)
def home():
    return open(os.path.join(APP_DIR, "index.html"), encoding="utf-8").read()

@app.get("/share/{token}", response_class=HTMLResponse)
def share_page(token: str):
    with db() as c:
        row = c.execute("SELECT * FROM shares WHERE token=? AND enabled=1", (token,)).fetchone()
    if not row: raise HTTPException(404, "分享链接不存在或已停用")
    return open(os.path.join(APP_DIR, "share.html"), encoding="utf-8").read()

@app.get("/api/admin/rentals")
def rentals(password: str):
    admin_check(password)
    data = tv_get("/reservations/rental/renewable")
    return {"items": extract_list(data), "raw": data}

@app.post("/api/admin/shares")
def create_share(payload: dict, password: str):
    admin_check(password)
    rid = str(payload.get("reservationId", "")).strip()
    number = str(payload.get("number", "")).strip()
    if not rid or not number: raise HTTPException(400, "缺少 reservationId 或 number")
    token = secrets.token_urlsafe(24)
    with db() as c:
        c.execute("INSERT INTO shares(token,reservation_id,number,enabled,created_at) VALUES(?,?,?,?,?)",
                  (token, rid, number, 1, datetime.now(timezone.utc).isoformat()))
    return {"token": token, "url": "/share/" + token, "number": number}

@app.get("/api/admin/shares")
def list_shares(password: str):
    admin_check(password)
    with db() as c:
        rows = c.execute("SELECT token,reservation_id,number,enabled,created_at FROM shares ORDER BY created_at DESC").fetchall()
    return {"items": [dict(x) for x in rows]}

@app.post("/api/admin/shares/{token}/toggle")
def toggle_share(token: str, payload: dict, password: str):
    admin_check(password)
    enabled = 1 if payload.get("enabled") else 0
    with db() as c:
        cur = c.execute("UPDATE shares SET enabled=? WHERE token=?", (enabled, token))
    if not cur.rowcount: raise HTTPException(404, "链接不存在")
    return {"ok": True}

@app.get("/api/share/{token}/sms")
def public_sms(token: str):
    with db() as c:
        row = c.execute("SELECT * FROM shares WHERE token=? AND enabled=1", (token,)).fetchone()
    if not row: raise HTTPException(404, "分享链接不存在或已停用")
    data = tv_get("/sms", {"to": row["number"], "reservationId": row["reservation_id"],
                           "reservationType": "renewable"})
    return {"number": row["number"], "items": extract_list(data), "raw": data,
            "pollSeconds": POLL_SECONDS}

app.mount("/static", StaticFiles(directory=APP_DIR), name="static")

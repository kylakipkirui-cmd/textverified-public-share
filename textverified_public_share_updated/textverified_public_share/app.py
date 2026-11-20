import os
import sqlite3
import secrets
import time
import threading
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.getenv("DATABASE_PATH", os.path.join(APP_DIR, "shares.db"))
TV_EMAIL = os.getenv("TV_EMAIL", "").strip()
TV_API_KEY = os.getenv("TV_API_KEY", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
POLL_SECONDS = max(5, int(os.getenv("POLL_SECONDS", "10")))
BASE = "https://www.textverified.com/api/pub/v2"

# Token is held only in server memory and renewed shortly before expiry.
_token = None
_token_refresh_at = 0.0
_token_lock = threading.Lock()

app = FastAPI(title="Textverified Public SMS Share")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    parent = os.path.dirname(DB)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS shares (
                token TEXT PRIMARY KEY,
                reservation_id TEXT NOT NULL,
                number TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )"""
        )


init_db()


def admin_check(password):
    if not ADMIN_PASSWORD or password != ADMIN_PASSWORD:
        raise HTTPException(401, "管理员密码错误或未设置")


def get_tv_token(force_refresh=False):
    global _token, _token_refresh_at
    if not TV_EMAIL or not TV_API_KEY:
        raise HTTPException(503, "服务器尚未配置 TV_EMAIL 和 TV_API_KEY")

    with _token_lock:
        now = time.monotonic()
        if not force_refresh and _token and now < _token_refresh_at:
            return _token

        try:
            response = requests.post(
                BASE + "/auth",
                headers={
                    "X-API-USERNAME": TV_EMAIL,
                    "X-API-KEY": TV_API_KEY,
                    "Accept": "application/json",
                },
                timeout=25,
            )
        except requests.RequestException:
            raise HTTPException(502, "无法连接 Textverified 身份验证接口")

        if response.status_code >= 400:
            # Do not return request headers or credentials to the browser.
            raise HTTPException(502, f"Textverified Token 申请失败（HTTP {response.status_code}），请检查邮箱和 API Key")

        try:
            payload = response.json()
            new_token = payload["token"]
            expires_in = int(payload["expiresIn"])
        except (ValueError, TypeError, KeyError):
            raise HTTPException(502, "Textverified 身份验证响应格式不符合预期")

        if not isinstance(new_token, str) or not new_token or expires_in <= 0:
            raise HTTPException(502, "Textverified 返回的 Token 或有效期无效")

        # Refresh up to 60 seconds early (or 10% of short token lifetimes).
        refresh_buffer = min(60, max(5, int(expires_in * 0.10)))
        _token = new_token
        _token_refresh_at = time.monotonic() + max(1, expires_in - refresh_buffer)
        return _token


def tv_get(path, params=None):
    token = get_tv_token()
    try:
        response = requests.get(
            BASE + path,
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
            params=params,
            timeout=25,
        )
    except requests.RequestException:
        raise HTTPException(502, "Textverified API 连接失败")

    # If the provider invalidates a token early, refresh and retry once.
    if response.status_code in (401, 403):
        token = get_tv_token(force_refresh=True)
        try:
            response = requests.get(
                BASE + path,
                headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
                params=params,
                timeout=25,
            )
        except requests.RequestException:
            raise HTTPException(502, "Textverified API 连接失败")

    if response.status_code >= 400:
        raise HTTPException(502, f"Textverified API 返回错误（HTTP {response.status_code}）")
    try:
        return response.json()
    except ValueError:
        return {}


def extract_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "results", "items", "reservations", "sms"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


@app.get("/", response_class=HTMLResponse)
def home():
    with open(os.path.join(APP_DIR, "index.html"), encoding="utf-8") as file:
        return file.read()


@app.get("/share/{token}", response_class=HTMLResponse)
def share_page(token: str):
    with db() as c:
        row = c.execute("SELECT * FROM shares WHERE token=? AND enabled=1", (token,)).fetchone()
    if not row:
        raise HTTPException(404, "分享链接不存在或已停用")
    with open(os.path.join(APP_DIR, "share.html"), encoding="utf-8") as file:
        return file.read()


@app.get("/api/admin/rentals")
def rentals(password: str):
    admin_check(password)

    renewable = tv_get("/reservations/rental/renewable")
    nonrenewable = tv_get("/reservations/rental/nonrenewable")

    renewable_items = extract_list(renewable)
    nonrenewable_items = extract_list(nonrenewable)

    # Merge both rental types and remove duplicate reservation IDs.
    items = []
    seen = set()

    for item in renewable_items + nonrenewable_items:
        if not isinstance(item, dict):
            continue

        reservation_id = str(
            item.get("id") or item.get("reservationId") or ""
        ).strip()

        if reservation_id and reservation_id not in seen:
            seen.add(reservation_id)
            items.append(item)

    return {
        "items": items,
        "raw": {
            "renewable": renewable,
            "nonrenewable": nonrenewable
        }
    }


@app.post("/api/admin/shares")
def create_share(payload: dict, password: str):
    admin_check(password)
    reservation_id = str(payload.get("reservationId", "")).strip()
    number = str(payload.get("number", "")).strip()
    if not reservation_id or not number:
        raise HTTPException(400, "缺少 reservationId 或 number")
    token = secrets.token_urlsafe(24)
    with db() as c:
        c.execute(
            "INSERT INTO shares(token,reservation_id,number,enabled,created_at) VALUES(?,?,?,?,?)",
            (token, reservation_id, number, 1, datetime.now(timezone.utc).isoformat()),
        )
    return {"token": token, "url": "/share/" + token, "number": number}


@app.get("/api/admin/shares")
def list_shares(password: str):
    admin_check(password)
    with db() as c:
        rows = c.execute(
            "SELECT token,reservation_id,number,enabled,created_at FROM shares ORDER BY created_at DESC"
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.post("/api/admin/shares/{token}/toggle")
def toggle_share(token: str, payload: dict, password: str):
    admin_check(password)
    enabled = 1 if payload.get("enabled") else 0
    with db() as c:
        cur = c.execute("UPDATE shares SET enabled=? WHERE token=?", (enabled, token))
    if not cur.rowcount:
        raise HTTPException(404, "链接不存在")
    return {"ok": True}


@app.get("/api/share/{token}/sms")
def public_sms(token: str):
    with db() as c:
        row = c.execute("SELECT * FROM shares WHERE token=? AND enabled=1", (token,)).fetchone()
    if not row:
        raise HTTPException(404, "分享链接不存在或已停用")
    data = tv_get(
        "/sms",
        {
            "to": row["number"],
            "reservationId": row["reservation_id"],
            "reservationType": "nonrenewable",
        },
    )
    return {
        "number": row["number"],
        "items": extract_list(data),
        "raw": data,
        "pollSeconds": POLL_SECONDS,
    }


app.mount("/static", StaticFiles(directory=APP_DIR), name="static")

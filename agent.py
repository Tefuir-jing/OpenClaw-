"""
Personal Automation Agent

功能：
1. 网页库存/价格/关键词状态监控
2. VPS systemd 服务状态检查
3. journalctl 日志摘要
4. Telegram / QQ OneBot 推送
5. FastAPI HTTP 指令入口

安全原则：
- 不执行任意 shell 命令。
- systemd 服务名必须在 ALLOWED_SERVICES 白名单中。
- 网页监控只做 GET 抓取和文本判断。
"""

import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


# =========================
# 基础配置
# =========================

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "18790"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()

MONITOR_INTERVAL_SECONDS = int(os.getenv("MONITOR_INTERVAL_SECONDS", "60"))
MONITORS_FILE = os.getenv("MONITORS_FILE", "monitors.json").strip()
RAW_MONITORS = os.getenv("MONITORS", "").strip()

ALLOWED_SERVICES = {
    item.strip()
    for item in os.getenv("ALLOWED_SERVICES", "").split(",")
    if item.strip()
}

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

ONEBOT_HTTP_URL = os.getenv("ONEBOT_HTTP_URL", "").strip().rstrip("/")
ONEBOT_ACCESS_TOKEN = os.getenv("ONEBOT_ACCESS_TOKEN", "").strip()
ONEBOT_GROUP_ID = os.getenv("ONEBOT_GROUP_ID", "").strip()

DB_PATH = Path(os.getenv("DB_PATH", "agent_state.db"))
if not DB_PATH.is_absolute():
    DB_PATH = BASE_DIR / DB_PATH


# =========================
# 通用工具
# =========================

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_json_loads(text: str, fallback: Any) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return fallback


def load_monitors() -> List[Dict[str, Any]]:
    """
    读取监控配置，优先级：
    1. 环境变量 MONITORS 中的 JSON
    2. MONITORS_FILE 指向的 JSON 文件
    """
    if RAW_MONITORS:
        data = safe_json_loads(RAW_MONITORS, [])
        return data if isinstance(data, list) else []

    path = Path(MONITORS_FILE)
    if not path.is_absolute():
        path = BASE_DIR / path

    if not path.exists():
        print(f"[{now_str()}] 未找到监控配置文件：{path}")
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[{now_str()}] 监控配置文件解析失败：{e}")
        return []


MONITORS = load_monitors()


# =========================
# SQLite 状态存储
# =========================

def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS monitor_state (
            name TEXT PRIMARY KEY,
            status TEXT,
            detail TEXT,
            updated_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def get_state(name: str) -> Optional[Dict[str, str]]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT name, status, detail, updated_at FROM monitor_state WHERE name = ?",
        (name,),
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        return None

    return {
        "name": row[0],
        "status": row[1],
        "detail": row[2],
        "updated_at": row[3],
    }


def save_state(name: str, status: str, detail: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO monitor_state (name, status, detail, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            status = excluded.status,
            detail = excluded.detail,
            updated_at = excluded.updated_at
        """,
        (name, status, detail, now_str()),
    )
    conn.commit()
    conn.close()


# =========================
# 通知模块
# =========================

def notify_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        if not response.ok:
            print(f"[{now_str()}] Telegram 推送失败：{response.status_code} {response.text[:300]}")
        return response.ok
    except Exception as e:
        print(f"[{now_str()}] Telegram 推送异常：{e}")
        return False


def notify_onebot(text: str, group_id: Optional[str] = None) -> bool:
    if not ONEBOT_HTTP_URL:
        return False

    gid = group_id or ONEBOT_GROUP_ID
    if not gid:
        return False

    url = f"{ONEBOT_HTTP_URL}/send_group_msg"
    headers = {}

    if ONEBOT_ACCESS_TOKEN:
        headers["Authorization"] = f"Bearer {ONEBOT_ACCESS_TOKEN}"

    payload = {
        "group_id": int(gid),
        "message": text,
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if not response.ok:
            print(f"[{now_str()}] OneBot 推送失败：{response.status_code} {response.text[:300]}")
        return response.ok
    except Exception as e:
        print(f"[{now_str()}] OneBot 推送异常：{e}")
        return False


def notify_all(text: str, group_id: Optional[str] = None) -> None:
    print(f"\n[{now_str()}] 通知：\n{text}\n")
    notify_telegram(text)
    notify_onebot(text, group_id=group_id)


# =========================
# AI 摘要模块
# =========================

def ai_summarize(title: str, content: str) -> str:
    """
    可选 AI 摘要。
    不配置 OPENAI_API_KEY 时，直接返回截断后的原始内容。
    """
    content = content.strip()

    if not content:
        return "没有可摘要的内容。"

    if not OPENAI_API_KEY or OpenAI is None:
        return content[:1200] + ("\n……" if len(content) > 1200 else "")

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        prompt = f"""
你是一个个人运维 Agent。
请把下面的信息压缩成简洁中文摘要。

要求：
1. 先说结论；
2. 再列关键异常；
3. 最后给下一步建议；
4. 不要编造没有出现的信息；
5. 语言短，直接，可执行。

标题：{title}

内容：
{content[:12000]}
""".strip()

        response = client.responses.create(
            model=OPENAI_MODEL,
            input=prompt,
        )
        return response.output_text.strip()

    except Exception as e:
        print(f"[{now_str()}] AI 摘要失败：{e}")
        return content[:1200] + ("\n……" if len(content) > 1200 else "")


# =========================
# 网页监控模块
# =========================

def fetch_page_text(url: str) -> Tuple[bool, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 Chrome/120 Safari/537.36"
        )
    }

    try:
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        text = soup.get_text("\n")
        text = re.sub(r"\n{2,}", "\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return True, text.strip()

    except Exception as e:
        return False, f"抓取失败：{e}"


def extract_context(text: str, target: str, window: int = 800) -> str:
    if not target:
        return text[:1600]

    idx = text.lower().find(target.lower())
    if idx == -1:
        return text[:1600]

    start = max(0, idx - window)
    end = min(len(text), idx + len(target) + window)
    return text[start:end]


def judge_stock(monitor: Dict[str, Any], text: str) -> Tuple[str, str]:
    """
    返回状态：
    - available：可能有货
    - unavailable：无货
    - unknown：状态不明确
    """
    name = monitor.get("name", "未命名监控")
    target = monitor.get("target", "")
    available_keywords = monitor.get("available_keywords", [])
    unavailable_keywords = monitor.get("unavailable_keywords", [])

    context = extract_context(text, target)

    if target and target.lower() not in text.lower():
        return "unknown", f"没有找到目标关键词：{target}"

    lower_context = context.lower()
    has_available = any(str(k).lower() in lower_context for k in available_keywords)
    has_unavailable = any(str(k).lower() in lower_context for k in unavailable_keywords)

    if has_available and not has_unavailable:
        return "available", f"可能有货：{name}\n\n附近文本：\n{context[:1000]}"

    if has_unavailable:
        return "unavailable", f"仍然无货：{name}\n\n附近文本：\n{context[:1000]}"

    return "unknown", f"状态不明确：{name}\n\n附近文本：\n{context[:1000]}"


def check_one_monitor(monitor: Dict[str, Any], push_on_change: bool = True) -> Dict[str, Any]:
    name = monitor.get("name", "未命名监控")
    url = monitor.get("url", "")

    if not url:
        return {
            "name": name,
            "status": "error",
            "detail": "缺少 url",
        }

    ok, text = fetch_page_text(url)

    if not ok:
        status = "error"
        detail = text
    else:
        status, detail = judge_stock(monitor, text)

    old = get_state(name)
    old_status = old["status"] if old else None
    changed = old_status is not None and old_status != status

    save_state(name, status, detail)

    if push_on_change and changed:
        if status == "available":
            msg = (
                f"【补货提醒】\n"
                f"{name}\n\n"
                f"状态：可能有货\n"
                f"时间：{now_str()}\n"
                f"链接：{url}\n\n"
                f"{ai_summarize('网页补货提醒', detail)}"
            )
            notify_all(msg)
        elif status == "error":
            msg = (
                f"【监控异常】\n"
                f"{name}\n\n"
                f"状态：抓取失败\n"
                f"时间：{now_str()}\n\n"
                f"{detail}"
            )
            notify_all(msg)

    return {
        "name": name,
        "url": url,
        "status": status,
        "old_status": old_status,
        "changed": changed,
        "detail": detail,
    }


def check_all_monitors(push_on_change: bool = True) -> List[Dict[str, Any]]:
    return [check_one_monitor(monitor, push_on_change=push_on_change) for monitor in MONITORS]


# =========================
# VPS 运维工具
# =========================

def safe_service_name(service: str) -> bool:
    return service in ALLOWED_SERVICES


def run_cmd(args: List[str], timeout: int = 15) -> Tuple[int, str]:
    try:
        process = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (process.stdout or "") + ("\n" + process.stderr if process.stderr else "")
        return process.returncode, output.strip()
    except subprocess.TimeoutExpired:
        return 124, "命令超时"
    except Exception as e:
        return 1, f"执行失败：{e}"


def check_service(service: str) -> str:
    if not safe_service_name(service):
        return (
            f"拒绝检查服务：{service}\n"
            f"原因：不在 ALLOWED_SERVICES 白名单中。\n"
            f"当前白名单：{', '.join(sorted(ALLOWED_SERVICES)) or '空'}"
        )

    _, output = run_cmd(["systemctl", "status", service, "--no-pager"], timeout=15)
    return ai_summarize(f"systemd 服务状态：{service}", output)


def tail_service_log(service: str, lines: int = 80) -> str:
    if not safe_service_name(service):
        return (
            f"拒绝读取日志：{service}\n"
            f"原因：不在 ALLOWED_SERVICES 白名单中。\n"
            f"当前白名单：{', '.join(sorted(ALLOWED_SERVICES)) or '空'}"
        )

    lines = max(20, min(lines, 300))
    _, output = run_cmd(
        ["journalctl", "-u", service, "-n", str(lines), "--no-pager"],
        timeout=20,
    )
    return ai_summarize(f"服务日志摘要：{service}", output)


def system_overview() -> str:
    cmds = [
        ["uptime"],
        ["df", "-h"],
        ["free", "-h"],
    ]

    parts = []
    for cmd in cmds:
        _, output = run_cmd(cmd, timeout=10)
        parts.append(f"$ {' '.join(cmd)}\n{output}")

    return ai_summarize("VPS 系统概览", "\n\n".join(parts))


# =========================
# Agent 指令解析
# =========================

HELP_TEXT = """
可用指令：

1. 帮助
2. 状态
3. 检查网页
4. 系统状态
5. 检查服务 openclaw
6. 日志 openclaw 100

HTTP 示例：
curl -X POST http://127.0.0.1:18790/command \\
  -H 'Content-Type: application/json' \\
  -d '{"text":"检查网页"}'

OneBot 群聊示例：
/agent 状态
/agent 检查服务 openclaw
/agent 日志 nginx 80
""".strip()


def normalize_command(text: str) -> str:
    text = text.strip()
    prefixes = ["/agent", "agent", "Agent", "机器人", "助手"]
    for prefix in prefixes:
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def handle_command(text: str) -> str:
    cmd = normalize_command(text)

    if not cmd or cmd in {"帮助", "help", "-h", "--help"}:
        return HELP_TEXT

    if cmd in {"状态", "status"}:
        states = []
        for monitor in MONITORS:
            name = monitor.get("name", "未命名监控")
            state = get_state(name)
            if state:
                states.append(
                    f"- {name}\n"
                    f"  状态：{state['status']}\n"
                    f"  更新时间：{state['updated_at']}"
                )
            else:
                states.append(f"- {name}\n  状态：尚未检查")

        monitor_text = "\n".join(states) if states else "未配置网页监控。"
        return f"【Agent 状态】\n\n网页监控：\n{monitor_text}"

    if cmd in {"检查网页", "监控检查", "check monitors"}:
        results = check_all_monitors(push_on_change=False)
        lines = []
        for result in results:
            detail = result.get("detail", "")[:300].replace("\n", " ")
            lines.append(
                f"- {result['name']}\n"
                f"  状态：{result['status']}\n"
                f"  变化：{result.get('changed', False)}\n"
                f"  说明：{detail}"
            )
        return "【网页检查结果】\n\n" + ("\n\n".join(lines) if lines else "没有配置监控任务。")

    if cmd in {"系统状态", "system", "overview"}:
        return "【系统状态】\n\n" + system_overview()

    match = re.match(r"^检查服务\s+([a-zA-Z0-9_.@-]+)$", cmd)
    if match:
        service = match.group(1)
        return f"【服务检查：{service}】\n\n{check_service(service)}"

    match = re.match(r"^日志\s+([a-zA-Z0-9_.@-]+)(?:\s+(\d+))?$", cmd)
    if match:
        service = match.group(1)
        lines = int(match.group(2) or "80")
        return f"【日志摘要：{service}】\n\n{tail_service_log(service, lines)}"

    return "我没识别这个指令。\n\n" + HELP_TEXT


# =========================
# FastAPI
# =========================

app = FastAPI(title="Personal Automation Agent")
scheduler = BackgroundScheduler()


@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "name": "Personal Automation Agent",
        "time": now_str(),
        "monitors": len(MONITORS),
        "allowed_services": sorted(ALLOWED_SERVICES),
    }


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "time": now_str()}


@app.post("/command")
async def command_api(request: Request) -> JSONResponse:
    """
    通用 HTTP 指令入口。
    """
    data = await request.json()
    text = str(data.get("text", "")).strip()
    group_id = str(data.get("group_id", "")).strip() or None

    reply = handle_command(text)

    if data.get("push") is True:
        notify_all(reply, group_id=group_id)

    return JSONResponse({"ok": True, "reply": reply})


@app.post("/onebot")
async def onebot_webhook(request: Request) -> Dict[str, Any]:
    """
    OneBot / NapCat 事件入口。
    只响应包含 /agent 或以 agent 开头的消息。
    """
    data = await request.json()

    post_type = data.get("post_type")
    message_type = data.get("message_type")
    raw_message = str(data.get("raw_message", "")).strip()
    group_id = data.get("group_id")

    if post_type != "message":
        return {"ok": True, "ignored": "not message"}

    if "/agent" not in raw_message and not raw_message.startswith("agent"):
        return {"ok": True, "ignored": "not agent command"}

    reply = handle_command(raw_message)

    if message_type == "group" and group_id:
        notify_onebot(reply, group_id=str(group_id))
    else:
        notify_all(reply)

    return {"ok": True, "reply": reply}


# =========================
# 定时任务
# =========================

def scheduled_monitor_job() -> None:
    print(f"[{now_str()}] 开始定时检查网页监控")
    try:
        check_all_monitors(push_on_change=True)
    except Exception as e:
        print(f"[{now_str()}] 定时任务异常：{e}")


@app.on_event("startup")
def on_startup() -> None:
    init_db()

    if MONITORS:
        scheduler.add_job(
            scheduled_monitor_job,
            "interval",
            seconds=MONITOR_INTERVAL_SECONDS,
            id="monitor_job",
            replace_existing=True,
            next_run_time=datetime.now(),
        )
        scheduler.start()
        print(f"[{now_str()}] 定时监控已启动，间隔 {MONITOR_INTERVAL_SECONDS} 秒")
    else:
        print(f"[{now_str()}] 没有配置 MONITORS，定时监控未启动")


@app.on_event("shutdown")
def on_shutdown() -> None:
    try:
        scheduler.shutdown()
    except Exception:
        pass


# =========================
# 本地启动
# =========================

if __name__ == "__main__":
    import uvicorn

    init_db()
    uvicorn.run("agent:app", host=HOST, port=PORT, reload=False)

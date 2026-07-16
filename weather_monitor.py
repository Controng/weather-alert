import argparse
import html
import json
import os
import re
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

URL = "https://sh.weather.com.cn/zhyj/index.shtml"
DATA_FILE = Path("data/alerts.json")
DEBUG_FILE = Path("data/last_page_text.txt")
TZ = ZoneInfo("Asia/Shanghai")
TARGET_REGION = "浦东新区"
TARGET_LEVELS = ("橙色", "红色")


def now_shanghai():
    return datetime.now(TZ)


def normalize(value):
    return re.sub(r"\s+", " ", value or "").strip()


def fetch_page_lines():
    """用 Chromium 渲染网页，返回页面逐行文字。"""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(
            viewport={"width": 1600, "height": 1200},
            locale="zh-CN",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        page.goto(URL, wait_until="networkidle", timeout=120000)

        # 动态列表有时比页面主框架更晚出现。
        for _ in range(12):
            text = page.locator("body").inner_text(timeout=30000)
            if TARGET_REGION in text or "当前无预警" in text:
                break
            page.wait_for_timeout(2500)

        page.wait_for_timeout(5000)
        text = page.locator("body").inner_text(timeout=30000)
        browser.close()

    DEBUG_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEBUG_FILE.write_text(text, encoding="utf-8")
    return [normalize(line) for line in text.splitlines() if normalize(line)]


def parse_time(text):
    patterns = [
        r"(20\d{2})年(\d{1,2})月(\d{1,2})日(\d{1,2})时(\d{1,2})分",
        r"(20\d{2})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{1,2})",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            y, month, day, hour, minute = map(int, m.groups())
            return datetime(y, month, day, hour, minute, tzinfo=TZ)
    return now_shanghai()


def is_target_line(line):
    return TARGET_REGION in line and any(level in line for level in TARGET_LEVELS)


def extract_alerts(lines):
    """围绕每一条含浦东新区和橙/红色的行，提取时间和完整正文。"""
    candidates = []
    seen = set()

    for i, line in enumerate(lines):
        if not is_target_line(line):
            continue

        # 向前后寻找同一条卡片的时间、标题和详细正文。
        start = max(0, i - 3)
        end = min(len(lines), i + 8)
        nearby = lines[start:end]

        # 优先选择包含“气象台”“发布”“预警信号”的详细正文。
        detailed = [
            x for x in nearby
            if TARGET_REGION in x
            and any(level in x for level in TARGET_LEVELS)
            and ("气象台" in x or "发布" in x or "预警信号" in x)
        ]
        description = max(detailed, key=len) if detailed else line

        # 若标题和正文被拆成两行，拼接有用内容。
        title_lines = [x for x in nearby if is_target_line(x) and len(x) < 80]
        title = title_lines[0] if title_lines else f"上海市浦东新区天气预警"
        if description == title and i + 1 < len(lines):
            nxt = lines[i + 1]
            if TARGET_REGION in nxt or "气象台" in nxt:
                description = normalize(title + "；" + nxt)

        context = " ".join(nearby)
        start_time = parse_time(description + " " + context)
        level = "红色" if "红色" in description or "红色" in title else "橙色"
        warning_type = "天气"
        type_match = re.search(r"(高温|暴雨|雷电|大风|台风|冰雹|寒潮|道路结冰|大雾|霜冻|暴雪)", title + description)
        if type_match:
            warning_type = type_match.group(1)

        key = f"浦东新区|{warning_type}|{level}|{start_time:%Y%m%d%H%M}"
        if key in seen:
            continue
        seen.add(key)

        candidates.append({
            "id": key,
            "title": title,
            "level": level,
            "warning_type": warning_type,
            "date_from": start_time.isoformat(),
            "date_to": None,
            "description": description,
            "first_seen": now_shanghai().isoformat(),
            "last_seen": now_shanghai().isoformat(),
            "active": True,
        })

    return candidates


def load_history():
    if not DATA_FILE.exists():
        return []
    try:
        value = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except Exception:
        return []


def save_history(history):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def merge_history(history, current):
    current_ids = {x["id"] for x in current}
    by_id = {x["id"]: x for x in history if "id" in x}
    now_iso = now_shanghai().isoformat()

    for item in current:
        if item["id"] in by_id:
            old = by_id[item["id"]]
            old["last_seen"] = now_iso
            old["active"] = True
            old["date_to"] = None
            if len(item["description"]) > len(old.get("description", "")):
                old["description"] = item["description"]
        else:
            history.append(item)
            by_id[item["id"]] = item

    for item in history:
        if item.get("active") and item.get("id") not in current_ids:
            item["active"] = False
            item["date_to"] = now_iso

    return history


def fmt_time(value):
    if not value:
        return "生效中"
    try:
        return datetime.fromisoformat(value).astimezone(TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)


def rows_last_7_days(history):
    cutoff = now_shanghai() - timedelta(days=7)
    rows = []
    for item in history:
        try:
            start = datetime.fromisoformat(item["date_from"])
        except Exception:
            continue
        if start >= cutoff or item.get("active"):
            rows.append(item)
    return sorted(rows, key=lambda x: x["date_from"])


def build_email(rows, test=False):
    prefix = "【测试】" if test else ""
    subject = f"{prefix}浦东新区橙色/红色天气预警周报"
    now_text = now_shanghai().strftime("%Y-%m-%d %H:%M")

    if rows:
        table = "".join(
            "<tr>"
            f"<td>{html.escape(fmt_time(x.get('date_from')))}</td>"
            f"<td>{html.escape(fmt_time(x.get('date_to')))}</td>"
            f"<td>{html.escape(x.get('description', ''))}</td>"
            "</tr>"
            for x in rows
        )
        summary = f"共发现 {len(rows)} 条符合条件的预警。"
    else:
        table = "<tr><td>—</td><td>—</td><td>过去7天未记录到浦东新区橙色或红色天气预警。</td></tr>"
        summary = "过去7天未记录到符合条件的预警。"

    body = f"""
    <html><body style="font-family:Arial,'Microsoft YaHei',sans-serif;line-height:1.6">
      <h2>浦东新区橙色/红色天气预警周报</h2>
      <p><b>统计截止时间：</b>{now_text}（上海时间）</p>
      <p><b>监测范围：</b>上海市浦东新区，仅统计橙色和红色预警。</p>
      <p>{summary}</p>
      <table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;width:100%">
        <thead><tr><th>Date From</th><th>Date To</th><th>Description</th></tr></thead>
        <tbody>{table}</tbody>
      </table>
      <p style="color:#666">数据来源：上海天气预警官网<br>{URL}</p>
    </body></html>
    """
    return subject, body


def send_email(subject, body):
    user = os.environ.get("GMAIL_USER", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    recipient = os.environ.get("RECIPIENT_EMAIL", "june.shao@disney.com").strip()
    if not user or not password:
        raise RuntimeError("GitHub Secrets 中缺少 GMAIL_USER 或 GMAIL_APP_PASSWORD")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = recipient
    msg.attach(MIMEText(body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(user, password)
        server.sendmail(user, [recipient], msg.as_string())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["collect", "weekly", "test"], default="collect")
    args = parser.parse_args()

    lines = fetch_page_lines()
    current = extract_alerts(lines)
    print(f"页面共读取 {len(lines)} 行文字")
    print(f"当前识别到浦东新区橙色/红色预警：{len(current)} 条")
    for item in current:
        print(f"- {item['date_from']} | {item['description']}")

    history = merge_history(load_history(), current)
    save_history(history)

    if args.mode == "collect":
        print("本次为采集模式：只更新历史记录，不发送邮件。")
        return

    rows = rows_last_7_days(history)
    if args.mode == "weekly" and not rows:
        print("本周没有符合条件的预警，不发送邮件。")
        return

    subject, body = build_email(rows, test=(args.mode == "test"))
    send_email(subject, body)
    print("邮件已发送。")


if __name__ == "__main__":
    main()

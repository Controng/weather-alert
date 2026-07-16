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

import requests
from bs4 import BeautifulSoup

URL = "https://sh.weather.com.cn/zhyj/index.shtml"
DATA_FILE = Path("data/alerts.json")
TZ = ZoneInfo("Asia/Shanghai")
TARGET_REGION = "浦东新区"
TARGET_LEVELS = ("橙色", "红色")


def now_shanghai():
    return datetime.now(TZ)


def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def fetch_page():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/126 Safari/537.36"
        ),
        "Referer": "https://sh.weather.com.cn/",
    }
    response = requests.get(URL, headers=headers, timeout=30)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def parse_start_time(text, fallback):
    patterns = [
        r"(20\d{2})年(\d{1,2})月(\d{1,2})日(\d{1,2})时(\d{1,2})分",
        r"(20\d{2})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{1,2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            y, m, d, hh, mm = map(int, match.groups())
            return datetime(y, m, d, hh, mm, tzinfo=TZ)
    return fallback


def extract_alerts(page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    found = []
    seen = set()

    # 从所有包含“浦东新区 + 橙色/红色 + 预警”的页面元素中提取最完整文本。
    for node in soup.find_all(string=re.compile(TARGET_REGION)):
        current = node.parent
        candidates = []
        for _ in range(7):
            if current is None:
                break
            text = clean_text(current.get_text(" ", strip=True))
            if (
                TARGET_REGION in text
                and "预警" in text
                and any(level in text for level in TARGET_LEVELS)
            ):
                candidates.append(text)
            current = current.parent

        if not candidates:
            continue

        # 选择既有详细内容、又不会把整页内容全部包进去的候选。
        reasonable = [t for t in candidates if 20 <= len(t) <= 1200]
        description = max(reasonable or candidates, key=len)
        description = clean_text(html.unescape(description))

        # 避免菜单、页脚等内容混入。
        for marker in ["信息来源：", "关于我们", "Copyright"]:
            if marker in description:
                description = description.split(marker, 1)[0].strip()

        if description in seen:
            continue
        seen.add(description)

        title_match = re.search(
            r"([^。；]{0,40}浦东新区[^。；]{0,40}(?:橙色|红色)[^。；]{0,20}预警[^。；]{0,20})",
            description,
        )
        title = clean_text(title_match.group(1)) if title_match else description[:100]
        level = "红色" if "红色" in title or "红色" in description else "橙色"
        start = parse_start_time(description, now_shanghai())
        alert_id = re.sub(r"\W+", "", title) + "_" + start.strftime("%Y%m%d%H%M")

        found.append(
            {
                "id": alert_id,
                "title": title,
                "level": level,
                "date_from": start.isoformat(),
                "date_to": None,
                "description": description,
                "first_seen": now_shanghai().isoformat(),
                "last_seen": now_shanghai().isoformat(),
                "active": True,
            }
        )

    # 同一预警可能被多个嵌套元素命中，只保留描述最完整的一条。
    unique = {}
    for alert in found:
        key = (alert["level"], alert["date_from"][:16])
        if key not in unique or len(alert["description"]) > len(unique[key]["description"]):
            unique[key] = alert
    return list(unique.values())


def load_history():
    if not DATA_FILE.exists():
        return []
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def save_history(history):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def merge_history(history, current_alerts):
    now = now_shanghai()
    current_ids = {item["id"] for item in current_alerts}
    history_by_id = {item["id"]: item for item in history}

    for current in current_alerts:
        if current["id"] in history_by_id:
            old = history_by_id[current["id"]]
            old["last_seen"] = now.isoformat()
            old["active"] = True
            if len(current["description"]) > len(old.get("description", "")):
                old["description"] = current["description"]
        else:
            history.append(current)
            history_by_id[current["id"]] = current

    for item in history:
        if item.get("active") and item["id"] not in current_ids:
            item["active"] = False
            item["date_to"] = now.isoformat()

    return history


def fmt_time(value):
    if not value:
        return "生效中"
    try:
        return datetime.fromisoformat(value).astimezone(TZ).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def weekly_rows(history):
    cutoff = now_shanghai() - timedelta(days=7)
    rows = []
    for item in history:
        try:
            start = datetime.fromisoformat(item["date_from"])
        except ValueError:
            continue
        if start >= cutoff or item.get("active"):
            rows.append(item)
    rows.sort(key=lambda x: x["date_from"])
    return rows


def build_email(rows, test=False):
    now_text = now_shanghai().strftime("%Y-%m-%d %H:%M")
    prefix = "【测试】" if test else ""
    subject = f"{prefix}浦东新区橙色/红色天气预警周报"

    if rows:
        table_rows = "".join(
            "<tr>"
            f"<td>{html.escape(fmt_time(r['date_from']))}</td>"
            f"<td>{html.escape(fmt_time(r.get('date_to')))}</td>"
            f"<td>{html.escape(r['description'])}</td>"
            "</tr>"
            for r in rows
        )
        status = f"过去7天共记录到 {len(rows)} 条符合条件的预警。"
    else:
        table_rows = (
            "<tr><td>—</td><td>—</td>"
            "<td>截至统计时间，过去7天未记录到浦东新区橙色或红色天气预警。</td></tr>"
        )
        status = "过去7天未记录到符合条件的预警。"

    body = f"""
    <html><body style="font-family:Arial,'Microsoft YaHei',sans-serif;line-height:1.6">
      <h2>浦东新区橙色/红色天气预警周报</h2>
      <p><b>统计截止时间：</b>{now_text}（上海时间）</p>
      <p><b>监测范围：</b>上海市浦东新区；仅统计橙色和红色天气预警。</p>
      <p>{status}</p>
      <table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;width:100%">
        <thead><tr><th>Date From</th><th>Date To</th><th>Description</th></tr></thead>
        <tbody>{table_rows}</tbody>
      </table>
      <p style="color:#666">数据来源：上海天气预警官网<br>{URL}</p>
    </body></html>
    """
    return subject, body


def send_email(subject, body):
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("RECIPIENT_EMAIL", "june.shao@disney.com")
    if not gmail_user or not gmail_password:
        raise RuntimeError("缺少 GMAIL_USER 或 GMAIL_APP_PASSWORD GitHub Secret")

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = gmail_user
    message["To"] = recipient
    message.attach(MIMEText(body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(gmail_user, gmail_password.replace(" ", ""))
        server.sendmail(gmail_user, [recipient], message.as_string())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="强制发送测试邮件")
    args = parser.parse_args()

    page_html = fetch_page()
    current = extract_alerts(page_html)
    history = merge_history(load_history(), current)
    save_history(history)

    now = now_shanghai()
    should_send = args.test or now.weekday() == 4  # 周五（上海时间）
    if should_send:
        rows = weekly_rows(history)
        # 正式周报仅在有预警时发送；手动测试即使无预警也发送。
        if rows or args.test:
            subject, body = build_email(rows, test=args.test)
            send_email(subject, body)
            print("邮件已发送。")
        else:
            print("本周无符合条件的预警，不发送邮件。")
    else:
        print(f"已完成检查，当前匹配预警 {len(current)} 条。")


if __name__ == "__main__":
    main()

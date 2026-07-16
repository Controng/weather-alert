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
TZ = ZoneInfo("Asia/Shanghai")
TARGET_REGION = "浦东新区"
TARGET_LEVELS = ("橙色", "红色")


def now_shanghai():
    return datetime.now(TZ)


def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def fetch_rendered_text():
    """用真实浏览器渲染网页，等待右侧动态预警加载完成。"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1600, "height": 1200},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        page.goto(URL, wait_until="domcontentloaded", timeout=90000)

        # 页面预警列表由 JavaScript 异步加载，等待“浦东新区”或“暂无预警”出现。
        try:
            page.wait_for_function(
                """() => document.body.innerText.includes('浦东新区')
                       || document.body.innerText.includes('暂无预警')""",
                timeout=30000,
            )
        except Exception:
            pass

        page.wait_for_timeout(8000)
        body_text = page.locator("body").inner_text()
        browser.close()
        return clean_text(body_text)


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


def extract_alerts(page_text):
    """
    从浏览器实际渲染出的文字中，提取浦东新区橙色/红色预警。
    网站通常按：标题 -> 时间 -> 详细正文 的顺序显示。
    """
    found = []
    seen = set()

    title_pattern = re.compile(
        r"(?:上海市)?浦东新区[^。；\n]{0,40}?(?:橙色|红色)[^。；\n]{0,20}?预警"
    )

    matches = list(title_pattern.finditer(page_text))
    for index, match in enumerate(matches):
        title = clean_text(match.group(0))
        level = "红色" if "红色" in title else "橙色"

        # 从标题起，截取到下一条预警标题之前，最多 1200 字。
        start_pos = match.start()
        end_pos = matches[index + 1].start() if index + 1 < len(matches) else min(len(page_text), start_pos + 1200)
        block = clean_text(page_text[start_pos:end_pos])

        # 确保详情确实属于浦东新区，并包含发布正文。
        if TARGET_REGION not in block or not any(level_name in block for level_name in TARGET_LEVELS):
            continue

        # 常见详情正文以“浦东新区气象台……”开始。
        detail_match = re.search(
            r"(浦东新区气象台20\d{2}年.*?)(?=(?:上海市)?[\u4e00-\u9fa5]{2,12}区[^。；]{0,40}(?:蓝色|黄色|橙色|红色)[^。；]{0,20}预警|$)",
            block,
        )
        description = clean_text(detail_match.group(1) if detail_match else block)
        description = description[:1600]

        if description in seen:
            continue
        seen.add(description)

        start = parse_start_time(description + " " + block, now_shanghai())
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

    return found


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
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
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
            old["date_to"] = None
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
        except (ValueError, TypeError):
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

    rendered_text = fetch_rendered_text()
    current = extract_alerts(rendered_text)

    print(f"当前抓取到浦东新区橙色/红色预警：{len(current)} 条")
    for item in current:
        print(item["title"])
        print(item["description"])

    history = merge_history(load_history(), current)
    save_history(history)

    now = now_shanghai()
    should_send = args.test or now.weekday() == 4

    if should_send:
        rows = weekly_rows(history)
        if rows or args.test:
            subject, body = build_email(rows, test=args.test)
            send_email(subject, body)
            print("邮件已发送。")
        else:
            print("本周无符合条件的预警，不发送邮件。")
    else:
        print("本次只保存预警记录，不发送邮件。")


if __name__ == "__main__":
    main()

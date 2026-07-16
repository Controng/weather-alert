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
HISTORY_FILE = Path("data/alerts.json")
NEXT_HISTORY_FILE = Path("alerts_next.json")
DEBUG_FILE = Path("debug_alarm_dom.json")

TZ = ZoneInfo("Asia/Shanghai")
TARGET_REGION = "浦东新区"
TARGET_LEVELS = ("橙色", "红色")


def now_shanghai():
    return datetime.now(TZ)


def normalize(text):
    return re.sub(r"\s+", " ", text or "").strip()


def parse_time(value):
    value = normalize(value)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=TZ)
        except ValueError:
            pass

    match = re.search(
        r"(20\d{2})年(\d{1,2})月(\d{1,2})日(\d{1,2})时(\d{1,2})分",
        value,
    )
    if match:
        y, m, d, hh, mm = map(int, match.groups())
        return datetime(y, m, d, hh, mm, tzinfo=TZ)

    return now_shanghai()


def fetch_alerts_from_dom():
    """
    直接读取网页 F12 中的真实 DOM：
    #alarmList li
      .alarm-head span  -> 标题
      .alarm-head em    -> 发布时间
      .alarm-body       -> 详细内容
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
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

        page.goto(URL, wait_until="domcontentloaded", timeout=90000)

        # 等待 JavaScript 把预警列表写入 #alarmList。
        try:
            page.wait_for_selector("#alarmList li", timeout=45000)
        except Exception:
            # 某些时刻确实没有预警，仍然继续保存诊断信息。
            pass

        page.wait_for_timeout(8000)

        rows = page.locator("#alarmList li")
        count = rows.count()
        raw_items = []

        for i in range(count):
            row = rows.nth(i)

            title_locator = row.locator(".alarm-head span")
            time_locator = row.locator(".alarm-head em")
            body_locator = row.locator(".alarm-body")

            title = normalize(title_locator.first.inner_text()) if title_locator.count() else ""
            publish_time = normalize(time_locator.first.inner_text()) if time_locator.count() else ""
            detail = normalize(body_locator.first.inner_text()) if body_locator.count() else ""

            raw_items.append(
                {
                    "title": title,
                    "publish_time": publish_time,
                    "detail": detail,
                }
            )

        # 留下诊断文件；即使网站以后改版，也能快速定位。
        DEBUG_FILE.write_text(
            json.dumps(raw_items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        browser.close()

    alerts = []
    seen = set()

    for item in raw_items:
        title = item["title"]
        detail = item["detail"]
        publish_time = item["publish_time"]

        combined = f"{title} {detail}"

        if TARGET_REGION not in combined:
            continue

        level = next((level for level in TARGET_LEVELS if level in combined), None)
        if not level:
            continue

        start_dt = parse_time(publish_time or detail)
        alert_id = f"{TARGET_REGION}_{level}_{start_dt.strftime('%Y%m%d%H%M%S')}"

        if alert_id in seen:
            continue
        seen.add(alert_id)

        description_parts = [part for part in (title, detail) if part]
        description = "；".join(description_parts)

        alerts.append(
            {
                "id": alert_id,
                "title": title,
                "level": level,
                "date_from": start_dt.isoformat(),
                "date_to": None,
                "description": description,
                "active": True,
                "first_seen": now_shanghai().isoformat(),
                "last_seen": now_shanghai().isoformat(),
            }
        )

    return alerts, raw_items


def load_history():
    if not HISTORY_FILE.exists():
        return []

    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def merge_history(history, current_alerts):
    now = now_shanghai()
    by_id = {item.get("id"): item for item in history if item.get("id")}
    current_ids = {item["id"] for item in current_alerts}

    for current in current_alerts:
        existing = by_id.get(current["id"])
        if existing:
            existing["title"] = current["title"]
            existing["description"] = current["description"]
            existing["last_seen"] = now.isoformat()
            existing["active"] = True
            existing["date_to"] = None
        else:
            history.append(current)
            by_id[current["id"]] = current

    for item in history:
        if item.get("active") and item.get("id") not in current_ids:
            item["active"] = False
            item["date_to"] = now.isoformat()
            item["last_seen"] = now.isoformat()

    history.sort(key=lambda x: x.get("date_from", ""))
    return history


def format_datetime(value, active=False):
    if not value:
        return "生效中" if active else "—"

    try:
        return (
            datetime.fromisoformat(value)
            .astimezone(TZ)
            .strftime("%Y-%m-%d %H:%M:%S")
        )
    except Exception:
        return str(value)


def rows_for_last_7_days(history):
    cutoff = now_shanghai() - timedelta(days=7)
    result = []

    for item in history:
        try:
            start = datetime.fromisoformat(item["date_from"]).astimezone(TZ)
        except Exception:
            continue

        if start >= cutoff or item.get("active"):
            result.append(item)

    return result


def build_email(rows, is_test):
    subject_prefix = "【测试】" if is_test else ""
    subject = f"{subject_prefix}浦东新区橙色/红色天气预警周报"

    if rows:
        body_rows = []
        for row in rows:
            body_rows.append(
                "<tr>"
                f"<td>{html.escape(format_datetime(row.get('date_from')))}</td>"
                f"<td>{html.escape(format_datetime(row.get('date_to'), row.get('active', False)))}</td>"
                f"<td>{html.escape(row.get('description', ''))}</td>"
                "</tr>"
            )
        summary = f"共检测到 {len(rows)} 条符合条件的天气预警。"
        table_rows = "".join(body_rows)
    else:
        summary = "截至目前，过去7天没有检测到符合条件的天气预警。"
        table_rows = (
            "<tr><td>—</td><td>—</td>"
            "<td>过去7天未检测到浦东新区橙色或红色天气预警。</td></tr>"
        )

    body = f"""
    <html>
      <body style="font-family:Arial,'Microsoft YaHei',sans-serif;line-height:1.6">
        <h2>浦东新区橙色/红色天气预警周报</h2>
        <p><b>统计截止：</b>{now_shanghai().strftime("%Y-%m-%d %H:%M:%S")}（上海时间）</p>
        <p><b>监测范围：</b>上海市浦东新区，仅统计橙色和红色预警。</p>
        <p>{summary}</p>

        <table border="1" cellpadding="8" cellspacing="0"
               style="border-collapse:collapse;width:100%;">
          <thead>
            <tr>
              <th>Date From</th>
              <th>Date To</th>
              <th>Description</th>
            </tr>
          </thead>
          <tbody>{table_rows}</tbody>
        </table>

        <p style="color:#666;">
          数据来源：上海天气预警官网<br>
          {URL}
        </p>
      </body>
    </html>
    """

    return subject, body


def send_email(subject, body):
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("RECIPIENT_EMAIL", "june.shao@disney.com")

    if not gmail_user or not gmail_password:
        raise RuntimeError("GitHub Secrets 中缺少 GMAIL_USER 或 GMAIL_APP_PASSWORD")

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
    parser.add_argument(
        "--mode",
        choices=("collect", "weekly", "test"),
        default="collect",
    )
    args = parser.parse_args()

    current_alerts, raw_items = fetch_alerts_from_dom()

    print(f"#alarmList 中共读取到 {len(raw_items)} 条网页预警。")
    for item in raw_items:
        print(
            "网页条目：",
            item.get("title"),
            "|",
            item.get("publish_time"),
        )

    print(
        f"其中符合“浦东新区 + 橙色/红色”的预警："
        f"{len(current_alerts)} 条。"
    )

    for alert in current_alerts:
        print("命中预警：", alert["description"])

    history = merge_history(load_history(), current_alerts)

    # 不直接修改仓库文件，避免运行中 git push 冲突。
    NEXT_HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if args.mode == "collect":
        print("本次仅采集并保存记录，不发送邮件。")
        return

    rows = rows_for_last_7_days(history)

    if args.mode == "weekly" and not rows:
        print("过去7天无符合条件的预警，不发送周报。")
        return

    subject, body = build_email(rows, is_test=(args.mode == "test"))
    send_email(subject, body)
    print("邮件已发送。")


if __name__ == "__main__":
    main()

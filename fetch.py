#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, ssl, smtplib, socket, time, html, threading
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List

import requests, feedparser
from dotenv import load_dotenv

# ---------------------- 全局限速器 ---------------------- #
_rate_lock = threading.Lock()
_last_request_time = 0.0
MIN_INTERVAL = 32  # 秒，最少等这么久才发下一条（3req/min → 每条 20s；取 32s 留 33% 余量）


def _rate_limited_get(session: requests.Session, url: str, timeout: int) -> requests.Response:
    global _last_request_time
    with _rate_lock:
        elapsed = time.time() - _last_request_time
        if elapsed < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - elapsed)
        _last_request_time = time.time()
    return session.get(url, timeout=timeout,
                       headers={"User-Agent": "arxiv-digest/1.0 (personal research tool)"})


# ----------------------------------------------------------- #

# ---------------------- 可选：中文翻译 ---------------------- #
from googletrans import Translator

_translator = Translator()                           # 默认即可

def zh(text: str) -> str:
    """
    英→中：优先用 googletrans；若抛异常或超时就返回原文，
    以保证脚本即使在 Google 被墙时也能继续发送邮件。
    """
    text = text.strip()
    try:
        return _translator.translate(text, dest="zh-cn").text      # src 自动检测
    except Exception as e:
        print(f"[warn] translate failed: {e}")
        return text            # 失败直接用英文
# ----------------------------------------------------------- #

ARXIV_API = (
    "https://export.arxiv.org/api/query"
    "?search_query={query}&sortBy=submittedDate&sortOrder=descending&max_results=50"
)
HTTP_TIMEOUT, RETRY, BACKOFF = 60, 5, 20

_session = requests.Session()


def _http_get(url: str) -> str:
    for n in range(RETRY):
        try:
            r = _rate_limited_get(_session, url, HTTP_TIMEOUT)
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                wait = int(retry_after) if retry_after and retry_after.isdigit() else BACKOFF * (2 ** n)
                print(f"[warn] 429 Too Many Requests — backing off {wait}s …")
                time.sleep(min(wait, 600))
                continue
            r.raise_for_status()
            return r.text
        except (requests.RequestException, socket.timeout) as e:
            wait = BACKOFF * (2 ** n)
            print(f"[warn] {e} — retry in {wait}s …")
            time.sleep(wait)
    raise RuntimeError("Failed after retries")

def fetch(query: str, hours: int = 24) -> List[dict]:
    since_utc = datetime.now(timezone.utc) - timedelta(hours=hours)
    raw = _http_get(ARXIV_API.format(query=query))
    feed = feedparser.parse(raw)
    out = []
    for e in feed.entries:
        pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        if pub < since_utc:
            continue
        title_en = e.title.replace("\n", " ")
        summ_en  = e.summary.replace("\n", " ")
        out.append({
            "title_en": title_en,
            "title_zh": zh(title_en),
            "url": e.link,
            "authors": ", ".join(a.name for a in e.authors),
            "abs_zh": zh(summ_en),
        })
        # print(zh(title_en))
    if not out:
        print("[Warning] No new papers found in the last 24 hours.")
    else:
        print(f"\t → {len(out)} papers")
    return out

# ---------- HTML 生成 --------------------------------------------------- #
def li_block(idx: int, p: dict) -> str:
    return (
        f"<p><b>[{idx}] {html.escape(p['title_en'])}</b></p>"
        f"<p>标题：{html.escape(p['title_zh'])}</p>"
        f"<p>链接：<a href='{p['url']}'>{p['url']}</a></p>"
        '<div style="max-height:120px; overflow-y:auto; '
        'background:#f5f5f5; padding:6px; border-radius:4px;">'
        f"<p>作者：{html.escape(p['authors'])}</p>"
        f"<p>摘要：{html.escape(p['abs_zh'])}</p>"
        "</div><hr>"
    )

def section_html(code: str, cname: str, papers: List[dict]) -> str:
    header = (
        f"<h2 style='text-align:center; color:#2f4f4f;'>"
        f"{code} {cname}，共计 {len(papers)} 篇</h2>"
    )
    body = "".join(li_block(i, p) for i, p in enumerate(papers, 1)) \
           or "<p>过去 24 h 暂无新稿 🎉</p>"
    return header + body

def build_email(cg, gr, pc) -> str:
    now_bj = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    return (
        "<html><body>"
        f"<p>更新时间：{now_bj} (Beijing)</p>"
        f"{section_html('cs.CG', '计算几何', cg)}"
        f"{section_html('cs.GR', '图形学', gr)}"
        f"{section_html('Point Cloud', '相关', pc)}"
        "</body></html>"
    )

# ---------- 邮件发送 ---------------------------------------------------- #
def send(html_body: str):
    host = os.getenv("EMAIL_HOST", "smtp.qq.com")
    port = int(os.getenv("EMAIL_PORT", "465"))
    user = os.environ["EMAIL_USER"]
    pwd  = os.environ["EMAIL_PASS"]
    to   = [x.strip() for x in os.environ["EMAIL_TO"].split(",")]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Daily arXiv Digest – CG, Graphics, Point Cloud"
    msg["From"] = user
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    ctx = ssl.create_default_context()
    smtp = (smtplib.SMTP_SSL(host, port, context=ctx, timeout=10)
            if port == 465 else smtplib.SMTP(host, port, timeout=10))
    if port != 465:
        smtp.starttls(context=ctx)
    smtp.login(user, pwd)
    smtp.send_message(msg)
    smtp.quit()
    print("[OK] Mail sent.")

# ---------- 主流程 ------------------------------------------------------ #
def main():
    print("[*] Fetching cs.CG …")
    cg = fetch("cat:cs.CG")
    print("[*] Fetching cs.GR …")
    gr = fetch("cat:cs.GR")
    print('[*] Fetching "point cloud" …')
    pc = fetch('ti:"point cloud"+OR+abs:"point cloud"')

    send(build_email(cg, gr, pc))

if __name__ == "__main__":
    load_dotenv()
    main()

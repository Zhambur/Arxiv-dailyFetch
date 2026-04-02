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
MIN_INTERVAL = 32  # 秒，最少等这么久才发下一条（arXiv 每分钟最多 3 条，取 32s 留余量）


def _rate_limited_get(session: requests.Session, url: str, timeout: int) -> requests.Response:
    global _last_request_time
    with _rate_lock:
        elapsed = time.time() - _last_request_time
        if elapsed < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - elapsed)
        _last_request_time = time.time()
    return session.get(url, timeout=timeout,
                       headers={"User-Agent": "arxiv-digest/1.0 (personal research tool)"})


# ---------------------- AI 摘要（Gemini） ---------------------- #
_GEMINI_AVAILABLE = False
_model = None

try:
    import google.generativeai as genai
    _api_key = os.getenv("GEMINI_API_KEY")
    if _api_key:
        genai.configure(api_key=_api_key)
        _model = genai.GenerativeModel("gemini-2.0-flash")
        _GEMINI_AVAILABLE = True
except ImportError:
    pass


def _ai_summary(title_en: str, abs_en: str) -> str:
    """
    用 Gemini 2.0 Flash 生成一句话中文摘要。
    API 未配置时直接返回空字符串。
    """
    if not _GEMINI_AVAILABLE or not _model:
        return ""
    prompt = (
        "你是一位计算机科学领域的学术助手。请为以下论文生成 1~2 句简洁的中文摘要，"
        "说明论文的核心贡献和方法要点，语言流畅专业。\n\n"
        f"标题：{title_en.strip()}\n\n摘要：{abs_en.strip()[:1500]}"
    )
    try:
        resp = _model.generate_content(prompt, request_options={"timeout": 30})
        return resp.text.strip()
    except Exception as e:
        print(f"[warn] Gemini summarize failed: {e}")
        return ""


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
        ai_sum = _ai_summary(title_en, summ_en)
        out.append({
            "title_en":  title_en,
            "title_zh":  ai_sum or title_en,   # 有 AI 摘要就用，没有则显示原文标题
            "url":       e.link,
            "authors":   ", ".join(a.name for a in e.authors),
            "abs_ai":    ai_sum,                # AI 摘要（中文）
            "abs_en":    summ_en,              # 英文原文摘要
        })
    if not out:
        print("[Warning] No new papers found in the last 24 hours.")
    else:
        print(f"\t → {len(out)} papers")
    return out

# ---------- 响应式 HTML 生成 --------------------------------------------------- #
_META = """
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
"""


def li_block(idx: int, p: dict) -> str:
    ai_note = (
        f"<p style='color:#1a7f37; font-weight:600;'>✦ AI 摘要：{html.escape(p['abs_ai'])}</p>"
        if p['abs_ai'] else ""
    )
    return f"""
<div class="paper-card">
  <div class="paper-idx">[{idx}]</div>
  <div class="paper-body">
    <h3 class="paper-title">{html.escape(p['title_en'])}</h3>
    {ai_note}
    <p class="paper-meta">
      <span class="label">作者：</span>{html.escape(p['authors'])}
    </p>
    <p class="paper-meta">
      <span class="label">摘要：</span>{html.escape(p['abs_en'][:400])}{'…' if len(p['abs_en']) > 400 else ''}
    </p>
    <p class="paper-link">
      <a href="{p['url']}" target="_blank">arXiv 链接 →</a>
    </p>
  </div>
</div>"""


def section_html(code: str, cname: str, papers: List[dict]) -> str:
    header = (
        f"<div class='section-header'>"
        f"<span class='section-code'>{code}</span>"
        f"<span class='section-name'>{cname}</span>"
        f"<span class='section-count'>{len(papers)} 篇</span>"
        f"</div>"
    )
    body = "".join(li_block(i, p) for i, p in enumerate(papers, 1)) \
           or "<p class='empty'>🎉 过去 24 小时暂无新稿</p>"
    return header + f"<div class='paper-list'>{body}</div>"


def build_email(cg, gr, pc) -> str:
    now_bj = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    ai_badge = " · AI 摘要" if _GEMINI_AVAILABLE else ""
    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
{_META}
<title>arXiv Daily{ai_badge}</title>
<style>
  /* === 全局重置与字体 === */
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI',
                 Roboto, 'Helvetica Neue', Arial, sans-serif;
    background: #f4f6f9;
    color: #1a1a2e;
    line-height: 1.7;
    padding: 16px;
  }}

  /* === 主容器 === */
  .container {{
    max-width: 760px;
    margin: 0 auto;
  }}

  /* === 顶部标题栏 === */
  .masthead {{
    text-align: center;
    padding: 24px 16px;
    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
    border-radius: 12px;
    margin-bottom: 24px;
    color: #fff;
  }}
  .masthead h1 {{
    font-size: 22px;
    font-weight: 700;
    letter-spacing: 0.5px;
  }}
  .masthead .subtitle {{
    font-size: 13px;
    opacity: 0.7;
    margin-top: 4px;
  }}
  .ai-badge {{
    display: inline-block;
    background: rgba(255,255,255,0.15);
    border: 1px solid rgba(255,255,255,0.3);
    border-radius: 20px;
    padding: 2px 10px;
    font-size: 12px;
    margin-top: 8px;
  }}

  /* === 分类区块 === */
  .section-header {{
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 16px;
    background: #fff;
    border-radius: 8px;
    margin-bottom: 12px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.06);
  }}
  .section-code {{
    font-family: 'Courier New', monospace;
    font-weight: 700;
    color: #4a90d9;
    font-size: 15px;
  }}
  .section-name {{
    font-weight: 600;
    color: #333;
    font-size: 15px;
  }}
  .section-count {{
    margin-left: auto;
    background: #eef2f8;
    color: #4a90d9;
    border-radius: 12px;
    padding: 2px 10px;
    font-size: 12px;
    font-weight: 600;
  }}

  /* === 论文卡片 === */
  .paper-list {{
    display: flex;
    flex-direction: column;
    gap: 10px;
    margin-bottom: 28px;
  }}
  .paper-card {{
    display: flex;
    gap: 12px;
    background: #fff;
    border-radius: 10px;
    padding: 14px 16px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.07);
    border-left: 4px solid #4a90d9;
    transition: box-shadow 0.2s;
    word-break: break-word;
  }}
  .paper-card:hover {{
    box-shadow: 0 3px 12px rgba(0,0,0,0.12);
  }}
  .paper-idx {{
    font-weight: 700;
    color: #aab4c4;
    font-size: 14px;
    min-width: 28px;
    padding-top: 2px;
  }}
  .paper-body {{ flex: 1; min-width: 0; }}
  .paper-title {{
    font-size: 15px;
    font-weight: 700;
    color: #1a1a2e;
    margin-bottom: 6px;
    line-height: 1.5;
  }}
  .paper-meta {{
    font-size: 13px;
    color: #555;
    margin-bottom: 4px;
  }}
  .label {{
    font-weight: 600;
    color: #333;
  }}
  .paper-link a {{
    color: #4a90d9;
    text-decoration: none;
    font-size: 13px;
    font-weight: 600;
  }}
  .paper-link a:hover {{ text-decoration: underline; }}
  .empty {{
    text-align: center;
    color: #999;
    padding: 24px;
    font-size: 14px;
  }}

  /* === 响应式：手机端 === */
  @media (max-width: 520px) {{
    body {{ padding: 10px; }}
    .masthead {{ padding: 18px 12px; }}
    .masthead h1 {{ font-size: 18px; }}
    .section-header {{
      flex-wrap: wrap;
      gap: 6px;
    }}
    .section-count {{ margin-left: 0; }}
    .paper-card {{
      flex-direction: column;
      gap: 6px;
      border-left-width: 3px;
    }}
    .paper-idx {{ display: none; }}
    .paper-title {{ font-size: 14px; }}
    .paper-meta, .paper-link {{ font-size: 12px; }}
  }}

  /* === 深色模式（邮件客户端支持） === */
  @media (prefers-color-scheme: dark) {{
    body {{ background: #1a1a2e; color: #e0e0e0; }}
    .section-header, .paper-card {{ background: #16213e; }}
    .section-name, .label, .paper-title {{ color: #e0e0e0; }}
    .section-code, .section-count, .paper-link a {{ color: #7ab3f5; }}
    .section-count {{ background: rgba(74,144,217,0.15); }}
    .paper-meta {{ color: #aaa; }}
    .masthead {{ background: #0f3460; }}
  }}
</style>
</head>
<body>
<div class="container">
  <div class="masthead">
    <h1>arXiv Daily{ai_badge}</h1>
    <div class="subtitle">更新时间：{now_bj} (北京时间)</div>
    <div class="ai-badge">Gemini 2.0 Flash</div>
  </div>
  {section_html('cs.CG', '计算几何', cg)}
  {section_html('cs.GR', '图形学', gr)}
  {section_html('Point Cloud', '点云相关', pc)}
</div>
</body>
</html>"""


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

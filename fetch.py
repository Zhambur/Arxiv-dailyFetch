#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, ssl, smtplib, socket, time, html, threading
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List

import requests, feedparser
from dotenv import load_dotenv
load_dotenv()

# ---------------------- 全局限速器，避免被arxiv api封禁 ---------------------- #
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


# ---------------------- 多 AI 摘要（支持多个提供商自动切换） ---------------------- #
_ai_provider = None

# 该位置可修改ai翻译摘要的风格，也可以添加其他ai提供商

def _build_providers():
    """
    按优先级返回可用的 AI provider。
    第一个成功的 provider 会被使用，后续的作为兜底。
    """
    providers = []

    # 1. 智谱 GLM（GLM-4-Flash，免费额度充足）
    glm_key = os.getenv("GLM_API_KEY")
    if glm_key:
        def glm_summary(title_en, abs_en):
            payload = {
                "model": "glm-4-flash",
                "messages": [
                    {"role": "system", "content": "你是一位计算机科学领域的学术助手。请严格参照以下论文的摘要，翻译为通顺的中文摘要，不要遗漏任何信息。最后额外说明论文的核心贡献和方法要点，语言流畅专业。"},
                    {"role": "user", "content": f"标题：{title_en.strip()}\n\n摘要：{abs_en.strip()[:1500]}"}
                ],
                "temperature": 0.3,
            }
            r = requests.post(
                "https://open.bigmodel.cn/api/paas/v4/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {glm_key}", "Content-Type": "application/json"},
                timeout=30,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()

        providers.append(("GLM (glm-4-flash)", glm_summary))

    # 2. DeepSeek
    deepseek_key = os.getenv("DEEPSEEK_API_KEY")
    if deepseek_key:
        def deepseek_summary(title_en, abs_en):
            payload = {
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": "你是一位计算机科学领域的学术助手。请严格参照以下论文的摘要，翻译为通顺的中文摘要，不要遗漏任何信息。最后额外说明论文的核心贡献和方法要点，语言流畅专业。"},
                    {"role": "user", "content": f"标题：{title_en.strip()}\n\n摘要：{abs_en.strip()[:1500]}"}
                ],
                "temperature": 0.3,
            }
            r = requests.post(
                "https://api.deepseek.com/v1/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {deepseek_key}", "Content-Type": "application/json"},
                timeout=30,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()

        providers.append(("DeepSeek (deepseek-chat)", deepseek_summary))

    # 3. Gemini
    try:
        from google import genai
        gemini_key = os.getenv("GEMINI_API_KEY")
        if gemini_key:
            client = genai.Client(api_key=gemini_key)

            def gemini_summary(title_en, abs_en):
                prompt = (
                    "你是一位计算机科学领域的学术助手。请严格参照以下论文的摘要，翻译为通顺的中文摘要，不要遗漏任何信息。"
                    "最后额外说明论文的核心贡献和方法要点，语言流畅专业。\n\n"
                    f"标题：{title_en.strip()}\n\n摘要：{abs_en.strip()[:1500]}"
                )
                resp = client.models.generate_content(
                    model="gemini-2.0-flash",
                    contents=prompt,
                )
                return resp.text.strip()

            providers.append(("Gemini (gemini-2.0-flash)", gemini_summary))
    except ImportError:
        pass

    return providers


_providers = _build_providers()
if _providers:
    _ai_provider = _providers[0][0]
    print(f"[OK] AI 摘要已启用 — 主提供商: {_ai_provider}（共 {len(_providers)} 个可用）")
else:
    print("[warn] 未检测到任何 AI API Key，AI 摘要功能已禁用")


def _ai_summary(title_en: str, abs_en: str) -> str:
    """遍历所有 provider，返回第一个成功的结果。"""
    for name, fn in _providers:
        try:
            return fn(title_en, abs_en)
        except Exception as e:
            print(f"[warn] {name} 摘要失败: {e}")
    return ""


# ---------- AI 相关性过滤 ------------------------------------------------------ #
def _ai_filter_relevant(papers: List[dict], category: str) -> List[dict]:
    """
    用 LLM 批量评估论文与给定类别的相关性，过滤低分论文。
    返回相关性评分 >= 3（满分 5）的论文。
    同时补全缺失的 AI 摘要（仅对保留论文调用）。
    """
    if not papers:
        return []

    paper_lines = "\n".join(
        f"[{i}] 标题：{p['title_en']}\n"
        f"    摘要：{p['abs_en'][:400]}"
        for i, p in enumerate(papers)
    )

    prompt = f"""你是一个严格的计算机科学论文相关性评审专家。

目标类别：{category}
评分标准（1-5 分）：
  1 = 完全无关（如物理、天文、化学、生物等非 CS 领域）
  2 = 勉强相关，但侧重点偏离（如纯理论、无应用场景）
  3 = 弱相关，主要贡献在其他领域（相关性一般）
  4 = 强相关，属于该领域的重要工作
  5 = 高度相关，核心贡献精确命中该领域

请逐篇评分，格式：每行一个，如「[0] 4」。

候选论文（共 {len(papers)} 篇）：
{paper_lines}

评分结果（每行一个）："""

    score_lines = []
    for name, fn in _providers:
        try:
            r = requests.post(
                "https://open.bigmodel.cn/api/paas/v4/chat/completions",
                json={
                    "model": "glm-4-flash",
                    "messages": [
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.1,
                },
                headers={
                    "Authorization": f"Bearer {os.getenv('GLM_API_KEY')}",
                    "Content-Type": "application/json",
                },
                timeout=60,
            )
            r.raise_for_status()
            raw = r.json()["choices"][0]["message"]["content"].strip()
            score_lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            break
        except Exception as e:
            print(f"[warn] AI 过滤器 {name} 调用失败: {e}")

    if not score_lines:
        print(f"[warn] 所有 AI 过滤器均失败，返回全部 {len(papers)} 篇（未过滤）")
        return papers

    score_map = {}
    for line in score_lines:
        # 支持 "[0] 4" 或 "[0] 4/5" 等格式
        parts = line.lstrip("[").split("]", 1)
        if len(parts) != 2:
            continue
        idx_str = parts[0].strip()
        score_str = parts[1].strip().split()[0].rstrip("/")
        try:
            idx = int(idx_str)
            score = int(score_str)
            score_map[idx] = score
        except ValueError:
            continue

    passed = []
    for i, p in enumerate(papers):
        score = score_map.get(i, 0)
        if score >= 3:
            if not p.get("abs_ai"):
                p["abs_ai"] = _ai_summary(p["title_en"], p["abs_en"])
            passed.append(p)

    kept = len(passed)
    dropped = len(papers) - kept
    if dropped:
        print(f"  → AI 过滤：保留 {kept} 篇，剔除 {dropped} 篇（评分 < 3）")
    else:
        print(f"  → AI 过滤：全部 {kept} 篇通过")
    return passed


# ----------------------------------------------------------- #
# 这里可以修改抓取论文的api源

ARXIV_API = (
    "https://export.arxiv.org/api/query"
    "?search_query={query}&sortBy=submittedDate&sortOrder=descending&max_results=10"
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

# 这里可以修改抓取论文的篇数和时间范围

def fetch(query: str, hours: int = 24, max_results: int = 10) -> List[dict]:
    since_utc = datetime.now(timezone.utc) - timedelta(hours=hours)
    url = f"https://export.arxiv.org/api/query?search_query={query}&sortBy=submittedDate&sortOrder=descending&max_results={max_results}"
    raw = _http_get(url)
    feed = feedparser.parse(raw)
    out = []
    for e in feed.entries:
        pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        if pub < since_utc:
            continue
        title_en = e.title.replace("\n", " ")
        summ_en  = e.summary.replace("\n", " ")
        out.append({
            "title_en":  title_en,
            "title_zh":  title_en,
            "url":       e.link,
            "authors":   ", ".join(a.name for a in e.authors),
            "abs_ai":    "",               # 由 _ai_filter_relevant() 统一补全
            "abs_en":    summ_en,
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
        f"<div class='ai-note'>"
        f"<span class='ai-label'>✦ AI 摘要</span>"
        f"<span class='ai-text'>{html.escape(p['abs_ai'])}</span>"
        f"</div>"
        if p['abs_ai'] else ""
    )
    return f"""
<div class="paper-card">
  <div class="paper-idx">[{idx}]</div>
  <div class="paper-body">
    <h3 class="paper-title">
      <a href="{p['url']}" target="_blank">{html.escape(p['title_en'])}</a>
    </h3>
    {ai_note}
    <p class="paper-meta">
      <span class="label">作者：</span>{html.escape(p['authors'])}
    </p>
    <p class="paper-meta abs-full">
      <span class="label">摘要：</span>{html.escape(p['abs_en'])}
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


def build_email(sections: dict) -> str:
    now_bj = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    ai_badge = f" · {_ai_provider}" if _ai_provider else ""
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
  .paper-title a {{
    color: #1a1a2e;
    text-decoration: none;
  }}
  .paper-title a:hover {{
    color: #4a90d9;
    text-decoration: underline;
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
  .ai-note {{
    background: #f0f7ff;
    border-left: 3px solid #1a7f37;
    border-radius: 4px;
    padding: 8px 12px;
    margin-bottom: 6px;
  }}
  .ai-label {{
    display: block;
    font-size: 11px;
    font-weight: 700;
    color: #1a7f37;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 3px;
  }}
  .ai-text {{
    font-size: 13px;
    color: #1a3a1a;
    line-height: 1.6;
  }}
  .paper-meta .abs-full {{
    white-space: pre-wrap;
  }}
  .paper-link a {{
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
  .footer {{
    text-align: center;
    color: #c0c8d8;
    font-size: 11px;
    padding: 16px 0 4px;
    letter-spacing: 0.3px;
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
    .no-papers {{ background: #16213e; color: #aaa; }}
  }}
  .no-papers {{
    text-align: center;
    padding: 48px 24px;
    background: #fff;
    border-radius: 12px;
    color: #666;
    margin-bottom: 24px;
  }}
  .no-papers-icon {{ font-size: 40px; margin-bottom: 12px; }}
  .no-papers-title {{ font-size: 18px; font-weight: 600; color: #1a1a2e; margin-bottom: 8px; }}
  .no-papers-sub {{ font-size: 14px; }}
</style>
</head>
<body>
<div class="container">
  <div class="masthead">
    <h1>arXiv Daily{ai_badge}</h1>
    <div class="subtitle">更新时间：{now_bj} (北京时间)</div>
    <div class="ai-badge">{_ai_provider or "Gemini 2.0 Flash"}</div>
  </div>
  {''.join(section_html(k, k, v) for k, v in sections.items()) if sections else f'''
  <div class="no-papers">
    <div class="no-papers-icon">📭</div>
    <div class="no-papers-title">今日无更新</div>
    <div class="no-papers-sub">过去 24 小时内各分类均无新论文，或 AI 过滤后无有效结果。<br>arXiv 在周末和节假日更新量较少属正常现象。</div>
  </div>'''}
  <div class="footer">made by Zhambur</div>
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
    msg["Subject"] = "Daily arXiv Fetch"
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
# arXiv CS 分类说明：
#   cs.AI  Artificial Intelligence       — AI Agent、推理、规划、知识图谱
#   cs.LG  Machine Learning              — 深度学习、强化学习、LLM
#   cs.CV  Computer Vision                — 图像/视频生成、目标检测、VQA、OCR
#   cs.RO  Robotics                      — 具身智能、机械臂、导航、sim-to-real
#   cs.CL  Computation and Language      — NLP、多模态大模型
#   cs.HC  Human-Computer Interaction    — GUI Agent、Web Agent
#   cs.MA  Multiagent Systems            — 多智能体系统
#
# 关键词前缀说明：
#   all:    全文检索（最宽）
#   ti:     限定标题（精准，但可能漏掉一些）
#   abs:    限定摘要
#   cat:    限定 arXiv 分类

_MODULES = [
    ("具身智能",
     '(cat:cs.RO OR cat:cs.AI) AND (all:VLA OR all:"vision language action" OR all:"vision-language-action" OR all:"embodied agent" OR all:"embodied AI" OR all:"physical AI" OR all:"robot manipulation" OR all:"dual-arm" OR all:"bimanual" OR ti:dexterous OR all:"whole-body" OR all:"in-hand manipulation" OR all:"imitation learning" OR all:"behavior cloning" OR all:dAgger OR all:LfD OR all:"reinforcement learning" OR ti:manipulation OR all:"visual navigation" OR all:ObjectNav OR all:"point-goal" OR all:"sim-to-real" OR all:"domain randomization")'),

    ("多模态大模型",
     '(cat:cs.CV OR cat:cs.CL OR cat:cs.LG) AND (all:"multimodal LLM" OR all:"vision language model" OR ti:LVLM OR all:LLaVA OR all:Vary OR all:Grounding OR all:InternVL OR all:CLIP OR all:BLIP OR all:SigLIP OR all:"LLaMA-Factory" OR all:LoRA OR all:"instruction tuning")'),

    ("世界模型 & 规划",
     '(cat:cs.RO OR cat:cs.AI) AND (all:"world model" OR all:"visual planning" OR all:"task planning" OR all:"motion planning" OR ti:reasoning OR all:"coarse-to-fine" OR all:"video prediction" OR all:"future prediction")'),

    ("图像生成 & 理解",
     'cat:cs.CV AND (all:"diffusion model" OR all:"text-to-image" OR all:"text-to-video" OR all:"image generation" OR all:"visual reasoning" OR all:VQA OR all:"visual question answering" OR all:"image captioning" OR all:"visual program" OR all:"Multimodal OCR" OR all:"document understanding" OR all:"visual chain-of-thought" OR all:GAN OR all:VAE)'),

    ("AI Agent",
     '(cat:cs.AI OR cat:cs.HC OR cat:cs.MA) AND (all:"multimodal agent" OR all:"visual agent" OR all:"GUI agent" OR all:"web agent" OR all:"tool use" OR all:"tool learning" OR all:"function calling" OR all:RAG OR all:"retrieval-augmented" OR all:"memory agent" OR all:"multi-agent" OR all:"agent collaboration" OR all:"agent swarm" OR all:"autonomous agent" OR all:"interactive agent")'),
]


def main():
    if not _providers:
        print("[warn] 未检测到任何 AI API Key，AI 摘要功能已禁用")
    sections = {}
    for name, query in _MODULES:
        # 具身智能 30 篇，其余 15 篇，留足余量给 AI 过滤器淘汰
        n = 30 if name == "具身智能" else 15
        print(f"[*] Fetching — {name} (max={n}) …")
        raw = fetch(query, max_results=n)
        print(f"    → 抓取原始论文 {len(raw)} 篇")
        papers = _ai_filter_relevant(raw, category=name)
        print(f"    → AI 过滤后剩余 {len(papers)} 篇")
        if not papers:
            print(f"[warn] {name} 过滤后无剩余论文，跳过该模块")
            continue
        sections[name] = papers

    print(f"[*] 共 {len(sections)} 个模块有有效论文")
    send(build_email(sections))


if __name__ == "__main__":
    main()

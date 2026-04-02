# arxiv-daily

每日自动抓取 arXiv 最新论文，翻译标题与摘要，生成 HTML 邮件推送到你的邮箱。

**GitHub Actions 定时任务**：每天北京时间 08:30 自动运行。

## 追踪领域

| 分类 | arXiv 标签 | 中文 |
|------|-----------|------|
| 计算几何 | `cat:cs.CG` | Computational Geometry |
| 计算机图形学 | `cat:cs.GR` | Graphics |
| 点云相关 | `ti:"point cloud" OR abs:"point cloud"` | Point Cloud |

## 快速开始

### 1. 克隆 & 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

创建 `.env` 文件（参考 `.env.example`）：

```env
# 邮件发送（支持任意 SMTP 服务）
EMAIL_HOST=smtp.qq.com
EMAIL_PORT=465
EMAIL_USER=your_email@example.com
EMAIL_PASS=your_smtp授权码
EMAIL_TO=receiver@example.com

# Gemini API（用于论文摘要生成，可选）
# 获取地址：https://aistudio.google.com/apikey
GEMINI_API_KEY=your_api_key
```

> 邮件配置说明：QQ 邮箱使用 SSL 端口 465，授权码在「设置 → 账户 → POP3/SMTP」获取。

### 3. 本地运行

```bash
python fetch.py
```

### 4. GitHub Actions 定时运行

在 GitHub 仓库 Settings → Secrets and variables → Actions 中配置以下 Secrets：

| Secret | 说明 |
|--------|------|
| `EMAIL_USER` | 发件邮箱 |
| `EMAIL_PASS` | SMTP 授权码 |
| `EMAIL_HOST` | SMTP 服务器（如 `smtp.qq.com`） |
| `EMAIL_PORT` | 端口（如 `465`） |
| `EMAIL_TO` | 收件人，多个用逗号分隔 |
| `GEMINI_API_KEY` | Gemini API 密钥（可选，不填则跳过 AI 摘要） |

定时任务在每天 **UTC 00:30（北京时间 08:30）** 自动执行。

## 依赖

- `requests` — HTTP 请求
- `feedparser` — 解析 arXiv Atom Feed
- `python-dotenv` — 环境变量读取
- `google-generativeai` — Gemini AI 摘要（可选）

## 注意事项

- **arXiv API 限速**：每 IP 每分钟最多 3 条请求，脚本内置全局节流器（每请求间隔 32s）。
- **避免重复触发**：不要短时间内多次手动运行，建议使用 GitHub Actions 定时任务。
- **Google 翻译**：googlethrans 已废弃，默认改用 Gemini API 生成摘要。

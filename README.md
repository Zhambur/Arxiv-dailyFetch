# arxiv-daily

每日自动抓取 arXiv 最新论文，翻译标题与摘要，生成 HTML 邮件推送到你的邮箱。

## 追踪领域

默认设定检索关键词，覆盖**具身智能**、**多模态大模型**、**世界模型 & 规划**、**图像生成 & 理解**、**AI Agent** 五个模块。其中具身智能每批次抓取 20 篇，其余模块各 10 篇。

篇数、追踪领域、ai摘要风格等均可自行调整，请将代码拉取到本地，并修改`fetch.py`，详细修改方法见代码注释。

## 快速开始

> 若无本地（如测试、修改代码）需求，请直接见步骤4。

### 1. 克隆 & 安装依赖

```bash
git clone <web URL>
pip install -r requirements.txt
```

### 2. 配置环境变量

> 若有该需求，在推送至远程仓库时，注意将该文件添加至 `.gitignore` 中，避免泄露隐私信息。

创建 `.env` 文件：

```env
# 邮件发送（支持 SMTP 服务）
EMAIL_HOST=smtp.qq.com
EMAIL_PORT=465
EMAIL_USER=your_email@example.com
EMAIL_PASS=your_smtp 授权码
EMAIL_TO=receiver@example.com

# AI 摘要（至少配置一个，不填则跳过 AI 摘要）
# 优先级：GLM → DeepSeek → Gemini（自动切换，失败时使用下一个）
GLM_API_KEY=       # https://bigmodel.cn/ 
DEEPSEEK_API_KEY=  # https://platform.deepseek.com/ 
GEMINI_API_KEY=    # https://aistudio.google.com/apikey
```

> 邮件配置：记得开启SMTP服务，「设置 → 账户与安全 → 安全设置 → POP3/SMTP」开启并获取授权码；SMTP服务使用 SSL 端口 465。

### 3. 本地运行

> 该步骤可以测试任务是否正常完成。

```bash
python fetch.py
```

### 4. GitHub Actions 定时运行

请确保已经 fork 或本地推送该 repo 到您的账户, 并已经开启SMTP服务（见2：邮箱配置）。在 GitHub 仓库 Settings → Secrets and variables → Actions 中，点击 New repository secret 配置以下 Secrets：

| Secret | 说明 |
|--------|------|
| `EMAIL_USER` | 发件邮箱 |
| `EMAIL_PASS` | SMTP 授权码 |
| `EMAIL_HOST` | SMTP 服务器（如 `smtp.qq.com`） |
| `EMAIL_PORT` | 端口（如 `465`） |
| `EMAIL_TO` | 收件人，多个用逗号分隔 |
| `GLM_API_KEY` | 智谱 GLM 密钥（可选） |
| `DEEPSEEK_API_KEY` | DeepSeek 密钥（可选） |
| `GEMINI_API_KEY` | Gemini 密钥（可选，apikey都不填则跳过 AI 摘要，呈现原文摘要） |

如有其他apikey需求，请自行在代码中修改，并创建对应的secret。

定时任务在每天 **UTC 00:00（北京时间 08:00）** 自动执行，通常存在 2-3 个小时的误差，这可能是 Actions 自带的发送时间偏差，也可能是SMTP服务的延迟（此处没有深入探讨）。

## 依赖

- `requests` — HTTP 请求
- `feedparser` — 解析 arXiv Atom Feed
- `python-dotenv` — 环境变量读取

## 注意事项

- **arXiv API 限速**：每 IP 每分钟最多 3 条请求，脚本内置全局节流器（每请求间隔 32s）。
- **避免重复触发**：不要短时间内多次手动运行，建议使用 GitHub Actions 定时任务。
- **AI 摘要自动切换**：请求失败时会自动切换到下一个 provider，全部失败则跳过该篇摘要。 

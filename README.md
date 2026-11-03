# Hokas-Renew

hokas.net 免费服务器自动续期脚本，跑在 GitHub Actions 上，每 6 小时自动执行一次。
结构参考同账号的 Pella-Renew 项目并针对 hokas 做了适配。

## 它做什么

1. 打开 `https://hokas.net/sign-in`，用**邮箱 + 邮箱验证码**自动登录（Clerk）：
   验证码优先用 IMAP 直接读邮箱（需 Gmail 应用专用密码），读不到时可用 Gmail 网页版兜底。
2. 进入目标服务器（默认 `myappas`）的 Manage → Overview，记录当前 Remaining 剩余时间。
3. 在 "SERVER EXPIRING" 面板依次处理 **Add 24 Hours** 和 **Add 12 Hours** 两个按钮
   （每个按钮 6 小时冷却，与定时任务的 6 小时间隔对齐，两次都赶上就能 +36 小时）。
4. 点 Add 会新开一个 shrinkme.click 广告短链，脚本自动走完三步：
   过 Cloudflare 验证 → 中间页点 CLICK TO VERIFY → 倒计时后点 Get Link → 跳回 hokas.net。
   途中弹出的广告窗会被自动关掉。
5. 每走完一个按钮，回到 Overview **复查剩余时间**，确认真的到账才算成功；
   两个都没到账会以失败状态结束（Actions 里标红）。结果通过 Telegram 推送（可选）。

## ⚠️ 先说清楚风险（shrinkme 这一关）

- hokas 的续期**不是**面板上点一下就到账，必须走完 shrinkme 的广告链，
  而 shrinkme 的 Cloudflare 验证对自动化浏览器非常不友好：
  手工实测中普通浏览器环境曾连续多次出现「正在验证… 然后重置」。
- 本脚本用 SeleniumBase UC 模式 + 耐心等待 + 多轮重试去过这一关，
  **但不保证每次都能过**。过不去时运行会失败标红，请下载 Artifacts 里的截图看卡在哪一步
  （截图文件名带序号，按顺序看就知道流程走到哪了）。
- 本项目**不使用**任何第三方付费打码服务，也不绕过付费功能。
- 这是对免费额度的正常续期操作，但自动化操作可能违反站点条款的风险请自行评估。

## 部署步骤

### 1. 建仓库并放入文件

新建一个 GitHub 仓库（建议**私有**，例如 `Hokas-Renew`），把本目录文件按原结构放进去：

```
hokas.py
requirements.txt
.github/workflows/renew.yml
README.md
```

### 2. 准备 Gmail 应用专用密码（IMAP 取码用）

EMAIL_PASSWORD 填的**不是** Gmail 登录密码，而是应用专用密码：

1. 用收验证码的那个 Google 账号登录，先确认已开启两步验证
   （Google 账号 → 安全性 → 两步验证）。
2. 打开 <https://myaccount.google.com/apppasswords>，
   应用名随便填（比如 `hokas-renew`），点创建。
3. 会得到 16 位密码（显示成 4×4 带空格），完整复制下来填到 Secret 里
   （空格不用管，脚本登录 IMAP 时会自动去掉）。

### 3. 添加 Secrets

仓库 → Settings → Secrets and variables → Actions → New repository secret，逐个添加：

| Secret 名 | 必填 | 值从哪来 |
|---|---|---|
| `EMAIL` | ✅ | hokas.net 的登录邮箱（同时也是收验证码的邮箱） |
| `EMAIL_PASSWORD` | ✅（与下一行二选一） | 上一步生成的 Gmail 应用专用密码 |
| `GMAIL_PASSWORD` | 可选 | Gmail 网页登录密码；仅 IMAP 读不到码时兜底用。注意：开了两步验证/风控时网页登录常被拦，能不用就不用 |
| `CODE_SOURCE` | 可选 | 取码方式：`auto`（默认，先 IMAP 再网页）/ `imap` / `web`，留空即可 |
| `IMAP_SERVER` | 可选 | 默认 `imap.gmail.com`，留空即可 |
| `IMAP_PORT` | 可选 | 默认 `993`，留空即可 |
| `SERVER_NAME` | 可选 | 服务器名；留空时优先找 `myappas`，找不到就点列表第一个 |
| `PANEL_URL` | 可选 | 默认 `https://hokas.net`，留空即可 |
| `SIGN_IN_URL` | 可选 | 默认 `https://hokas.net/sign-in`，留空即可 |
| `TG_BOT_TOKEN` | 可选 | Telegram 机器人 token（找 @BotFather 创建），不配就不推送 |
| `TG_CHAT_ID` | 可选 | 接收推送的 chat id（给机器人发一句话后用 @userinfobot 等查） |

### 4. 第一次手动运行

1. 仓库 → Actions → 左侧选「Hokas 自动续期」→ Run workflow → Run。
2. 等它跑完（正常约 5–20 分钟，shrinkme 验证重试会拉长时间）。
3. 点进这次运行：
   - 底部 **Job Summary** 里有完整日志；
   - **Artifacts → screenshots** 下载截图压缩包。
4. 对照日志里「续期前剩余 / 续期后剩余」，确认时间真的增加了。

之后不用管它，定时任务每 6 小时（UTC 00:15 / 06:15 / 12:15 / 18:15）自动跑。

## 怎么看截图排查

截图按执行顺序编号，大致对应关系：

| 截图前缀 | 阶段 |
|---|---|
| `open_signin` / `check_email` / `code_filled` / `after_submit_code` | 登录：开页面 → 等验证码 → 填码 → 提交 |
| `g1/g2/g3_gmail_*`、`g_error_*` | Gmail 网页版取码兜底流程 |
| `logged_in` / `overview` | 登录成功、进入服务器 Overview |
| `add24h_*` / `add12h_*` | 对应按钮的 shrinkme 全流程：`shrinkme_open` 打开短链 → `turnstile_*` 验证 → `after_step1` → `step_page`（中间页）→ `after_step2` → `getlink_page` → `back_on_hokas` 跳回 → `after_check` 复查结果 |
| `error_*` | 出错现场，先看最新的一张 |

常见情况：

- **失败在登录页/验证码**：先看日志有没有「IMAP 认证失败」——基本是应用专用密码不对
  或没开两步验证；日志里会打印最近邮件清单，可确认验证码邮件到底进没进邮箱。
- **`turnstile_failed`**：shrinkme 的 Cloudflare 这次没放行，属已知风险；
  等下一次定时运行（或手动再 Run 一次），换个时段通过率不一样。
- **`walk` 走完但 `not_credited`**（日志写「流程走完但未确认到账」）：
  多半是最后 Get Link 的跳转被弹窗吞了或回跳没落到 hokas；把 `addXXh_getlink_page`
  前后的截图发出来再针对性调整点击策略。
- **按钮 unavailable/冷却中**：上一次续期还没满 6 小时，属正常，等下一轮即可；
  但注意：两个按钮都没到账时本次运行会标红，这是预期设计，不用惊慌，
  先看日志区分是冷却还是真失败。

## 第二个 hokas 账号怎么加

两个账号的登录邮箱、验证码邮箱都不同，**最简单的办法是再复制一份仓库**：

1. 新建第二个仓库（如 `Hokas-Renew-2`），把同样的文件放进去；
2. Secrets 全部换成第二个账号的值
   （`EMAIL` 换成第二个账号邮箱，`EMAIL_PASSWORD` 换成**那个邮箱**的应用专用密码，
   `SERVER_NAME` 填第二个账号里的服务器名，比如 `mypyapp`）；
3. 手动跑一次确认到账即可，两个仓库互不干扰。

（也可以在同一仓库加第二个 workflow + 第二套 Secret 后缀，改动更多，不推荐。）

## 本地调试（可选）

```bash
pip install -r requirements.txt
EMAIL=你的邮箱 EMAIL_PASSWORD=应用专用密码 python3 hokas.py
```

本地需要有图形环境（脚本用 UC 模式非 headless 运行）。仍然建议优先在 Actions 上调试，
因为运行环境 IP 与本地不同，shrinkme 验证结果可能不一样。

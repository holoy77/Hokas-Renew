# BUILD_NOTES — Hokas-Renew（给主代理的交接说明）

## 做了什么

照 Pella-Renew（pella.py + renew.yml + requirements.txt，已通读原文）镜像结构，
只替换业务段：Pella 的「点面板 Claim 按钮」→ hokas 的「点 Add 按钮 + 走 shrinkme 三步
+ 回 Overview 用剩余时间增量验账」。登录段（Clerk 邮箱验证码、IMAP 优先取码、
Gmail 网页兜底、OTP 三种填法、誤入 Google OAuth 的回退）几乎原样移植，
仅把发件人匹配从 pella 改为 hokas/clerk、登录成功判据从 "Your Projects"
改为 My Servers/Dashboard/服务器名。

## 设计决定

- **验账不信跳转、只信数字**：Get Link 点完哪怕跳回了 hokas，也要回 Overview
  比对 Remaining 增量 ≥ 标称时长 80% 才记 credited；与今晚手工实测教训一致
  （流程走完 ≠ 到账）。
- **退出码语义**：任一按钮 credited → 0；两个都没到账（含冷却 unavailable）
  → exit 2；异常 → exit 1。冷却也会标红，是按任务要求定的，
  首次运行后若觉得吵，可再改成 unavailable 不算失败。
- **popunder 处理**：每次点击后统一 close_extra_windows（只保留 main + flow 两个
  handle），flow 窗口内重点最多 3 次，Get Link 最多 4 次；
  主窗口被误关时有兜底（找任意 hokas 窗口当 main）。
- **Turnstile**：pass_turnstile() 用 sb.uc_gui_click_captcha() + 45 秒/轮的耐心
  等待，总时限 150 秒，放行判据是 Continue 按钮出现或 CF iframe 消失。
  这是全脚本最不确定的一环（普通浏览器实测 5+ 次全被重置，
  但 Pella 脚本在 GH runner 上用 UC 模式能过 Turnstile，只能实跑验证）。
- **定时**：cron `15 */6 * * *` UTC，对齐 Add 按钮 6 小时冷却；
  加了 concurrency 组防重叠；timeout 放到 40 分钟（Pella 是 20，
  shrinkme 两条链 + 验证重试更慢，今晚 Pella 参考运行曾有 ~24 分钟的）。
- cron 之外还有 workflow_dispatch，首跑让用户手动触发看结果。
- 截图改带两位序号前缀（save_shot 内计数器），README 里有截图对照表。

## 选择器与脆弱点（按风险排序）

1. **shrinkme 全链**（未见过真实 DOM，全部来自今晚手工走查的文案）：
   - 验证页按钮：XPath 文本 "Click here to continue"；
   - 中间页（themezon.net）：文本 "STEP 1 OF 2" + "CLICK TO VERIFY"；
   - Get Link 页：文本 "YOUR LINK IS ALMOST READY" + "Get Link" 按钮
     （已避开 "Direct Download Link" 广告横幅，因为选择器要求精确包含
     "Get Link" 且广告文案是 Download 不会命中，但若广告改文案需警惕）。
   - 倒计时用「Get Link 存在且任一可点击」判就绪，is_clickable 沿用 Pella 的
     class 分词逻辑。
   - 风险：shrinkme 可能换中间页域名/步数（也可能出现 STEP 2 OF 2 类页面，
     当前脚本只处理了已验证的一页中间页；若第二步页出现，Get Link 等待会超时，
     截图名 after_step2 / getlink_page 能看出来）。
2. **hokas 面板**（来自今晚实测描述，无 DOM）：
   - Add 按钮：XPath `contains "Add" + contains "24"/"12"` 的 button/a；
   - Overview 入口：服务器名文本点击 → Manage → Overview 文本点击，多级兜底；
   - Remaining 解析 parse_remaining()：优先在 remaining/expires 上下文 200 字符内
     找 HH:MM:SS 或 "X hours Y minutes"，找不到再全页找。若面板实际文案是
     纯数字+单位的其它格式（如 "7.9 hours" 小数），解析会失败并在
     error_no_remaining 截图暴露，需要按截图补正则。
3. **登录判据** page_has_servers() 是文本启发式（My Servers / Dashboard /
   myappas 任一），若面板改版可能误判，error_after_code 截图可查。

## 第一次真实运行后要检查

1. Job Summary 日志里：续期前 Remaining 是否成功解析（分钟数是否合理，
   约几百分钟量级）；解析错位（抓到页面别处时长）是最隐蔽的坑。
2. Artifacts 截图按序号过一遍，确认三步 shrinkme 的实际页面与假设一致，
   特别是：是否有第二张中间页、Get Link 是否需要先等真实倒计时结束、
   跳回的是 hokas 哪个 URL。
3. Turnstile 在 runner 上的通过率：若 turnstile_failed 反复出现，
   说明 GH runner IP 段被 shrinkme 的 CF 拦得比预期狠，
   届时再考虑加长等待/改时段，而不是上打码服务（任务已明确禁止）。
4. TG 推送是否到达、exit code 与实际结果是否一致（尤其冷却标红是否符合用户预期）。
5. 第二个账号（mypyapp）还没接入：等用户决定用「复制仓库」方案时，
   提醒她 EMAIL_PASSWORD 必须是第二个邮箱自己的应用专用密码。

## 未做/未验证

- 未实际运行（本机无凭据且不应在此环境跑真实续期）；仅做了 py_compile 语法检查。
- 未包含 Cleanup-Old-Workflow（Pella 那个会改写 README 的清理流），
  本项目 README 是正经文档，不要让清理流覆盖它；若以后要加清理，
  让它写单独文件。

# -*- coding: utf-8 -*-
"""
hokas.net 免费服务器自动续期脚本
(结构参考同账号的 Pella-Renew 项目: Clerk 邮箱验证码登录 + IMAP 取码 + SeleniumBase UC)

流程:
  1. 打开 hokas 登录页(hokas.net/sign-in), 输入邮箱, 点击 Continue
  2. 跳转到验证码页(factor-one), 同时取验证码:
     - IMAP 直读邮箱(默认, 需应用专用密码)
     - 或浏览器登录 Gmail 网页版从邮件标题读码
     取到后填入 6 个分格输入框, 点击 Continue 完成登录
  3. 进入 Dashboard / My Servers, 打开目标服务器(默认 myappas, 或列表第一个)
     的 Manage -> Overview, 读取并记录 Remaining 剩余时间
  4. 在 "SERVER EXPIRING" 面板依次处理两个按钮(每个 6 小时可用一次):
       Add 24 Hours -> Add 12 Hours
     点击后会新开一个 shrinkme.click 短链标签页, 需要走完广告链:
       a) 验证页: 过 Cloudflare Turnstile 勾选框, 点 "Click here to continue"
       b) 中间页(themezom.net, ShrinkMe.io 样式): "STEP 1 OF 2",
          点大号蓝色按钮 "CLICK TO VERIFY"
       c) 回到 shrinkme.click: "YOUR LINK IS ALMOST READY." 倒计时结束,
          点绿色 "Get Link" 按钮, 浏览器跳回 hokas.net 才算这一单完成
     注意: 每次点击都可能弹广告窗(popunder, 如 VPN 安装页/YouTube 等),
     真流程始终在 shrinkme 标签页里, 脚本会在每次点击后关掉多余窗口并切回。
  5. 每个按钮走完后回 Overview 复查 Remaining 时间, 前后对比确认到账
     (两个按钮都没到账则以非 0 退出码结束, 便于在 Actions 里看出失败)
  6. 结果通过 Telegram Bot 推送, 全程关键步骤截图到 screenshots/

环境变量(GitHub Secrets):
  EMAIL           登录邮箱(同时也是收验证码的邮箱)
  EMAIL_PASSWORD  邮箱 IMAP 授权码/应用专用密码(非网页登录密码)
  GMAIL_PASSWORD  Gmail 网页登录密码(可选; 用于 IMAP 不可用时浏览器登录 Gmail 读码)
  CODE_SOURCE     取码方式: auto(默认, 先 IMAP 再网页) / imap / web
  IMAP_SERVER     IMAP 服务器, 默认 imap.gmail.com
  IMAP_PORT       IMAP 端口, 默认 993
  TG_BOT_TOKEN    Telegram 机器人 token
  TG_CHAT_ID      Telegram 接收消息的 chat id
  PANEL_URL       面板地址, 默认 https://hokas.net
  SIGN_IN_URL     登录页地址, 默认 https://hokas.net/sign-in
  SERVER_NAME     服务器名(可选, 默认优先找 myappas, 找不到则点第一个服务器)

已知风险: shrinkme 的 Cloudflare 对自动化浏览器很不友好, UC 模式也可能被重置。
脚本对验证做了耐心等待 + 多次重试, 且每一步都截图, 失败时请先看 Artifacts 截图定位。
"""

import email
import email.utils
import imaplib
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from email.header import decode_header

import requests
from seleniumbase import SB

# ---------------- 配置 ----------------
# 注意: GitHub Actions 中未配置的 secret 会注入为空字符串,
# 必须用 `or 默认值` 兜底, 不能依赖 os.environ.get 的第二参数
EMAIL = (os.environ.get("EMAIL") or "").strip()
EMAIL_PASSWORD = (os.environ.get("EMAIL_PASSWORD") or "").strip()
IMAP_SERVER = (os.environ.get("IMAP_SERVER") or "imap.gmail.com").strip()
IMAP_PORT = int(os.environ.get("IMAP_PORT") or "993")
TG_BOT_TOKEN = (os.environ.get("TG_BOT_TOKEN") or "").strip()
TG_CHAT_ID = (os.environ.get("TG_CHAT_ID") or "").strip()
PANEL_URL = (os.environ.get("PANEL_URL") or "https://hokas.net").strip().rstrip("/")
SIGN_IN_URL = (os.environ.get("SIGN_IN_URL") or "https://hokas.net/sign-in").strip()
SERVER_NAME = (os.environ.get("SERVER_NAME") or "").strip()
# Gmail 网页登录密码(未单独配置时复用 EMAIL_PASSWORD)
GMAIL_PASSWORD = (os.environ.get("GMAIL_PASSWORD") or EMAIL_PASSWORD).strip()
# 取码方式: auto / imap / web
CODE_SOURCE = (os.environ.get("CODE_SOURCE") or "auto").strip().lower()

SHOT_DIR = os.path.join(os.getcwd(), "screenshots")  # 关键步骤/失败截图目录
_shot_counter = [0]

CODE_MAX_RETRIES = 6      # 未收到验证码邮件时最多尝试次数
CODE_POLL_INTERVAL = 10   # 每次尝试间隔(秒)

GMAIL_URL = "https://mail.google.com/mail/u/0/#inbox"
# 未登录时直访 mail.google.com 会被重定向到营销页, 必须直接打开账号登录页
GMAIL_SIGNIN_URL = ("https://accounts.google.com/ServiceLogin?service=mail"
                    "&continue=https%3A%2F%2Fmail.google.com%2Fmail%2Fu%2F0%2F")
# Gmail 列表页/邮件标题中的验证码格式: "293767 is your verification code"
CODE_SUBJECT_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)\s+is your verification code", re.I)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def save_shot(sb, name):
    """保存带序号的页面截图到 screenshots 目录(CI 中会作为 Artifact 上传)"""
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
        _shot_counter[0] += 1
        path = os.path.join(
            SHOT_DIR, f"{_shot_counter[0]:02d}_{name}.png")
        sb.save_screenshot(path)
        log(f"已保存截图: {path}")
    except Exception as e:
        log(f"截图失败: {e}")


# ---------------- Telegram ----------------
def send_tg(text):
    """发送 Telegram 消息, 未配置时静默跳过"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("TG_BOT_TOKEN/TG_CHAT_ID 未配置, 跳过 TG 通知")
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=30,
        )
        if resp.status_code == 200:
            log("TG 通知发送成功")
        else:
            log(f"TG 通知发送失败: {resp.status_code} {resp.text}")
    except Exception as e:
        log(f"TG 通知异常: {e}")


# ---------------- 邮箱验证码 ----------------
def _decode(value):
    if not value:
        return ""
    parts = decode_header(value)
    out = ""
    for data, charset in parts:
        if isinstance(data, bytes):
            out += data.decode(charset or "utf-8", errors="ignore")
        else:
            out += data
    return out


def _mail_body(msg):
    """提取邮件正文纯文本"""
    texts = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    texts.append(payload.decode(charset, errors="ignore"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            texts.append(payload.decode(charset, errors="ignore"))
    return "\n".join(texts)


def _mail_matches(sender, subject):
    """判断是否验证码邮件: 发件人含 hokas/clerk, 或标题是验证码格式"""
    s = subject.lower()
    return ("hokas" in sender or "clerk" in sender or "hokas" in s
            or "verification code" in s or "verify" in s)


def _scan_folder(box, folder, after_ts, diag_lines):
    """在指定邮件夹中查找 after_ts 之后的验证码邮件, 找到返回 6 位码"""
    status, _ = box.select(folder, readonly=True)
    if status != "OK":
        return None
    since = datetime.now(timezone.utc).strftime("%d-%b-%Y")
    status, data = box.search(None, f'(SINCE "{since}")')
    if status != "OK":
        return None
    ids = data[0].split()
    # 从最新的邮件开始找
    for mail_id in reversed(ids[-20:]):
        status, msg_data = box.fetch(mail_id, "(RFC822)")
        if status != "OK":
            continue
        msg = email.message_from_bytes(msg_data[0][1])
        subject = _decode(msg.get("Subject", ""))
        sender = _decode(msg.get("From", "")).lower()
        date_hdr = msg.get("Date", "")
        if diag_lines is not None and len(diag_lines) < 10:
            diag_lines.append(f"  [{folder}] {date_hdr} | {sender} | {subject}")
        if not _mail_matches(sender, subject):
            continue
        # 校验邮件时间, 避免读到旧验证码
        if date_hdr:
            try:
                mail_ts = email.utils.parsedate_to_datetime(date_hdr).timestamp()
                if mail_ts < after_ts - 120:
                    continue
            except Exception:
                pass
        content = subject + "\n" + _mail_body(msg)
        m = re.search(r"(?<!\d)(\d{6})(?!\d)", content)
        if m:
            log(f"从[{folder}]邮件[{subject}]中提取到验证码: {m.group(1)}")
            return m.group(1)
    return None


def fetch_verification_code(after_ts):
    """
    轮询 IMAP 收件箱(含垃圾邮件夹), 获取 after_ts 之后到达的验证码邮件,
    返回 6 位数字验证码; 最多尝试 CODE_MAX_RETRIES 次, 不会无限重试
    """
    last_err = None
    # Gmail 需额外搜垃圾邮件夹和全部邮件(新发件人容易被判垃圾)
    folders = ["INBOX"]
    if "gmail" in IMAP_SERVER:
        folders += ['"[Gmail]/Spam"', '"[Gmail]/All Mail"']
    for attempt in range(1, CODE_MAX_RETRIES + 1):
        log(f"第 {attempt}/{CODE_MAX_RETRIES} 次尝试读取验证码邮件...")
        try:
            box = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
            # Gmail 应用专用密码页面展示为带空格的 4x4 分组, 登录时需去掉空格
            imap_password = (EMAIL_PASSWORD.replace(" ", "")
                             if "gmail" in IMAP_SERVER else EMAIL_PASSWORD)
            box.login(EMAIL, imap_password)
            # 第一次尝试时输出最近邮件清单, 便于排查为何匹配不到
            diag_lines = [] if attempt == 1 else None
            code = None
            for folder in folders:
                try:
                    code = _scan_folder(box, folder, after_ts, diag_lines)
                except Exception as fe:
                    log(f"扫描文件夹 {folder} 异常: {fe}")
                if code:
                    break
            if diag_lines is not None:
                log("最近邮件清单(用于排查):\n" + ("\n".join(diag_lines) or "  (今天没有任何邮件)"))
            box.logout()
            if code:
                return code
        except Exception as e:
            last_err = e
            log(f"读取邮箱异常: {e}")
            # 认证失败重试无意义, 直接报错提示检查密码
            if "AUTHENTICATIONFAILED" in str(e).upper() \
                    or "Invalid credentials" in str(e):
                raise RuntimeError(
                    f"IMAP 认证失败, 请检查 EMAIL_PASSWORD 是否为有效的应用专用密码: {e}")
        if attempt < CODE_MAX_RETRIES:
            log(f"尚未收到验证码邮件, {CODE_POLL_INTERVAL} 秒后重试...")
            time.sleep(CODE_POLL_INTERVAL)
    suffix = f" (最后一次异常: {last_err})" if last_err else ""
    raise TimeoutError(
        f"尝试 {CODE_MAX_RETRIES} 次仍未收到验证码邮件, 停止重试{suffix}")


# ---------------- Gmail 网页版读码(IMAP 不可用时的备用方案) ----------------
def gmail_login(sb):
    """
    在新标签页登录 Gmail 网页版: 输入邮箱 -> Next -> 输入密码 -> Next
    成功返回新标签页 handle, 失败返回 None(已关闭标签页)
    """
    # 应用专用密码无法登录网页, 没有网页密码就不尝试这条路
    if not GMAIL_PASSWORD:
        log("未配置 GMAIL_PASSWORD(网页登录密码), 跳过 Gmail 网页登录")
        return None
    password = GMAIL_PASSWORD
    main_tab = sb.driver.current_window_handle
    gmail_tab = None
    try:
        sb.driver.switch_to.new_window("tab")
        gmail_tab = sb.driver.current_window_handle
        log("新标签页打开 Gmail 登录页...")
        sb.open(GMAIL_SIGNIN_URL)
        sb.sleep(6)

        # 已登录时会直接进收件箱
        if gmail_in_inbox(sb):
            log("Gmail 已处于登录状态")
            sb.driver.switch_to.window(main_tab)
            return gmail_tab

        # 若被带到 Gmail 营销页, 点右上角 Sign in 进登录表单
        if not sb.is_element_visible('input[type="email"]'):
            for sel in ('a:contains("Sign in")',
                        '//a[contains(translate(., "SIGN IN", "sign in"), "sign in")]'):
                try:
                    if sb.is_element_visible(sel):
                        log("当前是 Gmail 营销页, 点击 Sign in...")
                        sb.click(sel)
                        sb.sleep(6)
                        break
                except Exception:
                    continue

        # 第一步: 输入邮箱点 Next
        if sb.is_element_visible('input[type="email"]'):
            sb.type('input[type="email"]', EMAIL)
            log(f"Gmail 登录: 已输入邮箱 {EMAIL}")
            save_shot(sb, "g1_gmail_email")
            gmail_click_next(sb)
            sb.sleep(6)
        else:
            save_shot(sb, "g_error_no_email_box")
            log("Gmail 登录页未找到邮箱输入框")

        # 第二步: 输入密码点 Next
        for _ in range(8):
            if sb.is_element_visible('input[type="password"]'):
                break
            sb.sleep(2)
        if not sb.is_element_visible('input[type="password"]'):
            save_shot(sb, "g_error_no_password_box")
            raise RuntimeError("Gmail 登录未出现密码输入框(可能被风控拦截)")
        sb.type('input[type="password"]', password)
        log("Gmail 登录: 已输入密码")
        gmail_click_next(sb)
        sb.sleep(8)
        save_shot(sb, "g2_gmail_after_login")

        # 风控提示检测
        for tip in ("couldn't sign you in", "This browser or app may not be secure",
                    "Verify it’s you", "2-Step Verification"):
            try:
                if sb.is_text_visible(tip):
                    save_shot(sb, "g_error_blocked")
                    raise RuntimeError(f"Gmail 网页登录被 Google 风控拦截: {tip}")
            except RuntimeError:
                raise
            except Exception:
                continue

        for _ in range(10):
            if gmail_in_inbox(sb):
                log("Gmail 登录成功, 已进入收件箱")
                sb.driver.switch_to.window(main_tab)
                return gmail_tab
            sb.sleep(3)
        save_shot(sb, "g_error_not_inbox")
        raise RuntimeError("Gmail 登录后未能进入收件箱")
    except Exception as e:
        log(f"Gmail 网页登录失败: {e}")
        try:
            if gmail_tab:
                sb.driver.switch_to.window(gmail_tab)
                sb.driver.close()
            sb.driver.switch_to.window(main_tab)
        except Exception:
            pass
        return None


def gmail_click_next(sb):
    """点击 Google 登录页的 Next/下一步按钮"""
    for sel in ("#identifierNext", "#passwordNext",
                '//button[contains(., "Next")]',
                '//span[text()="Next"]/ancestor::button[1]'):
        try:
            if sb.is_element_visible(sel):
                sb.click(sel)
                return True
        except Exception:
            continue
    # 兜底: 直接回车提交
    try:
        sb.send_keys('input:focus', "\n")
        return True
    except Exception:
        log("未找到 Next 按钮")
        return False


def gmail_in_inbox(sb):
    try:
        return ("mail.google.com" in sb.get_current_url()
                and (sb.is_element_present('div[role="main"]')
                     or sb.is_text_visible("Inbox")))
    except Exception:
        return False


def gmail_newest_code(sb):
    """从 Gmail 收件箱列表页标题中提取最新的 6 位验证码, 无则返回 None"""
    try:
        txt = sb.get_text('div[role="main"]') if sb.is_element_present('div[role="main"]') \
            else sb.get_text("body")
    except Exception:
        return None
    m = CODE_SUBJECT_RE.search(txt or "")
    return m.group(1) if m else None


def gmail_wait_new_code(sb, gmail_tab, baseline):
    """
    在 Gmail 标签页轮询新验证码(与 baseline 不同则认为是本次新收到的),
    最多尝试 CODE_MAX_RETRIES 次
    """
    main_tab = sb.driver.current_window_handle
    code = None
    try:
        for attempt in range(1, CODE_MAX_RETRIES + 1):
            sb.driver.switch_to.window(gmail_tab)
            log(f"第 {attempt}/{CODE_MAX_RETRIES} 次刷新 Gmail 网页读取验证码...")
            try:
                sb.refresh()
                sb.sleep(6)
            except Exception as e:
                log(f"刷新 Gmail 异常: {e}")
            newest = gmail_newest_code(sb)
            if newest and newest != baseline:
                log(f"从 Gmail 网页获取到新验证码: {newest}")
                save_shot(sb, "g3_gmail_code")
                code = newest
                break
            if attempt == CODE_MAX_RETRIES:
                save_shot(sb, "g_error_no_code")
            else:
                log(f"尚未收到新验证码, {CODE_POLL_INTERVAL} 秒后重试...")
                sb.sleep(CODE_POLL_INTERVAL)
    finally:
        try:
            sb.driver.switch_to.window(main_tab)
        except Exception:
            pass
    if not code:
        raise TimeoutError(f"尝试 {CODE_MAX_RETRIES} 次仍未从 Gmail 网页读到新验证码")
    return code


# ---------------- 验证码输入框 ----------------
def dump_inputs(sb):
    """调试用: 把页面上所有 input 的关键属性打印到日志, 便于下次按实际 DOM 修选择器"""
    try:
        info = sb.execute_script(
            "return Array.from(document.querySelectorAll('input')).map(el => "
            "({type: el.type, name: el.name, id: el.id, auto: el.autocomplete, "
            "mode: el.inputMode, max: el.maxLength, ph: el.placeholder, "
            "cls: el.className, vis: !!(el.offsetWidth || el.offsetHeight)}))")
        log(f"页面 input 清单(共 {len(info)} 个): {info}")
    except Exception as e:
        log(f"读取 input 清单失败: {e}")


def find_otp_inputs(sb, timeout=40):
    """查找验证码输入框(Clerk 为 6 个分格), 轮询等待渲染, 返回可见元素列表.

    首跑教训: 旧版只查一次, 且 HOKAS 的 factor-one 输入框属性与 Pella 的
    Clerk 版本不同, 导致直接报"未找到验证码输入框"。新版: 轮询 + 更宽的选择器
    + 通用兜底(表单内所有可见的单字符/数字输入框, 或单个验证码框)。
    """
    selectors = (
        'input[data-otp-input]', 'input[autocomplete="one-time-code"]',
        'input[name^="codeInput"]', 'input[inputmode="numeric"]',
        'input[maxlength="1"]',
        'input[name="code"]', 'input[id*="code"]', 'input[id*="otp"]',
        'input[data-testid*="otp"]', 'input[data-testid*="code"]',
        'input[aria-label*="digit" i]', 'input[aria-label*="code" i]',
        'input[placeholder*="code" i]', 'input[placeholder*="verification" i]',
    )
    deadline = time.time() + timeout
    while True:
        for sel in selectors:
            try:
                els = [e for e in sb.find_elements(sel) if e.is_displayed()]
                if els:
                    return els
            except Exception:
                continue
        # 通用兜底: factor-one 表单内所有可见 input, 过滤出像验证码的
        try:
            all_inputs = [e for e in sb.find_elements("input")
                          if e.is_displayed()]
            singles = []
            for e in all_inputs:
                try:
                    ml = e.get_attribute("maxlength")
                    mode = (e.get_attribute("inputmode") or "").lower()
                    name = (e.get_attribute("name") or "").lower()
                    if ml == "1" or mode == "numeric" or "code" in name or "otp" in name:
                        singles.append(e)
                except Exception:
                    continue
            if len(singles) >= 6:
                return singles
            if len(singles) == 1:
                return singles
        except Exception:
            pass
        if time.time() >= deadline:
            return []
        sb.sleep(2)


def js_fill_code(sb, code):
    """JS 兜底: 直接在 DOM 里找可见输入框填码, 返回填入的格数"""
    try:
        filled = sb.execute_script(
            """
            const code = arguments[0];
            const inputs = Array.from(document.querySelectorAll('input'))
                .filter(el => el.offsetWidth || el.offsetHeight);
            const singles = inputs.filter(el =>
                el.maxLength === 1 || el.inputMode === 'numeric' ||
                /code|otp/i.test(el.name || '') || /code|otp/i.test(el.id || ''));
            let n = 0;
            if (singles.length >= code.length) {
                singles.slice(0, code.length).forEach((el, i) => {
                    el.focus();
                    el.value = code[i];
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    n++;
                });
            } else if (inputs.length >= 1) {
                const el = inputs[0];
                el.focus();
                el.value = code;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                n = 1;
            }
            return n;
            """, code)
        return int(filled or 0)
    except Exception as e:
        log(f"JS 填码异常: {e}")
        return 0


def otp_value(sb):
    """读取当前验证码输入框已填内容"""
    try:
        return "".join([(e.get_attribute("value") or "")
                        for e in find_otp_inputs(sb)])
    except Exception:
        return ""


def fill_code(sb, code):
    """将验证码填入 Clerk 分格输入框: 先试聚焦第一格连续输入, 不生效再逐格填"""
    els = find_otp_inputs(sb, timeout=40)
    if not els:
        # 找不到输入框时先把页面 input 结构打进日志, 再用 JS 兜底试一次
        dump_inputs(sb)
        log("常规选择器未找到输入框, 尝试 JS 兜底填码...")
        if js_fill_code(sb, code) > 0:
            sb.sleep(1)
            if otp_value(sb) == code or len(code) == 6:
                log("JS 兜底填码已执行")
                save_shot(sb, "code_filled_js")
                return
        save_shot(sb, "error_no_otp_input")
        raise RuntimeError(f"未找到验证码输入框, 当前页面: {sb.get_current_url()}")
    log(f"检测到 {len(els)} 个验证码输入框")

    # 方式1: 聚焦第一格后逐个敲入, Clerk 会自动跳格
    try:
        els[0].click()
        for ch in code:
            els[0].send_keys(ch)
            sb.sleep(0.3)
    except Exception as e:
        log(f"连续输入异常: {e}")
    sb.sleep(1)

    if otp_value(sb) == code:
        log("验证码已填入")
        save_shot(sb, "code_filled")
        return

    # 方式2: 逐格填入
    log("连续输入未生效, 改为逐格填入...")
    els = find_otp_inputs(sb)
    for el in els:
        try:
            el.clear()
        except Exception:
            pass
    els = find_otp_inputs(sb)
    if len(els) >= len(code):
        for i, ch in enumerate(code):
            try:
                els[i].click()
                els[i].send_keys(ch)
                sb.sleep(0.3)
            except Exception as e:
                log(f"第 {i + 1} 格填入失败: {e}")
    sb.sleep(1)

    if otp_value(sb) != code:
        # 方式3: JS 直接赋值并触发 input 事件
        log("逐格填入仍未生效, 尝试 JS 赋值...")
        els = find_otp_inputs(sb)
        for i, ch in enumerate(code):
            if i >= len(els):
                break
            try:
                sb.execute_script(
                    "arguments[0].value=arguments[1];"
                    "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
                    "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
                    els[i], ch)
            except Exception:
                pass
        sb.sleep(1)
    save_shot(sb, "code_filled")


# ---------------- Clerk 登录表单通用操作 ----------------
def click_continue(sb):
    """
    点击 Clerk 表单的 Continue 提交按钮
    注意: 页面上还有 "Continue with Google" 按钮, 必须排除, 否则会跳到 Google OAuth
    """
    for sel in ('//button[contains(translate(., "CONTINUE", "continue"), "continue") '
                'and not(contains(translate(., "GOOGLE", "google"), "google"))]',
                'form button.cl-formButtonPrimary',
                'form button[type="submit"]'):
        try:
            if sb.is_element_visible(sel):
                sb.click(sel)
                return True
        except Exception:
            continue
    log("未找到 Continue 按钮")
    return False


def find_email_input(sb):
    """查找 Clerk 邮箱输入框, 找不到返回 None"""
    for sel in ('input[name="identifier"]', 'input[type="email"]',
                'input[id*="identifier"]',
                'input[placeholder*="email" i]'):
        try:
            if sb.is_element_visible(sel):
                return sel
        except Exception:
            continue
    return None


def wait_email_input(sb, timeout):
    """轮询等待邮箱输入框出现, 超时返回 None"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        sel = find_email_input(sb)
        if sel:
            return sel
        sb.sleep(2)
    return None


def switch_to_sign_in(sb):
    """
    若当前是 Clerk 注册页(Create your account, 带密码框),
    点击底部 'Already have an account? Sign in' 切换到登录页;
    发生切换返回 True
    """
    try:
        if not (sb.is_text_visible("Create your account")
                or sb.is_text_visible("Already have an account")):
            return False
        log("当前是注册页面, 点击底部 Sign in 切换到登录...")
        save_shot(sb, "signup_page")
        for sel in ('//a[normalize-space(text())="Sign in"]',
                    'a:contains("Sign in")', 'button:contains("Sign in")'):
            try:
                if sb.is_element_visible(sel):
                    sb.click(sel)
                    sb.sleep(4)
                    save_shot(sb, "signin_page")
                    return True
            except Exception:
                continue
        log("注册页上未找到 Sign in 链接")
    except Exception as e:
        log(f"切换登录页异常: {e}")
    return False


def click_login_entry(sb):
    """
    营销首页导航栏点击 LOGIN/Sign in 入口进入登录页
    注意: 账号已注册, 绝不能点 SIGN UP / Get Started
    """
    if find_email_input(sb):
        return
    # 用 XPath 忽略大小写匹配 login/log in/sign in, 并排除包含 sign up 的元素
    xpath = ('//a[contains(translate(., "LOGIN", "login"), "login") '
             'and not(contains(translate(., "SIGNUP", "signup"), "signup"))] | '
             '//button[contains(translate(., "LOGIN", "login"), "login") '
             'and not(contains(translate(., "SIGNUP", "signup"), "signup"))] | '
             '//a[contains(translate(., "SIGN", "sign"), "sign in")] | '
             '//button[contains(translate(., "SIGN", "sign"), "sign in")]')
    try:
        elements = sb.find_elements(xpath, by="xpath")
        for el in elements:
            try:
                if el.is_displayed():
                    log(f"点击登录入口: {el.text.strip()!r}")
                    el.click()
                    sb.sleep(5)
                    save_shot(sb, "after_login_click")
                    return
            except Exception:
                continue
        log("页面上未找到 LOGIN 入口")
    except Exception as e:
        log(f"查找 LOGIN 入口异常: {e}")


def get_code(sb, request_ts, gmail_tab, baseline):
    """按 CODE_SOURCE 取验证码: imap / web / auto(先 IMAP, 失败再走 Gmail 网页)"""
    imap_err = None
    if CODE_SOURCE != "web" and EMAIL_PASSWORD:
        try:
            return fetch_verification_code(request_ts)
        except Exception as e:
            imap_err = e
            log(f"IMAP 取码失败: {e}")
            if CODE_SOURCE == "imap":
                raise
            if GMAIL_PASSWORD:
                log("回退到 Gmail 网页版读码...")
    # 没有网页密码时不走网页登录(应用专用密码登不了网页), 直接把 IMAP 错误抛出去
    if not GMAIL_PASSWORD and CODE_SOURCE != "web":
        raise RuntimeError(f"IMAP 取码失败且未配置 GMAIL_PASSWORD: {imap_err}")
    if gmail_tab is None:
        gmail_tab = gmail_login(sb)
    if gmail_tab is None:
        raise RuntimeError(
            f"IMAP 与 Gmail 网页两种方式都无法获取验证码 (IMAP 错误: {imap_err})")
    return gmail_wait_new_code(sb, gmail_tab, baseline)


# ---------------- 登录 ----------------
def page_has_servers(sb):
    """登录成功后 Dashboard/My Servers 页的特征"""
    try:
        url = sb.get_current_url()
        if "sign-in" in url or "factor-one" in url:
            return False
        return (sb.is_text_visible("My Servers")
                or sb.is_text_visible("Your Servers")
                or (SERVER_NAME and sb.is_text_visible(SERVER_NAME))
                or sb.is_text_visible("myappas")
                or sb.is_text_visible("Dashboard"))
    except Exception:
        return False


def do_login(sb):
    """邮箱 + 验证码登录 hokas.net(Clerk)"""
    log(f"打开登录页: {SIGN_IN_URL}")
    sb.uc_open_with_reconnect(SIGN_IN_URL, reconnect_time=6)
    sb.sleep(5)
    log(f"当前页面: {sb.get_current_url()} | 标题: {sb.get_title()}")
    save_shot(sb, "open_signin")

    # 已登录时会直接跳回面板
    if page_has_servers(sb):
        log("检测到已处于登录状态")
        return

    # 若落在营销首页, 点击导航栏登录入口(账号已注册, 绝不点 SIGN UP)
    click_login_entry(sb)
    # 若进入的是注册页, 点底部 Sign in 切到登录页
    switch_to_sign_in(sb)

    # SPA 渲染较慢, 轮询等待邮箱输入框出现
    email_input = wait_email_input(sb, 30)
    if not email_input:
        # 可能被 Cloudflare Turnstile 拦截, 尝试自动过验证
        try:
            log("未出现登录表单, 尝试处理 Cloudflare 验证...")
            sb.uc_gui_click_captcha()
            sb.sleep(5)
        except Exception as e:
            log(f"处理 Cloudflare 验证失败(可忽略): {e}")
        email_input = wait_email_input(sb, 15)
    if not email_input:
        # 兜底: 回到面板首页, 点击登录入口
        log(f"登录页未出现表单, 回退到面板首页: {PANEL_URL}")
        sb.uc_open_with_reconnect(PANEL_URL, reconnect_time=6)
        sb.sleep(5)
        save_shot(sb, "panel_home")
        if page_has_servers(sb):
            log("检测到已处于登录状态")
            return
        click_login_entry(sb)
        switch_to_sign_in(sb)
        email_input = wait_email_input(sb, 15)
    if not email_input:
        save_shot(sb, "error_no_email_input")
        raise RuntimeError(f"未找到邮箱输入框, 当前页面: {sb.get_current_url()}")

    # 填邮箱前再确认一次不在注册页(注册页也有邮箱输入框, 不能误填)
    if switch_to_sign_in(sb):
        email_input = wait_email_input(sb, 15) or email_input
    sb.type(email_input, EMAIL)
    log(f"已输入邮箱: {EMAIL}")

    # 取码通道准备: web 模式下先登录 Gmail 网页版, 并记录当前最新验证码作为基线
    gmail_tab, baseline = None, None
    if CODE_SOURCE == "web" or (CODE_SOURCE == "auto" and GMAIL_PASSWORD):
        gmail_tab = gmail_login(sb)
        if gmail_tab:
            main_tab = sb.driver.current_window_handle
            try:
                sb.driver.switch_to.window(gmail_tab)
                baseline = gmail_newest_code(sb)
                log(f"Gmail 当前最新验证码基线: {baseline}")
            except Exception as e:
                log(f"读取验证码基线异常: {e}")
            finally:
                try:
                    sb.driver.switch_to.window(main_tab)
                except Exception:
                    pass

    request_ts = time.time()
    click_continue(sb)
    log("已点击 Continue, 等待验证码页(factor-one)...")
    sb.sleep(5)

    # 若误入 Google OAuth 页(点到了 Continue with Google), 退回重新提交邮箱表单
    if "accounts.google.com" in sb.get_current_url():
        log("误入 Google 登录页, 返回登录页重新提交...")
        save_shot(sb, "google_oauth_misclick")
        sb.uc_open_with_reconnect(SIGN_IN_URL, reconnect_time=6)
        sb.sleep(5)
        switch_to_sign_in(sb)
        email_input = wait_email_input(sb, 20)
        if not email_input:
            save_shot(sb, "error_no_email_input_retry")
            raise RuntimeError("返回登录页后未找到邮箱输入框")
        sb.type(email_input, EMAIL)
        request_ts = time.time()
        click_continue(sb)
        sb.sleep(5)
        if "accounts.google.com" in sb.get_current_url():
            save_shot(sb, "error_google_oauth_again")
            raise RuntimeError("两次都误入 Google OAuth, Continue 按钮定位失败")

    # 等待验证码页出现(Check your email + 6 个分格输入框)
    for _ in range(10):
        try:
            if (sb.is_text_visible("Check your email")
                    or "factor-one" in sb.get_current_url()
                    or find_otp_inputs(sb)):
                break
        except Exception:
            pass
        sb.sleep(2)
    log(f"当前页面: {sb.get_current_url()}")
    save_shot(sb, "check_email")

    # 获取验证码(IMAP 或 Gmail 网页版)
    code = get_code(sb, request_ts, gmail_tab, baseline)

    # 填入 6 位验证码并点击 Continue
    fill_code(sb, code)
    log("验证码已填入, 点击 Continue 提交...")
    sb.sleep(1)
    click_continue(sb)
    sb.sleep(5)
    save_shot(sb, "after_submit_code")

    # 等待进入服务器列表
    for _ in range(20):
        if page_has_servers(sb):
            log("登录成功, 已进入面板")
            save_shot(sb, "logged_in")
            return
        sb.sleep(2)
    save_shot(sb, "error_after_code")
    raise RuntimeError("登录后未能进入服务器列表页")


# ---------------- 服务器 Overview 与剩余时间 ----------------
def parse_remaining(text):
    """
    从页面文本中解析剩余时间, 返回 (总分钟数, 原始片段); 解析不到返回 (None, "")
    支持格式: "08:06:04" / "1 day 08:06:04" / "32 Hours 6 Minutes" / "8h 6m" / "2 days 3 hours"
    """
    if not text:
        return None, ""
    # 优先在 "remaining / expires / expiring" 附近找, 避免抓到页面别处的时长
    ctx = text
    m_ctx = re.search(r"(remaining|expires|expiring)[\s\S]{0,200}", text, re.I)
    if m_ctx:
        ctx = m_ctx.group(0)
    for src in (ctx, text):
        m = re.search(r"(?:(\d+)\s*days?\s*)?(\d{1,3}):(\d{2}):(\d{2})", src)
        if m:
            days = int(m.group(1) or 0)
            return days * 1440 + int(m.group(2)) * 60 + int(m.group(3)), m.group(0)
        m = re.search(r"(?:(\d+)\s*days?\s*)?(?:(\d+)\s*(?:hours?|hrs?|h)\s*)?"
                      r"(?:(\d+)\s*(?:minutes?|mins?|m)\b)?",
                      src, re.I)
        if m and (m.group(1) or m.group(2) or m.group(3)):
            days = int(m.group(1) or 0)
            hours = int(m.group(2) or 0)
            mins = int(m.group(3) or 0)
            if days or hours or mins:
                return days * 1440 + hours * 60 + mins, m.group(0)
    return None, ""


def get_remaining(sb):
    """读取当前 Overview 页的剩余时间 (分钟, 原始文本)"""
    try:
        txt = sb.get_text("body")
    except Exception:
        return None, ""
    return parse_remaining(txt or "")


def open_server_overview(sb):
    """从服务器列表打开目标服务器的 Manage -> Overview"""
    sb.sleep(2)
    target = SERVER_NAME or "myappas"
    clicked = False
    # 1) 按服务器名点击(myappas / SERVER_NAME)
    for name in dict.fromkeys([n for n in (SERVER_NAME, "myappas") if n]):
        try:
            if sb.is_element_visible(f'//*[contains(text(), "{name}")]'):
                sb.click(f'//*[contains(text(), "{name}")]')
                clicked = True
                log(f"已点击服务器: {name}")
                break
        except Exception:
            continue
    # 2) 兜底: 点击第一个服务器卡片/管理链接
    if not clicked:
        for sel in ('a[href*="/server"]', 'a[href*="manage"]',
                    '//a[contains(., "Manage")]',
                    '//*[text()="My Servers"]/following::a[1]'):
            try:
                sb.click(sel, timeout=5)
                clicked = True
                log("已点击第一个服务器卡片")
                break
            except Exception:
                continue
    if not clicked:
        save_shot(sb, "error_no_server_card")
        raise RuntimeError("未找到可点击的服务器卡片")
    sb.sleep(5)
    # 若有 Manage 按钮/标签, 再点进管理页
    try:
        if sb.is_element_visible('//a[contains(., "Manage")] | //button[contains(., "Manage")]'):
            sb.click('//a[contains(., "Manage")] | //button[contains(., "Manage")]')
            sb.sleep(4)
    except Exception:
        pass
    # 确保停在 Overview 标签
    try:
        if sb.is_element_visible('//*[contains(text(), "Overview")]'):
            sb.click('//*[contains(text(), "Overview")]')
            sb.sleep(3)
    except Exception:
        pass
    save_shot(sb, "overview")
    log(f"服务器 Overview: {sb.get_current_url()} (目标: {target})")


# ---------------- shrinkme 广告链 ----------------
def is_clickable(el):
    """判断按钮是否可点击(参考 Pella 版: 按 class 分词精确匹配, 避开 Tailwind 变体前缀)"""
    try:
        if not el.is_enabled():
            return False
        if el.get_attribute("disabled") is not None:
            return False
        if (el.get_attribute("aria-disabled") or "").lower() == "true":
            return False
        tokens = (el.get_attribute("class") or "").lower().split()
        for t in tokens:
            if t in ("disabled", "pointer-events-none", "cursor-not-allowed"):
                return False
        return True
    except Exception:
        return False


def close_extra_windows(sb, keep):
    """关掉 keep 集合之外的所有窗口(popunder 广告), 返回当前窗口数"""
    try:
        for h in list(sb.driver.window_handles):
            if h not in keep:
                try:
                    sb.driver.switch_to.window(h)
                    url = sb.get_current_url()
                    log(f"关闭多余窗口(广告): {url}")
                    sb.driver.close()
                except Exception:
                    pass
    except Exception as e:
        log(f"关闭多余窗口异常: {e}")


def focus_window(sb, handle):
    try:
        if handle in sb.driver.window_handles:
            sb.driver.switch_to.window(handle)
            return True
    except Exception:
        pass
    return False


def click_and_settle(sb, selectors, flow_handle, main_handle, tag, tries=3):
    """
    在 flow 窗口(shrinkme/中间页)里点击目标按钮, 处理 popunder:
    每次点击后关掉多余广告窗、切回 flow 窗口; 若页面无变化则重点。
    返回 True 表示至少成功点了一次。
    """
    clicked_any = False
    for attempt in range(1, tries + 1):
        if not focus_window(sb, flow_handle):
            log(f"[{tag}] flow 窗口已不存在, 停止点击")
            return clicked_any
        before_url = sb.get_current_url()
        el = None
        for sel in selectors:
            try:
                if sb.is_element_visible(sel):
                    el = sel
                    break
            except Exception:
                continue
        if not el:
            log(f"[{tag}] 第 {attempt} 次: 未找到可点击按钮")
            sb.sleep(3)
            continue
        try:
            sb.click(el)
            clicked_any = True
            log(f"[{tag}] 第 {attempt} 次点击: {el}")
        except Exception as e:
            log(f"[{tag}] 点击异常: {e}")
        sb.sleep(5)
        # 关掉这次点击弹出的广告窗, 切回 flow 窗口
        close_extra_windows(sb, {flow_handle, main_handle})
        focus_window(sb, flow_handle)
        try:
            after_url = sb.get_current_url()
        except Exception:
            after_url = ""
        if after_url != before_url:
            log(f"[{tag}] 页面已跳转: {before_url} -> {after_url}")
            return True
        sb.sleep(2)
    return clicked_any


def pass_turnstile(sb, flow_handle, main_handle, tag, timeout=150):
    """
    处理 shrinkme 验证页的 Cloudflare Turnstile 勾选框:
    UC 模式下用 uc_gui_click_captcha 点选, 然后耐心等待页面放行;
    已知该验证可能 "验证中 -> 重置", 做多轮重试。全程以截图取证。
    放行判据: 出现 "Click here to continue" 可点击, 或验证框消失。
    """
    deadline = time.time() + timeout
    round_no = 0
    while time.time() < deadline:
        round_no += 1
        if not focus_window(sb, flow_handle):
            return False
        # 已放行?
        try:
            if sb.is_element_visible(
                    '//button[contains(., "Click here to continue")] | '
                    '//a[contains(., "Click here to continue")]'):
                log(f"[{tag}] Turnstile 已放行(Continue 按钮出现)")
                return True
        except Exception:
            pass
        try:
            log(f"[{tag}] 第 {round_no} 轮: 尝试点击 Turnstile 验证框...")
            sb.uc_gui_click_captcha()
        except Exception as e:
            log(f"[{tag}] uc_gui_click_captcha 异常(可忽略): {e}")
        # 耐心等待: 每 5 秒看一次是否放行, 最多等 45 秒再进入下一轮
        for _ in range(9):
            sb.sleep(5)
            close_extra_windows(sb, {flow_handle, main_handle})
            focus_window(sb, flow_handle)
            try:
                if sb.is_element_visible(
                        '//button[contains(., "Click here to continue")] | '
                        '//a[contains(., "Click here to continue")]'):
                    log(f"[{tag}] Turnstile 已放行")
                    save_shot(sb, f"{tag}_turnstile_passed")
                    return True
                # 验证框彻底消失也视为放行
                if not sb.is_element_present('iframe[src*="challenges.cloudflare.com"]') \
                        and not sb.is_element_present('iframe[title*="Cloudflare"]'):
                    log(f"[{tag}] Turnstile 验证框已消失, 视为放行")
                    return True
            except Exception:
                continue
        save_shot(sb, f"{tag}_turnstile_retry{round_no}")
    log(f"[{tag}] Turnstile 在 {timeout} 秒内未放行")
    save_shot(sb, f"{tag}_turnstile_failed")
    return False


def walk_shrinkme(sb, flow_handle, main_handle, tag):
    """
    走完一条 shrinkme 短链(三步), 成功跳回 hokas.net 返回 True。
    flow_handle: 点击 Add 后新开的 shrinkme 标签页。
    """
    focus_window(sb, flow_handle)
    sb.sleep(5)
    save_shot(sb, f"{tag}_shrinkme_open")
    log(f"[{tag}] shrinkme 页: {sb.get_current_url()}")

    # --- 第 1 步: 验证页 ---
    if not pass_turnstile(sb, flow_handle, main_handle, tag):
        return False
    ok = click_and_settle(
        sb,
        ['//button[contains(., "Click here to continue")]',
         '//a[contains(., "Click here to continue")]',
         'button:contains("Click here to continue")'],
        flow_handle, main_handle, f"{tag}_step1_continue")
    save_shot(sb, f"{tag}_after_step1")
    if not ok:
        log(f"[{tag}] 第 1 步 Continue 点击失败")
        return False

    # --- 第 2 步: 中间博客页 STEP 1 OF 2 -> CLICK TO VERIFY ---
    sb.sleep(3)
    focus_window(sb, flow_handle)
    try:
        is_step_page = sb.is_text_visible("STEP 1 OF 2") \
            or "themezon" in sb.get_current_url() \
            or sb.is_element_visible('//*[contains(., "CLICK TO VERIFY")]')
    except Exception:
        is_step_page = False
    if is_step_page:
        save_shot(sb, f"{tag}_step_page")
        ok = click_and_settle(
            sb,
            ['//button[contains(., "CLICK TO VERIFY")]',
             '//a[contains(., "CLICK TO VERIFY")]',
             'button:contains("CLICK TO VERIFY")'],
            flow_handle, main_handle, f"{tag}_step2_verify")
        save_shot(sb, f"{tag}_after_step2")
        if not ok:
            log(f"[{tag}] 第 2 步 CLICK TO VERIFY 点击失败")
            return False
    else:
        log(f"[{tag}] 未出现 STEP 中间页, 直接进入第 3 步(当前: {sb.get_current_url()})")

    # --- 第 3 步: YOUR LINK IS ALMOST READY -> 倒计时 -> Get Link ---
    focus_window(sb, flow_handle)
    deadline = time.time() + 120
    get_link_sel = ('//button[contains(., "Get Link")] | '
                    '//a[contains(., "Get Link")]')
    ready = False
    while time.time() < deadline:
        close_extra_windows(sb, {flow_handle, main_handle})
        focus_window(sb, flow_handle)
        try:
            if "hokas.net" in sb.get_current_url():
                log(f"[{tag}] 已跳回 hokas.net")
                return True
            if sb.is_element_visible(get_link_sel):
                # 还要确认不是置灰状态
                els = sb.find_elements(get_link_sel, by="xpath")
                if any(is_clickable(e) for e in els):
                    ready = True
                    break
                log(f"[{tag}] Get Link 已出现但暂不可点(倒计时中)...")
            elif sb.is_text_visible("YOUR LINK IS ALMOST READY"):
                log(f"[{tag}] 倒计时进行中...")
        except Exception:
            pass
        sb.sleep(5)
    save_shot(sb, f"{tag}_getlink_page")
    if not ready:
        log(f"[{tag}] Get Link 在时限内未变为可点击")
        return False
    # 点 Get Link: popunder 会吞掉点击, 点完关广告回 flow 窗口重点, 直到跳回 hokas
    for attempt in range(1, 5):
        focus_window(sb, flow_handle)
        try:
            sb.click(get_link_sel)
            log(f"[{tag}] 第 {attempt} 次点击 Get Link")
        except Exception as e:
            log(f"[{tag}] 点击 Get Link 异常: {e}")
        sb.sleep(5)
        # 成功的话 flow 窗口本身会跳到 hokas, 或新开 hokas 窗口
        for h in list(sb.driver.window_handles):
            try:
                sb.driver.switch_to.window(h)
                if "hokas.net" in sb.get_current_url():
                    log(f"[{tag}] Get Link 成功, 已回到 hokas.net: {sb.get_current_url()}")
                    save_shot(sb, f"{tag}_back_on_hokas")
                    return True
            except Exception:
                continue
        close_extra_windows(sb, {flow_handle, main_handle})
        focus_window(sb, flow_handle)
        try:
            if "hokas.net" in sb.get_current_url():
                return True
        except Exception:
            pass
    log(f"[{tag}] Get Link 多次点击后仍未跳回 hokas.net")
    save_shot(sb, f"{tag}_getlink_failed")
    return False


def find_add_button(sb, hours):
    """在 Overview 页找 Add XX Hours 按钮, 返回 (xpath, 元素或None)"""
    xpath = (f'//button[contains(., "Add") and contains(., "{hours}")] | '
             f'//a[contains(., "Add") and contains(., "{hours}")]')
    try:
        els = [e for e in sb.find_elements(xpath, by="xpath") if e.is_displayed()]
        return xpath, (els[0] if els else None)
    except Exception:
        return xpath, None


def run_add_flow(sb, main_handle, hours, before_minutes):
    """
    处理一个 Add 按钮: 点击 -> 走 shrinkme -> 回 Overview 复查。
    返回 "credited" / "walk_failed" / "not_credited" / "unavailable"
    """
    tag = f"add{hours}h"
    xpath, el = find_add_button(sb, hours)
    if el is None:
        log(f"[Add {hours} Hours] 未找到按钮(可能面板结构变化或未到可用时间)")
        save_shot(sb, f"{tag}_no_button")
        return "unavailable"
    btn_text = (el.text or "").strip()
    if not is_clickable(el):
        log(f"[Add {hours} Hours] 按钮[{btn_text}]不可点击(6 小时冷却中)")
        return "unavailable"
    try:
        sb.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        sb.sleep(1)
        el.click()
        log(f"[Add {hours} Hours] 已点击按钮[{btn_text}], 等待 shrinkme 新标签页...")
    except Exception as e:
        log(f"[Add {hours} Hours] 点击失败: {e}")
        return "walk_failed"
    # 等待新窗口(shrinkme)出现
    flow_handle = None
    deadline = time.time() + 30
    while time.time() < deadline:
        sb.sleep(2)
        handles = sb.driver.window_handles
        if len(handles) > 1:
            for h in handles:
                if h != main_handle:
                    try:
                        sb.driver.switch_to.window(h)
                        if "shrinkme" in sb.get_current_url():
                            flow_handle = h
                            break
                    except Exception:
                        continue
            if flow_handle:
                break
        else:
            # 也可能同标签跳转
            try:
                if "shrinkme" in sb.get_current_url():
                    flow_handle = sb.driver.current_window_handle
                    break
            except Exception:
                pass
    if not flow_handle:
        log(f"[Add {hours} Hours] 未等到 shrinkme 标签页")
        save_shot(sb, f"{tag}_no_shrinkme_tab")
        focus_window(sb, main_handle)
        return "walk_failed"

    walked = walk_shrinkme(sb, flow_handle, main_handle, tag)

    # 回到主窗口并刷新 Overview 复查
    close_extra_windows(sb, {main_handle})
    if not focus_window(sb, main_handle):
        # 主窗口被关掉的兜底: 用剩下的任意 hokas 窗口
        for h in sb.driver.window_handles:
            try:
                sb.driver.switch_to.window(h)
                if "hokas.net" in sb.get_current_url():
                    main_handle = h
                    break
            except Exception:
                continue
    try:
        sb.open(sb.get_current_url())
    except Exception:
        pass
    sb.sleep(5)
    # 若刷新后不在 Overview, 重新进一次
    after_minutes, after_raw = get_remaining(sb)
    if after_minutes is None:
        try:
            open_server_overview(sb)
            after_minutes, after_raw = get_remaining(sb)
        except Exception as e:
            log(f"复查时重进 Overview 失败: {e}")
    save_shot(sb, f"{tag}_after_check")
    if after_minutes is None:
        log(f"[Add {hours} Hours] 复查未能读到剩余时间(walked={walked})")
        return "walk_failed" if not walked else "not_credited"
    log(f"[Add {hours} Hours] 剩余时间: {before_minutes} 分钟 -> {after_minutes} 分钟"
        f" ({after_raw})")
    gained = after_minutes - before_minutes
    # 到账判据: 增量达到标称时长的 80%(扣掉流程耗时与读数取整误差)
    if gained >= hours * 60 * 0.8:
        log(f"[Add {hours} Hours] ✅ 已到账, 增加约 {gained} 分钟")
        return "credited"
    log(f"[Add {hours} Hours] 未确认到账(增量 {gained} 分钟, walked={walked})")
    return "not_credited" if walked else "walk_failed"


# ---------------- 主流程 ----------------
def main():
    if not EMAIL:
        log("错误: 必须配置 EMAIL 环境变量")
        sys.exit(1)
    if not EMAIL_PASSWORD and not GMAIL_PASSWORD:
        log("错误: 必须配置 EMAIL_PASSWORD(IMAP 授权码) 或 GMAIL_PASSWORD(网页密码)")
        sys.exit(1)

    result_lines = ["🖥️ <b>Hokas 自动续期报告</b>", f"账号: {EMAIL}"]
    credited_any = False
    stage = "login"
    try:
        with SB(uc=True, test=True, locale_code="en",
                headless=False, xvfb=True) as sb:
            try:
                do_login(sb)
                stage = "renew"
                open_server_overview(sb)
                main_handle = sb.driver.current_window_handle

                before, before_raw = get_remaining(sb)
                if before is None:
                    save_shot(sb, "error_no_remaining")
                    raise RuntimeError("Overview 页未能解析到 Remaining 剩余时间")
                log(f"续期前剩余时间: {before} 分钟 ({before_raw})")
                result_lines.append(f"🕒 续期前剩余: {before_raw or str(before) + ' 分钟'}")

                current = before
                for hours in (24, 12):
                    status = run_add_flow(sb, main_handle, hours, current)
                    after, _ = get_remaining(sb)
                    if after is not None:
                        current = after
                    if status == "credited":
                        credited_any = True
                        result_lines.append(f"✅ Add {hours} Hours: 已到账")
                    elif status == "unavailable":
                        result_lines.append(
                            f"⏳ Add {hours} Hours: 按钮不可用(冷却中或未找到)")
                    elif status == "not_credited":
                        result_lines.append(
                            f"⚠️ Add {hours} Hours: 流程走完但未确认到账")
                    else:
                        result_lines.append(
                            f"❌ Add {hours} Hours: shrinkme 流程未走完")

                final, final_raw = get_remaining(sb)
                if final is not None:
                    result_lines.append(
                        f"🕒 续期后剩余: {final_raw or str(final) + ' 分钟'}"
                        f" (净增 {final - before} 分钟)")
                save_shot(sb, "final_overview")
            except Exception:
                # 失败时兜底截图, 便于通过 Artifact 排查
                save_shot(sb, "error_final")
                raise
        result_lines.append(f"完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        send_tg("\n".join(result_lines))
        log("任务执行完毕")
        if not credited_any:
            log("两个 Add 按钮均未到账, 以非 0 退出码结束(便于 Actions 标红排查)")
            sys.exit(2)
    except SystemExit:
        raise
    except Exception as e:
        if stage == "login":
            log("登录阶段失败, 本次未能进入续期流程(两个 Add 按钮根本没被执行, "
                "不是续期本身失败)")
        log(f"任务失败: {e}")
        traceback.print_exc()
        result_lines.append(f"❌ 续期失败: {e}")
        send_tg("\n".join(result_lines))
        sys.exit(1)


if __name__ == "__main__":
    main()

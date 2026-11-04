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
  SERVER_ID       服务器 ID(可选, 如 b9275; 设置后优先按 ID 找卡片, 不怕名字拼错)
  PROXY           代理(可选): http://user:pass@host:port 或 socks5://host:port
                  可直接给浏览器; vless:// 链接会在 runner 上经 Xray 转成
                  127.0.0.1:1081 的本地 HTTP 代理再给浏览器(浏览器不能直连 vless)

已知风险: shrinkme 的 Cloudflare 对自动化浏览器很不友好, UC 模式也可能被重置。
脚本对验证做了耐心等待 + 多次重试, 且每一步都截图, 失败时请先看 Artifacts 截图定位。
"""

import atexit
import email
import email.utils
import imaplib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request
import zipfile
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
# 服务器 ID(卡片上显示的 ID, 如 b9275): 比名字更可靠, 因为名字拼写
# 容易看错(如 jaosonserver 与 jasonserver)。设置后优先按 ID 找卡片。
SERVER_ID = (os.environ.get("SERVER_ID") or "").strip()
# Gmail 网页登录密码(未单独配置时复用 EMAIL_PASSWORD)
GMAIL_PASSWORD = (os.environ.get("GMAIL_PASSWORD") or EMAIL_PASSWORD).strip()
# 取码方式: auto / imap / web
CODE_SOURCE = (os.environ.get("CODE_SOURCE") or "auto").strip().lower()
# 家宽代理节点(可选, 由 GitHub Secrets 的 PROXY 注入):
# 格式如 http://user:pass@host:port 或 socks5://user:pass@host:port。
# GitHub 机房 IP 过 shrinkme/Cloudflare 容易被判机器人, 家宽 IP 更像真人。
# 代理只作用于浏览器流量(HOKAS 登录 + shrinkme), IMAP 取码不走代理。
# 注意: 该值含账号密码, 绝不能打印到日志, 只能打印脱敏后的协议+主机。
PROXY = (os.environ.get("PROXY") or "").strip()

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


def proxy_label():
    """代理的脱敏描述: 只显示协议和主机端口, 绝不包含账号密码、
    查询参数(SNI/路径等)和备注。教训(2026-11-04 run 41748751838):
    旧版把 vless URI 的查询串也打进了日志, 暴露了节点主机/SNI。"""
    if not PROXY:
        return "未配置(直连)"
    try:
        rest = PROXY.split("://", 1)[1] if "://" in PROXY else PROXY
        hostport = rest.split("@")[-1].split("?")[0].split("#")[0].split("/")[0]
        scheme = PROXY.split("://", 1)[0] if "://" in PROXY else "http"
        return f"已启用({scheme}://{hostport})"
    except Exception:
        return "已启用(地址已脱敏)"


# ---------------- VLESS 节点转本地代理(Xray) ----------------
# 浏览器不能直接用 vless:// 协议(2026-11-04 run 41748751838 在启动阶段
# 就被 seleniumbase 的代理校验拒绝, 浏览器根本没起来)。
# 做法: 在 runner 上起一个 Xray 进程, 把 VLESS 节点转成
# 127.0.0.1:1081 的本地 HTTP 代理, 再把这个普通地址给浏览器。
# 节点 UUID 只出现在 runner 本地的临时配置文件里, 绝不打日志。
_xray_proc = None


def _stop_xray():
    global _xray_proc
    try:
        if _xray_proc is not None and _xray_proc.poll() is None:
            _xray_proc.terminate()
    except Exception:
        pass


def parse_vless_uri(uri):
    """解析 vless://UUID@HOST:PORT?params, 返回节点 dict。
    调用方绝不能把原始 URI 或 UUID 打进日志。"""
    p = urllib.parse.urlparse(uri)
    if p.scheme.lower() != "vless":
        raise ValueError("不是 vless:// 链接")
    uuid = p.username
    host = p.hostname
    port = p.port
    if not uuid or not host or not port:
        raise ValueError("vless 链接缺少 UUID/主机/端口")
    q = {k: v[0] for k, v in urllib.parse.parse_qs(p.query).items()}
    return {
        "uuid": uuid, "address": host, "port": port,
        "security": (q.get("security") or "none").lower(),
        "sni": q.get("sni") or q.get("serverName") or host,
        "fp": q.get("fp") or "chrome",
        "network": (q.get("type") or "tcp").lower(),
        "ws_host": q.get("host") or host,
        "ws_path": q.get("path") or "/",
        "flow": q.get("flow") or "",
        "pbk": q.get("pbk") or "",
        "sid": q.get("sid") or "",
        "service_name": q.get("serviceName") or "",
    }


def build_xray_config(node):
    """由节点 dict 生成 Xray 配置 dict(纯函数, 便于单测)。"""
    stream = {"network": node["network"]}
    sec = node["security"]
    if sec == "tls":
        stream["security"] = "tls"
        stream["tlsSettings"] = {"serverName": node["sni"],
                                 "fingerprint": node["fp"]}
    elif sec == "reality":
        stream["security"] = "reality"
        stream["realitySettings"] = {"serverName": node["sni"],
                                     "publicKey": node["pbk"],
                                     "shortId": node["sid"],
                                     "fingerprint": node["fp"]}
    if node["network"] == "ws":
        stream["wsSettings"] = {"path": node["ws_path"],
                                "headers": {"Host": node["ws_host"]}}
    elif node["network"] == "grpc":
        stream["grpcSettings"] = {"serviceName": node["service_name"]}
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {"port": 1080, "listen": "127.0.0.1", "protocol": "socks",
             "settings": {"auth": "noauth", "udp": True}},
            {"port": 1081, "listen": "127.0.0.1", "protocol": "http",
             "settings": {}},
        ],
        "outbounds": [
            {"protocol": "vless",
             "settings": {"vnext": [{"address": node["address"],
                                     "port": node["port"],
                                     "users": [{"id": node["uuid"],
                                                "encryption": "none",
                                                "flow": node["flow"]}]}]},
             "streamSettings": stream},
        ],
    }


def ensure_xray():
    """下载 Xray 内核到 /tmp/xray-bin(已存在则复用), 返回可执行路径。"""
    bindir = "/tmp/xray-bin"
    exe = os.path.join(bindir, "xray")
    if os.path.isfile(exe) and os.access(exe, os.X_OK):
        return exe
    os.makedirs(bindir, exist_ok=True)
    url = ("https://github.com/XTLS/Xray-core/releases/latest/download/"
           "Xray-linux-64.zip")
    zpath = os.path.join(bindir, "xray.zip")
    log("正在下载 Xray 内核(用于 VLESS 转换)...")
    req = urllib.request.Request(url, headers={"User-Agent": "hokas-renew"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(zpath, "wb") as f:
        shutil.copyfileobj(resp, f)
    with zipfile.ZipFile(zpath) as z:
        z.extract("xray", bindir)
    os.chmod(exe, 0o755)
    return exe


def start_vless_proxy(uri):
    """启动 Xray 把 VLESS 节点转成本地代理, 返回给浏览器的代理串。
    任何一步失败都抛错, 调用方应在启动阶段退出, 不进登录。"""
    global _xray_proc
    node = parse_vless_uri(uri)
    log("检测到 VLESS 节点, 正在启动 Xray 转成本地代理...")
    xray = ensure_xray()
    cfg_path = "/tmp/xray-config.json"
    with open(cfg_path, "w") as f:
        json.dump(build_xray_config(node), f)
    os.chmod(cfg_path, 0o600)
    _xray_proc = subprocess.Popen(
        [xray, "run", "-c", cfg_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    atexit.register(_stop_xray)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", 1081), timeout=2):
                log("VLESS 转换就绪: 本地代理 127.0.0.1:1081")
                return "127.0.0.1:1081"
        except OSError:
            if _xray_proc.poll() is not None:
                raise RuntimeError("Xray 进程已退出, VLESS 转换失败")
            time.sleep(1)
    _stop_xray()
    raise RuntimeError("等待本地代理端口 1081 超时, VLESS 转换失败")


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
    """JS 填码(主通道): 直接在 DOM 里找验证码输入框填码, 返回 (填入格数, 填后读到的值).

    实测教训(2026-11-04 run 41680849009): HOKAS 的 factor-one 只有一个输入框
    (autocomplete=one-time-code, inputmode=numeric, maxlength=6, 无 name/id),
    且该框在视觉上是隐藏的(Clerk 用透明输入框 + 另外画的 6 个数字格),
    Selenium 的 is_displayed() 会判它不可见, 导致旧逻辑永远找不到它。
    因此这里不判断可见性, 并用 React 原生 value setter 赋值(普通 el.value=
    不会触发 React 的 onChange), 再补 input/change 事件。
    注意: 不用 execute_script 的 arguments 传参(在该浏览器驱动里会报
    ReferenceError: arguments is not defined), 码值经纯数字校验后直接拼进脚本。
    """
    if not (isinstance(code, str) and code.isdigit() and len(code) == 6):
        log(f"JS 填码: 码值格式异常, 拒绝拼入脚本")
        return 0, ""
    script = """
    function setVal(el, v) {
        const setter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set;
        setter.call(el, v);
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
    }
    const code = "%s";
    const inputs = Array.from(document.querySelectorAll('input'));
    const otp = inputs.filter(el =>
        el.autocomplete === 'one-time-code' || el.inputMode === 'numeric' ||
        /code|otp/i.test(el.name || '') || /code|otp/i.test(el.id || ''));
    if (otp.length === 1) {
        setVal(otp[0], code);
        return {n: 1, val: otp[0].value};
    }
    if (otp.length >= 6) {
        otp.slice(0, 6).forEach((el, i) => setVal(el, code[i]));
        return {n: 6, val: otp.slice(0, 6).map(el => el.value).join('')};
    }
    return {n: 0, val: ''};
    """ % code
    try:
        res = sb.execute_script(script)
        if isinstance(res, dict):
            return int(res.get("n") or 0), (res.get("val") or "")
        return 0, ""
    except Exception as e:
        log(f"JS 填码异常: {e}")
        return 0, ""


def otp_value(sb):
    """读取当前验证码输入框已填内容"""
    try:
        return "".join([(e.get_attribute("value") or "")
                        for e in find_otp_inputs(sb)])
    except Exception:
        return ""


def fill_code(sb, code):
    """将验证码填入 Clerk 输入框: JS 主通道优先, Selenium 方式兜底.

    HOKAS factor-one 是单个隐藏输入框, Selenium 的可见性判断会漏掉它,
    所以先走 JS(不判断可见性), 读回的值等于验证码才算成功。
    """
    n, val = js_fill_code(sb, code)
    if n > 0 and val == code:
        log(f"JS 填码成功(共 {n} 格), 读回值与验证码一致")
        save_shot(sb, "code_filled")
        return
    if n > 0:
        log(f"JS 填码后读回值不符(读到 {val!r}), 改走 Selenium 方式...")
    else:
        log("JS 未找到验证码输入框, 改走 Selenium 方式...")
    els = find_otp_inputs(sb, timeout=40)
    if not els:
        dump_inputs(sb)
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
        # 方式3: 再走一次 JS 原生 setter(不依赖 arguments 传参, 旧写法会报
        # ReferenceError), 校验不一致则拒绝提交, 避免用错码去撞登录风控
        log("逐格填入仍未生效, 再试 JS 原生 setter...")
        n3, val3 = js_fill_code(sb, code)
        sb.sleep(1)
        if not (n3 > 0 and val3 == code):
            save_shot(sb, "error_code_mismatch")
            raise RuntimeError(
                "验证码填入后校验不一致, 拒绝提交(避免无效登录尝试触发风控)")
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

    # 取码通道准备: 只有明确指定 web 模式时才预登录 Gmail 网页版。
    # 教训(2026-11-04 run 41680849009): auto 模式下预登录 Gmail 网页版
    # 会被 Google 风控拦截, 且 IMAP 本来就能取到码, 这一步纯属多余风险,
    # 因此 auto 模式默认只走 IMAP, 网页版仅在 IMAP 失败时由 get_code 兜底。
    gmail_tab, baseline = None, None
    if CODE_SOURCE == "web":
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
    """确定性导航: 登录后 -> 左侧 My Servers -> 目标服务器卡片 -> Overview.
    旧版兜底 `a[href*="/server"]` 会误点 Deploy New Server 链接(其 href
    也含 /server), 导致停在新建服务器页、根本没有 Remaining 可读——
    2026-11-04 run 41684794037 正是死在这里, 不是续期本身失败。
    """
    target = SERVER_NAME or "myappas"
    # 找卡片的标识: 优先用服务器 ID(拼写无关), 其次用名字
    ident = SERVER_ID or target
    # 1) 先关 Cookie 横幅, 免得遮挡左侧导航
    try:
        if sb.is_element_visible('//button[contains(., "Only necessary")]'):
            sb.click('//button[contains(., "Only necessary")]')
            sb.sleep(1)
    except Exception:
        pass
    # 2) 点左侧导航的 My Servers(精确文本), 而不是任何含 server 的链接。
    # 但登录后默认就已经落在服务器列表页(证据: run 41747780282 的截图,
    # 面包屑就是 Dashboard > My Servers), 且侧栏 My Servers 是带下拉箭头
    # 的分组、不是直链, 硬点会失败。因此先判断是否已在列表页, 是则跳过;
    # 点导航失败时也再判断一次, 已在列表页就不算失败。
    def _on_server_list():
        try:
            return (sb.is_text_visible("All your servers")
                    or sb.is_text_visible("SLOTS USED"))
        except Exception:
            return False
    if _on_server_list():
        log("登录后已在服务器列表页, 跳过 My Servers 导航点击")
    else:
        try:
            sb.click('//a[normalize-space()="My Servers"]', timeout=10)
            sb.sleep(3)
        except Exception:
            if not _on_server_list():
                save_shot(sb, "error_no_my_servers")
                raise RuntimeError("未能打开 My Servers 列表(且当前页也不是列表页)")
            log("导航点击失败, 但当前页已是服务器列表页, 继续")
    # 3) 在列表里点目标服务器卡片: 链接 href 含 /server 且卡片文本含服务器名
    clicked = False
    for sel in (f'//a[contains(@href, "/server") and contains(., "{ident}")]',
                f'//*[contains(text(), "{ident}")]/ancestor::a[1]'):
        try:
            if sb.is_element_visible(sel):
                sb.click(sel)
                clicked = True
                log(f"已点击服务器卡片(标识: {ident}, 目标: {target})")
                break
        except Exception:
            continue
    if not clicked:
        # 列表页兜底: 卡片上的 Manage 按钮。单服务器时可直接点唯一那个;
        # 多服务器时不乱点, 宁可报错。
        try:
            manage_btns = sb.find_elements(
                '//*[contains(., "Manage")][self::button or self::a]',
                by="xpath")
            if len(manage_btns) == 1:
                manage_btns[0].click()
                clicked = True
                log(f"已点击唯一服务器的 Manage 按钮(目标: {target})")
        except Exception:
            pass
    if not clicked:
        save_shot(sb, "error_no_server_card")
        raise RuntimeError(f"My Servers 列表里未找到服务器 {target} 的卡片")
    sb.sleep(5)
    # 4) 若有 Manage 按钮/标签, 再点进管理页
    try:
        if sb.is_element_visible('//a[contains(., "Manage")] | //button[contains(., "Manage")]'):
            sb.click('//a[contains(., "Manage")] | //button[contains(., "Manage")]')
            sb.sleep(4)
    except Exception:
        pass
    # 5) 确保停在 Overview 标签(优先点链接/标签形态的 Overview)
    try:
        if sb.is_element_visible('//a[contains(., "Overview")]'):
            sb.click('//a[contains(., "Overview")]')
            sb.sleep(3)
        elif sb.is_element_visible('//*[contains(text(), "Overview")]'):
            sb.click('//*[contains(text(), "Overview")]')
            sb.sleep(3)
    except Exception:
        pass
    # 6) 校验: 必须是服务器 Overview, 绝不能是 Deploy New Server 页。
    # 注意: 绝不能用整页 body 文本判 "Deploy New Server"——左侧导航栏
    # 在每一页都含有这几个字, 那样连正确的 Overview 页也会被误判走偏。
    # 因此只看主标题(h1/h2)和 URL 路径。
    try:
        url = sb.get_current_url()
    except Exception:
        url = ""
    try:
        headings = " ".join(
            (e.text or "") for e in
            sb.find_elements("//h1 | //h2", by="xpath"))
    except Exception:
        headings = ""
    try:
        page = sb.get_text("body") or ""
    except Exception:
        page = ""
    if "Deploy New Server" in headings or "/new" in url or "deploy" in url:
        save_shot(sb, "error_wrong_page_deploy")
        raise RuntimeError(f"导航走偏: 停在了 Deploy New Server 页({url}), "
                           "未进入服务器 Overview, 拒绝继续读 Remaining")
    if "Overview" not in page:
        save_shot(sb, "error_no_overview_tab")
        raise RuntimeError(f"未进入服务器 Overview 页({url})")
    save_shot(sb, "overview")
    log(f"服务器 Overview: {url} (目标: {target})")


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


def _find_button_selector(sb, selectors):
    """在当前 DOM 里找第一个可见的目标按钮选择器。
    按钮可能不是 <button>/<a>(如 div 模拟按钮或 input 提交按钮),
    因此选择器表里应同时包含: button/a 的 contains、任意元素的精确文本、
    input 的 value 匹配。run 41760381506 里 STEP 页的 CLICK TO VERIFY
    按钮明明可见, 却因只查 button/a 而 3 次都没找到。"""
    for sel in selectors:
        try:
            if sb.is_element_visible(sel):
                return sel
        except Exception:
            continue
    return None


def _click_button_in_frames(sb, selectors, tag):
    """在各 iframe 里找目标按钮并直接在该 frame 内点击。
    必须在 frame 内点完再切回: 切回默认内容后原 frame 的元素引用就失效了,
    先返回选择器再点必然失败。找不到返回 False。"""
    try:
        frames = sb.find_elements("iframe")
    except Exception:
        return False
    if frames:
        log(f"[{tag}] iframe 数量: {len(frames)}")
    for idx, frame in enumerate(frames):
        try:
            sb.switch_to_frame(frame)
        except Exception:
            try:
                sb.switch_to_frame(idx)
            except Exception:
                continue
        try:
            sel = _find_button_selector(sb, selectors)
            if sel:
                try:
                    sb.click(sel)
                    return True
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            try:
                sb.switch_to_default_content()
            except Exception:
                pass
    return False


def _js_click_by_text(sb, text):
    """用 JS 遍历 light DOM + shadow root, 按精确文本找元素并点击。
    普通 XPath/CSS 穿不透 shadow DOM: 截图可见、所有选择器都找不到时,
    按钮最可能就藏在 shadow root 里。返回 'clicked:TAG' 或 'not-found'。
    文本用 json 转义后拼进 JS, 不经过 execute_script 的 arguments 参数
    (旧版曾因 arguments 未定义而崩过)。"""
    lit = json.dumps(text)
    lit_up = json.dumps(text.upper())
    js = (
        "var out = [], outCi = [];"
        "function walk(root) {"
        "  var els = root.querySelectorAll('*');"
        "  for (var i = 0; i < els.length; i++) {"
        "    var el = els[i];"
        "    if (el.shadowRoot) { walk(el.shadowRoot); }"
        "    var t = (el.innerText || el.textContent || '').trim();"
        "    if (t === " + lit + ") { out.push(el); }"
        "    else if (t.toUpperCase() === " + lit_up + ") { outCi.push(el); }"
        "  }"
        "}"
        "var docs = [document];"
        "var frs = document.querySelectorAll('iframe');"
        "for (var k = 0; k < frs.length; k++) {"
        "  try { if (frs[k].contentDocument) { docs.push(frs[k].contentDocument); } }"
        "  catch (e) {}"
        "}"
        "for (var d = 0; d < docs.length; d++) { walk(docs[d]); }"
        "var cands = out.length > 0 ? out : outCi;"
        "if (cands.length === 0) { return 'not-found'; }"
        "var el = null;"
        "for (var j = 0; j < cands.length; j++) {"
        "  if (['BUTTON', 'A', 'INPUT'].indexOf(cands[j].tagName) >= 0) {"
        "    el = cands[j]; break;"
        "  }"
        "}"
        "if (!el) { el = cands[cands.length - 1]; }"
        "el.scrollIntoView({block: 'center'});"
        "el.click();"
        "return 'clicked:' + el.tagName;"
    )
    try:
        return sb.execute_script(js)
    except Exception as e:
        return f"js-error: {e}"


def _js_text_visible(sb, text):
    """用 JS 判断精确文本是否存在(穿透 shadow DOM, 容忍空白符差异)。
    只做存在性判断, 不点击, 供 Get Link 等待循环兜底。"""
    lit = json.dumps(text)
    lit_up = json.dumps(text.upper())
    js = (
        "var found = false;"
        "function walk(root) {"
        "  if (found) { return; }"
        "  var els = root.querySelectorAll('*');"
        "  for (var i = 0; i < els.length; i++) {"
        "    var el = els[i];"
        "    if (el.shadowRoot) { walk(el.shadowRoot); }"
        "    var t = (el.innerText || el.textContent || '').trim();"
        "    if (t === " + lit + " || t.toUpperCase() === " + lit_up + ") {"
        "      found = true; return;"
        "    }"
        "  }"
        "}"
        "var docs = [document];"
        "var frs = document.querySelectorAll('iframe');"
        "for (var k = 0; k < frs.length; k++) {"
        "  try { if (frs[k].contentDocument) { docs.push(frs[k].contentDocument); } }"
        "  catch (e) {}"
        "}"
        "for (var d = 0; d < docs.length; d++) { walk(docs[d]); }"
        "return found;"
    )
    try:
        return bool(sb.execute_script(js))
    except Exception:
        return False


def click_and_settle(sb, selectors, flow_handle, main_handle, tag, tries=3,
                     button_text=None):
    """
    在 flow 窗口(shrinkme/中间页)里点击目标按钮, 处理 popunder:
    每次点击后关掉多余广告窗、切回 flow 窗口; 若页面无变化则重点。
    找按钮的顺序: 普通选择器 -> iframe -> JS 穿透 shadow DOM 按文本点。
    返回 True 表示至少成功点了一次。
    """
    clicked_any = False
    for attempt in range(1, tries + 1):
        if not focus_window(sb, flow_handle):
            log(f"[{tag}] flow 窗口已不存在, 停止点击")
            return clicked_any
        before_url = sb.get_current_url()
        el = _find_button_selector(sb, selectors)
        frame_clicked = False
        if not el:
            frame_clicked = _click_button_in_frames(sb, selectors, tag)
        js_clicked = False
        if not el and not frame_clicked and button_text:
            js_res = _js_click_by_text(sb, button_text)
            log(f"[{tag}] JS 兜底找按钮({button_text}): {js_res}")
            js_clicked = isinstance(js_res, str) and js_res.startswith("clicked:")
        if not el and not frame_clicked and not js_clicked:
            log(f"[{tag}] 第 {attempt} 次: 未找到可点击按钮")
            sb.sleep(3)
            continue
        if frame_clicked:
            clicked_any = True
            log(f"[{tag}] 第 {attempt} 次 iframe 内点击成功")
        elif js_clicked:
            clicked_any = True
            log(f"[{tag}] 第 {attempt} 次 JS 点击成功")
        else:
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


def _captcha_gone(sb):
    """验证页是否已不在: 无 'check the captcha' 提示, 且无 Cloudflare 框。
    注意: 父页面读不到跨域 iframe 里的勾选状态, 所以这只能判“框消失”,
    不能判“已勾选通过”——真正的放行只能由 Continue 点击后的页面推进来验证。"""
    try:
        if sb.is_text_visible("Please check the captcha"):
            return False
    except Exception:
        pass
    try:
        if sb.is_element_present('iframe[src*="challenges.cloudflare.com"]'):
            return False
        if sb.is_element_present('iframe[title*="Cloudflare"]'):
            return False
    except Exception:
        pass
    return True


def pass_turnstile(sb, flow_handle, main_handle, tag, timeout=60):
    """
    对验证页做一轮“点验证码框 + 等验证”:
    - 若验证码框已消失, 直接算过;
    - 否则点一次框, 等最多 timeout 秒看框是否消失;
    - 即使框还在, 也返回 True 让调用方去点 Continue, 由页面是否推进
      来最终判定(框可能通过后仍显示, 死等它消失会误杀)。
    关键教训(run 41754237293): “Click here to continue”按钮在验证页上
    一直可见, 它出现≠验证通过。旧版一见 Continue 就判放行, 结果
    Continue 点完仍停留在验证页, 后续白白等了 120 秒 Get Link。
    返回 False 只表示窗口丢失或点框异常, 不代表验证一定没过。
    """
    if not focus_window(sb, flow_handle):
        return False
    if _captcha_gone(sb):
        log(f"[{tag}] 验证码框已不在, 视为已通过")
        return True
    try:
        log(f"[{tag}] 尝试点击 Turnstile 验证框...")
        sb.uc_gui_click_captcha()
    except Exception as e:
        log(f"[{tag}] uc_gui_click_captcha 异常: {e}")
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        sb.sleep(5)
        close_extra_windows(sb, {flow_handle, main_handle})
        focus_window(sb, flow_handle)
        try:
            if _captcha_gone(sb):
                log(f"[{tag}] 验证码框已消失")
                save_shot(sb, f"{tag}_turnstile_passed")
                return True
        except Exception:
            pass
    log(f"[{tag}] 等验证框消失超时({timeout}s), 仍尝试点 Continue 看能否推进")
    return True


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
    # 过验证码 -> 点 Continue -> 确认页面真的离开了验证页。
    # 若点完仍停留在验证页(仍有 check the captcha 提示), 说明验证码
    # 没真正通过, 回炉重来, 最多 3 轮, 避免在验证页上白等 Get Link。
    advanced = False
    for cycle in range(1, 4):
        if not pass_turnstile(sb, flow_handle, main_handle, tag):
            return False
        ok = click_and_settle(
            sb,
            ['//button[contains(., "Click here to continue")]',
             '//a[contains(., "Click here to continue")]',
             '//*[normalize-space()="Click here to continue"]',
             '//input[contains(@value, "Click here to continue")]',
             'button:contains("Click here to continue")'],
            flow_handle, main_handle, f"{tag}_step1_continue",
            button_text="Click here to continue")
        save_shot(sb, f"{tag}_after_step1")
        if not ok:
            log(f"[{tag}] 第 1 步 Continue 点击失败")
            return False
        sb.sleep(3)
        focus_window(sb, flow_handle)
        try:
            still_captcha = sb.is_text_visible("Please check the captcha")
        except Exception:
            still_captcha = False
        if not still_captcha:
            advanced = True
            break
        log(f"[{tag}] 第 {cycle} 轮 Continue 后仍停留在验证页, 回炉重过验证码...")
    if not advanced:
        log(f"[{tag}] 多轮后仍未离开验证页, 验证码未真正通过")
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
             '//*[normalize-space()="CLICK TO VERIFY"]',
             '//input[contains(@value, "CLICK TO VERIFY")]',
             'button:contains("CLICK TO VERIFY")'],
            flow_handle, main_handle, f"{tag}_step2_verify",
            button_text="CLICK TO VERIFY")
        save_shot(sb, f"{tag}_after_step2")
        if not ok:
            log(f"[{tag}] 第 2 步 CLICK TO VERIFY 点击失败")
            return False
    else:
        log(f"[{tag}] 未出现 STEP 中间页, 直接进入第 3 步(当前: {sb.get_current_url()})")

    # --- 第 2.5 步: STEP 1 和 STEP 2 都要滚到底再点 Continue... ---
    # STEP 1 提示是 "To scroll down, click Continue...",
    # STEP 2 提示是 "Scroll down to Generate Link, or click Continue...",
    # 两处都含 "click Continue"。只做一轮的话, 会在 STEP 2 OF 2 页上
    # 白等 Get Link 120 秒(run 41766250866 的截图证明了这点)。
    # Deborah 的建议: Continue 按钮多点几次 —— 因此一直点下去:
    # 只要还看到提示/按钮就滚到底+点, 点不上也不立刻放弃, 下轮重滚重试;
    # 直到提示消失(进了倒计时)才进第 3 步, 最多 6 轮封顶。
    if is_step_page:
        saw_hint = False
        clicked_once = False
        for round_no in range(1, 7):
            try:
                scroll_hint = sb.is_text_visible("click Continue") \
                    or _js_text_visible(sb, "Continue...")
            except Exception:
                scroll_hint = False
            if not scroll_hint:
                if saw_hint:
                    log(f"[{tag}] 滚到底提示已消失, 进入倒计时判断")
                break
            saw_hint = True
            log(f"[{tag}] 第 {round_no} 轮滚到底提示, 正在分段滚动页面...")
            focus_window(sb, flow_handle)
            try:
                # 懒加载页面滚到底后高度还会变, 每步重读高度, 直到滚不动为止
                y = 0
                for _ in range(60):
                    h = sb.execute_script("return document.body.scrollHeight") or 0
                    if y >= h:
                        break
                    sb.execute_script(f"window.scrollTo(0, {y})")
                    y += 500
                    sb.sleep(1)
                sb.execute_script("window.scrollTo(0, document.body.scrollHeight)")
            except Exception as e:
                log(f"[{tag}] 滚动异常: {e}")
            sb.sleep(3)
            ok2 = click_and_settle(
                sb,
                ['//button[contains(., "Continue")]',
                 '//a[contains(., "Continue")]',
                 '//*[normalize-space()="Continue..."]',
                 '//input[contains(@value, "Continue")]'],
                flow_handle, main_handle, f"{tag}_step2_scroll_continue_r{round_no}",
                button_text="Continue...")
            save_shot(sb, f"{tag}_after_scroll_continue_r{round_no}")
            if ok2:
                clicked_once = True
                log(f"[{tag}] 第 {round_no} 轮 Continue... 已点, 等待下一步")
            else:
                log(f"[{tag}] 第 {round_no} 轮 Continue... 未点上, 下轮重滚重试")
            sb.sleep(3)
        if saw_hint and not clicked_once:
            log(f"[{tag}] 多轮 Continue... 均未点上")
            return False
        log(f"[{tag}] 滚动点击阶段结束, 进入第 3 步")

    # --- 第 3 步: YOUR LINK IS ALMOST READY -> 倒计时 -> Get Link ---
    focus_window(sb, flow_handle)
    deadline = time.time() + 120
    get_link_sel = ('//button[contains(., "Get Link")] | '
                    '//a[contains(., "Get Link")] | '
                    '//*[normalize-space()="Get Link"] | '
                    '//input[contains(@value, "Get Link")]')
    ready = False
    waited = 0
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
            elif _js_text_visible(sb, "Get Link"):
                # XPath 未命中但 JS 检出: 文本空白符差异, 点 Get Link 环节有 JS 兜底
                log(f"[{tag}] Get Link 已出现(JS 检出, XPath 未命中)...")
                ready = True
                break
        except Exception:
            pass
        sb.sleep(5)
        waited += 5
        if waited % 30 == 0:
            # 每 30 秒留一条进度, 免得下次看日志只剩空白
            try:
                log(f"[{tag}] 等 Get Link 中({waited}s, 当前: {sb.get_current_url()})")
            except Exception:
                pass
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
            js_res = _js_click_by_text(sb, "Get Link")
            log(f"[{tag}] JS 兜底点 Get Link: {js_res}")
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


def _js_find_by_text(sb, text):
    """用 JS 按渲染文本找元素, 返回 WebElement 或 None。
    穿透 shadow DOM, 容忍空白符差异(innerText 归一化)。
    execute_script 可以直接返回 DOM 元素, 调用方用 WebDriver 原生点击
    (可信点击, 比 JS el.click() 更不容易被页面忽略)。
    CLICK TO VERIFY 就是靠这招才找到的, Add 按钮同理兜底。"""
    lit = json.dumps(text)
    lit_up = json.dumps(text.upper())
    js = (
        "var out = [], outCi = [];"
        "function walk(root) {"
        "  var els = root.querySelectorAll('*');"
        "  for (var i = 0; i < els.length; i++) {"
        "    var el = els[i];"
        "    if (el.shadowRoot) { walk(el.shadowRoot); }"
        "    var t = (el.innerText || el.textContent || '').trim();"
        "    if (t === " + lit + ") { out.push(el); }"
        "    else if (t.toUpperCase() === " + lit_up + ") { outCi.push(el); }"
        "  }"
        "}"
        "var docs = [document];"
        "var frs = document.querySelectorAll('iframe');"
        "for (var k = 0; k < frs.length; k++) {"
        "  try { if (frs[k].contentDocument) { docs.push(frs[k].contentDocument); } }"
        "  catch (e) {}"
        "}"
        "for (var d = 0; d < docs.length; d++) { walk(docs[d]); }"
        "var cands = out.length > 0 ? out : outCi;"
        "if (cands.length === 0) { return null; }"
        "for (var j = 0; j < cands.length; j++) {"
        "  if (['BUTTON', 'A', 'INPUT'].indexOf(cands[j].tagName) >= 0) {"
        "    return cands[j];"
        "  }"
        "}"
        "return cands[cands.length - 1];"
    )
    try:
        return sb.execute_script(js)
    except Exception:
        return None


def _js_find_by_contains(sb, *needles):
    """用 JS 按子串找元素(渲染文本同时包含所有 needles), 返回 WebElement 或 None。
    精确文本匹配会被图标字符/多余空白破坏, 子串匹配不受影响; 不分标签。"""
    conds = " && ".join(
        f"(t.indexOf({json.dumps(n)}) >= 0)" for n in needles)
    js = (
        "var out = [];"
        "function walk(root) {"
        "  var els = root.querySelectorAll('*');"
        "  for (var i = 0; i < els.length; i++) {"
        "    var el = els[i];"
        "    if (el.shadowRoot) { walk(el.shadowRoot); }"
        "    var t = (el.innerText || el.textContent || '').trim();"
        "    if (" + conds + ") { out.push(el); }"
        "  }"
        "}"
        "var docs = [document];"
        "var frs = document.querySelectorAll('iframe');"
        "for (var k = 0; k < frs.length; k++) {"
        "  try { if (frs[k].contentDocument) { docs.push(frs[k].contentDocument); } }"
        "  catch (e) {}"
        "}"
        "for (var d = 0; d < docs.length; d++) { walk(docs[d]); }"
        "if (out.length === 0) { return null; }"
        "for (var j = 0; j < out.length; j++) {"
        "  if (['BUTTON', 'A', 'INPUT'].indexOf(out[j].tagName) >= 0) {"
        "    return out[j];"
        "  }"
        "}"
        "return out[out.length - 1];"
    )
    try:
        return sb.execute_script(js)
    except Exception:
        return None


def find_add_button(sb, hours):
    """在 Overview 页找 Add XX Hours 按钮, 返回 (xpath, 元素或None)。
    放宽两点(run 41754237293 里 Add 12 明明可见却没找到):
    1) 按钮可能不是 <button>/<a>(如 div 模拟按钮), 兜底按精确文本
       "Add XX Hours" 找任意元素;
    2) 面板可能渲染稍慢, 轮询 3 次再放弃。"""
    xpaths = [
        (f'//button[contains(., "Add") and contains(., "{hours}")] | '
         f'//a[contains(., "Add") and contains(., "{hours}")]'),
        f'//*[normalize-space()="Add {hours} Hours"]',
    ]
    for _ in range(3):
        for xpath in xpaths:
            try:
                els = []
                for e in sb.find_elements(xpath, by="xpath"):
                    try:
                        if e.is_displayed():
                            els.append(e)
                    except Exception:
                        # 单个元素过期(如倒计时每秒重绘面板)不能带走整批结果
                        continue
                if els:
                    return xpath, els[0]
            except Exception:
                continue
        sb.sleep(3)
    # XPath 全军覆没时用 JS 渲染文本找: 面板按钮也可能有空白符/Shadow 问题,
    # Add 12 在多个运行里明明可见却找不到, 不能再带着这个已知问题跑。
    try:
        el = _js_find_by_text(sb, f"Add {hours} Hours")
        if el is not None:
            try:
                if el.is_displayed():
                    log(f"[Add {hours} Hours] JS 兜底找到按钮")
                    return "JS", el
            except Exception:
                pass
    except Exception:
        pass
    # 精确文本也找不到时, 按子串找(图标字符/多余空白会破坏精确匹配,
    # 但 "Add"+"12" 子串一定在): 不分标签, 优先 button/a/input。
    try:
        el = _js_find_by_contains(sb, "Add", str(hours))
        if el is not None:
            try:
                if el.is_displayed():
                    log(f"[Add {hours} Hours] JS 子串兜底找到按钮")
                    return "JS", el
            except Exception:
                pass
    except Exception:
        pass
    # 全部找不到时留一份 DOM 证据, 下次不再靠猜。
    try:
        dump = sb.execute_script(
            "var out = [];"
            "var els = document.querySelectorAll('*');"
            "for (var i = 0; i < els.length; i++) {"
            "  var t = (els[i].innerText || els[i].textContent || '').trim();"
            "  if (t.indexOf('Add') >= 0 && t.length < 60) {"
            "    out.push(els[i].tagName + '|' + t);"
            "  }"
            "}"
            "return out.slice(0, 20).join('; ');")
        log(f"[Add {hours} Hours] DOM 证据(文本含 Add 的元素): {dump}")
    except Exception as e:
        log(f"[Add {hours} Hours] DOM 证据采集失败: {e}")
    return xpaths[0], None


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
    # 若刷新后不在 Overview, 重新进一次。复查时当前窗可能已不是面板
    # (如停在广告页), 先回面板首页再进 Overview, 否则导航点击必然失败。
    after_minutes, after_raw = get_remaining(sb)
    if after_minutes is None:
        try:
            try:
                sb.open(PANEL_URL)
                sb.sleep(3)
            except Exception:
                pass
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
    # 阶段变量: 只有阶段真正开始时才推进, 失败信息按它来报, 绝不能让
    # 启动阶段的失败打印成登录失败、让登录失败打印成续期失败。
    stage = "startup"
    # 浏览器会话: 配置了家宽代理就全程走代理(登录和 shrinkme 都用家宽 IP,
    # 更像真人); 未配置则直连。seleniumbase 的 proxy 参数支持带账号密码
    # 的代理, 认证由它内部处理。
    sb_kwargs = dict(uc=True, test=True, locale_code="en",
                     headless=False, xvfb=True)
    log(f"浏览器代理: {proxy_label()}")
    # 代理处理(2026-11-04 run 41748751838 教训): vless 节点浏览器不能直接用,
    # 在此转成本地代理; vmess/trojan/ss 等暂不支持, 明说后退出, 不启动浏览器、
    # 不消耗任何登录尝试。
    if PROXY:
        _scheme = PROXY.split("://", 1)[0].lower() if "://" in PROXY else ""
        if _scheme == "vless":
            sb_kwargs["proxy"] = start_vless_proxy(PROXY)
        elif _scheme in ("vmess", "trojan", "ss", "ssr"):
            log(f"PROXY 格式错误: 当前值是 {_scheme} 协议节点, 暂不支持转换; "
                "请换成 http/socks5 节点或 vless 节点, 本次不启动浏览器")
            sys.exit(1)
        else:
            sb_kwargs["proxy"] = PROXY
    try:
        with SB(**sb_kwargs) as sb:
            try:
                stage = "login"
                do_login(sb)
                stage = "overview"
                open_server_overview(sb)
                stage = "renew"
                main_handle = sb.driver.current_window_handle

                before, before_raw = get_remaining(sb)
                if before is None:
                    save_shot(sb, "error_no_remaining")
                    raise RuntimeError(
                        "Overview 页未能解析到 Remaining 剩余时间 "
                        f"(当前页: {sb.get_current_url()})")
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
            # 注意: 这句话只表示“结束时未确认到账”, 不代表 Add 按钮被执行过;
            # 死在登录/Overview 阶段时 Add 流程根本未开始, 见上方阶段日志。
            log("结束时未确认任何 Add 到账(若死在登录/Overview 阶段, "
                "Add 流程根本未开始), 以非 0 退出码结束(便于 Actions 标红排查)")
            sys.exit(2)
    except SystemExit:
        raise
    except Exception as e:
        if stage == "startup":
            log("启动阶段失败, 浏览器未启动, HOKAS 未产生任何登录或操作")
        elif stage == "login":
            log("登录阶段失败, 本次未能进入续期流程(Add 按钮根本没被执行, "
                "不是续期本身失败)")
        elif stage == "overview":
            log("进入服务器 Overview 阶段失败, Add 流程根本没被执行, "
                "不是续期本身失败")
        log(f"任务失败: {e}")
        traceback.print_exc()
        result_lines.append(f"❌ 续期失败: {e}")
        send_tg("\n".join(result_lines))
        sys.exit(1)


if __name__ == "__main__":
    main()

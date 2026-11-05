# -*- coding: utf-8 -*-
"""
hokas.net 免费服务器自动续期脚本
(首选 Cookie 登入，失败自动回退到邮箱+密码登入；带出口 IP 检测与 sing-box 转发)
"""

import atexit
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import traceback
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime

import requests
from seleniumbase import SB

# ---------------- 配置 ----------------
EMAIL = (os.environ.get("EMAIL") or "").strip()
PASSWORD = (os.environ.get("PASSWORD") or os.environ.get("EMAIL_PASSWORD") or "").strip()
# Cookie 环境变量 (支持 JSON 数组字符串或 key=value 字符串)
COOKIES_ENV = (os.environ.get("COOKIES") or os.environ.get("HOKAS_COOKIES") or "").strip()

TG_BOT_TOKEN = (os.environ.get("TG_BOT_TOKEN") or "").strip()
TG_CHAT_ID = (os.environ.get("TG_CHAT_ID") or "").strip()
PANEL_URL = (os.environ.get("PANEL_URL") or "https://hokas.net").strip().rstrip("/")
SIGN_IN_URL = (os.environ.get("SIGN_IN_URL") or "https://hokas.net/sign-in").strip()
SERVER_NAME = (os.environ.get("SERVER_NAME") or "").strip()
SERVER_ID = (os.environ.get("SERVER_ID") or "").strip()

# 代理配置
PROXY = (os.environ.get("PROXY") or "").strip()
PROXY_CORE = (os.environ.get("PROXY_CORE") or "sing-box").strip().lower()

SHOT_DIR = os.path.join(os.getcwd(), "screenshots")
_shot_counter = [0]


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def proxy_label():
    if not PROXY:
        return "未配置(直连)"
    try:
        rest = PROXY.split("://", 1)[1] if "://" in PROXY else PROXY
        hostport = rest.split("@")[-1].split("?")[0].split("#")[0].split("/")[0]
        scheme = PROXY.split("://", 1)[0] if "://" in PROXY else "http"
        return f"已启用({scheme}://{hostport}, 内核: {PROXY_CORE})"
    except Exception:
        return "已启用(地址已脱敏)"


# ---------------- 后台转发进程管理 ----------------
_proxy_proc = None


def _stop_proxy():
    global _proxy_proc
    try:
        if _proxy_proc is not None and _proxy_proc.poll() is None:
            _proxy_proc.terminate()
    except Exception:
        pass


def _print_proxy_tail(log_path):
    try:
        if not os.path.isfile(log_path):
            return
        tail = open(log_path, errors="replace").read()[-3000:]
        if tail.strip():
            log(f"代理转发内核运行时日志(末尾):\n{tail.strip()}")
    except Exception:
        pass


# ---------------- sing-box 节点转换与运行 ----------------
def ensure_singbox():
    bindir = "/tmp/singbox-bin"
    exe = os.path.join(bindir, "sing-box")
    if os.path.isfile(exe) and os.access(exe, os.X_OK):
        return exe
    os.makedirs(bindir, exist_ok=True)
    tar_path = os.path.join(bindir, "singbox.tar.gz")
    log("正在获取 sing-box 最新版本并下载...")
    try:
        api_url = "https://api.github.com/repos/SagerNet/sing-box/releases/latest"
        req = urllib.request.Request(api_url, headers={"User-Agent": "hokas-renew"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        down_url = None
        for asset in data.get("assets", []):
            name = asset.get("name", "")
            if "linux-amd64.tar.gz" in name:
                down_url = asset.get("browser_download_url")
                break
        if not down_url:
            raise RuntimeError("未在 Release 中找到 linux-amd64.tar.gz 资产")
    except Exception as e:
        log(f"获取 sing-box release 失败，尝试备用地址: {e}")
        down_url = "https://github.com/SagerNet/sing-box/releases/download/v1.10.7/sing-box-1.10.7-linux-amd64.tar.gz"

    req = urllib.request.Request(down_url, headers={"User-Agent": "hokas-renew"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(tar_path, "wb") as f:
        shutil.copyfileobj(resp, f)

    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name.endswith("/sing-box") or member.name == "sing-box":
                f = tar.extractfile(member)
                if f:
                    with open(exe, "wb") as dest:
                        dest.write(f.read())
                    break
    os.chmod(exe, 0o755)
    return exe


def parse_uri_to_singbox_outbound(uri):
    p = urllib.parse.urlparse(uri)
    scheme = p.scheme.lower()
    host = p.hostname
    port = p.port
    q = {k: v[0] for k, v in urllib.parse.parse_qs(p.query).items()}

    if scheme == "vless":
        uuid = p.username
        if not uuid or not host or not port:
            raise ValueError("vless 缺少必要字段")
        outbound = {
            "type": "vless",
            "tag": "proxy-out",
            "server": host,
            "server_port": port,
            "uuid": uuid,
            "flow": q.get("flow", ""),
        }
        sec = (q.get("security") or "none").lower()
        if sec in ("tls", "reality"):
            tls_cfg = {
                "enabled": True,
                "server_name": q.get("sni") or q.get("serverName") or host,
                "utls": {"enabled": True, "fingerprint": q.get("fp") or "chrome"}
            }
            if sec == "reality":
                tls_cfg["reality"] = {
                    "enabled": True,
                    "public_key": q.get("pbk", ""),
                    "short_id": q.get("sid", "")
                }
            outbound["tls"] = tls_cfg

        net = (q.get("type") or "tcp").lower()
        if net == "ws":
            outbound["transport"] = {
                "type": "ws",
                "path": q.get("path") or "/",
                "headers": {"Host": q.get("host") or host}
            }
        elif net == "grpc":
            outbound["transport"] = {
                "type": "grpc",
                "service_name": q.get("serviceName", "")
            }
        return outbound

    elif scheme in ("hysteria2", "hy2"):
        password = p.password or p.username
        if not password or not host or not port:
            raise ValueError("hysteria2 缺少必要字段")
        outbound = {
            "type": "hysteria2",
            "tag": "proxy-out",
            "server": host,
            "server_port": port,
            "password": password,
            "tls": {
                "enabled": True,
                "server_name": q.get("sni") or q.get("peer") or host,
                "insecure": (q.get("insecure") or "").lower() in ("1", "true")
            }
        }
        obfs_type = (q.get("obfs") or "").lower()
        if obfs_type == "salamander":
            outbound["obfs"] = {
                "type": "salamander",
                "password": q.get("obfs-password") or q.get("obfs_password") or ""
            }
        return outbound

    elif scheme == "trojan":
        password = p.password or p.username
        return {
            "type": "trojan",
            "tag": "proxy-out",
            "server": host,
            "server_port": port,
            "password": password,
            "tls": {
                "enabled": True,
                "server_name": q.get("sni") or host
            }
        }

    elif scheme == "ss":
        userinfo = p.username
        if p.password:
            userinfo = f"{p.username}:{p.password}"
        try:
            import base64
            userinfo = base64.b64decode(userinfo + "==").decode()
        except Exception:
            pass
        if ":" not in userinfo:
            raise ValueError("Shadowsocks 用户信息解析失败")
        method, password = userinfo.split(":", 1)
        return {
            "type": "shadowsocks",
            "tag": "proxy-out",
            "server": host,
            "server_port": port,
            "method": method,
            "password": password
        }

    else:
        raise ValueError(f"sing-box 暂不支持直接解析协议: {scheme}")


def start_singbox_proxy(uri):
    global _proxy_proc
    log("使用 sing-box 启动本地代理转发...")
    sb_bin = ensure_singbox()
    outbound = parse_uri_to_singbox_outbound(uri)
    config = {
        "log": {"level": "warn"},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": 1081
            }
        ],
        "outbounds": [
            outbound,
            {"type": "direct", "tag": "direct"}
        ]
    }
    cfg_path = "/tmp/singbox-config.json"
    with open(cfg_path, "w") as f:
        json.dump(config, f)
    os.chmod(cfg_path, 0o600)

    err_path = "/tmp/proxy-error.log"
    err_f = open(err_path, "wb")
    _proxy_proc = subprocess.Popen(
        [sb_bin, "run", "-c", cfg_path],
        stdout=subprocess.DEVNULL, stderr=err_f)
    atexit.register(_stop_proxy)

    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", 1081), timeout=2):
                log("sing-box 转发就绪: 本地混合代理 127.0.0.1:1081")
                return "127.0.0.1:1081"
        except OSError:
            if _proxy_proc.poll() is not None:
                err_f.close()
                _print_proxy_tail(err_path)
                raise RuntimeError("sing-box 进程意外退出，转换失败")
            time.sleep(1)
    _stop_proxy()
    raise RuntimeError("等待本地代理端口 1081 超时，sing-box 转换失败")


# ---------------- Xray 备用转换 ----------------
def parse_vless_uri(uri):
    p = urllib.parse.urlparse(uri)
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
    stream = {"network": node["network"]}
    sec = node["security"]
    if sec == "tls":
        stream["security"] = "tls"
        stream["tlsSettings"] = {"serverName": node["sni"], "fingerprint": node["fp"]}
    elif sec == "reality":
        stream["security"] = "reality"
        stream["realitySettings"] = {"serverName": node["sni"], "publicKey": node["pbk"], "shortId": node["sid"], "fingerprint": node["fp"]}
    if node["network"] == "ws":
        stream["wsSettings"] = {"path": node["ws_path"], "headers": {"Host": node["ws_host"]}}
    elif node["network"] == "grpc":
        stream["grpcSettings"] = {"serviceName": node["service_name"]}
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {"port": 1081, "listen": "127.0.0.1", "protocol": "http", "settings": {}},
        ],
        "outbounds": [
            {"protocol": "vless",
             "settings": {"vnext": [{"address": node["address"], "port": node["port"], "users": [{"id": node["uuid"], "encryption": "none", "flow": node["flow"]}]}]},
             "streamSettings": stream},
        ],
    }


def ensure_xray():
    bindir = "/tmp/xray-bin"
    exe = os.path.join(bindir, "xray")
    if os.path.isfile(exe) and os.access(exe, os.X_OK):
        return exe
    os.makedirs(bindir, exist_ok=True)
    url = "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip"
    zpath = os.path.join(bindir, "xray.zip")
    log("正在下载 Xray 内核...")
    req = urllib.request.Request(url, headers={"User-Agent": "hokas-renew"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(zpath, "wb") as f:
        shutil.copyfileobj(resp, f)
    with zipfile.ZipFile(zpath) as z:
        z.extract("xray", bindir)
    os.chmod(exe, 0o755)
    return exe


def start_xray_proxy(uri):
    global _proxy_proc
    log("使用 Xray 启动本地代理转换...")
    node = parse_vless_uri(uri)
    xray = ensure_xray()
    cfg_path = "/tmp/xray-config.json"
    with open(cfg_path, "w") as f:
        json.dump(build_xray_config(node), f)
    os.chmod(cfg_path, 0o600)
    err_path = "/tmp/proxy-error.log"
    err_f = open(err_path, "wb")
    _proxy_proc = subprocess.Popen([xray, "run", "-c", cfg_path], stdout=subprocess.DEVNULL, stderr=err_f)
    atexit.register(_stop_proxy)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", 1081), timeout=2):
                log("Xray 转换就绪: 本地代理 127.0.0.1:1081")
                return "127.0.0.1:1081"
        except OSError:
            if _proxy_proc.poll() is not None:
                err_f.close()
                _print_proxy_tail(err_path)
                raise RuntimeError("Xray 进程退出，转换失败")
            time.sleep(1)
    _stop_proxy()
    raise RuntimeError("等待本地代理端口 1081 超时")


def save_shot(sb, name):
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
        _shot_counter[0] += 1
        path = os.path.join(SHOT_DIR, f"{_shot_counter[0]:02d}_{name}.png")
        sb.save_screenshot(path)
        log(f"已保存截图: {path}")
    except Exception as e:
        log(f"截图失败: {e}")


# ---------------- Telegram 通知 ----------------
def send_tg(text):
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


# ---------------- Cookie 解析与加载 ----------------
def load_cookies_data():
    """从环境变量或本地 hokas.net_cookies.json 读取 Cookie 列表"""
    raw = COOKIES_ENV
    if not raw and os.path.isfile("hokas.net_cookies.json"):
        try:
            with open("hokas.net_cookies.json", "r", encoding="utf-8") as f:
                raw = f.read().strip()
                log("已找到本地 hokas.net_cookies.json 文件")
        except Exception as e:
            log(f"读取本地 cookies 文件失败: {e}")

    if not raw:
        return []

    # 尝试按 JSON 解析
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except Exception:
        pass

    # 兼容分号分隔的字符串格式: a=b; c=d
    cookies = []
    for item in raw.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            cookies.append({"name": k.strip(), "value": v.strip(), "domain": ".hokas.net", "path": "/"})
    return cookies


def try_cookie_login(sb):
    """尝试通过 Cookie 直接登录"""
    cookies = load_cookies_data()
    if not cookies:
        log("未找到可用 Cookie 配置，跳过 Cookie 登录")
        return False

    log(f"检测到 {len(cookies)} 个 Cookie，正在尝试 Cookie 免密登入...")
    try:
        # 先打开面板建立域名上下文
        sb.uc_open_with_reconnect(PANEL_URL, reconnect_time=4)
        sb.sleep(2)

        for c in cookies:
            name = c.get("name")
            value = c.get("value")
            if not name or value is None:
                continue

            c_dict = {
                "name": str(name),
                "value": str(value),
                "path": str(c.get("path") or "/"),
            }
            # 域限定处理
            dom = c.get("domain") or ""
            if "hokas.net" in dom:
                c_dict["domain"] = dom
            if "secure" in c:
                c_dict["secure"] = bool(c["secure"])
            if "httpOnly" in c:
                c_dict["httpOnly"] = bool(c["httpOnly"])

            same_site = c.get("sameSite")
            if same_site in ("Strict", "Lax", "None"):
                c_dict["sameSite"] = same_site

            try:
                sb.driver.add_cookie(c_dict)
            except Exception:
                try:
                    c_dict.pop("domain", None)
                    sb.driver.add_cookie(c_dict)
                except Exception:
                    pass

        log("Cookie 植入完成，正在刷新页面验证身份...")
        sb.open(PANEL_URL)
        sb.sleep(4)

        if page_has_servers(sb):
            log("🎉 Cookie 登录成功！直接进入面板")
            save_shot(sb, "cookie_login_success")
            return True
        else:
            log("Cookie 未能直接生效，回退至账号密码登录...")
            return False
    except Exception as e:
        log(f"Cookie 登录过程异常: {e}")
        return False


# ---------------- 邮箱密码交互与登录 ----------------
def find_email_input(sb):
    for sel in (
        'input[name="identifier"]',
        'input#identifier-field',
        'input[type="email"]',
        'input[name="email"]',
        'input[id*="identifier"]',
        'input[autocomplete*="username"]',
        'input[placeholder*="email" i]',
    ):
        try:
            if sb.is_element_visible(sel):
                return sel
        except Exception:
            continue
    return None


def find_password_input(sb):
    for sel in (
        'input[name="password"]',
        'input#password-field',
        'input[type="password"]',
        'input[id*="password"]',
        'input[autocomplete*="password"]',
        'input[placeholder*="password" i]',
    ):
        try:
            if sb.is_element_visible(sel):
                return sel
        except Exception:
            continue
    return None


def click_continue(sb):
    for sel in (
        '//button[contains(translate(., "CONTINUE", "continue"), "continue") and not(contains(translate(., "GOOGLE", "google"), "google"))]',
        'form button.cl-formButtonPrimary',
        'button[data-localization-key="formButtonPrimary"]',
        'form button[type="submit"]',
    ):
        try:
            if sb.is_element_visible(sel):
                sb.click(sel)
                return True
        except Exception:
            continue
    try:
        sb.send_keys('input:focus', "\n")
        return True
    except Exception:
        pass
    return False


def page_has_servers(sb):
    try:
        url = sb.get_current_url() or ""
        if "sign-in" in url or "factor-one" in url or "factor-two" in url:
            return False
        if "/dashboard" in url or "/server" in url:
            return True
        return (sb.is_text_visible("My Servers") or sb.is_text_visible("Your Servers")
                or (SERVER_NAME and sb.is_text_visible(SERVER_NAME))
                or sb.is_text_visible("myappas") or sb.is_text_visible("Dashboard"))
    except Exception:
        return False


def check_login_page_errors(sb):
    try:
        errors = sb.execute_script("""
            var errs = [];
            var els = document.querySelectorAll('.cl-formFieldErrorText, [role="alert"], .cl-alertText, p[id*="error"]');
            els.forEach(el => {
                var t = (el.innerText || el.textContent || '').trim();
                if (t) errs.push(t);
            });
            return errs.join(' | ');
        """)
        if errors:
            log(f"⚠️ 页面报错信息: {errors}")
        body_text = sb.get_text("body") or ""
        if "Check your email" in body_text or "verification code" in body_text.lower():
            log("⚠️ 检测到页面要求输入【邮箱验证码】(Clerk 触发了异地二次验证)")
    except Exception:
        pass


def do_password_login(sb):
    log(f"开始邮箱密码登录流程，打开登录页: {SIGN_IN_URL}")
    sb.uc_open_with_reconnect(SIGN_IN_URL, reconnect_time=6)

    log("登入页面等待 3 秒...")
    sb.sleep(3)
    save_shot(sb, "open_signin")

    if page_has_servers(sb):
        log("检测到已处于登录状态")
        return

    log("等待登录表单渲染...")
    email_sel, pass_sel = None, None
    deadline = time.time() + 30
    while time.time() < deadline:
        email_sel = find_email_input(sb)
        pass_sel = find_password_input(sb)
        if email_sel or pass_sel:
            break
        sb.sleep(2)

    if not email_sel and not pass_sel:
        save_shot(sb, "error_no_login_form")
        raise RuntimeError(f"未能找到邮箱或密码输入框，当前页面: {sb.get_current_url()}")

    # 输入邮箱
    if email_sel:
        log(f"输入邮箱: {EMAIL}")
        sb.type(email_sel, EMAIL)
        sb.sleep(1)

    # 若尚未渲染密码框，点击 Continue 进入密码页
    if not pass_sel:
        save_shot(sb, "filled_email")
        log("点击 Continue 进入密码输入步...")
        click_continue(sb)
        sb.sleep(3)

        p_deadline = time.time() + 20
        while time.time() < p_deadline:
            pass_sel = find_password_input(sb)
            if pass_sel:
                break
            for pwd_opt in (
                '//button[contains(., "password") or contains(., "Password")]',
                '//a[contains(., "password") or contains(., "Password")]',
                '//button[contains(., "Use password")]',
            ):
                try:
                    if sb.is_element_visible(pwd_opt):
                        sb.click(pwd_opt)
                        sb.sleep(2)
                        break
                except Exception:
                    pass
            sb.sleep(2)

    if not pass_sel:
        check_login_page_errors(sb)
        save_shot(sb, "error_no_password")
        raise RuntimeError("未能加载到密码输入框，请查看截图")

    log(f"输入登录密码 (密码长度: {len(PASSWORD)} 位)...")
    sb.type(pass_sel, PASSWORD)
    sb.sleep(1)
    save_shot(sb, "filled_password")

    log("点击 Continue 提交登录...")
    click_continue(sb)
    sb.sleep(5)

    log("等待进入服务器面板...")
    for i in range(25):
        if page_has_servers(sb):
            log(f"登录成功，已进入服务器列表！当前网址: {sb.get_current_url()}")
            save_shot(sb, "logged_in")
            return
        if i % 5 == 0:
            check_login_page_errors(sb)
        sb.sleep(2)

    check_login_page_errors(sb)
    save_shot(sb, "error_login_timeout")
    raise RuntimeError(f"登录提交后未能进入服务器面板，当前停留在: {sb.get_current_url()}")


def do_login(sb):
    # 1. 首选 Cookie 登录
    if try_cookie_login(sb):
        return

    # 2. 回退到账号密码登录
    do_password_login(sb)


# ---------------- 出口 IP 探测 ----------------
def check_egress_ip(sb):
    """通过浏览器访问 ipify 查看实际出口 IP，验证代理是否生效"""
    try:
        sb.open("https://api.ipify.org?format=json")
        sb.sleep(2)
        txt = sb.get_text("body")
        data = json.loads(txt)
        log(f"🌐 浏览器实际出口 IP: {data.get('ip')}")
    except Exception as e:
        log(f"出口 IP 检测略过: {e}")


# ---------------- 业务与续期 ----------------
def parse_remaining(text):
    if not text:
        return None, ""
    ctx = text
    m_ctx = re.search(r"(remaining|expires|expiring)[\s\S]{0,200}", text, re.I)
    if m_ctx:
        ctx = m_ctx.group(0)
    for src in (ctx, text):
        m = re.search(r"(?:(\d+)\s*days?\s*)?(\d{1,3}):(\d{2}):(\d{2})", src)
        if m:
            days = int(m.group(1) or 0)
            return days * 1440 + int(m.group(2)) * 60 + int(m.group(3)), m.group(0)
        m = re.search(r"(?:(\d+)\s*days?\s*)?(?:(\d+)\s*(?:hours?|hrs?|h)\s*)?(?:(\d+)\s*(?:minutes?|mins?|m)\b)?", src, re.I)
        if m and (m.group(1) or m.group(2) or m.group(3)):
            days = int(m.group(1) or 0)
            hours = int(m.group(2) or 0)
            mins = int(m.group(3) or 0)
            if days or hours or mins:
                return days * 1440 + hours * 60 + mins, m.group(0)
    return None, ""


def get_remaining(sb):
    try:
        txt = sb.get_text("body")
    except Exception:
        return None, ""
    return parse_remaining(txt or "")


def open_server_overview(sb):
    target = SERVER_NAME or "myappas"
    ident = SERVER_ID or target

    try:
        if sb.is_element_visible('//button[contains(., "Only necessary")]'):
            sb.click('//button[contains(., "Only necessary")]')
            sb.sleep(1)
    except Exception:
        pass

    def _on_server_list():
        try:
            return sb.is_text_visible("All your servers") or sb.is_text_visible("SLOTS USED")
        except Exception:
            return False

    if not _on_server_list():
        try:
            sb.click('//a[normalize-space()="My Servers"]', timeout=10)
            sb.sleep(3)
        except Exception:
            pass

    clicked = False
    for sel in (f'//a[contains(@href, "/server") and contains(., "{ident}")]',
                f'//*[contains(text(), "{ident}")]/ancestor::a[1]'):
        try:
            if sb.is_element_visible(sel):
                sb.click(sel)
                clicked = True
                break
        except Exception:
            continue

    if not clicked:
        try:
            manage_btns = sb.find_elements('//*[contains(., "Manage")][self::button or self::a]', by="xpath")
            if len(manage_btns) == 1:
                manage_btns[0].click()
                clicked = True
        except Exception:
            pass

    if not clicked:
        save_shot(sb, "error_no_server_card")
        raise RuntimeError(f"未找到服务器卡片: {target}")

    sb.sleep(5)

    def _on_overview_tab():
        try:
            if "/dashboard/main/" in (sb.get_current_url() or ""):
                return True
        except Exception:
            pass
        return sb.is_text_visible("SERVER EXPIRING") or sb.is_text_visible("SERVER EXPIRED")

    if not _on_overview_tab():
        try:
            if sb.is_element_visible('//a[contains(., "Manage")] | //button[contains(., "Manage")]'):
                sb.click('//a[contains(., "Manage")] | //button[contains(., "Manage")]')
        except Exception:
            pass
        sb.sleep(3)

    for _ in range(3):
        if _on_overview_tab():
            break
        try:
            if sb.is_element_visible('//a[normalize-space()="Overview"]'):
                sb.click('//a[normalize-space()="Overview"]')
        except Exception:
            pass
        sb.sleep(3)

    save_shot(sb, "overview")


def close_extra_windows(sb, keep):
    try:
        for h in list(sb.driver.window_handles):
            if h not in keep:
                try:
                    sb.driver.switch_to.window(h)
                    sb.driver.close()
                except Exception:
                    pass
    except Exception:
        pass


def focus_window(sb, handle):
    try:
        if handle in sb.driver.window_handles:
            sb.driver.switch_to.window(handle)
            return True
    except Exception:
        pass
    return False


def _norm_btn_text(t):
    if t is None:
        return ""
    t = str(t).replace("\u00a0", " ").replace("\u2026", "...").replace("\u2025", "...")
    return " ".join(t.split())


def _click_overlay_button(sb, flow_handle, main_handle, text, tag):
    if not focus_window(sb, flow_handle):
        return False
    clicked = False
    try:
        sel = f'//button[normalize-space()="{text}"]'
        if sb.is_element_visible(sel):
            sb.click(sel)
            clicked = True
    except Exception:
        pass

    if not clicked:
        try:
            lit = json.dumps(_norm_btn_text(text))
            js = (
                "function norm(s){ return (s||'').replace(/\\u00a0/g,' ')"
                ".replace(/\\u2026/g,'...').replace(/\\u2025/g,'...')"
                ".replace(/\\s+/g,' ').trim(); }"
                "var target = " + lit + ";"
                "var els = document.querySelectorAll('button');"
                "for (var i = 0; i < els.length; i++) {"
                "  if (norm(els[i].innerText || els[i].textContent) === target) {"
                "    els[i].scrollIntoView({block: 'center'});"
                "    els[i].click(); return 'clicked-button';"
                "  }"
                "}"
                "return 'not-found';"
            )
            res = sb.execute_script(js)
            clicked = (res == "clicked-button")
        except Exception:
            pass

    sb.sleep(5)
    close_extra_windows(sb, {flow_handle, main_handle})
    focus_window(sb, flow_handle)
    return clicked


def _scroll_to_bottom(sb):
    try:
        y = 0
        for _ in range(40):
            h = sb.execute_script("return document.body.scrollHeight") or 0
            if y >= h:
                break
            sb.execute_script(f"window.scrollTo(0, {y})")
            y += 500
            sb.sleep(0.5)
        sb.execute_script("window.scrollTo(0, document.body.scrollHeight)")
        sb.sleep(2)
    except Exception:
        pass


def _wait_text(sb, text, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if sb.is_text_visible(text):
                return True
        except Exception:
            pass
        sb.sleep(2)
    return False


def _js_click_text_atomic(sb, text):
    lit = json.dumps(text)
    js = (
        "var norm = function(s){ return (s || '').replace(/\\u00a0/g, ' ')"
        "  .replace(/\\u2026|\\u2025/g, '...').replace(/\\s+/g, ' ').trim(); };"
        "var target = " + lit + ";"
        "function visible(el){ var r = el.getBoundingClientRect();"
        "  return r.width > 0 && r.height > 0; }"
        "var btns = document.querySelectorAll('button, a');"
        "for (var i = 0; i < btns.length; i++) {"
        "  if (norm(btns[i].innerText || btns[i].textContent) === target"
        "      && visible(btns[i]) && !btns[i].disabled) {"
        "    btns[i].click(); return 'clicked-button'; }"
        "}"
        "return 'not-found';"
    )
    try:
        return sb.execute_script(js) or "not-found"
    except Exception as e:
        return f"异常: {e}"


def pass_turnstile(sb, flow_handle, main_handle, tag, timeout=60):
    if not focus_window(sb, flow_handle):
        return False
    try:
        sb.uc_gui_click_captcha()
    except Exception:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        sb.sleep(5)
        close_extra_windows(sb, {flow_handle, main_handle})
        focus_window(sb, flow_handle)
        try:
            if not sb.is_text_visible("Please check the captcha"):
                return True
        except Exception:
            pass
    return True


def walk_shrinkme(sb, flow_handle, main_handle, tag):
    focus_window(sb, flow_handle)
    sb.sleep(5)
    save_shot(sb, f"{tag}_shrinkme_open")

    def on_hokas():
        try:
            return "hokas.net" in (sb.get_current_url() or "")
        except Exception:
            return False

    def settle_popups():
        close_extra_windows(sb, {flow_handle, main_handle})
        focus_window(sb, flow_handle)

    if not pass_turnstile(sb, flow_handle, main_handle, tag):
        return False

    deadline = time.time() + 60
    while time.time() < deadline:
        if sb.is_text_visible("成功") or sb.is_text_visible("Success"):
            break
        sb.sleep(3)
        settle_popups()

    _click_overlay_button(sb, flow_handle, main_handle, "Click here to continue", f"{tag}_step1_continue")
    if on_hokas():
        return True

    _wait_text(sb, "STEP 1 OF 2", timeout=25)
    _click_overlay_button(sb, flow_handle, main_handle, "CLICK TO VERIFY", f"{tag}_step2_verify")
    if on_hokas():
        return True

    _wait_text(sb, "click Continue", timeout=25)
    _scroll_to_bottom(sb)
    if not _click_overlay_button(sb, flow_handle, main_handle, "Continue...", f"{tag}_step2_continue"):
        _click_overlay_button(sb, flow_handle, main_handle, "CONTINUE NEXT", f"{tag}_step2_continue_next")
    if on_hokas():
        return True

    _wait_text(sb, "STEP 2 OF 2", timeout=25)
    _scroll_to_bottom(sb)
    if not _click_overlay_button(sb, flow_handle, main_handle, "Continue...", f"{tag}_step3_continue"):
        _click_overlay_button(sb, flow_handle, main_handle, "GENERATE LINK", f"{tag}_step3_generate")
    if on_hokas():
        return True

    _wait_text(sb, "CONTINUE TO YOUR LINK", timeout=25)
    _click_overlay_button(sb, flow_handle, main_handle, "CONTINUE TO YOUR LINK", f"{tag}_step3_to_link")
    if on_hokas():
        return True

    settle_popups()
    _wait_text(sb, "YOUR LINK IS ALMOST READY", timeout=30)

    _gl_deadline = time.time() + 90
    while time.time() < _gl_deadline:
        settle_popups()
        if on_hokas():
            return True
        focus_window(sb, flow_handle)
        _gl = _js_click_text_atomic(sb, "Get Link")
        if isinstance(_gl, str) and _gl.startswith("clicked"):
            sb.sleep(5)
            for h in list(sb.driver.window_handles):
                try:
                    sb.driver.switch_to.window(h)
                    if "hokas.net" in sb.get_current_url():
                        return True
                except Exception:
                    continue
        sb.sleep(5)
    return False


def find_add_button(sb, hours):
    xpath = f'//button[contains(., "Add") and contains(., "{hours}")] | //a[contains(., "Add") and contains(., "{hours}")]'
    for _ in range(3):
        try:
            els = [e for e in sb.find_elements(xpath, by="xpath") if e.is_displayed()]
            if els:
                return xpath, els[0]
        except Exception:
            pass
        sb.sleep(2)
    return xpath, None


def run_add_flow(sb, main_handle, hours, before_minutes):
    tag = f"add{hours}h"
    try:
        if sb.is_text_visible(f"{hours}h option used"):
            log(f"[Add {hours} Hours] 面板显示已冷却，跳过")
            return "unavailable"
    except Exception:
        pass

    _, el = find_add_button(sb, hours)
    if el is None:
        return "unavailable"

    try:
        sb.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        sb.sleep(1)
        el.click()
    except Exception:
        return "walk_failed"

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
                        pass
            if flow_handle:
                break

    if not flow_handle:
        return "walk_failed"

    walked = walk_shrinkme(sb, flow_handle, main_handle, tag)
    close_extra_windows(sb, {main_handle})
    focus_window(sb, main_handle)
    try:
        sb.open(PANEL_URL)
        sb.sleep(3)
        open_server_overview(sb)
    except Exception:
        pass

    after_minutes, after_raw = get_remaining(sb)
    if after_minutes is None:
        return "not_credited" if walked else "walk_failed"

    gained = after_minutes - before_minutes
    if gained >= hours * 60 * 0.8:
        log(f"[Add {hours} Hours] 到账增加约 {gained} 分钟")
        return "credited"
    return "not_credited" if walked else "walk_failed"


def ensure_started(sb):
    try:
        remaining, _ = get_remaining(sb)
        if remaining is not None and remaining <= 0:
            return "no_time"
    except Exception:
        pass

    js = """
    var btns = document.querySelectorAll('button, a');
    for (var i = 0; i < btns.length; i++) {
        var t = (btns[i].innerText || '').trim();
        if (t.indexOf('Start') !== -1 && t.indexOf('Restart') === -1 && !btns[i].disabled) {
            btns[i].click();
            return 'clicked';
        }
    }
    return 'not-found';
    """
    for _ in range(20):
        try:
            if sb.execute_script(js) == 'clicked':
                return "started"
        except Exception:
            pass
        sb.sleep(2)
    return "no_button"


# ---------------- 主入口 ----------------
def main():
    if not EMAIL and not load_cookies_data():
        log("错误: 必须配置 EMAIL 或 COOKIES")
        sys.exit(1)

    result_lines = ["🖥️ <b>Hokas 自动续期报告</b>", f"账号: {EMAIL or 'Cookie 模式'}"]
    credited_any = False

    sb_kwargs = dict(uc=True, test=True, locale_code="en", headless=False, xvfb=True)
    log(f"浏览器代理状态: {proxy_label()}")

    if PROXY:
        scheme = PROXY.split("://", 1)[0].lower() if "://" in PROXY else ""
        if scheme in ("vless", "hysteria2", "hy2", "trojan", "ss"):
            if PROXY_CORE == "sing-box":
                sb_kwargs["proxy"] = start_singbox_proxy(PROXY)
            else:
                sb_kwargs["proxy"] = start_xray_proxy(PROXY)
        elif scheme in ("http", "socks5"):
            sb_kwargs["proxy"] = PROXY
        else:
            log(f"未识别代理协议，直接传递代理: {scheme}")
            sb_kwargs["proxy"] = PROXY

    try:
        with SB(**sb_kwargs) as sb:
            check_egress_ip(sb)
            do_login(sb)
            open_server_overview(sb)
            main_handle = sb.driver.current_window_handle

            before, before_raw = get_remaining(sb)
            if before is None:
                raise RuntimeError("未能解析到剩余时间")
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
                    result_lines.append(f"⏳ Add {hours} Hours: 冷却中或不可用")
                else:
                    result_lines.append(f"❌ Add {hours} Hours: 失败/未到账")

            final, final_raw = get_remaining(sb)
            if final is not None:
                result_lines.append(f"🕒 续期后剩余: {final_raw or str(final) + ' 分钟'}")

            try:
                start_status = ensure_started(sb)
                result_lines.append(f"▶️ 服务器启动状态: {start_status}")
            except Exception:
                pass

        result_lines.append(f"完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        send_tg("\n".join(result_lines))
        _print_proxy_tail("/tmp/proxy-error.log")
        if not credited_any:
            sys.exit(2)
    except Exception as e:
        log(f"执行失败: {e}")
        _print_proxy_tail("/tmp/proxy-error.log")
        traceback.print_exc()
        result_lines.append(f"❌ 续期失败: {e}")
        send_tg("\n".join(result_lines))
        sys.exit(1)


if __name__ == "__main__":
    main()

"""
AstrBot 二维码卡片（QR Card）

指令（默认唤醒前缀 /）：
- /qr help                                查看全部生成模式
- /qr <内容> [txt]                        纯文本二维码
- /qr <网址> page                         网页二维码（自动补 https://）
- /qr wifi <名称> [密码] [hidden]       WiFi 二维码，扫码即连；不填密码为开放网络
- /qr tel <号码>                          拨打电话二维码
- /qr sms <号码> [短信内容]               发短信二维码
- /qr mail <邮箱> [主题] [正文]           发邮件二维码
- /qr card <姓名> <电话> [邮箱] [单位]    名片二维码
- /qr geo <纬度> <经度>                   位置二维码
- /unqr                                   引用含二维码图片的消息，解析出内容
"""

from __future__ import annotations

import os
import re
import tempfile
import time
import urllib.parse
import uuid
from typing import Any, List, Optional, Tuple

import qrcode
from qrcode.constants import ERROR_CORRECT_M

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as CompImage, Plain as CompPlain, Reply as CompReply
from astrbot.api.star import Context, Star, register
import subprocess
import sys
import asyncio

QR_NAMES = ("qr", "qrcode", "二维码")
UNQR_NAMES = ("unqr", "解二维码", "识别二维码")

# --------------------------- /qr 子命令（模式）---------------------------
# 写在内容末尾的关键字 -> 标准模式名
MODE_ALIASES = {
    "help": "help", "?": "help", "？": "help", "帮助": "help", "菜单": "help", "用法": "help",
    "txt": "txt", "text": "txt", "文本": "txt", "文字": "txt",
    "page": "page", "url": "page", "link": "page", "网页": "page", "链接": "page", "网址": "page", "网站": "page",
    "wifi": "wifi", "wi-fi": "wifi", "无线": "wifi", "热点": "wifi",
    "tel": "tel", "phone": "tel", "call": "tel", "电话": "tel", "拨号": "tel",
    "sms": "sms", "短信": "sms",
    "mail": "mail", "email": "mail", "邮箱": "mail", "邮件": "mail",
    "card": "card", "vcard": "card", "mecard": "card", "名片": "card", "联系人": "card",
    "geo": "geo", "location": "geo", "位置": "geo", "坐标": "geo",
}

HELP_TEXT = (
    "📖 /qr 二维码生成 · 用法\n"
    "━━━━━━━━━━━━━━━━\n"
    "/qr <内容>                  纯文本二维码（默认）\n"
    "/qr <内容> txt              显式文本模式\n"
    "/qr <网址> page             网页二维码，自动补 https://\n"
    "/qr wifi <名称> [密码] [hidden]\n"
    "                            WiFi 二维码，扫码即可连接；\n"
    "                            不填密码→开放网络，末尾加 hidden→隐藏 SSID\n"
    "/qr tel <电话号码>          拨打电话二维码\n"
    "/qr sms <号码> [短信内容]   发短信二维码\n"
    "/qr mail <邮箱> [主题] [正文]\n"
    "                            发邮件二维码\n"
    "/qr card <姓名> <电话> [邮箱] [单位]\n"
    "                            名片二维码\n"
    "/qr geo <纬度> <经度>       位置二维码\n"
    "━━━━━━━━━━━━━━━━\n"
    "· 名称/密码等含空格时用引号包住：/qr wifi \"My WiFi\" \"pass word\"\n"
    "· 内容本身以模式词结尾时，末尾再加 txt 强制按文本生成\n"
    "· 解析二维码：引用一张二维码图片发送 /unqr\n"
    "WiFi 示例：\n"
    "  /qr wifi 我家WiFi 12345678         ← 有密码，不隐藏\n"
    "  /qr wifi 访客网络                   ← 开放网络，不隐藏\n"
    "  /qr wifi 访客网络 hidden            ← 开放网络，隐藏 SSID\n"
    "  /qr wifi 公司WiFi 88888888 hidden   ← 有密码，隐藏 SSID\n"
    "  /qr wifi \"My WiFi\" \"pass word\"   ← 名称/密码含空格用引号"
)


# --------------------------- 二维码解码 ---------------------------

def _decode_with_zxing(path: str) -> List[str]:
    """zxing-cpp：纯 pip 安装、识别率高，主解码器。"""
    import zxingcpp
    from PIL import Image as PILImage

    out: List[str] = []
    with PILImage.open(path) as img:
        try:
            results = zxingcpp.read_barcodes(img)
        except Exception:
            results = []
        for r in results:
            text = str(getattr(r, "text", "") or "").strip()
            if text and text not in out:
                out.append(text)
    return out


def _decode_with_pyzbar(path: str) -> List[str]:
    """pyzbar（可选）：Windows 自带 zbar 库；Linux 需要系统安装 libzbar0。"""
    from pyzbar.pyzbar import decode as zbar_decode
    from PIL import Image as PILImage

    out: List[str] = []
    with PILImage.open(path) as img:
        for item in zbar_decode(img):
            data = getattr(item, "data", b"")
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            text = str(data).strip()
            if text and text not in out:
                out.append(text)
    return out


def _decode_with_cv2(path: str) -> List[str]:
    """OpenCV（可选兜底），用 PIL 加载避免中文路径问题。"""
    import cv2
    import numpy as np
    from PIL import Image as PILImage

    out: List[str] = []
    try:
        with PILImage.open(path) as pil:
            img = cv2.cvtColor(np.array(pil.convert("RGB")), cv2.COLOR_RGB2BGR)
    except Exception:
        return out
    detector = cv2.QRCodeDetector()
    try:
        ok, texts, _, _ = detector.detectAndDecodeMulti(img)
        if ok:
            for t in texts:
                t = str(t or "").strip()
                if t and t not in out:
                    out.append(t)
    except Exception:
        pass
    if not out:
        try:
            data, _, _ = detector.detectAndDecode(img)
            data = str(data or "").strip()
            if data:
                out.append(data)
        except Exception:
            pass
    if not out:
        # 图片太小时放大重试一次
        h, w = img.shape[:2]
        m = max(h, w)
        if 0 < m < 500:
            scale = 500.0 / m
            big = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            try:
                data, _, _ = detector.detectAndDecode(big)
                data = str(data or "").strip()
                if data:
                    out.append(data)
            except Exception:
                pass
    return out


def decode_qr_file(path: str) -> List[str]:
    """依次尝试可用的解码器，返回识别出的全部文本。"""
    for fn in (_decode_with_zxing, _decode_with_pyzbar, _decode_with_cv2):
        try:
            res = fn(path)
        except ImportError:
            continue
        except Exception as e:
            logger.debug(f"[QR] 解码器 {fn.__name__} 异常: {e}")
            continue
        if res:
            return res
    return []


# --------------------------- 插件主体 ---------------------------

@register(
    "二维码卡片",
    "linker9527",
    "/qr 多模式生成二维码（文本/网页/WiFi/电话/短信/邮件/名片/位置）；引用图片发 /unqr 解析",
    "1.0.0",
)
class QrCardPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config = config or {}
        # 会话 -> [(时间戳, 图片组件), ...]，每个会话最多保留 5 张
        self._recent_images: dict = {}
        logger.info("[QR] 二维码卡片已加载：/qr help 查看用法，/unqr 解析二维码")

    async def initialize(self):
        """激活全部 LLM 工具，让 AI 能通过自然语言生成/解析二维码。"""
        tools = (
            "qr_text", "qr_url", "qr_wifi", "qr_phone", "qr_sms",
            "qr_email", "qr_card", "qr_location", "qr_decode",
        )
        for name in tools:
            try:
                self.context.activate_llm_tool(name)
                logger.info(f"[QR] LLM 工具 {name} 激活成功")
            except Exception as e:
                logger.warning(f"[QR] LLM 工具 {name} 激活失败（不影响指令使用）: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1)
    async def _cache_recent_images(self, event: AstrMessageEvent):
        """缓存每个会话最近收到的图片。

        QQ 官方平台的引用消息不返回被引用消息内容（平台限制），
        所以 /unqr 取不到引用图片时，回退到本会话最近收到的图片。
        """
        try:
            for comp in event.get_messages():
                if isinstance(comp, CompImage):
                    key = event.unified_msg_origin
                    lst = self._recent_images.setdefault(key, [])
                    lst.append((time.time(), comp))
                    # 只保留最近 5 张
                    while len(lst) > 5:
                        lst.pop(0)
                    break
        except Exception:
            pass

    def _collect_reply_images(self, event: AstrMessageEvent) -> List[CompImage]:
        """从消息链的 Reply 组件里取被引用消息的图片。"""
        images: List[CompImage] = []
        try:
            for comp in event.get_messages():
                if isinstance(comp, CompReply):
                    for c in (getattr(comp, "chain", None) or []):
                        if isinstance(c, CompImage):
                            images.append(c)
        except Exception:
            pass
        return images

    def _collect_reply_text(self, event: AstrMessageEvent) -> str:
        """从消息链的 Reply 组件里取被引用消息的文字。"""
        try:
            for comp in event.get_messages():
                if isinstance(comp, CompReply):
                    text = (getattr(comp, "message_str", "") or "").strip()
                    if text:
                        return text
                    parts = [
                        c.text
                        for c in (getattr(comp, "chain", None) or [])
                        if isinstance(c, CompPlain) and getattr(c, "text", "")
                    ]
                    text = "".join(parts).strip()
                    if text:
                        return text
        except Exception:
            pass
        return ""

    def _get_recent_image(self, event: AstrMessageEvent) -> Optional[CompImage]:
        """取本会话最近收到的图片（10 分钟内有效）。"""
        lst = self._recent_images.get(event.unified_msg_origin)
        if not lst:
            return None
        now = time.time()
        # 清理过期项（10 分钟）
        fresh = [(t, c) for t, c in lst if now - t < 600]
        if fresh != lst:
            self._recent_images[event.unified_msg_origin] = fresh
            lst = fresh
        return lst[-1][1] if lst else None

    # ---------- 配置 ----------
    def _int_cfg(self, key: str, default: int, lo: int, hi: int) -> int:
        try:
            v = int(self.config.get(key, default))
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    def _str_cfg(self, key: str, default: str) -> str:
        v = self.config.get(key, default)
        return default if v in (None, "") else str(v)

    # ---------- 临时文件 ----------
    def _new_temp_path(self) -> str:
        d = os.path.join(tempfile.gettempdir(), "astrbot_plugin_qr_card")
        try:
            os.makedirs(d, exist_ok=True)
            now = time.time()
            for name in os.listdir(d):
                p = os.path.join(d, name)
                try:
                    if now - os.path.getmtime(p) > 3600:
                        os.remove(p)
                except Exception:
                    pass
        except Exception:
            d = tempfile.gettempdir()
        return os.path.join(d, f"qr_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.png")

    # ---------- 文本工具 ----------
    @staticmethod
    def _strip_wake(s: str) -> str:
        s = (s or "").strip()
        while s and s[0] in "/！!／":
            s = s[1:].lstrip()
        return s

    @staticmethod
    def _extract_arg(raw: str, names: tuple) -> str:
        """取出指令名后面的参数文本（兼容 message_str 带不带唤醒前缀）。"""
        s = QrCardPlugin._strip_wake(raw)
        for name in names:
            if s == name:
                return ""
            if s.startswith(name):
                rest = s[len(name):]
                if rest and rest[0] in (" ", "\t", "\n", "\u3000"):
                    return rest.strip()
        return ""

    @staticmethod
    def _tokenize(s: str) -> List[str]:
        """按空白分词，支持成对引号包裹含空格的参数。"""
        tokens: List[str] = []
        cur: List[str] = []
        quote = ""
        for ch in s or "":
            if quote:
                if ch == quote:
                    quote = ""
                else:
                    cur.append(ch)
            elif ch in "\"'\u201c\u201d\u2018\u2019":
                quote = ch
            elif ch.isspace():
                if cur:
                    tokens.append("".join(cur))
                    cur = []
            else:
                cur.append(ch)
        if cur:
            tokens.append("".join(cur))
        return tokens

    @staticmethod
    def _wifi_escape(s: str) -> str:
        """WIFI: 协议中需转义的字符： \\ ; , : \" """
        s = (s or "").replace("\\", "\\\\")
        for ch in (";", ",", ":", '"'):
            s = s.replace(ch, "\\" + ch)
        return s

    @staticmethod
    def _reply_chain(reply: Any) -> list:
        """兼容多种 reply 结构：对象.chain / 对象.message / dict / list。"""
        if reply is None:
            return []
        if isinstance(reply, list):
            return reply
        if isinstance(reply, dict):
            for key in ("chain", "message", "messages", "content"):
                val = reply.get(key)
                if val:
                    return val if isinstance(val, list) else [val]
            return []
        chain = getattr(reply, "chain", None)
        if not chain:
            chain = getattr(reply, "message", None)
        return chain or []

    # ---------- 各模式 payload 构造 ----------
    def _build_payload(self, mode: str, text: str) -> Tuple[Optional[str], Optional[str]]:
        """按模式构造二维码内容。返回 (payload, 错误提示)，成功时错误提示为 None。"""
        text = (text or "").strip()

        if mode == "txt":
            if not text:
                return None, "用法：/qr <内容> txt，例如 /qr 123456 txt"
            return text, None

        if mode == "page":
            if not text:
                return None, "用法：/qr <网址> page，例如 /qr pan.baidu.com/s/xxxx page"
            if not re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://", text):
                text = "https://" + text
            return text, None

        if mode == "wifi":
            return self._build_wifi(text)

        if mode == "tel":
            num = re.sub(r"[\s\-()（）]", "", text)
            if not num:
                return None, "用法：/qr tel <电话号码>，例如 /qr tel 13800138000"
            return f"tel:{num}", None

        if mode == "sms":
            t = self._tokenize(text)
            if not t:
                return None, "用法：/qr sms <号码> [短信内容]，例如 /qr sms 10086 查话费"
            num = t[0]
            msg = " ".join(t[1:])
            return f"SMSTO:{num}:{msg}", None

        if mode == "mail":
            t = self._tokenize(text)
            if not t or "@" not in t[0]:
                return None, "用法：/qr mail <邮箱> [主题] [正文]，例如 /qr me@qq.com 你好 这是正文"
            addr = t[0]
            subject = t[1] if len(t) > 1 else ""
            body = " ".join(t[2:]) if len(t) > 2 else ""
            payload = f"mailto:{addr}"
            params = []
            if subject:
                params.append("subject=" + urllib.parse.quote(subject, safe=""))
            if body:
                params.append("body=" + urllib.parse.quote(body, safe=""))
            if params:
                payload += "?" + "&".join(params)
            return payload, None

        if mode == "card":
            t = self._tokenize(text)
            if len(t) < 2:
                return None, "用法：/qr card <姓名> <电话> [邮箱] [单位]，例如 /qr card 张三 13800138000"
            name, tel = t[0], t[1]
            email = t[2] if len(t) > 2 else ""
            org = " ".join(t[3:]) if len(t) > 3 else ""
            fields = [f"N:{name}", f"TEL:{tel}"]
            if email:
                fields.append(f"EMAIL:{email}")
            if org:
                fields.append(f"ORG:{org}")
            return "MECARD:" + ";".join(fields) + ";;", None

        if mode == "geo":
            t = self._tokenize(text)
            if len(t) < 2:
                return None, "用法：/qr geo <纬度> <经度>，例如 /qr geo 39.9042 116.4074"
            try:
                float(t[0])
                float(t[1])
            except ValueError:
                return None, "纬度/经度必须是数字，例如 /qr geo 39.9042 116.4074"
            return f"geo:{t[0]},{t[1]}", None

        return None, f"未知模式：{mode}"

    def _build_wifi(self, text: str) -> Tuple[Optional[str], Optional[str]]:
        """<名称> [密码] [hidden]：填了密码按 WPA 处理，不填即为开放网络。"""
        tokens = self._tokenize(text)
        if not tokens:
            return None, (
                "用法：/qr wifi <名称> [密码] [hidden]\n"
                "例如：/qr wifi 我家WiFi 12345678\n"
                "或：/qr wifi 访客网络（不填密码即开放网络）\n"
                "或：/qr wifi \"My WiFi\" \"pass word\" hidden"
            )
        ssid = tokens[0]
        hidden = False
        pw_parts: List[str] = []
        for tok in tokens[1:]:
            if tok.lower() in ("hidden", "hide", "隐藏"):
                hidden = True
            else:
                pw_parts.append(tok)
        password = " ".join(pw_parts)
        seg = ["WIFI:"]
        if password:
            seg.append("T:WPA;")
        seg.append(f"S:{self._wifi_escape(ssid)};")
        if password:
            seg.append(f"P:{self._wifi_escape(password)};")
        if hidden:
            seg.append("H:true;")
        seg.append(";")
        return "".join(seg), None

    # ---------- 生成二维码 ----------
    def _make_qr_image(self, text: str) -> Optional[str]:
        box_size = self._int_cfg("box_size", 10, 2, 30)
        border = self._int_cfg("border", 4, 0, 10)
        fill = self._str_cfg("fill_color", "#000000")
        back = self._str_cfg("back_color", "#ffffff")
        try:
            qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=box_size, border=border)
            qr.add_data(text)
            qr.make(fit=True)
            img = qr.make_image(fill_color=fill, back_color=back)
        except Exception as e:
            logger.warning(f"[QR] 自定义样式生成失败，改用默认样式重试: {e}")
            try:
                qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=10, border=4)
                qr.add_data(text)
                qr.make(fit=True)
                img = qr.make_image(fill_color="black", back_color="white")
            except Exception as e2:
                logger.error(f"[QR] 生成二维码失败: {e2}")
                return None
        path = self._new_temp_path()
        try:
            img.save(path)
        except Exception as e:
            logger.error(f"[QR] 保存二维码图片失败: {e}")
            return None
        return path

    # ---------- 指令：/qr ----------
    @filter.command("qr", alias={"qrcode", "二维码"})
    async def qr_cmd(self, event: AstrMessageEvent):
        """二维码生成：/qr help 查看全部模式"""
        arg_str = self._extract_arg(event.message_str, QR_NAMES)

        # 没带参数时，尝试取被引用消息里的文字（引用消息是消息链里的 Reply 组件）
        if not arg_str:
            arg_str = self._collect_reply_text(event)

        tokens = self._tokenize(arg_str)
        mode = MODE_ALIASES.get(tokens[-1].lower()) if tokens else None

        if mode == "help" or not tokens:
            yield event.plain_result(HELP_TEXT)
            yield event.plain_result(
                "📖 /unqr 二维码解析 · 用法\n"
                "━━━━━━━━━━━━━━━━\n"
                "· 引用一条含二维码图片的消息发送 /unqr，即可解析内容\n"
                "· 也支持直接发送「图片 + unqr」\n"
                "· 别名：解二维码、识别二维码\n"
                "━━━━━━━━━━━━━━━━\n"
                "· 如果识别失败或需要安装备用识别器（2个，更准，约60MB），发送 /unqr download 一键安装\n"
            )
            # 支持作者信息
            image_path = os.path.join(os.path.dirname(__file__), "赞r.png")
            if os.path.exists(image_path):
                yield event.chain_result([CompPlain("支持作者↑↑↑"), CompImage.fromFileSystem(image_path)])
            else:
                yield event.plain_result("支持作者↑↑↑")
            return

        if mode is None:
            # 默认：整段当作纯文本
            payload, err = arg_str.strip(), None
        else:
            # 去掉末尾的模式关键字后交给对应模式处理
            body = arg_str.rstrip()
            idx = body.rfind(tokens[-1])
            body = body[:idx].strip() if idx >= 0 else ""
            payload, err = self._build_payload(mode, body)

        if err:
            yield event.plain_result(err)
            return

        path = self._make_qr_image(payload)
        if path is None:
            yield event.plain_result("生成失败：内容可能过长或包含无法编码的字符")
            return
        yield event.chain_result([CompImage.fromFileSystem(path)])

    # ---------- 指令：/unqr ----------
    @filter.command("unqr_download", alias={"下载解码器", "安装解码器"})
    async def unqr_download(self, event: AstrMessageEvent):
        """安装二维码解码依赖。"""
        try:
            yield event.plain_result("开始安装二维码解码依赖，可能需要一两分钟…")
        except Exception:
            pass
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                 "qrcode[pil]", "zxing-cpp", "pyzbar", "opencv-python-headless"],
                capture_output=True, text=True, timeout=600,
            )
            if proc.returncode == 0:
                msg = "✅ 解码依赖安装完成。请重启插件或 AstrBot 后使用 /unqr。"
            else:
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
                msg = "❌ 安装失败：\n" + "\n".join(tail)
            yield event.plain_result(msg)
        except Exception as e:
            yield event.plain_result(f"❌ 安装过程出错：{e}")

    @filter.command("unqr", alias={"解二维码", "识别二维码"})
    async def unqr_cmd(self, event: AstrMessageEvent):
        """解析二维码：引用一条含二维码图片的消息发送 /unqr"""
        # /unqr download：安装解码依赖
        arg = (self._extract_arg(event.message_str, UNQR_NAMES) or "").strip().lower()
        if arg in ("download", "下载", "安装", "安装解码器", "下载解码器"):
            yield event.plain_result("开始安装二维码解码依赖，可能需要一两分钟…")
            try:
                proc = await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "qrcode[pil]", "zxing-cpp", "pyzbar", "opencv-python-headless"],
                    capture_output=True, text=True, timeout=600,
                )
                if proc.returncode == 0:
                    yield event.plain_result("✅ 解码依赖安装完成。请重启插件或 AstrBot 后使用 /unqr。")
                else:
                    tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
                    yield event.plain_result("❌ 安装失败：\n" + "\n".join(tail))
            except Exception as e:
                yield event.plain_result(f"❌ 安装过程出错：{e}")
            return
        images: List[CompImage] = self._collect_reply_images(event)
        if not images:
            # 兜底：图片和 unqr 同一条消息发送
            try:
                images = [c for c in event.get_messages() if isinstance(c, CompImage)]
            except Exception:
                images = []
        if not images:
            # QQ 官方平台引用消息不返回内容（平台限制），回退到本会话最近收到的图片
            recent = self._get_recent_image(event)
            if recent is not None:
                images = [recent]
                logger.info("[QR] /unqr 使用会话最近收到的图片解码")
        if not images:
            yield event.plain_result(
                "没有找到二维码图片。\n"
                "QQ 官方平台无法读取引用消息的图片，请直接把二维码图片发出来，然后发 /unqr"
            )
            return

        prefix = self._str_cfg("result_prefix", "二维码内容：")
        all_texts: List[str] = []
        for comp in images:
            try:
                path = await comp.convert_to_file_path()
            except Exception as e:
                logger.warning(f"[QR] 获取图片失败: {e}")
                continue
            if not path or not os.path.exists(path):
                continue
            for t in decode_qr_file(path):
                if t not in all_texts:
                    all_texts.append(t)

        if not all_texts:
            if not _has_any_decoder():
                yield event.plain_result("识别失败，使用 /qr help 获取帮助")
            else:
                yield event.plain_result("没有从图片里识别出二维码，请确认图片清晰且包含二维码")
            return
        if len(all_texts) == 1:
            yield event.plain_result(f"{prefix}{all_texts[0]}")
        else:
            lines = [f"{prefix}（共 {len(all_texts)} 条）"]
            lines.extend(f"{i}. {t}" for i, t in enumerate(all_texts, 1))
            yield event.plain_result("\n".join(lines))

    # ---------- LLM 工具：自然语言生成/解析二维码 ----------

    async def _llm_send_qr(self, event: AstrMessageEvent, payload: str, desc: str) -> str:
        """生成二维码图片并发送，返回给 LLM 的结果文本。"""
        path = self._make_qr_image(payload)
        if path is None:
            return "生成二维码失败：内容可能过长或包含无法编码的字符"
        try:
            await event.send(event.chain_result([CompImage.fromFileSystem(path)]))
        except Exception as e:
            logger.warning(f"[QR] LLM 工具发送图片失败: {e}")
            return f"二维码已生成但发送失败：{e}"
        return f"已生成{desc}二维码并发送给用户。"

    @filter.llm_tool(name="qr_text")
    async def qr_text(self, event: AstrMessageEvent, text: str) -> str:
        """生成一个纯文本二维码图片并发送给用户。当用户想把任意文字变成二维码时调用。

        Args:
            text(string): 要编码进二维码的文本内容
        """
        try:
            return await self._llm_send_qr(event, text, "文本")
        except Exception as e:
            logger.error(f"[QR] LLM 工具异常: {e}")
            return f"生成二维码时发生错误：{e}"

    @filter.llm_tool(name="qr_url")
    async def qr_url(self, event: AstrMessageEvent, url: str) -> str:
        """生成一个网页链接二维码图片并发送给用户，扫码后可直接打开网页。当用户想把网址变成二维码时调用。

        Args:
            url(string): 网址，可以不带 https:// 前缀（会自动补全）
        """
        try:
            if not re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://", url):
                url = "https://" + url
            return await self._llm_send_qr(event, url, "网页链接")
        except Exception as e:
            logger.error(f"[QR] LLM 工具异常: {e}")
            return f"生成二维码时发生错误：{e}"

    @filter.llm_tool(name="qr_wifi")
    async def qr_wifi(
        self,
        event: AstrMessageEvent,
        ssid: str,
        password: str = "",
        hidden: bool = False,
    ) -> str:
        """生成一个 WiFi 连接二维码图片并发送给用户，手机扫码即可直接连接 WiFi。当用户提供 WiFi 名称和密码要求生成二维码时调用。

        Args:
            ssid(string): WiFi 名称（SSID）
            password(string): WiFi 密码，留空表示开放网络（无密码）
            hidden(boolean): WiFi 是否隐藏 SSID，默认 false
        """
        try:
            seg = ["WIFI:"]
            if password:
                seg.append("T:WPA;")
            seg.append(f"S:{self._wifi_escape(ssid)};")
            if password:
                seg.append(f"P:{self._wifi_escape(password)};")
            if hidden:
                seg.append("H:true;")
            seg.append(";")
            return await self._llm_send_qr(event, "".join(seg), "WiFi")
        except Exception as e:
            logger.error(f"[QR] LLM 工具异常: {e}")
            return f"生成二维码时发生错误：{e}"

    @filter.llm_tool(name="qr_phone")
    async def qr_phone(self, event: AstrMessageEvent, phone: str) -> str:
        """生成一个拨打电话二维码图片并发送给用户，扫码后可直接拨号。当用户想把电话号码变成二维码时调用。

        Args:
            phone(string): 电话号码
        """
        try:
            num = re.sub(r"[\s\-()（）]", "", phone)
            if not num:
                return "生成二维码失败：电话号码不能为空"
            return await self._llm_send_qr(event, f"tel:{num}", "电话")
        except Exception as e:
            logger.error(f"[QR] LLM 工具异常: {e}")
            return f"生成二维码时发生错误：{e}"

    @filter.llm_tool(name="qr_sms")
    async def qr_sms(self, event: AstrMessageEvent, phone: str, message: str = "") -> str:
        """生成一个发送短信二维码图片并发送给用户，扫码后可直接发送短信。当用户想把手机号和短信内容变成二维码时调用。

        Args:
            phone(string): 手机号码
            message(string): 预填的短信内容，可选
        """
        try:
            return await self._llm_send_qr(event, f"SMSTO:{phone}:{message}", "短信")
        except Exception as e:
            logger.error(f"[QR] LLM 工具异常: {e}")
            return f"生成二维码时发生错误：{e}"

    @filter.llm_tool(name="qr_email")
    async def qr_email(
        self,
        event: AstrMessageEvent,
        email: str,
        subject: str = "",
        body: str = "",
    ) -> str:
        """生成一个发送邮件二维码图片并发送给用户，扫码后可直接写邮件。当用户想把邮箱地址变成二维码时调用。

        Args:
            email(string): 收件人邮箱地址
            subject(string): 邮件主题，可选
            body(string): 邮件正文，可选
        """
        try:
            payload = f"mailto:{email}"
            params = []
            if subject:
                params.append("subject=" + urllib.parse.quote(subject, safe=""))
            if body:
                params.append("body=" + urllib.parse.quote(body, safe=""))
            if params:
                payload += "?" + "&".join(params)
            return await self._llm_send_qr(event, payload, "邮件")
        except Exception as e:
            logger.error(f"[QR] LLM 工具异常: {e}")
            return f"生成二维码时发生错误：{e}"

    @filter.llm_tool(name="qr_card")
    async def qr_card(
        self,
        event: AstrMessageEvent,
        name: str,
        phone: str,
        email: str = "",
        org: str = "",
    ) -> str:
        """生成一个联系人名片二维码图片并发送给用户，扫码后可直接保存联系人。当用户想把联系人信息变成二维码时调用。

        Args:
            name(string): 联系人姓名
            phone(string): 联系人电话号码
            email(string): 联系人邮箱，可选
            org(string): 单位/公司名称，可选
        """
        try:
            fields = [f"N:{name}", f"TEL:{phone}"]
            if email:
                fields.append(f"EMAIL:{email}")
            if org:
                fields.append(f"ORG:{org}")
            return await self._llm_send_qr(event, "MECARD:" + ";".join(fields) + ";;", "名片")
        except Exception as e:
            logger.error(f"[QR] LLM 工具异常: {e}")
            return f"生成二维码时发生错误：{e}"

    @filter.llm_tool(name="qr_location")
    async def qr_location(self, event: AstrMessageEvent, latitude: str, longitude: str) -> str:
        """生成一个地理位置二维码图片并发送给用户，扫码后可在地图上打开该位置。当用户想把经纬度坐标变成二维码时调用。

        Args:
            latitude(string): 纬度，例如 39.9042
            longitude(string): 经度，例如 116.4074
        """
        try:
            float(latitude)
            float(longitude)
        except ValueError:
            return "生成二维码失败：纬度/经度必须是数字"
        try:
            return await self._llm_send_qr(event, f"geo:{latitude},{longitude}", "位置")
        except Exception as e:
            logger.error(f"[QR] LLM 工具异常: {e}")
            return f"生成二维码时发生错误：{e}"

    @filter.llm_tool(name="qr_decode")
    async def qr_decode(self, event: AstrMessageEvent) -> str:
        """解析用户引用消息中的二维码图片，返回二维码里编码的内容。当用户发来一张二维码图片并要求识别/解码/解析内容时调用。

        Args:
        """
        try:
            images: List[CompImage] = self._collect_reply_images(event)
            if not images:
                try:
                    images = [c for c in event.get_messages() if isinstance(c, CompImage)]
                except Exception:
                    images = []
            if not images:
                recent = self._get_recent_image(event)
                if recent is not None:
                    images = [recent]
                    logger.info("[QR] qr_decode 使用会话最近收到的图片解码")
            if not images:
                return (
                    "没有找到二维码图片。QQ 官方平台无法读取引用消息里的图片，"
                    "请让用户直接把二维码图片发到对话里，然后再要求解析。"
                )

            all_texts: List[str] = []
            for comp in images:
                try:
                    path = await comp.convert_to_file_path()
                except Exception as e:
                    logger.warning(f"[QR] 获取图片失败: {e}")
                    continue
                if not path or not os.path.exists(path):
                    continue
                for t in decode_qr_file(path):
                    if t not in all_texts:
                        all_texts.append(t)

            if not all_texts:
                if not _has_any_decoder():
                    return "识别失败，使用 /qr help 获取帮助"
                return "没有从图片里识别出二维码，请确认图片清晰且包含二维码。"
            if len(all_texts) == 1:
                return f"二维码内容：{all_texts[0]}"
            return "二维码内容（共 {} 条）：\n{}".format(
                len(all_texts),
                "\n".join(f"{i}. {t}" for i, t in enumerate(all_texts, 1)),
            )
        except Exception as e:
            logger.error(f"[QR] LLM 工具异常: {e}")
            return f"解析二维码时发生错误：{e}"

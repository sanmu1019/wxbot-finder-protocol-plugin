# -*- coding: utf-8 -*-
from __future__ import annotations

import html
import os
import re
import xml.etree.ElementTree as ET
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

import requests

from config.config import Config
from core.context import ContextType
from core.plugin_system import (
    Event,
    EventAction,
    EventContext,
    Plugin,
    register,
)
from core.wechat_api import WechatAPIClient
from utils.download_helper import download_video
from utils.logger import get_logger


logger = get_logger(__name__)


@register(
    name="FinderProtocolParser",
    desire_priority=96,
    hidden=False,
    desc="使用 bot protocol-core 直刷视频号媒体并发送视频",
    version="1.0.0",
    author="codex",
)
class FinderProtocolParser(Plugin):
    U64_MASK = (1 << 64) - 1
    XML_FIELD_RE = re.compile(
        r"<(?P<tag>[A-Za-z_][A-Za-z0-9_.:-]*)\b[^>/]*>"
        r"\s*(?P<value>[^<>]*?)\s*</(?P=tag)>",
        re.DOTALL,
    )
    XML_ATTR_RE = re.compile(
        r"<(?P<tag>[A-Za-z_][A-Za-z0-9_.:-]*)"
        r"\b(?P<attrs>[^>]*)>",
        re.DOTALL,
    )
    XML_ATTR_VALUE_RE = re.compile(
        r"(?P<name>[A-Za-z_][A-Za-z0-9_.:-]*)"
        r"\s*=\s*(['\"])(?P<value>.*?)\2",
        re.DOTALL,
    )
    FINDER_MARKERS = (
        "<finder",
        "finderliveproductshare",
        "findernamecard",
        "finderfeed",
        "finderusername",
        "objectid",
        "objectnonceid",
    )
    FINDER_MEDIA_HOSTS = {
        "wxapp.tc.qq.com",
        "finder.video.qq.com",
    }
    FINDER_VIDEO_PATH_MARKERS = (
        "/20302/stodownload",
        "/251/20302/stodownload",
    )

    def __init__(self):
        super().__init__()
        self.handlers[Event.ON_HANDLE_CONTEXT] = (
            self.on_handle_context
        )

        self.config = self.load_config() or {}
        self.enabled = bool(self.config.get("enabled", True))
        self.allow_group = bool(
            self.config.get("allow_group", True)
        )
        self.allow_private = bool(
            self.config.get("allow_private", True)
        )
        self.auto_detect = bool(
            self.config.get("auto_detect", False)
        )
        self.allow_direct_share = bool(
            self.config.get("allow_direct_share", False)
        )
        self.send_summary = bool(
            self.config.get("send_summary", True)
        )
        self.send_video = bool(
            self.config.get("send_video", True)
        )
        self.send_failure_text = bool(
            self.config.get("send_failure_text", True)
        )
        self.video_max_size_mb = max(
            10,
            int(self.config.get("video_max_size_mb", 300)),
        )
        self.protocol_base = str(
            self.config.get(
                "protocol_base",
                "http://127.0.0.1:9000/api",
            )
        ).strip().rstrip("/")
        self.protocol_timeout = max(
            3,
            int(self.config.get("protocol_timeout", 15)),
        )
        self.protocol_cgi = max(
            1,
            int(self.config.get("protocol_cgi", 3906)),
        )
        self.download_timeout = max(
            15,
            int(self.config.get("download_timeout", 45)),
        )
        self.manual_triggers = [
            str(item).strip()
            for item in self.config.get(
                "triggers",
                ["协议解析", "协议视频"],
            )
            if str(item).strip()
        ]

        self.cache_dir = os.path.join(
            str(Config.BASE_DIR),
            "tmp",
            "finder_protocol_parser",
        )
        os.makedirs(self.cache_dir, exist_ok=True)
        self.wechat_api = WechatAPIClient()

        logger.info(
            "[FinderProtocolParser] init "
            f"enabled={self.enabled}, "
            f"auto_detect={self.auto_detect}, "
            f"allow_direct_share={self.allow_direct_share}, "
            f"protocol_base={self.protocol_base}"
        )

    def on_handle_context(
        self,
        e_context: EventContext,
    ) -> None:
        if not self.enabled:
            return

        context = e_context.get("context")
        if not context:
            return

        is_group = bool(e_context.get("is_group", False))
        if is_group and not self.allow_group:
            return
        if not is_group and not self.allow_private:
            return

        content = str(e_context.get("content", "") or "")
        original_content = str(
            e_context.get("original_content", content) or ""
        )
        outer_content = str(
            e_context.get("outer_content", "") or ""
        )
        source_blob = "\n".join(
            item
            for item in (
                original_content,
                outer_content,
                content,
            )
            if item
        )
        target_wxid = str(
            e_context.get("from_wxid", "") or ""
        ).strip() or str(
            e_context.get("to_wxid", "") or ""
        ).strip()
        if not target_wxid:
            return

        msg_type = getattr(context, "type", None)
        quoted_link = bool(e_context.get("quoted_link", False))
        normalized = self._normalize_text(content)
        explicit = self._match_trigger(normalized) is not None
        direct_share = msg_type == ContextType.SHARING

        if direct_share and not quoted_link:
            if not self.allow_direct_share:
                return
        elif not explicit and not self.auto_detect:
            return

        card_meta = self._extract_finder_card_meta(
            source_blob
        )
        object_id = card_meta.get("object_id", "")
        nonce_length = len(
            card_meta.get("object_nonce_id", "")
        )

        if not object_id or not nonce_length:
            if explicit or direct_share:
                self._send_text(
                    target_wxid,
                    "未从视频号卡片中提取到 "
                    "objectId 和 objectNonceId。",
                )
                e_context.action = EventAction.BREAK_PASS
            return

        logger.info(
            "[FinderProtocolParser] card received: "
            f"object_id={object_id}, "
            f"nonce_length={nonce_length}"
        )

        try:
            detail = self._fetch_finder_card_detail(
                card_meta
            )
            media = self._collect_video_candidates(detail)

            if self.send_video:
                if not media:
                    raise RuntimeError(
                        "protocol response has no downloadable video"
                    )
                if not self._send_first_video(
                    target_wxid,
                    media,
                ):
                    raise RuntimeError(
                        "video download, decrypt, or send failed"
                    )

            if self.send_summary:
                summary = self._build_summary(
                    detail,
                    card_meta,
                )
                if summary:
                    self._send_text(target_wxid, summary)

            logger.info(
                "[FinderProtocolParser] handled: "
                f"object_id={object_id}, "
                f"media_count={len(media)}"
            )
        except Exception as exc:
            logger.error(
                "[FinderProtocolParser] failed: "
                f"object_id={object_id}, error={exc}",
                exc_info=True,
            )
            if self.send_failure_text:
                self._send_text(
                    target_wxid,
                    "视频号协议解析失败："
                    f"{self._shorten(str(exc), 120)}",
                )
        finally:
            e_context.action = EventAction.BREAK_PASS

    def _match_trigger(
        self,
        text: str,
    ) -> Optional[str]:
        for trigger in sorted(
            self.manual_triggers,
            key=len,
            reverse=True,
        ):
            if text == trigger:
                return ""
            if text.startswith(trigger + " "):
                return text[len(trigger) :].strip()
            if text.startswith(trigger + "\n"):
                return text[len(trigger) :].strip()
        return None

    def _extract_finder_card_meta(
        self,
        text: str,
    ) -> dict[str, str]:
        meta: dict[str, str] = {}
        if not text or not self._looks_like_finder_card(text):
            return meta

        for source in self._iter_decoded_sources(text):
            for tag, value in self._iter_xml_fields(source):
                key = self._normalize_field_name(tag)
                clean = self._clean_text(value)
                if not clean:
                    continue
                if (
                    key in {"objectid", "object_id"}
                    and not meta.get("object_id")
                ):
                    meta["object_id"] = clean
                elif (
                    key
                    in {
                        "objectnonceid",
                        "object_nonce_id",
                        "nonceid",
                        "nonce_id",
                    }
                    and not meta.get("object_nonce_id")
                ):
                    meta["object_nonce_id"] = clean
                elif (
                    key in {"finderusername", "username"}
                    and not meta.get("username")
                ):
                    meta["username"] = clean
                elif (
                    key == "nickname"
                    and not meta.get("nickname")
                ):
                    meta["nickname"] = clean
                elif (
                    key in {"desc", "description"}
                    and not meta.get("desc")
                ):
                    meta["desc"] = clean
            if (
                meta.get("object_id")
                and meta.get("object_nonce_id")
            ):
                break
        return meta

    def _fetch_finder_card_detail(
        self,
        card_meta: dict[str, str],
    ) -> dict[str, Any]:
        object_id = self._clean_text(
            card_meta.get("object_id")
        )
        object_nonce_id = self._clean_text(
            card_meta.get("object_nonce_id")
        )
        if not object_id or not object_nonce_id:
            raise RuntimeError(
                "missing objectId or objectNonceId"
            )

        bot_wxid = self._clean_text(
            Config.CURRENT_BOT_WXID
            or Config.EXPECTED_BOT_WXID
        )
        if not bot_wxid:
            bot_wxid = self._clean_text(
                self.wechat_api.get_current_login_wxid()
                or ""
            )
        if not bot_wxid:
            raise RuntimeError(
                "current bot wxid is unavailable"
            )

        endpoint = (
            f"{self.protocol_base}/Finder/GetCommentDetail"
        )
        response = requests.post(
            endpoint,
            data={
                "wxid": bot_wxid,
                "objectId": object_id,
                "objectNonceId": object_nonce_id,
                "cgi": str(self.protocol_cgi),
            },
            headers={"Accept": "application/json"},
            timeout=self.protocol_timeout,
        )
        if response.status_code != 200:
            raise RuntimeError(
                "protocol HTTP "
                f"{response.status_code}: "
                f"{(response.text or '')[:160]}"
            )

        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(
                "invalid protocol JSON payload"
            )
        code = payload.get("Code", payload.get("code"))
        success = payload.get(
            "Success",
            payload.get("success"),
        )
        if (
            success is False
            or code not in (None, 0, 200, "0", "200")
        ):
            message = self._clean_text(
                payload.get("Message")
                or payload.get("message")
                or "finder detail request failed"
            )
            raise RuntimeError(message)

        detail = payload.get("Data", payload.get("data"))
        if not isinstance(detail, dict):
            raise RuntimeError(
                "protocol response has no detail data"
            )

        logger.info(
            "[FinderProtocolParser] protocol refreshed: "
            f"object_id={object_id}, "
            f"media_count={len(detail.get('media') or [])}"
        )
        return detail

    def _collect_video_candidates(
        self,
        detail: dict[str, Any],
    ) -> list[dict[str, str]]:
        media_items = detail.get("media")
        if not isinstance(media_items, list):
            return []

        candidates: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in media_items:
            if not isinstance(item, dict):
                continue

            full_url = self._clean_text(
                item.get("full_url")
                or item.get("fullUrl")
                or item.get("FullURL")
            )
            if not full_url:
                base_url = self._clean_text(
                    item.get("url")
                    or item.get("URL")
                )
                url_token = self._clean_text(
                    item.get("url_token")
                    or item.get("urlToken")
                    or item.get("URLToken")
                )
                if base_url and url_token:
                    full_url = base_url + url_token

            validated = self._validate_media_url(full_url)
            if not validated or validated in seen:
                continue
            seen.add(validated)

            decode_key = self._clean_text(
                item.get("decode_key")
                or item.get("decodeKey")
                or item.get("decrypt_key")
                or item.get("decryptKey")
            )
            candidates.append(
                {
                    "url": validated,
                    "decode_key": decode_key,
                }
            )
        return candidates

    def _validate_media_url(self, value: str) -> str:
        url = html.unescape(str(value or "")).strip()
        url = re.sub(
            r"^<!\[CDATA\[(.*?)\]\]>$",
            r"\1",
            url,
            flags=re.DOTALL,
        ).strip()
        url = self._clean_url(url)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return ""
        if (
            parsed.hostname or ""
        ).lower() not in self.FINDER_MEDIA_HOSTS:
            return ""
        path = (parsed.path or "").lower()
        if not any(
            marker in path
            for marker in self.FINDER_VIDEO_PATH_MARKERS
        ):
            return ""

        query = parse_qs(
            parsed.query or "",
            keep_blank_values=True,
        )
        if not self._first_query_value(
            query,
            "encfilekey",
        ):
            return ""
        if not self._first_query_value(query, "token"):
            return ""
        return url

    def _first_query_value(
        self,
        query: dict[str, list[str]],
        key: str,
    ) -> str:
        for value in query.get(key) or []:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    def _build_summary(
        self,
        detail: dict[str, Any],
        card_meta: dict[str, str],
    ) -> str:
        title = self._clean_text(
            detail.get("title")
            or card_meta.get("desc")
            or "视频号内容"
        )
        nickname = self._clean_text(
            detail.get("nickname")
            or card_meta.get("nickname")
        )
        lines = [f"标题：{self._shorten(title, 100)}"]
        if nickname:
            lines.append(
                f"作者：{self._shorten(nickname, 60)}"
            )
        lines.append("解析方式：bot 协议直刷")
        return "\n".join(lines)

    def _send_first_video(
        self,
        target_wxid: str,
        candidates: list[dict[str, str]],
    ) -> bool:
        for index, candidate in enumerate(candidates):
            video_url = self._clean_text(
                candidate.get("url")
            )
            decode_key = self._clean_text(
                candidate.get("decode_key")
            )
            if not video_url:
                continue

            video_path = ""
            try:
                logger.info(
                    "[FinderProtocolParser] media attempt: "
                    f"index={index + 1}/{len(candidates)}, "
                    f"url_length={len(video_url)}, "
                    f"query_keys={self._url_query_keys(video_url)}, "
                    f"decode_key_present={bool(decode_key)}"
                )
                video_path = download_video(
                    video_url,
                    self.cache_dir,
                    timeout=self.download_timeout,
                    max_size_mb=self.video_max_size_mb,
                    prefix="finder_protocol_",
                    referer=(
                        "https://channels.weixin.qq.com/"
                    ),
                )
                if not video_path:
                    continue

                if (
                    decode_key
                    and not self._is_probable_video_file(
                        video_path
                    )
                ):
                    self._decrypt_video_file(
                        video_path,
                        decode_key,
                    )

                if not self._is_probable_video_file(
                    video_path
                ):
                    logger.warning(
                        "[FinderProtocolParser] downloaded "
                        "file is not recognized as video"
                    )
                    continue

                try:
                    self.wechat_api.send_video_msg(
                        wxid=target_wxid,
                        video_path=video_path,
                        http_port=Config.WECHAT_HTTP_PORT,
                    )
                    return True
                except Exception as video_error:
                    logger.warning(
                        "[FinderProtocolParser] video send "
                        "failed, fallback to file: "
                        f"{video_error}"
                    )
                    self.wechat_api.send_file_msg(
                        wxid=target_wxid,
                        file_path=video_path,
                        http_port=Config.WECHAT_HTTP_PORT,
                    )
                    return True
            except Exception as exc:
                logger.warning(
                    "[FinderProtocolParser] media failed: "
                    f"{exc}"
                )
            finally:
                self._remove_file(video_path)
        return False

    def _decrypt_video_file(
        self,
        file_path: str,
        decode_key: str,
    ) -> None:
        key_text = self._clean_text(decode_key)
        if not key_text:
            return
        try:
            key = int(key_text, 10)
        except ValueError as exc:
            raise RuntimeError(
                "invalid decodeKey"
            ) from exc
        if key <= 0:
            return

        with open(file_path, "r+b") as file_obj:
            data = bytearray(file_obj.read(131072))
            if not data:
                return
            self._finder_isaac64_xor(data, key)
            file_obj.seek(0)
            file_obj.write(data)

        logger.info(
            "[FinderProtocolParser] decrypted video prefix: "
            f"bytes={len(data)}"
        )

    def _finder_isaac64_xor(
        self,
        data: bytearray,
        key: int,
    ) -> None:
        seed = [0] * 256
        mm = [0] * 256
        seed[0] = self._u64(key)
        aa = bb = cc = 0
        golden = 0x9E3779B97F4A7C13
        a = b = c = d = e = f = g = h = golden

        for _ in range(4):
            a, b, c, d, e, f, g, h = (
                self._isaac64_mix(
                    a,
                    b,
                    c,
                    d,
                    e,
                    f,
                    g,
                    h,
                )
            )

        for index in range(0, 256, 8):
            a, b, c, d, e, f, g, h = (
                self._u64(a + seed[index]),
                self._u64(b + seed[index + 1]),
                self._u64(c + seed[index + 2]),
                self._u64(d + seed[index + 3]),
                self._u64(e + seed[index + 4]),
                self._u64(f + seed[index + 5]),
                self._u64(g + seed[index + 6]),
                self._u64(h + seed[index + 7]),
            )
            a, b, c, d, e, f, g, h = (
                self._isaac64_mix(
                    a,
                    b,
                    c,
                    d,
                    e,
                    f,
                    g,
                    h,
                )
            )
            mm[index : index + 8] = [
                a,
                b,
                c,
                d,
                e,
                f,
                g,
                h,
            ]

        for index in range(0, 256, 8):
            a, b, c, d, e, f, g, h = (
                self._u64(a + mm[index]),
                self._u64(b + mm[index + 1]),
                self._u64(c + mm[index + 2]),
                self._u64(d + mm[index + 3]),
                self._u64(e + mm[index + 4]),
                self._u64(f + mm[index + 5]),
                self._u64(g + mm[index + 6]),
                self._u64(h + mm[index + 7]),
            )
            a, b, c, d, e, f, g, h = (
                self._isaac64_mix(
                    a,
                    b,
                    c,
                    d,
                    e,
                    f,
                    g,
                    h,
                )
            )
            mm[index : index + 8] = [
                a,
                b,
                c,
                d,
                e,
                f,
                g,
                h,
            ]

        seed, aa, bb, cc = self._isaac64(
            seed,
            mm,
            aa,
            bb,
            cc,
        )
        rand_count = 255

        for offset in range(0, len(data), 8):
            random_number = seed[rand_count]
            if rand_count == 0:
                seed, aa, bb, cc = self._isaac64(
                    seed,
                    mm,
                    aa,
                    bb,
                    cc,
                )
                rand_count = 255
            else:
                rand_count -= 1

            block = random_number.to_bytes(8, "big")
            for position, value in enumerate(block):
                data_index = offset + position
                if data_index >= len(data):
                    return
                data[data_index] ^= value

    def _isaac64(
        self,
        seed: list[int],
        mm: list[int],
        aa: int,
        bb: int,
        cc: int,
    ) -> tuple[list[int], int, int, int]:
        cc = self._u64(cc + 1)
        bb = self._u64(bb + cc)
        for index in range(256):
            x = mm[index]
            if index % 4 == 0:
                aa = self._u64(
                    ~(aa ^ self._u64(aa << 21))
                )
            elif index % 4 == 1:
                aa = self._u64(aa ^ (aa >> 5))
            elif index % 4 == 2:
                aa = self._u64(
                    aa ^ self._u64(aa << 12)
                )
            else:
                aa = self._u64(aa ^ (aa >> 33))
            aa = self._u64(
                aa + mm[(index + 128) % 256]
            )
            y = self._u64(
                mm[(x >> 3) % 256] + aa + bb
            )
            mm[index] = y
            bb = self._u64(
                mm[(y >> 11) % 256] + x
            )
            seed[index] = bb
        return seed, aa, bb, cc

    def _isaac64_mix(
        self,
        a: int,
        b: int,
        c: int,
        d: int,
        e: int,
        f: int,
        g: int,
        h: int,
    ) -> tuple[
        int,
        int,
        int,
        int,
        int,
        int,
        int,
        int,
    ]:
        a = self._u64(a - e)
        f = self._u64(f ^ (h >> 9))
        h = self._u64(h + a)
        b = self._u64(b - f)
        g = self._u64(g ^ self._u64(a << 9))
        a = self._u64(a + b)
        c = self._u64(c - g)
        h = self._u64(h ^ (b >> 23))
        b = self._u64(b + c)
        d = self._u64(d - h)
        a = self._u64(
            a ^ self._u64(c << 15)
        )
        c = self._u64(c + d)
        e = self._u64(e - a)
        b = self._u64(b ^ (d >> 14))
        d = self._u64(d + e)
        f = self._u64(f - b)
        c = self._u64(
            c ^ self._u64(e << 20)
        )
        e = self._u64(e + f)
        g = self._u64(g - c)
        d = self._u64(d ^ (f >> 17))
        f = self._u64(f + g)
        h = self._u64(h - d)
        e = self._u64(
            e ^ self._u64(g << 14)
        )
        g = self._u64(g + h)
        return a, b, c, d, e, f, g, h

    def _u64(self, value: int) -> int:
        return value & self.U64_MASK

    def _is_probable_video_file(
        self,
        file_path: str,
    ) -> bool:
        try:
            if (
                not file_path
                or os.path.getsize(file_path) < 1024
            ):
                return False
            with open(file_path, "rb") as file_obj:
                header = file_obj.read(64)
            return (
                b"ftyp" in header
                or header.startswith(b"\x1a\x45\xdf\xa3")
                or header.startswith(b"RIFF")
                or header.startswith(b"FLV")
            )
        except OSError:
            return False

    def _iter_decoded_sources(self, *values: str):
        seen: set[str] = set()
        for value in values:
            if not value:
                continue
            stack = [str(value)]
            while stack:
                item = stack.pop()
                if not item or item in seen:
                    continue
                seen.add(item)
                yield item
                decoded = self._decode_repeated(item)
                if decoded and decoded not in seen:
                    stack.append(decoded)

    def _decode_repeated(self, value: str) -> str:
        text = html.unescape(
            str(value or "")
        ).strip()
        text = re.sub(
            r"<!\[CDATA\[(.*?)\]\]>",
            r"\1",
            text,
            flags=re.DOTALL,
        )
        for _ in range(3):
            decoded = html.unescape(unquote(text))
            if decoded == text:
                break
            text = decoded
        return text.strip()

    def _iter_xml_fields(self, text: str):
        for xml_text in self._iter_xml_documents(text):
            try:
                root = ET.fromstring(xml_text)
                for element in root.iter():
                    tag = str(
                        element.tag or ""
                    ).split("}", 1)[-1]
                    for (
                        attribute_name,
                        attribute_value,
                    ) in element.attrib.items():
                        value = self._decode_repeated(
                            attribute_value
                        )
                        if tag and attribute_name and value:
                            yield (
                                f"{tag}.{attribute_name}",
                                value,
                            )
                    value = self._decode_repeated(
                        "".join(element.itertext())
                        if element.text is None
                        else element.text
                    )
                    if tag and value:
                        yield tag, value
                return
            except ET.ParseError:
                pass

        for match in self.XML_FIELD_RE.finditer(
            text or ""
        ):
            tag = match.group("tag") or ""
            value = self._decode_repeated(
                match.group("value") or ""
            )
            if tag and value:
                yield tag, value

        for match in self.XML_ATTR_RE.finditer(
            text or ""
        ):
            tag = match.group("tag") or ""
            attributes = match.group("attrs") or ""
            for attribute in (
                self.XML_ATTR_VALUE_RE.finditer(
                    attributes
                )
            ):
                name = attribute.group("name") or ""
                value = self._decode_repeated(
                    attribute.group("value") or ""
                )
                if tag and name and value:
                    yield f"{tag}.{name}", value

    def _iter_xml_documents(self, text: str):
        raw = str(text or "").strip()
        candidates = [raw]
        unescaped = html.unescape(raw)
        if unescaped != raw:
            candidates.append(unescaped)
        unquoted = unquote(raw)
        if unquoted not in candidates:
            candidates.append(unquoted)

        seen: set[str] = set()
        for value in candidates:
            if not value or value in seen:
                continue
            seen.add(value)
            xml_document = self._extract_xml_document(
                value
            )
            if xml_document:
                yield xml_document

    def _extract_xml_document(self, value: str) -> str:
        if "<?xml" in value:
            value = value[value.find("<?xml") :]
        elif "<msg" in value:
            value = value[value.find("<msg") :]
        elif "<appmsg" in value:
            value = value[value.find("<appmsg") :]
        else:
            return ""

        for end_tag in ("</msg>", "</appmsg>"):
            end_index = value.rfind(end_tag)
            if end_index >= 0:
                return value[
                    : end_index + len(end_tag)
                ]
        return value

    def _normalize_field_name(self, value: str) -> str:
        return re.sub(
            r"[^a-z0-9]",
            "",
            str(value or "").lower(),
        )

    def _looks_like_finder_card(self, text: str) -> bool:
        compact = (text or "").lower()
        return any(
            marker in compact
            for marker in self.FINDER_MARKERS
        )

    def _url_query_keys(self, url: str) -> list[str]:
        try:
            return sorted(
                parse_qs(
                    urlparse(url).query or "",
                    keep_blank_values=True,
                )
            )
        except Exception:
            return []

    def _clean_url(self, value: str) -> str:
        return str(value or "").strip().rstrip(
            "】）)】>。，,;；\"'`"
        )

    def _send_text(
        self,
        target_wxid: str,
        content: str,
    ) -> None:
        text = self._clean_text(content)
        if not target_wxid or not text:
            return
        self.wechat_api.send_text_msg(
            wxid=target_wxid,
            content=text,
            http_port=Config.WECHAT_HTTP_PORT,
        )

    def _remove_file(self, file_path: str) -> None:
        if not file_path:
            return
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except OSError:
            pass

    def _shorten(self, text: str, limit: int) -> str:
        value = self._clean_text(text)
        if len(value) <= limit:
            return value
        return (
            value[: max(0, limit - 3)].rstrip()
            + "..."
        )

    def _clean_text(self, value: Any) -> str:
        text = html.unescape(
            "" if value is None else str(value)
        )
        text = text.replace("\r", " ")
        text = text.replace("\n", " ")
        text = text.replace("\t", " ")
        return re.sub(r"\s+", " ", text).strip()

    def _normalize_text(self, value: Any) -> str:
        text = self._clean_text(value)
        return re.sub(
            r"^@\S+[\u2005\u00a0\s]*",
            "",
            text,
        ).strip()

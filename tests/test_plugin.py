from __future__ import annotations

import html
import importlib.util
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_FILE = (
    ROOT
    / "plugin"
    / "finder_protocol_parser"
    / "main.py"
)


class DummyLogger:
    def debug(self, *_args, **_kwargs) -> None:
        pass

    def info(self, *_args, **_kwargs) -> None:
        pass

    def warning(self, *_args, **_kwargs) -> None:
        pass

    def error(self, *_args, **_kwargs) -> None:
        pass


def install_framework_stubs() -> None:
    config_package = types.ModuleType("config")
    config_module = types.ModuleType("config.config")

    class Config:
        BASE_DIR = ROOT
        CURRENT_BOT_WXID = "bot_example"
        EXPECTED_BOT_WXID = ""
        WECHAT_HTTP_PORT = 9011

    config_module.Config = Config
    sys.modules["config"] = config_package
    sys.modules["config.config"] = config_module

    core_package = types.ModuleType("core")
    context_module = types.ModuleType("core.context")

    class ContextType:
        SHARING = "sharing"

    context_module.ContextType = ContextType

    plugin_module = types.ModuleType("core.plugin_system")

    class Event:
        ON_HANDLE_CONTEXT = "on_handle_context"

    class EventAction:
        BREAK_PASS = "break_pass"

    class EventContext:
        pass

    class Plugin:
        def __init__(self) -> None:
            self.handlers = {}

        def load_config(self) -> dict:
            return {}

    def register(**_kwargs):
        def decorator(cls):
            return cls

        return decorator

    plugin_module.Event = Event
    plugin_module.EventAction = EventAction
    plugin_module.EventContext = EventContext
    plugin_module.Plugin = Plugin
    plugin_module.register = register

    wechat_api_module = types.ModuleType("core.wechat_api")

    class WechatAPIClient:
        def get_current_login_wxid(self) -> str:
            return "bot_example"

    wechat_api_module.WechatAPIClient = WechatAPIClient

    sys.modules["core"] = core_package
    sys.modules["core.context"] = context_module
    sys.modules["core.plugin_system"] = plugin_module
    sys.modules["core.wechat_api"] = wechat_api_module

    utils_package = types.ModuleType("utils")
    download_module = types.ModuleType(
        "utils.download_helper"
    )
    logger_module = types.ModuleType("utils.logger")
    download_module.download_video = (
        lambda *_args, **_kwargs: ""
    )
    logger_module.get_logger = (
        lambda _name: DummyLogger()
    )

    sys.modules["utils"] = utils_package
    sys.modules["utils.download_helper"] = download_module
    sys.modules["utils.logger"] = logger_module


@pytest.fixture(scope="module")
def plugin_module():
    install_framework_stubs()
    spec = importlib.util.spec_from_file_location(
        "finder_protocol_parser_test_module",
        PLUGIN_FILE,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def parser(plugin_module):
    instance = plugin_module.FinderProtocolParser.__new__(
        plugin_module.FinderProtocolParser
    )
    instance.protocol_base = (
        "http://127.0.0.1:9000/api"
    )
    instance.protocol_timeout = 15
    instance.protocol_cgi = 3906
    instance.wechat_api = (
        plugin_module.WechatAPIClient()
    )
    return instance


def test_extracts_ids_from_escaped_xml(parser) -> None:
    raw_xml = """
    <msg>
      <appmsg>
        <finderFeed>
          <objectId>12345678901234567890</objectId>
          <objectNonceId>nonce_example_123456</objectNonceId>
          <nickname>示例作者</nickname>
          <desc>示例内容</desc>
        </finderFeed>
      </appmsg>
    </msg>
    """

    result = parser._extract_finder_card_meta(
        html.escape(raw_xml)
    )

    assert result == {
        "object_id": "12345678901234567890",
        "object_nonce_id": "nonce_example_123456",
        "nickname": "示例作者",
        "desc": "示例内容",
    }


def test_calls_protocol_endpoint_with_form_data(
    plugin_module,
    parser,
    monkeypatch,
) -> None:
    captured = {}

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "Code": 0,
                "Success": True,
                "Data": {
                    "title": "示例视频",
                    "media": [],
                },
            }

    def fake_post(url, data, headers, timeout):
        captured.update(
            {
                "url": url,
                "data": data,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return Response()

    monkeypatch.setattr(
        plugin_module.requests,
        "post",
        fake_post,
    )

    detail = parser._fetch_finder_card_detail(
        {
            "object_id": "12345678901234567890",
            "object_nonce_id": "nonce_example_123456",
        }
    )

    assert detail["title"] == "示例视频"
    assert captured["url"].endswith(
        "/Finder/GetCommentDetail"
    )
    assert captured["data"] == {
        "wxid": "bot_example",
        "objectId": "12345678901234567890",
        "objectNonceId": "nonce_example_123456",
        "cgi": "3906",
    }


def test_collects_full_media_url_and_decode_key(
    parser,
) -> None:
    base_url = (
        "https://wxapp.tc.qq.com/251/20302/stodownload"
        "?encfilekey=example"
    )
    token = "&token=example_token&sign=example_sign"
    detail = {
        "media": [
            {
                "url": base_url,
                "url_token": token,
                "decode_key": "123456789",
            }
        ]
    }

    result = parser._collect_video_candidates(detail)

    assert result == [
        {
            "url": base_url + token,
            "decode_key": "123456789",
        }
    ]


def test_isaac64_xor_is_reversible(parser) -> None:
    original = bytes(range(256)) * 4
    data = bytearray(original)

    parser._finder_isaac64_xor(data, 123456789)
    assert bytes(data) != original

    parser._finder_isaac64_xor(data, 123456789)
    assert bytes(data) == original

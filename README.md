# wxbot 视频号协议直刷插件

这是一个独立的 wxbot 插件项目，专门使用机器人自身的 protocol-core 登录态解析
视频号卡片并发送视频。

## 项目定位

本项目只走以下链路：

```text
wxbot 收到视频号卡片 XML
-> 提取 objectId + objectNonceId
-> protocol-core /Finder/GetCommentDetail
-> 微信 Finder CGI 3906
-> 返回 full_url + decode_key
-> 下载视频
-> ISAAC64 解密
-> bot 发送视频
```

本项目完全不依赖：

- `weixin.qq.com/sph/...` 短链。
- 视频号管理后台 Chromium。
- Finder 短链网关。
- 元宝 Cookie。
- `wxsph-api`。

## 与其他插件的区别

### `wxbot-finder-card-plugin`

```text
卡片 XML
-> 生成 sph
-> 元宝解析
-> 发送视频
```

### 本项目

```text
卡片 XML
-> bot 协议直接刷新媒体
-> 下载、解密、发送
```

两个插件不要使用相同触发词，否则可能同时响应。协议插件默认使用：

```text
协议解析
协议视频
```

## 依赖

### wxbot 框架

插件需要：

```text
config.config.Config
core.context.ContextType
core.plugin_system
core.wechat_api.WechatAPIClient
utils.download_helper.download_video
utils.logger.get_logger
```

### protocol-core

必须提供：

```text
POST http://127.0.0.1:9000/api/Finder/GetCommentDetail
```

protocol-core 需要保存当前 bot 微信号的有效 iPad/protocol 登录态，并支持：

```text
/cgi-bin/micromsg-bin/findergetcommentdetail
CGI 3906
```

## 本地安装

### 1. 获取代码

```powershell
git clone YOUR_REPOSITORY_URL
cd wxbot-finder-protocol-plugin
```

### 2. 安装 Python 依赖

在 wxbot 使用的 Python 环境中：

```powershell
python -m pip install -r .\requirements.txt
```

### 3. 安装插件

```powershell
powershell -ExecutionPolicy Bypass -File .\install_local.ps1 `
  -BotRoot "E:\path\to\your-bot"
```

目标目录：

```text
YOUR_BOT_ROOT/
└── wxbot/
    └── plugins/
        └── finder_protocol_parser/
            ├── main.py
            └── config.json
```

安装脚本会更新 `main.py`，但不会覆盖已经存在的 `config.json`。

### 4. 配置

```json
{
  "enabled": true,
  "auto_detect": false,
  "allow_direct_share": false,
  "send_summary": true,
  "send_video": true,
  "triggers": [
    "协议解析",
    "协议视频"
  ],
  "protocol_base": "http://127.0.0.1:9000/api",
  "protocol_timeout": 15,
  "protocol_cgi": 3906
}
```

配置文件：

```text
wxbot/plugins/finder_protocol_parser/config.json
```

### 5. 重启 wxbot

安装或修改配置后重启 wxbot，让插件管理器重新加载插件。

## 使用方法

默认安全模式：

1. 引用或回复视频号卡片。
2. 发送 `协议解析`。
3. 插件读取引用卡片 XML。
4. protocol-core 刷新媒体。
5. bot 下载、解密并发送视频。

直接转发卡片默认不处理。如需直接转发即解析：

```json
{
  "allow_direct_share": true
}
```

建议先在私聊中测试，不建议直接在大型群聊中启用自动解析。

## 配置项

| 配置 | 作用 |
|---|---|
| `enabled` | 启用插件 |
| `allow_group` | 允许群聊 |
| `allow_private` | 允许私聊 |
| `auto_detect` | 自动检测上下文中的视频号卡片 |
| `allow_direct_share` | 允许直接转发卡片触发 |
| `triggers` | 手动触发词 |
| `protocol_base` | protocol-core API 地址 |
| `protocol_timeout` | Finder 请求超时 |
| `protocol_cgi` | Finder CGI，默认 3906 |
| `send_video` | 下载并发送视频 |
| `send_summary` | 发送标题和作者 |
| `video_max_size_mb` | 最大下载体积 |
| `download_timeout` | 视频下载超时 |

完整说明见[安装与配置](docs/安装与配置.md)。

## 返回媒体要求

protocol-core 至少需要返回：

```json
{
  "Data": {
    "media": [
      {
        "full_url": "HTTPS_MEDIA_URL",
        "decode_key": "NUMERIC_DECODE_KEY"
      }
    ]
  }
}
```

或者：

```json
{
  "url": "BASE_MEDIA_URL",
  "url_token": "TOKEN_QUERY_PART",
  "decode_key": "NUMERIC_DECODE_KEY"
}
```

插件会拼接 `url + url_token`。

## 下载和解密

视频号 CDN 可能返回加密视频：

- HTTP 下载成功。
- `Content-Type` 可能仍为 `video/mp4`。
- 文件头不包含 `ftyp`。
- protocol-core 同批次返回 `decode_key`。

插件使用 `decode_key` 对文件前 128 KB 执行 ISAAC64 XOR。解密后重新检查视频
文件头，再发送给用户。

详见[视频下载与解密](docs/视频下载与解密.md)。

## 测试

```powershell
python -m pip install -r .\requirements-dev.txt
python -m pytest -q
python -m py_compile .\plugin\finder_protocol_parser\main.py
```

测试覆盖：

- 转义卡片 XML 字段提取。
- protocol-core 请求参数。
- `url + url_token + decode_key` 组合。
- ISAAC64 XOR 可逆性。

测试不会请求真实微信接口。

## 目录结构

```text
.
├── README.md
├── SECURITY.md
├── install_local.ps1
├── requirements.txt
├── requirements-dev.txt
├── plugin/
│   └── finder_protocol_parser/
│       ├── main.py
│       └── config.example.json
├── tests/
│   └── test_plugin.py
└── docs/
    ├── 协议直刷原理.md
    ├── 安装与配置.md
    ├── protocol-core接口要求.md
    ├── 视频下载与解密.md
    └── 故障排查.md
```

## 安全

- protocol-core 不要暴露公网。
- 不记录完整 nonce、媒体 URL 或 decodeKey。
- 不提交机器人登录态和真实卡片 XML。
- 保持显式触发，避免高频调用微信私有接口。

详细要求见 [SECURITY.md](SECURITY.md)。

## 许可证

本插件新增代码按 MIT License 发布，详见 [LICENSE](LICENSE)。

本仓库依赖的 wxbot、protocol-core、微信平台接口及其他第三方组件，
不由本许可证重新授权；使用前请确认各自的授权范围和平台规则。

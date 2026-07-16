# protocol-core 接口要求

## 路径

```text
POST /api/Finder/GetCommentDetail
```

默认完整地址：

```text
http://127.0.0.1:9000/api/Finder/GetCommentDetail
```

## 请求格式

```text
Content-Type: application/x-www-form-urlencoded
Accept: application/json
```

表单字段：

```text
wxid=BOT_WXID
objectId=OBJECT_ID
objectNonceId=OBJECT_NONCE_ID
cgi=3906
```

插件也依赖 wxbot 能通过以下任一方式取得当前 bot wxid：

```text
Config.CURRENT_BOT_WXID
Config.EXPECTED_BOT_WXID
WechatAPIClient.get_current_login_wxid()
```

## 成功响应

```json
{
  "Code": 0,
  "Success": true,
  "Message": "success",
  "Data": {
    "object_id": "OBJECT_ID",
    "title": "视频标题",
    "nickname": "作者",
    "media": [
      {
        "url": "BASE_MEDIA_URL",
        "url_token": "TOKEN_QUERY_PART",
        "full_url": "FULL_MEDIA_URL",
        "decode_key": "NUMERIC_KEY",
        "media_type": 4,
        "file_size": 123456
      }
    ]
  }
}
```

插件兼容：

```text
Code / code
Success / success
Data / data
Message / message
```

媒体字段兼容：

```text
full_url / fullUrl / FullURL
url / URL
url_token / urlToken / URLToken
decode_key / decodeKey / decrypt_key / decryptKey
```

## 失败响应

```json
{
  "Code": -8,
  "Success": false,
  "Message": "login data not found",
  "Data": null
}
```

常见消息：

```text
missing wxid
invalid objectId
missing objectNonceId
login data not found
findergetcommentdetail failed
```

## protocol-core 内部要求

protocol-core 需要：

1. 根据 wxid 读取有效登录态。
2. 构造 `FinderGetCommentDetailRequest`。
3. 调用 CGI 3906。
4. 解析 `FinderGetCommentDetailResponse`。
5. 从媒体 protobuf unknown fields 读取：
   - field 12：decode_key
   - field 13：url_token
6. 拼接 `full_url`。

## 安全要求

- 接口只监听本机或私有网络。
- 不在 access log 中记录表单体。
- 不输出完整 nonce。
- 不输出完整媒体 URL 和 decodeKey 到普通日志。
- 不允许匿名公网调用。

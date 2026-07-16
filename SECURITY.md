# 安全说明

## 禁止提交

不要提交：

```text
plugin/finder_protocol_parser/config.json
机器人 Cookie、session key、MMTLS Key
机器人 wxid
二维码
完整 objectNonceId
完整卡片 XML
完整带 token 的视频 URL
decodeKey
下载缓存和日志
```

## 网络边界

插件默认调用：

```text
http://127.0.0.1:9000/api/Finder/GetCommentDetail
```

protocol-core 应只监听本机或受保护的私有网络，不应直接暴露公网。跨机器部署时
应使用 VPN、私网隧道或带身份认证的 HTTPS 包装层。

## 日志

允许记录：

```text
objectId
objectNonceId 长度
媒体数量
URL 长度和查询参数名称
是否存在 decodeKey
错误类型
```

禁止记录：

```text
完整 nonce
完整 URL
decodeKey 值
登录态
请求头
```

# 官方 Lark CLI 与 auth-mcp 集成说明

VikingBot 继续使用官方 `lark-cli` 作为命令执行器，但不再读取 CLI 本地加密凭据、Keychain 或 refresh token。`close_helper.py` 和 `cli_credentials.py` 已移除。

```json
{"cli": {"executable": "lark-cli", "profile": "support"}}
```

用户 token 由 `wangoon-auth-mcp` 的 `provider=lark-cli` 提供：

- auth-mcp 优先使用 `lark-cli` 自有 refresh token；
- refresh 失败后才复用唯一扫码源 `provider=lark-qr` 自动授权；
- VikingBot 通过 `LARKSUITE_CLI_APP_ID` 和 `LARKSUITE_CLI_USER_ACCESS_TOKEN` 注入官方 CLI；
- 关单直接执行 `lark-cli api PUT /open-apis/helpdesk/v1/tickets/<id>`，业务 `before_call` 在一次 PUT 前完成。

因此部署需要保留：

- 官方 CLI 可执行文件及正确的 profile/app 配置；
- auth-mcp 状态目录和客户端 Token；
- 可用的 `lark-qr`、`lark-cli` provider。

不再需要 CLI 用户本地登录态、加密凭据目录、`master.key` 或 macOS Keychain。完整部署说明见 [服务台部署说明](../../docs/zh/extensions/feishu-helpdesk-followup.md)。

# Lark CLI 服务台请求头兼容补丁

补丁基于 `larksuite/cli` 提交 `4e2cbea94e33913378ce515f0fccfb9b1f2f2bb9`（v1.0.66）。它仅给原生 `api` 命令增加 `--headers` JSON 参数，支持 `-` 从 stdin 读取、`@file` 和内联 JSON。VikingBot 用 stdin 传递服务台授权头，CLI 继续自行读取并刷新当前用户凭据；无需导出 `user_access_token`。

补丁拒绝覆盖 Authorization、Host、Cookie、连接及请求体 framing 头，拒绝非法字段和 CRLF；检测多个参数争用 stdin。自定义请求头值不出现在 dry-run 和参数解析错误中。新增测试验证 HTTP 请求实际携带服务台头、用户身份不变、错误类型和脱敏。

## 构建私有副本

需要 Git、Python 3 和 Go 1.23+。从 VikingBot 仓库根运行：

```bash
bash bot/compat/lark-cli/build.sh \
  /path/to/lark-cli-source \
  /absolute/path/to/bin/lark-cli-helpdesk
```

源参数也可使用该 CLI 仓库的 Git URL。脚本在临时克隆中固定版本、应用补丁、获取官方命令元数据、运行相关测试并构建，结束时清理临时克隆。源仓库和全局安装的 CLI 不变，输出路径必须尚不存在。使用已有源仓库时，需包含上述固定提交。构建需要访问 Go 模块源和飞书公开元数据接口，不读取用户授权。

然后将现有渠道的 `bot.channels[].helpdesk.cli.executable` 指向这个私有二进制。保留既有 CLI profile，并按 [服务台部署说明](../../docs/zh/extensions/feishu-helpdesk.md) 授权。

不带此补丁的 CLI 可正常邀请 Bot、欢迎和发送提醒，但关单原生 API 缺少服务台头，无法完成自动关单。不要通过复制 CLI 的用户 Token 到 Bot 绕过这一依赖。更换 CLI 版本前先检查是否已有等价 `api --headers` 能力，避免重复应用补丁。

## 验证记录

2026-11-03：补丁的 `go test ./cmd/api`、`go vet ./...` 和二进制构建通过；完整 `make unit-test` 已执行。完整套件在 `cmd`（动态元数据的 bot-only 判定）、`semantic-review`（策略/waiver）和 `shortcuts/minutes`（本机将 example.com 判断为受限地址）失败。同样的失败已在固定提交的无补丁克隆中复现；请求头新增测试通过。没有发起真实用户 API 请求。

上游合并仅涉及 `cmd/api/api.go` 和新增的 `cmd/api/headers_test.go`，未改变 CLI 登录、Token 续期或凭据存储逻辑。

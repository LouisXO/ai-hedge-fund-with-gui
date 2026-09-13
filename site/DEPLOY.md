# P3 部署说明(两层)

## 公开层 — Netlify(观点与市场事实,无仓位)

内容:大师共识/分歧 + 各家理由、市场异动。**绝不含**持仓、成本、盈亏、期权结构。

1. 本地预览:`open /Users/louis/hedge-fund/site/public/index.html`
2. Netlify 配置(一次性,在 Netlify UI):
   - Site settings → Build & deploy → **Production branch 改为 `v2-rebuild`**
   - Publish directory 由仓库根的 `netlify.toml` 提供(`site/public`),无需构建
   - 老 v1 站保留在 `main`,可另建一个 site 指向 main 作为 legacy
3. 开启每日自动发布:在 8:41 的 wrapper 里把 `PUBLISH` 设为 `1`
   (`/Users/louis/optradar/bin/run_and_notify.sh` 中的 site build 阶段)
   在此之前只生成不推送。

## 私有层 — Tailscale(持仓/盈亏/期权结构)

当前实现:Mac 本地 HTTP(仅 127.0.0.1)+ `tailscale serve` 代理。

- 地址:**https://macbook-pro.tail5b470b.ts.net/**(tailnet 内,自动 HTTPS)
- 服务:`~/Library/LaunchAgents/com.louis.optradar.private.plist`
  (KeepAlive,开机自启,serve `/Users/louis/optradar/out`)
- 手机:装 Tailscale App 登同一账号后直接打开上述地址
- macOS 的 App Store 版 Tailscale 不支持直接 serve 目录(沙盒),
  所以走"本地端口 + serve 代理"这条路。

### ⚠️ 访问控制(必须处理)

tailnet 内有两个用户:`louis20001010@` 与 `soelieframing@`。
**Tailscale 默认允许 tailnet 内设备互访**,即对方设备可能打开上面的私有地址。

修复(Tailscale 管理后台 → Access Controls,编辑 ACL):

```jsonc
{
  "acls": [
    // 仅本人设备可访问 macbook-pro 的 web 服务
    {"action": "accept", "src": ["louis20001010@"], "dst": ["macbook-pro:443,8787"]},
    // 其余成员之间的既有互通规则按需保留
  ]
}
```
或把私有服务迁到一台仅自己拥有的节点,并用 ACL 收紧。

### 待办:迁到常开的 lx-pc

Mac 睡眠时私有页不可达(iMessage 文字早报仍会送达)。
lx-pc(100.90.46.88,常开,已在 tailnet,445/8096 开放)是更合适的宿主:
1. Mac 侧:把 `optradar/out` 经 SMB 同步到 lx-pc(需先在 Finder 挂载一次共享、凭据存钥匙串)
2. lx-pc 侧:`tailscale serve` 指向该目录(Windows 版无 macOS 的沙盒限制,可直接 serve 路径)

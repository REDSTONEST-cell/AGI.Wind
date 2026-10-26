# 公网 HTTPS（2026-10-26）

正式入口：https://agi.weidu123526.top:18443/ 。使用原有客户端密钥登录并点击通话。

已部署 Caddy 2.11.7 + AliDNS 模块，通过 DNS-01 取得 Let's Encrypt 正式证书。TLS 1.3、默认可信根与主机名校验、未登录请求拒绝、通话服务、WSS 同源票据握手和取消响应均已通过。本机通过公网域名访问成功不能替代 vivo 手机外网、麦克风与实际延迟验收。

原 sslip.io 域名的公网 80/443 验证超时，改用获准复用的 NAS 阿里云 DNS 凭据。只新增 agi 子域名 AAAA，NAS 原有记录和服务保持原样。Windows 专用防火墙规则现仅允许本项目 Caddy AliDNS 程序在指定 IPv6 的 TCP 18443 入站，之前的 80/443 试验规则已替换。代理转发至 127.0.0.1:18440，没有第三方隧道。

配置在 config/https.local.json，运行资产在 data/https 和 .devtools/caddy，均忽略提交。DNS 密钥通过 Windows 当前用户 DPAPI 加密在 data/https/alidns.dpapi；启动时只解密为子进程环境变量并恢复父环境。Caddyfile 使用占位符，管理 API 与配置持久化关闭；没有访问日志。SSH 密码不保存。证书、私钥、ACME 账号和密钥文件不得打包或提交。

scripts/start-https.ps1 启动/复用正式代理，-ValidateOnly 只校验；scripts/stop-https.ps1 仅停止受管代理。start-local.ps1 发现本地 HTTPS 配置时自动启动；stop-local.ps1 普通关停同时停止代理，-KeepVoice 重启路径保留代理。持续运行时自动续证，目前未注册独立开机任务。enable-https-firewall.ps1 仅在管理员终端调整本项目专用规则。

公网 IPv6 变化后，需更新本地 bind_address、agi AAAA 和专用防火墙规则并重启代理。目前电脑的新增 AAAA 为静态记录，NAS DDNS-Go 继续仅管理其原有域名，没有新增电脑 DDNS 定时器。

官方来源：https://caddyserver.com/docs/automatic-https 、https://github.com/caddy-dns/alidns 。基础 Caddy 包核对官方 SHA-512；AliDNS 构建从 Caddy 官方构建服务下载，版本与模块确认通过，来源和本地 SHA-256 保存在 .devtools/caddy 的下载记录中。

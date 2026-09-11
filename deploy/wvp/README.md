# WVP / ZLMediaKit 集成

本目录保存 Fish AI Monitoring System 对 WVP-GB28181-Pro 与 ZLMediaKit 的集成说明和脱敏配置模板。

> 本目录只保存可公开提交到 Git 的模板和架构文档。生产环境中的数据库密码、Redis 密码、SIP 密码、ZLMediaKit Secret、公网 IP 等信息不得提交。

## 1. 组件职责

- **WVP-GB28181-Pro**：负责 GB28181 设备接入、SIP 信令、设备/通道管理、点播控制、流媒体服务管理和录像业务控制。
- **ZLMediaKit (ZLM)**：负责 RTP/RTSP/RTMP/HTTP 等媒体流处理、转协议、Hook 回调和 MP4/HLS 等媒体能力。
- **Fish AI Monitoring System**：消费视频/业务数据，提供 AI 分析、监控业务、API、Dashboard 和相关应用能力。
- **Fish AI Nginx**：为 Fish AI 自身服务提供反向代理。它与 WVP/ZLM 是独立组件，不应复用已占用端口。

上游项目：

- WVP-GB28181-Pro: https://github.com/648540858/wvp-GB28181-pro
- ZLMediaKit: https://github.com/ZLMediaKit/ZLMediaKit

## 2. 总体架构

```text
GB28181 Camera / NVR
        |
        | SIP signaling
        | RTP media
        v
+---------------------------+
| WVP-GB28181-Pro           |
|                           |
| Device / Channel          |
| SIP / Playback Control    |
| Recording Control         |
+-------------+-------------+
              |
              | Hook / REST API
              v
+---------------------------+
| ZLMediaKit                |
|                           |
| RTP / RTSP / RTMP         |
| HTTP / HLS / MP4          |
+-------------+-------------+
              |
              | Live stream / recording / API
              v
+---------------------------+
| Fish AI Monitoring System |
|                           |
| AI / API / Dashboard      |
+---------------------------+
```

WVP 与 ZLM 部署在同一主机或同一 host-network 容器环境时，内部 Hook/API 通信可以使用 `127.0.0.1`；SIP、SDP 和媒体流对外地址则必须根据实际网络拓扑配置为设备能够访问的地址。

## 3. 目录结构

```text
deploy/wvp/
├── README.md
├── application.yml.example
├── config.ini.example
└── runtime.env.example
```

文件说明：

| 文件 | 用途 |
| --- | --- |
| `application.yml.example` | WVP Spring Boot 脱敏配置模板 |
| `config.ini.example` | ZLMediaKit 脱敏配置模板 |
| `runtime.env.example` | WVP/ZLM 运行参数模板 |
| `README.md` | 架构、端口、部署与安全说明 |

`.example` 文件可以提交到 Git。生产配置应在部署时由环境变量、Secret 管理系统或服务器本地文件生成。

## 4. 端口规划

当前 Fish AI 集成使用/预留的主要端口如下：

| 端口 | 协议 | 组件 | 用途 |
| ---: | --- | --- | --- |
| `18080` | TCP | WVP | Web/API/Hook 服务 |
| `80` | TCP | ZLM | HTTP 媒体服务 |
| `443` | TCP | ZLM | HTTPS 媒体服务 |
| `554` | TCP | ZLM | RTSP |
| `1935` | TCP | ZLM | RTMP |
| `10000` | TCP/UDP | ZLM | RTP Proxy |
| `9000` | TCP | ZLM | Shell 管理端口 |
| `18081` | TCP | Fish AI Nginx | Fish AI 内部服务，WVP Assist 不应占用 |

> 端口表描述的是当前项目集成规划，不代表所有端口都应该暴露到公网。实际开放范围应根据安全组、防火墙、NAT 和设备网络位置决定。

如果启用 WVP Record Assist，必须选择一个未被占用的端口；当前项目中 `18081` 已保留给 Fish AI Nginx，因此不能直接作为 Record Assist 端口。

## 5. 配置关系

### WVP

`application.yml.example` 中需要在生产部署时替换的主要变量包括：

```text
CHANGE_ME_REDIS_HOST
CHANGE_ME_REDIS_PASSWORD
CHANGE_ME_DB_URL
CHANGE_ME_DB_USER
CHANGE_ME_DB_PASSWORD
CHANGE_ME_SIP_IP
CHANGE_ME_SIP_DOMAIN
CHANGE_ME_SIP_ID
CHANGE_ME_SIP_PASSWORD
CHANGE_ME_ZLM_IP
CHANGE_ME_ZLM_SECRET
CHANGE_ME_MEDIA_SDP_IP
CHANGE_ME_MEDIA_STREAM_IP
```

直播推流录像业务开关：

```yaml
user-settings:
    record-push-live: true
    auto-apply-play: false
```

`record-push-live: true` 表示 WVP 业务层允许对推流直播进行录像控制。是否最终产生 MP4 文件，还取决于 WVP 对 ZLM 的录像控制、流状态、存储路径和运行时配置。

### ZLMediaKit

`config.ini.example` 中必须替换的主要变量包括：

```text
CHANGE_ME_ZLM_SECRET
CHANGE_ME_MEDIA_SERVER_ID
CHANGE_ME_ADMIN_PARAMS
```

WVP 与 ZLM 通过 Hook 交互，例如：

```ini
on_play=http://127.0.0.1:18080/index/hook/on_play
on_publish=http://127.0.0.1:18080/index/hook/on_publish
on_record_mp4=http://127.0.0.1:18080/api/record/on_record_mp4
```

模板中的：

```ini
publishToMP4=0
```

表示不要求 ZLM 对所有发布流无条件自动录制 MP4。录像可以由 WVP 根据业务逻辑动态控制，因此不要仅为了开启 WVP 录像而直接把该值改为 `1`。

### Runtime

`runtime.env.example` 用于记录部署时的运行参数。示例：

```text
WVP_CONFIG=/opt/wvp/config/application.yml
MEDIA_IP=127.0.0.1
MEDIA_HOOK_IP=127.0.0.1
MEDIA_SDP_IP=CHANGE_ME_PUBLIC_OR_LAN_IP
SIP_IP=CHANGE_ME_PUBLIC_OR_LAN_IP
MEDIA_STREAM_IP=CHANGE_ME_PUBLIC_OR_LAN_IP
MEDIA_RECORD_ASSIST_PORT=CHANGE_ME_ASSIST_PORT
```

公网 IP、私网 IP 或 NAT 映射地址应根据摄像机/NVR 与服务器之间的实际网络可达性选择，不能机械地使用 Docker 网桥地址或服务器私网地址。

## 6. 生产部署原则

生产环境不要直接运行 `.example` 文件。推荐流程：

```text
Git 中的脱敏模板
        |
        v
部署阶段注入 Secret / IP / 数据库参数
        |
        v
服务器本地生产配置
        |
        +--> /opt/wvp/config/application.yml
        |
        +--> ZLMediaKit config.ini
        |
        v
启动 WVP / ZLMediaKit
```

生产配置至少应满足：

1. WVP 与 ZLM 使用完全一致的 ZLM Secret。
2. WVP Hook 地址能够访问 WVP `18080`。
3. SIP/SDP/媒体流地址对摄像机或 NVR 可达。
4. RTP、RTSP、RTMP 等所需端口不存在冲突。
5. 录像目录具有足够磁盘空间和正确权限。
6. 管理端口不得无必要地暴露到公网。

## 7. Git 安全规则

以下文件允许提交：

```text
deploy/wvp/README.md
deploy/wvp/application.yml.example
deploy/wvp/config.ini.example
deploy/wvp/runtime.env.example
```

以下内容禁止提交：

```text
deploy/wvp/application.yml
deploy/wvp/config.ini
deploy/wvp/runtime.env
deploy/wvp/*.bak.*
deploy/wvp/*.beforefix.*
```

同时禁止在其他文件中提交：

- Redis 密码
- 数据库密码
- SIP 注册密码
- ZLMediaKit Secret
- 管理员 Secret/Token
- 生产服务器公网 IP（除非明确决定公开）
- 私钥、证书私钥或其他凭据

提交前建议执行：

```bash
git status --short
git diff --cached
```

确认暂存区只包含预期的脱敏文件。

## 8. 与 Fish AI 的边界

本目录用于描述和配置 **WVP/ZLMediaKit 集成层**，并不在本仓库中复制或维护 WVP、ZLMediaKit 的完整上游源码。

这样可以：

- 避免重复维护第三方源码；
- 清晰记录 Fish AI 所依赖的媒体架构；
- 保存可复现的部署模板；
- 将生产 Secret 与 Git 仓库隔离；
- 后续可以独立升级 WVP/ZLM，而不污染 Fish AI 主业务代码。

如果未来需要固定上游版本，建议在部署文档、镜像标签或锁定文件中记录明确版本，而不是直接复制整个上游仓库。

## 9. 录像链路

典型录像链路如下：

```text
Camera/NVR
    |
    v
GB28181 / RTP
    |
    v
WVP -------- on_publish / recording control ------+
    |                                             |
    v                                             v
ZLMediaKit -------------------------------> MP4 recording
    |
    +------ on_record_mp4 -----------------------> WVP
```

排查“直播正常但没有录像”时，建议依次检查：

1. WVP 的 `record-push-live` 是否开启；
2. `on_publish` Hook 是否正常返回；
3. WVP 与 ZLM 的 Secret 是否一致；
4. ZLM 是否收到开始录像控制；
5. 录像目录是否可写；
6. 磁盘空间是否充足；
7. `on_record_mp4` 回调是否成功。

## 10. 注意事项

- 不要把 Markdown 链接形式写进 `config.ini`，Hook 必须是纯 URL。
- 不要在 ZLM `snap` 配置中放置 Shell 命令；截图应使用正常 FFmpeg 命令。
- 不要因为 `publishToMP4=0` 就直接判断 WVP 录像未开启，WVP 可以动态控制录像。
- 不要把 `18081` 分配给 WVP Assist；当前 Fish AI Nginx 已使用该端口。
- 修改线上 WVP/ZLM 配置前必须先备份，并确认端口、Secret 和网络拓扑。

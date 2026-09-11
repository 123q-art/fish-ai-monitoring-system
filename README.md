# Fish AI Monitoring System

基于 Raspberry Pi 边缘 AI、NCNN、FFmpeg、RTMP、FastAPI、PostgreSQL 和 Web Dashboard 的鱼类实时智能监测系统。

## 主要功能

- Raspberry Pi 边缘端鱼类检测与跟踪
- NCNN FP16 模型推理
- FFmpeg H.264 RTMP 实时视频推流
- HTTP-FLV 浏览器实时播放
- AI 检测结果 JSON 上报
- FastAPI 数据接口
- PostgreSQL 数据存储
- 分钟级、小时级历史数据聚合
- 多摄像头管理
- 实时 AI Dashboard
- systemd 服务管理

## 项目结构

```text
edge/            Raspberry Pi 边缘 AI 程序
server/          FastAPI 服务端
dashboard/       Web Dashboard
deploy/nginx/    独立 Nginx 配置
deploy/systemd/  systemd 服务文件
sql/             数据保留与聚合 SQL
docs/            项目文档
```

## 配置

生产环境的 Token、数据库密码和 SSH 私钥不应提交到 Git。

环境变量示例请参考 `.env.example`。

## 数据流

```text
Raspberry Pi -> RTMP -> ZLMediaKit/WVP -> HTTP-FLV -> Dashboard
Raspberry Pi -> AI JSON -> SSH Tunnel -> FastAPI -> PostgreSQL -> Dashboard
```

## 数据保留

- 0-7 天：完整 AI Report 与目标数据
- 7-90 天：分钟级聚合数据
- 90 天以后：删除分钟级数据
- 小时级聚合数据长期保留

## Security

- 不要提交真实 `.env`
- 不要提交 Token
- 不要提交数据库密码
- 不要提交 SSH 私钥

## WVP / ZLMediaKit 视频平台集成

本项目通过 **WVP-GB28181-Pro + ZLMediaKit** 实现 GB28181 摄像机/NVR 接入、SIP 信令、RTP 媒体传输、直播与录像能力。

- **WVP-GB28181-Pro**：设备接入、SIP 信令、通道管理、点播与录像控制
- **ZLMediaKit**：RTP / RTSP / RTMP / HLS / MP4 等媒体处理
- **Fish AI Monitoring System**：视频 AI 分析、监控 API 与 Dashboard

详细架构、端口规划和脱敏配置模板见：[`deploy/wvp/README.md`](deploy/wvp/README.md)


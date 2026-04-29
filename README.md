# Personal Automation Agent

这是一个轻量个人自动化 Agent，适合写在“AI 驱动构建成果”里。

它包含：

- 网页库存 / 价格 / 关键词状态监控
- VPS systemd 服务状态检查
- journalctl 日志摘要
- Telegram 推送
- QQ / NapCat / OneBot HTTP 推送
- FastAPI HTTP 指令入口
- 可选 OpenAI 摘要

## 1. 安装

```bash
cd personal-automation-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. 配置

```bash
cp .env.example .env
cp monitors.example.json monitors.json
nano .env
nano monitors.json
```

`monitors.json` 示例：

```json
[
  {
    "name": "VMRack 三网精品云服务器 L3.VPS.1C1G.Base",
    "url": "https://www.vmrack.net/zh-CN/activity/2026-spring",
    "target": "L3.VPS.1C1G.Base",
    "available_keywords": ["立即购买", "购买", "有货", "Order", "In Stock"],
    "unavailable_keywords": ["无货", "缺货", "售罄", "Out of Stock", "Sold Out"]
  }
]
```

## 3. 启动

```bash
python agent.py
```

健康检查：

```bash
curl http://127.0.0.1:18790/health
```

## 4. 常用指令

检查网页：

```bash
curl -X POST http://127.0.0.1:18790/command \
  -H "Content-Type: application/json" \
  -d '{"text":"检查网页"}'
```

查看 Agent 状态：

```bash
curl -X POST http://127.0.0.1:18790/command \
  -H "Content-Type: application/json" \
  -d '{"text":"状态"}'
```

检查服务：

```bash
curl -X POST http://127.0.0.1:18790/command \
  -H "Content-Type: application/json" \
  -d '{"text":"检查服务 openclaw"}'
```

查看日志摘要：

```bash
curl -X POST http://127.0.0.1:18790/command \
  -H "Content-Type: application/json" \
  -d '{"text":"日志 openclaw 100"}'
```

推送结果到 Telegram / OneBot：

```bash
curl -X POST http://127.0.0.1:18790/command \
  -H "Content-Type: application/json" \
  -d '{"text":"检查网页", "push": true}'
```

## 5. OneBot / NapCat 接入

在 NapCat / OneBot 的 HTTP 上报地址中填：

```txt
http://你的服务器IP:18790/onebot
```

群聊中发送：

```txt
/agent 状态
/agent 检查网页
/agent 检查服务 openclaw
/agent 日志 nginx 80
```

## 6. systemd 后台运行

复制示例服务文件：

```bash
sudo cp systemd/personal-agent.service.example /etc/systemd/system/personal-agent.service
sudo nano /etc/systemd/system/personal-agent.service
```

把里面的路径改成你的实际路径，例如：

```ini
WorkingDirectory=/root/personal-automation-agent
ExecStart=/root/personal-automation-agent/.venv/bin/python /root/personal-automation-agent/agent.py
```

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable personal-agent
sudo systemctl start personal-agent
sudo systemctl status personal-agent
```

看日志：

```bash
journalctl -u personal-agent -f
```

## 7. 安全说明

这个项目默认不执行任意 shell 命令。

服务检查只允许访问 `.env` 中 `ALLOWED_SERVICES` 白名单内的 systemd 服务，例如：

```env
ALLOWED_SERVICES=openclaw,nginx,docker,ssh,personal-agent
```

不要把 `/command` 和 `/onebot` 直接暴露到公网。至少加反代鉴权、防火墙或内网访问限制。

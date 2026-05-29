# feishu-relay-bot

飞书消息通道 AI 中继节点。

> **本仓即 `feishu-relay-bot` 主仓（v3 起）**
> 老的独立仓库 `github.com/Zenwh/feishu-relay-bot`（v0.x / v0.3 系列）已归档（archived），
> 不再接受 issue / PR。所有新功能、bug 修复都在这里（`ModelProxy/relay_bot/`）继续。
>
> - 历史 README / docker / examples 已保留在 git 历史里
> - v3 与 v0.3 协议不兼容：v3 协议在 `docs/` 里，`install.sh` 同目录
> - 维护方：内网 ModelProxy 团队

## 安装

```bash
pip install feishu_relay_bot-*.whl
```

## 使用

```bash
feishu-relay-bot run --config config.yaml
```

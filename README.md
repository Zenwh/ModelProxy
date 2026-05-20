# ModelProxy

> LLM API 中转 & 渠道分发平台 - 产品 PRD & 高保真 Demo

---

## 仓库结构

```
ModelProxy/
├── README.md                       # 本文件
├── API.md                          # 已有 API 文档（不动）
├── 资源池切分方案.md                  # 已有底层切分方案（不动）
├── PRD/                            # 产品 PRD 文档
│   ├── 00-Overview.md              #   产品总览 / 架构 / 角色 / 术语
│   ├── 01-Resource-Management.md   #   模型 / 供应商管理
│   ├── 02-Channel-Management.md    #   渠道管理（核心）
│   ├── 03-Channel-Console.md       #   渠道运营后台
│   ├── 04-Reconciliation.md        #   对账中心（双轨）
│   ├── 05-Data-Model.md            #   数据模型 / API
│   └── 06-Demo-Plan.md             #   Demo 页面清单
└── demo/                           # 高保真 Demo（HTML/CSS/JS）
    ├── index.html                  #   入口门厅
    ├── platform-admin/             #   平台运营后台（深色主题）
    └── channel-console/            #   渠道运营后台（浅色主题）
```

---

## 快速开始

### 阅读 PRD

按顺序看 `PRD/00 → PRD/06`，每篇约 5-10 分钟。

最关键的两篇：

- [02-Channel-Management.md](./PRD/02-Channel-Management.md) - 渠道管理是产品核心
- [04-Reconciliation.md](./PRD/04-Reconciliation.md) - 双轨对账模型

### 运行 Demo

任意 HTTP 服务器即可，例如：

```bash
cd ModelProxy
python3 -m http.server 8765
```

然后浏览器打开 <http://localhost:8765/demo/index.html>。

---

## 演示路径

| 路径 | 时长 | 内容 |
|---|---|---|
| A · 平台运营完整链路 | 5 min | Provider → Model 详情 → 渠道详情 (Fallback / 预算 / ACL) → 对账中心 |
| B · 渠道运营自助经营 | 3 min | Dashboard → 新建 Key → 调用日志 (含 Fallback 路径) → 账单 |
| C · 双轨对账故事 | 2 min | 供应商对账 (差异分析) → 渠道对账 → 利润分析 |

---

## 关键设计决策

| 决策 | 选型 |
|---|---|
| 技术底座 | 基于 New-API 二次开发 |
| 渠道隔离 | 逻辑隔离 + 独立运营后台（子域名 / 路径） |
| Fallback 覆盖 | 故障 + 限流 + 预算耗尽 全场景 |
| 对账维度 | 双轨：供应商对账 (应付) + 渠道对账 (应收) |
| 单价 | 平台侧 / 渠道侧分离，独立版本化 |

---

## 角色矩阵

| 角色 | 工作台 | 核心权限 |
|---|---|---|
| 平台超管 / 运营 | Platform Admin | 管 Provider / Model / Channel / 对账 |
| 平台财务 | Platform Admin（财务视图） | 出账 / 对账 / 退款 |
| 渠道管理员 / 运营 | Channel Console | Key / 用量 / 账单 / 子项目 |
| 终端开发者 | Channel Console（弱化） | 自己 Key / 用量 |

---

## Demo 页面清单

### Platform Admin (12 页)

- 总览 Dashboard
- 供应商列表 / 详情
- 模型列表 / 详情（含通道矩阵、单价版本化）
- 渠道列表 / 详情（7 个 Tab：基础信息 / 模型池 / Fallback / 预算 / ACL / 成员 / 用量）
- 对账总览
- 供应商对账（差异分析）
- 渠道对账（争议处理）
- 利润分析（按渠道 / 模型 / Provider 三维度）

### Channel Console (10 页)

- 概览 Dashboard
- Key 管理（含限额、IP 白名单、协议白名单）
- 子项目
- 用量统计（多维透视）
- 模型市场（含 Fallback 链展示）
- 预算 / 账单（含历史使用率）
- 调用日志（含 Fallback 路径追溯）
- Playground（三协议切换 + 等效代码生成）
- 团队管理
- 设置（含自动接入文档）

---

## 里程碑

| 阶段 | 周期 | 内容 |
|---|---|---|
| M1 | 2 周 | Provider / Model / Channel CRUD + Gateway 鉴权 |
| M2 | 4 周 | 渠道后台（New-API 改造） |
| M3 | 4 周 | Fallback 引擎 + 预算 + ACL |
| M4 | 3 周 | 对账中心（双轨） |
| M5 | 持续 | 智能调度 + SLA + 独享通道 |

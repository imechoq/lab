# mcd-app · 麦当劳点餐助手

一个围绕「选门店 → 看菜单 → 定制餐品 → 管预算 → 预览下单」流程的点餐原型 App。

界面为移动端优先的单页应用，同时适配桌面宽屏。**所有操作均为只读浏览与本地购物车计算，不会真实下单。**

## 它解决什么问题

点麦当劳这类 App 时，痛点往往不在「找不到想吃的」，而在：

- 菜单太长、翻不到自己那家店的实际价格与优惠
- 多人聚餐时凑单难算：谁点什么、总共多少钱、有没有满减可用
- 想控预算但没地方看「人均」和「总额」的关系
- 定制项（加料、可选搭配）藏在二级页面，容易漏看

这个 App 把这几件事放在一条主流程里：先锁定门店与场景，菜单按该店真实数据渲染，购物车实时算人均与总额，提交前必须经过订单预览。

## 快速开始（不需要任何凭据）

仓库自带一份菜单数据快照，**克隆下来就能看**：

```bash
git clone https://github.com/imechoq/lab.git
cd lab/projects/mcd-app
python3 -m http.server 8777
```

打开 http://127.0.0.1:8777/app/ 即可。

> 直接双击 `app/index.html` 也能打开，但用 HTTP 服务访问更可靠（部分浏览器会限制 `file://` 下的数据加载）。

快照包含 3 家上海门店（七宝领展、虹桥万创中心、融信上坤中心）的完整菜单、分类、价格、优惠规则，以及 158 条营养数据。

## 开启实时数据（可选）

想拿到**实时**菜单、门店、优惠，需要连接麦当劳 MCP Server。这需要你自己的 Bearer token，**本仓库不包含任何凭据**。

### 1. 准备配置文件

创建 `mcp.json`：

```json
{
  "mcpServers": {
    "mcd-mcp": {
      "url": "https://mcp.mcd.cn",
      "headers": {
        "Authorization": "Bearer <你的 token>"
      }
    }
  }
}
```

token 在 open.mcd.cn/mcp 申请，需绑定个人会员账号。

### 2. 启动带凭据的本地代理

```bash
# 方式 A：环境变量指定（推荐，配置不入库）
MCD_MCP_CONFIG=/绝对路径/mcp.json python3 tools/mcd-server.py

# 方式 B：放到项目根目录的 mcp.json（记得别提交，见 .gitignore）
python3 tools/mcd-server.py
```

代理默认监听 http://127.0.0.1:8777/ ，它会同时提供静态页面和 `/api/*` 实时接口。

### 3. 确认连上了

```bash
curl http://127.0.0.1:8777/api/health
```

```json
{"ok": true, "service": "mcd-local-proxy", "mcp": "https://mcp.mcd.cn", "credentialed": true, "error": null}
```

`credentialed: true` 表示凭据已加载。**没有凭据时服务依然能启动**，页面会用静态快照渲染，只有实时接口返回一段可读的提示。

### 为什么要有这个代理

MCP Server 需要个人 Bearer token。如果把 token 写进前端 JS，任何打开页面的人都能从源码里看到你的 token。代理让 token 只留在本地进程里，浏览器始终只跟 `127.0.0.1` 通信。

## 目录结构

```
mcd-app/
├── app/                    # 前端（可直接部署的静态资源）
│   ├── index.html          # 主应用，单文件包含全部样式与逻辑
│   ├── flow.html           # 流程说明页
│   └── menu-data.js        # 菜单数据（由 tools/build-menu-data.py 生成）
├── data/                   # 数据源
│   ├── menu-snapshot.json  # MCP 原始快照
│   └── group-order-plans.json
└── tools/
    ├── mcd-server.py       # 本地只读代理（凭据不出进程）
    ├── collect-menu.py     # 抓取门店菜单快照
    ├── collect-details.py  # 补充单品详情
    ├── build-menu-data.py  # 快照 → menu-data.js
    └── test-app.js         # Playwright 端到端自测
```

## 重新生成数据

```bash
# 1. 抓取最新菜单（需凭据）
MCD_MCP_CONFIG=/path/to/mcp.json python3 tools/collect-menu.py

# 2. 补充商品详情
MCD_MCP_CONFIG=/path/to/mcp.json python3 tools/collect-details.py

# 3. 生成前端数据文件
python3 tools/build-menu-data.py
```

第 3 步会重写 `app/menu-data.js`，输出各门店的商品数与分类数供核对。

## 端到端自测

`file://` 协议下浏览器无法 fetch 相对路径的 `/api/*`，所以推荐用 HTTP 服务跑：

```bash
npm i -D playwright

# 终端 1：起服务
python3 tools/mcd-server.py --port 8777

# 终端 2：跑自测
MCD_APP_URL=http://127.0.0.1:8777/index.html node tools/test-app.js
```

脚本会驱动真实浏览器走完预算计算、菜单筛选、商品定制、套餐选规格、团餐折扣、门店切换、偏好联动、移动端适配等流程，并输出结构化结果（`errors` 数组应为空）。

若 playwright 装在别处，用 `PLAYWRIGHT_MODULE=/path/to/playwright` 指定。省略 `MCD_APP_URL` 则退回 `file://` 模式，此时接口调用会报错但界面渲染仍可验证。

## 设计约束

- **不做真实下单。** 任何建单动作都必须经过明确的二次确认，界面不制造「已下单」或「优惠已生效」的错觉。
- **数据缺失不伪造。** 图片、价格、优惠缺失时显示明确的加载或缺省状态。
- **金额以分为单位。** 接口返回的是元字符串，界面统一换算为分再格式化，避免浮点误差。
- **单人快点与多人组餐分流**，但共享同一套购物车、偏好与预算模型。

## 已知限制

- 快照数据是 2026-10-24 抓取的，价格与优惠可能已变化，实时使用请开代理。
- 团餐优惠规则依赖 `query-promotions` 的返回结构，麦当劳调整规则后可能需要更新解析逻辑。
- 门店搜索依赖 MCP 的地址解析，地址写法不同可能导致查不到门店。

## 相关文档

- 麦当劳点餐体验调研与页面规划
- 资料库复用分析：用餐赛道策略

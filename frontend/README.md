# Strategy Engine 前端（Stage 17）

第一版执行器前端：**只做展示、输入与调用本地 API**。
策略逻辑、订单逻辑、成交逻辑、持仓计算全部在 Python 后端，前端一条都没有实现。

```
浏览器 (React / Vite, 默认 http://127.0.0.1:5173)
      │  HTTP（地址来自 VITE_API_BASE_URL，默认 http://127.0.0.1:8000）
      ▼
FastAPI（api/app.py）
      ▼
StrategyRunner / ExecutionEngine → SQLite（本地模拟执行器，未连接任何真实交易所）
```

## 文件结构

```
frontend/
├── index.html                 页面骨架
├── vite.config.js             Vite + React 插件 +（vitest 使用 node 环境）
├── .env.example               VITE_API_BASE_URL 示例
├── package.json               依赖与脚本（dev / build / preview / test；图表依赖 lightweight-charts）
├── src/
│   ├── main.jsx               挂载入口
│   ├── App.jsx                状态编排：加载数据、调用 API、分发结果
│   ├── config.js              唯一的 API 地址来源（VITE_API_BASE_URL）
│   ├── format.js              纯展示用格式化（不做任何业务计算）
│   ├── styles.css             简单样式
│   ├── chart/klines.js         K 线↔信号纯计算（时间换算 / 蜡烛构建 / 按 timestamp 匹配标记）
│   ├── api/client.js          唯一的 HTTP 出口（含错误翻译）
│   └── components/            HealthBar / StrategyRunnerPanel / KlineChartPanel / SignalsPanel /
│                              ExecutionPanel / PositionsPanel / OrdersPanel / TradesPanel / ErrorBanner
└── tests/                     api-client.test.js、klines.test.js、components.test.jsx（最小化）
```

## 命令（在 frontend/ 目录下）

```powershell
npm install        # 安装依赖
npm run dev        # 开发服务器 http://127.0.0.1:5173
npm run build      # 产物输出到 frontend/dist
npm run test       # 最小化前端测试（vitest run）
```

后端需要同时在项目根目录运行：

```powershell
.venv\Scripts\python.exe -m uvicorn api.app:app --host 127.0.0.1 --port 8000
```

浏览器打开 **http://127.0.0.1:5173**。

> 当前前端只操作本地模拟执行器；页面上的「模拟执行」是模拟成交，**不会下单到任何真实交易所**。

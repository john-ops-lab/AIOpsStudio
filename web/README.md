# EasyOps 工作台前端

React/Vite 界面使用简体中文；依赖版本以锁文件为准。`server.mjs` 提供同源入口，Python API、PostgreSQL 和执行器在 Mac 本机运行。

## 启动与构建

```sh
npm ci --legacy-peer-deps --ignore-scripts
npm run build
npm start
```

部署参数、状态目录与启动顺序见[仓库 README](../README.md)。开发时先启动同源 Node 入口，再将 Vite API 代理指向它；下例使用通用默认端口：

```sh
EASYOPS_API_URL=http://127.0.0.1:3000 npm run dev
```

浏览器通过 `/api` 获取数据，凭据由后端保存。开发代理应指向 Node 同源入口。

## 页面与数据边界

- 运维工作台：Chat、会话历史、固定输入框、上下文编辑、Skill/模型选择、用户附件、多轮追问和分析画布。
- 告警：展示源接口实际返回的事件及限制，选择后携带对象与固定窗口进入 Chat。
- Skill/MCP/模型：草稿、版本、依赖、schema、只读确认、试调和只写凭据。
- 调用与概览：展示服务端记录；未知使用 `—` 或明确说明，不补零、不预置业务结果。

账号与配置管理按服务端角色展示，私有历史按当前用户归属隔离。`src/api.ts` 集中处理会话、错误和修订；执行动作附幂等 ID。持久 SSE 与只读轮询恢复同一轮次，刷新不重新提交。CopilotKit 和业务 REST 共享同一执行器，终态追问创建关联新轮次。

Markdown 不执行 HTML，不自动加载远端图片。登录口令与凭据不写入 localStorage 或客户端持久状态。公开图为完全新制的通用演示素材，不是运行截图。

## 验证

`npm run build` 包含 TypeScript 检查。界面行为、协议测试和真实业务分析分别记录；构建不代替交互或业务验收。历史结果见[前端 QA](QA.md)及[公开验证摘要](../docs/validation/2026-10-22.md)。

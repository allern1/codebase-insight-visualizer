# Codebase Insight Visualizer（代码库认知可视化器）

> **English** · [README_EN.md](./README_EN.md)

> **宏观可见（地图），微观可触（跳转），决策可溯（故事），增量可控（缓存）。**

将"死代码"转化为"活知识"：扫描真实代码库，确定性提取事实底座（依赖图），
AI 只补充语义便签（设计权衡 / 备选方案 / 已知风险 / 运行流程），
最终交付一个**离线单文件、三栏文档式**的交互式 HTML —— 金色节点 = AI 生成的代码，
灰色虚线 = 已删除的历史存档，每条叙述都可点击跳转到 IDE 的真实坐标。

## 预览（真实产物截图）

| 认知导航（三栏文档式） | 全景图谱（流程高亮） |
|---|---|
| ![认知导航视图](docs/assets/dashboard-doc-view.png) | ![全景图谱视图](docs/assets/dashboard-graph-view.png) |

- **认知导航**：左侧模块树（🔥=AI 生成 / 🗑=已删除存档 / ↪=移动）、中间节点详情（设计权衡 / 备选方案 / 已知风险 / 代码预览 / 符号索引）、右侧 Depends on / Used by 速览。
- **全景图谱**：ECharts 力导向图，金色=AI 生成、灰色=已删除（虚线边）；「▶ 运行流程」选择后链路节点按执行顺序点亮，其余淡化。

## 核心哲学

```
脚本给事实，AI 给判断 —— 地图错了是脚本的锅（可修），便签错了是 AI 的幻觉（可丢弃），互不污染。
```

- **事实底座（scanner.py）**：文件哈希、符号、import 边 → 全部确定性提取，AI 无权修改
- **语义增强（instructions.md）**：AI 只填 `design_reason / alternatives / known_risks / flows / soul_questions`
- **白名单缝合（manifest_builder.py）**：越界字段 / 编造节点 / 无背书边 → 静默丢弃 + 报告
- **交付契约**：validate → 原子 deliver，**任何失败保留上一版产物**（借鉴 archify）

## 快速开始（端到端）

```bash
# 1. 扫描事实底座（增量缓存自动对比，第二次运行只处理变更）
python scripts/scanner.py --project <你的项目路径> --config config.yaml

# 2. （可选）AI 语义增强：将 fact_graph.json 交给 LLM，按 instructions.md 输出
#    （privacy_mode: strict 时只发送函数签名+注释）

# 3. 数据缝合与质检
python scripts/manifest_builder.py \
    --fact-graph <fact_graph.json> --ai-output <ai_output.json> \
    --out <final_manifest.json> [--old-manifest <上一版>]

# 4. 构建可交付 dashboard（离线单文件）
python scripts/build.py --manifest <final_manifest.json> \
    --out dashboard.html [--echarts echarts.min.js]   # 内网用 --echarts 内联
```

**验证示例**（扫本项目的 `scripts/`）：

```bash
python scripts/scanner.py --project scripts
python scripts/build.py --manifest .../final_manifest.json --out demo/dashboard_real.html
```

## 数据契约（`schemas/final_manifest.schema.json`）

- `schema_version: 1` + `manifest_type: "codebase-insight"`（const 锁定）
- `repository {url, revision(40位sha), root}` —— 仓库证据绑定，非 git 约定全 0
- `nodes[]`：`id/name/type/file_path/start_line/hash/symbols` 为**事实**；
  `design_reason/alternatives/known_risks/code_snippet/story` 为**语义**
- `status`: `active | deleted | moved`（deleted 保留为历史存档，灰虚线展示）
- `edges[]`：`factual: true` 事实边（scanner 产出）；`factual: false` 语义边
  仅限 `动态调用 | 消息 | 接口实现 | 配置引用` 且端点必须已存在
- `flows[]`：运行流程叙事；相邻步骤无事实边背书时必须写 `gap_hint`
- `soul_questions[]`：由真实 risk 生成，2-5 条

## 目录结构

```
codebase-insight-visualizer/
├── instructions.md            # AI 语义层系统提示词（铁律 + 白名单 + 失败协议）
├── config.yaml                # 用户配置（忽略规则/缓存/隐私模式/IDE 协议）
├── manifest.json              # Skill 装配信息
├── schemas/
│   └── final_manifest.schema.json
├── scripts/
│   ├── scanner.py             # CLI 入口：扫描/忽略/边解析/缓存读写/git 证据
│   ├── fingerprint.py         # SHA-256 + 符号骨架指纹 + containment
│   ├── lifecycle.py           # delta 比较器（五态）+ 快照轮转（保留 30 份）
│   ├── manifest_builder.py    # 白名单过滤/坐标校验/增量合并/三重判定/原子交付
│   ├── build.py               # manifest 注入 + ECharts 内联 + 原子导出
│   └── plugins/               # python/js/go/java 正则插件（未知语言降级文件级节点）
├── demo/
│   ├── dashboard_proto.html   # 模板（docsify 三栏文档式 + 图例过滤 + 流程高亮）
│   └── dashboard_real.html    # 端到端演练产物（真实 manifest 注入）
└── tests/                     # 26 项 unittest（零依赖）
```

## 作为 Skill 安装（Claude Code / Codex CLI）

本仓库根目录的 `SKILL.md` 是标准 **Agent Skills** 格式（frontmatter `name`/`description` + 可移植相对路径指令），运行时（`scripts/` + `schemas/` + `instructions.md` + `config.yaml`）与它同目录打包，零依赖 Python 3.11+。

**方式一：skills CLI（推荐）**

```bash
# Claude Code（全局）
npx skills add allern1/codebase-insight-visualizer --agent claude-code --global
# Codex CLI
npx skills add allern1/codebase-insight-visualizer --agent codex --global
```

**方式二：手动复制**（把本仓库内容放入宿主的 skills 目录，保持 SKILL.md 与 scripts/ 同级）

```
~/.claude/skills/codebase-insight-visualizer/   # Claude Code
~/.codex/skills/codebase-insight-visualizer/    # Codex CLI
```

安装后，向 agent 说"分析当前项目结构/依赖/运行流程"或"看看最近 AI 改动的代码"，即会按 SKILL.md 的四步流水线执行（首次全量，二次增量 Token 降 95%）；内网/离线环境构建时加 `--echarts echarts.min.js` 内联。

## 测试

```bash
cd codebase-insight-visualizer
python -m unittest discover -s tests -p "test_*.py"
```

覆盖：插件提取（含相对导入/as 别名/符号行号）、schema 契约、生命周期五态与快照轮转、
scanner 端到端（删除/移动/新增/修改 + 根目录产物自排除）、缝合层（编造节点/无背书边过滤、
降级模式、增量保留、三重判定、交付保旧）、build 注入/内联/保旧、时间机器快照注入。

## 已知限制（诚实声明）

- 正则解析存在动态 import / 注释字符串里的误检漏检；未知语言 → 文件级节点（edges 留空）
- `is_ai_generated` 判定为**四重证据**（git 状态 / 时间窗 / 用户确认 --ai-files / AI 会话日志审计 `agent_evidence.py`），满足其二；**复制粘贴无法捕获**（生态共识，仅 --ai-files 可补）；活跃会话未归档时日志通道不可见
- 符号行号已到类/函数粒度（非表达式级）；`symbols` 为 `[{name, line}]`，点击符号精确跳转 IDE
- `build.py --echarts` 需要本地 echarts.min.js（运行 `npm i echarts` 或官网下载）

## 路线图

1. ✅ 符号级行号（`symbols` 附带 line）→ 点击跳转精确到函数
2. ✅ 时间机器 UI：快照列表 + 差异对比视图（绿新增/红删除/黄移动）
3. ⬜ tree-sitter 可选增强（探测可用时自动替换正则插件）
4. ⬜ `validator.py` 黄金测试集（小型 Django / Go 微服务 / 混合项目）

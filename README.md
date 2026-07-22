# SocialGraph — 基于聊天记录的社会网络构建与分析

从微信聊天记录中自动构建社交关系网络，支持 PageRank 分析、社区发现、关系强度量化、时序演化，以及网络感知的对话仿真。

## 架构

```
WeChat 导出 (WeFlow JSON / TXT)
    │
    ▼
chat_parser.py ─── 解析 → 标准化消息
    │
    ▼
neo4j_loader.py ─── 导入 Neo4j
    │
    ▼
batch_enrich_all.py ─── 情绪/人物/角色标注 (DeepSeek API)
    │
    ▼
identity_resolver.py ─── 实体去重 (规则 + 图结构 + LLM)
    │
    ▼
relationship_analyzer.py ─── 关系强度量化
    │
    ▼
graph_analytics.py ─── PageRank / Louvain 社区发现 (GDS)
    │
    ▼
temporal_evolution.py ─── 关系生命周期 + 月度热力图
```

## 快速开始

### 环境

- Python 3.9+
- Neo4j 2025.06+ (需 GDS 插件用于图算法)
- DeepSeek API Key

```bash
pip install neo4j openai flask
```

### 使用

1. 用 WeFlow 导出微信聊天，放入 `聊天记录/texts/`
2. 设置 `DEEPSEEK_API_KEY`

```bash
# 一键重建全流程
python tools/rebuild_all.py

# 或分步
python tools/chat_parser.py --all --dir 聊天记录/texts/
python tools/neo4j_loader.py --file all_merged.jsonl --password <pwd>
python tools/batch_enrich_all.py --all
python tools/identity_resolver.py
python tools/relationship_analyzer.py
python tools/graph_analytics.py --all
python tools/temporal_evolution.py
```

### 对话仿真

部署 SKILL.md 为 Claude Code Skill，社会网络数据自动注入对话上下文。

## 工具清单

| 脚本 | 功能 |
|------|------|
| `chat_parser.py` | WeFlow JSON / TXT → 标准化消息 |
| `neo4j_loader.py` | 批量导入 Neo4j |
| `batch_enrich_all.py` | 情绪 + 对话角色 + 人物标注 |
| `context_enrich.py` | 上下文感知标注 |
| `identity_resolver.py` | 跨聊天人物去重 |
| `relationship_analyzer.py` | Person-Person 关系强度 |
| `graph_analytics.py` | GDS PageRank + Louvain |
| `temporal_evolution.py` | 月度热力图 + 生命周期 |
| `graph_api.py` | Flask API (语义搜索 + 社会网络查询) |
| `style_scorer.py` | 仿真回复风格检查 |
| `eval_framework.py` | 五维量化评估 |
| `ablation_test.py` | 组件贡献度消融实验 |
| `annotation_reliability.py` | 标注信度检验 (Cohen's κ) |
| `rebuild_all.py` | 一键重建全流程 |

## 案例结果

在 55 个聊天 (44 私聊 + 10 群聊 + 1 TXT)、260 人、60,000 条消息的数据集上：

- **PageRank**: 发现群聊中心人物和私聊中心人物的差异
- **Louvain**: 16 个自然社区 (190 人大圈 + 53 人中圈 + 14 个微型组)
- **关系强度**: 对数缩放的 4–150 谱系
- **时序分析**: 精确定位关系断裂时间点

## 许可证

MIT

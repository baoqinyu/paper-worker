# paper_worker：本地论文写作 agent

用 Claude API（扣 Console 里的 API 额度，不占 Claude Code 订阅用量）在本地跑一个能读写文件的 agent，专门做两件事：

1. **文献**：检索参考论文 → 阅读/分析 → 写**面向论文 Introduction** 的综述段落（漏斗结构：背景 → 问题 → 现有方法 → 局限 → 研究空白），并生成 `references.bib` 和文献卡片。
2. **数据**：读取实验数据 → 在本地用 Python 分析、作图 → 写 Results 段落。所有数字都来自本地实际运行的输出。

## 1. 准备

```bash
pip install -r requirements.txt
```

在 https://platform.claude.com 创建 API Key（要建在领取了额度的那个 organization 里），然后**自己**在 PowerShell 里设置成用户环境变量（设置后重开终端生效）：

```powershell
setx ANTHROPIC_API_KEY "你的key"
```

## 2. 推荐的论文文件夹结构

```
my_paper/                 ← --workspace，agent 只能读写这里面的文件
  papers/                 ← 你已有的 PDF
  data/                   ← 实验数据 csv/xlsx
  drafts/                 ← agent 写的段落（intro_related_work.md, results.md）
  literature/             ← notes.md（文献卡片）, references.bib
  analysis/               ← 分析脚本、results_summary.md
  figures/                ← 图（png + pdf）
  .paper_worker/          ← 会话记录、临时脚本（自动生成）
```

## 3. 用法

**交互模式**（自己用）：

```bash
python paper_worker.py chat -w E:/my_paper
```

- 多行输入：单独一行 `"""` 开始，再单独一行 `"""` 结束
- `/cost` 查看花费，`/exit` 退出；退出时会打印会话 ID，下次加 `--resume <ID>` 接着聊
- 模型要在本地运行 Python 时，会先把代码给你看，你确认后才运行

**单任务模式**（给 Claude Code 或脚本调用）：

```bash
python paper_worker.py run -w E:/my_paper --json --task "围绕……写 Introduction 的相关工作部分"
```

stdout 只输出最终结果（加 `--json` 时是 JSON：`status / session_id / final / files_written / usage`），过程日志都写到 stderr。

### 任务示例

- 文献综述：
  > 我的论文研究 X 方法在 Y 场景下的 Z 问题，草稿摘要在 drafts/abstract.md。请检索相关文献（重点放在近 5 年，同时包括奠基性工作），写 Introduction 里的相关工作综述，约 4–5 段，并生成 bib。papers/ 里的 PDF 是我已经读过的核心文献，请优先使用。
- 数据分析：
  > data/exp1.csv 是三种策略在 5 个随机种子下的结果。先描述数据，再比较三种策略的平均回报（给出均值 ± 标准差和合适的显著性检验），画箱线图，最后写 results 段落。

### 常用参数

| 参数 | 说明 |
|---|---|
| `--model` | 默认 `claude-opus-5-5`（$4/$20 每百万 token）。想省钱可以用 `claude-sonnet-5-5`（$2/$10），或者用 `claude-haiku-5-5`（$0.1/$0.5，适合简单任务） |
| `--effort` | `low/medium/high/xhigh/max`，默认 `medium`。写正式综述可以用 `high`，但更花钱 |
| `--max-cost` | 单次进程的估算花费上限（美元），默认 5，设为 0 表示不限 |
| `--max-steps` | 单轮最多调用模型的次数，默认 60 |
| `--approve-python` | `ask`（默认，非交互环境下等同 `no`）/ `yes` / `no` |
| `--resume ID` | 继续之前的会话 |
| `--show-thinking` | 显示思考摘要 |

也可以用环境变量 `PAPER_WORKER_MODEL`、`PAPER_WORKER_EFFORT` 修改默认值。

## 4. 工具与安全

- 本地工具：`list_files`、`read_file`、`read_pdf`（PyMuPDF，带页码标记）、`write_file`（默认不覆盖已有文件）、`edit_file`、`search_papers`（OpenAlex，免费、不用 key，返回真实的 DOI 和摘要）、`run_python`（在 workspace 里运行，不会把 API Key 传给子进程）。
- 服务端工具：`web_search`（每次约 $0.01，另计）、`web_fetch`（读开放获取的全文）。
- 所有文件路径都限制在 workspace 内。`run_python` 能执行任意代码，所以默认每次运行前都要你确认。
- 系统提示词要求：不编造文献、不编造数字，拿不准的地方标 `[CHECK: ...]` 或 `[TODO: ...]`。但模型仍可能出错，**引用和数字请人工核对**。

## 5. 花费

- 命令行显示的花费是按公开价格估算的，以 Console 账单为准。
- 粗略量级：一次 Introduction 综述任务（检索 + 读 20 篇左右的摘要 + 几篇全文 + 写作）用 Opus，大概 $1–4；数据分析任务通常更便宜。
- 已开启 prompt caching，同一会话里重复的上下文会便宜很多。
- 已开启 `fallbacks: "default"`：Opus 5.5 因安全分类器拒答时，服务端会自动换一个模型重试（研究里涉及生物、网络安全等话题时可能触发）。

## 6. License

MIT，见 [LICENSE](LICENSE)。

## 7. 和 Claude Code 配合

把 `claude_code_skill/paper-worker/` 复制到论文项目的 `.claude/skills/` 下（或者 `~/.claude/skills/`）。之后在 Claude Code 里说"让 paper worker 去做文献综述"，Claude Code 就会用 `run --json` 调用它，读取它写出的文件并继续处理。这部分工作扣的是 API 额度。

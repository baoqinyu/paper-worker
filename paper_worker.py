#!/usr/bin/env python
"""paper_worker: 本地论文写作 agent（Claude API + 本地读写工具）

两类主要任务：
  1. 文献：找参考论文 -> 读/分析 -> 写面向 Introduction 的文献综述段落 + BibTeX
  2. 数据：读取实验数据 -> 本地 Python 分析/作图 -> 写 Results/讨论段落

用法（详见 README.md）：
  python paper_worker.py chat --workspace E:/my_paper
  python paper_worker.py run  --workspace E:/my_paper --task "..." [--json] [--resume ID]

鉴权：从环境变量 ANTHROPIC_API_KEY 读取（在 platform.claude.com 创建的 Key），代码里不保存 Key。
"""
from __future__ import annotations

import argparse
import copy
import datetime as _dt
import json
import os
import subprocess
import sys
import textwrap
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import anthropic

# ---------------------------------------------------------------- 配置
DEFAULT_MODEL = os.environ.get("PAPER_WORKER_MODEL", "claude-opus-5-5")
DEFAULT_EFFORT = os.environ.get("PAPER_WORKER_EFFORT", "medium")
MAX_TOKENS = 64000  # 流式请求，给模型足够空间
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# 美元 / 百万 token：(input, output, cache_read, cache_write_5min)。用于估算花费，以官方账单为准。
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10, 2.5),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}
WEB_SEARCH_PRICE = 10.0 / 1000  # 每次搜索

MAX_TOOL_OUTPUT_CHARS = 60_000

# ---------------------------------------------------------------- 系统提示词（整个会话固定不变，便于缓存）
SYSTEM_PROMPT = """\
You are paper_worker, a research-writing agent running on the user's local machine. You help a researcher write an academic paper. You work inside one workspace folder and can only read and write files there through your tools.

Communicate with the user in Chinese (concise progress notes and final summaries). Write manuscript text (introduction, results, etc.) in English unless the task says otherwise.

# Tools you have
- list_files / read_file / read_pdf / write_file / edit_file: local workspace files (paths are relative to the workspace).
- search_papers: scholarly metadata search (OpenAlex). Returns real bibliographic records with DOI, year, venue, citation count, abstract.
- web_search / web_fetch: general web. Use web_fetch to read open-access full text or landing pages of papers you found.
- run_python: run Python locally in the workspace (pandas, numpy, scipy, matplotlib available). The user may need to approve each run.

# Non-negotiable integrity rules
- Never invent a reference. Every paper you cite must come from a search_papers result, a web page you actually fetched, or a PDF in the workspace. Record its DOI or URL.
- Never claim a paper says something you have not seen. If you only read the abstract, base claims on the abstract and say so in the notes.
- Never invent numbers. Every number in a results paragraph must come from run_python output in this session (or a file you read). If a number is missing, write a TODO placeholder like [TODO: value].
- If something is uncertain, mark it [CHECK: ...] in the draft instead of guessing.

# Task type A: references -> Introduction-oriented literature review
Goal: not a standalone review article, but the related-work narrative that goes inside the paper's Introduction.
1. Clarify scope from the task and any files the user points to (draft, outline, own abstract). Identify 3-5 sub-themes.
2. Search: several search_papers queries per sub-theme (vary wording, use year filters, sort by citations for foundational work and by recency for latest work). Supplement with web_search when needed. Prefer peer-reviewed sources; include recent work (last ~3 years).
3. Screen and read: for the most relevant 10-30 papers, read abstracts; for key ones read full text (workspace PDFs via read_pdf, or open-access versions via web_fetch).
4. Write paper cards to literature/notes.md (append, don't overwrite others' work): citekey, full reference, DOI/URL, what you read (abstract/full text), problem, method, data/setting, key result, limitation, relevance to the user's paper.
5. Write literature/references.bib with BibTeX entries built only from retrieved metadata. Citekeys: FirstAuthorYearFirstword, e.g. smith2023deep.
6. Write the Introduction review to drafts/intro_related_work.md:
   - funnel structure: broad context and importance -> the specific problem -> main lines of existing approaches (grouped by idea, not paper-by-paper) -> their limitations -> the precise gap -> one sentence on how this paper addresses it (placeholder if the contribution is not given).
   - typically 3-6 paragraphs; every factual claim cited with [@citekey] (Pandoc style); synthesize and compare, don't list.
   - end the file with a short "Notes for the author" section: weakly supported claims, papers worth reading in full, possible missing areas.

# Task type B: experimental data -> analysis -> paper text
1. Inspect first: list the data files, read headers/samples, describe columns, units, sample sizes, missing values. Don't assume structure.
2. Save each analysis as a script under analysis/ (write_file) and run it with run_python(script_path=...), so it is reproducible. Use matplotlib with the Agg backend; save figures to figures/ as PNG (dpi=300) and also PDF.
3. Choose statistics appropriate to the design; report effect sizes and uncertainty (CI or SD/SE), test names, and n. Check assumptions where it matters.
4. Save key numeric results to analysis/results_summary.md (tables in Markdown).
5. Write text to drafts/results.md (or the file the user names): precise, past tense, numbers exactly as computed, figure/table references, no over-claiming. Add a short "Notes for the author" section.

# Working style
- Be economical: this runs on a limited API budget. Don't re-read large files needlessly; read PDFs page ranges when possible.
- Before overwriting an existing file, read it; prefer edit_file for small changes.
- Final answer: a short Chinese summary of what you did, the files you wrote, and open issues. That summary may be read by another agent (Claude Code), so list file paths explicitly.
"""

# ---------------------------------------------------------------- 工具定义
def _tool(name, description, properties, required):
    return {
        "name": name,
        "description": description,
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


CLIENT_TOOLS = [
    _tool("list_files", "List files in a workspace directory. Use pattern like '*.pdf' and recursive=true to search subfolders.",
          {"path": {"type": "string", "description": "Directory relative to workspace, default '.'"},
           "pattern": {"type": "string", "description": "Glob pattern, default '*'"},
           "recursive": {"type": "boolean"}},
          []),
    _tool("read_file", "Read a text file (md, txt, csv, tex, bib, py, json ...) from the workspace with line numbers. Large files are paged: use start_line/max_lines.",
          {"path": {"type": "string"},
           "start_line": {"type": "integer", "description": "1-based, default 1"},
           "max_lines": {"type": "integer", "description": "default 400"}},
          ["path"]),
    _tool("read_pdf", "Extract text from a PDF in the workspace, with <!-- page: N --> markers. Read a page range for long PDFs.",
          {"path": {"type": "string"},
           "start_page": {"type": "integer", "description": "1-based, default 1"},
           "end_page": {"type": "integer", "description": "inclusive, default start_page+14"}},
          ["path"]),
    _tool("write_file", "Create or overwrite a text file in the workspace. Parent folders are created. Set overwrite=true to replace an existing file; set append=true to append.",
          {"path": {"type": "string"},
           "content": {"type": "string", "description": "Full file content"},
           "overwrite": {"type": "boolean"},
           "append": {"type": "boolean"}},
          ["path", "content"]),
    _tool("edit_file", "Replace an exact, unique string in a workspace text file.",
          {"path": {"type": "string"},
           "old_string": {"type": "string"},
           "new_string": {"type": "string"}},
          ["path", "old_string", "new_string"]),
    _tool("search_papers", "Search scholarly works (OpenAlex). Returns title, authors, year, venue, DOI, citation count, open-access URL, abstract. Use doi to look up one paper exactly.",
          {"query": {"type": "string", "description": "Keywords; ignored if doi is given"},
           "doi": {"type": "string", "description": "Look up a single DOI, e.g. 10.1038/nature14539"},
           "year_from": {"type": "integer"},
           "year_to": {"type": "integer"},
           "sort": {"type": "string", "enum": ["relevance", "citations", "recent"]},
           "limit": {"type": "integer", "description": "1-25, default 10"}},
          []),
    _tool("run_python", "Run Python locally with the workspace as current directory. Give either code or script_path (a .py file in the workspace). Returns stdout/stderr. Print the results you need.",
          {"code": {"type": "string"},
           "script_path": {"type": "string"},
           "timeout_sec": {"type": "integer", "description": "default 300"}},
          []),
]

SERVER_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 10},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 10, "max_content_tokens": 40000},
]

TOOLS = CLIENT_TOOLS + SERVER_TOOLS
_SCHEMAS = {t["name"]: t["input_schema"] for t in CLIENT_TOOLS}
_JSON_TYPES = {"string": str, "integer": int, "boolean": bool}


def validate_input(name: str, data) -> str | None:
    """校验工具参数（开启 eager_input_streaming 后服务器不再校验）。返回错误信息或 None。"""
    schema = _SCHEMAS.get(name)
    if schema is None:
        return f"unknown tool {name}"
    if not isinstance(data, dict):
        return "input is not an object"
    for key in schema["required"]:
        if key not in data:
            return f"missing required field '{key}'"
    for key, value in data.items():
        spec = schema["properties"].get(key)
        if spec is None:
            return f"unexpected field '{key}'"
        py = _JSON_TYPES[spec["type"]]
        if not isinstance(value, py) or (py is int and isinstance(value, bool)):
            return f"field '{key}' must be {spec['type']}"
        if "enum" in spec and value not in spec["enum"]:
            return f"field '{key}' must be one of {spec['enum']}"
    return None


# ---------------------------------------------------------------- 工具实现
class ToolError(Exception):
    pass


class Tools:
    def __init__(self, workspace: Path, approve_python: str, log):
        self.ws = workspace.resolve()
        self.approve_python = approve_python  # "ask" | "yes" | "no"
        self.log = log
        self.files_written: list[str] = []
        self.state_dir = self.ws / ".paper_worker"

    # 所有路径限制在 workspace 内
    def _path(self, rel: str | None, must_exist=False) -> Path:
        rel = rel or "."
        p = (self.ws / rel).resolve()
        if p != self.ws and self.ws not in p.parents:
            raise ToolError(f"path '{rel}' is outside the workspace")
        if ".paper_worker" in p.relative_to(self.ws).parts[:1] and p.suffix == ".json":
            raise ToolError("session files are not accessible")
        if must_exist and not p.exists():
            raise ToolError(f"'{rel}' does not exist")
        return p

    def _rel(self, p: Path) -> str:
        return p.relative_to(self.ws).as_posix()

    def list_files(self, path=".", pattern="*", recursive=False):
        d = self._path(path, must_exist=True)
        if not d.is_dir():
            raise ToolError(f"'{path}' is not a directory")
        it = d.rglob(pattern) if recursive else d.glob(pattern)
        rows = []
        for p in sorted(it):
            if ".paper_worker" in p.relative_to(self.ws).parts:
                continue
            if p.is_dir():
                rows.append(f"[dir]  {self._rel(p)}/")
            else:
                rows.append(f"{p.stat().st_size:>10,}  {self._rel(p)}")
            if len(rows) >= 500:
                rows.append("... (truncated at 500 entries)")
                break
        return "\n".join(rows) or "(empty)"

    def read_file(self, path, start_line=1, max_lines=400):
        p = self._path(path, must_exist=True)
        if p.suffix.lower() == ".pdf":
            raise ToolError("use read_pdf for PDF files")
        if p.suffix.lower() in (".xlsx", ".xls"):
            raise ToolError("Excel file: inspect it with run_python (pandas.read_excel)")
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        start = max(1, start_line)
        end = min(len(lines), start + max(1, max_lines) - 1)
        body = "\n".join(f"{i:>6}\t{lines[i-1]}" for i in range(start, end + 1))
        more = f"\n... ({len(lines) - end} more lines; continue with start_line={end + 1})" if end < len(lines) else ""
        return f"[{self._rel(p)}: lines {start}-{end} of {len(lines)}]\n{body}{more}"

    def read_pdf(self, path, start_page=1, end_page=None):
        import pymupdf  # 延迟导入
        p = self._path(path, must_exist=True)
        try:
            doc = pymupdf.open(p)
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"cannot open PDF: {e}")
        if doc.needs_pass:
            raise ToolError("PDF is password-protected; ask the user for an unencrypted copy")
        n = doc.page_count
        start = max(1, start_page)
        end = min(n, end_page or start + 14)
        parts = [f"[{self._rel(p)}: pages {start}-{end} of {n}]"]
        for i in range(start - 1, end):
            parts.append(f"<!-- page: {i + 1} -->\n" + doc[i].get_text("text"))
        out = "\n".join(parts)
        if len(out.strip()) < 50 * (end - start + 1):
            out += "\n[note: very little text extracted - this may be a scanned PDF needing OCR]"
        if end < n:
            out += f"\n... ({n - end} more pages; continue with start_page={end + 1})"
        return out

    def write_file(self, path, content, overwrite=False, append=False):
        p = self._path(path)
        if p.exists() and not (overwrite or append):
            raise ToolError(f"'{path}' exists; read it first, then use overwrite=true, append=true or edit_file")
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a" if append else "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        self.files_written.append(self._rel(p))
        return f"{'appended to' if append else 'wrote'} {self._rel(p)} ({len(content)} chars)"

    def edit_file(self, path, old_string, new_string):
        p = self._path(path, must_exist=True)
        text = p.read_text(encoding="utf-8")
        count = text.count(old_string)
        if count != 1:
            raise ToolError(f"old_string found {count} times; it must match exactly once")
        p.write_text(text.replace(old_string, new_string), encoding="utf-8", newline="\n")
        self.files_written.append(self._rel(p))
        return f"edited {self._rel(p)}"

    def search_papers(self, query=None, doi=None, year_from=None, year_to=None, sort="relevance", limit=10):
        base = "https://api.openalex.org/works"
        if doi:
            doi = doi.strip().removeprefix("https://doi.org/").removeprefix("doi:")
            url = f"{base}/doi:{urllib.parse.quote(doi)}"
        else:
            if not query:
                raise ToolError("give query or doi")
            filters = []
            if year_from:
                filters.append(f"from_publication_date:{year_from}-01-01")
            if year_to:
                filters.append(f"to_publication_date:{year_to}-12-31")
            params = {"search": query, "per-page": str(min(max(limit, 1), 25))}
            if filters:
                params["filter"] = ",".join(filters)
            if sort == "citations":
                params["sort"] = "cited_by_count:desc"
            elif sort == "recent":
                params["sort"] = "publication_date:desc"
            url = f"{base}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "paper-worker/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            raise ToolError(f"OpenAlex HTTP {e.code}" + (" (DOI not found)" if e.code == 404 else ""))
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"OpenAlex request failed: {e}")
        works = [data] if doi else data.get("results", [])
        if not works:
            return "no results"
        return "\n\n".join(_format_work(w, i + 1) for i, w in enumerate(works))

    def run_python(self, code=None, script_path=None, timeout_sec=300):
        if bool(code) == bool(script_path):
            raise ToolError("give exactly one of code or script_path")
        if script_path:
            target = self._path(script_path, must_exist=True)
            preview = target.read_text(encoding="utf-8", errors="replace")
        else:
            scratch = self.state_dir / "scratch"
            scratch.mkdir(parents=True, exist_ok=True)
            target = scratch / f"run_{_dt.datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4]}.py"
            target.write_text(code, encoding="utf-8")
            preview = code
        if not self._approve(preview, target):
            return "USER DECLINED: the user did not approve running this code. Ask what they want or adjust."
        env = dict(os.environ, MPLBACKEND="Agg", PYTHONIOENCODING="utf-8")
        env.pop("ANTHROPIC_API_KEY", None)  # 不把 Key 暴露给模型写的代码
        try:
            r = subprocess.run([sys.executable, str(target)], cwd=self.ws, capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=min(max(timeout_sec, 5), 3600), env=env)
        except subprocess.TimeoutExpired:
            raise ToolError(f"timed out after {timeout_sec}s")
        out = f"exit code {r.returncode}\n--- stdout ---\n{r.stdout}"
        if r.stderr.strip():
            out += f"\n--- stderr ---\n{r.stderr}"
        return out

    def _approve(self, code: str, target: Path) -> bool:
        if self.approve_python == "yes":
            return True
        if self.approve_python == "no" or not sys.stdin.isatty():
            return False
        print("\n" + "=" * 70, file=sys.stderr)
        print(f"模型请求在本地运行 Python（{self._rel(target)}）：", file=sys.stderr)
        snippet = code if len(code) < 4000 else code[:4000] + "\n... (truncated)"
        print(textwrap.indent(snippet, "  | "), file=sys.stderr)
        ans = input("允许运行？[y/N] ").strip().lower()
        return ans in ("y", "yes")

    def call(self, name: str, args: dict) -> str:
        fn = getattr(self, name, None)
        if fn is None or name.startswith("_") or name == "call":
            raise ToolError(f"unknown tool {name}")
        return fn(**args)


def _format_work(w: dict, idx: int) -> str:
    authors = [a["author"]["display_name"] for a in w.get("authorships", []) if a.get("author")]
    auth = ", ".join(authors[:4]) + (" et al." if len(authors) > 4 else "")
    loc = (w.get("primary_location") or {})
    venue = ((loc.get("source") or {}).get("display_name")) or ""
    oa = (w.get("open_access") or {}).get("oa_url") or ""
    abstract = _abstract(w.get("abstract_inverted_index"))
    if len(abstract) > 1500:
        abstract = abstract[:1500] + " ..."
    lines = [
        f"[{idx}] {w.get('display_name') or w.get('title')}",
        f"    authors: {auth}",
        f"    year: {w.get('publication_year')} | venue: {venue} | type: {w.get('type')} | cited_by: {w.get('cited_by_count')}",
        f"    doi: {w.get('doi') or '-'} | openalex: {w.get('id')}",
    ]
    if oa:
        lines.append(f"    open_access_url: {oa}")
    lines.append(f"    abstract: {abstract or '(not available)'}")
    return "\n".join(lines)


def _abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


# ---------------------------------------------------------------- 会话与花费
class Usage:
    def __init__(self, model: str):
        self.model = model
        self.inp = self.out = self.cread = self.cwrite = self.searches = 0

    def add(self, u):
        if u is None:
            return
        self.inp += u.input_tokens or 0
        self.out += u.output_tokens or 0
        self.cread += getattr(u, "cache_read_input_tokens", 0) or 0
        self.cwrite += getattr(u, "cache_creation_input_tokens", 0) or 0
        stu = getattr(u, "server_tool_use", None)
        if stu is not None:
            self.searches += getattr(stu, "web_search_requests", 0) or 0

    def cost(self) -> float:
        pi, po, pr, pw = PRICES.get(self.model, PRICES["claude-opus-5-5"])
        return (self.inp * pi + self.out * po + self.cread * pr + self.cwrite * pw) / 1e6 + self.searches * WEB_SEARCH_PRICE

    def to_dict(self):
        return {"input": self.inp, "output": self.out, "cache_read": self.cread,
                "cache_write": self.cwrite, "web_searches": self.searches, "est_cost_usd": round(self.cost(), 4)}

    def summary(self) -> str:
        return (f"tokens in={self.inp:,} out={self.out:,} cache_read={self.cread:,} cache_write={self.cwrite:,}"
                f" web_search={self.searches}  估算花费 ≈ ${self.cost():.3f}")


class Session:
    def __init__(self, workspace: Path, session_id: str | None):
        self.dir = workspace / ".paper_worker" / "sessions"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.id = session_id or f"{_dt.datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        self.path = self.dir / f"{self.id}.json"
        self.messages: list[dict] = []
        self.model: str | None = None
        self.total_cost = 0.0
        if session_id:
            if not self.path.exists():
                raise SystemExit(f"找不到会话 {session_id}（{self.path}）")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.messages = data["messages"]
            self.model = data.get("model")
            self.total_cost = data.get("total_cost_usd", 0.0)

    def save(self, model: str, extra_cost: float):
        data = {"id": self.id, "model": model, "updated": _dt.datetime.now().isoformat(timespec="seconds"),
                "total_cost_usd": round(self.total_cost + extra_cost, 4), "messages": self.messages}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)


class StopWorker(Exception):
    pass


# ---------------------------------------------------------------- Agent 循环
class Agent:
    def __init__(self, args, out_stream):
        self.ws = Path(args.workspace).resolve()
        if not self.ws.is_dir():
            raise SystemExit(f"workspace 不存在：{self.ws}")
        self.session = Session(self.ws, args.resume)
        self.model = args.model or self.session.model or DEFAULT_MODEL
        if self.session.model and self.model != self.session.model:
            print(f"[提示] 会话原模型 {self.session.model}，现改用 {self.model}；旧的思考块将被忽略。", file=sys.stderr)
        self.effort = args.effort
        self.max_steps = args.max_steps
        self.max_cost = args.max_cost
        self.show_thinking = args.show_thinking
        self.out = out_stream  # 模型正文输出位置（chat: stdout；run: stderr 进度）
        self.usage = Usage(self.model)
        self.tools = Tools(self.ws, args.approve_python, self._log)
        self.client = anthropic.Anthropic(max_retries=4)

    def _log(self, msg: str):
        print(msg, file=sys.stderr, flush=True)

    def _request(self) -> object:
        """发送一次流式请求，返回最终消息。对无法解析的工具 JSON 最多重试 2 次。"""
        for attempt in range(3):
            try:
                with self.client.beta.messages.stream(
                    model=self.model,
                    max_tokens=MAX_TOKENS,
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=self.session.messages,
                    thinking={"type": "adaptive", "display": "summarized"},
                    output_config={"effort": self.effort},
                    cache_control={"type": "ephemeral"},
                    betas=[FALLBACK_BETA],
                    fallbacks="default",
                ) as stream:
                    in_thinking = False
                    for event in stream:
                        if event.type == "content_block_start":
                            bt = event.content_block.type
                            if bt == "thinking" and self.show_thinking:
                                in_thinking = True
                                print("\n\033[2m[思考] ", end="", file=self.out, flush=True)
                            elif bt == "server_tool_use":
                                self._log(f"  · {event.content_block.name} …")
                        elif event.type == "content_block_delta":
                            d = event.delta
                            if d.type == "text_delta":
                                print(d.text, end="", file=self.out, flush=True)
                            elif d.type == "thinking_delta" and self.show_thinking:
                                print(d.thinking, end="", file=self.out, flush=True)
                        elif event.type == "content_block_stop" and in_thinking:
                            in_thinking = False
                            print("\033[0m", file=self.out, flush=True)
                    return stream.get_final_message()
            except ValueError as e:  # 工具输入 JSON 无法解析（只捕获这一类）
                self._log(f"[警告] 工具参数 JSON 解析失败，重试 ({attempt + 1}/3)：{e}")
        raise StopWorker("工具参数 JSON 连续解析失败")

    def run_turn(self, user_text: str) -> str:
        """处理一条用户消息，直到模型给出最终回答。返回最终文本。"""
        msgs = self.session.messages
        snapshot = copy.deepcopy(msgs)
        if msgs and msgs[-1]["role"] == "user":
            # 上一轮在工具结果之后被中止：把新消息并入同一个 user 轮，保持交替顺序且不改动已发送的前缀
            last = msgs[-1]
            if isinstance(last["content"], str):
                last["content"] = [{"type": "text", "text": last["content"]}]
            last["content"].append({"type": "text", "text": user_text})
        else:
            msgs.append({"role": "user", "content": user_text})
        final_text = ""
        steps = 0
        try:
            while True:
                steps += 1
                if steps > self.max_steps:
                    raise StopWorker(f"超过最大步数 {self.max_steps}（可用 --max-steps 调大）")
                if self.max_cost and self.usage.cost() > self.max_cost:
                    raise StopWorker(f"本次花费已超过上限 ${self.max_cost}（可用 --max-cost 调整）")

                msg = self._request()
                self.usage.add(msg.usage)

                if msg.stop_reason == "refusal":
                    details = getattr(msg, "stop_details", None)
                    cat = getattr(details, "category", None)
                    msgs[:] = snapshot  # 丢弃本轮（只回退尾部，不改已有前缀），以便继续对话
                    raise StopWorker(f"模型拒绝了这个请求（category={cat}）。请换个说法或拆小任务。")

                content = [b.to_dict() for b in msg.content]
                self.session.messages.append({"role": "assistant", "content": content})
                text = "".join(b.text for b in msg.content if b.type == "text")
                if text.strip():
                    final_text = text

                if msg.stop_reason == "pause_turn":
                    continue  # 服务端工具循环暂停，原样续发

                tool_uses = [b for b in msg.content if b.type == "tool_use"]
                if msg.stop_reason == "max_tokens":
                    if not tool_uses:
                        self._log("[提示] 输出达到 max_tokens 被截断。")
                        break
                    results = [{"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                                "content": "Your output hit max_tokens and this tool input was truncated. Retry with smaller pieces (e.g. write the file in several appends)."}
                               for b in tool_uses]
                    self.session.messages.append({"role": "user", "content": results})
                    continue

                if msg.stop_reason != "tool_use" or not tool_uses:
                    break  # end_turn / stop_sequence

                results = []
                for b in tool_uses:
                    results.append(self._exec_tool(b))
                self.session.messages.append({"role": "user", "content": results})
                self.session.save(self.model, self.usage.cost())
        except KeyboardInterrupt:
            msgs[:] = snapshot  # 中断时回退本轮，避免留下没有结果的 tool_use
            raise
        finally:
            self.session.save(self.model, self.usage.cost())
        print(file=self.out)
        return final_text

    def _exec_tool(self, block) -> dict:
        args = block.input
        short = json.dumps(args, ensure_ascii=False)
        if len(short) > 160:
            short = short[:160] + "…"
        self._log(f"  → {block.name} {short}")
        err = validate_input(block.name, args)
        if err:
            return {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                    "content": json.dumps({"INVALID_INPUT": err, "received": short}, ensure_ascii=False)}
        try:
            out = self.tools.call(block.name, args)
            is_error = False
        except ToolError as e:
            out, is_error = f"Error: {e}", True
        except Exception as e:  # noqa: BLE001  工具内部异常返回给模型而不是崩溃
            out, is_error = f"Error ({type(e).__name__}): {e}", True
        if len(out) > MAX_TOOL_OUTPUT_CHARS:
            out = out[:MAX_TOOL_OUTPUT_CHARS] + f"\n... [truncated, {len(out) - MAX_TOOL_OUTPUT_CHARS} chars omitted; read a smaller range]"
        if is_error:
            self._log(f"    ✗ {out[:200]}")
        return {"type": "tool_result", "tool_use_id": block.id, "content": out, "is_error": is_error}


# ---------------------------------------------------------------- CLI
def _check_key():
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise SystemExit("未设置 ANTHROPIC_API_KEY。请在 platform.claude.com 创建 API Key，并设置为环境变量（见 README）。")


def cmd_chat(args):
    _check_key()
    agent = Agent(args, sys.stdout)
    print(f"paper_worker  会话 {agent.session.id}  模型 {agent.model}  effort {agent.effort}")
    print(f"workspace: {agent.ws}")
    print("输入任务开始；/cost 查看花费，/exit 退出（会话自动保存，可用 --resume 继续）。多行输入：以 \"\"\" 开始和结束。\n")
    while True:
        try:
            line = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if line == '"""':
            buf = []
            while True:
                nxt = input()
                if nxt.strip() == '"""':
                    break
                buf.append(nxt)
            line = "\n".join(buf)
        if not line:
            continue
        if line in ("/exit", "/quit"):
            break
        if line == "/cost":
            print(agent.usage.summary())
            continue
        print("\nClaude> ", end="", flush=True)
        try:
            agent.run_turn(line)
        except StopWorker as e:
            print(f"\n[停止] {e}")
        except KeyboardInterrupt:
            print("\n[已中断本轮]")
        except anthropic.APIError as e:
            print(f"\n[API 错误] {e}")
        print(f"[{agent.usage.summary()}]\n")
    print(f"会话已保存：{agent.session.id}")


def cmd_run(args):
    _check_key()
    task = args.task
    if args.task_file:
        task = Path(args.task_file).read_text(encoding="utf-8")
    if not task:
        raise SystemExit("需要 --task 或 --task-file")
    agent = Agent(args, sys.stderr)  # 过程输出到 stderr，stdout 只留最终结果
    agent._log(f"[paper_worker] 会话 {agent.session.id}  模型 {agent.model}  effort {agent.effort}")
    status, final = "ok", ""
    try:
        final = agent.run_turn(task)
    except StopWorker as e:
        status, final = "stopped", str(e)
    except anthropic.APIError as e:
        status, final = "api_error", str(e)
    agent._log(f"[paper_worker] {agent.usage.summary()}")
    files = sorted(set(agent.tools.files_written))
    if args.json:
        print(json.dumps({"status": status, "session_id": agent.session.id, "final": final,
                          "files_written": files, "usage": agent.usage.to_dict()}, ensure_ascii=False, indent=1))
    else:
        print(final)
        print(f"\n---\nstatus: {status}\nsession_id: {agent.session.id}\nfiles_written: {', '.join(files) or '-'}")
    sys.exit(0 if status == "ok" else 1)


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace", "-w", default=".", help="论文项目文件夹（工具只能访问这里）")
    common.add_argument("--model", default=None, help=f"默认 {DEFAULT_MODEL}，可用 claude-sonnet-5-5 / claude-haiku-5-5 省钱")
    common.add_argument("--effort", default=DEFAULT_EFFORT, choices=["low", "medium", "high", "xhigh", "max"])
    common.add_argument("--resume", default=None, metavar="SESSION_ID", help="继续之前的会话")
    common.add_argument("--max-steps", type=int, default=60, help="单轮最多调用模型次数")
    common.add_argument("--max-cost", type=float, default=5.0, help="本次进程估算花费上限（美元），0 = 不限")
    common.add_argument("--approve-python", choices=["ask", "yes", "no"], default="ask",
                        help="本地运行 Python 前是否询问；非交互环境下 ask 等同 no")
    common.add_argument("--show-thinking", action="store_true", help="显示思考摘要")

    ap = argparse.ArgumentParser(description="本地论文写作 agent（Claude API）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_chat = sub.add_parser("chat", parents=[common], help="交互式对话")
    p_chat.set_defaults(func=cmd_chat)
    p_run = sub.add_parser("run", parents=[common], help="执行单个任务后退出（供 Claude Code 等调用）")
    p_run.add_argument("--task", "-t", default=None)
    p_run.add_argument("--task-file", default=None, help="从文件读取任务描述")
    p_run.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    p_run.set_defaults(func=cmd_run)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

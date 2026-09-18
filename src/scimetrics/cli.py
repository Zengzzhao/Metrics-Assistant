"""统一入口：完整运行、检查点启动、查看及恢复；参数可由 TOML 配置。"""

import argparse
import json
import sqlite3
import tomllib
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from dotenv import load_dotenv
from langgraph.checkpoint.sqlite import SqliteSaver

from .utils.client import DeepSeekClient
from .utils.document import digest
from .utils.observability import validate_tracing, flush_traces
from .pipeline import Settings, build_graph

NodeName = Literal[
    "prepare_document", "classify_images", "plan_chunks", "discover_chunks",
    "merge_indicators", "extract_pi_relation", "extract_ii_relation"
]


class RunConfig(BaseModel):
    """唯一运行配置来源；Pydantic 负责类型、范围和未知字段检查。"""

    model_config = ConfigDict(extra="forbid")
    input: Path
    output: Path = Path(".debug")
    action: Literal["run", "start", "resume", "inspect", "continue"] = "run"
    pattern: str = "*.md"
    model: str = "deepseek-flash"
    max_output_tokens: int = Field(default=65536, gt=0, strict=True)
    max_chars: int = Field(default=250000, gt=0, strict=True)
    max_images: int = Field(default=40, ge=0, strict=True)
    max_body_bytes: int = Field(default=40000000, gt=0, strict=True)
    timeout: float = Field(default=600, gt=0)
    db: Path = Path(".debug/checkpoints.sqlite")
    thread_id: str = Field(default="paper-001", pattern=r"\S")
    stop_after: list[NodeName] = Field(
        default_factory=lambda: [
            "prepare_document",
            "classify_images",
            "plan_chunks", "merge_indicators",
        ]
    )
    stop_before: list[NodeName] = Field(default_factory=lambda: ["merge_indicators"])
    field: Literal["all", "raw_content", "images", "chunks", "skipped_sections",
                   "chunk_discoveries", "discovery", "merge_map", "paper_relations",
                   "indicator_relations", "extraction"] = "all"

    def settings(self, source=None, output=None):
        return Settings(
            source or self.input,
            output or self.output,
            self.max_chars,
            self.max_images,
            self.max_body_bytes,
        )

    def client_options(self):
        return {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "timeout": self.timeout,
        }


def load_config(path: Path, action=None):
    path = path.resolve()
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    if set(data) != {"run"}:
        raise ValueError("配置文件必须且只能包含 [run] 表")
    config = RunConfig.model_validate(data["run"])
    for key in ("input", "output", "db"):
        setattr(config, key, (path.parent / getattr(config, key)).resolve())
    if action:
        config.action = action
    if config.action == "continue":
        config.action, config.stop_after, config.stop_before = "resume", [], []
    return config

# 执行编排流程
def execute(graph, initial, name, thread_id=None):
    config = {
        "run_id": uuid4(),
        "run_name": name,
        "tags": ["scimetrics"],
        # 每章占一个图执行步；循环仍由有限的章节清单决定结束。
        "recursion_limit": 10000,
    }
    if thread_id:
        config["configurable"] = {"thread_id": thread_id}
    for update in graph.stream(initial, config=config, stream_mode="updates", durability="sync"):
        for node in update:
            if node != "__interrupt__":
                detail = ""
                if node == "discover_chunks":
                    records = update[node]["chunk_discoveries"]
                    if records:
                        detail = f"：已完成 {len(records)} 章，最新 {records[-1]['chunk_id']} {records[-1]['title']}"
                print(f"[{name}] {node}{detail}", flush=True)


class LazyClient:
    """整理 Markdown 或查看状态无需 DeepSeek Key；首次模型调用时才初始化。"""

    def __init__(self, config):
        self.config = config
        self.client = None

    def call(self, *args, **kwargs):
        if self.client is None:
            self.client = DeepSeekClient(**self.config)
        return self.client.call(*args, **kwargs)


# 检查点模式
def run_checkpoint(args):
    database = args.db.resolve()
    if args.action != "start" and not database.is_file():
        raise ValueError("检查点数据库不存在，请先 start")
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database, check_same_thread=False) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS debug_sessions (thread_id TEXT PRIMARY KEY, config TEXT NOT NULL)"
        )
        row = connection.execute(
            "SELECT config FROM debug_sessions WHERE thread_id = ?", (args.thread_id,)
        ).fetchone()
        if args.action == "start":
            if row:
                raise ValueError(
                    "thread-id 已存在，请使用 resume 或换一个新的 thread-id"
                )
            source = args.input.resolve()
            if source.suffix.lower() != ".md" or not source.is_file():
                raise ValueError("输入必须是已有 Markdown (.md)")
            settings = args.settings()
            saved = {
                "settings": {
                    k: str(v) if isinstance(v, Path) else v
                    for k, v in asdict(settings).items()
                },
                "client": args.client_options(),
                "input_hash": digest(source.read_bytes()),
            }
            connection.execute(
                "INSERT INTO debug_sessions VALUES (?, ?)",
                (args.thread_id, json.dumps(saved)),
            )
            connection.commit()
        else:
            if not row:
                raise ValueError("找不到该 thread-id，请核对数据库与任务 ID")
            saved = json.loads(row[0])
            values = saved["settings"]
            settings = Settings(
                **{
                    **values,
                    "input_path": Path(values["input_path"]),
                    "output_dir": Path(values["output_dir"]),
                }
            )
            if args.action == "resume":
                if (
                    not settings.input_path.is_file()
                    or digest(settings.input_path.read_bytes()) != saved["input_hash"]
                ):
                    raise ValueError(
                        "原始 Markdown 已变化或不存在，请使用新 thread-id 重新 start"
                    )
        # 输入、输出和模型固定；恢复时允许用当前配置调整资源限额。
        client_options = dict(saved["client"])
        if args.action == "resume":
            settings.max_chars = args.max_chars
            settings.max_images = args.max_images
            settings.max_body_bytes = args.max_body_bytes
            client_options.update(max_tokens=args.max_output_tokens, timeout=args.timeout)
            print(f"[LIMITS] max_output_tokens={args.max_output_tokens}, timeout={args.timeout}s, "
                  f"max_chars={settings.max_chars}, max_images={settings.max_images}, "
                  f"max_body_bytes={settings.max_body_bytes}", flush=True)
        graph = build_graph(
            settings,
            LazyClient(client_options),
            checkpointer=SqliteSaver(connection),
            interrupt_after=args.stop_after or None,
            interrupt_before=args.stop_before or None,
        )
        config = {"configurable": {"thread_id": args.thread_id}}
        if args.action != "inspect":
            validate_tracing()
            before = graph.get_state(config)
            if args.action == "resume" and before.values and not before.next:
                print("[COMPLETE] 该任务已完成，没有后续节点。")
                return before
            if args.action == "resume" and not before.next:
                raise ValueError("没有可恢复的检查点，请使用新的 thread-id start")
            execute(
                graph,
                {} if args.action == "start" else None,
                f"debug:{args.thread_id}",
                args.thread_id,
            )
        snapshot = graph.get_state(config)
        report = {
            "thread_id": args.thread_id,
            "next": list(snapshot.next),
            "state_keys": list(snapshot.values),
            "output": str(settings.output_dir / "result.json"),
            "discovery_progress": {
                "completed": len(snapshot.values.get("chunk_discoveries", [])),
                "total": len(snapshot.values.get("chunks", [])),
            },
        }
        if args.action == "inspect":
            report["state"] = (
                snapshot.values
                if args.field == "all"
                else snapshot.values.get(args.field)
            )
            report["tasks"] = [
                {"name": task.name, "error": str(task.error) if task.error else None}
                for task in snapshot.tasks
            ]
        # 读取之前state数据写出为json分析
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        report_dir = args.db.parent / ".reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{args.thread_id}.json"
        report_path.write_text(payload, encoding="utf-8")
        print(f"\n[REPORT] {report_path}", flush=True)
        return snapshot


# 完整模式
def run_standard(args):
    if not args.input.exists():
        raise ValueError("输入不存在，请在 run.toml 中配置 input")
    files = (
        sorted(p for p in args.input.rglob(args.pattern) if p.is_file())
        if args.input.is_dir()
        else [args.input]
    )
    if not files or any(p.suffix.lower() != ".md" for p in files):
        raise ValueError("仅支持 Markdown (.md)，或目录中没有匹配文件")
    client = DeepSeekClient(**args.client_options())
    failed = False
    for source in files:
        output = args.output / (
            source.stem + "-" + digest(str(source.resolve()).encode())[:8]
        )
        try:
            graph = build_graph(args.settings(source, output), client)
            execute(graph, {}, f"paper:{source.stem}")
            print(f"[OK] {output / 'result.json'}", flush=True)
        except Exception as error:
            failed = True
            print(
                f"[{source.name}] FAILED: {error_message(error)}；本轮未写入最终结果。",
                flush=True,
            )
    if failed:
        raise SystemExit(1)


def error_message(error):
    return (
        str(error) if isinstance(error, (ValueError, OSError)) else type(error).__name__
    )


def main():
    parser = argparse.ArgumentParser(
        description="运行参数统一读取 TOML，仅 action 可临时覆盖"
    )
    parser.add_argument("--config", type=Path, default=Path("run.toml"))
    parser.add_argument(
        "--action", choices=["run", "start", "resume", "inspect", "continue"]
    )
    options = parser.parse_args()
    # 无论从哪个工作目录调用，都读取配置文件旁的 .env；已有环境变量优先。
    load_dotenv(options.config.resolve().parent / ".env")
    config = None
    try:
        config = load_config(options.config, options.action)
        # 运行模式：完整运行
        if config.action == "run":
            run_standard(config)
        # 检查点模式：start、resume、inspect、continue
        else:
            run_checkpoint(config)
    except Exception as error:
        parser.exit(1, f"执行失败: {error_message(error)}\n")
    finally:
        if config and config.action != "inspect":
            flush_traces()


if __name__ == "__main__":
    main()

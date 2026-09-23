"""任务集：可确定性判分的小任务（21 个：单步 / 多步 / 跨会话 / 同一会话多轮）。

- 单阶段任务：check(final_answer, workdir) -> bool；
- 跨会话任务：phases = [phase1, phase2]，各自新会话、共享长期记忆库（验证长期记忆）；
- 同一会话多轮任务：turns = [...]，同一个 Agent 连续多轮（验证短期记忆连续性）；
- 全部判分确定性实现，不依赖 LLM judge，保证评测可复现。
"""
import json
from pathlib import Path
from typing import Callable, Dict, List


def _file_contains(workdir: str, filename: str, text: str) -> bool:
    path = Path(workdir) / filename
    if not path.exists():
        return False
    return text in path.read_text(encoding="utf-8", errors="ignore")


def _answer_has(text: str) -> Callable[[str, str], bool]:
    return lambda answer, workdir: text in (answer or "")


def _json_config_check(answer: str, workdir: str) -> bool:
    path = Path(workdir) / "config.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return data.get("name") == "miniagent" and data.get("version") == 1


def _answer_ok(answer: str, workdir: str) -> bool:
    """教学阶段（phase 1）只要有正常回答即算通过（排除调用失败）。"""
    text = (answer or "").strip()
    return bool(text) and "LLM调用失败" not in text


def _json_extend_check(answer: str, workdir: str) -> bool:
    path = Path(workdir) / "data.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return data.get("a") == 1 and data.get("b") == 2


TASKS: List[Dict] = [
    {
        "id": "sum_1_100",
        "prompt": "用 Python 计算 1 到 100 的和，直接告诉我最终结果。",
        "check": _answer_has("5050"),
    },
    {
        "id": "write_hello",
        "prompt": "在当前目录创建一个 hello.txt 文件，内容为 Hello MiniAgent。完成后告诉我。",
        "check": lambda answer, workdir: _file_contains(workdir, "hello.txt", "Hello MiniAgent"),
    },
    {
        "id": "squares_sum",
        "prompt": (
            "先用 Python 计算 1 到 20 的平方和，把结果写入 squares.txt，"
            "然后在回复中告诉我这个结果。"
        ),
        "check": lambda answer, workdir: _file_contains(workdir, "squares.txt", "2870"),
    },
    {
        "id": "lines_count",
        "prompt": (
            "创建一个 nums.txt，把数字 1 到 5 每行一个写进去，"
            "然后用 Python 统计文件共有多少行，并在回复中告诉我行数。"
        ),
        "check": lambda answer, workdir: _file_contains(workdir, "nums.txt", "5")
        and "5" in (answer or ""),
    },
    {
        "id": "copy_file",
        "prompt": (
            "创建一个 a.txt，内容为 MINI-42；然后把它的内容原样复制到 b.txt，"
            "最后读取 b.txt 确认内容。"
        ),
        "check": lambda answer, workdir: _file_contains(workdir, "b.txt", "MINI-42"),
    },
    {
        "id": "json_config",
        "prompt": (
            '创建一个 config.json 文件，内容为 {"name": "miniagent", "version": 1}，'
            "然后用 Python 读取它并确认 version 字段。"
        ),
        "check": _json_config_check,
    },

    # ---------- 多步任务（4-6 次工具调用） ----------
    {
        "id": "sum_two_files",
        "prompt": (
            "创建 n1.txt 写入 12，创建 n2.txt 写入 30；然后读取两个文件，"
            "把它们的和写入 total.txt，并告诉我结果。"
        ),
        "check": lambda answer, workdir: _file_contains(workdir, "total.txt", "42"),
    },
    {
        "id": "word_count",
        "prompt": (
            "创建 words.txt，内容为 alpha beta gamma delta；"
            "用 Python 统计单词数量，把数量写入 count.txt 并告诉我。"
        ),
        "check": lambda answer, workdir: _file_contains(workdir, "count.txt", "4"),
    },
    {
        "id": "json_extend",
        "prompt": (
            '创建 data.json，内容为 {"a": 1}；读取它并加入字段 b=2，写回 data.json，'
            "确认两个字段都在。"
        ),
        "check": _json_extend_check,
    },
    {
        "id": "file_rename",
        "prompt": (
            "创建 old.txt，内容为 MOVE-ME；把它重命名为 new.txt（原文件不再存在），"
            "并读取 new.txt 确认内容。"
        ),
        "check": lambda answer, workdir: _file_contains(workdir, "new.txt", "MOVE-ME")
        and not (Path(workdir) / "old.txt").exists(),
    },
    {
        "id": "avg_three_files",
        "prompt": (
            "创建 f1.txt=5、f2.txt=10、f3.txt=15 三个文件，读取它们求平均值，"
            "把平均值写入 avg.txt 并告诉我。"
        ),
        "check": lambda answer, workdir: _file_contains(workdir, "avg.txt", "10"),
    },
    {
        "id": "reverse_string",
        "prompt": "把字符串 MINIAGENT 反转，把反转结果写入 rev.txt 并告诉我。",
        "check": lambda answer, workdir: _file_contains(workdir, "rev.txt", "TNEGAINIM"),
    },
    {
        "id": "scores_total",
        "prompt": (
            "创建 scores.txt，内容为三行：a 10 / b 20 / c 30；"
            "用 Python 计算所有分数的总和，写入 total_score.txt 并告诉我。"
        ),
        "check": lambda answer, workdir: _file_contains(workdir, "total_score.txt", "60"),
    },
    {
        "id": "backup_copy",
        "prompt": (
            "创建 config.txt，内容为 v=1；然后创建一份备份 config.bak.txt，"
            "内容与 config.txt 相同；最后确认两个文件内容一致。"
        ),
        "check": lambda answer, workdir: _file_contains(workdir, "config.txt", "v=1")
        and _file_contains(workdir, "config.bak.txt", "v=1"),
    },

    # ---------- 跨会话任务（phase 1 教、phase 2 用，验证长期记忆） ----------
    {
        "id": "xs_project_code",
        "phases": [
            {
                "prompt": "请记住：我的项目代号是 ORION。只需确认你已记住。",
                "check": _answer_ok,
            },
            {
                "prompt": "创建 reports/project.txt，内容写我的项目代号。完成后告诉我。",
                "check": lambda answer, workdir: _file_contains(
                    workdir, "reports/project.txt", "ORION"
                ),
            },
        ],
    },
    {
        "id": "xs_decimal_pref",
        "phases": [
            {
                "prompt": "请记住我的偏好：所有数字保留两位小数。确认即可。",
                "check": _answer_ok,
            },
            {
                "prompt": "计算 10 除以 3，按我的偏好把结果写入 result.txt，并告诉我结果。",
                "check": lambda answer, workdir: _file_contains(workdir, "result.txt", "3.33"),
            },
        ],
    },
    {
        "id": "xs_three_numbers",
        "phases": [
            {
                "prompt": "请记住三个数字：7、11、13。确认即可。",
                "check": _answer_ok,
            },
            {
                "prompt": "把我之前给你的三个数字相加，把和写入 sum13.txt，并告诉我。",
                "check": lambda answer, workdir: _file_contains(workdir, "sum13.txt", "31"),
            },
        ],
    },
    {
        "id": "xs_reports_dir",
        "phases": [
            {
                "prompt": "请记住：所有报告都放在 reports 目录下。确认即可。",
                "check": _answer_ok,
            },
            {
                "prompt": "创建一份报告 status.txt，放在我说过的目录里，内容写 OK。",
                "check": lambda answer, workdir: _file_contains(
                    workdir, "reports/status.txt", "OK"
                ),
            },
        ],
    },

    # ---------- 同一会话多轮任务（短期记忆连续性） ----------
    {
        "id": "mt_file_chain",
        "turns": [
            {
                "prompt": "创建 draft.txt，内容写 TURN-1。完成后告诉我。",
                "check": _answer_ok,
            },
            {
                "prompt": "读取 draft.txt，把它的内容写进 final.txt。",
                "check": lambda answer, workdir: _file_contains(
                    workdir, "final.txt", "TURN-1"
                ),
            },
            {
                "prompt": "final.txt 里是什么内容？直接回答。",
                "check": lambda answer, workdir: "TURN-1" in (answer or ""),
            },
        ],
    },
    {
        "id": "mt_accumulate",
        "turns": [
            {
                "prompt": "创建 list.txt，写入一行 first。",
                "check": _answer_ok,
            },
            {
                "prompt": "在 list.txt 末尾追加一行 second（保留 first）。",
                "check": lambda answer, workdir: _file_contains(workdir, "list.txt", "first")
                and _file_contains(workdir, "list.txt", "second"),
            },
            {
                "prompt": "list.txt 现在有几行？只回答数字。",
                "check": lambda answer, workdir: "2" in (answer or ""),
            },
        ],
    },
    {
        "id": "mt_correction",
        "turns": [
            {
                "prompt": "记住：报告目录是 reports。",
                "check": _answer_ok,
            },
            {
                "prompt": "更正：报告目录改为 docs。",
                "check": _answer_ok,
            },
            {
                "prompt": "在（最新的）报告目录里创建 note.txt，内容写 OK。",
                "check": lambda answer, workdir: _file_contains(
                    workdir, "docs/note.txt", "OK"
                )
                and not (Path(workdir) / "reports" / "note.txt").exists(),
            },
        ],
    },
]

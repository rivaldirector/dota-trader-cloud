#!/usr/bin/env python3
"""
Multi-agent orchestrator: Claude (analyst) <-> GPT-4o (critic)

Claude имеет доступ к файлам проекта и SQLite БД.
GPT-4o получает анализ Claude и критикует его.
Агенты обмениваются несколько раундов, затем Claude пишет итоговый синтез.

Usage:
    python3 agents/orchestrator.py "Найди баги в backtest_daily.py"
    python3 agents/orchestrator.py "Предложи новые признаки для модели" --rounds 2
"""

import os
import sys
import json
import glob
import sqlite3
import argparse
from pathlib import Path
from datetime import datetime

import anthropic
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ── Config ───────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH      = PROJECT_ROOT / "storage" / "dota_trader.sqlite3"

CLAUDE_MODEL = "claude-opus-4-5"
GPT_MODEL    = "gpt-4o"
MAX_ROUNDS   = 2          # сколько раз GPT критикует → Claude отвечает
MAX_TOOL_ITERATIONS = 10  # защита от бесконечного tool-loop


# ── Инструменты для Claude ────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "read_file",
        "description": "Читает файл проекта dota_trader_v2 по пути относительно корня проекта.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Путь относительно корня, например 'scripts/backtest_daily.py'"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "query_db",
        "description": "Выполняет SELECT-запрос к SQLite dota_trader.sqlite3. Только SELECT.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "SQL SELECT запрос"}
            },
            "required": ["sql"],
        },
    },
    {
        "name": "list_files",
        "description": "Возвращает список файлов проекта по glob-паттерну.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob-паттерн, например '**/*.py'"}
            },
            "required": ["pattern"],
        },
    },
]


def _run_read_file(path: str) -> str:
    full = PROJECT_ROOT / path
    if not full.exists():
        return f"ERROR: file not found: {path}"
    try:
        return full.read_text(encoding="utf-8")
    except Exception as e:
        return f"ERROR: {e}"


def _run_query_db(sql: str) -> str:
    sql_upper = sql.strip().upper()
    if not sql_upper.startswith("SELECT"):
        return "ERROR: only SELECT queries are allowed"
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql).fetchall()
        conn.close()
        if not rows:
            return "(no rows)"
        cols = list(rows[0].keys())
        lines = [" | ".join(cols), "-" * (sum(len(c) for c in cols) + 3 * len(cols))]
        for r in rows[:50]:
            lines.append(" | ".join(str(r[c]) if r[c] is not None else "NULL" for c in cols))
        if len(rows) > 50:
            lines.append(f"... (showing 50 of {len(rows)} rows)")
        return "\n".join(lines)
    except Exception as e:
        return f"ERROR: {e}"


def _run_list_files(pattern: str) -> str:
    files = glob.glob(str(PROJECT_ROOT / pattern), recursive=True)
    rel = sorted(str(Path(f).relative_to(PROJECT_ROOT)) for f in files)
    return "\n".join(rel) if rel else "(no files found)"


def dispatch_tool(name: str, inputs: dict) -> str:
    if name == "read_file":
        return _run_read_file(inputs["path"])
    if name == "query_db":
        return _run_query_db(inputs["sql"])
    if name == "list_files":
        return _run_list_files(inputs["pattern"])
    return f"ERROR: unknown tool '{name}'"


# ── System prompts ────────────────────────────────────────────────────────────

CLAUDE_SYSTEM = """\
Ты senior quant researcher и Python разработчик.
Ты работаешь над проектом dota_trader_v2 — paper-trading движком для ставок на Dota 2.
У тебя есть инструменты: read_file, query_db, list_files.
Используй их чтобы изучить код, базу данных и реальные числа.
Давай конкретные технически точные ответы со ссылками на строки кода и данные.
Пиши на русском языке.
"""

GPT_SYSTEM = """\
Ты senior code reviewer, quant critic и ML-специалист по спортивным ставкам.
Ты получаешь анализ от другого AI-агента (Claude), который изучил код проекта dota_trader_v2.
Твоя роль — критически оценить его выводы:
- найти что он пропустил или неверно интерпретировал
- указать на слабые места в аргументации
- добавить свои идеи и альтернативные подходы
- задать конкретные уточняющие вопросы
Будь строгим и технически конкретным. Пиши на русском языке.
"""


# ── Orchestrator ──────────────────────────────────────────────────────────────

class MultiAgentOrchestrator:

    def __init__(self, verbose: bool = True):
        self.claude_client = anthropic.Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY", "")
        )
        self.gpt_client = OpenAI(
            api_key=os.environ.get("OPENAI_API_KEY", "")
        )
        self.verbose = verbose
        self.session_log: list[dict] = []
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ── Logging ──

    def _print(self, header: str, text: str, color_code: str = ""):
        if not self.verbose:
            return
        reset = "\033[0m"
        sep = "─" * 60
        print(f"\n{color_code}{sep}")
        print(f"  {header}")
        print(f"{sep}{reset}")
        # Trim long output in terminal, full version goes to log
        preview = text if len(text) <= 3000 else text[:3000] + "\n…[truncated]"
        print(preview)

    def _record(self, agent: str, role: str, content: str):
        self.session_log.append({
            "time": datetime.now().isoformat(),
            "agent": agent,
            "role": role,
            "content": content,
        })

    # ── Claude agentic loop ──

    def ask_claude(self, history: list[dict]) -> str:
        """
        Запускает Claude с tool-use loop.
        Возвращает финальный текстовый ответ.
        """
        for iteration in range(MAX_TOOL_ITERATIONS):
            response = self.claude_client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=4096,
                system=CLAUDE_SYSTEM,
                tools=TOOLS,
                messages=history,
            )

            if response.stop_reason == "tool_use":
                # Собираем все tool_use блоки из ответа
                assistant_content = response.content
                tool_results = []

                for block in response.content:
                    if block.type == "tool_use":
                        result = dispatch_tool(block.name, block.input)
                        if self.verbose:
                            preview_input = json.dumps(block.input)[:80]
                            print(f"  \033[33m[tool]\033[0m {block.name}({preview_input})")
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })

                # Добавляем ответ ассистента и результаты инструментов в историю
                history.append({"role": "assistant", "content": assistant_content})
                history.append({"role": "user", "content": tool_results})
                continue

            # stop_reason == "end_turn" — извлекаем текст
            text = "".join(
                block.text for block in response.content if hasattr(block, "text")
            )
            return text

        return "ERROR: reached max tool iterations"

    # ── GPT critique ──

    def ask_gpt(self, claude_output: str, round_num: int, task: str) -> str:
        messages = [
            {"role": "system", "content": GPT_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Исходная задача: {task}\n\n"
                    f"--- Анализ от Claude (раунд {round_num}) ---\n\n"
                    f"{claude_output}\n\n"
                    f"---\n\nТвоя критика и дополнения:"
                ),
            },
        ]
        response = self.gpt_client.chat.completions.create(
            model=GPT_MODEL,
            messages=messages,
            max_tokens=2048,
            temperature=0.7,
        )
        return response.choices[0].message.content

    # ── Main orchestration loop ──

    def run(self, task: str, rounds: int = MAX_ROUNDS) -> str:
        BLUE   = "\033[34m"
        GREEN  = "\033[32m"
        PURPLE = "\033[35m"
        BOLD   = "\033[1m"
        RESET  = "\033[0m"

        print(f"\n{BOLD}{'═'*60}")
        print(f"  MULTI-AGENT SESSION  {self.session_id}")
        print(f"  Claude ({CLAUDE_MODEL}) ↔ GPT ({GPT_MODEL})")
        print(f"  Rounds: {rounds}")
        print(f"{'═'*60}{RESET}")
        print(f"\n{BOLD}TASK:{RESET} {task}\n")

        # История Claude (накапливается через все раунды)
        claude_history: list[dict] = [{"role": "user", "content": task}]

        # ── Раунд 0: Claude делает первичный анализ ──
        self._print("CLAUDE — первичный анализ", "", BLUE)
        claude_output = self.ask_claude(claude_history)
        self._print("CLAUDE →", claude_output, BLUE)
        self._record("claude", "analysis", claude_output)

        # ── Раунды: GPT критикует → Claude отвечает ──
        for r in range(1, rounds + 1):
            # GPT critique
            self._print(f"GPT — раунд {r} критика", "", GREEN)
            gpt_output = self.ask_gpt(claude_output, r, task)
            self._print(f"GPT → раунд {r}", gpt_output, GREEN)
            self._record("gpt", f"critique_round_{r}", gpt_output)

            # Claude отвечает на критику (кроме последнего раунда — там сразу синтез)
            if r < rounds:
                followup = (
                    f"GPT-4o прокритиковал твой анализ:\n\n{gpt_output}\n\n"
                    f"Ответь на критику: уточни спорные моменты, исправь ошибки, "
                    f"добавь упущенное. Будь конкретным."
                )
                claude_history.append({"role": "assistant", "content": claude_output})
                claude_history.append({"role": "user", "content": followup})

                claude_output = self.ask_claude(claude_history)
                self._print(f"CLAUDE → раунд {r} ответ", claude_output, BLUE)
                self._record("claude", f"response_round_{r}", claude_output)

        # ── Финальный синтез от Claude ──
        synthesis_prompt = (
            f"Последняя критика от GPT-4o:\n\n{gpt_output}\n\n"
            f"Теперь напиши финальный структурированный синтез всего диалога:\n"
            f"1. Ключевые выводы (с которыми оба агента согласны)\n"
            f"2. Спорные моменты (разные точки зрения)\n"
            f"3. Что пропустили (из критики GPT)\n"
            f"4. TOP-5 рекомендаций к действию по приоритету\n"
        )
        claude_history.append({"role": "assistant", "content": claude_output})
        claude_history.append({"role": "user", "content": synthesis_prompt})

        self._print("CLAUDE — финальный синтез", "", PURPLE)
        synthesis = self.ask_claude(claude_history)
        self._print("SYNTHESIS →", synthesis, PURPLE)
        self._record("claude", "synthesis", synthesis)

        # ── Сохранение лога ──
        log_dir = PROJECT_ROOT / "agents" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"session_{self.session_id}.json"
        log_path.write_text(
            json.dumps(self.session_log, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n\033[90m[Log saved → {log_path.relative_to(PROJECT_ROOT)}]\033[0m")

        return synthesis


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Multi-agent: Claude ↔ GPT-4o")
    parser.add_argument("task", nargs="?", default=None, help="Задача для агентов")
    parser.add_argument("--rounds", type=int, default=MAX_ROUNDS, help=f"Количество раундов (default {MAX_ROUNDS})")
    parser.add_argument("--quiet", action="store_true", help="Не выводить промежуточные шаги")
    args = parser.parse_args()

    # Проверка ключей
    missing = []
    if not os.environ.get("ANTHROPIC_API_KEY"):
        missing.append("ANTHROPIC_API_KEY")
    if not os.environ.get("OPENAI_API_KEY"):
        missing.append("OPENAI_API_KEY")
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}")
        print("Добавь их в .env файл.")
        sys.exit(1)

    task = args.task or input("Задача: ").strip()
    if not task:
        print("Задача не задана.")
        sys.exit(1)

    orchestrator = MultiAgentOrchestrator(verbose=not args.quiet)
    orchestrator.run(task, rounds=args.rounds)


if __name__ == "__main__":
    main()

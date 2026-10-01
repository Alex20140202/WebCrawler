from __future__ import annotations

import json
import os
import sys
from typing import Callable, Dict, List, Optional

Prompter = Callable[[str], str]

MODES = ("ask", "auto", "never")


class Answer:
    __slots__ = ("key", "question", "value", "source", "detail")

    def __init__(self, key: str, question: str, value, source: str, detail: str = ""):
        self.key = key
        self.question = question
        self.value = value
        self.source = source  # asked | auto | cached | default | never
        self.detail = detail

    def as_dict(self) -> Dict:
        return {
            "key": self.key,
            "question": self.question,
            "value": self.value,
            "source": self.source,
            "detail": self.detail,
        }


class Interactor:
    """Decides when to ask the user something and when to act on its own.

    Modes:
      ask    - ask whenever a decision is ambiguous, cache by key so we only
               ask once per topic per run
      auto   - pick the safe default for anything reversible, ask only when the
               choice is expensive or one-way
      never  - never ask, always default (safe for unattended runs)

    Every decision is recorded so the report can show what was asked and why.
    """

    def __init__(
        self,
        mode: str = "auto",
        max_questions: int = 10,
        prompter: Prompter = None,
        interactive: Optional[bool] = None,
        logger: Optional[Callable[[str], None]] = None,
    ):
        if mode not in MODES:
            raise ValueError("mode must be one of %s" % (MODES,))
        self.mode = mode
        self.max_questions = max(0, max_questions)
        self.prompter = prompter or self._default_prompt
        self.logger = logger
        self.answers: Dict[str, Answer] = {}
        self.questions_asked = 0
        self.questions_skipped = 0

        if interactive is None:
            interactive = self._stdin_is_tty()
        self.interactive = interactive
        if not interactive and mode == "ask":
            # A non-tty has nobody to answer, so degrade instead of hanging.
            self.mode = "auto"
            self._note("no interactive terminal; falling back to auto decisions")

    @staticmethod
    def _stdin_is_tty() -> bool:
        try:
            return bool(sys.stdin and sys.stdin.isatty())
        except (AttributeError, ValueError):
            return False

    @staticmethod
    def _default_prompt(message: str) -> str:
        try:
            return input(message)
        except (EOFError, KeyboardInterrupt):
            raise
        except OSError:
            return ""

    def _note(self, message: str) -> None:
        if self.logger:
            self.logger(message)

    def get(self, key: str, question: str, default, detail: str = "") -> Answer:
        """Return the answer for `key`, asking the user only if policy allows."""
        cached = self.answers.get(key)
        if cached is not None:
            return cached

        if self.mode == "never":
            answer = Answer(key, question, default, "never", detail)
            self.answers[key] = answer
            self._note("  [auto:no-ask] %s -> %r" % (key, default))
            return answer

        if self.mode == "auto":
            answer = Answer(key, question, default, "auto", detail)
            self.answers[key] = answer
            self._note("  [auto] %s -> %r%s"
                       % (key, default, (" (%s)" % detail) if detail else ""))
            return answer

        if not self.interactive:
            self.questions_skipped += 1
            answer = Answer(key, question, default, "default", detail)
            self.answers[key] = answer
            self._note("  [no-tty] %s -> %r" % (key, default))
            return answer

        if self.questions_asked >= self.max_questions:
            self.questions_skipped += 1
            answer = Answer(key, question, default, "default", detail)
            self.answers[key] = answer
            self._note("  [budget] %s -> %r (question budget spent)" % (key, default))
            return answer

        self.questions_asked += 1
        prompt = "%s [%s]: " % (question, default)
        if detail:
            prompt = "%s\n  %s\n%s" % (question, detail, prompt)
        try:
            raw = (self.prompter(prompt) or "").strip()
        except (EOFError, KeyboardInterrupt):
            self._note("  input closed; using default for %s" % key)
            raw = ""

        value = self._coerce(raw, default)
        answer = Answer(key, question, value, "asked" if raw else "default", detail)
        self.answers[key] = answer
        return answer

    def choose(self, key: str, question: str, options: List[str], default_index: int = 0,
               detail: str = "") -> Answer:
        """Present numbered options and return the chosen index."""
        if self.answers.get(key) is not None:
            return self.answers[key]

        if self.mode != "ask" or not self.interactive \
                or self.questions_asked >= self.max_questions:
            if self.mode == "ask":
                self.questions_skipped += 1
            answer = Answer(key, question, default_index,
                           "auto" if self.mode == "auto" else "default", detail)
            self.answers[key] = answer
            return answer

        self.questions_asked += 1
        print(question)
        if detail:
            print("  %s" % detail)
        for index, option in enumerate(options, start=1):
            marker = "*" if index - 1 == default_index else " "
            print("  %s %d) %s" % (marker, index, option))

        while True:
            try:
                raw = (self.prompter("choice [%d]: " % (default_index + 1)) or "").strip()
            except (EOFError, KeyboardInterrupt):
                raw = ""
            if not raw:
                value = default_index
                break
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                value = int(raw) - 1
                break
            print("  please enter a number between 1 and %d" % len(options))

        answer = Answer(key, question, value, "asked", detail)
        self.answers[key] = answer
        return answer

    def confirm(self, key: str, question: str, default: bool = True,
                detail: str = "") -> Answer:
        answer = self.get(key, question, default, detail)
        return answer

    @staticmethod
    def _coerce(raw: str, default):
        if not raw:
            return default
        if isinstance(default, bool):
            return raw.lower() in ("y", "yes", "true", "1", "on")
        if isinstance(default, int) and not isinstance(default, bool):
            try:
                return int(raw)
            except ValueError:
                return default
        if isinstance(default, float):
            try:
                return float(raw)
            except ValueError:
                return default
        return raw

    def transcript(self) -> List[Dict]:
        return [self.answers[key].as_dict() for key in sorted(self.answers)]

    def summary(self) -> Dict:
        return {
            "mode": self.mode,
            "interactive": self.interactive,
            "questions_asked": self.questions_asked,
            "questions_skipped": self.questions_skipped,
            "decisions": len(self.answers),
        }

    def save(self, path: str) -> Optional[str]:
        if not path:
            return None
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        payload = {"summary": self.summary(), "answers": self.transcript()}
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        return path

    def load(self, path: str) -> int:
        """Pre-seed answers from a previous run so repeat runs stay unattended."""
        if not path or not os.path.exists(path):
            return 0
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return 0
        count = 0
        for item in payload.get("answers", []):
            key = item.get("key")
            if not key or key in self.answers:
                continue
            self.answers[key] = Answer(
                key, item.get("question", key), item.get("value"), "cached",
                item.get("detail", ""),
            )
            count += 1
        return count
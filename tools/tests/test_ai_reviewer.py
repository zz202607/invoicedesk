from __future__ import annotations

import argparse
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import pytest

from tools import ai_reviewer
from tools.ai_reviewer import (
    AGENT_PROFILES,
    MARKER,
    MAX_COMMENT_CHARS,
    Config,
    ConfigError,
    Reviewer,
    ReviewerError,
    _render_comment,
    build_config,
    build_review_messages,
    call_chat,
    prepare_diff,
    split_diff,
)

DIFF = """diff --git a/android/Foo.kt b/android/Foo.kt
index 1111111..2222222 100644
--- a/android/Foo.kt
+++ b/android/Foo.kt
@@ -1,2 +1,3 @@
 fun main() {
+    println("hi")
 }
"""

BINARY = """diff --git a/assets/logo.png b/assets/logo.png
index 1111111..2222222 100644
Binary files a/assets/logo.png and b/assets/logo.png differ
"""

LOCK = """diff --git a/yarn.lock b/yarn.lock
--- a/yarn.lock
+++ b/yarn.lock
@@ -1 +1 @@
-old
+new
"""

RENAME = """diff --git a/old.kt b/new.kt
similarity index 100%
rename from old.kt
rename to new.kt
"""

PR = {
    "title": "Test PR",
    "body": "Adds a line.",
    "user": {"login": "zz"},
    "base": {"ref": "main"},
    "head": {"ref": "feature/test"},
    "changed_files": 1,
    "additions": 2,
    "deletions": 0,
}


def make_config(**overrides: Any) -> Config:
    base = Config(
        deepseek_api_key="sk-test",
        github_token="gh-token",
        repository="o/r",
        pr_number=1,
    )
    return replace(base, **overrides)


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        json_data: Any = None,
        text: str = "",
        json_error: bool = False,
    ) -> None:
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self._json_error = json_error

    def json(self) -> Any:
        if self._json_error:
            raise ValueError("not json")
        return self._json


class FakeSession:
    def __init__(self, handler: Callable[[str, str, dict[str, Any]], FakeResponse]):
        self._handler = handler

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        return self._handler(method, url, kwargs)


class HandlerAIFactory:
    """One fake DeepSeek session per call, with thread-safe call recording."""

    def __init__(self, handler: Callable[[str, str, dict[str, Any]], FakeResponse]):
        self._handler = handler
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._lock = threading.Lock()

    def __call__(self) -> FakeSession:
        return FakeSession(self._handle)

    def _handle(self, method: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        with self._lock:
            self.calls.append((method, url, kwargs))
            return self._handler(method, url, kwargs)


class SequenceAIFactory:
    """Returns queued responses in order, regardless of agent."""

    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._lock = threading.Lock()

    def __call__(self) -> FakeSession:
        return FakeSession(self._handle)

    def _handle(self, method: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        with self._lock:
            self.calls.append((method, url, kwargs))
            if not self._responses:
                return FakeResponse(status_code=500, text="exhausted")
            return self._responses.pop(0)


class FakeGitHub:
    def __init__(
        self,
        diff: str = DIFF,
        comments: list[dict[str, Any]] | None = None,
        pr: dict[str, Any] | None = None,
    ) -> None:
        self.diff = diff
        self.comments = comments or []
        self.pr = pr or PR
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        accept = (kwargs.get("headers") or {}).get("Accept", "")
        if method == "GET" and url.endswith("/pulls/1") and accept.endswith(".diff"):
            return FakeResponse(text=self.diff)
        if method == "GET" and url.endswith("/pulls/1"):
            return FakeResponse(json_data=self.pr)
        if method == "GET" and "/issues/1/comments" in url:
            return FakeResponse(json_data=self.comments)
        if method == "PATCH" and "/issues/comments/" in url:
            return FakeResponse(json_data={})
        if method == "POST" and "/issues/1/comments" in url:
            return FakeResponse(json_data={})
        return FakeResponse(status_code=404, text=f"unexpected {method} {url}")


def fake_ai_handler(
    *, fail_agents: set[str] | None = None, synthesis_error: bool = False
) -> Callable[[str, str, dict[str, Any]], FakeResponse]:
    fail_agents = fail_agents or set()

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        payload = kwargs["json"]
        system = payload["messages"][0]["content"]
        user = payload["messages"][1]["content"]
        if "审查汇总代理" in system:
            if synthesis_error:
                return FakeResponse(status_code=500, text="boom")
            return FakeResponse(
                json_data={
                    "choices": [{"message": {"content": "## 结论\n汇总完成\n## 发现\n- 一条汇总发现"}}],
                    "usage": {"total_tokens": 30},
                }
            )
        for title in ("正确性", "安全", "测试"):
            if f"{title}审查" in user:
                if title in fail_agents:
                    return FakeResponse(status_code=500, text="boom")
                return FakeResponse(
                    json_data={
                        "choices": [{"message": {"content": f"## 发现\n- {title}发现"}}],
                        "usage": {"total_tokens": 10},
                    }
                )
        return FakeResponse(status_code=400, text="unknown role")

    return handler


def chat_ok(content: str = "looks fine") -> FakeResponse:
    return FakeResponse(
        json_data={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }
    )


def local_args(diff_file: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        diff_file=diff_file, pr_title="Local", pr_body="", dry_run=False
    )


# ------------------------------------------------------------------ diff handling


def test_split_diff_splits_segments_and_reads_paths() -> None:
    segments = split_diff(DIFF + BINARY)
    assert [path for path, _ in segments] == ["android/Foo.kt", "assets/logo.png"]


def test_prepare_diff_skips_binary_lock_and_no_hunk_files() -> None:
    bundle = prepare_diff(DIFF + BINARY + LOCK + RENAME)
    assert bundle.included == ["android/Foo.kt"]
    joined = " ".join(bundle.skipped)
    assert "logo.png" in joined and "二进制" in joined
    assert "yarn.lock" in joined and "生成/锁文件" in joined
    assert "new.kt" in joined and "无文本改动" in joined


def test_prepare_diff_truncates_a_large_file() -> None:
    large = (
        "diff --git a/big.py b/big.py\n"
        "--- a/big.py\n+++ b/big.py\n@@ -1 +1 @@\n"
        "+" + ("x" * 500) + "\n"
    )
    bundle = prepare_diff(large, max_total=10_000, max_file=200, max_files=5)
    assert bundle.included == ["big.py"]
    assert "big.py" in bundle.truncated
    assert "本文件 diff 已截断" in bundle.text


def test_prepare_diff_respects_total_budget_and_lists_skipped() -> None:
    first = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n"
        "+" + ("a" * 180) + "\n"
    )
    second = (
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n"
        "+" + ("b" * 180) + "\n"
    )
    bundle = prepare_diff(first + second, max_total=300, max_file=1000, max_files=5)
    assert bundle.included == ["a.py"]
    assert any("b.py" in item and "总预算" in item for item in bundle.skipped)


def test_prepare_diff_handles_empty_input() -> None:
    bundle = prepare_diff("")
    assert bundle.text == ""
    assert bundle.included == []


def test_prepare_diff_marks_truncated_file_in_skipped_note() -> None:
    bundle = prepare_diff(DIFF, max_total=10_000, max_file=10_000)
    assert "审查器说明" not in bundle.text
    assert bundle.truncated == []


# --------------------------------------------------------------------- prompts


@pytest.mark.parametrize("agent_key", list(AGENT_PROFILES))
def test_review_messages_carry_role_diff_and_injection_warning(agent_key: str) -> None:
    messages = build_review_messages(AGENT_PROFILES[agent_key], PR, DIFF)
    system = messages[0]["content"]
    user = messages[1]["content"]
    assert "不可信数据" in system
    assert "忽略" in system
    assert AGENT_PROFILES[agent_key].title + "审查" in user
    assert DIFF in user
    assert "Test PR" in user
    assert "zz" in user


# ------------------------------------------------------------------ DeepSeek call


def test_call_chat_sends_expected_payload_and_parses_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = SequenceAIFactory([chat_ok("review text")])
    content, usage = call_chat(
        make_config(),
        [{"role": "user", "content": "hi"}],
        session_factory=factory,
    )
    assert content == "review text"
    assert usage["total_tokens"] == 7
    method, url, kwargs = factory.calls[0]
    assert method == "POST"
    assert url == "https://api.deepseek.com/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer sk-test"
    assert kwargs["json"]["model"] == "deepseek-flash"
    assert kwargs["json"]["stream"] is False
    assert kwargs["json"]["messages"] == [{"role": "user", "content": "hi"}]


def test_call_chat_retries_on_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ai_reviewer.time, "sleep", lambda _: None)
    factory = SequenceAIFactory(
        [FakeResponse(status_code=429, text="slow"), chat_ok()]
    )
    content, _ = call_chat(make_config(), [], session_factory=factory)
    assert content == "looks fine"
    assert len(factory.calls) == 2


def test_call_chat_does_not_retry_on_401_and_hides_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ai_reviewer.time, "sleep", lambda _: None)
    factory = SequenceAIFactory([FakeResponse(status_code=401, text="bad key")])
    with pytest.raises(ReviewerError) as excinfo:
        call_chat(make_config(), [], session_factory=factory)
    assert len(factory.calls) == 1
    assert "sk-test" not in str(excinfo.value)
    assert "401" in str(excinfo.value)


def test_call_chat_gives_up_after_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ai_reviewer.time, "sleep", lambda _: None)
    factory = SequenceAIFactory([FakeResponse(status_code=500, text="boom")])
    with pytest.raises(ReviewerError) as excinfo:
        call_chat(make_config(retries=1), [], session_factory=factory)
    assert len(factory.calls) == 2
    assert "已重试 1 次" in str(excinfo.value)


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(json_data={}),
        FakeResponse(json_data={"choices": []}),
        FakeResponse(json_data={"choices": [{"message": {"content": "   "}}]}),
        FakeResponse(json_data={}, json_error=True),
    ],
)
def test_call_chat_rejects_malformed_responses(response: FakeResponse) -> None:
    factory = SequenceAIFactory([response])
    with pytest.raises(ReviewerError):
        call_chat(make_config(), [], session_factory=factory)


# ---------------------------------------------------------------------- full run


def test_run_posts_one_synthesized_comment() -> None:
    github = FakeGitHub()
    ai = HandlerAIFactory(fake_ai_handler())
    result = Reviewer(
        make_config(), github_session=github, ai_session_factory=ai
    ).run()

    assert result == 0
    posts = [call for call in github.calls if call[0] == "POST"]
    assert len(posts) == 1
    body = posts[0][2]["json"]["body"]
    assert body.startswith(MARKER)
    assert "汇总完成" in body
    assert "一条汇总发现" in body
    assert "DeepSeek 多智能体代码审查" in body
    assert "正确性、安全、测试" in body
    assert "tokens 60" in body
    assert len(ai.calls) == 4  # three reviewers + one synthesis


def test_run_updates_existing_marker_comment() -> None:
    github = FakeGitHub(comments=[{"id": 42, "body": f"{MARKER}\nold review"}])
    ai = HandlerAIFactory(fake_ai_handler())
    result = Reviewer(
        make_config(), github_session=github, ai_session_factory=ai
    ).run()

    assert result == 0
    patches = [call for call in github.calls if call[0] == "PATCH"]
    assert len(patches) == 1
    assert patches[0][1].endswith("/issues/comments/42")
    assert not [call for call in github.calls if call[0] == "POST"]


def test_run_isolates_a_failed_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_reviewer.time, "sleep", lambda _: None)
    github = FakeGitHub()
    ai = HandlerAIFactory(fake_ai_handler(fail_agents={"安全"}))
    result = Reviewer(
        make_config(retries=0),
        github_session=github,
        ai_session_factory=ai,
    ).run()

    assert result == 0
    body = [call for call in github.calls if call[0] == "POST"][0][2]["json"]["body"]
    assert "⚠️ 失败：security" in body
    assert "一条汇总发现" in body


def test_run_fails_when_every_agent_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_reviewer.time, "sleep", lambda _: None)
    github = FakeGitHub()
    ai = HandlerAIFactory(fake_ai_handler(fail_agents={"正确性", "安全", "测试"}))
    with pytest.raises(ReviewerError):
        Reviewer(
            make_config(retries=0),
            github_session=github,
            ai_session_factory=ai,
        ).run()
    assert not [call for call in github.calls if call[0] in {"POST", "PATCH"}]


def test_run_uses_section_report_when_synthesis_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ai_reviewer.time, "sleep", lambda _: None)
    github = FakeGitHub()
    ai = HandlerAIFactory(fake_ai_handler(synthesis_error=True))
    Reviewer(
        make_config(retries=0),
        github_session=github,
        ai_session_factory=ai,
    ).run()

    body = [call for call in github.calls if call[0] == "POST"][0][2]["json"]["body"]
    assert "分节报告" in body
    assert "正确性发现" in body
    assert "测试发现" in body
    assert "汇总失败" in body


def test_run_skips_empty_or_binary_only_diff() -> None:
    github = FakeGitHub(diff=BINARY)
    ai = HandlerAIFactory(fake_ai_handler())
    result = Reviewer(
        make_config(), github_session=github, ai_session_factory=ai
    ).run()

    assert result == 0
    assert ai.calls == []
    assert not [call for call in github.calls if call[0] in {"POST", "PATCH"}]


def test_run_dry_run_posts_nothing() -> None:
    github = FakeGitHub()
    ai = HandlerAIFactory(fake_ai_handler())
    result = Reviewer(
        make_config(dry_run=True),
        github_session=github,
        ai_session_factory=ai,
    ).run()

    assert result == 0
    assert not [call for call in github.calls if call[0] in {"POST", "PATCH"}]


def test_run_fetches_diff_with_diff_accept_header() -> None:
    github = FakeGitHub()
    ai = HandlerAIFactory(fake_ai_handler())
    Reviewer(
        make_config(dry_run=True),
        github_session=github,
        ai_session_factory=ai,
    ).run()

    diff_calls = [
        call
        for call in github.calls
        if call[0] == "GET"
        and call[1].endswith("/pulls/1")
        and (call[2].get("headers") or {}).get("Accept", "").endswith(".diff")
    ]
    assert len(diff_calls) == 1


def test_local_diff_file_dry_run_prints_comment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    diff_file = tmp_path / "changes.diff"
    diff_file.write_text(DIFF, encoding="utf-8")
    ai = HandlerAIFactory(fake_ai_handler())
    config = make_config(
        diff_file=str(diff_file),
        dry_run=True,
        github_token="",
        repository="",
        pr_number=0,
    )
    result = Reviewer(config, ai_session_factory=ai).run()
    captured = capsys.readouterr()
    assert result == 0
    assert MARKER in captured.out
    assert len(ai.calls) == 4


def test_comment_is_truncated_to_github_limit() -> None:
    comment = _render_comment(
        make_config(),
        PR,
        "x" * 100_000,
        [],
        {},
        1.0,
    )
    assert len(comment) <= MAX_COMMENT_CHARS
    assert "已截断" in comment


# ------------------------------------------------------------------------ config


def test_build_config_requires_api_key(tmp_path: Path) -> None:
    diff_file = tmp_path / "a.diff"
    diff_file.write_text(DIFF, encoding="utf-8")
    with pytest.raises(ConfigError):
        build_config({}, local_args(str(diff_file)))


def test_build_config_requires_github_fields_for_pr_mode() -> None:
    env = {"DEEPSEEK_API_KEY": "sk"}
    with pytest.raises(ConfigError):
        build_config(env, local_args())

    env.update({"GITHUB_TOKEN": "gh", "GITHUB_REPOSITORY": "o/r"})
    with pytest.raises(ConfigError):
        build_config(env, local_args())

    env["PR_NUMBER"] = "0"
    with pytest.raises(ConfigError):
        build_config(env, local_args())


def test_build_config_parses_valid_pr_env() -> None:
    env = {
        "DEEPSEEK_API_KEY": "sk",
        "GITHUB_TOKEN": "gh",
        "GITHUB_REPOSITORY": "o/r",
        "PR_NUMBER": "12",
        "DEEPSEEK_MODEL": "deepseek-flash",
        "REVIEW_AGENTS": "correctness,tests",
        "REVIEW_SYNTHESIS": "0",
        "REVIEW_MAX_DIFF_CHARS": "40000",
    }
    config = build_config(env, local_args())
    assert config.pr_number == 12
    assert config.agents == ("correctness", "tests")
    assert config.synthesis is False
    assert config.max_diff_chars == 40_000
    assert config.dry_run is False


def test_build_config_rejects_unknown_agent() -> None:
    env = {
        "DEEPSEEK_API_KEY": "sk",
        "GITHUB_TOKEN": "gh",
        "GITHUB_REPOSITORY": "o/r",
        "PR_NUMBER": "1",
        "REVIEW_AGENTS": "correctness,bogus",
    }
    with pytest.raises(ConfigError):
        build_config(env, local_args())


def test_build_config_local_mode_forces_dry_run(tmp_path: Path) -> None:
    diff_file = tmp_path / "a.diff"
    diff_file.write_text(DIFF, encoding="utf-8")
    config = build_config(
        {"DEEPSEEK_API_KEY": "sk"}, local_args(str(diff_file))
    )
    assert config.dry_run is True
    assert config.pr_number == 0
    assert config.github_token == ""


def test_build_config_missing_local_diff_file_errors() -> None:
    with pytest.raises(ConfigError):
        build_config({"DEEPSEEK_API_KEY": "sk"}, local_args("missing.diff"))

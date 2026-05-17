#!/usr/bin/env python3
"""GitHub Actions から呼ばれる Claude AI コードレビュースクリプト。

処理の流れ:
  1. PR のメタ情報を GitHub API から取得
  2. base..head の unified diff を git で取得
  3. Python / TypeScript / JavaScript の変更ファイルに絞る
  4. 各ファイルについて RIGHT 側(=PR 後)の変更行番号を抽出
  5. Claude API に diff を投げ、JSON 形式のインラインコメントを受け取る
  6. 行番号が diff に含まれているかを検証して、無効なものは捨てる
  7. GitHub の Pull Request Review API でまとめてインラインコメントを投稿

環境変数:
  ANTHROPIC_API_KEY   : Anthropic API キー
  GITHUB_TOKEN        : GitHub Actions の自動発行トークン
  GITHUB_REPOSITORY   : "owner/repo"
  PR_NUMBER           : Pull Request 番号
  BASE_SHA / HEAD_SHA : diff 計算用 SHA
  CLAUDE_MODEL        : 使用する Claude モデル名
  MAX_DIFF_CHARS      : Claude に送る diff の最大文字数(超えると切り詰め)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

import requests
from anthropic import Anthropic

from prompts import SYSTEM_PROMPT, build_user_prompt

# ---- 設定 ---------------------------------------------------------------

REVIEW_FILE_EXTENSIONS = {
    # Python
    ".py",
    # TypeScript / JavaScript
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
}

# パスに含まれていたらレビュー対象外にする (自動生成/ベンダー)
SKIP_PATH_FRAGMENTS = (
    "node_modules/",
    "dist/",
    "build/",
    ".next/",
    "vendor/",
    "__pycache__/",
    ".min.js",
    ".min.css",
    "package-lock.json",
    "yarn.lock",
    "poetry.lock",
)

GITHUB_API = "https://api.github.com"


# ---- データ構造 ---------------------------------------------------------

@dataclass
class FileDiff:
    path: str
    # RIGHT side (=PR 後ファイル) の、コメント可能な行番号集合
    right_lines: set[int] = field(default_factory=set)
    # LEFT side (=base 側) の、削除/変更前行番号集合
    left_lines: set[int] = field(default_factory=set)
    # 表示用の生 diff 断片
    raw: str = ""


# ---- ユーティリティ -----------------------------------------------------

def env(name: str, required: bool = True, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"[claude-review] required env var missing: {name}")
    return val or ""


def run(cmd: list[str]) -> str:
    """サブプロセスを同期実行して stdout を返す。"""
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit(
            f"[claude-review] command failed: {' '.join(cmd)}\n"
            f"stderr: {result.stderr}"
        )
    return result.stdout


def gh_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "claude-ai-code-review",
    }


# ---- GitHub / git -------------------------------------------------------

def fetch_pr_meta(repo: str, pr_number: int, token: str) -> dict[str, Any]:
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}"
    r = requests.get(url, headers=gh_headers(token), timeout=30)
    r.raise_for_status()
    return r.json()


def get_unified_diff(base_sha: str, head_sha: str) -> str:
    """base..head の unified diff を取得。リネーム検出 + 3 行コンテキスト。"""
    # base コミットが浅い checkout のせいで無い可能性に備え、必要なら fetch する
    fetch = subprocess.run(
        ["git", "cat-file", "-e", base_sha],
        capture_output=True, text=True
    )
    if fetch.returncode != 0:
        # 念のため fetch
        subprocess.run(["git", "fetch", "--no-tags", "--depth=200", "origin", base_sha],
                       capture_output=True, text=True)

    return run(["git", "diff", "--unified=3", "--no-color", "-M", base_sha, head_sha])


# ---- Diff 解析 ----------------------------------------------------------

DIFF_FILE_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_unified_diff(diff_text: str) -> list[FileDiff]:
    """ファイルごとに分割し、各ハンクから変更行番号を抽出する。"""
    files: list[FileDiff] = []
    current: FileDiff | None = None
    # ハンク内カウンタ
    left_no = right_no = 0
    in_hunk = False

    for line in diff_text.splitlines():
        m_file = DIFF_FILE_RE.match(line)
        if m_file:
            # 新しいファイル開始
            new_path = m_file.group(2)  # "b/" 側 = PR 後のパス
            current = FileDiff(path=new_path)
            files.append(current)
            in_hunk = False
            if current is not None:
                current.raw += line + "\n"
            continue

        if current is None:
            continue

        current.raw += line + "\n"

        # ファイル削除 / 新規 / index 行などはスキップ
        if line.startswith(("---", "+++", "index ", "similarity ", "rename ",
                            "new file", "deleted file", "Binary ", "old mode",
                            "new mode")):
            continue

        m_hunk = HUNK_RE.match(line)
        if m_hunk:
            left_no = int(m_hunk.group(1))
            right_no = int(m_hunk.group(3))
            in_hunk = True
            continue

        if not in_hunk:
            continue

        if line.startswith("+") and not line.startswith("+++"):
            current.right_lines.add(right_no)
            right_no += 1
        elif line.startswith("-") and not line.startswith("---"):
            current.left_lines.add(left_no)
            left_no += 1
        elif line.startswith(" "):
            left_no += 1
            right_no += 1
        # "\ No newline at end of file" 等は無視

    return files


def should_review(path: str) -> bool:
    if any(frag in path for frag in SKIP_PATH_FRAGMENTS):
        return False
    _, ext = os.path.splitext(path)
    return ext.lower() in REVIEW_FILE_EXTENSIONS


def filter_files(files: list[FileDiff]) -> list[FileDiff]:
    return [f for f in files if should_review(f.path) and (f.right_lines or f.left_lines)]


def summarize_changed_lines(files: list[FileDiff]) -> str:
    """Claude に渡す「コメント可能な行番号」のサマリを作る。"""
    lines: list[str] = []
    for f in files:
        if not f.right_lines:
            lines.append(f"- {f.path}: (削除のみ)")
            continue
        sorted_lines = sorted(f.right_lines)
        # 連続範囲に圧縮して可読性を上げる
        ranges: list[str] = []
        start = prev = sorted_lines[0]
        for n in sorted_lines[1:]:
            if n == prev + 1:
                prev = n
                continue
            ranges.append(f"{start}-{prev}" if start != prev else f"{start}")
            start = prev = n
        ranges.append(f"{start}-{prev}" if start != prev else f"{start}")
        lines.append(f"- {f.path}: {', '.join(ranges)}")
    return "\n".join(lines) if lines else "(対象ファイルなし)"


def truncate_diff(diff_text: str, max_chars: int) -> str:
    if len(diff_text) <= max_chars:
        return diff_text
    head = diff_text[: max_chars - 200]
    return head + "\n\n[... diff truncated due to size limit ...]\n"


# ---- Claude 呼び出し ----------------------------------------------------

def call_claude(model: str, system: str, user: str) -> str:
    client = Anthropic()  # ANTHROPIC_API_KEY を環境変数から自動読み取り
    msg = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    # text ブロックを連結
    parts: list[str] = []
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts).strip()


JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """Claude の出力からトップレベル JSON を取り出す。"""
    # まず ``` で囲まれた JSON を試す
    m = JSON_BLOCK_RE.search(text)
    candidate = m.group(1) if m else text

    # 先頭 '{' から末尾 '}' までを抽出
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON object in Claude response: {text[:300]}")
    raw = candidate[start: end + 1]
    return json.loads(raw)


# ---- GitHub レビュー投稿 ------------------------------------------------

def validate_comments(
    comments: list[dict[str, Any]],
    files: list[FileDiff],
) -> list[dict[str, Any]]:
    """diff に含まれない行へのコメントを除外し、API 形式に整える。"""
    by_path: dict[str, FileDiff] = {f.path: f for f in files}
    valid: list[dict[str, Any]] = []

    for c in comments:
        path = c.get("path")
        line = c.get("line")
        side = (c.get("side") or "RIGHT").upper()
        body = c.get("body") or ""

        if not path or not isinstance(line, int) or not body:
            print(f"[claude-review] skip malformed comment: {c}")
            continue
        if path not in by_path:
            print(f"[claude-review] skip comment for unknown file: {path}")
            continue

        target = by_path[path]
        allowed = target.right_lines if side == "RIGHT" else target.left_lines
        if line not in allowed:
            print(f"[claude-review] skip out-of-diff comment: {path}:{line} ({side})")
            continue

        # severity ラベルを body に前置(任意)
        severity = (c.get("severity") or "").lower()
        category = (c.get("category") or "").lower()
        if severity or category:
            tag = f"_severity: {severity or 'n/a'} · category: {category or 'n/a'}_\n\n"
            body = tag + body

        valid.append({
            "path": path,
            "line": line,
            "side": side,
            "body": body,
        })

    return valid


def post_review(
    repo: str,
    pr_number: int,
    head_sha: str,
    token: str,
    body: str,
    comments: list[dict[str, Any]],
) -> None:
    """インラインコメント付きの review を 1 つ投稿する (COMMENT event)。"""
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/reviews"
    payload = {
        "commit_id": head_sha,
        "body": body,
        "event": "COMMENT",  # APPROVE / REQUEST_CHANGES にはしない
        "comments": comments,
    }
    r = requests.post(url, headers=gh_headers(token), json=payload, timeout=60)
    if r.status_code >= 300:
        print(f"[claude-review] review API error {r.status_code}: {r.text[:500]}")
        # インラインコメントが全部弾かれた場合、本文のみで再送
        if comments:
            print("[claude-review] retrying without inline comments...")
            payload["comments"] = []
            r2 = requests.post(url, headers=gh_headers(token), json=payload, timeout=60)
            r2.raise_for_status()
        else:
            r.raise_for_status()
    else:
        print(f"[claude-review] review posted: {r.json().get('html_url')}")


# ---- メイン -------------------------------------------------------------

def main() -> int:
    repo = env("GITHUB_REPOSITORY")
    pr_number = int(env("PR_NUMBER"))
    base_sha = env("BASE_SHA")
    head_sha = env("HEAD_SHA")
    token = env("GITHUB_TOKEN")
    model = env("CLAUDE_MODEL", required=False, default="claude-sonnet-4-6")
    max_chars = int(env("MAX_DIFF_CHARS", required=False, default="120000"))
    env("ANTHROPIC_API_KEY")  # 存在チェックのみ。SDK が直接読む

    print(f"[claude-review] {repo}#{pr_number}  base={base_sha[:7]} head={head_sha[:7]}")

    pr = fetch_pr_meta(repo, pr_number, token)
    pr_title = pr.get("title", "")
    pr_body = pr.get("body", "") or ""

    diff_text = get_unified_diff(base_sha, head_sha)
    if not diff_text.strip():
        print("[claude-review] empty diff, nothing to review")
        return 0

    all_files = parse_unified_diff(diff_text)
    target_files = filter_files(all_files)
    if not target_files:
        print("[claude-review] no Python/TS/JS files changed, skipping")
        return 0

    # Claude には対象ファイルだけの diff を渡す(node_modules 等のノイズ除去)
    focused_diff = "\n".join(f.raw for f in target_files)
    focused_diff = truncate_diff(focused_diff, max_chars)
    changed_summary = summarize_changed_lines(target_files)

    user_prompt = build_user_prompt(
        repo=repo,
        pr_number=pr_number,
        pr_title=pr_title,
        pr_body=pr_body,
        diff_text=focused_diff,
        changed_lines_summary=changed_summary,
    )

    print(f"[claude-review] calling Claude ({model}), diff={len(focused_diff)} chars, "
          f"files={len(target_files)}")
    raw_response = call_claude(model, SYSTEM_PROMPT, user_prompt)

    try:
        parsed = extract_json(raw_response)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"[claude-review] failed to parse Claude response: {e}")
        # フォールバック: 全文を overall コメントとして投稿
        post_review(
            repo, pr_number, head_sha, token,
            body=f"## Claude AI Code Review (raw output)\n\n{raw_response}",
            comments=[],
        )
        return 0

    overall = parsed.get("overall_comment") or "(no overall comment)"
    raw_comments = parsed.get("comments") or []
    valid_comments = validate_comments(raw_comments, target_files)

    header = "## Claude AI Code Review\n\n"
    footer = (
        f"\n\n---\n"
        f"_model: `{model}` · inline comments: {len(valid_comments)} / "
        f"{len(raw_comments)} (others were outside diff)_"
    )
    body = header + overall + footer

    post_review(repo, pr_number, head_sha, token, body, valid_comments)
    return 0


if __name__ == "__main__":
    sys.exit(main())

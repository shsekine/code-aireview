# Claude AI Code Review for GitHub PRs

GitHub に Pull Request を上げると、Claude (Anthropic API) が自動的にコードを
レビューしてインラインコメントを残してくれる GitHub Actions ワークフローです。

- **対象トリガー**: PR の `opened` / `synchronize` / `reopened`
- **対象言語**: Python (`.py`) と TypeScript / JavaScript (`.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs`)
- **出力**: PR に対する **インラインレビューコメント** (`COMMENT` イベント)
- **観点**: 一般的なコード品質 (correctness / readability / maintainability / performance / style)

## ファイル構成

```
.github/
  workflows/
    claude-review.yml      # GitHub Actions ワークフロー定義
  scripts/
    claude_review.py       # メインスクリプト (diff取得→Claude→コメント投稿)
    prompts.py             # Claude に渡す system / user プロンプト
```

## セットアップ

### 1. リポジトリにこの一式を追加

`.github/` ディレクトリ以下をそのままターゲットリポジトリにコピーしてコミット
してください。

### 2. Anthropic API キーを登録

GitHub の **リポジトリ Settings → Secrets and variables → Actions → New repository secret** で:

| Name                | Value                          |
|---------------------|--------------------------------|
| `ANTHROPIC_API_KEY` | `sk-ant-...` (Anthropic で発行) |

`GITHUB_TOKEN` は Actions が自動で発行するので登録不要です。

### 3. PR への書き込み権限を確認

ワークフローは `permissions: pull-requests: write` を宣言しています。
**Settings → Actions → General → Workflow permissions** が
"Read and write permissions" になっているか、少なくとも
"Read repository contents and packages permissions" + 上書きを許可している
ことを確認してください。

### 4. (任意) モデルを変更

`.github/workflows/claude-review.yml` の `CLAUDE_MODEL` を編集します。
デフォルトは `claude-sonnet-4-6`。速度優先なら `claude-haiku-4-5-20251001`、
精度優先なら `claude-opus-4-6` も選べます。

## 使い方

通常通り PR を作成するだけで、Actions タブに `Claude AI Code Review`
ジョブが流れ、数十秒〜数分で PR にレビューが追加されます。

- レビューをスキップしたい PR は、**タイトルに `[skip-review]` を含める**
  と Job が走りません。
- 同じ PR に追加 push をするたびに最新の diff に対して再レビューが走ります。

## どんなレビューが返ってくるか

各インラインコメントは次の形式で付きます:

```
_severity: high · category: correctness_

**[correctness]** ここで `await` を付け忘れているため Promise が
そのまま捨てられています。エラーが握り潰されるので以下のように修正してください。

```ts
await saveUser(user);
```
```

PR 全体に対する 2〜4 文のサマリも、最初のレビュー本文として投稿されます。

## ノイズを減らす工夫 (内部仕様メモ)

- `node_modules/`, `dist/`, `*.min.js`, lockfile などは自動でスキップ
- 巨大な diff は `MAX_DIFF_CHARS` (デフォルト 120,000 文字) で切り詰め
- Claude が diff 外の行番号を指定したコメントはサーバ送信前に破棄
- JSON パースに失敗した場合はフォールバックで Raw 出力を本文に投稿

## トラブルシューティング

| 症状                                            | 確認ポイント                                                                 |
|-------------------------------------------------|------------------------------------------------------------------------------|
| Job が起動しない                                 | PR のタイトルに `[skip-review]` が入っていないか / Workflow が有効か         |
| `ANTHROPIC_API_KEY` の env var エラー            | Secrets に登録されているか、名前のタイポがないか                              |
| 403 で review API が失敗                         | Workflow permissions が write になっているか                                   |
| インラインコメントが付かず本文のみになる         | diff 外の行に Claude がコメントしていた可能性 (Actions ログに `skip out-of-diff` が出ます) |
| 出力が JSON でなく文章になっている              | モデルを上位 (sonnet → opus) に上げるか、`max_tokens` を増やす                |

## カスタマイズしたい

- レビュー観点を増やす → `.github/scripts/prompts.py` の `SYSTEM_PROMPT` を編集
- 対象拡張子を変える → `claude_review.py` の `REVIEW_FILE_EXTENSIONS`
- 除外パスを増やす → `claude_review.py` の `SKIP_PATH_FRAGMENTS`
- Approve/Request changes に変える → `post_review` の `event` を変更
  (誤検知での自動 block は危険なので推奨しません)

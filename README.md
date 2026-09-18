# Premiere Skills (Cut & SRT-Fast)

Premiere Pro 動画編集ワークフローを自動化するスキル集です。
無音・雑音区間の自動ジェットカット（`/cut`）および、Apple Silicon GPU / Whisper を用いた高速SRT字幕生成（`/srt-fast`）を同梱しています。

**Antigravity** および **Codex** の両環境でプラグインとして動作します。

---

## Antigravity への導入コード

### 1. ターミナルで実行する場合（1行で即時導入）
```bash
curl -fsSL https://raw.githubusercontent.com/fuuuuuuma/premiere-skills/main/install.sh | bash
```

または Git と `agy` CLI を使う場合:
```bash
git clone --depth 1 https://github.com/fuuuuuuma/premiere-skills.git /tmp/premiere-skills && \
agy plugin install /tmp/premiere-skills && \
rm -rf /tmp/premiere-skills
```

### 2. Antigravity チャットに投げて導入させる場合
以下のプロンプトを Antigravity のチャットにそのまま送信してください：

```text
以下のコマンドを実行して、GitHubの premiere-skills プラグインを Antigravity に導入してください。
curl -fsSL https://raw.githubusercontent.com/fuuuuuuma/premiere-skills/main/install.sh | bash
```

---

## Codex への導入コマンド

```bash
codex plugin marketplace add fuuuuuuma/premiere-skills
codex plugin add premiere-skills@premiere-skills
```

---

## 同封スキル一覧

| コマンド | 用途 | 入力 | 出力 |
|---|---|---|---|
| `/cut` | Premiere Pro XML の無音・雑音区間を自動ジェットカット | `.xml` | `output/cut/<basename>_カット済み.xml` |
| `/srt-fast` | 単一パスGPU転写（mlx-whisper）＋並列改行による高速日本語SRT字幕生成 | `.wav`（＋任意 `.xml`） | `<basename>.fast.srt` |

### 使い方

- **無音カット**:
  ```text
  /cut /path/to/your_timeline.xml
  ```
- **高速字幕生成**:
  ```text
  /srt-fast /path/to/your_audio.wav
  ```

## 必要な環境
- Python 3.9 以降
- `ffmpeg` (`brew install ffmpeg`)
- `mlx-whisper` または `faster-whisper` (`pip3 install --user mlx-whisper faster-whisper`)

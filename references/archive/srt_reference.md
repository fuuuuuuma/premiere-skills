# SRT リファレンス（トラブルシューティング・環境・WAV設定）

問題発生時や環境確認時に参照する。

## 禁則事項

1. **LLM 判断を機械ロジックで代替** — Python で `should_emit_between()` 等の品詞ベース emit 関数を書くのは禁止
2. **fugashi/Tier システムへの回帰** — ルールベースは候補抽出の補助のみ。最終判断は LLM
3. **CORRECTIONS 辞書未登録の固有名詞を lines.txt だけ変える** — 必ず辞書と lines.txt を同時に修正する

## 学習ワークフロー（継続改善サイクル）

1. `/srt` で生成
2. Premiere Pro でテロップを手動微調整
3. 修正済み SRT + WAV + XML を `@修正済みSRT @WAV @XML` で渡す
4. 差分分析 → `feedback_srt_grouping_rules.md` + `telop_channel_patterns.md` に蓄積
5. 次回の生成に反映

## トラブルシューティング

| エラー | 対処 |
|---|---|
| `faster-whisper が見つかりません` | `pip3 install --user faster-whisper` |
| Whisperタイムアウト | Bashタイムアウトを 600000ms に設定 |
| Whisperハリュシネーションループ | `condition_on_previous_text=False` を確認 |
| SRT が Premiere で文字化け | UTF-8 BOM + CRLF か品質チェックで確認 |
| XMLパースエラー | Premiere から「Final Cut Pro XML」で再書き出し（prproj は不可） |
| `segments.json が見つからない` | Step 3（Whisper 転写）を再実行 |

## 対応環境

- macOS / Linux（Windows は未検証）
- Python 3.9 以降
- Claude Code CLI
- faster-whisper + ffmpeg

## 推奨WAV書き出し設定

**重要**: WAV は Premiere Pro タイムラインの **OP 以降** の音声を書き出すこと。

| 項目 | 推奨値 | 備考 |
|---|---|---|
| 書き出し範囲 | OP終了〜本編終了 | WAV の 0 秒 = タイムラインの OP 終了位置 |
| サンプルレート | 16,000 Hz | Whisper 内部処理と同一 |
| チャンネル | モノラル | ステレオ不要 |
| サンプルサイズ | 16-bit | 24-bit は不要 |

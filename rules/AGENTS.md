# Premiere Skills Rules (Cut & SRT-Fast)

Premiere Pro 動画編集ワークフローを自動化するスキル集（/cut・/srt-fast）の利用ルールです。

## スキル一覧
- `/cut`: Premiere Pro XML の無音・雑音区間を自動カットし、編集済みXMLを出力する。
- `/srt-fast`: 単一パスGPU転写（mlx-whisper）とサブエージェント並列改行による高速SRT字幕生成。

## 基本方針
1. 原本素材を変更・上書き・削除しない。出力は常に別名または指定出力先ディレクトリに保存する。
2. 処理完了時は、成果物の絶対パスと確認結果を明確に報告する。

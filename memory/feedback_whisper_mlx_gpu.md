# 転写エンジン mlx-whisper (GPU) 化と gap補完（v6.1・2026-07-03）

## 結論

- `/srt` `/srt-fast` の転写既定は **mlx-whisper large-v3-turbo (Apple GPU)**。
  実測(M4 Max・180s実音声・word_timestamps込み): CPU large-v3 54.5s → **17.2s（3.2倍）**
- エンジン選択・転写パラメータ・補正は `whisper_to_srt.py run_whisper()` に一元化。
  `whisper_chunk.py` は自前転写をやめ canonical に委譲（分岐持つと精度乖離するため）
- `SRT_WHISPER_ENGINE=cpu` で従来エンジン強制 / `SRT_WHISPER_MODEL` で mlx モデル差し替え

## ハマりどころ（再発防止）

1. **mlx-whisper は VAD 無し** → 長い無音明けの短い接続句（「聞いてたが」等）を落とす。
   実測で 180s 中 3フレーズ欠落。→ 未カバー区間(≥1.5s)だけ CPU large-v3+VAD で補完転写する
   gap rescue を run_whisper に内蔵して解決。カバレッジは従来比同等以上を確認済み
2. **gap を1区間ずつ CPU 転写すると激遅**（Whisper は音声長に関わらず30秒窓単位で推論。
   11区間で+61s、長尺では破綻）→ 全区間を2秒無音スペーサ入りで連結し**1回で転写**
3. **連結転写は30秒窓内でスライスが融合する**（'聞いてたが'+'ます'+'じゃあ…'が1セグメント化）
   → セグメント単位でなく**語タイムスタンプ単位**でスライスへ写像し、スライスごとに再構成
4. **mlx large-v3（非turbo）はGPUでもフレーズ欠落があり turbo より劣った**（要注意・直感に反する）
5. CPU側の徒労リスト: cpu_threads増=逆効果 / BatchedInferencePipeline=効果なし＋セグメント粗化 /
   turbo CPU 3並列=+25%どまり（turboはエンコーダ律速でCPU飽和）
6. GPU は単一資源なので /srt-fast の転写並列の伸びは縮小（悪影響はなし）。
   意味区切り改行のLLM並列は引き続き有効

## 品質エビデンス（sample180・対baseline）

- テキスト類似度 0.91（差分は表記ゆれ: 数字・アルファベット化で、チャンネル表記規則に整合）
- 一致語の開始時刻ずれ: 中央値 80ms / p95 250ms（difflib全体アライメント＋カット点スナップで吸収域）
- 全期間カバー・反復ハルシネーション 0件（38分フル動画でも確認）

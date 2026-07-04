# SRT 品質チェック（Step 7）

Step 6 で生成された SRT に対して以下の Python コードを Bash で実行し、結果を報告する。

```bash
python3 -c "
with open('<srt>', 'r', encoding='utf-8-sig') as f:
    content = f.read()
blocks = content.strip().split('\n\n')
entries = []
for block in blocks:
    lines = block.strip().split('\n')
    if len(lines) >= 3:
        entries.append((int(lines[0]), lines[1], '\n'.join(lines[2:])))
def parse_time(t):
    h, m, rest = t.split(':')
    s, ms = rest.split(',')
    return int(h)*3600 + int(m)*60 + int(s) + int(ms)/1000
total = len(entries)
over25 = sum(1 for _,_,t in entries if len(t) > 25)
range21_25 = sum(1 for _,_,t in entries if 21 <= len(t) <= 25)
under4 = sum(1 for _,_,t in entries if len(t) < 4)
zero_dur = sum(1 for _,tl,_ in entries if parse_time(tl.split(' --> ')[1]) <= parse_time(tl.split(' --> ')[0]))
gaps500 = overlaps = 0
for i in range(len(entries)-1):
    end_t = parse_time(entries[i][1].split(' --> ')[1])
    start_t = parse_time(entries[i+1][1].split(' --> ')[0])
    g = start_t - end_t
    if g > 0.5: gaps500 += 1
    if g < -0.001: overlaps += 1
raw = open('<srt>', 'rb').read()
bom = 'OK' if raw[:3] == b'\xef\xbb\xbf' else 'NG'
crlf = 'OK' if b'\r\n' in raw else 'NG'
lens = [len(t) for _,_,t in entries]
durs = [parse_time(tl.split(' --> ')[1]) - parse_time(tl.split(' --> ')[0]) for _,tl,_ in entries]
print(f'総エントリ: {total}')
print(f'25字超: {over25}件')
print(f'21-25字: {range21_25}件')
print(f'4字未満: {under4}件')
print(f'0ms表示: {zero_dur}件')
print(f'500ms超ギャップ: {gaps500}件')
print(f'重複: {overlaps}件')
print(f'UTF-8 BOM: {bom}')
print(f'CRLF: {crlf}')
print(f'文字数平均: {sum(lens)/len(lens):.1f}')
print(f'表示時間平均: {sum(durs)/len(durs):.2f}秒')
print(f'文字/秒平均: {sum(lens)/sum(durs):.1f}')
"
```

**25字超が 0 でない、または 4字未満が多い場合は lines.txt を編集して Step 6 を再実行**する。

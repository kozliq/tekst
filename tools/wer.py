"""WER/CER по результатам bench.py: python wer.py audio_dir res_dir"""
import json, re, sys
from pathlib import Path

audio, res = Path(sys.argv[1]), Path(sys.argv[2])
ref = {m["file"].replace(".wav", ".ogg"): m["text"] for m in json.loads((audio / "podlodka.json").read_text())}
tts = (audio / "tts.txt").read_text()
ref["tts.ogg"] = ref["tts_noisy.ogg"] = tts


def norm(t):
    t = t.lower().replace("ё", "е")
    t = re.sub(r"[^\w\s]|_", " ", t)
    return t.split()


def dist(a, b):
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


rows = []
for f in sorted(res.glob("*.jsonl")):
    lines = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    items = [l for l in lines if "file" in l]
    if not items:
        continue
    load = next((l["load_s"] for l in lines if "load_s" in l), 0)
    rss = next((l["rss_mb"] for l in lines if "rss_mb" in l), 0)
    out = {}
    for group, pick in (("pod", lambda n: n.startswith("pod")), ("tts", lambda n: n == "tts.ogg"),
                        ("noisy", lambda n: n == "tts_noisy.ogg")):
        sel = [i for i in items if pick(i["file"])]
        if not sel:
            continue
        we = sum(dist(norm(ref[i["file"]]), norm(i["text"])) for i in sel)
        wn = sum(len(norm(ref[i["file"]])) for i in sel)
        ce = sum(dist(" ".join(norm(ref[i["file"]])), " ".join(norm(i["text"]))) for i in sel)
        cn = sum(len(" ".join(norm(ref[i["file"]]))) for i in sel)
        out[group] = (100 * we / wn, 100 * ce / cn)
    dur = sum(i["dur"] for i in items)
    cpu = sum(i["cpu"] for i in items)
    wall = sum(i["wall"] for i in items)
    rows.append((f.stem, out, 60 * cpu / dur, 60 * wall / dur, len(items), load, rss))
print(f"{'модель':18} {'подкаст WER/CER':>16} {'TTS':>12} {'TTS+шум':>12} {'сек CPU/мин':>11} {'сек/мин':>8} {'файлов':>6} {'загрузка':>8} {'память':>7}")
for name, out, cpm, wpm, n, load, rss in rows:
    g = lambda k: f"{out[k][0]:5.1f}/{out[k][1]:4.1f}" if k in out else "-"
    print(f"{name:18} {g('pod'):>16} {g('tts'):>12} {g('noisy'):>12} {cpm:11.1f} {wpm:8.1f} {n:6} {load:8.1f} {rss:6}М")

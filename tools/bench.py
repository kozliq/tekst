"""Замер: один движок + одна модель, N потоков. Печатает JSON по каждому файлу.
python bench.py ENGINE MODEL THREADS [опции] файлы…   ENGINE: gigaam | fw | wcpp"""
import json, os, resource, subprocess, sys, time
import numpy as np

engine, model, threads = sys.argv[1], sys.argv[2], int(sys.argv[3])
opts = dict(a.split("=", 1) for a in sys.argv[4:] if "=" in a and not a.startswith("/"))
files = [a for a in sys.argv[4:] if a.startswith("/")]
ROOT = "/tmp/tekst-bench"


def load(path):
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768


def cpu():
    r = resource.getrusage(resource.RUSAGE_SELF)
    c = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime + c.ru_utime + c.ru_stime


t0 = time.time()
if engine == "gigaam":
    import onnxruntime as rt, onnx_asr
    so = rt.SessionOptions()
    so.intra_op_num_threads, so.inter_op_num_threads = threads, 1
    q = opts.get("q", "int8") or None
    vad = onnx_asr.load_vad("silero", sess_options=so)
    asr = onnx_asr.load_model(model, quantization=q, sess_options=so).with_vad(vad, batch_size=1)
    run = lambda a: " ".join(s.text for s in asr.recognize(a, sample_rate=16000))
elif engine == "fw":
    from faster_whisper import WhisperModel
    m = WhisperModel(model, device="cpu", compute_type=opts.get("ct", "int8"), cpu_threads=threads,
                     download_root=f"{ROOT}/fw")
    beam = int(opts.get("beam", 5))
    run = lambda a: " ".join(s.text.strip() for s in m.transcribe(a, language="ru", beam_size=beam, vad_filter=True,
                                                                     condition_on_previous_text=False)[0])
elif engine == "wcpp":
    import tempfile, wave
    def run(a):
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            with wave.open(f.name, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
                w.writeframes((a * 32767).astype(np.int16).tobytes())
            cmd = [f"{ROOT}/whisper.cpp/build/bin/whisper-cli", "-m", f"{ROOT}/ggml/{model}", "-f", f.name, "-l", "ru",
                   "-t", str(threads), "-nt", "-np", "-bs", opts.get("beam", "5")]
            if opts.get("vad"):
                cmd += ["--vad", "-vm", f"{ROOT}/ggml/ggml-silero-v5.1.2.bin"]
            return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.replace("\n", " ").strip()
load_s = time.time() - t0
print(json.dumps({"load_s": round(load_s, 2)}), flush=True)
run(load(files[0])[:16000 * 3])    # прогрев
for path in files:
    a = load(path)
    c0, w0 = cpu(), time.time()
    text = run(a)
    print(json.dumps({"file": os.path.basename(path), "dur": round(len(a) / 16000, 2), "wall": round(time.time() - w0, 2),
                      "cpu": round(cpu() - c0, 2), "text": text}, ensure_ascii=False), flush=True)
rss = max(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
print(json.dumps({"rss_mb": round(rss / 1024)}), flush=True)

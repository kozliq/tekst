"""Речь → текст: ffmpeg достаёт звук, GigaAM v3 распознаёт русскую речь, Whisper small — другие языки.

GigaAM v3 e2e (Сбер, MIT) — лучшая открытая модель для русского: сама ставит запятые, точки
и заглавные. На одном ядре сервера минута звука — около 2 секунд. Но слушает куски не длиннее
~20 секунд, поэтому длинную запись режем в паузах (где тише всего) и склеиваем текст обратно.

Другие языки GigaAM пишет латиницей или русскими буквами наугад. Если текст подозрительный
(много латиницы или модель не уверена), Whisper small определяет язык по первым 30 секундам.
Не русский — расшифровывает Whisper: он в 6–7 раз медленнее, поэтому для других языков лимит меньше.

Файлы присылают чужие люди: ffmpeg читает только обычные аудио- и видеоконтейнеры и только с диска.
Запись и текст живут, пока идёт работа: бот удаляет файлы сразу после расшифровки.
"""
import json
import math
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

SR = 16000                  # модели слушают 16 кГц моно
FRAME = SR * 30 // 1000     # громкость меряем кадрами по 30 мс
CUT_MIN, CUT_MAX = 10.0, 20.0   # кусок для GigaAM: режем в самой длинной паузе между 10-й и 20-й секундой
PEEK = 20.0                 # сколько секунд речи послушать, прежде чем решать, русский ли это
LID = 30.0                  # Whisper определяет язык по стольким секундам
THREADS = int(os.environ.get("TEKST_THREADS") or 1)
MODELS = Path(os.environ.get("TEKST_MODELS") or Path(__file__).resolve().parent / "models")
FOREIGN_MAX = float(os.environ.get("TEKST_FOREIGN_MAX") or 15 * 60)   # Whisper медленный: не больше 15 минут
IDLE = 15 * 60              # Whisper занимает ~0,5 ГБ памяти: выгружаем, если 15 минут не нужен
# демуксеры, которым можно доверить чужой файл (mov — это и mp4/m4a/3gp, matroska — и webm)
FORMATS = "ogg,mov,mp4,matroska,webm,mp3,wav,flac,aac,amr,asf,avi,mpegts,flv,3gp,caf,aiff,w64"
SAFE_INPUT = ["-protocol_whitelist", "file", "-format_whitelist", FORMATS]
TERMINAL = ".!?…"
LANGS = {"en": "английский", "uk": "украинский", "be": "белорусский", "kk": "казахский", "de": "немецкий",
         "fr": "французский", "es": "испанский", "it": "итальянский", "tr": "турецкий", "pl": "польский",
         "uz": "узбекский", "ky": "киргизский", "hy": "армянский", "ka": "грузинский", "az": "азербайджанский",
         "zh": "китайский", "ja": "японский", "ko": "корейский", "ar": "арабский", "pt": "португальский",
         "he": "иврит", "ro": "румынский", "sr": "сербский", "cs": "чешский", "tg": "таджикский"}


class SpeechError(Exception):
    """Понятная человеку причина, почему текста не будет."""


# --- звук из файла ---

def probe(path):
    """Длина и есть ли звук. Битый или чужой формат — SpeechError."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", *SAFE_INPUT, "-show_format", "-show_streams",
                              "-of", "json", str(path)], capture_output=True, timeout=60, check=False)
    except subprocess.TimeoutExpired:
        raise SpeechError("Файл не читается — похоже, он повреждён.")
    try:
        info = json.loads(out.stdout or b"{}")
    except ValueError:
        info = {}
    if out.returncode or not info.get("format"):
        raise SpeechError("Не получилось открыть файл. Пришли голосовое, кружок, MP3, M4A, OGG или WAV.")
    if not any(s.get("codec_type") == "audio" for s in info.get("streams") or []):
        raise SpeechError("В этом файле нет звука.")
    try:
        duration = float(info["format"].get("duration") or 0)
    except ValueError:
        duration = 0.0
    return {"duration": duration if math.isfinite(duration) else 0.0}


def decode(src, dst, max_sec, timeout=None):
    """Звук → dst (16 кГц, моно, 16 бит, без заголовка), не длиннее max_sec. Вернёт длину в секундах."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", *SAFE_INPUT, "-i", str(src),
           "-map", "0:a:0", "-vn", "-sn", "-dn", "-t", f"{max_sec:.0f}",
           "-ac", "1", "-ar", str(SR), "-f", "s16le", str(dst)]
    try:
        # ошибки ffmpeg — в файл: на битом файле их бывает много, полная труба его остановит
        with tempfile.TemporaryFile() as log:
            proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=log, timeout=timeout or 300)
            log.seek(0)
            err = log.read().decode(errors="replace")
    except subprocess.TimeoutExpired:
        Path(dst).unlink(missing_ok=True)
        raise SpeechError("Файл распаковывается слишком долго — похоже, он повреждён.")
    size = Path(dst).stat().st_size if Path(dst).exists() else 0
    if proc.returncode or size < SR // 5:
        Path(dst).unlink(missing_ok=True)
        print("ffmpeg:", err.strip()[-300:], flush=True)
        raise SpeechError("Не получилось достать звук из файла. Попробуй другой файл или сохрани его заново.")
    return size / 2 / SR


def load_pcm(path):
    """PCM с диска без чтения в память целиком."""
    return np.memmap(path, dtype=np.int16, mode="r")


# --- где резать ---

def loudness(pcm):
    """Громкость каждого кадра по 30 мс, дБ."""
    n = len(pcm) // FRAME
    out = np.empty(n, np.float32)
    for i in range(0, n, 20000):            # частями: час записи — это 120 тысяч кадров
        x = np.asarray(pcm[i * FRAME:min(n, i + 20000) * FRAME], np.float32).reshape(-1, FRAME) / 32768
        out[i:i + len(x)] = 10 * np.log10((x * x).mean(1) + 1e-10)
    return out


def plan(pcm):
    """Куски для GigaAM: [(начало, конец, пауза перед ним)] в секундах. Режем посреди самой длинной
    тишины между CUT_MIN и CUT_MAX секундами куска; совсем тихие куски пропускаем."""
    db = loudness(pcm)
    total = len(pcm) / SR
    if not len(db):
        return []
    # пауза — заметно тише обычного для этой записи (в шумной записи паузы тоже шумные) или совсем тихо;
    # пропускаем только куски, где громче -50 дБ нет и трети секунды: иначе ровную громкую речь
    # (без пауз, на шумной улице) можно принять за тишину
    floor, middle = float(np.percentile(db, 10)), float(np.median(db))
    quiet = (db < floor + 8) & (db < middle - 6) | (db < -50)
    loud = db > -50
    step = FRAME / SR
    pieces, start, pause = [], 0.0, 0.0
    while start < total:
        if total - start <= CUT_MAX:
            end, gap = total, 0.0
        else:
            lo, hi = int((start + CUT_MIN) / step), int((start + CUT_MAX) / step)
            best, best_at, cur = 0, -1, 0
            for i in range(lo, hi):
                cur = cur + 1 if quiet[i] else 0
                if cur > best:
                    best, best_at = cur, i
            if best:
                end, gap = (best_at - best / 2 + 1) * step, best * step
            else:       # ни одной паузы (музыка, сплошной разговор) — режем где тише всего
                smooth = np.convolve(db[lo:hi], np.ones(7) / 7, mode="same")
                end, gap = (lo + int(np.argmin(smooth))) * step, 0.0
        a, b = int(start / step), max(int(start / step) + 1, int(end / step))
        if loud[a:b].sum() * step >= 0.3:
            pieces.append((round(start, 3), round(end, 3), round(pause, 2)))
        start, pause = end, gap
    return pieces


# --- модели ---

_lock = threading.Lock()
_gigaam = None
_whisper = None
_whisper_used = 0.0


def gigaam():
    global _gigaam
    with _lock:
        if _gigaam is None:
            import onnx_asr
            import onnxruntime as rt
            so = rt.SessionOptions()
            so.intra_op_num_threads, so.inter_op_num_threads = THREADS, 1
            so.log_severity_level = 3
            _gigaam = onnx_asr.load_model("gigaam-v3-e2e-rnnt", MODELS / "gigaam", quantization="int8",
                                          sess_options=so, providers=["CPUExecutionProvider"]).with_timestamps()
        return _gigaam


def whisper():
    """Whisper small или None, если его не поставили (тогда бот понимает только русский)."""
    global _whisper, _whisper_used
    if not (MODELS / "whisper-small" / "model.bin").exists():
        return None
    with _lock:
        if _whisper is None:
            from faster_whisper import WhisperModel
            _whisper = WhisperModel(str(MODELS / "whisper-small"), device="cpu", compute_type="int8",
                                    cpu_threads=THREADS, num_workers=1)
        _whisper_used = time.monotonic()
        return _whisper


def unload_idle():
    """Выгрузить Whisper, если давно не нужен (зовёт уборщик бота)."""
    global _whisper
    with _lock:
        if _whisper is not None and time.monotonic() - _whisper_used > IDLE:
            _whisper = None


# --- текст ---

def tidy(text):
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.!?…:;»])", r"\1", text)
    text = re.sub(r"«\s+", "«", text)
    text = re.sub(r"(\w)([—–])", r"\1 \2", text)        # «вообще— как» → «вообще — как»
    text = re.sub(r"([—–])(\w)", r"\1 \2", text)
    text = re.sub(r"(?<=[^\W\d])\+(?=\d)", " +", text)  # «обещают+18» → «обещают +18»
    return text


def sentences(tokens, stamps, offset):
    """Токены GigaAM → предложения [(начало, конец, текст)]. Время — от начала записи."""
    out, cur, t0 = [], [], None
    for tok, t in zip(tokens, stamps):
        if t0 is None and tok.strip():
            t0 = t
        cur.append(tok)
        if tok.strip() and tok.strip()[-1] in TERMINAL:
            out.append((offset + t0, offset + t + 0.08, "".join(cur)))
            cur, t0 = [], None
    if cur and "".join(cur).strip():
        out.append((offset + (t0 or 0), offset + stamps[-1] + 0.08, "".join(cur)))
    return out


def join_piece(prev, nxt):
    """Кусок резали посреди фразы: GigaAM начал его с заглавной — вернём строчную."""
    if prev and prev.rstrip()[-1:] not in TERMINAL and nxt[:1].isupper() and nxt[1:2].islower():
        return nxt[0].lower() + nxt[1:]
    return nxt


def paragraphs(pieces):
    """Предложения [(начало, конец, текст)] → абзацы. Новый абзац — после долгой паузы
    или когда абзац уже длинный и фраза закончилась."""
    out, cur, prev_end = [], "", None
    for start, end, text in pieces:
        text = tidy(text)
        if not text:
            continue
        gap = start - prev_end if prev_end is not None else 0.0
        done = cur[-1:] in TERMINAL
        if cur and done and (gap >= 1.5 and len(cur) >= 80 or gap >= 0.6 and len(cur) >= 300 or len(cur) >= 550):
            out.append(cur)
            cur = ""
        cur = f"{cur} {join_piece(cur, text)}".strip() if cur else text
        prev_end = end
    if cur:
        out.append(cur)
    return [tidy(p) for p in out]


class Result:
    def __init__(self, paras, lang="ru", engine="gigaam", cut=0.0, unsure=False):
        self.paragraphs, self.lang, self.engine, self.unsure = paras, lang, engine, unsure
        self.cut = cut          # не русская и длиннее FOREIGN_MAX — расшифровали только начало

    @property
    def text(self):
        return "\n\n".join(self.paragraphs)


# --- распознавание ---

def suspicious(tokens, logprobs):
    """Похоже, что речь не русская: много латиницы или модель не уверена."""
    text = "".join(tokens)
    letters = [c for c in text if c.isalpha()]
    latin = sum("a" <= c.lower() <= "z" for c in letters)
    if letters and latin / len(letters) > 0.4:
        return True
    lp = [x for x in logprobs if x is not None]
    return bool(lp) and float(np.mean(lp)) < -0.25


def detect(pcm, start):
    """Язык по первым LID секундам речи (Whisper). → (код, уверенность) или (None, 0)."""
    w = whisper()
    if w is None:
        return None, 0.0
    a = np.asarray(pcm[int(start * SR):int((start + LID) * SR)], np.float32) / 32768
    lang, prob, _ = w.detect_language(a)
    return lang, float(prob)


def transcribe(pcm):
    """Генератор: по ходу работы отдаёт долю готового (0…1), в конце возвращает Result.
    Работает кусками по 10–20 с — бот между кусками успевает заняться чужими записями."""
    total = len(pcm) / SR
    pieces = plan(pcm)
    if not pieces:
        return Result([])
    asr = gigaam()
    said, toks, lps, heard, checked, unsure = [], [], [], 0.0, False, False
    for n, (start, end, _pause) in enumerate(pieces, 1):
        a = np.asarray(pcm[int(start * SR):int(end * SR)], np.float32) / 32768
        r = asr.recognize(a, sample_rate=SR)
        if r.tokens:
            said += sentences(r.tokens, r.timestamps, start)
            toks += r.tokens
            lps += list(r.logprobs or [])
            heard += end - start
        if not checked and (heard >= PEEK or n == len(pieces)):
            checked = True
            if toks and suspicious(toks, lps):
                lang, prob = detect(pcm, pieces[0][0])
                if lang and lang != "ru" and prob >= 0.5:
                    return (yield from foreign(pcm, lang))
                unsure = lang is None      # Whisper не поставлен — честно скажем, что язык, похоже, не тот
        yield end / total
    return Result(paragraphs(said), unsure=unsure)


def foreign(pcm, lang):
    """Не русская речь — целиком через Whisper (не длиннее FOREIGN_MAX)."""
    total = min(len(pcm) / SR, FOREIGN_MAX)
    a = np.asarray(pcm[:int(total * SR)], np.float32) / 32768
    segments, _ = whisper().transcribe(a, language=lang, beam_size=5, vad_filter=True,
                                       condition_on_previous_text=False)
    said = []
    for s in segments:          # Whisper отдаёт фразы по мере готовности
        said.append((s.start, s.end, s.text))
        yield min(1.0, s.end / total)
    return Result(paragraphs(said), lang=lang, engine="whisper",
                  cut=total if len(pcm) / SR > FOREIGN_MAX + 1 else 0.0)


def run(pcm, progress=None):
    """Без очереди: распознать всё сразу (для проверок и замеров)."""
    gen = transcribe(pcm)
    while True:
        try:
            share = next(gen)
        except StopIteration as stop:
            return stop.value
        if progress:
            progress(share)

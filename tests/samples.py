"""Тестовые записи: речь говорит синтезатор macOS (say), остальное рисует ffmpeg.
Настоящие голосовые — пути через «:» в TEKST_REAL_AUDIO (текст для сверки — рядом, в .txt)."""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

DIR = Path(tempfile.gettempdir()) / "tekst-samples"
REAL = [Path(p) for p in os.environ.get("TEKST_REAL_AUDIO", "").split(":") if p]
CAN_SPEAK = bool(shutil.which("say"))

SHORT = "Привет! Перезвони мне, пожалуйста, когда освободишься. Я буду дома после шести."
LONG = (
    "Привет! Слушай, я тут подумал насчёт субботы. Давай встретимся не в десять, а в одиннадцать, "
    "потому что мне с утра надо заехать в сервис и забрать машину. Они обещали, что всё будет готово к пятнице, "
    "но ты же знаешь, как это бывает. Если вдруг задержат, я напишу заранее.\n"
    "Теперь по поводу дачи. Я посмотрел прогноз, обещают тепло и без дождя, так что можно спокойно жарить "
    "шашлык на улице. Мясо я возьму сам, с тебя овощи и уголь. Мангал там есть, решётку тоже не забудь, "
    "старая совсем погнулась.\n"
    "И ещё. Маша спрашивала, можно ли взять собаку. Я сказал, что не против, но надо уточнить у хозяев, "
    "потому что в прошлый раз соседи жаловались. Позвони им, пожалуйста, у тебя же есть номер.\n"
    "В общем, если что-то меняется, пиши. Я сегодня до семи на работе, потом свободен. Всё, давай, обнимаю!"
)
EXTRA = (
    "Кстати, про отпуск. Я посмотрел билеты на море, в сентябре они почти в два раза дешевле, чем летом. "
    "Гостиницу можно взять недалеко от пляжа, там есть завтрак и бассейн. Если решим быстро, успеем по старой цене.\n"
    "А ещё на работе новый проект. Начальник хочет, чтобы мы закончили его до конца месяца, но, по-моему, "
    "это нереально. Там одной документации на две недели, а тестировать вообще некому.\n"
)
ENGLISH = "Hey, it's me. I'm running about ten minutes late, the traffic on the bridge is terrible. Can you order me a coffee?"


def ff(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


def speak(text, dst, voice="Milena", rate=170, codec=("-c:a", "libopus", "-b:a", "24k", "-ac", "1", "-ar", "48000")):
    aiff = dst.with_suffix(".aiff")
    subprocess.run(["say", "-v", voice, "-r", str(rate), "-o", str(aiff), text], check=True)
    ff("-i", str(aiff), *codec, str(dst))
    aiff.unlink()


def make():
    DIR.mkdir(exist_ok=True)
    s = {k: DIR / v for k, v in {
        "short": "short.ogg", "long": "long-70s.ogg", "huge": "huge-3min.ogg", "note": "note.mp4",
        "mp3": "talk.mp3", "english": "english.ogg", "silence": "silence.ogg", "tone": "tone.ogg",
        "noaudio": "noaudio.mp4", "junk": "junk.ogg", "hls": "evil.m4a"}.items()}
    if CAN_SPEAK:
        if not s["short"].exists():
            speak(SHORT, s["short"])
        if not s["long"].exists():
            speak(LONG, s["long"], rate=150)
        if not s["huge"].exists():
            speak(LONG + "\n" + EXTRA + "\n" + LONG.replace("Привет! ", "И ещё раз. ") + "\n" + EXTRA, s["huge"])
        if not s["note"].exists():      # кружок: квадратное видео 384×384 со звуком
            ff("-f", "lavfi", "-i", "testsrc2=s=384x384:r=30:d=6", "-i", str(s["short"]),
               "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(s["note"]))
        if not s["mp3"].exists():
            speak(SHORT, s["mp3"], codec=("-c:a", "libmp3lame", "-b:a", "64k"))
        if not s["english"].exists():
            speak(ENGLISH, s["english"], voice="Samantha", rate=180)
    if not s["silence"].exists():
        ff("-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "5", "-c:a", "libopus", str(s["silence"]))
    if not s["tone"].exists():          # «музыка»: аккорд без слов
        ff("-f", "lavfi", "-i", "sine=f=220:d=6", "-f", "lavfi", "-i", "sine=f=277:d=6", "-f", "lavfi",
           "-i", "sine=f=330:d=6", "-filter_complex", "amix=inputs=3", "-c:a", "libopus", str(s["tone"]))
    if not s["noaudio"].exists():
        ff("-f", "lavfi", "-i", "testsrc=s=320x240:r=10:d=3", "-c:v", "libx264", "-preset", "ultrafast", str(s["noaudio"]))
    s["junk"].write_bytes(b"\x00\x01not audio at all" * 1000)
    # «аудио», которое на самом деле плейлист HLS со ссылкой на чужой файл на диске
    secret = DIR / "secret.ts"
    if not secret.exists():
        ff("-f", "lavfi", "-i", "sine=d=3", "-c:a", "aac", "-f", "mpegts", str(secret))
    s["hls"].write_text(f"#EXTM3U\n#EXT-X-TARGETDURATION:3\n#EXTINF:3,\nfile://{secret}\n#EXT-X-ENDLIST\n")
    return s


def real():
    return [p for p in REAL if p.exists()]

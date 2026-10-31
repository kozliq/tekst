"""Текст: Telegram-бот, который переводит голосовые, кружки и аудио в текст.

В личке — расшифровывает всё, что прислали или переслали. В группе — когда попросили:
ответом на голосовое /text (или «текст»), а если админ включил /auto — все голосовые и кружки сам.

Распознавание — на своём сервере (speech.py): GigaAM v3 для русского, Whisper small для других языков.
Записи идут в одну очередь по кругу: каждый человек по очереди получает кусок работы,
поэтому чужой часовой подкаст не задерживает короткое голосовое.

Бесплатно, без рекламы и подписок на каналы; звёзды — только по /donate.
Ничего не храним: запись удаляем сразу после расшифровки, текст — после отправки.
Для лимитов бот помнит в памяти, сколько минут человек расшифровал за час и сутки (перезапуск — забыл).

Запуск: python3 tekst.py (на сервере — служба tekst-bot). Нужны ffmpeg, модели (tools/models.sh)
и пакеты из requirements.txt. Токен — TEKST_TOKEN или файл bot.token в папке данных (TEKST_DATA).
Аватарка ставится один раз: python3 tekst.py avatar [путь.jpg]
"""
import hashlib
import html
import http.client
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # python3 -I не видит соседние файлы
import speech  # noqa: E402

try:
    import certifi
    TLS = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    TLS = ssl.create_default_context()

# с серверов до Telegram по IPv6 запрос иногда висит по минуте (так было у Качалки) — ходим по IPv4
_getaddrinfo = socket.getaddrinfo


def _ipv4_for_telegram(host, *args, **kwargs):
    if host == "api.telegram.org":
        return [a for a in _getaddrinfo(host, *args, **kwargs) if a[0] == socket.AF_INET] or \
            _getaddrinfo(host, *args, **kwargs)
    return _getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = _ipv4_for_telegram

ENV = os.environ.get
HERE = Path(__file__).resolve().parent
DATA = Path(ENV("TEKST_DATA") or Path.home() / "Library" / "Application Support" / "Tekst")
WORK = Path(ENV("TEKST_WORK") or Path.home() / "Library" / "Caches" / "Tekst")
TG_API = ENV("TEKST_TG_API", "https://api.telegram.org")
TG_LOCAL = ENV("TEKST_TG_LOCAL") == "1"
TG_DIR = Path(ENV("TEKST_TG_DIR") or "/var/lib/tekst-tgapi")   # где свой сервер Telegram держит скачанное
IN_MAX = int(ENV("TEKST_MAX_MB") or (2000 if TG_LOCAL else 20)) * 1024 * 1024   # сколько Telegram отдаёт боту
AVATAR = HERE / "brand" / "avatar-640.jpg"

# Лимиты. Бот открыт всем, а ядро у него одно — никто не должен забить очередь остальным.
MAX_SEC = 3 * 3600          # одна запись — до 3 часов
PER_HOUR = int(ENV("TEKST_PER_HOUR") or 120)    # минут на человека
PER_DAY = int(ENV("TEKST_PER_DAY") or 360)
GROUP_HOUR = 180            # минут на группу в час
FOREIGN_COST = 6            # Whisper в 6 раз дороже GigaAM: минута не по-русски считается за шесть
LINE = 5                    # записей одного человека в работе
QUEUE_MAX = 100             # записей в работе всего
QUEUE_SEC = 8 * 3600        # звука в работе всего: распакованный час — 115 МБ на диске
DISK_FREE = 2 << 30         # свободного места меньше 2 ГБ — новые записи не берём
FETCHES = threading.Semaphore(2)    # скачиваем и распаковываем разом
HANDLERS = threading.BoundedSemaphore(32)   # сообщений разбираем разом: остальные ждут у Telegram
QUICK = 60                  # записи до минуты — без сообщения «расшифровываю», сразу текст
FLOOD, MUTE = 20, 600       # больше 20 сообщений в минуту — бот молчит 10 минут
MSG_MAX = int(ENV("TEKST_MSG_MAX") or 4000)     # Telegram: сообщение до 4096 знаков
PARTS_MAX = 3               # длиннее трёх сообщений — присылаем файлом
FOLD = 500                  # в группе текст длиннее этого сворачиваем под «показать полностью»
AUDIO_EXT = (".ogg", ".oga", ".opus", ".mp3", ".m4a", ".aac", ".wav", ".flac", ".amr", ".wma", ".caf", ".aiff",
             ".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".3gp", ".mpeg", ".mpg", ".ts")
ASK = re.compile(r"(?:текст|текстом|в текст|расшифруй|расшифровать|расшифровка|что там|что говорит)[\s.!?)]*", re.I)

# имя бота: по этим словам его находят в поиске Telegram (до 64 знаков)
NAME = ENV("TEKST_NAME", "Текст — голосовые и кружки в текст")
SHORT_ABOUT = "Перевожу голосовые и кружки в текст. Бесплатно, без рекламы, регистрации и подписок на каналы."
ABOUT = (
    "Пришли или перешли голосовое, кружок или аудио — верну текстом.\n\n"
    "Лучше всего понимаю русский, другие языки тоже. Длинную запись разобью на абзацы.\n\n"
    "В чате с друзьями: ответь на голосовое /text — расшифрую. Админ может включить /auto — буду сам.\n\n"
    "Бесплатно. Без рекламы, регистрации и «подпишись на 4 канала». "
    "Записи и текст не храню: расшифровал — удалил."
)
HELLO = (
    "<b>Пришли голосовое, кружок или аудио — верну текстом.</b>\n\n"
    "Можно переслать из любого чата. Длинную запись разобью на абзацы, очень длинную пришлю файлом.\n\n"
    "<b>В чате с друзьями</b> (кнопка ниже) ответь на голосовое командой /text — расшифрую. "
    "Админ чата может включить /auto — тогда буду расшифровывать сам.\n\n"
    "Бесплатно, без рекламы. Записи и текст не храню — подробнее /privacy."
)
GROUP_HELLO = (
    "<b>Привет, чат! Ответьте на голосовое или кружок командой /text — пришлю текст.</b>\n\n"
    "Чтобы я расшифровывал все голосовые сам, админ может включить /auto.\n\n"
    "Записи и текст не храню. Бесплатно, без рекламы."
)
PRIVACY = (
    "<b>Что я храню</b>\n\n"
    "Записи — нет. Скачал, расшифровал, удалил (обычно через несколько секунд).\n\n"
    "Текст — нет. Отправил и забыл. Расшифровка идёт на моём сервере, никуда наружу запись не уходит.\n\n"
    "Кто пользуется — нет. Чтобы один человек не занял сервер, помню в памяти, сколько минут ты "
    "расшифровал за последний час и сутки. После перезапуска это стирается.\n\n"
    "Группы — только где включён /auto (номер группы в виде хеша), чтобы помнить настройку.\n\n"
    "Звёзды — кто и сколько кинул, чтобы можно было вернуть по /paysupport."
)
HINT = "Пришли голосовое, кружок или аудиофайл — верну текстом."
NO_TEXT_PRIVATE = "Тут ничего отвечать не нужно — просто пришли или перешли голосовое."
NO_REPLY = "Ответь этой командой на голосовое или кружок — пришлю текст."
NOT_AUDIO = "В этом сообщении нет голосового. Ответь /text на голосовое, кружок или аудио."
AUTO_ON = ("Теперь расшифровываю все голосовые и кружки в чате сам. Выключить — /auto.\n\n"
           "Записи и текст не храню.")
AUTO_OFF = "Ок, сам больше не расшифровываю. Нужно — ответьте на голосовое /text. Включить снова — /auto."
AUTO_PRIVATE = "Это для чата с друзьями: там /auto включает, расшифровываю ли я голосовые сам. Здесь я и так расшифровываю всё."
AUTO_ADMINS = "Включать и выключать /auto могут только админы чата."
AUTO_BLIND = ("\n\nТолько я пока не вижу сообщений в этом чате. Сделайте меня админом "
              "(права можно не давать никакие) — тогда увижу голосовые.")
TOO_BIG = ("Файл {mb} МБ, а Telegram даёт ботам скачать только до {max} МБ.\n\n"
           "Обрежь запись или сожми её — и пришли ещё раз.")
TOO_LONG = "Запись длиннее {h} часов — такие пока не беру. Обрежь её на части."
SILENCE = "Не расслышал тут слов — похоже, тишина или музыка."
UNSURE = "\n\n(Похоже, это не русский — другие языки я понимаю хуже.)"
CUT = "\n\n(Расшифровал первые {m} минут: на других языках дальше пока не беру.)"
AS_FILE = "Текст длинный — прислал файлом."
ALREADY = "Эту запись уже расшифровываю."
BUSY = "Сейчас много желающих. Пришли запись ещё раз через пару минут."
BROKEN = "Что-то сломалось у меня. Попробуй ещё раз через минуту."
STARS = (10, 50, 100, 500)
DONATE = (
    "Бот бесплатный и таким останется: без рекламы, подписок и платных функций.\n\n"
    "Если пригодился и хочется сказать спасибо — кинь звёздочку. "
    "Это добровольно и ни на что не влияет, всё работает одинаково для всех."
)
PAYSUPPORT = (
    "Звёзды здесь — просто спасибо, за них ничего не покупается и не открывается. "
    "Кинул случайно или передумал — нажми кнопку, верну последний донат."
)

THANKS = DATA / "thanks.jsonl"      # кто и сколько звёзд кинул (для возврата)
STATS = DATA / "stats.json"         # сколько записей и минут расшифровано — только числа, без людей
AUTO = DATA / "auto.json"           # группы с /auto — хешем, номеров групп в файле нет

lock = threading.Lock()
people = {}                 # человек или группа → когда писал, сколько минут, до скольки молчим
working = {}                # (чат, сообщение с записью) → запись в работе: второй раз не берём
auto_chats = set()
salt = ""
bot_name, bot_id = "", 0


class TgError(Exception):
    def __init__(self, text, code=0):
        super().__init__(text)
        self.code = code


def read_token():
    token = ENV("TEKST_TOKEN")
    if not token:
        try:
            token = (DATA / "bot.token").read_text("utf-8").strip()
        except OSError:
            pass
    if not token:
        sys.exit(f"Нет токена бота: положи его в {DATA / 'bot.token'}")
    return token


TOKEN = read_token()


def multipart(fields, files):
    boundary = uuid.uuid4().hex
    chunks = []
    for k, v in fields.items():
        v = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, (name, data, ctype) in files.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\n'
                      f"Content-Type: {ctype}\r\n\r\n".encode())
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


# сбой связи: urllib, TLS и разорванный ответ — всё, что не ответ Telegram
NET = (OSError, http.client.HTTPException, ValueError)


def tg(method, files=None, timeout=30, tries=3, once=False, **params):
    """Вызов Telegram Bot API. Сам ждёт, если Telegram просит притормозить.
    once — после сбоя связи не повторяем: сообщение могло уже дойти (иначе придёт два)."""
    params = {k: v for k, v in params.items() if v is not None}
    if files:
        body, ctype = multipart(params, files)
    else:
        body, ctype = json.dumps(params, ensure_ascii=False).encode(), "application/json"
    for attempt in range(tries):
        req = urllib.request.Request(f"{TG_API}/bot{TOKEN}/{method}", data=body, headers={"Content-Type": ctype})
        try:
            r = json.loads(urllib.request.urlopen(req, timeout=timeout, context=TLS).read())
        except urllib.error.HTTPError as e:
            try:
                r = json.loads(e.read() or b"{}")
            except ValueError:
                r = {}
            r.setdefault("error_code", e.code)
            r.setdefault("description", str(e))
        except NET as e:
            if attempt == tries - 1 or once:
                raise TgError(str(e))
            time.sleep(2 + attempt * 3)
            continue
        if r.get("ok"):
            return r.get("result")
        wait = (r.get("parameters") or {}).get("retry_after")
        if wait and attempt < tries - 1:
            time.sleep(min(wait, 60) + 1)
            continue
        raise TgError(r.get("description", "ошибка Telegram"), r.get("error_code", 0))
    raise TgError("Telegram не ответил")


def quiet(method, **params):
    """Вызов, ошибка которого ничего не ломает (поправить статус, убрать сообщение)."""
    try:
        return tg(method, tries=1, **params)
    except TgError:
        return None


def reply_to(message_id):
    return {"message_id": message_id, "allow_sending_without_reply": True}


def clock(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec // 60 % 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def plural(n, one, few, many):
    n10, n100 = n % 10, n % 100
    return one if n10 == 1 and n100 != 11 else few if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else many


def minutes(n):
    n = max(1, round(n))
    return f"{n} {plural(n, 'минуту', 'минуты', 'минут')}"


class Status:
    """Одно сообщение «что сейчас происходит», которое бот правит по ходу дела, а в конце убирает."""

    def __init__(self, chat, reply_id, text):
        self.chat, self.text, self.last = chat, text, 0.0
        self.id = tg("sendMessage", chat_id=chat, text=text, reply_parameters=reply_to(reply_id))["message_id"]

    def set(self, text, force=True):
        if text == self.text:
            return
        if not force and time.monotonic() - self.last < 2:    # Telegram не любит частые правки
            return
        self.text, self.last = text, time.monotonic()
        quiet("editMessageText", chat_id=self.chat, message_id=self.id, text=text)

    def drop(self):
        quiet("deleteMessage", chat_id=self.chat, message_id=self.id)


def bar(share):
    k = round(share * 10)
    return "●" * k + "○" * (10 - k) + f" {round(share * 100)}%"


# --- люди и лимиты (только в памяти) ---

def person(who):
    p = people.get(who)
    if not p:
        p = people[who] = {"said": deque(), "spent": deque(), "muted": 0.0, "told": {}}
    return p


def flood(user):
    """True — человек пишет слишком часто, и бот какое-то время его не слышит."""
    now = time.time()
    with lock:
        p = person(user)
        if p["muted"] > now:
            return True
        said = p["said"]
        said.append(now)
        while said and said[0] < now - 60:
            said.popleft()
        if len(said) > FLOOD:
            p["muted"] = now + MUTE
            said.clear()
            return "mute"
    return False


def fresh(who, kind, every):
    """Не повторять одну и ту же подсказку чаще, чем раз в every секунд."""
    now = time.time()
    with lock:
        told = person(who)["told"]
        if now - told.get(kind, 0) < every:
            return False
        told[kind] = now
        return True


def spent(who):
    """Минут за последний час и сутки."""
    now = time.time()
    with lock:
        q = person(who)["spent"]
        while q and q[0][0] < now - 86400:
            q.popleft()
        return sum(m for t, m in q if t > now - 3600), sum(m for _, m in q)


def charge(who, sec):
    with lock:
        person(who)["spent"].append((time.time(), sec / 60))


def limit_text(user, group, sec):
    hour, day = spent(user)
    want = sec / 60
    if day + want > PER_DAY:
        return f"На сегодня хватит: ты расшифровал {minutes(day)}. Приходи завтра."
    if hour + want > PER_HOUR:
        return f"За час ты расшифровал {minutes(hour)}. Передохни немного и приходи."
    if group and spent(group)[0] + want > GROUP_HOUR:
        return "В этом чате за час уже много расшифровок. Попробуйте чуть позже."
    return None


# --- группы с /auto: в файле только хеш номера группы ---

def chat_key(chat):
    return hashlib.sha256(f"{salt}:{chat}".encode()).hexdigest()[:20]


def load_auto():
    global salt
    try:
        saved = json.loads(AUTO.read_text("utf-8"))
    except (OSError, ValueError):
        saved = {}
    salt = saved.get("salt") or secrets.token_hex(16)
    auto_chats.update(saved.get("chats") or [])


def is_auto(chat):
    return chat_key(chat) in auto_chats


def toggle_auto(chat):
    """Вернёт True, если теперь расшифровываю сам."""
    with lock:
        auto_chats.symmetric_difference_update({chat_key(chat)})
        on = chat_key(chat) in auto_chats
        tmp = AUTO.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({"salt": salt, "chats": sorted(auto_chats)}), "utf-8")
            tmp.replace(AUTO)
        except OSError as e:
            print("auto:", e, flush=True)
    return on


def is_admin(msg):
    """Пишет админ группы? Анонимный админ пишет от имени самой группы."""
    chat = msg["chat"]["id"]
    if (msg.get("sender_chat") or {}).get("id") == chat:
        return True
    user = (msg.get("from") or {}).get("id")
    member = user and quiet("getChatMember", chat_id=chat, user_id=user)
    return bool(member) and member.get("status") in ("creator", "administrator")


def count(sec, foreign):
    with lock:
        try:
            stats = json.loads(STATS.read_text("utf-8"))
        except (OSError, ValueError):
            stats = {}
        stats["records"] = stats.get("records", 0) + 1
        stats["minutes"] = round(stats.get("minutes", 0) + sec / 60, 1)
        stats["foreign"] = stats.get("foreign", 0) + int(foreign)
        tmp = STATS.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(stats), "utf-8")
            tmp.replace(STATS)
        except OSError as e:
            print("stats:", e, flush=True)


# --- очередь по кругу ---

turn = threading.Condition(lock)
lines = {}                  # человек → его записи по порядку
order = deque()             # чья очередь получить следующий кусок работы


def ahead():
    return sum(len(q) for q in lines.values())


def push(job):
    with turn:
        q = lines.get(job.user)
        if q is None:
            q = lines[job.user] = deque()
            order.append(job.user)
        q.append(job)
        turn.notify()


def worker():
    """Один кусок работы (~20 с звука) — и следующий человек. Так короткое голосовое
    не ждёт, пока доделается чужой часовой подкаст."""
    while True:
        with turn:
            while not order:
                turn.wait()
            user = order.popleft()
            job = lines[user][0]
        try:
            done = job.step()
        except Exception as e:      # что бы ни случилось — очередь не встаёт
            print("work:", type(e).__name__, e, flush=True)
            job.fail(BROKEN)
            done = True
        with turn:
            q = lines[user]
            if done:
                q.popleft()
            if q:
                order.append(user)
            else:
                del lines[user]


class Job:
    def __init__(self, msg, media, explicit, req_msg):
        self.chat, self.private = msg["chat"]["id"], msg["chat"].get("type") == "private"
        self.user = (req_msg.get("from") or {}).get("id", self.chat)
        self.msg_id = msg["message_id"]             # сообщение с записью — на него и отвечаем
        self.explicit = explicit                    # попросили сами (личка, /text), а не /auto
        self.file_id, self.size, self.duration = media
        self.status = None
        self.src = self.pcm = None
        self.own = False                            # скачали сами — сами и удалим
        self.gen = None
        self.sec = 0.0
        self.charged = 0.0                          # сколько секунд уже записали в лимит
        self.t0 = time.monotonic()

    # отвечать в группе на /auto молча, если ничего не вышло
    def say(self, text):
        if self.status:
            self.status.set(text)
        elif self.explicit:
            quiet("sendMessage", chat_id=self.chat, text=text, reply_parameters=reply_to(self.msg_id))

    def fail(self, text):
        self.say(text)
        self.cleanup()

    def cleanup(self):
        for p in (self.src if self.own else None, self.pcm):
            if p:
                Path(p).unlink(missing_ok=True)
        self.src = self.pcm = None
        with lock:
            if working.get((self.chat, self.msg_id)) is self:
                del working[(self.chat, self.msg_id)]

    def prepare(self):
        """Скачать, проверить и распаковать — в отдельном потоке, пока очередь занята другими."""
        try:
            with FETCHES:
                if self.status:
                    self.status.set("Скачиваю запись…")
                try:
                    self.src, self.own = fetch(self.file_id, self.size)
                except speech.SpeechError as e:
                    return self.fail(str(e))
                except (TgError, *NET) as e:
                    print("fetch:", e, flush=True)
                    return self.fail("Не получилось скачать запись у Telegram. Пришли её ещё раз.")
                info = speech.probe(self.src)
                if info["duration"] > MAX_SEC + 1:
                    return self.fail(TOO_LONG.format(h=MAX_SEC // 3600))
                # файлом длину узнали только сейчас, а у голосового её мог подделать чужой бот
                why = self.overrun(info["duration"])
                if why:
                    return self.fail(why)
                if shutil.disk_usage(WORK).free < DISK_FREE:
                    return self.fail(BUSY)
                self.pcm = WORK / f"{uuid.uuid4().hex}.pcm"
                self.sec = speech.decode(self.src, self.pcm, MAX_SEC)
                if self.own:
                    Path(self.src).unlink(missing_ok=True)
                self.src = None
                why = self.overrun(self.sec)       # и длина в заголовке файла тоже бывает враньём
                if why:
                    return self.fail(why)
        except speech.SpeechError as e:
            return self.fail(str(e))
        except Exception as e:
            print("prepare:", type(e).__name__, e, flush=True)
            return self.fail(BROKEN)
        if self.status:
            n = ahead()
            self.status.set("Расшифровываю…" if not n else
                            f"В очереди: передо мной {n} {plural(n, 'запись', 'записи', 'записей')}…")
        push(self)

    def charge(self, sec):
        self.charged += sec
        charge(self.user, sec)
        if not self.private:
            charge(self.chat, sec)

    def overrun(self, sec):
        """Запись оказалась длиннее, чем записали в лимит: досчитать разницу. → причина отказа или None."""
        extra = sec - self.charged
        if extra <= 5:
            return None
        why = limit_text(self.user, None if self.private else self.chat, extra)
        if not why and queued_sec(self) + sec > QUEUE_SEC:
            why = BUSY
        if not why:
            self.charge(extra)
        return why

    def step(self):
        """Следующий кусок записи. True — запись готова (или сломалась)."""
        if self.gen is None:
            self.gen = speech.transcribe(speech.load_pcm(self.pcm))
            if not self.status:
                quiet("sendChatAction", chat_id=self.chat, action="typing")
        try:
            share = next(self.gen)
        except StopIteration as stop:
            self.finish(stop.value)
            return True
        except speech.SpeechError as e:
            self.fail(str(e))
            return True
        if self.status:
            self.status.set(f"Расшифровываю: {clock(share * self.sec)} из {clock(self.sec)}\n{bar(share)}",
                            force=False)
        return False

    def finish(self, res):
        took = time.monotonic() - self.t0
        foreign = res.engine == "whisper"
        print(f"готово: {clock(self.sec)} за {took:.1f} с ({res.engine}{', ' + res.lang if foreign else ''})",
              flush=True)
        if foreign:     # Whisper дороже: досчитываем разницу к лимиту
            self.charge(min(self.sec, speech.FOREIGN_MAX) * (FOREIGN_COST - 1))
        count(self.sec, foreign)
        try:
            if not res.paragraphs:
                return self.say(SILENCE)
            note = (UNSURE if res.unsure else "") + (CUT.format(m=round(res.cut / 60)) if res.cut else "")
            send_text(self.chat, self.msg_id, res.paragraphs, note, self.private)
            if self.status:
                self.status.drop()
        except TgError as e:
            print("send:", e, flush=True)
            self.say("Telegram не принял текст. Попробуй ещё раз через минуту.")
        finally:
            self.cleanup()


def fetch(file_id, size):
    """Скачать запись у Telegram. Со своим сервером Telegram файл уже на диске — просто путь к нему.
    Этот сервер только у Текста, поэтому файл после расшифровки удаляем и там."""
    try:
        f = tg("getFile", file_id=file_id, timeout=300 if TG_LOCAL else 30)
    except TgError as e:
        if "too big" in str(e).lower():
            raise speech.SpeechError(TOO_BIG.format(mb=round(size / 1048576), max=IN_MAX // 1048576))
        raise
    path = f.get("file_path") or ""
    if TG_LOCAL and path.startswith("/"):
        local = Path(path).resolve()
        if not local.is_relative_to(TG_DIR.resolve()):     # удаляем только у себя
            raise TgError(f"файл не в {TG_DIR}")
        return local, True
    dst = WORK / f"{uuid.uuid4().hex}.src"
    url = f"{TG_API}/file/bot{TOKEN}/{urllib.parse.quote(path)}"
    got = 0
    try:
        with urllib.request.urlopen(url, timeout=60, context=TLS) as r, dst.open("wb") as out:
            while chunk := r.read(1 << 16):
                got += len(chunk)
                if got > IN_MAX:
                    raise speech.SpeechError(TOO_BIG.format(mb=round(got / 1048576), max=IN_MAX // 1048576))
                out.write(chunk)
    except BaseException:
        dst.unlink(missing_ok=True)
        raise
    return dst, True


# --- ответ текстом ---

def pieces(paragraphs, limit):
    """Абзацы → сообщения не длиннее limit. Слишком длинный абзац режем по предложениям."""
    out, cur = [], ""
    for p in paragraphs:
        while len(p) > limit:
            cut = max(p.rfind(c, 0, limit) for c in ".!?… ")
            cut = cut + 1 if cut > limit // 2 else limit
            head, p = p[:cut].strip(), p[cut:].strip()
            if cur:
                out.append(cur)
                cur = ""
            out.append(head)
        if cur and len(cur) + 2 + len(p) > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        out.append(cur)
    return out


def send_text(chat, reply_id, paragraphs, note, private):
    """В личке — обычным текстом (удобно копировать). В группе длинный текст сворачиваем,
    чтобы не занимал весь чат. Совсем длинный — файлом."""
    full = "\n\n".join(paragraphs)
    parts = pieces(paragraphs, MSG_MAX - len(note) - 40)
    if len(parts) > PARTS_MAX:
        tg("sendDocument", files={"document": ("расшифровка.txt", (full + note + "\n").encode(), "text/plain")},
           chat_id=chat, caption=AS_FILE + note, reply_parameters=reply_to(reply_id), timeout=120, once=True)
        return
    for i, part in enumerate(parts):
        text = part + (note if i == len(parts) - 1 else "")
        if private or len(full) <= FOLD:
            tg("sendMessage", chat_id=chat, text=text, reply_parameters=reply_to(reply_id), once=True)
        else:
            tg("sendMessage", chat_id=chat, text=f"<blockquote expandable>{html.escape(part)}</blockquote>"
               + html.escape(text[len(part):]), parse_mode="HTML", reply_parameters=reply_to(reply_id), once=True)


# --- сообщения ---

def media_of(msg, everything=True):
    """Запись из сообщения → (file_id, размер, длина или 0) или None.
    everything=False — только голосовые и кружки (то, что /auto расшифровывает само)."""
    kinds = ("voice", "video_note", "audio", "video") if everything else ("voice", "video_note")
    for kind in kinds:
        m = msg.get(kind)
        if m:
            return m["file_id"], m.get("file_size") or 0, m.get("duration") or 0
    doc = msg.get("document")
    if everything and doc and (str(doc.get("mime_type", "")).startswith(("audio/", "video/")) or
                               str(doc.get("file_name", "")).lower().endswith(AUDIO_EXT)):
        return doc["file_id"], doc.get("file_size") or 0, 0
    return None


def command(text):
    """«/text@bot» → ("/text", кому). Команда другому боту — не наша."""
    cmd, _, to = (text.split() or [""])[0].lower().partition("@")
    return cmd, to


def mentioned(text):
    return bool(bot_name) and f"@{bot_name}".lower() in text.lower()


def request_in_group(msg):
    """Просят расшифровать в группе? → сообщение с записью или None."""
    text = (msg.get("text") or "").strip()
    cmd, to = command(text)
    asked = cmd == "/text" and to in ("", bot_name.lower()) or mentioned(text) or ASK.fullmatch(text)
    return msg.get("reply_to_message") if asked else None


def added_me(msg):
    return any(m.get("id") == bot_id for m in msg.get("new_chat_members") or [])


def on_message(msg):
    chat, private = msg["chat"]["id"], msg["chat"].get("type") == "private"
    user = (msg.get("from") or {}).get("id", chat)
    if msg.get("successful_payment"):
        return on_paid(msg)
    if added_me(msg):
        if fresh(chat, "hello", 60):
            tg("sendMessage", chat_id=chat, text=GROUP_HELLO, parse_mode="HTML")
        return
    text = msg.get("text") or ""
    cmd, to = command(text)
    ours = private or to == bot_name.lower()
    if cmd in ("/start", "/help"):
        if ours and fresh(chat, "start", 10):
            if private:
                tg("sendMessage", chat_id=chat, text=HELLO, parse_mode="HTML", reply_markup={"inline_keyboard": [[
                    {"text": "Добавить в чат с друзьями", "url": f"https://t.me/{bot_name}?startgroup=hello"}]]})
            else:
                tg("sendMessage", chat_id=chat, text=GROUP_HELLO, parse_mode="HTML")
        return
    if cmd in ("/privacy", "/donate", "/paysupport"):
        if ours and fresh(user, cmd, 5):
            if cmd == "/privacy":
                tg("sendMessage", chat_id=chat, text=PRIVACY, parse_mode="HTML")
            elif cmd == "/donate":
                donate(chat)
            else:
                tg("sendMessage", chat_id=chat, text=PAYSUPPORT, reply_markup={"inline_keyboard": [[
                    {"text": "Вернуть звёзды", "callback_data": "refund:"}]]})
        return
    if cmd == "/auto":
        if to in ("", bot_name.lower()) and fresh(user, "auto", 2):
            if private:
                answer = AUTO_PRIVATE
            elif not is_admin(msg):
                answer = AUTO_ADMINS
            elif toggle_auto(chat):
                answer = AUTO_ON + ("" if can_read(chat) else AUTO_BLIND)
            else:
                answer = AUTO_OFF
            tg("sendMessage", chat_id=chat, text=answer, reply_parameters=reply_to(msg["message_id"]))
        return
    if private:
        found = media_of(msg)
        if not found:
            answer = NO_TEXT_PRIVATE if cmd == "/text" else HINT
            if fresh(user, answer[:10], 20):
                tg("sendMessage", chat_id=chat, text=answer, reply_parameters=reply_to(msg["message_id"]))
            return
        return take(msg, found, True, msg)
    target = request_in_group(msg)
    if target is not None or cmd == "/text" and to in ("", bot_name.lower()):
        found = target and media_of(target)
        if not found:
            if fresh(user, "not-audio" if target else "no-reply", 20):
                tg("sendMessage", chat_id=chat, text=NOT_AUDIO if target else NO_REPLY,
                   reply_parameters=reply_to(msg["message_id"]))
            return
        return take(target, found, True, msg)
    found = media_of(msg, everything=False)
    if found and is_auto(chat):
        take(msg, found, False, msg)


def can_read(chat):
    """Видит ли бот все сообщения группы: режим приватности выключен или бот — админ."""
    if can_read_all:
        return True
    me = quiet("getChatMember", chat_id=chat, user_id=bot_id)
    return bool(me) and me.get("status") == "administrator"


can_read_all = False


def queued_sec(skip=None):
    """Сколько секунд звука сейчас в работе у всех (кроме skip)."""
    with lock:
        return sum(max(j.duration, j.sec) for j in working.values() if j is not skip)


def take(msg, media, explicit, req_msg):
    """Поставить запись в работу. msg — сообщение с записью, req_msg — кто попросил."""
    job = Job(msg, media, explicit, req_msg)
    group = None if job.private else job.chat

    def refuse(text):
        if explicit:
            tg("sendMessage", chat_id=job.chat, text=text, reply_parameters=reply_to(job.msg_id))

    if job.size > IN_MAX:
        return refuse(TOO_BIG.format(mb=round(job.size / 1048576), max=IN_MAX // 1048576))
    if job.duration > MAX_SEC:
        return refuse(TOO_LONG.format(h=MAX_SEC // 3600))
    why = limit_text(job.user, group, job.duration)
    if why:
        return refuse(why)
    with lock:
        if (job.chat, job.msg_id) in working:
            busy = ALREADY
        elif len(working) >= QUEUE_MAX or sum(max(j.duration, j.sec) for j in working.values()) \
                + job.duration > QUEUE_SEC:
            busy = BUSY
        elif sum(j.user == job.user for j in working.values()) >= LINE:
            busy = f"У тебя уже {LINE} записей в работе. Дождись их — и присылай ещё."
        else:
            busy = None
            working[(job.chat, job.msg_id)] = job
    if busy:
        return refuse(busy)
    if job.duration:
        job.charge(job.duration)
    if not job.duration or job.duration > QUICK or ahead() > 2:
        try:
            job.status = Status(job.chat, job.msg_id, "Принял запись…")
        except TgError:     # писать в чат нельзя (бота ограничили) — иначе место в очереди занято навсегда
            return job.cleanup()
    threading.Thread(target=job.prepare, daemon=True).start()


def on_button(q):
    if (q.get("data") or "") == "refund:":
        return refund(q)
    quiet("answerCallbackQuery", callback_query_id=q["id"])


# --- звёзды ---

star_links = {}


def donate(chat):
    """Кнопки «⭐ 10 / 50 / 100 / 500». Каждая сразу открывает оплату звёздами."""
    for n in STARS:
        if n not in star_links:
            star_links[n] = tg("createInvoiceLink", title="Спасибо боту «Текст»", payload=f"thanks:{n}",
                               description="Добровольное спасибо, ничего не покупается. "
                                           "Передумал — /paysupport, верну.",
                               currency="XTR", prices=[{"label": "Спасибо", "amount": n}])
    tg("sendMessage", chat_id=chat, text=DONATE, reply_markup={"inline_keyboard": [[
        {"text": f"⭐ {n}", "url": star_links[n]} for n in STARS]]})


def thanks_log():
    try:
        return [json.loads(line) for line in THANKS.read_text("utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def on_paid(msg):
    pay, user = msg["successful_payment"], msg["from"]["id"]
    with lock:
        with THANKS.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"user": user, "stars": pay["total_amount"], "time": int(time.time()),
                                "charge": pay["telegram_payment_charge_id"]}) + "\n")
    print(f"Донат: {pay['total_amount']} ⭐", flush=True)
    tg("sendMessage", chat_id=msg["chat"]["id"], text="Спасибо! ❤️ Звёздочка долетела, автору приятно.")


def refund(q):
    user = q["from"]["id"]
    with lock:
        log = thanks_log()
        last = next((t for t in reversed(log) if t["user"] == user and not t.get("refunded")), None)
        if last:
            try:
                tg("refundStarPayment", user_id=user, telegram_payment_charge_id=last["charge"])
            except TgError:
                last = None
            else:
                last["refunded"] = True
                THANKS.write_text("".join(json.dumps(t) + "\n" for t in log), "utf-8")
    quiet("answerCallbackQuery", callback_query_id=q["id"], show_alert=True,
          text=f"Вернул {last['stars']} ⭐" if last else "Возвращать нечего — донатов от тебя не нашёл.")


# --- главный цикл ---

def sender(update):
    """Кто прислал апдейт — для счёта сообщений. None — бот его и так пропустит
    (болтовня в группе): такой не считаем и не разбираем."""
    for kind in ("callback_query", "pre_checkout_query"):
        if kind in update:
            return update[kind]["from"]["id"]
    msg = update.get("message")
    if not msg:
        return None
    user = (msg.get("from") or {}).get("id") or msg["chat"]["id"]
    if msg["chat"].get("type") == "private" or added_me(msg) or msg.get("successful_payment"):
        return user
    text = msg.get("text") or ""
    if text.startswith("/") or request_in_group(msg) is not None:
        return user
    return user if media_of(msg, everything=False) and is_auto(msg["chat"]["id"]) else None


def handle(update):
    try:
        if "message" in update:
            on_message(update["message"])
        elif "callback_query" in update:
            on_button(update["callback_query"])
        elif "pre_checkout_query" in update:     # звёзды: Telegram ждёт «да» не дольше 10 секунд
            q = update["pre_checkout_query"]
            ok = q.get("currency") == "XTR" and str(q.get("invoice_payload", "")).startswith("thanks:")
            tg("answerPreCheckoutQuery", pre_checkout_query_id=q["id"], ok=ok,
               error_message=None if ok else "Этот платёж не от этого бота.")
    except Exception as e:
        print("update:", type(e).__name__, e, flush=True)
    finally:
        HANDLERS.release()


def setup():
    """Имя, описание и меню бота — то, что человек видит до первого сообщения."""
    me = tg("getMe")

    def texts():    # Telegram отвечает на это медленно — не держим из-за них приём сообщений
        if (quiet("getMyName") or {}).get("name") != NAME:     # имя Telegram даёт менять редко
            quiet("setMyName", name=NAME)
        quiet("setMyCommands", commands=[{"command": "start", "description": "Как пользоваться"},
                                         {"command": "privacy", "description": "Что я храню"},
                                         {"command": "donate", "description": "Сказать спасибо звёздочкой"}])
        quiet("setMyCommands", scope={"type": "all_group_chats"},
              commands=[{"command": "text", "description": "Ответом на голосовое — пришлю текст"},
                        {"command": "auto", "description": "Расшифровываю голосовые сам: вкл / выкл"},
                        {"command": "start", "description": "Как пользоваться"}])
        quiet("setMyShortDescription", short_description=SHORT_ABOUT)
        quiet("setMyDescription", description=ABOUT)
    threading.Thread(target=texts, daemon=True).start()
    print("В группах видит все сообщения:",
          "да" if me.get("can_read_all_group_messages") else "нет (BotFather → /setprivacy → Disable)", flush=True)
    return me


def tg_updates(offset):
    """Долгий опрос: Telegram держит запрос до 50 секунд, пока не появится сообщение."""
    body = json.dumps({"offset": offset, "timeout": 50,
                       "allowed_updates": ["message", "callback_query", "pre_checkout_query"]})
    req = urllib.request.Request(f"{TG_API}/bot{TOKEN}/getUpdates", data=body.encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=70, context=TLS).read())
    except urllib.error.HTTPError as e:
        raise TgError(f"HTTP {e.code}", e.code)
    except NET as e:
        raise TgError(str(e))
    if not r.get("ok"):
        raise TgError(r.get("description", ""), r.get("error_code", 0))
    return r["result"]


def janitor():
    """Раз в минуту: выгрузить Whisper, если давно не нужен, забыть старые лимиты
    и убрать у своего сервера Telegram записи, которые никто не забрал (бот упал, запрос оборвался)."""
    while True:
        time.sleep(60)
        speech.unload_idle()
        now = time.time()
        if TG_LOCAL:
            for f in TG_DIR.glob("*/*/*"):      # <токен>/<voice, videos…>/<файл>; td.binlog глубже не лежит
                try:
                    if f.is_file() and f.stat().st_mtime < now - 3600:
                        f.unlink()
                except OSError:
                    pass
        with lock:
            for who in [w for w, p in people.items()
                        if p["muted"] < now and not p["said"] and (not p["spent"] or p["spent"][-1][0] < now - 86400)]:
                del people[who]


def main():
    global bot_name, bot_id, can_read_all
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        sys.exit("Нужен ffmpeg (brew install ffmpeg / apt install ffmpeg)")
    if not (speech.MODELS / "gigaam" / "config.json").exists():
        sys.exit(f"Нет моделей в {speech.MODELS} — запусти tools/models.sh")
    DATA.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    for old in WORK.iterdir():          # недоделанное с прошлого запуска
        if old.suffix in (".src", ".pcm"):
            old.unlink(missing_ok=True)
    load_auto()
    speech.gigaam()                     # модель грузится ~5 с — до первого голосового, а не во время
    while True:
        try:
            me = setup()
            bot_name, bot_id = me.get("username") or "", me.get("id") or 0
            can_read_all = bool(me.get("can_read_all_group_messages"))
            break
        except TgError as e:
            if e.code == 401:
                sys.exit("Telegram не принял токен — проверь bot.token")
            print("Telegram недоступен:", e, flush=True)
            time.sleep(15)
    print(f"Бот @{bot_name} запущен, Telegram: {'свой сервер' if TG_LOCAL else 'обычный'}, "
          f"файлы до {IN_MAX // 1048576} МБ, другие языки: {'да' if speech.whisper() else 'нет'}", flush=True)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=janitor, daemon=True).start()
    offset = 0
    while True:
        try:
            updates = tg_updates(offset)
        except TgError as e:
            if e.code == 401:
                sys.exit("Telegram не принял токен — проверь bot.token")
            print("getUpdates:", e, flush=True)
            time.sleep(5)
            continue
        for u in updates or []:
            offset = max(offset, u["update_id"] + 1)
            user = sender(u)
            if user is None:
                continue
            if "pre_checkout_query" not in u:
                muted = flood(user)
                if muted == "mute" and "message" in u and u["message"]["chat"].get("type") == "private":
                    quiet("sendMessage", chat_id=u["message"]["chat"]["id"],
                          text="Слишком много сообщений подряд. Передохну 10 минут — потом продолжим.")
                if muted:
                    continue
            HANDLERS.acquire()
            threading.Thread(target=handle, args=(u,), daemon=True).start()


def set_avatar(path=AVATAR):
    """Аватарка бота. Telegram копит аватарки, поэтому старую сначала убираем."""
    quiet("removeMyProfilePhoto")
    tg("setMyProfilePhoto", files={"avatar": (path.name, path.read_bytes(), "image/jpeg")},
       photo=json.dumps({"type": "static", "photo": "attach://avatar"}))
    print("Аватарка поставлена:", path.name)


if __name__ == "__main__":
    if sys.argv[1:2] == ["avatar"]:
        set_avatar(Path(sys.argv[2]) if len(sys.argv) > 2 else AVATAR)
    else:
        main()

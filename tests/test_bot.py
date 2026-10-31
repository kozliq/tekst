"""Бот целиком на поддельном Telegram: присылаем записи — проверяем, что пришёл правильный текст.

Запуск (из папки tekst, Python с пакетами из requirements.txt): python -m unittest discover -s tests
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import samples  # noqa: E402
from mock_tg import MockTelegram  # noqa: E402

GROUP = -1001


def length(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout
    return float(out.strip() or 0)


@unittest.skipUnless(samples.CAN_SPEAK, "нужен синтезатор речи macOS (say)")
class Bot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = samples.make()
        cls.tg = MockTelegram()
        for name, path in cls.s.items():
            cls.tg.files[name] = path
        cls.dir = Path(tempfile.mkdtemp(prefix="tekst-bot-"))
        env = {**os.environ, "TEKST_TOKEN": "123:test", "TEKST_TG_API": cls.tg.url,
               "TEKST_DATA": str(cls.dir / "data"), "TEKST_WORK": str(cls.dir / "work"),
               "TEKST_MSG_MAX": "600", "TEKST_PER_HOUR": "5", "PYTHONUNBUFFERED": "1"}
        cls.log = open(cls.dir / "bot.log", "w+")
        cls.bot = subprocess.Popen([sys.executable, "-I", str(HERE.parent / "tekst.py")], env=env,
                                   stdout=cls.log, stderr=subprocess.STDOUT)
        assert cls.tg.wait(lambda c: any(m == "setMyDescription" for m, _ in c), 60), "бот не запустился"

    @classmethod
    def tearDownClass(cls):
        cls.bot.terminate()
        cls.bot.wait()
        cls.tg.stop()
        cls.log.seek(0)
        print("\n--- журнал бота ---\n" + cls.log.read())

    chat = 100

    def setUp(self):
        Bot.chat += 1
        self.chat = Bot.chat

    def media(self, name, kind="voice", size=None):
        m = {"file_id": name, "file_size": size or Path(self.s[name]).stat().st_size}
        if kind != "document":
            m["duration"] = round(length(self.s[name]))
        else:
            m["mime_type"] = "audio/mpeg"
            m["file_name"] = Path(self.s[name]).name
        return m

    def send(self, name, kind="voice", size=None, **extra):
        return self.tg.message(self.chat, **{kind: self.media(name, kind, size)}, **extra)

    def replies(self, chat=None):
        chat = chat or self.chat
        return [p for p in self.tg.of("sendMessage", chat) if p.get("reply_parameters")] + \
            self.tg.of("sendDocument", chat)

    def texts(self, chat=None):
        chat = chat or self.chat
        return [p.get("text", "") for p in self.tg.of("sendMessage", chat) + self.tg.of("editMessageText", chat)]

    def wait_text(self, word, chat=None, timeout=90):
        got = self.tg.wait(lambda c: any(word.lower() in t.lower() for t in self.texts(chat)), timeout)
        self.assertTrue(got, f"нет сообщения со словом «{word}»: {self.texts(chat)}")
        return next(t for t in self.texts(chat) if word.lower() in t.lower())

    def assert_clean(self):
        """Записей не осталось: всё, что скачали и распаковали, удалено."""
        time.sleep(0.3)
        left = [p.name for p in (self.dir / "work").iterdir()]
        self.assertEqual(left, [], "после расшифровки остались файлы")

    # --- личка ---

    def test_start(self):
        self.tg.message(self.chat, text="/start")
        self.wait_text("верну текстом")
        start = self.tg.of("sendMessage", self.chat)[0]
        self.assertIn("startgroup", json.dumps(start["reply_markup"]))

    def test_privacy(self):
        self.tg.message(self.chat, text="/privacy")
        self.wait_text("Записи — нет")

    def test_short_voice(self):
        msg = self.send("short")
        text = self.wait_text("перезвони")
        self.assertIn("шести", text.lower())
        r = self.replies()[-1]
        self.assertEqual(r["reply_parameters"]["message_id"], msg["message_id"])
        self.assertFalse(self.tg.of("editMessageText", self.chat), "для короткого не нужен «расшифровываю»")
        self.assertTrue(self.tg.of("sendChatAction", self.chat))
        self.assert_clean()

    def test_forwarded_voice(self):
        self.send("short", forward_origin={"type": "hidden_user", "sender_user_name": "Кто-то", "date": 1})
        self.wait_text("перезвони")

    def test_long_voice_in_parts(self):
        # 70 с: статус с прогрессом, в конце его убрали; текст — абзацами и (при лимите 600 знаков) частями
        self.send("long")
        self.wait_text("Расшифровываю")
        self.wait_text("обнимаю", timeout=120)
        self.assertTrue(self.tg.wait(lambda c: self.tg.of("deleteMessage", self.chat), 10), "статус не убран")
        parts = [p["text"] for p in self.replies() if "суббот" in p["text"] or "обнимаю" in p["text"]
                 or "дач" in p["text"]]
        self.assertGreaterEqual(len(parts), 2)
        self.assertTrue(all(len(p) <= 600 for p in parts))
        self.assertIn("\n\n", "\n\n".join(parts))
        self.assert_clean()

    def test_huge_as_file(self):
        self.send("huge")
        self.assertTrue(self.tg.wait(lambda c: self.tg.of("sendDocument", self.chat), 180), "нет файла")
        doc = self.tg.of("sendDocument", self.chat)[0]
        text = Path(doc["document"]).read_text("utf-8")
        self.assertIn("отпуск", text.lower())
        self.assertGreaterEqual(text.count("\n\n"), 3, "нет абзацев")
        self.assertIn("файлом", doc["caption"])
        # лимит на человека: ещё одна такая же запись за час — уже нельзя (лимит в тесте — 5 минут)
        self.send("huge")
        self.wait_text("За час ты расшифровал")
        self.assert_clean()

    def test_fake_duration(self):
        # чужой бот может прислать голосовое с подделанной длиной «1 секунда» — лимит считаем по настоящей
        fake = {**self.media("huge"), "duration": 1}
        self.tg.message(self.chat, voice=fake)
        self.assertTrue(self.tg.wait(lambda c: self.tg.of("sendDocument", self.chat), 180), "нет файла")
        self.tg.message(self.chat, voice=fake)
        self.wait_text("За час ты расшифровал")
        self.assertEqual(len(self.tg.of("sendDocument", self.chat)), 1)
        self.assert_clean()

    def test_video_note(self):
        self.send("note", kind="video_note")
        self.wait_text("перезвони")

    def test_mp3_document(self):
        self.send("mp3", kind="document")
        self.wait_text("перезвони")
        self.assert_clean()

    def test_english(self):
        self.send("english")
        text = self.wait_text("coffee", timeout=120)
        self.assertIn("late", text.lower())

    def test_silence_and_tone(self):
        self.send("silence")
        self.wait_text("Не расслышал")
        self.send("tone")
        self.tg.wait(lambda c: sum("Не расслышал" in t for t in self.texts()) >= 2, 30)
        self.assertEqual(sum("Не расслышал" in t for t in self.texts()), 2)

    def test_broken_files(self):
        self.send("junk", kind="audio")
        self.wait_text("Не получилось открыть файл")
        self.send("noaudio", kind="video")
        self.wait_text("нет звука")
        self.send("hls", kind="audio")       # плейлист со ссылкой на чужой файл — не открываем
        self.tg.wait(lambda c: sum("Не получилось" in t or "нет звука" in t for t in self.texts()) >= 2, 30)
        self.assertFalse(any("8 Uhr" in t for t in self.texts()))
        self.assert_clean()

    def test_too_big(self):
        self.send("short", size=50 * 1024 * 1024)
        self.wait_text("до 20 МБ")

    def test_text_and_links(self):
        self.tg.message(self.chat, text="привет")
        self.wait_text("Пришли голосовое")

    def test_fair_queue(self):
        # кто-то прислал 3 минуты, другой сразу за ним — короткое: короткое не ждёт длинное до конца
        other = self.chat + 5000
        self.send("huge")
        self.wait_text("Расшифровываю")
        self.tg.message(other, voice=self.media("short"))
        self.wait_text("перезвони", chat=other)
        self.assertFalse(self.tg.of("sendDocument", self.chat), "короткое ждало, пока доделается длинное")
        self.tg.wait(lambda c: self.tg.of("sendDocument", self.chat), 180)

    def test_flood(self):
        for _ in range(22):
            self.tg.message(self.chat, text="а")
        self.wait_text("Слишком много сообщений")

    def test_donate(self):
        self.tg.message(self.chat, text="/donate")
        self.wait_text("звёздочку")
        self.assertEqual(len(self.tg.of("createInvoiceLink")), 4)

    # --- группа ---

    def group(self, user, **fields):
        return self.tg.message(GROUP - self.chat, user=user, kind="supergroup", **fields)

    def test_group_on_request(self):
        g, user = GROUP - self.chat, self.chat
        voice = self.group(user, voice=self.media("short"))
        time.sleep(3)
        self.assertFalse(self.replies(g), "без /auto в группе сам не расшифровывает")
        self.group(user, text="/text")
        self.wait_text("Ответь этой командой", chat=g)
        self.group(user, text="/text@tekst_test_bot", reply_to_message=voice)
        self.wait_text("перезвони", chat=g)
        r = [p for p in self.replies(g) if "ерезвони" in p.get("text", "")][-1]
        self.assertEqual(r["reply_parameters"]["message_id"], voice["message_id"], "ответ — на само голосовое")
        hi = self.group(user, text="всем привет")
        self.group(user, text="текст", reply_to_message=hi)
        self.wait_text("нет голосового", chat=g)
        self.group(user, text="/text@other_bot", reply_to_message=voice)
        time.sleep(2)
        self.assertEqual(sum("ерезвони" in t for t in self.texts(g)), 1, "команда другому боту — не нам")

    def test_group_words_and_fold(self):
        g, user = GROUP - self.chat, self.chat
        voice = self.group(user, voice=self.media("long"))
        self.group(user, text="расшифруй", reply_to_message=voice)
        self.tg.wait(lambda c: any("<blockquote expandable>" in p.get("text", "") for p in self.replies(g)), 120)
        folded = [p for p in self.replies(g) if "<blockquote expandable>" in p.get("text", "")]
        self.assertTrue(folded, "длинный текст в группе не свёрнут")
        self.assertEqual(folded[0]["parse_mode"], "HTML")

    def test_auto(self):
        g, user, admin = GROUP - self.chat, self.chat, self.chat + 7000
        self.tg.admins.add(admin)
        self.group(user, text="/auto")
        self.wait_text("только админы", chat=g)
        self.group(admin, text="/auto")
        self.wait_text("расшифровываю все голосовые", chat=g)
        auto = (self.dir / "data" / "auto.json").read_text()
        self.assertNotIn(str(g), auto, "номер группы в файле открытым текстом")
        self.group(user, video_note=self.media("note"))
        self.wait_text("перезвони", chat=g)
        self.group(user, voice=self.media("silence"))
        time.sleep(3)
        self.assertFalse(any("Не расслышал" in t for t in self.texts(g)), "в /auto тишину не комментируем")
        self.group(admin, text="/auto")
        self.wait_text("сам больше не расшифровываю", chat=g)
        n = len(self.replies(g))
        self.group(user, voice=self.media("short"))
        time.sleep(3)
        self.assertEqual(len(self.replies(g)), n)

    def test_added_to_group(self):
        g = GROUP - self.chat
        self.group(self.chat, new_chat_members=[{"id": 1, "is_bot": True, "username": "tekst_test_bot"}])
        self.wait_text("Привет, чат", chat=g)

    # --- что остаётся на диске ---

    def test_zz_nothing_kept(self):
        """Последним: в папке данных только настройки и числа, в журнале — ни слова из записей."""
        time.sleep(2)
        data = self.dir / "data"
        self.assertLessEqual({p.name for p in data.iterdir()}, {"auto.json", "stats.json", "thanks.jsonl"})
        stats = json.loads((data / "stats.json").read_text())
        self.assertEqual(set(stats), {"records", "minutes", "foreign"})
        self.assertGreater(stats["records"], 5)
        self.log.flush()
        log = (self.dir / "bot.log").read_text().lower()
        for word in ("перезвони", "суббот", "coffee", str(GROUP)):
            self.assertNotIn(word, log, f"в журнале «{word}»")


@unittest.skipUnless(samples.real(), "нет настоящих записей (TEKST_REAL_AUDIO)")
class RealAudio(unittest.TestCase):
    def test_real(self):
        sys.path.insert(0, str(HERE.parent))
        import speech
        for path in samples.real():
            pcm_path = Path(tempfile.mktemp(suffix=".pcm"))
            try:
                sec = speech.decode(path, pcm_path, 3600)
                t = time.time()
                res = speech.run(speech.load_pcm(pcm_path))
                print(f"\n{path.name}: {sec:.0f} с за {time.time() - t:.1f} с, {res.engine}\n{res.text[:300]}")
                self.assertTrue(res.paragraphs)
            finally:
                pcm_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

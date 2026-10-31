"""Поддельный Telegram Bot API для тестов: бот ходит сюда вместо api.telegram.org.

Тест кладёт «входящие» через push(), записи для getFile — через files[file_id] = путь,
а потом смотрит, что бот вызвал (calls) и какие файлы прислал (в параметрах — путь к сохранённому файлу).
"""
import json
import re
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class MockTelegram:
    def __init__(self):
        self.calls = []                 # (метод, параметры)
        self.updates = []
        self.files = {}                 # file_id → путь к записи на диске
        self.too_big = set()            # file_id, на которые getFile ответит «file is too big»
        self.admins = set()             # кто админ в группах
        self.read_all = True            # бот видит все сообщения групп (режим приватности выключен)
        self.local_paths = False        # как свой сервер Telegram: getFile отдаёт путь на диске
        self.out = Path(tempfile.mkdtemp(prefix="mocktg-"))
        self.cond = threading.Condition()
        self._msg = 1000
        self._upd = 0
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                m = re.match(r"/file/bot[^/]+/(.+)", self.path)
                path = mock.files.get(m.group(1)) if m else None
                if not path:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = Path(path).read_bytes()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                m = re.match(r"/bot([^/]+)/(\w+)", self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                params, files = mock.parse(self.headers.get("Content-Type", ""), body)
                ok, res = mock.answer(m.group(2), params, files)
                out = json.dumps({"ok": True, "result": res} if ok else
                                 {"ok": False, "error_code": 400, "description": res}).encode()
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()

    @staticmethod
    def parse(ctype, body):
        if ctype.startswith("application/json"):
            return json.loads(body or b"{}"), {}
        boundary = ctype.split("boundary=")[1].encode()
        params, files = {}, {}
        for part in body.split(b"--" + boundary)[1:-1]:
            head, _, data = part[2:].partition(b"\r\n\r\n")
            data = data[:-2]
            name = re.search(rb'name="([^"]+)"', head).group(1).decode()
            if b"filename=" in head:
                files[name] = data
            else:
                v = data.decode()
                try:
                    params[name] = json.loads(v)
                except ValueError:
                    params[name] = v
        return params, files

    def answer(self, method, params, files):
        saved = {}
        for name, data in files.items():
            p = self.out / f"{len(self.calls)}-{name}"
            p.write_bytes(data)
            saved[name] = str(p)
        with self.cond:
            if method == "getUpdates":
                end = time.time() + min(float(params.get("timeout", 0)), 1.0)
                while True:
                    ups = [u for u in self.updates if u["update_id"] >= params.get("offset", 0)]
                    if ups or time.time() >= end:
                        return True, ups
                    self.cond.wait(end - time.time())
            self.calls.append((method, {**params, **saved}))
            self.cond.notify_all()
            if method == "getMe":
                return True, {"id": 1, "is_bot": True, "username": "tekst_test_bot",
                              "can_read_all_group_messages": self.read_all}
            if method == "getChatMember":
                admin = params.get("user_id") in self.admins
                return True, {"status": "administrator" if admin else "member", "user": {"id": params.get("user_id")}}
            if method == "getFile":
                fid = params["file_id"]
                if fid in self.too_big:
                    return False, "Bad Request: file is too big"
                if fid not in self.files:
                    return False, "Bad Request: invalid file_id"
                return True, {"file_id": fid, "file_path": str(Path(self.files[fid]).resolve())
                              if self.local_paths else fid}
            if method.startswith("send"):
                self._msg += 1
                return True, {"message_id": self._msg, "chat": {"id": params.get("chat_id")}}
            if method == "createInvoiceLink":
                return True, f"https://t.me/$inv{params['payload']}"
            if method == "getMyName":
                return True, {"name": ""}
            return True, True

    def push(self, kind, obj):
        with self.cond:
            self._upd += 1
            self.updates.append({"update_id": self._upd, kind: obj})
            self.cond.notify_all()

    def message(self, chat, user=None, kind="private", **fields):
        self._msg += 1
        msg = {"message_id": self._msg, "chat": {"id": chat, "type": kind},
               "from": {"id": user or chat, "first_name": "Тест"}, "date": int(time.time()), **fields}
        self.push("message", msg)
        return msg

    def wait(self, test, timeout=60):
        """Ждать, пока test(calls) не станет правдой. Вернёт его значение."""
        end = time.time() + timeout
        with self.cond:
            while True:
                got = test(self.calls)
                if got or time.time() >= end:
                    return got
                self.cond.wait(min(1.0, end - time.time()))

    def of(self, method, chat=None):
        return [p for m, p in self.calls if m == method and (chat is None or p.get("chat_id") == chat)]

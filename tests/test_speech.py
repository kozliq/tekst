"""Мелочи speech.py и нарезки текста — без моделей и Telegram."""
import os
import sys
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault("TEKST_TOKEN", "123:test")
import speech  # noqa: E402
import tekst  # noqa: E402


def talk(*parts):
    """Звук из кусков: (секунды, громко?) — громкое как речь (шум), тихое как пауза."""
    rng = np.random.default_rng(1)
    out = [(rng.standard_normal(int(sec * speech.SR)) * (3000 if loud else 30)).astype(np.int16)
           for sec, loud in parts]
    return np.concatenate(out)


class Plan(unittest.TestCase):
    def test_cut_in_pause(self):
        # 14 с речи, пауза 1 с, ещё 14 с: резать надо в паузе, а не на 20-й секунде
        pieces = speech.plan(talk((14, True), (1, False), (14, True)))
        self.assertEqual(len(pieces), 2)
        self.assertAlmostEqual(pieces[0][1], 14.5, delta=0.2)
        self.assertGreater(pieces[1][2], 0.8, "пауза перед вторым куском")

    def test_never_longer_than_max(self):
        pieces = speech.plan(talk((95, True)))
        self.assertTrue(all(b - a <= speech.CUT_MAX + 0.01 for a, b, _ in pieces))
        self.assertAlmostEqual(pieces[-1][1], 95, delta=0.1)

    def test_skip_silence(self):
        self.assertEqual(speech.plan(talk((30, False))), [])
        pieces = speech.plan(talk((5, True), (40, False), (5, True)))
        self.assertEqual(len(pieces), 2, "тихий кусок посередине не распознаём")


class Text(unittest.TestCase):
    def test_tidy(self):
        self.assertEqual(speech.tidy("вообще— как ,  так"), "вообще — как, так")
        self.assertEqual(speech.tidy("обещают+18"), "обещают +18")
        self.assertEqual(speech.tidy("«Ребята, начать? » ему"), "«Ребята, начать?» ему")

    def test_join_mid_sentence(self):
        self.assertEqual(speech.join_piece("как наше", "Ухо воспринимает"), "ухо воспринимает")
        self.assertEqual(speech.join_piece("Привет.", "Как дела"), "Как дела")
        self.assertEqual(speech.join_piece("пишу в", "ООО Ромашка"), "ООО Ромашка")

    def test_paragraph_on_long_pause(self):
        paras = speech.paragraphs([(0, 3, "Первая мысль, довольно длинная, чтобы быть отдельным абзацем сама по себе, без продолжения."),
                                   (3.1, 5, "Ещё про то же."), (8, 10, "А это уже другое.")])
        self.assertEqual(len(paras), 2)

    def test_pieces(self):
        paras = ["а" * 300 + ".", "б" * 300 + ".", "в. " * 400]
        parts = tekst.pieces(paras, 600)
        self.assertTrue(all(len(p) <= 600 for p in parts))
        self.assertEqual("".join(parts).replace("\n", "").replace(" ", ""),
                         "".join(paras).replace(" ", ""))

    def test_suspicious(self):
        self.assertTrue(speech.suspicious(list("Hey, it's me"), [-0.1] * 12))
        self.assertFalse(speech.suspicious(list("Привет, это я"), [-0.1] * 13))
        self.assertTrue(speech.suspicious(list("Салем"), [-0.6] * 5))


if __name__ == "__main__":
    unittest.main()

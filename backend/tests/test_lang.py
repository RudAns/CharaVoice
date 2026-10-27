"""Language choice for material-library voices (run: python -m unittest discover backend/tests)."""
import unittest

from charavoice import simple


class TextLang(unittest.TestCase):
    def test_kana_is_japanese(self):
        for folder in ("ja", "zh", "en"):
            self.assertEqual(simple.text_lang("今日はいい天気ですね。", folder), "ja")

    def test_chinese_sentence_to_japanese_voice_is_chinese(self):
        self.assertEqual(simple.text_lang("今天天气不错，我们去散步吧。", "ja"), "zh")
        self.assertEqual(simple.text_lang("我在东京工作", "ja"), "zh")  # simplified forms, no kana

    def test_long_kanji_without_kana_is_chinese(self):
        self.assertEqual(simple.text_lang("天下太平国家安康", "ja"), "zh")

    def test_short_kanji_word_follows_the_folder(self):
        self.assertEqual(simple.text_lang("東京", "ja"), "ja")
        self.assertEqual(simple.text_lang("東京", "zh"), "zh")
        self.assertEqual(simple.text_lang("東京", "en"), "zh")

    def test_chinese_punctuation_or_forms_make_short_text_chinese(self):
        self.assertEqual(simple.text_lang("你们好？", "ja"), "zh")
        self.assertEqual(simple.text_lang("时间", "ja"), "zh")

    def test_latin_is_english(self):
        self.assertEqual(simple.text_lang("Hello there, how are you?", "ja"), "en")

    def test_mixed_chinese_english_stays_chinese(self):
        self.assertEqual(simple.text_lang("我今天用 Python 写代码。", "en"), "zh")

    def test_accent_warning_only_across_languages(self):
        self.assertIsNone(simple.accent_warning("ja", "ja"))
        self.assertIn("口音", simple.accent_warning("ja", "zh"))


class Doubt(unittest.TestCase):
    def test_per_language_checks(self):
        self.assertIsNone(simple._doubt("你好，今天天气不错。", 2.0, None, "zh"))
        self.assertIsNotNone(simple._doubt("こんにちは", 1.0, None, "zh"))
        self.assertIsNone(simple._doubt("Hello there, how are you today?", 1.8, None, "en"))
        self.assertIsNotNone(simple._doubt("你好", 1.0, None, "en"))
        self.assertIsNotNone(simple._doubt("可恶。", 1.1, None, "ja"))  # no kana in a Japanese folder


if __name__ == "__main__":
    unittest.main()

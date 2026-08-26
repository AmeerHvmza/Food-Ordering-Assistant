"""Unit tests for Orpheus 200-character chunking. No network."""

from __future__ import annotations

import unittest

from voice.chunker import ORPHEUS_CHAR_LIMIT, chunk_for_tts, strip_for_speech


class ChunkerTests(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(chunk_for_tts(""), [])
        self.assertEqual(chunk_for_tts("   \n"), [])

    def test_short_stays_one_chunk(self) -> None:
        text = "The nihari here is the popular pick."
        chunks = chunk_for_tts(text)
        self.assertEqual(chunks, [text])

    def test_strips_markdown(self) -> None:
        chunks = chunk_for_tts("**Foods Inn** has great **burgers**.")
        self.assertEqual(len(chunks), 1)
        self.assertNotIn("*", chunks[0])

    def test_does_not_split_rupees_from_amount(self) -> None:
        text = "The nihari is Rs. 750 and the maghaz is Rs. 1125."
        chunks = chunk_for_tts(text)
        joined = " ".join(chunks)
        self.assertIn("Rs. 750", joined)
        self.assertIn("Rs. 1125", joined)
        for chunk in chunks:
            self.assertFalse(chunk.endswith("Rs."))

    def test_does_not_split_decimal_rating(self) -> None:
        text = "Foods Inn is rated 4.9 from 39949 reviews near SMCHS."
        chunks = chunk_for_tts(text)
        self.assertTrue(any("4.9" in c for c in chunks))

    def test_under_limit_sentence_intact(self) -> None:
        text = "A" * 199
        self.assertEqual(chunk_for_tts(text), [text])

    def test_over_limit_splits_not_mid_word(self) -> None:
        word = "biryani"
        text = " ".join([word] * 40)  # well over 200
        chunks = chunk_for_tts(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), ORPHEUS_CHAR_LIMIT)
            self.assertFalse(chunk.startswith("iryani"))

    def test_menu_list_becomes_several_chunks(self) -> None:
        text = (
            "Here are three options. "
            "Chicken Biryani at Rs. 350 from Super Biryani. "
            "Beef Biryani at Rs. 420 from Al Syed. "
            "Maghaz Nihari at Rs. 1125 from Zahid Nihari in Saddar, "
            "which is a hearty pick if you are feeding several people tonight."
        )
        chunks = chunk_for_tts(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), ORPHEUS_CHAR_LIMIT)
        self.assertEqual(" ".join(chunks).replace("  ", " "), strip_for_speech(text))

    def test_clause_split_on_long_sentence(self) -> None:
        text = (
            "This is a long recommendation about the menu: "
            + ("delicious spicy nihari with nalli and maghaz, " * 8)
            + "and naan on the side."
        )
        chunks = chunk_for_tts(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), ORPHEUS_CHAR_LIMIT)
            self.assertTrue(chunk.strip())


if __name__ == "__main__":
    unittest.main()

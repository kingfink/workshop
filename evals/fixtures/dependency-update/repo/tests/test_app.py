import json
import unittest
from pathlib import Path

from app import message


class AppTest(unittest.TestCase):
    def test_message(self):
        self.assertEqual(message(), "HELLO")

    def test_lock(self):
        self.assertEqual(
            json.loads(Path("dependencies.lock").read_text()),
            json.loads(Path("dependencies.json").read_text()),
        )

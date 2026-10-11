import unittest
from collections import Counter, defaultdict

from hbkit.fixtures.generator import SENTINEL, GeneratorOptions, generate
from hbkit.xhb.reader import read_bytes

from .helpers import sample


class GeneratorTests(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(generate(GeneratorOptions(seed=5)).data, generate(GeneratorOptions(seed=5)).data)
        self.assertNotEqual(generate(GeneratorOptions(seed=5)).data, generate(GeneratorOptions(seed=6)).data)

    def test_layout_matches_homebank(self):
        text = sample().data.decode("utf-8")
        lines = text.split("\n")
        self.assertEqual(lines[0], '<?xml version="1.0"?>')
        self.assertEqual(lines[1], '<homebank v="1.6" d="051003">')
        self.assertEqual(lines[-2], "</homebank>")
        self.assertEqual(lines[-1], "")
        self.assertFalse(any(line.startswith(" ") for line in lines))  # no indentation

    def test_truth_categories_exist_and_dominate(self):
        gen = sample()
        book = read_bytes(gen.data)
        paths = {book.category_path(k) for k in book.categories}
        self.assertTrue(set(gen.truth.values()) <= paths)
        # The Netflix merchant is always categorised as streaming.
        cats = Counter(book.category_path(t.category_key) for t in book.transactions
                       if "NETFLIX" in t.memo and t.category_key)
        self.assertEqual(set(cats), {gen.truth["NETFLIX"]})

    def test_inconsistent_merchants_are_inconsistent(self):
        gen = sample()
        book = read_bytes(gen.data)
        per = defaultdict(Counter)
        for t in book.transactions:
            for code in gen.inconsistent:
                if code in t.memo and t.category_key:
                    per[code][t.category_key] += 1
        for code in gen.inconsistent:
            top = per[code].most_common(1)[0][1]
            self.assertLess(top / sum(per[code].values()), 0.9)

    def test_sentinel_marks_all_free_text(self):
        book = read_bytes(sample(sentinel=True).data)
        self.assertTrue(all(SENTINEL in t.memo for t in book.transactions))
        self.assertTrue(all(SENTINEL in c.name for c in book.categories.values()))
        self.assertTrue(all(SENTINEL in p.name for p in book.payees.values()))
        self.assertTrue(all(SENTINEL in a.name for a in book.accounts.values()))
        self.assertTrue(all(SENTINEL in r.search for r in book.rules))
        self.assertIn(SENTINEL, book.properties.title)

    def test_scale(self):
        small = generate(GeneratorOptions(scale=0.5)).transactions
        big = generate(GeneratorOptions(scale=2.0)).transactions
        self.assertLess(small, big)


if __name__ == "__main__":
    unittest.main()

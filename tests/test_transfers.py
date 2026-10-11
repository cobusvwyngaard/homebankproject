"""Finding and linking transfers between own accounts (synthetic file only)."""

import difflib

from hbkit.errors import HbkitError
from hbkit.fixtures.generator import ACC_CARD, ACC_CHEQUE, ACC_SAVINGS
from hbkit.imports.transfers import default_output, find_transfers, link_transfers
from hbkit.xhb import format as F
from hbkit.xhb import serialise as S
from hbkit.xhb.reader import read_bytes

from .helpers import TempDirTestCase, sample


class FindTests(TempDirTestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = read_bytes(sample(unlinked_transfers=True).data)
        cls.result = find_transfers(cls.book)

    def by_memo(self, memo, amount):
        return next(t for t in self.book.transactions if t.memo == memo and t.amount == amount)

    def test_clear_pair(self):
        (pair,) = self.result.pairs
        self.assertEqual((pair.out.account_key, pair.into.account_key, pair.out.amount, pair.gap),
                         (ACC_CHEQUE, ACC_SAVINGS, -1500.0, 1))
        self.assertIn("the text names the other account", pair.signs)
        self.assertTrue(any(s.endswith("earlier transfers between these accounts") for s in pair.signs))

    def test_ambiguous_group(self):
        (group,) = self.result.choices
        self.assertEqual(len(group), 4)  # two outs x two ins
        self.assertEqual({p.out.memo for p in group}, {"PAYMENT TO CARD", "TRANSFER OUT"})
        self.assertEqual({p.into.account_key for p in group}, {ACC_CARD, ACC_SAVINGS})

    def test_without_another_side(self):
        lonely = {(x.txn.memo, x.txn.amount): x for x in self.result.lonely}
        x = lonely[("IB TRANSFER TO SAVINGS", -800.0)]
        self.assertEqual(x.likely_account, ACC_SAVINGS)
        self.assertIn("payment mode is a transfer", x.reasons)
        self.assertIn(("MONTHLY SWEEP", -1234.56), lonely)  # six days from its other side
        self.assertEqual(len(lonely), 3)

    def test_window(self):
        wide = find_transfers(self.book, 6)
        self.assertIn((-1234.56, 6), [(p.out.amount, p.gap) for p in wide.pairs])
        self.assertEqual(len(find_transfers(self.book, 0).pairs), 0)
        with self.assertRaises(HbkitError):
            find_transfers(self.book, 40)

    def test_only_uncategorised_open_and_unlinked(self):
        for p in self.result.pairs + [p for g in self.result.choices for p in g]:
            for t in (p.out, p.into):
                self.assertEqual((t.category_key, t.kxfer, t.is_internal_transfer), (0, 0, False))
                self.assertFalse(self.book.accounts[t.account_key].is_closed)

    def test_standard_sample_has_none(self):
        r = find_transfers(read_bytes(sample().data))
        self.assertEqual((r.pairs, r.choices, r.lonely), ([], [], []))


class LinkTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.src = self.write_sample("finances.xhb", unlinked_transfers=True)
        self.book = read_bytes(self.src.read_bytes())
        self.result = find_transfers(self.book)
        self.out = self.tmp / "out"

    def link(self, pairs, **kw):
        return link_transfers(self.book, pairs, 4, self.src, default_output(self.out), **kw)

    def test_link_pair_and_choice(self):
        pair = self.result.pairs[0]
        choice = next(p for p in self.result.choices[0] if p.out.memo == "PAYMENT TO CARD" and p.into.account_key == ACC_CARD)
        before = self.src.read_bytes()
        summary, path = self.link([pair.key, choice.key], expected_sha256=self.result.source_sha256)
        self.assertEqual(self.src.read_bytes(), before)  # the original is untouched
        rows = {r["label"]: r for r in summary.to_dict()["rows"]}
        self.assertEqual(rows["Transfers linked"]["text"], "2")

        new = read_bytes(path.read_bytes())
        top = max(t.kxfer for t in self.book.transactions)
        for p, k in ((pair, top + 1), (choice, top + 2)):
            a, b = new.transactions[p.out.index], new.transactions[p.into.index]
            self.assertTrue(a.is_internal_transfer and b.is_internal_transfer)
            self.assertEqual((a.kxfer, b.kxfer), (k, k))
            self.assertEqual((a.dst_account_key, b.dst_account_key), (b.account_key, a.account_key))
            self.assertEqual((a.amount, a.paymode, a.memo, a.date_julian), (p.out.amount, p.out.paymode, p.out.memo, p.out.date_julian))
            # The edited lines are exactly what HomeBank would write for these transactions.
            for t in (a, b):
                self.assertEqual(path.read_bytes()[t.src.start:t.src.end].decode(), S.serialise_transaction(t))

        diff = [d for d in difflib.ndiff(before.decode().splitlines(), path.read_text().splitlines()) if d[:1] in "+-"]
        self.assertEqual(len(diff), 8)  # four lines changed, nothing else

        # Linked pairs are no longer listed; what was left of the ambiguous group is now clear.
        again = find_transfers(new)
        self.assertEqual(again.choices, [])
        self.assertEqual([(p.out.memo, p.into.memo) for p in again.pairs], [("TRANSFER OUT", "DEPOSIT TRANSFER")])

    def test_skips_and_refusals(self):
        group = self.result.choices[0]
        a = next(p for p in group if p.out.memo == "PAYMENT TO CARD" and p.into.account_key == ACC_CARD)
        b = next(p for p in group if p.out.memo == "TRANSFER OUT" and p.into.account_key == ACC_CARD)  # same card side
        summary, path = self.link([a.key, b.key, (1, 2)])
        labels = [r["label"] for r in summary.to_dict()["rows"]]
        self.assertIn("Skipped: a transaction was chosen twice", labels)
        self.assertIn("Skipped: no longer a possible transfer", labels)
        self.assertIsNotNone(path)
        summary, path = self.link([])
        self.assertIsNone(path)
        with self.assertRaises(HbkitError):
            self.link([a.key], expected_sha256="0" * 64)

    def test_never_overwrites_the_input(self):
        with self.assertRaises(HbkitError):
            link_transfers(self.book, [self.result.pairs[0].key], 4, self.src, self.src)

    def test_flags_kept(self):
        summary, path = self.link([self.result.pairs[0].key])
        new = read_bytes(path.read_bytes())
        out = new.transactions[self.result.pairs[0].out.index]
        into = new.transactions[self.result.pairs[0].into.index]
        self.assertEqual(out.flags, F.OF_INTXFER)
        self.assertEqual(into.flags, F.OF_INTXFER | F.OF_INCOME)


class CommandTests(TempDirTestCase):
    def test_find_review_and_link(self):
        import csv

        from .helpers import run_cli

        src = self.write_sample("finances.xhb", unlinked_transfers=True)
        out = self.tmp / "out"
        code, stdout, err = run_cli("link-transfers", "--file", str(src), "--out", str(out))
        self.assertEqual(code, 0, err)
        folder = next((out / "transfers").iterdir())
        self.assertEqual({p.name for p in folder.iterdir()}, {"transfers.csv", "without_other_side.csv", "summary.json"})
        with open(folder / "transfers.csv", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual([r["kind"] for r in rows], ["pair", "choice", "choice", "choice", "choice"])
        rows[0]["approve"] = "y"
        next(r for r in rows if r["out_memo"] == "TRANSFER OUT" and r["in_memo"] == "DEPOSIT TRANSFER")["approve"] = "ja"
        with open(folder / "transfers.csv", "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        code, stdout, err = run_cli("link-transfers", "--file", str(src), "--out", str(out), "--review", str(folder))
        self.assertEqual(code, 0, err)
        self.assertIn("Transfers linked", stdout)
        new = read_bytes(next(out.glob("with-transfers_*.xhb")).read_bytes())
        self.assertEqual(sum(1 for t in new.transactions if t.is_internal_transfer) -
                         sum(1 for t in read_bytes(src.read_bytes()).transactions if t.is_internal_transfer), 4)

        src.write_bytes(sample(unlinked_transfers=True, seed=2).data)  # the file changed since the listing
        code, _, err = run_cli("link-transfers", "--file", str(src), "--out", str(out), "--review", str(folder))
        self.assertEqual(code, 2)
        self.assertIn("has changed since the transfers were listed", err)

    def test_privacy(self):
        from .helpers import run_cli

        src = self.write_sample("s.xhb", unlinked_transfers=True, sentinel=True)
        code, stdout, err = run_cli("link-transfers", "--file", str(src), "--out", str(self.tmp / "out"))
        self.assertEqual(code, 0, err)
        self.assertNoSentinel(stdout, err)

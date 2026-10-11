import unittest

from hbkit.rules.mining import literal_rule_text, rule_text_options
from hbkit.rules.normalise import explain, normalise


class NormaliseTests(unittest.TestCase):
    CASES = {
        # card numbers and dates
        "POS PURCHASE WOOLWORTHS SANDTON 458712******1234 12 MAR": "POS PURCHASE WOOLWORTHS SANDTON",
        "pos purchase woolworths sandton 4587*8842 03 apr": "POS PURCHASE WOOLWORTHS SANDTON",
        "CARD PURCHASE 458712XXXXXX0071 CHECKERS HYPER FOURWAYS 2020-02-29": "CARD PURCHASE CHECKERS HYPER FOURWAYS",
        "PNP CRESTA xxxx1234 12/03/2024": "PNP CRESTA",
        "SPAR 1234-5678-9012-3456 1 JANUARY 2024": "SPAR",
        "SPAR 12MAR24 14:32": "SPAR",
        "SPAR MAR 12": "SPAR",
        # references, accounts, branch codes
        "DEBIT ORDER VODACOM 0012345678 REF 8273645": "DEBIT ORDER VODACOM",
        "DEBIT ORDER AFRIHOST REF:AB12C34": "DEBIT ORDER AFRIHOST",
        "FNB APP PAYMENT TO SUNNYDALE PRIMARY REF C/ID 1234": "FNB APP PAYMENT TO SUNNYDALE PRIMARY",
        "MAGTAPE CREDIT ACME CORP SALARY RRN12345678": "MAGTAPE CREDIT ACME CORP SALARY",
        "ATM WITHDRAWAL 250655 SANDTON CITY": "ATM WITHDRAWAL SANDTON CITY",
        "DEPOSIT BRANCH 250655 CASH": "DEPOSIT CASH",
        "PREPAID ELECTRICITY 12345678901 RRN123456789": "PREPAID ELECTRICITY",
        # amounts, punctuation, country codes
        "7-ELEVEN BRAAMFONTEIN R123.45 ZA": "7-ELEVEN BRAAMFONTEIN",
        "#MONTHLY ACCOUNT FEE": "MONTHLY ACCOUNT FEE",
        "NETFLIX.COM LOS GATOS 4587*1234 03 MAR": "NETFLIX.COM LOS GATOS",
        "TAKEALOT.COM 783456123": "TAKEALOT.COM",
        # things that must survive
        "12 MARKET STREET BAKERY": "MARKET STREET BAKERY",
        "PAYMENT TO J SMITH REF RENT": "PAYMENT TO J SMITH REF RENT",
        "DIS-CHEM MALL OF AFRICA": "DIS-CHEM MALL OF AFRICA",
        "Café Société": "CAFÉ SOCIÉTÉ",
    }

    def test_cases(self):
        for raw, expected in self.CASES.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalise(raw), expected)

    def test_same_merchant_same_pattern(self):
        variants = [
            "POS PURCHASE WOOLWORTHS SANDTON 458712******1234 12 MAR",
            "POS PURCHASE  WOOLWORTHS SANDTON 4587*8842 03 APR",
            "POS PURCHASE WOOLWORTHS SANDTON xxxx0071 28 DEC",
        ]
        self.assertEqual(len({normalise(v) for v in variants}), 1)

    def test_explain(self):
        steps = dict(explain("SPAR 4587*1234 12 MAR"))
        self.assertIn("masked card numbers", steps)
        self.assertIn("day-month dates", steps)


class RuleTextTests(unittest.TestCase):
    def test_longest_run_present_everywhere(self):
        raws = ["POS PURCHASE WOOLWORTHS SANDTON 4587*1234 12 MAR",
                "POS PURCHASE  WOOLWORTHS SANDTON 4587*8842 03 APR"]
        self.assertEqual(literal_rule_text("POS PURCHASE WOOLWORTHS SANDTON", raws), "WOOLWORTHS SANDTON")

    def test_generic_words_are_not_rule_edges(self):
        raws = ["DEBIT ORDER VODACOM 123", "DEBIT  ORDER VODACOM 456"]
        self.assertEqual(literal_rule_text("DEBIT ORDER VODACOM", raws), "VODACOM")
        self.assertIsNone(literal_rule_text("POS PURCHASE", ["POS PURCHASE 1"]))

    def test_partial_coverage_options(self):
        raws = ["PNP CRESTA 1"] * 9 + ["PNP  CRESTA 2"]
        options = rule_text_options("PNP CRESTA", raws)
        self.assertEqual(options[0], ("CRESTA", 1.0))
        self.assertEqual(options[1], ("PNP CRESTA", 0.9))

    def test_minimum_length(self):
        self.assertIsNone(literal_rule_text("BP", ["BP GARAGE"], min_len=4))


if __name__ == "__main__":
    unittest.main()

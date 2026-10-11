"""Generate realistic but entirely fictional HomeBank files for testing.

The output is written element by element with hbkit.xhb.serialise, i.e. exactly
the way HomeBank 5.10.3 writes its files. Content:

* five accounts (cheque, credit card, savings, cash, one closed), one currency (ZAR);
* two-level categories, payees, tags, two templates and a saved filter;
* several years of transactions with messy South African bank-style
  descriptions (masked card numbers, dates, references, branch codes);
* split transactions, paired internal transfers, void and uncategorised items;
* merchants with known ("ground truth") categories, a few deliberately
  inconsistent ones, and one whose category changes over time;
* existing assignment rules including deliberately bad ones (shadowed, dead,
  broken target, weak regex, invalid regex, payee-field, overwrite).

Options cover older file versions, Windows line endings, unknown attributes and
elements, control characters, and a "sentinel" mode that marks every piece of
free text so tests can prove none of it reaches the terminal.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from ..xhb import format as F
from ..xhb import serialise as S
from ..xhb.model import (
    Account, Category, Currency, Filter, Group, Payee, Properties, Rule, Split, Tag, Template,
    Transaction,
)

#: Marker placed in every piece of free text when ``sentinel=True``.
SENTINEL = "ZQXSENT"

MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


@dataclass
class GeneratorOptions:
    seed: int = 1
    start_year: int = 2020
    years: int = 6
    #: The last year stops after this month, like a file still in use.
    end_month: int = 9
    newline: str = "\n"
    file_version: str = "1.6"
    data_version: int = F.KNOWN_DATA_VERSION
    sentinel: bool = False
    unknown_extras: bool = False
    control_chars: bool = False
    #: Multiply the number of everyday purchases (1.0 gives roughly 4,000 transactions).
    scale: float = 1.0


@dataclass
class GeneratedFile:
    data: bytes
    #: Merchant code -> category path it truly belongs to (dominant category).
    truth: dict[str, str]
    #: Merchant codes deliberately categorised inconsistently.
    inconsistent: set[str]
    options: GeneratorOptions
    transactions: int = 0


# --- static content ------------------------------------------------------------------------

# key, parent, name, income?
CATEGORIES = [
    (1, 0, "Groceries", False),
    (2, 0, "Household", False),
    (3, 2, "Cleaning", False),
    (4, 2, "Furniture & appliances", False),
    (5, 0, "Transport", False),
    (6, 5, "Fuel", False),
    (7, 5, "Car insurance", False),
    (8, 5, "Ride hailing", False),
    (9, 0, "Eating out", False),
    (10, 9, "Restaurants", False),
    (11, 9, "Takeaways", False),
    (12, 0, "Utilities", False),
    (13, 12, "Electricity", False),
    (14, 12, "Water & rates", False),
    (15, 0, "Communication", False),
    (16, 15, "Cellphone", False),
    (17, 15, "Internet", False),
    (18, 0, "Insurance", False),
    (19, 18, "Life", False),
    (20, 0, "Medical", False),
    (21, 20, "Medical aid", False),
    (22, 20, "Pharmacy", False),
    (23, 0, "Entertainment", False),
    (24, 23, "Streaming", False),
    (25, 0, "Bank charges", False),
    (26, 0, "Salary", True),
    (27, 0, "Interest", True),
    (28, 0, "Gifts", False),
    (29, 0, "Clothing", False),
    (30, 0, "Education", False),
    (31, 30, "School fees", False),
    (32, 0, "Personal care", False),
    (33, 0, "Sport & fitness", False),
    (34, 0, "Cash withdrawals", False),
]

PAYEES = [
    (1, "Acme Corp"), (2, "Woolworths"), (3, "Checkers"), (4, "Pick n Pay"), (5, "Engen"),
    (6, "Shell"), (7, "Netflix"), (8, "Vodacom"), (9, "Afrihost"), (10, "Discovery"),
    (11, "City of Johannesburg"), (12, "Clicks"), (13, "Dis-Chem"), (14, "Takealot"),
    (15, "Uber"), (16, "Spur"), (17, "Mr Price"), (18, "First National Bank"),
    (19, "Sunnydale Primary"), (20, "Virgin Active"), (21, "OUTsurance"), (22, "Eskom prepaid"),
]

ACC_CHEQUE, ACC_CARD, ACC_SAVINGS, ACC_CASH, ACC_OLD = 1, 2, 3, 4, 5


@dataclass
class Merchant:
    code: str
    category: int
    templates: list[str]
    amount: tuple[float, float]
    per_month: float  # average events per month (scaled)
    payee: int = 0
    branches: list[str] = field(default_factory=list)
    branch_weights: list[int] | None = None
    accounts: tuple[tuple[int, int, float], ...] = ((ACC_CHEQUE, 6, 1.0),)  # account, paymode, weight
    alternatives: tuple[tuple[int, float], ...] = ()  # (category, probability)
    switch: tuple[int, int] | None = None  # (from year offset, new category)
    fixed_day: int | None = None  # monthly on this day instead of random
    income: bool = False
    split_share: float = 0.0
    uncategorised: float = 0.0
    scaled: bool = True


MERCHANTS = [
    Merchant("WOOLWORTHS", 1, ["POS PURCHASE WOOLWORTHS {branch} {card} {ddmon}"], (150, 1800), 4,
             payee=2, branches=["SANDTON", "ROSEBANK", "MENLYN", "CAVENDISH"], branch_weights=[8, 5, 1, 1],
             accounts=((ACC_CHEQUE, 6, 0.7), (ACC_CARD, 1, 0.3))),
    Merchant("CHECKERS", 1, ["CARD PURCHASE {card} CHECKERS HYPER {branch} {iso}"], (300, 2500), 2,
             payee=3, branches=["FOURWAYS", "GREENSTONE"], split_share=0.3),
    Merchant("PNP", 1, ["PNP {branch} {card} {dmy}"], (80, 900), 1.5, payee=4, branches=["CRESTA", "EASTGATE"]),
    Merchant("ENGEN", 6, ["POS PURCHASE ENGEN {branch} {card} {ddmon}"], (400, 1100), 2, payee=5,
             branches=["N1 CITY", "GRAYSTON"], accounts=((ACC_CARD, 1, 1.0),)),
    Merchant("SHELL", 6, ["SHELL ULTRA CITY {branch} {card}"], (400, 1100), 1, payee=6,
             branches=["KRUGERSDORP", "HEIDELBERG"], accounts=((ACC_CARD, 1, 1.0),)),
    Merchant("NETFLIX", 24, ["NETFLIX.COM LOS GATOS {card} {ddmon}"], (199, 199), 1, payee=7, fixed_day=3,
             accounts=((ACC_CARD, 1, 1.0),), scaled=False),
    Merchant("VODACOM", 16, ["DEBIT ORDER VODACOM {acct} {ref}"], (499, 499), 1, payee=8, fixed_day=1,
             accounts=((ACC_CHEQUE, 11, 1.0),), scaled=False),
    Merchant("AFRIHOST", 17, ["DEBIT ORDER AFRIHOST {ref}"], (599, 599), 1, payee=9, fixed_day=1,
             accounts=((ACC_CHEQUE, 11, 1.0),), scaled=False),
    Merchant("DISCOVERY HEALTH", 21, ["DEBIT ORDER DISCOVERY HEALTH {acct}"], (4200, 4200), 1, payee=10,
             fixed_day=1, accounts=((ACC_CHEQUE, 11, 1.0),), scaled=False),
    Merchant("DISCOVERY LIFE", 19, ["DEBIT ORDER DISCOVERY LIFE {acct}"], (650, 650), 1, payee=10,
             fixed_day=2, accounts=((ACC_CHEQUE, 11, 1.0),), scaled=False),
    Merchant("CITY OF JOBURG", 14, ["IB PAYMENT TO CITY OF JOBURG REF {acct}"], (1500, 3200), 1, payee=11,
             fixed_day=7, accounts=((ACC_CHEQUE, 8, 1.0),), scaled=False),
    Merchant("PREPAID ELECTRICITY", 13, ["PREPAID ELECTRICITY {meter} {ref}"], (300, 1000), 2, payee=22),
    Merchant("CLICKS", 22, ["POS PURCHASE CLICKS {branch} {card} {ddmon}"], (60, 700), 1.5, payee=12,
             branches=["SANDTON", "BEDFORDVIEW"], alternatives=((32, 0.35),)),
    Merchant("DIS-CHEM", 22, ["POS PURCHASE DIS-CHEM {branch} {card} {ddmon}"], (80, 900), 1, payee=13,
             branches=["MALL OF AFRICA"]),
    Merchant("TAKEALOT", 4, ["TAKEALOT.COM {ref} {card}"], (250, 4000), 1, payee=14,
             alternatives=((28, 0.45),), accounts=((ACC_CARD, 1, 1.0),)),
    Merchant("UBER EATS", 11, ["UBER EATS HELP.UBER.COM {card} {ddmon}"], (90, 350), 2, payee=15,
             accounts=((ACC_CARD, 1, 1.0),)),
    Merchant("UBER TRIP", 8, ["UBER TRIP HELP.UBER.COM {card} {ddmon}"], (60, 300), 2, payee=15,
             accounts=((ACC_CARD, 1, 1.0),)),
    Merchant("SPUR", 10, ["POS PURCHASE SPUR {branch} {card} {ddmon}"], (250, 900), 1, payee=16,
             branches=["MONTECASINO", "CLEARWATER"]),
    Merchant("MR PRICE", 29, ["POS PURCHASE MRP {branch} {card} {ddmon}"], (150, 1200), 0.7, payee=17,
             branches=["CRESTA"]),
    Merchant("VIRGIN ACTIVE", 32, ["DEBIT ORDER VIRGIN ACTIVE {acct}"], (650, 650), 1, payee=20, fixed_day=1,
             accounts=((ACC_CHEQUE, 11, 1.0),), switch=(3, 33), scaled=False),
    Merchant("OUTSURANCE", 7, ["DEBIT ORDER OUTSURANCE {acct}"], (980, 980), 1, payee=21, fixed_day=1,
             accounts=((ACC_CHEQUE, 11, 1.0),), scaled=False),
    Merchant("SUNNYDALE", 31, ["FNB APP PAYMENT TO SUNNYDALE PRIMARY REF {ref}"], (3100, 3100), 1, payee=19,
             fixed_day=5, accounts=((ACC_CHEQUE, 8, 1.0),), scaled=False),
    Merchant("ACCOUNT FEE", 25, ["#MONTHLY ACCOUNT FEE"], (69, 69), 1, payee=18, fixed_day=28,
             accounts=((ACC_CHEQUE, 10, 1.0),), scaled=False),
    Merchant("SERVICE FEE", 25, ["#SERVICE FEE {ref}"], (5, 25), 2, payee=18, accounts=((ACC_CHEQUE, 10, 1.0),)),
    Merchant("SALARY", 26, ["MAGTAPE CREDIT ACME CORP SALARY {ref}"], (35000, 35000), 1, payee=1,
             fixed_day=25, income=True, accounts=((ACC_CHEQUE, 9, 1.0),), scaled=False),
    Merchant("INTEREST", 27, ["CREDIT INTEREST"], (40, 160), 1, fixed_day=28, income=True,
             accounts=((ACC_SAVINGS, 9, 1.0),), scaled=False),
    Merchant("ATM", 34, ["ATM WITHDRAWAL {code6} {branch}"], (200, 2000), 1,
             branches=["SANDTON CITY", "ROSEBANK MALL"], uncategorised=0.2),
]

#: Small shops seen once or twice — noise that must not become rules.
ONE_OFF_SHOPS = [
    "BOOKWORM CORNER", "THE GOOD COFFEE CO", "MAMAS KITCHEN", "GREEN THUMB NURSERY",
    "PIXEL PHOTO LAB", "SOLE MATES SHOES", "RAINBOW TOYS", "SHARP KNIVES CO", "PETALS FLORIST",
    "VELO CYCLE HUB", "OCEAN BASKET", "CORNER CAFE", "PLANT PARADISE", "LUCKY LAUNDRY",
]
ONE_OFF_CATEGORIES = [28, 29, 10, 2, 32, 23]


class _Gen:
    def __init__(self, o: GeneratorOptions):
        self.o = o
        self.rng = random.Random(o.seed)
        self.txns: list[Transaction] = []
        self.kxfer = 0

    # --- text helpers -----------------------------------------------------------------

    def t(self, text: str, tag: str = "") -> str:
        """Free text, marked in sentinel mode."""
        if not self.o.sentinel or not text:
            return text
        return f"{SENTINEL}{tag}_{text}"

    def card(self) -> str:
        r = self.rng
        last4 = r.choice(["1234", "8842", "0071"])
        return r.choice([
            f"458712******{last4}", f"4587*{last4}", f"458712XXXXXX{last4}", f"xxxx{last4}",
        ])

    def ref(self) -> str:
        r = self.rng
        digits = "".join(r.choice("0123456789") for _ in range(r.randint(6, 9)))
        code = "".join(r.choice("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789") for _ in range(7))
        return r.choice([f"REF {digits}", f"REF:{code}", digits, f"C/ID {digits[:4]}", f"RRN{digits}"])

    def describe(self, m: Merchant, d: date, branch: str) -> str:
        r = self.rng
        values = {
            "branch": branch,
            "card": self.card(),
            "ddmon": f"{d.day:02d} {MONTHS[d.month - 1]}",
            "iso": d.isoformat(),
            "dmy": f"{d.day:02d}/{d.month:02d}/{d.year}",
            "ref": self.ref(),
            "acct": f"00{r.randint(10_000_000, 99_999_999)}",
            "meter": f"{r.randint(10**10, 10**11 - 1)}",
            "code6": f"{r.randint(100_000, 999_999)}",
        }
        text = r.choice(m.templates).format(**values)
        if r.random() < 0.05:  # banks are not consistent about spacing
            text = text.replace(" ", "  ", 1)
        if self.o.control_chars and r.random() < 0.01:
            text += "\x01"
        return text

    # --- transactions -----------------------------------------------------------------

    def months(self):
        o = self.o
        for y in range(o.start_year, o.start_year + o.years):
            last = o.end_month if y == o.start_year + o.years - 1 else 12
            for mth in range(1, last + 1):
                yield y, mth

    def day_in(self, y: int, mth: int) -> date:
        first = date(y, mth, 1)
        nxt = date(y + (mth == 12), mth % 12 + 1, 1)
        return first + timedelta(days=self.rng.randrange((nxt - first).days))

    def status_for(self, d: date, end: date) -> int:
        if (end - d).days < 60:
            return F.TXN_STATUS_NONE
        return F.TXN_STATUS_RECONCILED if self.rng.random() < 0.7 else F.TXN_STATUS_CLEARED

    def add(self, **kw) -> Transaction:
        t = Transaction(**kw)
        if t.amount > 0:
            t.flags |= F.OF_INCOME
        self.txns.append(t)
        return t

    def merchant_events(self, end: date) -> None:
        r = self.rng
        for m in MERCHANTS:
            for y, mth in self.months():
                if m.fixed_day:
                    days = [date(y, mth, min(m.fixed_day, 28))]
                    if m.code == "SUNNYDALE" and mth == 12:
                        continue
                else:
                    rate = m.per_month * (self.o.scale if m.scaled else 1.0)
                    count = int(rate) + (1 if r.random() < rate - int(rate) else 0)
                    days = sorted(self.day_in(y, mth) for _ in range(count))
                for d in days:
                    if d > end:
                        continue
                    self.one_event(m, d, end)

    def one_event(self, m: Merchant, d: date, end: date) -> None:
        r = self.rng
        branch = ""
        if m.branches:
            branch = r.choices(m.branches, weights=m.branch_weights or [1] * len(m.branches))[0]
        acc, paymode, _ = r.choices(m.accounts, weights=[w for *_, w in m.accounts])[0]
        lo, hi = m.amount
        amount = round(r.uniform(lo, hi), 2) if hi > lo else lo
        if m.code == "NETFLIX" and d.year >= self.o.start_year + 3:
            amount = 229.0
        amount = amount if m.income else -amount

        category = m.category
        if m.switch and d.year >= self.o.start_year + m.switch[0]:
            category = m.switch[1]
        roll = r.random()
        for alt, prob in m.alternatives:
            if roll < prob:
                category = alt
                break
            roll -= prob
        if m.uncategorised and r.random() < m.uncategorised:
            category = 0
        if (end - d).days < 45 and r.random() < 0.25:  # recent imports not yet categorised
            category = 0

        splits: list[Split] = []
        flags = 0
        if m.split_share and r.random() < m.split_share:
            household = round(amount * 0.25, 2)
            splits = [
                Split(category_key=1, amount=round(amount - household, 2), memo=""),
                Split(category_key=3, amount=household, memo=self.t("cleaning", "SPLIT")),
            ]
            category = 0
            flags |= F.OF_SPLIT

        tags = []
        if m.category in (10, 11) and d.month == 12 and r.random() < 0.5:
            tags = [self.t("holiday", "TAG")]
        status = self.status_for(d, end)
        if r.random() < 0.003:
            status = F.TXN_STATUS_VOID
        self.add(
            date_julian=d.toordinal(), amount=amount, account_key=acc, paymode=paymode,
            status=status, flags=flags, payee_key=m.payee if r.random() < 0.5 else 0,
            category_key=category, memo=self.t(self.describe(m, d, branch), "MEMO"),
            tags=tags, splits=splits,
        )

    def one_offs(self, end: date) -> None:
        r = self.rng
        for shop in ONE_OFF_SHOPS:
            for _ in range(r.randint(1, 3)):
                y, mth = r.choice(list(self.months()))
                d = self.day_in(y, mth)
                if d > end:
                    continue
                category = r.choice(ONE_OFF_CATEGORIES) if r.random() > 0.3 else 0
                self.add(
                    date_julian=d.toordinal(), amount=-round(r.uniform(50, 900), 2),
                    account_key=ACC_CHEQUE, paymode=6, status=self.status_for(d, end),
                    category_key=category,
                    memo=self.t(f"POS PURCHASE {shop} {self.card()} {d.day:02d} {MONTHS[d.month - 1]}", "MEMO"),
                )

    def transfer(self, d: date, src: int, dst: int, amount: float, text: str, end: date) -> None:
        self.kxfer += 1
        status = self.status_for(d, end)
        for acc, other, amt in ((src, dst, -amount), (dst, src, amount)):
            self.add(
                date_julian=d.toordinal(), amount=amt, account_key=acc, dst_account_key=other,
                paymode=4, status=status, flags=F.OF_INTXFER, memo=self.t(text, "MEMO"),
                kxfer=self.kxfer,
            )

    def transfers(self, end: date) -> None:
        for y, mth in self.months():
            d = date(y, mth, 26)
            if d > end:
                continue
            self.transfer(d, ACC_CHEQUE, ACC_SAVINGS, 2000.0, "TRANSFER TO SAVINGS", end)
            self.transfer(date(y, mth, 27), ACC_CHEQUE, ACC_CARD,
                          round(self.rng.uniform(3000, 9000), 2), "CREDIT CARD PAYMENT", end)
            if self.rng.random() < 0.3:
                self.transfer(self.day_in(y, mth), ACC_CHEQUE, ACC_CASH, 500.0, "CASH FOR WALLET", end)

    # --- static elements ------------------------------------------------------------------

    def static_elements(self, end: date) -> list[str]:
        t = self.t
        lines = [S.serialise_properties(Properties(
            title=t("Synthetic household", "TITLE"), currency_key=1, auto_smode=1, auto_weekday=1,
        ))]
        lines.append(S.serialise_currency(Currency(
            key=1, flags=0, iso="ZAR", name="South African rand", symbol="R", symbol_prefix=1,
            decimal_char=".", grouping_char=" ", frac_digits=2,
        )))
        lines.append(S.serialise_group(Group(key=1, name=t("Banking", "GROUP"))))
        accounts = [
            Account(key=1, pos=1, type=6, currency_key=1, name=t("Cheque account", "ACC"),
                    number=t("62000000001", "ACCNUM"), bank_name=t("First National Bank", "BANK"),
                    initial=15000.0, minimum=-5000.0, group_key=1, reconciled_julian=end.toordinal() - 60),
            Account(key=2, pos=2, type=4, currency_key=1, name=t("Credit card", "ACC"), group_key=1, cc_day=25),
            Account(key=3, pos=3, type=7, currency_key=1, name=t("Savings", "ACC"), initial=20000.0, group_key=1),
            Account(key=4, pos=4, type=2, currency_key=1, name=t("Cash", "ACC")),
            Account(key=5, flags=F.AF_CLOSED, pos=5, type=6, currency_key=1, name=t("Old cheque account", "ACC"),
                    notes=t("Closed when we moved banks.\nKept for history.", "NOTES"),
                    website=t("https://bank.example", "WEB")),
        ]
        if self.o.unknown_extras:
            accounts[0].extra["futureattr"] = "1"
        lines += [S.serialise_account(a) for a in accounts]
        lines += [S.serialise_payee(Payee(key=k, name=t(n, "PAYEE"))) for k, n in PAYEES]
        for key, parent, name, income in CATEGORIES:
            flags = (F.GF_SUB if parent else 0) | (F.GF_INCOME if income else 0)
            budget = {}
            if key == 1:
                budget = {0: -4500.0}
                flags |= F.GF_BUDGET
            lines.append(S.serialise_category(Category(
                key=key, parent_key=parent, flags=flags, name=t(name, "CAT"), budget=budget,
            )))
        lines += [S.serialise_tag(Tag(key=k, name=t(n, "TAG"))) for k, n in
                  ((1, "holiday"), (2, "business"), (3, "tax"))]
        lines += [S.serialise_rule(r) for r in self.rules()]
        lines += [S.serialise_template(x) for x in self.templates(end)]
        return lines

    def rules(self) -> list[Rule]:
        t = self.t
        doc = F.ASGF_DOCAT
        return [
            Rule(key=1, pos=1, flags=doc | F.ASGF_DOPAY, search=t("NETFLIX", "RULE"), payee_key=7, category_key=24),
            Rule(key=2, pos=2, flags=doc, search=t("UBER", "RULE"), category_key=11),  # too broad
            Rule(key=3, pos=3, flags=doc, search=t("UBER TRIP", "RULE"), category_key=8),  # shadowed by 2
            Rule(key=4, pos=4, flags=doc, search=t("VODACOM", "RULE"), category_key=16,
                 notes=t("Cellphone contract", "RULENOTE")),
            Rule(key=5, pos=5, flags=doc, search=t("OLD VIDEO STORE", "RULE"), category_key=23),  # dead
            Rule(key=6, pos=6, flags=doc | F.ASGF_REGEX, search=t("^DEBIT ORDER DISCOVERY", "RULE"),
                 category_key=21),  # also catches Discovery Life: weak
            Rule(key=7, pos=7, flags=doc, search=t("AFRIHOST", "RULE"), category_key=999),  # broken target
            Rule(key=8, pos=8, flags=doc, match_field=F.RULE_FIELD_PAYEE, search=t("Engen", "RULE"),
                 category_key=6),
            Rule(key=9, pos=9, flags=doc | F.ASGF_OVWCAT, search=t("SALARY", "RULE"), category_key=26),
            Rule(key=10, pos=10, flags=doc | F.ASGF_REGEX, search=t("[unclosed", "RULE"), category_key=28),
        ]

    def templates(self, end: date) -> list[Template]:
        nxt = date(end.year + (end.month == 12), end.month % 12 + 1, 25)
        return [
            Template(key=1, amount=35000.0, account_key=1, paymode=9, flags=F.OF_INCOME, payee_key=1,
                     category_key=26, memo=self.t("Salary", "TPL"), rec_flags=1,
                     next_julian=nxt.toordinal(), every=1, unit=2),
            Template(key=2, amount=-499.0, account_key=1, paymode=11, payee_key=8, category_key=16,
                     memo=self.t("Vodacom contract", "TPL"), rec_flags=3, limit=12,
                     next_julian=nxt.toordinal(), every=1, unit=2),
        ]

    # --- assemble ------------------------------------------------------------------------

    def build(self) -> GeneratedFile:
        o = self.o
        last_year = o.start_year + o.years - 1
        end = date(last_year + (o.end_month == 12), o.end_month % 12 + 1, 1) - timedelta(days=1)
        static = self.static_elements(end)
        self.merchant_events(end)
        self.one_offs(end)
        self.transfers(end)

        # HomeBank writes transactions account by account, each sorted by date.
        ordered = sorted(enumerate(self.txns), key=lambda it: (it[1].account_key, it[1].date_julian, it[0]))
        ope_lines = []
        for i, (_, txn) in enumerate(ordered):
            if self.o.unknown_extras and i % 500 == 7:
                txn.extra["futureflag"] = "1"
            if o.data_version < F.STATUS_CHANGE_DATA_VERSION and txn.status == F.TXN_STATUS_VOID:
                txn.status = F.TXN_OLDSTATUS_VOID  # how HomeBank before 5.9 wrote "void"
            ope_lines.append(S.serialise_transaction(txn))

        lines = ['<?xml version="1.0"?>', S.serialise_root(o.file_version, o.data_version)]
        lines += static + ope_lines
        lines.append(S.serialise_filter(Filter(
            key=1, name=self.t("Groceries this year", "FILTER"), groups={"dat": "1|6", "cat": "1|0"},
        )))
        if self.o.unknown_extras:
            lines.append('<zzfuture key="1" note="from a future version"/>')
        lines.append("</homebank>")
        text = o.newline.join(lines) + o.newline
        truth = {m.code: _path(m.switch[1] if m.switch else m.category) for m in MERCHANTS}
        inconsistent = {m.code for m in MERCHANTS if m.alternatives}
        return GeneratedFile(
            data=text.encode("utf-8"), truth=truth, inconsistent=inconsistent, options=o,
            transactions=len(ope_lines),
        )


def _path(key: int) -> str:
    names = {k: (p, n) for k, p, n, _ in CATEGORIES}
    parent, name = names[key]
    return f"{names[parent][1]}:{name}" if parent else name


def generate(options: GeneratorOptions | None = None) -> GeneratedFile:
    """Build a synthetic HomeBank file in memory."""
    return _Gen(options or GeneratorOptions()).build()


def write_sample(path: str | Path, options: GeneratorOptions | None = None) -> GeneratedFile:
    gen = generate(options)
    Path(path).write_bytes(gen.data)
    return gen

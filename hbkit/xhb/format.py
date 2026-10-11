"""Constants describing the HomeBank .xhb format.

Everything here was taken from the HomeBank 5.10.3 source code (src/hb-xml.c,
src/hb-transaction.h, src/hb-assign.h, src/hb-account.h, src/hb-category.h,
src/enums.h, src/homebank.h). See docs/xhb-format.md for the narrative.
"""

from __future__ import annotations

# --- versions -----------------------------------------------------------------

#: The newest structure version the reader was written against (homebank.h FILE_VERSION).
KNOWN_FILE_VERSION = 1.6
#: The HomeBank release the reader was checked against (HB_VERSION_NUM for 5.10.3).
KNOWN_DATA_VERSION = 51003

# --- element order --------------------------------------------------------------

#: Order in which homebank_save_xml() writes the top-level elements.
SAVE_ORDER: tuple[str, ...] = (
    "properties", "cur", "grp", "account", "pay", "cat", "tag", "asg", "fav", "ope", "flt",
)

# --- attributes each element is read with (homebank_load_xml_* functions) --------

KNOWN_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    "homebank": ("v", "d"),
    "properties": (
        "title", "curr", "car_category", "auto_smode", "auto_weekday",
        "auto_nbmonths", "auto_nbdays", "earnbyh",
    ),
    "cur": ("key", "flags", "iso", "name", "symb", "syprf", "dchar", "gchar", "frac", "rate", "mdate"),
    "grp": ("key", "name"),
    "account": (
        "key", "flags", "pos", "type", "curr", "name", "number", "bankname", "initial",
        "minimum", "maximum", "cheque1", "cheque2", "website", "notes", "tpl", "grp",
        "ccday", "rdate",
    ),
    "pay": ("key", "flags", "name", "category", "paymode", "notes"),
    "cat": ("key", "parent", "flags", "name") + tuple(f"b{i}" for i in range(13)),
    "tag": ("key", "name"),
    "asg": (
        "key", "flags", "pos", "field", "name", "notes", "payee", "category", "paymode",
        "amount", "tags", "exact",
    ),
    "fav": (
        "key", "amount", "account", "damt", "dst_account", "paymode", "grpflg", "st", "flags",
        "payee", "category", "wording", "info", "tags", "recflg", "nextdate", "every", "unit",
        "limit", "weekend", "gap", "ordn", "wkdy", "scat", "samt", "smem",
    ),
    "ope": (
        "date", "amount", "account", "damt", "dst_account", "paymode", "grpflg", "st", "flags",
        "payee", "category", "wording", "info", "tags", "kxfer", "scat", "samt", "smem",
    ),
    "flt": ("key", "name", "dat", "acc", "pay", "cat", "tag", "txt", "amt", "mod", "sta", "typ"),
}

# --- transaction flags (hb-transaction.h) ---------------------------------------

OF_INCOME = 1 << 1
OF_INTXFER = 1 << 3
OF_ADVXFER = 1 << 4
OF_REMIND = 1 << 5
OF_CHEQ2 = 1 << 6
OF_SPLIT = 1 << 8
OF_ISIMPORT = 1 << 9
OF_ISPAST = 1 << 10

#: Payment mode 5 was the internal-transfer marker before HomeBank 5.x.
OLDPAYMODE_INTXFER = 5

# Transaction status. From data version 50900 (HomeBank 5.9) "void" is 3; before
# that 3 meant "remind" and 4 meant "void" (see homebank_upgrade_to_v14_59).
TXN_STATUS_NONE = 0
TXN_STATUS_CLEARED = 1
TXN_STATUS_RECONCILED = 2
TXN_STATUS_VOID = 3
TXN_OLDSTATUS_REMIND = 3
TXN_OLDSTATUS_VOID = 4
STATUS_CHANGE_DATA_VERSION = 50900

STATUS_NAMES = {0: "none", 1: "cleared", 2: "reconciled", 3: "void"}

# --- payment modes (enums.h) -----------------------------------------------------

PAYMODE_NAMES = {
    0: "none",
    1: "credit card",
    2: "cheque",
    3: "cash",
    4: "bank transfer",
    5: "internal transfer (obsolete)",
    6: "debit card",
    7: "standing order",
    8: "electronic payment",
    9: "deposit",
    10: "financial institution fee",
    11: "direct debit",
    12: "mobile phone",
}

# --- accounts (hb-account.h) -----------------------------------------------------

AF_CLOSED = 1 << 1
AF_NOSUMMARY = 1 << 4
AF_NOBUDGET = 1 << 5
AF_NOREPORT = 1 << 6

ACCOUNT_TYPE_NAMES = {
    0: "none",
    1: "bank",
    2: "cash",
    3: "asset",
    4: "credit card",
    5: "liability",
    6: "checking",
    7: "savings",
}

# --- categories (hb-category.h) --------------------------------------------------

GF_SUB = 1 << 0
GF_INCOME = 1 << 1
GF_CUSTOM = 1 << 2
GF_BUDGET = 1 << 3
GF_FORCED = 1 << 4
GF_MIXED = 1 << 5
GF_HIDDEN = 1 << 8

#: da_cat_build_fullname() joins parent and child names with a colon.
CATEGORY_SEPARATOR = ":"

# --- assignment rules (hb-assign.h) ------------------------------------------------

ASGF_EXACT = 1 << 0  # case-sensitive match
ASGF_DOPAY = 1 << 1  # set payee if empty
ASGF_DOCAT = 1 << 2  # set category if empty
ASGF_DOMOD = 1 << 3  # set payment mode if empty
ASGF_DOTAG = 1 << 4  # set tags if empty
ASGF_PREFILLED = 1 << 5  # session-only, cleared on save
ASGF_AMOUNT = 1 << 7  # also require an exact amount
ASGF_REGEX = 1 << 8  # search text is a regular expression
ASGF_OVWPAY = 1 << 9  # overwrite payee
ASGF_OVWCAT = 1 << 10  # overwrite category
ASGF_OVWMOD = 1 << 11  # overwrite payment mode
ASGF_OVWTAG = 1 << 12  # overwrite tags

RULE_FIELD_MEMO = 0
RULE_FIELD_PAYEE = 1
RULE_FIELD_NAMES = {0: "memo", 1: "payee"}

# --- dates (homebank.h) -------------------------------------------------------------

#: GLib "Julian" day numbers count days from 0001-01-01 = 1, the same as
#: Python's date.toordinal(). HomeBank's own limits:
HB_MINDATE = 693596  # 1900-01-01
HB_MAXDATE = 803533  # 2200-12-31

# The HomeBank `.xhb` format

Everything here was learnt from the HomeBank **5.10.3** source code
(`homebank-5.10.3.tar.gz` from https://www.gethomebank.org; the same code is on
Launchpad) — mainly `src/hb-xml.c` (`homebank_load_xml*` and `homebank_save_xml*`)
and the headers it uses — and checked against HomeBank's own `example.xhb`. It was
**not** learnt from any real user file.

## Layout

HomeBank writes a flat XML file: no indentation, one element per line, `\n` line
endings (files edited elsewhere may have `\r\n`; the toolkit keeps whatever the
file uses).

```xml
<?xml version="1.0"?>
<homebank v="1.6" d="051003">
<properties title="..." curr="1" auto_smode="1" earnbyh="0"/>
<cur key="1" flags="0" iso="ZAR" name="..." symb="R" syprf="1" dchar="." gchar=" " frac="2" rate="0" mdate="0"/>
<grp .../>      account groups
<account .../>
<pay .../>      payees
<cat .../>      categories
<tag .../>
<asg .../>      assignment rules
<fav .../>      templates and scheduled transactions
<ope .../>      transactions
<flt .../>      saved filters
</homebank>
```

The order above is the order `homebank_save_xml()` writes. Transactions are written
account by account, each account's transactions sorted by date.

* `v` — structure version (`FILE_VERSION`, 1.6 in HomeBank 5.10). Read as a number;
  `0` means 0.1 and `5.0` means 1.0 (a historical mistake HomeBank corrects).
* `d` — data version, the HomeBank release as `MMmmuu` (051003 = 5.10.3). Several
  upgrades depend on it rather than on `v`.

HomeBank refuses files whose `v` is newer than it knows. The toolkit reads them but
reports a warning.

## Values

| What | How it is written | Notes |
|---|---|---|
| Integers | decimal | Left out when 0 (`hb_xml_append_int`), except `auto_smode` and the fixed `<cur>`, `<grp>`, `<tag>` formats |
| Amounts | `g_ascii_dtostr`, i.e. `"%.17g"` | `9.72` is written `9.7200000000000006`; always written, even 0 |
| Text | `g_markup_escape_text` | Left out when empty. `& < > ' "` escaped; control characters written as `&#x1;` etc. (not valid XML 1.0, so strict parsers fail) |
| Account and payee notes | HomeBank's own variant | Also escapes CR and LF (`&#xd;`, `&#xa;`) |
| Dates | GLib Julian day number | Day 1 = 0001-01-01, identical to Python's `date.toordinal()`. HomeBank's limits: 693596 (1900-01-01) to 803533 (2200-12-31) |
| `"(null)"` | Very old versions wrote it for missing text | Ignored for account name/number/bankname/website/notes, payee notes, rule name/notes, transaction and template wording/info, tags; kept literally elsewhere |

HomeBank reads with GLib's GMarkup parser, which does **not** normalise whitespace in
attribute values. The toolkit's scanner (`hbkit/xhb/scanner.py`) behaves the same way.

## Elements and attributes

### `<account>`
`key flags pos type curr name number bankname initial minimum maximum cheque1 cheque2
website notes tpl grp ccday rdate`

* `type`: 0 none, 1 bank, 2 cash, 3 asset, 4 credit card, 5 liability, 6 checking, 7 savings.
* `flags`: bit 1 closed, 4 no summary, 5 no budget, 6 no report.
* `rdate`: last reconciliation date (Julian).

### `<pay>` — payee
`key flags name category paymode notes` — `category`/`paymode` are the payee's defaults.

### `<cat>` — category
`key parent flags name b0 … b12`

* Two levels: a subcategory has `parent` = parent key and flag `GF_SUB` (1).
* Full name is `Parent:Child` (`da_cat_build_fullname`).
* `flags`: 1 sub, 2 income, 4 custom, 8 budget, 16 forced, 32 mixed, 256 hidden.
* `b0` is the monthly budget for every month; `b1`–`b12` per month.

### `<tag>`
`key name`. Tags are referred to **by name**, space-separated, in `tags="..."`.

### `<asg>` — assignment rule
`key flags pos field name notes payee category paymode amount tags`

* `name` is the search text; `field` 0 = memo, 1 = payee name.
* `flags`: 1 case-sensitive ("exact"), 2 set payee, 4 set category, 8 set payment mode,
  16 set tags, 128 also match amount, 256 regular expression, 512 overwrite payee,
  1024 overwrite category, 2048 overwrite payment mode, 4096 overwrite tags.
  Bit 32 ("prefilled") is session-only and cleared on save.
* Before file version 0.8 an `exact` attribute existed instead of the flag.

**How HomeBank applies rules** (`transaction_auto_assign` in `hb-assign.c`):

1. All rules are sorted by `pos` and **every** rule is tested against the transaction.
2. Matching: without the regex flag, the search text must occur anywhere in the text
   ("contains"), ignoring case unless flag 1 is set (`g_utf8_casefold`). With the
   regex flag, a PCRE regular expression (caseless unless flag 1). With flag 128 the
   amount must also be equal.
3. The text is the memo, or the payee's name when `field` = 1. A transaction without a
   payee has payee key 0, which HomeBank maps to a built-in "no payee" entry with an
   empty name (`da_pay_new`), so payee rules never match it. Only a payee key that
   points nowhere leaves the memo as the text.
4. For each matching rule, in order: "set category" (4) only fills an **empty**
   category, so the **first** matching rule wins; "overwrite category" (1024) always
   replaces it, so the **last** matching overwrite rule wins. The same applies to
   payee, payment mode and tags.
5. Split transactions: each split line is tested against memo rules (`field` 0) only,
   using the split's memo and amount; a split category is set if empty, or always
   when the rule has flag 1024.
6. Reconciled transactions are skipped when the "lock reconciled" option is on.
7. **Rules have no account condition.** `struct _assign` (hb-assign.h) holds only the
   search text, field, flags, notes, payee, category, payment mode, position, amount and
   tags. The `kacc` argument of `transaction_auto_assign(txn_list, kacc, lockrecon)` only
   limits a manual run to one account's transactions (0 = all); every rule still applies.
   A rule can therefore only treat accounts differently through the text it matches.

### `<ope>` — transaction
`date amount account damt dst_account paymode grpflg st flags payee category wording
info tags kxfer scat samt smem`

* `wording` is the memo; `info` the info/number field.
* `amount` is signed: negative = money out.
* `st` status: 0 none, 1 cleared, 2 reconciled, 3 void. **Before data version 50900
  (HomeBank 5.9)** 3 meant "remind" (now flag 32) and 4 meant void — the reader
  converts.
* `flags`: 2 income, 8 internal transfer, 16 transfer between currencies (then `damt`
  is the other side's amount), 32 remind, 64 second cheque book, 256 split,
  512 imported, 1024 past.
* Order: HomeBank prepends each `<ope>` to its account while loading and then sorts
  every account's transactions by date, so the order of `<ope>` elements in the file
  does not matter. It saves them account by account.
* Internal transfers: two `<ope>` elements with the same `kxfer`, each pointing at the
  other account with `dst_account`. Before 5.x, payment mode 5 marked them instead.
  Linking two existing transactions (`transaction_xfer_change_to_child` in
  hb-transaction.c) sets flag 8 on both, `kxfer` to one above the highest in the file on
  both, and `dst_account` to the other side's account; the payment mode is not changed.
  HomeBank refuses transfers with a closed account. Its import pairs transfers by itself
  only for OFX files; its CSV import turns payment mode 5 into 4 ("bank transfer").
* `paymode`: 0 none, 1 credit card, 2 cheque, 3 cash, 4 bank transfer, 5 internal
  transfer (old), 6 debit card, 7 standing order, 8 electronic payment, 9 deposit,
  10 bank fee, 11 direct debit, 12 mobile phone.
* Splits: `scat="12||13" samt="-70||-20" smem="food||"` — three `||`-separated lists of
  equal length (category keys, amounts, memos). The transaction's own `category` is
  then normally empty. HomeBank ignores the splits if the lists differ in length.

### `<fav>` — template / scheduled transaction
The `<ope>` attributes (without `date`, `kxfer`) plus `recflg nextdate every unit
limit weekend gap ordn wkdy`. Since 5.9, `limit` is written only when `recflg` has
bit 2 (limit), and `ordn`/`wkdy` only with bit 4 (relative). HomeBank 5.1 always
wrote `limit`, which is the one difference the tests allow for in `example.xhb`.

### `<flt>` — saved filter
`key dat acc pay cat tag txt amt mod sta typ name`; each group attribute is
`option|values`. Kept as text by the toolkit.

### `<properties>`, `<cur>`, `<grp>`
See `KNOWN_ATTRIBUTES` in `hbkit/xhb/format.py`.

## How the toolkit stays faithful

* `hbkit/xhb/format.py` holds the constants above.
* `hbkit/xhb/serialise.py` copies HomeBank's save functions. The tests regenerate every
  line of HomeBank's `example.xhb` (written by 5.1) byte for byte, except template
  lines, which differ only by the `limit` change described above.
* Attributes and elements the toolkit does not know are kept (`extra`, `unknown_elements`)
  and listed by the structure report with a `*`.

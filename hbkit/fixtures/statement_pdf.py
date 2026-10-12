"""Synthetic PDF bank statements for tests (entirely made-up content).

The layout follows the structure of a South African bank's monthly statement: a
summary page with the account number, then a bank-charges table and the transaction
table (Tran list no, Date, Description, Fees, Debits, Credits, Balance) with an opening
and a closing balance line, amounts right-aligned, charges marked with *, long
descriptions wrapped onto a second line and the table continuing on further pages with
its header repeated.

Font options exercise the PDF reader: a standard font with WinAnsiEncoding, an embedded
Type 1 font whose built-in encoding is scrambled, or a composite (Type0) font with
two-byte codes and a ToUnicode map.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field

from ..imports.pdf import WINANSI_NAMES


@dataclass
class StatementRow:
    date: str  # as printed, e.g. "02/03/2026"
    description: str | list[str]  # a list prints over several lines
    debit: float | None = None
    credit: float | None = None
    fee: float | None = None
    marker: str = ""  # "*" for charges including VAT
    reference: str = ""


@dataclass
class StatementOptions:
    account_number: str = "62000000001"
    opening: float = 1000.0
    opening_date: str = "02/03/2026"
    rows_per_page: int = 40
    compress: bool = True
    font: str = "standard"  # "standard", "embedded" (scrambled built-in encoding) or "composite"
    wrong_balance_at: int | None = None  # print a wrong balance on this row (0-based)
    encrypted: bool = False
    extra_pages: list[str] = field(default_factory=list)


def _width(ch: str) -> int:
    if ch.isdigit():
        return 556
    if ch in " ,.":
        return 278
    if ch.isupper():
        return 667
    return 556 if ch.isalpha() else 500


def _money(v: float) -> str:
    return f"{v:,.2f}"


class _Pdf:
    def __init__(self, options: StatementOptions):
        self.o = options
        self.objects: list[bytes] = []

    def add(self, body: bytes) -> int:
        self.objects.append(body)
        return len(self.objects)

    def stream(self, data: bytes, extra: str = "") -> int:
        if self.o.compress:
            data = zlib.compress(data)
            extra += " /Filter /FlateDecode"
        return self.add(f"<< /Length {len(data)}{extra} >>\nstream\n".encode() + data + b"\nendstream")

    # text encoding per font option
    def encode(self, text: str) -> bytes:
        kind = self.o.font
        if kind == "embedded":
            return bytes(ord(c) - 31 for c in text)  # scrambled: space is code 1
        if kind == "composite":
            return b"".join((ord(c) + 100).to_bytes(2, "big") for c in text)
        return text.encode("cp1252")

    def text_width(self, text: str, size: float) -> float:
        return sum(_width(c) for c in text) / 1000 * size

    def font_object(self) -> int:
        kind = self.o.font
        printable = [chr(c) for c in range(32, 127)]
        if kind == "standard":
            widths = " ".join(str(_width(c)) for c in printable)
            return self.add(f"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding "
                            f"/FirstChar 32 /LastChar 126 /Widths [{widths}] >>".encode())
        if kind == "embedded":
            clear = ["%!PS-AdobeFont-1.0: SyntheticSans 001", "/Encoding 256 array",
                     "0 1 255 {1 index exch /.notdef put} for"]
            clear += [f"dup {ord(c) - 31} /{WINANSI_NAMES[ord(c)]} put" for c in printable]
            program = ("\n".join(clear) + "\nreadonly def\ncurrentfile eexec\n").encode() + bytes(range(256)) * 4
            file_ref = self.stream(program, " /Length1 100 /Length2 1024 /Length3 0")
            desc = self.add(f"<< /Type /FontDescriptor /FontName /SyntheticSans /Flags 32 /FontBBox [0 0 1000 1000] "
                            f"/ItalicAngle 0 /Ascent 700 /Descent -200 /CapHeight 700 /StemV 80 "
                            f"/FontFile {file_ref} 0 R >>".encode())
            widths = " ".join(str(_width(c)) for c in printable)
            return self.add(f"<< /Type /Font /Subtype /Type1 /BaseFont /SyntheticSans /FirstChar 1 /LastChar 95 "
                            f"/Widths [{widths}] /FontDescriptor {desc} 0 R >>".encode())
        cmap = ("/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
                "1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
                "1 beginbfrange <0084> <00E2> <0020> endbfrange\n"
                "endcmap CMapName currentdict /CMap defineresource pop end end\n").encode()
        tounicode = self.stream(cmap)
        w = " ".join(str(_width(c)) for c in printable)
        cid = self.add(f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /SyntheticSans "
                       f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
                       f"/DW 500 /W [132 [{w}]] >>".encode())
        return self.add(f"<< /Type /Font /Subtype /Type0 /BaseFont /SyntheticSans /Encoding /Identity-H "
                        f"/DescendantFonts [{cid} 0 R] /ToUnicode {tounicode} 0 R >>".encode())


def statement_pdf(rows: list[StatementRow], options: StatementOptions | None = None) -> bytes:
    o = options or StatementOptions()
    pdf = _Pdf(o)
    catalog = pdf.add(b"")  # filled in at the end
    pages_obj = pdf.add(b"")
    font = pdf.font_object()
    page_contents: list[bytes] = []
    ops: list[str] = []

    def text(x: float, y: float, s: str, size: float = 7.0, right: bool = False) -> None:
        if right:
            x -= pdf.text_width(s, size)
        ops.append(f"BT /F1 {size:g} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm <{pdf.encode(s).hex()}> Tj ET")

    # page 1: summary
    text(40, 760, "Account summary", 14)
    text(40, 730, "Account type", 9)
    text(300, 730, "Account number", 9)
    text(40, 715, "Current account", 11)
    text(300, 715, o.account_number, 11)
    text(40, 690, "Statement period", 9)
    text(300, 690, "Statement date", 9)
    text(40, 60, "Page 1", 6)
    page_contents.append("\n".join(ops).encode())

    cols = {"ref": 37, "date": 96, "desc": 142, "fees": 334, "debit": 414, "credit": 487, "balance": 564}

    def header(y: float) -> None:
        text(cols["ref"], y, "Tran list no", 9)
        text(cols["date"], y, "Date", 9)
        text(cols["desc"], y, "Description", 9)
        for key, label in (("fees", "Fees (R)"), ("debit", "Debits (R)"), ("credit", "Credits (R)"),
                           ("balance", "Balance (R)")):
            text(cols[key], y, label, 9, right=True)

    balance = o.opening
    ops = []
    y = 760.0
    text(50, y, "Bank charges for the period", 12)
    y -= 18
    text(37, y, "Narrative Description", 9)
    text(394, y, "Item cost (R)", 9, right=True)
    text(481, y, "VAT (R)", 9, right=True)
    text(561, y, "Total (R)", 9, right=True)
    y -= 13
    text(37, y, "Monthly fee", 9)
    text(394, y, "12.17", 9, right=True)
    text(481, y, "1.83", 9, right=True)
    text(561, y, "14.00", 9, right=True)
    y -= 16
    header(y)
    y -= 13
    text(cols["date"], y, o.opening_date)
    text(cols["desc"], y, "Opening balance")
    text(cols["balance"], y, _money(balance), right=True)
    on_page = 1
    for i, r in enumerate(rows):
        if on_page >= o.rows_per_page:  # continue on a new page with the header repeated
            text(40, 40, "Continued", 6)
            page_contents.append("\n".join(ops).encode())
            ops, y, on_page = [], 760.0, 0
            header(y)
        y -= 12
        on_page += 1
        lines = r.description if isinstance(r.description, list) else [r.description]
        if r.reference:
            text(cols["ref"], y, r.reference)
        text(cols["date"], y, r.date)
        text(cols["desc"], y, lines[0])
        for key, value in (("fees", r.fee), ("debit", r.debit), ("credit", r.credit)):
            if value is not None:
                text(cols[key], y, _money(value) + (" *" if r.marker and key == "debit" else ""), right=True)
        balance = round(balance - (r.fee or 0) - (r.debit or 0) + (r.credit or 0), 2)
        printed = balance + (1.0 if o.wrong_balance_at == i else 0.0)
        text(cols["balance"], y, _money(printed), right=True)
        for extra in lines[1:]:
            y -= 10
            text(cols["desc"], y, extra)
    y -= 16
    text(40, y, "Closing balance", 10)
    text(cols["balance"] - 1, y, _money(balance), 10, right=True)
    text(28, 75, "Thank you for banking with us", 12)
    text(234, 60, "Synthetic statement for tests. Not a real bank document.", 6)
    page_contents.append("\n".join(ops).encode())
    for extra in o.extra_pages:
        ops = []
        text(40, 760, extra, 9)
        page_contents.append("\n".join(ops).encode())

    kids = []
    for content in page_contents:
        c = pdf.stream(content)
        kids.append(pdf.add(f"<< /Type /Page /Parent {pages_obj} 0 R /MediaBox [0 0 595 842] "
                            f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {c} 0 R >>".encode()))
    pdf.objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode()
    pdf.objects[pages_obj - 1] = (f"<< /Type /Pages /Count {len(kids)} /Kids [" +
                                  " ".join(f"{k} 0 R" for k in kids) + "] >>").encode()
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, body in enumerate(pdf.objects, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(offsets) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
    encrypt = " /Encrypt << /Filter /Standard /V 1 /R 2 >>" if o.encrypted else ""
    out += (f"trailer\n<< /Size {len(offsets) + 1} /Root {catalog} 0 R{encrypt} >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


#: A month of made-up transactions on the synthetic file's cheque account.
SAMPLE_ROWS = [
    StatementRow("02/03/2026", "MONTHLY ACCOUNT FEE", debit=14.00, marker="*", reference="000417"),
    StatementRow("06/03/2026", "MAGTAPE CREDIT ACME CORP SALARY", credit=35000.00),
    StatementRow("10/03/2026", "DEBIT ORDER VIRGIN ACTIVE 0012345678", debit=650.00),
    StatementRow("10/03/2026", "DEBIT ORDER FEE", debit=3.50, marker="*"),
    StatementRow("14/03/2026", ["POS PURCHASE WOOLWORTHS SANDTON", "4587*1234 14 MAR"], debit=1234.56),
    StatementRow("18/03/2026", "IB PAYMENT TO CITY OF JOBURG", debit=2100.00, fee=5.50),
    StatementRow("25/03/2026", "TRANSFER FROM SAVINGS", credit=750.00),
]

"""Read the text of a PDF bank statement, with positions, using only the standard library.

Enough of the PDF format (ISO 32000) for statements produced by document systems:

* objects (also inside object streams), the page tree with inherited resources;
* stream filters FlateDecode, ASCIIHexDecode and ASCII85Decode;
* the text operators of content streams (BT/ET, Tf, Tc, Tw, Tz, TL, Ts, Td, TD, Tm, T*,
  Tj, TJ, ', "), the graphics state (q, Q, cm) and form XObjects (Do);
* fonts: simple fonts with a named encoding, /Differences, a ToUnicode map or the
  built-in encoding of an embedded Type 1 font; composite (Type0) fonts with a
  ToUnicode map; glyph widths from /Widths or /W, so the end of each text is known.

Not supported, with a clear message: encrypted (password-protected) PDFs, and scanned
statements without a text layer.

The result is a list of glyphs per page (x, y in points from the bottom left, text and
size); ``lines`` groups them into lines and the lines into chunks separated by gaps.
"""

from __future__ import annotations

import base64
import binascii
import re
import zlib
from dataclasses import dataclass, field

from .formats import ImportFormatError


class PdfError(ImportFormatError):
    """A PDF the toolkit cannot read (terminal-safe message)."""


# --- glyph names --------------------------------------------------------------------------------------

#: WinAnsiEncoding (PDF 32000 annex D): code -> glyph name, for codes 32-255.
_WINANSI = (
    "space exclam quotedbl numbersign dollar percent ampersand quotesingle parenleft parenright asterisk plus "
    "comma hyphen period slash zero one two three four five six seven eight nine colon semicolon less equal "
    "greater question at A B C D E F G H I J K L M N O P Q R S T U V W X Y Z bracketleft backslash "
    "bracketright asciicircum underscore grave a b c d e f g h i j k l m n o p q r s t u v w x y z braceleft "
    "bar braceright asciitilde - Euro - quotesinglbase florin quotedblbase ellipsis dagger daggerdbl "
    "circumflex perthousand Scaron guilsinglleft OE - Zcaron - - quoteleft quoteright quotedblleft "
    "quotedblright bullet endash emdash tilde trademark scaron guilsinglright oe - zcaron Ydieresis space "
    "exclamdown cent sterling currency yen brokenbar section dieresis copyright ordfeminine guillemotleft "
    "logicalnot hyphen registered macron degree plusminus twosuperior threesuperior acute mu paragraph "
    "periodcentered cedilla onesuperior ordmasculine guillemotright onequarter onehalf threequarters "
    "questiondown Agrave Aacute Acircumflex Atilde Adieresis Aring AE Ccedilla Egrave Eacute Ecircumflex "
    "Edieresis Igrave Iacute Icircumflex Idieresis Eth Ntilde Ograve Oacute Ocircumflex Otilde Odieresis "
    "multiply Oslash Ugrave Uacute Ucircumflex Udieresis Yacute Thorn germandbls agrave aacute acircumflex "
    "atilde adieresis aring ae ccedilla egrave eacute ecircumflex edieresis igrave iacute icircumflex "
    "idieresis eth ntilde ograve oacute ocircumflex otilde odieresis divide oslash ugrave uacute ucircumflex "
    "udieresis yacute thorn ydieresis"
).split()
WINANSI_NAMES = {code: name for code, name in enumerate(_WINANSI, start=32) if name != "-"}

#: StandardEncoding differs from WinAnsi in the ASCII range only at 39 and 96; above 127
#: it has its own layout (annex D).
STANDARD_NAMES = {code: name for code, name in WINANSI_NAMES.items() if code < 127}
STANDARD_NAMES.update({39: "quoteright", 96: "quoteleft"})
STANDARD_NAMES.update(dict(zip(
    (161, 162, 163, 164, 165, 166, 167, 168, 169, 170, 171, 172, 173, 174, 175, 177, 178, 179, 180, 182,
     183, 184, 185, 186, 187, 188, 189, 191, 193, 194, 195, 196, 197, 198, 199, 200, 202, 203, 205, 206,
     207, 208, 225, 227, 232, 233, 234, 235, 241, 245, 248, 249, 250, 251),
    ("exclamdown cent sterling fraction yen florin section currency quotesingle quotedblleft guillemotleft "
     "guilsinglleft guilsinglright fi fl endash dagger daggerdbl periodcentered paragraph bullet "
     "quotesinglbase quotedblbase quotedblright guillemotright ellipsis perthousand questiondown grave acute "
     "circumflex tilde macron breve dotaccent dieresis ring cedilla hungarumlaut ogonek caron emdash AE "
     "ordfeminine Lslash Oslash OE ordmasculine ae dotlessi lslash oslash oe germandbls").split())))

GLYPHS: dict[str, str] = {}
for _code, _name in WINANSI_NAMES.items():
    GLYPHS.setdefault(_name, bytes([_code]).decode("cp1252"))
GLYPHS.update({"space": " ", "hyphen": "-", "quotesingle": "'", "grave": "`", "minus": "−",
               "nbspace": " ", "nonbreakingspace": " ", "sfthyphen": "-", "fi": "fi", "fl": "fl",
               "fraction": "⁄", "Lslash": "Ł", "lslash": "ł", "dotlessi": "ı",
               "breve": "˘", "dotaccent": "˙", "ring": "˚", "hungarumlaut": "˝",
               "ogonek": "˛", "caron": "ˇ", "Euro": "€", "mu": "µ"})


def glyph_text(name: str) -> str:
    if name in GLYPHS:
        return GLYPHS[name]
    m = re.fullmatch(r"uni([0-9A-Fa-f]{4})(?:\..*)?", name) or re.fullmatch(r"u([0-9A-Fa-f]{4,6})", name)
    if m:
        return chr(int(m.group(1), 16))
    base = name.split(".")[0].split("_")[0]
    if base in GLYPHS:
        return GLYPHS[base]
    return base if len(base) == 1 else "�"


# --- objects ------------------------------------------------------------------------------------------


class Name(str):
    pass


@dataclass(frozen=True)
class Ref:
    num: int
    gen: int = 0


@dataclass
class Stream:
    dict: dict
    raw: bytes


class _Keyword(str):
    pass


_WS = b"\x00\t\n\x0c\r "
_DELIM = b"()<>[]{}/%"
_NUMBER = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)")


class Lexer:
    def __init__(self, data: bytes, pos: int = 0):
        self.data, self.pos = data, pos

    def skip(self) -> None:
        d, n = self.data, len(self.data)
        while self.pos < n:
            c = d[self.pos]
            if c in _WS:
                self.pos += 1
            elif c == 0x25:  # % comment
                while self.pos < n and d[self.pos] not in b"\r\n":
                    self.pos += 1
            else:
                break

    def token(self):
        """The next token: a value, '[', ']', '<<', '>>', '{', '}' or a keyword; None at the end."""
        self.skip()
        d = self.data
        if self.pos >= len(d):
            return None
        c = d[self.pos:self.pos + 1]
        if c == b"/":
            m = re.compile(rb"/[^\x00\t\n\x0c\r ()<>\[\]{}/%]*").match(d, self.pos)
            self.pos = m.end()
            raw = re.sub(rb"#([0-9A-Fa-f]{2})", lambda x: bytes([int(x.group(1), 16)]), m.group(0)[1:])
            return Name(raw.decode("latin-1"))
        if c == b"(":
            return self._literal()
        if c == b"<":
            if d[self.pos:self.pos + 2] == b"<<":
                self.pos += 2
                return "<<"
            end = d.index(b">", self.pos)
            hexdata = re.sub(rb"\s", b"", d[self.pos + 1:end])
            self.pos = end + 1
            if len(hexdata) % 2:
                hexdata += b"0"
            return binascii.unhexlify(hexdata)
        if c == b">" and d[self.pos:self.pos + 2] == b">>":
            self.pos += 2
            return ">>"
        if c in (b"[", b"]", b"{", b"}"):
            self.pos += 1
            return c.decode()
        m = _NUMBER.match(d, self.pos)
        if m and (m.end() == len(d) or d[m.end()] in _WS + _DELIM):
            self.pos = m.end()
            text = m.group(0)
            return float(text) if b"." in text else int(text)
        m = re.compile(rb"[^\x00\t\n\x0c\r ()<>\[\]{}/%]+").match(d, self.pos)
        if not m:
            self.pos += 1
            return _Keyword(c.decode("latin-1"))
        self.pos = m.end()
        word = m.group(0).decode("latin-1")
        return {"true": True, "false": False, "null": None}.get(word, _Keyword(word))

    def _literal(self) -> bytes:
        d, out, depth = self.data, bytearray(), 0
        self.pos += 1
        escapes = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
        while self.pos < len(d):
            c = d[self.pos]
            self.pos += 1
            if c == 0x5C:  # backslash
                e = d[self.pos]
                self.pos += 1
                if e in escapes:
                    out.append(escapes[e])
                elif e in b"01234567":
                    digits = bytes([e])
                    while len(digits) < 3 and self.pos < len(d) and d[self.pos] in b"01234567":
                        digits += bytes([d[self.pos]])
                        self.pos += 1
                    out.append(int(digits, 8) & 0xFF)
                elif e == 0x0D:
                    if self.pos < len(d) and d[self.pos] == 0x0A:
                        self.pos += 1
                elif e != 0x0A:
                    out.append(e)
            elif c == 0x28:
                depth += 1
                out.append(c)
            elif c == 0x29:
                if depth == 0:
                    return bytes(out)
                depth -= 1
                out.append(c)
            else:
                out.append(c)
        return bytes(out)

    def value(self, token=None):
        """A complete value (dictionaries, arrays and references assembled)."""
        tok = self.token() if token is None else token
        if tok == "<<":
            d = {}
            while True:
                key = self.token()
                if key == ">>" or key is None:
                    return d
                d[str(key)] = self.value()
        if tok == "[":
            items = []
            while True:
                t = self.token()
                if t == "]" or t is None:
                    return items
                items.append(self.value(t))
        if isinstance(tok, int):
            save = self.pos
            gen = self.token()
            if isinstance(gen, int):
                r = self.token()
                if r == "R":
                    return Ref(tok, gen)
            self.pos = save
        return tok


def _decode_stream(s: Stream, doc: Document) -> bytes:
    filters = doc.resolve(s.dict.get("Filter"))
    filters = [] if filters is None else filters if isinstance(filters, list) else [filters]
    data = s.raw
    for f in filters:
        f = str(doc.resolve(f))
        if f in ("FlateDecode", "Fl"):
            try:
                data = zlib.decompress(data)
            except zlib.error:
                data = zlib.decompressobj().decompress(data)  # tolerate a damaged end
        elif f in ("ASCIIHexDecode", "AHx"):
            data = binascii.unhexlify(re.sub(rb"\s|>", b"", data) + (b"0" if len(re.sub(rb"\s|>", b"", data)) % 2 else b""))
        elif f in ("ASCII85Decode", "A85"):
            data = base64.a85decode(re.sub(rb"\s", b"", data).removesuffix(b"~>"), adobe=False)
        else:
            raise PdfError("the PDF uses a compression the toolkit cannot read")
    return data


class Document:
    """The objects of a PDF, found by scanning (no cross-reference table needed)."""

    def __init__(self, raw: bytes):
        if not raw.startswith(b"%PDF-"):
            raise PdfError("this is not a PDF file")
        self.raw = raw
        self.objects: dict[int, object] = {}
        pending: list[tuple[int, dict, int]] = []  # streams whose /Length is in another object
        skip_until = 0
        for m in re.finditer(rb"(?<![\d])(\d+)\s+(\d+)\s+obj\b", raw):
            if m.start() < skip_until:  # inside a stream already read
                continue
            lex = Lexer(raw, m.end())
            try:
                value = lex.value()
            except (ValueError, IndexError):
                continue
            num = int(m.group(1))
            if isinstance(value, dict):
                save = lex.pos
                if lex.token() == "stream":
                    start_pos = lex.pos
                    if raw[start_pos:start_pos + 2] == b"\r\n":
                        start_pos += 2
                    elif raw[start_pos:start_pos + 1] in (b"\n", b"\r"):
                        start_pos += 1
                    length = value.get("Length")
                    if isinstance(length, int) and raw.find(b"endstream", start_pos + length, start_pos + length + 20) >= 0:
                        value = Stream(value, raw[start_pos:start_pos + length])
                        skip_until = start_pos + length
                    else:
                        pending.append((num, value, start_pos))
                        skip_until = max(skip_until, raw.find(b"endstream", start_pos))
                        continue
                else:
                    lex.pos = save
            self.objects[num] = value
        for num, d, start_pos in pending:
            length = self.resolve(d.get("Length"))
            if isinstance(length, int) and raw.find(b"endstream", start_pos + length, start_pos + length + 20) >= 0:
                data = raw[start_pos:start_pos + length]
            else:
                end_pos = raw.find(b"endstream", start_pos)
                data = raw[start_pos:end_pos]
                data = data[:-2] if data.endswith(b"\r\n") else data[:-1] if data.endswith((b"\n", b"\r")) else data
            self.objects[num] = Stream(d, data)
        for obj in list(self.objects.values()):  # objects inside object streams
            if isinstance(obj, Stream) and obj.dict.get("Type") == "ObjStm":
                data = _decode_stream(obj, self)
                lex = Lexer(data)
                pairs = [(lex.token(), lex.token()) for _ in range(int(obj.dict.get("N", 0)))]
                first = int(obj.dict.get("First", 0))
                for num, off in pairs:
                    if isinstance(num, int) and isinstance(off, int) and num not in self.objects:
                        self.objects[num] = Lexer(data, first + off).value()
        if any(isinstance(o, dict) and "Encrypt" in o for o in self.objects.values()) or \
                re.search(rb"trailer\s*<<[^>]*/Encrypt", raw):
            raise PdfError("this PDF is password-protected; download the statement without a password, "
                           "or as CSV or QIF")

    def resolve(self, value):
        seen = 0
        while isinstance(value, Ref) and seen < 50:
            value = self.objects.get(value.num)
            seen += 1
        return value

    def stream_data(self, value) -> bytes:
        value = self.resolve(value)
        return _decode_stream(value, self) if isinstance(value, Stream) else b""

    def pages(self) -> list[tuple[dict, dict]]:
        """(page dictionary, resources) in reading order."""
        root = None
        m = re.search(rb"/Root\s+(\d+)\s+\d+\s+R", self.raw)
        if m:
            root = self.resolve(Ref(int(m.group(1))))
        if not isinstance(root, dict):
            root = next((o for o in self.objects.values() if isinstance(o, dict) and o.get("Type") == "Catalog"), None)
        out: list[tuple[dict, dict]] = []

        def walk(node, inherited, depth=0):
            node = self.resolve(node)
            if isinstance(node, Stream):
                node = node.dict
            if not isinstance(node, dict) or depth > 30:
                return
            resources = self.resolve(node.get("Resources")) or inherited
            if node.get("Type") == "Pages" or "Kids" in node:
                for kid in self.resolve(node.get("Kids")) or []:
                    walk(kid, resources, depth + 1)
            else:
                out.append((node, resources or {}))

        if root is not None:
            walk(root.get("Pages"), {})
        if not out:  # no usable page tree: pages in file order
            out = [(o, self.resolve(o.get("Resources")) or {}) for o in self.objects.values()
                   if isinstance(o, dict) and o.get("Type") == "Page"]
        return out


# --- fonts --------------------------------------------------------------------------------------------


def _cmap_unicode(data: bytes) -> tuple[dict[int, str], int]:
    """A ToUnicode CMap: code -> text, and the code length in bytes."""
    out: dict[int, str] = {}
    width = 1
    for m in re.finditer(rb"begincodespacerange(.*?)endcodespacerange", data, re.S):
        lens = [len(x) // 2 for x in re.findall(rb"<([0-9A-Fa-f]+)>", m.group(1))]
        if lens:
            width = max(lens)

    def text(h: bytes) -> str:
        b = binascii.unhexlify(h if len(h) % 2 == 0 else h + b"0")
        return b.decode("utf-16-be", errors="replace") if len(b) >= 2 else chr(b[0])

    for m in re.finditer(rb"beginbfchar(.*?)endbfchar", data, re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", m.group(1)):
            out[int(src, 16)] = text(dst)
            width = max(width, len(src) // 2)
    for m in re.finditer(rb"beginbfrange(.*?)endbfrange", data, re.S):
        body = m.group(1)
        for lo, hi, rest in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])", body):
            lo_i, hi_i = int(lo, 16), int(hi, 16)
            if hi_i - lo_i > 65535:
                continue
            if rest.startswith(b"["):
                for i, h in enumerate(re.findall(rb"<([0-9A-Fa-f]*)>", rest)):
                    out[lo_i + i] = text(h)
            else:
                start = binascii.unhexlify(rest[1:-1]) if len(rest) > 2 else b"\x00"
                base = int.from_bytes(start, "big")
                for i in range(hi_i - lo_i + 1):
                    v = base + i
                    out[lo_i + i] = v.to_bytes(max(2, len(start)), "big").decode("utf-16-be", errors="replace") \
                        if v > 0xFF or len(start) >= 2 else chr(v)
            width = max(width, len(lo) // 2)
    return out, width


def _type1_encoding(data: bytes) -> dict[int, str] | None:
    """The built-in encoding in the clear-text part of an embedded Type 1 font."""
    clear = data[:data.find(b"eexec")] if b"eexec" in data else data[:20000]
    if re.search(rb"/Encoding\s+StandardEncoding", clear):
        return dict(STANDARD_NAMES)
    puts = re.findall(rb"dup\s+(\d+)\s*/([^\s/]+)\s+put", clear)
    return {int(c): n.decode("latin-1") for c, n in puts} or None


class Font:
    def __init__(self, doc: Document, font: dict):
        self.composite = font.get("Subtype") == "Type0"
        self.unicode: dict[int, str] = {}
        self.code_bytes = 2 if self.composite else 1
        self.widths: dict[int, float] = {}
        self.default_width = 500.0
        to_unicode = font.get("ToUnicode")
        if to_unicode is not None:
            self.unicode, n = _cmap_unicode(doc.stream_data(to_unicode))
            if self.composite:
                self.code_bytes = n
        if self.composite:
            desc = doc.resolve((doc.resolve(font.get("DescendantFonts")) or [{}])[0]) or {}
            self.default_width = float(doc.resolve(desc.get("DW", 1000)))
            w = doc.resolve(desc.get("W")) or []
            i = 0
            while i < len(w):
                first = doc.resolve(w[i])
                nxt = doc.resolve(w[i + 1]) if i + 1 < len(w) else None
                if isinstance(nxt, list):
                    for k, v in enumerate(nxt):
                        self.widths[first + k] = float(doc.resolve(v))
                    i += 2
                else:
                    last, v = nxt, doc.resolve(w[i + 2]) if i + 2 < len(w) else 0
                    for c in range(int(first), int(last) + 1):
                        self.widths[c] = float(v)
                    i += 3
            return
        first = int(doc.resolve(font.get("FirstChar", 0)) or 0)
        for k, v in enumerate(doc.resolve(font.get("Widths")) or []):
            self.widths[first + k] = float(doc.resolve(v))
        desc = doc.resolve(font.get("FontDescriptor")) or {}
        self.default_width = float(doc.resolve(desc.get("MissingWidth", 0)) or 0) or 500.0
        if self.unicode:
            return
        names: dict[int, str] = {}
        enc = doc.resolve(font.get("Encoding"))
        base = enc.get("BaseEncoding") if isinstance(enc, dict) else enc
        if base == "WinAnsiEncoding":
            names = dict(WINANSI_NAMES)
        elif base == "MacRomanEncoding":
            self.unicode = {c: bytes([c]).decode("mac_roman") for c in range(32, 256)}
        elif base == "StandardEncoding":
            names = dict(STANDARD_NAMES)
        else:
            program = desc.get("FontFile") or desc.get("FontFile3")
            names = (_type1_encoding(doc.stream_data(program)) if program is not None else None) or dict(STANDARD_NAMES)
        if isinstance(enc, dict):
            code = 0
            for item in doc.resolve(enc.get("Differences")) or []:
                item = doc.resolve(item)
                if isinstance(item, int):
                    code = item
                elif isinstance(item, str):
                    names[code] = str(item)
                    code += 1
        for c, n in names.items():
            self.unicode.setdefault(c, glyph_text(n))

    def codes(self, s: bytes) -> list[int]:
        if self.code_bytes == 1:
            return list(s)
        n = self.code_bytes
        return [int.from_bytes(s[i:i + n], "big") for i in range(0, len(s) - n + 1, n)]

    def text(self, code: int) -> str:
        if code in self.unicode:
            return self.unicode[code]
        return chr(code) if not self.composite and 32 <= code < 127 else "�"

    def width(self, code: int) -> float:
        return self.widths.get(code, self.default_width) / 1000.0


# --- content streams --------------------------------------------------------------------------------


@dataclass
class Glyph:
    x0: float
    x1: float
    y: float
    size: float
    text: str


def _mul(a, b):
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5])


_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


@dataclass
class _State:
    ctm: tuple = _IDENTITY
    font: Font | None = None
    size: float = 0.0
    tc: float = 0.0
    tw: float = 0.0
    th: float = 1.0
    tl: float = 0.0
    rise: float = 0.0

    def copy(self) -> _State:
        return _State(self.ctm, self.font, self.size, self.tc, self.tw, self.th, self.tl, self.rise)


class _Interpreter:
    def __init__(self, doc: Document):
        self.doc = doc
        self.glyphs: list[Glyph] = []
        self._fonts: dict[int, Font] = {}

    def font(self, resources: dict, name: str) -> Font | None:
        fonts = self.doc.resolve(resources.get("Font")) or {}
        ref = fonts.get(name)
        obj = self.doc.resolve(ref)
        if not isinstance(obj, dict):
            return None
        key = ref.num if isinstance(ref, Ref) else id(obj)
        if key not in self._fonts:
            self._fonts[key] = Font(self.doc, obj)
        return self._fonts[key]

    def run(self, data: bytes, resources: dict, state: _State, depth: int = 0) -> None:
        lex = Lexer(data)
        operands: list = []
        stack: list[_State] = []
        tm = tlm = _IDENTITY
        while True:
            tok = lex.token()
            if tok is None:
                return
            if not isinstance(tok, _Keyword):
                operands.append(lex.value(tok) if tok in ("[", "<<") else tok)
                continue
            op, args, operands = str(tok), operands, []
            try:
                if op == "q":
                    stack.append(state.copy())
                elif op == "Q":
                    state = stack.pop() if stack else state
                elif op == "cm" and len(args) == 6:
                    state.ctm = _mul(tuple(float(a) for a in args), state.ctm)
                elif op == "BT":
                    tm = tlm = _IDENTITY
                elif op == "Tf" and len(args) == 2:
                    state.font, state.size = self.font(resources, str(args[0])), float(args[1])
                elif op == "Tc":
                    state.tc = float(args[0])
                elif op == "Tw":
                    state.tw = float(args[0])
                elif op == "Tz":
                    state.th = float(args[0]) / 100.0
                elif op == "TL":
                    state.tl = float(args[0])
                elif op == "Ts":
                    state.rise = float(args[0])
                elif op in ("Td", "TD") and len(args) == 2:
                    if op == "TD":
                        state.tl = -float(args[1])
                    tlm = tm = _mul((1, 0, 0, 1, float(args[0]), float(args[1])), tlm)
                elif op == "Tm" and len(args) == 6:
                    tlm = tm = tuple(float(a) for a in args)
                elif op == "T*":
                    tlm = tm = _mul((1, 0, 0, 1, 0, -state.tl), tlm)
                elif op in ("Tj", "'", '"', "TJ"):
                    if op in ("'", '"'):
                        if op == '"':
                            state.tw, state.tc = float(args[0]), float(args[1])
                        tlm = tm = _mul((1, 0, 0, 1, 0, -state.tl), tlm)
                    items = args[-1] if op == "TJ" else [args[-1]]
                    for item in items if isinstance(items, list) else []:
                        if isinstance(item, (int, float)):
                            tm = _mul((1, 0, 0, 1, -float(item) / 1000.0 * state.size * state.th, 0), tm)
                        elif isinstance(item, bytes):
                            tm = self.show(item, state, tm)
                elif op == "Do" and args and depth < 10:
                    xobjects = self.doc.resolve(resources.get("XObject")) or {}
                    form = self.doc.resolve(xobjects.get(str(args[0])))
                    if isinstance(form, Stream) and form.dict.get("Subtype") == "Form":
                        inner = state.copy()
                        matrix = self.doc.resolve(form.dict.get("Matrix"))
                        if isinstance(matrix, list) and len(matrix) == 6:
                            inner.ctm = _mul(tuple(float(self.doc.resolve(v)) for v in matrix), inner.ctm)
                        self.run(_decode_stream(form, self.doc),
                                 self.doc.resolve(form.dict.get("Resources")) or resources, inner, depth + 1)
                elif op == "BI":  # inline image: skip to EI
                    end = re.compile(rb"\sEI(?=[\s]|$)").search(lex.data, lex.pos)
                    lex.pos = end.end() if end else len(lex.data)
            except (ValueError, TypeError, IndexError, ZeroDivisionError):
                continue

    def show(self, s: bytes, st: _State, tm: tuple) -> tuple:
        font = st.font
        if font is None or st.size == 0:
            return tm
        for code in font.codes(s):
            text = font.text(code)
            w = font.width(code)
            trm = _mul((st.size * st.th, 0, 0, st.size, 0, st.rise), _mul(tm, st.ctm))
            advance = (w * st.size + st.tc + (st.tw if font.code_bytes == 1 and code == 32 else 0)) * st.th
            end = _mul((1, 0, 0, 1, advance, 0), _mul(tm, st.ctm))
            size = abs(trm[3]) or abs(trm[0])
            if text.strip():
                self.glyphs.append(Glyph(trm[4], end[4], trm[5], size, text))
            tm = _mul((1, 0, 0, 1, advance, 0), tm)
        return tm


def page_glyphs(raw: bytes) -> list[list[Glyph]]:
    """The glyphs of every page."""
    doc = Document(raw)
    pages = []
    for page, resources in doc.pages():
        interp = _Interpreter(doc)
        contents = doc.resolve(page.get("Contents"))
        parts = contents if isinstance(contents, list) else [contents] if contents is not None else []
        data = b"\n".join(doc.stream_data(p) for p in parts)
        interp.run(data, resources, _State())
        pages.append(interp.glyphs)
    if not any(pages):
        raise PdfError("this PDF has no text layer (a scanned statement?); download it as CSV or QIF instead")
    return pages


@dataclass
class Chunk:
    x0: float
    x1: float
    text: str


@dataclass
class Line:
    y: float
    size: float
    chunks: list[Chunk] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(c.text for c in self.chunks)


def lines(glyphs: list[Glyph], gap: float = 0.9) -> list[Line]:
    """Glyphs grouped into lines (top first), and each line into chunks separated by more
    than `gap` times the font size. Words inside a chunk are joined with one space."""
    out: list[Line] = []
    for g in sorted(glyphs, key=lambda g: (-round(g.y, 1), g.x0)):
        if out and abs(out[-1].y - g.y) <= 0.35 * max(g.size, out[-1].size, 1.0):
            line = out[-1]
        else:
            line = Line(g.y, g.size)
            out.append(line)
        line.chunks.append(Chunk(g.x0, g.x1, g.text))  # merged below
    for line in out:
        glyphs_sorted = sorted(line.chunks, key=lambda c: c.x0)
        merged: list[Chunk] = []
        for c in glyphs_sorted:
            if merged:
                last = merged[-1]
                space = c.x0 - last.x1
                if space <= gap * line.size:
                    last.text += (" " if space > 0.15 * line.size else "") + c.text
                    last.x1 = max(last.x1, c.x1)
                    continue
            merged.append(Chunk(c.x0, c.x1, c.text))
        line.chunks = merged
    return out

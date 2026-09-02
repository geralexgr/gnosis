"""Binance's own trade-history CSV export, read strictly.

The export is the only way most traders can hand over a real book without
issuing an API key, so it is the first live adapter worth having: it needs no
credentials, no network and no OAuth dance, and it covers the whole account
history rather than the trailing window an endpoint will give you.

It is also an awkward file. Binance ships at least two layouts under the same
name -- a spot export (`Date(UTC), Pair, Side, Price, Executed, Amount, Fee`)
and a futures export, which drops `Executed` for `Quantity` and adds the
exchange's own `Realized Profit` and a leverage or margin column. Quantities
carry their asset glued to the number (`0.5ETH`, `1,500USDT`), timestamps are
UTC wall clock with no offset written down, and the fee is denominated in
whichever asset the account happened to pay it in. None of that is documented
anywhere; all of it is load-bearing.

**This module refuses rather than guesses.** A row it cannot parse raises,
naming the line number and the value that defeated it. That is a deliberate
inversion of the usual CSV-importer instinct to skip and carry on: Gnosis
computes a behavioural profile, and every claim it makes is a comparison
between a slice of the book and the rest of it. Silently dropping the fills
that happen to be malformed biases both arms by an unknown amount, in an
unknown direction, invisibly. A profile built on a partially-eaten history is
worse than no profile, because the user has no way to know to distrust it.

The same reasoning drives the one piece of friction in the API. A fee paid in
an asset that is neither side of the pair -- overwhelmingly BNB, because of the
fee discount -- cannot be valued without a price this module does not have.
`tests/test_roundtrip.py` contains a case named "fees can flip the sign", and
it does: quietly treating an unvaluable fee as zero turns losing trades into
winners in the profile. So the parser raises and tells the caller to pass
`fee_prices={"BNB": ...}`. One explicit number beats a silent understatement.

Other people's exports have to survive this module, so it is tolerant about
*shape* and strict about *meaning*. It detects the delimiter (Binance writes
commas; a file round-tripped through a European Excel comes back
semicolon-delimited), honours a byte-order mark in any of the encodings that
carry one, reads scientific notation, and scrubs the invisible characters a
spreadsheet leaves behind. None of that is guessing: each is a fact the file
states about itself. What it still refuses is anything that would require
inventing a *value* -- a decimal comma it cannot tell from a thousands
separator, a codepage with no BOM to name it, a row too short to carry the fee
it claims to have. Those raise, naming the line.

What this adapter does not do: it does not read funding payments, interest, or
transfers (those live in other Binance exports and belong in `CashFlow`), and
it does not reconcile against the exchange's own `Realized Profit`. That figure
is carried through untouched in `Fill.meta` so a future reconciliation pass can
use it, but round-trip PnL is recomputed FIFO -- see the note in `roundtrip.py`
about why lineage is worth more here than tying out to the cent.
"""

from __future__ import annotations

import csv
import io
import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from ..model.events import Fill, History, Side, Venue

Dialect = Literal["spot", "futures"]


class CsvFormatError(ValueError):
    """A header or a row we refuse to guess at.

    Carries `row` (the CSV line number, so it can be found in a text editor)
    and `value` (the cell that defeated the parse) as attributes, because an
    error a user cannot act on is only marginally better than a silent skip.
    """

    def __init__(self, message: str, *, row: int | None = None, value: object | None = None) -> None:
        self.row = row
        self.value = value
        prefix = f"row {row}: " if row is not None else ""
        suffix = f" -- offending value: {value!r}" if value is not None else ""
        super().__init__(f"{prefix}{message}{suffix}")


# Column aliases, canonical name first. Binance has renamed these across
# exports and locales (and the futures export uses "Symbol" where spot uses
# "Pair"), so matching is by alias set rather than by position -- a positional
# parser silently mis-reads a file that gained a column, which is exactly the
# failure mode this module exists to prevent.
_SPOT_COLUMNS: dict[str, tuple[str, ...]] = {
    "ts": ("date(utc)", "date", "date (utc)", "date_utc", "utc_time", "time", "date(utc+0)"),
    "symbol": ("pair", "market"),
    "side": ("side", "type"),
    "price": ("price", "average price", "avg price"),
    "qty": ("executed", "executed qty", "executed amount", "filled"),
    "amount": ("amount", "total"),
    "fee": ("fee", "fees", "commission", "trading fee"),
}

_FUTURES_COLUMNS: dict[str, tuple[str, ...]] = {
    "ts": ("date(utc)", "date", "date (utc)", "date_utc", "utc_time", "time", "date(utc+0)"),
    "symbol": ("symbol", "pair", "contract"),
    "side": ("side", "type"),
    "price": ("price", "average price", "avg price"),
    "qty": ("quantity", "qty", "executed", "size", "amount(base)"),
    "amount": ("amount", "total", "notional", "quote quantity"),
    "fee": ("fee", "fees", "commission", "trading fee"),
    "realized": ("realized profit", "realized pnl", "realized p&l", "realised profit", "profit"),
    # Optional. Absent on some futures exports; blank on others.
    "leverage": ("leverage", "lev"),
    "margin_mode": ("margin mode", "margin_mode", "margin type", "margin"),
}

_SPOT_REQUIRED = ("ts", "symbol", "side", "price", "qty", "amount", "fee")
_FUTURES_REQUIRED = ("ts", "symbol", "side", "price", "qty", "fee", "realized")

_ALL_ALIASES = {a for aliases in _SPOT_COLUMNS.values() for a in aliases} | {
    a for aliases in _FUTURES_COLUMNS.values() for a in aliases
}

# Quote assets we can peel off the end of a concatenated pair when the export
# does not tell us which half is which. Longest match wins, so BTCUSDT resolves
# to (BTC, USDT) rather than (BTCUSD, T)-adjacent nonsense.
_QUOTE_ASSETS = (
    "USDT", "FDUSD", "USDC", "BUSD", "TUSD", "DAI", "USDP", "USD",
    "BTC", "ETH", "BNB", "TRX", "XRP", "SOL", "DOGE",
    "EUR", "TRY", "GBP", "BRL", "ARS", "JPY", "AUD", "RUB", "UAH", "PLN", "RON", "ZAR",
)

# Fees are paid in one of three places: the quote asset, the base asset, or BNB.
# Only the first is already in the units the profile is denominated in.
_STABLES = frozenset({"USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI", "USDP", "USD"})

# The exponent group sits *before* the asset group and the engine is greedy, so
# `1e-8ETH` reads as (1e-8, ETH) while `2ETH` still reads as (2, ETH) -- the
# exponent alternative fails on "TH" and backtracks. Exports touched by Excel
# write small prices as `1.2E-05`, and refusing those loses every SHIB-class
# fill in the file.
#
# The asset group is unicode-aware (`[^\W\d_]` is "a letter, in any script").
# Binance's own tickers are ASCII, but a cell that carries a non-ASCII letter is
# still a *name*, and a name we can carry through untouched is not a reason to
# refuse an otherwise perfectly readable number.
_NUMBER_WITH_ASSET = re.compile(
    r"^\s*([+-]?(?:\d{1,3}(?:,\d{3})*|\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    r"\s*([^\W\d_][\w.]*)?\s*$",
    re.UNICODE,
)

# Characters that are never data: C0/C1 controls (a stray NUL turns BTCUSDT into
# a symbol that matches no quote asset and produces a baffling fee error rather
# than a parse failure), zero-width marks, and the BOM when it appears mid-file.
_ZERO_WIDTH = "\u200b\u200c\u200d\u2060\ufeff"
# Non-breaking and typographic spaces used as *thousands separators* by exports
# written in a European locale: `3\u00a0200`. Removing them outright is right --
# they only ever appear between digit groups, never inside an asset name.
_INVISIBLE_SPACE = "\u00a0\u202f\u2009\u2007"

# A number whose only comma is followed by one or two digits: `3200,50`, never
# a thousands group (which is always exactly three).
_DECIMAL_COMMA = re.compile(r"^\s*[+-]?\d+(?:[.,]\d{3})*,\d{1,2}\s*[^\d]*$")

# Timestamp layouts seen in exports. `fromisoformat` is tried first and handles
# the fractional-second variants; these cover the rest.
_TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S")

# Margin-mode words that legitimately appear in the column we look at for
# leverage. They are position metadata, not a multiplier.
_MARGIN_MODES = frozenset({"cross", "isolated", "cross margin", "isolated margin"})


def _norm(cell: str) -> str:
    """Header cells, flattened to something we can match on."""
    return " ".join(cell.replace("\ufeff", "").strip().lower().split())


def _match_header(raw: Sequence[str]) -> tuple[dict[str, int | None], Dialect]:
    """Work out which export this is, or refuse and say what we saw."""
    header = [_norm(c) for c in raw]
    # First occurrence wins: some exports repeat a column name (a second "Fee"
    # for the fee asset), and the first is the one carrying the number.
    index: dict[str, int] = {}
    for i, name in enumerate(header):
        index.setdefault(name, i)

    def resolve(spec: dict[str, tuple[str, ...]]) -> dict[str, int | None]:
        return {field: next((index[a] for a in aliases if a in index), None)
                for field, aliases in spec.items()}

    futures = resolve(_FUTURES_COLUMNS)
    # `realized` is the discriminator: it exists only on futures exports, and
    # every futures export has it. Checking it first stops a futures file being
    # read as spot because both happen to carry Price and Fee.
    if futures["realized"] is not None and all(futures[f] is not None for f in _FUTURES_REQUIRED):
        return futures, "futures"

    spot = resolve(_SPOT_COLUMNS)
    if all(spot[f] is not None for f in _SPOT_REQUIRED):
        return spot, "spot"

    unrecognised = [raw[i] for i, name in enumerate(header) if name not in _ALL_ALIASES]
    missing_spot = [_SPOT_COLUMNS[f][0] for f in _SPOT_REQUIRED if spot[f] is None]
    missing_fut = [_FUTURES_COLUMNS[f][0] for f in _FUTURES_REQUIRED if futures[f] is None]
    raise CsvFormatError(
        "unrecognised Binance export. "
        f"Unrecognised columns: {unrecognised}. "
        f"Missing for a spot export: {missing_spot}. "
        f"Missing for a futures export: {missing_fut}. "
        "Expected spot 'Date(UTC),Pair,Side,Price,Executed,Amount,Fee', or a futures "
        "export carrying 'Quantity' and 'Realized Profit'"
    )


def _scrub(cell: str) -> str:
    """Drop characters that are formatting or corruption, never data.

    Controls and zero-width marks are removed because they are invisible: a NUL
    glued into `BTCUSDT` does not look wrong in any editor, but it stops the
    pair splitting, which surfaces two steps later as a fee error naming an
    asset the user can see is correct. Invisible spaces are removed because
    European exports use them to group thousands.
    """
    if not cell:
        return ""
    out = []
    for ch in cell:
        if ch in _INVISIBLE_SPACE or ch in _ZERO_WIDTH:
            continue
        # Cf is zero-width formatting; Cc is a control character. Tab and
        # newline inside a quoted field are neither data nor worth keeping.
        if unicodedata.category(ch) in ("Cc", "Cf"):
            continue
        out.append(ch)
    return "".join(out).strip()


def _cell(row: Sequence[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return _scrub(row[idx])


def _quantity(raw: str, *, field: str, line: int, required: bool = True) -> tuple[float, str | None]:
    """Split `0.5ETH` into (0.5, "ETH"). Bare numbers return asset `None`."""
    if not raw:
        if required:
            raise CsvFormatError(f"{field} is empty", row=line)
        return 0.0, None
    m = _NUMBER_WITH_ASSET.match(raw)
    if not m or m.group(1) in ("", "+", "-"):
        # A decimal comma (`3200,50`) is the single most likely reason a
        # perfectly valid European export fails here, and it is indistinguishable
        # from a thousands separator without knowing the locale -- so say so
        # rather than let the user stare at a number that looks fine.
        hint = ""
        if _DECIMAL_COMMA.match(raw):
            hint = (
                " -- this looks like a decimal comma; the file was probably exported "
                "in a locale that writes 1.234,56. Re-export with an English locale, "
                "or convert the decimal separator to '.'"
            )
        raise CsvFormatError(f"cannot read {field} as a number{hint}", row=line, value=raw)
    number, asset = m.group(1), m.group(2)
    value = float(number.replace(",", ""))
    # `float("1e400")` is `inf`, silently. An infinite price sails through
    # `Fill`'s non-negative check and poisons every average it touches
    # downstream, so it has to die here where the row number is still known.
    if not math.isfinite(value):
        raise CsvFormatError(
            f"{field} is not a finite number (it overflows to {value})", row=line, value=raw
        )
    return value, (asset.upper() if asset else None)


def _timestamp(raw: str, *, line: int) -> datetime:
    """Parse the export's UTC wall clock into an aware datetime.

    The file writes `2026-03-01 04:12:33` and says UTC only in the column
    header. Attaching the *local* zone instead -- which is what a naive
    `fromisoformat` plus `astimezone()` would do -- shifts every hour-of-day
    figure by the machine's offset, and the session detector reports on
    hour-of-day. A profile that blames 00:00-06:00 because the analyst sat in
    Athens is worse than useless.
    """
    if not raw:
        raise CsvFormatError("timestamp is empty", row=line)
    text = raw.strip().rstrip("Z")
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in _TS_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        raise CsvFormatError("cannot read timestamp", row=line, value=raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _side(raw: str, *, line: int) -> Side:
    """BUY/SELL, tolerating the `BUY LONG` / `SELL SHORT` futures wording."""
    text = raw.strip().upper()
    if text.startswith("BUY"):
        return Side.BUY
    if text.startswith("SELL"):
        return Side.SELL
    raise CsvFormatError("side is neither BUY nor SELL", row=line, value=raw)


def _leverage(raw: str, *, line: int) -> float | None:
    """`20x` -> 20.0. Blank, or a margin mode, means we do not know.

    `None` rather than 1.0 is deliberate and matches `events.py`: an unknown
    leverage must not be counted as an unleveraged trade by the leverage
    detector, because that would dilute the high-leverage arm with trades whose
    leverage was simply not exported.
    """
    text = raw.strip().lower()
    if not text or text in ("-", "n/a", "--"):
        return None
    if text in _MARGIN_MODES:
        return None
    value, _asset = _quantity(text.rstrip("x"), field="leverage", line=line)
    if value <= 0:
        raise CsvFormatError("leverage must be positive", row=line, value=raw)
    return value


def _split_pair(pair: str, base_hint: str | None) -> tuple[str, str]:
    """Best effort (base, quote) for a concatenated pair.

    The quantity column usually carries the base asset as a suffix, which
    settles it exactly. Failing that we peel a known quote asset off the end.
    An unresolved quote returns `""`, which only matters for fee conversion --
    and that path raises rather than assuming.
    """
    symbol = pair.replace("-", "").replace("/", "").replace("_", "").upper()
    if base_hint and symbol.startswith(base_hint) and len(symbol) > len(base_hint):
        return base_hint, symbol[len(base_hint):]
    for quote in sorted(_QUOTE_ASSETS, key=len, reverse=True):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)], quote
    return symbol, ""


def _fee_in_quote(
    amount: float,
    asset: str | None,
    *,
    base: str,
    quote: str,
    price: float,
    fee_prices: dict[str, float] | None,
    line: int,
    raw: str,
) -> float:
    """Express the fee in the pair's quote asset, or refuse.

    A negative figure (a rebate) is passed through with its sign: net PnL is
    gross minus fees, so a rebate correctly increases it.
    """
    if amount == 0:
        return 0.0
    if asset is None or (quote and asset == quote) or (not quote and asset in _STABLES):
        return amount
    if asset == base:
        # Fee taken out of what was bought: value it at the fill's own price,
        # which is exact rather than an approximation.
        return amount * price
    price_of_fee_asset = (fee_prices or {}).get(asset)
    if price_of_fee_asset is None:
        raise CsvFormatError(
            f"fee is denominated in {asset}, which is neither side of {base}{quote} and "
            f"cannot be valued from the file alone. Pass fee_prices={{'{asset}': <price in "
            f"{quote or 'the quote asset'}>}} -- treating it as zero would understate costs "
            "and can turn a losing trade into a winner in the profile",
            row=line,
            value=raw,
        )
    return amount * price_of_fee_asset


def _venue(symbol: str, dialect: Dialect) -> Venue:
    if dialect == "spot":
        return Venue.SPOT
    # COIN-margined contracts are written as BTCUSD_PERP / BTCUSD_260626; the
    # USDⓈ-M ones are plain BTCUSDT. They are separate books and reconstruct
    # separately, so the distinction has to survive ingest.
    upper = symbol.upper()
    if "_" in upper or (upper.endswith("USD") and not upper.endswith("BUSD")):
        return Venue.FUTURES_COIN
    return Venue.FUTURES_USDS


def _fill_from_row(
    row: Sequence[str],
    *,
    cols: dict[str, int | None],
    dialect: Dialect,
    line: int,
    fee_prices: dict[str, float] | None,
    id_prefix: str,
) -> Fill:
    # A row physically shorter than the header is the module's own nightmare
    # wearing a disguise. `_cell` returns "" for a column the row does not
    # reach, and an empty Fee is a legal zero -- so a truncated line silently
    # books a real fill with no cost at all, which is precisely the "quietly
    # treating an unvaluable fee as zero" failure the docstring above refuses.
    # Optional columns (leverage, margin mode) may legitimately be absent; the
    # required ones may not.
    required = _SPOT_REQUIRED if dialect == "spot" else _FUTURES_REQUIRED
    widest = max((i for f in required if (i := cols.get(f)) is not None), default=-1)
    if len(row) <= widest:
        missing = [
            (_SPOT_COLUMNS if dialect == "spot" else _FUTURES_COLUMNS)[f][0]
            for f in required
            if (i := cols.get(f)) is not None and i >= len(row)
        ]
        raise CsvFormatError(
            f"row has {len(row)} columns but the header has at least {widest + 1}; "
            f"{', '.join(missing)} would be read as empty. A truncated row books a "
            "fill with no fee, which understates costs invisibly",
            row=line,
        )

    ts = _timestamp(_cell(row, cols["ts"]), line=line)
    pair_raw = _cell(row, cols["symbol"])
    if not pair_raw:
        raise CsvFormatError("symbol is empty", row=line)
    side = _side(_cell(row, cols["side"]), line=line)
    price, _ = _quantity(_cell(row, cols["price"]), field="price", line=line)
    qty, qty_asset = _quantity(_cell(row, cols["qty"]), field="quantity", line=line)

    symbol = pair_raw.replace("-", "").replace("/", "").upper()
    base, quote = _split_pair(pair_raw, qty_asset)

    fee_raw = _cell(row, cols["fee"])
    fee_amount, fee_asset = _quantity(fee_raw, field="fee", line=line, required=False)
    fee = _fee_in_quote(
        fee_amount, fee_asset, base=base, quote=quote, price=price,
        fee_prices=fee_prices, line=line, raw=fee_raw,
    )

    meta: dict = {"row": line, "dialect": dialect}
    if quote:
        meta["quote_asset"] = quote
    if fee_asset and fee_asset != quote:
        meta["fee_asset"] = fee_asset
        meta["fee_amount"] = fee_amount

    amount_raw = _cell(row, cols.get("amount"))
    if amount_raw:
        amount, _ = _quantity(amount_raw, field="amount", line=line, required=False)
        meta["quote_amount"] = amount
        # Cross-check against price x quantity. Rounding in the export moves
        # this by fractions of a percent; a swapped or mis-detected column
        # moves it by orders of magnitude. 5% catches the second without ever
        # tripping on the first. Only checked on amounts large enough that the
        # export's rounding is not itself the dominant term.
        expected = price * qty
        if amount > 1.0 and expected > 0 and abs(amount - expected) / max(amount, expected) > 0.05:
            raise CsvFormatError(
                f"amount {amount:g} does not match price x quantity ({expected:g}); "
                "the columns are probably not what this parser thinks they are",
                row=line,
                value=amount_raw,
            )

    leverage: float | None = None
    if dialect == "futures":
        leverage = _leverage(_cell(row, cols.get("leverage")), line=line)
        margin_mode = _cell(row, cols.get("margin_mode"))
        if margin_mode and margin_mode.lower() not in ("-", "n/a"):
            meta["margin_mode"] = margin_mode
        realized_raw = _cell(row, cols.get("realized"))
        if realized_raw:
            realized, _ = _quantity(realized_raw, field="realized profit", line=line, required=False)
            # Kept, never used to compute PnL: round-trips are rebuilt FIFO so
            # that each exit keeps the lineage to the entry it closed.
            meta["realized_profit"] = realized

    try:
        return Fill(
            fill_id=f"{id_prefix}-{line:06d}",
            ts=ts,
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            fee=fee,
            venue=_venue(symbol, dialect),
            leverage=leverage,
            meta=meta,
        )
    except ValueError as exc:
        # `Fill` rejects non-positive quantities and negative prices. Those are
        # real rows in the file, so the error has to name the line, not just
        # the invariant.
        raise CsvFormatError(str(exc), row=line) from exc


def parse_rows(
    rows: Iterable[Sequence[str]],
    *,
    source: str = "binance:csv",
    fee_prices: dict[str, float] | None = None,
    id_prefix: str = "csv",
    first_line: int = 1,
) -> History:
    """Parse already-split CSV rows. The header must be the first non-blank one.

    `first_line` exists so that line numbers in errors line up with the file
    when the caller has already consumed part of it.
    """
    fills: list[Fill] = []
    cols: dict[str, int | None] | None = None
    dialect: Dialect = "spot"
    offset = first_line - 1
    counted = 0

    iterator = iter(rows)
    while True:
        try:
            row = next(iterator)
        except StopIteration:
            break
        except csv.Error as exc:
            # An unterminated quote swallows the rest of the file; a field over
            # the reader's size limit aborts mid-stream. Both arrive as a bare
            # `csv.Error` with no line number, which is exactly the traceback a
            # user cannot act on.
            raise CsvFormatError(
                f"the CSV reader could not read this line ({exc}). The file is "
                "probably truncated, or has an unclosed quote earlier in it",
                row=getattr(rows, "line_num", counted + 1) + offset,
            ) from exc
        counted += 1
        # A `csv.reader` knows the true physical line, which is what a user
        # needs to find the row in an editor; a quoted field containing a
        # newline makes that differ from the row count. Fall back to counting
        # when handed a plain list of rows.
        line = getattr(rows, "line_num", counted) + offset
        # Wholly blank lines are formatting, not fills. Every other row is
        # parsed or raises -- there is no third outcome.
        if not any(cell.strip() for cell in row):
            continue
        if cols is None:
            cols, dialect = _match_header(row)
            continue
        fills.append(_fill_from_row(
            row, cols=cols, dialect=dialect, line=line,
            fee_prices=fee_prices, id_prefix=id_prefix,
        ))

    if cols is None:
        raise CsvFormatError("empty file: no header row found")
    return History(fills=fills, source=source)


# Delimiters seen in the wild. Binance's own export is comma-delimited, but a
# file that has been round-tripped through a European Excel comes back
# semicolon-delimited, and one pasted out of a terminal comes back tabbed.
_DELIMITERS = (",", ";", "\t")

# The stdlib default (128 KB) is well under a legitimately long cell but well
# over anything Binance writes, so a corrupt quote hits it as an opaque abort.
# Ten megabytes lets a genuinely long field through and still fails loudly on a
# file whose quoting has come apart.
_FIELD_LIMIT = 10 * 1024 * 1024


# C0 controls that are never data and can break the reader outright. Tab,
# carriage return and newline are excluded -- those are structure.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _lines(text: str) -> io.StringIO:
    """The file, split the way `csv` needs it split.

    `newline=""` is not a detail: the default translates newlines, which both
    hides bare-CR line endings from the reader (an old Mac Excel writes them,
    and `csv` then dies with "new-line character seen in unquoted field") and
    rewrites a CRLF that lives *inside* a quoted field. The empty string is the
    setting the csv module's own documentation asks for.

    NUL is stripped here, before the reader sees it, rather than per-cell with
    the other invisibles. It has to be: on Python 3.10 `csv` raises
    `_csv.Error: line contains NUL` while parsing the row, so a per-cell scrub
    never runs. Python 3.11 changed that and tolerates it, which is why this was
    green locally on 3.13 and red on 3.10 in CI -- and why the version matrix
    earns its keep. The other C0 controls go too; none of them are ever data in
    a trade export, and any of them can end a field early.
    """
    if _CONTROL_RE.search(text):
        text = _CONTROL_RE.sub("", text)
    return io.StringIO(text, newline="")


def sniff_delimiter(text: str) -> str:
    """Which delimiter makes this file's header a Binance export.

    Detection, not guessing: a delimiter is only chosen because splitting on it
    produces a header that a dialect actually recognises. When none does, the
    one that produced the most columns is returned, purely so that the
    unrecognised-header error lists real column names instead of the whole line
    as a single cell.
    """
    head = text[:65536].lstrip("\ufeff")
    best, best_cols = ",", -1
    for delimiter in _DELIMITERS:
        try:
            reader = csv.reader(_lines(head), delimiter=delimiter)
            row = next((r for r in reader if any(c.strip() for c in r)), None)
        except csv.Error:
            continue
        if row is None:
            continue
        try:
            _match_header(row)
        except CsvFormatError:
            if len(row) > best_cols:
                best, best_cols = delimiter, len(row)
            continue
        return delimiter
    return best


def _decode(data: bytes, *, what: str) -> str:
    """Bytes to text, honouring a BOM and refusing to guess past that.

    A BOM is a statement of encoding, so acting on one is not a guess. Anything
    else that is not UTF-8 gets an error naming the byte, because silently
    falling back to cp1252 turns a mis-encoded symbol into a *different* symbol
    that reconstructs as its own separate position -- data loss that looks like
    a clean parse.
    """
    for bom, encoding in (
        (b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"),
        (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"),
        (b"\xef\xbb\xbf", "utf-8-sig"),
    ):
        if data.startswith(bom):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError as exc:
                raise CsvFormatError(
                    f"{what} starts with a {encoding} byte-order mark but does not decode "
                    f"as {encoding} ({exc.reason} at byte {exc.start})"
                ) from exc
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CsvFormatError(
            f"{what} is not UTF-8 ({exc.reason} at byte {exc.start}: {data[exc.start:exc.start + 4]!r}). "
            "Binance's own export is UTF-8; a file that is not has usually been re-saved by a "
            "spreadsheet in a local codepage. Re-export it, or re-save it as UTF-8 -- guessing the "
            "codepage here would silently rename symbols and split one position into two"
        ) from exc


def parse_text(
    text: str,
    *,
    source: str = "binance:csv",
    fee_prices: dict[str, float] | None = None,
    id_prefix: str = "csv",
    delimiter: str | None = None,
) -> History:
    """Parse an export held in memory. Line numbers count from 1 at the header."""
    reader = csv.reader(_lines(text), delimiter=delimiter or sniff_delimiter(text))
    previous = csv.field_size_limit()
    try:
        csv.field_size_limit(_FIELD_LIMIT)
        return parse_rows(reader, source=source, fee_prices=fee_prices, id_prefix=id_prefix)
    finally:
        csv.field_size_limit(previous)


def parse_csv(
    path: str | Path,
    *,
    source: str | None = None,
    fee_prices: dict[str, float] | None = None,
    id_prefix: str | None = None,
) -> History:
    """Read a Binance trade-history export from disk.

    `fee_prices` maps an asset symbol to its price *in the pair's quote asset*
    -- almost always USDT. Supply it only for fees paid in a third asset (BNB);
    fees in the base or quote asset are converted exactly from the fill itself.
    Passing a USD price for a book quoted in something other than a dollar
    stablecoin will be wrong, which is why there is no default.

    Raises `CsvFormatError` on anything it cannot read, naming the line.
    """
    p = Path(path)
    # Read as bytes and decode here rather than leaning on the file object's
    # encoding: exports downloaded through the web UI carry a UTF-8 BOM, ones
    # re-saved by Excel can carry a UTF-16 one, and a decode failure needs to
    # arrive as a `CsvFormatError` like every other thing this module refuses.
    return parse_text(
        _decode(p.read_bytes(), what=f"{p.name}"),
        source=source or f"binance:csv:{p.name}",
        fee_prices=fee_prices,
        id_prefix=id_prefix or "csv",
    )


def detect_dialect(header: Sequence[str]) -> Dialect:
    """Which export layout a header row belongs to. Raises if neither."""
    return _match_header(header)[1]

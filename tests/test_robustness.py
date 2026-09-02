"""Hostile and malformed input against the CSV parser and the reconstructor.

`test_ingest.py` proves the adapter reads the files Binance is *documented* to
write. This file assumes the documentation is wrong. The futures dialect was
written against column names nobody has ever seen in a real export, and the
first real file this project meets will arrive from a stranger's account, in
whatever locale, encoding and spreadsheet round-trip it has been through.

Two properties are asserted throughout, and they pull in opposite directions:

  * a malformed row raises `CsvFormatError` **naming its line number**, because
    an error a user cannot locate is barely better than a silent skip; and
  * a merely *odd* row -- a unicode ticker, a dust-sized quantity, a price in
    scientific notation -- parses, because refusing valid data is the same
    class of bug as accepting invalid data, and it is the one a parser written
    defensively is far more likely to commit.

So every case below asserts the *specific* outcome. "It raised something" is
not a passing test: a file that fails for the wrong reason sends the user
hunting for a problem they do not have.

Several sections are regressions for bugs found while writing this suite, and
are marked as such -- each one is a way a real export could have silently
produced a wrong profile.

Run: python3 tests/test_robustness.py
"""

from __future__ import annotations

import csv
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gnosis.ingest.binance_csv import (  # noqa: E402
    CsvFormatError,
    detect_dialect,
    parse_csv,
    parse_text,
    sniff_delimiter,
)
from gnosis.model.events import Fill, Side, Venue  # noqa: E402
from gnosis.model.roundtrip import DUST_FRACTION, reconstruct  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{'' if cond else '  <- ' + detail}")


def raises(exc_type, fn, *args, **kwargs):
    """Return the exception `fn` raised, or None. Keeps the checks one-liners."""
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        return exc
    except Exception as exc:  # noqa: BLE001 - a wrong exception type is a failure, not a crash
        return ("wrong-type", exc)
    return None


def refuses(fn, *, at_row=None, saying=None, value=None):
    """Assert a specific refusal: right exception, right row, right reason.

    Returns (ok, detail). The row number is checked because it is the whole
    contract of `CsvFormatError` -- a user with a 40,000-row export needs the
    line, not the sentiment.
    """
    exc = raises(CsvFormatError, fn)
    if exc is None:
        return False, "did not raise"
    if isinstance(exc, tuple):
        return False, f"raised {type(exc[1]).__name__} instead: {exc[1]}"
    if at_row is not None and exc.row != at_row:
        return False, f"named row {exc.row}, expected {at_row}: {exc}"
    if saying is not None and saying.lower() not in str(exc).lower():
        return False, f"message did not mention {saying!r}: {exc}"
    if value is not None and value not in str(exc):
        return False, f"message did not quote {value!r}: {exc}"
    return True, ""


SPOT = "Date(UTC),Pair,Side,Price,Executed,Amount,Fee\n"
FUT = ("Date(UTC),Symbol,Side,Price,Quantity,Amount,Fee,"
       "Realized Profit,Leverage,Margin Mode\n")
GOOD = "2026-03-01 12:00:00,ETHUSDT,BUY,3200,2ETH,6400USDT,1USDT\n"

TMP = Path(tempfile.mkdtemp(prefix="gnosis-robustness-"))


def write(name: str, content, encoding: str | None = "utf-8") -> Path:
    """Materialise a fixture on disk. Bytes go through untouched."""
    p = TMP / name
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_bytes(content.encode(encoding))
    return p


# ==========================================================================
print("=== shapes a file can arrive in ===")

check("empty file refuses, does not return an empty history",
      *refuses(lambda: parse_text(""), saying="empty file"))
check("whitespace-only file refuses",
      *refuses(lambda: parse_text("\n\n   \n"), saying="empty file"))
check("header alone is an empty history, not an error",
      parse_text(SPOT).fills == [])

bom = parse_text("﻿" + SPOT + GOOD)
check("BOM-prefixed header still matches", len(bom.fills) == 1, f"got {len(bom.fills)}")
check("BOM does not glue itself to the symbol", bom.fills[0].symbol == "ETHUSDT",
      bom.fills[0].symbol)

crlf = parse_text((SPOT + GOOD).replace("\n", "\r\n"))
check("CRLF line endings parse", len(crlf.fills) == 1, f"got {len(crlf.fills)}")
check("CRLF leaves no carriage return in the symbol", "\r" not in crlf.fills[0].symbol)

cr_only = parse_text((SPOT + GOOD).replace("\n", "\r"))
check("bare-CR (classic Mac) line endings parse", len(cr_only.fills) == 1,
      f"got {len(cr_only.fills)}")

trailing = parse_text(SPOT + GOOD + "\n\n   \n\n")
check("trailing blank lines are not fills", len(trailing.fills) == 1,
      f"got {len(trailing.fills)}")

interior = parse_text(SPOT + GOOD + "\n" + GOOD.replace("12:00", "13:00"))
check("a blank line between fills is skipped, not fatal", len(interior.fills) == 2,
      f"got {len(interior.fills)}")

stray = parse_text(SPOT.rstrip("\n") + ",\n" + GOOD.rstrip("\n") + ",\n")
check("stray trailing comma column parses", len(stray.fills) == 1, f"got {len(stray.fills)}")
check("stray trailing column did not shift the mapping",
      stray.fills[0].symbol == "ETHUSDT" and abs(stray.fills[0].price - 3200) < 1e-9,
      f"{stray.fills[0].symbol} @ {stray.fills[0].price}")

# A header wider than its rows is the shape a stray trailing comma leaves; a
# row narrower than its *required* columns is truncation, and must refuse.
check("header with a trailing comma but full rows still parses",
      len(parse_text(SPOT.rstrip('\n') + ',\n' + GOOD).fills) == 1)


# ==========================================================================
print("\n=== delimiters and quoting ===")

semi = parse_text(SPOT.replace(",", ";") + GOOD.replace(",", ";"))
check("semicolon-delimited export parses", len(semi.fills) == 1, f"got {len(semi.fills)}")
check("semicolon export reads the same numbers",
      abs(semi.fills[0].price - 3200) < 1e-9 and abs(semi.fills[0].qty - 2) < 1e-12,
      f"{semi.fills[0].price} {semi.fills[0].qty}")
check("sniffer names the semicolon", sniff_delimiter(SPOT.replace(",", ";")) == ";",
      sniff_delimiter(SPOT.replace(",", ";")))
check("sniffer leaves a comma file alone", sniff_delimiter(SPOT) == ",")

tabbed = parse_text(SPOT.replace(",", "\t") + GOOD.replace(",", "\t"))
check("tab-delimited export parses", len(tabbed.fills) == 1, f"got {len(tabbed.fills)}")

# A semicolon file whose *values* contain commas is the European export in
# full: `;` separates, `,` is the decimal point. The first half must work even
# though the second half is refused below.
semi_bom = parse_text("﻿" + SPOT.replace(",", ";").replace("\n", "\r\n")
                      + GOOD.replace(",", ";").replace("\n", "\r\n"))
check("BOM + CRLF + semicolon together parse", len(semi_bom.fills) == 1,
      f"got {len(semi_bom.fills)}")

quoted = parse_text(SPOT + '2026-03-01 12:00:00,ETHUSDT,BUY,3200,0.5ETH,"1,600USDT",1USDT\n')
check("quoted field containing a comma parses",
      quoted.fills[0].meta["quote_amount"] == 1600.0, str(quoted.fills[0].meta))
check("quoted thousands separator did not become 1.6",
      quoted.fills[0].meta["quote_amount"] > 1000, str(quoted.fills[0].meta))

slashed = parse_text(SPOT + '2026-03-01 12:00:00,"ETH/USDT",BUY,3200,2ETH,6400USDT,1USDT\n')
check("a quoted, slash-separated pair normalises to ETHUSDT",
      slashed.fills[0].symbol == "ETHUSDT", slashed.fills[0].symbol)
check("...and its quote asset still resolves, so the fee is not refused",
      slashed.fills[0].fee == 1.0, f"got {slashed.fills[0].fee}")

# A quoted field holding the delimiter must not shift every column after it.
shifted = parse_text(SPOT + '2026-03-01 12:00:00,ETHUSDT,BUY,3200,2ETH,"6,400USDT",1USDT\n')
check("a comma inside a quoted field does not shift the columns",
      abs(shifted.fills[0].price - 3200) < 1e-9 and shifted.fills[0].fee == 1.0,
      f"{shifted.fills[0].price} fee={shifted.fills[0].fee}")

# An unclosed quote swallows the rest of the file. It must be a CsvFormatError
# with a line number, not a bare csv.Error from three frames down.
ok, detail = refuses(lambda: parse_text(SPOT + '2026-03-01 12:00:00,"ETHUSDT,BUY,3200,2ETH\n'))
check("an unclosed quote refuses with a row number", ok, detail)


# ==========================================================================
print("\n=== encodings (regression: these used to escape as UnicodeDecodeError) ===")

utf16 = write("utf16.csv", (SPOT + GOOD).encode("utf-16"))
check("UTF-16 file with a BOM parses", len(parse_csv(utf16).fills) == 1)

utf8bom = write("utf8bom.csv", b"\xef\xbb\xbf" + (SPOT + GOOD).encode("utf-8"))
check("UTF-8 BOM file parses", len(parse_csv(utf8bom).fills) == 1)

latin = write("latin1.csv", (SPOT + GOOD).encode("utf-8").replace(b"ETHUSDT", b"ETH\xe9USDT", 1))
ok, detail = refuses(lambda: parse_csv(latin), saying="UTF-8")
check("a non-UTF-8 file refuses as a CsvFormatError, not a UnicodeDecodeError", ok, detail)
err = raises(CsvFormatError, parse_csv, latin)
check("the encoding error says what to do about it",
      isinstance(err, CsvFormatError) and "re-save" in str(err).lower(), str(err)[:90])

check("a missing file is still an OSError, not a format error",
      isinstance(raises(CsvFormatError, parse_csv, TMP / "nope.csv"), tuple))


# ==========================================================================
print("\n=== odd but valid: these must parse, not raise ===")

uni = parse_text(SPOT + "2026-03-01 12:00:00,DÖGEUSDT,BUY,1,2DÖGE,2USDT,0.1USDT\n")
check("unicode ticker parses", len(uni.fills) == 1, f"got {len(uni.fills)}")
check("unicode ticker survives intact", uni.fills[0].symbol == "DÖGEUSDT", uni.fills[0].symbol)
check("unicode asset suffix stripped from the quantity", abs(uni.fills[0].qty - 2.0) < 1e-12,
      f"got {uni.fills[0].qty}")

cyr = parse_text(SPOT + "2026-03-01 12:00:00,БТСUSDT,BUY,1,2БТС,2USDT,0USDT\n")
check("cyrillic ticker parses", len(cyr.fills) == 1, f"got {len(cyr.fills)}")

long_symbol = "A" * 200_000
long_field = parse_text(SPOT + f"2026-03-01 12:00:00,{long_symbol}USDT,BUY,1,2{long_symbol},"
                               "2USDT,0USDT\n")
check("a 200k-character field parses rather than aborting the reader",
      len(long_field.fills) == 1, f"got {len(long_field.fills)}")
check("the long field is carried through, not truncated",
      len(long_field.fills[0].symbol) == 200_004, f"got {len(long_field.fills[0].symbol)}")

# Excel writes small prices in scientific notation. Refusing them loses every
# SHIB-class fill in the file -- silently, because the *file* looks fine.
sci = parse_text(SPOT + "2026-03-01 12:00:00,SHIBUSDT,BUY,1.2E-05,1000000SHIB,12USDT,0USDT\n")
check("scientific-notation price parses", abs(sci.fills[0].price - 1.2e-5) < 1e-18,
      f"got {sci.fills[0].price}")
sci_q = parse_text(SPOT + "2026-03-01 12:00:00,BTCUSDT,BUY,68000,1e-8BTC,0.00068USDT,0USDT\n")
check("scientific-notation quantity with an asset suffix parses",
      abs(sci_q.fills[0].qty - 1e-8) < 1e-20, f"got {sci_q.fills[0].qty}")
check("the exponent did not eat the asset suffix",
      sci_q.fills[0].meta.get("quote_asset") == "USDT", str(sci_q.fills[0].meta))
check("a plain quantity is still not read as an exponent",
      abs(parse_text(SPOT + GOOD).fills[0].qty - 2.0) < 1e-12)
check("an asset starting with E is not read as an exponent",
      abs(parse_text(SPOT + "2026-03-01 12:00:00,ETHEUR,BUY,3000,2ETH,6000EUR,1EUR\n")
          .fills[0].fee - 1.0) < 1e-9)

nbsp = parse_text(SPOT + "2026-03-01 12:00:00,ETHUSDT,BUY,3 200,2ETH,6400USDT,1USDT\n")
check("non-breaking space as a thousands separator parses",
      abs(nbsp.fills[0].price - 3200.0) < 1e-9, f"got {nbsp.fills[0].price}")

precise = parse_text(SPOT + "2026-03-01 12:00:00,ETHUSDT,BUY,"
                            "3200.12345678901234567890,2ETH,6400.24USDT,1USDT\n")
check("more precision than a float carries does not raise",
      abs(precise.fills[0].price - 3200.123456789012) < 1e-6, f"got {precise.fills[0].price}")

tiny = parse_text(SPOT + "2026-03-01 12:00:00,BTCUSDT,BUY,68000,0.00000001BTC,0.00068USDT,0USDT\n")
check("dust-sized quantity parses rather than being rejected as zero",
      tiny.fills[0].qty > 0, f"got {tiny.fills[0].qty}")

zero_price = parse_text(SPOT + "2026-03-01 12:00:00,ETHUSDT,BUY,0,2ETH,0USDT,0USDT\n")
check("zero price parses (an airdrop or a fee-only line is priced at zero)",
      zero_price.fills[0].price == 0.0)

rebate = parse_text(SPOT + "2026-03-01 12:00:00,ETHUSDT,BUY,3200,2ETH,6400USDT,-1USDT\n")
check("a negative fee (a rebate) keeps its sign", rebate.fills[0].fee == -1.0,
      f"got {rebate.fills[0].fee}")

blank_fee = parse_text(SPOT + "2026-03-01 12:00:00,ETHUSDT,BUY,3200,2ETH,6400USDT,\n")
check("an explicitly empty fee cell is zero, not an error", blank_fee.fills[0].fee == 0.0)


# ==========================================================================
print("\n=== numeric edges that must refuse, with the right reason ===")

for label, row, row_no, saying in (
    ("zero quantity", "2026-03-01 12:00:00,ETHUSDT,BUY,3200,0ETH,0USDT,0USDT", 2, "positive"),
    ("negative quantity", "2026-03-01 12:00:00,ETHUSDT,BUY,3200,-2ETH,6400USDT,0USDT", 2, "positive"),
    ("negative price", "2026-03-01 12:00:00,ETHUSDT,BUY,-3200,2ETH,6400USDT,0USDT", 2, "non-negative"),
    ("NaN price", "2026-03-01 12:00:00,ETHUSDT,BUY,nan,2ETH,6400USDT,0USDT", 2, "number"),
    ("infinite price", "2026-03-01 12:00:00,ETHUSDT,BUY,inf,2ETH,6400USDT,0USDT", 2, "number"),
    ("a lone decimal point", "2026-03-01 12:00:00,ETHUSDT,BUY,.,2ETH,6400USDT,0USDT", 2, "number"),
    ("empty price", "2026-03-01 12:00:00,ETHUSDT,BUY,,2ETH,6400USDT,0USDT", 2, "empty"),
    ("empty symbol", "2026-03-01 12:00:00,,BUY,3200,2ETH,6400USDT,0USDT", 2, "symbol"),
):
    ok, detail = refuses(lambda r=row: parse_text(SPOT + r + "\n"), at_row=row_no, saying=saying)
    check(f"{label} refuses at row {row_no} saying {saying!r}", ok, detail)

# Regression: `float("1e400")` is `inf`, and an infinite price sails straight
# through Fill's non-negative check to poison every average downstream.
ok, detail = refuses(
    lambda: parse_text(SPOT + "2026-03-01 12:00:00,ETHUSDT,BUY,1e400,2ETH,6400USDT,0USDT\n"),
    at_row=2, saying="finite")
check("an exponent that overflows to inf refuses rather than becoming inf", ok, detail)

# Regression: a European decimal comma is the likeliest way a valid export
# fails, and "cannot read as a number" sends the user hunting for corruption.
err = raises(CsvFormatError, parse_text,
             SPOT + '2026-03-01 12:00:00,ETHUSDT,BUY,"3200,50",2ETH,6401USDT,0USDT\n')
check("a decimal comma refuses", isinstance(err, CsvFormatError), repr(err))
check("...and the message diagnoses the locale rather than blaming the number",
      isinstance(err, CsvFormatError) and "decimal comma" in str(err).lower(), str(err)[:100])

# The line number is the whole contract. Check it survives past row 2.
bulk = SPOT + GOOD * 500 + "2026-03-01 12:00:00,ETHUSDT,BUY,wat,2ETH,6400USDT,0USDT\n"
ok, detail = refuses(lambda: parse_text(bulk), at_row=502, value="'wat'")
check("row number is right 500 rows in, and the value is quoted", ok, detail)


# ==========================================================================
print("\n=== regression: a truncated row used to book a free fill ===")

# `_cell` returns "" for a column the row does not reach, and an empty Fee is a
# legal zero -- so a row cut short silently produced a real fill with no cost.
# That is the exact failure the module's docstring says it exists to prevent.
ok, detail = refuses(
    lambda: parse_text(SPOT + "2026-03-01 12:00:00,ETHUSDT,BUY,3200,2ETH,6400USDT\n"),
    at_row=2, saying="fee")
check("a row missing its Fee column refuses instead of charging zero", ok, detail)
ok, detail = refuses(
    lambda: parse_text(SPOT + "2026-03-01 12:00:00,ETHUSDT,BUY\n"), at_row=2, saying="columns")
check("a badly truncated row names how many columns it had", ok, detail)
ok, detail = refuses(
    lambda: parse_text(FUT + "2026-03-01 12:00:00,BTCUSDT,BUY,67000,0.1BTC,6700USDT,2.68USDT\n"),
    at_row=2, saying="realized profit")
check("a futures row missing Realized Profit refuses", ok, detail)
# ...but genuinely optional futures columns may be absent without complaint.
partial_fut = parse_text(FUT + "2026-03-01 12:00:00,BTCUSDT,BUY,67000,0.1BTC,6700USDT,"
                               "2.68USDT,0USDT\n")
check("an absent optional Leverage column is not an error",
      len(partial_fut.fills) == 1 and partial_fut.fills[0].leverage is None,
      str(partial_fut.fills))


# ==========================================================================
print("\n=== regression: invisible characters used to corrupt a symbol ===")

# A NUL glued into BTCUSDT stops the pair splitting, which surfaced two steps
# later as a fee error naming an asset the user can plainly see is correct.
nul = write("nul.csv", b"Date(UTC),Pair,Side,Price,Executed,Amount,Fee\n"
                       b"2026-03-01 12:00:00,ETH\x00USDT,BUY,3200,2ETH,6400USDT,1USDT\n")
h = parse_csv(nul)
check("a NUL inside the symbol is scrubbed, not carried", h.fills[0].symbol == "ETHUSDT",
      repr(h.fills[0].symbol))
check("...and the fee still resolves against the real quote asset", h.fills[0].fee == 1.0,
      f"got {h.fills[0].fee}")
zwsp = parse_text(SPOT + "2026-03-01 12:00:00,ETH​USDT,BUY,3200,2ETH,6400USDT,1USDT\n")
check("a zero-width space inside the symbol is scrubbed",
      zwsp.fills[0].symbol == "ETHUSDT", repr(zwsp.fills[0].symbol))


# ==========================================================================
print("\n=== timestamps ===")

far_future = parse_text(SPOT + "9999-12-31 23:59:59,ETHUSDT,BUY,3200,2ETH,6400USDT,0USDT\n")
check("a year-9999 timestamp parses rather than overflowing",
      far_future.fills[0].ts.year == 9999, str(far_future.fills[0].ts))
far_past = parse_text(SPOT + "1970-01-01 00:00:00,ETHUSDT,BUY,3200,2ETH,6400USDT,0USDT\n")
check("an epoch-zero timestamp parses", far_past.fills[0].ts.year == 1970,
      str(far_past.fills[0].ts))
check("both are timezone-aware",
      far_future.fills[0].ts.tzinfo is not None and far_past.fills[0].ts.tzinfo is not None)

out_of_order = parse_text(
    SPOT
    + "2026-03-05 08:00:00,ETHUSDT,SELL,3300,2ETH,6600USDT,0USDT\n"
    + "2026-03-01 12:00:00,ETHUSDT,BUY,3200,2ETH,6400USDT,0USDT\n")
check("out-of-order rows are sorted into time order",
      out_of_order.fills[0].side is Side.BUY, str([f.side for f in out_of_order.fills]))
check("...and the line numbers still point at the original rows",
      out_of_order.fills[0].meta["row"] == 3, str(out_of_order.fills[0].meta))
check("span is computed from the sorted ends, never negative",
      out_of_order.span_days > 0, f"got {out_of_order.span_days}")

for text, expect in (
    ("2026-03-01T12:00:00", 12), ("2026-03-01 12:00:00.123", 12),
    ("2026-03-01 12:00:00Z", 12), ("2026-03-01 12:00:00+02:00", 10),
    ("2026-03-01 12:00", 12), ("01/03/2026 12:00:00", 12),
):
    got = parse_text(SPOT + f"{text},ETHUSDT,BUY,3200,2ETH,6400USDT,0USDT\n").fills[0]
    check(f"timestamp {text!r} -> {expect:02d}:00 UTC", got.hour_utc == expect,
          f"got {got.ts}")

# A DST-ambiguous local wall clock. The export writes UTC and says so only in
# the column header, so the *only* correct reading is UTC -- and it must not
# depend on where the machine running this is sitting. 2026-10-25 02:30 is
# inside the repeated hour in most of Europe; read as local time it is
# genuinely ambiguous, read as UTC it is not.
AMBIGUOUS = "2026-10-25 02:30:00,ETHUSDT,BUY,3200,2ETH,6400USDT,0USDT\n"
seen = {}
original_tz = os.environ.get("TZ")
try:
    for tz in ("UTC", "Europe/Athens", "America/Santiago", "Australia/Lord_Howe"):
        os.environ["TZ"] = tz
        if hasattr(time, "tzset"):
            time.tzset()
        seen[tz] = parse_text(SPOT + AMBIGUOUS).fills[0].ts
finally:
    if original_tz is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original_tz
    if hasattr(time, "tzset"):
        time.tzset()

check("a DST-ambiguous wall clock reads identically in every machine timezone",
      len(set(seen.values())) == 1, str(seen))
check("...and it reads as UTC, not as the machine's local zone",
      seen["UTC"] == datetime(2026, 10, 25, 2, 30, tzinfo=timezone.utc), str(seen["UTC"]))
check("...so hour-of-day is not shifted by the analyst's offset",
      all(t.astimezone(timezone.utc).hour == 2 for t in seen.values()), str(seen))

ok, detail = refuses(
    lambda: parse_text(SPOT + "2026-13-45 99:99:99,ETHUSDT,BUY,3200,2ETH,6400USDT,0USDT\n"),
    at_row=2, saying="timestamp")
check("an impossible date refuses naming its row", ok, detail)


# ==========================================================================
print("\n=== identity: fill ids must stay unique and traceable ===")

dupes = parse_text(SPOT + GOOD * 5)
ids = [f.fill_id for f in dupes.fills]
check("five identical rows produce five fills", len(dupes.fills) == 5, f"got {len(dupes.fills)}")
check("...with distinct ids", len(set(ids)) == 5, str(ids))
check("...each naming its own file line",
      [f.meta["row"] for f in dupes.fills] == [2, 3, 4, 5, 6],
      str([f.meta["row"] for f in dupes.fills]))
check("id encodes the line number", ids[0] == "csv-000002", ids[0])

prefixed = parse_text(SPOT + GOOD, id_prefix="acct2")
check("id_prefix keeps two files' ids from colliding",
      prefixed.fills[0].fill_id == "acct2-000002", prefixed.fills[0].fill_id)

# Duplicate ids are not something the parser can produce, but a caller merging
# two files with the same prefix can. The reconstructor must not choke.
T0 = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
same_id = [
    Fill(fill_id="dup", ts=T0, symbol="ETHUSDT", side=Side.BUY, qty=1, price=100),
    Fill(fill_id="dup", ts=T0 + timedelta(minutes=1), symbol="ETHUSDT", side=Side.SELL,
         qty=1, price=110),
]
closed, _ = reconstruct(same_id)
check("duplicate fill ids do not break reconstruction",
      len(closed) == 1 and abs(closed[0].gross_pnl - 10) < 1e-9, str(closed))


# ==========================================================================
print("\n=== scale ===")

N = 50_000
big = SPOT + "".join(
    f"2026-03-{1 + i % 28:02d} 12:00:00,ETHUSDT,{'BUY' if i % 2 == 0 else 'SELL'},"
    f"{3000 + i % 100},0.001ETH,{3.0 + (i % 100) / 1000:.3f}USDT,0.001USDT\n"
    for i in range(N))
started = time.time()
huge = parse_text(big)
parse_seconds = time.time() - started
check(f"{N:,} rows parse", len(huge.fills) == N, f"got {len(huge.fills)}")
check(f"...in reasonable time ({parse_seconds:.2f}s)", parse_seconds < 30.0,
      f"took {parse_seconds:.1f}s")
check("...with every id distinct", len(({f.fill_id for f in huge.fills})) == N)

started = time.time()
closed, still_open = reconstruct(huge.fills)
recon_seconds = time.time() - started
check(f"{N:,} fills reconstruct in reasonable time ({recon_seconds:.2f}s)",
      recon_seconds < 30.0, f"took {recon_seconds:.1f}s")
check("reconstruction of 50k alternating fills closes trips",
      len(closed) > 0, f"got {len(closed)}")

big_file = write("big.csv", big)
started = time.time()
from_disk = parse_csv(big_file)
check(f"the same file parses from disk in reasonable time "
      f"({time.time() - started:.2f}s)", len(from_disk.fills) == N,
      f"got {len(from_disk.fills)}")


# ==========================================================================
print("\n=== reconstructor: positions that never behave ===")

_seq = [0]


def f(side, qty, price, *, mins=0, symbol="ETHUSDT", venue=Venue.SPOT, fee=0.0, ts=None):
    _seq[0] += 1
    return Fill(fill_id=f"r{_seq[0]:06d}", ts=ts or (T0 + timedelta(minutes=mins)),
                symbol=symbol, side=side, qty=qty, price=price, fee=fee, venue=venue)


closed, still_open = reconstruct([f(Side.BUY, 1, 100)])
check("a single fill that never closes yields no closed trips", closed == [], str(closed))
check("...and exactly one open trip", len(still_open) == 1, f"got {len(still_open)}")
check("...flagged open, with no close time",
      still_open[0].is_open and still_open[0].closed_at is None, str(still_open[0]))
check("...whose hold time is 0, not negative", still_open[0].hold_seconds == 0.0,
      str(still_open[0].hold_seconds))

# Scaling out in equal slices, where each slice is larger than the dust
# threshold, must close exactly: no residue, no drift, no phantom position.
EXACT = 90
assert 1.0 / EXACT > DUST_FRACTION, "this case must not trip the dust sweep"
scaled_out = [f(Side.BUY, 1.0, 100.0)] + [
    f(Side.SELL, 1.0 / EXACT, 110.0, mins=i + 1) for i in range(EXACT)]
closed, still_open = reconstruct(scaled_out)
check(f"{EXACT} equal partial closes collapse to one trip", len(closed) == 1,
      f"got {len(closed)} closed, {len(still_open)} open")
check("...leaving nothing open", still_open == [], str(len(still_open)))
check("...with PnL accurate to a cent", abs(closed[0].gross_pnl - 10.0) < 0.01,
      f"got {closed[0].gross_pnl}")
check("...average exit not drifted by 90 running recomputations",
      abs(closed[0].avg_exit - 110.0) < 1e-9, f"got {closed[0].avg_exit}")
check("...and every partial counted but the last",
      closed[0].n_partial_exits == EXACT - 1, f"got {closed[0].n_partial_exits}")
check("...with no dust abandoned", closed[0].dust_qty == 0.0, f"got {closed[0].dust_qty}")

# Thousands of slices each far *below* the dust threshold. The reconstructor
# sweeps the residue once the open quantity falls under DUST_FRACTION of what
# was opened -- deliberately, because a real spot book pays its fee in the base
# asset and never reaches exactly zero. What must hold regardless of where that
# threshold sits is conservation: every unit sold is either matched against the
# long, abandoned as recorded dust, or re-opened on the other side. Nothing may
# simply vanish.
TINY = 5_000
partials = [f(Side.BUY, 1.0, 100.0)] + [
    f(Side.SELL, 1.0 / TINY, 110.0, mins=i + 1) for i in range(TINY)]
closed, still_open = reconstruct(partials)
check(f"{TINY:,} sub-dust partial closes still close the long exactly once",
      len(closed) == 1 and closed[0].direction is Side.BUY,
      f"got {len(closed)} closed, {len(still_open)} open")
long_trip = closed[0]
check("...matching all but the swept dust against the entry",
      abs(long_trip.qty_opened - 1.0) < 1e-9, f"got {long_trip.qty_opened}")
# The dust threshold is a fraction of the *closing fill*, not of the cumulative
# quantity opened. Under the old cumulative rule these 5,000 slices tripped the
# sweep partway through and abandoned ~1% of a whole BTC; under the current rule
# each slice is far too small to sweep anything, so the position closes exactly
# and nothing is abandoned. Either is acceptable to this test -- what is not
# acceptable is quantity going missing, which the conservation check below pins.
check("...abandoning at most a dust-sized residue, and recording it if so",
      0 <= long_trip.dust_qty <= DUST_FRACTION, f"got {long_trip.dust_qty}")
check("...with an average exit that has not drifted",
      abs(long_trip.avg_exit - 110.0) < 1e-6, f"got {long_trip.avg_exit}")
check("...and PnL matching the quantity actually closed",
      abs(long_trip.gross_pnl - (1.0 - long_trip.dust_qty) * 10.0) < 0.01,
      f"got {long_trip.gross_pnl} on {1.0 - long_trip.dust_qty} closed")
# Conservation across the whole stream, which is the claim that actually
# matters. Every unit *sold* either matched the long or re-opened as a short;
# the dust is the slice of the *long* that was abandoned unsold, so it comes
# off the entry side, not the exit side.
sold = TINY * (1.0 / TINY)
reopened = sum(t.qty_opened for t in still_open)
matched = long_trip.qty_opened - long_trip.dust_qty
check("...and nothing vanishes: matched + re-opened == sold",
      abs(matched + reopened - sold) < 1e-6,
      f"matched={matched} dust={long_trip.dust_qty} reopened={reopened} sold={sold}")
check("...with the abandoned dust accounted for on the entry side",
      abs(matched + long_trip.dust_qty - long_trip.qty_opened) < 1e-9,
      f"{matched} + {long_trip.dust_qty} != {long_trip.qty_opened}")

# A position that flips through zero repeatedly: each crossing must close one
# trip and open its opposite, never net a long against a short.
# Each fill after the first is twice the open size, so it closes the position
# and re-opens the same size on the other side: 40 crossings, 40 closed trips.
flips = [f(Side.BUY, 1, 100)]
for i in range(1, 41):
    flips.append(f(Side.SELL if i % 2 else Side.BUY, 2, 100 + i, mins=i))
closed, still_open = reconstruct(flips)
check("40 flips through zero produce 40 closed trips", len(closed) == 40,
      f"got {len(closed)}")
# `all()` over an empty list is True, so each of these carries the count too --
# a reconstructor that emitted nothing would otherwise pass them all.
check("...alternating direction every time",
      len(closed) == 40 and all(a.direction is not b.direction
                                for a, b in zip(closed, closed[1:])),
      str([t.direction.value for t in closed[:6]]))
check("...each with both entry and exit receipts",
      len(closed) == 40 and all(t.entry_fill_ids and t.exit_fill_ids for t in closed))
check("...none of them holding for a negative time",
      len(closed) == 40 and all(t.hold_seconds >= 0 for t in closed))
check("...and every crossing netted 1 unit of PnL, never a long against a short",
      len(closed) == 40 and all(abs(abs(t.gross_pnl) - 1.0) < 1e-9 for t in closed),
      str([round(t.gross_pnl, 6) for t in closed[:5]]))
check("...and one leftover position still open", len(still_open) == 1,
      f"got {len(still_open)}")

# One fill crossing zero appears in two trips. That is correct, and the
# receipts should say so rather than invent an id.
cross = reconstruct([f(Side.BUY, 1, 100), f(Side.SELL, 3, 110, mins=1)])
shared = set(cross[0][0].exit_fill_ids) & set(cross[1][0].entry_fill_ids)
check("a fill that crosses zero keeps one id across both trips", len(shared) == 1,
      f"{cross[0][0].exit_fill_ids} vs {cross[1][0].entry_fill_ids}")

# Determinism at identical timestamps. Exchanges emit whole blocks of fills on
# the same millisecond, and the order they arrive in is not stable.
identical = ([f(Side.BUY, 1, 100 + i, mins=0) for i in range(6)]
             + [f(Side.SELL, 1, 200 + i, mins=0) for i in range(6)])
signatures = set()
for _ in range(25):
    shuffled = identical[:]
    random.Random(len(signatures)).shuffle(shuffled)
    c, o = reconstruct(shuffled)
    signatures.add(tuple(
        (round(t.gross_pnl, 9), tuple(t.entry_fill_ids), tuple(t.exit_fill_ids)) for t in c))
check("fills at identical timestamps reconstruct deterministically",
      len(signatures) == 1, f"{len(signatures)} distinct outcomes")
check("...and every input order closes the same number of trips",
      len(reconstruct(identical)[0]) == len(reconstruct(identical[::-1])[0]))

# Degenerate but legal fills must not produce NaN or a division by zero.
zero_priced = reconstruct([f(Side.BUY, 1, 0.0), f(Side.SELL, 1, 0.0, mins=1)])[0][0]
check("a zero-priced round-trip has zero notional, not a crash",
      zero_priced.notional == 0.0 and zero_priced.return_pct == 0.0,
      f"{zero_priced.notional} {zero_priced.return_pct}")
check("...and no NaN adverse excursion",
      zero_priced.adverse_excursion_pct == zero_priced.adverse_excursion_pct,
      str(zero_priced.adverse_excursion_pct))

dust = reconstruct([f(Side.BUY, 1e-8, 68000), f(Side.SELL, 1e-8, 69000, mins=1)])[0][0]
check("a dust-sized round-trip still closes", dust.closed_at is not None)
check("...with a sane return percentage", 0 < dust.return_pct < 100,
      f"got {dust.return_pct}")

check("an empty fill list reconstructs to nothing", reconstruct([]) == ([], []))

# CSV -> reconstruction, end to end, with a position that flips inside the file.
flip_csv = parse_text(SPOT + "".join([
    "2026-03-01 12:00:00,ETHUSDT,BUY,3200,1ETH,3200USDT,0USDT\n",
    "2026-03-01 13:00:00,ETHUSDT,SELL,3300,3ETH,9900USDT,0USDT\n",
    "2026-03-01 14:00:00,ETHUSDT,BUY,3250,2ETH,6500USDT,0USDT\n",
]))
fc, fo = reconstruct(flip_csv.fills)
check("a flip written in a CSV reconstructs as two closed trips", len(fc) == 2,
      f"got {len(fc)} closed, {len(fo)} open")
check("...long first, then short", fc[0].direction is Side.BUY and fc[1].direction is Side.SELL,
      str([t.direction.value for t in fc]))
check("...and nothing left open", fo == [], str(fo))


# ==========================================================================
print("\n=== header detection ===")

check("a header row alone still detects its dialect",
      detect_dialect(["Date(UTC)", "Pair", "Side", "Price", "Executed", "Amount", "Fee"]) == "spot")
check("a futures header is not mistaken for spot",
      detect_dialect(["Date(UTC)", "Symbol", "Side", "Price", "Quantity", "Amount", "Fee",
                      "Realized Profit"]) == "futures")
check("case and spacing in the header do not matter",
      detect_dialect(["  DATE(UTC) ", "pair", "SiDe", "Price", "EXECUTED", "amount", "Fee"])
      == "spot")
check("an empty header refuses",
      isinstance(raises(CsvFormatError, detect_dialect, []), CsvFormatError))
err = raises(CsvFormatError, detect_dialect, ["Nonsense", "Widget"])
check("an unrecognised header names what it saw",
      isinstance(err, CsvFormatError) and "Widget" in str(err), str(err)[:80])

# A file whose first line is a preamble rather than a header -- some exports
# prepend an account line. It must refuse rather than eat the fills.
ok, detail = refuses(lambda: parse_text("Trade History Export\n" + SPOT + GOOD))
check("a preamble line before the header refuses rather than silently dropping rows",
      ok, detail)

# The amount cross-check exists to catch a mis-detected column layout.
ok, detail = refuses(
    lambda: parse_text(SPOT + "2026-03-01 12:00:00,ETHUSDT,BUY,3200,2ETH,100USDT,0USDT\n"),
    at_row=2, saying="price x quantity")
check("an Amount that disagrees with price x qty refuses", ok, detail)
check("...but export rounding does not trip it",
      len(parse_text(SPOT + "2026-03-01 12:00:00,ETHUSDT,BUY,3200,2ETH,6399.98USDT,0USDT\n")
          .fills) == 1)


# ==========================================================================
print("\n=== fees are never silently zeroed ===")

ok, detail = refuses(
    lambda: parse_text(SPOT + "2026-03-01 12:00:00,ETHUSDT,BUY,3200,2ETH,6400USDT,0.01BNB\n"),
    at_row=2, saying="fee_prices")
check("a fee in a third asset refuses and says how to fix it", ok, detail)
priced = parse_text(SPOT + "2026-03-01 12:00:00,ETHUSDT,BUY,3200,2ETH,6400USDT,0.01BNB\n",
                    fee_prices={"BNB": 600.0})
check("...and converts exactly once a price is supplied", abs(priced.fills[0].fee - 6.0) < 1e-9,
      f"got {priced.fills[0].fee}")
base_fee = parse_text(SPOT + "2026-03-01 12:00:00,ETHUSDT,BUY,3200,2ETH,6400USDT,0.002ETH\n")
check("a base-asset fee is valued at the fill's own price",
      abs(base_fee.fills[0].fee - 6.4) < 1e-9, f"got {base_fee.fills[0].fee}")
check("total_fees over a whole file is the sum of the parts",
      abs(parse_text(SPOT + GOOD * 3).total_fees() - 3.0) < 1e-9)


# ==========================================================================
print("\n=== the two fixtures a real export is most likely to resemble ===")

FIXTURES = ROOT / "tests" / "fixtures"

# A European export: UTF-8 BOM, CRLF, semicolon-delimited. Every one of those
# is a fact the file states about itself, so all three are read, not guessed.
euro = parse_csv(FIXTURES / "binance_spot_semicolon.csv")
check("the semicolon/BOM/CRLF fixture parses from disk", len(euro.fills) == 2,
      f"got {len(euro.fills)}")
check("...into the same numbers a comma file would give",
      abs(euro.fills[0].price - 3200) < 1e-9 and abs(euro.fills[0].qty - 2) < 1e-12,
      f"{euro.fills[0].price} x {euro.fills[0].qty}")
check("...and reconstructs into one closed trip",
      len(reconstruct(euro.fills)[0]) == 1, str(reconstruct(euro.fills)))
check("...with the base-asset fee valued, not zeroed",
      abs(reconstruct(euro.fills)[0][0].fees - 13.0) < 1e-9,
      f"got {reconstruct(euro.fills)[0][0].fees}")

# A file carrying one of each failure mode at once, so the diagnostic's
# bucketing has something real to bucket. Ingest must refuse the whole thing.
ok, detail = refuses(lambda: parse_csv(FIXTURES / "binance_messy.csv"))
check("the messy fixture is refused outright, not partially imported", ok, detail)
err = raises(CsvFormatError, parse_csv, FIXTURES / "binance_messy.csv")
check("...failing on the first bad row, which is row 3", getattr(err, "row", None) == 3,
      str(err)[:90])


# ==========================================================================
print("\n=== the diagnostic never crashes and never fails the shell ===")

INSPECT = ROOT / "scripts" / "inspect_csv.py"

targets = [
    ("a good spot export", FIXTURES / "binance_spot.csv"),
    ("a good futures export", FIXTURES / "binance_futures.csv"),
    ("a nonexistent path", TMP / "definitely-not-here.csv"),
    ("a directory", TMP),
    ("a file that is not a CSV", Path("/etc/hosts")),
    ("a non-UTF-8 file", latin),
    ("a UTF-16 file", utf16),
    ("a file with a NUL in it", nul),
    ("a 50k-row file", big_file),
    ("an empty file", write("empty.csv", b"")),
    ("a file of only newlines", write("blank.csv", "\n\n\n\n")),
    ("a header with no rows", write("headeronly.csv", SPOT)),
    ("random bytes", write("random.csv", bytes(random.Random(7).randrange(256)
                                               for _ in range(4096)))),
    ("a file with an unclosed quote", write("quote.csv", SPOT + '2026-03-01 12:00:00,"ETH\n')),
    ("a European semicolon export", FIXTURES / "binance_spot_semicolon.csv"),
    ("a file with one of every failure mode", FIXTURES / "binance_messy.csv"),
]
for label, target in targets:
    try:
        proc = subprocess.run(
            [sys.executable, str(INSPECT), str(target)],
            capture_output=True, text=True, timeout=180)
        crashed = "Traceback" in proc.stdout + proc.stderr
        check(f"inspect_csv on {label}: exits 0", proc.returncode == 0,
              f"exit {proc.returncode}: {(proc.stderr or proc.stdout)[-200:]}")
        check(f"inspect_csv on {label}: no traceback", not crashed,
              (proc.stdout + proc.stderr)[-300:])
    except subprocess.TimeoutExpired:
        check(f"inspect_csv on {label}: exits 0", False, "timed out")
        check(f"inspect_csv on {label}: no traceback", False, "timed out")

check("inspect_csv with no arguments still exits 0",
      subprocess.run([sys.executable, str(INSPECT)],
                     capture_output=True, text=True).returncode == 0)
check("inspect_csv with too many arguments still exits 0",
      subprocess.run([sys.executable, str(INSPECT), "a", "b"],
                     capture_output=True, text=True).returncode == 0)

# Redaction: the output is meant to be pasted publicly.
secret = write("secret.csv",
               "Date(UTC),User_ID,Email,Pair,Side,Price,Executed,Amount,Fee\n"
               "2026-03-01 12:00:00,884422119955,someone@example.com,ETHUSDT,BUY,3200,"
               "2ETH,6400USDT,1USDT\n"
               "2026-03-01 13:00:00,884422119955,someone@example.com,ETHUSDT,SELL,wat,"
               "2ETH,6600USDT,1USDT\n")
out = subprocess.run([sys.executable, str(INSPECT), str(secret)],
                     capture_output=True, text=True).stdout
check("account identifiers are not printed in full", "884422119955" not in out,
      [line for line in out.splitlines() if "8844" in line][:2])
check("email addresses are not printed in full", "someone@example.com" not in out,
      [line for line in out.splitlines() if "example.com" in line][:2])
check("...but a short prefix is kept, so the column is still identifiable",
      "8844" in out and "redacted" in out)
check("...and the trade data is not redacted", "ETHUSDT" in out and "3200" in out)
check("...while the report still diagnoses the bad row", "'wat'" in out)


# --------------------------------------------------------------------------
# The scale cases write a few megabytes; a suite that is run on every commit
# should not leave them behind.
shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)

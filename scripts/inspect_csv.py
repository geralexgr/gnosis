#!/usr/bin/env python3
"""Tell a human what Gnosis makes of an unknown CSV, without ever raising.

`binance_csv.py` refuses rather than guesses: the first row it cannot read
aborts the whole parse, naming the line. That is the right behaviour for
ingest -- a profile built on a partially-eaten history is worse than no
profile -- but it is a terrible way to *meet* a file. The user gets one error
about line 4,182 and learns nothing about the other 20,000 rows, the header,
the dialect, or whether line 4,182 is a one-off or the shape of the whole
export.

This script is the other half of that bargain. It answers "what is this file
and what would happen if we ingested it" in one command, on any input, and it
is written so that it cannot itself fail: every stage is wrapped, an
unreadable file produces a report rather than a traceback, and the exit status
is always 0. A diagnostic that crashes on the input it exists to diagnose is
useless -- the moment it is needed most is the moment the input is worst.

The output is meant to be pasted into a bug report, so account-shaped columns
(`Account`, `User_ID`, `UID`, `Email`) are truncated to a short prefix. Nothing
else is redacted: symbols, prices and sizes are the whole point, and a
diagnostic that hides the numbers cannot diagnose anything.

    python3 scripts/inspect_csv.py <file.csv>
"""

from __future__ import annotations

import csv
import difflib
import io
import sys
import traceback
from datetime import timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Private helpers, used deliberately. The public API is all-or-nothing by
# design; a diagnostic needs per-row granularity, which is exactly the thing
# `parse_csv` refuses to offer callers who might be tempted to skip bad rows.
from gnosis.ingest.binance_csv import (  # noqa: E402
    _ALL_ALIASES,
    _FUTURES_COLUMNS,
    _FUTURES_REQUIRED,
    _SPOT_COLUMNS,
    _SPOT_REQUIRED,
    _fill_from_row,
    _match_header,
    _norm,
    CsvFormatError,
    sniff_delimiter,
)
from gnosis.model.events import History  # noqa: E402
from gnosis.model.roundtrip import reconstruct  # noqa: E402

MAX_FAILURES_SHOWN = 10
SAMPLE_ROWS_SHOWN = 3

# Columns that identify the human rather than the trade. Matched as substrings
# of the normalised header, so "User_ID", "User ID" and "Sub-account Email" all
# land. Everything else prints in full.
_SENSITIVE = ("account", "user_id", "user id", "userid", "uid", "email", "customer")


def rule(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m" if sys.stdout.isatty() else f"\n{title}")
    print("-" * max(len(title), 12))


def kv(key: str, value: object) -> None:
    print(f"  {key:<22} {value}")


def note(text: str) -> None:
    """A 'what to do next' line. Prefixed so it can be grepped out of a paste."""
    for i, line in enumerate(_wrap(text, 74)):
        print(f"  {'->' if i == 0 else '  '} {line}")


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        lines.append(cur)
    return lines or [""]


def short(value: object, limit: int = 60) -> str:
    """One line, bounded. Long fields are the thing being diagnosed, not shown."""
    text = str(value).replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    if len(text) > limit:
        return f"{text[:limit]}... ({len(text)} chars)"
    return text


# --------------------------------------------------------------------------
# Stage 1: the bytes


def read_bytes(path: Path) -> tuple[bytes | None, str]:
    """The file's contents, or a plain-English reason there aren't any."""
    if not path.exists():
        return None, f"no such file: {path}"
    if path.is_dir():
        return None, f"{path} is a directory, not a file"
    try:
        return path.read_bytes(), ""
    except PermissionError:
        return None, f"permission denied reading {path}"
    except OSError as exc:
        return None, f"could not read {path}: {exc}"


_BOMS = (
    (b"\xff\xfe\x00\x00", "UTF-32-LE", "utf-32"),
    (b"\x00\x00\xfe\xff", "UTF-32-BE", "utf-32"),
    (b"\xff\xfe", "UTF-16-LE", "utf-16"),
    (b"\xfe\xff", "UTF-16-BE", "utf-16"),
    (b"\xef\xbb\xbf", "UTF-8", "utf-8-sig"),
)


def decode(data: bytes) -> tuple[str | None, str, str, str]:
    """(text, encoding used, BOM description, warning).

    Guesses where `binance_csv` refuses to, because a guess that is only ever
    printed cannot corrupt a profile -- and telling the user "this is probably
    cp1252" is more useful than telling them it is not UTF-8.
    """
    for bom, name, encoding in _BOMS:
        if data.startswith(bom):
            try:
                return data.decode(encoding), encoding, f"yes, {name}", ""
            except UnicodeDecodeError as exc:
                return None, encoding, f"yes, {name}", f"BOM says {name} but it does not decode: {exc}"
    try:
        return data.decode("utf-8"), "utf-8", "no", ""
    except UnicodeDecodeError as exc:
        offending = data[exc.start : exc.start + 4]
        for fallback in ("cp1252", "latin-1"):
            try:
                text = data.decode(fallback)
            except UnicodeDecodeError:
                continue
            return text, fallback, "no", (
                f"not valid UTF-8 (byte {exc.start} is {offending!r}); read as {fallback} "
                "for this report only -- the parser will refuse it"
            )
    return None, "?", "no", "not decodable as UTF-8, cp1252 or latin-1; this may not be a text file"


def looks_binary(data: bytes) -> bool:
    head = data[:8192]
    if b"\x00" in head:
        return True
    printable = sum(1 for b in head if 9 <= b <= 13 or 32 <= b <= 126 or b >= 128)
    return bool(head) and printable / len(head) < 0.9


# --------------------------------------------------------------------------
# Stage 2: the header


def alias_report(header: list[str]) -> None:
    """For an unrecognised header, show what nearly matched what.

    The futures dialect in this repo was written against Binance's *documented*
    column names and never against a real export, so "unrecognised" is a
    likely and recoverable outcome rather than a dead end. What a human needs
    is not "this failed" but "your column X is one character away from the
    alias Y that the parser wants for field Z" -- which is a five-minute fix in
    `_FUTURES_COLUMNS`, if only they can see it.
    """
    normalised = [_norm(c) for c in header]
    unknown = [(raw, n) for raw, n in zip(header, normalised) if n not in _ALL_ALIASES]

    if unknown:
        print("  columns the parser does not know:")
        for raw, n in unknown:
            matches = difflib.get_close_matches(n, sorted(_ALL_ALIASES), n=3, cutoff=0.55)
            if matches:
                owners = []
                for alias in matches:
                    # A field name is the same thing in both dialects, so an
                    # alias that appears in each is reported once, not twice.
                    fields = {f for spec in (_SPOT_COLUMNS, _FUTURES_COLUMNS)
                              for f, aliases in spec.items() if alias in aliases}
                    owners.append(f"{alias!r} -> {'/'.join(sorted(fields))}")
                ratio = difflib.SequenceMatcher(None, n, matches[0]).ratio()
                print(f"    {short(raw, 30):<32} [{ratio:>4.0%}] {', '.join(dict.fromkeys(owners))}")
            else:
                print(f"    {short(raw, 30):<32} [  --] nothing close -- probably not a column "
                      "we need")

    for label, spec, required in (("spot", _SPOT_COLUMNS, _SPOT_REQUIRED),
                                  ("futures", _FUTURES_COLUMNS, _FUTURES_REQUIRED)):
        missing = [f for f in required if not any(a in normalised for a in spec[f])]
        if not missing:
            continue
        print(f"  for a {label} export, still missing:")
        for field in missing:
            print(f"    {field:<12} any of: {', '.join(spec[field])}")


# --------------------------------------------------------------------------
# Stage 3: the rows


class RowFailure:
    def __init__(self, line: int, message: str, value: object, cells: list[str]) -> None:
        self.line = line
        self.message = message
        self.value = value
        self.cells = cells

    @property
    def kind(self) -> str:
        """Coarse bucket, so 20,000 identical failures report as one problem."""
        text = self.message.lower()
        for needle, label in (
            ("fee is denominated", "fee in a third asset"),
            ("decimal comma", "decimal comma"),
            ("cannot read timestamp", "unreadable timestamp"),
            ("timestamp is empty", "empty timestamp"),
            ("as a number", "unreadable number"),
            ("not a finite", "numeric overflow"),
            ("neither buy nor sell", "unreadable side"),
            ("qty must be positive", "non-positive quantity"),
            ("price must be non-negative", "negative price"),
            ("does not match price x quantity", "amount vs price x qty mismatch"),
            ("row has", "short row"),
            ("symbol is empty", "empty symbol"),
            ("csv reader could not", "CSV structure damage"),
        ):
            if needle in text:
                return label
        return "other"


NEXT_STEPS = {
    "fee in a third asset": (
        "Fees were paid in an asset that is neither side of the pair (almost always BNB). "
        "Re-run ingest with fee_prices={'BNB': <price in USDT>}. Gnosis refuses to assume "
        "zero because an unpriced fee turns losing trades into winners in the profile."
    ),
    "decimal comma": (
        "The file uses a comma as the decimal separator, so it was exported in a non-English "
        "locale. Re-export from Binance with the language set to English, or convert the "
        "decimal separator to '.' before ingest."
    ),
    "unreadable timestamp": (
        "The date format is not one of the layouts we know. Send one of the offending values "
        "listed above and it can be added to _TS_FORMATS in binance_csv.py -- a one-line fix."
    ),
    "unreadable number": (
        "A price, quantity or fee cell is not a number in any shape we recognise. Check "
        "whether the columns are in the order the header claims; a shifted column reads as "
        "garbage in exactly this way."
    ),
    "numeric overflow": (
        "A number is too large to hold as a float. This is corruption, not data -- check the "
        "row in a text editor."
    ),
    "unreadable side": (
        "The Side column holds something other than BUY/SELL. If this export uses OPEN/CLOSE "
        "or LONG/SHORT wording, the mapping needs adding to _side() in binance_csv.py."
    ),
    "non-positive quantity": (
        "Rows with zero or negative quantity. If these are cancellations or fee-only lines "
        "rather than fills, they need filtering upstream -- Gnosis will not silently drop them."
    ),
    "amount vs price x qty mismatch": (
        "The Amount column disagrees with price x quantity by more than 5%, which usually "
        "means the columns are not what the parser thinks. Compare the header against the "
        "sample rows above before trusting anything else in this report."
    ),
    "short row": (
        "Some rows have fewer columns than the header. A truncated row would book a fill with "
        "no fee, so it is refused. Check whether the export was cut off, or whether an "
        "unquoted comma inside a field is splitting one column into two."
    ),
    "CSV structure damage": (
        "The CSV reader itself could not get through the file -- usually an unclosed quote "
        "that swallows everything after it. Open the named line in a text editor."
    ),
    "empty timestamp": "Rows with no date at all. Usually a footer or a summary line at the end of the export.",
    "empty symbol": "Rows with no trading pair. Usually a footer or subtotal line.",
    "other": "Not a failure mode this script knows. Paste the message above into an issue.",
}


def sensitive_indices(header: list[str]) -> set[int]:
    return {i for i, c in enumerate(header) if any(s in _norm(c) for s in _SENSITIVE)}


def redactor(header: list[str]):
    """Truncate account-shaped cells. The user may paste this output publicly."""
    hidden = sensitive_indices(header)

    def cell(index: int, value: str) -> str:
        if index in hidden and len(value) > 4:
            return f"{value[:4]}...[redacted, {len(value)} chars]"
        return short(value, 40)

    def value(raw: object, cells: list[str]) -> str:
        """Redact an offending value if it came from a sensitive column."""
        text = str(raw)
        for i in hidden:
            if i < len(cells) and cells[i] and cells[i].strip() == text.strip():
                return cell(i, text)
        if "@" in text and "." in text:  # belt and braces: an email anywhere
            return f"{text[:3]}...[redacted]"
        return short(text, 60)

    return cell, value, hidden


# --------------------------------------------------------------------------


def inspect(target: str) -> None:
    path = Path(target).expanduser()

    rule("file")
    kv("path", path)
    data, problem = read_bytes(path)
    if data is None:
        kv("readable", "no")
        note(problem)
        note("Pass the path to a Binance trade-history export "
             "(Orders -> Trade History -> Export on the web UI).")
        return
    kv("size", f"{len(data):,} bytes ({len(data) / 1024:.1f} KiB)")
    if not data:
        kv("content", "empty file")
        note("The file has no bytes at all. The export probably failed; download it again.")
        return

    rule("encoding")
    text, encoding, bom, warning = decode(data)
    kv("byte-order mark", bom)
    kv("decoded as", encoding)
    if looks_binary(data):
        kv("looks like text", "no -- NUL bytes or non-printable content")
    if warning:
        note(warning)
    if text is None:
        note("This is not a text file Gnosis can read. If it is an .xlsx, export it as CSV "
             "from the spreadsheet first.")
        return
    if encoding not in ("utf-8", "utf-8-sig"):
        note(f"The parser accepts UTF-8 and BOM-marked UTF-16/32. This file read as {encoding}; "
             "if that is a guess (no BOM above), re-save it as UTF-8 before ingest -- guessing "
             "a codepage can silently rename a symbol and split one position into two.")

    line_count = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
    kv("lines", f"{line_count:,}")
    blank = sum(1 for line in text.splitlines() if not line.strip())
    if blank:
        kv("blank lines", blank)
    if "\r\n" in text:
        kv("line endings", "CRLF (Windows)")
    elif "\r" in text:
        kv("line endings", "CR (classic Mac)")
    else:
        kv("line endings", "LF (Unix)")

    rule("delimiter")
    delimiter = sniff_delimiter(text)
    counts = {d: text[:65536].count(d) for d in (",", ";", "\t", "|")}
    kv("chosen", {",": "comma", ";": "semicolon", "\t": "tab"}.get(delimiter, repr(delimiter)))
    kv("occurrences in head", ", ".join(
        f"{ {',': 'comma', ';': 'semicolon', chr(9): 'tab', '|': 'pipe'}[d] }={n}"
        for d, n in counts.items()))
    if delimiter != ",":
        note(f"This is not a comma-delimited file. The parser detects that on its own, but a "
             f"{delimiter!r}-delimited export usually also means a non-English locale, so watch "
             "for decimal commas below.")

    # --- rows ---
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except csv.Error as exc:
        rule("structure")
        kv("csv reader", "failed")
        note(f"The CSV reader could not split this file: {exc}. Almost always an unclosed "
             "quote. Open the file in a text editor and look for a lone \" character.")
        return

    header_index = next((i for i, r in enumerate(rows) if any(c.strip() for c in r)), None)
    if header_index is None:
        rule("header")
        kv("found", "no -- every line is blank")
        note("The file has lines but no content. Re-download the export.")
        return

    header = rows[header_index]
    data_rows = [(i + 1, r) for i, r in enumerate(rows) if i > header_index and any(c.strip() for c in r)]
    redact_cell, redact_value, hidden = redactor(header)

    rule("header")
    kv("on line", header_index + 1)
    kv("columns", len(header))
    for i, name in enumerate(header):
        flag = "  [redacted in this report]" if i in hidden else ""
        print(f"    {i:>2}  {short(name, 44)}{flag}")

    try:
        cols, dialect = _match_header(header)
    except CsvFormatError as exc:
        cols, dialect = None, None
        kv("dialect", "UNRECOGNISED")
        print()
        alias_report(header)
        note("The parser will refuse this file. Either the export is a layout we have not seen "
             "(add the column names to _SPOT_COLUMNS/_FUTURES_COLUMNS in binance_csv.py using "
             "the near-misses above), or this is not a trade-history export at all -- Binance "
             "also exports deposits, withdrawals and funding, none of which are fills.")
        del exc
    else:
        kv("dialect", dialect)
        mapped = ", ".join(
            f"{f}={short(header[i], 18)}" for f, i in cols.items() if i is not None)
        kv("mapped columns", mapped)
        unmapped = [f for f, i in cols.items() if i is None]
        if unmapped:
            kv("optional, absent", ", ".join(unmapped))

    rule("sample rows")
    if not data_rows:
        kv("data rows", "0 -- header only")
    else:
        kv("data rows", f"{len(data_rows):,}")
        for line, row in data_rows[:SAMPLE_ROWS_SHOWN]:
            cells = " | ".join(redact_cell(i, c) for i, c in enumerate(row))
            print(f"    line {line}: {cells}")
        widths = {len(r) for _, r in data_rows}
        if len(widths) > 1:
            kv("row widths", f"{sorted(widths)} -- rows are not all the same width")
            note("Rows of differing width usually mean an unquoted delimiter inside a field, "
                 "or a footer line appended after the fills.")

    if cols is None:
        rule("verdict")
        print("  Cannot parse: the header was not recognised. Nothing below could be attempted.")
        return

    # --- parse, row by row ---
    rule("parse attempt")
    fills, failures = [], []
    for line, row in data_rows:
        try:
            fills.append(_fill_from_row(
                row, cols=cols, dialect=dialect, line=line, fee_prices=None, id_prefix="csv"))
        except CsvFormatError as exc:
            failures.append(RowFailure(line, str(exc), exc.value, row))
        except Exception as exc:  # noqa: BLE001 - an unexpected type is itself the finding
            failures.append(RowFailure(
                line, f"UNEXPECTED {type(exc).__name__}: {exc}", None, row))

    kv("rows parsed", f"{len(fills):,}")
    kv("rows failed", f"{len(failures):,}")
    if data_rows:
        kv("success rate", f"{len(fills) / len(data_rows):.1%}")

    if failures:
        buckets: dict[str, list[RowFailure]] = {}
        for f in failures:
            buckets.setdefault(f.kind, []).append(f)
        print("\n  failures by kind:")
        for kind, group in sorted(buckets.items(), key=lambda kv_: -len(kv_[1])):
            lines = ", ".join(str(f.line) for f in group[:5])
            more = f", +{len(group) - 5} more" if len(group) > 5 else ""
            print(f"    {len(group):>6}  {kind}  (lines {lines}{more})")

        print(f"\n  first {min(MAX_FAILURES_SHOWN, len(failures))} failures in full:")
        for f in failures[:MAX_FAILURES_SHOWN]:
            print(f"    line {f.line}: {short(f.message, 150)}")
            if f.value is not None:
                print(f"      offending value: {redact_value(f.value, f.cells)}")

        print()
        for kind in sorted(buckets, key=lambda k: -len(buckets[k])):
            note(f"[{kind}] {NEXT_STEPS.get(kind, NEXT_STEPS['other'])}")

        note("Ingest is all-or-nothing on purpose: parse_csv() will raise on the first of these "
             "and import nothing. Gnosis will not skip rows, because a profile built on a "
             "partially-eaten history is biased in an unknown direction, invisibly.")

    if not fills:
        rule("verdict")
        if failures:
            print(f"  Cannot ingest: {len(failures):,} of {len(data_rows):,} rows failed. "
                  "Fix the failure modes above and re-run this script.")
        else:
            print("  Nothing to ingest: the file has a valid header and no fills.")
        return

    # --- what parsed, and does it reconstruct ---
    rule("history")
    history = History(fills=fills, source=f"binance:csv:{path.name}")
    kv("fills", f"{len(history.fills):,}")
    kv("symbols", f"{len(history.symbols)}: {short(', '.join(history.symbols), 60)}")
    kv("venues", ", ".join(sorted({f.venue.value for f in history.fills})))
    first, last = history.fills[0].ts, history.fills[-1].ts
    kv("first fill", first.astimezone(timezone.utc))
    kv("last fill", last.astimezone(timezone.utc))
    kv("span", f"{history.span_days:.1f} days")
    kv("total fees", f"{history.total_fees():,.4f} (quote asset)")
    quotes = sorted({f.meta.get("quote_asset", "?") for f in history.fills})
    kv("quote assets", ", ".join(quotes))
    if len(quotes) > 1:
        note("More than one quote asset in the book. Fees and PnL are summed in each pair's own "
             "quote, so a mixed-quote history is not directly addable -- treat the totals above "
             "as indicative only.")
    if history.span_days and history.span_days < 30:
        note(f"Only {history.span_days:.1f} days of history. Detectors refuse to draw "
             "conclusions from a window this short, which is the difference between a profile "
             "and an insult.")

    rule("round-trip reconstruction")
    try:
        closed, still_open = reconstruct(history.fills)
    except Exception as exc:  # noqa: BLE001
        kv("reconstruct", "FAILED")
        note(f"reconstruct() raised {type(exc).__name__}: {exc}. This is a bug in "
             "roundtrip.py -- the fills parsed cleanly. Please report it with this output.")
        return

    kv("closed trips", f"{len(closed):,}")
    kv("open at end", f"{len(still_open):,}")
    if closed:
        wins = sum(1 for t in closed if t.is_winner)
        kv("win rate", f"{wins / len(closed):.1%}")
        kv("net pnl", f"{sum(t.net_pnl for t in closed):,.2f}")
        kv("median hold", f"{sorted(t.hold_hours for t in closed)[len(closed) // 2]:.2f} h")

    # Sanity, not correctness: these are the ways a mis-parsed file shows up
    # downstream as a plausible-looking profile that is quietly wrong.
    problems = []
    if any(t.hold_seconds < 0 for t in closed):
        problems.append("a trip closed before it opened -- timestamps are out of order or misparsed")
    if any(t.notional <= 0 for t in closed):
        problems.append("a trip has zero or negative notional -- a zero price or quantity got through")
    if any(t.net_pnl != t.net_pnl for t in closed):  # NaN
        problems.append("a trip has NaN PnL")
    if any(not (t.entry_fill_ids and t.exit_fill_ids) for t in closed):
        problems.append("a closed trip is missing its receipts")
    unclosed_share = len(still_open) / max(len(closed) + len(still_open), 1)
    if closed and unclosed_share > 0.5:
        problems.append(
            f"{unclosed_share:.0%} of positions never close. Either the export starts "
            "mid-position, or fills are missing -- both make the profile unreliable")
    if not closed and still_open:
        problems.append("nothing closed at all: every position is still open")

    if problems:
        kv("sanity", "SUSPECT")
        for p in problems:
            note(p)
    else:
        kv("sanity", "round-trips look coherent")

    rule("verdict")
    if failures:
        print(f"  Would NOT ingest: {len(failures):,} rows fail, and ingest is all-or-nothing.")
        print(f"  The other {len(fills):,} rows parse cleanly and reconstruct into "
              f"{len(closed):,} closed trips, so the file is close to usable.")
    else:
        print(f"  Ready to ingest: all {len(fills):,} rows parse, {len(closed):,} closed "
              f"trips over {history.span_days:.0f} days.")
        print(f"    python3 -m gnosis.cli card {path}")


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        # Not an error: being asked for help is not a failure, and neither is
        # being called wrong. This script never returns non-zero.
        return 0
    try:
        inspect(argv[0])
    except Exception:  # noqa: BLE001 - the whole point of this script
        print("\nThis diagnostic hit a bug of its own. That is a defect in "
              "inspect_csv.py, not in your file:")
        traceback.print_exc(file=sys.stdout)
        print("\nPlease report the traceback above. The file itself may still be fine.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

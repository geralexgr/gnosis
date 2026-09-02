"""Enrichment: facts about a history that the history does not itself contain.

Everything under `detectors/` is closed — it computes from fills and nothing
else, which is what makes it reproducible offline and what lets the eval score
it against a labelled corpus. Enrichment is the deliberate exception: it asks
an outside service a question about something in the history, and it therefore
has to be honest about a different failure mode.

The rule that governs this package: **an enricher may report what the outside
service said and what the history says, and it may put them side by side. It
may not combine them into a claim neither one supports.** A token audit knows
whether a contract has a honeypot function; the history knows what the trader
realised on it. Neither knows whether the token "went to zero", so nothing here
says that.

The other rule is the same one the detectors follow: absence of data is
reported as absence of data. A token with no audit coverage is "no data", never
"passed".
"""

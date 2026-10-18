"""Strict review-only interpretation of the channel's observed text templates."""

import re
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

ENTRY = re.compile(
    r"(?:\d+[.)]\s*)?(?:let'?s buy|let sbuy) (?:- )?(?P<strike>\d+(?:\.\d+)?) "
    r"(?:(?P<ticker>[a-z]{1,6}) )?(?P<right>calls?|puts?) (?:for )?"
    r"(?P<cents>\d+(?:\.\d+)?) (?:cents?|centsz|bents)[.!]?(?: yolo[.!]?)?"
)
CUT = re.compile(r"cut the (?P<right>calls|puts) att? (?P<cents>\d+(?:\.\d+)?) cents?[.!]?")


def size_entry(budget: Decimal, premium: Decimal, multiplier: int) -> tuple[int, Decimal]:
    if not budget.is_finite() or not premium.is_finite():
        raise ValueError("Budget and premium must be finite.")
    if budget <= 0 or premium <= 0 or multiplier <= 0:
        raise ValueError("Budget, premium and multiplier must be positive.")
    cost = premium * multiplier
    quantity = int(budget // cost)
    return quantity, cost * quantity


def parse_signal(text: str, sent_at: str) -> dict:
    if len(text) > 4000:
        return {"kind": "review", "reason": "Long message needs review."}
    normalized = ' '.join(text.replace('’', "'").lower().split())
    match = ENTRY.fullmatch(normalized)
    if match:
        fields = match.groupdict()
        strike = Decimal(fields['strike'])
        premium = Decimal(fields['cents']) / 100
        if strike <= 0 or premium <= 0:
            return {"kind": "review", "reason": "Invalid strike or premium."}
        written_ticker = fields['ticker']
        if written_ticker is None and not Decimal('500') <= strike < Decimal('1000'):
            return {"kind": "review", "strike": str(strike),
                    "right": fields['right'].rstrip('s'), "premium": str(premium),
                    "reason": "Tickerless strike is outside the supported SPY range."}
        ticker = written_ticker.upper() if written_ticker else 'SPY'
        stamp = datetime.fromisoformat(sent_at)
        if stamp.tzinfo is None:
            return {"kind": "review", "reason": "Message timezone is missing."}
        expiry = stamp.astimezone(ZoneInfo('America/New_York')).date().isoformat() if ticker in {'SPY', 'SPX'} else None
        kind = 'entry' if ticker == 'SPY' else 'review'
        reason = ('SPY 0DTE inferred from tickerless 500–999 strike.' if not written_ticker else
                  'SPY 0DTE per channel description.' if ticker == 'SPY' else
                  'SPX 0DTE is outside the SPY-only paper strategy.' if ticker == 'SPX' else
                  'Expiration is not specified.')
        return {"kind": kind, "ticker": ticker,
                "strike": str(strike), "right": fields['right'].rstrip('s'),
                "premium": str(premium), "expiration": expiry,
                "reason": reason}
    match = CUT.fullmatch(normalized)
    if match:
        return {"kind": "exit", "right": match['right'].rstrip('s'),
                "mentioned_premium": str(Decimal(match['cents']) / 100),
                "needs_position_context": True}
    if re.fullmatch(r"(?:sell|sold|sold out|out|outz|cut)(?: at \d+(?:\.\d+)? cents)?[.!]?", normalized):
        return {"kind": "exit", "right": None, "needs_position_context": True}
    match = re.fullmatch(r"(?:add+|double it|triple it|tripe it|quadruple it|quadriple it)(?: to (?:the )?(calls|puts))?[.!]?", normalized)
    if match:
        return {"kind": "add", "right": match[1].rstrip('s') if match[1] else None,
                "needs_position_context": True, "needs_fresh_quote": True}
    if re.search(r"\b(?:add(?:ed|d)?|double|triple|tripe|quadruple|quadriple)\b", normalized):
        return {"kind": "review", "reason": "Add-related recap or unsupported instruction."}
    if re.search(r"\b(?:buy|sell|sold|cut)\b", normalized):
        return {"kind": "review", "reason": "Not an exact supported alert template."}
    return {"kind": "ignore"}


def directed_add_legs(text: str) -> list[dict] | None:
    """Read exact per-contract adds without using the caller's order quantities."""
    lines = text.strip().splitlines()
    if not 1 <= len(lines) <= 2:
        return None
    legs = []
    seen = set()
    pattern = (r'added (?P<qty>\d+) more to the (?P<strike>\d+(?:\.\d+)?) '
               r'(?P<right>calls?|puts?) at (?P<cents>\d+(?:\.\d+)?) cents?[.!]?')
    for line in lines:
        match = re.fullmatch(pattern, ' '.join(line.lower().split()))
        if not match or int(match['qty']) <= 0 or Decimal(match['cents']) <= 0:
            return None
        strike = Decimal(match['strike'])
        if not Decimal('500') <= strike < Decimal('1000'):
            return None
        key = (str(strike), match['right'].rstrip('s'))
        if key in seen:
            return None
        seen.add(key)
        legs.append({'kind': 'add', 'strike': key[0], 'right': key[1]})
    return legs

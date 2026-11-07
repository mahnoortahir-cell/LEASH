"""Turn the owner's plain-English instruction into a Mandate.

The AI (or the simple rule reader) only produces a DRAFT. Then plain code:
  * looks merchant names up in the owner's own directory (the AI can NOT invent merchants),
  * refuses anything above hard ceilings (the AI can NOT hand out big limits),
  * and the owner must still read and confirm the result.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from pydantic import BaseModel, Field, ValidationError

from .llm import LLM, extract_json
from .models import Mandate, check_amount
from .util import norm_id


class MandateError(Exception):
    pass


@dataclass(frozen=True)
class Ceilings:
    """Hard limits that no instruction (and no AI) can raise."""
    per_purchase: Decimal = Decimal("500")
    budget: Decimal = Decimal("2000")
    window_days: int = 31
    per_day: int = 20
    valid_days: int = 31


class MandateDraft(BaseModel):
    currency: str = "USD"
    per_purchase_cap: Optional[Decimal] = None
    budget_amount: Optional[Decimal] = None
    budget_window_days: Optional[int] = None
    max_purchases_per_day: Optional[int] = None
    step_up_threshold: Optional[Decimal] = None
    merchants: list[str] = Field(default_factory=list)
    valid_days: Optional[int] = None
    assumptions: list[str] = Field(default_factory=list)


# ---- the simple rule reader (works offline, no AI) ------------------------------------------
_NUM = r"\$?\s*(\d+(?:\.\d{1,2})?)"
RE_PER = re.compile(r"(?:max(?:imum)?|up to|no more than|at most|limit(?:ed)? to)\s*" + _NUM +
                    r"\s*(?:per|each|a|an|/)\s*(?:purchase|order|payment|transaction|item)", re.I)
RE_BUDGET = re.compile(_NUM + r"\s*(?:per|a|each|every|/)\s*(day|week|month)\b", re.I)
RE_COUNT = re.compile(r"(\d+)\s*(?:purchases|orders|payments|transactions)\s*(?:per|a|each)\s*day", re.I)
RE_ASK = re.compile(r"(?:ask me|check with me|approve|approval|confirm)[^.\d$]*" + _NUM, re.I)
RE_DAYS = re.compile(r"(?:for|next|valid for)\s*(\d+)\s*(day|days|week|weeks)\b", re.I)
_WINDOW_DAYS = {"day": 1, "week": 7, "month": 30}


def parse_with_rules(text: str, directory: dict[str, list[str]]) -> MandateDraft:
    draft = MandateDraft()
    if (m := RE_PER.search(text)):
        draft.per_purchase_cap = Decimal(m.group(1))
    if (m := RE_BUDGET.search(text)):
        draft.budget_amount = Decimal(m.group(1))
        draft.budget_window_days = _WINDOW_DAYS[m.group(2).lower()]
    if (m := RE_COUNT.search(text)):
        draft.max_purchases_per_day = int(m.group(1))
    if (m := RE_ASK.search(text)):
        draft.step_up_threshold = Decimal(m.group(1))
    if (m := RE_DAYS.search(text)):
        draft.valid_days = int(m.group(1)) * (7 if m.group(2).lower().startswith("week") else 1)
    draft.merchants = [name for name in directory if re.search(rf"\b{re.escape(name)}\b", text, re.I)]
    return draft


# ---- the AI reader --------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You convert a person's spending instruction into JSON. Reply with ONLY one JSON object with these keys: "
    "currency (3-letter code), per_purchase_cap, budget_amount, budget_window_days, max_purchases_per_day, "
    "step_up_threshold (amount above which the person wants to be asked first), merchants (a list of merchant "
    "names EXACTLY as the person wrote them), valid_days. Use numbers or numeric strings. "
    "Use null for anything the person did not say. Never invent merchants or numbers."
)


def parse_with_llm(text: str, llm: LLM) -> MandateDraft:
    reply = llm.complete(SYSTEM_PROMPT, f"Instruction:\n{text}", max_tokens=400)
    try:
        return MandateDraft.model_validate(extract_json(reply))
    except (ValueError, ValidationError) as exc:
        raise MandateError(f"I could not understand the AI's answer ({exc}). Try rephrasing.") from exc


# ---- the strict part: draft -> Mandate ------------------------------------------------------
def build_mandate(draft: MandateDraft, directory: dict[str, list[str]], *, owner: str, now: datetime,
                  note: str = "", ceilings: Ceilings = Ceilings()) -> tuple[Mandate, list[str]]:
    """Returns the Mandate plus a list of plain-English notes about anything that was assumed."""
    assumptions = list(draft.assumptions)
    lowered = {name.lower(): ids for name, ids in directory.items()}
    all_ids = {norm_id(i): i for ids in directory.values() for i in ids}

    allowed: list[str] = []
    for name in draft.merchants:
        if name.lower() in lowered:
            allowed.extend(lowered[name.lower()])
        elif norm_id(name) in all_ids:
            allowed.append(name)
        else:
            raise MandateError(f"I do not know a merchant called '{name}'. Add it to your merchants file "
                               f"(known: {', '.join(directory) or 'none'}).")
    if not allowed:
        raise MandateError("I could not find a merchant from your list in that text. "
                           f"Your merchants are: {', '.join(directory) or 'none yet (add some to merchants.json)'}.")

    try:
        budget = check_amount(draft.budget_amount) if draft.budget_amount is not None else None
        cap = check_amount(draft.per_purchase_cap) if draft.per_purchase_cap is not None else None
        ask = check_amount(draft.step_up_threshold) if draft.step_up_threshold is not None else None
    except (ValueError, InvalidOperation) as exc:
        raise MandateError(f"One of the amounts is not valid: {exc}") from exc
    if budget is None:
        raise MandateError("Say how much can be spent in total (for example '$200 a week').")
    if cap is None:
        cap = (budget / 2).quantize(Decimal("0.01"))
        assumptions.append(f"No per-purchase limit given, so I used half the budget: {cap}")
    if ask is None:
        ask = (cap / 2).quantize(Decimal("0.01"))
        assumptions.append(f"No 'ask me first' amount given, so I used half the per-purchase limit: {ask}")
    ask = min(ask, cap)
    cap = min(cap, budget)

    window_days = draft.budget_window_days or 7
    per_day = draft.max_purchases_per_day or 3
    valid_days = draft.valid_days or 7
    if draft.budget_window_days is None:
        assumptions.append("No time window given for the budget, so I used 7 days")
    if draft.max_purchases_per_day is None:
        assumptions.append("No daily purchase limit given, so I used 3 per day")
    if draft.valid_days is None:
        assumptions.append("No end date given, so the rules last 7 days")

    problems = []
    if cap > ceilings.per_purchase:
        problems.append(f"per-purchase limit {cap} is above the safety ceiling of {ceilings.per_purchase}")
    if budget > ceilings.budget:
        problems.append(f"budget {budget} is above the safety ceiling of {ceilings.budget}")
    if window_days > ceilings.window_days:
        problems.append(f"budget window of {window_days} days is above the ceiling of {ceilings.window_days}")
    if per_day > ceilings.per_day:
        problems.append(f"{per_day} purchases a day is above the ceiling of {ceilings.per_day}")
    if valid_days > ceilings.valid_days:
        problems.append(f"{valid_days} days is longer than the ceiling of {ceilings.valid_days}")
    if problems:
        raise MandateError("Refused for safety: " + "; ".join(problems) + ". Ask for less, or raise the ceilings in code yourself.")

    try:
        mandate = Mandate(owner=owner, currency=draft.currency, per_purchase_cap=cap, budget_amount=budget,
                          budget_window_hours=window_days * 24, max_purchases_per_day=per_day,
                          step_up_threshold=ask, allowed_merchants=tuple(allowed), valid_from=now,
                          valid_until=now + timedelta(days=valid_days), note=note[:500])
    except ValidationError as exc:
        raise MandateError("The rules do not make sense together: " +
                           "; ".join(e["msg"] for e in exc.errors())) from exc
    return mandate, assumptions


def parse_mandate(text: str, directory: dict[str, list[str]], *, owner: str, now: datetime,
                  llm: Optional[LLM] = None, ceilings: Ceilings = Ceilings()) -> tuple[Mandate, list[str], str]:
    """Returns (mandate, assumptions, how) where how is 'ai' or 'rules'. The owner must still confirm."""
    how = "rules"
    draft: Optional[MandateDraft] = None
    ai_problem: Optional[str] = None
    if llm is not None:
        try:
            draft = parse_with_llm(text, llm)
            how = "ai"
        except MandateError:
            draft = None  # the AI answered, but not in a usable way: fall back to the simple reader
        except Exception as exc:  # bad key, wrong model name, no internet...: never let the AI stop the owner
            draft = None
            ai_problem = f"The AI could not be used ({type(exc).__name__}), so the simple rule reader was used instead."
    if draft is None:
        draft = parse_with_rules(text, directory)
    mandate, assumptions = build_mandate(draft, directory, owner=owner, now=now, note=text, ceilings=ceilings)
    if ai_problem:
        assumptions = [ai_problem] + assumptions
    return mandate, assumptions, how

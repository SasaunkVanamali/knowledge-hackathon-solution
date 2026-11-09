"""Product taxonomy for rate-table rows and for questions.

The taxonomy is generic credit-union vocabulary (certificates, money markets, auto loans...),
not answers: values always come from the snapshots.
"""
from __future__ import annotations

import re

# (family, pattern on ROW text) - first match wins, so specific families come first.
ROW_FAMILIES = [
    ("ira", r"\bira\b|\biras\b|roth|traditional:"),
    ("hsa", r"health savings|\bhsa\b"),
    ("certificate", r"certificate|\bcd\b|term share"),
    ("money market", r"money market|investor.s choice|\bachieve\b"),
    ("high-yield savings", r"high[- ]yield"),
    ("bright savings", r"\bbright\b"),
    ("launchpad savings", r"launchpad"),
    ("holiday club", r"holiday"),
    ("you name it savings", r"you[- ]?name[- ]?it"),
    ("share savings", r"share savings|savings account|regular savings|^savings"),
    ("checking", r"checking"),
    ("atv loan", r"\batv\b|snowmobile|jet ski"),
    ("boat rv loan", r"\bboat\b|\brv\b|recreational"),
    ("auto loan", r"\bauto\b|vehicle|motorcycle|model year"),
    ("heloc", r"heloc|line of credit"),
    ("home equity loan", r"home equity loan|loan – standard|loan - standard|\d+ years?$"),
    ("mortgage", r"mortgage|conventional|\d\d-year fixed|purchase:|refinance:|home loan|rate relief"),
    ("home improvement loan", r"home improvement"),
    ("personal loan", r"personal loan|^personal"),
    ("credit card", r"visa|credit card|balance transfer|card type"),
]

# (family, pattern on QUESTION text)
Q_FAMILIES = [
    ("ira", r"\bira\b|\biras\b|roth|traditional ira|retirement (account|certificate)"),
    ("hsa", r"\bhsa\b|health savings"),
    ("money market", r"money ?market|investor.?s choice|\bachieve\b"),
    ("high-yield savings", r"high[- ]?yield"),
    ("bright savings", r"\bbright\b"),
    ("launchpad savings", r"launch ?pad"),
    ("holiday club", r"holiday|christmas club"),
    ("you name it savings", r"you[- ]?name[- ]?it"),
    ("certificate", r"certificate|\bcds?\b|share cert|term share|time deposit"),
    ("checking", r"checking"),
    ("atv loan", r"\batvs?\b|snowmobile|jet ?ski|side[- ]by[- ]side|utv"),
    ("boat rv loan", r"\bboats?\b|\brvs?\b|camper|recreational|motor ?home"),
    ("auto loan", r"\bauto\b|\bcars?\b|vehicle|motorcycle|truck|\bsuv\b"),
    ("heloc", r"heloc|home equity line|equity line"),
    ("home equity loan", r"home equity loan|equity loan|second mortgage"),
    ("home improvement loan", r"home improvement|remodel|renovation"),
    ("mortgage", r"mortgage|home ?loan|house|\d\d[- ]year fixed|\d\d[- ]year|refinanc|refi\b|home purchase"),
    ("personal loan", r"personal loan|signature loan|unsecured loan"),
    ("credit card", r"credit cards?|\bvisa\b|\bcards?\b"),
    ("share savings", r"share savings|savings account|regular savings|\bsavings\b"),
]

# qualifiers that split one product into different facts (rate-vs-APY is handled by columns)
QUALIFIERS = {
    "jumbo": r"jumbo",
    "business": r"business",
    "special": r"special|promo",
    "refinance": r"refinanc|\brefi\b",
    "purchase": r"purchase|buy",
    "intro": r"intro|introductory|balance transfer",
    "roth": r"roth",
    "traditional": r"traditional",
    "equity": r"equity",
    "rewards": r"rewards?",
    "relief": r"relief",
    "achieve": r"achieve",
}


def row_family(text: str) -> str | None:
    t = text.lower()
    for fam, pat in ROW_FAMILIES:
        if re.search(pat, t):
            return fam
    return None


def question_families(q: str) -> list[str]:
    t = q.lower()
    out = []
    for fam, pat in Q_FAMILIES:
        if re.search(pat, t):
            out.append(fam)
    # "IRA certificate" -> ira; "savings" alone is share savings only if nothing more specific
    if "ira" in out and "certificate" in out:
        out.remove("certificate")
    if "share savings" in out and len(out) > 1 and not re.search(r"share savings", t):
        out.remove("share savings")
    if "credit card" in out and len(out) > 1 and not re.search(r"credit card|visa", t):
        out.remove("credit card")
    if "home equity loan" in out and "heloc" in out:
        out.remove("home equity loan")
    if "mortgage" in out and ("home equity loan" in out or "heloc" in out or "home improvement loan" in out):
        out.remove("mortgage")
    return out


def qualifiers(text: str) -> set[str]:
    t = text.lower()
    return {k for k, p in QUALIFIERS.items() if re.search(p, t)}

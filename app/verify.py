"""Checks done by plain code, so no AI can talk its way past them."""
import re
import unicodedata

NUMBER_WORDS = {
    "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
}
COMPANY_ENDINGS = re.compile(
    r"\b(inc|incorporated|llc|ltd|limited|pvt|private|corp|corporation|co|company|plc|gmbh)\b\.?",
    re.I,
)


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    for a, b in {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"}.items():
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip().lower()


def quote_on_page(quote: str, page: str) -> bool:
    quote = normalise(quote).strip("\"' .…")
    return len(quote) >= 15 and quote in normalise(page)


def company_core(name: str) -> str:
    return normalise(COMPANY_ENDINGS.sub("", name or "")).strip(" .,&-")


def same_company(a: str, b: str) -> bool:
    a, b = company_core(a), company_core(b)
    return bool(a and b) and (a == b or a in b or b in a)


def numbers_in(text: str) -> set[str]:
    text = normalise(text)
    found = {m.replace(",", "").rstrip(".") for m in re.findall(r"\d[\d,.]*", text)}
    found |= {digit for word, digit in NUMBER_WORDS.items() if re.search(rf"\b{word}\b", text)}
    return found


def linkedin_slug(url: str) -> str:
    match = re.search(r"linkedin\.com/in/([^/?#]+)", url or "", re.I)
    return match.group(1).lower() if match else ""


def mask(text: str, words: list[str], stand_in: str) -> str:
    """Hide names so a judge cannot use them. Longest first, so full names go before parts."""
    for word in sorted({w for w in words if len(w) > 1}, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(word)}\b", stand_in, text, flags=re.I)
    return text

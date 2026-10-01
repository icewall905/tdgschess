"""Cheap Danish grammar fixes for LLM text: the model often gets en/et wrong ("mit plan", "din tårn", "tårnen").
Rules only for the words chess chat uses all the time; no model, no dictionary, microseconds per message."""
import re

NEUTER = ("tårn", "træk", "felt", "bræt", "skakbræt", "spil", "slutspil", "midtspil", "angreb", "modangreb", "offer",
          "forsvar", "tempo", "centrum", "valg", "mål", "problem", "sted", "øjeblik", "råd", "svar", "hul", "skridt",
          "fremstød", "trick", "kneb", "par", "point", "slag")
COMMON = ("bonde", "springer", "løber", "dronning", "konge", "brik", "plan", "fejl", "rokade", "åbning", "trussel",
          "chance", "tur", "idé", "ide", "fordel", "stilling", "side", "linje", "række", "diagonal", "kamp", "sejr",
          "gaffel", "binding", "fælde", "taktik", "strategi", "position", "hest")
# adjectives after en/et: common form -> neuter form ("en god plan", "et godt træk")
ADJ = {"god": "godt", "stor": "stort", "ny": "nyt", "fin": "fint", "sikker": "sikkert", "farlig": "farligt",
       "vigtig": "vigtigt", "dårlig": "dårligt", "svag": "svagt", "stærk": "stærkt", "sjov": "sjovt", "smuk": "smukt",
       "klog": "klogt", "hurtig": "hurtigt", "rolig": "roligt", "aktiv": "aktivt", "modig": "modigt", "flot": "flot",
       "smart": "smart", "frækt": "frækt", "fræk": "frækt", "skarp": "skarpt", "dum": "dumt", "lækker": "lækkert",
       "vild": "vildt", "rigtig": "rigtigt", "forkert": "forkert", "perfekt": "perfekt", "kæmpe": "kæmpe",
       "lille": "lille", "spændende": "spændende", "solid": "solidt", "elegant": "elegant", "underlig": "underligt"}
ADJ_COMMON = {v: k for k, v in ADJ.items() if v != k}

GENDER = {**{w: "t" for w in NEUTER}, **{w: "n" for w in COMMON}}
NOUN = r"(" + "|".join(sorted(GENDER, key=len, reverse=True)) + r")\b"
POSS = {"n": {"mit": "min", "dit": "din", "sit": "sin"}, "t": {"min": "mit", "din": "dit", "sin": "sit"}}

POSS_RE = re.compile(r"\b(min|din|sin|mit|dit|sit)(\s+(?:\w+e\s+)?)" + NOUN, re.IGNORECASE)
DEM_RE = re.compile(r"\b(den|det)(\s+\w+e\s+)" + NOUN, re.IGNORECASE)  # "den lille tårn" -> "det lille tårn"
INDEF_RE = re.compile(r"\b(en|et)(\s+)(?:(\w+)(\s+))?" + NOUN, re.IGNORECASE)
DEF_FIX = {"tårnen": "tårnet", "trækken": "trækket", "bræten": "brættet", "spillen": "spillet",
           "feltten": "feltet", "angrebben": "angrebet", "planet": None}  # planet is also "the planet": left alone
DEF_RE = re.compile(r"\b(" + "|".join(k for k, v in DEF_FIX.items() if v) + r")\b", re.IGNORECASE)


def _case(src: str, word: str) -> str:
    return word[:1].upper() + word[1:] if src[:1].isupper() else word


def _poss(m):
    want = GENDER[m.group(3).lower()]
    word = POSS[want].get(m.group(1).lower(), m.group(1))
    return _case(m.group(1), word) + m.group(2) + m.group(3)


def _dem(m):
    want = GENDER[m.group(3).lower()]
    return _case(m.group(1), "det" if want == "t" else "den") + m.group(2) + m.group(3)


def _indef(m):
    art, sp1, adj, sp2, noun = m.groups()
    want = GENDER[noun.lower()]
    if adj:
        low = adj.lower()
        table = ADJ if want == "t" else ADJ_COMMON
        if low in table:
            adj = _case(adj, table[low])
        elif low not in ADJ and low not in ADJ_COMMON:
            return m.group(0)  # not an adjective we know: "en" may not belong to this noun at all
    out = _case(art, "et" if want == "t" else "en") + sp1
    return out + (adj + sp2 if adj else "") + noun


def fix(text: str) -> str:
    if not text:
        return text
    text = POSS_RE.sub(_poss, text)
    text = DEM_RE.sub(_dem, text)
    text = INDEF_RE.sub(_indef, text)
    return DEF_RE.sub(lambda m: _case(m.group(1), DEF_FIX[m.group(1).lower()]), text)


EN_WORDS = re.compile(r"\b(the|you|your|my|is|are|and|it's|i'm|we've|we're|look|this|that|what|with|oops|wow)\b", re.I)
DA_WORDS = re.compile(r"\b(og|jeg|du|din|dit|min|mit|er|det|den|på|med|nu|har|ikke|til|en|et|af|hov|lige)\b", re.I)


def looks_english(text: str) -> bool:
    return len(EN_WORDS.findall(text)) >= 3 and len(EN_WORDS.findall(text)) > 2 * len(DA_WORDS.findall(text))

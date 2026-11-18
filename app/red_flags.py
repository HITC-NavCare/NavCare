"""Safety net for call_911: finds clear emergency phrases in the person's message.

The v2 model decides call_911 from the whole message. This check does not replace it: it can
only raise the flag, never lower it, so a model that ignores its instructions cannot hide an
emergency. It also works when v1 answers (v1 cannot read messages) or v2 falls back.

Limitations, stated plainly: it matches phrases, not meaning. It misses other wordings (the
model is there for those), and it also flags negations such as "no chest pain". Showing the
911 advice when it was not needed is the safer mistake.
"""

import re
import unicodedata

# English and French, written without accents and in lower case (messages are normalised the
# same way). Same situations as the "life-threatening" list in the v2 system instruction.
EMERGENCY_PATTERNS = [
    # chest pain
    r"chest pains?", r"pain in (my|his|her|the) chest", r"douleurs? (a la |dans la )?poitrine",
    r"douleurs? thoraciques?", r"mal a la poitrine", r"heart attack", r"crise cardiaque",
    # breathing
    r"(can ?not|can't|cant|unable to) breathe?", r"(trouble|difficulty|problems?) breathing",
    r"hard to breathe", r"short(ness)? of breath", r"not breathing", r"stopped breathing",
    r"(difficulte|mal) a respirer", r"(ne|n') ?respire (plus|pas)", r"essouffle",
    r"choking", r"s'etouffe",
    # stroke
    r"stroke", r"avc", r"face (is )?drooping", r"slurred speech",
    # bleeding
    r"(severe|heavy|heavily|lots of|a lot of) bleed(ing)?", r"bleeding (heavily|a lot)",
    r"won't stop bleeding", r"saigne (beaucoup|abondamment)", r"saignement (abondant|important)",
    r"hemorragie",
    # consciousness, seizures, poisoning
    r"unconscious", r"unresponsive", r"passed out", r"fainted", r"inconscient",
    r"perte de conscience", r"(s'est )?evanoui", r"seizure", r"convulsions?", r"overdose",
    r"surdose",
    # self-harm
    r"suicid\w*", r"kill (myself|himself|herself)", r"me tuer",
]
_EMERGENCY = re.compile(r"\b(" + "|".join(EMERGENCY_PATTERNS) + r")\b")


def normalise(message):
    """Lower case, accents removed, curly apostrophes made straight, spaces collapsed."""
    text = unicodedata.normalize("NFKD", message or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower().replace("’", "'")
    return " ".join(text.split())


def mentions_emergency(message):
    """True if the message contains one of the emergency phrases above."""
    return bool(_EMERGENCY.search(normalise(message)))

import pytest

from app import red_flags


@pytest.mark.parametrize(
    "message",
    [
        "chest pain and trouble breathing",
        "My dad has CHEST PAINS",
        "I can’t breathe",  # curly apostrophe, as phones type it
        "she cannot breathe properly",
        "he is short of breath",
        "I think my mother is having a stroke",
        "my friend passed out and is unresponsive",
        "cut my hand, severe bleeding",
        "my son is having a seizure",
        "I want to kill myself",
        "j'ai une douleur à la poitrine",
        "Mon père fait un AVC",
        "il ne respire plus",
        "mon bébé s’étouffe",
        "perte de conscience depuis 5 minutes",
        "idées suicidaires",
    ],
)
def test_emergency_phrases_are_found(message):
    assert red_flags.mentions_emergency(message)


@pytest.mark.parametrize(
    "message",
    [
        None,
        "",
        "my 6-year-old has a high fever",
        "I twisted my ankle",
        "j'ai mal au ventre",
        "heatstroke last summer, now a rash",  # "stroke" only as a whole word
        "sore throat, breathing is fine",
    ],
)
def test_ordinary_messages_are_not_flagged(message):
    assert not red_flags.mentions_emergency(message)

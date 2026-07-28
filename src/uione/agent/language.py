"""Answering in the language the person actually speaks.

An enterprise assistant that only speaks English is one that half an
organisation writes to in English and the other half stops using. The models
here are multilingual already; what the product has to get right is the boundary
between *prose*, which should be translated, and *identifiers*, which must not
be.

**The measured problem.** Asked a Turkish question about two incidents and given
English tool output, the models sometimes answer beautifully in Turkish and drop
the incident numbers — `INC0010001` becomes "the card settlement incident". The
prose is correct and the answer is useless, because the number is what you type
into ServiceNow.

Measured over six runs per configuration at temperature 0.7, on the tool output
in `docs/EVALS.md`:

| model | plain prompt | with the rule below |
|---|---|---|
| `ministral-3:8b` | both identifiers kept in 4/6 | 6/6 |
| `gemma4:e4b` | 5/6 | 6/6 |

So the rule helps, measurably. Six runs is not a proof, and **that is exactly why
identifiers are also structured fields** — the same defence the brief uses for
`complete` and `unavailable`. A UI that renders `structured["keys"]` shows the
incident numbers whatever the prose did.

**What counts as an identifier.** Anything a person types into another system to
find the thing again: ticket keys, incident numbers, message ids, email
addresses, channel names, file paths, and status values. Status is the
non-obvious one — "In Progress" is a value in a dropdown somewhere, and a user
searching for "Devam Ediyor" in ServiceNow finds nothing.

**Why not detect the language ourselves.** A detector is another dependency,
another model, and another thing to be wrong about a two-word message. The model
already knows; it is told to match the user rather than asked to report what it
found. Proactive output is the exception — a morning brief has no user message
to match, so it uses a stated preference.
"""

from __future__ import annotations

#: The rule appended to every prompt where a person reads the output.
#:
#: Phrased as a statement about identifiers rather than a translation
#: instruction, because "translate accurately" is advice a model cannot check
#: itself against and "these tokens appear exactly as given" is.
LANGUAGE_RULES = """
Language:
- Reply in the same language the user wrote in. If their message mixes \
languages, use the one the question itself is in.
- Identifiers are copied exactly and never translated, localised or omitted: \
ticket keys, incident numbers, message ids, email addresses, channel names, \
file paths, and status values as the system reports them.
- Everything else is prose and should read naturally in the user's language.
- When a status has been given in English, keep it and add the translation in \
brackets if it helps — the English value is what appears in the system itself."""

#: Used where there is no user message to match: the morning brief, the weekly
#: review, anything the scheduler produces.
PROACTIVE_LANGUAGE_RULE = """
Language:
- Write in {language}.
- Identifiers are copied exactly and never translated or omitted: ticket keys, \
incident numbers, email addresses, and status values as the system reports them."""

#: Languages named rather than coded, because the instruction goes to a model
#: and "Turkish" is a better prompt than "tr". Extending this list is not the
#: only way to add a language — an unknown code is passed through as-is, which
#: works for anything the model knows and keeps this from being a gate.
LANGUAGE_NAMES = {
    "en": "English",
    "tr": "Turkish",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "nl": "Dutch",
    "pt": "Portuguese",
    "pl": "Polish",
    "ru": "Russian",
    "ar": "Arabic",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
}


def language_name(locale: str) -> str:
    """Turn a locale into something worth putting in a prompt.

    ``tr-TR`` and ``tr`` both give "Turkish". An unrecognised code is returned
    unchanged rather than replaced with English: a deployment using a language
    this list has not heard of should get its own language, not a silent
    downgrade to the one the author happened to speak.
    """
    code = (locale or "en").strip().replace("_", "-").split("-")[0].lower()
    return LANGUAGE_NAMES.get(code, code or "English")


def with_language_rules(prompt: str) -> str:
    """For prompts answering a person who just wrote something."""
    return prompt.rstrip() + "\n" + LANGUAGE_RULES


def with_proactive_language(prompt: str, locale: str) -> str:
    """For prompts with no user message to match.

    English gets no instruction at all. Telling a model to write in English when
    everything it has been given is already English spends context on a
    tautology, and an unnecessary instruction is one more thing to be partially
    followed.
    """
    name = language_name(locale)
    if name == "English":
        return prompt
    return prompt.rstrip() + "\n" + PROACTIVE_LANGUAGE_RULE.format(language=name)

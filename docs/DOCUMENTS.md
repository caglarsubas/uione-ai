# Writing documents

The last capability from the original brief, and the one where the write is the
easy part.

## The question nobody asks and everybody needs

**Who can read this now?**

The answer is frequently not the one the author assumed. The assistant runs as a
service account, so a file it creates is owned by *that* account, with *that*
umask, in a directory whose group and mode it did not choose. Alice asks for a
document; it lands owned by `uione`, group `staff`, mode 0640, in a folder her
team cannot traverse — and she is told it was saved. She finds out it is
unreadable when somebody needs it.

So the connector derives the ACL of the file it just wrote, from the filesystem,
and reports it. Not the ACL it intended — the one that exists:

```
Saved documents/Odeme-mutabakati-postmortem.md (186 bytes).
Readable by everyone in the organisation.
```

and when the answer is nobody:

> Nobody can currently read it: the file's permissions, or those of a directory
> above it, exclude everyone. It is saved but unreachable.

`readable_by_nobody` is a structured field, not only prose. The recurring finding
in [EVALS.md](EVALS.md) is that models omit caveats they were told to include, so
the UI renders the field rather than trusting the sentence.

## Four refusals

**Outside the root, never.** A title is attacker-influenced text — it can arrive
from an email the model was summarising — and `../../../etc/cron.d/x` is a
document name. Two checks, because they catch different things: the assembled
path is checked before anything is created, and the resolved directory is checked
after, since a symlink is the other half of the same attack.

**An existing file, never.** Overwriting destroys work with no undo. Silently
writing `report-2.md` when somebody asked for `report.md` is the kind of
helpfulness that loses a document, so a collision is reported instead.

**Nothing enormous.** A model in a loop writing a gigabyte into a share is a
plausible Tuesday.

**No empty documents.** A zero-byte file with a confident filename is worse than
an error, because it looks like success.

## Filenames in languages that are not English

NFKD splits an accented letter into a base plus a combining mark, so `é` and `ö`
survive the ASCII step as `e` and `o`. It does nothing for letters that are not
accented forms of anything, which then vanish entirely:

| Title | Without transliteration | With |
|---|---|---|
| `Ödeme mutabakatı` | `Odeme-mutabakat` | `Odeme-mutabakati` |
| `Straße größe` | `Strae-groe` | `Strasse-grosse` |
| `Łódź raport` | `odz-raport` | `Lodz-raport` |

Turkish dotless `ı`, German `ß`, Polish `ł`, Nordic `ø` and `æ`, Icelandic `þ`
and `ð` are all distinct letters rather than decorated ones. A filename that
silently drops letters from the author's own language is a document they cannot
find again.

## Provenance

Every document carries front matter naming what produced it:

```yaml
---
title: Ödeme mutabakatı postmortem
generated: 2026-07-28T09:31:02+00:00
generated_by: UiOne assistant
requested_by: uione
---
```

An assistant-written document that looks hand-written is one nobody can audit
later.

## Why the share and not a private store

Because the document is then indexed by the same pipeline as everything else,
under permissions derived from where it actually landed — so it is findable by
search immediately, and it obeys the same ACL rules as a document a person wrote.
A private store would need its own permission model, and a second permission
model is a second place to be wrong.

## Risk

`REVERSIBLE_WRITE`. It creates a new file and refuses to overwrite, so deleting
it restores the world exactly. That is what reversible means here — and it is
what lets the tool eventually earn unattended execution through the
[autonomy ladder](SECURITY_MODEL.md).

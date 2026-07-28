# UiOne AI

## Design System

Always read DESIGN.md before making any visual or UI decision.
All typography, colour, spacing, layout and motion are defined there.
Do not deviate without explicit user approval.
In QA mode, flag any code that does not match DESIGN.md.

Three rules that are load-bearing and easy to break by accident:
- Colour means consequence. Chrome is achromatic; a saturated pixel means
  something changed a real system, is about to, or is on fire.
- Amber appears in exactly three places: the approvals count, the left edge of a
  held action, and the composer's "this will write" indicator.
- Identifiers are mono and never truncate. Titles are what you throw away when
  the window narrows.

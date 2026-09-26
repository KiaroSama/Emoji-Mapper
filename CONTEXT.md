# Numera Emoji Mapper

A toolset for building and curating Telegram premium custom-emoji packs, which
anyone can run with their own bots and their own packs.

## Language

**Numera Emoji Mapper**:
The name of this project and of everything the repository ships.
_Avoid_: Numera Emoji Mapper

**Operator**:
The person running one installation, with their own bots, packs and brand.
_Avoid_: owner (for the person running an installation)

**Bot identity**:
The Telegram username of a bot an operator runs. Operator configuration, never
part of the repository.
_Avoid_: bot name

### Packs

**Pack family**:
Every pack published under one pack base; the unit one publisher may change at
a time.

**Pack base**:
The operator-chosen prefix shared by every pack name in a pack family.

**Pack name**:
Telegram's permanent short name for one pack: the pack base, a number, and the
bot identity that created it.
_Avoid_: set name (Telegram's API term, fine in code)

**Pack title**:
The display name Telegram shows for a pack; it can be changed later, unlike the
pack name.

**Brand logo**:
The operator's own image, published as the first emoji of every new pack from
a bot that carries it. Operator configuration, never part of the repository.
_Avoid_: YourBrand logo

### Curation

**Catalog**:
The operator's collection of emoji, deduplicated by content, from which packs
are built.

**Sandbox**:
A disposable copy of the catalog that the curate panel can serve without any
risk to the real one.

# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed - an emoji joins the pack it was dropped on, and a selection can be seen

- **A drop reads the card you aimed at, not the card it landed above.** Those
  differ at every pack boundary: dropping a held emoji on pack 4's FIRST card
  stamped it pack 3 and filed it as pack 3's last slot. The destination is now
  taken from the card under the pointer while the drag is live.
- **A drag inside the grid re-stamps too.** Only drops from the holding tray
  did, so an emoji carried from pack 1 into the middle of pack 4 kept pack 1 and
  SPLIT pack 4 into two runs with a false separator between them.
- **Nothing is inferred when nothing was aimed at**, and an emoji dropped into a
  run with no pack number yet keeps no number, so it continues that run.
- **A click anywhere on a grid card selects it** in selection mode, with
  Shift-click taking the range. The grid ignored the click entirely, leaving a
  1.7em box as the only target on a 140px card; the tray was fixed for this and
  the grid was not.
- **A picked card wears a bright moving ring.** The flat inset outline read as
  one more dark line among the format accent colours. The rotation is a
  transform, so it costs no repaint, and it stops under `prefers-reduced-motion`.
- A drop that changes only the pack now records history, so it can be undone.

### Fixed - the holding tray can be selected from and moved in bulk

- **A pick box now means what it shows.** It drew the same check whether or not
  the card was picked and only changed colour, so an unpicked box looked ticked
  and two states were told apart by colour alone. Unpicked is empty now; the
  check appears only when picked, on grid cards and held cards alike.
- **A held card is selectable by clicking it.** Only a ~17px box responded, so a
  click on the card did nothing and selecting a run meant aiming twice.
  Shift-click takes the whole range. Dragging is unaffected -- a drag emits no
  click, so the gestures cannot collide.
- **Several held emoji move in one drag.** The machinery was already right; it
  was unreachable, because a multi-selection could not be made by hand.
- **The test sandbox starts again.** It held the clone's own catalog lock for
  the server's lifetime, so the panel it started was refused its own database
  and exited; and it cleared the environment before reading it, leaving the
  process without even `PATH`. It had served nothing since it shipped.


### Fixed - a save is answered for what it actually submitted

- **A stale Save is refused instead of quietly shrunk.** The panel intersected
  the tab's scope with the server's shared view and applied whatever was left,
  answering `{"ok": true}` for a decision it had silently narrowed. The whole
  submitted scope is now validated against the live catalog under the writer
  lease: it applies completely or returns 409 with nothing written, and the
  draft can be exported before reloading.
- **The webhook validates its envelope before dispatching.** Parsing proved the
  body was JSON, never that it was an update, so a malformed authenticated body
  reached the dispatcher. Only `update_id` is required, so unknown future update
  types stay valid.
- **The test sandbox no longer hands the panel real credentials.** It cloned the
  catalog and then passed its own environment through untouched, so a sandboxed
  panel still reached live Telegram as the real bot. It now runs with dotenv
  reading off and every credential-shaped variable stripped.
- **The test sandbox can no longer be argued into serving the real catalog.**
  Unknown options were forwarded to the panel AFTER the wrapper's own
  `--data-dir`, so one passed through won. Arguments are an allowlist now, with
  abbreviations refused and nothing forwarded.
- **The sandbox clone shares no bytes with the original.** Media was
  hard-linked, a path outside the collection was left pointing at the owner's
  archive, and the database was copied as a file, which skips an uncheckpointed
  WAL. It is now an online snapshot plus per-file copies with every path
  rewritten, `pack_plan.json` included, and it refuses rather than finishing a
  clone it cannot make faithfully.
- **Starting a sandbox no longer deletes another one that is running.** The
  start-up sweep removed every directory matching its name prefix. It now
  requires both a directory-bound `.sandbox-owner.json` and the ability to take
  that directory's lock, and it keeps the lock file when it reclaims.


### Fixed - saved curation survives, and the writes that carry it are bounded

- **A saved pack move now survives a reload.** The page rebuilt its layout from
  live Telegram membership alone, so an emoji moved to another pack reappeared
  at its old one and the next save erased the move. Intent is read back from
  `pack_plan.json` and overlaid on the render; the server's own view of what is
  live is unchanged, so `from_pack` still means what it says.
- **A partial view no longer discards decisions it cannot see.** A page showing
  part of the catalog replaces only its own scope and merges the rest. The plan
  gained `targets` (the authoritative intent), `known` and `excluded` (the scope
  those decisions were made in) and `logo_slots`; `moves` is now derived from
  `targets`. Plans written by an older panel are still read. See
  `docs/GUIDE.md` § "The move plan" for the full shape.
- **A Save carries one immutable body.** The request used to be rebuilt from the
  live model on every attempt, so a failed save for pack 1 could retry as pack 2
  after an unsaved edit. Pack-only edits now count as unsaved work for the dirty
  marker and the close warning, and a refusal is matched to the body it refused.
- **An HTTP 200 is no longer taken as proof on its own.** A success status with a
  malformed, null or never-finishing body cleared pending work and said "Saved".
  The client now requires a complete acknowledgement and retries otherwise.
- **The inclusion write and the plan write share one catalog lease.** Releasing
  it between them let maintenance re-key the database and then receive a fresh
  plan full of obsolete keys. Keys are validated against the database while it
  is owned; a plan I/O failure answers 503 instead of acknowledging.
- **Migration and rollback now cover the curation plan.** Their inventory
  enumerated `publish_*.json` only, so a re-key left the plan naming keys that
  no longer existed and the rollback bundle contained no plan at all. One shared
  inventory remaps only schema-defined ID fields; free text is left alone.
- **SQLite backups stop waiting forever.** `sqlite3.connect(timeout=...)` does
  not bound `Connection.backup`'s own BUSY retry loop, so a locked database
  could block a snapshot indefinitely. Capture is bounded at 30 s. Rollback
  writes onto the live catalog and is bounded far higher, because abandoning a
  restore half-written is worse than waiting out a competing reader.
- **A video sample cache could return another file's pixels.** Its key used a
  relative filename plus size and mtime, so two `clip.webm` files with matching
  metadata in different working directories collided. Keys now use the resolved
  path and include the FFmpeg identity the samples were decoded with.
- **Native Windows now has its own CI job**, covering locks, migration and
  restore, HTTP persistence, video identity and the CLI contracts — paths that
  Linux and WSL do not exercise. Which suites belong there is a judgement rather
  than a property of the code, so each one declares `RUNS_ON_NATIVE_WINDOWS` and
  `tests/test_ci_coverage.py` enforces the match in both directions: a marked
  suite missing from the job, and a name in the job that no longer marks itself,
  each fail.
- **A saved plan no longer grows forever.** Decisions about emoji the catalog no
  longer holds are dropped on the next save, checked against the database under
  the same lease that writes the plan. Previously every out-of-scope key was
  kept unconditionally, so a long-lived catalog accumulated dead entries that
  still counted toward a pack's total and could report a false `over_capacity`.

### Changed - relicensed under the GNU GPL v3 or later

- **`LICENSE` is now the GNU General Public License v3**, replacing the previous
  proprietary All Rights Reserved terms. Everyone may use, study, modify and
  redistribute this project, provided derivative works carry the same freedoms.
  The licence text is the canonical one, verbatim and unedited.
- The declaration was corrected everywhere it appeared, not just in `LICENSE`:
  `README.md` carries the standard notice, `docs/GUIDE.md` no longer calls the
  repository private or All Rights Reserved, `worker/package.json` gained
  `"license": "GPL-3.0-or-later"`, and the contributing guide and pull-request
  template now welcome outside contributions under the same terms instead of
  requiring a written agreement and a copyright assignment.
- Every runtime dependency was checked for compatibility first: requests
  (Apache-2.0), Pillow (MIT-CMU), resvg-py (MIT), numpy (BSD-3/0BSD/MIT/Zlib/
  CC0) and rlottie-python (LGPL-2.1, upgradeable to the GPL by its own terms).
  Third-party logos and provider data keep their own licences, which this change
  does not and cannot alter.

### Changed - CI runs on GitHub-hosted runners

- **The workflow moved from a self-hosted WSL runner to `ubuntu-latest`.** The
  self-hosted runner existed because this account's hosted minutes are blocked
  for private repositories; they are free for public ones, so publishing the
  repository removed the reason. A self-hosted runner is also incompatible with
  a public repository on purpose: a public repo accepts pull requests from
  anyone, and a fork's workflow would execute that code on the owner's own
  machine, so the runner was deregistered before publication.
- Consequences now baked into the jobs: ffmpeg and Chromium's system libraries
  are **installed** by the jobs rather than assumed from a global setup, and the
  ffmpeg step verifies the binaries actually resolve afterwards — apt can answer
  a mirror outage by installing nothing and still exiting 0, which surfaces
  three steps later as an application error naming the wrong component.
  Dependency caching is on, and the timeouts are ceilings sized for the smaller
  two-core hosted machines.

### Fixed - the holding tray, cross-pack drops and cold previews

- **Several held emoji can be picked at once.** The pick box lives on a grid
  card and a held emoji has none, so the tray was the one place selection mode
  could not reach and held emoji moved one Unhold at a time. Held cards now
  carry the same pick box: click plus shift-click takes a run, dragging one
  picked card carries every picked held emoji, and Unhold on a picked card
  returns the whole set.
- **An emoji dropped into another pack now joins it, and Save writes the plan.**
  The panel states the intended layout rather than mirroring Telegram: drag an
  emoji into the pack it should end up in, and it is re-stamped into that pack
  instead of being silently re-grouped by the pack it came from — which is what
  made the drag look broken. Nothing moves on Telegram at that moment and
  nothing can (the Bot API has no move-between-sets call), so **Save now writes
  `<data-dir>/pack_plan.json`**: every emoji that changed pack with its `from`
  and `to`, every emoji parked in the tray with the pack it came out of, the
  resulting per-pack counts, and any pack over capacity. The real
  rearrangement is a separate deliberate step read from that file. Packs stay
  fixed 200-emoji buckets: a full one refuses the drop and names the gesture
  that makes room — park one of its emoji in the tray, then bring the
  replacement in. Capacity is now asked of the destination pack; it used to be
  asked of the emoji's own, which refused moves out of a full pack and allowed
  moves into one.
- **Previews are warmed in the background and render wider.** One animation
  costs ~155 ms and one video poster ~613 ms, and the render bound was a
  hard-coded 2, so a cold tier arrived in visible chunks while the owner
  scrolled. The bound is resource-aware now (24 cold animations: 2.92 s to
  1.55 s on 16 cores) and a daemon thread renders the page's previews in grid
  order ahead of the scroll.

### Fixed - curation and coordinated identity recovery

- Held cards leave the grid and pack counts; Unhold/Unhold all restore positions
  with capacity checks. Shift range picking, complete Undo/Redo and a last-Save
  Reset checkpoint cover the curation workflow.
- Newer saves survive older permanent refusals. UI action/error logging is
  bounded, and conflict warnings offer a local draft export.
- Compact previews reduce size/frame rate, covered animations stop, and inactive
  video players release their decoders. Rendering work is limited per server.
- Video merges and recovery verify native timelines, including unmatched tails;
  colliding sampled keys preserve distinct files and identities.
- Migration excludes permanent writers, replays evidenced per-file operations,
  refuses unrelated destinations, preserves an application rollback bundle and
  requires complete final invariants before success.
- Nine support modules moved into the shared package; public CLI paths stay
  stable. CI covers the new browser/native regressions.

### Added - a holding area for parked emoji

- **A holding area in the curate panel.** Drag an emoji onto it to exclude it
  from the next publish without hunting for its tick in a long grid; drag a
  parked one back into the grid to re-include it at the exact position
  dropped. Reuses the existing `included` flag — no new state, no backend
  change — rendered in its own strip inside the sticky header instead of
  wherever the card's position happens to be.
- **Zoom now takes an exact percentage.** `zoomReset` is a typeable field:
  type a value and press Enter to apply it, double-click still resets to
  100 %.

### Fixed - selection mode looked broken, and a pack's own count lied

- **Select all / Deselect all / Invert only ever touched publish inclusion.**
  While selection mode was on (picking several emoji to move together), these
  three buttons had no visible effect on the picks at all — nothing on
  screen responded, which read as the mode being broken outright. They now
  drive the picks while selection mode is on, publish inclusion otherwise.
- **The pick checkbox was easy to miss.** Its resting-state border and glyph
  used the same near-invisible colour as the card's own border; it now uses
  the same contrast the publish tick already has.
- **A pack's own upper bound could overcount by exactly what was excluded
  from its tail.** It was computed from the raw size of the underlying array,
  which counts an excluded item sitting past the pack's real end as if it
  still occupied a slot. It is now the last actually-included item, wherever
  it sits — a pack that is not the last one is unaffected, since it correctly
  keeps drawing on whatever candidates follow to stay full.

### Fixed - the launcher's yes/no prompts

- **A "yes" answer at four launcher prompts silently acted as "go back".**
  `Ask-YesNo` returns a bare `[bool]` for a plain yes/no or the string `'back'`
  for an explicit `0`; comparing a `[bool]` to `'back'` with `-eq` coerces the
  string operand to match the bool side (`'back'` becomes `$true`), so
  `$true -eq 'back'` is `True`. Every "yes" — including the default on a bare
  Enter — read as "back". B4 (open the curate panel) was the visible case: an
  answer of "yes" to "show the published packs too?" silently returned to the
  menu with no error, and "no" fell through correctly but then opened the
  panel without `--with-pack`, showing 1 catalog emoji instead of every pack.
  The same anti-pattern also affected A1 and A3's "run it now?" confirmation
  and B3's "dry-run then upload now?" — a "yes" there stepped the wizard back
  one screen instead of proceeding. Fixed at all four sites by checking the
  return type instead of relying on implicit string/bool coercion.

### Fixed - a second audit: identity, migration and the panel's queues

- **Video identity now fails closed.** A missing `libvpx-vp9`, a failed codec
  probe or a container naming no codec used to answer "no decoder needed",
  which drops VP9's separate alpha layer — and a probe failure was CACHED, so
  one transient error made every later decode of that file lose its alpha
  silently. Two clips differing only in opacity then share one content key, and
  `Catalog.add` merges them and deletes the file it merged away. All three paths
  now raise `UndecodableVideo`, and nothing undecidable reaches the catalog.
- **Comparing two videos walks the timeline.** `same_image(..., "video")` read
  frame zero and nothing else: two clips sharing ten opening frames compared
  equal. It now compares every sampled frame at 30 fps with no averaging, and no
  longer accepts content-key equality as proof for video — the identity stream
  is sampled at 10 fps, so a change shorter than one interval falls between its
  frames.
- **A content-key migration is one versioned change** (`collection_migrate.py`).
  The previous round moved three tables and stopped, leaving the publisher's
  state file naming keys the catalog no longer had — which `reconcile_set` reads
  as a reordered pack — plus stale derived hashes and archived filenames still
  carrying the old key. Every durable reference now moves together, under the
  pack-family lock, behind a verified snapshot taken with SQLite's online backup
  API (`copy2` of a WAL database opens as `no such table: items`). Stages are
  idempotent and journalled, so an interrupted run resumes and a second run is a
  verified no-op. `--from-backup` repairs a migration that ran before journals
  existed, pairing rows by content rather than position.
- **A failed inspection is not a success.** A row whose media was missing was
  counted as "unchanged", so a catalog nobody could read printed "Every video
  key already matches" and exited 0. Outcomes are counted apart and exit 4 means
  incomplete — distinct from clean (0) and from pending (3).
- **Unique attribution requires excluding every rival.** With two candidates
  matching one live sticker the resolver correctly reported ambiguity; deleting
  one candidate's FILE made it answer "unique" with the survivor.
- **The panel tells unsaved work from an in-flight request.** An acknowledgement
  of an older snapshot used to clear the dirty state, show "Saved" and drop the
  navigation guard while newer ticks were unsaved. It now tracks what the server
  has acknowledged, marks the Save button, and guards the tab until the visible
  selection is stored.
- **A refusal that retrying cannot fix stops being resubmitted.** HTTP 400/409
  ended the retry loop its own comment said could not help; the five-second
  heartbeat now honours the backoff instead of bypassing it; and a request that
  never resolves is released by a raced deadline rather than claiming the queue
  for the life of the page.
- **The Worker cannot leak a bot token through an error.** Every Bot API URL
  embeds it and a transport failure names the URL, which reached the HTTP 502
  body and every log sink. One redactor now covers the URL shape, the bare token
  shape and the configured secrets, on every path out.

### Fixed - identity, locking and validation defects found by an external audit

Fifteen numbered findings and eight adjacent-path items, each reproduced against
real artifacts before it was fixed. The ones that could corrupt data:

- **Two publishers could hold one pack-family lock.** Ownership was decided by
  reading a file, comparing a token and unlinking it — and an unlinked inode is
  a lock nobody else can see, so the stale-lock recovery path could seat two
  processes at once. Measured: two real processes inside one lock for 1.538 s.
  The lock is now an OS-level byte-range lock (`msvcrt.locking` on Windows,
  `flock` elsewhere) held for the whole critical section, and the file is never
  unlinked. The recorded holder is still written, but only as a hint for the
  "who has it" message.
- **Recovery could give a stranger's picture our identity.** The nearest
  candidate by perceptual hash was accepted outright, and dHash is a *grayscale
  structure* hash: an opaque red square and an opaque blue square are zero
  apart. A perceptual match may now only nominate; `same_image` decides, more
  than one verified match is `AMBIGUOUS`, and a candidate that could not be
  examined is `UNDECIDABLE`. Both refuse instead of guessing.
- **Video identity discarded alpha, and the two fingerprint APIs disagreed.**
  ffmpeg's default `vp9` decoder drops the separate alpha layer in silence, so
  two clips identical but for opacity shared one content key; and `fingerprint`
  and `perceptual_hash` sampled differently, so one 30 fps clip had two hashes.
  The decoder is now chosen from the **probed codec** (never the extension) in
  one new module, `emojikit/video_decode.py`, which both callers share — with a
  bounded frame cache that also cut the identity suite from 163 s to 48 s.
- **Fitting an image applied its alpha twice.** `fit_100` pasted the image using
  itself as the mask, compositing against the transparent canvas underneath:
  `(255, 0, 0, 128)` came out `(128, 0, 0, 64)`.
- **Preflight reported acceptances it never obtained.** The counter counted
  attempts, so a run in which every `check_uploadable` failed at the transport
  printed "all accepted" and exited 0. Accepted, refused, missing and
  *not checked* are now counted apart (`collection_preflight.py`). A refusal or
  a missing file exits 1; an unreachable Telegram exits 3, whether it happened
  to one file or to all of them — exit 1 is what "Telegram said no" means, and
  answering a dropped connection with it sends someone editing artwork that was
  never the problem.
- **A stale panel tab spoke for the whole catalog.** `/api/save` carries the
  full selection, so a page opened before another change re-included rows it had
  never seen — and answered `{"ok": true}`. The request now states the scope it
  was showing; a page too old to say gets 409 and one reload.
- **A Lottie timeline that was not a number passed validation**, because every
  comparison against NaN is false. Repainting also silently skipped a keyframed
  colour, and overwrote a gradient's opacity ramp with colour when the stop
  count was inferred as `len // 4` instead of read from `g.p`.
- **A set that closed mid-run was still appended to** on the next item, and the
  blank-media check received the family's format rather than the item's, so
  `--mixed` bypassed it.
- **The Worker accepted an unvalidated publish body**, and a note over the
  chunking limit escaped it.

Existing catalogs are **not** migrated implicitly: the corrected video decoder
changes what a content key IS, so `scripts/identity_repair.py` reports the
affected rows and moves them only with `--apply`, refusing outright on a
collision or an undecodable file. Ten new test modules cover the above, plus
`tests/test_panel_browser.py`, which drives the real panel in headless Chromium
(`requirements-dev.txt`). `panel.py` was over the 800-line ceiling after the
save-scope fix; its view model moved to `panel_view.py`.

### Changed - the curate panel is a virtual grid, and it zooms

The panel built every card up front. With six published packs open that was
1 068 cards, and every drag step re-laid-out all of them (14-17 ms per pointer
move, measured), while 56 `<video>` elements each held a media player from the
moment the page loaded. Now only the rows within half a screen of the viewport
exist; two spacers carry the height of the rest, every card is one fixed
height, and every row offset is integer arithmetic, so a render is a binary
search plus a hundred node moves whatever the catalog holds. Drag edits the
order as you drag and re-projects the grid — the drop records, a cancel
restores — and a video card gets its source only while it is near the viewport.
Measured in headless Chromium on that catalog: drag frames 24-46 ms → 16-24 ms
with none over two vsyncs, cold-scroll long tasks 802 ms → 113 ms, 11 743 DOM
nodes → 714.

**Zoom**: `−` / `100%` / `+` in the header, Ctrl+wheel, Ctrl+plus/minus/0.
Out fits more emoji per screen (below 75 % the text under each thumbnail is
dropped so rows pack tighter); in enlarges one. The level is remembered and
the emoji at the top of the screen stays there across a change.

The behaviour moved out of `assets/panel.html` into `assets/panel-grid.js` and
`assets/panel-actions.js`, served from `/static/` under a content-hash `?v=`
so an edited script is never served stale from the immutable cache.
`scripts/panel_sandbox.py` now forwards `--all` / `--with-pack N` to the panel
and clones the publish state, so a sandbox can show published packs too.

### Changed - the roster page keeps the panel's view-only controls, and gates its media

Stripping "everything that mutates" from the roster page took the header
controls with it. Top, Bottom, the backdrop cycle and the animation switch
change no data - they are how you SEE a grid of a thousand emoji, and the
backdrop in particular is what makes a black or a white emoji visible at all.
They are back, in the panel's own markup. Everything that writes is still
absent: no drag, no tick, no selection, no save, no form field.

The page also gates its media the way the panel does, which is the real answer
to "heavy": every animated card now inlines BOTH a still and the animation,
starts frozen, and only what is on screen is swapped to the moving version.
Scrolling freezes everything until 180 ms after it settles, a hidden tab
freezes everything, and `Animation: Off` freezes everything permanently. Before
this the page decoded 156 animations at once on load.

Thumbnails also dropped from 96px/12fps to 88px/9fps: a roster is for telling
emoji apart, not for admiring the motion, and fps is the single biggest lever on
the inlined weight. The largest page went from 10.3 MB to 7.4 MB.

**A misdiagnosis worth recording.** The gating measured as never starting an
animation, and the first explanation - that `content-visibility: auto` stops a
child from producing IntersectionObserver records - was wrong. The test browser
reported `visibilityState: "hidden"` with `innerHeight: 0`, where nothing
intersects anything and no animation *should* start. The observer now watches
the card rather than the image, which is defensible on its own terms, but the
freeze half is the only half that environment can prove.

### Changed - the roster page is the curate panel, minus everything that mutates

`packs/<set>.html` now uses the panel's own visual language: the same grid, card,
per-format accent (static cyan, animated violet, video green, logo amber),
position pill, format badge and checkerboard thumbnail with its format outline.
The two read as one product because they show the same objects.

What it deliberately does NOT carry is every control that changes something -
no `draggable`, no tick, no selection state, no save, no form field at all. It
is a record, and a control that looks live but saves nothing is worse than no
control. A test asserts that on the rendered markup rather than on the source,
because the stylesheet comment names the very attributes the page leaves out.

Each card now shows BOTH ids, labelled and individually click-to-copy: **this
pack** and, where the emoji came from someone else's, **original pack**. The
second one is what an external map may still be pointing at, so it is as
load-bearing as ours and gets the same affordance.

### Added - the glyph, in both grids

Every card in the roster page AND in the curate panel now shows the glyph its
sticker carries, under the artwork. Telegram never displays it - a custom emoji
renders as its picture - so these two grids are the only place the label can be
checked against the art it claims to describe, which is exactly how 21 wrong
glyphs were found. A sticker with no glyph shows an em dash rather than a gap.

Verified while adding it: across all 34 packs and 6587 emoji there is no row
with a missing or non-numeric id, no gap in any pack's slot sequence, and no id
appearing in two packs.

### Added - `packs/`, the roster of what is actually in every published pack

`pack_manifest.py --refresh` writes, per live set, `packs/<set>.json` (machine),
`packs/<set>.md` (human) and `packs/<set>.html` (a self-contained page showing
every emoji, animation included), plus `packs/index.json` and `packs/README.md`
over all 34 packs - the 5 general ones and the 29 crypto ones.

Each row carries the emoji's `custom_emoji_id`, its position numbered from 0
(so the brand logo is emoji 0) alongside the 1-based slot Telegram shows, its
format and glyph, a name, and - for anything taken from someone else's pack -
the id it had THERE. `index.json` also carries two flat lookups, `by_current_id`
and `by_source_id`, so "which of ours replaced that one" is one dereference.

**The order and the ids come from Telegram, never from the state file.** A pack
can be reordered live, and replacing a sticker mints a NEW id; a roster rebuilt
from the recorded plan would drift silently, which is the exact failure this
file exists to prevent.

Two details that were wrong in the older `collection/manifests` and are right
here: the brand logo now has its id (it has no catalog row, so anything walking
the catalog cannot name it - the live set can), and the "emoji 0 is the logo"
note is suppressed for the coin family, whose bot is exempt from the logo.

`packs/` is git-ignored: it is derived from Telegram on demand, and one refresh
rewrites ~79 MB of pages and cached thumbnails.

### Added - `Pack-Roster-Check`, a Stop gate that keeps the roster honest

A roster is only worth having if it is never stale, so this blocks the turn when
`packs/` stops describing its inputs - a new download, a replaced or recoloured
sticker, a reorder, a coin remap. The staleness test lives in ONE place,
`pack_manifest.py --check` (four stat calls and a small JSON read, no network),
so the hook and the tool cannot disagree about what stale means.

It honours `stop_hook_active`, blocks once per distinct input state so declining
to refresh cannot loop, re-arms the moment anything changes again, and fails
OPEN when the interpreter is missing or the check times out - a gate that blocks
on its own broken dependency is unclearable.

### Fixed - three things wrong with the curate grid

**Drag and drop moved the card somewhere you had not aimed.** The drop handler
did `splice(from,1)` and then `splice(to,0,…)` with a `to` measured BEFORE the
removal. Removing the card shifts everything after it down one, so dragging
DOWN landed it one slot past the tile you released on while dragging UP landed
it exactly there - one gesture, two behaviours - and nothing showed where it
would go until it was already there.

The card is now MOVED as you drag, so the grid is the proposal: it sits
translucent (`opacity:.35`, dashed outline) in the slot it would take, and the
drop only adopts the DOM order. Preview and result cannot disagree because
there is only one of them, and no index is computed at drop time. Which half of
a tile the pointer is on decides before-or-after, so the last slot of a row is
reachable. A cancelled drag puts the node back, because `ITEMS` never changed
and a grid showing an order it would not save is worse than no preview at all.

**The tick offered a hand for a drag.** `cursor` inherits and the card is
`cursor:grab` because it is also the drag handle, so the one control that reads
as a switch showed the wrong affordance. `.tick` is `cursor:pointer` now.

**Animation stayed heavy.** This grid holds 76 animated previews (median 36
frames each) and 15 videos. Two changes, both about not decoding what nobody is
looking at: the IntersectionObserver band drops from `120px` to `0px` (it was
animating roughly a row above and below the viewport), and everything holds
frame 0 while you scroll, thawing 180 ms after it settles. Scrolling is the one
moment the work is pure waste - the compositor is already busy and the frames
go past too fast to read. The still is the same immutable-cached URL, so
freezing and thawing costs no request.

### Fixed - the grid drew no boundary between two published packs

`--with-pack 2 --with-pack 5` returned both packs as one unbroken run of 192
cards. The splits were computed purely from capacity: count included items,
start a new pack every `PER_SET - 1`. That is right for the pack being BUILT,
where a flat candidate list really is sliced into equal packs. It cannot work
for packs that already exist: pack 2 holds 95 and pack 5 holds 96, neither is
199, so counting to capacity found no seam at all and `starts.length < 2`
returned before drawing anything.

An already-live emoji knows which pack it is in - `--with-pack` resolves that
through the publishers' own state files - so `packs_named()` now returns
`{name: index}` instead of just names, `build_view` tags each already-live card
with `pack`, and `renumber()` splits on a CHANGE OF PACK whenever any card
carries one. Capacity arithmetic still runs for a grid of pure candidates,
which is the case it was written for.

A candidate carries no pack, so a mixed grid groups them under "Not in a pack
yet" rather than claiming they belong to the last one. A dict is still a
container of names, so every membership test on `keep_sets` reads as before.

### Added - `--tint`, baking Telegram's repaint into the asset ourselves

`needs_repainting` cannot be set on an existing pack - the Bot API exposes it on
the `Sticker` object (read-only) and on `createNewStickerSet` (whole-set, at
creation) and nowhere else, it is not a field of `InputSticker`, and it is not
stored in the file. So the flag is out of reach. The *look* is not: a repainted
sticker is just its silhouette filled with one colour, and we can do that.

`fetch_emoji_ids.py --tint '#RRGGBB'` flattens every REPAINTABLE emoji in the
batch to that colour and leaves everything else alone. It answers
`--repaintable` on its own - once the repaint is baked there is nothing left to
warn about - and records `tint:#RRGGBB` on the item beside its `premium-id:`,
because a recoloured asset does not resemble its source and nothing else says
why.

`media.repaint_in_place()` does the work, next to `reencode_in_place()` and
under the same rule: it runs BEFORE the fingerprint, so the content key
describes what is on disk.

- **animated** goes through the Lottie tree, not the raster - flattening frames
  would throw the animation away. Solid fills, strokes and gradient stops are
  all rewritten; gradient OFFSETS are kept, so the ramp's shape survives.
- **static** fills through the original alpha. Anti-aliased edges survive, and
  so do cut-outs: the tick inside a verified badge is a HOLE, not a dark shape,
  which is why it still reads correctly against any background.
- **video** is refused rather than silently passed through, since a no-op here
  would publish untouched art under a name that claims otherwise.
- The .tgs size cap is re-checked afterwards; over it, the original is kept.

### Fixed - the repaintable warning claimed the art is black; measured, it often is not

`--repaintable` told the operator that a repaintable emoji's "stored art is
usually flat black" and "will look black" in one of our packs. That was
generalised from one sample, Telegram's built-in `TopicIcons`. Two ids from
`vector_icons_by_fStikBot` tripped the gate on a real ingest and were skipped on
that advice; rendering their Lottie showed a **blue** verified badge (mean RGB
62,162,222) and a **purple/pink** star (133,102,240). The flag does not mean the
art has no colour, it means the client OVERRIDES whatever colour is there - so
the source pack is precisely where you cannot see what you would be publishing.

The gate now says that, and says to look first. Nothing about its behaviour
changed: `ask` with no terminal still skips, because skipping is still the
reversible half.

Re-verified against the live Bot API while correcting this: `needs_repainting`
appears in exactly two places, the `Sticker` object (read-only) and
`createNewStickerSet` (whole-set, at creation, custom-emoji sets only). It is
**not** a field of `InputSticker`, so `addStickerToSet` cannot carry it, and no
setter exists. It is not a property of the file either - it cannot be injected
into an asset before upload.


### Fixed - dHash read RGB from underneath transparent pixels

`_dhash` called `img.convert("L")` on an RGBA image. **That conversion discards
alpha and reads the raw RGB**, and RGB beneath a fully transparent pixel is
undefined - every encoder rewrites it. The same trap owner rule 1 meets with
`exact=True`, and the one `_premultiplied` was written for; `_dhash` simply did
not use it.

Caught by a real upload, not by reasoning: a logo measured **10** dHash bits
from Telegram's re-encode of *itself* (tolerance 6), which failed the
upload-verification gate and stranded a sticker that had in fact landed. Its
alpha channel was byte-identical and its premultiplied colour delta was 1.23 -
deep inside the calibrated same-image range of 0.02..2.35. Premultiplied first,
the distance is **0**, and a genuinely different image still measures **23**.

- A fully opaque image is unaffected: premultiplying by 255 is the identity.
- 492 of 550 stored catalog hashes changed, because emoji art is nearly all
  transparent - those values had been describing encoder noise. They were
  self-consistent among files this pipeline produced, which is why near-duplicate
  search never looked broken: it just could not match one logo against another
  source's copy of the same picture.
- Three tests pin it: invisible pixels cannot move the hash, genuinely different
  art stays far apart, and an opaque image hashes exactly as before.


### Added - `sync_order.py --pack N`

Publishing **appends**; it cannot move a sticker that is already live. So after
arranging a pack in the curate panel and publishing into it, the live order
still has to be applied with `sync_order.py` - and without a filter that reorders
the WHOLE family. Arranging pack 2 and syncing found 43 moves there, but also 28
in pack 5 and 1 in pack 1, neither of which the owner had looked at.

`--pack N` (repeatable) limits the run. An unknown number is a usage error, not
a silent no-op that reports success having reordered nothing.

`setStickerPositionInSet` moves an existing sticker, so no `custom_emoji_id`
changes - verified after the live run: all 105 ids the bot inventories name
still resolve.


### Added — `--repaintable`, a gate on emoji Telegram paints itself

A repaintable emoji carries no colour of its own: the client paints it with the
text or accent colour, so the stored asset is typically flat black. Republished
into one of our sets — created without that flag, and the Bot API has no method
to add it afterwards — it arrives black. `5354899958329784877` from Telegram's
built-in `TopicIcons` went through exactly that and had to be replaced sticker
by sticker.

- `fetch_pack.py` and `fetch_emoji_ids.py` now take
  `--repaintable {ask,skip,keep}`, default `ask`. The check runs **before any
  download**, once per run, and names the ids.
- **The flag is on the Sticker, not the StickerSet.** `getStickerSet` on
  `TopicIcons` answers `None` at the set level while all 160 of its stickers
  answer `True`, so a gate that read the set would pass every one through.
- With no one to answer, the default is **skip**, not proceed: a skipped emoji
  is one re-run away with `--repaintable keep`, while one already published has
  to be replaced in a live set.
- **`isatty()` alone was not enough, and only a real run showed it.** Under Git
  Bash, `fetch_emoji_ids.py … < /dev/null` still reports a tty; `input()` then
  raised `EOFError` and killed the whole ingest with an uncaught traceback. A
  question nobody answered is now a no. The fakes missed it because they inject
  the prompt — the live run is what caught it.


### Added — `--repaint`, so a monochrome mark can look right

- Telegram's own marks (TopicIcons and friends) are stored BLACK and painted by
  the client, because their set carries `needs_repainting`. Republished into an
  ordinary pack they simply look black. `--repaint` sets the flag when a new set
  is created.
- **Creation-only and whole-set, both enforced by the API not by us:** the field
  exists on `Sticker` and on `createNewStickerSet` and nowhere else, so an
  existing pack can never gain it, and turning it on flattens every full-colour
  emoji in the same set. Repaintable art needs its own family.
- Absent by default, not `"false"` — a test pins that, because no existing pack
  may start claiming a value it never had.

### Changed — the last two files over 800 lines, and the worker's toolchain

- `coins/fetch_paprika.py` 801 → 703, with the CoinPaprika HTTP/search half in
  `coins/_paprika_api.py`. Chosen because it is the one block with no stake in
  the state paths the tests redirect; `QUOTA_EXHAUSTED` is an identity sentinel,
  so importing the name keeps `is` comparisons working, and a test asserts it.
- `tests/test_resume_safety.py` 807 → 467, with the unknown-live-state cases in
  `tests/test_unresolved_mutation.py`.
- `wrangler` 4.125.0 → 4.127.1 and `@cloudflare/workers-types` → 5.20260831.1;
  the `^` ranges already allowed both, so the manifest floors were raised to the
  versions actually tested. Worker typecheck clean, 40/40 vitest.


### Removed — one blank-image rule instead of four, and a constant that was a trap

- **`emojikit.media.is_blank_image` is now the only definition.**
  `make_emoji_pngs._is_blank` and `coins/_dedup_plan.is_blank` each carried
  their own copy with their own re-declared `> 10` / `<= 8` — the thresholds
  media names `VISIBLE_ALPHA` and `BLANK_MAX_VISIBLE` — and the coins one was
  the slow per-pixel form media had already moved away from. Both delegate now;
  `_video_is_blank` reads the named constants instead of two more literals. The
  pipeline's central quality gate had four definitions that could drift apart.
- **`logsetup.LOG_RETENTION_DAYS` deleted.** Its own comment said nothing that
  acts on the retention window may read it, because `load_env()` has not run at
  import — a constant kept only so a test could assert on the parsing it did.
  `log_retention_days()` is the accessor. With no parse at import there is no
  import-time failure left, so `LogRetentionParsing` went with it; every value
  it checked is already covered by `TestRetentionIsReadWhenItIsUsed` against the
  call-time path, and the one guarantee it held alone — a garbage value must not
  break the module, not merely return the wrong number — is folded in there.
- **`make_emoji_pngs._pick_sources` deleted**: no production caller. It was
  `[g[0] for g in _source_groups(...)]`, and the two tests now read
  `_source_groups` directly.

Suite 760 → 756: exactly the four methods of the deleted class.


### Changed — six files over 800 lines split along real seams

No behaviour change: 760 tests before, 760 after. Every cut runs one way, so
none of the new modules imports the one it was cut from.

- `emojikit/media.py` 978 → 713, with identity in `emojikit/identity.py`.
  Making a file and *identifying* one are different jobs, and conversion never
  called a hash. **`identity` imports the media MODULE, not its names**: a bare
  `from ... import _run` binds once at import, so a test patching `media._run`
  to count ffmpeg launches would no longer be seen — and that count is exactly
  what `fingerprint()`'s one-pass guarantee is pinned by.
- `build_collection.py` 1495 → 782, layered under `collection_state.py` (plan,
  resume state, brand logo) and `collection_reconcile.py` (what is live in a
  set, and whose key each sticker is). The reconcile block turned out to be a
  clean leaf: it called nothing defined below it.
- `build_pack.py` 1692 → 696, with `telegram_api.py` (the Bot API client, its
  errors and Telegram's caps), `packstate.py` (state-file shape, atomic write,
  pack-family lock) and `announce.py`. The client depended on exactly one name
  from the engine, `api_base`, which went with it.
- `tests/test_build_collection_state.py` 1250 → 627, with the CLI half in
  `test_publish_cli.py`; `tests/test_coin_providers.py` 858 → 467, with the
  unverified-upload recovery path in `test_coin_recovery.py`.
- `tests/test_panel.py` 1392 → 641, plus `test_panel_page.py` (assertions on
  the served document), `test_panel_guard.py` and `test_panel_server.py`.
  `MutationGuard` travels whole: it owns nine `test_*` methods and is
  subclassed twice, so sharing it would inflate the count, not move it.

`coins/fetch_paprika.py` (801) and `tests/test_resume_safety.py` (807) were
left alone: one and seven lines over is the small overage the size policy
exempts, and cutting either would produce a fragment, not a seam.

Two traps worth recording, both caught by the suite:

- **`str.splitlines()` is not `split("
")`.** It also breaks on U+2028/U+2029
  — which `test_panel.py` embeds deliberately, because escaping them is what
  `InertItemJson` tests. Every line number past them was off by four.
- **A monkeypatch must name the namespace the CALLER reads.** Retargeting
  `patch.object(build_pack, "Telegram")` to `telegram_api` left the real client
  in place, and the suite spent five real retry sleeps dialling the discard
  port. `announce_via_worker` is the mirror case: its only caller is
  `announce.announce_packs`, so it belongs on `announce`.


### Added — `--into-pack N`, so any pack with room can be topped up

- **Publishing always appended to the newest set**, so once a later pack
  existed, the room left in an earlier one was unreachable — a pack stopped at
  21/200 could never be filled again. `--into-pack 1` aims the run at that pack.
- **It fails loudly instead of falling back**, because filling some *other*
  pack would still look like success: an unknown number, a full pack, or one
  holding a sticker this publisher cannot identify each stop the run. The last
  is not fussiness — appending past an unattributable sticker is what hands a
  new key someone else's `custom_emoji_id`.
- `--new-set` and `--into-pack` together are rejected as contradictory.
- **The real hazard was bookkeeping, not upload.** The live count and the
  recorded key order were written to `fmt_sets[-1]`, which equals the target
  only while filling the newest pack; aiming at a middle one would have
  credited the upload to the wrong record — state describing a set the sticker
  never entered, the exact drift `reconcile_set` exists to catch. Both now
  follow the set actually written to, pinned by a test that fails (`4 != 0`)
  when the old expression is restored.

### Changed — the curate page is an asset, not a string literal in `panel.py`

- **`panel.py` 1622 → 737 lines.** 902 of them were one HTML/CSS/JS string
  literal, so the module only ever held 720 lines of Python. The page now lives
  in `assets/panel.html` and is read at import. Not a module split: no boundary
  moved, no import changed, and `panel.PAGE` is byte-identical (42 156 chars,
  same SHA) — verified by comparing the loaded value before and against after.
- Ruff stops linting a JS blob as opaque, editors highlight it as HTML, and a
  UI change no longer churns the diff of a Python module.
- **The page is resolved `ROOT`-relative, never absolutely**, and
  `ThePanelPageActuallyShips` pins it: the asset exists inside the repo, the
  loaded page still opens `<!doctype html>` and closes `</html>`, and all five
  `__PLACEHOLDER__` tokens the handler substitutes survive. An absolute asset
  path is exactly how the "mandatory" brand logo once vanished on every machine
  but one with no test noticing, so a truncated or missing file now fails loudly
  rather than serving a blank page.

### Fixed — an upload check that called every one of its own uploads a stranger

- **`Telegram._sticker_matches` confirmed an upload by exact content key.** That
  key is a SHA of exact pixels and Telegram re-encodes what it stores, so the
  comparison could never hold for our own upload — and the caller reads a
  negative as "a foreign sticker landed". A 449-emoji publish stopped on a
  sticker that had landed correctly. The path is rare (it needs a network
  failure to trigger the probe), which is why it took a 449-item run to surface.
- **`media.same_image` now owns the decision for every caller.** Exact key
  first, then a dHash *and* a colour check, both of which must agree.
  `build_collection._same_image` keeps only the fetch and delegates: two answers
  to "is this the same picture" drifting apart is how one caller starts
  accusing what the other accepts.
- **Neither signal is safe alone**, which the suite proved before this shipped:
  dHash survives the re-encode but is grayscale, so a stranger's green square
  sits 4 bits from our red one and a structure-only check reported it as ours.
  Tolerances measured over 30 known-same and 30 known-different pairs across
  three live packs — dHash 0–3 vs 12–47 bits, mean channel delta 0.02–2.35 vs
  35.46–188.22.
- A false negative costs a halted publish and is recoverable; a false positive
  attributes a stranger's sticker to our item and is not. Anything that cannot
  be compared — animated is vector, so there is no raster hash — is reported as
  undecidable rather than negative, and callers reconcile instead of accusing.

### Added — `panel.py --with-pack N`, to arrange a half-full pack

- **Un-hides one published set** so its emoji can be arranged beside the new
  candidates going into it: `panel.py --with-pack 5` shows pack 5 and the
  unpublished items together, and nothing else. Repeatable.
- `--all` was the only way to do this and is the wrong tool: it also returns
  every finished pack, which on a grown catalog is hundreds of cards that
  cannot change.
- The index is resolved through the publisher's own `publish_*.json` rather
  than by rebuilding `<base><n>_by_<bot>`, because the state file already
  records the exact name. An index that matches no published pack is an error,
  not an empty grid — silence there is indistinguishable from "that pack is
  empty".
- Here, and only here, the lookup asks *which* set an item belongs to, so a
  publication row with no recorded `set_name` stays hidden: unknown-where is
  not answered with a guess. The plain filter still asks only *whether* a row
  exists.

### Added — the curate panel shows where each published pack starts and ends

- **Pack boundary markers.** When the selection spans more than one set, a
  full-width marker carrying the brand logo now sits at the head of each pack,
  labelled with the grid range it covers. The logo really is the first emoji of
  every set and costs one of the 200 slots, so this is where it will land.
- The splits are counted from **included** items only — an unticked card never
  ships and so cannot push the next emoji into the following pack — which is
  why selection changes now recompute them and not just the counter.
- A marker is a `.packsep`, deliberately not a `.card`: the drop handler
  resolves its target with `closest('.card')`, so a marker carrying that class
  would swallow a drop aimed past it and do nothing. Markers live only in the
  DOM — never in `ITEMS`, never saved — so reorder and save are unaffected.
- **↑ Top / ↓ Bottom** buttons in the header. They scroll the document rather
  than calling `scrollIntoView`, which aligns an element with the top of the
  viewport — behind the sticky header — and so stopped a header-height short of
  both ends.

### Fixed — the curate panel kept showing an abandoned pack's emoji

- **A published pack that is not full no longer appears in the grid.** The panel
  hid a pack only once it was *full*, on the theory that a set still being
  filled is still the pack being built. `--new-set` ended that theory: a pack
  can now be left half-empty deliberately, so "full" stopped meaning "finished"
  and a 21/200 pack's emoji kept coming back while curating the next one. The
  test is now simply whether the item is published — `is_published` skips it at
  publish time regardless, so showing it only invited pruning that changed
  nothing. `panel.py --all` still brings them back, which is how you reorder a
  live pack. Drops the `publish_*.json` read and the capacity arithmetic that
  went with the old rule.
- The check asks whether a publication row exists, **not** whether it records a
  set name: a row with a NULL `set_name` is still published, and reading the
  name would turn "I do not know where it went" into "never published" and
  offer a live emoji up to be republished.

### Added — `--new-set`, for starting the next pack before this one is full

- **`build_collection.py --new-set`** rolls the run into a fresh set and leaves
  the current one at whatever size it has. Until now set *N+1* opened only when
  set *N* reached `--per-set`, so a pack deliberately stopped early had no
  supported way forward. Both workarounds were wrong: a smaller `--per-set` caps
  every *later* set at the same wrong size, and a second `--base` starts a family
  whose publication table is empty, so the whole catalog re-uploads into it.
  The flag reuses the existing closed-set roll rather than adding a second way to
  open a set, and is read once per run — later items fill the new set normally.

### Fixed — a stopped publish locked its own pack family for six hours

- **A killed run could not be resumed.** The pack-family lock was only
  reclaimable once it was six hours old *and* its process was gone. The liveness
  check already refuses to touch a live holder at any age, so the six hours had
  nothing left to protect — it only stood between the owner and resuming a
  publish they had stopped themselves, with a lock file to delete by hand as the
  workaround. The grace is now two minutes, which is all that is needed to cover
  claiming being `O_CREAT|O_EXCL` followed by a separate write: in that window
  the record is empty and its pid reads as 0, i.e. "dead".

### Changed — the curate panel

- **Animated is light violet** (`#c4b5fd`). The violet it started as sat too
  close to the dark background for the thumbnail outline to read.
- **The sticky header no longer blurs its backdrop.** `backdrop-filter` re-reads
  and blurs everything behind the header on every scroll frame, across its full
  width; with 201 cards scrolling under it that was the largest per-frame cost
  left. The header background is opaque instead, which needs no blur to stay
  readable. Off-screen cards were already free — they pause their video, swap an
  animation for its first frame, and skip rendering entirely
  (`content-visibility`).

### Changed — dependency upgrades (four merged, one rejected)

- **TypeScript 5.9 -> 7.0.2** and **vitest 2.1 -> 4.1.11** in `worker/`. Both
  verified locally: `tsc --noEmit` clean and all 34 worker tests pass on the new
  pair. Vitest 4 drops the `basic` reporter, which the project never used —
  `npm test` and the CI job both run a plain `vitest run`.
- **`actions/setup-node` v4 -> v7.** Its three breaking changes do not touch
  this workflow: the action now runs on Node 24 (fine on `ubuntu-latest`),
  automatic caching is limited to npm (npm is what we use), and the dummy
  `NODE_AUTH_TOKEN` export is gone (no registry publish here, and no
  `registry-url`). `node-version: "22"` is unaffected.
- **`rlottie-python >=1.3 -> >=1.3.8`** — the pinned floor now matches the
  version the suite has been running against all along.
- **`numpy >=1.26 -> >=2.5.2` was NOT merged: that release does not exist.**
  The newest published numpy is 2.4.6, so the bump would have made
  `pip install -r requirements-coins.txt` fail outright
  (`No matching distribution found for numpy>=2.5.2`).

### Changed — "port already in use" now names the process holding it

- The advice was "press Ctrl+C in the window running it", which helps nobody
  when the holder was started detached and has no window — which is how every
  stray one so far got there. The message now carries the pid and the exact
  `taskkill` line. The lookup is best-effort and can never raise: it exists only
  to improve an error message.

### Fixed — the log eviction read the whole table on every write

- The 10 MB budget is enforced with a window function that sums every row. It
  ran on **every insert**. Measured on the live database: 77 rows today, so
  harmless — but the cost grows exactly as the log fills, which is its normal
  state. At the cap the table holds roughly 163 000 rows, and D1's free tier
  allows 5 000 000 row reads a day: about **thirty log lines**.
- It now runs once every 250 rows, keyed off the `last_row_id` the insert just
  returned. Not a counter in the isolate — a Worker isolate is short-lived, so
  that counter would reset before reaching its threshold and the eviction would
  never run at all. The budget stays exact when it runs; between runs the table
  may sit up to 250 rows over it, and one row is capped at 2000 characters.

### Changed — the curate panel moved to port 9450

- 8765 is a busy neighbourhood and a stray listener there made the launcher's
  panel step fail with a bare WinError 10048.
- **`panel_sandbox` now IMPORTS the panel's port** (`panel.DEFAULT_PORT`) and
  serves on it + 1, instead of both files spelling the number out. The sandbox
  exists to stay off the real panel's port; two copies of that number would have
  drifted the moment one moved — and pointing an automated drag at the REAL
  catalog is how three hours of manual ordering were lost once. A test pins it.

### Fixed — saving from the filtered grid could re-include hidden emoji

- `set_inclusion` re-includes every key it is not given, and the new filter
  meant a save posted from the grid carried only the VISIBLE keys — so any
  hidden emoji that had been deselected would have been silently re-included.
  The save now carries every hidden exclusion through. The panel already
  documented an earlier variant of this bug; the filter reintroduced it.
- The save toast says `in the catalog` when a filter is active: its counts come
  from the catalog, not the grid, and "213 included" under 15 visible cards is
  alarming until you know that.

### Changed — the curate panel shows the pack being built, not the finished ones

- **Emoji in a FINISHED pack are hidden from the grid** — finished meaning the
  set is full, so nothing can be added to it again. The header reports how many
  are hidden and `panel.py --all` brings them back; a filter nobody can see is
  indistinguishable from having lost the items.
- The first version of this test was "already published", which hid the 14
  emoji of a half-empty second pack along with the 200 of the finished first
  one and left an empty grid. Fullness comes from the publishers' state files,
  not from counting catalog rows: the brand logo takes a slot without being a
  row, so counting calls a full set one short. An item whose set cannot be
  resolved stays visible — unknown is not finished.
- Hidden, never deleted. Those rows are what dedup recognises a re-download by,
  what maps a source premium id to ours, and what `sync_order` reads to re-sort
  an already published set. `set_order` keeps unlisted items in their relative
  order, so arranging the new pack cannot scramble a published one.

### Changed — a tidier repository root

- `CONTRIBUTING.md` and `SECURITY.md` moved to `.github/`, where GitHub reads
  them just the same.

### Fixed — reordering a pack left it unpublishable

- **`sync_order.py` moved the live stickers but never rewrote the order the
  publisher recorded.** That record is checked position by position, by
  identity, before anything is added to a set, so a reorder made the whole
  family unpublishable: the next publish stopped on "position 117 now holds a
  sticker this publisher cannot identify". The record now follows the reorder.
- It is rewritten **even when nothing needs moving**, because a set can already
  be in the right order while the record of that order is stale — which is
  exactly the state an earlier reorder leaves behind, and the one that blocks
  publishing. A report-only run still writes nothing.

### Added — the bot answers in both directions

- **Send ids, get the emoji.** The bot already turned premium emoji into ids;
  it now takes ids as plain text and replies with the emoji beside each one, in
  one message. A single id, newline-separated, comma-separated and
  `, `-separated all parse, and a repeated id is answered once.
- The whole message must be ids and separators, so prose containing a long
  number — a chat id, a timestamp — is ignored rather than answered.
- Ids are resolved with `getCustomEmojiStickers` before rendering: a
  `<tg-emoji>` tag shows its placeholder for an id that does not exist, so an
  unchecked id would make a typo look exactly like a success. Unresolvable ids
  are named. Resolving also supplies each sticker's own emoji as the tag's
  fallback, replacing a fixed star wherever the tag cannot render
  (notification previews, copied-out text, older clients).
- Implemented in both the Worker (live) and `emoji_bot.py`, which the Worker's
  extractor is documented as a faithful port of.

### Fixed — the provider path wrote set records without their live count

- **`coins/fetch_paprika.py` appended set records with no `live` key**, while
  `rebuild_dedup.py` always writes one. Twelve of the 29 coin sets were recorded
  that way, so the state file on disk was wrong about them. Nothing crashed only
  because the rebuild refreshes every set from Telegram before it subscripts
  `state["sets"][-1]["live"]` — the two writers agreeing is what makes that safe
  rather than lucky. Both provider writers now record `live`.
- A test scans the set-record literals in the provider and fails if one omits
  the field.

### Fixed — the manifest reported the catalog's row count, not the pack's size

- **It said "199 emoji" for a 200-emoji pack.** The brand logo is the set's
  first sticker but not a catalog item, so the key list is one short of what is
  live. The manifest is the file someone opens to see what a pack contains, and
  it now counts the pack: the logo is row 1 and the catalog items follow from 2.
  A set published without a logo is unaffected.

### Added — a preflight that asks Telegram before anything is published

- **`build_collection.py --preflight`** offers every queued file to
  `uploadStickerFile`, which runs the same validator as `addStickerToSet` and
  touches no set. A file Telegram will refuse is now found in about a minute
  instead of at whatever point of the publish it happens to reach — one `.tgs`
  surfaced 46 minutes in, after 99 uploads and two flood waits. A refusal stops
  the run, so nothing is left half-published.
- Launcher **B3** runs it between the dry run and the upload, so the default
  path is covered without anyone having to remember the flag.
- A transport failure is reported as "could not check", never as a bad file:
  a dropped connection must not send someone editing artwork that was fine.
- The publisher, the dry run and the preflight now share one `pending_keys`
  filter, so all three answer "what is queued" the same way.

### Fixed — re-encoding a transparent WEBM flattened it to an opaque square

- **`to_video_webm` did not name the alpha-capable decoder on the way in.** VP9
  stores alpha as a SEPARATE WebM layer and ffmpeg's default vp9 decoder drops
  it silently, so the filter chain never saw an alpha channel and the
  transparent pad landed on an opaque frame. The path had never had to
  re-encode a transparent source before — owner rule 1 remuxes every downloaded
  `.webm` with `-c copy` — so it flattened a cue-ball emoji to a black square
  before anyone noticed. A GIF or PNG input is unaffected and is not handed a
  vp9 decoder.
- Measuring this needs the same flag: probing a VP9 emoji with the default
  decoder reports every one of them as opaque, including the correct ones.

### Fixed — an incomplete pack announced itself in the channel

- **The end-of-run announcement was unconditional**, so a run that finished 199
  of 200 — one emoji refused by Telegram — still posted the pack link as if it
  were done. It now goes out only from a run with no failure and no skip. A skip
  counts as incomplete just like a failure: with permanent-refusal
  classification an unpublishable file no longer raises, and without that the
  very case behind this bug would have become a silent success. Nothing is lost
  — `state["sent"]` never records it, so the next clean run announces it, and
  the log says why it was held back.

### Fixed — an unpublishable .tgs was accepted at ingest and retried forever

- **`validate_tgs` now refuses a subtract mask**, naming the offending layer.
  Telegram's uploader rejects `masksProperties[].mode == "s"` while its player
  renders it, so a sticker can be live in a published pack and still be refused
  when the same bytes are uploaded — confirmed by downloading one from a live
  pack and sending it back untouched, which Telegram also refused. The file is
  otherwise entirely valid, so nothing local caught it and it only failed deep
  inside a publish with a message naming neither the item nor the reason. Masks
  hide inside precomposition assets, which is where the real one was, so the
  scan walks those too. Checked against the 146 animated files Telegram did
  accept: no false positives.
- **A permanent refusal is recorded as a skip, not retried.** "Bad Request:
  wrong file type" describes the bytes, not the moment, so `will retry` meant
  retrying on every future run forever and exiting non-zero for it. The match
  list is deliberately narrow — a skip is permanent, and mislabelling a
  transient error would lose an emoji.

### Fixed — a publish logged nothing while it worked

- **`build_pack.py` had no logger at all.** Every wait and retry the Bot API
  client makes -- flood waits, name-lock waits, network retries, and the
  "network error but the change is verified live" case -- was printed to the
  console and reached no log file. A publish that stalled 269 s on a flood wait
  left its log silent for the whole pause, which reads exactly like a hung
  process. Every tool in the project shares this client, so they were all blind
  together. The console output stays; the same lines now also reach the log.
- **A successful upload logged nothing.** Only failures were logged, so a
  healthy run wrote its plan and then went quiet for an hour -- and afterwards
  the log could not answer when a given emoji went in. There is now one line per
  sticker that lands, with its set and how far along the run is.

### Added — reorder a pack that is already published

- **`sync_order.py` makes a live pack match the curate panel** without
  republishing it. `setStickerPositionInSet` moves a sticker that is already in
  the set, so every emoji keeps its `file_id` *and* its `custom_emoji_id` —
  anyone already using one is unaffected, and nothing is re-uploaded. It is the
  one set mutation here that is genuinely idempotent, so it may be re-run and an
  interrupted run continues. Moves are a selection sort, one API call each, so a
  run stopped halfway leaves a set correct up to that point rather than
  scrambled. It refuses a set holding a sticker the catalog cannot identify —
  positions only mean something once every sticker is known — and pins the brand
  logo at position 0. It takes the publisher's pack-family lock.

### Fixed — publishing a mixed family stopped twice on its own checks

- **`mixed` was rejected by the state validator** that the publisher's own
  writes had to pass. A new format value was added without teaching the check
  about it.
- **An upload was confirmed by exact content key, which cannot match.**
  Telegram re-encodes what it stores: measured on a real sticker, 2304 of 16384
  normalised bytes changed. Confirmation now compares against the file that was
  just sent — exact first, then a bounded perceptual distance. The catalog-wide
  search uses a much tighter tolerance than that targeted compare, because a
  catalog full of near-identical check-marks put a rival within 5 bits of the
  right answer while the right answer sat at 0.

### Fixed — the launcher died on Ctrl+C

- **Ctrl+C during a task closed the whole launcher.** The console signal reached
  the parent as well as the child, so stopping the panel ended the session. It
  now stops the child and returns to the menu.

### Changed — pack titles are one sequence

- **`build_collection.py` titles now read `<title> 1, 2, 3` across every
  format**, in creation order. They used to be `<title> Animated 1` and
  `<title> Static 1` — three separate sequences, so two different packs were
  both called "1". The number counts every set already recorded, which is what
  makes it resumable: a restarted run continues the count instead of restarting
  it. Set **names** are unchanged (`<base>s<n>`/`<base>v<n>`/`<base>a<n>` remain
  the identity); only the human title moved.
- **Announcements gained `style`.** `cards` (default, unchanged) per pack;
  `list` renders one line per pack for a whole family. The direct Telegram path
  renders both shapes identically, so enabling the Worker cannot change how a
  post looks.
- **The per-set announcement uses the recorded title** instead of rebuilding it
  — rebuilding is how the announcement and the set drift apart.

### Fixed — a single-frame video had no content key at all

- **`_video_content_digest` resamples at a fixed `fps=10`, and a single-frame
  video is shorter than one sampling interval** — the filter emitted nothing and
  the digest fell through to hashing the container bytes. That is not a content
  key: two such stickers differing only in container framing did not dedup, and
  re-encoding one under owner rule 1 moved its primary key, orphaning the
  catalog row and the media file, both of which are named after it. WebM also
  randomises its SegmentUID, so the "identity" of such a file changed on every
  remux. An empty resample now retries at the file's own frames; raw bytes
  remain only for a file ffmpeg cannot render at all.
- **`fingerprint()` carried its own copy of that ffmpeg call.** Fixing the
  digest alone made the two disagree — the same file getting one key from
  `content_key()` and another from `fingerprint()`. They now share one decode.
- **A hung ffmpeg no longer becomes a byte hash.** `fingerprint()` caught the
  failure and returned a key anyway; a timeout is "I could not look", and
  turning it into a valid-looking key hid it. `MediaError` propagates, and the
  bounded-subprocess test now covers `fingerprint()` and the shared helper too.
  Every ingest site already fails that one item and counts it.

### Added

- **`worker/` — both bots in one Cloudflare Worker (TypeScript).** Webhook
  routes `POST /tg/general` and `POST /tg/coin`, plus `POST /publish` so a
  finished pack is announced **by the bot** in the channel instead of by the
  machine that built it, and `GET /health`. Each bot gets its own path and its
  own webhook secret: the token never appears in a webhook request, so one
  endpoint could not tell them apart, and one shared secret would let a leak
  from either forge the other's updates.
  `ADMIN_USER_IDS` fails closed like `emoji_bot.allowed_user_ids()` — unset,
  empty or all-invalid answers nobody, and only plain positive integers count
  (`Number()` would have taken `0x10`, `12.5`, `1e3`). A stranger gets one reply
  in private and silence in a group.
  Webhook handlers return 200 even on failure: Telegram redelivers any non-2xx
  and every action is a `sendMessage`, so a redelivery after a partial success
  posts the reply twice.
  **A token can use `getUpdates` or a webhook, never both** — registering a
  webhook stops `emoji_bot.py` receiving anything on that token. `emoji_bot.py`
  is unchanged and still works; `deleteWebhook` hands the token back.
  17 vitest tests with `fetch` stubbed, `tsc --noEmit` clean.
- **All three publishers now announce through one `build_pack.announce_packs`.**
  `build_pack.py`, `build_collection.py` and `coins/rebuild_dedup.py` each
  carried their own copy of "format the link and sendMessage", and when the
  Worker arrived only the collector learned about it — so a coin rebuild kept
  talking to Telegram from the build machine while the owner believed the bot
  was posting. A test asserts all three share the function. It routes to the
  Worker only when URL *and* secret are both set, keeps link previews off on the
  direct path (which the coin script used to do through a private `_call`), and
  the coin family now goes in one call so the Worker can split it.
- **`renderAnnouncement` returns a list.** 30+ packs in one message eventually
  passes Telegram's 4096-character limit, and the rejection costs *every* link,
  not just the overflow. Entries are never split from their own link.
- **`build_collection.notify()` routes through the Worker** when
  `WORKER_PUBLISH_URL` *and* `WORKER_PUBLISH_SECRET` are both set; the original
  direct path runs unchanged otherwise. `state["sent"]` still owns duplicate
  suppression, and a failed announcement is deliberately **not** recorded as
  sent — recording it would make the guard skip that pack forever.
- **Log-channel messages follow the Ad Timer Bot's format**, with the bot tag on
  its own first line, a level emoji, and a UTC stamp. Three of its behaviours
  came across with it because they solve problems this Worker has: a 12/minute
  budget that **drops and counts** instead of queueing (a queue inside a Worker
  isolate outlives its request and loses the messages anyway), a chat-id
  normaliser that accepts the bare id Telegram's UI shows and adds the `-100`
  the Bot API needs, and a 700-character cap for readability on a phone.
- **The bots no longer log their own output.** Both administer the log channel,
  so every line posted there returned as a `channel_post` — to both — and each
  wrote another row about a message we had just written. Not a loop today, but
  one as soon as channel handling grows a path that logs an ERROR. Found by
  watching live traffic, not by reading the code.
- **Worker logs: a 10 MB D1 table plus an errors-only Telegram channel.** Every
  line starts with the bot that wrote it, in both sinks — the two bots share one
  Worker, one table and one channel. The insert and the oldest-first eviction go
  in one `batch()`, so a row cannot be stored without its budget check, and the
  eviction is recomputed from the table rather than tracked in a counter that
  could drift after a failed write. The cap counts stored *text*, not the
  database file: D1 exposes no cheap reliable file size. Only ERRORs reach the
  channel; an unauthorised hit on a public webhook URL is a WARNING and
  level-based routing would let a scanner flood it. Logging runs in
  `ctx.waitUntil()` and swallows every sink failure, so it can neither delay a
  webhook response nor take a bot down. `GET /health` now reports `log_db`.
- **`worker/scripts/set-webhooks.ps1`** — registers one webhook per bot from the
  same `.env` the secrets came from, so a registration and its deployed secret
  cannot drift apart. That mismatch is silent: Telegram accepts `setWebhook`
  and every delivery is then rejected 401, which looks exactly like a dead bot.
  `-Status`, `-Delete`, and a refusal to replace a webhook pointing elsewhere
  without `-Force`.
- **Preview URLs disabled** (`preview_urls = false`). They default to on and
  would publish `/tg/general`, `/tg/coin` and `/publish` on a second hostname
  per deployed version.
- **`worker/scripts/put-secrets.ps1`** — pushes every Worker secret straight
  from `.env` to `wrangler secret put` through **stdin**, so no value is
  printed, kept in shell history, or passed as an argument (arguments are
  visible in the process list). It composes `ADMIN_USER_IDS` from
  `PACK_OWNER_USER_ID` + `BOT_ALLOWED_USER_IDS` — that list fails closed, so a
  hand-typed mistake is silent — and generates any missing webhook/publish
  secret, writing it back to `.env` so a later run cannot mint a different one
  and break every delivery's secret check.
  `wrangler.toml` now declares **no `[vars]` at all**: the channel became a
  secret too, not because it is a credential but because that file is committed
  and it would have published the channel name. Both forms arrive as
  `env.PACK_LINKS_CHAT_ID`.
- **Panel: an `Animation: On/Off` button**, persisted in `localStorage`, and
  animated cards now hold frames only while near the viewport (one
  `IntersectionObserver` swapping a ~3 KB still for the ~60 KB animation).
  `content-visibility:auto` alone did not stop an off-screen animation costing
  its full frame buffer.
- **`scripts/check.ps1`** — one command that byte-compiles every source file,
  runs `ruff check .`, then runs the full unit suite. Cheap gates first, each
  step bounded (per-step wall ceiling, exit 124 on timeout) and propagating a
  real exit code. `run.ps1` menu **D1** and `.github/workflows/ci.yml` both call
  it, so a local run and CI can no longer drift into different invocations.
- **Lint wired up** — `ruff check .` (no arguments; `ruff.toml` owns the rule
  set) runs in `scripts/check.ps1` and as its own CI step ahead of the suite, and
  CI installs `ruff`. The rule set is narrow on purpose: ruff's defaults plus
  `E402`, `BLE001`, `B` and `RUF100`, so the ~120 `# noqa: E402` /
  `# noqa: BLE001` comments already in the source mean something, and `RUF100`
  keeps them honest.

### Fixed — the coin providers were reading a state file that does not exist

- **`coins\fetch_paprika.py` (and `fetch_cmc.py`, which publishes through it) now
  read `coins\rebuild_dedup_state.json`** — the file that names the live packs,
  and the one every other coin tool already used. They were reading
  `rebuild_state.json`, which the rebuild calls "the current 30 packs, to
  delete" and which nothing has written since; a live run died on an unguarded
  read of a missing file. Missing state and an empty set list now both stop with
  a message naming the file instead of a traceback.
- Because both tools now share that file, the providers keep their in-flight
  intent under `provider_in_flight` and tally their uploads in `provider_added`,
  and the rebuild's live-versus-recorded consistency check counts both writers.
  Its `order` list cannot hold a provider's coin — that list must mirror the
  frozen plan — so without this a top-up made the next rebuild refuse to start.

### Fixed — a typo could publish

- **`coins\fetch_paprika.py` and `coins\fetch_cmc.py` now parse their arguments.**
  Both decided their one destructive switch with `"--dry" in sys.argv`, so any
  spelling that was not exactly `--dry` — `--dryy`, `--dr`, a stray positional,
  an unknown flag — silently selected the live branch, which spends the API
  quota, uploads to Telegram and rewrites the canonical ticker map. Unrecognised
  arguments are now a usage error (exit 2), and prefix matching is off so `--dr`
  is not guessed at either. Both gained `--help`.
- **`coins\fetch_logos.py` no longer reads the command line at import.** The page
  count was `int(sys.argv[1])` at module scope, so `fetch_logos.py --help` raised
  `ValueError` before argparse could answer. It is an optional argument now; the
  old positional form still works.

### Removed

- Four pieces of internal surface with no callers: a format-keyed MIME table
  and its only reader (uploads resolve the type from the file's real extension
  instead), a `pack_state.json` default that nothing had used since state files
  became per-pack, an unread set of video source extensions, and an unread lock
  heartbeat interval. No public behaviour changes.

### Fixed — documentation caught up with the code

Every command block in `README.md` and `docs/GUIDE.md` was re-checked against
the code and re-run. The stale ones:

- `--per-set` was documented as defaulting to **400**. The default and the hard
  cap are both **200**; 400 is rejected as a usage error (exit 2).
- `README` told the reader to run `coins\rebuild_packs.py`, which does not
  exist. The tool is `coins\rebuild_dedup.py`, whose default subcommand `all`
  is destructive (it deletes the old packs first) — now stated.
- `README` referred to launcher "option 7" / "option 8". The menu uses lettered
  keys (`A1`…`D1`); the bot is `C1` and the panel is `B4`. `run.ps1`'s own
  header comment still claimed the sections were `B`/`C`/`R`.
- Both files taught the suite as bare `python -m unittest`. Everything now uses
  `python -m unittest discover -s tests -t . -p "test_*.py"` (or
  `scripts\check.ps1`) and explains that dropping `-t .` silently disables the
  test-suite credential scrub and network block.
- `coins\remap_ids.py --apply` was documented with `--max-distance` optional; it
  is required (and must be > 0), because an uncalibrated run would overwrite the
  map with nearest-but-wrong matches.
- `--brand-logo`'s default was documented as a machine-specific `F:\...` path;
  it is the repo's own `assets/emoji-mapper-logo.png`.
- The curate panel was described as autoplaying video and looping animations.
  Nothing plays until hover — that was the fix for the page hanging on large
  catalogs. The observer margin is 200 px, not 250, and `POST /api/order` was
  missing from the route table.
- CI was described as Python 3.11 only; the matrix is 3.11 **and** 3.12.
- `emoji_bot.py` was described as replying with two collapsed quotes; it sends
  one, plus `copy_text` buttons.
- `.env.example` listed `GENERAL_BOT_USERNAME` / `GENERAL_BOT_NAME`, which no
  code reads (the bot's username comes from `getMe`), and omitted
  `EMOJI_FFMPEG_TIMEOUT`, `TELEGRAM_API_BASE` and `EMOJI_MAPPER_NO_DOTENV`,
  which it does.

### Fixed — eighth audit pass (1 critical)

- **An unresolved upload keeps the image that can prove it.** Per-run staging
  fixed a shared mutable path, and then discarded the whole directory on the way
  out — including the one file the *next* run needed. A run whose upload reached
  Telegram unconfirmed left a recorded source path pointing at nothing, so the
  next run fell back to its own fresh download of the same ticker, correctly
  identified the live sticker from the recorded hash, and then published that
  fallback as the local logo: the map naming one image while
  `coins/logos/emoji/<ticker>.png` held another. The source is now kept until
  the upload is settled, and promotion verifies the recorded hash itself rather
  than trusting its caller. When the original cannot be produced, nothing is
  published and the run says the local logo is stale and how to refresh it.

### Fixed — seventh audit pass (1 critical, 1 high)

- **Provider staging is private per run.** Sharing it by ticker name still meant
  a shared *mutable* path: downloads happen before any lock, so a second fetcher
  could replace the file between this run's hash, its upload, its
  applied-check and its promotion — four steps that must describe the same
  image, or an ambiguous request is decided against the wrong picture. Each run
  now stages into its own directory, carries that exact path through every step,
  records it in the in-flight intent so a recovery run reads what was actually
  sent, and deletes only its own staging.
- **`verify_logos --fix` reads the set list under the lock it needs.** It built
  the list from `--state` and only then waited for the pack lock; a rebuild
  finishing in that window deletes the old packs and writes a new list, so the
  run searched packs that no longer existed while the map already pointed at the
  new family. Lock order is unchanged: pack lock, then canonical map lock.

### Fixed — sixth audit pass (3 critical, 4 high, plus two found while fixing them)

The fifth pass established identity correctly. This one is about where that
answer leaked back out: code that **read the value it protects before taking the
lock that protects it**, and code that turned *"I could not determine X"* into
*"X is false"* — the same defect re-entering through the error path.

- **An unproven answer is no longer a negative.** `build_collection` resolved a
  live sticker through one function that returned `None` for three different
  facts: the content is not in our catalog, the download failed, the hash
  failed. Callers answer absence by publishing another copy — so one flaky fetch
  duplicated an emoji, and on a host without ffmpeg *every* video and animated
  sticker duplicated on *every* run. Unresolvable is now its own outcome and
  every caller refuses on it.
- **A provider's identity oracle can no longer be edited out from under it.** It
  was `EMOJI/<ticker>.png` — a shared path both fetchers write with no lock
  held. An unresolved upload leaves the map unwritten, the next run re-downloads
  that ticker and overwrites the file, and recovery then compares the live
  sticker against the *new* art, concludes the upload never landed, and sends a
  second copy. No concurrency required. The image's identity is now recorded with
  the mutation.
- **Providers re-filter under the lock**, so the loser of a race no longer
  uploads a coin the winner just published.
- **Whole-map writers hold the pack lock across the read**, not just the map lock
  across the write; `verify_logos` no longer chooses a sticker from an unlocked
  read of the map.
- **`build_pack` measures the set instead of assuming.** A post-add read that had
  not caught up recorded one too few and every later expectation inherited the
  drift; a set that answered MISSING read as *zero*, so the run created a second
  pack, announced its link and exited 0. Restart reconciliation demanded
  `expected` or `expected + 1`, but the sequence it exists for leaves the set two
  bigger — it stopped forever with a live sticker off the books.
- **The lock's own recovery no longer defeats the lock.** Reclaiming a stale lock
  unlinked and re-created unconditionally, so two runs that judged the same dead
  record stale could each delete the other's fresh claim and both proceed.
- **Two refusals that stated no workable repair now state one**, and the tests
  execute the repair rather than matching words in the message.

`tests/test_lock_order.py` is new: it enforces the documented lock order and
non-reentrancy across every tool by parsing them, because that invariant spans
files and no single-file review can see it.

### Fixed — fifth audit pass (7 findings)

One defect, in seven places: **a count is not an identity.** "Exactly one new
`file_unique_id` appeared", "the live count is `expected + 1`", and "a set of
that name exists" are all satisfied when our own request fails while one sticker
arrives from somewhere else — and the run then bound that stranger's
`custom_emoji_id` and `file_unique_id` to our catalog key, permanently.

- **A new sticker is now proven ours by its content.** `Telegram._added_check`,
  the restart reconciliation in `build_pack` (both in-flight ADD and in-flight
  CREATE), `build_collection._confirm_new_upload` and the coin rebuild's CREATE
  recovery each download the candidate and compare content keys with the source
  image. When the comparison cannot be made the answer is UNKNOWN — never a
  silent yes, and never a blind re-send either. The old count fallback is gone;
  it reinstated the same bug through the error path.
- **The rebuild's resume state must describe one walk.** `cursor=0` beside
  `order=["aaa"]` was structurally valid and meant the build was about to upload
  `aaa` a second time. `order` must now be a subsequence of the plan prefix the
  cursor claims to have walked, uploads must have a recorded set to live in, and
  an in-flight marker must be the plan entry the cursor was moved past to run it.
  That check also moved **ahead of the delete phase**: a state that live Telegram
  disagrees with never gets as far as destroying the existing packs.
- **One lock around the whole canonical map.** Atomic replacement prevents a
  truncated `ticker_to_id.json`; it does nothing about a lost update, where two
  tools each read the map, apply their own edit, and the second write discards
  the first. `alias_map`, `enhance_map` and `remap_ids --apply` took no lock at
  all, and the providers read the map *before* waiting for theirs. All of them
  now hold one shared lock across the complete read-modify-write with the read
  inside it, and exit `FAILED` on a busy lock instead of writing a merged-from-
  stale map. Lock order is documented where the locks are defined: pack-family
  lock first, canonical map lock second.
- **A `verify_logos` replacement intent is bound to its target.** The intent file
  has one fixed path while `--map`/`--state` are chosen per run, so a crash under
  one map could be reconciled into another — repointing a ticker inside a file
  that never held the old id. The intent now records its resolved targets and
  refuses to recover against a different one.

### Fixed — second audit pass (34 findings)
- **The bot rejected everyone, including its owner.** `main()` built the numeric
  allowlist as `allowed`, then reassigned that same name to the Telegram
  update-type filter and passed it to the handler, so the check compared a user
  id against `["message", ...]` and was always true. The regression test now
  runs through the real polling wiring, which is where the defect lived.
- **Unresolved uploads no longer let the run continue.** An ambiguous
  add/create used to be logged while the loop moved on, and the next item's
  in-flight record overwrote the unresolved one — after which a later run could
  re-send an upload that had already landed. The run now stops with a retryable
  exit until the ambiguity is settled, and the in-flight record is structured
  (operation, target set, index, expected count) so even an ambiguous *create*
  whose set was never recorded can be reconciled by name.
- **"Unknown" is no longer read as "empty".** Live set reads are tri-state
  (EXISTS / MISSING / UNKNOWN). Previously any network error became a live count
  of zero, and a probe failure could clear the only record of an unresolved
  mutation.
- **Corrupt state and plans fail closed.** A file that exists but cannot be
  parsed is an error; only an absent file may fall back to a default. All state,
  plan and canonical-map writes are atomic.
- **Concurrent publishers are refused** via an exclusive per-state lock.
- **Live drift is detected by identity, not position.** The full set manifest is
  verified by `file_unique_id`, so a manual delete/shrink/reorder/replace inside
  the recorded prefix is caught, and a `custom_emoji_id` is never assigned from
  a positional guess.
- **Partial failure never reports success.** `build_pack` and `fetch_pack` now
  return 3 (partial) or 4 (failed); a state file whose base disagrees with the
  run is an integrity error instead of a silent restart from zero.
- **Every ffmpeg/ffprobe child is bounded**; a hang becomes a `MediaError`
  instead of stalling ingest forever.
- `coins/verify_logos.py` died on an import that no longer existed; all 22
  entry points, including every `coins/*` module, now import cleanly.
- Provider tools (CoinPaprika/CMC) use one verified mutation path with
  `expected_before` instead of blind retries, reject blank logos, and no longer
  map emoji ids from the tail of a set.
- Smaller contracts: `--formats`/`--per-set`/`--limit` validated, perceptual
  hash threshold bounded inside `Catalog`, a broken SVG falls back to a healthy
  same-stem raster, the panel bounds TGS decompression, `EMOJI_LOG_RETENTION_DAYS`
  parses safely, cached logo files are validated, and an ambiguous normalized
  coin name is reported rather than auto-resolved to the first candidate.

### Security
- **Real credentials were committed as test fixtures.** The live
  `GENERAL_BOT_TOKEN` and `CMC_API_KEY` were hard-coded in
  `tests/test_logsetup.py`. They are replaced with synthetic values and
  `TestNoCommittedSecrets` now fails if any secret-length `.env` value appears
  in a git-tracked file. **Both credentials must be rotated** — they remain in
  git history.
- **Bot token could reach stdout/stderr.** The token is embedded in every
  request URL, and a `requests` exception carries that URL; exception text is
  now redacted before printing, and the uncaught-exception hook renders its own
  redacted traceback instead of delegating to the default hook, which printed
  it raw.
- **Curate panel: stored script injection.** Catalog labels (which come from
  downloaded packs) were embedded in an inline `<script>`; a label containing
  `</script>` escaped the data block. Item data is now parsed from an inert
  JSON block and written with `textContent`.
- **Curate panel: unauthenticated mutation.** Any page in the browser could
  POST to the localhost panel and change inclusion or publish order. Mutations
  now require a per-run token, a loopback `Host`/`Origin`, `application/json`,
  and a bounded body; `/api/order` must be an exact permutation.

### Fixed
- **Duplicate uploads on resume.** Resume position was derived from the live
  sticker count, but a skipped plan entry (missing, blank or failed image)
  consumes a plan position without producing a sticker — so each skip shifted
  the cursor back by one and the next run re-uploaded already-published
  entries. `build_pack.py` and `coins/rebuild_dedup.py` now resume from a
  recorded cursor plus a write-ahead in-flight record, and refuse to continue
  when live state and the record disagree instead of guessing.
- **Corrupted canonical coin map.** The same drift was written into
  `coins/ticker_to_id.json`: 4202 of 5962 entries had been rewritten and one
  emoji id was claimed by 129 unrelated tickers. Restored from the
  pre-corruption backup; the positional mapping fallback that produced it is
  removed, and a duplicate-upload / oversized-shared-group guard now runs
  before any map write.
- **False coin aliases.** `enhance_map.py` treated a single trailing `c` as a
  chain suffix, mapping `ghc`→`gh` and `zbc`→`zb`. The heuristic is removed;
  the two genuine cases (`avaxc`, `bttc`) are named aliases.
- **Animated emoji canvas.** Telegram requires a 512×512 Lottie canvas for
  `.tgs`; the converter rewrote every animation to 100×100 by scaling the
  top-level layer transform, which clips artwork, and the validator never
  checked. The canvas is now preserved, other sizes are rejected, and
  validation covers gzip packaging, frame rate, and duration.
- **State files could be truncated.** Progress is now written atomically, and
  unreadable state aborts instead of silently restarting from zero (which
  re-uploaded everything).
- **Six-minute stall on a missing set.** `STICKERSET_INVALID` was retried for
  every method; it is now scoped to set creation with an overall deadline, and
  no sleep follows the final attempt.
- **Per-set default exceeded Telegram's cap** (400 vs the documented 200), and
  `--per-set 0` reached a division by zero. Bounds are validated.
- `TELEGRAM_API_BASE` set only in `.env` was ignored, because the base was read
  at import time — before `load_env()` runs.
- Rebuild deleted the old packs before checking the plan was usable.

### Changed
- **SVG rasterizing moved from svglib+reportlab to `resvg-py`.** svglib 2.x
  requires `reportlab>=4.4.3`, which the pinned `reportlab<4` blocked; that pin
  also parked the project on reportlab 3.6.13 (April 2023), whose `>=3.6` floor
  can resolve to a CVE-2023-33733-vulnerable build, and reportlab 5.0 removed
  the bundled renderPM backend outright. resvg ships a prebuilt self-contained
  wheel, needs no system cairo, and renders straight to RGBA — deleting the
  two-pass white/black render and the numpy alpha reconstruction, and fixing
  the long-standing blank-gradient bug. Nothing pins Python 3.11 any more; CI
  now runs 3.11 and 3.12.
- **Curate panel is substantially faster** on a 199-item catalog: first paint
  589 ms → 45 ms, and toggling a selection 13.6 ms → 1.2 ms. Selection and
  drag no longer rebuild the whole grid, videos no longer autoplay at rest,
  off-screen cards skip layout and paint, and the blocking webfont import is
  gone.
- Documentation: removed four logging helpers the guide documented but that
  never existed (its example import failed), and corrected the SVG/dependency
  claims.

### Added
- Project logos in `assets/`, shown in the README and the curate panel.
- **The emoji bot is now private.** `BOT_ALLOWED_USER_IDS` lists the numeric
  Telegram ids allowed to use it (defaulting to `PACK_OWNER_USER_ID`). An unset
  allowlist means *nobody* — the bot refuses to start rather than run open.
  Authorization is checked on the message sender, so a stranger cannot extract
  emoji ids; unauthorized group messages are ignored silently.
- **`PACK_LINKS_CHAT_ID`** announces finished-pack links in a channel (numeric
  id or `@username`) instead of the owner's private chat. The bot must be an
  administrator of that channel; unset keeps the previous PM behaviour.
- **`EMOJI_LOG_RETENTION_DAYS`** (default 30) prunes old run logs, which
  previously accumulated one file per run forever. The end-of-run summary now
  also reports the outcome and the real exit code.
- Publication records are queryable per pack family: `is_published(base, key)`,
  `custom_emoji_id_for(base, key)`, `publication_bases()` and
  `forget_publication(base)` to make a deleted pack family publishable again.
- **Curate panel — drag to reorder.** Emoji can now be dragged to set the
  **publish order**. The order is persisted to a new `items.position` column
  (`Catalog.set_order`, POST `/api/order`) and drives both the panel and
  `build_collection` (each per-format set publishes in this relative order). On
  first open the order is seeded to the look-alike similarity grouping.
- **Curate panel**: a distinct gold "Brand logo" preview card now appears first
  when the configured bot is `@GodVerifyEmojiMapperbot` and the logo file
  exists, showing where the mandatory logo will be inserted on publish. It's
  preview-only (not clickable, not counted, never reordered, never sent to
  `/api/save`) since the logo is only actually added by `build_collection.py`
  at publish time.

### Changed
- **Curate panel performance**: animated `.tgs` are now shown as a **static
  first frame** and only play on **hover** (were all autoplaying+looping at
  once, which hung the page on large collections). Off-screen Lottie players are
  destroyed. Launcher menu label clarified to "Open web panel to pick & reorder
  emoji (browser)" and its start message shortened.
- **Curate panel**: benign browser disconnects while scrolling no longer spam
  the log with `ConnectionAbortedError` tracebacks (handled in the request
  handler and server `handle_error`).
- **Emoji Mapper bot**: each "Copy"/"Copy a-b" button's `copy_text` now ends
  with a trailing newline, so pasting the copied IDs leaves a blank line after
  the last one.
- **Emoji Mapper bot**: each "Copy"/"Copy a-b" button's `copy_text` now ends
  with a trailing newline, so pasting the copied IDs leaves a blank line after
  the last one.

### Fixed
- **Duplicate-proof uploads, verified end-to-end.** `addStickerToSet` /
  `createNewStickerSet` are not idempotent, and a network timeout after
  Telegram had already applied the call was blindly re-sent — the same emoji
  could land in a pack twice (both in the collector and the coin flows).
  `Telegram._call` now verifies the live set after a network failure
  (applied → success, not applied → safe retry, unknown → new
  `AmbiguousUploadError`), and `build_collection.publish_format` reconciles
  every applied-but-unrecorded live sticker back to its catalog item (by
  `file_unique_id`, else by downloaded content) before computing pending
  items — a crash between the upload and `mark_uploaded` can no longer cause
  a re-upload on resume. A create that landed ambiguously (or a leftover
  "occupied" set name) is now adopted instead of wedging every later run.
  Covered by `tests/test_publish_dedup.py`.
- **Upload retries no longer send an empty file.** Retried upload calls
  (flood wait or network retry) used an already-exhausted open file handle,
  so the retry posted a zero-byte body and the sticker failed; all upload
  methods now send the file content as bytes.
- **Own packs are never re-downloaded.** Publishing records each uploaded
  copy's `file_unique_id` in the catalog's `seen_files`, so a later
  `fetch_pack.py` / `fetch_emoji_ids.py` of our own published packs is caught
  by the fast pre-dedup and downloads nothing (previously every copy was
  re-downloaded and, for static, could even re-enter the catalog after
  Telegram's re-encode).
- **Emoji Mapper bot**: id batching now follows the message-length limit again
  (not the smaller `copy_text` button limit), so a 50-id reply is 2 messages as
  before, not 5. Telegram's `copy_text` button is still hard-capped at 256 chars
  (~12 ids), so a message with more ids than that shows a few chunked
  "Copy a-b" buttons together covering every id in that message — Telegram
  itself has no single-tap way to copy an arbitrarily large id list, and a
  single message is capped at 4096 chars (a 50-id message would need ~4500+),
  so very large results still need more than one message.
- **Emoji Mapper bot**: a manually quoted reply (the highlighted `>` excerpt
  above a reply) containing multiple premium emoji only surfaced **one** ID.
  Root cause: per the Bot API, custom_emoji entities inside a quoted excerpt
  live in `message.quote.entities` (a `TextQuote`), not in the reply's own
  `entities`. `extract_custom_emoji_ids` now also scans `quote.entities` and
  `external_reply.quote.entities`, so every premium emoji in the quote is
  listed; repeated emoji (in the quote, the reply text, or both) still collapse
  to a single ID/copy entry.

### Changed
- **Launcher (`run.ps1`)** reworked to match the FFmWiz style: a centered pink
  banner (title + full-width rule + yellow `Logging to: ...`), quiet startup
  (env/Python/ffmpeg OK lines go to the log only), cyan screen titles (no more
  black-on-cyan bar), and an ANSI 256-colour sectioned menu with sections
  lettered in order (`A`=Build, `B`=Collection, `C`=Bot), each with its own
  numbering (e.g. `A1`, `B3`, `C1`) and blank-line spacing. Removed the `q) Quit`
  row in favour of a colored `{back=0, quit=exit}` hint on every prompt. Actions
  are now step wizards (`Run-Wizard`) so **`0` steps back exactly one prompt**
  (only the first prompt returns to the menu), and `exit`/`quit` leaves from
  anywhere. Per-run UTC log under `logs\run_<UTC>.log` also records each launched
  Python command and its exit code (no secret values).
- **Emoji Mapper bot reply** reworked: sending one or more premium emoji (any
  spacing/newlines) now returns a **single collapsed (expandable) quote** of the
  **actual premium emoji** (rendered via `<tg-emoji>`) + `<code>` ID per line for
  individual tap-to-copy, plus a `copy_text` **“Copy all” inline button** for
  reliable one-click copy-all on every platform (chunked for long lists). Huge
  lists split across messages; a rejected custom emoji falls back to plain
  fallback chars.
- **`build_collection.py`** now places the **God Verify logo as the first emoji
  of every set** built with `@GodVerifyEmojiMapperbot`. Since Bot API 7.2 a
  single set may contain mixed formats, so the logo is always a static 100x100
  PNG and leads static, video AND animated sets alike (verified live). The coin
  bot is exempt. New `--brand-logo` / `--no-brand-logo` flags.
- Renamed the project from `CoinEmojiMapper` to **Emoji Mapper** across the
  codebase, documentation and launcher.
- Generalized the engine so `make_emoji_pngs.py` + `build_pack.py` build emoji
  packs from any folder of images, not only crypto-coin logos.
- **Curate panel** thumbnails now use a dark-slate contrast checkerboard
  (`#828c9a`/`#464e5a`) plus a Backdrop switch (Checker/Light/Dark/Gray,
  persisted to `localStorage`) so black, hollow-center and faint emoji stay
  clearly visible while matching the dark theme.

### Added
- **`fetch_emoji_ids.py`** — download only *specific* premium custom-emoji by
  their IDs (e.g. `premium-id:<n>` entries from bot inventory files) instead of
  whole packs. Reads only real `premium-id:` entry lines (start-anchored, so
  example/prose mentions are skipped and trailing labels/emoji are still
  captured), de-duplicates IDs within a file and across files (each real emoji
  fetched once) and again by content, resolving via `getCustomEmojiStickers`
  into the catalog. New `Telegram.get_custom_emoji_stickers()` helper (batched,
  ≤200 IDs/call). Extraction/dedup covered by `tests/test_fetch_emoji_ids.py`.
- **Advanced logging** (`emojikit/logsetup.py`): per-run id on every line,
  automatic secret-value + token redaction across all records and tracebacks
  (content hashes/ids preserved), rich file format, optional JSONL sidecar,
  uncaught-exception capture, `log_duration`/`logcall` helpers, third-party
  noise reduction, and an end-of-run warnings/errors summary.
- **Curate web panel** (`panel.py`): a local dark neon-blue panel to review the
  downloaded emoji as large labelled cards — static/video previewed and
  **animated `.tgs` rendered & looped via a vendored Lottie player** (lazy,
  IntersectionObserver-driven so only on-screen animations play) — toggle which
  ones to include (all on by default, Shift+click ranges), with look-alikes
  ordered next to each other; the saved selection drives what `build_collection`
  publishes.
- **Emoji Mapper bot** (`emoji_bot.py`): interactive long-polling bot that
  extracts premium custom-emoji IDs from sent/forwarded messages and from new
  channel/group posts, with tap-to-copy (`copy_text`) inline buttons and a
  `/start` help menu.
- **Multi-format collector workflow**: download emoji from existing Telegram
  packs (`fetch_pack.py`), build emoji from scratch (`add_media.py`) and
  republish into new per-format packs (`build_collection.py`).
- Support for **animated** (`.tgs`) and **video** (`.webm`/VP9) custom emoji in
  addition to static, including GIF/MP4 → WEBM conversion via ffmpeg.
- `emojikit/` core toolkit: UTC file logging, media detection/hashing/conversion
  and a content-addressed SQLite **catalog** that deduplicates at ingest time
  (by `file_unique_id`, content hash and perceptual hash) and makes publishing
  idempotent and resumable — eliminating the old delete-and-rebuild churn.
- `tests/` unit suite (stdlib `unittest`) with a committed Lottie fixture.
- Proprietary `LICENSE` (All Rights Reserved).
- GitHub repository support files: issue/PR templates, Dependabot config and
  this changelog.

## [1.0.0] - 2026-06-24

### Added
- Core engine: image → 100×100 PNG conversion (`make_emoji_pngs.py`) and
  resumable Telegram custom-emoji pack uploader (`build_pack.py`).
- `coins/` component: logo fetchers (CoinGecko / CoinPaprika / CoinMarketCap),
  keyword builder, duplicate-proof rebuild and pack integrity audit.
- PowerShell launcher (`run.ps1`) with general and crypto-coin workflows.
- CI workflow with byte-compile, import smoke test and offline dry-run.

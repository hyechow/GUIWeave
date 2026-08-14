---
id: knowledge.android.Mastodon.interface
source_type: knowledge_interface
platform: android
app: Mastodon
scope:
  - orchestrator
selector_when: favorite toots tagged dogs saved favorites bookmarks Mastodon
source: manual_verified
confidence: high
ttl: session
---
# Mastodon interface

- Route invariant: every TaggedToots query repeats the exact tag in **filters**, even when its
  preceding reach already has the tag. After either saved view, every target-bound TootDetail reach
  repeats that exact **tag** together with the row's **author_handle** and **content** identities;
  none of those three dimensions substitutes for another. Its entity is exactly **TootDetail**;
  **TaggedToots** is collection-only and is never the target-bound mutation surface.
- **TaggedToots**, **SavedFavorites**, and **SavedBookmarks** are three separate complete
  collections. All expose exactly **author_handle** (`text`) and **content** (`text`); the exact
  pair identifies the same Toot across collections. TaggedToots has source filter **tag**, including
  its leading `#`; the two saved collections have no query filters.
- SavedFavorites requires observable state **active_view** = `Favorites`; SavedBookmarks requires
  **active_view** = `Bookmarks`. Only their complete traversed memberships determine exclusion;
  controls on a tag row or TootDetail are not authoritative membership evidence.
- Both saved views are reached from the signed-in user's global bottom-navigation route
  `Profile → Saved → Favorites` or `Profile → Saved → Bookmarks`. Scrolling can hide the inner
  selector, but the fixed profile header, selected `Saved` tab, global navigation, and profile strip
  still identify the view. A nested tag/search/post hides global navigation; use visible Back one
  frame at a time until the global bar returns. After both sets are complete, use global Search.
- In Explore search suggestions, the exact hashtag row is the row with the `#` icon and tag text
  (for example, `dogs` after entering `#dogs`). Open it to reach TaggedToots. Posts-tab `Could not
  find anything` and `0 people are talking` are search/activity metadata, never proof that the tag
  timeline is empty; confirm its tag header and toot cards.
- Visiting a saved collection replaces the active tag route. Before mutating, restore TaggedToots
  with the exact tag, then process every tag row whose identity pair is absent from both saved sets.
- Each selected tag row reaches **TootDetail** inside the mutation loop. Favorite is one targeted durable mutation with
  **favorited** = boolean `true`, never the text string `"true"`; favorited is not a query or read
  field. A long detail may show only fixed `reply` while its action row is offscreen; scroll until a
  separate structured `Favorite` control is visible—`reply` is never Favorite.
- For an eligible tag row, open its text body, confirm identity, use TootDetail's Favorite control,
  then Back once. `Post from <author>` identifies TootDetail; the exact tag title identifies
  TaggedToots even if the processed toot fills the screen. Never favorite from timeline action bars.

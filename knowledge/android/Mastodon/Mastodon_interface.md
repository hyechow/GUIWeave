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
  **active_view** = `Bookmarks`. Their memberships, not fields on a tag row, determine exclusion.
- Visiting a saved collection replaces the active tag route. Before mutating, restore TaggedToots
  with the exact tag, then process every tag row whose identity pair is absent from both saved sets.
- Each selected tag row reaches **TootDetail** inside the mutation loop. Favorite is one targeted durable mutation with
  **favorited** = boolean `true`, never the text string `"true"`; favorited is not a query or read
  field.

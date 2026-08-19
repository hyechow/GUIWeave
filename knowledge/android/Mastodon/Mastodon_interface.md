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

- TaggedToots is opened with the exact tag, including its leading `#`. A TootDetail
  retains that tag route plus the row's **author_handle** and **content** identities;
  none of those three dimensions substitutes for another.
- **TaggedToots**, **SavedFavorites**, and **SavedBookmarks** are three separate complete
  collections. All expose exactly **author_handle** (`text`) and **content** (`text`); the exact
  pair identifies the same Toot across collections.
- SavedFavorites requires observable state **active_view** = `Favorites`; SavedBookmarks requires
  **active_view** = `Bookmarks`. A filled blue star with a count on a Toot action row
  means it is favorited; an outline star means it is not. Favorite and bookmark are
  independent state dimensions; only a task-stated predicate makes either saved
  collection relevant to the target set.
- Both saved views are reached from the signed-in user's global bottom-navigation route
  `Profile → Saved → Favorites` or `Profile → Saved → Bookmarks`. Scrolling can hide the inner
  selector, but the fixed profile header, selected `Saved` tab, global navigation, and profile strip
  still identify the view. A nested tag/search/post hides global navigation; use visible Back one
  frame at a time until the global bar returns.
- In Explore search suggestions, the exact hashtag row is the row with the `#` icon and tag text
  (for example, `dogs` after entering `#dogs`). Open it to reach TaggedToots. Posts-tab `Could not
  find anything` and `0 people are talking` are search/activity metadata, never proof that the tag
  timeline is empty; confirm its tag header and toot cards.
- Visiting a saved collection replaces the active tag route. If the task requires a saved
  membership check, restore TaggedToots with the exact tag before mutating its rows.
- Each selected tag row reaches **TootDetail** inside the mutation loop. Favorite is one targeted durable mutation with
  **favorited** = boolean `true`, never the text string `"true"`; favorited is not a query or read
  field. A long detail may show only fixed `reply` while its action row is offscreen; scroll until a
  separate structured `Favorite` control is visible—`reply` is never Favorite.
- For a target tag row, open its text body, confirm identity, use TootDetail's Favorite control,
  then Back once. `Post from <author>` identifies TootDetail; the exact tag title identifies
  TaggedToots even if the processed toot fills the screen. Never favorite from timeline action bars.

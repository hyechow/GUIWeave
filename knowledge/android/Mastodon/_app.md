---
id: knowledge.android.Mastodon.navigation
source_type: knowledge_navigation
platform: android
app: Mastodon
scope:
  - decompose
  - planner
  - replanner
  - worker
source: manual_verified
confidence: high
sensitivity: internal
ttl: session
version: 5
---
# Mastodon on Android

- Reply Send and `Pin on profile` are write-through mutations. After Reply Send, returning
  from the composer to the post detail with the new signed-in-user reply visible proves the
  reply commit. The post detail keeps a persistent `Reply to <author>` input below replies;
  a new signed-in-user name/handle row immediately above that input is the posted reply,
  not an open composer, even when its body is below the fold. Bind that row to the exact
  typed payload in the preceding Send transaction. Do not reopen the composer or send the
  same content again.
- After `Pin on profile` is activated for the verified target post and its action menu
  closes without error, that invocation is the durable pin commit. Do not reopen another
  post's menu merely because the Timeline itself has no separate pin badge.
- `Bookmark` and `Remove bookmark` in a post's overflow menu are write-through mutations.
  When the selected option closes the menu without error, complete that post's bookmark
  Commitment. For a multi-post task, return to the verified source collection and continue
  with an unhandled post; do not reopen the handled post's menu to confirm or repeat it.

- `SavedFavorites`: navigate through the signed-in user's bottom-navigation
  `Profile → Saved → Favorites`; membership in this collection proves that a
  Toot is already favorited.
- `SavedBookmarks`: navigate through the signed-in user's bottom-navigation
  `Profile → Saved → Bookmarks`; membership in this collection proves that a
  Toot is bookmarked.
- An author's avatar, handle, or post profile opens that author's public profile;
  it is not the signed-in user's `Profile` route and cannot expose the signed-in
  user's saved collections. From a nested tag or post view without bottom
  navigation, do not target `Profile` yet: activate the visible Back control one
  frame at a time until the global bottom navigation is actually visible. Only
  then activate the signed-in user's `Profile` tab.
- `TaggedToots`: navigate through `Explore`, search the exact tag, and open its
  posts view.
- All three collections expose `author_handle` and `content`, but no stable
  status ID or permalink. `author_handle` is the `@`-prefixed account handle,
  not the display name.
- Match a Toot across these collections by the exact pair
  (`author_handle`, `content`). A tag-result Toot can only be relocated through
  its `TaggedToots` collection.
- A filled blue star with a count on a Toot action row means it is favorited; an
  outline star means it is not. The corresponding Saved collection is an alternate
  membership view, not a prerequisite for reading this visible control state.
- Favorite and bookmark are independent state dimensions. Membership in one neither
  implies nor excludes membership in the other; only a task-stated predicate makes a
  saved collection relevant to the target set.
- Favorite mutation uses the boolean field `favorited`; this is not a field
  displayed by `TaggedToots` and is neither a query field nor a query filter.
- Tapping the visible text body of a Toot opens its single-post detail view,
  titled `Post from <author>`. Do not tap the author/avatar or a media thumbnail:
  those open the author profile or media viewer instead. The detail view exposes
  the same author and content together with that Toot's own Favorite control.

## Interface contract

- The exact query entity identifiers are `TaggedToots`, `SavedFavorites`, and
  `SavedBookmarks`. `Favorites` and `Bookmarks` are `active_view` values, not
  abbreviated entity names.
- `TaggedToots` is an exact tag-scoped view, not an unfiltered collection. Opening
  it requires a concrete `tag`, including its leading `#`. Its source filter field is
  `tag`, and its query fields are exactly `author_handle` and `content`.
- `SavedFavorites` is established only while the `Favorites` saved view is active;
  its required observable state is `active_view` equal to `Favorites`. Its query
  fields are exactly `author_handle` and `content`.
- `SavedBookmarks` is established only while the `Bookmarks` saved view is active;
  its required observable state is `active_view` equal to `Bookmarks`. Its query
  fields are exactly `author_handle` and `content`.
- Favoriting updates an existing `TaggedToots` record. `author_handle` and `content`
  identify that record; `favorited` is the only mutation field and is not a
  `TaggedToots` query field.
- `TootDetail` interface:
  - required observable route state: `tag`, retaining its exact leading `#`
  - row identity fields: `author_handle`, `content`
  - mutation fields: `favorited`
- A `TaggedToots` row reaches `TootDetail` only through its exact source tag view and
  exact (`author_handle`, `content`) pair. A shared-list match is insufficient because
  another Toot's action bar may be visible. The tag is route state, not a row field,
  and full Toot content is not a global search term.
- Entering either saved collection replaces the active tag route. After inspecting
  saved views, the exact `TaggedToots` tag route must be active again before any of its
  rows can expose `TootDetail`.

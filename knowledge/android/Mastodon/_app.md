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
version: 14
---
# Mastodon on Android

- Native posting-language settings are under `Home` → top-right settings gear →
  exact account row → `Posting defaults` → `Posting language`. The picker exposes a
  `Chinese` row; selecting it returns to `Posting defaults` with
  `Posting language: Chinese` displayed. The page banner says these are
  defaults for new posts and can be edited per post, so this control does not establish
  the account/interface locale.

- The profile's `Featured` tab is a read-only summary of existing featured hashtags
  and pinned posts. Entering `Edit profile` switches to `About` and disables all four
  profile content tabs until the edit is saved or discarded, so tapping `Featured`
  there cannot navigate. The `About` tab's `Add row` fields edit Label/Value profile
  metadata; they do not create featured hashtags. This Android version exposes no
  control for adding or removing featured hashtags.

- An image's visible `ALT` badge opens a read-only `Alt text` sheet; scrolling that
  sheet cannot reveal an edit control. To revise an existing image description on the
  signed-in user's own post, open that post's detail view, use the post's top-right
  three-dot overflow menu, and choose `Edit`. Verify that the composer title is `Edit
  post`, then use the small edit control on the attachment card to open `Add alt text`.
  That field contains the existing description. Preserve it when adding a requested
  first line. Android Back returns the changed description to the edit composer (there
  is no separate save button on the alt-text screen); the edit is committed only by
  the composer's top-right submit arrow. The large floating pencil over a profile or
  timeline is the global new-post control and opens `New post`; it never edits the
  image beneath it.

- Explore search matches an exact contiguous string, not AND-tokenized terms; a
  multi-word query combining an account handle with a topic almost always returns
  "Could not find anything for these search terms". When a search comes back empty,
  do not stop or ask the user — simplify to one distinctive term and re-query: search
  the account handle alone on the `People` tab, or the topic alone on `Posts`/`Hashtags`,
  then open the matching profile or post.
- A task phrase like "the gourmet user"/"the <name> user" names the account by handle:
  search that bare identifier (no `@`) on the `People` tab to open the account's profile.
  Do not ask what the handle is.

- The Home timeline only shows recent posts from followed accounts; it cannot surface an
  arbitrary account's older or unscoped toot. To find a toot by a specific account (or
  about a specific topic), open the `Explore` tab, search the account handle or a
  distinctive topic term, and open the matching `Posts` result or the account's `People`
  profile — then scroll that profile's posts. Never try to locate it by scrolling Home.

- Lists are managed only on the Home surface: navigate to the Home bottom-navigation
  tab, open the dropdown beside the `Home` title, and choose `Lists`, then `Create list`
  to create a list or `Manage lists` to edit one. From any other surface, reach the Home
  tab first. Do not look for Lists in the Profile page, its tabs, or its top-right
  share/QR control — none of those open the Lists management panel.

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
  The label is the action that tapping performs, never the current state:
  - `Remove bookmark`: currently bookmarked; tap this to remove it.
  - `Bookmark`: currently not bookmarked; tapping this would add it.
  With that menu open, Android Back only dismisses it and leaves the Saved surface visible.
  Dismiss it as one action and observe the closed-menu frame before choosing any navigation.
  Only selecting a mutation item proves a write. Saved membership can remain visually stale
  afterward, so identify its resolved row by (`author_handle`, `content`).

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
- The signed-in user's followed hashtags are not under `Profile`. From the global
  `Home` surface, open the dropdown beside the `Home` title and choose `Followed
  hashtags`; this opens the followed-hashtag navigation panel.
  Selecting a hashtag opens its tag view. A `Following` button means the hashtag is
  currently followed; tapping it unfollows the hashtag and changes the button to
  `Follow`. Return to the followed-hashtag list to continue with other matching tags.
- All three collections expose `author_handle` and `content`, but no stable
  status ID or permalink. `author_handle` is the `@`-prefixed account handle,
  not the display name.
- Match a Toot across these collections by the exact pair (`author_handle`, `content`),
  never by ordinal. A tag-result Toot can only be relocated through its `TaggedToots`
  collection.
- Hashtags shown in a saved post's visible body are the post's classification evidence;
  the overflow menu contains actions, not classification data. Open it only for a matching
  post. If that post's header is clipped above its visible body, use a small upward scroll to
  reveal the aligned overflow control without crossing into the preceding post.
- A Toot's action row belongs to the card directly above it. Its Favorite control is
  directly mutable in `TaggedToots` when that card's (`author_handle`, `content`) and
  action row are visibly aligned. A filled blue star with a count means the Toot is
  favorited; an outline star means it is not. The corresponding Saved collection is
  an alternate membership view, not a prerequisite for reading or changing this state.
- Favorite and bookmark are independent state dimensions. Membership in one neither
  implies nor excludes membership in the other; only a task-stated predicate makes a
  saved collection relevant to the target set.
- Favorite mutation uses the boolean field `favorited`. It is a visible control state
  and mutation field on a `TaggedToots` row, but is neither a query field nor a query
  filter.
- Tapping the visible text body of a Toot opens its single-post detail view,
  titled `Post from <author>`. Do not tap the author/avatar or a media thumbnail:
  those open the author profile or media viewer instead. The detail view exposes
  the same author and content together with that Toot's own Favorite control, so it
  is an alternate mutation surface when the list action row is clipped or ambiguous.

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
- `TaggedToots` row mutation interface:
  - required observable route state: `tag`, retaining its exact leading `#`
  - row identity fields: `author_handle`, `content`
  - mutation fields: `favorited`, through that row's aligned Favorite control
- `TootDetail` interface:
  - required observable route state: `tag`, retaining its exact leading `#`
  - row identity fields: `author_handle`, `content`
  - mutation fields: `favorited`
- A `TaggedToots` row reaches `TootDetail` only through its exact source tag view and
  exact (`author_handle`, `content`) pair. The tag is route state, not a row field, and
  full Toot content is not a global search term.
- Entering either saved collection replaces the active tag route. After inspecting
  saved views, the exact `TaggedToots` tag route must be active again before any of its
  rows can expose `TootDetail`.

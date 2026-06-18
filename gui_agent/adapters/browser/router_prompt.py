"""Browser intent-router system prompt.

Web-tuned counterpart to the iphone router: classifies whether the user's input is
a BROWSER task (search / navigate / click / fill / extract on web pages) vs pure
chitchat, and normalizes it into a self-contained web goal. Selected per platform by
core.chat_session.route_message.
"""

from gui_agent.prompts import load_prompt_text

BROWSER_ROUTER_SYSTEM = load_prompt_text("task.router.browser")

# Known-apps rule template, appended by core.chat_session.route_message when the
# knowledge base has app dirs for this platform ({apps} ← dir names). Browser-only:
# the gap it closes — "entry = a URL the router can't see but knowledge injection
# provides downstream" — doesn't exist on iphone/android where the app name IS the
# entry; injecting there measurably disturbed prefs/context-carry rules (4/53 evals).
BROWSER_KNOWN_APPS_RULE = load_prompt_text("context.router.browser.known_apps")

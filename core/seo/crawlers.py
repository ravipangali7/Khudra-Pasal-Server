"""Detect social / messenger crawlers that need server-rendered Open Graph HTML."""

import re

# Facebook, Messenger, WhatsApp, Twitter/X, LinkedIn, Telegram, Slack, Discord, Pinterest, etc.
SOCIAL_CRAWLER_UA_RE = re.compile(
    r"facebookexternalhit|Facebot|Twitterbot|LinkedInBot|WhatsApp|"
    r"Slackbot|Discordbot|TelegramBot|Telegram|Google-InspectionTool|"
    r"Pinterest|Embedly|vkShare|W3C_Validator|redditbot|Applebot",
    re.I,
)


def is_social_crawler(user_agent: str | None) -> bool:
    return bool(user_agent and SOCIAL_CRAWLER_UA_RE.search(user_agent))

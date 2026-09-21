"""Facts shared by the four legal pages (templates/legal/).

Only what the application itself can state is filled in. Everything the business or its lawyers must decide (legal
entity, address, effective date, governing law, retention periods, refund method and timing ...) is left as a visible
[BUSINESS / LEGAL CONFIRMATION REQUIRED] marker in the page text - never guessed."""

PENDING = "[BUSINESS / LEGAL CONFIRMATION REQUIRED]"
VERSION = "1.0 (draft for legal review)"
LAST_UPDATED = "21 September 2026"

# (key, url name, short footer label, full title)
PAGES = (
    ("privacy", "legal:privacy", "Privacy Policy", "Privacy Policy"),
    ("terms", "legal:terms", "Terms & Conditions", "Terms & Conditions"),
    ("refund", "legal:refund", "Refund Policy", "Refund & Cancellation Policy"),
    ("ai_usage", "legal:ai_usage", "AI Usage Policy", "AI & Third-Party Model Usage Policy"),
)
PROVIDERS = (
    ("Anthropic", "Claude models", "https://www.anthropic.com/legal"),
    ("OpenAI", "GPT models", "https://openai.com/policies/"),
    ("Google", "Gemini models", "https://ai.google.dev/gemini-api/terms"),
    ("xAI", "Grok models", "https://x.ai/legal"),
    ("DeepSeek", "DeepSeek models", "https://www.deepseek.com/"),
)

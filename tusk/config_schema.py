"""Settings panel for Tusk in the Hermes dashboard and Desktop (Capabilities → Memory)."""

from plugins.memory.config_schema import (
    KIND_BOOL,
    KIND_NUMBER,
    KIND_SECRET,
    KIND_TEXT,
    STORAGE_FLAT_JSON,
    ProviderConfigSchema,
    ProviderField,
)

CONFIG_SCHEMA = ProviderConfigSchema(
    name="tusk",
    label="Tusk (memory on Walrus)",
    storage=STORAGE_FLAT_JSON,
    docs_url="https://github.com/Automationbot24x7/hermes-tusk#configuration",
    fields=(
        ProviderField(
            key="private_key",
            label="Delegate private key",
            kind=KIND_SECRET,
            env_key="MEMWAL_PRIVATE_KEY",
            inline=True,
            description="Create a delegate key at memory.walrus.xyz. Stored in your profile's .env.",
        ),
        ProviderField(
            key="account_id",
            label="Account id",
            kind=KIND_TEXT,
            inline=True,
            env_fallbacks=("MEMWAL_ACCOUNT_ID",),
            placeholder="0x…",
            description="Your Walrus Memory account id.",
        ),
        ProviderField(
            key="namespace",
            label="Namespace",
            kind=KIND_TEXT,
            default="default",
            inline=True,
            description="Use the same namespace as your other agents to share one memory.",
        ),
        ProviderField(
            key="capture_turns",
            label="Distill every turn into facts",
            kind=KIND_BOOL,
            default="true",
            group="Behaviour",
        ),
        ProviderField(
            key="recall_limit", label="Memories recalled per turn", kind=KIND_NUMBER, default="6", group="Behaviour"
        ),
        ProviderField(
            key="max_distance",
            label="Recall cutoff (lower is stricter)",
            kind=KIND_NUMBER,
            default="0.8",
            group="Behaviour",
        ),
        ProviderField(
            key="prefetch_timeout_s",
            label="Longest wait for live recall (seconds)",
            kind=KIND_NUMBER,
            default="2.5",
            group="Behaviour",
        ),
    ),
)

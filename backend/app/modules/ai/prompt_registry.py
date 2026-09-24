"""Immutable, server-owned prompt templates; user data is never executable instructions."""
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class PromptTemplate:
    key: str
    version: int
    instructions: str


FACEBOOK_EVENT_V1 = PromptTemplate(
    key="facebook_event_v1",
    version=1,
    instructions=(
        "Create nightclub social copy in the target language supplied in the user data. "
        "Return exactly three distinct variants. Do not invent prices, addresses, dates, "
        "times, or other event facts not supplied in the user data. The result requires "
        "human review and must contain only the requested structured schema."
    ),
)

PROMPT_REGISTRY = MappingProxyType({FACEBOOK_EVENT_V1.key: FACEBOOK_EVENT_V1})

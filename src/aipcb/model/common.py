"""Types shared across the model layers."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["NET_NAME_PATTERN", "Ident", "Layer", "NetName", "Strict"]

#: Copper layer names, in KiCad's spelling. Signal layers only; planes are zones.
Layer = Annotated[str, Field(pattern=r"^(F|B|In[0-9]{1,2})\.Cu$")]

#: Identifiers for modules, instances, parameters and the like.
Ident = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]

#: Net names. Permissive, but no whitespace and nothing that breaks KiCad's parser.
NET_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.+\-/]*$"
NetName = Annotated[str, Field(pattern=NET_NAME_PATTERN)]


class Strict(BaseModel):
    """Base model rejecting unknown fields, so a typo is an error not a shrug."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

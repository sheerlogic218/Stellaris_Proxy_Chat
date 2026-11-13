"""Pure description of which embassy voice channels should exist and who may join them.

Each claimed empire gets one voice channel. A user may join an empire's channel
when they claimed that empire or an empire it has communications with, and
(occupancy rule) their empire has communications with every empire already in
the channel. So if A knows B and B knows C, but A doesn't know C, B's channel is
hidden from C while A is in it.
"""

from dataclasses import dataclass

from proxychat_core.names import MAX_NAME as MAX_CHANNEL_NAME
from proxychat_core.names import clean_name


@dataclass(frozen=True)
class Embassy:
    country: int
    name: str
    allowed_users: frozenset[int]


def channel_name(country_id, country):
    return clean_name(country.name if country else "", f"Empire {country_id}", MAX_CHANNEL_NAME)


def desired_embassies(game):
    """Map country id -> Embassy, for every empire with at least one claimer."""
    if game is None:
        return {}
    result = {}
    for country in sorted(set(game.claims.values())):
        allowed_countries = {country} | game.contacts(country)
        users = frozenset(u for u, c in game.claims.items() if c in allowed_countries)
        result[country] = Embassy(country, channel_name(country, game.countries.get(country)), users)
    return result


GALCOM_NAME = "Galactic Community"


def galcom_users(game):
    """Users who may join the Galactic Community channel: claimers of member empires.

    The occupancy rule doesn't apply there: it's where every member meets.
    """
    if game is None:
        return frozenset()
    return frozenset(u for u, c in game.claims.items() if c in game.galcom)


def _compatible(game, a, b):
    return a == b or b in game.contacts(a)


def resolve_occupancy(game, emb, present):
    """Apply the occupancy rule to an embassy.

    present: user ids currently in the channel, earliest joiner first.
    Returns (allowed_users, kick): who may see/join the channel right now, and who
    must be disconnected. Users are kept in join order while their empire knows
    every empire kept before them, so on a simultaneous join the later one goes.
    """
    kept = []
    kick = []
    for user in present:
        country = game.claims.get(user)
        if user in emb.allowed_users and all(_compatible(game, country, k) for k in kept):
            kept.append(country)
        else:
            kick.append(user)
    allowed = frozenset(
        u for u in emb.allowed_users if all(_compatible(game, game.claims[u], k) for k in kept)
    )
    return allowed, kick


@dataclass(frozen=True)
class FederationRoom:
    fed: int
    name: str
    allowed_users: frozenset[int]


def federation_name(fed):
    return clean_name(fed.name, f"Federation {fed.id}", MAX_CHANNEL_NAME)


def desired_federations(game):
    """Map federation id -> FederationRoom, for every federation with a claimed member.

    Claimers of member empires and of associates may join. As in the Galactic
    Community, the occupancy rule doesn't apply: it's where the federation meets.
    """
    if game is None:
        return {}
    result = {}
    for fid, fed in sorted(game.federations.items()):
        if not any(c in fed.members for c in game.claims.values()):
            continue
        empires = fed.members | fed.associates
        users = frozenset(u for u, c in game.claims.items() if c in empires)
        result[fid] = FederationRoom(fid, federation_name(fed), users)
    return result

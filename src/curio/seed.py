"""Idempotent demo data seeder for local development and manual testing.

Never called automatically on app startup — invoke via `curio seed-demo`.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from curio.db.models.catalogs import Preset
from curio.db.models.users import User
from curio.repositories import catalogs as catalogs_repo
from curio.repositories import users as users_repo
from curio.services import users as users_service


@dataclass(frozen=True)
class DemoSeedResult:
    user: User
    created_user: bool
    login_token: str | None
    movies_count: int
    series_count: int
    movie_preset_id: UUID
    series_preset_id: UUID


_DEMO_MOVIES: list[dict[str, object]] = [
    {
        "imdb_id": "tt0114369",
        "name": "Se7en",
        "year": 1995,
        "rating": 8.6,
        "tmdb_id": 807,
        "poster_url": "https://image.tmdb.org/t/p/w500/6yoghtyTpznpBik8EngEmJskVUO.jpg",
        "description": (
            "Two detectives hunt a serial killer who uses the seven deadly sins as his motives."
        ),
    },
    {
        "imdb_id": "tt1392214",
        "name": "Prisoners",
        "year": 2013,
        "rating": 8.2,
        "tmdb_id": 146233,
        "poster_url": None,
        "description": (
            "When two young girls go missing, a desperate father takes matters into his own hands."
        ),
    },
    {
        "imdb_id": "tt0443706",
        "name": "Zodiac",
        "year": 2007,
        "rating": 7.7,
        "tmdb_id": 1949,
        "poster_url": None,
        "description": (
            "Cartoonists and detectives become obsessed with tracking the Zodiac killer in San "
            "Francisco."
        ),
    },
    {
        "imdb_id": "tt0780504",
        "name": "Drive",
        "year": 2011,
        "rating": 7.8,
        "tmdb_id": 64690,
        "poster_url": "https://image.tmdb.org/t/p/w500/602vevIURmpDfzbnv5Ubi6wIkQm.jpg",
        "description": (
            "A quiet Hollywood stuntman moonlights as a getaway driver and runs into trouble."
        ),
    },
    {
        "imdb_id": "tt2872718",
        "name": "Nightcrawler",
        "year": 2014,
        "rating": 7.9,
        "tmdb_id": 242582,
        "poster_url": "https://image.tmdb.org/t/p/w500/j9HrX8f7GbZQm1BrBiR40uFQZSb.jpg",
        "description": (
            "A driven loner breaks into the nocturnal world of freelance crime journalism in "
            "Los Angeles."
        ),
    },
    {
        "imdb_id": "tt3397884",
        "name": "Sicario",
        "year": 2015,
        "rating": 7.6,
        "tmdb_id": 273481,
        "poster_url": None,
        "description": (
            "An idealistic FBI agent is enlisted by a government task force to aid in the "
            "escalating war on drugs."
        ),
    },
    {
        "imdb_id": "tt0477348",
        "name": "No Country for Old Men",
        "year": 2007,
        "rating": 8.2,
        "tmdb_id": 6977,
        "poster_url": "https://image.tmdb.org/t/p/w500/6d5XOczc226jECq0LIX0siKtgHR.jpg",
        "description": (
            "Violence and mayhem ensue after a hunter stumbles upon a drug deal gone wrong in "
            "West Texas."
        ),
    },
    {
        "imdb_id": "tt0369339",
        "name": "Collateral",
        "year": 2004,
        "rating": 7.5,
        "tmdb_id": 1538,
        "poster_url": None,
        "description": (
            "A cab driver finds himself the hostage of an engaging contract killer making his "
            "rounds."
        ),
    },
    {
        "imdb_id": "tt0113277",
        "name": "Heat",
        "year": 1995,
        "rating": 8.3,
        "tmdb_id": 949,
        "poster_url": "https://image.tmdb.org/t/p/w500/umSVjVdbVwtx5ryCA2QXL44Durm.jpg",
        "description": (
            "A master thief and a relentless LAPD detective lock horns across Los Angeles."
        ),
    },
    {
        "imdb_id": "tt0209144",
        "name": "Memento",
        "year": 2000,
        "rating": 8.4,
        "tmdb_id": 77,
        "poster_url": "https://image.tmdb.org/t/p/w500/yuNs09hvpHVU1cBTCAk9zxsL2oW.jpg",
        "description": (
            "A man with short-term memory loss uses Polaroids and tattoos to hunt his wife's "
            "killer."
        ),
    },
    {
        "imdb_id": "tt0364569",
        "name": "Oldboy",
        "year": 2003,
        "rating": 8.3,
        "tmdb_id": 670,
        "poster_url": "https://image.tmdb.org/t/p/w500/pWDtjs568ZfOTMbURQBYuT4Qxka.jpg",
        "description": (
            "After being kidnapped and imprisoned for fifteen years, Oh Dae-Su has five days to "
            "find his captor."
        ),
    },
    {
        "imdb_id": "tt0353969",
        "name": "Memories of Murder",
        "year": 2003,
        "rating": 8.1,
        "tmdb_id": 11423,
        "poster_url": None,
        "description": (
            "Two detectives in a small Korean province struggle to solve a string of baffling "
            "murders."
        ),
    },
    {
        "imdb_id": "tt2267998",
        "name": "Gone Girl",
        "year": 2014,
        "rating": 8.1,
        "tmdb_id": 210577,
        "poster_url": None,
        "description": (
            "With his wife's disappearance having become the focus of an intense media circus, "
            "a man sees suspicion fall on him."
        ),
    },
    {
        "imdb_id": "tt1130884",
        "name": "Shutter Island",
        "year": 2010,
        "rating": 8.2,
        "tmdb_id": 11324,
        "poster_url": "https://image.tmdb.org/t/p/w500/kve20tXwUZpu4GUX8l6X7Z4jmL6.jpg",
        "description": (
            "A U.S. Marshal investigates a psychiatric facility on a remote island after a "
            "patient vanishes."
        ),
    },
    {
        "imdb_id": "tt0407887",
        "name": "The Departed",
        "year": 2006,
        "rating": 8.5,
        "tmdb_id": 1422,
        "poster_url": "https://image.tmdb.org/t/p/w500/nT97ifVT2J1yMQmeq20Qblg61T.jpg",
        "description": (
            "An undercover cop and a mole in the police attempt to identify each other in Boston."
        ),
    },
    {
        "imdb_id": "tt2582802",
        "name": "Whiplash",
        "year": 2014,
        "rating": 8.5,
        "tmdb_id": 244786,
        "poster_url": "https://image.tmdb.org/t/p/w500/7fn624j5lj3xTme2SgiLCeuedmO.jpg",
        "description": (
            "A promising young drummer enrolls at a cut-throat music conservatory under an "
            "abusive instructor."
        ),
    },
    {
        "imdb_id": "tt1392190",
        "name": "Mad Max: Fury Road",
        "year": 2015,
        "rating": 8.1,
        "tmdb_id": 76341,
        "poster_url": "https://image.tmdb.org/t/p/w500/8tZYtuWezp8JbcsvHYO0O46tFbo.jpg",
        "description": (
            "In a post-apocalyptic wasteland, a woman rebels against a tyrannical ruler in "
            "search of her homeland."
        ),
    },
    {
        "imdb_id": "tt5727208",
        "name": "Uncut Gems",
        "year": 2019,
        "rating": 7.4,
        "tmdb_id": 473033,
        "poster_url": "https://image.tmdb.org/t/p/w500/6XN1vxHc7kUSqNWtaQKN45J5x2v.jpg",
        "description": (
            "A charismatic New York City jeweler always on the lookout for the next big score "
            "makes a high-stakes bet."
        ),
    },
    {
        "imdb_id": "tt4846232",
        "name": "Good Time",
        "year": 2017,
        "rating": 7.3,
        "tmdb_id": 429200,
        "poster_url": None,
        "description": (
            "After a botched bank robbery lands his younger brother in prison, a man embarks on "
            "a twisted odyssey."
        ),
    },
    {
        "imdb_id": "tt4062536",
        "name": "Green Room",
        "year": 2015,
        "rating": 7.0,
        "tmdb_id": 313922,
        "poster_url": None,
        "description": (
            "A punk rock band is forced to fight for survival after witnessing a murder at an "
            "isolated venue."
        ),
    },
    {
        "imdb_id": "tt5362988",
        "name": "Wind River",
        "year": 2017,
        "rating": 7.7,
        "tmdb_id": 395834,
        "poster_url": None,
        "description": (
            "A wildlife tracker and an FBI agent investigate a murder on a snowy Wyoming "
            "reservation."
        ),
    },
    {
        "imdb_id": "tt1790864",
        "name": "The Maze Runner",
        "year": 2014,
        "rating": 6.8,
        "tmdb_id": 198663,
        "poster_url": "https://image.tmdb.org/t/p/w500/ode14q7WtDugFDp78fo9lCsmay9.jpg",
        "description": (
            "Thomas is deposited in a community of boys after his memory is erased and must "
            "escape a giant maze."
        ),
    },
    {
        "imdb_id": "tt0094898",
        "name": "Coming to America",
        "year": 1988,
        "rating": 7.1,
        "tmdb_id": 9602,
        "poster_url": None,  # Intentionally missing poster to test fallback rendering
        "description": (
            "An extremely pampered African prince travels to Queens, New York, to find a bride."
        ),
    },
    {
        "imdb_id": "tt0119174",
        "name": "The Game",
        "year": 1997,
        "rating": 7.7,
        "tmdb_id": 2649,
        "poster_url": None,
        "description": (
            "After a wealthy San Francisco banker is given an unusual birthday gift, his life "
            "spirals out of control."
        ),
    },
    {
        "imdb_id": "tt1758830",
        "name": "The Snowman",
        "year": 2017,
        "rating": 4.7,
        "tmdb_id": 372343,
        "poster_url": None,
        "description": (
            "Detective Harry Hole investigates the disappearance of a woman whose scarf is "
            "found on a snowman."
        ),
    },
]

_DEMO_SERIES: list[dict[str, object]] = [
    {
        "imdb_id": "tt11280740",
        "name": "Severance",
        "year": 2022,
        "rating": 8.7,
        "tmdb_id": 95396,
        "poster_url": "https://image.tmdb.org/t/p/w500/lFf6LLrQjYldcZItzOkGmMMigP7.jpg",
        "description": (
            "Mark leads a team of office workers whose memories have been surgically divided "
            "between work and personal lives."
        ),
    },
    {
        "imdb_id": "tt5753856",
        "name": "Dark",
        "year": 2017,
        "rating": 8.7,
        "tmdb_id": 70523,
        "poster_url": "https://image.tmdb.org/t/p/w500/apbrbWs8M9lyOpJYU5WXrpFbk1Z.jpg",
        "description": (
            "A family saga with a supernatural twist, set in a German town where the "
            "disappearance of children exposes secrets."
        ),
    },
    {
        "imdb_id": "tt9253284",
        "name": "Andor",
        "year": 2022,
        "rating": 8.4,
        "tmdb_id": 83867,
        "poster_url": "https://image.tmdb.org/t/p/w500/59SVNwLfoMnZPPB6ukW6dlPxAdI.jpg",
        "description": (
            "In an era filled with danger and intrigue, Cassian Andor embarks on a path "
            "destined to turn him into a rebel hero."
        ),
    },
    {
        "imdb_id": "tt3230854",
        "name": "The Expanse",
        "year": 2015,
        "rating": 8.5,
        "tmdb_id": 63639,
        "poster_url": None,
        "description": (
            "A hardened detective and a rogue ship's crew uncover a vast conspiracy that "
            "threatens the Solar System."
        ),
    },
    {
        "imdb_id": "tt14688458",
        "name": "Silo",
        "year": 2023,
        "rating": 8.1,
        "tmdb_id": 125988,
        "poster_url": None,
        "description": (
            "Men and women live in a giant silo underground with several regulations they "
            "believe protect them."
        ),
    },
    {
        "imdb_id": "tt0804484",
        "name": "Foundation",
        "year": 2021,
        "rating": 7.6,
        "tmdb_id": 93740,
        "poster_url": None,
        "description": (
            "A complex saga of humans scattered on planets throughout the galaxy all living "
            "under the rule of the Galactic Empire."
        ),
    },
    {
        "imdb_id": "tt0475784",
        "name": "Westworld",
        "year": 2016,
        "rating": 8.5,
        "tmdb_id": 63247,
        "poster_url": "https://image.tmdb.org/t/p/w500/8MfgyFHf7XEboZJPZXCIDqqiz6e.jpg",
        "description": (
            "At the intersection of the near future and the reimagined past, a world awaits in "
            "which every human appetite can be indulged."
        ),
    },
    {
        "imdb_id": "tt8134186",
        "name": "Devs",
        "year": 2020,
        "rating": 7.7,
        "tmdb_id": 91363,
        "poster_url": None,
        "description": (
            "A computer engineer investigates the secretive development division in her "
            "company, which she believes is behind her boyfriend's disappearance."
        ),
    },
    {
        "imdb_id": "tt2699110",
        "name": "The Leftovers",
        "year": 2014,
        "rating": 8.3,
        "tmdb_id": 54344,
        "poster_url": None,
        "description": (
            "Three years after the disappearance of two percent of the global population, a "
            "group of people in New York struggle to continue."
        ),
    },
    {
        "imdb_id": "tt10574558",
        "name": "Station Eleven",
        "year": 2021,
        "rating": 7.7,
        "tmdb_id": 111044,
        "poster_url": None,
        "description": (
            "A post-apocalyptic saga spanning multiple timelines, telling the stories of "
            "survivors of a devastating flu."
        ),
    },
    {
        "imdb_id": "tt4158110",
        "name": "Mr. Robot",
        "year": 2015,
        "rating": 8.5,
        "tmdb_id": 62560,
        "poster_url": "https://image.tmdb.org/t/p/w500/oKIBhzZzDX07SoE2bOLhq2EE8rf.jpg",
        "description": (
            "Elliot, a brilliant but unstable cyber-security engineer and vigilante hacker, "
            "becomes a key figure in a complex game."
        ),
    },
    {
        "imdb_id": "tt1474684",
        "name": "Luther",
        "year": 2010,
        "rating": 8.4,
        "tmdb_id": 1426,
        "poster_url": None,
        "description": (
            "DCI John Luther is a near-genius murder detective whose brilliant mind can't "
            "always save him from the dark."
        ),
    },
    {
        "imdb_id": "tt2261227",
        "name": "Altered Carbon",
        "year": 2018,
        "rating": 7.9,
        "tmdb_id": 68421,
        "poster_url": None,
        "description": (
            "Set in a future where consciousness is digitized and stored, a prisoner returns to "
            "life in a new body."
        ),
    },
    {
        "imdb_id": "tt1839578",
        "name": "Person of Interest",
        "year": 2011,
        "rating": 8.5,
        "tmdb_id": 1411,
        "poster_url": None,
        "description": (
            "An ex-CIA agent and a wealthy programmer save lives via a surveillance AI that "
            "sends them the identities of civilians."
        ),
    },
    {
        "imdb_id": "tt2085059",
        "name": "Black Mirror",
        "year": 2011,
        "rating": 8.7,
        "tmdb_id": 42009,
        "poster_url": "https://image.tmdb.org/t/p/w500/5UaYsGZOFhjFDwQh6GuLjjA1WlF.jpg",
        "description": (
            "An anthology series exploring a twisted, high-tech multiverse where humanity's "
            "greatest innovations collide with its darkest instincts."
        ),
    },
    {
        "imdb_id": "tt7772588",
        "name": "For All Mankind",
        "year": 2019,
        "rating": 8.1,
        "tmdb_id": 87917,
        "poster_url": None,
        "description": (
            "In an alternative version of 1969, the Soviet Union beats the United States to the "
            "Moon, and the space race continues."
        ),
    },
    {
        "imdb_id": "tt1119644",
        "name": "Fringe",
        "year": 2008,
        "rating": 8.4,
        "tmdb_id": 1705,
        "poster_url": "https://image.tmdb.org/t/p/w500/sY9hg5dLJ93RJOyKEiu1nAtBRND.jpg",
        "description": (
            "An F.B.I. agent is forced to work with an institutionalized scientist and his son "
            "in order to rationalize a brewing storm."
        ),
    },
    {
        "imdb_id": "tt13016388",
        "name": "3 Body Problem",
        "year": 2024,
        "rating": 7.5,
        "tmdb_id": 108545,
        "poster_url": None,
        "description": (
            "A fateful decision made in 1960s China reverberates in the present day, where a "
            "group of scientists confront humanity's greatest threat."
        ),
    },
    {
        "imdb_id": "tt15438246",
        "name": "Scavengers Reign",
        "year": 2023,
        "rating": 8.7,
        "tmdb_id": 203744,
        "poster_url": None,
        "description": (
            "The surviving crew of a damaged deep space freighter are stranded on a beautiful "
            "yet unforgiving alien planet."
        ),
    },
    {
        "imdb_id": "tt2802850",
        "name": "Fargo",
        "year": 2014,
        "rating": 8.9,
        "tmdb_id": 60622,
        "poster_url": "https://image.tmdb.org/t/p/w500/6U9CPeD8obHzweikFhiLhpc7YBT.jpg",
        "description": (
            "Various chronicles of deception, intrigue, and murder in and around frozen Minnesota."
        ),
    },
    {
        "imdb_id": "tt7660850",
        "name": "Succession",
        "year": 2018,
        "rating": 8.8,
        "tmdb_id": 76331,
        "poster_url": "https://image.tmdb.org/t/p/w500/7HW47XbkNQ5fiwQFYGWdw9gs144.jpg",
        "description": (
            "The Roy family is known for controlling the biggest media and entertainment "
            "company in the world."
        ),
    },
    {
        "imdb_id": "tt8772296",
        "name": "Euphoria",
        "year": 2019,
        "rating": 8.3,
        "tmdb_id": 85552,
        "poster_url": "https://image.tmdb.org/t/p/w500/3Q0hd3heuWwDWpwcDkhQOA6TYWI.jpg",
        "description": (
            "A look at life for a group of high school students as they grapple with issues of "
            "drugs, sex, and violence."
        ),
    },
    {
        "imdb_id": "tt2077823",
        "name": "The OA",
        "year": 2016,
        "rating": 7.8,
        "tmdb_id": 69061,
        "poster_url": None,  # Intentionally missing poster to test fallback rendering
        "description": (
            "Having gone missing seven years ago, a previously blind young woman returns home "
            "with her sight restored."
        ),
    },
    {
        "imdb_id": "tt1856010",
        "name": "House of Cards",
        "year": 2013,
        "rating": 8.6,
        "tmdb_id": 1425,
        "poster_url": "https://image.tmdb.org/t/p/w500/hKWxWjFwnMvkWQawbhvC0Y7ygQ8.jpg",
        "description": (
            "A Congressman works with his equally conniving wife to exact revenge on the people "
            "who betrayed him."
        ),
    },
    {
        "imdb_id": "tt1043813",
        "name": "Titans",
        "year": 2018,
        "rating": 5.2,
        "tmdb_id": 75450,
        "poster_url": None,
        "description": (
            "A team of young superheroes led by Nightwing form to combat evil and other perils."
        ),
    },
]


async def _ensure_preset(
    session: AsyncSession,
    *,
    user_id: UUID,
    name: str,
    media_type: str,
    min_rating: float | None,
    criteria: str | None,
) -> Preset:
    existing = await catalogs_repo.get_preset_by_name(
        session,
        user_id=user_id,
        name=name,
        media_type=media_type,
    )
    if existing is not None:
        return existing

    return await catalogs_repo.create_preset(
        session,
        user_id=user_id,
        name=name,
        media_type=media_type,
        min_rating=min_rating,
        criteria=criteria,
    )


async def seed_demo(session: AsyncSession, *, username: str = "demo") -> DemoSeedResult:
    """Idempotently seed a demo user, 50 media items, presets, slots, history, and vetoes."""
    existing_user = await users_repo.get_user_by_username(session, username=username)
    if existing_user is None:
        provisioned = await users_service.create_user_with_login_token(session, username=username)
        user = provisioned.user
        created_user = True
        login_token: str | None = provisioned.login_token
    else:
        user = existing_user
        created_user = False
        login_token = None

    media_rows: list[dict[str, object]] = [
        {**item, "media_type": "movie"} for item in _DEMO_MOVIES
    ] + [{**item, "media_type": "series"} for item in _DEMO_SERIES]
    await catalogs_repo.insert_media_items_ignore_conflicts(session, media_rows)

    movie_preset = await _ensure_preset(
        session,
        user_id=user.id,
        name="Late Night Thrillers",
        media_type="movie",
        min_rating=6.5,
        criteria="Atmospheric, taut, late-night thrillers and neo-noir mysteries.",
    )
    series_preset = await _ensure_preset(
        session,
        user_id=user.id,
        name="Prestige Sci-Fi",
        media_type="series",
        min_rating=7.0,
        criteria="High-concept, character-driven prestige science fiction series.",
    )

    slot_rows: list[dict[str, object]] = [
        {
            "user_id": user.id,
            "slot_id": "curio_movie_1",
            "media_type": "movie",
            "preset_id": movie_preset.id,
        },
        {
            "user_id": user.id,
            "slot_id": "curio_movie_2",
            "media_type": "movie",
            "preset_id": None,
        },
        {
            "user_id": user.id,
            "slot_id": "curio_movie_3",
            "media_type": "movie",
            "preset_id": None,
        },
        {
            "user_id": user.id,
            "slot_id": "curio_series_1",
            "media_type": "series",
            "preset_id": series_preset.id,
        },
        {
            "user_id": user.id,
            "slot_id": "curio_series_2",
            "media_type": "series",
            "preset_id": None,
        },
        {
            "user_id": user.id,
            "slot_id": "curio_series_3",
            "media_type": "series",
            "preset_id": None,
        },
    ]
    await catalogs_repo.insert_slots_ignore_conflicts(session, slot_rows)

    candidate_rows: list[dict[str, object]] = [
        {
            "user_id": user.id,
            "preset_id": movie_preset.id,
            "imdb_id": str(item["imdb_id"]),
            "position": idx,
            "blurb": f"Curated pick #{idx + 1} for Late Night Thrillers.",
        }
        for idx, item in enumerate(_DEMO_MOVIES)
    ] + [
        {
            "user_id": user.id,
            "preset_id": series_preset.id,
            "imdb_id": str(item["imdb_id"]),
            "position": idx,
            "blurb": f"Curated pick #{idx + 1} for Prestige Sci-Fi.",
        }
        for idx, item in enumerate(_DEMO_SERIES)
    ]
    await catalogs_repo.insert_candidates_ignore_conflicts(session, candidate_rows)

    now = datetime.now(UTC)
    watch_history_rows: list[dict[str, object]] = [
        {
            "user_id": user.id,
            "imdb_id": str(_DEMO_MOVIES[0]["imdb_id"]),  # Se7en - watched, high rating
            "status": "watched",
            "rating": 8.5,
            "watched_at": now,
            "source": "manual",
        },
        {
            "user_id": user.id,
            "imdb_id": str(_DEMO_MOVIES[24]["imdb_id"]),  # The Snowman - low-rated
            "status": "watched",
            "rating": 3.5,
            "watched_at": now,
            "source": "manual",
        },
        {
            "user_id": user.id,
            "imdb_id": str(_DEMO_SERIES[24]["imdb_id"]),  # Titans - dropped series
            "status": "dropped",
            "rating": 4.0,
            "watched_at": now,
            "source": "manual",
        },
    ]
    await catalogs_repo.insert_watch_history_ignore_conflicts(session, watch_history_rows)

    veto_rows: list[dict[str, object]] = [
        {
            "user_id": user.id,
            "imdb_id": str(_DEMO_MOVIES[1]["imdb_id"]),  # Prisoners - seen
            "kind": "seen",
        },
        {
            "user_id": user.id,
            "imdb_id": str(_DEMO_SERIES[21]["imdb_id"]),  # Euphoria - not_for_me
            "kind": "not_for_me",
        },
    ]
    await catalogs_repo.insert_vetoes_ignore_conflicts(session, veto_rows)

    return DemoSeedResult(
        user=user,
        created_user=created_user,
        login_token=login_token,
        movies_count=len(_DEMO_MOVIES),
        series_count=len(_DEMO_SERIES),
        movie_preset_id=movie_preset.id,
        series_preset_id=series_preset.id,
    )

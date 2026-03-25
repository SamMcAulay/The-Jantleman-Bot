import asyncio
import logging
import re
from difflib import get_close_matches

import aiosqlite
import discord
from discord.ext import commands

import database

# ── Heuristic Feedback Classifier ─────────────────────────────────────────────
#
# Scores a block of text on two axes:
#   1. Domain relevance  — Rust / base-building vocabulary
#   2. Feedback intent   — critique, suggestion, or actionable language
#
# Both axes must register for detection to fire.  Plain Rust chat ("nice base")
# fails axis 2; generic opinions ("you should fix it") fail axis 1.
#
# Fuzzy matching (difflib) handles typos and common misspellings on all
# single-word keyword sets.  Multi-word phrases use exact substring matching
# since they're already distinctive enough.

# ── Domain vocabulary (axis 1) ─────────────────────────────────────────────────

# Very short Rust abbreviations that the tokeniser (min 4 chars) would drop.
# Checked with word-boundary regex directly against the raw lowercased text.
_RUST_ABBREV_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE) for p in [
        r"\btc\b",   # tool cupboard
        r"\bc4\b",   # C4 explosive
        r"\bbp\b",   # blueprint (sometimes used in base feedback context)
        r"\bak\b",   # AK-47
        r"\blr\b",   # LR-300
    ]
)

# Single words — fuzzy matched (handles typos / misspellings)
_RUST_SINGLES: frozenset[str] = frozenset({
    # Structural components
    "honeycomb", "honeycombing", "honeycombed",
    "airlock", "bunker", "foundation", "compound", "hatch",
    "roof", "wall", "floor", "ceiling", "corridor",
    "entrance", "doorway", "window", "tower", "perimeter",
    "triangle", "core", "embrasure", "barricade", "halfwall",
    "chokepoint", "choke", "exterior", "interior", "externals",
    # Base types
    "cave", "cliff", "rock", "island",
    # Materials / tiers
    "twig", "wood", "stone", "metal", "armored", "armoured",
    # Raid / offence / defence
    "raider", "raiders", "turret", "rocket", "satchel",
    "grief", "griefing", "griefer", "griefable",
    "splash", "raidable", "offline",
    "pickaxe", "jackhammer",
    # Tactical / line-of-sight
    "angle", "angles", "sightline", "elevation", "highground",
    # Upkeep / resources
    "upkeep", "decay", "sulfur",
    # General base anatomy (these score lower via _BUILDING_SINGLES if listed there)
    "placement", "layout", "design", "layer", "layers",
    "coverage", "gap", "gaps", "opening", "access", "positioning",
})

# Multi-word Rust phrases — exact substring (already distinctive enough)
_RUST_PHRASES: tuple[str, ...] = (
    "tool cupboard", "loot room", "air lock", "soft side", "hard side",
    "sheet door", "garage door", "armored door", "high external",
    "outer wall", "inner wall", "square foundation", "triangle foundation",
    "shooting floor", "window bars", "auto turret", "sam site",
    "flame turret", "shotgun trap", "door camp", "roof access",
    "external tc", "pick through", "double wall", "single wall",
    "wall frame", "floor frame", "high quality", "tool cup",
    "line of sight", "shooting angle", "door stack",
    "easy offline", "offline raid", "easy raid",
    "solo base", "duo base", "trio base", "quad base",
    "external walls", "tc range", "loot cave",
    "high ground", "getting picked",
)

# General building / structural words meaningful in a feedback context
# (fuzzy matched — lower weight, supplements Rust-specific terms)
_BUILDING_SINGLES: frozenset[str] = frozenset({
    "base", "build", "building", "structure", "side", "corner",
    "front", "back", "flank", "cover", "covering",
    "protected", "protection", "exposed", "vulnerable", "accessible",
    "reachable", "blocked", "sealed", "tight", "solid", "sturdy", "fragile",
})

# ── Feedback / intent vocabulary (axis 2) ──────────────────────────────────────

# Single opinion / suggestion words — fuzzy matched
_INTENT_SINGLES: frozenset[str] = frozenset({
    # Suggestion / improvement verbs
    "suggest", "recommend", "consider", "reconsider", "rethink",
    "improve", "upgrade", "reinforce", "fortify", "strengthen",
    "rework", "rebuild", "redesign", "restructure",
    # Problem / weakness words
    "problem", "issue", "flaw", "weakness", "mistake", "oversight",
    "risk", "danger", "concern", "warning", "vulnerability",
    # Observation words
    "noticed", "notice", "observed", "observation", "spotted",
    "pointed", "pointing",
    # Opinion markers
    "personally", "honestly", "overall", "generally", "basically",
    "typically", "ideally", "realistically",
})

# Regex patterns for actionable / critique language (compiled once at import)
_CRITIQUE_PATTERNS: tuple[re.Pattern, ...] = tuple(re.compile(p, re.IGNORECASE) for p in [
    # Classic "too X" critique
    r"\btoo\s+\w+",
    # Vulnerability / exposure
    r"\bvulner\w*",
    r"\bexpos\w+",
    r"\bweak\s*spot",
    r"\bweak\s+point",
    r"\braidable\b",
    r"\bgriefable\b",
    # Possessive critiques: "your X is / looks / seems"
    r"\byou[r']?[s]?\s+\w[\w\s]{0,20}\b(is|are|looks?|seems?|appears?)\b",
    # "the X is / needs / lacks"
    r"\bthe\s+\w[\w\s]{0,15}\b(is|are|needs?|lacks?)\b",
    # "this X is / looks"
    r"\bthis\s+\w[\w\s]{0,15}\b(is|are|looks?|seems?)\b",
    # Needs / requires / lacks
    r"\bneeds?\s+\w+",
    r"\brequires?\s+\w+",
    r"\black(s|ing)?\b",
    r"\bmissing\b",
    # Should / could / would — broadened
    r"\bshould\s+\w+",
    r"\bcould\s+\w+",
    r"\bwould\s+(be\s+\w+|help|suggest|recommend)\b",
    # First-person suggestions
    r"\bi['\s]?d\s+(suggest|recommend|say|add|move|put|try|drop|push|pull)\b",
    r"\bi\s+would\s+\w+",
    r"\byou\s+(might|may|should|could|need\s+to|want\s+to)\b",
    r"\btry\s+(to\s+)?\w+",
    r"\bconsider\s+\w+",
    # Problem / issue framing
    r"\bthe\s+(problem|issue|weakness|flaw|concern|risk)\b",
    r"\ba\s+(problem|issue|weakness|flaw|concern)\b",
    r"\bone\s+(thing|issue|problem|concern)\b",
    r"\bif\s+(someone|they|raiders?|anyone|enemies)\s+\w+",
    # Ease-of-raid / accessibility language
    r"\beasy\s+(to\s+)?\w+",
    r"\beasily\s+\w+",
    r"\bcan\s+(get|reach|access|pick|break|rush|raid|grief|splash)\b",
    r"\bcan\s+be\s+(rushed|raided|picked|griefed|accessed|reached)\b",
    r"\braider[s']?\s+can\b",
    r"\bpick\s+through\b",
    r"\bget\s+(on|over|around|through|in|to)\b",
    r"\bleave[s']?\s+you\b",           # "leaves you open"
    r"\bgive[s']?\s+(raiders?|them|access|an?\s+angle)\b",
    r"\bopen\s+to\b",                  # "open to raid / attack"
    r"\bgets?\s+on\s+(the|your)\b",   # "gets on the roof"
    # Fix / change / action verbs
    r"\badd\s+\w+",
    r"\bmove\s+(the\s+)?\w+",
    r"\bdrop\s+(the\s+|your\s+)?\w+", # "drop your tc higher"
    r"\bpush\s+(the\s+)?\w+",
    r"\bpull\s+(the\s+)?\w+",
    r"\breplace\s+(the\s+)?\w+",
    r"\bfix\s+\w+",
    r"\bclose\s+off\b",
    r"\bseal\s+(up\s+)?\w+",
    r"\bpatch\s+\w+",
    r"\bblock\s+\w+",
    r"\bshift\s+\w+",
    r"\brotate\s+\w+",
    r"\bswap\s+\w+",
    r"\braise\s+(the\s+)?\w+",
    r"\blower\s+(the\s+)?\w+",
    # Comparative / improvement language
    r"\bbetter\s+(if|with|to|placement|layout|position)\b",
    r"\bworse\b",
    r"\bimprove[sd]?\b",
    r"\bstronger\b",
    r"\bweaker\b",
    r"\bmore\s+(cover|protection|layers?|airlocks?|walls?|cover|honeycombing)\b",
    r"\bless\s+(exposed|cover|protection|accessible)\b",
    r"\bway\s+(better|worse|more|less)\b",
    # Watch-out / warning language
    r"\bwatch\s+out\b",
    r"\bcareful\b",
    r"\bdangerous\b",
    r"\brisky\b",
    r"\bdon['\s]?t\s+\w+",
    r"\bmake\s+sure\b",
    r"\bremember\s+to\b",
    # Opinion openers
    r"\bi\s+(think|feel|believe|reckon|notice|see|noticed)\b",
    r"\bin\s+my\s+(opinion|experience|view)\b",
    r"\bfrom\s+my\s+experience\b",
    r"\bngl\b",                        # "not gonna lie"
    r"\bimo\b",
    r"\bimho\b",
    r"\bhonestly\b",
    r"\bpersonally\b",
    r"\btbh\b",                        # "to be honest"
    r"\blowkey\b",                     # "lowkey the entrance is weak"
    # Contrast/concession — real feedback often follows a compliment
    r"\b(but|however|although|though|that\s+said|having\s+said)\b.{5,}",
    r"\bexcept\s+(for|that|the)\b",
])

# List / structural formatting patterns
_STRUCTURE_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"^\s*[-–—•*]\s+\S", re.MULTILINE),         # bullet points
    re.compile(r"^\s*\d+[.)]\s+\S", re.MULTILINE),          # numbered lists
    re.compile(r"\.\s+\w", re.MULTILINE),                    # multiple sentences (case-insensitive, catches lowercase Discord messages)
    re.compile(r"\n\s*\n", re.MULTILINE),                    # paragraph breaks
)

# ── Fuzzy word matching ────────────────────────────────────────────────────────

_FUZZY_CUTOFF = 0.82   # SequenceMatcher ratio threshold
_MIN_FUZZY_LEN = 4     # don't fuzz very short words — too ambiguous


def _tokenise(text: str) -> list[str]:
    """Return unique lowercase words (alpha only, length >= _MIN_FUZZY_LEN)."""
    return list({w for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= _MIN_FUZZY_LEN})


def _fuzzy_hits(words: list[str], keyword_set: frozenset[str]) -> int:
    """Count how many words fuzzy-match at least one keyword (exact first, then fuzzy)."""
    # Single-word members of the keyword set only (phrases handled separately)
    singles = [k for k in keyword_set if " " not in k]
    count = 0
    matched_keywords: set[str] = set()
    for word in words:
        if word in keyword_set:
            # Exact hit — mark whichever keyword it is
            matched_keywords.add(word)
            count += 1
            continue
        # Fuzzy — find the closest keyword not already matched
        candidates = [k for k in singles if k not in matched_keywords
                      and abs(len(k) - len(word)) <= 2]
        if candidates:
            m = get_close_matches(word, candidates, n=1, cutoff=_FUZZY_CUTOFF)
            if m:
                matched_keywords.add(m[0])
                count += 1
    return count


# ── Main classifier ────────────────────────────────────────────────────────────

def _classify(text: str) -> tuple[bool, float]:
    """Return (is_feedback, confidence) using local heuristics + fuzzy matching."""
    if len(text.strip()) < 30:
        return False, 0.0

    lower = text.lower()
    words = _tokenise(lower)
    score = 0.0

    # ── Axis 1: Domain relevance ──────────────────────────────────────────────

    # Short abbreviations checked by word-boundary regex (bypass 4-char minimum)
    abbrev_hits = sum(1 for pat in _RUST_ABBREV_PATTERNS if pat.search(lower))
    # Rust-specific single words (fuzzy)
    rust_word_hits = _fuzzy_hits(words, _RUST_SINGLES)
    # Rust multi-word phrases (exact substring)
    rust_phrase_hits = sum(1 for phrase in _RUST_PHRASES if phrase in lower)
    # General building/structural words (fuzzy, lower weight)
    building_hits = _fuzzy_hits(words, _BUILDING_SINGLES)

    domain_score = (
        min(abbrev_hits * 2.0, 4.0)
        + min(rust_word_hits * 2.5, 7.5)
        + min(rust_phrase_hits * 3.0, 6.0)
        + min(building_hits * 1.0, 3.0)
    )
    domain_hits = abbrev_hits + rust_word_hits + rust_phrase_hits

    # ── Axis 2: Feedback intent ───────────────────────────────────────────────

    # Opinion / suggestion words (fuzzy)
    intent_word_hits = _fuzzy_hits(words, _INTENT_SINGLES)
    # Critique / actionable patterns (regex)
    pattern_hits = sum(1 for pat in _CRITIQUE_PATTERNS if pat.search(lower))

    intent_score = min(intent_word_hits * 1.5, 4.5) + min(pattern_hits * 1.5, 7.5)
    intent_hits = intent_word_hits + pattern_hits

    # ── Structure bonus ───────────────────────────────────────────────────────

    structure_bonus = sum(1.5 for pat in _STRUCTURE_PATTERNS if pat.search(text))

    # ── Length bonus ──────────────────────────────────────────────────────────

    length_bonus = (1.0 if len(text) > 100 else 0.0) + (1.0 if len(text) > 250 else 0.0)

    score = domain_score + intent_score + structure_bonus + length_bonus

    # ── Two-axis gate ─────────────────────────────────────────────────────────
    # Both axes must register.  Long structured text with strong intent but
    # zero domain hits can still pass (e.g. a wall-of-text critique using only
    # general terms) — but the threshold is higher.

    if domain_hits == 0 and intent_hits == 0:
        return False, 0.0

    if domain_hits == 0:
        # Purely generic critique: require very high intent score + length
        if intent_score < 7 or len(text) < 150:
            return False, 0.0

    if intent_hits == 0:
        # Domain vocab with zero critique language = just chatting about the base
        return False, 0.0

    # ── Threshold ─────────────────────────────────────────────────────────────

    if score >= 8:
        return True, min(0.95, 0.72 + (score - 8) * 0.03)
    if score >= 5:
        return True, 0.60 + (score - 5) * 0.04
    return False, 0.0


# ── Discord UI ─────────────────────────────────────────────────────────────────

class FeedbackRatingModal(discord.ui.Modal, title="Rate this feedback"):
    reason = discord.ui.TextInput(
        label="Reason (optional)",
        placeholder="What made this feedback good or unhelpful?",
        required=False,
        max_length=500,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, rating: int, feedback_author_id: int, first_message_id: str,
                 channel_id: str, guild_id: int):
        super().__init__()
        self.rating = rating
        self.feedback_author_id = feedback_author_id
        self.first_message_id = first_message_id
        self.channel_id = channel_id
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        async with database.get_db() as db:
            await db.execute(
                """INSERT INTO FeedbackRatings
                   (feedback_message_id, channel_id, guild_id, feedback_author_id, rater_id, rating, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (self.first_message_id, self.channel_id, self.guild_id,
                 self.feedback_author_id, interaction.user.id, self.rating,
                 self.reason.value or None),
            )
            await db.commit()
        label = "quality feedback" if self.rating == 1 else "unhelpful feedback"
        await interaction.response.send_message(
            f"Thanks for rating — logged as **{label}**.", ephemeral=True
        )


class FeedbackRatingView(discord.ui.View):
    def __init__(self, feedback_author_id: int, first_message_id: str,
                 channel_id: str, guild_id: int):
        super().__init__(timeout=86400)  # 24 hours
        self.feedback_author_id = feedback_author_id
        self.first_message_id = first_message_id
        self.channel_id = channel_id
        self.guild_id = guild_id

    @discord.ui.button(label="👍  Quality Feedback", style=discord.ButtonStyle.green)
    async def rate_good(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id == self.feedback_author_id:
            await interaction.response.send_message(
                "You can't rate your own feedback.", ephemeral=True
            )
            return
        modal = FeedbackRatingModal(
            1, self.feedback_author_id, self.first_message_id,
            self.channel_id, self.guild_id,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="👎  Not Helpful", style=discord.ButtonStyle.red)
    async def rate_poor(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id == self.feedback_author_id:
            await interaction.response.send_message(
                "You can't rate your own feedback.", ephemeral=True
            )
            return
        modal = FeedbackRatingModal(
            -1, self.feedback_author_id, self.first_message_id,
            self.channel_id, self.guild_id,
        )
        await interaction.response.send_modal(modal)


# ── Cog ────────────────────────────────────────────────────────────────────────

class FeedbackDetector(commands.Cog):
    """Detects actionable feedback in monitored threads and prompts quality ratings."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # (guild_id, channel_id, user_id) -> {"messages": [Message], "task": Task}
        self._buffers: dict = {}
        # 30-min cooldown per (guild_id, channel_id, user_id) after a detection
        self._cooldowns: set = set()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.Thread):
            return
        if not message.content.strip():
            return
        if not message.guild:
            return

        guild_id = message.guild.id
        channel_id = message.channel.id
        parent_id = message.channel.parent_id
        user_id = message.author.id

        logging.debug(f"[FeedbackDetector] Message in thread {channel_id} (parent {parent_id}) from {message.author} in guild {guild_id}")

        try:
            async with database.get_db() as db:
                db.row_factory = aiosqlite.Row

                async with db.execute(
                    "SELECT 1 FROM MonitoredChannels WHERE guild_id = ? AND channel_id = ?",
                    (guild_id, parent_id),
                ) as cursor:
                    if not await cursor.fetchone():
                        logging.debug(f"[FeedbackDetector] parent {parent_id} not in MonitoredChannels — skipping")
                        return

                async with db.execute(
                    "SELECT feedback_detection FROM Settings WHERE guild_id = ?",
                    (guild_id,),
                ) as cursor:
                    row = await cursor.fetchone()
        except Exception as exc:
            logging.error(f"[FeedbackDetector] DB error in on_message: {exc}")
            return

        if not row or not row["feedback_detection"]:
            logging.debug(f"[FeedbackDetector] feedback_detection off for guild {guild_id} — skipping")
            return

        logging.info(f"[FeedbackDetector] Buffering message from {message.author} in thread {channel_id}")

        cooldown_key = (guild_id, channel_id, user_id)
        if cooldown_key in self._cooldowns:
            return

        key = (guild_id, channel_id, user_id)
        if key not in self._buffers:
            self._buffers[key] = {"messages": [], "task": None}

        entry = self._buffers[key]
        entry["messages"].append(message)

        if entry["task"] and not entry["task"].done():
            entry["task"].cancel()

        entry["task"] = asyncio.create_task(
            self._process_after_delay(key, message.channel, message.author)
        )

    async def _process_after_delay(
        self, key: tuple, channel: discord.Thread, author: discord.Member
    ):
        try:
            await asyncio.sleep(45)
        except asyncio.CancelledError:
            return

        entry = self._buffers.pop(key, None)
        if not entry or not entry["messages"]:
            return

        messages = entry["messages"]
        combined = "\n".join(
            m.content for m in messages if m.content.strip()
        )

        is_feedback, confidence = _classify(combined)
        logging.info(f"[FeedbackDetector] Classified {len(combined)}ch from {author}: is_feedback={is_feedback}, confidence={confidence:.2f}")

        if not is_feedback or confidence < 0.60:
            return

        guild_id, channel_id, user_id = key
        first_message = messages[0]

        cooldown_key = (guild_id, channel_id, user_id)
        self._cooldowns.add(cooldown_key)
        asyncio.create_task(self._clear_cooldown(cooldown_key, delay=1800))

        async with database.get_db() as db:
            await db.execute(
                """INSERT INTO DetectedFeedback
                   (first_message_id, channel_id, guild_id, author_id)
                   VALUES (?, ?, ?, ?)""",
                (str(first_message.id), str(channel_id), guild_id, user_id),
            )
            await db.commit()

        embed = discord.Embed(
            title="Feedback Detected",
            description=(
                f"{author.mention} just left feedback on this base.\n"
                "Rate the quality of their feedback below."
            ),
            color=0x5865F2,
        )
        embed.set_thumbnail(url=author.display_avatar.url)
        embed.set_footer(
            text="The Jantleman • Feedback Quality",
            icon_url=self.bot.user.display_avatar.url,
        )

        view = FeedbackRatingView(
            feedback_author_id=user_id,
            first_message_id=str(first_message.id),
            channel_id=str(channel_id),
            guild_id=guild_id,
        )

        try:
            await channel.send(embed=embed, view=view)
        except Exception as exc:
            logging.error(f"[FeedbackDetector] Failed to send rating embed: {exc}")

    async def _clear_cooldown(self, key: tuple, delay: int):
        await asyncio.sleep(delay)
        self._cooldowns.discard(key)


async def setup(bot: commands.Bot):
    await bot.add_cog(FeedbackDetector(bot))

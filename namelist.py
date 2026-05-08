"""
namelist.py — Curated lists of desirable Minecraft usernames to track.

Categories:
  • All 3-letter combinations (a-z, 0-9, _) — 50 653 names
  • Priority 3-letter real words — checked first
  • OG 4-5 letter words (animals, foods, items, cool words)
"""

from __future__ import annotations

import random

_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789_"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Priority 3-letter real words (checked first / more often)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PRIORITY_3CHAR: list[str] = sorted(set([
    # Animals
    "ant", "ape", "bat", "bee", "bug", "cat", "cod", "cow", "cub",
    "dog", "eel", "elk", "emu", "ewe", "fly", "fox", "gnu", "hen",
    "hog", "jay", "koi", "owl", "pig", "pug", "ram", "rat", "yak",
    # Foods & drinks
    "ale", "bun", "egg", "fig", "ham", "jam", "nut", "oat", "pea",
    "pie", "rye", "soy", "tea", "yam",
    # Items & objects
    "axe", "bag", "bed", "bow", "box", "bus", "cab", "can", "cap",
    "car", "cup", "fan", "gem", "gun", "hat", "ink", "jar", "jug",
    "key", "kit", "map", "mat", "mop", "mug", "net", "orb", "ore",
    "pad", "pan", "pen", "pin", "pot", "rod", "rug", "saw", "ski",
    "tab", "tin", "tub", "urn", "van", "vat", "wig",
    # Nature
    "ash", "bay", "bog", "bud", "dew", "elm", "fir", "fog", "ice",
    "ivy", "log", "mud", "oak", "sun", "tar", "wax",
    # Cool / OG
    "ace", "aim", "air", "arc", "ark", "arm", "art", "ban", "bar",
    "bit", "bot", "boy", "cop", "cry", "cut", "dam", "den", "dim",
    "dip", "dot", "dry", "dub", "duo", "dye", "ear", "ego", "end",
    "era", "eve", "eye", "fad", "foe", "fun", "fur", "gag", "gal",
    "gap", "gas", "gel", "gin", "god", "gum", "gut", "guy", "gym",
    "hex", "hip", "hit", "hop", "hot", "hub", "hue", "hug", "hum",
    "hut", "ill", "imp", "inn", "ion", "ire", "jab", "jag", "jaw",
    "jet", "jig", "job", "jog", "joy", "kid", "kin", "lab", "lad",
    "lag", "lap", "law", "lay", "leg", "let", "lid", "lip", "lit",
    "lot", "low", "lug", "lux", "mad", "max", "men", "mid", "mix",
    "mob", "mod", "nap", "new", "nil", "nip", "nit", "nod", "now",
    "nub", "odd", "ode", "ohm", "oil", "old", "one", "opt", "out",
    "owe", "own", "pal", "paw", "pay", "peg", "pep", "pet", "pit",
    "ply", "pod", "pop", "pow", "pro", "pry", "pub", "pun", "pup",
    "rag", "ran", "rap", "raw", "ray", "red", "ref", "rib", "rid",
    "rig", "rim", "rip", "rob", "rot", "row", "rub", "rum", "run",
    "rut", "sad", "sag", "sap", "set", "shy", "sin", "sip", "sir",
    "sit", "six", "sky", "sly", "sob", "sod", "son", "spa", "spy",
    "sty", "sub", "sum", "sup", "tag", "tan", "tap", "ten", "tic",
    "tie", "tip", "toe", "ton", "top", "tow", "toy", "try", "tug",
    "two", "use", "vet", "vex", "vim", "vow", "wad", "wag", "war",
    "way", "web", "wed", "wet", "who", "why", "win", "wit", "woe",
    "wok", "won", "woo", "wow", "yap", "yaw", "yea", "yes", "yet",
    "yew", "yin", "zap", "zen", "zig", "zip", "zoo",
]))

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# OG 4-5+ letter words
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

OG_WORDS: list[str] = sorted(set([
    # Animals
    "bear", "bird", "bull", "calf", "clam", "claw", "colt", "crab",
    "crow", "deer", "dove", "duck", "fawn", "fish", "flea", "foal",
    "frog", "goat", "gull", "hare", "hawk", "hive", "ibis", "kite",
    "lamb", "lark", "lion", "lynx", "mink", "mole", "moth", "mule",
    "newt", "pony", "puma", "seal", "slug", "stag", "swan", "toad",
    "wasp", "wolf", "worm", "wren", "tiger", "eagle", "raven",
    "shark", "whale", "snake", "viper", "cobra", "panther", "falcon",
    # Foods
    "bean", "beef", "beer", "cake", "chip", "chop", "corn", "crab",
    "diet", "dish", "feed", "fish", "fowl", "hash", "herb", "kale",
    "kiwi", "lamb", "lard", "lime", "loaf", "malt", "mayo", "meal",
    "meat", "melt", "milk", "mint", "miso", "naan", "oats", "peel",
    "pita", "plum", "pork", "rice", "roll", "sage", "salt", "seed",
    "soup", "stew", "taco", "tart", "tofu", "tuna", "veal", "vine",
    "wine", "wrap", "pizza", "sushi", "bread", "candy", "sugar",
    "honey", "apple", "mango", "grape", "lemon",
    # Items / objects
    "axle", "belt", "bolt", "bomb", "boot", "cape", "card", "case",
    "chip", "clip", "coin", "core", "dart", "desk", "dial", "disc",
    "door", "drum", "fang", "flag", "fork", "fuse", "gate", "gear",
    "grip", "hilt", "hood", "hook", "horn", "iron", "jade", "keel",
    "knob", "lamp", "lens", "lock", "loom", "mace", "mask", "nail",
    "opal", "orbs", "pawn", "pick", "pipe", "rack", "rail", "ring",
    "rope", "ruby", "sash", "silk", "slot", "tank", "tool", "veil",
    "wand", "whip", "wire", "yoke", "sword", "blade", "crown",
    "shield", "armor",
    # Cool / OG
    "acid", "aura", "bane", "beam", "bite", "blot", "blur", "bold",
    "bone", "boom", "burn", "calm", "char", "cold", "cool", "dark",
    "dawn", "doom", "dusk", "dust", "echo", "edge", "emit", "envy",
    "epic", "evil", "eyes", "face", "fade", "fame", "fang", "fate",
    "fear", "fire", "five", "flex", "flux", "foes", "fury", "gale",
    "gaze", "glow", "gold", "good", "gore", "grey", "grim", "grit",
    "halo", "hate", "haze", "heal", "hero", "hope", "howl", "hunt",
    "hype", "itch", "jinx", "just", "keep", "kill", "king", "knot",
    "lace", "lake", "lava", "laze", "life", "link", "lone", "lord",
    "lore", "lost", "luck", "lull", "luna", "lure", "lush", "mage",
    "main", "mars", "maze", "mind", "mint", "mist", "moon", "myth",
    "neon", "nope", "nova", "null", "omen", "onyx", "pain", "pale",
    "peak", "pity", "play", "pray", "pure", "quad", "quiz", "race",
    "rage", "raid", "rain", "rant", "rare", "raze", "rise", "risk",
    "rite", "ruin", "rune", "rush", "rust", "sage", "scar", "seer",
    "silk", "slay", "slow", "snow", "soar", "solo", "song", "soul",
    "sour", "star", "stop", "tale", "tang", "tech", "tide", "tomb",
    "tone", "torn", "true", "tune", "twin", "undo", "upon", "vain",
    "vast", "vibe", "vice", "void", "volt", "vows", "wail", "wake",
    "wane", "ward", "warm", "warp", "wave", "weak", "wild", "will",
    "wind", "wisp", "wish", "zero", "zest", "zone",
    # Popular MC / gaming
    "dream", "ghost", "flame", "frost", "night", "light", "shade",
    "storm", "flash", "blaze", "chaos", "steel", "stone", "smoke",
    "cyber", "pixel", "ninja", "titan", "beast", "demon", "angel",
    "omega", "alpha", "elite", "royal", "reign", "prime", "ultra",
    "hyper", "turbo", "swift", "rapid", "quick",
]))


def get_3char_all(shuffle: bool = True) -> list[str]:
    """Generate ALL valid 3-char Minecraft usernames (50 653 names)."""
    names = [a + b + c for a in _CHARS for b in _CHARS for c in _CHARS]
    if shuffle:
        random.shuffle(names)
    return names


def get_priority_names(shuffle: bool = True) -> list[str]:
    """Return the priority 3-char real words + OG 4+ letter words."""
    combined = list(set(PRIORITY_3CHAR + OG_WORDS))
    if shuffle:
        random.shuffle(combined)
    return combined


def get_all_tracked(shuffle: bool = True) -> list[str]:
    """Return ALL names: priority list first, then remaining 3-char combos."""
    priority = set(PRIORITY_3CHAR + OG_WORDS)
    remaining_3 = [n for n in get_3char_all(shuffle=False) if n not in priority]
    if shuffle:
        random.shuffle(remaining_3)
    combined = list(priority) + remaining_3
    if shuffle:
        # keep priority at the front but shuffle within each group
        p = list(priority)
        random.shuffle(p)
        combined = p + remaining_3
    return combined

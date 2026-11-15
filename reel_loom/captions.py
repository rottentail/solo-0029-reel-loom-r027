import json
import re
from .errors import require, missing
from .values import identifier, now, canonical, text
from .timebase import project_interval, stamp, TICKS_PER_SECOND

TIME = re.compile(r"^(\d{2,}):(\d{2}):(\d{2})[,.](\d{3})$")


def parse_stamp(value):
    match = TIME.fullmatch(value.strip())
    require(match is not None, "cue_time", "Invalid subtitle timestamp")
    h, m, s, ms = map(int, match.groups())
    require(m < 60 and s < 60, "cue_time", "Subtitle minute or second is invalid")
    return ((h*60+m)*60+s)*TICKS_PER_SECOND + ms*1000


def parse_srt(body, duration):
    require(isinstance(body, str) and len(body.encode()) <= 2_000_000,
            "caption_size", "Subtitle input exceeds limit")
    blocks = re.split(r"\n\s*\n", body.lstrip("\ufeff").replace("\r\n", "\n").strip())
    require(len(blocks) <= 10_000, "cue_limit", "Too many subtitle cues")
    cues = []
    for block in blocks:
        lines = block.splitlines()
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        require(len(lines) >= 2 and " --> " in lines[0], "cue_format", "Invalid SRT cue")
        left, right = lines[0].split(" --> ", 1)
        start, end = parse_stamp(left), parse_stamp(right)
        require(0 <= start < end <= duration, "cue_bounds", "Subtitle lies outside source media")
        caption = "\n".join(lines[1:]).strip()
        require(bool(caption) and len(caption) <= 2000, "cue_text", "Invalid subtitle text")
        require(not cues or start >= cues[-1]["start_tick"], "cue_order", "Subtitles must be in start-time order")
        cues.append({"start_tick": start, "end_tick": end, "text": caption})
    return cues


def language_slug(language):
    """Filesystem-safe label derived from a language tag; uniqueness of slugs is enforced by callers."""
    slug = re.sub(r"[^a-z0-9.-]+", "-", language.lower()).strip("-.")
    return slug or "language"


def render_srt(cues):
    return "\n\n".join(f'{i+1}\n{stamp(c["start_tick"])} --> {stamp(c["end_tick"])}\n{c["text"]}'
                       for i, c in enumerate(cues)) + ("\n" if cues else "")


def merge_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


class Captions:
    def __init__(self, db, assets, reels):
        self.db, self.assets, self.reels = db, assets, reels

    def import_track(self, asset_id, language, body):
        language = text(language, "language", 40)
        with self.db.write_unit() as db:
            asset = self.assets.get(asset_id, db, ready=True)
            cues = parse_srt(body, asset["metadata"]["duration_tick"])
            track_id = identifier("cue")
            db.execute("INSERT INTO cue_tracks VALUES(?,?,?,?,?)",
                       (track_id, asset_id, language, canonical(cues), now()))
            self.db.event(db, asset_id, "cues_imported", {"track": track_id, "count": len(cues)})
            return self.get(track_id, db)

    def get(self, track_id, db=None):
        if db is None:
            with self.db.read() as read:
                return self.get(track_id, read)
        row = db.execute("SELECT * FROM cue_tracks WHERE id=?", (track_id,)).fetchone()
        if row is None:
            raise missing("cue track", track_id)
        return {**dict(row), "cues": json.loads(row["cues"])}

    def _project_track(self, reel, track):
        slices = [item for item in reel["document"]["slices"] if item["asset"] == track["asset"]]
        require(slices, "unrelated_track", "Cue track does not belong to this reel")
        slice_indexes = [index for index, item in enumerate(reel["document"]["slices"])
                         if item["asset"] == track["asset"]]
        projected = []
        for cue in track["cues"]:
            for interval in project_interval(cue["start_tick"], cue["end_tick"], slices):
                # project_interval indexes the asset-filtered list; report the index in the whole reel.
                interval["slice"] = slice_indexes[interval["slice"]]
                projected.append({**interval, "text": cue["text"], "track": track["id"], "language": track["language"]})
        projected.sort(key=lambda x: (x["start_tick"], x["end_tick"], x["text"]))
        return projected

    def _coverage(self, reel, cues):
        """Per-slice coverage against the frozen program timeline; missing segments name the slice."""
        covered = merge_intervals([(c["start_tick"], c["end_tick"]) for c in cues])
        slices, missing_segments = [], []
        for index, item in enumerate(reel["document"]["slices"]):
            start, end = item["at_tick"], item["at_tick"] + item["out_tick"] - item["in_tick"]
            gaps = []
            cursor = start
            for cover_start, cover_end in covered:
                gap_start, gap_end = max(cursor, start), min(cover_start, end)
                if gap_start < gap_end:
                    gaps.append({"start_tick": gap_start, "end_tick": gap_end})
                cursor = max(cursor, min(cover_end, end))
            if cursor < end:
                gaps.append({"start_tick": cursor, "end_tick": end})
            duration = end-start
            covered_ticks = sum(min(end, b)-max(start, a) for a, b in covered if b > start and a < end)
            entry = {"slice": index, "label": item["label"], "asset": item["asset"],
                     "start_tick": start, "end_tick": end,
                     "covered_tick": covered_ticks, "missing_tick": duration-covered_ticks,
                     "fully_covered": not gaps, "missing_segments": gaps}
            slices.append(entry)
            for gap in gaps:
                missing_segments.append({"slice": index, "label": item["label"], **gap})
        return {"slices": slices, "missing_segments": missing_segments,
                "fully_covered": not missing_segments}

    def project(self, reel_id, revision, track_ids):
        require(isinstance(track_ids, list) and 1 <= len(track_ids) <= 64, "track_count", "Expected 1..64 cue tracks")
        require(len(track_ids) == len(set(track_ids)), "duplicate_track", "Cue tracks cannot repeat")
        with self.db.read() as db:
            reel = self.reels.get(reel_id, revision, db)
            projected = []
            for track_id in track_ids:
                projected.extend(self._project_track(reel, self.get(track_id, db)))
            projected.sort(key=lambda x: (x["start_tick"], x["end_tick"], x["track"], x["text"]))
            return {"reel": reel_id, "revision": reel["revision"], "reel_hash": reel["hash"], "cues": projected,
                    "srt": render_srt(projected)}

    def _language_groups(self, reel, db, groups):
        """Validate grouped delivery input and project every group onto the same program timeline."""
        require(isinstance(groups, list) and 1 <= len(groups) <= 64, "group_count", "Expected 1..64 caption groups")
        seen_languages, seen_slugs, seen_tracks = set(), set(), set()
        projected_groups = []
        for raw in groups:
            require(isinstance(raw, dict), "invalid_group", "Each caption group must be an object")
            require(set(raw) <= {"language", "tracks"}, "unknown_fields", "Unknown caption group fields",
                    fields=sorted(set(raw) - {"language", "tracks"}))
            language = text(raw.get("language"), "language", 40)
            track_ids = raw.get("tracks")
            require(isinstance(track_ids, list) and 1 <= len(track_ids) <= 64,
                    "track_count", "Each caption group requires 1..64 cue tracks")
            require(language not in seen_languages, "duplicate_language", "Caption languages cannot repeat",
                    language=language)
            seen_languages.add(language)
            slug = language_slug(language)
            require(slug not in seen_slugs, "language_name_collision",
                    "Caption languages resolve to the same package file name", language=language)
            seen_slugs.add(slug)
            cue_list = []
            for track_id in track_ids:
                require(isinstance(track_id, str), "invalid_tracks", "Cue tracks must be identifiers")
                require(track_id not in seen_tracks, "duplicate_track", "Cue tracks cannot repeat across languages")
                seen_tracks.add(track_id)
                cue_list.append(track_id)
            cues = []
            track_refs = []
            for track_id in cue_list:
                track = self.get(track_id, db)
                require(track["language"] == language, "language_mismatch",
                        "Cue track language does not match its caption group",
                        track=track_id, expected=language, actual=track["language"])
                cues.extend(self._project_track(reel, track))
                track_refs.append({"track": track_id, "asset": track["asset"]})
            cues.sort(key=lambda x: (x["start_tick"], x["end_tick"], x["text"]))
            coverage = self._coverage(reel, cues)
            projected_groups.append({"language": language, "file": f"captions/{slug}.srt",
                                     "tracks": track_refs, "cue_count": len(cues),
                                     "cues": cues, "srt": render_srt(cues), "coverage": coverage})
        return projected_groups

    def preview(self, reel_id, revision, groups):
        """Roll-level caption preview: per-language cues and slice coverage before hand-off."""
        with self.db.read() as db:
            reel = self.reels.get(reel_id, revision, db)
            projected_groups = self._language_groups(reel, db, groups)
            return {"reel": reel_id, "revision": reel["revision"], "reel_hash": reel["hash"],
                    "duration_tick": reel["document"]["duration_tick"],
                    "slices": [{"slice": index, "label": item["label"], "asset": item["asset"],
                                "start_tick": item["at_tick"],
                                "end_tick": item["at_tick"]+item["out_tick"]-item["in_tick"]}
                               for index, item in enumerate(reel["document"]["slices"])],
                    "languages": projected_groups}

    def project_groups(self, reel_id, revision, groups):
        with self.db.read() as db:
            reel = self.reels.get(reel_id, revision, db)
            return reel, self._language_groups(reel, db, groups)

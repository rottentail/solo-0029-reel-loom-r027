"""A bounded production walkthrough using generated audiovisual media and HTTP."""
import argparse
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import zipfile
from .config import Settings
from .application import Application
from .api import Api
from .http import MediaHttpServer
from .client import Client
from .errors import LoomError, require

WORKFLOWS = ("ingest", "revisions", "looks", "render", "signal", "waveform", "loudness",
             "sheet", "captions", "imprint", "multilingual-import", "cancel-retry", "recovery",
             "resources", "matrix", "conformance", "lineage")


def expect_error(action, code):
    try:
        action()
    except LoomError as exc:
        require(exc.code == code, "walkthrough_error", "Unexpected workflow rejection", expected=code, actual=exc.code)
        return exc.as_dict()
    raise RuntimeError("The walkthrough expected an operation to be rejected: " + code)


def reel_duration(slices):
    # Mirrors Reels.normalize: ordered half-open source intervals accumulate onto the program timeline.
    from fractions import Fraction
    return sum(round((Fraction(str(item["out"])) - Fraction(str(item["in"]))) * 1_000_000) for item in slices)


class Walkthrough:
    def __init__(self, settings):
        self.settings = settings
        self.app = Application(settings, start_workers=False)
        self.server = MediaHttpServer(("127.0.0.1", 0), Api(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()
        self.client = Client(f"http://127.0.0.1:{self.server.server_port}/loom/v1")

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.app.close()

    def setup(self):
        with self.app.blobs.temporary(".mp4") as sample:
            self.app.processes.run(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=3",
                                    "-f", "lavfi", "-i", "sine=frequency=659:sample_rate=48000:duration=3",
                                    "-af", "volume=enable='between(t,1,1.5)':volume=0",
                                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(sample)])
            self.content = sample.read_bytes()
        self.asset = self.client.ingest(self.content, "harbor-tone")
        self.slices = [{"asset": self.asset["id"], "in": .2, "out": 1.2, "label": "开场"},
                       {"asset": self.asset["id"], "in": 1.6, "out": 2.8, "label": "收束"}]
        self.reel = self.client.create_reel("回声样片", self.slices, "demo-reel")
        self.look = self.client.put_look("preview", {"kind": "video", "width": 320, "height": 180})
        self.audio_look = self.client.put_look("listening", {"kind": "audio", "sample_rate": 48000})
        self.ticket = self.client.render(self.reel["id"], "preview", key="demo-render")
        return self

    def finish(self):
        ticket = self.client.request("GET", "/tickets/"+self.ticket["id"])
        if ticket["state"] == "queued":
            self.app.renderer.run_once()
        ticket = self.client.wait(self.ticket["id"], timeout=5)
        require(ticket["state"] == "succeeded", "walkthrough_render", "Walkthrough render did not succeed", ticket=ticket)
        return ticket

    def run(self, name):
        c, a = self.client, self.asset["id"]
        if name == "ingest":
            duplicate = c.ingest(self.content, "duplicate-name")
            require(duplicate["id"] == a, "walkthrough_dedup", "Identical ready media should be reused")
            failure = expect_error(lambda: c.ingest(b"not-media", "bad"), "unreadable_media")
            return {"asset": self.asset, "deduplicated": True, "failure": failure}
        if name == "revisions":
            updated = c.replace_reel(self.reel["id"], 1, "回声样片·修订", list(reversed(self.slices)))
            old = c.request("GET", "/reels/"+self.reel["id"]+"?revision=1")
            require(old["hash"] == self.reel["hash"] and updated["revision"] == 2,
                    "walkthrough_revision", "Historical reel changed")
            failure = expect_error(lambda: c.replace_reel(self.reel["id"], 1, "旧稿", self.slices), "revision_conflict")
            return {"head": updated["head"], "historical_hash": old["hash"], "failure": failure}
        if name == "looks":
            new = c.put_look("preview", {"kind": "video", "width": 640, "height": 360}, 1)
            ticket = c.request("GET", "/tickets/"+self.ticket["id"])
            require(ticket["look_version"] == 1 and new["version"] == 2, "walkthrough_look", "Queued look drifted")
            failure = expect_error(lambda: c.put_look("preview", {"kind":"video"}, 1), "revision_conflict")
            return {"new_version": 2, "frozen_version": ticket["look_version"], "failure": failure}
        if name == "render":
            ticket = self.finish()
            body = {"reel": self.reel["id"], "look": "preview"}
            replay = c.request("POST", "/tickets", body, {"Idempotency-Key":"demo-render"})
            require(replay["id"] == ticket["id"], "walkthrough_idempotency", "Render replay changed identity")
            failure = expect_error(lambda: c.request("POST", "/tickets", {**body,"look":"listening"},
                                                   {"Idempotency-Key":"demo-render"}), "idempotency_conflict")
            audio = c.render(self.reel["id"], "listening")
            self.app.renderer.run_once()
            audio = c.wait(audio["id"], 5)
            require(audio["state"] == "succeeded", "walkthrough_audio", "Audio rendition did not succeed")
            return {"video": ticket, "audio": audio, "failure": failure}
        if name in ("signal", "waveform", "loudness"):
            result = c.request("POST", f"/assets/{a}/{name}", {})
            replay = c.request("POST", f"/assets/{a}/{name}", {})
            require(result["id"] == replay["id"], "walkthrough_analysis", "Analysis cache should be content/settings stable")
            failure = expect_error(lambda: c.request("POST", f"/assets/{a}/{name}", {"unsupported": 1}), "unknown_fields")
            return {"analysis": result, "cached": True, "failure": failure}
        if name == "sheet":
            result = c.request("POST", f"/assets/{a}/contact-sheet", {"count": 6, "columns": 3})
            media = c.request("GET", "/artifacts/"+result["id"]+"/content", raw=True)
            require(media.startswith(b"\xff\xd8"), "walkthrough_sheet", "Contact sheet is not JPEG")
            failure = expect_error(lambda: c.request("POST", f"/assets/{a}/contact-sheet", {"count":1,"columns":3}), "sheet_layout")
            return {"artifact": result, "bytes": len(media), "failure": failure}
        if name == "captions":
            track = c.request("POST", f"/assets/{a}/cues", {"language":"zh-CN",
                              "srt":"1\n00:00:00,000 --> 00:00:02,900\n潮声\n"})
            projected = c.request("POST", f'/reels/{self.reel["id"]}/captions', {"revision":1,"tracks":[track["id"]]})
            require(len(projected["cues"]) == 2, "walkthrough_captions", "A cue spanning cuts must project to both slices")
            failure = expect_error(lambda: c.request("POST", f"/assets/{a}/cues", {"language":"zh","srt":"broken"}), "cue_format")
            return {"projection": projected, "track": track, "failure": failure}
        if name == "imprint":
            failure = expect_error(lambda: c.imprint(self.ticket["id"]), "imprint_state")
            ticket = self.finish()
            imprint = c.imprint(ticket["id"])
            verified = c.request("GET", f'/imprints/{imprint["id"]}/verify')
            self.client.replace_reel(self.reel["id"], 1, "新的草剪", list(reversed(self.slices)))
            unchanged = c.imprint(ticket["id"])
            require(imprint["id"] == unchanged["id"], "walkthrough_imprint", "Upstream editing changed frozen imprint")
            return {"imprint": imprint, "verified": verified, "failure": failure}
        if name == "multilingual-import":
            # Two source media: the same finished program repeats and trims intervals from each source.
            with self.app.blobs.temporary(".mp4") as sample:
                self.app.processes.run(["-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=3",
                                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
                                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                                        "-shortest", str(sample)])
                second_bytes = sample.read_bytes()
            second = c.ingest(second_bytes, "harbor-echo")
            b = second["id"]
            multi_slices = [{"asset": self.asset["id"], "in": .2, "out": 1.2, "label": "开场"},
                            {"asset": b, "in": .4, "out": 1.4, "label": "中段"},
                            {"asset": self.asset["id"], "in": 1.6, "out": 2.8, "label": "收束"}]
            reel = c.create_reel("多语交接样片", multi_slices, "demo-multilingual-reel")
            zh_srt = ("1\n00:00:00,500 --> 00:00:01,800\n第一段中文\n\n"
                      "2\n00:00:02,000 --> 00:00:02,600\n末段中文\n")
            en_srt = ("1\n00:00:00,500 --> 00:00:01,800\nFirst line in English\n\n"
                      "2\n00:00:02,000 --> 00:00:02,600\nClosing line in English\n")
            ja_srt = "1\n00:00:00,500 --> 00:00:01,300\n日本語の字幕\n"
            zh = c.request("POST", f"/assets/{a}/cues", {"language": "zh-CN", "srt": zh_srt})
            en = c.request("POST", f"/assets/{a}/cues", {"language": "en", "srt": en_srt})
            ja = c.request("POST", f"/assets/{b}/cues", {"language": "ja", "srt": ja_srt})
            groups = [{"language": "zh-CN", "tracks": [zh["id"]]},
                      {"language": "en", "tracks": [en["id"]]},
                      {"language": "ja", "tracks": [ja["id"]]}]

            # 1) Roll preview: producers see coverage gaps per slice before any hand-off.
            preview = c.caption_preview(reel["id"], groups, revision=1)
            by_language = {item["language"]: item for item in preview["languages"]}
            require(preview["duration_tick"] == reel_duration(multi_slices),
                    "walkthrough_preview", "Preview timeline differs from the frozen reel")
            require(by_language["zh-CN"]["coverage"]["fully_covered"] is False,
                    "walkthrough_preview", "Coverage must report gaps rather than silently dropping them")
            require(by_language["ja"]["coverage"]["slices"][2]["fully_covered"] is False
                    and by_language["ja"]["coverage"]["slices"][2]["covered_tick"] == 0
                    and by_language["ja"]["coverage"]["slices"][0]["covered_tick"] == 0
                    and by_language["ja"]["coverage"]["slices"][1]["covered_tick"] > 0,
                    "walkthrough_preview", "The Japanese track should cover only the slices its source feeds")
            missing_slice = next(item for item in by_language["ja"]["coverage"]["missing_segments"] if item["slice"] == 1)
            require(missing_slice["label"] == "中段",
                    "walkthrough_preview", "Missing segment must name the slice that lacks cues")
            preview_failures = [
                expect_error(lambda: c.caption_preview(reel["id"], [{"language": "en", "tracks": [zh["id"]]}]),
                             "language_mismatch"),
                expect_error(lambda: c.caption_preview(reel["id"], groups + [{"language": "en", "tracks": [en["id"]]}]),
                             "duplicate_language"),
                expect_error(lambda: c.caption_preview(reel["id"], [{"language": "en", "tracks": [zh["id"], zh["id"]]}]),
                             "duplicate_track"),
            ]

            # 2) A frozen render on the same reel; packaging never re-encodes the unchanged picture.
            ticket = c.render(reel["id"], "preview", key="demo-multilingual-render")
            while self.app.renderer.run_once() is not None:
                pass
            ticket = c.wait(ticket["id"], 5)
            require(ticket["state"] == "succeeded", "walkthrough_render", "Multi-language render did not succeed")
            media_blob = ticket["artifacts"][0]["blob"]

            # 3) Multi-language imprint: one SRT per language, all sharing the program timeline.
            imprint = c.imprint(ticket["id"], caption_groups=groups)
            manifest = imprint["manifest"]
            require([item["language"] for item in manifest["captions"]] == ["zh-CN", "en", "ja"],
                    "walkthrough_imprint", "Imprint must keep one independent package entry per language")
            require(all(item["file"].startswith("captions/") and item["file"].endswith(".srt")
                        for item in manifest["captions"]), "walkthrough_imprint", "Caption file names must identify the language")
            package = zipfile.ZipFile(io.BytesIO(c.request("GET", f"/imprints/{imprint['id']}/content", raw=True)))
            names = set(package.namelist())
            require(names == {"manifest.json", ticket["artifacts"][0]["id"] + ".mp4",
                              "captions/zh-cn.srt", "captions/en.srt", "captions/ja.srt"},
                    "walkthrough_imprint", "Package members do not match the frozen manifest")
            zh_text = package.read("captions/zh-cn.srt").decode()
            require(zh_text.count(" --> ") == 3 and "00:00:00,300 --> 00:00:01,000" in zh_text
                    and "00:00:02,000 --> 00:00:02,200" in zh_text,
                    "walkthrough_imprint", "Repeated source intervals must each project onto the program timeline")
            require(package.read("captions/en.srt").decode().count(" --> ") == 3
                    and package.read("captions/ja.srt").decode().count(" --> ") == 1,
                    "walkthrough_imprint", "Languages must stay in separate tracks instead of one merged stream")

            # 4) Imprint verification re-reads every packaged caption and the unchanged media bytes.
            verified = c.request("GET", f'/imprints/{imprint["id"]}/verify')
            require(verified["valid"] and len(verified["verified_captions"]) == 3
                    and {item["language"] for item in verified["verified_captions"]} == {"zh-CN", "en", "ja"},
                    "walkthrough_imprint", "Verification did not cover every packaged language")
            require(next(item for item in verified["verified_captions"] if item["language"] == "ja")["fully_covered"] is False
                    and next(item for item in verified["verified_captions"] if item["language"] == "zh-CN")["fully_covered"] is False,
                    "walkthrough_imprint", "Verification must retain the coverage warnings seen in the preview")

            # 5) Completing the Japanese track yields a new imprint; the historical package and bytes stay untouched.
            ja_full = c.request("POST", f"/assets/{b}/cues",
                                {"language": "ja",
                                 "srt": ja_srt + "\n2\n00:00:01,000 --> 00:00:01,350\n追加された日本語\n"})
            completed_groups = [{"language": "zh-CN", "tracks": [zh["id"]]},
                                {"language": "en", "tracks": [en["id"]]},
                                {"language": "ja", "tracks": [ja_full["id"]]}]
            new_imprint = c.imprint(ticket["id"], caption_groups=completed_groups)
            require(new_imprint["id"] != imprint["id"] and new_imprint["blob"] != imprint["blob"],
                    "walkthrough_imprint", "Caption completion must create a new frozen delivery")
            historical = c.request("GET", f"/imprints/{imprint['id']}/verify")
            require(historical["valid"], "walkthrough_imprint", "Historical imprint must remain verifiable")
            old_package = zipfile.ZipFile(io.BytesIO(c.request("GET", f"/imprints/{imprint['id']}/content", raw=True)))
            require(old_package.read("captions/ja.srt").decode().count(" --> ") == 1,
                    "walkthrough_imprint", "Historical package content changed after re-delivery")
            new_manifest = new_imprint["manifest"]
            require(new_manifest["files"][0]["blob"] == media_blob,
                    "walkthrough_imprint", "Re-delivery must reuse the unchanged rendition instead of re-encoding")
            new_verify = c.request("GET", f"/imprints/{new_imprint['id']}/verify")
            require(new_verify["valid"], "walkthrough_imprint", "The replacement delivery must verify")

            # 6) Legacy single-language call shape still works and stays a merged captions.srt.
            legacy = c.imprint(ticket["id"], cue_tracks=[zh["id"]])
            legacy_manifest = legacy["manifest"]
            require("captions" not in legacy_manifest and legacy_manifest["caption"] is not None,
                    "walkthrough_imprint", "Legacy cue_tracks delivery must keep its original manifest shape")
            legacy_package = zipfile.ZipFile(io.BytesIO(c.request("GET", f"/imprints/{legacy['id']}/content", raw=True)))
            require("captions.srt" in legacy_package.namelist()
                    and "captions/zh-CN.srt" not in legacy_package.namelist(),
                    "walkthrough_imprint", "Legacy packaging layout must remain unchanged")
            mixed_failure = expect_error(
                lambda: c.imprint(ticket["id"], cue_tracks=[zh["id"]], caption_groups=groups), "invalid_body")
            return {"preview": preview, "imprint": imprint, "verified": verified,
                    "new_imprint": new_imprint, "legacy_imprint": legacy,
                    "reused_media_blob": media_blob, "preview_failures": preview_failures,
                    "failure": mixed_failure}
        if name == "cancel-retry":
            cancelled = c.request("POST", f'/tickets/{self.ticket["id"]}/cancel')
            require(cancelled["state"] == "cancelled", "walkthrough_cancel", "Queued render did not cancel")
            retried = c.request("POST", f'/tickets/{self.ticket["id"]}/retry')
            ticket = self.finish()
            failure = expect_error(lambda: c.request("POST", f'/tickets/{self.ticket["id"]}/retry'), "retry_state")
            require(retried["attempt"] == 2, "walkthrough_attempt", "Retry attempt was not incremented")
            return {"ticket": ticket, "failure": failure}
        if name == "recovery":
            claimed = self.app.tickets.claim()
            self.close()
            with Application(self.settings, start_workers=False) as restarted:
                recovered = restarted.tickets.get(claimed["id"])
                require(recovered["state"] == "failed" and recovered["error"]["error"] == "worker_interrupted",
                        "walkthrough_recovery", "Interrupted render was not recovered")
                restarted.tickets.retry(claimed["id"])
                finished = restarted.renderer.run_once()
                require(finished["state"] == "succeeded", "walkthrough_recovery", "Recovered render could not retry")
                return {"interrupted": recovered, "retried_state": finished["state"]}
        if name == "resources":
            c.request("POST", f"/assets/{a}/retire")
            failure = expect_error(lambda: c.create_reel("退役来源", self.slices), "asset_retired")
            ticket = self.finish()
            sweep = c.request("POST", "/resources/sweep", {"dry_run":False})
            status = c.request("GET", "/resources")
            require(status["consistent"] and not sweep["removed"], "walkthrough_resources", "Referenced source was collected")
            return {"reconcile":status,"sweep":sweep,"render_state":ticket["state"],"failure":failure}
        if name == "matrix":
            body = {"reel":self.reel["id"],"looks":[{"id":"preview"},{"id":"listening"}]}
            matrix = c.request("POST", "/matrices", body, {"Idempotency-Key":"demo-matrix"})
            before = len(c.request("GET", "/tickets")["items"])
            failure = expect_error(lambda: c.request("POST", "/matrices", {**body,"looks":[{"id":"preview"},{"id":"missing-look"}]}), "not_found")
            require(len(c.request("GET", "/tickets")["items"]) == before, "walkthrough_matrix", "Invalid matrix committed partial work")
            while self.app.renderer.run_once() is not None:
                pass
            finished = c.request("GET", "/matrices/"+matrix["id"])
            require(finished["state"] == "succeeded", "walkthrough_matrix", "Matrix did not complete")
            return {"matrix": finished,"failure":failure}
        if name == "conformance":
            ticket = self.finish()
            artifact = ticket["artifacts"][0]
            result = c.request("GET", f'/artifacts/{artifact["id"]}/conformance')
            require(result["valid"], "walkthrough_conformance", "Rendered media violates frozen look", report=result)
            failure = expect_error(lambda: c.request("GET", "/artifacts/missing/conformance"), "not_found")
            return {"conformance":result,"failure":failure}
        if name == "lineage":
            self.finish()
            imprint = c.imprint(self.ticket["id"])
            result = c.request("GET", f'/tickets/{self.ticket["id"]}/lineage')
            require(any(n["id"] == imprint["id"] for n in result["nodes"]), "walkthrough_lineage", "Imprint missing from lineage")
            failure = expect_error(lambda: c.request("GET", "/tickets/missing/lineage"), "not_found")
            return {"lineage":result,"failure":failure}
        raise ValueError("Unknown walkthrough: " + name)


def main():
    parser = argparse.ArgumentParser(description="Run a bounded audiovisual production walkthrough")
    parser.add_argument("--workflow", choices=WORKFLOWS, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="reel-loom-demo-") as folder:
        settings = replace(Settings.from_env(), root=Path(folder), render_timeout=30)
        demo = Walkthrough(settings)
        try:
            demo.setup()
            result = demo.run(args.workflow)
            print(json.dumps({"workflow":args.workflow,"ok":True,"result":result}, ensure_ascii=False))
        finally:
            if args.workflow != "recovery":
                demo.close()


if __name__ == "__main__":
    main()
